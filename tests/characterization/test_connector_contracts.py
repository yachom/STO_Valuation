"""특성화(안전망) — 부동산 전용 정형 커넥터의 공개 API를 잠근다.

Phase A는 이 6개 커넥터를 ``infra/external/`` → ``real_estate/external/`` 로 옮긴다
(인터넷 검색 커넥터만 infra에 남기는 설계 원칙). 이동 후에도 각 커넥터의 공개 심볼이
그대로 import 되어야 한다.

운용:
  - 지금(이동 전): ``CONNECTOR_HOME = "infra.external"`` 로 현재 위치 계약을 고정.
  - Phase A(이동 후): ``CONNECTOR_HOME`` 을 ``"real_estate.external"`` 로 바꾸고,
    ``test_old_infra_path_still_imports`` 로 infra의 임시 re-export shim 도 검증한다.
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

# Phase A가 "real_estate.external" 로 바꾼다.
CONNECTOR_HOME = "infra.external"


@pytest.mark.parametrize("suffix,symbols", CONNECTORS, ids=[c[0] for c in CONNECTORS])
def test_connector_public_api(suffix, symbols):
    """커넥터의 공개 심볼이 정규 위치에서 import 된다."""
    mod = importlib.import_module(f"{CONNECTOR_HOME}.{suffix}")
    missing = [s for s in symbols if not hasattr(mod, s)]
    assert not missing, f"{CONNECTOR_HOME}.{suffix} missing {missing}"
