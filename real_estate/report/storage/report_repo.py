"""Report 도메인 객체의 SQLite 영속화."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

from real_estate.report.domain import Report, ReportSection, ReportVersion
from real_estate.report.storage.schema_extension import ensure_report_schema

logger = logging.getLogger("repository.report")


class ReportRepository:
    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        # 보고서 본문 sidecar 디렉토리 (truth는 SQLite이지만 git-able diff 용).
        self._sidecar_dir = Path(self._db_path).parent / "reports"

    # ─── write ─────────────────────────────────────────────
    def save(self, report: Report) -> None:
        conn = sqlite3.connect(self._db_path, timeout=60)
        try:
            ensure_report_schema(conn)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO report (report_id, asset_id, asset_summary, status,
                                    created_at, finalized_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET
                    status = excluded.status,
                    finalized_at = excluded.finalized_at
                """,
                (
                    report.report_id, report.asset_id, report.asset_summary,
                    report.status, report.created_at, report.finalized_at,
                ),
            )
            for v in report.versions:
                body_json = json.dumps(
                    [asdict(s) for s in v.sections],
                    ensure_ascii=False,
                )
                cur.execute(
                    """
                    INSERT OR REPLACE INTO report_version (
                        version_id, report_id, parent_version_id,
                        body_json, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        v.version_id, report.report_id, v.parent_version_id,
                        body_json, v.notes, v.created_at,
                    ),
                )
                self._write_sidecar(report.report_id, v)
            conn.commit()
            logger.info("Report saved: %s (%d versions)",
                        report.report_id, len(report.versions))
        finally:
            conn.close()

    def _write_sidecar(self, report_id: str, version: ReportVersion) -> None:
        target_dir = self._sidecar_dir / report_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{version.version_id}.md"
        parts = []
        for s in version.sections:
            parts.append(f"## {s.title}\n\n{s.body_markdown}\n")
        path.write_text("\n".join(parts), encoding="utf-8")

    # ─── read ──────────────────────────────────────────────
    def fetch(self, report_id: str) -> Report | None:
        conn = sqlite3.connect(self._db_path)
        try:
            ensure_report_schema(conn)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM report WHERE report_id=?", (report_id,),
            ).fetchone()
            if row is None:
                return None
            versions = []
            for v in conn.execute(
                "SELECT * FROM report_version WHERE report_id=? "
                "ORDER BY created_at ASC",
                (report_id,),
            ):
                sections = [
                    ReportSection(**s) for s in json.loads(v["body_json"])
                ]
                versions.append(ReportVersion(
                    version_id=v["version_id"],
                    parent_version_id=v["parent_version_id"],
                    sections=sections,
                    created_at=v["created_at"],
                    notes=v["notes"] or "",
                ))
            return Report(
                report_id=row["report_id"],
                asset_id=row["asset_id"],
                asset_summary=row["asset_summary"] or "",
                status=row["status"],
                versions=versions,
                created_at=row["created_at"],
                finalized_at=row["finalized_at"],
            )
        finally:
            conn.close()

    def list_by_asset(self, asset_id: str) -> list[dict]:
        conn = sqlite3.connect(self._db_path)
        try:
            ensure_report_schema(conn)
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT report_id, status, created_at, finalized_at "
                "FROM report WHERE asset_id=? ORDER BY created_at DESC",
                (asset_id,),
            )]
        finally:
            conn.close()
