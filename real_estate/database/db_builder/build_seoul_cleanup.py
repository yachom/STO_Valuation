import importlib.util
import os
import sqlite3
import subprocess
import sys
from io import BytesIO

REQUIRED = ["xlrd", "openpyxl", "pandas", "requests"]

missing = [pkg for pkg in REQUIRED if importlib.util.find_spec(pkg) is None]
if missing:
    print(f"설치 필요: {missing}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
else:
    print("모든 패키지가 이미 설치되어 있습니다.")

import openpyxl
import pandas as pd
import requests
import xlrd

print("\n=== 환경 확인 ===")
print(f"Python    : {sys.version.split()[0]}")
print(f"pandas    : {pd.__version__}")
print(f"requests  : {requests.__version__}")
print(f"openpyxl  : {openpyxl.__version__}")
print(f"xlrd      : {xlrd.__version__}")
print(f"sqlite3   : {sqlite3.sqlite_version}")


def main():
    url = "https://cleanup.seoul.go.kr/cleanup/bsnssttus/lsubBsnsSttusExcel.do"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    res = requests.get(url, headers=headers)
    res.raise_for_status()

    # .xls의 첫 행은 제목/병합셀이라 실제 헤더는 두 번째 행 → header=1
    df = pd.read_excel(BytesIO(res.content), header=1)
    print("Excel 로드:", df.shape)
    print("컬럼:", df.columns.tolist())
    print(df.head())

    DB_PATH = os.path.join("../raw_data", "seoul_cleanup.db")
    TABLE = "bsns_sttus"

    print("현재 작업 디렉토리(cwd):", os.getcwd())
    print("DB 저장 경로:", DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    try:
        df.to_sql(TABLE, conn, if_exists="replace", index=False)
    finally:
        conn.close()

    print(f"✅ {len(df)} rows → {DB_PATH} (table: {TABLE})")


if __name__ == "__main__":
    main()
