import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from src.has_pokedex import HASPokedex, HASSettings, PokedexError, activity_text


class HASPokedexTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_vault_legacy_name_fallback(self):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            self.assertEqual(request.headers["Authorization"], "Bearer vault-token")
            if "webchat-secret" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, json={"value": "secret"})

        settings = HASSettings("example", "clinical", "https://example.vault.azure.net/", "bot-example-webchat-secret")
        credential = Mock(get_token=AsyncMock(return_value=Mock(token="vault-token")), close=AsyncMock())
        helper = HASPokedex(settings)
        with patch.dict("os.environ", {}, clear=True), patch("src.has_pokedex.GameCredential", return_value=credential):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                self.assertEqual(await helper._secret(client, settings), "secret")
        self.assertEqual(paths, ["/secrets/bot-example-webchat-secret", "/secrets/bot-example-web-chat-secret"])
        credential.get_token.assert_awaited_once_with("https://vault.azure.net/.default")
        credential.close.assert_awaited_once()

    async def test_explicit_secret_failure_closes_credential_without_fallback(self):
        credential = Mock(get_token=AsyncMock(return_value=Mock(token="vault-token")), close=AsyncMock())
        settings = HASSettings("example", "clinical", "https://example.vault.azure.net/", "custom-secret")
        paths = []

        def respond(request):
            paths.append(request.url.path)
            return httpx.Response(403)

        helper = HASPokedex(settings, transport=httpx.MockTransport(respond))
        with patch.dict("os.environ", {}, clear=True), patch("src.has_pokedex.GameCredential", return_value=credential):
            with self.assertRaisesRegex(PokedexError, "HTTP 403"):
                await helper.ask("Help", {})
        self.assertEqual(paths, ["/secrets/custom-secret"])
        credential.close.assert_awaited_once()

    async def test_filters_echo_typing_debug_then_reads_structured_answer(self):
        polls = 0

        def respond(request):
            nonlocal polls
            if request.url.path.endswith("/conversations"):
                return httpx.Response(201, json={"conversationId": "conversation", "token": "session-token"})
            if request.method == "POST":
                return httpx.Response(200, json={"id": "sent"})
            polls += 1
            if polls == 1:
                activities = [{"type": "message", "text": "session_start"}]
            elif polls == 2:
                activities = [{"type": "typing"}, {"type": "event", "name": "debug_capture", "text": "private"}, {"type": "message", "text": "Debug_data:<pre>private</pre>"}]
            else:
                activities = [{"type": "event", "value": {"response": {"answer": "Ask about duration."}}}]
            for activity in activities:
                activity["from"] = {"id": "bot-test"}
            activities.append({"from": {"id": "another-user"}, "type": "message", "text": "echo"})
            return httpx.Response(200, json={"watermark": str(polls), "activities": activities})

        helper = HASPokedex(HASSettings("bot-test", "clinical"), transport=httpx.MockTransport(respond))
        helper.poll_interval = 0
        with patch.dict("os.environ", {"HAS_DIRECT_LINE_SECRET": "test-secret"}):
            self.assertEqual(await helper.ask("Help", {}), "Ask about duration.")
        self.assertEqual(polls, 3)

    async def test_malformed_response_and_backend_failure_are_safe(self):
        for response, message in ((httpx.Response(200, json=[]), "unexpected response"), (httpx.Response(502, text="private"), "HTTP 502")):
            helper = HASPokedex(HASSettings("bot-test", "clinical"), transport=httpx.MockTransport(lambda request: response))
            with patch.dict("os.environ", {"HAS_DIRECT_LINE_SECRET": "test-secret"}):
                with self.assertRaisesRegex(PokedexError, message):
                    await helper.ask("Help", {})
            self.assertEqual(helper.history, [])

    async def test_direct_line_scenario_history_and_reply(self):
        payloads = []
        polls = 0

        def respond(request):
            nonlocal polls
            if request.url.path.endswith("/conversations"):
                self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
                return httpx.Response(201, json={"conversationId": "conversation", "token": "session-token"})
            self.assertEqual(request.headers["Authorization"], "Bearer session-token")
            if request.method == "POST":
                payloads.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "sent"})
            polls += 1
            text = "session_start" if polls % 2 else "Ask about duration."
            return httpx.Response(200, json={"watermark": str(polls), "activities": [{"from": {"id": "bot-test"}, "type": "message", "text": text}]})

        helper = HASPokedex(HASSettings("bot-test", "clinical"), transport=httpx.MockTransport(respond))
        with patch.dict("os.environ", {"HAS_DIRECT_LINE_SECRET": "test-secret"}):
            self.assertEqual(await helper.ask("What next?", {"tests": []}), "Ask about duration.")
            await helper.ask("Anything else?", {})
        self.assertEqual(payloads[0]["value"]["triggeredScenario"], {"trigger": "clinical", "args": {}})
        history = next(payload for payload in payloads if payload.get("name") == "OverrideChatHistory")
        self.assertEqual(len(history["value"]), 2)
        self.assertEqual(len(helper.history), 4)

    async def test_http_error_does_not_expose_secret_or_add_history(self):
        helper = HASPokedex(HASSettings("bot-test", "clinical"), transport=httpx.MockTransport(lambda request: httpx.Response(403, text="private details")))
        with patch.dict("os.environ", {"HAS_DIRECT_LINE_SECRET": "test-secret"}):
            with self.assertRaisesRegex(PokedexError, "HTTP 403") as raised:
                await helper.ask("Help", {})
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertNotIn("private details", str(raised.exception))
        self.assertEqual(helper.history, [])

    async def test_timeout_and_cancellation(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def respond(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        helper = HASPokedex(HASSettings("bot-test", "clinical"), transport=httpx.MockTransport(respond))
        with patch.dict("os.environ", {"HAS_DIRECT_LINE_SECRET": "test-secret"}):
            helper.timeout = 0.01
            with self.assertRaisesRegex(PokedexError, "timed out"):
                await helper.ask("Help", {})
            self.assertTrue(cancelled.is_set())
            helper.timeout = 120
            started.clear()
            task = asyncio.create_task(helper.ask("Help", {}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    def test_configuration_missing_and_europe_region(self):
        with patch.dict("os.environ", {"HAS_BOT_ID": ""}, clear=True), self.assertRaisesRegex(PokedexError, "HAS_BOT_ID"):
            HASSettings.from_environment()
        self.assertIn("europe.directline", HASSettings("bot-europe-test", "clinical").base_url)

    def test_reference_hcp_configuration_and_overrides(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = HASSettings.from_environment()
        self.assertEqual(settings.bot_id, "obs-hcp-1-kz3jzz4")
        self.assertEqual(settings.scenario, "dsb_debug_scenario")
        self.assertEqual(settings.secret_name, "bot-obs-hcp-1-kz3jzz4-webchat-secret")
        with patch.dict("os.environ", {"HAS_BOT_ID": "bot-other", "HAS_SCENARIO": "clinical"}, clear=True):
            self.assertEqual(HASSettings.from_environment().secret_name, "bot-other-webchat-secret")

    def test_structured_answer_and_sources_exclude_debug_trace(self):
        result = activity_text({"text": "Internal display trace", "value": {"response": {"answer": "Check the history.", "grounding_urls": ["https://example.org/reference", "javascript:alert(1)"], "debug": "Private reasoning"}}})
        self.assertIn("Check the history.", result)
        self.assertIn("https://example.org/reference", result)
        self.assertNotIn("Private reasoning", result)
        self.assertNotIn("Internal display trace", result)
        self.assertNotIn("javascript", result)

    def test_debug_summary_card_is_skipped_entirely(self):
        card = {"contentType": "application/vnd.microsoft.card.adaptive", "content": {"body": [{"type": "TextBlock", "text": "Interaction summary"}, {"type": "TextBlock", "text": "Internal trace"}]}}
        self.assertEqual(activity_text({"attachments": [card]}), "")
        self.assertEqual(activity_text({"name": "debug_capture", "text": "Internal trace"}), "")

    def test_reply_filters_debug_and_keeps_card_sources(self):
        self.assertEqual(activity_text({"text": "Debug_data:<pre>private trace</pre>"}), "")
        result = activity_text({"attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {"body": [{"type": "TextBlock", "text": "Check the history."}], "actions": [{"type": "Action.OpenUrl", "title": "Reference", "url": "https://example.org/reference"}]}}]})
        self.assertIn("Check the history.", result)
        self.assertIn("https://example.org/reference", result)