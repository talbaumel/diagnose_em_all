from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import pygame

from src.game_ui import wrap_text
from src.realtime_conversation import ConversationResult, PatientAnimator, PatientType, Test, _run_conversation, strat_conversation


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

    async def test_cancel_during_connection_cleans_up(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def connection(*args):
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
            animator._running = False
            stop.set()

        with patch("src.realtime_conversation._conversation_session", new=AsyncMock(side_effect=ConnectionError("offline"))), patch.object(PatientAnimator, "run", animation):
            result = await _run_conversation("prompt", "test", 0, [], window=self.window)
        self.assertEqual(result, ConversationResult.RETURNED)

    async def test_programming_errors_are_not_hidden(self):
        with patch("src.realtime_conversation._conversation_session", new=AsyncMock(side_effect=TypeError("bug"))):
            with self.assertRaises(TypeError):
                await _run_conversation("prompt", "test", 0, [], window=self.window)


if __name__ == "__main__":
    unittest.main()