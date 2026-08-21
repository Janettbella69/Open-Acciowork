"""Gold 标注抽样与标签存储（PRD §3.11：judge 校准的前置工序）。

评测系统自有代码，独立于主系统：本模块只用 stdlib，不 import ophira_backend；
主系统也不知道本目录的存在（单向边界见 evals/tools/README.md）。

只做两件事，且都不调用模型：
1. 从 L2 运行的可见文本 + 账本快照里，用确定性启发式抽取"候选标注条目"
   （每个 judge 的判定单元不同：数据点 / 关键事实陈述 / 疑似混淆句与账本条目）。
2. 落盘负责人给出的标签（`evals/gold/<judge>/labels.jsonl`，按 item_id 幂等 upsert）。

gold 标签必须由项目负责人本人给出——启发式只负责"把要判的条目摆到面前"，
判定本身发生在标注界面（evals/tools/annotate_gold.py）。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from pathlib import Path

JUDGES = ("evidence-coverage", "unsourced-claims", "fact-inference-confusion")

# 标注界面按 judge 展示的判定问题；label=true 的语义与各 judge 输出字段对齐
# （coverage→covered、unsourced→sourced/合规、confusion→确属混淆）。
JUDGE_QUESTIONS = {
    "evidence-coverage": (
        "这条关键事实性陈述是否被账本证据覆盖？"
        "（存在语义匹配的证据条目 + 有效来源与时间 + 陈述强度与证据类型相称）"
    ),
    "unsourced-claims": (
        "这个具体数据点是否有来源（正文内联或可回链账本证据），"
        "或已被明确标注为估算/假设/示例？（计算工具的派生数字视为有来源）"
    ),
    "fact-inference-confusion": (
        "这里是否把推断/代理信号当成了既成事实？"
        "（正文句：无限定词却仅有推断支撑；账本条目：kind 性质在正文引用中丢失，"
        "或 observed_fact 并非可核验观察）"
    ),
}

# 明确的推断/假设标注词——带这些词的句子不进 coverage/confusion 的正文候选
# （coverage 口径：标注为推断/假设的句子不计入分母；confusion 找的是"没标"的）。
HEDGE_MARKERS = ("推断", "假设", "估算", "预计", "可能", "待验证")

# 无数字但典型需要证据支撑的结论/要求类关键词（evidence-coverage 口径）。
CLAIM_KEYWORDS = (
    "首选", "推荐", "排名", "排序", "认证", "合规", "需求", "供给",
    "供应商", "首发", "最高", "最低", "优先", "结论",
)

_SENTENCE_BOUNDARY = re.compile(r"[。！？!?]|\.(?=\s|$)")
_LINE_MARKER = re.compile(r"^\s*(?:[-*+>]|#{1,6}|\d{1,2}[.)、])\s+")
_DATE_LIKE = re.compile(r"\d{4}[-/年]\s?\d{1,2}(?:[-/月]\s?\d{1,2}\s?日?)?|\d{1,2}:\d{2}")
_ID_LIKE = re.compile(r"\b(?:run|ev|project|decision|artifact|hyp)_[0-9a-zA-Z]+\b")


def split_sentences(text: str) -> list[str]:
    """按中英句界与行内列表标记切句；保留 2–400 字符的句子。"""
    sentences: list[str] = []
    for line in text.splitlines() or [text]:
        line = _LINE_MARKER.sub("", line)
        for piece in _SENTENCE_BOUNDARY.split(line):
            piece = piece.strip()
            if 2 <= len(piece) <= 400:
                sentences.append(piece)
    return sentences


def has_data_point(sentence: str) -> bool:
    """句中是否含"具体数据点"数字——排除纯日期/时间与 id 引用里的数字。"""
    stripped = _ID_LIKE.sub("", _DATE_LIKE.sub("", sentence))
    return bool(re.search(r"\d", stripped))


def _is_hedged(sentence: str) -> bool:
    return any(marker in sentence for marker in HEDGE_MARKERS)


def item_id_for(judge: str, run_id: str, item: str) -> str:
    digest = hashlib.sha1(f"{judge}|{run_id}|{item}".encode()).hexdigest()
    return digest[:12]


def _text_sources(view: dict) -> list[tuple[str, str]]:
    sources = [("final_text", view.get("final_text") or "")]
    for artifact in view.get("artifacts", []):
        sources.append((f"artifact:{artifact['name']}", artifact.get("text") or ""))
    return sources


def extract_candidates(view: dict, judge: str) -> list[dict]:
    """一个 run 里某 judge 的全部候选条目（保持阅读顺序，按条目文本去重）。"""
    run_id = view["run_id"]
    candidates: list[tuple[str, str]] = []  # (item_text, source)

    for source, text in _text_sources(view):
        for sentence in split_sentences(text):
            if judge == "unsourced-claims":
                keep = has_data_point(sentence)
            else:  # coverage 与 confusion 的正文候选口径一致：无对冲词的断言句
                keep = not _is_hedged(sentence) and (
                    has_data_point(sentence)
                    or any(k in sentence for k in CLAIM_KEYWORDS)
                )
            if keep:
                candidates.append((sentence, source))

    if judge == "fact-inference-confusion":
        for row in view.get("evidence", []):
            kind = row.get("kind")
            if kind in {"inference", "community_signal"} or (
                kind == "observed_fact" and row.get("source") == "NONE"
            ):
                candidates.append((f"[账本 {kind}] {row['claim']}", f"ledger:{row['id']}"))

    seen: set[str] = set()
    items: list[dict] = []
    for item_text, source in candidates:
        if item_text in seen:
            continue
        seen.add(item_text)
        items.append(
            {
                "item_id": item_id_for(judge, run_id, item_text),
                "judge": judge,
                "run_id": run_id,
                "item": item_text,
                "source": source,
            }
        )
    return items


def assign_splits(items: list[dict], seed: int) -> None:
    """按 judge 分层，4:3:3 确定性划分 train/dev/test（gold README 步骤 3）。"""
    for judge in JUDGES:
        stratum = sorted(
            (i for i in items if i["judge"] == judge), key=lambda i: i["item_id"]
        )
        if not stratum:
            continue
        random.Random(f"{seed}:{judge}").shuffle(stratum)
        n = len(stratum)
        quota = [n * 4 // 10, n * 3 // 10, n * 3 // 10]
        for index in range(n - sum(quota)):
            quota[index % 3] += 1
        bounds = (quota[0], quota[0] + quota[1])
        for position, item in enumerate(stratum):
            if position < bounds[0]:
                item["split"] = "train"
            elif position < bounds[1]:
                item["split"] = "dev"
            else:
                item["split"] = "test"


def build_samples(views: list[dict], *, per_run_cap: int, seed: int) -> dict:
    """把多个 run 的候选条目组成一批样本（确定性：同输入同 seed 结果一致）。"""
    items: list[dict] = []
    for judge in JUDGES:
        for view in views:
            candidates = extract_candidates(view, judge)
            if len(candidates) > per_run_cap:
                rng = random.Random(f"{seed}:{judge}:{view['run_id']}")
                chosen = sorted(rng.sample(range(len(candidates)), per_run_cap))
                candidates = [candidates[index] for index in chosen]
            items.extend(candidates)
    assign_splits(items, seed)
    runs = {
        view["run_id"]: {key: value for key, value in view.items() if key != "run_id"}
        for view in views
    }
    return {"seed": seed, "per_run_cap": per_run_cap, "runs": runs, "items": items}


class LabelStore:
    """`gold/<judge>/labels.jsonl` 的读写：按 item_id 幂等 upsert，原子重写。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path_for(self, judge: str) -> Path:
        return self.root / judge / "labels.jsonl"

    def _load_judge(self, judge: str) -> dict[str, dict]:
        path = self._path_for(judge)
        records: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    records[record["item_id"]] = record
        return records

    def load(self) -> dict[str, dict]:
        merged: dict[str, dict] = {}
        judges = set(JUDGES)
        if self.root.exists():
            judges.update(p.parent.name for p in self.root.glob("*/labels.jsonl"))
        for judge in sorted(judges):
            merged.update(self._load_judge(judge))
        return merged

    def upsert(self, record: dict) -> None:
        judge = record["judge"]
        records = self._load_judge(judge)
        records[record["item_id"]] = record
        path = self._path_for(judge)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(records[key], ensure_ascii=False) + "\n" for key in records
        )
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
