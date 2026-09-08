"""Roster audio contracts, independent of unreviewed or newly sourced assets."""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.audio_playback import Segment
from src.conversation_runtime import ConversationRuntime, Response
from src.cue_catalog import CUE_KINDS, LOCAL_CUE_EVENTS, Cue, CueChoice, load_catalog
from src.cue_timing import insert_cue, internal_pauses
from src.hospital_game import load_patient_scenario
from src.patient_performance import COUGH_CLIP, CuePolicy, PerformanceProfile
from tests.audio_fakes import ControlledSink, until

ROOT = Path(__file__).resolve().parents[1]
PCM = bytes(2) + struct.pack("<h", 2000) * 4798 + bytes(2)
EVENT = "ankle_examination"
CHOICES = (CueChoice("test_reaction"),)
CATALOG = {
    "test_reaction": Cue("test_reaction", "discomfort_exhale", COUGH_CLIP, "0" * 64,
                         "[exhales]", ("event", "requested", "internal_pause")),
    "test_regular": Cue("test_regular", "sigh", COUGH_CLIP, "0" * 64,
                        "[sighs]", ("requested", "internal_pause")),
}


class CatalogFixture:
    def patch_catalog(self):
        for target in ("src.patient_performance.load_catalog", "src.conversation_runtime.load_catalog"):
            patcher = patch(target, return_value=CATALOG)
            patcher.start()
            self.addCleanup(patcher.stop)


class RosterProfileTests(CatalogFixture, unittest.TestCase):
    def setUp(self):
        self.patch_catalog()

    def test_defaults_and_event_only_profiles_do_not_advertise_remote_events(self):
        profile = PerformanceProfile(event_cues={EVENT: CHOICES})
        self.assertIsNone(profile.max_spontaneous_cues)
        self.assertEqual(profile.event_limits[EVENT], 1)
        self.assertEqual(profile.cue_tools, [])
        self.assertNotIn("ankle_examination", profile.instructions)
        self.assertEqual(PerformanceProfile().cue_tools[0]["name"], "cough")
        self.assertEqual(PerformanceProfile(cues=()).cue_tools, [])
        self.assertEqual(PerformanceProfile(max_spontaneous_cues=0).max_spontaneous_cues, 0)

    def test_invalid_profiles(self):
        invalid = [
            *({"max_spontaneous_cues": value} for value in (True, -1, 1.5, "1")),
            {"event_cues": []}, {"event_cues": {"remote_event": CHOICES}},
            {"event_cues": {EVENT: list(CHOICES)}}, {"event_cues": {EVENT: ()}},
            {"event_cues": {EVENT: ("test_reaction",)}},
            {"event_cues": {EVENT: CHOICES * 2}},
            {"event_cues": {EVENT: (CueChoice("missing"),)}},
            {"event_cues": {EVENT: (CueChoice("test_regular"),)}},
            {"event_limits": {EVENT: 1}}, {"event_limits": []},
            *({"event_cues": {EVENT: CHOICES}, "event_limits": {EVENT: value}}
              for value in (-1, True, 1.5, None)),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                PerformanceProfile(**values)

    def test_json_event_arrays_are_validated_and_converted(self):
        data = json.loads((ROOT / "data/prompts/01_common_cold_kid.json").read_text())
        data["performance_profile"] = {
            "cues": [], "max_spontaneous_cues": 0,
            "event_cues": {EVENT: [{"id": "test_reaction", "weight": 2}]},
            "event_limits": {EVENT: 2},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scenario.json"
            path.write_text(json.dumps(data))
            profile = load_patient_scenario(path).performance_profile
            self.assertEqual(profile.event_cues[EVENT], (CueChoice("test_reaction", 2),))
            self.assertEqual(profile.event_limits[EVENT], 2)
            for events in ([], {EVENT: "test_reaction"}, {EVENT: [None]}, {"remote": []}):
                data["performance_profile"]["event_cues"] = events
                path.write_text(json.dumps(data))
                with self.subTest(events=events), self.assertRaises(ValueError):
                    load_patient_scenario(path)

    def test_catalog_accepts_all_kinds_and_event_context_without_asset_promotion(self):
        entry = {
            "status": "approved", "file": COUGH_CLIP, "sha256": "0" * 64,
            "caption": "[test]", "provenance": "test fixture", "license": "test fixture",
            "review": "test fixture only", "contexts": ["event"], "gain_db": 0,
        }
        data = {"schema_version": 1, "cues": {
            kind: dict(entry, kind=kind) for kind in CUE_KINDS
        }}
        with patch("src.cue_catalog.CATALOG_PATH", SimpleNamespace(read_text=lambda **_: json.dumps(data))):
            self.assertEqual(set(load_catalog()), CUE_KINDS)
            data["cues"]["yawn"]["contexts"] = ["remote"]
            with self.assertRaises(ValueError):
                load_catalog()

    def test_lifetime_counts_only_commit_at_audible_start_and_requested_bypasses_cap(self):
        now = [0.]
        profile = PerformanceProfile(cues=CHOICES, max_spontaneous_cues=1,
                                     event_cues={EVENT: CHOICES}, spontaneous_every_turns=1)
        policy = CuePolicy(profile, lambda: now[0])
        policy.new_turn()
        self.assertTrue(policy.reserve(spontaneous=True))
        self.assertEqual(policy.spontaneous_count, 0)
        policy.release(policy.turn)
        self.assertTrue(policy.reserve(spontaneous=True))
        policy.started("test_reaction", spontaneous=True)
        self.assertEqual(policy.spontaneous_count, 1)
        self.assertFalse(policy.reserve())
        policy.new_turn()
        self.assertFalse(policy.reserve())
        now[0] = 21
        self.assertFalse(policy.reserve(spontaneous=True))
        self.assertTrue(policy.reserve())
        policy.started("test_reaction")
        self.assertEqual(policy.spontaneous_count, 1)
        policy.new_turn()
        now[0] = 42
        self.assertTrue(policy.reserve(event=EVENT))
        self.assertEqual(policy.event_counts, {})
        policy.started("test_reaction", event=EVENT)
        self.assertEqual(policy.event_counts, {EVENT: 1})
        policy.new_turn()
        now[0] = 63
        self.assertFalse(policy.reserve(event=EVENT))


class RosterRuntimeTests(CatalogFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patch_catalog()
        for target, value in (("src.cue_catalog.Cue.verify", None),
                              ("src.conversation_runtime.read_pcm_clip", PCM)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sink = ControlledSink()
        self.animator = SimpleNamespace(
            push_to_talk=False, evidence_open=False, won=False, _menu=None,
            skills_modal=False, effects_volume=1.0, _diagnosis_open=False,
            add_transcript=Mock(), set_talking=Mock(),
            begin_cue_reaction=Mock(), end_cue_reaction=Mock(),
        )
        self.now = 100.
        self.profile = PerformanceProfile(cues=CHOICES, event_cues={EVENT: CHOICES},
                                          max_spontaneous_cues=1, spontaneous_every_turns=1)
        self.runtime = ConversationRuntime(
            AsyncMock(), self.animator, asyncio.Event(), {}, self.sink,
            profile=self.profile, clock=lambda: self.now,
        )
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def audible(self, task):
        await until(lambda: self.runtime.playback.active is not None)
        self.sink.ready = True
        await until(lambda: self.runtime.playback.started)
        self.sink.done = True
        self.sink.heard_ms = len(self.sink.history[-1]) // 48
        return await task

    async def test_event_uses_shared_sink_audible_reaction_and_one_per_visit(self):
        self.task(self.runtime.playback.run())
        event = self.task(self.runtime.play_event_cue(EVENT))
        await until(lambda: bool(self.sink.history))
        self.animator.begin_cue_reaction.assert_not_called()
        self.assertEqual(self.runtime.policy.event_counts, {})
        self.assertTrue(await self.audible(event))
        self.animator.begin_cue_reaction.assert_called_once_with(EVENT, len(PCM) / 48000)
        self.animator.end_cue_reaction.assert_called_once_with()
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "[exhales]")
        self.assertEqual(self.runtime.policy.event_counts, {EVENT: 1})
        self.runtime.policy.new_turn()
        self.now += 21
        self.assertFalse(await self.runtime.play_event_cue(EVENT))
        self.assertEqual(self.sink.history, [PCM])

    async def test_interrupt_before_audible_start_does_not_spend_event_limit(self):
        self.task(self.runtime.playback.run())
        event = self.task(self.runtime.play_event_cue(EVENT))
        await until(lambda: self.runtime.playback.active is not None)
        self.runtime.playback.interrupt()
        self.assertFalse(await event)
        self.assertEqual(self.runtime.policy.event_counts, {})
        self.animator.begin_cue_reaction.assert_not_called()
        retry = self.task(self.runtime.play_event_cue(EVENT))
        self.assertTrue(await self.audible(retry))

    async def test_event_never_waits_behind_speech_or_processing(self):
        self.runtime.playback.enqueue(Segment(0, "speech", "speech", PCM, "hello"))
        self.assertFalse(await self.runtime.play_event_cue(EVENT))
        self.runtime.playback.interrupt()
        for attr, value in (("processing_bytes", 2), ("buffered_bytes", 2), ("active_id", "response"),
                            ("requested_generation", 0)):
            original = getattr(self.runtime, attr)
            setattr(self.runtime, attr, value)
            self.assertFalse(await self.runtime.play_event_cue(EVENT))
            setattr(self.runtime, attr, original)
        self.assertEqual(self.runtime.policy.event_counts, {})

    async def test_skill_override_never_bypasses_hard_blocks(self):
        self.runtime.pending_skills.add("exam")
        self.animator.skills_modal = True
        self.assertFalse(await self.runtime.play_event_cue(EVENT))
        for attr, value in (("push_to_talk", True), ("evidence_open", True), ("won", True),
                            ("_menu", object()), ("_diagnosis_open", True)):
            original = getattr(self.animator, attr)
            setattr(self.animator, attr, value)
            self.assertFalse(await self.runtime.play_event_cue(EVENT, allow_skill=True))
            setattr(self.animator, attr, original)
        self.runtime.stop.set()
        self.assertFalse(await self.runtime.play_event_cue(EVENT, allow_skill=True))
        self.runtime.stop.clear()
        self.runtime.user_talking = True
        self.assertFalse(await self.runtime.play_event_cue(EVENT, allow_skill=True))
        self.runtime.user_talking = False
        self.task(self.runtime.playback.run())
        event = self.task(self.runtime.play_event_cue(EVENT, allow_skill=True))
        self.assertTrue(await self.audible(event))

    async def test_mute_and_late_mute_skip_without_captions_counts_or_reaction(self):
        self.animator.effects_volume = 0.
        self.assertFalse(await self.runtime.play_event_cue(EVENT))
        self.runtime.policy.new_turn()
        self.assertEqual(await self.runtime.requested_cue(Response("r", 0), "discomfort_exhale"), "skipped")
        self.animator.effects_volume = 1.
        event = self.task(self.runtime.play_event_cue(EVENT))
        await until(lambda: not self.runtime.playback.queue.empty())
        self.animator.effects_volume = 0.
        self.task(self.runtime.playback.run())
        self.assertFalse(await event)
        self.assertEqual(self.sink.history, [])
        self.assertEqual(self.runtime.policy.event_counts, {})
        self.animator.effects_volume = 1.
        retry = self.task(self.runtime.play_event_cue(EVENT))
        await until(lambda: self.runtime.playback.active is not None)
        self.animator.effects_volume = 0.
        self.assertFalse(await retry)
        self.assertEqual(self.runtime.policy.event_counts, {})
        self.animator.begin_cue_reaction.assert_not_called()
        self.animator.add_transcript.assert_not_called()

    async def test_queued_volume_applies_to_effect_only_and_preserves_requested_caption(self):
        self.runtime.policy.new_turn()
        requested = self.task(self.runtime.requested_cue(Response("r", 0), "discomfort_exhale"))
        await until(lambda: not self.runtime.playback.queue.empty())
        self.animator.effects_volume = .25
        self.task(self.runtime.playback.run())
        self.assertEqual(await self.audible(requested), "played")
        self.assertEqual(self.sink.history[0][2:4], struct.pack("<h", 500))
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "[exhales]")
        speech = self.runtime.playback.enqueue(Segment(0, "s", "s", PCM, "speech"))
        self.animator.effects_volume = 0.
        self.assertEqual(await self.audible(speech), "played")
        self.assertEqual(self.sink.history[-1], PCM)

    async def test_muted_inline_restores_original_speech_and_does_not_count(self):
        self.runtime.policy.new_turn()
        speech = struct.pack("<h", 3000) * 4800 + bytes(9600) + struct.pack("<h", -3000) * 4800
        insertion = insert_cue(speech, PCM, internal_pauses(speech)[0])
        self.assertTrue(self.runtime.policy.reserve(spontaneous=True))
        segment = Segment(
            0, "r", "s", insertion.pcm, "original caption", insertion=insertion,
            cue_id="test_reaction", cue_caption="[exhales]", spontaneous=True,
            source_pcm=speech, cue_turn=self.runtime.policy.turn,
        )
        ticket = self.runtime._enqueue_performance(segment)
        self.animator.effects_volume = 0.
        self.task(self.runtime.playback.run())
        self.assertEqual(await self.audible(ticket), "played")
        self.assertEqual(self.sink.history, [speech])
        self.assertEqual(self.runtime.policy.spontaneous_count, 0)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "original caption")
        self.assertTrue(self.runtime.policy.reserve(spontaneous=True))

    async def test_stale_event_skips_when_speech_arrives_before_dequeue(self):
        event = self.task(self.runtime.play_event_cue(EVENT))
        await until(lambda: not self.runtime.playback.queue.empty())
        speech = self.runtime.playback.enqueue(Segment(0, "s", "s", PCM, "hello"))
        self.task(self.runtime.playback.run())
        self.assertFalse(await event)
        self.assertEqual(await self.audible(speech), "played")
        self.assertEqual(self.runtime.policy.event_counts, {})
        self.assertEqual(self.sink.history, [PCM])

    async def test_explicit_catalog_never_loads_legacy_cough_and_callbacks_optional(self):
        with patch("src.conversation_runtime.read_pcm_clip", return_value=PCM) as read:
            runtime = ConversationRuntime(AsyncMock(), self.animator, asyncio.Event(), {}, self.sink,
                                          profile=PerformanceProfile(cues=()))
        read.assert_not_called()
        self.assertEqual(runtime.cough_pcm, b"")
        self.animator.begin_cue_reaction = None
        self.animator.end_cue_reaction = None
        self.task(self.runtime.playback.run())
        event = self.task(self.runtime.play_event_cue(EVENT))
        self.assertTrue(await self.audible(event))

    async def test_legacy_cough_still_loads_and_plays(self):
        self.runtime = ConversationRuntime(
            AsyncMock(), self.animator, asyncio.Event(), {}, self.sink, profile=PerformanceProfile(),
        )
        self.assertEqual(self.runtime.cough_pcm, PCM)
        self.runtime.policy.new_turn()
        self.task(self.runtime.playback.run())
        cough = self.task(self.runtime.cough(Response("r", 0), spontaneous=True))
        self.assertEqual(await self.audible(cough), "played")
        self.assertEqual(self.runtime.policy.spontaneous_count, 1)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "[coughs]")

    async def test_invalid_event_and_zero_limit(self):
        for event in ("remote_event", "cough", "../ankle_examination", None):
            with self.assertRaises(ValueError):
                await self.runtime.play_event_cue(event)
        for event in LOCAL_CUE_EVENTS - {EVENT}:
            self.assertFalse(await self.runtime.play_event_cue(event))
        self.runtime.policy = CuePolicy(
            replace(self.profile, event_limits={EVENT: 0}), lambda: self.now,
        )
        self.assertFalse(await self.runtime.play_event_cue(EVENT))
