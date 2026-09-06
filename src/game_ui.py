from __future__ import annotations

import math
import time
from collections.abc import Sequence
from pathlib import Path
from unicodedata import bidirectional

import pygame
from bidi import get_display


def chat_font(size: int, *, bold: bool = False) -> pygame.font.Font:
    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return pygame.font.Font(str(Path(__file__).resolve().parent.parent / "data" / "fonts" / filename), size)


def is_rtl(text: str) -> bool:
    for character in text:
        direction = bidirectional(character)
        if direction in ("R", "AL", "L"):
            return direction != "L"
    return False


def wrap_text(text: str, font: pygame.font.Font, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if font.size(get_display(candidate))[0] <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = ""
            for character in word:
                if current and font.size(get_display(current + character))[0] > width:
                    lines.append(current)
                    current = ""
                current += character
        lines.append(current)
    return lines


def draw_spinner(
    screen: pygame.Surface,
    center: tuple[int, int],
    radius: int,
    color: tuple[int, int, int],
) -> None:
    angle = time.monotonic() * 5
    bounds = pygame.Rect(0, 0, radius * 2, radius * 2)
    bounds.center = center
    pygame.draw.arc(screen, color, bounds, angle, angle + math.tau * 0.72, width=2)


class ChoiceMenu:
    def __init__(self, title: str, choices: Sequence[str], detail: str = "") -> None:
        self.title = title
        self.choices = tuple(choices)
        self.detail = detail
        self.selected = 0
        self._font = pygame.font.SysFont("Avenir Next", 15, bold=True)
        self._small_font = pygame.font.SysFont("Avenir Next", 12)
        self._buttons = tuple(pygame.Rect(88, 230 + index * 48, 304, 38) for index in range(len(choices)))

    def handle_event(self, event: pygame.event.Event, window_size: tuple[int, int]) -> str | None:
        if event.type == pygame.KEYDOWN:
            if event.key in (pygame.K_UP, pygame.K_w):
                self.selected = (self.selected - 1) % len(self.choices)
            elif event.key in (pygame.K_DOWN, pygame.K_s):
                self.selected = (self.selected + 1) % len(self.choices)
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                return self.choices[self.selected]
        if event.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONUP):
            position = (event.pos[0] * 480 / window_size[0], event.pos[1] * 480 / window_size[1])
            for index, button in enumerate(self._buttons):
                if button.collidepoint(position):
                    self.selected = index
                    if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                        return self.choices[index]
        return None

    def draw(self, screen: pygame.Surface) -> None:
        overlay = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        overlay.fill((8, 20, 23, 190))
        screen.blit(overlay, (0, 0))
        panel = pygame.Rect(62, 120, 356, 126 + 48 * len(self.choices))
        pygame.draw.rect(screen, (247, 249, 244), panel, border_radius=8)
        pygame.draw.rect(screen, (52, 132, 121), (62, 120, 356, 5))
        title = self._font.render(self.title, True, (20, 32, 38))
        screen.blit(title, title.get_rect(center=(240, 151)))
        for index, line in enumerate(wrap_text(self.detail, self._small_font, 300)):
            rendered = self._small_font.render(line, True, (62, 83, 85))
            screen.blit(rendered, rendered.get_rect(midtop=(240, 174 + index * 17)))
        for index, (choice, button) in enumerate(zip(self.choices, self._buttons)):
            selected = index == self.selected
            pygame.draw.rect(screen, (52, 132, 121) if selected else (220, 232, 228), button, border_radius=5)
            text = self._font.render(choice, True, (255, 255, 251) if selected else (20, 32, 38))
            screen.blit(text, text.get_rect(center=button.center))