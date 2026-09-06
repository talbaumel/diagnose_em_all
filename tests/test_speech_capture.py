from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from src.audio_playback import PlaybackController, Segment
from tools.preview_performance import RecordingSink
from tools.smoke_conversation import export_speech, smoke


class SpeechExportTests(unittest.TestCase):
    def test_saves_exact_generated_pcm_not_played_excerpt(self):
        segment = Segment(0, "response", "item", b"\x01\x02" * 24000, "My nose feels blocked.")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "capture"
            export_speech(segment, directory, "Neutral prompt", "Requested line")
            with wave.open(str(directory / "speech.wav"), "rb") as audio:
                self.assertEqual(audio.getparams()[:3], (1, 2, 24000))
                self.assertEqual(audio.readframes(audio.getnframes()), segment.pcm)
            source = json.loads((directory / "source.json").read_text())
            self.assertEqual(source["actual_transcript"], segment.caption)
            self.assertEqual(source["duration_seconds"], 1.0)
            self.assertEqual(source["sha256"], hashlib.sha256((directory / "speech.wav").read_bytes()).hexdigest())
            self.assertFalse(source["local_performance_cues"])
            with self.assertRaises(FileExistsError):
                export_speech(segment, directory, "New prompt", None)

    def test_rejects_cues_empty_audio_and_missing_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            for pcm, caption, kind in ((b"\0\0", "[coughs]", "cough"), (b"", "Words", "speech"),
                                       (b"\0", "Words", "speech"), (b"\0\0", "", "speech")):
                with self.subTest(pcm=pcm, caption=caption):
                    directory = Path(temporary) / "capture"
                    with self.assertRaises(ValueError):
                        export_speech(Segment(0, "r", "i", pcm, caption, kind), directory, "Prompt", None)
                    self.assertFalse(directory.exists())


class CaptureSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_path_disables_cues_and_keeps_whole_content_part(self):
        segment = Segment(0, "response", "item", b"\1\0" * 24000, "My nose feels blocked.")

        async def connected(prompt, disease, patient_index, tests, *, performance_profile):
            self.assertIsNone(performance_profile)
            self.assertEqual(tests, [])
            self.assertIn("neutral", prompt)
            self.assertEqual(patient_index, 0)
            from src.realtime_conversation import DeviceSink, PatientAnimator
            controller = PlaybackController(RecordingSink(), lambda item: None, lambda item, status: None)
            controller.enqueue(Segment(-1, "old", "stale", b"\2\0" * 20, "Stale"))
            controller.enqueue(segment)
            PatientAnimator.add_transcript(Mock(spec=PatientAnimator), "Patient", segment.caption)
            sink = DeviceSink()
            sink.begin(segment.pcm)
            sink.abort()

        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary) / "captured"
            with (
                patch("tools.smoke_conversation._run_conversation", connected),
                patch("tools.smoke_conversation.PatientAnimator.add_transcript"),
            ):
                await smoke(None, Path(temporary) / "unused.png", capture_dir=capture,
                            line="My nose feels blocked.")
            with wave.open(str(capture / "speech.wav"), "rb") as saved:
                self.assertEqual(saved.readframes(saved.getnframes()), segment.pcm)

    async def test_invalid_capture_arguments_fail_before_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with patch("tools.smoke_conversation._run_conversation") as connection:
                with self.assertRaises(ValueError):
                    await smoke(None, output, line="Some words")
                with self.assertRaises(FileExistsError):
                    await smoke(None, output, capture_dir=output)
                with self.assertRaises(ValueError):
                    await smoke(None, output, message=" ")
                with self.assertRaises(ValueError):
                    await smoke(None, output, capture_dir=output, message="Sniff please")
                connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
