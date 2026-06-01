"""부동산 자산 클래스.

infra.asset.Asset(얇은 공통 베이스)을 상속하여 부동산 특유의 속성을 더한다.
현재는 위치(LocationInfo)만 본체 속성이고, 추후 면적·용도·건축연도·시세
등이 채워질 예정이다.

값 객체(LocationInfo)는 별도 파일(real_estate/location.py)에 둔다.
이 파일은 "부동산이라는 자산은 무엇으로 식별되고 무엇을 갖는가"만 표현한다.
"""

from dataclasses import dataclass

from infra.asset import Asset
from real_estate.location import LocationInfo, LocationResolver


@dataclass
class RealEstate(Asset):
    location: LocationInfo

    @classmethod
    def from_address(cls, address: str, resolver: LocationResolver) -> "RealEstate":
        """주소 한 줄로 완성된 RealEstate를 만든다.

        호출자가 매번 resolver.resolve() → asset_id 생성 → 인스턴스 조립을
        반복하지 않도록 응집한 entry point.

        asset_id 생성 규칙:
            건물관리번호가 있으면 "re:{bd_mgt_sn}" (가장 안정적),
            없으면 좌표로 대체 "re:{lat},{lon}".
        """
        location = resolver.resolve(address)
        if location.bd_mgt_sn is not None:
            asset_id = "re:" + location.bd_mgt_sn
        else:
            asset_id = "re:{},{}".format(location.latitude, location.longitude)
        return cls(
            asset_id=asset_id,
            asset_type="real_estate",
            location=location,
        )
