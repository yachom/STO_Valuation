"""음악저작권 자산 클래스.

infra.asset.Asset(얇은 공통 베이스)을 상속해 음악저작권 특유의 속성을 더한다.
real_estate/asset.py의 RealEstate를 미러링한다 — RealEstate가 LocationInfo를
본체 속성으로 갖듯, MusicCopyright는 WorkIdentity(해소 결과)를 갖는다.

값 객체(WorkIdentity)와 인식자(WorkResolver)는 별도 파일(music_copyright/resolver.py).
이 파일은 "음악저작권이라는 자산은 무엇으로 식별되고 무엇을 갖는가"만 표현한다.

식별자(설계 확정):
    앵커 = ISWC(작품 = 저작권 본체). 실무 조인 = ISRC(녹음).
    asset_id = "mc:{work_id}", work_id = iswc | "isrc:"+isrc | "q:"+해시.
"""

from __future__ import annotations

from dataclasses import dataclass

from infra.asset import Asset
from music_copyright.resolver import WorkIdentity, WorkResolver


@dataclass
class MusicCopyright(Asset):
    # ── 도메인 속성 ──
    # 해소 결과 전체(다운스트림이 ISRC/ISWC/Spotify ID 등을 골라 쓴다).
    # 외부 해소 없이 만든 경우(from_work_id) identity는 None.
    work_id: str = ""
    title: str | None = None
    identity: WorkIdentity | None = None

    @classmethod
    def from_query(
        cls,
        title: str,
        artist: str | None,
        resolver: WorkResolver,
    ) -> "MusicCopyright":
        """제목+아티스트 한 줄로 완성된 MusicCopyright를 만든다.

        real_estate.RealEstate.from_address에 대응하는 entry point. 호출자가 매번
        resolver.resolve() → asset_id 생성 → 인스턴스 조립을 반복하지 않도록 응집한다.

        asset_id 생성 규칙: 해소된 WorkIdentity가 가진 가장 강한 식별자
        (ISWC > ISRC > 질의해시)로 work_id를 정하고 "mc:{work_id}"로 만든다.
        """
        ident = resolver.resolve(title=title, artist=artist)
        return cls(
            asset_id=ident.asset_id,
            asset_type="music_copyright",
            work_id=ident.work_id,
            title=ident.title,
            identity=ident,
        )

    @classmethod
    def from_work_id(cls, work_id: str, *, title: str | None = None) -> "MusicCopyright":
        """이미 알고 있는 정규 식별자(ISWC/ISRC/내부키)로 직접 만든다.

        외부 해소를 건너뛰는 경로 — 재평가(이미 work_id 보유)나 테스트에 쓴다.
        asset_id 생성 규칙: "mc:{work_id}".
        """
        return cls(
            asset_id=f"mc:{work_id}",
            asset_type="music_copyright",
            work_id=work_id,
            title=title,
        )
