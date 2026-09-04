from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pygame

from src.animation_assets import load_atlas
from src.realtime_conversation import normalize_character_frames
from tools.prepare_atlas import prepare_atlas
from src.hospital_game import HospitalNavigator, PLAYER_DIRECTION_ROWS


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