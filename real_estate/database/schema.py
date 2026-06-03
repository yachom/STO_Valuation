"""real_estate.db 통합 스키마.

5개 출처(BEEC, G-SEED, 서울 상권분석, KB 단지, 서울 정비사업)의 데이터를
도메인별로 정규화해 한 SQLite 파일에 담는다.

설계 원칙:
    1. 건물·행정동·상권·단지 같은 **자연 키가 있는 것은 마스터 테이블**로 1행 1엔티티.
    2. 인증·시계열·이벤트는 **마스터 FK로 연결되는 디테일 테이블**.
    3. 같은 건물에 BEEC + G-SEED 인증이 있어도 building 마스터는 1행, 인증은 별도.
    4. 자연 키(bdmgt_sn, hjdong_cd, trdar_cd 등)를 PK로 직접 사용.
    5. 빌더는 자기 테이블만 책임 — 다른 빌더와 충돌 없음.

각 빌더는 시작 시 ensure_schema(conn)을 호출하면 충분하다.
INSERT OR REPLACE / INSERT OR IGNORE 정책은 호출자가 결정한다.
"""

# 단일 DB 파일 경로 (real_estate/database/DB/real_estate.db)
DB_FILENAME = "real_estate.db"


# ───────────────────────────────────────────────────────────
# 마스터 테이블
#
# 정규화 원칙은 가지되 (building, admin_dong 등) 실제 populator가 생기는
# 시점까지 도입을 보류한다. cert 테이블들이 bdmgt_sn을 보유하므로 그 자체로
# join key 역할을 한다.
# ───────────────────────────────────────────────────────────
COMMERCIAL_AREA = """
CREATE TABLE IF NOT EXISTS commercial_area (
    trdar_cd        TEXT PRIMARY KEY,
    trdar_se_name   TEXT,
    trdar_name      TEXT,
    signgu_cd       TEXT,
    adstrd_cd       TEXT,              -- 행정동코드 (8자리 또는 10자리)
    adstrd_name     TEXT,
    area_sqm        REAL,
    center_lon      REAL,
    center_lat      REAL,
    bbox_min_lon    REAL,
    bbox_min_lat    REAL,
    bbox_max_lon    REAL,
    bbox_max_lat    REAL,
    polygon_json    TEXT
)
"""
COMMERCIAL_AREA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_commercial_area_bbox ON commercial_area(bbox_min_lon, bbox_max_lon, bbox_min_lat, bbox_max_lat)",
    "CREATE INDEX IF NOT EXISTS idx_commercial_area_dong ON commercial_area(adstrd_cd)",
]


APARTMENT_COMPLEX = """
CREATE TABLE IF NOT EXISTS apartment_complex (
    master_complex_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name      TEXT NOT NULL,
    road_address        TEXT,
    legal_address       TEXT,
    latitude            REAL NOT NULL,
    longitude           REAL NOT NULL,
    household_count     INTEGER,
    completion_date     TEXT,
    total_buildings     INTEGER,
    seoul_complex_code  TEXT UNIQUE,
    kb_complex_id       INTEGER UNIQUE,
    data_source         TEXT NOT NULL,
    built_at            TEXT NOT NULL
)
"""
APARTMENT_COMPLEX_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_apt_complex_lat       ON apartment_complex(latitude)",
    "CREATE INDEX IF NOT EXISTS idx_apt_complex_lng       ON apartment_complex(longitude)",
    "CREATE INDEX IF NOT EXISTS idx_apt_complex_seoul_code ON apartment_complex(seoul_complex_code)",
    "CREATE INDEX IF NOT EXISTS idx_apt_complex_kb_id     ON apartment_complex(kb_complex_id)",
]


# ───────────────────────────────────────────────────────────
# 건물 인증 (bdmgt_sn 컬럼이 cert 간·자산 간 조인 키)
# 풍부한 cert 단일 테이블을 유지하되 모두 같은 DB 안에 두어 JOIN 가능.
# ───────────────────────────────────────────────────────────
ENERGY_EFFICIENCY_CERT = """
CREATE TABLE IF NOT EXISTS energy_efficiency_cert (
    rqid                INTEGER PRIMARY KEY,   -- BEEC 원본 RQID
    crtif_no            TEXT,                  -- 인증번호
    bld_nm              TEXT NOT NULL,
    loc_addr            TEXT,                  -- BEEC 원본 주소
    rgn_sct_name        TEXT,
    full_addr           TEXT,                  -- 지역 + 주소
    jibun_addr          TEXT,                  -- 카카오 보강
    bdmgt_sn            TEXT,                  -- JUSO 보강 (cert↔자산 조인 키)
    purpose_kind_name   TEXT,
    bld_scale           TEXT,
    hshld_numb          INTEGER,
    exus_area_m2        REAL,
    real_area_m2        REAL,
    crtif_kind          TEXT,                  -- 본인증/예비
    grd_name            TEXT,
    grade_code          TEXT,
    grade_rank          INTEGER,
    crtif_isu_dd        TEXT,                  -- 인증일자
    app_rcpt_org_name   TEXT,
    ente_nm             TEXT,
    primary_energy_req  REAL,
    energy_reduce_rate  REAL,
    src_reg_dt          TEXT,
    fetched_at          TEXT,
    ingested_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
ENERGY_EFFICIENCY_CERT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_eec_bdmgt      ON energy_efficiency_cert(bdmgt_sn)",
    "CREATE INDEX IF NOT EXISTS idx_eec_jibun      ON energy_efficiency_cert(jibun_addr)",
    "CREATE INDEX IF NOT EXISTS idx_eec_crtif_no   ON energy_efficiency_cert(crtif_no)",
    "CREATE INDEX IF NOT EXISTS idx_eec_region     ON energy_efficiency_cert(rgn_sct_name)",
    "CREATE INDEX IF NOT EXISTS idx_eec_grade_rank ON energy_efficiency_cert(grade_rank)",
    "CREATE INDEX IF NOT EXISTS idx_eec_purpose    ON energy_efficiency_cert(purpose_kind_name, crtif_kind)",
    "CREATE INDEX IF NOT EXISTS idx_eec_bld_nm     ON energy_efficiency_cert(bld_nm)",
]


GREEN_CERT = """
CREATE TABLE IF NOT EXISTS green_cert (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    cert_no            TEXT NOT NULL,
    province           TEXT NOT NULL,
    cert_standard      TEXT NOT NULL,
    cert_type          TEXT NOT NULL,
    building_name      TEXT NOT NULL,
    building_category  TEXT NOT NULL,
    cert_grade         TEXT NOT NULL,
    cert_date          TEXT NOT NULL,
    address            TEXT NOT NULL,         -- GSEED 원본 주소
    jibun_addr         TEXT,                  -- 카카오 보강
    bdmgt_sn           TEXT,                  -- JUSO 보강 (조인 키)
    total_floor_area   REAL,
    floors_above       INTEGER,
    floors_below       INTEGER
)
"""
GREEN_CERT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_gc_bdmgt   ON green_cert(bdmgt_sn)",
    "CREATE INDEX IF NOT EXISTS idx_gc_jibun   ON green_cert(jibun_addr)",
    "CREATE INDEX IF NOT EXISTS idx_gc_cert_no ON green_cert(cert_no)",
    "CREATE INDEX IF NOT EXISTS idx_gc_address ON green_cert(address)",
    "CREATE INDEX IF NOT EXISTS idx_gc_bld_nm  ON green_cert(building_name)",
]


# ───────────────────────────────────────────────────────────
# 상권 시계열 (FK commercial_area)
# ───────────────────────────────────────────────────────────
COMMERCIAL_FLOATING_POPULATION = """
CREATE TABLE IF NOT EXISTS commercial_floating_population (
    quarter_code    TEXT NOT NULL,
    trdar_se_code   TEXT,
    trdar_se_name   TEXT,
    trdar_cd        TEXT NOT NULL,
    trdar_name      TEXT,
    total           INTEGER,
    male            INTEGER, female INTEGER,
    age_10 INTEGER, age_20 INTEGER, age_30 INTEGER,
    age_40 INTEGER, age_50 INTEGER, age_60_over INTEGER,
    time_00_06 INTEGER, time_06_11 INTEGER, time_11_14 INTEGER,
    time_14_17 INTEGER, time_17_21 INTEGER, time_21_24 INTEGER,
    mon INTEGER, tue INTEGER, wed INTEGER, thu INTEGER,
    fri INTEGER, sat INTEGER, sun INTEGER,
    PRIMARY KEY (trdar_cd, quarter_code)
)
"""

COMMERCIAL_RESIDENT_POPULATION = """
CREATE TABLE IF NOT EXISTS commercial_resident_population (
    quarter_code    TEXT NOT NULL,
    trdar_se_code   TEXT,
    trdar_se_name   TEXT,
    trdar_cd        TEXT NOT NULL,
    trdar_name      TEXT,
    total           INTEGER,
    male            INTEGER, female INTEGER,
    age_10 INTEGER, age_20 INTEGER, age_30 INTEGER,
    age_40 INTEGER, age_50 INTEGER, age_60_over INTEGER,
    male_10 INTEGER, male_20 INTEGER, male_30 INTEGER,
    male_40 INTEGER, male_50 INTEGER, male_60_over INTEGER,
    female_10 INTEGER, female_20 INTEGER, female_30 INTEGER,
    female_40 INTEGER, female_50 INTEGER, female_60_over INTEGER,
    household INTEGER,
    apartment_household INTEGER,
    non_apartment_household INTEGER,
    PRIMARY KEY (trdar_cd, quarter_code)
)
"""

COMMERCIAL_ESTIMATED_SALES = """
CREATE TABLE IF NOT EXISTS commercial_estimated_sales (
    quarter_code    TEXT NOT NULL,
    trdar_se_code   TEXT,
    trdar_se_name   TEXT,
    trdar_cd        TEXT NOT NULL,
    trdar_name      TEXT,
    industry_code   TEXT NOT NULL,
    industry_name   TEXT,
    sales_amount        INTEGER, sales_count INTEGER,
    weekday_amount      INTEGER, weekend_amount INTEGER,
    mon_amount INTEGER, tue_amount INTEGER, wed_amount INTEGER,
    thu_amount INTEGER, fri_amount INTEGER, sat_amount INTEGER, sun_amount INTEGER,
    time_00_06_amount INTEGER, time_06_11_amount INTEGER, time_11_14_amount INTEGER,
    time_14_17_amount INTEGER, time_17_21_amount INTEGER, time_21_24_amount INTEGER,
    male_amount INTEGER, female_amount INTEGER,
    age_10_amount INTEGER, age_20_amount INTEGER, age_30_amount INTEGER,
    age_40_amount INTEGER, age_50_amount INTEGER, age_60_over_amount INTEGER,
    weekday_count INTEGER, weekend_count INTEGER,
    mon_count INTEGER, tue_count INTEGER, wed_count INTEGER,
    thu_count INTEGER, fri_count INTEGER, sat_count INTEGER, sun_count INTEGER,
    time_00_06_count INTEGER, time_06_11_count INTEGER, time_11_14_count INTEGER,
    time_14_17_count INTEGER, time_17_21_count INTEGER, time_21_24_count INTEGER,
    male_count INTEGER, female_count INTEGER,
    age_10_count INTEGER, age_20_count INTEGER, age_30_count INTEGER,
    age_40_count INTEGER, age_50_count INTEGER, age_60_over_count INTEGER,
    PRIMARY KEY (trdar_cd, quarter_code, industry_code)
)
"""


# ───────────────────────────────────────────────────────────
# 행정동 폴백 시계열 (FK admin_dong)
# 상권 매칭 실패 시 행정동 단위로 폴백 조회
# ───────────────────────────────────────────────────────────
ADMIN_DONG_FLOATING_POPULATION = """
CREATE TABLE IF NOT EXISTS admin_dong_floating_population (
    quarter_code TEXT NOT NULL,
    adstrd_cd    TEXT NOT NULL,
    adstrd_name  TEXT,
    total INTEGER,
    male INTEGER, female INTEGER,
    age_10 INTEGER, age_20 INTEGER, age_30 INTEGER,
    age_40 INTEGER, age_50 INTEGER, age_60_over INTEGER,
    time_00_06 INTEGER, time_06_11 INTEGER, time_11_14 INTEGER,
    time_14_17 INTEGER, time_17_21 INTEGER, time_21_24 INTEGER,
    mon INTEGER, tue INTEGER, wed INTEGER, thu INTEGER,
    fri INTEGER, sat INTEGER, sun INTEGER,
    PRIMARY KEY (adstrd_cd, quarter_code)
)
"""

ADMIN_DONG_RESIDENT_POPULATION = """
CREATE TABLE IF NOT EXISTS admin_dong_resident_population (
    quarter_code  TEXT NOT NULL,
    adstrd_cd     TEXT NOT NULL,
    adstrd_name   TEXT,
    total INTEGER, male INTEGER, female INTEGER,
    age_10 INTEGER, age_20 INTEGER, age_30 INTEGER,
    age_40 INTEGER, age_50 INTEGER, age_60_over INTEGER,
    male_10 INTEGER, male_20 INTEGER, male_30 INTEGER,
    male_40 INTEGER, male_50 INTEGER, male_60_over INTEGER,
    female_10 INTEGER, female_20 INTEGER, female_30 INTEGER,
    female_40 INTEGER, female_50 INTEGER, female_60_over INTEGER,
    household INTEGER,
    apartment_household INTEGER,
    non_apartment_household INTEGER,
    PRIMARY KEY (adstrd_cd, quarter_code)
)
"""

ADMIN_DONG_ESTIMATED_SALES = """
CREATE TABLE IF NOT EXISTS admin_dong_estimated_sales (
    quarter_code    TEXT NOT NULL,
    adstrd_cd       TEXT NOT NULL,
    adstrd_name     TEXT,
    industry_code   TEXT NOT NULL,
    industry_name   TEXT,
    sales_amount    INTEGER, sales_count INTEGER,
    weekday_amount  INTEGER, weekend_amount INTEGER,
    mon_amount INTEGER, tue_amount INTEGER, wed_amount INTEGER,
    thu_amount INTEGER, fri_amount INTEGER, sat_amount INTEGER, sun_amount INTEGER,
    time_00_06_amount INTEGER, time_06_11_amount INTEGER, time_11_14_amount INTEGER,
    time_14_17_amount INTEGER, time_17_21_amount INTEGER, time_21_24_amount INTEGER,
    male_amount INTEGER, female_amount INTEGER,
    age_10_amount INTEGER, age_20_amount INTEGER, age_30_amount INTEGER,
    age_40_amount INTEGER, age_50_amount INTEGER, age_60_over_amount INTEGER,
    weekday_count INTEGER, weekend_count INTEGER,
    mon_count INTEGER, tue_count INTEGER, wed_count INTEGER,
    thu_count INTEGER, fri_count INTEGER, sat_count INTEGER, sun_count INTEGER,
    time_00_06_count INTEGER, time_06_11_count INTEGER, time_11_14_count INTEGER,
    time_14_17_count INTEGER, time_17_21_count INTEGER, time_21_24_count INTEGER,
    male_count INTEGER, female_count INTEGER,
    age_10_count INTEGER, age_20_count INTEGER, age_30_count INTEGER,
    age_40_count INTEGER, age_50_count INTEGER, age_60_over_count INTEGER,
    PRIMARY KEY (adstrd_cd, quarter_code, industry_code)
)
"""


# ───────────────────────────────────────────────────────────
# 단지 — KB 원본 raw (FK apartment_complex)
# 기존 complex_unified.db의 raw 테이블 구조 유지
# ───────────────────────────────────────────────────────────
COMPLEX_MATCH_LOG = """
CREATE TABLE IF NOT EXISTS complex_match_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    master_complex_id INTEGER,
    seoul_complex_code TEXT,
    kb_complex_id     INTEGER,
    needs_review      INTEGER,
    reason            TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
COMPLEX_MATCH_LOG_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_cml_master ON complex_match_log(master_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_cml_review ON complex_match_log(needs_review) WHERE needs_review = 1",
]


# ───────────────────────────────────────────────────────────
# 정비사업 (seoul_cleanup)
# ───────────────────────────────────────────────────────────
DEVELOPMENT_PROJECT = """
CREATE TABLE IF NOT EXISTS development_project (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    seq_no              INTEGER,             -- 원본 '번호'
    district            TEXT,                -- 자치구
    category            TEXT,                -- 사업구분 (재건축 등)
    name                TEXT,                -- 사업장명
    jibun               TEXT,                -- 대표지번
    stage               TEXT,                -- 진행단계
    operation_kind      TEXT,                -- 운영구분
    operation_stage     TEXT,                -- 운영단계
    public_doc_count    TEXT,
    timeliness          TEXT,
    completeness        TEXT,
    built_at            TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
DEVELOPMENT_PROJECT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_dev_proj_district ON development_project(district)",
    "CREATE INDEX IF NOT EXISTS idx_dev_proj_jibun    ON development_project(jibun)",
]


# ───────────────────────────────────────────────────────────
# 임대차/거래 이벤트 (테넌트 가치 평가용)
# MOLIT RTMS 아파트 매매 실거래가가 첫 소스. 단지명은 자유 텍스트이므로
# bcode 범위 내 fuzzy 매칭 후 master_complex_id를 보강한다 (실패 시 NULL).
# ───────────────────────────────────────────────────────────
APT_SALE_TRANSACTION = """
CREATE TABLE IF NOT EXISTS apt_sale_transaction (
    transaction_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_year_month    TEXT NOT NULL,         -- YYYYMM
    deal_day           INTEGER,
    deal_amount        INTEGER,               -- 거래금액 (만원 단위)
    lawd_cd            TEXT NOT NULL,         -- 시군구 LAWD_CD (5자리)
    legal_dong         TEXT,                  -- 법정동명
    bcode              TEXT,                  -- 법정동 10자리 (시군구+읍면동), 가능한 경우
    jibun              TEXT,                  -- 지번
    road_name          TEXT,                  -- 도로명 (있을 경우)
    apt_name           TEXT NOT NULL,         -- 단지명 (free text)
    area_sqm           REAL,                  -- 전용면적
    floor              INTEGER,
    build_year         INTEGER,
    is_canceled        INTEGER DEFAULT 0,     -- 해제여부 (Y=1, N=0)
    cancel_date        TEXT,                  -- 해제사유발생일 (YYYYMMDD)
    master_complex_id  INTEGER,               -- nullable, fuzzy 매칭 결과
    raw_payload        TEXT,                  -- 원본 dict JSON dump (audit)
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lawd_cd, deal_year_month, deal_day, apt_name, area_sqm, floor, deal_amount, jibun)
)
"""
APT_SALE_TRANSACTION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_apt_sale_lawd_ymd   ON apt_sale_transaction(lawd_cd, deal_year_month)",
    "CREATE INDEX IF NOT EXISTS idx_apt_sale_apt_name   ON apt_sale_transaction(apt_name)",
    "CREATE INDEX IF NOT EXISTS idx_apt_sale_complex_id ON apt_sale_transaction(master_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_apt_sale_bcode      ON apt_sale_transaction(bcode)",
]


# MOLIT 아파트 전월세 실거래가. contract_type='전세' 면 monthly_rent=0.
APT_RENT_TRANSACTION = """
CREATE TABLE IF NOT EXISTS apt_rent_transaction (
    rent_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_year_month    TEXT NOT NULL,         -- YYYYMM
    deal_day           INTEGER,
    deposit            INTEGER,               -- 보증금 (만원 단위)
    monthly_rent       INTEGER,               -- 월세 (만원 단위, 전세는 0)
    contract_type      TEXT,                  -- 전세 / 월세 (deposit/monthly_rent 기반 산출)
    contract_period    TEXT,                  -- 계약기간 (yymm~yymm, 가능한 경우)
    use_renewal_right  INTEGER DEFAULT 0,     -- 갱신요구권 사용 여부 (Y=1, N=0)
    previous_deposit   INTEGER,               -- 종전계약 보증금 (갱신만)
    previous_monthly_rent INTEGER,            -- 종전계약 월세 (갱신만)
    lawd_cd            TEXT NOT NULL,
    legal_dong         TEXT,
    bcode              TEXT,
    jibun              TEXT,
    apt_name           TEXT NOT NULL,
    area_sqm           REAL,
    floor              INTEGER,
    build_year         INTEGER,
    master_complex_id  INTEGER,
    raw_payload        TEXT,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lawd_cd, deal_year_month, deal_day, apt_name, area_sqm, floor, deposit, monthly_rent, jibun)
)
"""
APT_RENT_TRANSACTION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_apt_rent_lawd_ymd    ON apt_rent_transaction(lawd_cd, deal_year_month)",
    "CREATE INDEX IF NOT EXISTS idx_apt_rent_apt_name    ON apt_rent_transaction(apt_name)",
    "CREATE INDEX IF NOT EXISTS idx_apt_rent_complex_id  ON apt_rent_transaction(master_complex_id)",
    "CREATE INDEX IF NOT EXISTS idx_apt_rent_bcode       ON apt_rent_transaction(bcode)",
    "CREATE INDEX IF NOT EXISTS idx_apt_rent_contract    ON apt_rent_transaction(contract_type)",
]


# LH 공공임대주택. apt 거래와 보완 — 같은 bcode 안에 LH 단지가 있으면
# 그 지역의 임대 시장 구성을 더 정확히 파악할 수 있다 (테넌트 가치).
PUBLIC_LEASE_COMPLEX = """
CREATE TABLE IF NOT EXISTS public_lease_complex (
    lh_complex_id     TEXT PRIMARY KEY,        -- LH 단지 고유번호
    complex_name      TEXT NOT NULL,
    lease_type        TEXT,                    -- 영구임대/국민임대/장기전세/행복주택 등
    region_name       TEXT,                    -- 시도 + 시군구
    bcode             TEXT,                    -- 법정동 코드 10자리 (가능한 경우)
    sigungu_cd        TEXT,                    -- 시군구 5자리
    road_address      TEXT,
    legal_address     TEXT,
    latitude          REAL,
    longitude         REAL,
    household_count   INTEGER,
    completion_date   TEXT,
    raw_payload       TEXT,
    fetched_at        TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
PUBLIC_LEASE_COMPLEX_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_public_lease_sigungu ON public_lease_complex(sigungu_cd)",
    "CREATE INDEX IF NOT EXISTS idx_public_lease_bcode   ON public_lease_complex(bcode)",
    "CREATE INDEX IF NOT EXISTS idx_public_lease_type    ON public_lease_complex(lease_type)",
]


# ───────────────────────────────────────────────────────────
# 일괄 적용
# ───────────────────────────────────────────────────────────
ALL_DDL = [
    ("commercial_area", COMMERCIAL_AREA, COMMERCIAL_AREA_INDEXES),
    ("apartment_complex", APARTMENT_COMPLEX, APARTMENT_COMPLEX_INDEXES),
    ("energy_efficiency_cert", ENERGY_EFFICIENCY_CERT, ENERGY_EFFICIENCY_CERT_INDEXES),
    ("green_cert", GREEN_CERT, GREEN_CERT_INDEXES),
    ("commercial_floating_population", COMMERCIAL_FLOATING_POPULATION, []),
    ("commercial_resident_population", COMMERCIAL_RESIDENT_POPULATION, []),
    ("commercial_estimated_sales", COMMERCIAL_ESTIMATED_SALES, []),
    ("admin_dong_floating_population", ADMIN_DONG_FLOATING_POPULATION, []),
    ("admin_dong_resident_population", ADMIN_DONG_RESIDENT_POPULATION, []),
    ("admin_dong_estimated_sales", ADMIN_DONG_ESTIMATED_SALES, []),
    ("complex_match_log", COMPLEX_MATCH_LOG, COMPLEX_MATCH_LOG_INDEXES),
    ("development_project", DEVELOPMENT_PROJECT, DEVELOPMENT_PROJECT_INDEXES),
    ("apt_sale_transaction", APT_SALE_TRANSACTION, APT_SALE_TRANSACTION_INDEXES),
    ("apt_rent_transaction", APT_RENT_TRANSACTION, APT_RENT_TRANSACTION_INDEXES),
    ("public_lease_complex", PUBLIC_LEASE_COMPLEX, PUBLIC_LEASE_COMPLEX_INDEXES),
]


def ensure_schema(conn):
    """모든 테이블·인덱스를 생성. idempotent (이미 있으면 no-op)."""
    cur = conn.cursor()
    for name, ddl, indexes in ALL_DDL:
        cur.execute(ddl)
        for idx in indexes:
            cur.execute(idx)
    conn.commit()
