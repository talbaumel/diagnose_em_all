from __future__ import annotations

import argparse
from pathlib import Path

import pygame


def prepare_logo(source: Path, output: Path) -> None:
    if source.resolve() == output.resolve():
        raise ValueError("The source image must be preserved")
    image = pygame.image.load(str(source))
    cleaned = pygame.Surface(image.get_size(), pygame.SRCALPHA)
    cleaned.blit(image, (0, 0))
    whites = pygame.mask.from_threshold(cleaned, (255, 255, 255, 255), (32, 32, 32, 255))
    background = whites.connected_component((0, 0))
    if not background.count():
        raise ValueError("Expected a white background at the top-left corner")
    alpha = background.to_surface(
        surface=pygame.Surface(image.get_size(), pygame.SRCALPHA),
        setcolor=(255, 255, 255, 0),
        unsetcolor=(255, 255, 255, 255),
    )
    cleaned.blit(alpha, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    output.parent.mkdir(parents=True, exist_ok=True)
    pygame.image.save(cleaned, str(output))
    result = pygame.image.load(str(output))
    assert result.get_at((0, 0)).a == 0
    assert 0 < pygame.mask.from_surface(result).count() < result.get_width() * result.get_height()
    print(f"Prepared transparent logo: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Remove the connected white background from a logo")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    prepare_logo(arguments.source, arguments.output)