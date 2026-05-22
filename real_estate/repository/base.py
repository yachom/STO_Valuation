"""모든 데이터 소스의 베이스 추상 클래스.

이 파일은 모든 데이터 조회 클래스(`*Repository`)가 따라야 할 최소한의 약속을
정의한다.

규약:
    1. 모든 데이터 소스는 LocationInfo를 입력으로 받는다.
    2. 결과는 파이썬 dict로 반환한다.
    3. dict의 구체적인 키 구조는 각 데이터별 추상 클래스의 docstring에
       명시한다.
    4. 데이터에 없는 필드는 None으로 표현한다 (빈 문자열 X).

이 베이스에는 일부러 시그니처만 강제하고 그 이상은 강제하지 않는다.
이유는 데이터마다 자원의 형태가 다르기 때문이다:
    - SQLite 파일 경로일 수도 있고
    - 외부 API의 URL + 키일 수도 있고
    - 메모리 dict일 수도 있다.
구현체의 생성자는 각자 자유롭게 설계한다.

향후 공통 패턴이 드러나면 베이스에 메서드를 추가할 수 있다 (예: 캐싱,
에러 처리, 로깅). 지금은 헐겁게 두는 게 시작에 유리하다.
"""

from abc import ABC, abstractmethod

from STO_Valuation.real_estate.location import LocationInfo


class BaseRepository(ABC):
    """모든 데이터 소스 조회 클래스의 추상 베이스.

    상속받는 자식 클래스는 fetch()를 반드시 구현해야 한다.
    """

    @abstractmethod
    def fetch(self, location: LocationInfo) -> dict:
        """LocationInfo를 받아 해당 데이터를 dict로 반환한다.

        Args:
            location: 정규화된 위치 정보 객체.
                      구현체는 location의 필드 중 자기에게 필요한 것만 골라 쓴다.
                      (예: 환경등급은 location.bname, 인구는 좌표, 상권은 hcode)

        Returns:
            데이터별로 정해진 dict. 구체적인 키 구조는 각 자식 클래스의
            docstring에 명시되어 있다. 데이터가 없는 필드는 None.

        Raises:
            LookupError: 해당 위치에 데이터가 존재하지 않는 경우.
            ValueError:  필요한 location 필드가 비어있는 경우.
        """
        ...
