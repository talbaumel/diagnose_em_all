from __future__ import annotations

from pathlib import Path

import pygame


ASSETS = Path(__file__).resolve().parents[1] / "data"
APP_ICON = ASSETS / "sprites/ui/app_icon.png"
SPLASH_IMAGE = ASSETS / "sprites/ui/splash_screen.png"
SPLASH_SECONDS = 4.0
TITLE = "Diagnose Em' All"
BACKGROUND = (242, 249, 245)


def configure_app_icon() -> None:
    pygame.display.set_icon(pygame.image.load(str(APP_ICON)))


class SplashScreen:
    def __init__(self) -> None:
        self._artwork = pygame.image.load(str(SPLASH_IMAGE)).convert()

    def draw(self, window: pygame.Surface, elapsed: float) -> None:
        viewport = self._artwork.get_rect().fit(window.get_rect())
        frame = pygame.transform.smoothscale(self._artwork, viewport.size)
        alpha = round(255 * min(1.0, max(0.0, elapsed) / 0.3))
        if alpha < 255:
            frame.set_alpha(alpha)
        window.fill(BACKGROUND)
        window.blit(frame, viewport)


def show_splash(window: pygame.Surface) -> bool:
    pygame.display.set_caption(TITLE)
    splash = SplashScreen()
    clock = pygame.time.Clock()
    started = pygame.time.get_ticks()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    return False
                if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                    return True
            if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                return True
        elapsed = (pygame.time.get_ticks() - started) / 1000
        splash.draw(window, elapsed)
        pygame.display.flip()
        if elapsed >= SPLASH_SECONDS:
            return True
        clock.tick(60)