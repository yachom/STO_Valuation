from __future__ import annotations

import unittest

from music_copyright.graphflow.harvester import (
    FactDraft, _fact_grounded_in_doc, _match_scope_name,
    _missing_required_artist_context, _rank_docs)
from music_copyright.graphflow.navigator import FrontierCandidate, Navigator
from music_copyright.graphflow.report import _claim_id, _section_id
from music_copyright.graphflow.research import QueryOutcome, QueryVariant, ResearchTask


class ResearchPlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = FrontierCandidate(
            ref="Work:isrc:KRE702300105",
            label="Work",
            key="isrc:KRE702300105",
            card="토끼 — 우예린는 2023 발매된 평가 대상 음악저작물이다.",
            proper_nouns=["토끼", "우예린"],
            templates=["토끼 우예린 차트"],
        )

    def test_validation_repairs_anchor_without_replacing_intent(self) -> None:
        task = ResearchTask(
            target=self.work.ref,
            axis="social_value",
            question="작품을 위로의 노래로 해석한 원문 비평이 있는가?",
            evidence_goal="비평 원문 또는 아티스트 인터뷰",
            evidence_type="direct_work",
            variants=[QueryVariant(
                text="위로 해석 원문 인터뷰",
                channels=["web"],
                strategy="criticism",
            )],
        )
        planned = Navigator()._validate(
            [task], [self.work], [], max_targets=2, queries_per_node=2)

        self.assertEqual(len(planned), 1)
        query = planned[0].variants[0].text
        self.assertIn("토끼", query)
        self.assertIn("우예린", query)
        self.assertIn("위로 해석 원문 인터뷰", query)
        self.assertEqual(planned[0].variants[0].channels, ["web"])
        self.assertEqual(planned[0].variants[0].strategy, "criticism")

    def test_query_outcome_exposes_adaptive_feedback(self) -> None:
        outcome = QueryOutcome(
            task_id="task", target=self.work.ref, axis="influence",
            query="토끼 우예린 비평", strategy="criticism", channels=["web"],
            docs_selected=8, docs_ingested=2, grounded_facts=3,
            skip_reasons={"missing_artist_context": 6}, sources={"source-a", "source-b"},
        )
        data = outcome.as_dict()
        self.assertGreater(data["utility"], 0)
        self.assertLess(data["utility"], 0.5)
        self.assertEqual(data["skip_reasons"]["missing_artist_context"], 6)

    def test_direct_work_task_is_rebound_from_artist_to_work(self) -> None:
        artist = FrontierCandidate(
            ref="Artist:uye-rin", label="Artist", key="uye-rin",
            card="우예린 — performer 아티스트", proper_nouns=["우예린"],
            templates=["우예린 인터뷰"],
        )
        task = ResearchTask(
            target=artist.ref, axis="social_value", question="곡의 메시지는 무엇인가?",
            evidence_goal="가사 원문", evidence_type="direct_work",
            variants=[QueryVariant(text="우예린 토끼 가사", channels=["web"])],
        )
        planned = Navigator()._validate(
            [task], [self.work, artist], [], max_targets=2, queries_per_node=2)
        self.assertEqual(planned[0].target, self.work.ref)

    def test_document_ranking_prefers_target_anchored_sources(self) -> None:
        docs = [
            {"title": "토끼 사육", "content": "동물 이야기", "source": "news-a",
             "url": "https://noise.example/a", "channel": "news"},
            {"title": "우예린 토끼 인터뷰", "content": "우예린이 토끼를 설명했다",
             "source": "news-b", "url": "https://music.example/a", "channel": "news"},
        ]
        ranked = _rank_docs(
            docs, "토끼 — 우예린는 2023 발매된 평가 대상 음악저작물이다. 작품식별자 x.")
        self.assertEqual(ranked[0]["source"], "news-b")

    def test_fact_requires_an_actual_supporting_excerpt(self) -> None:
        doc = {
            "title": "우예린 토끼 가사", "source": "example", "url": "https://example.test",
            "youtube_channel": "", "content": (
                "우예린의 토끼 가사에는 사람과 토끼가 함께 사는 세상이 등장한다."),
        }
        card = "토끼 — 우예린는 2023 발매된 평가 대상 음악저작물이다. 작품식별자 x."
        valid = FactDraft(
            text="우예린의 '토끼' 가사에는 사람과 토끼가 함께 사는 세상이 등장한다.",
            source_excerpt="우예린의 토끼 가사에는 사람과 토끼가 함께 사는 세상이 등장한다.",
        )
        inferred = FactDraft(
            text="'토끼'의 가사는 청소년 정체성과 사회적 연대를 다룬다.",
            source_excerpt="우예린의 토끼 가사에는 사람과 토끼가 함께 사는 세상이 등장한다.",
        )
        self.assertTrue(_fact_grounded_in_doc(valid, doc, "토끼 우예린 가사", card))
        self.assertFalse(_fact_grounded_in_doc(inferred, doc, "토끼 우예린 가사", card))

    def test_fact_rejects_work_subject_over_attribution(self) -> None:
        # 멤버(RM)의 활동을 곡(Dynamite)으로 둔갑시킨 과대귀속 Fact. excerpt는 RM을
        # 주어로 말하고 곡명을 일절 지칭하지 않으므로 grounding을 통과시키면 안 된다.
        # (카드 제목은 영문 'Dynamite'인데 Fact는 음차 '다이너마이트'를 쓴다 — 카드
        # 제목 매칭만으로는 못 잡고, Fact가 인용한 곡명을 excerpt에서 확인해야 한다.)
        card = ("Dynamite — BTS는 2020 발매된 평가 대상 음악저작물이다. "
                "작품식별자 T-300.516.442-7.")
        excerpt = ("랩몬스터(RM)는 바이든 대통령이 최근 서명 및 발효한 "
                   "'아시안 증오범죄 방지 법안(COVID-19 Hate Crimes Act)'를 지지했다.")
        doc = {
            "title": "방탄소년단 RM, 아시안 증오범죄 방지 법안 지지", "source": "naver",
            "url": "https://news.example/rm", "youtube_channel": "",
            "content": "방탄소년단(BTS)의 'Dynamite'가 빌보드 정상에 오른 가운데, " + excerpt,
        }
        over_attributed = FactDraft(
            text="BTS의 '다이너마이트'는 아시안 증오범죄 방지 법안과 관련이 있다.",
            source_excerpt=excerpt,
        )
        self.assertFalse(
            _fact_grounded_in_doc(over_attributed, doc, "BTS 다이너마이트 사회", card))

        # 과차단 금지 1: excerpt가 곡명을 직접 표기하면 정상 통과(실제 grounded 사실류).
        named_excerpt = ("‘다이너마이트(Dynamite)’로 2020년 빌보드 핫100 차트 1위를 "
                         "차지하며 방탄소년단(BTS)은 세계적 팝 아이콘이 되었다.")
        named = FactDraft(
            text="BTS의 'Dynamite'는 2020년 빌보드 핫100 차트 1위를 차지했다.",
            source_excerpt=named_excerpt,
        )
        named_doc = {"title": "BTS 다이너마이트 빌보드 1위", "source": "kci",
                     "url": "https://kci.example", "youtube_channel": "",
                     "content": named_excerpt}
        self.assertTrue(
            _fact_grounded_in_doc(named, named_doc, "BTS 다이너마이트 차트", card))

        # 과차단 금지 2: excerpt가 대용 표현('이 곡')으로 같은 곡을 지칭하면 정상 통과.
        anaphora = FactDraft(
            text="'Dynamite'는 빌보드 핫100 차트 정상에 올랐다.",
            source_excerpt="이 곡은 빌보드 핫100 차트 정상에 올랐다.",
        )
        anaphora_doc = {"title": "BTS Dynamite", "source": "x",
                        "url": "https://y.test", "youtube_channel": "",
                        "content": "BTS의 Dynamite. 이 곡은 빌보드 핫100 차트 정상에 올랐다."}
        self.assertTrue(
            _fact_grounded_in_doc(anaphora, anaphora_doc, "BTS Dynamite 차트", card))

    def test_alias_aware_grounding_accepts_native_script(self) -> None:
        # 영문으로 resolve된 작품(LOVE ATTACK/RESCENE)이라도, 원어 표기(러브어택/리센느)를
        # 쓰는 한국어 보도가 별칭 덕에 수집·grounding된다(음차 불일치 해소). 카드 [별칭] 라인.
        card = ("LOVE ATTACK — RESCENE는 2024 발매된 평가 대상 음악저작물이다. "
                "작품식별자 isrc:KRA382405206. [별칭] 제목: LOVE ATTACK · 아티스트: RESCENE|리센느")
        doc = {"title": "리센느 러브어택 역주행 신드롬", "source": "MSN",
               "url": "https://x.test", "youtube_channel": "",
               "content": "리센느의 러브어택이 SNS 밈 열풍과 유튜브 화제로 음원 차트를 역주행했다."}
        # 원어(리센느) 표기 doc은 영문 카드여도 수집된다(이전엔 missing_artist_context로 기각)
        self.assertFalse(
            _missing_required_artist_context(doc, "리센느 러브어택 역주행", card))
        # 아티스트-주어 원어 fact가 grounded(역주행 신호가 그래프에 들어옴)
        native = FactDraft(
            text="리센느는 SNS 밈 열풍과 유튜브 화제로 역주행했다.",
            source_excerpt="리센느의 러브어택이 SNS 밈 열풍과 유튜브 화제로 음원 차트를 역주행했다")
        self.assertTrue(
            _fact_grounded_in_doc(native, doc, "리센느 러브어택 역주행", card))

    def test_mention_matching_blocks_over_eager_substring(self) -> None:
        scope = {
            "방탄소년단": ("Artist", "a1"),
            "위로": ("SocialTheme", "t1"),
            "bts": ("Artist", "a1"),
        }
        # 정확일치
        self.assertEqual(_match_scope_name("방탄소년단", scope), ("Artist", "a1"))
        # 정당한 부분일치: mention이 스코프명을 포함하고 길이비율 충족
        self.assertEqual(_match_scope_name("방탄소년단bts", scope), ("Artist", "a1"))
        # 과다매칭 차단: 짧은 스코프 '위로'(2)는 '위로의노래'에 잘못 안 걸림
        self.assertIsNone(_match_scope_name("위로의노래", scope))
        # 과다매칭 차단: 'bts'(3)가 substring이지만 비율<0.6인 'btsarmy'에 안 걸림
        self.assertIsNone(_match_scope_name("btsarmy", scope))

    def test_report_graph_ids_are_namespaced_per_report(self) -> None:
        self.assertNotEqual(
            _claim_id("rpt_a", "influence", "같은 주장"),
            _claim_id("rpt_b", "influence", "같은 주장"),
        )
        self.assertNotEqual(
            _section_id("rpt_a", "influence"),
            _section_id("rpt_b", "influence"),
        )


if __name__ == "__main__":
    unittest.main()
