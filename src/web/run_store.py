"""실행 결과 영속화: data/runs/<hash>/."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path("data/runs")


def address_id(address: str) -> str:
    return hashlib.md5(address.encode("utf-8")).hexdigest()[:12]


def run_dir(address: str) -> Path:
    d = ROOT / address_id(address)
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_run(address: str, result: dict[str, Any], report_md: str, kg_json: dict[str, Any] | None) -> dict[str, str]:
    d = run_dir(address)
    (d / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (d / "report.md").write_text(report_md, encoding="utf-8")
    if kg_json is not None:
        (d / "knowledge_graph.json").write_text(json.dumps(kg_json, ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "meta.json").write_text(
        json.dumps({"address": address, "saved_at": datetime.now().isoformat()}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "dir": str(d),
        "report": str(d / "report.md"),
        "result": str(d / "result.json"),
        "kg": str(d / "knowledge_graph.json"),
    }


def list_runs() -> list[dict[str, Any]]:
    if not ROOT.exists():
        return []
    runs = []
    for d in sorted(ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        runs.append({"id": d.name, "address": meta.get("address"), "saved_at": meta.get("saved_at"), "dir": str(d)})
    return runs


def load_run(run_id: str) -> dict[str, str] | None:
    d = ROOT / run_id
    if not d.exists():
        return None
    return {
        "dir": str(d),
        "report": str(d / "report.md"),
        "result": str(d / "result.json"),
        "kg": str(d / "knowledge_graph.json"),
    }
