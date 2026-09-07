from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from src.skill_activity import (
    MEASURE_SECONDS, MOUTH_LANDMARKS, SUPPORTED_ACTIVITIES,
    INSTRUMENTS, BODY_LANDMARKS, InstrumentActivity, ThermometerActivity, draw_instrument, draw_thermometer,
)


ROOT = Path(__file__).resolve().parents[1]
PATIENTS = {
    "COMMON_COLD_KID": ("01_common_cold_kid", (.51, .44)),
    "STOMACHACHE_TEEN": ("02_stomachache_teen", (.36, .42)),
    "MIGRAINE_SUFFERER": ("03_migraine_patient", (.46, .44)),
    "ALLERGIES_PATIENT": ("04_allergy_patient", (.50, .43)),
    "SPRAINED_ANKLE_ATHLETE": ("05_injured_athlete", (.50, .405)),
    "ANXIOUS_ADULT": ("06_anxious_adult", (.47, .40)),
    "FEVERISH_PATIENT": ("07_feverish_patient", (.45, .51)),
    "RASH_PATIENT": ("08_rash_patient", (.48, .45)),
    "ELDERLY_WITH_BACK_PAIN": ("09_older_patient_back_pain", (.38, .40)),
    "SLEEP_DEPRIVED_WORKER": ("10_sleep_deprived_worker", (.55, .415)),
    "ECCENTRIC_NEIGHBOR": ("11_eccentric_neighbor", (.56, .445)),
}


class ThermometerActivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()
        cls.frames = {}
        for name, (filename, _) in PATIENTS.items():
            sheet = pygame.image.load(str(ROOT / "data/sprites/patients" / f"{filename}_atlas.png"))
            cls.frames[name] = sheet.subsurface((0, 256, 256, 256)).copy()

    def setUp(self):
        self.activity = ThermometerActivity(self.frames["COMMON_COLD_KID"], "COMMON_COLD_KID")
        self.clock = patch("src.skill_activity.time.monotonic", return_value=100.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def mouse(self, kind, position, button=1):
        event = pygame.event.Event(kind, button=button, pos=(999, 999))
        return self.activity.handle_event(event, position)

    def key(self, key):
        return self.activity.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key), (0, 0))

    def start_measurement(self):
        self.key(pygame.K_RETURN)
        self.key(pygame.K_TAB)
        self.key(pygame.K_RETURN)

    def test_public_contract_and_initial_state(self):
        self.assertEqual(SUPPORTED_ACTIVITIES, frozenset({
            "temperature", "oxygen_saturation", "lung_auscultation", "ear_examination",
        }))
        self.assertEqual(MEASURE_SECONDS, 2.5)
        self.assertEqual(self.activity.MEASURE_SECONDS, MEASURE_SECONDS)
        self.assertEqual(self.activity.state, "idle")
        self.assertIsNone(self.activity.measurement_started)
        self.assertFalse(self.activity.update(100000))
        with self.assertRaises(ValueError):
            ThermometerActivity(self.frames["COMMON_COLD_KID"], "unknown")
        with self.assertRaises(ValueError):
            ThermometerActivity(pygame.Surface((32, 32), pygame.SRCALPHA), "COMMON_COLD_KID")

    def test_new_instruments_have_patient_specific_targets_and_require_placement(self):
        self.assertEqual(set(BODY_LANDMARKS), set(PATIENTS))
        for patient, frame in self.frames.items():
            for skill_id in SUPPORTED_ACTIVITIES - {"temperature"}:
                with self.subTest(patient=patient, skill_id=skill_id):
                    self.activity = InstrumentActivity(frame, patient, skill_id)
                    target = self.activity.target_rect
                    self.assertTrue(self.activity.patient_rect.contains(target))
                    self.assertFalse(target.colliderect(self.activity.tray_rect))
                    self.key(pygame.K_RETURN)
                    self.mouse(pygame.MOUSEBUTTONUP, (10, 10))
                    self.assertFalse(self.activity.update(100000))
                    self.key(pygame.K_TAB)
                    self.key(pygame.K_RETURN)
                    self.assertFalse(self.activity.update(102.49))
                    self.assertTrue(self.activity.update(102.5))
                    self.assertEqual(self.activity.tip, target.center)
                    self.activity.draw(pygame.Surface((480, 480)))

    def test_all_instruments_cancel_and_reset_after_focus_loss(self):
        for skill_id in SUPPORTED_ACTIVITIES:
            self.activity = InstrumentActivity(self.frames["COMMON_COLD_KID"], "COMMON_COLD_KID", skill_id)
            self.start_measurement()
            self.activity.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), (0, 0))
            self.assertEqual(self.activity.state, "idle")
            self.assertFalse(self.activity.update(100000))
            self.start_measurement()
            self.assertIs(self.key(pygame.K_ESCAPE), False)
            self.assertFalse(self.activity.update(100000))

    def test_instrument_icons_are_distinct_and_have_no_numeric_readouts(self):
        images = set()
        for skill_id in INSTRUMENTS:
            canvas = pygame.Surface((120, 60))
            canvas.fill((247, 249, 244))
            draw_instrument(canvas, skill_id, (106, 28))
            images.add(pygame.image.tobytes(canvas, "RGB"))
        self.assertEqual(len(images), len(INSTRUMENTS))
        with self.assertRaises(ValueError):
            draw_instrument(canvas, "x_ray", (106, 28))
        with self.assertRaises(ValueError):
            InstrumentActivity(self.frames["COMMON_COLD_KID"], "COMMON_COLD_KID", "x_ray")

    def test_real_drag_places_only_on_release_and_snaps(self):
        self.assertIsNone(self.mouse(pygame.MOUSEBUTTONDOWN, self.activity.tray_rect.center))
        self.assertEqual(self.activity.state, "picked")
        target = (self.activity.mouth_rect.centerx + 4, self.activity.mouth_rect.centery + 2)
        self.assertIsNone(self.mouse(pygame.MOUSEMOTION, target))
        self.assertEqual(self.activity.tip, target)
        self.assertFalse(self.activity.update(100000))
        self.assertIsNone(self.mouse(pygame.MOUSEBUTTONUP, target))
        self.assertEqual(self.activity.state, "measuring")
        self.assertEqual(self.activity.tip, self.activity.mouth_rect.center)
        self.assertEqual(self.activity.measurement_started, 100.0)
        self.assertFalse(self.activity.update(100.0))

    def test_click_pick_then_click_place(self):
        tray = self.activity.tray_rect.center
        for event in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
            self.assertIsNone(self.mouse(event, tray))
        self.assertEqual(self.activity.state, "picked")
        self.assertNotIn("Not quite", self.activity.message)
        for event in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
            self.assertIsNone(self.mouse(event, self.activity.mouth_rect.center))
        self.assertEqual(self.activity.state, "measuring")
        self.assertFalse(self.activity.update(102.49))
        self.assertTrue(self.activity.update(102.5))

    def test_misses_never_complete_and_can_retry(self):
        self.mouse(pygame.MOUSEBUTTONDOWN, (230, 270))
        self.mouse(pygame.MOUSEBUTTONUP, self.activity.mouth_rect.center)
        self.assertEqual(self.activity.state, "idle")
        self.mouse(pygame.MOUSEBUTTONDOWN, self.activity.tray_rect.center)
        for miss in ((200, 250), (0, 0), (-20, -20), (479, 479)):
            self.mouse(pygame.MOUSEMOTION, miss)
            self.mouse(pygame.MOUSEBUTTONUP, miss)
            self.assertEqual(self.activity.state, "picked")
            self.assertIsNone(self.activity.measurement_started)
            self.assertIn("silver tip", self.activity.message)
            self.assertFalse(self.activity.update(100000))
        self.mouse(pygame.MOUSEBUTTONDOWN, self.activity.mouth_rect.center)
        self.assertEqual(self.activity.state, "measuring")

    def test_body_over_mouth_is_not_silver_tip_contact(self):
        self.key(pygame.K_RETURN)
        mouth_x, mouth_y = self.activity.mouth_rect.center
        self.mouse(pygame.MOUSEBUTTONDOWN, (mouth_x + 60, mouth_y))
        self.assertEqual(self.activity.state, "picked")
        self.assertFalse(self.activity.update(100000))

    def test_keyboard_requires_aim_then_timed_placement(self):
        self.assertIsNone(self.key(pygame.K_RETURN))
        self.assertEqual(self.activity.state, "picked")
        self.assertIsNone(self.key(pygame.K_RETURN))
        self.assertEqual(self.activity.state, "picked")
        self.assertFalse(self.activity.update(100000))
        original = self.activity.tip
        for key in (pygame.K_LEFT, pygame.K_UP, pygame.K_RIGHT, pygame.K_DOWN):
            self.key(key)
        self.assertEqual(self.activity.tip, original)
        self.key(pygame.K_TAB)
        self.assertEqual(self.activity.tip, self.activity.mouth_rect.center)
        self.assertEqual(self.activity.state, "picked")
        self.assertIsNone(self.key(pygame.K_KP_ENTER))
        self.assertEqual(self.activity.state, "measuring")
        self.assertFalse(self.activity.update(100))
        self.assertTrue(self.activity.update(102.5))

    def test_timer_does_not_restart_and_completion_is_sticky(self):
        self.start_measurement()
        self.assertFalse(self.activity.update(99))
        self.assertFalse(self.activity.update(101.25))
        self.assertEqual(self.activity._progress, .5)
        for key in (pygame.K_RETURN, pygame.K_TAB, pygame.K_LEFT):
            self.assertIsNone(self.key(key))
        self.mouse(pygame.MOUSEMOTION, (0, 0))
        self.mouse(pygame.MOUSEBUTTONDOWN, self.activity.tray_rect.center)
        self.assertEqual(self.activity.tip, self.activity.mouth_rect.center)
        self.assertEqual(self.activity.measurement_started, 100)
        self.assertFalse(self.activity.update(102.499))
        self.assertTrue(self.activity.update(102.5))
        for now in (102.5, 103, 100000, 0):
            self.assertTrue(self.activity.update(now))
            self.assertEqual(self.activity.state, "complete")

    def test_escape_right_click_and_cancel_reset_pending_timer(self):
        for cancel in ("escape", "right-down", "right-up", "cancel-down", "cancel-up"):
            with self.subTest(cancel=cancel):
                self.activity = ThermometerActivity(self.frames["COMMON_COLD_KID"], "COMMON_COLD_KID")
                self.start_measurement()
                if cancel == "escape":
                    result = self.key(pygame.K_ESCAPE)
                else:
                    kind = pygame.MOUSEBUTTONDOWN if cancel.endswith("down") else pygame.MOUSEBUTTONUP
                    position = self.activity.cancel_rect.center if cancel.startswith("cancel") else (0, 0)
                    result = self.mouse(kind, position, 3 if cancel.startswith("right") else 1)
                self.assertIs(result, False)
                self.assertFalse(self.activity.update(100000))
                self.assertIsNone(self.activity.measurement_started)

    def test_focus_loss_resets_picked_and_measuring(self):
        for measuring in (False, True):
            with self.subTest(measuring=measuring):
                self.activity = ThermometerActivity(self.frames["COMMON_COLD_KID"], "COMMON_COLD_KID")
                self.key(pygame.K_RETURN)
                if measuring:
                    self.key(pygame.K_TAB)
                    self.key(pygame.K_RETURN)
                    self.activity.update(101)
                self.assertIsNone(self.activity.handle_event(
                    pygame.event.Event(pygame.WINDOWFOCUSLOST), (0, 0),
                ))
                self.assertEqual(self.activity.state, "idle")
                self.assertEqual(self.activity.tip, self.activity._tray_tip)
                self.assertIsNone(self.activity.measurement_started)
                self.assertEqual(self.activity._progress, 0)
                self.assertFalse(self.activity.update(100000))
                self.mouse(pygame.MOUSEBUTTONUP, self.activity.mouth_rect.center)
                self.assertEqual(self.activity.state, "idle")
                self.start_measurement()
                self.assertTrue(self.activity.update(102.5))

    def test_every_named_anchor_uses_cropped_alpha_bounds_and_fits(self):
        # Inspect the enum without importing the voice/auth runtime into this widget test.
        tree = ast.parse((ROOT / "src/realtime_conversation.py").read_text())
        patient_enum = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PatientType")
        names = {node.targets[0].id for node in patient_enum.body if isinstance(node, ast.Assign)}
        self.assertEqual(names, set(MOUTH_LANDMARKS))
        self.assertEqual(len(names), 11)
        viewport = pygame.Rect(0, 0, 480, 480)
        for name, (_, anchor) in PATIENTS.items():
            with self.subTest(patient=name):
                activity = ThermometerActivity(self.frames[name], name)
                self.assertEqual(MOUTH_LANDMARKS[name], anchor)
                for rect in (activity.patient_rect, activity.tray_rect, activity.mouth_rect, activity.cancel_rect):
                    self.assertTrue(viewport.contains(rect))
                expected = (round(activity.patient_rect.x + activity.patient_rect.width * anchor[0]),
                            round(activity.patient_rect.y + activity.patient_rect.height * anchor[1]))
                self.assertEqual(activity.mouth_rect.center, expected)
                self.assertTrue(activity.patient_rect.contains(activity.mouth_rect))
                self.assertFalse(activity.tray_rect.colliderect(activity.mouth_rect))
                frame = self.frames[name]
                cropped = frame.subsurface(frame.get_bounding_rect(min_alpha=17))
                padded = pygame.Surface((400, 400), pygame.SRCALPHA)
                padded.blit(cropped, (73, 51))
                other = ThermometerActivity(padded, name)
                self.assertEqual(other.patient_rect, activity.patient_rect)
                self.assertEqual(other.mouth_rect, activity.mouth_rect)
                canvas = pygame.Surface((480, 480))
                activity.draw(canvas)
                self.assertNotEqual(canvas.get_at(activity.mouth_rect.center), canvas.get_at((0, 0)))

    def test_render_all_states_without_results_or_advancing_time(self):
        canvas = pygame.Surface((480, 480))
        images = []
        for state in ("idle", "picked", "measuring", "complete"):
            if state == "picked":
                self.key(pygame.K_RETURN)
            elif state == "measuring":
                self.key(pygame.K_TAB)
                self.key(pygame.K_RETURN)
                self.activity.update(101.25)
            elif state == "complete":
                self.activity.update(102.5)
            with patch.object(self.activity, "_text", wraps=self.activity._text) as text:
                self.activity.draw(canvas)
            rendered = " ".join(call.args[1] for call in text.call_args_list)
            self.assertNotRegex(rendered, r"\d|°|fever|normal|abnormal|score|points")
            self.assertEqual(self.activity.state, state)
            images.append(pygame.image.tobytes(canvas, "RGB"))
            self.assertGreater(len(set(images[-1])), 20)
        self.assertEqual(len(set(images)), 4)

    def test_thermometer_draws_left_from_silver_tip_with_blank_display(self):
        for scale in (.5, 1.0, 2.0):
            with self.subTest(scale=scale):
                layer = pygame.Surface((300, 100), pygame.SRCALPHA)
                tip = (250.0, 50.0)
                draw_thermometer(layer, tip, scale)
                bounds = layer.get_bounding_rect()
                self.assertEqual(bounds.left, 250 - round(100 * scale))
                self.assertLessEqual(bounds.right, 250 + max(1, round(scale)))
                self.assertGreater(layer.get_at((250, 50)).a, 0)
                display = layer.subsurface((
                    250 - round(80 * scale), 50 - round(3 * scale),
                    round(25 * scale), round(6 * scale),
                ))
                self.assertEqual(len(set(pygame.image.tobytes(display, "RGB"))), 3)
        for scale in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                draw_thermometer(layer, (250, 50), scale)


if __name__ == "__main__":
    unittest.main()
