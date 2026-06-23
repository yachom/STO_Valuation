"""Gov24 건축물대장 PDF → LLM-ready 텍스트 정리 (어댑터, 순수 함수).

이 모듈은 PDF 텍스트 레이어를 *정리·직렬화*만 한다. 필드/표 추출은 별도 층
(extract.py)에 둔다 — 다운스트림 프롬프트는 독립적으로 바뀌므로.

프로토타입(downloads/gov24/gov24_pdf_parser.py)에서 트리 안 어댑터로 이전:
CLI(main/argv/파일쓰기)는 떼고 `parse_gov24_pdf`/`render_markdown` 같은 순수
함수만 노출한다. 로거 컨벤션: external.gov24. PDF 백엔드: pypdf.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader

logger = logging.getLogger("external.gov24")


GOV24_URL_RE = re.compile(r"https?://www\.gov\.kr/\S+")
TITLE_RE = re.compile(r"집합건축물대장\(([^)]*)\)")
PAGE_MARK_RE = re.compile(r"\((?:총)?\d+쪽 중 제\d+쪽\)")
ISSUED_AT_RE = re.compile(r"^\d{2}\.\s*\d{1,2}\.\s*\d{1,2}\.\s*(?:오전|오후)\s*\d{1,2}:\d{2}$")

BOILERPLATE_LINE_RE = re.compile(
    r"^(?:"
    r"■?\s*건축물대장의 기재 및 관리 등에 관한 규칙"
    r"|이 등\(초\)본은 건축물대장의 원본 내용과 틀림없음을 증명합니다\."
    r")"
)


@dataclass
class PageText:
    page_index: int
    title: str | None
    page_mark: str | None
    raw_char_count: int
    layout_char_count: int
    text_char_count: int
    needs_ocr: bool
    layout_text: str
    text: str


@dataclass
class PdfText:
    source_path: str
    filename: str
    page_count: int
    titles: list[str] = field(default_factory=list)
    page_marks: list[str] = field(default_factory=list)
    llm_text: str = ""
    pages: list[PageText] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def squeeze_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def compact_spaced_numbers(text: str) -> str:
    """PDF 문자 배치로 쪼개진 숫자(날짜·전화번호)를 복원."""

    text = re.sub(
        r"20\s+(\d{2})년\s+0\s*([1-9])월\s+0\s*([1-9])일",
        lambda match: f"20{match.group(1)}년 0{match.group(2)}월 0{match.group(3)}일",
        text,
    )
    text = re.sub(r"0\s*2-\s*260\s*0\s*-\s*690\s*2", "02-2600-6902", text)
    return text


def is_noise_line(line: str) -> bool:
    stripped = squeeze_spaces(line)
    if not stripped:
        return False
    if "인터넷 열람문서 확인" in stripped:
        return True
    if ISSUED_AT_RE.match(stripped):
        return True
    if re.match(r"^\d+\s*/\s*\d+$", stripped):
        return True
    return False


def is_boilerplate_line(line: str) -> bool:
    return bool(BOILERPLATE_LINE_RE.match(squeeze_spaces(line)))


def clean_layout_text(text: str) -> str:
    """레이아웃을 최대한 보존하며 가벼운 정리."""

    text = nfc(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned_lines: list[str] = []

    for raw_line in text.splitlines():
        line = GOV24_URL_RE.sub("", raw_line).rstrip()
        line = re.sub(r"\s+\d+\s*/\s*\d+\s*$", "", line)
        if is_noise_line(line):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = compact_spaced_numbers(cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def collapse_letter_spaced_korean(cell: str) -> str:
    tokens = cell.split()
    if len(tokens) >= 2 and all(re.fullmatch(r"[가-힣]", token) for token in tokens):
        return "".join(tokens)

    cell = re.sub(r"전\s+화\s*:", "전화:", cell)
    cell = re.sub(r"담\s+당\s+자\s*:", "담당자:", cell)
    return squeeze_spaces(cell)


def split_layout_cells(line: str) -> list[str]:
    parts = re.split(r"([ \t]{2,})", line.strip())
    cells: list[str] = []
    current = ""

    for index, part in enumerate(parts):
        if not part:
            continue
        if index % 2 == 0:
            if not current:
                current = part
            else:
                current += " " + part
            continue

        next_part = parts[index + 1] if index + 1 < len(parts) else ""
        current_stripped = current.strip()
        next_stripped = next_part.strip()
        short_letter_gap = (
            len(part) <= 4
            and re.fullmatch(r"[가-힣]+", current_stripped or "")
            and bool(re.match(r"[가-힣]", next_stripped))
        )
        if short_letter_gap:
            current += next_part
            parts[index + 1] = ""
        else:
            if current.strip():
                cells.append(current.strip())
            current = ""

    if current.strip():
        cells.append(current.strip())

    return cells


def layout_line_to_llm_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""

    cells = [
        collapse_letter_spaced_korean(cell)
        for cell in split_layout_cells(stripped)
        if cell.strip()
    ]
    if not cells:
        return ""
    if len(cells) == 1:
        return cells[0]
    normalized = " | ".join(cells)
    normalized = normalized.replace("전 | 화 :", "전화:")
    normalized = normalized.replace("담 | 당 | 자 :", "담당자:")
    return normalized


def clean_llm_text(layout_text: str) -> str:
    """레이아웃 텍스트 → LLM 입력에 적합한 압축 텍스트."""

    lines: list[str] = []
    previous_blank = False

    for line in layout_text.splitlines():
        if is_boilerplate_line(line):
            continue

        normalized = layout_line_to_llm_line(line)
        is_blank = not normalized
        if is_blank and previous_blank:
            continue

        lines.append(normalized)
        previous_blank = is_blank

    return "\n".join(lines).strip()


def extract_page_text(page: Any) -> str:
    try:
        text = page.extract_text(extraction_mode="layout") or ""
    except TypeError:
        text = page.extract_text() or ""

    if text.strip():
        return text

    return page.extract_text() or ""


def likely_needs_ocr(raw_text: str, llm_text: str) -> bool:
    compact_raw = squeeze_spaces(raw_text)
    compact_text = squeeze_spaces(llm_text)
    if len(compact_text) < 80:
        return True
    if "gov.kr" in compact_raw and len(compact_text) < 250:
        return True
    return False


def compose_llm_text(filename: str, pages: list[PageText]) -> str:
    chunks = [f"[파일] {filename}"]

    for page in pages:
        header_parts = [f"페이지 {page.page_index}"]
        if page.page_mark:
            header_parts.append(page.page_mark)
        if page.title:
            header_parts.append(f"집합건축물대장({page.title})")
        if page.needs_ocr:
            header_parts.append("needs_ocr")

        chunks.append(f"[{' | '.join(header_parts)}]\n{page.text}")

    return "\n\n".join(chunks).strip()


def parse_gov24_pdf(path: str | Path) -> PdfText:
    """건축물대장 PDF 1개 → 정리된 PdfText (페이지 메타 + llm_text)."""
    pdf_path = Path(path)
    reader = PdfReader(str(pdf_path))

    pages: list[PageText] = []
    titles: list[str] = []
    page_marks: list[str] = []

    for page_index, page in enumerate(reader.pages, start=1):
        raw_text = extract_page_text(page)
        layout_text = clean_layout_text(raw_text)
        llm_text = clean_llm_text(layout_text)
        compact = squeeze_spaces(layout_text)

        title_match = TITLE_RE.search(compact)
        title = squeeze_spaces(title_match.group(1)) if title_match else None
        if title and title not in titles:
            titles.append(title)

        page_mark_match = PAGE_MARK_RE.search(compact)
        page_mark = page_mark_match.group(0) if page_mark_match else None
        if page_mark:
            page_marks.append(page_mark)

        pages.append(
            PageText(
                page_index=page_index,
                title=title,
                page_mark=page_mark,
                raw_char_count=len(raw_text),
                layout_char_count=len(layout_text),
                text_char_count=len(llm_text),
                needs_ocr=likely_needs_ocr(raw_text, llm_text),
                layout_text=layout_text,
                text=llm_text,
            )
        )

    filename_nfc = nfc(pdf_path.name)
    warnings: list[str] = []
    sparse_pages = [page.page_index for page in pages if page.needs_ocr]
    if sparse_pages:
        warnings.append(f"pages likely need OCR or visual pass: {sparse_pages}")

    logger.info("parse_gov24_pdf %s — %d page(s), titles=%s",
                filename_nfc, len(reader.pages), titles)
    return PdfText(
        source_path=str(pdf_path),
        filename=filename_nfc,
        page_count=len(reader.pages),
        titles=titles,
        page_marks=page_marks,
        llm_text=compose_llm_text(filename_nfc, pages),
        pages=pages,
        warnings=warnings,
    )


def parse_many(paths: Iterable[str | Path]) -> list[PdfText]:
    return [parse_gov24_pdf(path) for path in paths]


def to_jsonable(parsed: PdfText) -> dict[str, Any]:
    return asdict(parsed)


def render_markdown(parsed: PdfText) -> str:
    lines: list[str] = [
        f"# {parsed.filename}",
        "",
        "## LLM Text",
        "",
        "```text",
        parsed.llm_text,
        "```",
    ]

    if parsed.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in parsed.warnings)

    lines.extend(["", "## Page Stats", "",
                  "| page | title | mark | raw_chars | text_chars | needs_ocr |",
                  "|---:|---|---|---:|---:|---|"])
    for page in parsed.pages:
        lines.append(
            f"| {page.page_index} | {page.title or ''} | {page.page_mark or ''} | "
            f"{page.raw_char_count} | {page.text_char_count} | {str(page.needs_ocr).lower()} |"
        )

    return "\n".join(lines).rstrip() + "\n"
