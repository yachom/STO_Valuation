"""건축HUB 건물에너지(전기+가스) — ESG-E 정량 지표. building_hub와 평행, 동일 키.

전기·가스 사용량(useQty, kWh) 연 합산 → 1차에너지 환산(전기 2.75/가스 1.1) → 연면적으로
나눠 실사용 추정 등급. 로거: external.building_energy.
"""

from real_estate.external.building_energy.repository import (
    API_NAME,
    BuildingEnergyRepository,
    grade_for_primary,
    primary_energy_assessment,
)

__all__ = ["API_NAME", "BuildingEnergyRepository", "primary_energy_assessment",
           "grade_for_primary"]
