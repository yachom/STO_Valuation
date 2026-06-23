"""원콜 PDF 변환 — 정리된 Markdown + 구조화 데이터 (순수 함수).

PDF 경로 1개 → 요약표·건축물 현황·변동사항·페이지별 텍스트가 담긴 단일 Markdown
문서와 구조화 dict. 프로토타입(downloads/gov24/gov24_pdf_convert.py)에서 트리 안
어댑터로 이전 — 크로스 임포트는 패키지 경로로.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from real_estate.external.gov24.extract import (
    extract_from_clean_pdf,
    to_jsonable as extracted_to_jsonable,
)
from real_estate.external.gov24.parser import (
    PdfText,
    parse_gov24_pdf,
    squeeze_spaces,
    to_jsonable as parsed_to_jsonable,
)


GOV24_TYPE_HINTS = {
    "gov24",
    "gov24_pdf",
    "gov24_building_register",
    "building_register",
    "building_register_pdf",
    "collective_building_register",
    "건축물대장",
    "집합건축물대장",
}


def convert_pdf_to_clean_document(file_path: str | Path, file_type: str = "pdf") -> dict[str, Any]:
    """PDF → 정리된 Markdown + 구조화 추출 데이터.

    Args:
        file_path: PDF 파일 경로.
        file_type: 입력/문서 유형 힌트. ``"pdf"``는 자동 감지, ``"gov24"``/
            ``"building_register"``/``"건축물대장"``는 건축물대장 추출 강제.

    Returns:
        ``markdown``, ``data``, ``warnings``, 감지 메타가 담긴 dict.
    """

    pdf_path = Path(file_path).expanduser()
    normalized_file_type = _normalize_file_type(file_type)

    if not _is_supported_pdf_type(normalized_file_type):
        raise ValueError(f"Unsupported file_type for PDF conversion: {file_type}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file path, got: {pdf_path}")
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    parsed = parse_gov24_pdf(pdf_path)
    is_gov24_building_register = _should_extract_gov24(parsed, normalized_file_type)
    extracted = extract_from_clean_pdf(parsed) if is_gov24_building_register else None
    detected_type = "gov24_building_register" if extracted else "generic_pdf"

    warnings = _unique([*parsed.warnings, *(extracted.warnings if extracted else [])])
    markdown = (
        _render_gov24_markdown(parsed, extracted, normalized_file_type, warnings)
        if extracted
        else _render_generic_markdown(parsed, normalized_file_type, warnings)
    )

    data: dict[str, Any] = {
        "parsed": parsed_to_jsonable(parsed),
    }
    if extracted:
        data["extracted"] = extracted_to_jsonable(extracted)

    return {
        "file_path": str(pdf_path),
        "file_type": normalized_file_type,
        "detected_type": detected_type,
        "markdown": markdown,
        "data": data,
        "warnings": warnings,
    }


def _normalize_file_type(file_type: str) -> str:
    normalized = file_type.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise ValueError("file_type must not be empty")
    return normalized


def _is_supported_pdf_type(file_type: str) -> bool:
    return (
        file_type == "pdf"
        or file_type.endswith("_pdf")
        or "pdf" in file_type
        or file_type in GOV24_TYPE_HINTS
        or "gov24" in file_type
        or "building_register" in file_type
        or "건축물대장" in file_type
    )


def _should_extract_gov24(parsed: PdfText, file_type: str) -> bool:
    if file_type in GOV24_TYPE_HINTS or "gov24" in file_type or "building_register" in file_type:
        return True
    if "건축물대장" in file_type:
        return True
    if parsed.titles:
        return True

    fingerprint = f"{parsed.filename}\n{squeeze_spaces(parsed.llm_text[:3000])}"
    return "건축물대장" in fingerprint or ("건물ID" in fingerprint and "고유번호" in fingerprint)


def _render_gov24_markdown(
    parsed: PdfText,
    extracted: Any,
    file_type: str,
    warnings: list[str],
) -> str:
    lines: list[str] = [
        f"# {parsed.filename}",
        "",
        "## 문서 요약",
        "",
        _render_table(
            ["항목", "값"],
            [
                ("파일 종류", file_type),
                ("감지 유형", "gov24_building_register"),
                ("문서 유형", extracted.document_type),
                ("대장 구분", extracted.register_part),
                ("장", extracted.sheet),
                ("건물ID", extracted.building_id),
                ("고유번호", extracted.unique_number),
                ("명칭", extracted.name),
                ("호명칭", extracted.unit_name),
                ("호수/가구수/세대수", extracted.unit_count_summary),
                ("대지위치", extracted.lot_location),
                ("지번", extracted.lot_number),
                ("도로명주소", extracted.road_address),
                ("페이지 수", parsed.page_count),
            ],
        ),
        "",
        "## 건축물 현황",
        "",
    ]

    if extracted.building_rows:
        rows = [
            (
                row.page_index,
                row.kind,
                row.floor,
                row.structure,
                row.usage,
                row.area_text,
            )
            for row in extracted.building_rows
        ]
        lines.append(_render_table(["페이지", "구분", "층별", "구조", "용도", "면적(㎡)"], rows))
    else:
        lines.append("- 추출된 건축물 현황 행이 없습니다.")

    lines.extend(["", "## 변동사항", ""])
    if extracted.changes:
        rows = [(change.page_index, change.date, change.description) for change in extracted.changes]
        lines.append(_render_table(["페이지", "변동일", "변동내용 및 원인"], rows))
    else:
        lines.append("- 추출된 변동사항이 없습니다.")

    lines.extend(_render_warning_section(warnings))
    lines.extend(_render_page_text_section(parsed))
    return "\n".join(lines).rstrip() + "\n"


def _render_generic_markdown(parsed: PdfText, file_type: str, warnings: list[str]) -> str:
    lines: list[str] = [
        f"# {parsed.filename}",
        "",
        "## 문서 요약",
        "",
        _render_table(
            ["항목", "값"],
            [
                ("파일 종류", file_type),
                ("감지 유형", "generic_pdf"),
                ("페이지 수", parsed.page_count),
                ("제목", ", ".join(parsed.titles)),
            ],
        ),
    ]
    lines.extend(_render_warning_section(warnings))
    lines.extend(_render_page_text_section(parsed))
    return "\n".join(lines).rstrip() + "\n"


def _render_warning_section(warnings: list[str]) -> list[str]:
    if not warnings:
        return []
    return ["", "## 경고", "", *[f"- {warning}" for warning in warnings]]


def _render_page_text_section(parsed: PdfText) -> list[str]:
    lines = ["", "## 페이지별 정리", ""]
    for page in parsed.pages:
        heading_parts = [f"{page.page_index}페이지"]
        if page.page_mark:
            heading_parts.append(page.page_mark)
        if page.title:
            heading_parts.append(f"집합건축물대장({page.title})")
        if page.needs_ocr:
            heading_parts.append("OCR 확인 필요")

        lines.extend(
            [
                f"### {' | '.join(heading_parts)}",
                "",
                "```text",
                _safe_fence_text(page.text or "(추출된 텍스트 없음)"),
                "```",
                "",
            ]
        )
    return lines


def _render_table(headers: list[str], rows: list[tuple[Any, ...]]) -> str:
    header_line = "| " + " | ".join(_markdown_cell(header) for header in headers) + " |"
    divider_line = "| " + " | ".join("---" for _ in headers) + " |"
    body_lines = [
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |"
        for row in rows
    ]
    return "\n".join([header_line, divider_line, *body_lines])


def _markdown_cell(value: Any) -> str:
    if value is None:
        return ""
    text = squeeze_spaces(str(value).replace("\n", " "))
    return text.replace("|", "\\|")


def _safe_fence_text(text: str) -> str:
    return text.replace("```", "` ` `")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


__all__ = ["convert_pdf_to_clean_document"]
