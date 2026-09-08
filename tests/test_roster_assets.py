from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.conversation_runtime import ConversationRuntime
from src.cue_catalog import load_catalog
from src.hospital_game import load_patient_scenario
from src.roster_audio_review import roster_review_complete
from tests.audio_fakes import ControlledSink, until
from tools.prepare_roster_cues import verify

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "assets/audio/roster_candidates"


class RosterAssetsTests(unittest.TestCase):
    def test_sources_and_prepared_cues_match_catalog_and_remain_unsigned(self):
        with contextlib.redirect_stdout(io.StringIO()):
            manifest = verify(PACK)
        self.assertFalse(manifest["approved_for_gameplay"])
        self.assertEqual(len(manifest["sources"]), 21)
        self.assertEqual(len(manifest["cues"]), 19)
        catalog = load_catalog()
        for cue in manifest["cues"]:
            catalog[cue["id"]].verify()
            self.assertEqual(catalog[cue["id"]].sha256, cue["sha256"])
            self.assertFalse(cue["approved_for_gameplay"])
            self.assertLessEqual(cue["peak_dbfs"], -2.99)
            self.assertLessEqual(cue["gain_db"], 12.00001)
        self.assertFalse(roster_review_complete({c["id"]: c["sha256"] for c in manifest["cues"]}))

    def test_all_eleven_configs_are_character_scoped_and_adults_have_no_voice_filter(self):
        scenarios = [load_patient_scenario(p) for p in sorted((ROOT / "data/prompts").glob("*.json"))]
        self.assertEqual(len(scenarios), 11)
        prefixes = ("cold_", "teen_", "migraine_", "allergy_", "athlete_", "anxious_",
                    "flu_", "rash_", "back_", "worker_", "neighbor_")
        tools = ({"cough", "sniffle", "sneeze", "throat_clear"}, set(), set(), {"sniffle"},
                 set(), set(), {"cough"}, set(), set(), set(), set())
        for index, scenario in enumerate(scenarios):
            profile = scenario.performance_profile
            assert profile is not None and profile.cues is not None
            choices = list(profile.cues)
            choices.extend(c for group in profile.event_cues.values() for c in group)
            self.assertTrue(choices)
            self.assertTrue(all(c.id.startswith(prefixes[index]) for c in choices))
            self.assertEqual({tool["name"] for tool in profile.cue_tools}, tools[index])
            if index:
                self.assertIsNone(profile.voice)


class FullRosterPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_patients_play_their_exact_assets_through_shared_runtime(self):
        speech = struct.pack("<h", 2000) * 24000 + bytes(24000) + struct.pack("<h", -2000) * 24000
        for path in sorted((ROOT / "data/prompts").glob("*.json")):
            with self.subTest(patient=path.name):
                profile = load_patient_scenario(path).performance_profile
                assert profile is not None
                profile = replace(profile, voice=None)
                sink = ControlledSink()
                animator = SimpleNamespace(
                    push_to_talk=False, evidence_open=False, won=False, _menu=None,
                    effects_volume=1.0, add_transcript=Mock(), set_talking=Mock(),
                    begin_cue_reaction=Mock(), end_cue_reaction=Mock(),
                )
                runtime = ConversationRuntime(AsyncMock(), animator, asyncio.Event(), {}, sink,
                                              profile=profile)
                tasks = [asyncio.create_task(runtime.playback.run()),
                         asyncio.create_task(runtime.process_speech()),
                         asyncio.create_task(runtime.finish_responses())]
                try:
                    await runtime.interrupt(new_turn=True)
                    if profile.cues:
                        await runtime.request_response(runtime.playback.generation)
                        runtime.handle({"type": "response.created", "response": {"id": "r"}})
                        for kind, data in (
                            ("response.output_audio.delta", {"delta": base64.b64encode(speech).decode()}),
                            ("response.output_audio_transcript.done", {"transcript": "Before. After."}),
                            ("response.content_part.done", {}),
                        ):
                            runtime.handle({"type": kind, "response_id": "r", "item_id": "i", **data})
                        runtime.handle({"type": "response.done", "response": {"id": "r", "status": "completed"}})
                    else:
                        event = next(iter(profile.event_cues))
                        tasks.append(asyncio.create_task(runtime.play_event_cue(event)))
                    await until(lambda: runtime.playback.active is not None)
                    assert runtime.playback.active is not None
                    segment = runtime.playback.active[0]
                    assert segment.cue_id is not None and runtime.policy is not None
                    self.assertIn(segment.cue_id, runtime.cue_audio)
                    animator.add_transcript.assert_not_called()
                    sink.ready = True
                    if segment.insertion:
                        span = segment.insertion.spans[1]
                        self.assertEqual(segment.pcm[span.output_start * 2:span.output_end * 2],
                                         runtime.cue_audio[segment.cue_id])
                        sink.heard_ms = span.output_start // 24 + 1
                        await until(lambda: runtime.policy.spontaneous_count == 1)
                    else:
                        self.assertEqual(segment.pcm, runtime.cue_audio[segment.cue_id])
                        await until(lambda: bool(runtime.policy.event_counts))
                        animator.begin_cue_reaction.assert_called_once()
                    self.assertEqual(animator.add_transcript.call_args.args[1],
                                     load_catalog()[segment.cue_id].caption)
                    sink.done = True
                    await until(lambda: runtime.playback.active is None)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)


if __name__ == "__main__":
    unittest.main()