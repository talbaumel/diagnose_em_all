from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pygame
from azure.core.credentials import AccessToken
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from src.azure_auth import AzureSignInRequired
from src.game_ui import is_rtl, wrap_text
from src.care_plan import Prescription, Referral
from src.consultation_review import AxisScore, ConsultationScorecard, SCORE_AXES
from src.realtime_conversation import CONSULTATION_PLAYER_DIRECTION_ROW, ConversationResult, PatientAnimator, PatientType, Test, _conversation_session, _diagnosis_matches, _run_conversation, _show_consultation_review, strat_conversation


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
        self.animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.addCleanup(self.animator.close)
        self.stop = asyncio.Event()

    def key(self, value):
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=value), self.stop)

    def test_dr_ash_faces_patient_during_consultation(self):
        self.assertEqual(CONSULTATION_PLAYER_DIRECTION_ROW, 2)
        self.assertIs(self.animator._player_frames[0], self.animator._player_frames[1])

    def test_loading_spinners_follow_dragon_and_review_wait_states(self):
        with patch("src.pokedex_ui.PokedexPanel.busy", new_callable=unittest.mock.PropertyMock, return_value=True), patch("src.pokedex_ui.draw_spinner") as dragon_spinner:
            self.animator._pokedex.draw(self.animator._screen)
        dragon_spinner.assert_called_once()

        self.animator._review_loading = True
        with patch("src.realtime_conversation.draw_spinner") as review_spinner:
            self.animator._draw_review()
        review_spinner.assert_called_once()

    def test_review_shows_numerical_overview_before_feedback(self):
        self.animator._available_test_count = 8
        self.animator.metrics.discover_test("Temperature", "38 C")
        self.animator.metrics.discover_test("Throat examination", "Mild redness")
        self.animator._scorecard = ConsultationScorecard(
            "Detailed feedback.",
            tuple(AxisScore(key, None if key == "referrals" else 4, "Observed behavior.") for key in SCORE_AXES),
        )
        with patch("src.realtime_conversation.wrap_text", wraps=wrap_text) as wrapped:
            self.animator._draw_review()
        texts = [call.args[0] for call in wrapped.call_args_list]
        self.assertIn("Tests discovered: 2/8", texts)
        self.assertIn("Clinical knowledge: 80/100", texts)
        self.assertIn("Clinical professionalism: 80/100", texts)
        self.assertIn("Referral decisions: 0/100", texts)
        self.assertIn("Referral decisions: 0/100 (insufficient evidence)", texts)
        self.assertLess(texts.index("Referral decisions: 0/100"), texts.index("Detailed feedback."))
        overview = texts[:texts.index("Referral decisions: 0/100") + 1]
        self.assertLessEqual(sum(len(wrap_text(text, self.animator._status_font, 420)) for text in overview) + 1, 14)

    def test_review_keeps_discovery_total_when_scores_pending_or_unavailable(self):
        self.animator._available_test_count = 8
        for loading, error, expected in ((True, "", "Pending"), (False, "Connection failed.", "Unavailable")):
            with self.subTest(expected=expected):
                self.animator._review_loading = loading
                self.animator._review_error = error
                with patch("src.realtime_conversation.wrap_text", wraps=wrap_text) as wrapped:
                    self.animator._draw_review()
                texts = [call.args[0] for call in wrapped.call_args_list]
                self.assertIn("Tests discovered: 0/8", texts)
                self.assertIn(f"Clinical knowledge: {expected}", texts)

    def test_pokedex_toolbar_leaves_room_for_patient_speaking(self):
        self.assertLessEqual(self.animator._pokedex_button.right, 462)
        self.assertFalse(self.animator._pokedex_button.colliderect(self.animator._care_button))
        self.assertLess(self.animator._text_send_button.bottom, 461)
        self.assertLess(self.animator._text_input_rect.bottom, 461)
        for button, label in ((self.animator._diagnose_button, "FINISH VISIT"), (self.animator._care_button, "CARE PLAN"), (self.animator._pokedex_button, "Dragon Copilot")):
            self.assertLessEqual(self.animator._status_font.size(label)[0] + 16, button.width)

    def test_dragon_copilot_opens_and_closes_by_click_at_scaled_sizes(self):
        self.addCleanup(pygame.display.set_mode, self.window.get_size())
        for size in (480, 720, 960):
            self.window = pygame.display.set_mode((size, size))
            self.animator._window = self.window
            for button in (self.animator._pokedex_button, self.animator._pokedex.close_button):
                position = tuple(round(coordinate * size / 480) for coordinate in button.center)
                self.animator.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=position), self.stop)
                self.assertEqual(self.animator._pokedex.open, button == self.animator._pokedex_button)
            self.assertFalse(self.stop.is_set())

    def test_pokedex_isolates_chat_and_restores_patient_draft(self):
        self.animator._text_input = "Patient draft"
        self.animator._set_text_focus(True)
        self.key(pygame.K_F6)
        self.assertTrue(self.animator._pokedex.open)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="Help me"), self.stop)
        self.key(pygame.K_LSHIFT)
        self.animator._submit_text()
        self.animator._open_diagnosis()
        self.animator._open_care_menu()
        self.assertFalse(self.animator.push_to_talk)
        self.assertFalse(self.animator._diagnosis_open)
        self.assertIsNone(self.animator._menu)
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(self.animator._pokedex.draft, "Help me")
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.stop.is_set())
        self.assertTrue(self.animator._text_focused)
        self.assertEqual(self.animator._text_input, "Patient draft")

    def test_pokedex_context_only_contains_observed_visit(self):
        self.animator.add_transcript("Patient", "My throat hurts.")
        self.animator.add_transcript("Case", "Hidden case feedback")
        self.animator.metrics.discover_test("Throat", "Mild redness")
        self.animator.metrics.discover_test("Temperature", "data/sprites/tests/thermometer.png")
        self.animator.metrics.care_plan.add(Referral("Clinic", "Assessment", "Routine"))
        context = self.animator._pokedex_context()
        encoded = json.dumps(context)
        self.assertIn("My throat hurts.", encoded)
        self.assertIn("Mild redness", encoded)
        self.assertIn("Clinic", encoded)
        self.assertNotIn("common cold", encoded)
        self.assertNotIn("Hidden case feedback", encoded)
        self.assertNotIn("thermometer.png", encoded)
        self.assertEqual(len(context["discovered_tests"]), 2)

    def test_pokedex_evidence_and_focus_loss_priority(self):
        self.key(pygame.K_F6)
        self.animator.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), self.stop)
        self.assertFalse(self.animator._pokedex.focused)
        self.animator.show_test_result(Test("Throat", "Mild redness"))
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.animator.evidence_open)
        self.assertTrue(self.animator._pokedex.open)
        self.assertTrue(self.animator._pokedex.focused)
        self.animator.show_error("offline")
        self.assertFalse(self.animator._pokedex.open)

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

    def test_chat_diagnosis_does_not_win(self):
        self.animator._text_input = "common cold"
        self.animator._submit_text()
        self.assertFalse(self.animator.won)
        self.assertFalse(self.animator._diagnosis_confirmed.is_set())
        self.assertEqual(self.animator._text_messages.get_nowait(), "common cold")

    def test_explicit_diagnosis_keeps_call_open_until_finished(self):
        self.animator._diagnosis_open = True
        self.animator._diagnosis_input = " Common-COLD. "
        self.animator._submit_diagnosis()
        self.animator._submit_diagnosis()
        self.assertFalse(self.animator.won)
        self.assertTrue(self.animator._diagnosis_confirmed.is_set())
        self.assertFalse(self.animator._diagnosis_open)
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(len(self.animator._transcript), 2)
        self.animator._finish_consultation()
        self.assertTrue(self.animator.won)
        self.assertTrue(self.animator._consultation_finished.is_set())

    def test_finish_requires_diagnosis_and_does_not_drop_pending_messages(self):
        self.animator._finish_consultation()
        self.assertFalse(self.animator.won)
        self.animator._diagnosis_confirmed.set()
        self.animator._text_input = "Let us discuss your care plan."
        self.animator._submit_text()
        self.animator._finish_consultation()
        self.assertFalse(self.animator.won)
        self.assertFalse(self.animator._consultation_finished.is_set())

    def test_incorrect_diagnosis_allows_retry_without_revealing_answer(self):
        self.animator._diagnosis_open = True
        self.animator._diagnosis_input = "migraine"
        self.animator._submit_diagnosis()
        self.assertFalse(self.animator.won)
        self.assertTrue(self.animator._diagnosis_open)
        self.assertNotIn("common cold", self.animator._diagnosis_feedback)
        self.animator._diagnosis_input = "common cold"
        self.animator._submit_diagnosis()
        self.assertTrue(self.animator._diagnosis_confirmed.is_set())
        self.assertFalse(self.animator.won)

    def test_diagnosis_requires_open_ready_dialog_and_nonempty_answer(self):
        for opened, ready, evidence, answer in (
            (False, True, False, "common cold"),
            (True, False, False, "common cold"),
            (True, True, True, "common cold"),
            (True, True, False, "   "),
        ):
            with self.subTest(opened=opened, ready=ready, evidence=evidence, answer=answer):
                self.animator._diagnosis_open = opened
                self.animator._ready = ready
                self.animator._test_result = Test("test", "result") if evidence else None
                self.animator._diagnosis_input = answer
                self.animator._submit_diagnosis()
                self.assertFalse(self.animator.won)
                self.assertFalse(self.animator._diagnosis_confirmed.is_set())

    def test_diagnosis_matching_rejects_negation_lists_and_blank_answers(self):
        for answer in ("not common cold", "common cold or migraine", "cold", "", "!!!"):
            self.assertFalse(_diagnosis_matches(answer, "common cold"))
        self.assertFalse(_diagnosis_matches("", ""))
        self.assertTrue(_diagnosis_matches("COMMON  COLD", "common cold"))

    def click(self, rectangle):
        width, height = self.window.get_size()
        position = (round(rectangle.centerx * width / 480), round(rectangle.centery * height / 480))
        self.animator.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=position), self.stop)

    def test_diagnosis_dialog_preserves_chat_draft_and_blocks_microphone(self):
        self.animator._text_input = "How long has this lasted?"
        self.animator._set_text_focus(True)
        self.click(self.animator._diagnose_button)
        self.assertTrue(self.animator._diagnosis_open)
        self.key(pygame.K_LSHIFT)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="migraine"), self.stop)
        self.animator._submit_text()
        self.assertFalse(self.animator.push_to_talk)
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(self.animator._diagnosis_input, "migraine")
        self.key(pygame.K_ESCAPE)
        self.assertFalse(self.animator._diagnosis_open)
        self.assertTrue(self.animator._text_focused)
        self.assertEqual(self.animator._text_input, "How long has this lasted?")
        self.assertFalse(self.stop.is_set())
        self.assertFalse(self.animator.won)

    def test_diagnosis_keyboard_submission(self):
        self.key(pygame.K_F2)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="common cold"), self.stop)
        self.key(pygame.K_TAB)
        self.key(pygame.K_TAB)
        self.assertEqual(self.animator._diagnosis_focus, 2)
        self.key(pygame.K_RETURN)
        self.assertFalse(self.animator.won)
        self.key(pygame.K_F2)
        self.key(pygame.K_DOWN)
        self.key(pygame.K_RETURN)
        self.assertTrue(self.animator.won)

    def test_diagnosis_click_submission_and_cancel(self):
        self.click(self.animator._diagnose_button)
        self.click(self.animator._diagnosis_cancel_button)
        self.assertFalse(self.animator._diagnosis_open)
        self.assertFalse(self.animator.won)
        self.click(self.animator._diagnose_button)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="common cold"), self.stop)
        self.click(self.animator._diagnosis_submit_button)
        self.assertTrue(self.animator._diagnosis_confirmed.is_set())
        self.assertFalse(self.animator.won)

    def test_diagnosis_focus_loss_blocks_typing_until_refocused(self):
        self.key(pygame.K_F2)
        self.animator.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="hidden"), self.stop)
        self.assertEqual(self.animator._diagnosis_input, "")
        self.click(self.animator._diagnosis_input_rect)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="common cold"), self.stop)
        self.assertEqual(self.animator._diagnosis_input, "common cold")

    def test_diagnosis_unavailable_during_connection_evidence_and_menu(self):
        self.animator._ready = False
        self.key(pygame.K_F2)
        self.assertFalse(self.animator._diagnosis_open)
        self.animator._ready = True
        self.animator.show_test_result(Test("Temperature", "38 C"))
        self.key(pygame.K_F2)
        self.assertFalse(self.animator._diagnosis_open)
        self.key(pygame.K_ESCAPE)
        self.key(pygame.K_ESCAPE)
        self.key(pygame.K_F2)
        self.assertFalse(self.animator._diagnosis_open)

    def test_diagnosis_dialog_draws_long_input_and_feedback(self):
        self.key(pygame.K_F2)
        self.animator._diagnosis_input = "longdiagnosis" * 40
        self.animator._submit_diagnosis()
        self.animator.draw()
        self.assertFalse(self.animator.won)
        self.assertTrue(self.animator._diagnosis_feedback)

    def test_focus_loss_releases_microphone(self):
        self.key(pygame.K_LSHIFT)
        self.assertTrue(self.animator.push_to_talk)
        self.animator.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), self.stop)
        self.assertFalse(self.animator.push_to_talk)

    def test_prescription_form_records_order_and_preserves_chat_draft(self):
        self.animator._text_input = "existing draft"
        self.animator._set_text_focus(True)
        self.key(pygame.K_F4)
        self.key(pygame.K_RETURN)
        self.assertIsNotNone(self.animator._care_form)
        self.key(pygame.K_LSHIFT)
        self.assertFalse(self.animator.push_to_talk)
        for index, value in enumerate(("Example medication", "Player-entered directions", "Symptom relief")):
            self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=value), self.stop)
            if index < 2:
                self.key(pygame.K_TAB)
        self.key(pygame.K_RETURN)
        self.assertIsNone(self.animator._care_form)
        order = self.animator.metrics.care_plan.prescriptions[0]
        self.assertEqual(order.medication, "Example medication")
        self.assertEqual(self.animator._text_messages.get_nowait(), order.message())
        self.assertEqual(self.animator._text_input, "existing draft")
        self.assertFalse(self.animator.won)

    def test_referral_form_urgency_and_review(self):
        self.key(pygame.K_F4)
        self.key(pygame.K_DOWN)
        self.key(pygame.K_RETURN)
        self.animator._care_form.values[:2] = ["Specialist clinic", "Further assessment"]
        self.animator._care_form.focus(2)
        self.key(pygame.K_RIGHT)
        self.key(pygame.K_RETURN)
        self.assertEqual(self.animator.metrics.care_plan.referrals, [Referral("Specialist clinic", "Further assessment", "Urgent")])
        self.key(pygame.K_F4)
        self.key(pygame.K_DOWN)
        self.key(pygame.K_DOWN)
        self.key(pygame.K_RETURN)
        self.assertIn("Specialist clinic", self.animator._test_result.results)
        self.assertEqual(len(self.animator.metrics.discovered_tests), 0)

    def test_care_form_validation_cancel_and_focus_loss(self):
        self.key(pygame.K_F4)
        self.key(pygame.K_RETURN)
        form = self.animator._care_form
        form.focus(4)
        self.key(pygame.K_RETURN)
        self.assertTrue(form.error)
        self.assertEqual(self.animator.metrics.care_plan.prescriptions, [])
        self.animator.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="hidden"), self.stop)
        self.assertEqual(form.values, ["", "", ""])
        self.key(pygame.K_ESCAPE)
        self.assertIsNone(self.animator._care_form)
        self.assertTrue(self.animator._text_messages.empty())

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

    def test_chat_fonts_have_distinct_hebrew_and_russian_glyphs(self):
        for font in (self.animator._text_font, self.animator._status_font):
            for alphabet in ("\u05e9\u05dc\u05d5\u05dd", "\u041f\u0440\u0438\u0432\u0435\u0442"):
                with self.subTest(alphabet=alphabet, height=font.get_height()):
                    self.assertTrue(all(metric is not None for metric in font.metrics(alphabet)))
                    glyphs = {
                        pygame.image.tostring(font.render(character, True, (255, 255, 255)), "RGBA")
                        for character in alphabet
                    }
                    self.assertEqual(len(glyphs), len(alphabet))

    def test_multilingual_input_is_sent_in_logical_order(self):
        for message in ("\u05e9\u05dc\u05d5\u05dd 123", "\u041f\u0440\u0438\u0432\u0435\u0442 123"):
            with self.subTest(message=message):
                self.animator._sending = False
                self.animator._set_text_focus(True)
                self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=message), self.stop)
                self.assertEqual(self.animator._text_input, message)
                self.animator.draw()
                self.animator._submit_text()
                self.assertEqual(self.animator._text_messages.get_nowait(), message)
                self.assertIn(("You", message), self.animator._transcript)

    def test_hebrew_transcript_reorders_only_at_render_time(self):
        message = "\u05e9\u05dc\u05d5\u05dd"
        self.animator.add_transcript("Patient", message, "response")
        with patch.object(self.animator, "_status_font", wraps=self.animator._status_font) as font:
            self.animator._draw_transcript()
            self.assertEqual(font.render.call_args.args[0], "Patient: \u05dd\u05d5\u05dc\u05e9")
        self.assertEqual(self.animator._transcript, [("Patient", message)])
        self.assertEqual(self.animator._transcript_lines, [f"Patient: {message}"])

    def test_hebrew_input_renders_rtl_and_keeps_caret_visible(self):
        self.animator._set_text_focus(True)
        for message in ("\u05e9\u05dc\u05d5\u05dd", "\u05e9\u05dc\u05d5\u05dd" * 100):
            with self.subTest(length=len(message)):
                self.animator._text_input = message
                with patch.object(self.animator, "_text_font", wraps=self.animator._text_font) as font, patch("src.realtime_conversation.time.monotonic", return_value=2), patch("pygame.draw.line", wraps=pygame.draw.line) as draw_line:
                    self.animator._draw_console("idle")
                    self.assertEqual(font.render.call_args.args[0], message[::-1])
                area = self.animator._text_input_rect.inflate(-18, -8)
                expected_x = max(area.x, area.right - self.animator._text_font.size(message)[0] - 2)
                self.assertTrue(any(call.args[2] == (expected_x, area.y + 3) for call in draw_line.call_args_list))

    def test_multilingual_wrapping_preserves_logical_order(self):
        for word in ("\u05e9\u05dc\u05d5\u05dd", "\u041f\u0440\u0438\u0432\u0435\u0442"):
            text = word * 100
            lines = wrap_text(text, self.animator._status_font, 438)
            self.assertGreater(len(lines), 1)
            self.assertEqual("".join(lines), text)
            self.assertTrue(all(self.animator._status_font.size(line)[0] <= 438 for line in lines))
        self.assertTrue(is_rtl("123 \u05e9\u05dc\u05d5\u05dd"))
        self.assertFalse(is_rtl("\u041f\u0440\u0438\u0432\u0435\u0442"))
        self.assertFalse(is_rtl("123"))

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
                self.assertIs(strat_conversation("prompt", "disease", PatientType.COMMON_COLD_KID, []), result in (ConversationResult.SOLVED, ConversationResult.SOLVED_QUIT))

    def test_discovered_test_list_does_not_reveal_other_tests_or_increment_count(self):
        self.animator.metrics.discover_test("Temperature", "38 C")
        self.key(pygame.K_F3)
        self.assertTrue(self.animator.evidence_open)
        self.assertEqual(self.animator._test_result.results, "Temperature")
        self.assertEqual(len(self.animator.metrics.discovered_tests), 1)

    def test_review_scroll_and_return(self):
        self.animator.show_win()
        self.animator._review_open = True
        self.animator._review_error = "Scoring unavailable. " * 100
        self.animator.draw()
        self.assertGreater(self.animator._review_max_scroll, 0)
        self.key(pygame.K_PAGEDOWN)
        self.assertGreater(self.animator._review_scroll, 0)
        self.key(pygame.K_RETURN)
        self.assertTrue(self.stop.is_set())
        self.assertTrue(self.animator.won)

    def test_pending_review_blocks_return_until_scoring_completes(self):
        self.animator._review_open = True
        self.animator._review_loading = True
        for key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
            self.key(key)
            self.assertFalse(self.stop.is_set())
        center = self.animator._review_return_button.center
        position = (center[0] * 720 / 480, center[1] * 720 / 480)
        click = pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=position)
        self.animator.handle_event(click, self.stop)
        self.assertFalse(self.stop.is_set())
        self.animator._review_loading = False
        self.animator.handle_event(click, self.stop)
        self.assertTrue(self.stop.is_set())

    def test_review_retry_and_loading_input_does_not_restart_consultation(self):
        self.animator.show_win()
        self.animator._review_open = True
        self.animator._review_error = "Scoring unavailable."
        self.key(pygame.K_F5)
        self.assertTrue(self.animator._review_retry.is_set())
        self.assertTrue(self.animator._review_loading)
        self.assertFalse(self.animator.retry_requested)
        self.key(pygame.K_LSHIFT)
        self.assertFalse(self.animator.push_to_talk)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cache = patch("src.realtime_conversation._cached_realtime_token", None)
        cache.start()
        self.addCleanup(cache.stop)
        pygame.init()
        self.window = pygame.display.set_mode((720, 720))

    async def asyncTearDown(self):
        pygame.quit()

    async def test_completed_consultation_opens_review_after_session_cleanup(self):
        session_closed = asyncio.Event()

        async def session(prompt, disease, index, tests, animator, stop, *, sign_in=False):
            try:
                animator.show_win()
                stop.set()
                return True
            finally:
                session_closed.set()

        async def review(animator, prompt, disease, tests):
            self.assertTrue(session_closed.is_set())
            animator.quit_requested = True

        with patch("src.realtime_conversation._conversation_session", session), patch("src.realtime_conversation._show_consultation_review", review):
            result = await _run_conversation("patient", "common cold", 0, [], window=self.window)
        self.assertEqual(result, ConversationResult.SOLVED_QUIT)

    async def test_review_retries_failure_without_losing_completion(self):
        animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.addCleanup(animator.close)
        animator.show_win()
        scorecard = ConsultationScorecard("Review complete.", tuple(AxisScore(key, 3, "Relevant questions.") for key in SCORE_AXES))

        @asynccontextmanager
        async def authorize():
            yield {}

        async def animation(animator, stop):
            while not animator._review_error:
                await asyncio.sleep(0)
            self.assertTrue(animator.won)
            animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F5), stop)
            while animator._scorecard is None:
                await asyncio.sleep(0)
            animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), stop)

        with patch("src.realtime_conversation._realtime_authorization", authorize), patch("src.realtime_conversation.score_consultation", new=AsyncMock(side_effect=[ValueError("bad response"), scorecard])) as scoring, patch.object(PatientAnimator, "run", animation):
            await asyncio.wait_for(_show_consultation_review(animator, "patient", "common cold", []), 2)
        self.assertEqual(scoring.await_count, 2)
        self.assertTrue(animator.won)
        self.assertFalse(animator._review_open)

    async def test_review_timeout_unlocks_return(self):
        animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.addCleanup(animator.close)
        animator.show_win()

        @asynccontextmanager
        async def authorize():
            yield {}

        async def animation(animator, stop):
            while not animator._review_error:
                await asyncio.sleep(0)
            self.assertFalse(animator._review_loading)
            animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), stop)
            self.assertTrue(stop.is_set())

        with patch("src.realtime_conversation._realtime_authorization", authorize), patch("src.realtime_conversation.score_consultation", new=AsyncMock(side_effect=asyncio.TimeoutError)), patch.object(PatientAnimator, "run", animation):
            await asyncio.wait_for(_show_consultation_review(animator, "patient", "common cold", []), 2)
        self.assertTrue(animator.won)

    async def test_closing_window_during_pending_review_cancels_scoring(self):
        animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.addCleanup(animator.close)
        animator.show_win()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        @asynccontextmanager
        async def authorize():
            yield {}

        async def score(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def animation(animator, stop):
            await started.wait()
            animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), stop)
            self.assertFalse(stop.is_set())
            animator.handle_event(pygame.event.Event(pygame.QUIT), stop)
            self.assertTrue(stop.is_set())

        with patch("src.realtime_conversation._realtime_authorization", authorize), patch("src.realtime_conversation.score_consultation", score), patch.object(PatientAnimator, "run", animation):
            await asyncio.wait_for(_show_consultation_review(animator, "patient", "common cold", []), 2)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(animator.won)

    async def test_realtime_win_event_is_ignored_and_explicit_submission_finishes(self):
        processed = asyncio.Event()
        receiver_closed = asyncio.Event()
        care_sent = asyncio.Event()

        async def send(payload):
            event = json.loads(payload)
            if event["type"] == "response.create":
                care_sent.set()

        async def events():
            try:
                yield json.dumps({"type": "response.function_call_arguments.done", "name": "win", "call_id": "untrusted"})
                yield json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": "common cold", "item_id": "voice"})
                yield json.dumps({"type": "response.function_call_arguments.done", "name": "perform_test_1_temperature", "call_id": "test-1"})
                yield json.dumps({"type": "response.function_call_arguments.done", "name": "perform_test_1_temperature", "call_id": "test-2"})
                processed.set()
                await asyncio.Event().wait()
            finally:
                receiver_closed.set()

        websocket = MagicMock()
        websocket.send = AsyncMock(side_effect=send)
        websocket.recv = AsyncMock(return_value=json.dumps({"type": "session.updated"}))
        websocket.__aiter__.side_effect = events

        @asynccontextmanager
        async def authorize():
            yield {}

        @asynccontextmanager
        async def connect(headers):
            yield websocket

        @contextmanager
        def microphone(callback):
            yield None

        async def close_evidence(animator):
            animator.close_test_result()

        animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.addCleanup(animator.close)
        animator._ready = False
        stop = asyncio.Event()
        with (
            patch("src.realtime_conversation._realtime_authorization", authorize),
            patch("src.realtime_conversation._realtime_connection", connect),
            patch("src.realtime_conversation._microphone_stream", microphone),
            patch("src.realtime_conversation.sd.RawOutputStream") as output,
            patch("src.realtime_conversation.RELIEVED_DURATION_SECONDS", 0.01),
            patch.object(PatientAnimator, "wait_for_evidence_close", close_evidence),
        ):
            task = asyncio.create_task(_conversation_session("patient", "common cold", 0, [Test("Temperature", "38 C")], animator, stop))
            try:
                await asyncio.wait_for(processed.wait(), 2)
                configuration = json.loads(websocket.send.call_args_list[0].args[0])["session"]
                self.assertNotIn("win", [tool["name"] for tool in configuration["tools"]])
                self.assertEqual(len(configuration["tools"]), 1)
                self.assertFalse(animator.won)
                self.assertFalse(stop.is_set())
                self.assertIn(("You", "common cold"), animator._transcript)
                self.assertEqual(animator.metrics.discovered_tests, {"Temperature": "38 C"})
                self.assertIsNotNone(animator.metrics.started_at)
                animator._open_diagnosis()
                animator._diagnosis_input = "migraine"
                animator._submit_diagnosis()
                self.assertFalse(animator.won)
                self.assertFalse(task.done())
                animator._diagnosis_input = "common cold"
                animator._submit_diagnosis()
                self.assertFalse(animator.won)
                self.assertFalse(task.done())
                prescription = Prescription("Example medication", "Player-entered directions", "Symptom relief")
                animator._record_care_order(prescription)
                animator._open_care_menu()
                await asyncio.wait_for(care_sent.wait(), 2)
                sent_items = [json.loads(call.args[0]) for call in websocket.send.call_args_list]
                self.assertTrue(any(event.get("item", {}).get("content") == [{"type": "input_text", "text": prescription.message()}] for event in sent_items))
                self.assertFalse(animator.won)
                animator._menu = None
                animator._finish_consultation()
                self.assertTrue(await asyncio.wait_for(task, 2))
                self.assertIsNotNone(animator.metrics.finished_at)
                self.assertTrue(stop.is_set())
                self.assertTrue(receiver_closed.is_set())
                output.return_value.__enter__.return_value.abort.assert_called()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

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

        with patch("src.realtime_conversation._conversation_session", side_effect=connection), patch.object(PatientAnimator, "run", animation), patch("src.realtime_conversation._show_consultation_review", new=AsyncMock()):
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