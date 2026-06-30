"""infra/external/audit.py — 외부호출 통합 기록·알림 단위 테스트.

모든 응답→로컬 responses.jsonl, 오류(비-2xx/예외)만→Slack 두 채널(api_limit+alerts),
종료 요약→alerts. 비밀 redact + 자산/단계 컨텍스트 부착. 네트워크/LLM 없이 mock으로 검증.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from infra.agent.tracing import RunTracer, set_tracer
from infra.external import audit
from infra.notify.slack import (
    CHANNEL_ALERTS,
    CHANNEL_API_LIMIT,
    SlackNotifier,
    disable_slack_alerting,
    enable_slack_alerting,
    get_notifier,
)


class ExternalAuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tracer = RunTracer("test_run", Path(self._tmp.name) / "store.db")
        set_tracer(self.tracer)
        self.captured: list[tuple[str, dict]] = []
        notifier = SlackNotifier(
            webhooks={CHANNEL_API_LIMIT: "https://hooks/x",
                      CHANNEL_ALERTS: "https://hooks/y"},
            post_fn=lambda ch, payload: self.captured.append((ch, payload)),
            throttle_seconds=0.0)
        enable_slack_alerting(notifier=notifier, attach_root=False, hook_tracing=False)
        audit.clear_external_context()

    def tearDown(self):
        disable_slack_alerting()
        set_tracer(None)
        audit.clear_external_context()
        self._tmp.cleanup()

    def _lines(self) -> list[dict]:
        p = self.tracer.run_dir / "responses.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ── 오류 판정 ──────────────────────────────────────────────
    def test_is_error(self):
        self.assertFalse(audit.is_error(200, None))
        self.assertFalse(audit.is_error(204, None))
        self.assertTrue(audit.is_error(400, None))
        self.assertTrue(audit.is_error(429, None))
        self.assertTrue(audit.is_error(500, None))
        self.assertTrue(audit.is_error(None, None))       # 네트워크 실패
        self.assertTrue(audit.is_error(200, "boom"))      # 예외 동반

    # ── 성공: 로컬만, Slack 없음 ───────────────────────────────
    def test_success_logs_local_only(self):
        audit.record_response(api_name="naver", operation="search", status=200,
                              response_excerpt="{ok}")
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["status"], 200)
        self.assertEqual(lines[0]["api_name"], "naver")
        self.assertEqual(self.captured, [])   # 성공은 Slack 안 감

    # ── 오류: 로컬 + Slack 두 채널 동시 ────────────────────────
    def test_error_notifies_both_channels(self):
        audit.record_response(api_name="tavily", operation="search", status=401,
                              response_excerpt="unauthorized")
        self.assertEqual(len(self._lines()), 1)
        self.assertEqual(sorted(ch for ch, _ in self.captured),
                         [CHANNEL_ALERTS, CHANNEL_API_LIMIT])

    # ── 자산/단계 컨텍스트가 기록·메시지에 박힌다 ──────────────
    def test_context_attached(self):
        audit.set_external_context(asset_type="music_copyright", asset_id="isrc:X", phase="seed")
        audit.record_response(api_name="genius", operation="song", status=500)
        rec = self._lines()[0]
        self.assertEqual(rec["asset_type"], "music_copyright")
        self.assertEqual(rec["asset_id"], "isrc:X")
        self.assertEqual(rec["phase"], "seed")
        self.assertEqual(rec.get("run_id"), "test_run")
        _, payload = self.captured[0]
        self.assertIn("isrc:X", payload["text"])      # 메시지만 보고 자산 식별 가능
        self.assertIn("genius", payload["text"])

    # ── 비밀 redact (로컬·Slack 둘 다) ─────────────────────────
    def test_redaction(self):
        audit.record_response(api_name="x", status=403,
                              endpoint="https://api/x?api_key=SECRET123",
                              response_excerpt='{"token": "TOPSECRET999"}')
        blob = json.dumps(self._lines()[0], ensure_ascii=False)
        self.assertNotIn("SECRET123", blob)
        self.assertNotIn("TOPSECRET999", blob)
        _, payload = self.captured[0]
        self.assertNotIn("TOPSECRET999", payload["text"])

    # ── notify_error(force_channels) 는 지정 채널 모두로 ───────
    def test_force_channels(self):
        sent = get_notifier().notify_error(
            title="t", message="m",
            force_channels=[CHANNEL_API_LIMIT, CHANNEL_ALERTS])
        self.assertTrue(sent)
        self.assertEqual(sorted(ch for ch, _ in self.captured),
                         [CHANNEL_ALERTS, CHANNEL_API_LIMIT])

    # ── 종료 요약은 alerts 1건 ─────────────────────────────────
    def test_flush_summary_alerts_only(self):
        audit.record_response(api_name="a", status=200)
        audit.record_response(api_name="a", status=500)
        self.captured.clear()
        audit.flush_summary()
        self.assertEqual([ch for ch, _ in self.captured], [CHANNEL_ALERTS])
        self.assertIn("총 2건", self.captured[0][1]["text"])
        self.assertIn("오류 1건", self.captured[0][1]["text"])


if __name__ == "__main__":
    unittest.main()
