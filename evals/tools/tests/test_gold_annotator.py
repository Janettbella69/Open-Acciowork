"""GoldAnnotationService：样本+标签合并、落盘、跳过状态。"""

from __future__ import annotations

import json

import pytest

from gold_annotator import GoldAnnotationService


@pytest.fixture()
def service(tmp_path):
    samples = {
        "seed": 7,
        "per_run_cap": 6,
        "runs": {"run_1": {"task": "b-product-validation", "final_text": "到岸成本 $4.83。"}},
        "items": [
            {
                "item_id": "aaa111",
                "judge": "unsourced-claims",
                "run_id": "run_1",
                "item": "到岸成本 $4.83",
                "source": "final_text",
                "split": "train",
            },
            {
                "item_id": "bbb222",
                "judge": "fact-inference-confusion",
                "run_id": "run_1",
                "item": "[账本 inference] 产能可支撑首单",
                "source": "ledger:ev_2",
                "split": "test",
            },
        ],
    }
    samples_path = tmp_path / "batch-1.json"
    samples_path.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
    return GoldAnnotationService(samples_path, tmp_path / "gold")


def test_state_merges_labels_and_deferred(service):
    state = service.state()
    assert state["batch"] == "batch-1.json"
    assert {i["item_id"] for i in state["items"]} == {"aaa111", "bbb222"}
    assert all(not i["labeled"] and not i["deferred"] for i in state["items"])
    assert "unsourced-claims" in state["questions"]

    service.save_label("aaa111", label=False, rationale="无内联来源")
    service.toggle_defer("bbb222")
    state = service.state()
    by_id = {i["item_id"]: i for i in state["items"]}
    assert by_id["aaa111"]["labeled"] and by_id["aaa111"]["label"] is False
    assert by_id["aaa111"]["rationale"] == "无内联来源"
    assert by_id["bbb222"]["deferred"]


def test_save_label_writes_readme_schema_superset(service, tmp_path):
    record = service.save_label("aaa111", label=True, rationale="计算输入可回链")
    lines = (
        tmp_path / "gold" / "unsourced-claims" / "labels.jsonl"
    ).read_text(encoding="utf-8").strip().splitlines()
    saved = json.loads(lines[0])
    # gold/README.md 的最小 schema：run_id / item / label / rationale
    assert saved["run_id"] == "run_1"
    assert saved["item"] == "到岸成本 $4.83"
    assert saved["label"] is True
    assert saved["rationale"] == "计算输入可回链"
    # 校准需要的补充字段
    assert saved["split"] == "train"
    assert saved["judge"] == "unsourced-claims"
    assert saved["labeled_at"] == record["labeled_at"]


def test_labeling_clears_deferred_and_defer_toggles(service):
    assert service.toggle_defer("aaa111") is True
    service.save_label("aaa111", label=True, rationale="")
    assert not any(i["deferred"] for i in service.state()["items"])
    assert service.toggle_defer("bbb222") is True
    assert service.toggle_defer("bbb222") is False
    with pytest.raises(KeyError):
        service.toggle_defer("nope")
