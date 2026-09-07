from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

import pygame

from src.game_ui import wrap_text


class _Skill(Protocol):
    id: str
    name: str
    category: str
    description: str
    aliases: Iterable[str]
    examples: Iterable[str]
    note: str
    icon: Path | None
    parameters: Mapping[str, Any]


class SkillBrowser:
    """480px logical modal; actions are ('close', '') or ('draft', request).

    Pass catalog entries in display order and logical mouse coordinates to
    handle_event. Left-button release activates controls. Draft actions never
    execute a skill; the caller owns dismissal, chat focus, and existing drafts.
    """

    ROWS = 12
    ROW_HEIGHT = 27
    LIST_RECT = pygame.Rect(24, 78, 420, ROWS * ROW_HEIGHT)
    ART_RECT = pygame.Rect(24, 100, 120, 120)
    TEXT_RECT = pygame.Rect(24, 244, 420, 144)
    BACK_RECT = pygame.Rect(24, 426, 94, 32)
    REQUEST_RECT = pygame.Rect(130, 426, 326, 32)
    LINE_HEIGHT = 18

    _BG = (247, 249, 244)
    _INK = (20, 32, 38)
    _MUTED = (62, 83, 85)
    _ACCENT = (37, 107, 98)
    _PALE = (220, 232, 228)

    def __init__(self, catalog: Iterable[_Skill]) -> None:
        self.catalog = tuple(catalog)
        self.selected = 0
        self.list_offset = 0
        self.details_open = False
        self.detail_scroll = 0
        self._font = pygame.font.SysFont("Avenir Next", 14)
        self._bold = pygame.font.SysFont("Avenir Next", 15, bold=True)
        self._small = pygame.font.SysFont("Avenir Next", 12)
        self._title = pygame.font.SysFont("Avenir Next", 20, bold=True)
        self._art_cache: dict[Path, pygame.Surface | None] = {}
        self._detail_lines: list[str] = []

    @property
    def selected_skill(self) -> _Skill | None:
        return self.catalog[self.selected] if self.catalog else None

    @property
    def _detail_limit(self) -> int:
        return max(0, len(self._detail_lines) - self.TEXT_RECT.height // self.LINE_HEIGHT)

    def _select(self, index: int) -> None:
        self.selected = max(0, min(index, len(self.catalog) - 1))
        self.list_offset = min(self.list_offset, self.selected)
        self.list_offset = max(self.list_offset, self.selected - self.ROWS + 1)
        self.list_offset = max(0, min(self.list_offset, len(self.catalog) - self.ROWS))

    def _open_details(self) -> None:
        skill = self.selected_skill
        if skill is None:
            return
        self.details_open = True
        self.detail_scroll = 0
        paragraphs = [
            skill.name,
            f"Category: {skill.category}",
            f"ID: {skill.id}",
            "",
            skill.description,
            "",
            "Aliases: " + (", ".join(skill.aliases) or "None"),
            "",
            "Example requests",
        ]
        examples = tuple(skill.examples)[:3]
        paragraphs.extend(f"{index}. {example}" for index, example in enumerate(examples, 1))
        if not examples:
            paragraphs.append("No examples supplied.")
        paragraphs.extend(("", "Note: " + (skill.note or "None"), "", "Required parameters"))
        required = skill.parameters.get("required", ())
        properties = skill.parameters.get("properties", {})
        for name in required:
            schema = properties.get(name, {})
            paragraphs.append(f"{name}: {json.dumps(schema, ensure_ascii=False)}")
        if not required:
            paragraphs.append("None")
        self._detail_lines = [
            line for text in paragraphs for line in wrap_text(text, self._font, self.TEXT_RECT.width - 8)
        ]

    def _draft(self) -> tuple[str, str] | None:
        skill = self.selected_skill
        if skill is None:
            return None
        example = next(iter(skill.examples), "")
        return "draft", example or f"Please perform {skill.name}."

    def _scroll(self, amount: int) -> None:
        if self.details_open:
            self.detail_scroll = max(0, min(self.detail_scroll + amount, self._detail_limit))
        else:
            self._select(self.selected + amount)

    def handle_event(
        self, event: pygame.event.Event, position: tuple[float, float] | None = None
    ) -> tuple[str, str] | None:
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                if self.details_open:
                    self.details_open = False
                else:
                    return "close", ""
            elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                if self.details_open:
                    return self._draft()
                self._open_details()
            elif event.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                page = self.TEXT_RECT.height // self.LINE_HEIGHT if self.details_open else self.ROWS
                amount = {
                    pygame.K_UP: -1, pygame.K_DOWN: 1,
                    pygame.K_PAGEUP: -page, pygame.K_PAGEDOWN: page,
                }[event.key]
                self._scroll(amount)
            elif event.key in (pygame.K_HOME, pygame.K_END):
                if self.details_open:
                    self.detail_scroll = 0 if event.key == pygame.K_HOME else self._detail_limit
                else:
                    self._select(0 if event.key == pygame.K_HOME else len(self.catalog) - 1)
        elif event.type == pygame.MOUSEWHEEL:
            direction = -1 if getattr(event, "flipped", False) else 1
            self._scroll(-event.y * direction * 3)
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button in (4, 5):
            self._scroll(-3 if event.button == 4 else 3)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            point = position if position is not None else getattr(event, "pos", None)
            if point is None:
                return None
            if self.BACK_RECT.collidepoint(point):
                if self.details_open:
                    self.details_open = False
                else:
                    return "close", ""
            elif self.details_open:
                if self.REQUEST_RECT.collidepoint(point):
                    return self._draft()
            elif self.LIST_RECT.collidepoint(point):
                index = self.list_offset + int((point[1] - self.LIST_RECT.top) // self.ROW_HEIGHT)
                if index < len(self.catalog):
                    self._select(index)
                    self._open_details()
        return None

    def _thumbnail(self, path: Path | None) -> pygame.Surface | None:
        if path is None:
            return None
        if path not in self._art_cache:
            try:
                image = pygame.image.load(str(path))
                ratio = min(120 / image.get_width(), 120 / image.get_height())
                size = (max(1, round(image.get_width() * ratio)), max(1, round(image.get_height() * ratio)))
                self._art_cache[path] = pygame.transform.smoothscale(image, size)
            except (OSError, pygame.error, ValueError):
                self._art_cache[path] = None
        return self._art_cache[path]

    def _text(self, surface: pygame.Surface, text: str, font: pygame.font.Font,
              x: int, y: int, width: int, color: tuple[int, int, int] | None = None) -> None:
        if font.size(text)[0] > width:
            while text and font.size(text + "...")[0] > width:
                text = text[:-1]
            text += "..."
        surface.blit(font.render(text, True, color or self._INK), (x, y))

    def _button(self, surface: pygame.Surface, rect: pygame.Rect, label: str, primary: bool = False) -> None:
        pygame.draw.rect(surface, self._ACCENT if primary else self._PALE, rect, border_radius=5)
        text = self._bold.render(label, True, self._BG if primary else self._INK)
        surface.blit(text, text.get_rect(center=rect.center))

    def _scrollbar(self, surface: pygame.Surface, rect: pygame.Rect, offset: int, total: int, visible: int) -> None:
        if total <= visible:
            return
        track = pygame.Rect(rect.right + 5, rect.top, 5, rect.height)
        pygame.draw.rect(surface, self._PALE, track, border_radius=2)
        height = max(12, round(track.height * visible / total))
        y = track.top + round((track.height - height) * offset / (total - visible))
        pygame.draw.rect(surface, self._ACCENT, (track.x, y, track.width, height), border_radius=2)

    def draw(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(surface, self._BG, (12, 12, 456, 456), border_radius=8)
        self._text(surface, "Skill browser", self._title, 24, 22, 300)
        count = f"{self.selected + 1 if self.catalog else 0} / {len(self.catalog)}"
        self._text(surface, count, self._bold, 376, 26, 80)
        self._text(surface, "All skills / illustrative art only", self._small, 24, 51, 430, self._MUTED)
        if self.details_open and self.selected_skill is not None:
            self._draw_details(surface)
        else:
            self._draw_list(surface)

    def _draw_list(self, surface: pygame.Surface) -> None:
        if not self.catalog:
            self._text(surface, "No skills available.", self._font, 24, 90, 420)
        for row, skill in enumerate(self.catalog[self.list_offset:self.list_offset + self.ROWS]):
            index = self.list_offset + row
            rect = pygame.Rect(self.LIST_RECT.x, self.LIST_RECT.y + row * self.ROW_HEIGHT,
                               self.LIST_RECT.width, self.ROW_HEIGHT)
            selected = index == self.selected
            if selected:
                pygame.draw.rect(surface, self._ACCENT, rect, border_radius=4)
            self._text(surface, skill.name, self._bold, rect.x + 8, rect.y + 4, rect.width - 16,
                       self._BG if selected else self._INK)
        self._scrollbar(surface, self.LIST_RECT, self.list_offset, len(self.catalog), self.ROWS)
        self._text(surface, "Up/Down or wheel: select   Enter / click: details", self._small, 24, 407, 430, self._MUTED)
        self._button(surface, self.BACK_RECT, "Close")
        self._text(surface, "PgUp/PgDn / Home/End   Esc: close", self._small, 130, 435, 326, self._MUTED)

    def _draw_details(self, surface: pygame.Surface) -> None:
        skill = self.selected_skill
        self._text(surface, skill.name, self._bold, 24, 76, 430)
        pygame.draw.rect(surface, self._PALE, self.ART_RECT, border_radius=5)
        art = self._thumbnail(skill.icon)
        if art is not None:
            surface.blit(art, art.get_rect(center=self.ART_RECT.center))
        else:
            self._text(surface, "No illustration", self._small, 32, 149, 104, self._MUTED)
        for index, line in enumerate(wrap_text(
            "Conceptual illustration only.\nNot a patient finding or result.\n\n"
            "Browse any skill; a request only fills your chat draft.", self._font, 286
        )):
            self._text(surface, line, self._font, 160, 101 + index * self.LINE_HEIGHT, 286, self._MUTED)
        self._text(surface, "Skill details", self._bold, 24, 223, 420)
        old_clip = surface.get_clip()
        surface.set_clip(old_clip.clip(self.TEXT_RECT))
        try:
            visible = self.TEXT_RECT.height // self.LINE_HEIGHT
            for row, line in enumerate(self._detail_lines[self.detail_scroll:self.detail_scroll + visible]):
                surface.blit(self._font.render(line, True, self._INK),
                             (self.TEXT_RECT.x, self.TEXT_RECT.y + row * self.LINE_HEIGHT))
        finally:
            surface.set_clip(old_clip)
        self._scrollbar(surface, self.TEXT_RECT, self.detail_scroll, len(self._detail_lines), visible)
        self._text(surface, "Up/Down, PgUp/PgDn, Home/End or wheel: scroll", self._small, 24, 392, 430, self._MUTED)
        self._text(surface, "Enter: draft request   Esc: back   Nothing is sent.", self._small, 24, 408, 430, self._MUTED)
        self._button(surface, self.BACK_RECT, "Back")
        self._button(surface, self.REQUEST_RECT, "Draft request (not sent)", primary=True)
