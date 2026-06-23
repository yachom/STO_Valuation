"""음악저작권 자산 클래스 (스캐폴드 — 도메인 미확정).

infra.asset.Asset(얇은 공통 베이스)을 상속해 음악저작권 특유의 속성을 더한다.
real_estate/asset.py의 RealEstate를 미러링하되, 식별자는 placeholder다.

⚠️ TODO(domain): 자산 단위(저작물 1곡? 앨범? 음원 ISRC? 작품 카탈로그?)가 아직
정해지지 않았다. 지금은 placeholder string key `work_id`로 식별하고, 그래프
앵커 라벨은 Work를 쓴다(real_estate의 Asset에 해당). asset_id는 "mc:{work_id}".
도메인이 확정되면 from_isrc 등 진짜 entry point + 도메인 속성(아티스트·권리자·
발매일 등)을 채운다.
"""

from __future__ import annotations

from dataclasses import dataclass

from infra.asset import Asset


@dataclass
class MusicCopyright(Asset):
    # ── placeholder 도메인 속성 ──
    # TODO(domain): work_id의 정체(ISRC/ISWC/내부키)와 추가 속성(title·artist·
    # rightsholder·release_date 등)은 도메인 확정 후 채운다. 지금은 식별 최소만.
    work_id: str
    title: str | None = None

    @classmethod
    def from_work_id(cls, work_id: str, *, title: str | None = None) -> "MusicCopyright":
        """placeholder work_id로 MusicCopyright를 만든다.

        real_estate.RealEstate.from_address에 해당하는 entry point. 지금은 외부
        해소(resolver)가 없어 work_id를 그대로 받는다 — 도메인 확정 시 ISRC 조회·
        메타데이터 보강 등을 여기에 응집한다.

        asset_id 생성 규칙(placeholder): "mc:{work_id}".
        """
        return cls(
            asset_id=f"mc:{work_id}",
            asset_type="music_copyright",
            work_id=work_id,
            title=title,
        )
