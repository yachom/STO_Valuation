"""Gov24 건축물대장 PDF 어댑터 — 표제부/전유부 텍스트 정리·추출·변환.

정부24(gov.kr)에서 발급한 집합건축물대장 PDF(표제부·전유부)를 LLM-ready 텍스트로
정리하고(parser), 건물ID·주소·건축물 현황·변동사항을 구조화 추출한다(extract).
원콜 변환은 convert.

이 어댑터는 *파싱/추출*만 한다 — '어떤 추가 문서를 요청할지' 판정은 graphflow의
doc_requester 에이전트(LLM)가 한다. 프로토타입(downloads/gov24/)을 트리 안으로
이전한 것이며, 외부 호출 로거 컨벤션은 external.gov24. PDF 백엔드: pypdf.
"""

from real_estate.external.gov24.convert import convert_pdf_to_clean_document
from real_estate.external.gov24.extract import (
    BuildingRow,
    ChangeEntry,
    Gov24Extract,
    extract_from_clean_pdf,
)
from real_estate.external.gov24.parser import (
    PageText,
    PdfText,
    parse_gov24_pdf,
    render_markdown,
)

__all__ = [
    "convert_pdf_to_clean_document",
    "parse_gov24_pdf",
    "render_markdown",
    "extract_from_clean_pdf",
    "PdfText",
    "PageText",
    "Gov24Extract",
    "BuildingRow",
    "ChangeEntry",
]
