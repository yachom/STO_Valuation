"""특성화(안전망) — 부동산 전용 정형 커넥터의 공개 API를 잠근다.

Phase A에서 이 6개 커넥터를 ``infra/external/`` → ``real_estate/external/`` 로 옮겼다
(infra/external에는 인터넷 검색 커넥터만 남기는 설계 원칙). 이동 후에도 각 커넥터의
공개 심볼이 그대로 import 되어야 한다. importer는 전부 real_estate + scripts라
임시 shim 없이 한 번에 갱신했으므로, infra 구 경로는 더 이상 존재하지 않는다(의도).
"""

from __future__ import annotations

import importlib

import pytest

# (서브패키지, [공개 심볼…]) — 각 커넥터의 client/exception/모델 공개 표면.
CONNECTORS = [
    ("kakao_local", ["KakaoLocalClient", "KakaoLocalError"]),
    ("kakao_place_scraper", ["KakaoPlaceScraper", "PlaceReview"]),
    ("localdata", ["LocalDataClient", "LocalDataError", "BusinessLicense"]),
    ("public_data_portal", ["PublicDataPortalClient", "PublicDataPortalError"]),
    ("reb_rone", ["RebRoneClient", "RebRoneError"]),
    ("seoul_open_data", ["SeoulOpenDataClient"]),
]

CONNECTOR_HOME = "real_estate.external"


@pytest.mark.parametrize("suffix,symbols", CONNECTORS, ids=[c[0] for c in CONNECTORS])
def test_connector_public_api(suffix, symbols):
    """커넥터의 공개 심볼이 정규 위치에서 import 된다."""
    mod = importlib.import_module(f"{CONNECTOR_HOME}.{suffix}")
    missing = [s for s in symbols if not hasattr(mod, s)]
    assert not missing, f"{CONNECTOR_HOME}.{suffix} missing {missing}"
