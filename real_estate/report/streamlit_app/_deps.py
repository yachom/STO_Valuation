"""Streamlit 앱에서 공유하는 의존성 (Neo4j driver, LocationResolver, Repository).

Streamlit은 매 interaction마다 스크립트를 top-to-bottom 재실행하므로
st.cache_resource로 driver/repo 등 무거운 객체를 한 번만 만들고 재사용한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from infra.env import load_env
from real_estate.location import LocationResolver
from real_estate.report.graph.driver import get_neo4j_driver
from real_estate.report.tool.retriever.naive_retriever import NaiveNeighborhoodRetriever
from real_estate.report.storage.report_repo import ReportRepository

DB_PATH = Path("real_estate/database/DB/real_estate.db")


@st.cache_resource(show_spinner=False)
def get_loaded_env() -> dict:
    load_env()
    keys = [
        "ANTHROPIC_API_KEY", "NEO4J_PASSWORD",
        "KAKAO_API_KEY", "JUSO_API_KEY",
    ]
    missing = [k for k in keys if not os.environ.get(k)]
    return {"missing": missing}


@st.cache_resource(show_spinner=False)
def get_driver():
    return get_neo4j_driver()


@st.cache_resource(show_spinner=False)
def get_repo() -> ReportRepository:
    return ReportRepository(DB_PATH)


@st.cache_resource(show_spinner=False)
def get_resolver() -> LocationResolver:
    return LocationResolver(
        kakao_api_key=os.environ["KAKAO_API_KEY"],
        juso_api_key=os.environ["JUSO_API_KEY"],
    )


@st.cache_resource(show_spinner=False)
def get_retriever() -> NaiveNeighborhoodRetriever:
    return NaiveNeighborhoodRetriever(get_driver())
