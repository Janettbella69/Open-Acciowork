"""Gold 标注抽样逻辑（gold_sampling.py）：候选条目抽取、分层切分、标签存储。

抽样只做确定性启发式抽取，绝不调用模型；gold 标签本身由负责人经标注界面写入。
"""

from __future__ import annotations

import json

from gold_sampling import (
    JUDGES,
    LabelStore,
    assign_splits,
    build_samples,
    extract_candidates,
    has_data_point,
    item_id_for,
    split_sentences,
)


def make_run_view(**overrides) -> dict:
    view = {
        "run_id": "run_test0001",
        "task": "b-product-validation",
        "final_text": (
            "德国市场保温杯月搜索量约 45,000 次，来源见账本证据 ev_1。"
            "综合评分后 SUP-B（77.0 分）为首选供应商。"
            "据此推断，欧盟渠道适合首发（假设关税率 2.2%）。"
            "本报告基于合成 fixture。"
        ),
        "artifacts": [
            {
                "name": "validation-report.md",
                "text": "到岸成本为 $4.83/件（悲观情景 $5.41）。预计退货率 8%（估算值）。",
            }
        ],
        "evidence": [
            {
                "id": "ev_1",
                "kind": "community_signal",
                "claim": "Amazon.de 保温杯类目月搜索量约 45,000",
                "source": "url",
                "status": "active",
            },
            {
                "id": "ev_2",
                "kind": "inference",
                "claim": "供给端产能可支撑首单 5,000 件",
                "source": "NONE",
                "status": "active",
            },
            {
                "id": "ev_3",
                "kind": "observed_fact",
                "claim": "fixture 中 SUP-B 报价为 $3.20/件",
                "source": "path",
                "status": "active",
            },
            {
                "id": "ev_4",
                "kind": "observed_fact",
                "claim": "行业惯例账期为 30 天",
                "source": "NONE",
                "status": "active",
            },
        ],
    }
    view.update(overrides)
    return view


# --- 句子切分与数据点识别 ---


def test_split_sentences_handles_chinese_and_western_boundaries():
    text = "第一句有 45,000 次搜索。Second sentence has $4.83. 第三句！\n- 列表项 8% 退货率"
    sentences = split_sentences(text)
    assert "第一句有 45,000 次搜索" in sentences
    assert "Second sentence has $4.83" in sentences
    assert "第三句" in sentences
    assert "列表项 8% 退货率" in sentences


def test_split_sentences_drops_fragments_and_overlong():
    sentences = split_sentences("短。 " + "长" * 500 + "。 这一句长度正常可以保留。")
    assert "短" not in sentences
    assert all(len(s) <= 400 for s in sentences)
    assert "这一句长度正常可以保留" in sentences


def test_has_data_point_accepts_quantities_and_rejects_bare_dates_and_ids():
    assert has_data_point("月搜索量约 45,000 次")
    assert has_data_point("到岸成本 $4.83")
    assert has_data_point("退货率 8%")
    assert not has_data_point("报告生成于 2026-08-20")
    assert not has_data_point("详见 run_99841e2e 的记录")
    assert not has_data_point("没有数字的普通句子")


# --- 候选抽取：三个 judge 各自的判定单元 ---


def test_extract_unsourced_claims_candidates_cover_final_text_and_artifacts():
    items = extract_candidates(make_run_view(), judge="unsourced-claims")
    texts = [item["item"] for item in items]
    assert any("45,000" in t for t in texts)
    assert any("4.83" in t for t in texts)  # 来自 artifact 正文
    assert all(item["run_id"] == "run_test0001" for item in items)
    sources = {item["source"] for item in items}
    assert "final_text" in sources
    assert any(s.startswith("artifact:") for s in sources)


def test_extract_evidence_coverage_includes_keyword_claims_without_numbers():
    view = make_run_view(
        final_text="综合比较后 SUP-B 为首选供应商。出口欧盟需要 CE 认证。今天天气不错。",
        artifacts=[],
    )
    texts = [item["item"] for item in extract_candidates(view, judge="evidence-coverage")]
    assert any("首选" in t for t in texts)
    assert any("认证" in t for t in texts)
    assert not any("天气" in t for t in texts)


def test_extract_confusion_skips_hedged_sentences_and_adds_ledger_items():
    items = extract_candidates(make_run_view(), judge="fact-inference-confusion")
    texts = [item["item"] for item in items]
    # 正文中带对冲词（据此推断/假设/预计/估算）的句子不作正文候选
    assert not any("据此推断" in t for t in texts)
    # 无对冲词的断言句进入候选
    assert any("45,000" in t for t in texts)
    # 账本条目：inference / community_signal 全部进入；observed_fact 仅无来源的进入
    assert any(t.startswith("[账本 inference]") and "5,000" in t for t in texts)
    assert any(t.startswith("[账本 community_signal]") for t in texts)
    assert any(t.startswith("[账本 observed_fact]") and "账期" in t for t in texts)
    assert not any("3.20" in t for t in texts)  # 有来源的 observed_fact 不进


def test_extract_candidates_dedupes_identical_sentences():
    view = make_run_view(
        final_text="到岸成本 $4.83/件。到岸成本 $4.83/件。",
        artifacts=[],
        evidence=[],
    )
    items = extract_candidates(view, judge="unsourced-claims")
    assert len([i for i in items if "4.83" in i["item"]]) == 1


# --- 组批、切分与确定性 ---


def test_build_samples_caps_per_run_and_is_deterministic():
    views = [make_run_view(run_id=f"run_{i:04d}") for i in range(3)]
    first = build_samples(views, per_run_cap=3, seed=7)
    second = build_samples(views, per_run_cap=3, seed=7)
    assert first == second
    for judge in JUDGES:
        for view in views:
            per_run = [
                i for i in first["items"]
                if i["judge"] == judge and i["run_id"] == view["run_id"]
            ]
            assert len(per_run) <= 3
    assert set(first["runs"]) == {v["run_id"] for v in views}
    assert first["runs"]["run_0000"]["task"] == "b-product-validation"


def test_assign_splits_stratifies_4_3_3_per_judge():
    items = [
        {"item_id": f"i{j}{n}", "judge": judge, "run_id": "r", "item": f"x{n}"}
        for j, judge in enumerate(JUDGES)
        for n in range(10)
    ]
    assign_splits(items, seed=1)
    for judge in JUDGES:
        splits = [i["split"] for i in items if i["judge"] == judge]
        assert splits.count("train") == 4
        assert splits.count("dev") == 3
        assert splits.count("test") == 3
    again = [dict(i, split=None) for i in items]
    assign_splits(again, seed=1)
    assert [i["split"] for i in again] == [i["split"] for i in items]


def test_item_id_is_stable_and_distinct():
    a = item_id_for("unsourced-claims", "run_1", "到岸成本 $4.83")
    b = item_id_for("unsourced-claims", "run_1", "到岸成本 $4.83")
    c = item_id_for("evidence-coverage", "run_1", "到岸成本 $4.83")
    assert a == b != c


# --- LabelStore：负责人标签的落盘 ---


def test_label_store_upserts_by_item_id_and_survives_reload(tmp_path):
    store = LabelStore(tmp_path)
    record = {
        "item_id": "abc123",
        "judge": "unsourced-claims",
        "run_id": "run_1",
        "item": "到岸成本 $4.83",
        "label": False,
        "rationale": "无内联来源",
        "split": "train",
    }
    store.upsert(record)
    store.upsert({**record, "label": True, "rationale": "计算工具输入可回链"})

    lines = (tmp_path / "unsourced-claims" / "labels.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["label"] is True

    reloaded = LabelStore(tmp_path).load()
    assert reloaded["abc123"]["rationale"] == "计算工具输入可回链"


def test_label_store_keeps_judges_in_separate_files(tmp_path):
    store = LabelStore(tmp_path)
    store.upsert({"item_id": "a", "judge": "unsourced-claims", "run_id": "r",
                  "item": "x", "label": True, "rationale": "", "split": "dev"})
    store.upsert({"item_id": "b", "judge": "evidence-coverage", "run_id": "r",
                  "item": "y", "label": False, "rationale": "", "split": "test"})
    assert (tmp_path / "unsourced-claims" / "labels.jsonl").exists()
    assert (tmp_path / "evidence-coverage" / "labels.jsonl").exists()
    assert len(LabelStore(tmp_path).load()) == 2
