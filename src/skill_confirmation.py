"""Explicit local approval of an LLM interpretation, never approval by the model."""

from __future__ import annotations

import json

import pygame

from src.game_ui import wrap_text


class SkillConfirmation:
    def __init__(self, name: str, parameters: dict, request: str) -> None:
        self.text = (
            f"{name}\nParameters: {json.dumps(parameters, ensure_ascii=True)}\n"
            f"Your latest request: {request or '(voice transcript not yet available)'}\n\n"
            "Confirm only if this matches an action you intend now. Cancel a "
            "misrecognition, question, hypothetical or negated request: no points "
            "are charged. Confirmation does not establish patient consent. "
            "This is a simulated educational procedure."
        )
        self.selected = 0
        self.scroll = 0
        self.max_scroll = 0
        self.buttons = (pygame.Rect(24, 422, 208, 38), pygame.Rect(248, 422, 208, 38))
        self.font = pygame.font.SysFont("Avenir Next", 15)
        self.heading = pygame.font.SysFont("Avenir Next", 16, bold=True)

    def handle_event(self, event: pygame.event.Event, position: tuple[int, int]) -> bool | None:
        key = event.key if event.type == pygame.KEYDOWN else None
        if key == pygame.K_ESCAPE:
            return False
        if key in (pygame.K_TAB, pygame.K_LEFT, pygame.K_RIGHT):
            self.selected = 1 - self.selected
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return self.selected == 1
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            for index, button in enumerate(self.buttons):
                if button.collidepoint(position):
                    return index == 1
        elif event.type == pygame.MOUSEWHEEL:
            self.scroll = max(0, min(self.max_scroll, self.scroll - event.y * 42))
        elif key in (pygame.K_UP, pygame.K_DOWN, pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            delta = -120 if key in (pygame.K_UP, pygame.K_PAGEUP) else 120
            self.scroll = max(0, min(self.max_scroll, self.scroll + delta))
        return None

    def draw(self, screen: pygame.Surface) -> None:
        screen.fill((247, 249, 244))
        screen.blit(self.heading.render("CONFIRM INTERPRETED SKILL", True, (20, 32, 38)), (24, 22))
        area = pygame.Rect(24, 62, 424, 340)
        lines = wrap_text(self.text, self.font, area.width - 12)
        self.max_scroll = max(0, len(lines) * 23 - area.height)
        previous = screen.get_clip()
        screen.set_clip(area)
        for index, line in enumerate(lines):
            screen.blit(self.font.render(line, True, (20, 32, 38)), (area.x, area.y + index * 23 - self.scroll))
        screen.set_clip(previous)
        if self.max_scroll:
            screen.blit(self.font.render("Scroll for details", True, (52, 132, 121)), (24, 398))
        for index, (button, label) in enumerate(zip(self.buttons, ("Cancel / not intended", "Confirm action"))):
            pygame.draw.rect(screen, (52, 132, 121), button, border_radius=5)
            if self.selected == index:
                pygame.draw.rect(screen, (238, 105, 82), button, width=3, border_radius=5)
            text = self.font.render(label, True, (255, 255, 251))
            screen.blit(text, text.get_rect(center=button.center))
