"""Exercise the real playback queue offline, without Azure or microphone capture."""

from __future__ import annotations

import argparse
import asyncio
import time
import wave
from pathlib import Path

from src.audio_playback import AudioSink, DeviceSink, PlaybackController, Segment
from src.patient_performance import COUGH_CLIP, PROJECT_ROOT, read_pcm_clip


class RecordingSink:
    """A silent, real-time sink for smoke tests and offline PCM export."""

    def __init__(self) -> None:
        self.pcm = b""
        self.started_at = 0.0
        self.recorded = bytearray()

    def begin(self, pcm: bytes) -> None:
        self.pcm = pcm
        self.started_at = time.monotonic()
        self.recorded.extend(pcm)

    def state(self) -> tuple[bool, bool, int]:
        heard = min(len(self.pcm), int((time.monotonic() - self.started_at) * 48000))
        return True, heard >= len(self.pcm), heard // 48

    def abort(self) -> None:
        heard = min(len(self.pcm), int((time.monotonic() - self.started_at) * 48000))
        unheard = max(0, len(self.pcm) - heard)
        if unheard:
            del self.recorded[-unheard:]
        self.pcm = b""

    def close(self) -> None:
        self.abort()


async def preview(before: Path | None, after: Path | None, output: Path | None, play: bool) -> None:
    segments = [
        (read_pcm_clip(before) if before else bytes(7200), "Before speech" if before else "[silent lead-in]", "speech"),
        (read_pcm_clip(PROJECT_ROOT / COUGH_CLIP), "[coughs]", "cough"),
        (read_pcm_clip(after) if after else bytes(7200), "After speech" if after else "[silent lead-out]", "speech"),
    ]
    sink: AudioSink = DeviceSink() if play else RecordingSink()
    started_at = time.monotonic()

    def started(segment: Segment) -> None:
        print(f"{time.monotonic() - started_at:.3f}s START {segment.caption}")

    def ended(segment: Segment, status: str) -> None:
        print(f"{time.monotonic() - started_at:.3f}s END {segment.caption}: {status}")

    controller = PlaybackController(sink, started, ended)
    worker = asyncio.create_task(controller.run())
    try:
        tickets = [
            controller.enqueue(Segment(0, "preview", str(index), pcm, caption, kind))
            for index, (pcm, caption, kind) in enumerate(segments)
        ]
        completion = asyncio.gather(*tickets)
        done, _ = await asyncio.wait({worker, completion}, timeout=35, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            worker.result()
        if completion not in done:
            raise TimeoutError("Offline performance playback did not finish")
        assert completion.result() == ["played"] * 3
        if output is not None:
            if not isinstance(sink, RecordingSink):
                raise ValueError("Use --output without --play for a silent PCM capture")
            output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output), "wb") as recording:
                recording.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                recording.writeframes(sink.recorded)
            print(f"Saved offline audio to {output}")
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        sink.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, help="Optional mono 24 kHz PCM16 speech WAV, max 10 seconds")
    parser.add_argument("--after", type=Path, help="Optional mono 24 kHz PCM16 speech WAV, max 10 seconds")
    parser.add_argument("--output", type=Path, help="Save silent-run PCM as a WAV")
    parser.add_argument("--play", action="store_true", help="Play through the real output device")
    args = parser.parse_args()
    if args.play and args.output:
        parser.error("Choose --play or --output, not both")
    asyncio.run(preview(args.before, args.after, args.output, args.play))


if __name__ == "__main__":
    main()
