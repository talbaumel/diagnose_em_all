"""Explicit local approval of an LLM interpretation, never approval by the model."""

from __future__ import annotations

import json
from pathlib import Path

import pygame

from src.game_ui import wrap_text
from src.skill_art import load_thumbnail, placeholder_art


class SkillConfirmation:
    ART_RECT = pygame.Rect(24, 70, 176, 176)
    CONTENT_RECT = pygame.Rect(24, 276, 432, 88)
    DETAILS_RECT = pygame.Rect(24, 373, 112, 28)

    def __init__(self, name: str, parameters: dict, request: str, *,
                 skill_id: str | None = None, icon: Path | None = None) -> None:
        self.name = name
        self.skill_id = skill_id
        self.icon = icon
        self.art = load_thumbnail(icon, self.ART_RECT.size)
        if self.art is None:
            self.art = placeholder_art(skill_id)
        self.summary = "\n".join(
            f"{key.replace('_', ' ').capitalize()}: {self._value(value)}"
            for key, value in parameters.items() if key not in {"consent_turn", "evidence_turns"}
        ) or "Only confirm if this is what you requested."
        self.details = (
            f"{name}\nYour request: {request or '(voice transcript not yet available)'}\n"
            + (f"Options: {json.dumps(parameters, ensure_ascii=False)}\n" if parameters else "")
            + "Cancel a wrong or unintended request: no points charged.\n"
            "Patient consent must be obtained separately."
        )
        self.details_open = False
        self.selected = 0
        self.scroll = 0
        self.max_scroll = 0
        self.buttons = (pygame.Rect(24, 422, 208, 38), pygame.Rect(248, 422, 208, 38))
        self.font = pygame.font.SysFont("Avenir Next", 15)
        self.heading = pygame.font.SysFont("Avenir Next", 22, bold=True)
        self.name_font = pygame.font.SysFont("Avenir Next", 20, bold=True)
        self.small = pygame.font.SysFont("Avenir Next", 11)
        self.name_lines = wrap_text(name, self.name_font, 236)
        if len(self.name_lines) > 6:
            self.name_font = pygame.font.SysFont("Avenir Next", 16, bold=True)
            self.name_lines = wrap_text(name, self.name_font, 236)
        if len(self.name_lines) > 6:
            self.name_lines = self.name_lines[:5] + ["More in Details..."]

    @staticmethod
    def _value(value: object) -> str:
        if isinstance(value, str):
            return value.replace("_", " ")
        if isinstance(value, list):
            return ", ".join(SkillConfirmation._value(item) for item in value)
        return json.dumps(value, ensure_ascii=False)

    @property
    def text(self) -> str:
        return self.details if self.details_open else self.summary

    def _toggle_details(self) -> None:
        self.details_open = not self.details_open
        self.scroll = 0

    def handle_event(self, event: pygame.event.Event, position: tuple[int, int]) -> bool | None:
        key = event.key if event.type == pygame.KEYDOWN else None
        if key == pygame.K_ESCAPE:
            return False
        if key == pygame.K_d:
            self._toggle_details()
            return None
        if key in (pygame.K_TAB, pygame.K_LEFT, pygame.K_RIGHT):
            self.selected = 1 - self.selected
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            return self.selected == 1
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self.DETAILS_RECT.collidepoint(position):
                self._toggle_details()
                return None
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
        screen.blit(self.heading.render("Confirm skill", True, (20, 32, 38)), (24, 22))
        pygame.draw.rect(screen, (220, 232, 228), self.ART_RECT, border_radius=6)
        screen.blit(self.art, self.art.get_rect(center=self.ART_RECT.center))
        caption = self.small.render("Illustration only - not a result", True, (62, 83, 85))
        screen.blit(caption, caption.get_rect(midtop=(self.ART_RECT.centerx, 252)))
        name_y = self.ART_RECT.centery - len(self.name_lines) * 12
        for index, line in enumerate(self.name_lines):
            screen.blit(self.name_font.render(line, True, (20, 32, 38)), (220, name_y + index * 24))
        area = self.CONTENT_RECT
        lines = wrap_text(self.text, self.font, area.width - 12)
        self.max_scroll = max(0, len(lines) * 23 - area.height)
        self.scroll = min(self.scroll, self.max_scroll)
        previous = screen.get_clip()
        screen.set_clip(area)
        for index, line in enumerate(lines):
            screen.blit(self.font.render(line, True, (20, 32, 38)), (area.x, area.y + index * 23 - self.scroll))
        screen.set_clip(previous)
        pygame.draw.rect(screen, (220, 232, 228), self.DETAILS_RECT, border_radius=4)
        detail_label = self.font.render("Less / D" if self.details_open else "Details / D", True, (20, 32, 38))
        screen.blit(detail_label, detail_label.get_rect(center=self.DETAILS_RECT.center))
        if self.max_scroll:
            screen.blit(self.small.render("Scroll / Up / Down for more", True, (52, 132, 121)), (154, 382))
        screen.blit(self.small.render("Simulation only. Confirmation is not patient consent.", True, (62, 83, 85)), (24, 406))
        for index, (button, label) in enumerate(zip(self.buttons, ("Cancel", "Confirm"))):
            pygame.draw.rect(screen, (220, 232, 228) if index == 0 else (52, 132, 121), button, border_radius=5)
            if self.selected == index:
                pygame.draw.rect(screen, (238, 105, 82), button, width=3, border_radius=5)
            text = self.font.render(label, True, (20, 32, 38) if index == 0 else (255, 255, 251))
            screen.blit(text, text.get_rect(center=button.center))
