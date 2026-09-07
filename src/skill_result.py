"""Compact completed-skill reports, separate from authored evidence and scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pygame

from src.game_ui import wrap_text
from src.skill_activity import INSTRUMENTS, draw_instrument
from src.skill_art import load_thumbnail, placeholder_art, thermometer_result_art


@dataclass(frozen=True)
class SkillResultSummary:
    skill_id: str
    name: str
    result: str
    points: int
    rationale: str


class SkillResultPanel:
    DETAILS_RECT = pygame.Rect(52, 380, 116, 30)
    BODY_RECT = pygame.Rect(52, 280, 376, 76)
    REPORT_RECT = pygame.Rect(52, 110, 376, 246)
    INK = (20, 32, 38)
    TEAL = (39, 108, 98)

    def __init__(self, summary: SkillResultSummary, icon: Path | None,
                 evidence: pygame.Surface | None = None) -> None:
        self.summary = summary
        self.details_open = False
        self.font = pygame.font.SysFont("Avenir Next", 15)
        self.title_font = pygame.font.SysFont("Avenir Next", 20, bold=True)
        self.reading_font = pygame.font.SysFont("Avenir Next", 40, bold=True)
        self.small = pygame.font.SysFont("Avenir Next", 11)
        self.reading = None
        if summary.skill_id == "temperature":
            match = re.search(r"(\d+(?:\.\d+)?)\s*\N{DEGREE SIGN}?\s*C\b", summary.result)
            if match:
                self.reading = f"{match[1]} \N{DEGREE SIGN}C"
        if summary.skill_id == "temperature":
            self.art = thermometer_result_art(self.reading)
            if self.art is None:
                self.art = pygame.transform.smoothscale(placeholder_art(summary.skill_id), (96, 96))
        elif summary.skill_id in INSTRUMENTS:
            self.art = pygame.Surface((352, 152), pygame.SRCALPHA)
            draw_instrument(self.art, summary.skill_id, (324, 72), 2.8)
            self.art = self.art.subsurface(self.art.get_bounding_rect()).copy()
            ratio = min(312 / self.art.get_width(), 96 / self.art.get_height())
            self.art = pygame.transform.scale(self.art, (
                round(self.art.get_width() * ratio), round(self.art.get_height() * ratio),
            ))
        else:
            self.art = load_thumbnail(icon, (312, 96))
            if self.art is None:
                self.art = pygame.transform.smoothscale(placeholder_art(summary.skill_id), (96, 96))
        self.evidence = None
        if evidence is not None:
            ratio = min(360 / evidence.get_width(), 180 / evidence.get_height())
            self.evidence = pygame.transform.smoothscale(evidence, (
                max(1, round(evidence.get_width() * ratio)),
                max(1, round(evidence.get_height() * ratio)),
            ))

    @property
    def report(self) -> str:
        return f"{self.summary.name}\n{self.summary.result}\n\nWhy this score:\n{self.summary.rationale}"

    def toggle_details(self, event: pygame.event.Event, position: tuple[int, int]) -> bool:
        if ((event.type == pygame.KEYDOWN and event.key == pygame.K_d)
                or (event.type == pygame.MOUSEBUTTONUP and event.button == 1
                    and self.DETAILS_RECT.collidepoint(position))):
            self.details_open = not self.details_open
            return True
        return False

    def draw(self, screen: pygame.Surface, scroll: int) -> int:
        area = self.REPORT_RECT if self.details_open else self.BODY_RECT
        text = self.report if self.details_open else (self.reading or self.summary.result)
        font = self.reading_font if self.reading and not self.details_open else self.font
        line_height = font.get_linesize() + 3
        lines = wrap_text(text, font, area.width - 12)
        image_height = self.evidence.get_height() + 12 if self.details_open and self.evidence is not None else 0
        max_scroll = max(0, image_height + len(lines) * line_height - area.height)
        if not self.details_open:
            title_lines = wrap_text(self.summary.name, self.title_font, 376)
            if len(title_lines) > 2:
                title_lines = title_lines[:1] + ["More in Details..."]
            for index, line in enumerate(title_lines):
                screen.blit(self.title_font.render(line, True, self.TEAL), (52, 110 + index * 24))
            screen.blit(self.art, self.art.get_rect(center=(240, 212)))
            caption = self.small.render("Illustration only - not a result", True, self.INK)
            screen.blit(caption, caption.get_rect(midtop=(240, 263)))
        previous_clip = screen.get_clip()
        screen.set_clip(area.clip(previous_clip))
        y = area.y - min(scroll, max_scroll)
        if self.details_open and self.evidence is not None:
            screen.blit(self.evidence, self.evidence.get_rect(midtop=(area.centerx, y)))
            y += image_height
        for line in lines:
            rendered = font.render(line, True, self.INK)
            x = area.centerx - rendered.get_width() // 2 if self.reading and not self.details_open else area.x
            screen.blit(rendered, (x, y))
            y += line_height
        screen.set_clip(previous_clip)
        score = self.font.render(f"Appropriate use {self.summary.points:+d}", True, self.TEAL)
        screen.blit(score, (242, 386))
        pygame.draw.rect(screen, (220, 232, 228), self.DETAILS_RECT, border_radius=4)
        label = self.font.render("Less / D" if self.details_open else "Details / D", True, self.INK)
        screen.blit(label, label.get_rect(center=self.DETAILS_RECT.center))
        if max_scroll:
            hint = self.small.render("Scroll / Up / Down for more", True, self.TEAL)
            screen.blit(hint, (52, 360))
        return max_scroll
