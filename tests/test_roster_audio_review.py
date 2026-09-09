from __future__ import annotations

import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.roster_audio_review import (
    AUDITION_ENV, REVIEW_PATIENTS, roster_audio_enabled, roster_review_complete,
)


class RosterAudioReviewTests(unittest.TestCase):
    def test_local_game_enables_roster_audition_and_respects_explicit_override(self):
        entrypoint = Path(__file__).resolve().parents[1] / "src/debug_example.py"
        for configured, expected in ((None, True), ("0", False), ("1", True)):
            with self.subTest(configured=configured), patch.dict("os.environ"), patch(
                "sys.argv", [str(entrypoint)],
            ), patch("src.hospital_game.start_hospital_game") as start, patch(
                "src.roster_audio_review.roster_review_complete", return_value=False,
            ):
                if configured is None:
                    os.environ.pop(AUDITION_ENV, None)
                else:
                    os.environ[AUDITION_ENV] = configured
                start.side_effect = lambda scenarios: self.assertEqual(
                    roster_audio_enabled()[0], expected,
                )
                runpy.run_path(str(entrypoint), run_name="__main__")
                start.assert_called_once()
                scenarios = start.call_args.args[0]
                self.assertEqual(len(scenarios), len(REVIEW_PATIENTS))
                self.assertTrue(all(scenario.performance_profile is not None for scenario in scenarios))

    def test_signoff_requires_every_clip_exact_hash_and_every_patient(self):
        hashes = {"new_cue": "a" * 64}
        review = {
            "schema_version": 1,
            "cues": {"new_cue": {
                "sha256": "a" * 64, "reviewer": "Human reviewer",
                "natural_and_clean": True, "character_fit": True, "clinical_appropriate": True,
            }},
            "patients": {patient: {
                "reviewer": "Human reviewer", "live_playtest_passed": True,
                "muted_playtest_passed": True,
            } for patient in REVIEW_PATIENTS},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.json"
            self.assertFalse(roster_review_complete(hashes, path))
            path.write_text(json.dumps(review))
            self.assertTrue(roster_review_complete(hashes, path))
            self.assertFalse(roster_review_complete({"new_cue": "b" * 64}, path))
            self.assertFalse(roster_review_complete({}, path))
            for key in ("natural_and_clean", "character_fit", "clinical_appropriate"):
                review["cues"]["new_cue"][key] = False
                path.write_text(json.dumps(review))
                self.assertFalse(roster_review_complete(hashes, path))
                review["cues"]["new_cue"][key] = True
            review["patients"]["RASH_PATIENT"]["muted_playtest_passed"] = False
            path.write_text(json.dumps(review))
            self.assertFalse(roster_review_complete(hashes, path))
            path.write_text("[]")
            with self.assertRaisesRegex(ValueError, "Invalid roster"):
                roster_review_complete(hashes, path)

    def test_audition_is_explicit_and_never_claims_approval(self):
        with patch.dict("os.environ", {AUDITION_ENV: "1"}):
            enabled, notice = roster_audio_enabled()
        self.assertTrue(enabled)
        self.assertIn("unreviewed", notice)
        self.assertIn("not release-approved", notice)
        with patch.dict("os.environ", {AUDITION_ENV: "yes"}):
            with self.assertRaisesRegex(ValueError, "must be 0 or 1"):
                roster_audio_enabled()
        with patch.dict("os.environ", {AUDITION_ENV: "0"}), patch(
            "src.roster_audio_review.roster_review_complete", return_value=False,
        ):
            enabled, notice = roster_audio_enabled()
        self.assertFalse(enabled)
        self.assertIn("awaiting", notice)


if __name__ == "__main__":
    unittest.main()
