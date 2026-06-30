"""Kakao Local REST API 어댑터 — 장소(키워드/카테고리) 검색.

LocationResolver(real_estate/location.py)는 Kakao Local의 주소/좌표 변환만
쓴다. 이 패키지는 그와 별개로 '장소(POI) 검색' family를 담당한다 — 특정
건물의 입점업체 로스터 수집에 쓰인다.

로거 컨벤션: external.kakao_local (폴더명과 일치).
"""

from real_estate.external.kakao_local.client import KakaoLocalClient, KakaoLocalError

__all__ = ["KakaoLocalClient", "KakaoLocalError"]
