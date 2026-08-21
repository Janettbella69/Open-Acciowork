"""Gold 标注界面的服务核心（HTTP 层在 evals/tools/annotate_gold.py）。

评测系统自有代码：只依赖 stdlib 与同目录的 gold_sampling，不触主系统。
读取 sample_gold.py 产出的样本批文件，合并已有标签与跳过状态，
把负责人的判定写进 `evals/gold/<judge>/labels.jsonl`。不调用模型。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from gold_sampling import JUDGE_QUESTIONS, LabelStore


class GoldAnnotationService:
    def __init__(self, samples_path: Path, gold_dir: Path) -> None:
        self.samples_path = Path(samples_path)
        self.gold_dir = Path(gold_dir)
        self.samples = json.loads(self.samples_path.read_text(encoding="utf-8"))
        self.store = LabelStore(self.gold_dir)
        self._items_by_id = {item["item_id"]: item for item in self.samples["items"]}
        self._progress_path = self.gold_dir / "annotation-progress.json"

    def _deferred(self) -> set[str]:
        if self._progress_path.exists():
            data = json.loads(self._progress_path.read_text(encoding="utf-8"))
            return set(data.get("deferred", []))
        return set()

    def state(self) -> dict:
        labels = self.store.load()
        deferred = self._deferred()
        items = []
        for item in self.samples["items"]:
            record = labels.get(item["item_id"])
            items.append(
                {
                    **item,
                    "label": record.get("label") if record else None,
                    "rationale": record.get("rationale", "") if record else "",
                    "labeled": record is not None,
                    "deferred": item["item_id"] in deferred,
                }
            )
        return {
            "batch": self.samples_path.name,
            "questions": JUDGE_QUESTIONS,
            "runs": self.samples["runs"],
            "items": items,
        }

    def save_label(self, item_id: str, label: bool, rationale: str) -> dict:
        item = self._items_by_id[item_id]
        record = {
            "run_id": item["run_id"],
            "item": item["item"],
            "label": bool(label),
            "rationale": rationale,
            "item_id": item_id,
            "judge": item["judge"],
            "split": item.get("split"),
            "source": item.get("source"),
            "batch": self.samples_path.name,
            "labeled_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.upsert(record)
        deferred = self._deferred()
        if item_id in deferred:
            deferred.discard(item_id)
            self._write_progress(deferred)
        return record

    def toggle_defer(self, item_id: str) -> bool:
        if item_id not in self._items_by_id:
            raise KeyError(item_id)
        deferred = self._deferred()
        if item_id in deferred:
            deferred.discard(item_id)
            now_deferred = False
        else:
            deferred.add(item_id)
            now_deferred = True
        self._write_progress(deferred)
        return now_deferred

    def _write_progress(self, deferred: set[str]) -> None:
        self.gold_dir.mkdir(parents=True, exist_ok=True)
        self._progress_path.write_text(
            json.dumps({"deferred": sorted(deferred)}, ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
