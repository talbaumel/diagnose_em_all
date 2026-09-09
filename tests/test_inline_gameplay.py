from __future__ import annotations

import asyncio
import base64
import json
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.audio_playback import PlaybackController, Segment
from src.conversation_runtime import ConversationRuntime
from src.cue_catalog import CueChoice, load_catalog
from src.cue_timing import internal_pauses, insert_cue
from src.hospital_game import load_patient_scenario
from src.patient_performance import CuePolicy, PerformanceProfile
from tests.audio_fakes import ControlledSink, until

ROOT = Path(__file__).resolve().parents[1]


def speech_pcm() -> bytes:
    return struct.pack("<h", 2000) * 24000 + bytes(24000) + struct.pack("<h", -2000) * 24000


class CatalogAndPolicyTests(unittest.TestCase):
    def test_promoted_catalog_and_kid_only(self):
        catalog = load_catalog()
        cold_ids = {"cold_dry_cough", "cold_short_sniffle", "cold_sneeze", "cold_throat_clear"}
        self.assertTrue(cold_ids <= set(catalog))
        for cue in catalog.values():
            cue.verify()
        scenarios = [load_patient_scenario(path) for path in sorted((ROOT/"data/prompts").glob("*.json"))]
        kid = scenarios[0].performance_profile
        assert kid and kid.cues
        self.assertEqual({c.id for c in kid.cues}, cold_ids)
        self.assertEqual({tool["name"] for tool in kid.cue_tools}, {"cough", "sniffle", "sneeze", "throat_clear"})
        self.assertTrue(all(s.performance_profile is not None for s in scenarios[1:]))
        self.assertNotIn("child", PerformanceProfile(cues=(), delivery="Speak as an adult.").instructions)
        self.assertEqual(PerformanceProfile(cues=()).cue_tools, [])

    def test_json_rejects_invalid_cue_choices(self):
        data = json.loads((ROOT/"data/prompts/01_common_cold_kid.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"persona.json"
            for cues in (None, {}, ["cold_dry_cough"], [{"id": "../audio.wav"}],
                         [{"id": "unapproved"}], [{"id": "cold_dry_cough", "weight": True}],
                         [{"id": "cold_dry_cough", "weight": float("nan")}],
                         [{"id": "cold_dry_cough", "weight": 0}],
                         [{"id": "cold_dry_cough", "unexpected": 2}],
                         [{"id": "cold_dry_cough"}]*2):
                with self.subTest(cues=cues):
                    data["performance_profile"]["cues"] = cues
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        load_patient_scenario(path)

    def test_shared_budget_and_avoid_last_audible_choice(self):
        now = [0.]
        choices = (CueChoice("cold_dry_cough"), CueChoice("cold_short_sniffle", 3))
        policy = CuePolicy(PerformanceProfile(cues=choices), lambda: now[0])
        policy.new_turn()
        self.assertTrue(policy.reserve(spontaneous=True))
        self.assertEqual(policy.last_started, float("-inf"))
        self.assertFalse(policy.reserve())
        policy.started("cold_dry_cough")
        self.assertEqual(policy.choose(choices), "cold_short_sniffle")
        self.assertEqual(policy.choose(choices, kind="cough"), "cold_dry_cough")
        self.assertIsNone(policy.choose(choices, kind="sneeze"))
        policy.new_turn()
        self.assertFalse(policy.reserve())
        now[0] = 21
        self.assertTrue(policy.reserve())
        policy.started("cold_short_sniffle")
        self.assertFalse(policy.reserve())
        self.assertEqual(policy.choose(choices), "cold_dry_cough")

    def test_catalog_tampering_and_symlinks_are_rejected(self):
        cue = load_catalog()["cold_short_sniffle"]
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            replace(cue, sha256="0"*64).verify()
        with self.assertRaises(ValueError):
            replace(cue, file="../outside.wav").path

    def test_cycle_visits_each_cue_and_only_advances_on_audible_start(self):
        profile = load_patient_scenario(ROOT/"data/prompts/01_common_cold_kid.json").performance_profile
        assert profile and profile.cues
        self.assertEqual(profile.cue_selection, "cycle")
        policy = CuePolicy(profile, lambda: 0)
        expected = {choice.id for choice in profile.cues}
        for _ in range(3):
            heard = set()
            for _ in range(len(expected)):
                choice = policy.choose(profile.cues)
                self.assertNotIn(choice, heard)
                self.assertEqual(policy.played_in_cycle, heard)
                policy.started(choice)
                heard.add(choice)
            self.assertEqual(heard, expected)
        policy.played_in_cycle.clear()
        policy.choose(profile.cues)
        self.assertEqual(policy.played_in_cycle, set())  # Unplayed reservation/cancel does not consume it.
        policy.started("cold_dry_cough")
        self.assertEqual(policy.choose(profile.cues, kind="cough"), "cold_dry_cough")
        for value in ("shuffle", None, True, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PerformanceProfile(cue_selection=value)  # pyright: ignore[reportArgumentType]

    def test_inline_schedule_retries_missed_opportunities_not_fixed_turn_numbers(self):
        profile = PerformanceProfile(cues=(CueChoice("cold_short_sniffle"),), spontaneous_every_turns=3)
        now = [0.]
        policy = CuePolicy(profile, lambda: now[0])
        policy.new_turn()
        self.assertFalse(policy.reserve(spontaneous=True, blocked=True))
        now[0] = 25
        policy.new_turn()
        self.assertTrue(policy.reserve(spontaneous=True))
        policy.started("cold_short_sniffle")
        now[0] = 50
        policy.new_turn()
        self.assertFalse(policy.reserve(spontaneous=True))
        policy.new_turn()
        self.assertFalse(policy.reserve(spontaneous=True))
        policy.new_turn()
        self.assertFalse(policy.reserve(spontaneous=True, blocked=True))
        policy.new_turn()
        self.assertTrue(policy.reserve(spontaneous=True))  # Retry turn 6, not a modulo slot.

    def test_kid_checks_each_normal_reply_but_still_obeys_cooldown(self):
        profile = load_patient_scenario(ROOT/"data/prompts/01_common_cold_kid.json").performance_profile
        assert profile
        self.assertEqual(profile.spontaneous_every_turns, 1)
        now = [0.]
        policy = CuePolicy(profile, lambda: now[0])
        policy.new_turn()
        self.assertTrue(policy.reserve(spontaneous=True))
        policy.started("cold_short_sniffle")
        policy.new_turn()
        now[0] = 19
        self.assertFalse(policy.reserve(spontaneous=True))
        now[0] = 20
        self.assertTrue(policy.reserve(spontaneous=True))
        self.assertFalse(policy.reserve(spontaneous=True))
        for interval in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                PerformanceProfile(cues=(), spontaneous_every_turns=interval)  # pyright: ignore[reportArgumentType]


class InlineGameplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sink = ControlledSink()
        self.animator = Mock()
        self.animator.push_to_talk = self.animator.evidence_open = self.animator.won = False
        self.animator._menu = None
        self.now = 100.
        self.profile = PerformanceProfile(cues=(CueChoice("cold_short_sniffle"), CueChoice("cold_dry_cough")))
        self.runtime = ConversationRuntime(
            AsyncMock(), self.animator, asyncio.Event(), {}, self.sink,
            profile=self.profile, clock=lambda: self.now,
        )
        self.tasks = [asyncio.create_task(self.runtime.process_speech()),
                      asyncio.create_task(self.runtime.playback.run()),
                      asyncio.create_task(self.runtime.finish_responses())]

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def begin(self, response_id="r"):
        await self.runtime.interrupt(new_turn=True)
        await self.runtime.request_response(self.runtime.playback.generation)
        self.runtime.handle({"type":"response.created", "response":{"id":response_id}})

    def event(self, kind, response_id="r", **values):
        self.runtime.handle({"type":kind, "response_id":response_id, **values})

    def speech(self, pcm=None, item="i", response_id="r"):
        self.event("response.output_audio.delta", response_id, item_id=item,
                   delta=base64.b64encode(pcm if pcm is not None else speech_pcm()).decode())
        self.event("response.output_audio_transcript.done", response_id, item_id=item, transcript="First phrase. Second phrase.")
        self.event("response.content_part.done", response_id, item_id=item)

    def done(self, response_id="r", status="completed"):
        self.runtime.handle({"type":"response.done", "response":{"id":response_id,"status":status}})

    async def audible(self, heard_ms):
        self.sink.ready = True
        self.sink.heard_ms = heard_ms
        await asyncio.sleep(.02)

    def active_segment(self) -> Segment:
        active = self.runtime.playback.active
        assert active is not None
        return active[0]

    async def test_patients_two_and_four_play_packaged_spontaneous_cues(self):
        for filename in ("02_stomachache_teen.json", "04_allergy_patient.json"):
            with self.subTest(patient=filename):
                profile = load_patient_scenario(ROOT / "data/prompts" / filename).performance_profile
                assert profile and profile.cues
                sink = ControlledSink()
                animator = Mock(push_to_talk=False, evidence_open=False, won=False, _menu=None)
                runtime = ConversationRuntime(
                    AsyncMock(), animator, asyncio.Event(), {}, sink,
                    profile=profile, clock=lambda: self.now,
                )
                self.tasks.extend((asyncio.create_task(runtime.process_speech()),
                                   asyncio.create_task(runtime.playback.run()),
                                   asyncio.create_task(runtime.finish_responses())))
                await runtime.interrupt(new_turn=True)
                await runtime.request_response(runtime.playback.generation)
                runtime.handle({"type": "response.created", "response": {"id": "roster"}})
                runtime.handle({
                    "type": "response.output_audio.delta", "response_id": "roster", "item_id": "speech",
                    "delta": base64.b64encode(speech_pcm()).decode(),
                })
                runtime.handle({
                    "type": "response.output_audio_transcript.done", "response_id": "roster",
                    "item_id": "speech", "transcript": "First phrase. Second phrase.",
                })
                runtime.handle({
                    "type": "response.content_part.done", "response_id": "roster", "item_id": "speech",
                })
                runtime.handle({
                    "type": "response.done", "response": {"id": "roster", "status": "completed"},
                })
                await until(lambda: runtime.playback.active is not None)
                assert runtime.playback.active is not None and runtime.policy is not None
                segment = runtime.playback.active[0]
                assert segment.insertion is not None
                self.assertIn(segment.cue_id, {choice.id for choice in profile.cues})
                self.assertEqual(sink.history, [segment.insertion.pcm])
                sink.ready = True
                sink.heard_ms = segment.insertion.spans[1].output_start // 24 + 1
                await until(lambda: runtime.policy.spontaneous_count == 1)
                self.assertEqual(animator.add_transcript.call_args.args[1], segment.cue_caption)
                sink.heard_ms = len(segment.pcm) // 48
                sink.done = True
                await until(lambda: "roster" not in runtime.responses)

    async def test_internal_cue_markers_source_time_and_cooldown(self):
        await self.begin()
        self.speech()
        await asyncio.sleep(.03)
        self.assertEqual(self.sink.history, [])  # Wait for response.done/tool priority.
        self.done()
        await until(lambda: bool(self.sink.history))
        segment = self.active_segment()
        assert self.runtime.policy is not None
        assert segment.insertion and segment.cue_id
        cue_span = segment.insertion.spans[1]
        self.assertEqual(self.sink.history, [segment.insertion.pcm])
        self.assertGreater(cue_span.output_start, 0)
        self.assertLess(cue_span.output_end, len(segment.pcm)//2)
        self.assertEqual(self.runtime.policy.last_started, float("-inf"))
        await self.audible(100)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], segment.caption)
        await self.audible(cue_span.output_start//24+1)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], segment.cue_caption)
        self.assertEqual(self.runtime.policy.last_started, 100)
        self.assertEqual(self.runtime.policy.last_cue, segment.cue_id)
        self.assertFalse(self.animator.set_talking.call_args.args[0])
        await self.audible(cue_span.output_end//24+1)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], segment.caption)
        self.assertTrue(self.animator.set_talking.call_args.args[0])
        self.sink.done = True
        await until(lambda: "r" not in self.runtime.responses)
        self.assertEqual(len(self.sink.history), 1)  # No appended second cough.

    async def test_requested_cue_beats_spontaneous_even_when_tool_arrives_after_audio(self):
        await self.begin()
        self.speech()
        await asyncio.sleep(.02)
        self.event("response.function_call_arguments.done", name="cough", call_id="c")
        self.done()
        await until(lambda: bool(self.sink.history))
        segment = self.active_segment()
        self.assertIsNone(segment.insertion)
        self.assertEqual(segment.pcm, speech_pcm())
        self.sink.ready = self.sink.done = True
        self.sink.heard_ms = len(segment.pcm)//48
        await until(lambda: len(self.sink.history)==2)
        requested = self.active_segment()
        self.assertEqual(requested.cue_id, "cold_dry_cough")
        self.assertEqual(requested.kind, "cue")
        self.assertEqual(requested.pcm, self.runtime.cue_audio["cold_dry_cough"])

    async def test_no_gap_cancelled_and_blocked_responses_never_add_cue(self):
        await self.begin()
        self.speech(struct.pack("<h", 3000)*24000)
        self.done()
        await until(lambda: bool(self.sink.history))
        self.assertIsNone(self.active_segment().insertion)
        self.sink.ready = self.sink.done = True
        await until(lambda: "r" not in self.runtime.responses)
        self.assertEqual(len(self.sink.history),1)
        await self.begin("cancelled")
        self.speech(response_id="cancelled")
        self.done("cancelled", "cancelled")
        await until(lambda: "cancelled" not in self.runtime.responses)
        self.assertEqual(len(self.sink.history),1)

    async def test_interrupt_while_waiting_for_terminal_event_discards_audio(self):
        await self.begin()
        self.speech()
        await until(lambda: self.runtime.processing_task is not None)
        await self.runtime.interrupt(new_turn=True)
        self.done()
        await until(lambda: "r" not in self.runtime.responses)
        self.assertEqual(self.sink.history, [])
        self.assertEqual(self.runtime.processing_bytes, 0)

    async def test_one_cue_across_multiple_speech_parts(self):
        await self.begin()
        self.speech(item="one")
        self.speech(item="two")
        self.done()
        await until(lambda: self.runtime.playback.active is not None
                    and self.runtime.playback.queue.qsize() == 1)
        first = self.active_segment()
        queued = self.runtime.playback.queue.get_nowait()
        second, _ = queued
        self.runtime.playback.queue.put_nowait(queued)
        self.assertIsNotNone(first.insertion)
        self.assertIsNone(second.insertion)

    async def test_diagnostic_and_continuation_suppress_inline_cues(self):
        await self.begin()
        self.speech()
        self.event("response.function_call_arguments.done", name="win", call_id="w")
        self.done()
        await until(lambda: bool(self.sink.history))
        self.assertIsNone(self.active_segment().insertion)
        await self.runtime.interrupt(new_turn=True)
        await self.begin("continuation")
        self.runtime.continuations = 1
        self.speech(response_id="continuation")
        self.done("continuation")
        await until(lambda: len(self.sink.history)==2)
        self.assertIsNone(self.active_segment().insertion)


class MappedPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_before_during_after_maps_to_original_speech(self):
        speech = speech_pcm()
        cue = b"\0\0"+struct.pack("<h",1000)*24000+b"\0\0"
        insertion = insert_cue(speech, cue, internal_pauses(speech)[0])
        span = insertion.spans[1]
        for heard in (200, span.output_start//24+10, span.output_end//24+100):
            with self.subTest(heard=heard):
                sink = ControlledSink()
                markers = Mock()
                player = PlaybackController(sink, Mock(), Mock(), markers)
                segment = Segment(0,"r","item",insertion.pcm,"Full transcript",
                                  insertion=insertion,cue_id="test",cue_caption="[sniffles]")
                player.enqueue(segment)
                worker = asyncio.create_task(player.run())
                try:
                    await until(lambda: player.active is not None)
                    sink.ready = True
                    sink.heard_ms = heard
                    events = player.interrupt()
                    self.assertEqual(len(events),1)
                    self.assertEqual(events[0]["audio_end_ms"], insertion.source_sample_at(heard*24)//24)
                finally:
                    worker.cancel()
                    await asyncio.gather(worker,return_exceptions=True)

    async def test_markers_reset_for_next_segment_and_unplayed_cue_never_starts(self):
        speech = speech_pcm()
        cue = b"\0\0"+struct.pack("<h",1000)*24000+b"\0\0"
        insertion = insert_cue(speech,cue,internal_pauses(speech)[0])
        sink = ControlledSink()
        markers = Mock()
        player = PlaybackController(sink,Mock(),Mock(),markers)
        for index in range(2):
            player.enqueue(Segment(0,"r",str(index),insertion.pcm,"Text",insertion=insertion))
        worker = asyncio.create_task(player.run())
        try:
            await until(lambda: player.active is not None)
            sink.ready = True
            sink.heard_ms = len(insertion.pcm)//48
            sink.done = True
            await until(lambda: len(sink.history)==2)
            self.assertEqual(markers.call_count,2)
            sink.ready = True
            sink.heard_ms = insertion.spans[1].output_start//24+1
            await until(lambda: markers.call_count==3)
            player.interrupt()
        finally:
            worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)
        sink = ControlledSink()
        markers = Mock()
        player = PlaybackController(sink,Mock(),Mock(),markers)
        player.enqueue(Segment(0,"r","never",insertion.pcm,"Text",insertion=insertion))
        player.interrupt()
        markers.assert_not_called()


class AllCueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        profile = load_patient_scenario(ROOT/"data/prompts/01_common_cold_kid.json").performance_profile
        assert profile
        profile = replace(profile, voice=None)
        sink = ControlledSink()
        animator = Mock()
        animator.push_to_talk = animator.evidence_open = animator.won = False
        animator._menu = None
        self.now = 100.
        runtime = ConversationRuntime(AsyncMock(), animator, asyncio.Event(), {}, sink,
                                      profile=profile, clock=lambda: self.now)
        return runtime, sink, animator

    async def test_each_requested_tool_plays_its_exact_asset_and_caption(self):
        for kind in ("sniffle", "sneeze", "throat_clear", "cough"):
            with self.subTest(kind=kind):
                runtime, sink, animator = self.runtime()
                tasks = [asyncio.create_task(runtime.playback.run()),
                         asyncio.create_task(runtime.finish_responses()),
                         asyncio.create_task(runtime.process_speech())]
                try:
                    await runtime.interrupt(new_turn=True)
                    await runtime.request_response(runtime.playback.generation)
                    runtime.handle({"type":"response.created","response":{"id":"r"}})
                    runtime.handle({"type":"response.function_call_arguments.done","response_id":"r",
                                    "name":kind,"call_id":"requested"})
                    runtime.handle({"type":"response.done","response":{"id":"r","status":"completed"}})
                    await until(lambda: bool(sink.history))
                    active = runtime.playback.active
                    assert active is not None
                    segment = active[0]
                    assert segment.cue_id is not None
                    self.assertEqual(runtime.catalog[segment.cue_id].kind, kind)
                    self.assertEqual(segment.pcm, runtime.cue_audio[segment.cue_id])
                    animator.add_transcript.assert_not_called()
                    sink.ready = True
                    await until(lambda: animator.add_transcript.called)
                    self.assertEqual(animator.add_transcript.call_args.args[1], runtime.catalog[segment.cue_id].caption)
                    assert runtime.policy is not None
                    self.assertEqual(runtime.policy.last_cue, segment.cue_id)
                    self.assertFalse(runtime.policy.reserve())
                    sink.done = True
                    await until(lambda: "r" not in runtime.responses)
                    outputs = [json.loads(call.args[0]) for call in runtime.websocket.send.call_args_list]
                    tool_output = next(event["item"]["output"] for event in outputs
                                       if event["type"] == "conversation.item.create")
                    self.assertEqual(json.loads(tool_output), {"status":"played"})
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

    async def test_all_four_occur_inside_consecutive_ordinary_replies_without_tool_requests(self):
        runtime, sink, animator = self.runtime()
        tasks = [asyncio.create_task(runtime.playback.run()),
                 asyncio.create_task(runtime.finish_responses()),
                 asyncio.create_task(runtime.process_speech())]
        heard = set()
        try:
            for index in range(4):
                self.now += 21
                await runtime.interrupt(new_turn=True)
                response_id = f"r{index}"
                await runtime.request_response(runtime.playback.generation)
                runtime.handle({"type":"response.created","response":{"id":response_id}})
                for kind, values in (
                    ("response.output_audio.delta", {"delta":base64.b64encode(speech_pcm()).decode()}),
                    ("response.output_audio_transcript.done", {"transcript":"Before. After."}),
                    ("response.content_part.done", {}),
                ):
                    runtime.handle({"type":kind,"response_id":response_id,"item_id":"i",**values})
                runtime.handle({"type":"response.done","response":{"id":response_id,"status":"completed"}})
                await until(lambda: len(sink.history) == index+1)
                active = runtime.playback.active
                assert active is not None
                segment = active[0]
                assert segment.insertion and segment.cue_id
                cue = runtime.catalog[segment.cue_id]
                self.assertNotIn(cue.kind, heard)
                heard.add(cue.kind)
                span = segment.insertion.spans[1]
                self.assertEqual(segment.pcm[span.output_start*2:span.output_end*2],
                                 runtime.cue_audio[segment.cue_id])
                self.assertTrue(segment.pcm.endswith(struct.pack("<h",-2000)*24000))
                sink.ready = True
                sink.heard_ms = span.output_start//24+1
                await until(lambda: runtime.active_cue_segment is segment)
                self.assertEqual(animator.add_transcript.call_args.args[1], cue.caption)
                sink.heard_ms = span.output_end//24+1
                await until(lambda: runtime.active_cue_segment is None)
                self.assertTrue(animator.set_talking.call_args.args[0])
                sink.done = True
                await until(lambda: response_id not in runtime.responses)
            self.assertEqual(heard, {"cough", "sniffle", "sneeze", "throat_clear"})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_no_pause_reply_does_not_delay_cue_on_next_normal_reply(self):
        runtime, sink, _ = self.runtime()
        tasks = [asyncio.create_task(runtime.playback.run()),
                 asyncio.create_task(runtime.finish_responses()),
                 asyncio.create_task(runtime.process_speech())]
        try:
            for index, pcm in enumerate((struct.pack("<h", 2000)*24000, speech_pcm())):
                await runtime.interrupt(new_turn=True)
                rid = str(index)
                await runtime.request_response(runtime.playback.generation)
                runtime.handle({"type":"response.created","response":{"id":rid}})
                for kind, values in (
                    ("response.output_audio.delta", {"delta":base64.b64encode(pcm).decode()}),
                    ("response.output_audio_transcript.done", {"transcript":"Ordinary patient reply."}),
                    ("response.content_part.done", {}),
                ):
                    runtime.handle({"type":kind,"response_id":rid,"item_id":"i",**values})
                runtime.handle({"type":"response.done","response":{"id":rid,"status":"completed"}})
                await until(lambda: len(sink.history)==index+1)
                active = runtime.playback.active
                assert active
                if index == 0:
                    self.assertIsNone(active[0].insertion)
                else:
                    self.assertIsNotNone(active[0].insertion)
                sink.ready = sink.done = True
                sink.heard_ms = len(active[0].pcm)//48
                await until(lambda: rid not in runtime.responses)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
