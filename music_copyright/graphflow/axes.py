"""평가 축 (음악저작권) — 고정 3축 척추 + 동적 축(사람 지정 / 에이전트 발견).

real_estate와 music 모두 보고서 축이 하드코딩 3개였다. SemanticPolicy 도입으로 새 축은
*의도 문장* 하나면 충분(수동 가중치 표 불필요 — 의미유사도가 관련 노드를 부상)하므로,
축을 데이터(Axis)로 들어 올려 런타임에 확장한다:

    - 사람: 실행 시 --axis "라벨::의도" 로 추가 (end-to-end 개입).
    - 에이전트: AxisDiscoverer가 work_card를 보고 3축 외 독립 평가축을 자동 제안.

축 1개가 흐르는 곳: compose(의도→semantic+Fact검색) · writer(라벨→제목, 의도→focus) ·
synthesizer(섹션별 등급, 축-불가지론) · workflow(축별 write 노드). 동적 축은 discourse
관점발견엔 참여하지 않고(기본 3축 척추 유지), 자체 semantic 검색+집필+등급만 받는다.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from music_copyright.graphflow.agents.base import BaseAgent, music_system_prompt
from music_copyright.graphflow.prompts import get_prompt

logger = logging.getLogger("music_copyright.axes")


@dataclass(frozen=True)
class Axis:
    """평가 축 1개. key=식별자(섹션/클레임 키), label=표시명, intent=semantic 검색 의도,
    news_intent=웹 Fact 타겟(없으면 intent), focus=writer 초점(없으면 intent),
    profile=StaticPolicy 프로파일 키(없으면 default 폴백)."""
    key: str
    label: str
    intent: str
    news_intent: str = ""
    focus: str = ""
    profile: str = ""

    @property
    def title(self) -> str:
        return f"{self.label} 분석"

    @property
    def news(self) -> str:
        return self.news_intent or self.intent

    @property
    def writer_focus(self) -> str:
        return self.focus or self.intent

    @property
    def prof(self) -> str:
        return self.profile or self.key


# ── 고정 3축 척추 (intent = composer/writer가 쓰던 의도·focus를 단일 출처로 통합) ──
BASE_AXES: list[Axis] = [
    Axis(
        key="influence", label="영향력", profile="influence",
        intent="비평적 평가와 수상·노미네이트, 미디어·평론 언급, 다른 아티스트의 커버·"
               "샘플·인용, 작곡·작사가의 위상과 문화적 파급",
        news_intent="수상·시상식 결과, 평단 리뷰와 비평, 표절·저작권 분쟁, 커버·리메이크·"
                    "샘플링 사례, 문화적 반향",
        focus="비평·문화적 영향, 수상·미디어 언급, 창작자(작곡·작사) 위상, 샘플·커버·인용"),
    Axis(
        key="popularity", label="대중성·팬덤", profile="popularity",
        intent="차트 성적과 스트리밍·플랫폼 도달, 누적 재생·청취자 규모, 소셜·팬덤 활동과 "
               "바이럴, 대중적 인기 지표",
        news_intent="차트 진입과 순위 기록, 스트리밍·조회수 마일스톤, 팬덤 캠페인·소셜 "
                    "트렌드, 음원 성적 뉴스",
        focus="차트 성적, 스트리밍·플랫폼 도달, 청취자 규모, 팬덤 활동"),
    Axis(
        key="sustainability", label="지속성", profile="sustainability",
        intent="롱테일 스트리밍 지속과 재생 추이, 권리 카탈로그 안정성과 퍼블리셔·레이블, "
               "라이선스 다양성, 음반 인증과 세대 전이",
        news_intent="롱런·역주행, 재발매·리이슈, 싱크 라이선싱(광고·영화·드라마), "
                    "세대별 재유행과 카탈로그 가치",
        focus="롱테일 스트리밍·재생 지속, 권리 카탈로그 안정성, 라이선스 다양성, 세대 전이"),
    Axis(
        key="social_value", label="사회적 가치", profile="social_value",
        intent="가사·메시지가 다루는 사회적 의제(정의·인권·정신건강·환경·정체성·위로), "
               "사회운동·시위 앤섬·공익/자선 캠페인과의 연대, 아티스트/가수의 공적 행위와 "
               "사회적 신뢰, 팬덤의 집단 행동, 소수자·다양성·세대·국가를 잇는 대표성과 "
               "공동체적 결속, 문화외교·소프트파워, 공적·교육·기념 활용",
        news_intent="사회운동·시위에서의 사용과 앤섬화, 기부·자선·공익 캠페인 연계, 공공·교육·"
                    "기념식 활용, 아티스트/가수의 사회공헌·공적 발언·문화외교, 팬덤의 기부·"
                    "캠페인, 대표성·포용·정체성 담론, 문화외교·국가브랜드 기여, 사회적 메시지를 "
                    "둘러싼 논쟁",
        focus="작품이 다루는 사회적 의제와 메시지, 사회운동·공익 연계, 대표성·다양성, "
              "아티스트/가수와 팬덤이 만든 공익·문화외교 맥락, 공동체·세대·국가를 잇는 "
              "사회적 파급과 공적 활용. 단, 작품 자체 근거와 아티스트/팬덤 맥락은 구분"),
]

BASE_KEYS = {a.key for a in BASE_AXES}
BASE_BY_KEY = {a.key: a for a in BASE_AXES}


def axis_by_key(key: str) -> Axis | None:
    return BASE_BY_KEY.get(key)


def _slug(label: str) -> str:
    """동적 축 key — 라벨 해시 기반 ASCII id(x_xxxxxxxx). LangGraph 노드명·섹션 키가
    유니코드(한글 라벨)로 깨지지 않게 ASCII로 고정. 표시는 label이 따로 보존."""
    h = hashlib.sha256((label or "axis").strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"x_{h}"


def parse_human_axis(spec: str) -> Axis | None:
    """'라벨::의도' 또는 '라벨::의도::웹의도' → Axis. 형식 미준수면 None.

    key는 라벨 슬러그(x_ 접두 — 기본 3축과 충돌 방지). 사람이 end-to-end 실행 시
    --axis 로 넘긴다(반복 가능).
    """
    if not spec or "::" not in spec:
        return None
    parts = [p.strip() for p in spec.split("::")]
    label, intent = parts[0], parts[1] if len(parts) > 1 else ""
    if not (label and intent):
        return None
    news = parts[2].strip() if len(parts) > 2 and parts[2].strip() else ""
    return Axis(key=_slug(label), label=label, intent=intent, news_intent=news)


def resolve_axes(*, human: list[Axis] | None = None,
                 discovered: list[Axis] | None = None) -> list[Axis]:
    """기본 3축 + 사람 추가 + 에이전트 발견 → dedup(key) 축 리스트."""
    out: list[Axis] = list(BASE_AXES)
    seen = set(BASE_KEYS)
    for a in (human or []) + (discovered or []):
        if a and a.key not in seen:
            out.append(a)
            seen.add(a.key)
    if len(out) > len(BASE_AXES):
        logger.info("resolve_axes: 기본 3축 + 추가 %d개 → %s",
                    len(out) - len(BASE_AXES), [a.label for a in out[len(BASE_AXES):]])
    return out


# ───────────────────────────────────────────────────────────
# AxisDiscoverer — 에이전트 자동 축 발견 (3축 외 독립 평가축 제안)
# ───────────────────────────────────────────────────────────
class _AxisProposal(BaseModel):
    label: str = Field(description="새 평가축 이름 (예: '샘플링 생태계', '글로벌 vs 로컬 도달')")
    intent: str = Field(description="이 축이 평가할 관심사 1~2문장 (semantic 검색 의도가 됨)")


class _AxesOut(BaseModel):
    axes: list[_AxisProposal] = Field(default_factory=list)


class AxisDiscoverer(BaseAgent):
    """work_card를 보고 기본 3축(영향력·대중성·지속성)으로 안 잡히는 *독립* 평가축을 제안."""

    name = "music_axis_discoverer"

    def discover(self, work_card: str, *, max_axes: int = 2) -> list[Axis]:
        user = (
            f"[평가 대상]\n{work_card}\n\n"
            "이 음악저작물의 가치를 평가할 때, 기본 4축(영향력·대중성·지속성·사회적 가치)으로는 "
            "충분히 안 잡히는 *독립적인* 평가 축이 있으면 최대 "
            f"{max_axes}개 제안하세요. 각 축은 label과 평가 의도(intent) 1~2문장. "
            "기본 4축과 겹치거나 그 하위 관점에 불과하면 제안하지 마세요(없으면 빈 배열).")
        try:
            out: _AxesOut = self.complete(
                system=music_system_prompt(get_prompt("base.frame",
                    role="음악 가치평가 축 설계자 — 기본 4축으로 안 잡히는 독립 평가 차원을 발굴")),
                user=user, schema=_AxesOut)
        except Exception as exc:  # noqa: BLE001
            logger.warning("axis_discoverer 실패: %s", exc)
            return []
        res: list[Axis] = []
        for p in out.axes[:max_axes]:
            if p.label and p.intent:
                res.append(Axis(key=_slug(p.label), label=p.label.strip(),
                                intent=p.intent.strip()))
        logger.info("axis_discoverer: %d 축 발견 → %s", len(res), [a.label for a in res])
        return res
