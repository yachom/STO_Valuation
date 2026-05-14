"""주소 → 정규화 메타데이터 번들 생성."""

from __future__ import annotations

from typing import Any

from ..tools import building as building_tool
from ..tools import juso as juso_tool
from ..tools import kakao_local
from ..tools import vworld as vworld_tool


def resolve_address(address: str) -> dict[str, Any]:
    juso_result = juso_tool.search_address(address)
    top = juso_result["top"]
    pnu = juso_tool.pnu_from_juso_row(top)
    ids = juso_tool.split_pnu(pnu)

    road_addr = top.get("roadAddr") or address
    jibun_addr = top.get("jibunAddr")
    bjdong_name = top.get("emdNm")
    sido = top.get("siNm")
    sigungu = top.get("sggNm")

    coord = kakao_local.address_to_coord(road_addr) or kakao_local.address_to_coord(jibun_addr or address)
    land = vworld_tool.land_pack(pnu)
    building = building_tool.building_pack(ids)

    return {
        "input_address": address,
        "address": {
            "road": road_addr,
            "jibun": jibun_addr,
            "sido": sido,
            "sigungu": sigungu,
            "bjdong_name": bjdong_name,
            "building_management_no": top.get("bdMgtSn"),
            "building_name": top.get("bdNm"),
        },
        "identifiers": ids,
        "coord": coord,
        "land": land,
        "building": building,
    }
