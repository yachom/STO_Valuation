"""Commercial real-estate evaluation units."""

from __future__ import annotations

from common.domain.target import Target


class BuildingTarget(Target):
    """A building identified by 19-digit PNU."""

    pnu: str
    address: str | None = None

    @property
    def identifier(self) -> str:
        return self.pnu


class TenantTarget(Target):
    """A tenant (사업자) identified by 사업자등록번호."""

    business_number: str
    name: str | None = None

    @property
    def identifier(self) -> str:
        return self.business_number
