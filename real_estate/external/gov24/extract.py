"""정리된 Gov24 PDF 텍스트에서 필드/표 추출 (문서별 휴리스틱, 순수 함수).

parser.py와 분리: parser는 LLM 입력 텍스트에만 집중하고, 이 파일은 건축물대장
특정 휴리스틱(건물ID·주소·건축물 현황 행·변동사항)을 담는다. 다운스트림 프롬프트가
바뀌어도 텍스트 정리는 안 건드린다.

프로토타입(downloads/gov24/gov24_pdf_extract.py)에서 트리 안 어댑터로 이전 —
CLI는 제거, 크로스 임포트는 패키지 경로로. 로거: external.gov24.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from real_estate.external.gov24.parser import PdfText, squeeze_spaces


BUILDING_ID_RE = re.compile(r"건물ID\s*\|\s*(\d{10,})|건물ID\s+(\d{10,})")
UNIQUE_NO_RE = re.compile(r"고유번호\s*\|\s*([0-9-]+)|고유번호\s+([0-9-]+)")
CHANGE_RE = re.compile(
    r"(?P<date>\d{4}\.\d{1,2}\.\d{1,2})\s+"
    r"(?P<description>.*?)(?=\s+\d{4}\.\d{1,2}\.\d{1,2}\s+|\s+-\s*이하여백\s*-|$)"
)
TABLE_ROW_RE = re.compile(
    r"(?P<kind>주\d*|주|부\d*|속\d*)\s+"
    r"(?P<floor>(?:지상|지하)?\s*\d+(?:~\d+)?층?|지\d+(?:~\d+)?층|각층|옥탑\d*층?)\s+"
    r"(?P<structure>[^\d\n|]{0,50}?구조(?:,[^\d\n|]{0,50}?구조)?)\s+"
    r"(?P<usage>.+?)\s+"
    r"(?P<area>\d[\d,]*(?:\.\d+)?)"
)
UNIT_COUNT_RE = re.compile(r"(?P<unit_count>\d+호/\d+가구/\d+세대)$")


@dataclass
class BuildingRow:
    page_index: int
    kind: str
    floor: str
    structure: str
    usage: str
    area_sqm: float | None
    area_text: str


@dataclass
class ChangeEntry:
    page_index: int
    date: str
    description: str


@dataclass
class Gov24Extract:
    filename: str
    document_type: str | None
    register_part: str | None
    sheet: str | None
    building_id: str | None
    unique_number: str | None
    name: str | None
    unit_name: str | None
    unit_count_summary: str | None
    lot_location: str | None
    lot_number: str | None
    road_address: str | None
    building_rows: list[BuildingRow] = field(default_factory=list)
    changes: list[ChangeEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    for group in match.groups():
        if group:
            return squeeze_spaces(group)
    return None


def parse_title(title: str | None) -> tuple[str | None, str | None]:
    if not title:
        return None, None
    pieces = [piece.strip() for piece in title.split(",")]
    return pieces[0] if pieces else None, pieces[1] if len(pieces) > 1 else None


def parse_name_fields(text: str) -> tuple[str | None, str | None, str | None]:
    lines = [squeeze_spaces(line) for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if "명칭" not in line:
            continue
        if "호명칭" not in line and "호수/가구수/세대수" not in line:
            continue
        if index + 1 >= len(lines):
            continue

        value = lines[index + 1].replace(" | ", " ")
        if value.startswith("대지위치 "):
            continue

        if "호수/가구수/세대수" in line:
            match = UNIT_COUNT_RE.search(value)
            if match:
                name = value[: match.start()].strip()
                return name or None, None, match.group("unit_count")

        tokens = value.split()
        if "호명칭" in line and len(tokens) >= 3 and tokens[0] == tokens[1]:
            return tokens[0], " ".join(tokens[1:]), None
        if "호명칭" in line and len(tokens) >= 2:
            return " ".join(tokens[:-1]), tokens[-1], None

    return None, None, None


def parse_location(text: str) -> tuple[str | None, str | None, str | None]:
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if "대지위치" not in line or "지번" not in line:
            continue
        cells = [cell.strip() for cell in line.split("|")]
        location = lot_number = road_address = None

        for cell_index, cell in enumerate(cells):
            if cell == "대지위치" and cell_index + 1 < len(cells):
                location = cells[cell_index + 1]
            if cell == "지번" and cell_index + 1 < len(cells):
                candidate = cells[cell_index + 1]
                if re.match(r"^\d+(?:-\d+)?(?:\s+외\s+\d+필지)?$", candidate):
                    lot_number = candidate
            if cell == "도로명주소" and cell_index + 1 < len(cells):
                road_address = cells[cell_index + 1]

        next_cells = (
            [cell.strip() for cell in lines[index + 1].split("|")]
            if index + 1 < len(lines)
            else []
        )
        for next_index, cell in enumerate(next_cells):
            if re.match(r"^\d+(?:-\d+)?(?:\s+외\s+\d+필지)?$", cell):
                lot_number = cell
                if not road_address and next_index + 1 < len(next_cells):
                    road_address = next_cells[next_index + 1]
                break

        if location or lot_number or road_address:
            return location, lot_number, road_address

    return None, None, None


def parse_area(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def extract_building_rows(page_index: int, text: str) -> list[BuildingRow]:
    rows: list[BuildingRow] = []

    for line in text.splitlines():
        normalized = squeeze_spaces(line.replace("|", " "))
        if "구조" not in normalized or "이하여백" in normalized:
            continue
        if "구분" in normalized and "층별" in normalized and "면적" in normalized:
            continue

        for match in TABLE_ROW_RE.finditer(normalized):
            usage = squeeze_spaces(match.group("usage"))
            if not usage or usage in {"용도", "주소"}:
                continue
            area_text = match.group("area")
            rows.append(
                BuildingRow(
                    page_index=page_index,
                    kind=squeeze_spaces(match.group("kind")),
                    floor=squeeze_spaces(match.group("floor")),
                    structure=squeeze_spaces(match.group("structure")),
                    usage=usage,
                    area_sqm=parse_area(area_text),
                    area_text=area_text,
                )
            )

    return rows


def extract_changes(page_index: int, text: str) -> list[ChangeEntry]:
    compact_text = squeeze_spaces(text.replace("|", " "))
    if "변동사항" not in compact_text and "변동내용 및 원인" not in compact_text:
        return []

    entries: list[ChangeEntry] = []
    for match in CHANGE_RE.finditer(compact_text):
        description = squeeze_spaces(match.group("description"))
        if description:
            entries.append(
                ChangeEntry(page_index=page_index, date=match.group("date"),
                            description=description)
            )
    return entries


def extract_from_clean_pdf(parsed: PdfText) -> Gov24Extract:
    """정리된 PdfText → 구조화 Gov24Extract (건물ID·주소·현황행·변동사항)."""
    full_text = parsed.llm_text
    document_type = parsed.titles[0] if parsed.titles else None
    register_part, sheet = parse_title(document_type)
    name, unit_name, unit_count_summary = parse_name_fields(full_text)
    lot_location, lot_number, road_address = parse_location(full_text)

    building_rows: list[BuildingRow] = []
    changes: list[ChangeEntry] = []
    for page in parsed.pages:
        building_rows.extend(extract_building_rows(page.page_index, page.text))
        changes.extend(extract_changes(page.page_index, page.text))

    warnings = list(parsed.warnings)
    if document_type and "표제부" in parsed.filename and register_part == "전유부":
        warnings.append("filename says 표제부, but PDF title says 전유부")
    if document_type and "전유부" in parsed.filename and register_part == "표제부":
        warnings.append("filename says 전유부, but PDF title says 표제부")

    return Gov24Extract(
        filename=parsed.filename,
        document_type=document_type,
        register_part=register_part,
        sheet=sheet,
        building_id=first_match(BUILDING_ID_RE, full_text),
        unique_number=first_match(UNIQUE_NO_RE, full_text),
        name=name,
        unit_name=unit_name,
        unit_count_summary=unit_count_summary,
        lot_location=lot_location,
        lot_number=lot_number,
        road_address=road_address,
        building_rows=building_rows,
        changes=changes,
        warnings=warnings,
    )


def to_jsonable(extracted: Gov24Extract) -> dict[str, Any]:
    return asdict(extracted)
