from __future__ import annotations

import argparse
from pathlib import Path

import pygame

from src.animation_assets import load_atlas
from src.realtime_conversation import normalize_character_frames


def prepare_atlas(source: Path, output: Path, columns: int) -> None:
    if source.resolve() == output.resolve():
        raise ValueError("The source image must be preserved")
    sheet = pygame.image.load(str(source)).convert_alpha()
    whites = pygame.mask.from_threshold(sheet, (255, 255, 255, 255), (32, 32, 32, 255))
    components = whites.connected_components()
    if not components:
        raise ValueError("A white-background source is required")
    background = max(components, key=lambda component: component.count())
    alpha = background.to_surface(surface=pygame.Surface(sheet.get_size(), pygame.SRCALPHA), setcolor=(255, 255, 255, 0), unsetcolor=(255, 255, 255, 255))
    sheet.blit(alpha, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    figures = pygame.mask.from_surface(sheet, threshold=16).connected_components(1000)
    if len(figures) != columns * 4:
        raise ValueError(f"Expected {columns * 4} isolated figures, found {len(figures)}")
    figures.sort(key=lambda figure: figure.get_bounding_rects()[0].centery)
    frames = []
    for row in range(4):
        row_figures = figures[row * columns:(row + 1) * columns]
        row_figures.sort(key=lambda figure: figure.get_bounding_rects()[0].centerx)
        for figure in row_figures:
            bounds = figure.get_bounding_rects()[0]
            frame = sheet.subsurface(bounds).copy()
            mask = figure.to_surface(surface=pygame.Surface(sheet.get_size(), pygame.SRCALPHA), setcolor=(255, 255, 255, 255), unsetcolor=(255, 255, 255, 0))
            frame.blit(mask, (0, 0), area=bounds, special_flags=pygame.BLEND_RGBA_MULT)
            frames.append(frame)
    normalized = normalize_character_frames(frames, 208)
    atlas = pygame.Surface((columns * 256, 1024), pygame.SRCALPHA)
    for index, frame in enumerate(normalized):
        row, column = divmod(index, columns)
        atlas.blit(frame, frame.get_rect(midbottom=(column * 256 + 128, row * 256 + 236)))
    output.parent.mkdir(parents=True, exist_ok=True)
    pygame.image.save(atlas, str(output))
    load_atlas(output, columns)
    print(f"Prepared and validated {len(frames)} frames: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Repack isolated figures from a white-background source into a transparent atlas")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--columns", type=int, choices=(4, 6), required=True)
    arguments = parser.parse_args()
    pygame.init()
    pygame.display.set_mode((1, 1))
    try:
        prepare_atlas(arguments.source, arguments.output, arguments.columns)
    finally:
        pygame.quit()


if __name__ == "__main__":
    main()