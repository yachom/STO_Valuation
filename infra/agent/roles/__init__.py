"""infra.agent.roles — 양 자산이 공유하는 '역할베이스' 에이전트.

generic BaseAgent(infra.agent.base) 위에, 자산 무관하게 동일한 호출 골격을 갖는
공통 역할(Writer/Synthesizer/Outliner/Moderator …)의 베이스를 둔다. 각 자산은
이 베이스를 상속해 name·프롬프트 슬롯 키·자산별 출력 스키마·증거 포맷터·프레임
함수만 채운다(graphflow 오케스트레이션은 자산별로 유지).

설계:
- 프롬프트 *문구*와 출력 *스키마*는 자산별로 유지한다 — Pydantic Field description이
  structured-output으로 LLM에 전달되므로, 통합하면 한 자산의 LLM 입력이 바뀐다.
  베이스는 호출 *골격*만 공유하고, 내용은 hook으로 받는다.
- frame-wrap 비대칭: 음악은 모든 역할을 시스템 프레임으로 감싸고, 부동산은 Writer/
  Synthesizer만 감싼다. 따라서 system 조립은 `_frame` hook으로(기본=프레임 없음).
"""

from infra.agent.roles.writer import BaseWriterAgent

__all__ = ["BaseWriterAgent"]
