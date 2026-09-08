from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pygame

from src.character_effects import CueReaction, EffectsSettings, examination_cue_event
from src.diagnostic_skills import SkillEngine
from src.realtime_conversation import PatientAnimator, Test, _resolve_skill_call, _test_tools


class EffectsSettingsTests(unittest.TestCase):
    def test_round_trip_all_levels_and_no_progress_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            settings = EffectsSettings(path)
            settings.load()
            self.assertEqual(settings.volume, 1.0)
            for expected in (0.5, 0.25, 0.0, 1.0):
                settings.cycle()
                restored = EffectsSettings(path)
                restored.load()
                self.assertEqual(restored.volume, expected)
                self.assertFalse(restored.warning)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_corrupt_settings_are_reported_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            for value in ("{", '{"version":1,"effects_volume":true}',
                          '{"version":1,"effects_volume":2}', "[]"):
                path.write_text(value)
                settings = EffectsSettings(path)
                settings.load()
                self.assertTrue(settings.warning)
                with self.assertRaises(ValueError):
                    settings.cycle()
                self.assertEqual(path.read_text(), value)

    def test_failed_save_keeps_previous_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = EffectsSettings(Path(temporary) / "settings.json")
            with patch("src.character_effects.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    settings.cycle()
            self.assertEqual(settings.volume, 1.0)
            self.assertEqual(list(Path(temporary).iterdir()), [])


class CueReactionTests(unittest.TestCase):
    def test_pose_is_bounded_and_stops(self):
        for event in ("ankle_examination", "back_examination", "rash_fidget", "back_posture"):
            reaction = CueReaction()
            reaction.start(event, 2, 10)
            self.assertEqual(reaction.pose(9), (0, 0))
            self.assertNotEqual(reaction.pose(10.3), (0, 0))
            self.assertLessEqual(abs(reaction.pose(11)[0]), 3)
            self.assertEqual(reaction.pose(12), (0, 0))
            reaction.stop()
            self.assertEqual(reaction.pose(11), (0, 0))
        with self.assertRaises(ValueError):
            CueReaction().start("unknown", 1, 0)

    def test_only_actual_relevant_movement_maps_to_pain(self):
        self.assertEqual(examination_cue_event("SPRAINED_ANKLE_ATHLETE", "range_of_motion"),
                         "ankle_examination")
        self.assertEqual(examination_cue_event("ELDERLY_WITH_BACK_PAIN", "gait_assessment"),
                         "back_examination")
        for patient, skill in (("COMMON_COLD_KID", "range_of_motion"),
                               ("SPRAINED_ANKLE_ATHLETE", "ankle_x_ray"),
                               ("ELDERLY_WITH_BACK_PAIN", "functional_assessment"),
                               ("ELDERLY_WITH_BACK_PAIN", "neurological_examination")):
            self.assertIsNone(examination_cue_event(patient, skill))


class CharacterEffectsUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pygame.init()
        self.window = pygame.display.set_mode((480, 480))
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = EffectsSettings(Path(self.temporary.name) / "audio.json")
        with patch("src.realtime_conversation.EffectsSettings", return_value=self.settings):
            self.animator = PatientAnimator(4, window=self.window, disease="lateral ankle sprain")
        self.animator.skill_engine = SkillEngine("SPRAINED_ANKLE_ATHLETE", [
            Test("ankle examination", "There is lateral tenderness.")])

    async def asyncTearDown(self):
        self.animator.close()
        pygame.quit()
        self.temporary.cleanup()

    async def test_button_and_keyboard_preserve_text_and_persist(self):
        self.animator._text_input = "unsent"
        self.animator._set_text_focus(True)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F9),
                                   asyncio.Event())
        self.assertEqual(self.animator.effects_volume, 0.5)
        self.assertEqual(self.animator._text_input, "unsent")
        self.animator.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONUP, button=1, pos=self.animator._effects_button.center),
            asyncio.Event())
        self.assertEqual(self.animator.effects_volume, 0.25)
        self.assertEqual(json.loads(self.settings.path.read_text())["effects_volume"], 0.25)
        self.animator.begin_cue_reaction("ankle_examination", 1)
        self.animator.draw()
        self.animator.end_cue_reaction()

    async def test_exam_cue_after_completion_before_evidence_not_on_cancel_or_repeat(self):
        events = []

        async def cue(event):
            self.assertFalse(self.animator.evidence_open)
            self.assertTrue(self.animator.skill_engine.actions)
            events.append(event)
            return True

        async def present(evidence):
            self.assertIsNotNone(evidence)
            events.append("evidence")
            return {}

        call = {"name": "propose_ankle_examination", "arguments": "{}", "call_id": "exam"}
        with patch.object(self.animator, "confirm_skill", AsyncMock(return_value=False)):
            result = await _resolve_skill_call(call, _test_tools([])[1], self.animator,
                                               cue_event=cue, present_result=present)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(events, [])
        with patch.object(self.animator, "confirm_skill", AsyncMock(return_value=True)):
            result = await _resolve_skill_call(call, _test_tools([])[1], self.animator,
                                               cue_event=cue, present_result=present)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(events, ["ankle_examination", "evidence"])
            events.clear()
            await _resolve_skill_call(call, _test_tools([])[1], self.animator,
                                      cue_event=cue, present_result=present)
            self.assertNotIn("ankle_examination", events)


if __name__ == "__main__":
    unittest.main()
