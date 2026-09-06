from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path

try:
    import numpy as np

    from src.voice_processing import (
        Analysis, RATE, active_mask, analyze, match_levels, modified_aperiodicity,
        pcm16, resynthesize, voiced_edge_weights,
    )
    from tools.audition_voice import (
        CALIBRATION_PRESETS, PRESETS, REFINEMENT_PRESETS, audition, difference_metrics,
        read_speech, summarize_analysis,
    )
except ModuleNotFoundError as error:
    if error.name not in {"numpy", "pyworld"}:
        raise
    raise unittest.SkipTest("Run with the optional requirements-audition.txt environment") from error


class VoiceAuditionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        time = np.arange(RATE, dtype=np.float64) / RATE
        self.signal = np.sum(
            [0.12 / harmonic * np.sin(2 * np.pi * 180 * harmonic * time)
             for harmonic in range(1, 9)], axis=0,
        )
        self.source = self.root / "source.wav"
        self.save(self.source, self.signal)

    def save(self, path, signal, rate=24000, channels=1):
        with wave.open(str(path), "wb") as output:
            output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
            output.writeframes(np.rint(signal * 32767).astype("<i2").tobytes())

    def test_input_validation(self):
        self.assertEqual(len(read_speech(self.source)), RATE)
        for name, signal, rate, channels in (
            ("rate", self.signal, 16000, 1),
            ("stereo", self.signal, RATE, 2),
            ("short", self.signal[:100], RATE, 1),
            ("silence", np.zeros(RATE), RATE, 1),
            ("long", np.ones(RATE * 31) * 0.1, RATE, 1),
        ):
            with self.subTest(name=name):
                path = self.root / f"{name}.wav"
                self.save(path, signal, rate, channels)
                with self.assertRaises(ValueError):
                    read_speech(path)

    def test_rejects_truncated_wav(self):
        self.source.write_bytes(self.source.read_bytes()[:-20])
        with self.assertRaisesRegex(ValueError, "truncated"):
            read_speech(self.source)

    def test_zero_strength_control_and_unvoiced_frames_are_untouched(self):
        ap = np.full((3, 513), 0.1)
        analysis = Analysis(np.array([180., 0., 180.]), np.ones_like(ap), ap, RATE, 0)
        control = modified_aperiodicity(analysis, 0)
        np.testing.assert_array_equal(control, ap)
        changed = modified_aperiodicity(analysis, 0.12)
        np.testing.assert_array_equal(changed[1], ap[1])
        np.testing.assert_array_equal(changed[:, 0], ap[:, 0])
        self.assertGreater(changed[0, -1], ap[0, -1])
        self.assertTrue(np.all(changed < 1))
        np.testing.assert_array_equal(analysis.aperiodicity, ap)
        for value in (-0.01, 0.21, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                modified_aperiodicity(analysis, value)

    def test_real_world_resynthesis_duration_and_nonidentity(self):
        speech = read_speech(self.source)
        analysis = analyze(speech)
        f0 = analysis.f0.copy()
        spectral = analysis.spectral_envelope.copy()
        control, timing = resynthesize(analysis, PRESETS[0])
        changed, _ = resynthesize(analysis, PRESETS[2])
        self.assertEqual(len(control), len(speech))
        self.assertEqual(len(changed), len(speech))
        self.assertGreater(timing["total_processor_ms"], 0)
        self.assertFalse(np.array_equal(control, changed))
        np.testing.assert_array_equal(analysis.f0, f0)
        np.testing.assert_array_equal(analysis.spectral_envelope, spectral)

    def test_broad_calibration_reaches_low_harmonics_without_changing_unvoiced_frames(self):
        ap = np.full((3, 513), 0.1)
        analysis = Analysis(np.array([180., 0., 180.]), np.ones_like(ap), ap, RATE, 0)
        previous = ap.copy()
        for strength in (0., 0.15, 0.40, 0.80):
            changed = modified_aperiodicity(analysis, strength, band="broad")
            self.assertTrue(np.all(changed >= previous))
            self.assertTrue(np.all(changed < 1))
            np.testing.assert_array_equal(changed[1], ap[1])
            np.testing.assert_array_equal(changed[:, 0], ap[:, 0])
            if strength:
                self.assertGreater(changed[0, 8], ap[0, 8])  # 187.5 Hz
            previous = changed
        np.testing.assert_array_equal(analysis.aperiodicity, ap)
        np.testing.assert_array_equal(modified_aperiodicity(analysis, 0, band="broad"), ap)
        for strength in (-0.1, 0.81, float("nan"), float("inf")):
            with self.subTest(strength=strength), self.assertRaises(ValueError):
                modified_aperiodicity(analysis, strength, band="broad")
        with self.assertRaises(ValueError):
            modified_aperiodicity(analysis, 0.40)

    def test_difference_metrics_identity_known_residual_and_invalid_inputs(self):
        mask = np.ones(len(self.signal), dtype=bool)
        same = difference_metrics(self.signal, self.signal, mask)
        self.assertEqual(same["active_residual_rms_ratio"], 0)
        self.assertIsNone(same["active_residual_db_relative_to_control"])
        changed = difference_metrics(self.signal * 1.5, self.signal, mask)
        self.assertAlmostEqual(changed["active_residual_rms_ratio"], 0.5)
        self.assertAlmostEqual(changed["active_residual_db_relative_to_control"], -6.0206, places=4)
        for signal, control, selection in (
            (self.signal[:-1], self.signal, mask),
            (self.signal, np.zeros_like(self.signal), mask),
            (self.signal, self.signal, ~mask),
            (self.signal * float("nan"), self.signal, mask),
        ):
            with self.assertRaises(ValueError):
                difference_metrics(signal, control, selection)

    def test_effect_edge_taper_stays_within_voiced_runs(self):
        f0 = np.array([0.] + [180.] * 15 + [0., 0.] + [180.] * 3 + [0.])
        weights = voiced_edge_weights(f0, 20)
        np.testing.assert_array_equal(weights[f0 == 0], 0)
        self.assertEqual(weights[1], 0)
        self.assertEqual(weights[15], 0)
        self.assertEqual(weights[8], 1)
        self.assertGreater(weights[2], 0)
        self.assertLess(weights[2], weights[3])
        self.assertLess(weights[19], 1)
        np.testing.assert_allclose(weights[1:16], weights[1:16][::-1])
        np.testing.assert_array_equal(voiced_edge_weights(f0, 0), (f0 > 0).astype(float))
        ap = np.full((len(f0), 513), 0.1)
        analysis = Analysis(f0, np.ones_like(ap), ap, RATE, 0)
        base = modified_aperiodicity(analysis, 0.8, band="broad")
        softer = modified_aperiodicity(analysis, 0.8, band="broad", edge_ms=20)
        np.testing.assert_array_equal(softer[8], base[8])
        np.testing.assert_array_equal(softer[1], ap[1])
        np.testing.assert_array_equal(softer[f0 == 0], ap[f0 == 0])
        self.assertTrue(np.all(softer >= ap))
        self.assertTrue(np.all(softer <= base))
        np.testing.assert_array_equal(analysis.aperiodicity, ap)
        for value in (-1, 31, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                voiced_edge_weights(f0, value)

    def test_harvest_analysis_and_preset_must_match(self):
        speech = read_speech(self.source)
        analysis = analyze(speech, pitch_method="harvest")
        self.assertEqual(analysis.pitch_method, "harvest")
        stats = summarize_analysis(analysis)
        self.assertEqual(stats["pitch_method"], "harvest")
        self.assertGreater(stats["voiced_frame_fraction"], 0)
        with self.assertRaisesRegex(ValueError, "does not match"):
            resynthesize(analysis, PRESETS[0])
        for preset in REFINEMENT_PRESETS[3:]:
            rendered, _ = resynthesize(analysis, preset)
            self.assertEqual(len(rendered), len(speech))
            self.assertTrue(np.all(np.isfinite(rendered)))

    def test_harvest_rejects_insufficient_voicing_without_falling_back(self):
        time = np.arange(RATE, dtype=np.float64) / RATE
        sparse_tone = (0.2 * np.sin(2 * np.pi * 180 * time)
                       + 0.06 * np.sin(2 * np.pi * 360 * time))
        with self.assertRaisesRegex(ValueError, "Too few voiced"):
            analyze(sparse_tone, pitch_method="harvest")

    def test_refinement_preserves_preferred_and_supplies_matching_controls(self):
        def render(path: Path, *, calibration: bool = False, refinement: bool = False) -> dict:
            return audition(
                self.source, path, transcript="Harmonic test fixture", source_kind="local-synthetic",
                source_note="180 Hz test", rights_note="Test fixture", repeats=1,
                calibration=calibration, refinement=refinement,
            )

        base_dir = self.root / "calibration"
        base = render(base_dir, calibration=True)
        output = self.root / "refinement"
        refined = render(output, refinement=True)
        self.assertEqual(refined["mode"], "naturalness_refinement")
        self.assertNotIn("blind_playlist", refined)
        records = refined["conditions"]
        self.assertEqual(len(records), 7)
        self.assertEqual(records[2]["sha256"], base["conditions"][-1]["sha256"])
        self.assertEqual(records[2]["difference_vs_preferred"]["active_residual_rms_ratio"], 0)
        self.assertGreater(records[3]["difference_vs_preferred"]["active_residual_rms_ratio"], 0)
        for index in (1, 4):
            self.assertEqual(records[index]["difference_vs_world_control"]["active_residual_rms_ratio"], 0)
        for index in (4, 5, 6):
            self.assertEqual(records[index]["world_control_condition"], "harvest_control")
            self.assertEqual(records[index]["parameters"]["pitch_method"], "harvest")
        playlist = refined["refinement_playlist"]
        self.assertEqual([item["label"] for item in playlist["timeline"]],
                         ["sample_03", "sample_04", "sample_06", "sample_07"])
        self.assertEqual(playlist["samples"], 7 * RATE)
        self.assertEqual(refined["short_comparison"]["samples"], 3 * RATE)
        self.assertEqual(set(refined["analysis_by_pitch_method"]), {"dio", "harvest"})
        levels = [item["active_rms_dbfs"] for item in records]
        self.assertLess(max(levels) - min(levels), 0.02)
        for item in records:
            self.assertEqual(item["clipped_samples"], 0)
            self.assertLessEqual(item["peak_dbfs"], -0.99)
            self.assertEqual(item["samples"], RATE)
        review = json.loads((output / "review.json").read_text())
        self.assertTrue(all(item["less_robotic_than_preferred"] is None for item in review["items"]))
        with wave.open(str(output / refined["short_comparison"]["file"]), "rb") as audio:
            pcm = audio.readframes(audio.getnframes())
        with wave.open(str(output / records[2]["file"]), "rb") as audio:
            self.assertEqual(pcm[:2 * RATE], audio.readframes(RATE))
        self.assertEqual(pcm[2 * RATE:4 * RATE], b"\0" * (2 * RATE))
        with wave.open(str(output / records[-1]["file"]), "rb") as audio:
            self.assertEqual(pcm[4 * RATE:], audio.readframes(RATE))
        with self.assertRaisesRegex(ValueError, "not both"):
            render(self.root / "bad", refinement=True, calibration=True)
        self.assertFalse((self.root / "bad").exists())

    def test_calibration_pack_is_ordered_and_strong_effect_is_not_a_noop(self):
        output = self.root / "calibration"
        manifest = audition(
            self.source, output, transcript="Harmonic test fixture", source_kind="local-synthetic",
            source_note="180 Hz test", rights_note="Test fixture", repeats=1, calibration=True,
        )
        expected = ["original_level_matched"] + [preset.name for preset in CALIBRATION_PRESETS]
        records = manifest["conditions"]
        self.assertEqual([record["condition"] for record in records], expected)
        self.assertEqual([record["label"] for record in records],
                         [f"sample_{index:02d}" for index in range(1, 7)])
        self.assertEqual(manifest["mode"], "audibility_calibration")
        self.assertNotIn("blind_playlist", manifest)
        self.assertFalse((output / "listen_blind.wav").exists())
        self.assertEqual(records[1]["difference_vs_world_control"]["active_residual_rms_ratio"], 0)
        ratio = records[-1]["difference_vs_world_control"]["active_residual_rms_ratio"]
        self.assertGreater(ratio, 0.1)  # Engineering non-noop check, not a perceptual gate.
        self.assertEqual(manifest["short_comparison"]["order"],
                         ["world_control", "broad_080_exaggerated"])
        levels = []
        for record in records:
            levels.append(record["active_rms_dbfs"])
            self.assertEqual(record["samples"], RATE)
            self.assertEqual(record["clipped_samples"], 0)
            self.assertLessEqual(record["peak_dbfs"], -0.99)
            self.assertEqual(hashlib.sha256((output / record["file"]).read_bytes()).hexdigest(),
                             record["sha256"])
        self.assertLess(max(levels) - min(levels), 0.02)
        review = json.loads((output / "review.json").read_text())
        self.assertEqual([item["condition"] for item in review["items"]], expected)
        self.assertTrue(all(item["audible_difference_vs_world_control"] is None
                            for item in review["items"]))
        for filename, sample_count in (("listen_calibration.wav", RATE * 11),
                                        ("control_then_exaggerated.wav", RATE * 3)):
            with wave.open(str(output / filename), "rb") as audio:
                self.assertEqual(audio.getnframes(), sample_count)
                pcm = audio.readframes(sample_count)
            if filename == "control_then_exaggerated.wav":
                with wave.open(str(output / records[1]["file"]), "rb") as control:
                    self.assertEqual(pcm[:RATE * 2], control.readframes(RATE))
                self.assertEqual(pcm[RATE * 2:RATE * 4], b"\0" * RATE * 2)
                with wave.open(str(output / records[-1]["file"]), "rb") as effect:
                    self.assertEqual(pcm[RATE * 4:], effect.readframes(RATE))

    def test_active_level_matching_preserves_relative_loudness_and_headroom(self):
        source = np.concatenate((np.zeros(RATE), self.signal))
        mask = active_mask(source)
        self.assertFalse(np.any(mask[:RATE]))
        spiked = source * 0.01
        spiked[RATE + 20] = 50
        matched, gains = match_levels([source, source * 5, spiked], mask)
        levels = [np.sqrt(np.mean(signal[mask] ** 2)) for signal in matched]
        np.testing.assert_allclose(levels, levels[0])
        self.assertTrue(all(np.max(np.abs(signal)) <= 10 ** (-1 / 20) for signal in matched))
        self.assertAlmostEqual(gains[0] / gains[1], 5)
        with self.assertRaises(ValueError):
            match_levels([source, np.zeros_like(source)], mask)
        with self.assertRaises(ValueError):
            match_levels([source[:100]], mask)

    def test_export_refuses_invalid_samples(self):
        for value in (1., -1., float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pcm16(np.array([0., value]))

    def test_pack_has_provenance_blinding_and_safe_pcm(self):
        output = self.root / "pack"

        def render(path: Path) -> dict:
            return audition(
                self.source, path, transcript="Synthetic test tone, not speech",
                source_kind="local-synthetic", source_note="180 Hz harmonic fixture",
                rights_note="Test fixture", repeats=1, seed=19,
            )

        manifest = render(output)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual((output / "source_original.wav").read_bytes(), self.source.read_bytes())
        self.assertEqual(len(manifest["conditions"]), 4)
        self.assertEqual(manifest, json.loads((output / "manifest.json").read_text()))
        self.assertEqual(len(manifest["benchmark"]["conditions"]), 3)
        for record in manifest["conditions"]:
            path = output / record["file"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"])
            self.assertEqual(record["clipped_samples"], 0)
            self.assertLessEqual(record["peak_dbfs"], -0.99)
            self.assertEqual(len(read_speech(path)), RATE)
        levels = [item["active_rms_dbfs"] for item in manifest["conditions"]]
        self.assertLess(max(levels) - min(levels), 0.02)
        review = json.loads((output / "review.json").read_text())
        self.assertTrue(all(item["naturalness_1_to_5"] is None for item in review["items"]))
        self.assertNotIn("world_control", json.dumps(review))
        self.assertNotIn("breathiness_light", json.dumps(review))
        second = render(self.root / "pack2")
        self.assertEqual([item["label"] for item in manifest["conditions"]],
                         [item["label"] for item in second["conditions"]])
        before = (output / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            render(output)
        self.assertEqual((output / "manifest.json").read_bytes(), before)

    def test_invalid_metadata_does_not_create_pack(self):
        for repeats, transcript in ((0, "Words"), (11, "Words"), (1, " ")):
            with self.subTest(repeats=repeats, transcript=transcript):
                output = self.root / "invalid"
                with self.assertRaises(ValueError):
                    audition(self.source, output, transcript=transcript, source_kind="local-synthetic",
                             source_note="Test", rights_note="Test", repeats=repeats)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
