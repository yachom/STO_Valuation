"""서울 정비사업 현황 → development_project 테이블에 적재.

출처: cleanup.seoul.go.kr 공개 xls.
적재 위치: real_estate.db / development_project (영문 컬럼으로 정규화).
"""

import importlib.util
import sqlite3
import subprocess
import sys
from io import BytesIO
from pathlib import Path

REQUIRED = ["xlrd", "openpyxl", "pandas", "requests"]
missing = [pkg for pkg in REQUIRED if importlib.util.find_spec(pkg) is None]
if missing:
    print(f"설치 필요: {missing}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

import pandas as pd  # noqa: E402
import requests  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from real_estate.database import schema  # noqa: E402

DB_PATH = SCRIPT_DIR.parent / "DB" / schema.DB_FILENAME

# Excel 한글 컬럼 → development_project 영문 컬럼
COL_MAP = {
    "번호":              "seq_no",
    "자치구":            "district",
    "사업구분":          "category",
    "사업장명":          "name",
    "대표지번":          "jibun",
    "진행단계":          "stage",
    "운영구분":          "operation_kind",
    "운영단계":          "operation_stage",
    "공개자료수(최근자료)": "public_doc_count",
    "공개적시성(전체자료)": "timeliness",
    "자료충실도":        "completeness",
}


def main():
    url = "https://cleanup.seoul.go.kr/cleanup/bsnssttus/lsubBsnsSttusExcel.do"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    print(f"DB 경로: {DB_PATH}")
    res = requests.get(url, headers=headers)
    res.raise_for_status()

    # .xls 첫 행은 제목/병합셀이라 헤더는 두 번째 행
    df = pd.read_excel(BytesIO(res.content), header=1)
    print(f"Excel 로드: {df.shape}, 컬럼: {df.columns.tolist()}")

    df = df.rename(columns=COL_MAP)
    expected = list(COL_MAP.values())
    df = df[[c for c in expected if c in df.columns]]
    if "seq_no" in df.columns:
        df["seq_no"] = pd.to_numeric(df["seq_no"], errors="coerce").astype("Int64")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        schema.ensure_schema(conn)
        conn.execute("DELETE FROM development_project")
        df.to_sql("development_project", conn, if_exists="append", index=False)
        conn.commit()
    finally:
        conn.close()

    print(f"✅ {len(df)} rows → {DB_PATH} (table: development_project)")


if __name__ == "__main__":
    main()
