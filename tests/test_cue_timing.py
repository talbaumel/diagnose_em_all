from __future__ import annotations

import hashlib
import json
import struct
import shutil
import tempfile
import unittest
from pathlib import Path

from src.cue_timing import RATE, Pause, insert_cue, internal_pauses, quiet_limit
from tools.preview_inline_cues import build, read_pcm, save_pcm

ROOT = Path(__file__).resolve().parents[1]


def constant(value: int, seconds: float) -> bytes:
    return struct.pack("<h", value) * round(RATE * seconds)


class CueTimingTests(unittest.TestCase):
    def setUp(self):
        self.before = constant(2000, 1)
        self.gap = constant(0, .4)
        self.after = constant(-2000, 1)
        self.speech = self.before + self.gap + self.after
        self.cue = b"\0\0" + constant(1000, .6) + b"\0\0"
        self.pause = Pause(RATE, round(RATE * 1.4))

    def test_detects_only_internal_long_quiet_intervals(self):
        leading = constant(0, .3)
        trailing = constant(0, .4)
        pauses = internal_pauses(leading + self.speech + trailing)
        self.assertEqual(pauses, (Pause(round(RATE * 1.3), round(RATE * 1.7)),))
        self.assertEqual(internal_pauses(constant(0, 2)), ())
        self.assertEqual(internal_pauses(self.before + constant(0, .1) + self.after), ())
        self.assertEqual(internal_pauses(self.before + constant(0, .3)), ())
        self.assertEqual(internal_pauses(constant(0, .3) + self.after), ())

    def test_replaces_only_quiet_samples_preserves_all_other_speech(self):
        result = insert_cue(self.speech, self.cue, self.pause)
        cut_start, cut_end = result.removed_quiet.start, result.removed_quiet.end
        self.assertEqual(cut_start, RATE + 1200)
        self.assertEqual(cut_end, round(RATE * 1.4) - 1200)
        self.assertEqual(self.speech[2 * cut_start:2 * cut_end], bytes((cut_end-cut_start)*2))
        self.assertEqual(result.pcm, self.speech[:cut_start*2] + self.cue + self.speech[cut_end*2:])
        self.assertEqual(len(result.pcm), len(self.speech)+len(self.cue)-2*(cut_end-cut_start))
        self.assertGreater(result.spans[1].output_start, 0)
        self.assertLess(result.spans[1].output_end, len(result.pcm)//2)
        self.assertEqual(result.pcm[-len(self.after):], self.after)

    def test_short_cue_does_not_shorten_intentional_pause(self):
        short = b"\0\0" + constant(1000, .1) + b"\0\0"
        result = insert_cue(self.speech, short, self.pause)
        self.assertEqual(len(result.pcm), len(self.speech))
        self.assertEqual(result.removed_quiet.end-result.removed_quiet.start, len(short)//2)

    def test_source_time_holds_during_cue_then_resumes_after_removed_quiet(self):
        result = insert_cue(self.speech, self.cue, self.pause)
        cue_span = result.spans[1]
        self.assertEqual(result.source_sample_at(0), 0)
        self.assertEqual(result.source_sample_at(cue_span.output_start-1), cue_span.source_start-1)
        self.assertEqual(result.source_sample_at(cue_span.output_start), cue_span.source_start)
        self.assertEqual(result.source_sample_at(cue_span.output_end-1), cue_span.source_start)
        self.assertEqual(result.source_sample_at(cue_span.output_end), cue_span.source_end)
        self.assertEqual(result.source_sample_at(len(result.pcm)//2), len(self.speech)//2)
        mapped = [result.source_sample_at(i) for i in range(0, len(result.pcm)//2, 73)]
        self.assertEqual(mapped, sorted(mapped))
        for index in (-1, len(result.pcm)//2+1, 0.5, True):
            with self.subTest(index=index), self.assertRaises(ValueError):
                result.source_sample_at(index)  # pyright: ignore[reportArgumentType]

    def test_rejects_word_offsets_nonquiet_gaps_and_invalid_cues(self):
        for pause in (Pause(0, 1000), Pause(1000, 10000), Pause(RATE, RATE+100), Pause(-1, RATE)):
            with self.subTest(pause=pause), self.assertRaises(ValueError):
                insert_cue(self.speech, self.cue, pause)
        # A single loud sample inside a gap prevents it being treated as one long silence.
        gap = constant(0, .15) + b"\xff\x7f" + constant(0, .15)
        self.assertEqual(internal_pauses(self.before + gap + self.after), ())
        for cue in (b"", b"\0", constant(1000, .5), constant(0, 1),
                    b"\0\0"+constant(32767, .5)+b"\0\0", constant(0, 11)):
            with self.subTest(length=len(cue)), self.assertRaises(ValueError):
                insert_cue(self.speech, cue, self.pause)
        for value in (float("nan"), float("inf"), -20, -70, True):
            with self.assertRaises(ValueError):
                quiet_limit(value)
        for value in (0, 159, 1001, 180.5, True):
            with self.assertRaises(ValueError):
                internal_pauses(self.speech, minimum_ms=value)  # pyright: ignore[reportArgumentType]
        for value in (-1, 19, 71, True):
            with self.assertRaises(ValueError):
                insert_cue(self.speech, self.cue, self.pause, margin_ms=value)
        for pcm in (b"", b"\0", constant(0, 31)):
            with self.assertRaises(ValueError):
                internal_pauses(pcm)


class InlinePreviewTests(unittest.TestCase):
    def test_real_asset_pack_has_internal_cues_and_exact_source_mapping(self):
        packaged = ROOT / "assets/audio/cue_candidates"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            root.mkdir()
            shutil.copytree(packaged / "prepared", root / "prepared")
            sources = json.loads((packaged / "SOURCES.json").read_text())
            speech = constant(2000, 1) + constant(0, .5) + constant(-2000, 1)
            save_pcm(root / "voice.wav", speech)
            sources["voice_reference"]["prepared_file"] = "voice.wav"
            (root / "SOURCES.json").write_text(json.dumps(sources))
            output = Path(temporary) / "preview"
            manifest = build(root, output)
            self.assertEqual(len(manifest["clips"]), 7)
            self.assertFalse(manifest["approved_for_gameplay"])
            by_id = {cue["id"]: cue for cue in sources["cues"]}
            for item in manifest["clips"]:
                pcm = read_pcm(output / item["file"])
                cue = read_pcm(root / by_id[item["cue"]]["file"])
                before, inserted, after = item["timing"]["spans"]
                start, end = inserted["source_start"], inserted["source_end"]
                self.assertEqual(pcm, speech[:2*start]+cue+speech[2*end:])
                self.assertGreater(start, 0)
                self.assertLess(end, len(speech)//2)
                self.assertEqual(inserted["output_end"], after["output_start"])
                self.assertEqual(inserted["output_start"], before["output_end"])
                removed = struct.unpack(f"<{end-start}h", speech[2*start:2*end])
                self.assertLessEqual(max(abs(value) for value in removed), quiet_limit(-42))
                self.assertEqual(hashlib.sha256((output/item["file"]).read_bytes()).hexdigest(), item["sha256"])
            with self.assertRaises(FileExistsError):
                build(root, output)

    def test_no_internal_pause_fails_instead_of_appending_at_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_pcm(root / "voice.wav", constant(1000, 1) + constant(0, .4))
            (root / "SOURCES.json").write_text(json.dumps({
                "voice_reference": {"prepared_file": "voice.wav"}, "cues": [],
            }))
            output = root / "out"
            with self.assertRaisesRegex(ValueError, "No safe internal pause"):
                build(root, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
