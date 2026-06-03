"""report_* 테이블의 schema extension.

real_estate.db의 보조 영역. 데이터 빌더는 자기 테이블만 책임지고,
report 시스템은 자기 테이블 셋만 책임진다 (관심사 분리).

본문 마크다운은 SQLite에 truth로 두되, `database/DB/reports/{report_id}/v{n}.md`
sidecar로도 함께 기록해 git diff 친화 (sidecar는 truth가 아니므로 누락돼도 무방).
"""

from __future__ import annotations

REPORT = """
CREATE TABLE IF NOT EXISTS report (
    report_id      TEXT PRIMARY KEY,
    asset_id       TEXT NOT NULL,
    asset_summary  TEXT,
    status         TEXT NOT NULL,         -- draft / revised / final / qa_passed
    created_at     TEXT NOT NULL,
    finalized_at   TEXT
)
"""
REPORT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_report_asset  ON report(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_report_status ON report(status)",
]


REPORT_VERSION = """
CREATE TABLE IF NOT EXISTS report_version (
    version_id         TEXT PRIMARY KEY,
    report_id          TEXT NOT NULL REFERENCES report(report_id),
    parent_version_id  TEXT,
    body_json          TEXT NOT NULL,    -- ReportSection 리스트의 직렬화
    notes              TEXT,
    created_at         TEXT NOT NULL
)
"""
REPORT_VERSION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_report_version_rpt ON report_version(report_id, created_at)",
]


# review_comment는 PR-9에서 추가 예정. 미리 정의해두면 ensure_report_schema가
# 1회로 끝나서 PR-9에 마이그레이션 노이즈가 없다.
REVIEW_COMMENT = """
CREATE TABLE IF NOT EXISTS review_comment (
    comment_id     TEXT PRIMARY KEY,
    version_id     TEXT NOT NULL REFERENCES report_version(version_id),
    section_id     TEXT NOT NULL,
    intent         TEXT NOT NULL,        -- factual / tone / structure / missing-evidence
    anchor_start   INTEGER,
    anchor_end     INTEGER,
    instruction    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',  -- open / resolved
    created_at     TEXT NOT NULL,
    resolved_at    TEXT
)
"""
REVIEW_COMMENT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_review_comment_ver ON review_comment(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_review_comment_st  ON review_comment(status)",
]


REPORT_EXTENSION_DDL = [
    ("report", REPORT, REPORT_INDEXES),
    ("report_version", REPORT_VERSION, REPORT_VERSION_INDEXES),
    ("review_comment", REVIEW_COMMENT, REVIEW_COMMENT_INDEXES),
]


def ensure_report_schema(conn) -> None:
    """report_* 테이블 생성. idempotent."""
    cur = conn.cursor()
    for name, ddl, idxs in REPORT_EXTENSION_DDL:
        cur.execute(ddl)
        for idx in idxs:
            cur.execute(idx)
    conn.commit()
