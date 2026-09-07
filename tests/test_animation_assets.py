from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pygame

from src.animation_assets import load_atlas
from src.realtime_conversation import normalize_character_frames
from tools.prepare_atlas import prepare_atlas
from src.hospital_game import (
    HospitalNavigator,
    OVERWORLD_CRAWL_HEIGHT,
    PLAYER_CRAWL_CYCLE_DISTANCE,
    PLAYER_CRAWL_SPEED,
    PLAYER_DANCES,
    PLAYER_DIRECTION_ROWS,
    PLAYER_JUMP_SECONDS,
)


class AtlasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        pygame.display.set_mode((1, 1))

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "atlas.png"

    def test_wrong_dimensions_rejected(self):
        pygame.image.save(pygame.Surface((100, 100)), str(self.path))
        with self.assertRaises(ValueError):
            load_atlas(self.path, 4)

    def test_empty_and_opaque_sheets_rejected(self):
        for color in ((0, 0, 0, 0), (255, 255, 255, 255)):
            sheet = pygame.Surface((1024, 1024), pygame.SRCALPHA)
            sheet.fill(color)
            pygame.image.save(sheet, str(self.path))
            with self.assertRaises(ValueError):
                load_atlas(self.path, 4)

    def test_transparent_frames_keep_detached_details(self):
        sheet = pygame.Surface((1024, 1024), pygame.SRCALPHA)
        for row in range(4):
            for column in range(4):
                pygame.draw.rect(sheet, (20, 40, 60), (column * 256 + 100, row * 256 + 60, 50, 160))
                pygame.draw.rect(sheet, (240, 240, 240), (column * 256 + 160, row * 256 + 80, 10, 10))
        pygame.image.save(sheet, str(self.path))
        frames = load_atlas(self.path, 4)
        self.assertEqual(len(frames), 4)
        self.assertEqual(pygame.mask.from_surface(frames[0][0]).count(), 8100)
        normalized = normalize_character_frames(frames[0], 92)
        self.assertEqual({frame.get_height() for frame in normalized}, {92})

    def test_preparation_removes_background_but_keeps_enclosed_white(self):
        source = pygame.Surface((1024, 1024))
        source.fill("white")
        for row in range(4):
            for column in range(4):
                center = (column * 256 + 128, row * 256 + 128)
                pygame.draw.circle(source, (25, 40, 45), center, 70)
                pygame.draw.circle(source, "white", center, 30)
        input_path = self.path.with_name("source.png")
        pygame.image.save(source, str(input_path))
        prepare_atlas(input_path, self.path, 4)
        frame = load_atlas(self.path, 4)[0][0]
        self.assertEqual(frame.get_at((0, 0)).a, 0)
        center = frame.get_rect().center
        self.assertEqual(frame.get_at(center), pygame.Color("white"))

    def test_preparation_fits_wide_crawl_poses_without_clipping(self):
        source = pygame.Surface((1024, 1024))
        source.fill("white")
        for row in range(4):
            for column in range(4):
                pygame.draw.rect(source, (25, 40, 45), (column * 256 + 28, row * 256 + 100, 200, 110))
        input_path = self.path.with_name("source.png")
        pygame.image.save(source, str(input_path))
        prepare_atlas(input_path, self.path, 4)
        for row in load_atlas(self.path, 4):
            for frame in row:
                self.assertLessEqual(frame.get_width(), 232)
                self.assertLess(frame.get_height(), 208)
                self.assertEqual(pygame.mask.from_surface(frame).count(), frame.get_width() * frame.get_height())

    def test_real_atlases_have_shared_canvas_and_distinct_motion(self):
        navigator = HospitalNavigator([], set(), window=pygame.display.get_surface())
        self.addCleanup(navigator.close)
        self.assertIsNotNone(navigator._player_idle_frames)
        dimensions = set()
        for direction in PLAYER_DIRECTION_ROWS:
            walking = navigator._player_frames[direction]
            idle = navigator._player_idle_frames[direction]
            self.assertEqual(len(walking), 6)
            self.assertEqual(len(idle), 4)
            dimensions.update(frame.get_size() for frame in (*walking, *idle))
            self.assertGreater(len({pygame.image.tostring(frame, "RGBA") for frame in walking}), 1)
        self.assertEqual(len(dimensions), 1)
        self.assertEqual(next(iter(dimensions))[1], 92)

    def test_side_walk_has_passing_poses_between_contacts(self):
        navigator = HospitalNavigator([], set(), window=pygame.display.get_surface())
        self.addCleanup(navigator.close)
        for direction in ("left", "right"):
            with self.subTest(direction=direction):
                frames = navigator._player_frames[direction]
                foot_spans = [
                    frame.subsurface((0, frame.get_height() - 18, frame.get_width(), 18)).get_bounding_rect().width
                    for frame in frames
                ]
                for contact, passing in ((0, 2), (3, 5)):
                    self.assertLess(foot_spans[passing], foot_spans[contact] * 0.8)

    def test_dance_atlases_have_distinct_padded_frames_for_every_direction(self):
        navigator = HospitalNavigator([], set(), window=pygame.display.get_surface())
        self.addCleanup(navigator.close)
        dimensions = set()
        signatures = set()
        self.assertEqual(len(navigator._player_dance_frames), len(PLAYER_DANCES))
        for index, (name, _) in enumerate(PLAYER_DANCES):
            navigator._cycle_dance()
            dimensions.clear()
            for direction in PLAYER_DIRECTION_ROWS:
                with self.subTest(dance=name, direction=direction):
                    frames = navigator._player_dance_frames[index][direction]
                    self.assertEqual(len(frames), 4)
                    self.assertEqual(len({pygame.image.tostring(frame, "RGBA") for frame in frames}), 4)
                    dimensions.update(frame.get_size() for frame in frames)
                    signatures.add(tuple(pygame.image.tostring(frame, "RGBA") for frame in frames))
                    navigator._facing = direction
                    self.assertIs(navigator._player_frame(0), frames[0])
                    for frame in frames:
                        self.assertGreater(pygame.mask.from_surface(frame).count(), 100)
                        self.assertEqual(frame.get_at((0, 0)).a, 0)
            self.assertEqual(len(dimensions), 1)
            self.assertEqual(next(iter(dimensions))[1], 92)
        self.assertEqual(len(signatures), len(PLAYER_DANCES) * len(PLAYER_DIRECTION_ROWS))

    def test_action_art_keeps_crouch_lower_and_selects_jump_poses(self):
        navigator = HospitalNavigator([], set(), window=pygame.display.get_surface())
        self.addCleanup(navigator.close)
        for direction in PLAYER_DIRECTION_ROWS:
            with self.subTest(direction=direction):
                navigator._facing = direction
                standing, crouch, takeoff, airborne = navigator._player_action_frames[direction]
                self.assertLess(crouch.get_bounding_rect().height, standing.get_bounding_rect().height * 0.85)
                self.assertEqual(crouch.get_bounding_rect().bottom, standing.get_bounding_rect().bottom)
                navigator.update(pygame.Vector2(), 0.01, crouching=True)
                self.assertIs(navigator._player_frame(0), crouch)
                navigator._start_jump()
                self.assertIs(navigator._player_frame(0), takeoff)
                navigator.update(pygame.Vector2(), PLAYER_JUMP_SECONDS / 2)
                self.assertIs(navigator._player_frame(0), airborne)
                navigator.update(pygame.Vector2(), PLAYER_JUMP_SECONDS / 2)
                self.assertFalse(navigator._jumping)

    def test_real_crawl_frames_change_limbs_and_animate_in_all_directions(self):
        navigator = HospitalNavigator([], set(), window=pygame.display.get_surface())
        self.addCleanup(navigator.close)
        for direction, vector in (
            ("down", (0, 1)), ("left", (-1, 0)),
            ("right", (1, 0)), ("up", (0, -1)),
        ):
            with self.subTest(direction=direction):
                frames = navigator._player_crawl_frames[direction]
                self.assertEqual(len(frames), 4)
                self.assertEqual({frame.get_height() for frame in frames}, {OVERWORLD_CRAWL_HEIGHT})
                # Check the lower half, not just a blinking face or moving hair.
                limb_frames = {
                    pygame.image.tostring(frame.subsurface((0, frame.get_height() // 2, frame.get_width(), frame.get_height() // 2)), "RGBA")
                    for frame in frames
                }
                self.assertEqual(len(limb_frames), 4)
                if direction in ("left", "right"):
                    gathered_width = frames[1].get_bounding_rect().width
                    for extended in (frames[0], frames[2]):
                        self.assertGreater(extended.get_bounding_rect().width, gathered_width * 1.25)
                navigator._player_position = pygame.Vector2(480, 480)
                navigator.update(pygame.Vector2(), 0, crouching=True)
                navigator._facing = direction
                self.assertIs(navigator._player_frame(0), navigator._player_action_frames[direction][1])
                rendered = set()
                step_time = PLAYER_CRAWL_CYCLE_DISTANCE / PLAYER_CRAWL_SPEED / len(frames)
                for _ in frames:
                    navigator.update(pygame.Vector2(vector), step_time, crouching=True)
                    self.assertIn(navigator._player_frame(0), frames)
                    rendered.add(pygame.image.tostring(navigator._player_frame(0), "RGBA"))
                self.assertEqual(len(rendered), 4)

    def test_missing_new_atlas_preserves_fallback(self):
        navigator = HospitalNavigator.__new__(HospitalNavigator)
        fallback = {"down": (pygame.Surface((1, 1)),)}
        navigator._player_frames = fallback
        navigator._player_idle_frames = None
        with patch("src.hospital_game.load_atlas", side_effect=ValueError("bad atlas")), self.assertWarns(RuntimeWarning):
            navigator._load_player_atlases()
        self.assertIs(navigator._player_frames, fallback)


if __name__ == "__main__":
    unittest.main()