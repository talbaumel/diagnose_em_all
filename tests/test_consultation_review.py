from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from src.care_plan import Prescription, Referral
from src.consultation_review import SCORING_ENDPOINT_ENV, SCORING_MODEL, SCORE_AXES, ConsultationMetrics, format_duration, parse_scorecard, score_consultation


def scorecard_document():
    return {
        "summary": "Clear questions with room for more acknowledgement.",
        "axes": {key: {"score": 3, "feedback": "Asked about symptom duration."} for key in SCORE_AXES},
    }


class ScorecardParsingTests(unittest.TestCase):
    def test_valid_axes_and_insufficient_evidence(self):
        document = scorecard_document()
        document["axes"]["empathy"]["score"] = None
        result = parse_scorecard(json.dumps(document))
        self.assertEqual(len(result.axes), 8)
        self.assertEqual(result.axes[2].score, 0)
        self.assertIn("Insufficient evidence", result.axes[2].feedback)
        self.assertIn(document["axes"]["empathy"]["feedback"], result.axes[2].feedback)
        self.assertEqual(result.axes[0].score, 3)

    def test_insufficient_evidence_scores_zero_on_every_axis(self):
        document = scorecard_document()
        for axis in document["axes"].values():
            axis["score"] = None
        result = parse_scorecard(json.dumps(document))
        self.assertTrue(all(axis.score == 0 for axis in result.axes))
        self.assertTrue(all("Insufficient evidence" in axis.feedback for axis in result.axes))

    def test_rejects_invalid_scores_and_missing_axes(self):
        for score in (-1, 6, True, 2.5, "5"):
            document = scorecard_document()
            document["axes"]["warmth"]["score"] = score
            with self.subTest(score=score), self.assertRaises(ValueError):
                parse_scorecard(json.dumps(document))
        document = scorecard_document()
        del document["axes"]["warmth"]
        with self.assertRaises(ValueError):
            parse_scorecard(json.dumps(document))


class ScoringClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.endpoint = "https://scoring.example.test/mai/v1/chat/completions"
        self.endpoint_environment = patch.dict(os.environ, {SCORING_ENDPOINT_ENV: self.endpoint})
        self.endpoint_environment.start()
        self.addCleanup(self.endpoint_environment.stop)

    async def test_endpoint_payload_and_response(self):
        transcript = [("You", "When did this begin?"), ("Patient", "Yesterday.")]
        metrics = ConsultationMetrics(started_at=10, finished_at=45)
        metrics.discover_test("Temperature", "38 C")
        metrics.diagnostic_skills = {"score": 2, "formula": "bounded sum", "actions": [
            {"name": "Temperature", "result": "38 C", "points": 2, "rationale": "Fever assessment"},
        ]}
        metrics.skill_requests = [{"status": "cancelled", "tool": "propose_chest_ct"}]
        metrics.care_plan.add(Prescription("Example medication", "Player-entered directions", "Symptom relief"))
        metrics.care_plan.add(Referral("Specialist clinic", "Further assessment", "Urgent"))

        def respond(request):
            self.assertEqual(str(request.url), self.endpoint)
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], SCORING_MODEL)
            self.assertIn("max_completion_tokens", payload)
            self.assertNotIn("max_tokens", payload)
            evidence = json.loads(payload["messages"][1]["content"])
            self.assertEqual(len(evidence["transcript"]), 2)
            self.assertEqual(evidence["elapsed_seconds"], 35)
            self.assertEqual(evidence["care_plan"]["prescriptions"][0]["medication"], "Example medication")
            self.assertEqual(evidence["care_plan"]["referrals"][0]["urgency"], "Urgent")
            self.assertEqual(evidence["discovered_tests"], [{"name": "Temperature", "result": "38 C"}])
            self.assertEqual(evidence["deterministic_skills"], metrics.diagnostic_skills)
            self.assertEqual(evidence["skill_request_log"], metrics.skill_requests)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(scorecard_document())}}]})

        result = await score_consultation(transcript, "common cold", "child", metrics, 2, {"Authorization": "Bearer test-token"}, transport=httpx.MockTransport(respond))
        self.assertEqual(len(result.axes), 8)

    async def test_http_and_malformed_response_fail_without_fabricating_scores(self):
        for response, error in (
            (httpx.Response(429), httpx.HTTPStatusError),
            (httpx.Response(200, json={"choices": []}), ValueError),
            (httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}), ValueError),
            (httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]}), ValueError),
        ):
            with self.subTest(response=response), self.assertRaises(error):
                await score_consultation([], "cold", "child", ConsultationMetrics(), 2, {}, transport=httpx.MockTransport(lambda request: response))

    async def test_request_is_cancellable(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(score_consultation([], "cold", "child", ConsultationMetrics(), 2, {}, transport=httpx.MockTransport(pending)))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())


class ConsultationMetricsTests(unittest.TestCase):
    def test_timer_excludes_connection_and_stops_at_diagnosis(self):
        metrics = ConsultationMetrics()
        self.assertEqual(metrics.elapsed_seconds, 0)
        with patch("src.consultation_review.time.monotonic", return_value=100):
            metrics.start()
        with patch("src.consultation_review.time.monotonic", return_value=145):
            metrics.start()
            self.assertEqual(metrics.elapsed_seconds, 45)
            metrics.finish()
        with patch("src.consultation_review.time.monotonic", return_value=200):
            metrics.finish()
            self.assertEqual(metrics.elapsed_seconds, 45)

    def test_test_discovery_is_unique(self):
        metrics = ConsultationMetrics()
        metrics.discover_test("Temperature", "38 C")
        metrics.discover_test("Temperature", "38 C")
        metrics.discover_test("Throat examination", "Mild redness")
        self.assertEqual(len(metrics.discovered_tests), 2)
        self.assertEqual(metrics.discovered_tests["Temperature"], "38 C")

    def test_duration_formatting(self):
        self.assertEqual(format_duration(0), "00:00")
        self.assertEqual(format_duration(125.9), "02:05")
        self.assertEqual(format_duration(3601), "60:01")


if __name__ == "__main__":
    unittest.main()