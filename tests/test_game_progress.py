from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.game_progress import Progress, ProgressStore


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
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