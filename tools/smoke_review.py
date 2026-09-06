from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pygame

from src.consultation_review import REVIEW_TIMEOUT_SECONDS, ConsultationMetrics, score_consultation
from src.realtime_conversation import PatientAnimator, _realtime_authorization


async def run(output: Path) -> None:
    transcript = [
        ("You", "Hello, I'm Dr. Ash. It's okay to feel nervous. What has been bothering you?"),
        ("Patient", "My nose is runny and my throat hurts. I'm tired."),
        ("You", "I'm sorry you're feeling poorly. When did this start?"),
        ("Patient", "Yesterday. I have a cough too."),
        ("You", "Are you having trouble breathing or drinking water?"),
        ("Patient", "No. I can drink water."),
        ("You", "May I check your temperature and look at your throat?"),
        ("Patient", "Okay."),
        ("You", "Your mild fever, runny nose and cough fit a common cold. The throat has no white patches. Thank you for telling me how you feel."),
        ("Diagnosis", "common cold"),
    ]
    metrics = ConsultationMetrics(started_at=0, finished_at=125)
    metrics.discover_test("Temperature", "38 C, mild fever")
    metrics.discover_test("Throat examination", "Mild redness, no white patches or swelling")
    async with _realtime_authorization() as headers:
        scorecard = await score_consultation(
            transcript, "common cold", "A nervous child with a runny nose, sore throat, cough and tiredness since yesterday.",
            metrics, 2, headers,
        )
    print(json.dumps(asdict(scorecard), indent=2))
    pygame.init()
    animator = PatientAnimator(0, disease="common cold")
    try:
        animator.metrics = metrics
        animator.show_win()
        animator._review_open = True
        animator._available_test_count = 2
        animator._scorecard = scorecard
        animator.draw()
        output.parent.mkdir(parents=True, exist_ok=True)
        pygame.image.save(animator._window, str(output))
        animator._review_scroll = animator._review_max_scroll
        animator.draw()
        pygame.image.save(animator._window, str(output.with_name(f"{output.stem}_bottom{output.suffix}")))
        print(f"Saved scorecard previews to {output.parent}")
    finally:
        animator.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Score one synthetic consultation using MAI-Thinking-1")
    parser.add_argument("--live", action="store_true", help="Send one paid scoring request using Azure sign-in")
    parser.add_argument("--output", type=Path, default=Path(".artifacts/polish/scorecard_live.png"))
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("Pass --live to permit a real scoring request")
    asyncio.run(asyncio.wait_for(run(arguments.output), REVIEW_TIMEOUT_SECONDS))


if __name__ == "__main__":
    main()