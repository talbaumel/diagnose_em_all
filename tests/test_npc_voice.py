from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.audio_playback import AudioPlaybackError, MAX_AUDIO_BYTES
from src.conversation_runtime import ConversationRuntime
from src.hospital_game import load_patient_scenario
from src.patient_performance import PerformanceProfile
from src.speech_processor import SpeechProcessor
from src.voice_profile import VoiceProfile
from tests.audio_fakes import ControlledSink, until

ROOT = Path(__file__).resolve().parents[1]
HAS_VOICE = all(importlib.util.find_spec(name) is not None for name in ("numpy", "pyworld"))


class VoiceConfigurationTests(unittest.TestCase):
    def test_selected_profile_and_other_npcs(self):
        scenarios = [load_patient_scenario(path) for path in sorted((ROOT / "data/prompts").glob("*.json"))]
        profile = scenarios[0].performance_profile
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.voice, VoiceProfile(enabled=True))
        assert profile.voice is not None
        self.assertIn("local processor", profile.instructions)
        self.assertEqual(profile.voice.edge_ms, 0)
        self.assertTrue(all(scenario.performance_profile is None for scenario in scenarios[1:]))
        self.assertIsNone(PerformanceProfile().voice)

    def test_json_disabled_missing_and_invalid(self):
        source = json.loads((ROOT / "data/prompts/01_common_cold_kid.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "patient.json"
            for value in (None, [], True, {"enabled": 1}, {"strength": True},
                          {"strength": float("nan")}, {"strength": .81}, {"edge_ms": -1},
                          {"edge_ms": float("inf")}, {"band": "low"},
                          {"processor": "unknown"}, {"pitch_method": "other"}, {"typo": 2},
                          {"band": "high", "strength": .8}):
                with self.subTest(value=value):
                    source["performance_profile"]["voice"] = value
                    path.write_text(json.dumps(source))
                    with self.assertRaisesRegex(ValueError, "Invalid performance profile"):
                        load_patient_scenario(path)
            source["performance_profile"]["voice"] = {"enabled": False}
            path.write_text(json.dumps(source))
            profile = load_patient_scenario(path).performance_profile
            assert profile and profile.voice
            self.assertFalse(profile.voice.enabled)
            self.assertNotIn("local processor", profile.instructions)
            del source["performance_profile"]["voice"]
            path.write_text(json.dumps(source))
            profile = load_patient_scenario(path).performance_profile
            assert profile
            self.assertIsNone(profile.voice)

    def test_missing_dependencies_are_actionable_only_when_enabled(self):
        with patch("src.speech_processor.importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(AudioPlaybackError, "uv sync --locked"):
                SpeechProcessor(VoiceProfile(enabled=True))
            SpeechProcessor(VoiceProfile(enabled=False))


class VoiceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sink = ControlledSink()
        self.animator = Mock()
        self.animator.push_to_talk = self.animator.evidence_open = self.animator.won = False
        self.animator._menu = None
        self.stop = asyncio.Event()
        self.socket = AsyncMock()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.original = b"\1\0" * 24000
        self.transformed = b"\2\0" * 24000

        async def process(pcm):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            return self.transformed

        self.processor = AsyncMock(side_effect=process)
        self.runtime = ConversationRuntime(
            self.socket, self.animator, self.stop, {}, self.sink,
            profile=PerformanceProfile(), speech_processor=self.processor,
        )
        self.work = asyncio.create_task(self.runtime.process_speech())
        self.tasks = [self.work, asyncio.create_task(self.runtime.playback.run()),
                      asyncio.create_task(self.runtime.finish_responses())]

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def begin(self, name="r"):
        await self.runtime.interrupt(new_turn=True)
        await self.runtime.request_response(self.runtime.playback.generation)
        self.runtime.handle({"type": "response.created", "response": {"id": name}})

    def speech(self, name="r", item="speech"):
        for kind, values in (
            ("response.output_audio.delta", {"delta": base64.b64encode(self.original).decode()}),
            ("response.output_audio_transcript.done", {"transcript": "My nose feels blocked."}),
            ("response.content_part.done", {}),
        ):
            self.runtime.handle({"type": kind, "response_id": name, "item_id": item, **values})

    def done(self, name="r"):
        self.runtime.handle({"type": "response.done", "response": {"id": name, "status": "completed"}})

    def sent(self, kind):
        return [event for call in self.socket.send.call_args_list
                if (event := json.loads(call.args[0]))["type"] == kind]

    async def test_receiver_and_captions_wait_only_for_actual_playback(self):
        await self.begin()
        self.speech()
        await self.started.wait()
        self.done()
        self.assertTrue(self.runtime.idle.is_set())
        self.assertEqual(self.sink.history, [])
        self.animator.add_transcript.assert_not_called()
        self.assertEqual(self.runtime.processing_bytes, len(self.original))
        self.release.set()
        await until(lambda: bool(self.sink.history))
        self.assertEqual(self.sink.history, [self.transformed])
        self.animator.add_transcript.assert_not_called()
        self.sink.ready = True
        await until(lambda: self.animator.add_transcript.called)
        self.assertEqual(self.animator.add_transcript.call_args.args[1], "My nose feels blocked.")
        self.sink.done = True
        await until(lambda: len(self.sink.history) == 2)
        self.assertEqual(self.sink.history[1], self.runtime.cough_pcm)
        self.processor.assert_awaited_once_with(self.original)
        self.assertEqual(self.runtime.processing_bytes, 0)

    async def test_interrupt_cancels_pending_and_preserves_new_turn(self):
        await self.begin()
        self.speech()
        self.speech(item="queued")
        await self.started.wait()
        response = self.runtime.responses["r"]
        started = time.monotonic()
        await self.runtime.interrupt(new_turn=True)
        self.assertLess(time.monotonic() - started, .2)
        await self.cancelled.wait()
        self.assertTrue(all(ticket.done() and ticket.result() == "skipped" for ticket in response.tickets))
        truncations = self.sent("conversation.item.truncate")
        self.assertEqual({event["item_id"] for event in truncations}, {"speech", "queued"})
        self.assertTrue(all(event["audio_end_ms"] == 0 for event in truncations))
        self.assertEqual(self.runtime.processing_bytes, 0)
        self.assertTrue(self.runtime.speech_queue.empty())
        self.assertEqual(self.sink.history, [])
        self.done()
        self.release.set()
        await self.begin("next")
        self.speech("next")
        await until(lambda: bool(self.sink.history))
        self.assertEqual(self.sink.history, [self.transformed])

    async def test_stale_result_is_discarded_even_if_processor_ignores_cancel(self):
        async def ignores_cancel(pcm):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            return self.transformed
        self.runtime.speech_processor = ignores_cancel
        await self.begin()
        self.speech()
        await self.started.wait()
        await self.runtime.interrupt(new_turn=True)
        self.release.set()
        await until(lambda: self.runtime.processing_task is None)
        self.assertEqual(self.sink.history, [])
        self.animator.add_transcript.assert_not_called()

    async def test_transformed_playback_truncates_same_source_time(self):
        await self.begin()
        self.release.set()
        self.speech()
        await until(lambda: bool(self.sink.history))
        self.sink.ready = True
        self.sink.heard_ms = 420
        await self.runtime.interrupt(new_turn=True)
        self.assertEqual(self.sent("conversation.item.truncate")[-1]["audio_end_ms"], 420)

    async def test_bad_duration_surfaces_and_settles_ticket(self):
        self.runtime.speech_processor = AsyncMock(return_value=b"\0\0")
        await self.begin()
        self.speech()
        ticket = self.runtime.responses["r"].tickets[0]
        with self.assertRaisesRegex(AudioPlaybackError, "changed duration"):
            await self.work
        self.assertTrue(ticket.done())
        self.assertEqual(self.runtime.processing_bytes, 0)
        self.assertEqual(self.sink.history, [])

    async def test_pending_processing_counts_toward_buffer_limit(self):
        await self.begin()
        self.speech()
        await self.started.wait()
        self.runtime.buffered_bytes = MAX_AUDIO_BYTES - len(self.original)
        with self.assertRaisesRegex(AudioPlaybackError, "buffer overflow"):
            self.runtime.handle({"type": "response.output_audio.delta", "response_id": "r",
                                 "item_id": "extra", "delta": base64.b64encode(b"\0\0").decode()})

    async def test_disabled_profile_is_exact_passthrough_without_worker(self):
        profile = PerformanceProfile(voice=VoiceProfile(enabled=False))
        with patch("src.conversation_runtime.SpeechProcessor") as factory:
            runtime = ConversationRuntime(self.socket, self.animator, self.stop, {}, self.sink, profile=profile)
            factory.assert_not_called()
        runtime.requested_generation = 0
        runtime.handle({"type": "response.created", "response": {"id": "plain"}})
        for kind, values in (
            ("response.output_audio.delta", {"delta": base64.b64encode(self.original).decode()}),
            ("response.output_audio_transcript.done", {"transcript": "Hello"}),
            ("response.content_part.done", {}),
        ):
            runtime.handle({"type": kind, "response_id": "plain", "item_id": "i", **values})
        segment, _ = runtime.playback.queue.get_nowait()
        self.assertEqual(segment.pcm, self.original)
        self.assertEqual(runtime.processing_bytes, 0)


class VoiceProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_terminates_and_reaps_child(self):
        with patch("src.speech_processor.importlib.util.find_spec", return_value=object()):
            processor = SpeechProcessor(VoiceProfile(enabled=True))
        child = Mock()
        child.returncode = None
        entered = asyncio.Event()
        async def communicate(pcm):
            entered.set()
            await asyncio.Event().wait()
        async def wait():
            child.returncode = -15
            return -15
        child.communicate = AsyncMock(side_effect=communicate)
        child.wait = AsyncMock(side_effect=wait)
        with patch("src.speech_processor.asyncio.create_subprocess_exec", AsyncMock(return_value=child)):
            task = asyncio.create_task(processor(b"\0\0" * 24000))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        child.terminate.assert_called_once()
        child.wait.assert_awaited()
        child.kill.assert_not_called()

    async def test_worker_failure_and_malformed_output_are_explicit(self):
        with patch("src.speech_processor.importlib.util.find_spec", return_value=object()):
            processor = SpeechProcessor(VoiceProfile(enabled=True))
        for code, stdout, stderr in ((1, b"", b"backend failed"),
                                     (0, b"invalid\n\0\0", b""),
                                     (0, b'{"status":"processed"}\n', b""),
                                     (0, b'[]\n\0\0', b"")):
            child = Mock(returncode=code)
            child.communicate = AsyncMock(return_value=(stdout, stderr))
            with self.subTest(code=code, stdout=stdout), patch(
                "src.speech_processor.asyncio.create_subprocess_exec", AsyncMock(return_value=child),
            ), self.assertRaises(AudioPlaybackError):
                await processor(b"\0\0")


@unittest.skipUnless(HAS_VOICE, "Run uv sync --locked for real voice processing tests")
class RealVoiceTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, samples=24000):
        import numpy as np
        from src.voice_processing import pcm16
        time_axis = np.arange(samples, dtype=np.float64) / 24000
        signal = np.sum([.12 / n * np.sin(2 * np.pi * 180 * n * time_axis)
                         for n in range(1, 9)], axis=0)
        return pcm16(signal)

    async def test_subprocess_matches_shared_audition_processor(self):
        from src.voice_worker import process_pcm
        pcm = self.fixture()
        profile = VoiceProfile(enabled=True)
        expected, metadata = await asyncio.to_thread(process_pcm, pcm, profile)
        actual = await SpeechProcessor(profile)(pcm)
        self.assertEqual(metadata["status"], "processed")
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(pcm))
        self.assertNotEqual(actual, pcm)

    async def test_real_worker_through_runtime_skips_spontaneous_cue_without_pause(self):
        from src.voice_worker import process_pcm
        profile = load_patient_scenario(ROOT / "data/prompts/01_common_cold_kid.json").performance_profile
        assert profile and profile.voice
        pcm = self.fixture()
        expected, _ = await asyncio.to_thread(process_pcm, pcm, profile.voice)
        sink = ControlledSink()
        animator = Mock()
        animator.push_to_talk = animator.evidence_open = animator.won = False
        animator._menu = None
        runtime = ConversationRuntime(AsyncMock(), animator, asyncio.Event(), {}, sink, profile=profile)
        tasks = [asyncio.create_task(runtime.process_speech()),
                 asyncio.create_task(runtime.playback.run()),
                 asyncio.create_task(runtime.finish_responses())]
        try:
            await runtime.interrupt(new_turn=True)
            await runtime.request_response(runtime.playback.generation)
            runtime.handle({"type": "response.created", "response": {"id": "real"}})
            for kind, values in (
                ("response.output_audio.delta", {"delta": base64.b64encode(pcm).decode()}),
                ("response.output_audio_transcript.done", {"transcript": "Test utterance"}),
                ("response.content_part.done", {}),
            ):
                runtime.handle({"type": kind, "response_id": "real", "item_id": "i", **values})
            runtime.handle({"type": "response.done", "response": {"id": "real", "status": "completed"}})
            await asyncio.wait_for(until(lambda: bool(sink.history)), 10)
            self.assertEqual(sink.history, [expected])
            animator.add_transcript.assert_not_called()
            sink.ready = True
            await until(lambda: animator.add_transcript.called)
            self.assertEqual(animator.add_transcript.call_args.args[1], "Test utterance")
            sink.done = True
            await until(lambda: "real" not in runtime.responses)
            self.assertEqual(sink.history, [expected])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_short_quiet_unvoiced_and_disabled_are_handled(self):
        import numpy as np
        from src.voice_worker import process_pcm
        from src.voice_processing import pcm16
        pcm = self.fixture(6000)
        profile = VoiceProfile(enabled=True)
        output, metadata = await asyncio.to_thread(process_pcm, pcm, profile)
        self.assertEqual(len(output), len(pcm))
        self.assertEqual(metadata["status"], "processed")
        self.assertEqual(metadata["analysis_padding_samples"], 6000)
        silence = bytes(4800)
        with self.assertLogs("src.speech_processor", level="WARNING"):
            self.assertEqual(await SpeechProcessor(profile)(silence), silence)
        self.assertEqual(process_pcm(pcm, replace(profile, enabled=False))[0], pcm)
        t = np.arange(24000, dtype=np.float64) / 24000
        unvoiced = pcm16(.2 * np.sin(2*np.pi*180*t) + .06 * np.sin(2*np.pi*360*t))
        unchanged, metadata = await asyncio.to_thread(process_pcm, unvoiced, profile)
        self.assertEqual(metadata["status"], "unchanged_unvoiced")
        self.assertEqual(unchanged, unvoiced)
        for invalid in (b"", b"\0", bytes(24000*2*30+2)):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                process_pcm(invalid, profile)


if __name__ == "__main__":
    unittest.main()
