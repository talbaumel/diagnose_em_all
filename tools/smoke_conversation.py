from __future__ import annotations

import argparse
import asyncio
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pygame

from src.hospital_game import load_patient_scenario
from src.realtime_conversation import PatientAnimator, _run_conversation


async def smoke(speech: Path | None, output: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    scenario = load_patient_scenario(root / "data/prompts/01_common_cold_kid.json")
    audio_input = b""
    if speech is not None:
        with wave.open(str(speech), "rb") as source:
            if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (24000, 1, 2):
                raise ValueError("Speech must be mono 24 kHz signed 16-bit PCM WAV")
            if source.getnframes() > 24000 * 30:
                raise ValueError("Speech must be shorter than 30 seconds")
            audio_input = source.readframes(source.getnframes())
    active: PatientAnimator | None = None
    response_done = False
    audio_bytes = 0
    feeders: list[asyncio.Task] = []
    original_add = PatientAnimator.add_transcript

    class SilentOutput:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def write(self, audio):
            nonlocal audio_bytes
            audio_bytes += len(audio)

        def abort(self):
            pass

    @contextmanager
    def input_stream(callback):
        async def feed():
            while active is None or not active._ready:
                await asyncio.sleep(1 / 60)
            active._push_to_talk.set()
            for offset in range(0, len(audio_input), 4800):
                callback(audio_input[offset:offset + 4800].ljust(4800, b"\x00"), 2400, None, None)
                await asyncio.sleep(0.1)
            active._push_to_talk.clear()
            for frame in range(15):
                callback(b"\x00" * 4800, 2400, None, None)
                await asyncio.sleep(0.1)
        task = asyncio.create_task(feed()) if audio_input else None
        if task:
            feeders.append(task)
        try:
            yield object() if task else None
        finally:
            if task:
                task.cancel()

    def add_transcript(animator, speaker, text, item_id=None, *, append=False):
        nonlocal response_done
        original_add(animator, speaker, text, item_id, append=append)
        if speaker == "Patient" and text and not append:
            response_done = True

    async def run_ui(animator, stop):
        nonlocal active
        active = animator
        submitted = False
        started = time.monotonic()
        while not stop.is_set():
            for event in pygame.event.get():
                animator.handle_event(event, stop)
            if animator._menu is not None:
                raise RuntimeError("Live consultation could not connect; see the preceding service error")
            if animator._ready and not submitted:
                submitted = True
                if not audio_input:
                    animator._text_input = "Hello. What symptoms have you been experiencing?"
                    animator._submit_text()
            animator.draw(time.monotonic() - started)
            if response_done and audio_bytes:
                output.parent.mkdir(parents=True, exist_ok=True)
                pygame.image.save(animator._window, str(output))
                stop.set()
                return
            await asyncio.sleep(1 / 60)

    try:
        with (
            patch("src.realtime_conversation._microphone_stream", input_stream),
            patch("src.realtime_conversation.sd.RawOutputStream", SilentOutput),
            patch.object(PatientAnimator, "run", run_ui),
            patch.object(PatientAnimator, "add_transcript", add_transcript),
        ):
            await asyncio.wait_for(_run_conversation(scenario.system_prompts, scenario.disease, 0, scenario.tests), 75)
        if not response_done or not audio_bytes:
            raise RuntimeError("No complete patient transcript and audio response received")
        print(f"Live {'voice' if audio_input else 'text'} smoke passed: {audio_bytes} output audio bytes; screenshot {output}")
    finally:
        for task in feeders:
            task.cancel()
        await asyncio.gather(*feeders, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit opt-in live Realtime check without microphone capture or speaker playback")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--speech", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/polish/live.png"))
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("Pass --live to send one consultation request to the configured Azure deployment")
    asyncio.run(smoke(arguments.speech, arguments.output))


if __name__ == "__main__":
    main()