"""Shared, optional catalog illustrations; never patient-specific evidence."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import pygame


@lru_cache(maxsize=4)
def load_instrument_art(skill_id: str) -> tuple[pygame.Surface, tuple[int, int]]:
    """Return a native pixel-art sprite and its patient-contact pixel."""
    root = Path(__file__).resolve().parents[1] / "data/sprites/instruments"
    manifest = json.loads((root / "instruments.json").read_text())
    if skill_id not in manifest:
        raise ValueError(f"Unknown instrument: {skill_id!r}")
    entry = manifest[skill_id]
    image = pygame.image.load(str(root / entry["file"]))
    return image, (entry["contact"][0], entry["contact"][1])


def load_thumbnail(path: Path | None, size: tuple[int, int]) -> pygame.Surface | None:
    if path is None:
        return None
    try:
        image = pygame.image.load(str(path))
    except (OSError, pygame.error, ValueError) as error:
        logging.getLogger(__name__).warning("Cannot load skill illustration %s: %s", path, error)
        return None
    ratio = min(size[0] / image.get_width(), size[1] / image.get_height())
    return pygame.transform.smoothscale(image, (
        max(1, round(image.get_width() * ratio)), max(1, round(image.get_height() * ratio)),
    ))


def thermometer_result_art(reading: str | None) -> pygame.Surface | None:
    """Reuse the original casing; change only a display copy, never the evidence."""
    path = Path(__file__).resolve().parent.parent / "data/sprites/tests/thermometer.png"
    image = load_thumbnail(path, (1254, 1254))
    if image is None:
        return None
    # Coordinates belong to the original 1254px thermometer asset. Remove its
    # baked-in 38.0 reading even when no numeric result is available.
    display = pygame.Rect(584, 551, 302, 104)
    pygame.draw.rect(image, (145, 158, 135), display)
    if reading is not None:
        font = pygame.font.SysFont("Courier", 80, bold=True)
        text = font.render(reading.replace(" ", ""), False, (20, 26, 17))
        ratio = min(282 / text.get_width(), 84 / text.get_height(), 1)
        text = pygame.transform.scale(text, (
            max(1, round(text.get_width() * ratio)), max(1, round(text.get_height() * ratio)),
        ))
        image.blit(text, text.get_rect(center=display.center))
    casing = image.subsurface(pygame.Rect(32, 462, 1196, 300))
    return pygame.transform.smoothscale(casing, (312, 78))


def placeholder_art(skill_id: str | None) -> pygame.Surface:
    """Neutral specimen/request symbols for legacy skills without catalog art."""
    image = pygame.Surface((160, 160), pygame.SRCALPHA)
    ink, teal, paper = (20, 32, 38), (52, 132, 121), (247, 249, 244)
    if skill_id in {"urinalysis", "urine_nucleic_acid_amplification_test"}:
        pygame.draw.polygon(image, ink, [(35, 46), (125, 46), (117, 142), (43, 142)])
        pygame.draw.polygon(image, paper, [(40, 49), (120, 49), (112, 137), (48, 137)])
        pygame.draw.polygon(image, (244, 202, 105), [(44, 92), (116, 92), (112, 137), (48, 137)])
        pygame.draw.rect(image, ink, (31, 32, 98, 20), border_radius=4)
        pygame.draw.rect(image, teal, (35, 36, 90, 12), border_radius=2)
        pygame.draw.rect(image, paper, (55, 72, 50, 33))
        for y in (80, 88, 96):
            pygame.draw.line(image, teal, (63, y), (96, y), 2)
    else:
        pygame.draw.rect(image, ink, (34, 22, 92, 124), border_radius=5)
        pygame.draw.rect(image, paper, (39, 27, 82, 114))
        pygame.draw.rect(image, teal, (60, 15, 40, 20), border_radius=3)
        for y in (56, 78, 100, 122):
            pygame.draw.rect(image, teal, (51, y, 8, 8), width=2)
            pygame.draw.line(image, teal, (69, y + 4), (108, y + 4), 3)
    return image
