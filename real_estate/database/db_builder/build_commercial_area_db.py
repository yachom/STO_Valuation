"""상권 분석 DB 빌더.

서울시 상권분석서비스 데이터(영역·인구·매출)를 real_estate.db의
다음 7개 테이블에 적재:

    commercial_area                  상권 영역(경계 다각형)
    commercial_floating_population   유동인구 (상권 단위)
    commercial_resident_population   거주인구 (상권 단위)
    admin_dong_floating_population   유동인구 (행정동 단위, 폴백용)
    admin_dong_resident_population   거주인구 (행정동 단위, 폴백용)
    commercial_estimated_sales       추정매출 (상권 단위)
    admin_dong_estimated_sales       추정매출 (행정동 단위, 폴백용)

외부 라이브러리 없이 표준 라이브러리만 사용한다(Shapefile 직접 파싱,
좌표 변환 직접 구현).

실행:
        python build_commercial_area_db.py
"""

import csv
import json
import math
import os
import sqlite3
import struct
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from real_estate.database import schema  # noqa: E402


# ─────────────────────────────────────────────────────────────────
# 경로 설정 (db_builder 폴더에서 실행 기준).
# ─────────────────────────────────────────────────────────────────
PATHS = {
    "shp_base":      "../raw_data/서울시 상권분석서비스(영역-상권)",
    "db":            f"../DB/{schema.DB_FILENAME}",
    "floating":      "../raw_data/서울시 상권분석서비스(길단위인구-상권).csv",
    "resident":      "../raw_data/서울시 상권분석서비스(상주인구-상권).csv",
    "floating_dong": "../raw_data/서울시 상권분석서비스(길단위인구-행정동).csv",
    "resident_dong": "../raw_data/서울시 상권분석서비스(상주인구-행정동).csv",
    "sales":         "../raw_data/서울시 상권분석서비스(추정매출-상권)_2025년.csv",
    "sales_dong":    "../raw_data/서울시 상권분석서비스(추정매출-행정동)_2025년.csv",
}


# ═════════════════════════════════════════════════════════════════
# 1. 좌표 변환 — EPSG:5181 → WGS84 위경도
# ═════════════════════════════════════════════════════════════════

def tm5181_to_wgs84(x, y):
    """EPSG:5181 평면좌표(x, y 미터)를 WGS84 위경도(lat, lon)로 변환.

    상권 Shapefile 좌표계(Korea 2000 중부원점)를 우리 시스템의 위경도로
    바꾼다. 횡축 메르카토르 역변환. .prj 파라미터:
    중앙자오선 127°, 원점위도 38°, FE 200000, FN 500000, k0 1.0, GRS80.
    """
    a = 6378137.0
    f = 1 / 298.257222101
    k0 = 1.0
    lon0 = math.radians(127.0)
    lat0 = math.radians(38.0)
    FE = 200000.0
    FN = 500000.0

    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)

    def meridian_arc(lat):
        return a * ((1 - e2/4 - 3*e2**2/64 - 5*e2**3/256) * lat
                  - (3*e2/8 + 3*e2**2/32 + 45*e2**3/1024) * math.sin(2*lat)
                  + (15*e2**2/256 + 45*e2**3/1024) * math.sin(4*lat)
                  - (35*e2**3/3072) * math.sin(6*lat))

    M0 = meridian_arc(lat0)
    M = M0 + (y - FN) / k0

    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    mu = M / (a * (1 - e2/4 - 3*e2**2/64 - 5*e2**3/256))
    lat1 = (mu + (3*e1/2 - 27*e1**3/32) * math.sin(2*mu)
              + (21*e1**2/16 - 55*e1**4/32) * math.sin(4*mu)
              + (151*e1**3/96) * math.sin(6*mu))

    C1 = ep2 * math.cos(lat1)**2
    T1 = math.tan(lat1)**2
    N1 = a / math.sqrt(1 - e2 * math.sin(lat1)**2)
    R1 = a * (1 - e2) / (1 - e2 * math.sin(lat1)**2)**1.5
    D = (x - FE) / (N1 * k0)

    lat = lat1 - (N1 * math.tan(lat1) / R1) * (D**2/2
            - (5 + 3*T1 + 10*C1 - 4*C1**2 - 9*ep2) * D**4/24
            + (61 + 90*T1 + 298*C1 + 45*T1**2 - 252*ep2 - 3*C1**2) * D**6/720)
    lon = lon0 + (D - (1 + 2*T1 + C1) * D**3/6
            + (5 - 2*C1 + 28*T1 - 3*C1**2 + 8*ep2 + 24*T1**2) * D**5/120) / math.cos(lat1)

    return math.degrees(lat), math.degrees(lon)


# ═════════════════════════════════════════════════════════════════
# 2. Shapefile 직접 파싱 (.dbf 속성 / .shp 다각형)
# ═════════════════════════════════════════════════════════════════

def read_dbf(path):
    """DBF 파일을 읽어 레코드(dict) 리스트로 반환한다."""
    with open(path, "rb") as f:
        data = f.read()

    num_records = struct.unpack("<I", data[4:8])[0]
    header_size = struct.unpack("<H", data[8:10])[0]
    record_size = struct.unpack("<H", data[10:12])[0]

    # 필드 정의 (32바이트씩)
    fields = []
    pos = 32
    while data[pos] != 0x0D:
        name = data[pos:pos+11].split(b"\x00")[0].decode("utf-8", errors="replace")
        ftype = chr(data[pos+11])
        flen = data[pos+16]
        fields.append((name, ftype, flen))
        pos += 32

    records = []
    for r in range(num_records):
        start = header_size + r * record_size
        rec = data[start:start+record_size]
        offset = 1  # 첫 바이트는 삭제 표시 플래그
        row = {}
        for name, ftype, flen in fields:
            raw = rec[offset:offset+flen].decode("utf-8", errors="replace").strip()
            row[name] = raw
            offset += flen
        records.append(row)
    return records


def read_shp_polygons(path):
    """SHP 파일을 읽어 각 레코드의 다각형을 반환한다.

    반환: 리스트. 각 원소는 part들의 리스트이고, 각 part는 (x, y) 좌표
          튜플의 리스트다 (EPSG:5181 원본 좌표).
    """
    with open(path, "rb") as f:
        data = f.read()

    polygons = []
    pos = 100  # 파일 헤더 100바이트 건너뛰기

    while pos < len(data):
        content_len = struct.unpack(">I", data[pos+4:pos+8])[0] * 2
        shape_type = struct.unpack("<I", data[pos+8:pos+12])[0]

        if shape_type == 5:  # Polygon
            num_parts = struct.unpack("<I", data[pos+44:pos+48])[0]
            num_points = struct.unpack("<I", data[pos+48:pos+52])[0]

            parts_start = pos + 52
            parts = struct.unpack(
                f"<{num_parts}I",
                data[parts_start:parts_start + num_parts*4]
            )

            points_start = parts_start + num_parts*4
            all_points = []
            for p in range(num_points):
                x, y = struct.unpack(
                    "<2d",
                    data[points_start + p*16 : points_start + p*16 + 16]
                )
                all_points.append((x, y))

            rec_parts = []
            for i in range(num_parts):
                s = parts[i]
                e = parts[i+1] if i+1 < num_parts else num_points
                rec_parts.append(all_points[s:e])
            polygons.append(rec_parts)
        else:
            polygons.append([])  # Polygon이 아닌 레코드

        pos += 8 + content_len

    return polygons


# ═════════════════════════════════════════════════════════════════
# 3. 상권 영역 테이블
# ═════════════════════════════════════════════════════════════════

def build_area_table(conn, shp_base):
    """상권 영역 Shapefile을 읽어 commercial_area 테이블을 만든다.

    다각형 좌표를 EPSG:5181 → WGS84로 변환하고, 경계상자(bbox)와 함께
    저장한다. bbox는 런타임 point-in-polygon의 1차 필터에 쓰인다.
    """
    dbf_records = read_dbf(shp_base + ".dbf")
    shp_polygons = read_shp_polygons(shp_base + ".shp")
    assert len(dbf_records) == len(shp_polygons), \
        "DBF와 SHP 레코드 수 불일치"

    # 다각형을 위경도로 변환 ([경도, 위도] 순으로 저장 — GeoJSON 관례)
    polygons_wgs84 = []
    for rec_parts in shp_polygons:
        converted_parts = []
        for part in rec_parts:
            converted = [[*reversed(tm5181_to_wgs84(x, y))] for x, y in part]
            converted_parts.append(converted)
        polygons_wgs84.append(converted_parts)

    cur = conn.cursor()
    cur.execute("DELETE FROM commercial_area")

    rows = 0
    for rec, parts in zip(dbf_records, polygons_wgs84):
        if not parts or not parts[0]:
            continue

        all_lon = [pt[0] for part in parts for pt in part]
        all_lat = [pt[1] for part in parts for pt in part]
        bbox = (min(all_lon), min(all_lat), max(all_lon), max(all_lat))

        try:
            cx = float(rec["XCNTS_VALU"])
            cy = float(rec["YDNTS_VALU"])
            center_lat, center_lon = tm5181_to_wgs84(cx, cy)
        except (ValueError, KeyError):
            center_lat, center_lon = None, None

        try:
            area = float(rec["RELM_AR"])
        except (ValueError, KeyError):
            area = None

        cur.execute(
            """
            INSERT OR REPLACE INTO commercial_area
                (trdar_cd, trdar_se_name, trdar_name, signgu_cd, adstrd_cd, adstrd_name,
                 area_sqm, center_lon, center_lat,
                 bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat,
                 polygon_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (rec["TRDAR_CD"], rec["TRDAR_SE_1"], rec["TRDAR_CD_N"],
             rec["SIGNGU_CD"], rec["ADSTRD_CD"], rec["ADSTRD_CD_"],
             area, center_lon, center_lat,
             bbox[0], bbox[1], bbox[2], bbox[3],
             json.dumps(parts)))
        rows += 1

    conn.commit()
    print(f"commercial_area: {rows}개 상권")


# ═════════════════════════════════════════════════════════════════
# 4. CSV 적재 공통 함수
# ═════════════════════════════════════════════════════════════════

def load_csv_table(conn, csv_path, table, columns, encoding="cp949"):
    """CSV → 이미 존재하는 스키마 테이블에 INSERT OR REPLACE.

    Args:
        conn:        DB 연결 (스키마는 이미 ensure_schema로 생성됨).
        csv_path:    CSV 파일 경로.
        table:       대상 테이블 이름 (schema.py에 정의된 이름).
        columns:     CSV 컬럼 순서에 대응하는 DB 영문 컬럼명 리스트
                     (테이블의 CREATE 컬럼 순서와 일치해야 함).
        encoding:    CSV 인코딩.
    """
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table}")

    col_list = ", ".join(columns)
    placeholders = ",".join("?" * len(columns))
    insert_sql = (
        f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
    )

    with open(csv_path, encoding=encoding) as f:
        reader = csv.reader(f)
        header = next(reader)
        assert len(header) == len(columns), (
            f"{table} 컬럼 수 불일치: CSV {len(header)} vs 정의 {len(columns)}")
        count = 0
        for row in reader:
            cur.execute(insert_sql, row)
            count += 1

    conn.commit()
    print(f"{table}: {count}행")


# ═════════════════════════════════════════════════════════════════
# 5. 컬럼 정의 (CSV 순서대로)
# ═════════════════════════════════════════════════════════════════

# 유동인구 (상권 27 / 행정동 25)
_FLOATING_TAIL = [
    "total", "male", "female",
    "age_10", "age_20", "age_30", "age_40", "age_50", "age_60_over",
    "time_00_06", "time_06_11", "time_11_14",
    "time_14_17", "time_17_21", "time_21_24",
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
]
FLOATING_COLS = ["quarter_code", "trdar_se_code", "trdar_se_name",
                 "trdar_cd", "trdar_name"] + _FLOATING_TAIL
FLOATING_DONG_COLS = ["quarter_code", "adstrd_cd", "adstrd_name"] + _FLOATING_TAIL

# 거주인구 (상권 29 / 행정동 27)
_RESIDENT_TAIL = [
    "total", "male", "female",
    "age_10", "age_20", "age_30", "age_40", "age_50", "age_60_over",
    "male_10", "male_20", "male_30", "male_40", "male_50", "male_60_over",
    "female_10", "female_20", "female_30", "female_40", "female_50",
    "female_60_over",
    "household", "apartment_household", "non_apartment_household",
]
RESIDENT_COLS = ["quarter_code", "trdar_se_code", "trdar_se_name",
                 "trdar_cd", "trdar_name"] + _RESIDENT_TAIL
RESIDENT_DONG_COLS = ["quarter_code", "adstrd_cd", "adstrd_name"] + _RESIDENT_TAIL

# 추정매출 (상권 55 / 행정동 53)
_SALES_TAIL = [
    "sales_amount", "sales_count",
    "weekday_amount", "weekend_amount",
    "mon_amount", "tue_amount", "wed_amount", "thu_amount",
    "fri_amount", "sat_amount", "sun_amount",
    "time_00_06_amount", "time_06_11_amount", "time_11_14_amount",
    "time_14_17_amount", "time_17_21_amount", "time_21_24_amount",
    "male_amount", "female_amount",
    "age_10_amount", "age_20_amount", "age_30_amount",
    "age_40_amount", "age_50_amount", "age_60_over_amount",
    "weekday_count", "weekend_count",
    "mon_count", "tue_count", "wed_count", "thu_count",
    "fri_count", "sat_count", "sun_count",
    "time_00_06_count", "time_06_11_count", "time_11_14_count",
    "time_14_17_count", "time_17_21_count", "time_21_24_count",
    "male_count", "female_count",
    "age_10_count", "age_20_count", "age_30_count",
    "age_40_count", "age_50_count", "age_60_over_count",
]
SALES_COLS = ["quarter_code", "trdar_se_code", "trdar_se_name",
              "trdar_cd", "trdar_name",
              "industry_code", "industry_name"] + _SALES_TAIL
SALES_DONG_COLS = ["quarter_code", "adstrd_cd", "adstrd_name",
                   "industry_code", "industry_name"] + _SALES_TAIL


# ═════════════════════════════════════════════════════════════════
# 6. 전체 빌드
# ═════════════════════════════════════════════════════════════════

def build(paths=PATHS):
    """real_estate.db 안의 상권 7개 테이블을 채운다."""
    db_dir = os.path.dirname(paths["db"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(paths["db"])
    try:
        schema.ensure_schema(conn)

        # 1) 상권 영역
        build_area_table(conn, paths["shp_base"])

        # 2) 인구 (상권 단위)
        load_csv_table(conn, paths["floating"],
                       "commercial_floating_population", FLOATING_COLS)
        load_csv_table(conn, paths["resident"],
                       "commercial_resident_population", RESIDENT_COLS)

        # 3) 인구 (행정동 단위, 폴백용)
        load_csv_table(conn, paths["floating_dong"],
                       "admin_dong_floating_population", FLOATING_DONG_COLS)
        load_csv_table(conn, paths["resident_dong"],
                       "admin_dong_resident_population", RESIDENT_DONG_COLS)

        # 4·5) 추정매출
        load_csv_table(conn, paths["sales"],
                       "commercial_estimated_sales", SALES_COLS)
        load_csv_table(conn, paths["sales_dong"],
                       "admin_dong_estimated_sales", SALES_DONG_COLS)

        print()
        print(f"=> {schema.DB_FILENAME} 상권 영역: 7개 테이블 적재 완료")
    finally:
        conn.close()


if __name__ == "__main__":
    build()
