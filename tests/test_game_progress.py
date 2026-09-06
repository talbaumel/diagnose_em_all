from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.game_progress import Progress, ProgressStore


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "progress.json"
        self.store = ProgressStore(["first", "second"], self.path)

    def test_missing_save_is_fresh(self):
        self.assertEqual(self.store.load(), Progress())

    def test_round_trip_and_roster_order(self):
        progress = Progress({"first", "unknown"}, (430, 450), True)
        self.assertTrue(self.store.save(progress))
        restored = ProgressStore(["second", "first"], self.path).load()
        self.assertEqual(restored.diagnosed, {"first"})
        self.assertEqual(restored.position, (430, 450))
        self.assertTrue(restored.completion_announced)

    def test_skill_scores_round_trip_with_bounds_and_roster_order(self):
        progress = Progress({"first", "second"}, (430, 450), True, {"first": -100, "second": 100})
        self.assertTrue(self.store.save(progress))
        self.assertEqual(ProgressStore(["second", "first"], self.path).load(), progress)
        document = json.loads(self.path.read_text())
        self.assertEqual(document["version"], 1)
        self.assertEqual(set(document["campaigns"][self.store.key]),
                         {"diagnosed", "position", "completion_announced", "skill_scores"})

    def test_legacy_version_one_save_loads_without_scores(self):
        document = {"version": 1, "campaigns": {self.store.key: {
            "diagnosed": ["first"], "position": [430, 450], "completion_announced": True,
        }}}
        self.path.write_text(json.dumps(document))
        progress = self.store.load()
        self.assertEqual(progress, Progress({"first"}, (430, 450), True))
        self.assertEqual(progress.skill_scores, {})
        self.assertEqual(self.store.warning, "")
        progress.skill_scores["first"] = 0
        self.assertTrue(self.store.save(progress))
        self.assertEqual(self.store.load().skill_scores, {"first": 0})

    def test_score_dicts_are_independent(self):
        first, second = Progress(), Progress()
        first.skill_scores["first"] = 5
        self.assertEqual(second.skill_scores, {})

    def test_invalid_optional_scores_preserve_completed_cases_and_file(self):
        invalid_scores = (
            None, [], "12", {"first": True}, {"first": 1.5}, {"first": "1"},
            {"first": -101}, {"first": 101}, {"first": None},
            {"first": float("nan")}, {"first": float("inf")},
            {"unknown": 1}, {"second": 2},
        )
        for scores in invalid_scores:
            with self.subTest(scores=scores):
                document = {"version": 1, "campaigns": {self.store.key: {
                    "diagnosed": ["first"], "position": [430, 450],
                    "completion_announced": True, "skill_scores": scores,
                }}}
                self.path.write_text(json.dumps(document))
                before = self.path.read_bytes()
                restored = self.store.load()
                self.assertEqual(restored, Progress({"first"}, (430, 450), True))
                self.assertIn("Skill scores unreadable", self.store.warning)
                self.assertFalse(self.store.save(restored))
                self.assertTrue(self.store.warning)
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_in_memory_scores_cannot_replace_save(self):
        self.assertTrue(self.store.save(Progress({"first"}, skill_scores={"first": 4})))
        before = self.path.read_bytes()
        for scores in ({"unknown": 1}, {"second": 1}, {"first": True},
                       {"first": 1.0}, {"first": 101}, {"first": -101}, None):
            with self.subTest(scores=scores):
                self.assertFalse(self.store.save(Progress({"first"}, skill_scores=scores)))
                self.assertEqual(self.path.read_bytes(), before)

    def test_score_rosters_and_reset_are_isolated(self):
        self.store.save(Progress({"first"}, skill_scores={"first": -3}))
        other = ProgressStore(["first"], self.path)
        other.save(Progress({"first"}, skill_scores={"first": 8}))
        self.assertEqual(self.store.load().skill_scores, {"first": -3})
        self.assertEqual(other.load().skill_scores, {"first": 8})
        self.assertTrue(self.store.save(Progress(), reset=True))
        self.assertEqual(self.store.load(), Progress())
        self.assertEqual(other.load().skill_scores, {"first": 8})

    def test_reset_clears_malformed_optional_scores_without_losing_other_roster(self):
        self.store.save(Progress({"first"}))
        other = ProgressStore(["other"], self.path)
        other.save(Progress({"other"}, skill_scores={"other": 10}))
        document = json.loads(self.path.read_text())
        document["campaigns"][self.store.key]["skill_scores"] = {"first": "bad"}
        self.path.write_text(json.dumps(document))
        self.assertTrue(self.store.save(Progress(), reset=True))
        self.assertEqual(self.store.load(), Progress())
        self.assertEqual(other.load().skill_scores, {"other": 10})

    def test_rosters_are_isolated(self):
        self.store.save(Progress({"first"}))
        other = ProgressStore(["other"], self.path)
        other.save(Progress({"other"}))
        self.assertEqual(self.store.load().diagnosed, {"first"})
        self.assertEqual(other.load().diagnosed, {"other"})
        self.store.save(Progress(), reset=True)
        self.assertEqual(other.load().diagnosed, {"other"})

    def test_corrupt_save_is_preserved(self):
        self.path.write_text("broken", encoding="utf-8")
        self.assertEqual(self.store.load(), Progress())
        self.assertFalse(self.store.save(Progress()))
        self.assertEqual(self.path.read_text(), "broken")
        self.assertTrue(self.store.save(Progress(), reset=True))
        self.assertEqual(self.path.with_name("progress.json.bak").read_text(), "broken")

    def test_unknown_version_is_preserved(self):
        payload = '{"version": 100, "campaigns": {}}'
        self.path.write_text(payload)
        self.store.load()
        self.assertFalse(self.store.save(Progress()))
        self.assertEqual(self.path.read_text(), payload)

    def test_nonfinite_coordinates_fall_back(self):
        self.store.save(Progress())
        document = json.loads(self.path.read_text())
        document["campaigns"][self.store.key]["position"] = [float("nan"), 100]
        self.path.write_text(json.dumps(document))
        self.assertEqual(self.store.load().position, (480, 480))

    def test_failed_atomic_replace_keeps_save(self):
        self.store.save(Progress({"first"}))
        before = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("read only")):
            self.assertFalse(self.store.save(Progress()))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()