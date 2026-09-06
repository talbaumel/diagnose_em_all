from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from src.patient_performance import PerformanceProfile

HAS_AUDIO_DEPS = all(importlib.util.find_spec(name) is not None for name in ("numpy", "pyworld"))
ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets/audio/cue_candidates"


class CandidateManifestTests(unittest.TestCase):
    def test_sources_are_licensed_preserved_and_not_enabled(self):
        manifest = json.loads((ASSETS / "SOURCES.json").read_text())
        self.assertFalse(manifest["approved_for_gameplay"])
        self.assertEqual(manifest["status"], "prepared_for_review")
        self.assertEqual(manifest["dataset_license"], "CC-BY-4.0")
        self.assertEqual(len(manifest["sources"]), 9)
        for source in manifest["sources"].values():
            path = ASSETS / source["file"]
            self.assertEqual(source["clip_license"], "CC0-1.0")
            self.assertFalse(source["approved_for_gameplay"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source["sha256"])
            self.assertIn(manifest["mirror_revision"], source["download_url"])
            self.assertTrue(source["creator"])
        rejected = {key for key, source in manifest["sources"].items()
                    if source["technical_status"] == "rejected"}
        self.assertEqual(rejected, {"368494", "167642", "120793"})
        self.assertTrue(all(cue["source_id"] not in rejected for cue in manifest["cues"]))
        for cue in manifest["cues"]:
            with self.subTest(cue=cue["id"]):
                self.assertFalse(cue["approved_for_gameplay"])
                self.assertEqual(cue["review_status"], "unreviewed")
                with self.assertRaises(ValueError):
                    PerformanceProfile(cough_clip=f"assets/audio/cue_candidates/{cue['file']}")

    def test_prepared_files_match_manifest(self):
        manifest = json.loads((ASSETS / "SOURCES.json").read_text())
        self.assertEqual(len(manifest["cues"]), 7)
        for item in manifest["cues"]:
            path = ASSETS / item["file"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
            with wave.open(str(path), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()),
                                 (1, 2, 24000))
                self.assertEqual(audio.getnframes(), item["samples"])
            self.assertEqual(item["full_scale_samples"], 0)
        for cue in manifest["cues"]:
            self.assertLessEqual(cue["duration_seconds"], 10)
            self.assertLessEqual(cue["gain_db"], 12.00001)
            self.assertLessEqual(cue["peak_dbfs"], -2.99)
            with wave.open(str(ASSETS / cue["file"]), "rb") as audio:
                pcm = audio.readframes(audio.getnframes())
                self.assertEqual(pcm[:2], b"\0\0")
                self.assertEqual(pcm[-2:], b"\0\0")
        self.assertEqual([entry["cue"] for entry in manifest["auditions"]["cues_only"]["timeline"]],
                         [cue["id"] for cue in manifest["cues"]])


@unittest.skipUnless(HAS_AUDIO_DEPS, "Install requirements-voice.txt to test candidate processing")
class CuePreparationTests(unittest.TestCase):
    def test_gain_headroom_and_invalid_inputs(self):
        import numpy as np
        from tools.prepare_cue_candidates import prepare_levels
        time = np.arange(24000) / 24000
        signal = np.sin(2 * np.pi * 200 * time).astype(np.float64)
        for amplitude in (1., .01, .0001):
            with self.subTest(amplitude=amplitude):
                output, metadata = prepare_levels(signal * amplitude, -32)
                self.assertLessEqual(np.max(np.abs(output)), 10 ** (-3 / 20))
                self.assertLessEqual(metadata["gain_db"], 12.00001)
                self.assertEqual(output[0], 0)
                self.assertEqual(output[-1], 0)
        for audio, target in ((np.zeros(24000), -32), (signal, float("nan")),
                              (signal, -10), (signal * float("nan"), -32)):
            with self.assertRaises(ValueError):
                prepare_levels(audio, target)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for cue preparation")
    def test_builder_verifies_license_and_never_overwrites(self):
        import numpy as np
        from tools.prepare_cue_candidates import build
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "originals").mkdir()
            source = root / "originals/1.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
                signal = .1 * np.sin(2*np.pi*200*np.arange(44100)/44100)
                wav.writeframes(np.rint(signal*32767).astype("<i2").tobytes())
            voice = root / "voice.wav"
            with wave.open(str(voice), "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\1\0" * 24000)
            plan = {
                "source_ids": ["1"], "rejected_sources": {}, "mirror": "https://example.org",
                "mirror_revision": "test", "dataset": "test", "dataset_url": "https://example.org",
                "metadata_url": "https://example.org/metadata", "selection_method": "test",
                "cues": [{"id": "sniffle_01", "kind": "sniffle", "source_id": "1",
                          "start_seconds": .1, "end_seconds": .8, "target_active_rms_dbfs": -32}],
            }
            (root / "PREPARATION.json").write_text(json.dumps(plan))
            archive = root / "metadata.zip"
            info = {"1": {"license": "http://creativecommons.org/licenses/by-nc/3.0/",
                          "title": "test", "uploader": "test", "description": "test", "tags": []}}
            def save_metadata():
                with zipfile.ZipFile(archive, "w") as zip_file:
                    zip_file.writestr("FSD50K.metadata/dev_clips_info_FSD50K.json", json.dumps(info))
            save_metadata()
            with self.assertRaisesRegex(ValueError, "not explicitly CC0"):
                build(root, archive, voice)
            self.assertFalse((root / "prepared").exists())
            info["1"]["license"] = "http://creativecommons.org/publicdomain/zero/1.0/"
            save_metadata()
            before = source.read_bytes()
            result = build(root, archive, voice)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(result["approved_for_gameplay"])
            self.assertEqual(len(result["cues"]), 1)
            for item in result["auditions"].values():
                path = root / item["file"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
                with wave.open(str(path), "rb") as audio:
                    self.assertEqual(audio.getnframes(), item["samples"])
            review = json.loads((root / "audition/review.json").read_text())
            self.assertEqual(len(review["items"]), 1)
            self.assertIsNone(review["items"][0]["matches_kid"])
            self.assertFalse(review["items"][0]["approved_for_gameplay"])
            with self.assertRaises(FileExistsError):
                build(root, archive, voice)


if __name__ == "__main__":
    unittest.main()
