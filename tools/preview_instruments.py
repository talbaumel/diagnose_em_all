"""Export the equipment drawer and all four instrument activities without live services."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pygame

from src.realtime_conversation import PatientAnimator
from src.skill_activity import INSTRUMENTS, InstrumentActivity
from src.skill_browser import EquipmentDrawer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/instruments"))
    arguments = parser.parse_args()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    pygame.init()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    window = pygame.display.set_mode((480, 480))
    animator = PatientAnimator(0, window=window, disease="common cold")
    try:
        animator._open_skills()
        assert animator._skill_browser is not None
        catalog = animator._skill_browser.catalog
        animator._skill_browser = EquipmentDrawer(skill for skill in catalog if skill.id in INSTRUMENTS)
        for size in (480, 720, 960):
            pygame.display.set_mode((size, size))
            animator.draw(.3)
            pygame.image.save(window, str(arguments.output_dir / f"drawer_{size}.png"))
        sheet = pygame.image.load(str(root / "data/sprites/patients/01_common_cold_kid_atlas.png"))
        frame = sheet.subsurface((0, 256, 256, 256)).copy()
        for state in ("idle", "picked"):
            overview = pygame.Surface((960, 960))
            for index, skill_id in enumerate(INSTRUMENTS):
                activity = InstrumentActivity(frame, "COMMON_COLD_KID", skill_id)
                if state == "picked":
                    activity.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), (0, 0))
                    activity.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB), (0, 0))
                canvas = pygame.Surface((480, 480))
                activity.draw(canvas)
                overview.blit(canvas, ((index % 2) * 480, (index // 2) * 480))
            pygame.image.save(overview, str(arguments.output_dir / f"instruments_{state}.png"))
    finally:
        animator.close()
        pygame.quit()
        loop.close()
        asyncio.set_event_loop(None)
    print(f"Exported drawer and instrument previews to {arguments.output_dir}")


if __name__ == "__main__":
    main()