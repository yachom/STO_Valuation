"""agentic_graph validator — LLM이 제안한 (엔티티→관계→노드)의 스키마·provenance·anchor
가드 단위 테스트. LLM 호출 없이 순수 함수만 검증.

P2(2026-06-29): 웹 Chart/Award를 LLM이 관계와 함께 승격하되, 코드는 의미 판단이 아니라
검증 가능한 불변식(스키마 합법·source_excerpt·work_anchored·허용 엣지)만 본다.
"""
from __future__ import annotations

import unittest

from music_copyright.graphflow import agentic_graph as ag
from music_copyright.graphflow.agentic_graph import (
    EntityCtx,
    FactCtx,
    GraphProposal,
    GraphState,
    WorkCtx,
    _resolve_anchor,
    _resolve_relationship,
    _validate_promotion,
)
from music_copyright.graphflow.graph.schema import EDGE_TYPES as E
from music_copyright.graphflow.graph.schema import NODE_LABELS as L

_AUTO = ag._AUTO_COMMIT_EDGES


def _state(artist_key: str | None = "a1") -> GraphState:
    return GraphState(
        work=WorkCtx(work_id="w1", title="Dynamite", artist_key=artist_key, artist_name="BTS"),
        work_card="", coverage_text="", entities=[], scope_refs={}, scope_name_index={})


def _entity(*, work_anchored=True, collection_anchored=True, excerpt="빌보드 핫 100 1위로 데뷔했다") -> EntityCtx:
    fact = FactCtx(fact_id="f1", text="다이너마이트는 빌보드 핫 100 1위로 데뷔했다", source_excerpt=excerpt)
    return EntityCtx(entity_id="e1", name="Billboard Hot 100", facts=(fact,),
                     work_anchored=work_anchored, collection_anchored=collection_anchored)


def _prop(**kw) -> GraphProposal:
    base = dict(action="promote_entity", entity_id="e1", target_label="Chart",
                target_name="Billboard Hot 100", relationship="CHARTED_ON",
                evidence_fact_ids=["f1"], confidence=0.8, rationale="핫100 1위")
    base.update(kw)
    return GraphProposal(**base)


class ResolveTest(unittest.TestCase):
    def test_relationship_explicit_wins(self):
        self.assertEqual(_resolve_relationship(_prop(relationship="WON"), "Award"), "WON")

    def test_relationship_default_from_label(self):
        self.assertEqual(_resolve_relationship(_prop(relationship=None), "Chart"), E.CHARTED_ON)
        self.assertEqual(_resolve_relationship(_prop(relationship=None), "SocialTheme"), E.ADDRESSES)

    def test_anchor_default_work_for_charted_on(self):
        self.assertEqual(_resolve_anchor(_state(), _prop(), E.CHARTED_ON), (L.WORK, "w1"))

    def test_anchor_default_artist_for_won_career(self):
        # WON src={Work,Artist}; anchor_ref가 Artist면 그대로 사용
        got = _resolve_anchor(_state(), _prop(anchor_ref="Artist:a1"), E.WON)
        self.assertEqual(got, (L.ARTIST, "a1"))

    def test_anchor_illegal_ref_falls_back_to_work(self):
        # CHARTED_ON src={Work}; Artist anchor_ref는 불법 → 기본 Work로 폴백
        self.assertEqual(_resolve_anchor(_state(), _prop(anchor_ref="Artist:a1"), E.CHARTED_ON),
                         (L.WORK, "w1"))

    def test_anchor_none_when_no_artist(self):
        self.assertEqual(_resolve_anchor(_state(artist_key=None), _prop(), E.HAS_FANDOM),
                         (None, None))


class ValidatePromotionTest(unittest.TestCase):
    def _ok(self, **kw):
        prop = kw.pop("prop", _prop())
        ent = kw.pop("entity", _entity())
        label = kw.pop("label", "Chart")
        rel = kw.pop("rel", E.CHARTED_ON)
        src = kw.pop("src_label", L.WORK)
        return _validate_promotion(_state(), ent, prop, label, rel, src, _AUTO)

    def test_legal_charted_on_passes(self):
        ok, reason = self._ok()
        self.assertTrue(ok, reason)

    def test_illegal_dst_label_rejected(self):
        # CHARTED_ON dst는 Chart만 — Work를 dst로 주면 거부
        ok, reason = self._ok(label="Work")
        self.assertFalse(ok)
        self.assertEqual(reason, "illegal_target_label")

    def test_illegal_src_rejected(self):
        # CHARTED_ON src는 Work만 — Artist를 src로 주면 거부
        ok, reason = self._ok(src_label=L.ARTIST)
        self.assertFalse(ok)
        self.assertEqual(reason, "no_valid_anchor")

    def test_edge_not_in_allowlist_rejected(self):
        ok, reason = self._ok(rel=E.BY_ARTIST, label="Artist", src_label=L.WORK)
        self.assertFalse(ok)
        self.assertEqual(reason, "edge_not_auto")

    def test_missing_source_excerpt_rejected(self):
        ok, reason = self._ok(entity=_entity(excerpt=""))
        self.assertFalse(ok)
        self.assertEqual(reason, "no_source_excerpt")

    def test_work_edge_requires_work_anchored(self):
        ok, reason = self._ok(entity=_entity(work_anchored=False))
        self.assertFalse(ok)
        self.assertEqual(reason, "not_work_anchored")

    def test_artist_edge_accepts_collection_anchored(self):
        # WON Artist→Award: work_anchored 아니어도 collection_anchored면 통과(통산 수상)
        ent = _entity(work_anchored=False, collection_anchored=True)
        ent = EntityCtx(entity_id="e1", name="Grammy Awards", facts=ent.facts,
                        work_anchored=False, collection_anchored=True)
        prop = _prop(target_label="Award", target_name="Grammy Awards", relationship="WON",
                     anchor_ref="Artist:a1")
        ok, reason = _validate_promotion(_state(), ent, prop, "Award", E.WON, L.ARTIST, _AUTO)
        self.assertTrue(ok, reason)

    def test_certified_auto_commits(self):
        # 객관적 성취(CERTIFIED, Work→Certification)는 자동커밋
        fact = FactCtx(fact_id="f1", text="RIAA 플래티넘 인증을 받았다",
                       source_excerpt="certified platinum by the RIAA")
        ent = EntityCtx(entity_id="e1", name="RIAA Platinum", facts=(fact,), work_anchored=True)
        prop = _prop(target_label="Certification", target_name="RIAA Platinum",
                     relationship="CERTIFIED")
        ok, reason = _validate_promotion(_state(), ent, prop, "Certification", E.CERTIFIED, L.WORK, _AUTO)
        self.assertTrue(ok, reason)

    def test_has_fandom_auto_commits(self):
        # 검색-인식 팬덤(guess_label=Fandom)은 HAS_FANDOM으로 자동 승격 — consolidate의 하드코딩
        # 팬덤 사전(ARMY/BLINK/…)을 대체한다. performer Artist에 grounding과 함께 앵커.
        fact = FactCtx(fact_id="f1", text="ARMY는 BTS의 공식 팬덤이다",
                       source_excerpt="ARMY는 BTS의 공식 팬덤이다")
        ent = EntityCtx(entity_id="e1", name="ARMY", facts=(fact,),
                        work_anchored=True, collection_anchored=True)
        prop = _prop(target_label="Fandom", target_name="ARMY", relationship="HAS_FANDOM",
                     anchor_ref="Artist:a1")
        ok, reason = _validate_promotion(_state(), ent, prop, "Fandom", E.HAS_FANDOM, L.ARTIST, _AUTO)
        self.assertTrue(ok, reason)

    def test_interpretive_addresses_routed_to_review(self):
        # 정책(2026-06-29): 해석성 엣지(ADDRESSES 등)는 자동커밋 안 함 → review(과대귀속 방지).
        fact = FactCtx(fact_id="f1", text="희망의 메시지", source_excerpt="celebrate the thrill of success")
        ent = EntityCtx(entity_id="e1", name="자신감", facts=(fact,), work_anchored=True)
        prop = _prop(target_label="SocialTheme", target_name="자신감", relationship="ADDRESSES")
        ok, reason = _validate_promotion(_state(), ent, prop, "SocialTheme", E.ADDRESSES, L.WORK, _AUTO)
        self.assertFalse(ok)
        self.assertEqual(reason, "edge_not_auto")

    def test_managed_by_routed_to_review(self):
        # WHO를 소속사로 오인하는 과대귀속 방지 — MANAGED_BY는 자동커밋 안 함.
        fact = FactCtx(fact_id="f1", text="WHO가 극찬했다", source_excerpt="praised by the WHO")
        ent = EntityCtx(entity_id="e1", name="WHO", facts=(fact,),
                        work_anchored=True, collection_anchored=True)
        prop = _prop(target_label="Agency", target_name="WHO", relationship="MANAGED_BY",
                     anchor_ref="Artist:a1")
        ok, reason = _validate_promotion(_state(), ent, prop, "Agency", E.MANAGED_BY, L.ARTIST, _AUTO)
        self.assertFalse(ok)
        self.assertEqual(reason, "edge_not_auto")


if __name__ == "__main__":
    unittest.main()
