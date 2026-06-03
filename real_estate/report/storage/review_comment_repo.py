"""review_comment 테이블 CRUD.

스키마는 schema_extension.py에 정의됨. status: open/resolved.
intent: factual / tone / structure / missing-evidence.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime

from real_estate.report.storage.schema_extension import ensure_report_schema


@dataclass
class ReviewComment:
    comment_id: str
    version_id: str
    section_id: str
    intent: str
    anchor_start: int | None
    anchor_end: int | None
    instruction: str
    status: str           # open / resolved
    created_at: str
    resolved_at: str | None = None


class ReviewCommentRepository:
    def __init__(self, db_path: str):
        self._db_path = db_path

    def add(
        self, *,
        version_id: str,
        section_id: str,
        intent: str,
        instruction: str,
        anchor_start: int | None = None,
        anchor_end: int | None = None,
    ) -> ReviewComment:
        comment = ReviewComment(
            comment_id=f"cmt_{uuid.uuid4().hex[:10]}",
            version_id=version_id,
            section_id=section_id,
            intent=intent,
            anchor_start=anchor_start,
            anchor_end=anchor_end,
            instruction=instruction,
            status="open",
            created_at=datetime.utcnow().isoformat(),
            resolved_at=None,
        )
        conn = sqlite3.connect(self._db_path, timeout=60)
        try:
            ensure_report_schema(conn)
            conn.execute(
                """
                INSERT INTO review_comment (
                    comment_id, version_id, section_id, intent,
                    anchor_start, anchor_end, instruction, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comment.comment_id, comment.version_id, comment.section_id,
                    comment.intent, comment.anchor_start, comment.anchor_end,
                    comment.instruction, comment.status, comment.created_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return comment

    def list_for_version(self, version_id: str) -> list[ReviewComment]:
        conn = sqlite3.connect(self._db_path)
        try:
            ensure_report_schema(conn)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM review_comment WHERE version_id=? "
                "ORDER BY created_at ASC",
                (version_id,),
            )
            return [ReviewComment(**dict(r)) for r in cur]
        finally:
            conn.close()

    def list_open_for_section(
        self, version_id: str, section_id: str,
    ) -> list[ReviewComment]:
        return [c for c in self.list_for_version(version_id)
                if c.section_id == section_id and c.status == "open"]

    def mark_resolved(self, comment_id: str) -> None:
        conn = sqlite3.connect(self._db_path, timeout=60)
        try:
            ensure_report_schema(conn)
            conn.execute(
                "UPDATE review_comment SET status='resolved', resolved_at=? "
                "WHERE comment_id=?",
                (datetime.utcnow().isoformat(), comment_id),
            )
            conn.commit()
        finally:
            conn.close()
