from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

import pygame

from src.hospital_game import HospitalNavigator, load_patient_scenarios
from src.realtime_conversation import PatientAnimator, Test
from src.game_ui import ChoiceMenu


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline hospital and consultation preview")
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/polish"))
    parser.add_argument("--size", type=int, default=720)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    scenarios = load_patient_scenarios(sorted((root / "data/prompts").glob("*.json")))
    pygame.init()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    window = pygame.display.set_mode((arguments.size, arguments.size))
    navigator = HospitalNavigator(scenarios, set(), window=window)
    navigator._scene_started_at = -1000
    if arguments.interactive:
        try:
            navigator.run()
        finally:
            pygame.quit()
            loop.close()
            asyncio.set_event_loop(None)
        return
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    navigator.draw()
    pygame.image.save(window, str(arguments.output_dir / "hospital.png"))
    navigator._player_position = pygame.Vector2(225, 360)
    navigator._camera_position = navigator._camera_target()
    navigator.update(pygame.Vector2(), 0.016)
    navigator.draw()
    pygame.image.save(window, str(arguments.output_dir / "patient.png"))
    navigator._pause()
    navigator.draw()
    pygame.image.save(window, str(arguments.output_dir / "pause.png"))
    navigator._diagnosed.update(scenario.patient_type for scenario in scenarios)
    navigator._menu = ChoiceMenu("ALL CASES CLOSED", ("Continue Exploring", "New Game"), "11 patients helped. Hospital rounds complete.")
    navigator.draw()
    pygame.image.save(window, str(arguments.output_dir / "complete.png"))
    navigator._menu = None
    navigator._diagnosed.clear()
    player_sheet = pygame.Surface((600, 4 * 128), pygame.SRCALPHA)
    player_sheet.fill((228, 237, 231))
    for row, direction in enumerate(("down", "left", "right", "up")):
        for column, frame in enumerate(navigator._player_frames[direction]):
            player_sheet.blit(frame, frame.get_rect(midbottom=(column * 100 + 50, row * 128 + 112)))
    pygame.image.save(player_sheet, str(arguments.output_dir / "walk_frames.png"))
    idle_sheet = pygame.Surface((400, 4 * 128), pygame.SRCALPHA)
    idle_sheet.fill((22, 43, 46))
    if navigator._player_idle_frames:
        for row, direction in enumerate(("down", "left", "right", "up")):
            for column, frame in enumerate(navigator._player_idle_frames[direction]):
                idle_sheet.blit(frame, frame.get_rect(midbottom=(column * 100 + 50, row * 128 + 112)))
    pygame.image.save(idle_sheet, str(arguments.output_dir / "idle_frames.png"))
    patients = pygame.Surface((1600, 128 * len(scenarios)))
    patients.fill((228, 237, 231))
    for row, scenario in enumerate(scenarios):
        for state_index, state in enumerate(("idle", "talking", "worried", "relieved")):
            for frame_index, frame in enumerate(navigator._patient_frames[scenario.patient_type][state]):
                column = state_index * 4 + frame_index
                patients.blit(frame, frame.get_rect(midbottom=(column * 100 + 50, row * 128 + 112)))
    pygame.image.save(patients, str(arguments.output_dir / "patient_frames.png"))
    animator = PatientAnimator(0, window=window)
    animator._scene_started_at = time.monotonic() - 1
    animator.add_transcript("You", "How have you been feeling today?")
    animator.add_transcript("Patient", "My nose is stuffy and my throat hurts. I started feeling sick yesterday.", "preview")
    animator.draw(0.3)
    pygame.image.save(window, str(arguments.output_dir / "consultation.png"))
    animator.show_test_result(Test("Temperature", "Temperature 38 C. Mild fever."))
    animator.draw(0.3)
    pygame.image.save(window, str(arguments.output_dir / "evidence.png"))
    animator.show_test_result(Test("Detailed laboratory findings and follow-up observations", "Measurements within expected limits. " * 30 + "LongFinding" * 20))
    animator.draw(0.3)
    pygame.image.save(window, str(arguments.output_dir / "long_evidence.png"))
    animator.close_test_result()
    animator.show_win()
    animator.draw(0.3)
    pygame.image.save(window, str(arguments.output_dir / "solved.png"))
    animator.close()
    pygame.quit()
    loop.close()
    asyncio.set_event_loop(None)
    print(f"Exported offline previews to {arguments.output_dir}")


if __name__ == "__main__":
    main()