from __future__ import annotations

import asyncio
import base64
import json
import unittest
from copy import deepcopy
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pygame
from azure.core.credentials import AccessToken
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from src.azure_auth import AzureSignInRequired
from src.game_ui import is_rtl, wrap_text
from src.hospital_game import load_patient_scenario
from src.audio_playback import AudioPlaybackError
from src.care_plan import Prescription, Referral
from src.consultation_review import AxisScore, ConsultationScorecard, SCORE_AXES
from src.diagnostic_skills import SkillEngine, load_catalog
from src.skill_activity import ThermometerActivity
from src.skill_browser import ADVANCED_TEST_IDS, AdvancedTestDrawer, EquipmentDrawer
from src.skill_activity import SUPPORTED_ACTIVITIES
from tests.audio_fakes import until
from src.patient_celebration import CELEBRATION_SECONDS
from tools.preview_performance import RecordingSink
from src.realtime_conversation import CONSULTATION_PLAYER_DIRECTION_ROW, ConversationResult, PatientAnimator, PatientType, Test, _conversation_session, _diagnosis_matches, _run_conversation, _show_consultation_review, strat_conversation
from src.realtime_conversation import _test_tools


ROOT = Path(__file__).resolve().parents[1]
COUGH_PATH = ROOT / "data/audio/coughvid/dry_01.wav"


class AudioEvidenceTests(unittest.TestCase):
    def test_cold_kid_configures_mild_dry_cough(self):
        scenario = load_patient_scenario(ROOT / "data/prompts/01_common_cold_kid.json")
        assert scenario.performance_profile is not None
        self.assertEqual(scenario.performance_profile.clip_path, COUGH_PATH.with_name("dry_01_short.wav"))
        self.assertIn("call the cough symptom tool", scenario.system_prompts)
        tools, by_name = _test_tools(scenario.tests)
        self.assertEqual(len(tools), len(load_catalog()) + 1)
        self.assertEqual(set(by_name.values()), {skill.id for skill in load_catalog()} | {"you_win"})
        self.assertTrue(all(test.audio is None for test in scenario.tests))
        self.assertNotIn("assets/", json.dumps(tools))

    def test_audio_is_optional_and_paths_resolve_from_project_root(self):
        self.assertIsNone(Test("text", "result").audio_path)
        self.assertEqual(Test("cough", "Listen", audio=str(COUGH_PATH)).audio_path, COUGH_PATH)
        test = Test("cough", "Listen", audio="data/audio/coughvid/dry_01.wav")
        self.assertEqual(test.audio_path, COUGH_PATH)
        self.assertIsNone(test.image_path)

    def test_invalid_audio_configuration_is_rejected(self):
        for audio in json.loads('["", "  ", "clip.mp3", 42]'):
            with self.subTest(audio=audio), self.assertRaises(ValueError):
                Test("cough", "Listen", audio=audio)

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

    def test_recording_prompt_explains_shift_release_and_fits_status_badge(self):
        self.key(pygame.K_LSHIFT)
        text, _ = self.animator._status("idle")
        self.assertEqual(text, "RELEASE SHIFT TO SEND")
        self.assertLessEqual(self.animator._status_font.size(text)[0], 160)
        self.animator.draw()
        self.animator.handle_event(pygame.event.Event(pygame.KEYUP, key=pygame.K_LSHIFT), self.stop)
        self.assertEqual(self.animator._status("idle")[0], "READY")

    def test_room_drawer_opens_before_picking_up_thermometer(self):
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [])
        self.animator._text_input = "Unsent interview question"
        self.animator._set_text_focus(True)
        self.animator.draw()
        x, y = self.animator._equipment_drawer_hotspot.center
        self.animator.handle_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(x * 1.5, y * 1.5)), self.stop,
        )
        self.assertIsInstance(self.animator._skill_browser, EquipmentDrawer)
        self.assertEqual({s.id for s in self.animator._skill_browser.catalog}, SUPPORTED_ACTIVITIES)
        self.assertTrue(self.animator._local_skill_requests.empty())
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.key(pygame.K_RETURN)
        self.key(pygame.K_F7)
        self.key(pygame.K_F2)
        self.assertEqual(self.animator._local_skill_requests.qsize(), 1)
        self.assertEqual(self.animator._local_skill_requests.get_nowait(), "temperature")
        self.assertFalse(self.animator._diagnosis_open)
        self.assertTrue(self.animator.skills_modal)
        self.assertTrue(self.animator._local_skill_chat_was_focused)
        self.assertEqual(self.animator._text_input, "Unsent interview question")
        self.assertTrue(self.animator._text_messages.empty())

    def test_drawer_closes_without_action_and_restores_draft(self):
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [])
        self.animator._text_input = "Unsent draft"
        self.animator._set_text_focus(True)
        for close_key in (pygame.K_ESCAPE, pygame.K_F7):
            self.key(pygame.K_F7)
            self.assertIsInstance(self.animator._skill_browser, EquipmentDrawer)
            self.assertFalse(self.animator.push_to_talk)
            self.key(close_key)
            self.assertIsNone(self.animator._skill_browser)
            self.assertTrue(self.animator._text_focused)
        self.assertEqual(self.animator._text_input, "Unsent draft")
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.assertTrue(self.animator._local_skill_requests.empty())

    def test_old_floating_thermometer_is_gone_and_drawer_only_highlights_on_hover(self):
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [])
        before = pygame.image.tobytes(self.animator._screen, "RGB")
        self.animator._draw_equipment_drawer_hint()
        self.assertEqual(pygame.image.tobytes(self.animator._screen, "RGB"), before)
        self.animator.handle_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(90 * 1.5, 103 * 1.5)), self.stop,
        )
        self.assertIsNone(self.animator._skill_browser)
        x, y = self.animator._equipment_drawer_hotspot.center
        self.animator.handle_event(
            pygame.event.Event(pygame.MOUSEMOTION, pos=(x * 1.5, y * 1.5)), self.stop,
        )
        self.assertTrue(self.animator._drawer_hovered)
        self.animator._draw_equipment_drawer_hint()
        self.assertNotEqual(pygame.image.tobytes(self.animator._screen, "RGB"), before)
        self.animator.handle_event(pygame.event.Event(pygame.WINDOWLEAVE), self.stop)
        self.assertFalse(self.animator._drawer_hovered)

    def test_temperature_catalog_uses_instrument_without_sending_draft(self):
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [])
        self.animator._text_input = "Draft"
        self.key(pygame.K_F5)
        browser = self.animator._skill_browser
        browser.selected = next(i for i, skill in enumerate(browser.catalog) if skill.id == "temperature")
        self.key(pygame.K_RETURN)
        self.key(pygame.K_RETURN)
        self.assertIsNone(self.animator._skill_browser)
        self.assertEqual(self.animator._local_skill_requests.get_nowait(), "temperature")
        self.assertEqual(self.animator._text_input, "Draft")
        self.assertTrue(self.animator._text_messages.empty())

    def test_advanced_room_drawer_drafts_request_without_ordering_or_losing_text(self):
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [])
        self.animator._text_input = "Existing question."
        self.animator._set_text_focus(True)
        self.assertFalse(self.animator._equipment_drawer_hotspot.colliderect(self.animator._advanced_drawer_hotspot))
        x, y = self.animator._advanced_drawer_hotspot.center
        self.animator.handle_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(x * 1.5, y * 1.5)), self.stop,
        )
        browser = self.animator._skill_browser
        self.assertIsInstance(browser, AdvancedTestDrawer)
        self.assertEqual({s.id for s in browser.catalog}, ADVANCED_TEST_IDS)
        self.assertFalse(self.animator.push_to_talk)
        browser.selected = next(i for i, s in enumerate(browser.catalog) if s.id == "targeted_pathogen_pcr")
        self.key(pygame.K_RETURN)
        self.animator.draw()
        self.key(pygame.K_RETURN)
        self.assertIsNone(self.animator._skill_browser)
        self.assertTrue(self.animator._text_input.startswith("Existing question. "))
        self.assertTrue(self.animator._text_focused)
        self.assertTrue(self.animator._text_messages.empty())
        self.assertTrue(self.animator._local_skill_requests.empty())
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.key(pygame.K_F8)
        self.assertIsInstance(self.animator._skill_browser, AdvancedTestDrawer)
        self.key(pygame.K_F8)
        self.assertIsNone(self.animator._skill_browser)

    def test_instrument_cannot_start_while_connecting_or_after_finish(self):
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [])
        self.animator._ready = False
        self.key(pygame.K_F7)
        self.animator._request_local_skill("temperature")
        self.assertTrue(self.animator._local_skill_requests.empty())
        self.assertIsNone(self.animator._skill_browser)
        self.animator._ready = True
        self.animator._diagnosis_confirmed.set()
        self.animator.show_win()
        self.key(pygame.K_F7)
        self.animator._request_local_skill("temperature")
        self.assertTrue(self.animator.won)
        self.assertTrue(self.animator._local_skill_requests.empty())
        self.assertIsNone(self.animator._skill_browser)

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

    def test_give_up_and_copilot_toolbar_leaves_room_for_patient_speaking(self):
        self.assertLessEqual(self.animator._diagnose_button.right, 462)
        self.assertFalse(self.animator._diagnose_button.colliderect(self.animator._pokedex_button))
        self.assertLess(self.animator._text_send_button.bottom, 461)
        self.assertLess(self.animator._text_input_rect.bottom, 461)
        self.assertLessEqual(self.animator._status_font.size("GIVE UP")[0] + 16, self.animator._diagnose_button.width)
        self.assertLessEqual(self.animator._status_font.size("Dragon Simulator Assist")[0] + 32, self.animator._pokedex_button.width)

    def test_dragon_copilot_clicks_and_removed_visit_buttons_do_not(self):
        self.addCleanup(pygame.display.set_mode, self.window.get_size())
        for size in (480, 720, 960):
            self.window = pygame.display.set_mode((size, size))
            self.animator._window = self.window
            position = tuple(round(coordinate * size / 480) for coordinate in self.animator._pokedex_button.center)
            self.animator.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=position), self.stop)
            self.assertTrue(self.animator._pokedex.open)
            self.animator._pokedex.hide()
            for button in (self.animator._care_button, self.animator._skills_button, self.animator._discovered_tests_button):
                position = tuple(round(coordinate * size / 480) for coordinate in button.center)
                self.animator.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=position), self.stop)
                self.assertIsNone(self.animator._menu)
                self.assertFalse(self.animator._pokedex.open)
                self.assertFalse(self.animator.evidence_open)
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

    def test_chat_diagnosis_waits_for_patient_win_tool(self):
        self.animator._text_input = "common cold"
        self.animator._submit_text()
        self.assertFalse(self.animator.won)
        self.assertFalse(self.animator._diagnosis_confirmed.is_set())
        self.assertEqual(self.animator._text_messages.get_nowait(), "common cold")

    def test_explicit_diagnosis_keeps_call_open_until_you_win(self):
        self.animator._diagnosis_open = True
        self.animator._diagnosis_input = " Common-COLD. "
        self.animator._submit_diagnosis()
        self.animator._submit_diagnosis()
        self.assertFalse(self.animator.won)
        self.assertTrue(self.animator._diagnosis_confirmed.is_set())
        self.assertFalse(self.animator._diagnosis_open)
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(len(self.animator._transcript), 2)
        self.assertFalse(self.animator.won)
        self.assertFalse(self.animator._consultation_finished.is_set())

    def test_all_patients_celebrate_without_changing_scores_or_completing(self):
        for path in sorted((ROOT / "data/prompts").glob("*.json")):
            scenario = load_patient_scenario(path)
            with self.subTest(patient=scenario.patient_type), patch("src.realtime_conversation.time.monotonic", return_value=1000) as clock:
                animator = PatientAnimator(list(PatientType).index(scenario.patient_type), window=self.window, disease=scenario.disease)
                try:
                    animator.metrics.start()
                    metrics = deepcopy(animator.metrics)
                    animator._open_diagnosis()
                    animator._diagnosis_input = scenario.disease
                    animator._submit_diagnosis()
                    self.assertTrue(animator._celebration.active(1000))
                    self.assertEqual(animator._current_state(), "relieved")
                    self.assertEqual(animator.metrics, metrics)
                    self.assertFalse(animator.won)
                    self.assertFalse(animator._consultation_finished.is_set())
                    self.assertTrue(animator._text_messages.empty())
                    self.assertLessEqual(animator._text_font.size(animator._celebration.thanks.message)[0], 236)
                    clock.return_value = 1000.5
                    self.assertIs(animator._centered_frame("relieved", 999), animator._patient_frames["relieved"][2])
                    animator.draw(999)
                    self.assertTrue(pygame.Rect(16, 96, 448, 194).contains(animator._celebration_layer.get_bounding_rect()))
                    clock.return_value = 1000 + CELEBRATION_SECONDS + .1
                    animator._state = "worried"
                    self.assertEqual(animator._current_state(), "worried")
                    self.assertFalse(animator.won)
                finally:
                    animator.close()

    def test_celebration_does_not_replay_or_change_transcript_on_duplicate_submission(self):
        with patch("src.realtime_conversation.time.monotonic", return_value=1000) as clock:
            self.animator._open_diagnosis()
            self.animator._diagnosis_input = "common cold"
            self.animator._submit_diagnosis()
            transcript = list(self.animator._transcript)
            clock.return_value = 1001
            self.animator._submit_diagnosis()
            self.animator._diagnosis_open = True
            self.animator._submit_diagnosis()
            self.assertEqual(self.animator._celebration.started_at, 1000)
            self.assertEqual(self.animator._transcript, transcript)

    def test_celebration_keeps_chat_care_and_give_up_available(self):
        with patch("src.realtime_conversation.time.monotonic", return_value=1000):
            self.animator._text_input = "Let's discuss your care."
            self.animator._set_text_focus(True)
            self.animator._open_diagnosis()
            self.animator._diagnosis_input = "common cold"
            self.animator._submit_diagnosis()
            self.assertTrue(self.animator._text_focused)
            self.assertEqual(self.animator._text_input, "Let's discuss your care.")
            self.animator._submit_text()
            self.assertEqual(self.animator._text_messages.get_nowait(), "Let's discuss your care.")
            self.animator._state = "talking"
            self.assertEqual(self.animator._status(self.animator._current_state())[0], "PATIENT SPEAKING")
            self.key(pygame.K_F4)
            self.assertIsNotNone(self.animator._menu)
            self.key(pygame.K_ESCAPE)
            self.key(pygame.K_F2)
            self.assertIn(
                ("Case", "Diagnosis already confirmed. Tell the patient your diagnosis when ready."),
                self.animator._transcript,
            )
            self.assertFalse(self.animator.won)
            self.click(self.animator._diagnose_button)
            self.assertTrue(self.stop.is_set())
            self.assertFalse(self.animator._running)
            self.assertFalse(self.animator._consultation_finished.is_set())

    def test_celebration_layer_leaves_other_ui_pixels_untouched(self):
        with patch("src.realtime_conversation.time.monotonic", return_value=1000.6):
            self.animator._celebration.start(1000)
            self.animator._screen.fill((1, 2, 3))
            self.animator._draw_celebration()
            for region in (pygame.Rect(0, 0, 480, 96), pygame.Rect(0, 290, 480, 190)):
                image = self.animator._screen.subsurface(region)
                self.assertEqual(pygame.image.tobytes(image, "RGB"), bytes((1, 2, 3)) * region.width * region.height)
            self.assertNotEqual(self.animator._screen.get_at((218, 126))[:3], (1, 2, 3))
            self.animator.close()
            self.assertFalse(self.animator._celebration.active(1000.6))

    def test_celebration_preserves_native_framebuffer_alpha_through_fade(self):
        # Cocoa's display format has an alpha mask even on non-SRCALPHA surfaces.
        masks = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        self.animator._screen = pygame.Surface((480, 480), depth=32, masks=masks)
        output = pygame.Surface((720, 720), depth=32, masks=masks)
        self.animator._celebration.start(1000)
        with patch("src.realtime_conversation.time.monotonic") as clock:
            for elapsed in (0, .02, .08, .15, .3, 2.9, 3.02, 3.2, 3.38, 3.41):
                with self.subTest(elapsed=elapsed):
                    clock.return_value = 1000 + elapsed
                    self.animator._screen.fill((100, 150, 200, 255))
                    self.animator._draw_celebration()
                    area = self.animator._screen.subsurface((0, 0, 100, 290))
                    self.assertEqual(
                        pygame.image.tobytes(area, "RGBA"),
                        bytes((100, 150, 200, 255)) * 100 * 290,
                    )
                    pygame.transform.scale(self.animator._screen, output.get_size(), output)
                    self.assertEqual(output.get_at((45, 270)), (100, 150, 200, 255))

    def test_cancelling_diagnosis_does_not_celebrate(self):
        self.animator._open_diagnosis()
        self.animator._diagnosis_input = "common cold"
        self.key(pygame.K_ESCAPE)
        self.animator._submit_diagnosis()
        self.assertIsNone(self.animator._celebration.started_at)
        self.assertFalse(self.animator._diagnosis_confirmed.is_set())

    def test_case_closed_waits_until_win_dance_and_confetti_finish(self):
        with patch("src.realtime_conversation.time.monotonic", return_value=1000) as clock:
            self.animator._scene_started_at = 990
            self.animator.show_win()
            with patch.object(self.animator, "_draw_win", wraps=self.animator._draw_win) as draw_win:
                for elapsed in (0, .6, 1.4, CELEBRATION_SECONDS - .01):
                    with self.subTest(elapsed=elapsed):
                        clock.return_value = 1000 + elapsed
                        self.animator.draw(elapsed)
                        self.assertTrue(self.animator.won)
                        self.assertTrue(self.animator._celebration.active(clock.return_value))
                        draw_win.assert_not_called()
                clock.return_value = 1000 + CELEBRATION_SECONDS + .01
                self.animator.draw(CELEBRATION_SECONDS + .01)
                draw_win.assert_called_once()
                self.assertFalse(self.animator._celebration.active(clock.return_value))
                self.assertLess(clock.return_value, self.animator._relieved_until)

    def test_you_win_does_not_flash_at_end_of_relief_animation(self):
        with patch("src.realtime_conversation.time.monotonic", return_value=1000) as clock:
            self.animator._scene_started_at = 990
            self.animator._open_diagnosis()
            self.animator._diagnosis_input = "common cold"
            self.animator._submit_diagnosis()
            self.animator.show_win()
            self.animator._consultation_finished.set()
            deadline = self.animator._relieved_until
            for offset in (-.4, -.2, -.01, 0, .01, .2):
                with self.subTest(offset=offset):
                    clock.return_value = deadline + offset
                    self.animator._screen.fill((100, 150, 200))
                    before = pygame.image.tobytes(self.animator._screen, "RGB")
                    self.animator._draw_scene_fade()
                    self.assertEqual(pygame.image.tobytes(self.animator._screen, "RGB"), before)
            self.assertTrue(self.animator._consultation_finished.is_set())

    def test_scene_entry_fade_still_expires_without_restarting_after_celebration(self):
        with patch("src.realtime_conversation.time.monotonic", return_value=1000) as clock:
            self.animator._scene_started_at = 1000
            self.animator._screen.fill((100, 150, 200))
            self.animator._draw_scene_fade()
            self.assertNotEqual(self.animator._screen.get_at((0, 0))[:3], (100, 150, 200))
            self.animator._celebration.start(1001)
            for now in (1001, 1001 + CELEBRATION_SECONDS - .01, 1001 + CELEBRATION_SECONDS + .01):
                clock.return_value = now
                self.animator._screen.fill((100, 150, 200))
                self.animator._draw_scene_fade()
                self.assertEqual(self.animator._screen.get_at((0, 0))[:3], (100, 150, 200))

    def test_celebration_renders_at_supported_window_sizes(self):
        self.addCleanup(pygame.display.set_mode, self.window.get_size())
        with patch("src.realtime_conversation.time.monotonic", return_value=1000.6):
            self.animator._scene_started_at = 0
            self.animator._celebration.start(1000)
            for size in (480, 720, 960):
                with self.subTest(size=size):
                    self.animator._window = pygame.display.set_mode((size, size))
                    self.animator.draw(.6)
                    expected = pygame.transform.scale(self.animator._screen, (size, size))
                    self.assertEqual(pygame.image.tobytes(self.animator._window, "RGB"), pygame.image.tobytes(expected, "RGB"))

    def test_give_up_returns_without_winning_or_healing_patient(self):
        self.click(self.animator._diagnose_button)
        self.assertTrue(self.stop.is_set())
        self.assertFalse(self.animator._running)
        self.assertFalse(self.animator.won)
        self.assertNotEqual(self.animator._current_state(), "relieved")
        self.assertFalse(self.animator._consultation_finished.is_set())

    def test_incorrect_diagnosis_allows_retry_without_revealing_answer(self):
        self.animator._diagnosis_open = True
        self.animator._diagnosis_input = "migraine"
        self.animator._submit_diagnosis()
        self.assertFalse(self.animator.won)
        self.assertTrue(self.animator._diagnosis_open)
        self.assertIsNone(self.animator._celebration.started_at)
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
                self.assertIsNone(self.animator._celebration.started_at)

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
        self.key(pygame.K_F2)
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
        self.assertTrue(self.animator._diagnosis_confirmed.is_set())
        self.assertFalse(self.animator.won)

    def test_diagnosis_mouse_submission_and_cancel(self):
        self.key(pygame.K_F2)
        self.assertTrue(self.animator._diagnosis_open)
        self.click(self.animator._diagnosis_cancel_button)
        self.assertFalse(self.animator._diagnosis_open)
        self.assertFalse(self.animator.won)
        self.key(pygame.K_F2)
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
        self.assertEqual(self.animator._test_result.results, "Temperature\n38 C")
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

    async def test_connected_session_routes_scaled_mouse_thermometer_and_ignores_late_voice(self):
        scenario = load_patient_scenario(ROOT / "data/prompts/01_common_cold_kid.json")
        animator = PatientAnimator(0, window=self.window)
        self.addCleanup(animator.close)
        stop = asyncio.Event()
        incoming = asyncio.Queue()
        websocket = AsyncMock()
        websocket.recv.return_value = json.dumps({"type": "session.updated"})
        credential = AsyncMock()
        credential.get_token.return_value = AccessToken("test-token", 9999999999)

        async def events():
            while not stop.is_set():
                yield json.dumps(await incoming.get())

        @asynccontextmanager
        async def connection(headers):
            yield websocket

        def mouse(kind, position):
            animator.handle_event(pygame.event.Event(
                kind, button=1, pos=tuple(round(n * 1.5) for n in position),
            ), stop)

        async def play():
            await until(lambda: animator.skill_engine is not None)
            animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F7), stop)
            self.assertIsInstance(animator._skill_browser, EquipmentDrawer)
            self.assertTrue(animator._local_skill_requests.empty())
            drawer = animator._skill_browser
            mouse(pygame.MOUSEBUTTONUP, (drawer.LIST_RECT.centerx, drawer.LIST_RECT.y + 34))
            await until(lambda: isinstance(animator._skill_confirmation, ThermometerActivity))
            activity = animator._skill_confirmation
            await incoming.put({"type": "input_audio_buffer.speech_started"})
            await incoming.put({"type": "input_audio_buffer.committed"})
            await until(incoming.empty)
            mouse(pygame.MOUSEBUTTONDOWN, (220, 300))
            mouse(pygame.MOUSEBUTTONUP, (220, 300))
            self.assertEqual(activity.state, "picked")
            self.assertEqual(animator.skill_engine.actions, [])
            mouse(pygame.MOUSEMOTION, activity.mouth_rect.center)
            mouse(pygame.MOUSEBUTTONUP, activity.mouth_rect.center)
            self.assertEqual(activity.state, "measuring")
            animator._advance_skill_activity(activity.measurement_started + activity.MEASURE_SECONDS)
            animator.draw()
            await until(lambda: animator.evidence_open)
            self.assertIn("38.0", animator._test_result.results)
            animator.draw()
            animator.close_test_result()
            await until(lambda: not animator._local_skill_busy)
            stop.set()

        websocket.__aiter__.side_effect = events
        with (
            patch("src.realtime_conversation.GameCredential", return_value=credential),
            patch("src.realtime_conversation._realtime_connection", connection),
            patch("src.realtime_conversation._microphone_stream", return_value=nullcontext(None)),
            patch("src.realtime_conversation.DeviceSink", return_value=RecordingSink()),
        ):
            session = asyncio.create_task(_conversation_session(
                str(scenario.system_prompts), scenario.disease, 0, scenario.tests, animator, stop,
            ))
            try:
                await asyncio.wait_for(play(), 5)
                await asyncio.wait_for(session, 2)
            finally:
                session.cancel()
                await asyncio.gather(session, return_exceptions=True)
        sent = [json.loads(call.args[0]) for call in websocket.send.call_args_list]
        self.assertFalse(any(e["type"] == "response.create" for e in sent))
        self.assertEqual(animator.skill_engine.actions[-1]["status"], "completed")
        self.assertFalse(animator.skills_modal)

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
        with patch("src.realtime_conversation._conversation_session", side_effect=session), patch(
            "src.realtime_conversation._show_consultation_review", new=AsyncMock()
        ):
            result = await _run_conversation(
                str(scenario.system_prompts), scenario.disease, 0, scenario.tests,
                window=self.window, performance_profile=scenario.performance_profile,
            )
        self.assertEqual(result, ConversationResult.SOLVED)

    async def test_completed_consultation_opens_review_after_session_cleanup(self):
        session_closed = asyncio.Event()

        async def session(
            prompt,
            disease,
            index,
            tests,
            animator,
            stop,
            *,
            sign_in=False,
            performance_profile=None,
        ):
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

    async def test_give_up_returns_unsolved_without_opening_review(self):
        async def session(prompt, disease, index, tests, animator, stop, **kwargs):
            await stop.wait()
            return False

        async def animation(animator, stop):
            animator._running = False
            stop.set()

        with patch("src.realtime_conversation._conversation_session", session), patch.object(
            PatientAnimator, "run", animation,
        ), patch("src.realtime_conversation._show_consultation_review", new=AsyncMock()) as review:
            result = await _run_conversation("patient", "common cold", 0, [], window=self.window)
        self.assertEqual(result, ConversationResult.RETURNED)
        review.assert_not_awaited()

    async def test_finished_visit_exports_skill_score_before_review_failure_or_quit(self):
        from src.diagnostic_skills import SkillEngine
        from src.patient_performance import PerformanceProfile

        received = []
        profile = PerformanceProfile()

        async def session(prompt, disease, index, tests, animator, stop, *, sign_in=False, performance_profile=None):
            self.assertIs(performance_profile, profile)
            animator.skill_engine = SkillEngine("COMMON_COLD_KID", [Test("temperature", "37.6 C")])
            proposal = animator.skill_engine.prepare("temperature", {}, ())
            animator.skill_engine.execute(proposal, ())
            animator.show_win()
            stop.set()
            return True

        async def review(animator, prompt, disease, tests):
            self.assertEqual(received, [animator.skill_engine.score])
            animator._review_error = "Service unavailable"
            animator.quit_requested = True

        with patch("src.realtime_conversation._conversation_session", session), patch(
            "src.realtime_conversation._show_consultation_review", review,
        ):
            result = await _run_conversation(
                "patient", "common cold", 0, [], window=self.window,
                on_skill_score=received.append, performance_profile=profile,
            )
        self.assertEqual(result, ConversationResult.SOLVED_QUIT)
        self.assertEqual(len(received), 1)

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

    async def test_realtime_typed_diagnosis_triggers_patient_win_without_confirmation(self):
        await self._assert_realtime_diagnosis_win(voice=False)

    async def test_realtime_voice_diagnosis_triggers_patient_win_without_confirmation(self):
        await self._assert_realtime_diagnosis_win(voice=True)

    async def _assert_realtime_diagnosis_win(self, *, voice):
        receiver_closed = asyncio.Event()
        incoming = asyncio.Queue()
        response_count = 0

        async def send(payload):
            nonlocal response_count
            event = json.loads(payload)
            if event["type"] == "response.create":
                response_count += 1
                response_id = f"response-{response_count}"
                await incoming.put({"type": "response.created", "response": {"id": response_id}})
                if response_count == 1:
                    for name, call_id in (
                        ("win", "untrusted"),
                        ("propose_you_win", "you-win"),
                    ):
                        await incoming.put({
                            "type": "response.function_call_arguments.done",
                            "response_id": response_id, "name": name, "call_id": call_id,
                        })
                    if voice:
                        await incoming.put({
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": "You have a common cold", "item_id": "voice",
                        })
                await incoming.put({
                    "type": "response.done",
                    "response": {
                        "id": response_id, "status": "completed",
                        "output": [
                            {"type": "function_call", "name": name, "call_id": call_id,
                             "arguments": json.dumps({"diagnosis": "common cold"})}
                            for name, call_id in (
                                ("win", "untrusted"),
                                ("propose_you_win", "you-win"),
                            )
                        ] if response_count == 1 else [],
                    },
                })

        async def events():
            try:
                while True:
                    yield json.dumps(await incoming.get())
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
        animator._text_input = "Hello" if voice else "You have a common cold"
        animator._submit_text()
        animator._ready = False
        stop = asyncio.Event()
        sink = RecordingSink()
        with (
            patch("src.realtime_conversation._realtime_authorization", authorize),
            patch("src.realtime_conversation._realtime_connection", connect),
            patch("src.realtime_conversation._microphone_stream", microphone),
            patch("src.realtime_conversation.DeviceSink", return_value=sink),
            patch.object(sink, "abort", wraps=sink.abort) as abort,
            patch("src.realtime_conversation.RELIEVED_DURATION_SECONDS", 0.01),
            patch.object(PatientAnimator, "wait_for_evidence_close", close_evidence),
            patch.object(PatientAnimator, "confirm_skill", new=AsyncMock()) as confirm,
        ):
            task = asyncio.create_task(_conversation_session("patient", "common cold", 0, [Test("Temperature", "38 C")], animator, stop))
            try:
                async def wait_for_win():
                    while not animator.won:
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_for_win(), 2)
                async def wait_for_skills():
                    while animator._skills_busy:
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_for_skills(), 2)
                configuration = json.loads(websocket.send.call_args_list[0].args[0])["session"]
                self.assertNotIn("win", [tool["name"] for tool in configuration["tools"]])
                self.assertEqual(len(configuration["tools"]), len(load_catalog()) + 2)
                self.assertIn("propose_you_win", [tool["name"] for tool in configuration["tools"]])
                self.assertTrue(animator.won)
                self.assertIn(("You", "You have a common cold"), animator._transcript)
                self.assertIsNotNone(animator._celebration.started_at)
                self.assertTrue(animator._diagnosis_confirmed.is_set())
                confirm.assert_not_awaited()
                self.assertIsNotNone(animator.metrics.started_at)
                self.assertTrue(await asyncio.wait_for(task, 2))
                self.assertTrue(stop.is_set())
                self.assertIsNotNone(animator.metrics.finished_at)
                self.assertTrue(stop.is_set())
                self.assertTrue(receiver_closed.is_set())
                abort.assert_called()
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