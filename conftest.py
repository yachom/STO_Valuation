"""pytest 루트 conftest — repo 루트를 sys.path에 앵커하고 .env를 best-effort 로드.

이 repo는 패키징 설정(pyproject/setup)이 없어, infra/real_estate/music_copyright
절대 import가 어디서 pytest를 돌리든 동작하도록 루트를 명시적으로 넣는다. .env 로드는
일부 에이전트 생성자가 환경변수(NAVER/TAVILY 키 등)를 보고 검색 도구를 켜기 때문 —
인스턴스화만 검증하는 특성화 테스트가 운영과 같은 경로를 타게 한다(네트워크 호출 없음).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:  # best-effort — .env가 없는 CI에서도 테스트는 진행
    from infra.env import load_env

    load_env()
except Exception:  # noqa: BLE001
    pass
