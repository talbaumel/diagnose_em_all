from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import pygame

from src.hospital_game import HospitalNavigator, PLAYER_SPEED, PLAYER_START, HOSPITAL_ROOMS, load_patient_scenarios, start_hospital_game
from src.game_ui import ChoiceMenu
from src.game_progress import Progress, ProgressStore
from src.realtime_conversation import ConversationResult


class MovementTests(unittest.TestCase):
    def navigator(self, position=(480, 480)):
        navigator = HospitalNavigator.__new__(HospitalNavigator)
        navigator._player_position = pygame.Vector2(position)
        navigator._scenarios = ()
        navigator._diagnosed = set()
        navigator._walking = False
        navigator._facing = "down"
        navigator._animation_time = 0.0
        navigator._walk_time = 0.0
        navigator._walk_distance = 0.0
        navigator._camera_position = navigator._camera_target()
        navigator._greeting_patient = None
        navigator._greeting_until = 0.0
        return navigator

    def test_blocked_movement_does_not_walk(self):
        navigator = self.navigator((16, 470))
        navigator.move(pygame.Vector2(-1, 0), 0.05)
        self.assertEqual(navigator.player_position, (16, 470))
        self.assertFalse(navigator._walking)
        self.assertEqual(navigator._facing, "left")

    def test_slides_along_wall(self):
        navigator = self.navigator((16, 470))
        navigator.move(pygame.Vector2(-1, 1), 0.05)
        self.assertEqual(navigator.player_position[0], 16)
        self.assertGreater(navigator.player_position[1], 470)
        self.assertTrue(navigator._walking)

    def test_diagonal_speed_is_normalized(self):
        navigator = self.navigator()
        navigator.move(pygame.Vector2(1, 1), 0.05)
        self.assertAlmostEqual(
            navigator._player_position.distance_to((480, 480)), PLAYER_SPEED * 0.05
        )

    def test_no_input_stops_walking(self):
        navigator = self.navigator()
        navigator._walking = True
        navigator.move(pygame.Vector2(), 0.05)
        self.assertFalse(navigator._walking)

    def test_walk_clock_resets_when_blocked(self):
        navigator = self.navigator((16, 470))
        navigator._walk_time = 2.0
        navigator._walk_distance = 50.0
        navigator.update(pygame.Vector2(-1, 0), 0.05)
        self.assertEqual(navigator._walk_time, 0)
        self.assertEqual(navigator._walk_distance, 0)

    def test_side_walk_phase_follows_distance_at_any_frame_rate(self):
        frames = tuple(pygame.Surface((1, 1)) for frame_index in range(6))
        for rate in (30, 60, 120):
            navigator = self.navigator()
            navigator._player_idle_frames = {}
            navigator._player_frames = {"right": frames}
            for frame_index in range(rate // 5):
                navigator.update(pygame.Vector2(1, 0), 1 / rate)
            self.assertAlmostEqual(navigator._walk_distance, 44.0)
            self.assertIs(navigator._player_frame(navigator._walk_time), frames[3])

    def test_walk_distance_uses_actual_wall_slide(self):
        navigator = self.navigator((16, 470))
        navigator.update(pygame.Vector2(-2, 1), 0.05)
        self.assertEqual(navigator._facing, "left")
        self.assertAlmostEqual(navigator._walk_distance, navigator._player_position.y - 470)
        self.assertLess(navigator._walk_distance, PLAYER_SPEED * 0.05)

    def test_camera_easing_is_frame_rate_independent(self):
        positions = []
        for rate in (30, 60, 120):
            navigator = self.navigator()
            navigator._player_position = pygame.Vector2(540, 480)
            for frame in range(rate):
                navigator.update(pygame.Vector2(), 1 / rate)
            positions.append(navigator._camera())
        self.assertLess(positions[0].distance_to(positions[2]), 0.001)
        self.assertGreater(positions[0].x, 299)

    def test_camera_target_is_clamped(self):
        for position, expected in (((16, 470), (0, 230)), ((940, 470), (480, 230))):
            navigator = self.navigator(position)
            self.assertEqual(navigator._camera_target(), pygame.Vector2(expected))

    def test_reset_requires_confirmation(self):
        pygame.font.init()
        navigator = self.navigator()
        navigator.restart_requested = False
        navigator._choose_menu("New Game")
        self.assertFalse(navigator.restart_requested)
        self.assertEqual(navigator._menu_kind, "reset")
        self.assertTrue(navigator._choose_menu("Reset Progress"))
        self.assertTrue(navigator.restart_requested)

    def test_menu_scales_mouse_coordinates(self):
        pygame.font.init()
        menu = ChoiceMenu("Paused", ("Resume", "New Game"))
        event = pygame.event.Event(pygame.MOUSEBUTTONUP, pos=(240, 300), button=1)
        self.assertEqual(menu.handle_event(event, (480, 480)), "New Game")
        event = pygame.event.Event(pygame.MOUSEBUTTONUP, pos=(480, 600), button=1)
        self.assertEqual(menu.handle_event(event, (960, 960)), "New Game")


class HospitalFlowTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.scenarios = load_patient_scenarios(sorted((root / "data/prompts").glob("*.json")))
        pygame.init()
        self.window = pygame.display.set_mode((720, 720))
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.save_path = Path(directory.name) / "progress.json"
        self.addCleanup(pygame.quit)

    def test_locked_restore_falls_back_and_prerequisite_unlocks(self):
        diagnosed = set()
        navigator = HospitalNavigator(self.scenarios, diagnosed, (150, 750), window=self.window)
        self.assertEqual(navigator.player_position, PLAYER_START)
        self.assertFalse(navigator._room_unlocked(HOSPITAL_ROOMS[2]))
        diagnosed.add(self.scenarios[0].patient_type)
        self.assertTrue(navigator._room_unlocked(HOSPITAL_ROOMS[2]))
        self.assertTrue(navigator._can_stand(pygame.Vector2(150, 750)))

    def test_subset_without_prerequisite_unlocks_room(self):
        navigator = HospitalNavigator(self.scenarios[-1:], set(), window=self.window)
        self.assertTrue(navigator._room_unlocked(HOSPITAL_ROOMS[3]))

    def test_pause_does_not_advance_simulation(self):
        navigator = HospitalNavigator(self.scenarios[:1], set(), window=self.window)
        navigator._pause()
        with patch("pygame.event.get", side_effect=[[], [pygame.event.Event(pygame.QUIT)]]):
            navigator.run()
        self.assertEqual(navigator._animation_time, 0)
        self.assertEqual(navigator.player_position, PLAYER_START)

    def test_side_walk_keeps_supporting_foot_on_ground(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        navigator._scene_started_at = -1000
        navigator._walking = True
        marker = pygame.Surface((12, 20), pygame.SRCALPHA)
        marker.fill((251, 0, 251))
        expected_bottom = round((navigator._player_position - navigator._camera()).y) - 1
        for direction in ("left", "right"):
            navigator._facing = direction
            for walk_time in (0.0, 0.07, 0.14):
                navigator._walk_time = walk_time
                with patch.object(navigator, "_player_frame", return_value=marker):
                    navigator.draw()
                foot = navigator._screen.get_at((240, expected_bottom))
                self.assertEqual(foot, pygame.Color(251, 0, 251))

    def test_full_roster_completion_and_relaunch(self):
        selected = iter(self.scenarios)
        completions = []

        def visit(navigator):
            scenario = next(selected, None)
            if scenario is None:
                completions.append(navigator._menu_kind)
                navigator._choose_menu("Continue Exploring")
            return scenario

        with patch.object(HospitalNavigator, "run", visit), patch("src.hospital_game.start_consultation", return_value=ConversationResult.SOLVED):
            start_hospital_game(self.scenarios, save_path=self.save_path)
        self.assertEqual(completions, ["complete"])
        store = ProgressStore((scenario.patient_type.value for scenario in self.scenarios), self.save_path)
        progress = store.load()
        self.assertEqual(len(progress.diagnosed), len(self.scenarios))
        self.assertTrue(progress.completion_announced)
        pygame.init()
        self.window = pygame.display.set_mode((720, 720))
        navigator = HospitalNavigator(self.scenarios, {scenario.patient_type for scenario in self.scenarios}, window=self.window, completion_announced=progress.completion_announced)
        self.assertIsNone(navigator._menu)

    def test_return_does_not_award_progress_and_quit_stays_quit(self):
        for result, visits in ((ConversationResult.RETURNED, 2), (ConversationResult.QUIT, 1)):
            with patch.object(HospitalNavigator, "run", side_effect=[self.scenarios[0], None]) as run, patch("src.hospital_game.start_consultation", return_value=result):
                start_hospital_game(self.scenarios[:1], save_path=self.save_path)
            self.assertEqual(run.call_count, visits)
            self.assertFalse(ProgressStore([self.scenarios[0].patient_type.value], self.save_path).load().diagnosed)

    def test_quitting_completed_review_saves_case_before_exiting(self):
        def consult(**kwargs):
            kwargs["on_skill_score"](-4)
            return ConversationResult.SOLVED_QUIT

        with patch.object(HospitalNavigator, "run", return_value=self.scenarios[0]) as run, patch("src.hospital_game.start_consultation", side_effect=consult):
            start_hospital_game(self.scenarios[:1], save_path=self.save_path)
        self.assertEqual(run.call_count, 1)
        progress = ProgressStore([self.scenarios[0].patient_type.value], self.save_path).load()
        self.assertEqual(progress.diagnosed, {self.scenarios[0].patient_type.value})
        self.assertEqual(progress.skill_scores, {self.scenarios[0].patient_type.value: -4})

    def test_unsolved_outcomes_never_save_callback_score(self):
        patient = self.scenarios[0]
        for result in (ConversationResult.RETURNED, ConversationResult.QUIT):
            with self.subTest(result=result):
                def consult(**kwargs):
                    kwargs["on_skill_score"](8)
                    return result

                with patch.object(HospitalNavigator, "run", side_effect=[patient, None]), patch(
                    "src.hospital_game.start_consultation", side_effect=consult
                ):
                    start_hospital_game([patient], save_path=self.save_path)
                progress = ProgressStore([patient.patient_type.value], self.save_path).load()
                self.assertEqual(progress.diagnosed, set())
                self.assertEqual(progress.skill_scores, {})

    def test_scores_survive_navigator_recreation_and_later_cases(self):
        patients = self.scenarios[:2]
        scores = iter((7, -2))

        def consult(**kwargs):
            kwargs["on_skill_score"](next(scores))
            return ConversationResult.SOLVED

        with patch.object(HospitalNavigator, "run", side_effect=[*patients, None]), patch(
            "src.hospital_game.start_consultation", side_effect=consult
        ):
            start_hospital_game(patients, save_path=self.save_path)
        store = ProgressStore((patient.patient_type.value for patient in patients), self.save_path)
        self.assertEqual(store.load().skill_scores,
                         {patients[0].patient_type.value: 7, patients[1].patient_type.value: -2})
        with patch.object(HospitalNavigator, "run", return_value=None):
            start_hospital_game(patients, save_path=self.save_path)
        self.assertEqual(store.load().skill_scores,
                         {patients[0].patient_type.value: 7, patients[1].patient_type.value: -2})

    def test_callback_is_optional_for_completed_cases_and_does_not_leak_between_visits(self):
        patients = self.scenarios[:2]
        calls = 0

        def consult(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                kwargs["on_skill_score"](0)
            return ConversationResult.SOLVED

        with patch.object(HospitalNavigator, "run", side_effect=[*patients, None]), patch(
            "src.hospital_game.start_consultation", side_effect=consult
        ):
            start_hospital_game(patients, save_path=self.save_path)
        progress = ProgressStore((patient.patient_type.value for patient in patients), self.save_path).load()
        self.assertEqual(progress.diagnosed, {patient.patient_type.value for patient in patients})
        self.assertEqual(progress.skill_scores, {patients[0].patient_type.value: 0})

    def test_reset_removes_completed_scores(self):
        patient = self.scenarios[0]
        store = ProgressStore([patient.patient_type.value], self.save_path)
        store.save(Progress({patient.patient_type.value}, skill_scores={patient.patient_type.value: 6}))
        visits = 0

        def visit(navigator):
            nonlocal visits
            visits += 1
            if visits == 1:
                navigator.restart_requested = True
            return None

        with patch.object(HospitalNavigator, "run", visit):
            start_hospital_game([patient], save_path=self.save_path)
        self.assertEqual(visits, 2)
        self.assertEqual(store.load(), Progress())

    def test_invalid_callback_score_does_not_prevent_case_completion(self):
        patient = self.scenarios[0]

        def consult(**kwargs):
            kwargs["on_skill_score"](True)
            return ConversationResult.SOLVED_QUIT

        with patch.object(HospitalNavigator, "run", return_value=patient), patch(
            "src.hospital_game.start_consultation", side_effect=consult
        ):
            start_hospital_game([patient], save_path=self.save_path)
        progress = ProgressStore([patient.patient_type.value], self.save_path).load()
        self.assertEqual(progress.diagnosed, {patient.patient_type.value})
        self.assertEqual(progress.skill_scores, {})

if __name__ == "__main__":
    unittest.main()