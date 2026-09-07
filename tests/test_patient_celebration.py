from __future__ import annotations

import unittest

import pygame

from src.patient_celebration import CELEBRATION_SECONDS, CONFETTI_COUNT, THANK_YOUS, PatientCelebration
from src.realtime_conversation import PatientType


class PatientCelebrationTests(unittest.TestCase):
    def test_every_patient_has_a_personal_thank_you(self):
        self.assertEqual(set(THANK_YOUS), {patient.name for patient in PatientType})
        self.assertEqual(len({thanks.message for thanks in THANK_YOUS.values()}), len(PatientType))
        with self.assertRaises(ValueError):
            PatientCelebration("unconfigured patient")

    def test_celebration_is_finite_and_starts_only_once(self):
        celebration = PatientCelebration("COMMON_COLD_KID")
        self.assertFalse(celebration.active(100))
        self.assertTrue(celebration.start(100))
        self.assertEqual(len(celebration._particles), CONFETTI_COUNT)
        self.assertFalse(celebration.start(101))
        self.assertEqual(celebration.started_at, 100)
        self.assertFalse(celebration.active(99))
        self.assertTrue(celebration.active(100))
        self.assertTrue(celebration.active(100 + CELEBRATION_SECONDS - 0.01))
        self.assertFalse(celebration.active(100 + CELEBRATION_SECONDS + 0.01))
        self.assertFalse(celebration.start(110))

    def test_gesture_uses_existing_poses_and_settles(self):
        celebration = PatientCelebration("COMMON_COLD_KID")
        celebration.start(100)
        self.assertEqual(
            [celebration.frame_index(100 + age, 4) for age in (0, .25, .5, .75, .9, 1.2, 2)],
            [0, 1, 2, 3, 2, 1, 0],
        )
        self.assertEqual(celebration.pose(102), (0, 0))
        self.assertEqual(celebration.frame_index(110, 4), 0)
        self.assertEqual(celebration.opacity(110), 0)
        self.assertEqual(celebration.pose(110), (0, 0))
        for frames in (1, 2, 3):
            for age in (0, .25, .5, .75, 2):
                self.assertLess(celebration.frame_index(100 + age, frames), frames)
        with self.assertRaises(ValueError):
            celebration.frame_index(101, 0)

    def test_injured_and_older_patients_keep_their_feet_grounded(self):
        for patient in ("SPRAINED_ANKLE_ATHLETE", "ELDERLY_WITH_BACK_PAIN"):
            celebration = PatientCelebration(patient)
            celebration.start(100)
            for age in (.1, .3, .5, 1, 2):
                self.assertEqual(celebration.pose(100 + age), (0, 0))

    def test_confetti_is_time_based_and_stops_without_an_update_loop(self):
        colors = ((255, 80, 80), (230, 180, 70), (180, 230, 200), (40, 130, 110), (255, 255, 240))
        celebration = PatientCelebration("COMMON_COLD_KID")
        celebration.start(100)

        def render(now):
            layer = pygame.Surface((480, 480), pygame.SRCALPHA)
            celebration.draw_confetti(layer, now, colors)
            return pygame.image.tobytes(layer, "RGBA")

        peak = render(100.6)
        self.assertNotEqual(peak, bytes(len(peak)))
        for frame in range(90):
            render(100 + frame / 60)
        self.assertEqual(render(100.6), peak)
        self.assertNotEqual(render(101), peak)
        self.assertEqual(render(110), bytes(len(peak)))

    def test_dismissal_clears_particles_without_replaying(self):
        celebration = PatientCelebration("COMMON_COLD_KID")
        celebration.start(100)
        celebration.dismiss()
        self.assertFalse(celebration.active(100.5))
        self.assertEqual(celebration._particles, ())
        self.assertFalse(celebration.start(101))


if __name__ == "__main__":
    unittest.main()
