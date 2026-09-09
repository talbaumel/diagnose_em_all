from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pygame

from src.audio_playback import PlaybackController, Segment
from src.hospital_game import load_patient_scenario
from src.realtime_conversation import PatientAnimator, _combine_prompts, _run_conversation
from tools.preview_performance import RecordingSink


def export_speech(segment: Segment, directory: Path, prompt: str, requested_line: str | None) -> None:
    if segment.kind != "speech" or not segment.pcm or len(segment.pcm) % 2 or not segment.caption.strip():
        raise ValueError("Capture requires a complete speech segment with a transcript and PCM16 audio")
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "speech.wav"
    with wave.open(str(path), "wb") as destination:
        destination.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        destination.writeframes(segment.pcm)
    metadata = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "source_kind": "azure-realtime",
        "patient_index": 0, "system_prompt": prompt, "requested_line": requested_line,
        "actual_transcript": segment.caption, "response_id": segment.response_id,
        "item_id": segment.item_id, "content_index": segment.content_index,
        "scope": "First complete generated speech content part, not necessarily the entire response",
        "local_performance_cues": False, "sample_rate": 24000,
        "duration_seconds": len(segment.pcm) / 48000,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "review_status": "unreviewed; verify words and source rights before use",
    }
    (directory / "source.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


async def smoke(
    speech: Path | None, output: Path, *,
    capture_dir: Path | None = None, line: str | None = None, message: str | None = None,
) -> None:
    if message is not None and (not message.strip() or speech is not None or capture_dir is not None or line is not None):
        raise ValueError("--message requires nonempty text and cannot be combined with --speech or capture mode")
    if line is not None and (capture_dir is None or speech is not None or not line.strip()):
        raise ValueError("--line requires --capture-dir, nonempty text and no --speech")
    if capture_dir is not None and capture_dir.exists():
        raise FileExistsError(f"Use a new capture directory: {capture_dir}")
    root = Path(__file__).resolve().parents[1]
    scenario = load_patient_scenario(root / "data/prompts/01_common_cold_kid.json")
    prompt = _combine_prompts(scenario.system_prompts)
    if line is not None:
        prompt = (
            "You are voicing a young game character for an offline audio experiment. "
            "Repeat exactly the user's supplied line, once, without introductions or sound effects. "
            "Use a comfortable neutral speaking voice, normal breathing and natural phrasing. "
            "Do not act congested, hoarse or ill, even if the words mention symptoms. "
            "Do not call tools."
        )
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
    captured: Segment | None = None
    output_sink: RecordingSink | None = None
    feeders: list[asyncio.Task] = []
    original_add = PatientAnimator.add_transcript
    original_enqueue = PlaybackController.enqueue

    def enqueue(controller: PlaybackController, segment: Segment) -> asyncio.Future[str]:
        nonlocal captured
        ticket = original_enqueue(controller, segment)
        if (capture_dir is not None and captured is None and segment.kind == "speech"
                and segment.generation == controller.generation):
            captured = segment
        return ticket

    class SilentOutput(RecordingSink):
        def __init__(self) -> None:
            nonlocal output_sink
            super().__init__()
            output_sink = self

        def begin(self, pcm: bytes) -> None:
            nonlocal audio_bytes
            super().begin(pcm)
            audio_bytes += len(pcm)

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
                    animator._text_input = message or line or "Hello. What symptoms have you been experiencing?"
                    animator._submit_text()
            animator.draw(time.monotonic() - started)
            drained = output_sink is not None and output_sink.state()[1]
            if response_done and audio_bytes and (capture_dir is not None or drained):
                output.parent.mkdir(parents=True, exist_ok=True)
                pygame.image.save(animator._window, str(output))
                stop.set()
                return
            await asyncio.sleep(1 / 60)

    try:
        with (
            patch("src.realtime_conversation._microphone_stream", input_stream),
            patch("src.realtime_conversation.DeviceSink", SilentOutput),
            patch.object(PatientAnimator, "run", run_ui),
            patch.object(PatientAnimator, "add_transcript", add_transcript),
            patch.object(PlaybackController, "enqueue", enqueue),
        ):
            await asyncio.wait_for(_run_conversation(
                prompt, scenario.disease, 0, [] if capture_dir is not None else scenario.tests,
                performance_profile=None if capture_dir is not None else scenario.performance_profile,
            ), 75)
        if not response_done or not audio_bytes:
            raise RuntimeError("No complete patient transcript and audio response received")
        if capture_dir is not None:
            if captured is None:
                raise RuntimeError("No complete generated speech segment was available to capture")
            export_speech(captured, capture_dir, prompt, line)
            print(f"Saved complete generated content part to {capture_dir}; check source.json for actual words")
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
    parser.add_argument("--capture-dir", type=Path, help="Save the first complete speech content part; disables local cues/tests")
    parser.add_argument("--line", help="With --capture-dir, request this exact line in a neutral voice")
    parser.add_argument("--message", help="Send a specific clinician request through the normal game profile")
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("Pass --live to send one consultation request to the configured Azure deployment")
    asyncio.run(smoke(arguments.speech, arguments.output, capture_dir=arguments.capture_dir,
                      line=arguments.line, message=arguments.message))


if __name__ == "__main__":
    main()