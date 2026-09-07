from __future__ import annotations

import unittest
from unittest.mock import patch

import pygame

from src.hospital_game import start_hospital_game
from src.splash_screen import BACKGROUND, SPLASH_IMAGE, SPLASH_SECONDS, SplashScreen, show_splash


class SplashScreenTests(unittest.TestCase):
    def setUp(self):
        pygame.init()
        self.window = pygame.display.set_mode((720, 720))
        self.addCleanup(pygame.quit)

    def test_assets_render_and_fit_square_portrait_and_wide_windows(self):
        splash = SplashScreen()
        artwork = pygame.image.load(str(SPLASH_IMAGE)).convert()
        self.assertEqual(artwork.get_size(), (1024, 1024))
        for size in ((720, 720), (360, 640), (1280, 720)):
            with self.subTest(size=size):
                window = pygame.Surface(size)
                splash.draw(window, 1.5)
                pixels = pygame.surfarray.array3d(window)
                self.assertGreater(pixels.std(), 20)
                viewport = artwork.get_rect().fit(window.get_rect())
                self.assertTrue(window.get_rect().contains(viewport))
                expected = pygame.Surface(size)
                expected.fill(BACKGROUND)
                expected.blit(pygame.transform.smoothscale(artwork, viewport.size), viewport)
                self.assertEqual(pygame.image.tostring(window, "RGB"), pygame.image.tostring(expected, "RGB"))

    def test_entrance_animation_changes_pixels(self):
        splash = SplashScreen()
        splash.draw(self.window, 0)
        initial = pygame.image.tostring(self.window, "RGB")
        splash.draw(self.window, 0.15)
        middle = pygame.image.tostring(self.window, "RGB")
        splash.draw(self.window, 1)
        self.assertNotEqual(initial, middle)
        self.assertNotEqual(middle, pygame.image.tostring(self.window, "RGB"))

    def test_splash_renders_with_font_subsystem_disabled(self):
        pygame.font.quit()
        splash = SplashScreen()
        splash.draw(self.window, 1.5)
        self.assertFalse(pygame.font.get_init())
        self.assertGreater(pygame.surfarray.array3d(self.window).std(), 20)

    def test_keyboard_and_left_click_skip_without_closing_shared_display(self):
        events = [
            pygame.event.Event(pygame.KEYDOWN, key=key)
            for key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE)
        ] + [pygame.event.Event(pygame.MOUSEBUTTONUP, button=1)]
        for event in events:
            with self.subTest(event=event), patch("src.splash_screen.pygame.event.get", return_value=[event]):
                self.assertTrue(show_splash(self.window))
                self.assertTrue(pygame.display.get_init())

    def test_escape_and_window_close_exit(self):
        for event in (pygame.event.Event(pygame.QUIT), pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)):
            with self.subTest(event=event), patch("src.splash_screen.pygame.event.get", return_value=[event]):
                self.assertFalse(show_splash(self.window))

    def test_auto_advance_and_unrelated_input_do_not_skip_early(self):
        with patch("src.splash_screen.pygame.time.get_ticks", side_effect=[0, 0, 1000, SPLASH_SECONDS * 1000]), patch(
            "src.splash_screen.pygame.time.Clock"
        ) as clock, patch(
            "src.splash_screen.pygame.event.get",
            return_value=[pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a), pygame.event.Event(pygame.MOUSEBUTTONUP, button=3)],
        ), patch("src.splash_screen.pygame.display.flip") as flip:
            self.assertTrue(show_splash(self.window))
        self.assertEqual(flip.call_count, 3)
        self.assertEqual(clock.return_value.tick.call_count, 2)


class SplashStartupTests(unittest.TestCase):
    def test_splash_precedes_hospital_and_appears_once(self):
        order = []
        with patch("src.hospital_game.ProgressStore"), patch(
            "src.hospital_game.HospitalNavigator"
        ) as navigator, patch(
            "src.hospital_game.show_splash",
            side_effect=lambda window: order.append("splash") or True,
        ) as splash:
            navigator.return_value.run.side_effect = lambda: order.append("hospital")
            navigator.return_value.restart_requested = False
            start_hospital_game([])
        self.assertEqual(order, ["splash", "hospital"])
        splash.assert_called_once()
        self.assertFalse(pygame.get_init())

    def test_closing_splash_does_not_start_hospital_or_save(self):
        with patch("src.hospital_game.ProgressStore") as store, patch(
            "src.hospital_game.HospitalNavigator"
        ) as navigator, patch(
            "src.hospital_game.show_splash", return_value=False,
        ):
            navigator.return_value.restart_requested = False
            navigator.return_value.run.return_value = None
            start_hospital_game([])
        navigator.assert_not_called()
        store.return_value.save.assert_not_called()
        self.assertFalse(pygame.get_init())


if __name__ == "__main__":
    unittest.main()