"""Centralized configuration loaded from .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip().strip('"').strip("'")


# Anthropic
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
REPORT_MODEL = _get("REPORT_MODEL", "claude-opus-4-7")
AGENT_MODEL = _get("AGENT_MODEL", "claude-sonnet-4-6")

# JUSO / MOLIT / VWorld
JUSO_CONFIRM_KEY = _get("JUSO_CONFIRM_KEY")
MOLIT_SERVICE_KEY = _get("MOLIT_SERVICE_KEY") or _get("DATA_GO_KR_SERVICE_KEY")
VWORLD_API_KEY = _get("VWORLD_API_KEY")
VWORLD_DOMAIN = _get("VWORLD_DOMAIN")
BUILDING_REGISTER_BASE_URL = _get(
    "BUILDING_REGISTER_BASE_URL",
    "https://apis.data.go.kr/1613000/BldRgstHubService",
)

# G-SEED
GSEED_SERVICE_KEY = _get("SERVICE_KEY")

# Naver
NAVER_CLIENT_ID = _get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = _get("NAVER_CLIENT_SECRET")
NAVER_SEARCHAD_CUSTOMER_ID = _get("CUSTOMER_ID")
NAVER_SEARCHAD_ACCESS_LICENSE = _get("ACCESS_LICENSE")
NAVER_SEARCHAD_SECRET_KEY = _get("SECRET_KEY")

# Kakao
KAKAO_REST_API_KEY = _get("KAKAO_REST_API_KEY")

# 소상공인진흥공단
SBIZ_SERVICE_KEY = _get("SBIZ_SERVICE_KEY")

# Langfuse
LANGFUSE_PUBLIC_KEY = _get("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = _get("LANGFUSE_SECRET_KEY")
LANGFUSE_BASE_URL = _get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

USER_AGENT = "sto-valuation-poc/0.1"
