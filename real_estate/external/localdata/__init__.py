"""LOCALDATA(지방행정 인허가데이터) 외부 어댑터.

상가 인허가/폐업 이력(임차 회전율)을 도로명주소 단위로 가져온다.
client는 envelope 처리와 정규화만 책임지고, 어떤 업종(opnSvcId)·지역(localCode)·
기간을 부를지는 호출자(repository/collector)가 결정한다.
"""

from real_estate.external.localdata.client import (
    BusinessLicense,
    LocalDataClient,
    LocalDataError,
)

__all__ = ["LocalDataClient", "LocalDataError", "BusinessLicense"]
