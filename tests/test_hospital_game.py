from __future__ import annotations

import json
import unittest
import tempfile
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import pygame

from src.hospital_game import (
    HospitalNavigator,
    PLAYER_DANCES,
    PLAYER_DANCE_FPS,
    PLAYER_JUMP_HEIGHT,
    PLAYER_JUMP_SECONDS,
    PLAYER_SPEED,
    PLAYER_START,
    HOSPITAL_ROOMS,
    load_patient_scenario,
    load_patient_scenarios,
    start_hospital_game,
)
from src.game_ui import ChoiceMenu
from src.game_progress import Progress, ProgressStore
from src.realtime_conversation import ConversationResult


class PatientDemographicsTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "data/prompts/01_common_cold_kid.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def load(self):
        with patch("src.hospital_game.json.load", return_value=self.data):
            return load_patient_scenario(self.path)

    def test_demographics_reach_conversation_instructions(self):
        self.data.update(age=8, gender="male", system_prompts="Answer as the patient.")
        prompt = self.load().conversation_parameters()["system_prompts"]
        self.assertIn("Age: 8 years.", prompt)
        self.assertIn("Gender: male.", prompt)
        self.assertIn("Answer as the patient.", prompt)

    def test_list_prompts_preserve_order(self):
        self.data.update(age=16, gender="female", system_prompts=["First.", "Second."])
        prompt = self.load().system_prompts
        self.assertEqual(prompt[1:], ("First.", "Second."))
        self.assertIn("Age: 16 years.", prompt[0])
        self.assertIn("Gender: female.", prompt[0])

    def test_legacy_patient_without_demographics_still_loads(self):
        self.data.pop("age", None)
        self.data.pop("gender", None)
        self.assertEqual(self.load().system_prompts, self.data["system_prompts"])

    def test_invalid_demographics_are_rejected(self):
        for field, values in (
            ("age", (True, -1, 121, 8.5, "8", None)),
            ("gender", ("", "  ", 1, None)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.data.update(age=8, gender="male")
                    self.data[field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        self.load()

    def test_unknown_fields_remain_rejected(self):
        self.data["age_years"] = 8
        with self.assertRaisesRegex(ValueError, "Patient prompt must contain"):
            self.load()


class MovementTests(unittest.TestCase):
    def navigator(self, position=(480, 480)):
        navigator = HospitalNavigator.__new__(HospitalNavigator)
        navigator._player_position = pygame.Vector2(position)
        navigator._scenarios = ()
        navigator._diagnosed = set()
        navigator._walking = False
        navigator._dance_index = None
        navigator._dance_time = 0.0
        navigator._crouching = False
        navigator._crawl_distance = 0.0
        navigator._jump_time = None
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

    def test_dance_cycles_in_place_and_restarts_each_animation(self):
        navigator = self.navigator()
        navigator._player_dance_frames = tuple(
            {"down": tuple(pygame.Surface((1, 1)) for _ in range(4))}
            for _ in PLAYER_DANCES
        )
        frames = navigator._player_dance_frames[0]["down"]
        navigator._walking = True
        navigator._walk_time = 2
        navigator._walk_distance = 40
        navigator._cycle_dance()
        self.assertTrue(navigator._dancing)
        self.assertFalse(navigator._walking)
        self.assertEqual(navigator._walk_time, 0)
        self.assertEqual(navigator._walk_distance, 0)
        self.assertIs(navigator._player_frame(0), frames[0])
        navigator.update(pygame.Vector2(), 1 / PLAYER_DANCE_FPS)
        self.assertEqual(navigator.player_position, PLAYER_START)
        self.assertIs(navigator._player_frame(0), frames[1])
        navigator.update(pygame.Vector2(), 3 / PLAYER_DANCE_FPS)
        self.assertIs(navigator._player_frame(0), frames[0])
        for index in range(1, len(PLAYER_DANCES)):
            navigator._cycle_dance()
            self.assertEqual(navigator._dance_index, index)
            self.assertEqual(navigator._dance_time, 0)
            self.assertIs(navigator._player_frame(0), navigator._player_dance_frames[index]["down"][0])
            navigator.update(pygame.Vector2(), 0.2)
        navigator._cycle_dance()
        self.assertFalse(navigator._dancing)
        navigator._cycle_dance()
        self.assertEqual(navigator._dance_time, 0)
        self.assertIs(navigator._player_frame(0), frames[0])

    def test_movement_cancels_dance_even_at_a_wall(self):
        for position in (PLAYER_START, (16, 470)):
            with self.subTest(position=position):
                navigator = self.navigator(position)
                navigator._cycle_dance()
                navigator.update(pygame.Vector2(-1, 0), 0.05)
                self.assertFalse(navigator._dancing)
                self.assertEqual(navigator._dance_time, 0)
                self.assertEqual(navigator._facing, "left")

    def test_crouching_cancels_dance_and_crawls_until_released(self):
        navigator = self.navigator()
        navigator._cycle_dance()
        navigator.update(pygame.Vector2(1, 0), 0.05, crouching=True)
        self.assertTrue(navigator._crouching)
        self.assertFalse(navigator._dancing)
        self.assertTrue(navigator._walking)
        self.assertAlmostEqual(navigator.player_position[0], PLAYER_START[0] + PLAYER_SPEED * 0.5 * 0.05)
        navigator._cycle_dance()
        self.assertFalse(navigator._dancing)
        before_release = navigator._player_position.copy()
        navigator.update(pygame.Vector2(1, 0), 0.05)
        self.assertFalse(navigator._crouching)
        self.assertTrue(navigator._walking)
        self.assertAlmostEqual(navigator._player_position.distance_to(before_release), PLAYER_SPEED * 0.05)

    def test_crawl_speed_is_half_walking_speed_in_every_direction(self):
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1)):
            with self.subTest(direction=direction):
                navigator = self.navigator()
                navigator.update(pygame.Vector2(direction), 0.05, crouching=True)
                self.assertAlmostEqual(
                    navigator._player_position.distance_to(PLAYER_START),
                    PLAYER_SPEED * 0.5 * 0.05,
                )
                self.assertTrue(navigator._crouching)
                self.assertTrue(navigator._walking)
                position = navigator.player_position
                navigator.update(pygame.Vector2(), 0.05, crouching=True)
                self.assertEqual(navigator.player_position, position)
                self.assertFalse(navigator._walking)
                self.assertTrue(navigator._crouching)

    def test_crawl_keeps_wall_collision_and_sliding(self):
        navigator = self.navigator((16, 470))
        navigator.update(pygame.Vector2(-1, 0), 0.05, crouching=True)
        self.assertEqual(navigator.player_position, (16, 470))
        self.assertFalse(navigator._walking)
        navigator.update(pygame.Vector2(-1, 1), 0.05, crouching=True)
        self.assertEqual(navigator.player_position[0], 16)
        self.assertGreater(navigator.player_position[1], 470)
        self.assertTrue(navigator._walking)

    def test_crawl_animation_advances_instead_of_sliding_one_pose(self):
        navigator = self.navigator()
        frames = tuple(pygame.Surface((1, 1)) for _ in range(4))
        crouch = pygame.Surface((1, 1))
        navigator._player_crawl_frames = {"right": frames}
        navigator._player_action_frames = {"right": (crouch,) * 4}
        navigator.update(pygame.Vector2(1, 0), 0.01, crouching=True)
        self.assertIs(navigator._player_frame(0), frames[0])
        navigator.update(pygame.Vector2(1, 0), 0.2, crouching=True)
        self.assertIs(navigator._player_frame(0), frames[1])
        navigator.update(pygame.Vector2(), 0.1, crouching=True)
        self.assertIs(navigator._player_frame(0), crouch)
        self.assertEqual(navigator._crawl_distance, 0)

    def test_crawl_phase_tracks_actual_distance_at_any_frame_rate(self):
        frames = tuple(pygame.Surface((1, 1)) for _ in range(4))
        for rate in (30, 60, 120):
            navigator = self.navigator()
            navigator._player_crawl_frames = {"right": frames}
            for _ in range(rate // 5):
                navigator.update(pygame.Vector2(1, 0), 1 / rate, crouching=True)
            self.assertAlmostEqual(navigator._crawl_distance, 22)
            self.assertIs(navigator._player_frame(0), frames[1])

    def test_crawl_phase_resets_at_walls_and_on_standing_or_jumping(self):
        navigator = self.navigator((16, 470))
        navigator.update(pygame.Vector2(-1, 1), 0.05, crouching=True)
        self.assertAlmostEqual(navigator._crawl_distance, navigator.player_position[1] - 470)
        navigator.update(pygame.Vector2(-1, 0), 0.05, crouching=True)
        self.assertEqual(navigator._crawl_distance, 0)
        navigator.update(pygame.Vector2(1, 0), 0.05, crouching=True)
        navigator.update(pygame.Vector2(1, 0), 0.05)
        self.assertEqual(navigator._crawl_distance, 0)
        navigator.update(pygame.Vector2(1, 0), 0.05, crouching=True)
        navigator._start_jump()
        navigator.update(pygame.Vector2(1, 0), 0.05, crouching=True)
        self.assertEqual(navigator._crawl_distance, 0)

    def test_jump_arc_and_landing_are_frame_rate_independent(self):
        for rate in (30, 60, 120):
            with self.subTest(rate=rate):
                navigator = self.navigator()
                navigator._cycle_dance()
                navigator._start_jump()
                self.assertFalse(navigator._dancing)
                self.assertEqual(navigator._jump_offset(), 0)
                half_frames = round(PLAYER_JUMP_SECONDS * rate / 2)
                for _ in range(half_frames):
                    navigator.update(pygame.Vector2(), 1 / rate)
                self.assertAlmostEqual(navigator._jump_offset(), PLAYER_JUMP_HEIGHT)
                for _ in range(half_frames):
                    navigator.update(pygame.Vector2(), 1 / rate)
                self.assertFalse(navigator._jumping)
                self.assertEqual(navigator._jump_offset(), 0)
                self.assertEqual(navigator.player_position, PLAYER_START)

    def test_jump_cannot_restart_or_dance_in_midair(self):
        navigator = self.navigator()
        navigator._start_jump()
        navigator.update(pygame.Vector2(), 0.2)
        navigator._start_jump()
        navigator._cycle_dance()
        self.assertEqual(navigator._jump_time, 0.2)
        self.assertFalse(navigator._dancing)

    def test_jump_from_crouch_and_crouch_on_landing(self):
        navigator = self.navigator()
        navigator.update(pygame.Vector2(), 0.01, crouching=True)
        navigator._start_jump()
        self.assertFalse(navigator._crouching)
        navigator.update(pygame.Vector2(), 0.1, crouching=True)
        self.assertFalse(navigator._crouching)
        navigator.update(pygame.Vector2(), PLAYER_JUMP_SECONDS, crouching=True)
        self.assertFalse(navigator._jumping)
        self.assertTrue(navigator._crouching)

    def test_jumping_uses_ground_collision_and_can_move(self):
        for position, direction in (
            ((16, 470), pygame.Vector2(-1, 0)),
            ((480, 950), pygame.Vector2(0, 1)),
            (PLAYER_START, pygame.Vector2(1, 0)),
        ):
            with self.subTest(position=position):
                ground = self.navigator(position)
                airborne = self.navigator(position)
                airborne._start_jump()
                ground.update(direction, 0.05)
                airborne.update(direction, 0.05)
                self.assertEqual(ground.player_position, airborne.player_position)
                self.assertTrue(airborne._jumping)

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

    def test_d_key_cycles_without_moving_and_ignores_repeat(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        key = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d)
        repeat = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d, repeat=True)
        held_keys = defaultdict(bool, {pygame.K_d: True})
        with patch("pygame.key.get_pressed", return_value=held_keys), patch(
            "pygame.event.get",
            side_effect=[[key], [repeat], [pygame.event.Event(pygame.QUIT)]],
        ):
            navigator.run()
        self.assertTrue(navigator._dancing)
        self.assertEqual(navigator._dance_index, 0)
        self.assertGreater(navigator._dance_time, 0)
        self.assertEqual(navigator.player_position, PLAYER_START)
        for expected in (1, 2, None):
            with patch("pygame.key.get_pressed", return_value=held_keys), patch(
                "pygame.event.get", side_effect=[[key], [pygame.event.Event(pygame.QUIT)]]
            ):
                navigator.run()
            self.assertEqual(navigator._dance_index, expected)
        self.assertFalse(navigator._dancing)
        self.assertEqual(navigator.player_position, PLAYER_START)

    def test_arrow_key_moves_and_cancels_dance(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        navigator._cycle_dance()
        held_keys = defaultdict(bool, {pygame.K_RIGHT: True})
        with patch("pygame.key.get_pressed", return_value=held_keys), patch(
            "pygame.event.get", side_effect=[[], [pygame.event.Event(pygame.QUIT)]]
        ):
            navigator.run()
        self.assertFalse(navigator._dancing)
        self.assertGreater(navigator.player_position[0], PLAYER_START[0])

    def test_focus_loss_freezes_dance_and_menu_ignores_d(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        navigator._cycle_dance()
        navigator.update(pygame.Vector2(), 0.1)
        with patch(
            "pygame.event.get",
            side_effect=[
                [pygame.event.Event(pygame.WINDOWFOCUSLOST)],
                [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d)],
                [pygame.event.Event(pygame.QUIT)],
            ],
        ):
            navigator.run()
        self.assertTrue(navigator._dancing)
        self.assertEqual(navigator._dance_time, 0.1)
        navigator._choose_menu("Resume")
        navigator.update(pygame.Vector2(), 0.1)
        self.assertEqual(navigator._dance_time, 0.2)

    def test_c_and_arrows_crawl_and_release_stands(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        held_keys = defaultdict(bool, {pygame.K_c: True, pygame.K_RIGHT: True})
        with patch("pygame.key.get_pressed", return_value=held_keys), patch(
            "pygame.event.get", side_effect=[[], [pygame.event.Event(pygame.QUIT)]]
        ):
            navigator.run()
        self.assertTrue(navigator._crouching)
        self.assertGreater(navigator.player_position[0], PLAYER_START[0])
        self.assertEqual(navigator._facing, "right")
        self.assertIn(navigator._player_frame(0), navigator._player_crawl_frames["right"])
        with patch("pygame.key.get_pressed", return_value=defaultdict(bool)), patch(
            "pygame.event.get", side_effect=[[], [pygame.event.Event(pygame.QUIT)]]
        ):
            navigator.run()
        self.assertFalse(navigator._crouching)

    def test_crawl_stays_grounded_without_walk_bob_or_dust(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        navigator._scene_started_at = -1000
        marker = pygame.Surface((12, 20), pygame.SRCALPHA)
        marker.fill((251, 0, 251))
        for direction in ((0, 1), (0, -1)):
            navigator.update(pygame.Vector2(direction), 0.05, crouching=True)
            navigator._walk_time = 0.07
            with patch.object(navigator, "_player_frame", return_value=marker), patch.object(navigator, "_draw_walk_dust") as dust:
                navigator.draw()
            position = navigator._player_position - navigator._camera()
            foot = (round(position.x), round(position.y) - 1)
            self.assertEqual(navigator._screen.get_at(foot), pygame.Color(251, 0, 251))
            dust.assert_not_called()

    def test_crawl_cannot_enter_locked_room(self):
        navigator = HospitalNavigator(self.scenarios, set(), (420, 700), window=self.window)
        navigator.update(pygame.Vector2(-1, 0), 0.05, crouching=True)
        self.assertEqual(navigator.player_position, (420, 700))
        self.assertFalse(navigator._walking)
        self.assertTrue(navigator._crouching)

    def test_space_starts_one_jump_and_repeat_does_not_start_another(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        key = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE)
        repeat = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE, repeat=True)
        with patch("pygame.key.get_pressed", return_value=defaultdict(bool)), patch(
            "pygame.event.get", side_effect=[[key], [pygame.event.Event(pygame.QUIT)]]
        ):
            navigator.run()
        self.assertTrue(navigator._jumping)
        navigator.update(pygame.Vector2(), PLAYER_JUMP_SECONDS)
        with patch("pygame.key.get_pressed", return_value=defaultdict(bool)), patch(
            "pygame.event.get", side_effect=[[repeat], [pygame.event.Event(pygame.QUIT)]]
        ):
            navigator.run()
        self.assertFalse(navigator._jumping)

    def test_pause_freezes_jump_and_clears_crouch(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        navigator._start_jump()
        navigator.update(pygame.Vector2(), 0.1)
        navigator._pause()
        with patch("pygame.event.get", side_effect=[
            [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d)],
            [pygame.event.Event(pygame.QUIT)],
        ]):
            navigator.run()
        self.assertEqual(navigator._jump_time, 0.1)
        navigator._choose_menu("Resume")
        navigator.update(pygame.Vector2(), PLAYER_JUMP_SECONDS, crouching=True)
        self.assertTrue(navigator._crouching)
        navigator._pause()
        self.assertFalse(navigator._crouching)
        with patch("pygame.key.get_pressed", return_value=defaultdict(bool)), patch(
            "pygame.event.get",
            side_effect=[
                [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SPACE)],
                [pygame.event.Event(pygame.QUIT)],
            ],
        ):
            navigator.run()
        self.assertIsNone(navigator._menu)
        self.assertFalse(navigator._jumping)

    def test_jump_renders_above_its_ground_shadow_without_walk_dust(self):
        navigator = HospitalNavigator([], set(), window=self.window)
        navigator._scene_started_at = -1000
        navigator._start_jump()
        navigator.update(pygame.Vector2(), PLAYER_JUMP_SECONDS / 2)
        navigator._walking = True
        marker = pygame.Surface((12, 20), pygame.SRCALPHA)
        marker.fill((251, 0, 251))
        with patch.object(navigator, "_player_frame", return_value=marker), patch.object(navigator, "_draw_walk_dust") as dust:
            navigator.draw()
        ground = navigator._player_position - navigator._camera()
        foot = (round(ground.x), round(ground.y - PLAYER_JUMP_HEIGHT) - 1)
        self.assertEqual(navigator._screen.get_at(foot), pygame.Color(251, 0, 251))
        self.assertEqual(navigator._screen.get_at((round(ground.x), round(ground.y))), pygame.Color(78, 103, 97))
        dust.assert_not_called()

    def test_jump_cannot_bypass_locked_room_or_start_consultation(self):
        navigator = HospitalNavigator(self.scenarios, set(), (420, 700), window=self.window)
        navigator._start_jump()
        navigator.update(pygame.Vector2(-1, 0), 0.05)
        self.assertEqual(navigator.player_position, (420, 700))
        navigator._player_position = pygame.Vector2(225, 360)
        with patch("pygame.key.get_pressed", return_value=defaultdict(bool)), patch(
            "pygame.event.get",
            side_effect=[
                [pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)],
                [pygame.event.Event(pygame.QUIT)],
            ],
        ), patch.object(navigator, "_fade_out") as fade:
            self.assertIsNone(navigator.run())
        fade.assert_not_called()

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
        visiting = iter(patients)
        self.assertTrue(patients[0].performance_profile.voice.enabled)
        self.assertIsNone(patients[1].performance_profile)

        def consult(**kwargs):
            patient = next(visiting)
            self.assertIs(kwargs.get("performance_profile"), patient.performance_profile)
            self.assertEqual(kwargs["tests"], patient.tests)
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