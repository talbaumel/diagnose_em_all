import importlib.util
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pygame

from src.hospital_game import HospitalNavigator, PLAYER_SPEED, load_patient_scenarios
from tools.record_demo import RATE, WALK_ROUTE, DemoDirector, ScreenRecorder, TimelineSink, encoder_path


class DirectorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_patient_proposed_activity_does_not_wait_for_idle(self):
        director = DemoDirector(Mock(), Mock())
        director.animator = Mock(_transcript=[], _skill_confirmation=SimpleNamespace(skill_id="temperature"))
        with patch.object(director, "type_text", new_callable=AsyncMock), patch.object(
            director, "complete_test", new_callable=AsyncMock,
        ) as complete_test:
            completed = await director.say("May I check your temperature?", expected_skill="temperature")
        self.assertTrue(completed)
        complete_test.assert_awaited_once_with("temperature")

    async def test_unexpected_test_is_cancelled(self):
        director = DemoDirector(Mock(), Mock())
        director.animator = Mock(_skill_confirmation=SimpleNamespace(skill_id="chest_xray"))
        with patch.object(director, "wait_for", new_callable=AsyncMock), patch.object(director, "key") as key:
            with self.assertRaisesRegex(RuntimeError, "cancelled unexpected test"):
                await director.complete_test("temperature")
        key.assert_called_once_with(pygame.K_ESCAPE)

    async def test_failed_grading_never_marks_demo_complete(self):
        director = DemoDirector(Mock(), Mock())
        director.animator = Mock(_scorecard=None, _review_error="Service unavailable")
        with patch.object(director, "wait_for", new_callable=AsyncMock), patch.object(director, "mark"):
            with self.assertRaisesRegex(RuntimeError, "Live grading failed"):
                await director.review(Mock())
        self.assertFalse(director.review_complete)


@unittest.skipUnless(shutil.which("ffmpeg") or importlib.util.find_spec("imageio_ffmpeg"), "Optional demo encoder not installed")
class ScreenRecorderTests(unittest.TestCase):
    def test_encoded_video_round_trip_and_audio_track(self):
        pygame.init()
        try:
            window = pygame.display.set_mode((480, 480))
            with tempfile.TemporaryDirectory() as directory:
                with patch("tools.record_demo.time.monotonic", return_value=0.0):
                    recorder = ScreenRecorder(Path(directory) / "take", size=480, fps=10)
                    window.fill((220, 40, 50))
                    recorder.capture(window)
                with patch("tools.record_demo.time.monotonic", return_value=0.2):
                    window.fill((30, 160, 70))
                    recorder.capture(window)
                output = recorder.finish()
                decoded = subprocess.run([
                    encoder_path(), "-v", "error", "-i", str(output), "-map", "0:v:0",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                ], check=True, capture_output=True, timeout=30).stdout
                frame_bytes = 480 * 480 * 3
                self.assertEqual(len(decoded), frame_bytes * 3)
                self.assertGreater(decoded[0], 200)
                self.assertGreater(decoded[frame_bytes + 1], 140)
                audio = subprocess.run([
                    encoder_path(), "-v", "error", "-i", str(output), "-map", "0:a:0",
                    "-f", "s16le", "pipe:1",
                ], check=True, capture_output=True, timeout=30).stdout
                self.assertGreaterEqual(len(audio), round(0.3 * RATE) * 2)
                self.assertEqual(set(audio), {0})
        finally:
            pygame.quit()


class HospitalRouteTests(unittest.TestCase):
    def test_route_reaches_kid_without_teleporting(self):
        pygame.init()
        try:
            window = pygame.display.set_mode((480, 480))
            root = Path(__file__).resolve().parents[1]
            scenarios = load_patient_scenarios(sorted((root / "data/prompts").glob("*.json")))
            navigator = HospitalNavigator(scenarios, set(), window=window)
            for destination in WALK_ROUTE:
                target = pygame.Vector2(destination)
                for _ in range(600):
                    delta = target - navigator._player_position
                    if delta.length() <= 0.1:
                        break
                    navigator.update(delta, min(1 / 60, delta.length() / PLAYER_SPEED))
                self.assertLess(navigator._player_position.distance_to(target), 0.1)
            self.assertEqual(navigator.nearest_patient().patient_type.name, "COMMON_COLD_KID")
            self.assertEqual(navigator._diagnosed, set())
        finally:
            pygame.quit()


class TimelineSinkTests(unittest.TestCase):
    def test_silence_and_interruption_are_preserved(self):
        now = [10.0]
        sink = TimelineSink(lambda: now[0])
        sink.begin(b"\x01\x00" * RATE)
        now[0] = 10.25
        self.assertEqual(sink.state(), (True, False, 250))
        sink.abort()
        now[0] = 10.5
        sink.begin(b"\x02\x00" * (RATE // 4))
        now[0] = 11.0
        self.assertEqual(sink.state(), (True, True, 250))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            sink.write_wave(path, 9.5, 2.0)
            with wave.open(str(path), "rb") as source:
                self.assertEqual(source.getparams()[:3], (1, 2, RATE))
                audio = source.readframes(source.getnframes())
        self.assertEqual(audio, b"\x00\x00" * (RATE // 2)
                         + b"\x01\x00" * (RATE // 4)
                         + b"\x00\x00" * (RATE // 4)
                         + b"\x02\x00" * (RATE // 4)
                         + b"\x00\x00" * (RATE * 3 // 4))

    def test_close_does_not_duplicate_audio(self):
        now = [0.0]
        sink = TimelineSink(lambda: now[0])
        sink.begin(b"\x01\x00" * RATE)
        now[0] = 2.0
        sink.close()
        sink.close()
        self.assertEqual(sink.chunks, [(0.0, b"\x01\x00" * RATE)])


if __name__ == "__main__":
    unittest.main()