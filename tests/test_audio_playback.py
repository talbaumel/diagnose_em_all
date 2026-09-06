from __future__ import annotations

import asyncio
import base64
import json
import threading
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.audio_playback import AudioPlaybackError, DeviceSink, MAX_AUDIO_BYTES, PlaybackController, Segment
from src.conversation_runtime import ConversationRuntime
from src.hospital_game import load_patient_scenario
from src.patient_performance import COUGH_CLIP, CoughPolicy, PerformanceProfile, read_pcm_clip

ROOT = Path(__file__).resolve().parents[1]


class FakeBufferedSink:
    def __init__(self):
        self.now = 0.0
        self.start = 0.0
        self.pcm = b""
        self.history = []
        self.aborts = 0
        self.latency = 0.1

    def begin(self, pcm):
        self.pcm = pcm
        self.history.append(pcm)
        self.start = self.now + self.latency

    def state(self):
        elapsed = max(0, self.now - self.start)
        return self.now >= self.start, elapsed >= len(self.pcm) / 48000, min(int(elapsed * 1000), len(self.pcm) // 48)

    def abort(self):
        self.aborts += 1
        self.pcm = b""

    def close(self):
        self.abort()


async def poll(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.006)
    raise AssertionError("Timed out waiting for deterministic worker")


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sink = FakeBufferedSink()
        self.started = Mock()
        self.ended = Mock()
        self.player = PlaybackController(self.sink, self.started, self.ended)
        self.task = asyncio.create_task(self.player.run())

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def test_buffered_is_not_played_and_segments_never_overlap(self):
        first = self.player.enqueue(Segment(0, "r", "a", b"\1\0" * 24000, "Hello"))
        second = self.player.enqueue(Segment(0, "r", "b", b"\2\0" * 24000, "[coughs]", "cough"))
        await poll(lambda: len(self.sink.history) == 1)
        self.started.assert_not_called()
        self.assertFalse(first.done())
        self.sink.now = 0.6
        await poll(lambda: self.started.called)
        self.assertFalse(first.done())
        self.assertEqual(len(self.sink.history), 1)
        self.sink.now = 1.2
        await poll(lambda: len(self.sink.history) == 2)
        self.assertEqual(await first, "played")
        self.assertFalse(second.done())
        self.sink.now = 2.4
        await poll(second.done)
        self.assertEqual(await second, "played")

    async def test_interrupt_aborts_buffered_audio_and_truncates_heard_time(self):
        active = self.player.enqueue(Segment(0, "old", "a", b"\1\0" * 24000, "Long answer"))
        queued = self.player.enqueue(Segment(0, "old", "b", b"\2\0" * 24000, "Never heard"))
        await poll(lambda: self.player.active is not None)
        self.sink.now = 0.35
        await poll(lambda: self.started.called)
        truncations = self.player.interrupt()
        self.assertEqual([event["audio_end_ms"] for event in truncations], [249, 0])
        self.assertEqual(await active, "interrupted")
        self.assertEqual(await queued, "skipped")
        self.assertEqual(self.sink.aborts, 1)
        late = self.player.enqueue(Segment(0, "old", "c", b"\3\0", "Late"))
        self.assertEqual(await late, "skipped")
        fresh = self.player.enqueue(Segment(1, "new", "d", b"\4\0" * 24, "New"))
        await poll(lambda: len(self.sink.history) == 2)
        self.sink.now += 1
        await poll(fresh.done)
        self.assertEqual(self.player.queued_bytes, 0)

    async def test_queue_overflow_is_explicit(self):
        self.player.enqueue(Segment(0, "r", "a", bytes(MAX_AUDIO_BYTES), "full"))
        with self.assertRaisesRegex(AudioPlaybackError, "overflow"):
            self.player.enqueue(Segment(0, "r", "b", b"\0\0", "overflow"))

    async def test_interrupt_before_dac_onset_does_not_claim_audible_cough(self):
        ticket = self.player.enqueue(Segment(0, "r", "c", b"\1\0" * 24, "[coughs]", "cough"))
        await poll(lambda: self.player.active is not None)
        self.player.interrupt()
        self.assertEqual(await ticket, "skipped")
        self.started.assert_not_called()


class DeviceTests(unittest.TestCase):
    def test_callback_completion_waits_for_dac_and_abort_clears_future_callbacks(self):
        stream = Mock()
        stream.time = 5.0
        with patch("src.audio_playback.sd.RawOutputStream", return_value=stream):
            sink = DeviceSink()
        audio = b"\x12\x34" * 480
        sink.begin(audio)
        buffer = bytearray(960)
        sink._callback(buffer, 480, SimpleNamespace(outputBufferDacTime=5.1), False)
        self.assertEqual(buffer, audio)
        self.assertEqual(sink.state(), (False, False, 0))
        stream.time = 5.11
        self.assertTrue(sink.state()[0])
        self.assertFalse(sink.state()[1])
        stream.time = 5.13
        self.assertTrue(sink.state()[1])
        sink.abort()
        sink._callback(buffer, 480, SimpleNamespace(outputBufferDacTime=5.2), False)
        self.assertEqual(buffer, bytes(960))
        stream.abort.assert_called_once()
        sink.close()

    def test_abort_does_not_hold_callback_lock(self):
        stream = Mock()
        with patch("src.audio_playback.sd.RawOutputStream", return_value=stream):
            sink = DeviceSink()
        acquired = []
        def abort():
            worker = threading.Thread(target=lambda: (sink._lock.acquire(), acquired.append(True), sink._lock.release()))
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
        stream.abort.side_effect = abort
        sink.abort()
        self.assertEqual(acquired, [True])

    def test_stopped_device_is_reported_instead_of_waiting_forever(self):
        stream = Mock()
        stream.active = False
        with patch("src.audio_playback.sd.RawOutputStream", return_value=stream):
            sink = DeviceSink()
        with self.assertRaisesRegex(AudioPlaybackError, "device stopped"):
            sink.state()
        sink.close()


class ConfigurationTests(unittest.TestCase):
    def test_profile_optional_and_only_cold_child_enabled(self):
        scenarios = [load_patient_scenario(path) for path in sorted((ROOT / "data/prompts").glob("*.json"))]
        self.assertIsNotNone(scenarios[0].performance_profile)
        self.assertTrue(all(s.performance_profile is None for s in scenarios[1:]))
        self.assertNotIn("performance_profile", scenarios[1].conversation_parameters())
        self.assertIs(scenarios[0].conversation_parameters()["performance_profile"], scenarios[0].performance_profile)
        self.assertEqual(len(scenarios[0].tests), 2)

    def test_real_clip_format(self):
        pcm = read_pcm_clip(ROOT / COUGH_CLIP)
        self.assertGreater(len(pcm), 0)
        self.assertLess(len(pcm), 48000 * 2)
        self.assertTrue(any(pcm))

    def test_invalid_profiles(self):
        for values in (
            {"cough_clip": "../cough.wav"}, {"cough_clip": "https://example.org/cough.wav"},
            {"cough_clip": str(ROOT / COUGH_CLIP)}, {"cough_clip": 42},
            {"cooldown_seconds": float("nan")}, {"cooldown_seconds": True},
            {"cooldown_seconds": 0}, {"spontaneous_every_turns": 1},
            {"spontaneous_every_turns": 2.5},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                PerformanceProfile(**values)

    def test_invalid_and_missing_clips_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.wav"
            with self.assertRaises(FileNotFoundError):
                read_pcm_clip(path)
            path.write_bytes(b"not a wave")
            with self.assertRaisesRegex(ValueError, "Invalid WAV"):
                read_pcm_clip(path)
            for channels, rate, width, frames in (
                (2, 24000, 2, 10), (1, 44100, 2, 10),
                (1, 24000, 1, 10), (1, 24000, 2, 0),
            ):
                with wave.open(str(path), "wb") as clip:
                    clip.setparams((channels, width, rate, 0, "NONE", "not compressed"))
                    clip.writeframes(bytes(channels * width * frames))
                with self.assertRaisesRegex(ValueError, "mono 24 kHz"):
                    read_pcm_clip(path)

    def test_cooldown_turn_limit_and_spontaneous_schedule(self):
        now = [0.0]
        policy = CoughPolicy(PerformanceProfile(), lambda: now[0])
        self.assertFalse(policy.reserve())
        policy.new_turn()
        self.assertTrue(policy.reserve())
        policy.started()
        now[0] = 30
        self.assertFalse(policy.reserve())
        policy.new_turn()
        self.assertFalse(policy.reserve(spontaneous=True))
        policy.new_turn()
        self.assertFalse(policy.reserve(blocked=True))
        policy.new_turn()
        self.assertTrue(policy.reserve(spontaneous=True))
        policy.started()
        policy.new_turn()
        now[0] = 31
        self.assertFalse(policy.reserve())
        now[0] = 51
        self.assertTrue(policy.reserve())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sink = FakeBufferedSink()
        self.animator = Mock()
        self.animator.push_to_talk = self.animator.evidence_open = self.animator.won = False
        self.animator._menu = None
        self.websocket = AsyncMock()
        self.runtime = ConversationRuntime(
            self.websocket, self.animator, asyncio.Event(), {}, self.sink,
            profile=PerformanceProfile(), clock=lambda: self.sink.now,
        )
        self.tasks = [
            asyncio.create_task(self.runtime.playback.run()),
            asyncio.create_task(self.runtime.finish_responses()),
            asyncio.create_task(self.runtime.send_user_requests()),
        ]

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def sent(self, kind):
        return [event for call in self.websocket.send.call_args_list if (event := json.loads(call.args[0]))["type"] == kind]

    async def response(self, name="r"):
        await self.runtime.interrupt(new_turn=True)
        await self.runtime.request_response(self.runtime.playback.generation)
        self.runtime.handle({"type": "response.created", "response": {"id": name}})

    def event(self, kind, **values):
        self.runtime.handle({"type": kind, "response_id": "r", **values})

    def speech(self):
        self.event("response.output_audio.delta", item_id="speech", delta=base64.b64encode(b"\1\0" * 24000).decode())
        self.event("response.output_audio_transcript.done", item_id="speech", transcript="My throat feels scratchy.")
        self.event("response.content_part.done", item_id="speech")

    def done(self):
        self.runtime.handle({"type": "response.done", "response": {"id": "r", "status": "completed"}})

    async def test_tool_waits_for_response_and_speaker_drain_without_popup(self):
        await self.response()
        self.speech()
        self.event("response.function_call_arguments.done", name="cough", call_id="c")
        await poll(lambda: self.runtime.playback.active is not None)
        self.sink.now = 2
        await poll(lambda: self.runtime.playback.active is None)
        self.assertEqual(len(self.sink.history), 1)
        self.assertEqual(len(self.sent("response.create")), 1)
        self.done()
        await poll(lambda: len(self.sink.history) == 2)
        self.assertEqual(self.sent("conversation.item.create"), [])
        self.sink.now = 2.2
        await poll(lambda: self.animator._state == "worried")
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "[coughs]")
        self.sink.now = 5
        await poll(lambda: len(self.sent("response.create")) == 2)
        outputs = self.sent("conversation.item.create")
        self.assertEqual(json.loads(outputs[0]["item"]["output"]), {"status": "played"})
        self.assertEqual(self.sent("response.create")[-1]["response"], {"tool_choice": "none"})
        self.animator.show_test_result.assert_not_called()

    async def test_response_done_before_drain_does_not_continue(self):
        await self.response()
        self.speech()
        self.event("response.function_call_arguments.done", name="cough", call_id="c")
        self.done()
        await poll(lambda: len(self.sink.history) == 1)
        self.assertEqual(len(self.sent("response.create")), 1)
        self.assertTrue(self.runtime.idle.is_set())
        self.sink.now = 1.2
        await poll(lambda: len(self.sink.history) == 2)
        self.assertEqual(len(self.sent("response.create")), 1)

    async def test_cough_interruption_reports_truth_and_no_stale_continuation(self):
        await self.response()
        self.event("response.function_call_arguments.done", name="cough", call_id="c")
        self.done()
        await poll(lambda: self.runtime.playback.active is not None)
        self.sink.now = 0.2
        await poll(lambda: self.runtime.playback.started)
        await self.runtime.interrupt(new_turn=True)
        await poll(lambda: bool(self.sent("conversation.item.create")))
        result = self.sent("conversation.item.create")[0]["item"]
        self.assertEqual(json.loads(result["output"])["status"], "interrupted")
        self.assertEqual(len(self.sent("response.create")), 1)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "[cough interrupted]")

    async def test_cancel_late_events_cannot_restart_old_speech_or_captions(self):
        await self.response()
        self.speech()
        await poll(lambda: self.runtime.playback.active is not None)
        self.sink.now = 0.4
        await poll(lambda: self.runtime.playback.started)
        await self.runtime.interrupt(new_turn=True)
        self.speech()
        self.done()
        await poll(lambda: not self.runtime.responses)
        self.assertEqual(len(self.sink.history), 1)
        self.assertEqual(len(self.sent("response.cancel")), 1)
        self.assertGreater(self.sent("conversation.item.truncate")[0]["audio_end_ms"], 0)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "[speech interrupted]")

    async def test_duplicate_cough_calls_only_play_once(self):
        await self.response()
        for call_id in ("one", "one", "two"):
            self.event("response.function_call_arguments.done", name="cough", call_id=call_id)
        self.done()
        await poll(lambda: len(self.sink.history) == 1)
        self.sink.now = 3
        await poll(lambda: len(self.sent("conversation.item.create")) == 2)
        statuses = [json.loads(event["item"]["output"])["status"] for event in self.sent("conversation.item.create")]
        self.assertEqual(statuses, ["played", "skipped"])
        self.assertEqual(len(self.sink.history), 1)

    async def test_spontaneous_cough_at_response_boundary_only(self):
        await self.response()
        self.speech()
        await poll(lambda: len(self.sink.history) == 1)
        self.sink.now = 2
        await poll(lambda: self.runtime.playback.active is None)
        self.assertEqual(len(self.sink.history), 1)
        self.done()
        await poll(lambda: len(self.sink.history) == 2)
        self.assertEqual(self.sent("conversation.item.create"), [])

    async def test_cancelled_response_does_not_spontaneously_cough(self):
        await self.response()
        self.event("response.output_audio.delta", item_id="speech", delta="AAA=")
        self.runtime.handle({"type": "response.done", "response": {"id": "r", "status": "cancelled"}})
        await poll(lambda: not self.runtime.responses)
        self.assertEqual(self.sink.history, [])
        self.assertEqual(self.runtime.buffered_bytes, 0)

    async def test_request_cooldown_reports_skipped_and_continues_without_loop(self):
        await self.response()
        self.runtime.policy.last_started = self.sink.now
        self.event("response.function_call_arguments.done", name="cough", call_id="cooldown")
        self.done()
        await poll(lambda: len(self.sent("response.create")) == 2)
        self.assertEqual(self.sink.history, [])
        self.assertEqual(json.loads(self.sent("conversation.item.create")[0]["item"]["output"])["status"], "skipped")

    async def test_blocked_cough_skips_without_display_or_playback(self):
        await self.response()
        response = self.runtime.responses["r"]
        for attribute in ("push_to_talk", "evidence_open", "won"):
            setattr(self.animator, attribute, True)
            self.assertEqual(await self.runtime.cough(response), "skipped")
            setattr(self.animator, attribute, False)
        self.assertEqual(self.sink.history, [])
        self.animator.add_transcript.assert_not_called()

    async def test_cancel_in_flight_creation_waits_for_terminal_response(self):
        await self.runtime.interrupt(new_turn=True)
        await self.runtime.request_response(self.runtime.playback.generation)
        await self.runtime.interrupt(new_turn=True)
        self.runtime.queue_user("Another question")
        self.runtime.handle({"type": "response.created", "response": {"id": "old"}})
        await asyncio.sleep(0.01)
        self.assertEqual(len(self.sent("response.create")), 1)
        self.runtime.handle({"type": "response.done", "response": {"id": "old", "status": "cancelled"}})
        await poll(lambda: len(self.sent("response.create")) == 2)

    async def test_missing_transcript_and_buffer_overflow_are_explicit(self):
        await self.response()
        self.event("response.output_audio.delta", item_id="bad", delta="AAA=")
        with self.assertRaisesRegex(AudioPlaybackError, "transcript"):
            self.event("response.content_part.done", item_id="bad")
        self.runtime.buffered_bytes = MAX_AUDIO_BYTES
        with self.assertRaisesRegex(AudioPlaybackError, "overflow"):
            self.event("response.output_audio.delta", item_id="huge", delta="AAA=")

    async def test_evidence_audio_uses_playback_queue_and_close_interrupts(self):
        closed = asyncio.Event()
        self.animator.wait_for_evidence_close = AsyncMock(side_effect=closed.wait)
        self.animator.show_test_result.side_effect = lambda test: setattr(self.animator, "evidence_open", True)
        self.runtime.tests["exam"] = SimpleNamespace(
            description="Example evidence", results="Result", audio_path=ROOT / COUGH_CLIP,
        )
        await self.response()
        self.event("response.function_call_arguments.done", name="exam", call_id="exam")
        self.done()
        await poll(lambda: bool(self.sink.history))
        self.assertEqual(self.sink.history[0], read_pcm_clip(ROOT / COUGH_CLIP))
        active = self.runtime.playback.active
        assert active is not None
        self.assertEqual(active[0].kind, "evidence")
        self.sink.now = .2
        await poll(lambda: self.runtime.playback.started)
        self.animator.evidence_open = False
        closed.set()
        await poll(lambda: bool(self.sent("conversation.item.create")))
        output = json.loads(self.sent("conversation.item.create")[0]["item"]["output"])
        self.assertEqual(output["audio_status"], "interrupted")
        self.assertEqual(self.runtime.playback.queued_bytes, 0)
        self.assertGreater(self.sink.aborts, 0)

    async def test_missing_evidence_audio_fails_through_runtime(self):
        self.runtime.tests["exam"] = SimpleNamespace(
            description="Evidence", results="Result", audio_path=ROOT / "missing-evidence.wav",
        )
        await self.response()
        self.event("response.function_call_arguments.done", name="exam", call_id="exam")
        self.done()
        with self.assertRaisesRegex(AudioPlaybackError, "Test audio unavailable"):
            await self.tasks[1]
        self.assertEqual(self.sink.history, [])


if __name__ == "__main__":
    unittest.main()
