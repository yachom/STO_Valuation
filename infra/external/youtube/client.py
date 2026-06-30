"""YouTube 검색 클라이언트 — yt-dlp 기반 public metadata 추출.

YouTube HTML/API 표면은 자주 바뀌므로 직접 파싱하지 않고 yt-dlp에 추출 계층을
위임한다. 이 클라이언트는 검색(`ytsearchN:`) 결과를 graphflow 검색 채널 공통
형태로 정규화할 수 있는 dataclass로 반환한다.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from infra.external.base_client import BaseExternalClient
from infra.external.youtube.exceptions import (
    YouTubeDependencyError,
    YouTubeExtractError,
)
from infra.gateway import APIGateway

logger = logging.getLogger("external.youtube")


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class YouTubeSearchResult:
    title: str
    url: str
    description: str
    published_at: str | None
    channel: str | None
    video_id: str | None
    duration: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return "youtube.com"

    @property
    def content(self) -> str:
        bits = [self.description.strip()]
        meta = []
        if self.channel:
            meta.append(f"channel={self.channel}")
        if self.view_count is not None:
            meta.append(f"views={self.view_count}")
        if self.like_count is not None:
            meta.append(f"likes={self.like_count}")
        if self.duration is not None:
            meta.append(f"duration={self.duration}s")
        if meta:
            bits.append("YouTube metadata: " + ", ".join(meta))
        return "\n".join(x for x in bits if x)


class _YtdlpLogger:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: str) -> None:
        if self.verbose:
            logger.debug(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)
        if self.verbose:
            logger.warning(message)

    def error(self, message: str) -> None:
        self.errors.append(message)
        logger.debug("yt-dlp error: %s", message)


class YouTubeClient(BaseExternalClient):
    """yt-dlp thin wrapper. Optional dependency라 import는 호출 시점에 수행한다.
    (검색 호출 성공/실패는 BaseExternalClient._audited로 audit 기록·오류 통지)"""

    API_NAME = "youtube"
    api_name = "youtube"

    def __init__(
        self,
        *,
        timeout: int = 20,
        retries: int = 2,
        gateway: APIGateway | None = None,
        verbose: bool = False,
    ):
        super().__init__(gateway=gateway, timeout=timeout)
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose

    def search(self, query: str, *, max_results: int = 5) -> list[YouTubeSearchResult]:
        """YouTube 검색. 실패 시 YouTubeExtractError/DependencyError."""
        YoutubeDL, DownloadError = _import_ytdlp()
        collector = _YtdlpLogger(verbose=self.verbose)
        options: dict[str, Any] = {
            "skip_download": True,
            "quiet": not self.verbose,
            "no_warnings": False,
            "logger": collector,
            "noplaylist": True,
            "extract_flat": False,
            "socket_timeout": self.timeout,
            "retries": self.retries,
            "extractor_retries": self.retries,
            "http_headers": {"User-Agent": DEFAULT_USER_AGENT},
        }

        self._gateway.wait(self.API_NAME)
        with self._audited("search", request_summary=query):
            try:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(f"ytsearch{int(max_results)}:{query}", download=False)
            except DownloadError as exc:
                raise YouTubeExtractError(f"YouTube 검색 실패: {exc}", cause=exc) from exc
            except Exception as exc:
                raise YouTubeExtractError(f"YouTube 추출 오류: {exc}", cause=exc) from exc

            entries = (info or {}).get("entries") or []
            out = [_to_result(e, collector.warnings) for e in entries if e]
            logger.info("youtube '%s' → %d results", query[:40], len(out))
            return out


def _import_ytdlp():
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ModuleNotFoundError as exc:
        raise YouTubeDependencyError(
            "yt-dlp가 설치되어 있지 않습니다. downloads/youtube_crawler/requirements.txt의 "
            "yt-dlp>=2025.05.22 설치가 필요합니다.",
            cause=exc,
        ) from exc
    return YoutubeDL, DownloadError


def _to_result(info: dict[str, Any], warnings: list[str]) -> YouTubeSearchResult:
    video_id = info.get("id") or info.get("display_id")
    url = (info.get("webpage_url") or info.get("url") or
           (f"https://www.youtube.com/watch?v={video_id}" if video_id else ""))
    return YouTubeSearchResult(
        title=info.get("title") or "",
        url=url,
        description=info.get("description") or "",
        published_at=_iso_date_from_yyyymmdd(info.get("upload_date")),
        channel=info.get("channel") or info.get("uploader"),
        video_id=video_id,
        duration=info.get("duration"),
        view_count=info.get("view_count"),
        like_count=info.get("like_count"),
        warnings=list(warnings),
    )


def _iso_date_from_yyyymmdd(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) == 8 and value.isdigit():
        try:
            return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()
        except ValueError:
            return value
    return value
