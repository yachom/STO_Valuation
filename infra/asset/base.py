"""모든 자산이 상속하는 얇은 베이스.

자산 종류와 무관하게 통합적으로 관리되어야 하는 항목만 담는다:

    - asset_id:   자산을 한 줄로 식별하는 stable key.
                  로깅·DB·캐시·에이전트 상태에서 자산을 가리키는 키로 사용.
                  형식은 자산별로 자유 (예: "re:서울_강남_역삼동_123-4",
                  "mc:ISRC:USRC17607839"). 안정적으로 같은 자산을 같은 키로
                  표현할 수 있기만 하면 된다.
    - asset_type: 자산 종류 식별자. 예: "real_estate", "music_copyright".
                  로거 이름·디스패치·필터링에 사용.

자산별 실제 속성(주소·좌표·ISRC·권리자 등)은 서브클래스가 자유롭게 더한다.
이 베이스는 일부러 얇게 유지한다 — 자산이 늘어나며 진짜 공통이라 확인된
것만 추가한다.
"""

from dataclasses import dataclass
import logging


@dataclass
class Asset:
    asset_id: str
    asset_type: str

    def get_logger(self) -> logging.Logger:
        """이 자산의 컨텍스트가 박힌 로거를 돌려준다.

        로거 이름이 `asset.{asset_type}.{asset_id}` 형태라
        로그를 자산 종류별·자산별로 필터링/집계하기 쉽다.
        """
        return logging.getLogger(f"asset.{self.asset_type}.{self.asset_id}")
