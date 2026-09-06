from __future__ import annotations

import asyncio
import base64
import json
import unittest
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pygame
from azure.core.credentials import AccessToken
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from src.azure_auth import AzureSignInRequired
from src.game_ui import wrap_text
from src.hospital_game import load_patient_scenario
from src.audio_playback import AudioPlaybackError
from tools.preview_performance import RecordingSink
from src.realtime_conversation import ConversationResult, PatientAnimator, PatientType, Test, _conversation_session, _run_conversation, _test_tools, strat_conversation


ROOT = Path(__file__).resolve().parents[1]
COUGH_PATH = ROOT / "assets/audio/coughvid/dry_01.wav"


class AudioEvidenceTests(unittest.TestCase):
    def test_cold_kid_configures_mild_dry_cough(self):
        scenario = load_patient_scenario(ROOT / "data/prompts/01_common_cold_kid.json")
        assert scenario.performance_profile is not None
        self.assertEqual(scenario.performance_profile.clip_path, COUGH_PATH.with_name("dry_01_short.wav"))
        self.assertIn("call the cough symptom tool", scenario.system_prompts)
        tools, by_name = _test_tools(scenario.tests)
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(test.audio is None for test in by_name.values()))
        self.assertNotIn("assets/", json.dumps(tools))

    def test_audio_is_optional_and_paths_resolve_from_project_root(self):
        self.assertIsNone(Test("text", "result").audio_path)
        self.assertEqual(Test("cough", "Listen", str(COUGH_PATH)).audio_path, COUGH_PATH)
        test = Test("cough", "Listen", "assets/audio/coughvid/dry_01.wav")
        self.assertEqual(test.audio_path, COUGH_PATH)
        self.assertIsNone(test.image_path)

    def test_invalid_audio_configuration_is_rejected(self):
        for audio in json.loads('["", "  ", "clip.mp3", 42]'):
            with self.subTest(audio=audio), self.assertRaises(ValueError):
                Test("cough", "Listen", audio)

class ConversationUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        pygame.init()
        cls.window = pygame.display.set_mode((720, 720))

    @classmethod
    def tearDownClass(cls):
        pygame.quit()
        cls.loop.close()
        asyncio.set_event_loop(None)

    def setUp(self):
        self.animator = PatientAnimator(0, window=self.window)
        self.addCleanup(self.animator.close)
        self.stop = asyncio.Event()

    def key(self, value):
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=value), self.stop)

    def test_evidence_blocks_text_and_microphone(self):
        self.animator._text_input = "do not send"
        self.animator._set_text_focus(True)
        self.animator.show_test_result(Test("Temperature", "38 C"))
        self.key(pygame.K_LSHIFT)
        self.animator._submit_text()
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="hidden"), self.stop)
        self.assertFalse(self.animator.push_to_talk)
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(self.animator._text_input, "do not send")
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.animator.evidence_open)
        self.assertFalse(self.stop.is_set())

    def test_win_blocks_send(self):
        self.animator._text_input = "hidden"
        self.animator.show_win()
        self.key(pygame.K_RETURN)
        self.animator._submit_text()
        self.assertTrue(self.animator._text_messages.empty())

    def test_focus_loss_releases_microphone(self):
        self.key(pygame.K_LSHIFT)
        self.assertTrue(self.animator.push_to_talk)
        self.animator.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), self.stop)
        self.assertFalse(self.animator.push_to_talk)

    def test_return_requires_confirmation(self):
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.stop.is_set())
        self.key(pygame.K_DOWN)
        self.key(pygame.K_RETURN)
        self.assertTrue(self.stop.is_set())
        self.assertFalse(self.animator.won)
        self.assertFalse(self.animator.quit_requested)

    def test_quit_is_distinct(self):
        self.animator.handle_event(pygame.event.Event(pygame.QUIT), self.stop)
        self.assertTrue(self.animator.quit_requested)
        self.assertTrue(self.stop.is_set())

    def test_transcript_streams_without_duplicates(self):
        self.animator.add_transcript("Patient", "Hello ", "response", append=True)
        self.animator.add_transcript("Patient", "doctor.", "response", append=True)
        self.animator.add_transcript("Patient", "Hello doctor.", "response")
        self.assertEqual(self.animator._transcript, [("Patient", "Hello doctor.")])

    def test_long_evidence_is_scrollable(self):
        self.animator.show_test_result(Test("Detailed " * 20, "longfinding" * 150))
        self.animator.draw()
        self.assertGreater(self.animator._evidence_max_scroll, 0)
        self.key(pygame.K_PAGEDOWN)
        self.assertGreater(self.animator._evidence_scroll, 0)
        font = self.animator._test_result_font
        self.assertTrue(all(font.size(line)[0] <= 340 for line in wrap_text("longword" * 80, font, 340)))

    def test_empty_focused_field_has_caret(self):
        self.animator._set_text_focus(True)
        with patch("src.realtime_conversation.time.monotonic", return_value=2), patch("pygame.draw.line", wraps=pygame.draw.line) as draw_line:
            self.animator._draw_console("idle")
            self.assertGreaterEqual(draw_line.call_count, 2)

    def test_connecting_blocks_send(self):
        self.animator._ready = False
        self.animator._text_input = "queued"
        self.animator._submit_text()
        self.assertTrue(self.animator._text_messages.empty())

    def test_sign_in_button_requests_authenticated_retry(self):
        self.animator.show_sign_in("Sign in to continue.")
        self.key(pygame.K_RETURN)
        self.assertTrue(self.animator.sign_in_requested)
        self.assertTrue(self.animator.retry_requested)
        self.assertTrue(self.stop.is_set())

    def test_pending_sign_in_blocks_input_and_can_return(self):
        self.animator.show_sign_in("Continue in your browser.", pending=True)
        self.key(pygame.K_LSHIFT)
        self.animator._text_input = "hidden"
        self.animator._submit_text()
        self.assertFalse(self.animator.push_to_talk)
        self.assertTrue(self.animator._text_messages.empty())
        self.key(pygame.K_RETURN)
        self.assertTrue(self.stop.is_set())
        self.assertFalse(self.animator.retry_requested)

    def test_sign_in_menu_supports_mouse_at_window_scale(self):
        self.animator.show_sign_in("Sign in to continue.")
        self.animator.handle_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(360, 373)),
            self.stop,
        )
        self.assertTrue(self.animator.sign_in_requested)

    def test_sign_in_details_fit_above_buttons(self):
        for detail in (
            "Sign in with a Microsoft work or school account that has access to this Azure resource.",
            "Finish Microsoft sign-in in your browser. This consultation will continue automatically.",
            "Azure denied access. Sign in with an authorized account, or ask the resource owner for access.",
        ):
            self.animator.show_sign_in(detail)
            menu = self.animator._menu
            lines = wrap_text(detail, menu._small_font, 300)
            self.assertLessEqual(174 + len(lines) * 17, menu._buttons[0].top)
            self.animator.draw()

    def test_speaking_status_takes_priority_over_focused_input(self):
        self.animator.set_microphone_available(False)
        self.assertEqual(self.animator._status("idle")[0], "TEXT MODE")
        self.animator._text_input = "Hello"
        self.assertEqual(self.animator._status("idle")[0], "TYPING")
        self.assertEqual(self.animator._status("talking")[0], "PATIENT SPEAKING")

    def test_legacy_api_remains_boolean(self):
        for result in ConversationResult:
            with patch("src.realtime_conversation.start_consultation", return_value=result):
                self.assertIs(strat_conversation("prompt", "disease", PatientType.COMMON_COLD_KID, []), result == ConversationResult.SOLVED)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pygame.init()
        self.window = pygame.display.set_mode((720, 720))

    async def asyncTearDown(self):
        pygame.quit()

    async def test_complete_session_routes_cough_and_continuation_without_popup(self):
        scenario = load_patient_scenario(ROOT / "data/prompts/01_common_cold_kid.json")
        animator = PatientAnimator(0, window=self.window)
        self.addCleanup(animator.close)
        stop = asyncio.Event()
        incoming = asyncio.Queue()
        websocket = AsyncMock()
        websocket.recv.return_value = json.dumps({"type": "session.updated"})
        credential = AsyncMock()
        credential.get_token.return_value = AccessToken("test-token", 9999999999)
        sink = RecordingSink()
        response_count = 0
        captions = []
        original_add = animator.add_transcript

        def caption(speaker, text, item_id=None, *, append=False):
            original_add(speaker, text, item_id, append=append)
            if speaker == "Patient":
                captions.append(text)
                if text == "My nose is runny too.":
                    stop.set()

        async def send(raw):
            nonlocal response_count
            event = json.loads(raw)
            if event["type"] == "response.create":
                response_count += 1
                response_id = f"response-{response_count}"
                await incoming.put({"type": "response.created", "response": {"id": response_id}})
                if response_count == 1:
                    self.assertEqual(event["response"], {})
                    await incoming.put({
                        "type": "response.function_call_arguments.done",
                        "response_id": response_id, "name": "cough", "call_id": "cough-1",
                    })
                else:
                    self.assertEqual(event["response"]["tool_choice"], "none")
                    await incoming.put({
                        "type": "response.output_audio.delta", "response_id": response_id,
                        "item_id": "answer", "delta": base64.b64encode(bytes(4800)).decode(),
                    })
                    await incoming.put({
                        "type": "response.output_audio_transcript.done",
                        "response_id": response_id, "item_id": "answer",
                        "transcript": "My nose is runny too.",
                    })
                    await incoming.put({
                        "type": "response.content_part.done", "response_id": response_id,
                        "item_id": "answer",
                    })
                await incoming.put({
                    "type": "response.done", "response": {"id": response_id, "status": "completed"},
                })

        async def events():
            while not stop.is_set():
                yield json.dumps(await incoming.get())

        @asynccontextmanager
        async def connection(headers):
            yield websocket

        websocket.send.side_effect = send
        websocket.__aiter__.side_effect = events
        animator._text_messages.put_nowait("Can you cough for me?")
        with (
            patch("src.realtime_conversation.GameCredential", return_value=credential),
            patch("src.realtime_conversation._realtime_connection", connection),
            patch("src.realtime_conversation._microphone_stream", return_value=nullcontext(None)),
            patch("src.realtime_conversation.DeviceSink", return_value=sink),
            patch.object(animator, "add_transcript", side_effect=caption),
            patch.object(animator, "show_test_result") as show_evidence,
        ):
            await asyncio.wait_for(_conversation_session(
                str(scenario.system_prompts), scenario.disease, 0, scenario.tests,
                animator, stop, performance_profile=scenario.performance_profile,
            ), timeout=5)
        self.assertEqual(captions[:2], ["[coughs]", "My nose is runny too."])
        show_evidence.assert_not_called()
        self.assertEqual(response_count, 2)
        sent = [json.loads(call.args[0]) for call in websocket.send.call_args_list]
        self.assertFalse(sent[0]["session"]["audio"]["input"]["turn_detection"]["create_response"])
        self.assertEqual(
            {tool["name"] for tool in sent[0]["session"]["tools"][-4:]},
            {"cough", "sniffle", "sneeze", "throat_clear"},
        )
        tool_output = next(event["item"]["output"] for event in sent
                           if event["type"] == "conversation.item.create"
                           and event["item"]["type"] == "function_call_output")
        self.assertEqual(json.loads(tool_output), {"status": "played"})
        credential.close.assert_awaited_once()

    async def test_device_error_uses_audio_unavailable_menu(self):
        async def animation(animator, stop):
            while animator._menu is None:
                await asyncio.sleep(0)
            self.assertEqual(animator._menu.title, "AUDIO UNAVAILABLE")
            stop.set()

        with patch(
            "src.realtime_conversation._conversation_session",
            new=AsyncMock(side_effect=AudioPlaybackError("Output disconnected")),
        ), patch.object(PatientAnimator, "run", animation):
            self.assertEqual(
                await _run_conversation("prompt", "test", 0, [], window=self.window),
                ConversationResult.RETURNED,
            )

    async def test_cough_profile_is_forwarded_to_session(self):
        scenario = load_patient_scenario(ROOT / "data/prompts/01_common_cold_kid.json")
        async def session(*args, **kwargs):
            self.assertIs(kwargs["performance_profile"], scenario.performance_profile)
            args[4]._won = True
        with patch("src.realtime_conversation._conversation_session", side_effect=session):
            result = await _run_conversation(
                str(scenario.system_prompts), scenario.disease, 0, scenario.tests,
                window=self.window, performance_profile=scenario.performance_profile,
            )
        self.assertEqual(result, ConversationResult.SOLVED)

    async def test_cancel_during_connection_cleans_up(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def connection(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def animation(animator, stop):
            await started.wait()
            animator.quit_requested = True
            stop.set()

        with patch("src.realtime_conversation._conversation_session", side_effect=connection), patch.object(PatientAnimator, "run", animation):
            result = await _run_conversation("prompt", "test", 0, [], window=self.window)
        self.assertEqual(result, ConversationResult.QUIT)
        self.assertTrue(cancelled.is_set())

    async def test_connection_error_can_return(self):
        async def animation(animator, stop):
            while animator._menu is None:
                await asyncio.sleep(0)
            self.assertEqual(animator._menu.choices, ("Retry", "Return to Hospital"))
            animator._running = False
            stop.set()

        with patch("src.realtime_conversation._conversation_session", new=AsyncMock(side_effect=ConnectionError("offline"))), patch.object(PatientAnimator, "run", animation):
            result = await _run_conversation("prompt", "test", 0, [], window=self.window)
        self.assertEqual(result, ConversationResult.RETURNED)

    async def test_authentication_error_offers_sign_in_and_reconnects(self):
        attempts = []

        async def connection(prompt, disease, index, tests, animator, stop, *, sign_in=False):
            attempts.append(sign_in)
            if not sign_in:
                raise AzureSignInRequired("Sign in to continue.")
            animator._won = True

        async def animation(animator, stop):
            while animator._menu is None and not stop.is_set():
                await asyncio.sleep(0)
            if not stop.is_set():
                self.assertEqual(animator._menu.choices[0], "Sign in to Azure")
                animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), stop)

        with patch("src.realtime_conversation._conversation_session", side_effect=connection), patch.object(PatientAnimator, "run", animation):
            result = await _run_conversation("prompt", "test", 0, [], window=self.window)
        self.assertEqual(result, ConversationResult.SOLVED)
        self.assertEqual(attempts, [False, True])

    async def test_azure_access_denied_offers_account_sign_in(self):
        from websockets.datastructures import Headers

        async def animation(animator, stop):
            while animator._menu is None:
                await asyncio.sleep(0)
            self.assertEqual(animator._menu.choices, ("Sign in to Azure", "Return to Hospital"))
            stop.set()

        for status in (401, 403):
            error = InvalidStatus(Response(status, "Denied", Headers()))
            with self.subTest(status=status), patch(
                "src.realtime_conversation._conversation_session", new=AsyncMock(side_effect=error)
            ), patch.object(PatientAnimator, "run", animation):
                result = await _run_conversation("prompt", "test", 0, [], window=self.window)
            self.assertEqual(result, ConversationResult.RETURNED)

    async def test_browser_success_resumes_connection_and_clears_prompt(self):
        from src.realtime_conversation import _conversation_session

        animator = PatientAnimator(0, window=self.window)
        credential = AsyncMock()

        async def get_token(scope):
            self.assertEqual(animator._menu.title, "SIGNING IN TO AZURE")
            return AccessToken("test-token", 9999999999)

        def connect(headers):
            self.assertIsNone(animator._menu)
            self.assertEqual(headers, {"Authorization": "Bearer test-token"})
            raise ConnectionError("stop before real connection")

        credential.get_token.side_effect = get_token
        try:
            with patch("src.realtime_conversation.GameCredential", return_value=credential) as factory, patch(
                "src.realtime_conversation._realtime_connection", side_effect=connect
            ):
                with self.assertRaises(ConnectionError):
                    await _conversation_session("prompt", "test", 0, [], animator, asyncio.Event(), sign_in=True)
            factory.assert_called_once_with(sign_in=True)
            credential.close.assert_awaited_once()
        finally:
            animator.close()

    async def test_return_or_quit_during_browser_sign_in_cancels_authentication(self):
        for quit_game in (False, True):
            cancelled = asyncio.Event()
            credentials = []

            def factory(*, sign_in):
                credential = AsyncMock()
                credentials.append(credential)

                async def get_token(scope):
                    if not sign_in:
                        raise AzureSignInRequired("Sign in to continue.")
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                credential.get_token.side_effect = get_token
                return credential

            async def animation(animator, stop):
                while animator._menu is None:
                    await asyncio.sleep(0)
                if animator._menu.title == "SIGNING IN TO AZURE" and quit_game:
                    event = pygame.event.Event(pygame.QUIT)
                else:
                    event = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)
                animator.handle_event(event, stop)

            with self.subTest(quit_game=quit_game), patch(
                "src.realtime_conversation.GameCredential", side_effect=factory
            ), patch.object(PatientAnimator, "run", animation):
                result = await _run_conversation("prompt", "test", 0, [], window=self.window)
            self.assertEqual(result, ConversationResult.QUIT if quit_game else ConversationResult.RETURNED)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(len(credentials), 2)
            for credential in credentials:
                credential.close.assert_awaited_once()

    async def test_programming_errors_are_not_hidden(self):
        with patch("src.realtime_conversation._conversation_session", new=AsyncMock(side_effect=TypeError("bug"))):
            with self.assertRaises(TypeError):
                await _run_conversation("prompt", "test", 0, [], window=self.window)


if __name__ == "__main__":
    unittest.main()