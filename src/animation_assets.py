from __future__ import annotations

from pathlib import Path

import pygame


def load_atlas(path: Path, columns: int, rows: int = 4) -> tuple[tuple[pygame.Surface, ...], ...]:
    sheet = pygame.image.load(str(path)).convert_alpha()
    if sheet.get_size() != (columns * 256, rows * 256):
        raise ValueError(f"Invalid atlas dimensions: {path.name}")
    result = []
    for row in range(rows):
        frames = []
        for column in range(columns):
            frame = sheet.subsurface((column * 256, row * 256, 256, 256)).copy()
            mask = pygame.mask.from_surface(frame, threshold=16)
            bounds = frame.get_bounding_rect(min_alpha=17)
            if mask.count() < 100 or bounds.width == 0:
                raise ValueError(f"Empty atlas cell: {path.name} ({column}, {row})")
            if not pygame.Rect(4, 4, 248, 248).contains(bounds):
                raise ValueError(f"Atlas cell lacks transparent padding: {path.name} ({column}, {row})")
            frames.append(frame.subsurface(bounds).copy())
        result.append(tuple(frames))
    return tuple(result)