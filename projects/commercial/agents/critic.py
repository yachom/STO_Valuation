"""Commercial Critic — decides when collected evidence is enough."""

from __future__ import annotations

from typing import Any

from common.agents.critic_base import CriticBase, Verdict


class CommercialCritic(CriticBase):
    name = "commercial_critic"
    system_prompt = (
        "You judge whether collected evidence is sufficient to write a "
        "commercial real-estate sentiment valuation report. Be strict about "
        "presence of: address resolution, PNU, building ledger, ≥1 recent "
        "market signal."
    )

    def run(self, state: Any) -> Verdict:
        raise NotImplementedError
