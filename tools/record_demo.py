"""Record the actual game display and live patient audio without touching saves."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
import wave
from contextlib import nullcontext
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pygame

from src.conversation_runtime import ConversationRuntime
from src.hospital_game import HospitalNavigator, PLAYER_SPEED, load_patient_scenarios
from src.realtime_conversation import (
    ConversationResult, PatientAnimator, _combine_prompts, _run_conversation,
)
from src.skill_activity import InstrumentActivity


RATE = 24000
WALK_ROUTE = ((225, 480), (225, 360))


def encoder_path() -> str:
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    try:
        imageio_ffmpeg = import_module("imageio_ffmpeg")
    except ImportError as error:
        raise RuntimeError("Install FFmpeg or imageio-ffmpeg to record a demo.") from error
    return imageio_ffmpeg.get_ffmpeg_exe()


class TimelineSink:
    """A real-time audio sink that records only the samples actually played."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.chunks: list[tuple[float, bytes]] = []
        self.pcm = b""
        self.started_at = 0.0

    def begin(self, pcm: bytes) -> None:
        self.abort()
        self.pcm = pcm
        self.started_at = self.clock()

    def state(self) -> tuple[bool, bool, int]:
        samples = min(len(self.pcm) // 2, max(0, int((self.clock() - self.started_at) * RATE)))
        return bool(self.pcm), samples * 2 >= len(self.pcm), samples // 24

    def abort(self) -> None:
        if self.pcm:
            samples = min(len(self.pcm) // 2, max(0, int((self.clock() - self.started_at) * RATE)))
            if samples:
                self.chunks.append((self.started_at, self.pcm[:samples * 2]))
        self.pcm = b""

    def close(self) -> None:
        self.abort()

    def write_wave(self, path: Path, started_at: float, duration: float) -> None:
        self.abort()
        audio = bytearray(round(duration * RATE) * 2)
        for timestamp, pcm in self.chunks:
            sample_offset = round((timestamp - started_at) * RATE)
            source_offset = max(0, -sample_offset) * 2
            destination_offset = max(0, sample_offset) * 2
            count = min(len(pcm) - source_offset, len(audio) - destination_offset)
            if count > 0:
                audio[destination_offset:destination_offset + count] = pcm[source_offset:source_offset + count]
        with wave.open(str(path), "wb") as destination:
            destination.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            destination.writeframes(audio)


class ScreenRecorder:
    def __init__(self, directory: Path, size: int = 960, fps: int = 30) -> None:
        if size < 480 or size % 2 or fps < 1:
            raise ValueError("Use an even size of at least 480 pixels and a positive frame rate.")
        executable = encoder_path()
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.fps = fps
        self.size = size
        self.frames = 0
        self.started_at = time.monotonic()
        self.sink = TimelineSink()
        self.log = (directory / "encoder.log").open("wb")
        self.process = subprocess.Popen([
            executable, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
            "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{size}x{size}",
            "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(directory / "video.mp4"),
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)

    def capture(self, window: pygame.Surface) -> None:
        expected = int((time.monotonic() - self.started_at) * self.fps) + 1
        if expected <= self.frames:
            return
        if window.get_size() != (self.size, self.size):
            raise ValueError("The game window changed size during recording.")
        pixels = pygame.image.tobytes(window, "RGB")
        assert self.process.stdin is not None
        while self.frames < expected:
            self.process.stdin.write(pixels)
            self.frames += 1

    def finish(self) -> Path:
        assert self.process.stdin is not None
        self.process.stdin.close()
        try:
            return_code = self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise
        finally:
            self.log.close()
        if return_code or not self.frames:
            raise RuntimeError(f"Video encoder failed; see {self.directory / 'encoder.log'}")
        self.sink.write_wave(self.directory / "audio.wav", self.started_at, self.frames / self.fps)
        output = self.directory / "common-cold-demo.mp4"
        subprocess.run([
            encoder_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(self.directory / "video.mp4"), "-i", str(self.directory / "audio.wav"),
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            "-shortest", str(output),
        ], check=True, timeout=120)
        return output


class DemoDirector:
    def __init__(self, recorder: ScreenRecorder, window: pygame.Surface) -> None:
        self.recorder = recorder
        self.window = window
        self.runtime: ConversationRuntime | None = None
        self.animator: PatientAnimator | None = None
        self.markers: list[dict] = []
        self.review_complete = False

    def mark(self, stage: str) -> None:
        seconds = round(time.monotonic() - self.recorder.started_at, 3)
        self.markers.append({"seconds": seconds, "stage": stage})
        print(f"[{seconds:7.2f}s] {stage}", flush=True)
        pygame.image.save(self.window, str(self.recorder.directory / f"{len(self.markers):02d}-{stage}.png"))

    async def wait_for(self, predicate: Callable[[], bool], label: str, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if self.animator is not None and self.animator._menu is not None:
                raise RuntimeError(f"{label}: {self.animator._menu.title}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for {label}")
            await asyncio.sleep(0.03)

    def key(self, key: int) -> None:
        assert self.animator is not None
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key, mod=0), asyncio.Event())

    def idle(self) -> bool:
        runtime = self.runtime
        return bool(runtime is not None and runtime.idle.is_set()
                    and runtime.user_requests.empty() and not runtime.pending_speech
                    and runtime.playback.active is None and runtime.playback.queue.empty()
                    and not runtime.pending_skills and not runtime.animator._sending
                    and not runtime.animator._local_skill_busy)

    async def type_text(self, text: str, *, helper: bool = False) -> None:
        assert self.animator is not None
        if helper:
            self.animator._pokedex.focus(True)
        else:
            self.animator._set_text_focus(True)
        for character in text:
            self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=character), asyncio.Event())
            await asyncio.sleep(0.025)
        await asyncio.sleep(0.6)
        self.key(pygame.K_RETURN)

    async def say(self, text: str, *, expected_skill: str | None = None) -> bool:
        assert self.animator is not None
        animator = self.animator
        previous = len(animator._transcript)
        await self.type_text(text)
        await self.wait_for(
            lambda: (any(speaker == "Patient" for speaker, _ in animator._transcript[previous:])
                     and self.idle()) or (expected_skill is not None and animator._skill_confirmation is not None),
            "patient response or requested examination",
        )
        if expected_skill is not None and animator._skill_confirmation is not None:
            await self.complete_test(expected_skill)
            return True
        await asyncio.sleep(1.0)
        return False

    async def walk(self, navigator: HospitalNavigator) -> None:
        self.mark("hospital-walk")
        for destination in WALK_ROUTE:
            target = pygame.Vector2(destination)
            deadline = time.monotonic() + 12
            while navigator._player_position.distance_to(target) > 0.1:
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        raise RuntimeError("Recording cancelled by closing the game window")
                delta = target - navigator._player_position
                navigator.update(delta, min(1 / 60, delta.length() / PLAYER_SPEED))
                navigator.draw()
                self.recorder.capture(self.window)
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Hospital route blocked before {destination}")
                await asyncio.sleep(1 / 60)
        navigator.update(pygame.Vector2(), 0)
        patient = navigator.nearest_patient()
        if patient is None or patient.patient_type.name != "COMMON_COLD_KID":
            raise RuntimeError("Dr. Ash did not reach the common-cold kid")
        for _ in range(75):
            navigator.update(pygame.Vector2(), 1 / 60)
            navigator.draw()
            self.recorder.capture(self.window)
            await asyncio.sleep(1 / 60)
        self.mark("meet-common-cold-kid")

    async def complete_test(self, expected: str) -> None:
        assert self.animator is not None
        animator = self.animator
        await self.wait_for(lambda: animator._skill_confirmation is not None, f"{expected} confirmation")
        activity = animator._skill_confirmation
        assert activity is not None
        if activity.skill_id != expected:
            self.key(pygame.K_ESCAPE)
            raise RuntimeError(f"Expected {expected}, received {activity.skill_id}; cancelled unexpected test")
        self.mark(f"{expected}-interaction")
        await asyncio.sleep(1.5)
        if isinstance(activity, InstrumentActivity):
            if activity.state == "idle":
                self.key(pygame.K_RETURN)
            origin = pygame.Vector2(activity.tip)
            target = pygame.Vector2(activity.target_rect.center)
            for index in range(45):
                tip = origin.lerp(target, (index + 1) / 45)
                position = tuple(round(value * self.recorder.size / 480) for value in tip)
                animator.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=position), asyncio.Event())
                await asyncio.sleep(1 / 30)
            self.key(pygame.K_RETURN)
        else:
            self.key(pygame.K_TAB)
            await asyncio.sleep(0.6)
            self.key(pygame.K_RETURN)
        await self.wait_for(lambda: animator.evidence_open, f"{expected} result")
        await asyncio.sleep(0.2)
        self.mark(f"{expected}-result")
        await asyncio.sleep(4)
        self.key(pygame.K_d)
        await asyncio.sleep(2.5)
        self.key(pygame.K_RETURN)
        await self.wait_for(self.idle, f"{expected} completion")
        await asyncio.sleep(0.8)

    async def consultation(self) -> None:
        assert self.animator is not None
        animator = self.animator
        await self.wait_for(lambda: animator._ready, "patient connection")
        self.mark("ask-symptoms")
        await self.say("Hi! I'm Dr. Ash. What symptoms have you been having?")
        await self.say("I'm sorry you're feeling poorly. Is your throat sore, and have you been coughing?")
        await self.say("Are you breathing comfortably, and can you drink water?")
        self.key(pygame.K_F6)
        if not animator._pokedex.open:
            raise RuntimeError("Dragon helper did not open")
        self.mark("ask-dragon-copilot")
        await self.type_text(
            "What bedside tests do you recommend for this child's symptoms? "
            "Would a temperature check and throat examination be useful? "
            "Please give a brief recommendation without the diagnosis.", helper=True,
        )
        await self.wait_for(lambda: not animator._pokedex.busy, "Dragon recommendation", 150)
        if not any(speaker == "Dragon Simulator Assist" for speaker, _ in animator._pokedex.messages):
            raise RuntimeError(f"Dragon helper failed: {animator._pokedex.status}")
        animator._pokedex.scroll = animator._pokedex.max_scroll
        await asyncio.sleep(0.2)
        self.mark("dragon-recommendations")
        await asyncio.sleep(5)
        while animator._pokedex.scroll > 0:
            animator.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1), asyncio.Event())
            await asyncio.sleep(1.2)
        await asyncio.sleep(3)
        self.key(pygame.K_F6)
        self.key(pygame.K_F7)
        await asyncio.sleep(1.5)
        self.mark("equipment-drawer")
        self.key(pygame.K_ESCAPE)
        if not await self.say("May I gently check your temperature?", expected_skill="temperature"):
            animator._request_local_skill("temperature")
            await self.complete_test("temperature")
        if not await self.say("May I gently examine your throat? Please open your mouth and say ahh.",
                              expected_skill="throat_examination"):
            await self.type_text("It won't hurt. I'll gently examine your throat now.")
            await self.complete_test("throat_examination")
        self.mark("diagnose-common-cold")
        await self.type_text("You have a common cold.")
        await self.wait_for(lambda: animator.won, "automatic diagnosis win")
        await asyncio.sleep(0.3)
        self.mark("you-win")

    async def review(self, stop: asyncio.Event) -> None:
        assert self.animator is not None
        animator = self.animator
        self.mark("grading-loading")
        await self.wait_for(lambda: not animator._review_loading, "live grading", 150)
        if animator._scorecard is None or animator._review_error:
            raise RuntimeError(f"Live grading failed: {animator._review_error}")
        await asyncio.sleep(0.2)
        self.mark("grading-top")
        await asyncio.sleep(6)
        while animator._review_scroll < animator._review_max_scroll:
            animator.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1), stop)
            await asyncio.sleep(0.8)
        self.mark("grading-bottom")
        await asyncio.sleep(6)
        self.review_complete = True
        stop.set()

    async def run_ui(self, animator: PatientAnimator, stop: asyncio.Event) -> None:
        self.animator = animator
        script = asyncio.create_task(self.review(stop) if animator._review_open else self.consultation())
        started_at = time.monotonic()
        try:
            while not stop.is_set():
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        raise RuntimeError("Recording cancelled by closing the game window")
                animator.draw(time.monotonic() - started_at)
                self.recorder.capture(self.window)
                if script.done():
                    script.result()
                await asyncio.sleep(1 / 60)
        finally:
            script.cancel()
            await asyncio.gather(script, return_exceptions=True)

    def report(self, complete: bool, error: str = "") -> dict:
        animator = self.animator
        return {
            "complete": complete, "error": error, "response_source": "live services, not scripted answers",
            "capture": "Pygame display frames with time-aligned patient PCM; no desktop or microphone capture",
            "doctor_input": "scripted typing", "fps": self.recorder.fps,
            "duration_seconds": self.recorder.frames / self.recorder.fps,
            "markers": self.markers,
            "transcript": animator._transcript if animator else [],
            "dragon": animator._pokedex.messages if animator else [],
            "findings": animator.metrics.discovered_tests if animator else {},
            "skill_requests": animator.metrics.skill_requests if animator else [],
            "scorecard": asdict(animator._scorecard) if animator and animator._scorecard else None,
        }


async def record(directory: Path, *, size: int = 960, fps: int = 30, check: bool = False) -> Path:
    root = Path(__file__).resolve().parents[1]
    scenarios = load_patient_scenarios(sorted((root / "data/prompts").glob("*.json")))
    pygame.init()
    window = pygame.display.set_mode((size, size))
    navigator = HospitalNavigator(scenarios, set(), window=window)
    navigator._scene_started_at = -1000
    navigator.draw()
    recorder = ScreenRecorder(directory, size, fps)
    director = DemoDirector(recorder, window)
    complete = False
    error = ""

    class ObservedRuntime(ConversationRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            director.runtime = self

    try:
        await director.walk(navigator)
        if not check:
            scenario = navigator.nearest_patient()
            assert scenario is not None
            with (
                patch("src.realtime_conversation._microphone_stream", lambda callback: nullcontext(None)),
                patch("src.realtime_conversation.DeviceSink", lambda: recorder.sink),
                patch("src.realtime_conversation.ConversationRuntime", ObservedRuntime),
                patch.object(PatientAnimator, "run", lambda animator, stop: director.run_ui(animator, stop)),
            ):
                result = await asyncio.wait_for(_run_conversation(
                    _combine_prompts(scenario.system_prompts), scenario.disease, 0, scenario.tests,
                    window=window, performance_profile=scenario.performance_profile,
                ), 600)
            if result != ConversationResult.SOLVED or not director.review_complete:
                raise RuntimeError("Demo did not complete the live win and grading sequence")
        complete = True
    except BaseException as failure:
        error = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        recorder.capture(window)
        try:
            output = recorder.finish()
            report = director.report(complete, error)
            report["capture_check_only"] = check
            (directory / "run.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"{'Saved' if complete else 'Saved incomplete recording to'} {output}", flush=True)
        finally:
            pygame.quit()
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use real patient, Dragon, and grading services")
    parser.add_argument("--check", action="store_true", help="Record only the hospital walk; no network calls")
    parser.add_argument("--headless", action="store_true", help="Capture the game display without a visible OS window")
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/demos") / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--size", type=int, default=960)
    parser.add_argument("--fps", type=int, default=30)
    arguments = parser.parse_args()
    if arguments.live == arguments.check:
        parser.error("Choose exactly one of --live or --check")
    if arguments.headless:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["SDL_AUDIODRIVER"] = "dummy"
    asyncio.run(record(arguments.output_dir, size=arguments.size, fps=arguments.fps, check=arguments.check))


if __name__ == "__main__":
    main()