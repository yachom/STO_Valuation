"""Report 도메인 객체 — 1급 시민.

상태기계: draft → revised* → final → qa_passed.
전이 규칙은 transition()이 강제한다. 잘못된 전이는 ValueError.

레퍼런스: [[project_vision]]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ReportStatus = Literal["draft", "revised", "final", "qa_passed"]

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"revised", "final"},
    "revised": {"revised", "final"},
    "final": {"qa_passed"},
    "qa_passed": set(),
}


@dataclass
class ReportSection:
    section_id: str
    axis: str                    # tenant / esg / commercial / overview
    title: str
    body_markdown: str
    evidence_node_refs: list[str] = field(default_factory=list)
    evidence_snippets: list[str] = field(default_factory=list)


@dataclass
class ReportVersion:
    version_id: str
    parent_version_id: str | None
    sections: list[ReportSection]
    created_at: str
    notes: str = ""


@dataclass
class Report:
    report_id: str
    asset_id: str
    asset_summary: str
    status: ReportStatus
    versions: list[ReportVersion]
    created_at: str
    finalized_at: str | None = None

    @staticmethod
    def new(asset_id: str, asset_summary: str) -> "Report":
        return Report(
            report_id=f"rpt_{uuid.uuid4().hex[:12]}",
            asset_id=asset_id,
            asset_summary=asset_summary,
            status="draft",
            versions=[],
            created_at=datetime.utcnow().isoformat(),
            finalized_at=None,
        )

    def latest(self) -> ReportVersion | None:
        return self.versions[-1] if self.versions else None

    def append_version(self, sections: list[ReportSection], notes: str = "") -> ReportVersion:
        parent = self.latest()
        v = ReportVersion(
            version_id=f"v_{uuid.uuid4().hex[:10]}",
            parent_version_id=parent.version_id if parent else None,
            sections=sections,
            created_at=datetime.utcnow().isoformat(),
            notes=notes,
        )
        self.versions.append(v)
        return v

    def transition(self, new: ReportStatus) -> None:
        allowed = _ALLOWED_TRANSITIONS[self.status]
        if new not in allowed:
            raise ValueError(
                f"허용되지 않은 전이: {self.status} → {new}. "
                f"허용: {sorted(allowed)}"
            )
        self.status = new
        if new == "final":
            self.finalized_at = datetime.utcnow().isoformat()
