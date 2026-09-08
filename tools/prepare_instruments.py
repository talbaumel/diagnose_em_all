"""Extract transparent, native-size drawer sprites from the retained source sheet."""

from __future__ import annotations

import json
from pathlib import Path

import pygame


ROOT = Path(__file__).resolve().parents[1] / "data/sprites/instruments"
REGIONS = {
    "temperature": ((0, 0, 900, 450), None),
    "oxygen_saturation": ((900, 0, 636, 450), (1320, 333)),
    "lung_auscultation": ((0, 450, 900, 574), (734, 593)),
    "ear_examination": ((900, 450, 636, 574), (1301, 560)),
}


def prepare() -> None:
    source = pygame.image.load(str(ROOT / "drawer_tools_source.png"))
    if source.get_size() != (1536, 1024):
        raise ValueError("Instrument crop coordinates require a 1536x1024 source")
    manifest = {}
    for skill_id, (region, contact) in REGIONS.items():
        cell = pygame.Surface(region[2:], pygame.SRCALPHA)
        cell.blit(source, (0, 0), region)
        whites = pygame.mask.from_threshold(cell, (255, 255, 255, 255), (12, 12, 12, 255))
        for background in whites.connected_components(256):
            alpha = background.to_surface(
                surface=pygame.Surface(cell.get_size(), pygame.SRCALPHA),
                setcolor=(255, 255, 255, 0), unsetcolor=(255, 255, 255, 255),
            )
            cell.blit(alpha, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        foreground = pygame.mask.from_surface(cell).connected_components(256)
        if len(foreground) != 1:
            raise ValueError(f"Expected one complete {skill_id}, found {len(foreground)}")
        silhouette = foreground[0]
        alpha = silhouette.to_surface(
            surface=pygame.Surface(cell.get_size(), pygame.SRCALPHA),
            setcolor=(255, 255, 255, 255), unsetcolor=(255, 255, 255, 0),
        )
        cell.blit(alpha, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        bounds = cell.get_bounding_rect()
        ratio = min(101 / bounds.width, 44 / bounds.height)
        size = (max(1, round(bounds.width * ratio)), max(1, round(bounds.height * ratio)))
        image = pygame.transform.scale(cell.subsurface(bounds), size)
        if contact is None:
            contact_x = image.get_bounding_rect().right - 1
            contact_y = min(
                (row for row in range(size[1]) if image.get_at((contact_x, row)).a),
                key=lambda row: abs(row - size[1] // 2),
            )
        else:
            contact_x = round((contact[0] - region[0] - bounds.x) * size[0] / bounds.width)
            contact_y = round((contact[1] - region[1] - bounds.y) * size[1] / bounds.height)
        if not image.get_at((contact_x, contact_y)).a:
            raise ValueError(f"Contact point is transparent for {skill_id}")
        pygame.image.save(image, str(ROOT / f"{skill_id}.png"))
        manifest[skill_id] = {"file": f"{skill_id}.png", "contact": [contact_x, contact_y]}
        print(f"{skill_id}: {size}, contact {(contact_x, contact_y)}")
    (ROOT / "instruments.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    prepare()