"""Universal skills through the extracted audio/session runtime, without devices."""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock

import pygame

from src.conversation_runtime import ConversationRuntime
from src.cue_catalog import CueChoice
from src.diagnostic_skills import SkillEngine
from src.patient_performance import COUGH_CLIP, PROJECT_ROOT, PerformanceProfile, read_pcm_clip
from src.realtime_conversation import PatientAnimator, Test, _resolve_skill_call, _test_tools
from tests.audio_fakes import ControlledSink, until


class SkillRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pygame.init()
        self.animator = PatientAnimator(
            0, window=pygame.display.set_mode((480, 480)), disease="common cold",
        )
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [Test("temperature", "37 C")])
        self.animator.add_transcript("You", "Please measure my temperature and check my throat.")
        self.sink = ControlledSink()
        self.websocket = AsyncMock()
        self.stop = asyncio.Event()
        mapping = _test_tools([])[1]
        self.processor = AsyncMock(side_effect=lambda pcm: pcm)
        self.evidence_audio = {}
        self.runtime = ConversationRuntime(
            self.websocket, self.animator, self.stop, {}, self.sink,
            profile=PerformanceProfile(cues=(CueChoice("cold_dry_cough"),)),
            speech_processor=self.processor,
            skill_tools=mapping,
            skill_handler=lambda call, current, present: _resolve_skill_call(
                call, mapping, self.animator, current,
                present_result=present, evidence_audio=self.evidence_audio,
            ),
        )
        self.tasks = [
            asyncio.create_task(self.runtime.playback.run()),
            asyncio.create_task(self.runtime.process_speech()),
            asyncio.create_task(self.runtime.finish_responses()),
            asyncio.create_task(self.runtime.send_user_requests()),
        ]

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.animator.close()
        pygame.quit()

    def sent(self, kind):
        return [
            event for call in self.websocket.send.call_args_list
            if (event := json.loads(call.args[0]))["type"] == kind
        ]

    def outputs(self):
        return [
            (event["item"]["call_id"], json.loads(event["item"]["output"]))
            for event in self.sent("conversation.item.create")
            if event["item"]["type"] == "function_call_output"
        ]

    def call(self, call_id="temperature", name="propose_temperature"):
        return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}

    async def begin(self, response_id="r"):
        await self.runtime.interrupt(new_turn=True)
        await self.runtime.request_response(self.runtime.playback.generation)
        self.runtime.handle({"type": "response.created", "response": {"id": response_id}})

    def done(self, calls=(), response_id="r", status="completed"):
        self.runtime.handle({
            "type": "response.done",
            "response": {"id": response_id, "status": status, "output": list(calls)},
        })

    def decide(self, confirmed):
        if confirmed:
            self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), self.stop)

    async def test_cold_covid_and_flu_pcr_batch_confirms_and_reports_each_target(self):
        self.animator.add_transcript("Patient", "I have a runny nose and a cough.")
        self.animator.add_transcript("You", "Run COVID and flu PCR tests on nasal swabs.")
        await self.begin()
        calls = []
        for target in ("SARS_CoV_2", "influenza_A_B"):
            call = self.call(target, "propose_targeted_pathogen_pcr")
            call["arguments"] = json.dumps({"specimen": "nasal_swab", "target": target})
            calls.append(call)
        self.done(calls)
        for count, target in enumerate(("SARS_CoV_2", "influenza_A_B")):
            await until(lambda: self.animator._skill_confirmation is not None)
            self.assertEqual(len(self.animator.skill_engine.actions), count)
            self.decide(True)
            await until(lambda: self.animator.evidence_open)
            self.assertEqual(self.animator.skill_engine.actions[-1]["parameters"]["target"], target)
            self.animator.close_test_result()
        await until(lambda: len(self.sent("response.create")) == 2)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed", "completed"])
        self.assertEqual(self.animator.skill_engine.score, 2)
        findings = self.animator._pokedex_context()["discovered_tests"]
        self.assertEqual(len(findings), 1)
        self.assertIn("SARS-CoV-2 PCR is not detected", findings[0]["result"])
        self.assertIn("influenza A not detected", findings[0]["result"])
        self.assertIn("influenza B not detected", findings[0]["result"])

    async def test_one_panel_call_is_one_confirmation_result_and_charge(self):
        self.animator.add_transcript("Patient", "My nose is runny and I have a cough.")
        self.animator.add_transcript("You", "Run one respiratory viral PCR panel on a nasal swab.")
        await self.begin()
        call = self.call("panel", "propose_targeted_pathogen_pcr")
        call["arguments"] = json.dumps({"specimen": "nasal_swab", "target": "respiratory_viral_panel"})
        self.done([call])
        await until(lambda: self.animator._skill_confirmation is not None)
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.decide(True)
        await until(lambda: self.animator.evidence_open)
        self.animator.draw()
        self.assertIn("Rhinovirus/enterovirus: detected", self.animator._test_result.results)
        self.animator.close_test_result()
        await until(lambda: len(self.sent("response.create")) == 2)
        self.assertIsNone(self.animator._skill_confirmation)
        self.assertEqual(len(self.animator.skill_engine.actions), 1)
        self.assertEqual(self.animator.skill_engine.score, -1)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed"])
        findings = self.animator._pokedex_context()["discovered_tests"]
        self.assertEqual(len(findings), 1)
        self.assertIn("Rhinovirus/enterovirus: detected", findings[0]["result"])
        self.assertIn("Influenza A: not detected", findings[0]["result"])

    async def test_you_win_precedes_pending_final_speech_and_medical_skills(self):
        release_audio = asyncio.Event()

        async def process(pcm):
            await release_audio.wait()
            return pcm

        self.runtime.speech_processor = process
        self.animator.add_transcript("You", "You have a common cold")
        await self.begin()
        for kind, payload in (
            ("response.output_audio.delta", {"delta": base64.b64encode(b"\1\0" * 2400).decode()}),
            ("response.output_audio_transcript.done", {"transcript": "Thank you, doctor."}),
            ("response.content_part.done", {}),
        ):
            self.runtime.handle({"type": kind, "response_id": "r", "item_id": "speech", **payload})
        ticket = self.runtime.responses["r"].tickets[0]
        win = self.call("win", "propose_you_win")
        win["arguments"] = json.dumps({"diagnosis": "common cold"})
        self.done([self.call(), win])
        await until(lambda: self.animator.won and not self.animator._skills_busy)
        self.assertFalse(release_audio.is_set())
        self.assertEqual(self.sink.history, [])
        self.assertNotIn(("Patient", "Thank you, doctor."), self.animator._transcript)
        self.assertTrue(ticket.done())
        self.assertEqual(self.runtime.processing_bytes, 0)
        self.assertEqual(self.runtime.playback.queued_bytes, 0)
        self.assertTrue(self.animator._consultation_finished.is_set())
        self.assertIsNone(self.animator._skill_confirmation)
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed", "cancelled"])
        self.assertEqual(len(self.sent("response.create")), 1)

    async def test_you_win_interrupts_final_speech_already_playing(self):
        self.runtime.inline_cues = False
        self.animator.add_transcript("You", "You have a common cold")
        await self.begin()
        for kind, payload in (
            ("response.output_audio.delta", {"delta": base64.b64encode(b"\1\0" * 2400).decode()}),
            ("response.output_audio_transcript.done", {"transcript": "Thank you, doctor."}),
            ("response.content_part.done", {}),
        ):
            self.runtime.handle({"type": kind, "response_id": "r", "item_id": "speech", **payload})
        await until(lambda: bool(self.sink.history))
        self.sink.ready = True
        self.sink.heard_ms = 60
        await until(lambda: self.runtime.playback.started)
        win = self.call("win", "propose_you_win")
        win["arguments"] = json.dumps({"diagnosis": "common cold"})
        self.runtime.handle({**win, "type": "response.function_call_arguments.done", "response_id": "r"})
        await asyncio.sleep(0)
        self.assertFalse(self.animator.won)
        self.done([win])
        await until(lambda: self.animator.won and not self.animator._skills_busy)
        self.assertFalse(self.sink.done)
        self.assertTrue(self.animator._consultation_finished.is_set())
        self.assertEqual(self.sent("conversation.item.truncate")[-1]["audio_end_ms"], 60)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed"])

    async def test_rejected_you_win_preserves_speech_before_medical_skills(self):
        await self.begin()
        pcm = b"\1\0" * 2400
        for kind, payload in (
            ("response.output_audio.delta", {"delta": base64.b64encode(pcm).decode()}),
            ("response.output_audio_transcript.done", {"transcript": "My nose is still running."}),
            ("response.content_part.done", {}),
        ):
            self.runtime.handle({"type": kind, "response_id": "r", "item_id": "speech", **payload})
        win = self.call("win", "propose_you_win")
        win["arguments"] = json.dumps({"diagnosis": "migraine"})
        self.done([self.call(), win])
        await until(lambda: len(self.outputs()) == 1 and bool(self.sink.history))
        self.assertEqual(self.outputs()[0][1]["status"], "clarification_required")
        self.assertFalse(self.animator.won)
        self.assertIsNone(self.animator._skill_confirmation)
        self.assertEqual(self.sink.history, [pcm])
        self.assertEqual(self.sent("conversation.item.truncate"), [])
        self.sink.ready = self.sink.done = True
        await until(lambda: self.animator._skill_confirmation is not None)
        self.decide(False)
        await until(lambda: not self.animator._skills_busy)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["clarification_required", "cancelled"])
        self.assertFalse(self.animator._consultation_finished.is_set())

    async def test_terminal_batch_waits_for_processed_speech_and_skips_cue(self):
        await self.begin()
        pcm = b"\1\0" * 2400
        for kind, payload in (
            ("response.output_audio.delta", {"delta": base64.b64encode(pcm).decode()}),
            ("response.output_audio_transcript.done", {"transcript": "You may check my throat."}),
            ("response.content_part.done", {}),
        ):
            self.runtime.handle({"type": kind, "response_id": "r", "item_id": "speech", **payload})
        self.done([self.call("cough", "cough"), self.call()])
        self.assertTrue(self.animator._skills_busy)
        self.animator._diagnosis_confirmed.set()
        self.assertFalse(self.animator.won)
        await until(lambda: bool(self.sink.history))
        self.assertEqual(self.sink.history, [pcm])
        self.assertIsNone(self.animator._skill_confirmation)
        self.assertIsNone(self.runtime.playback.active[0].insertion)
        self.sink.ready = self.sink.done = True
        await until(lambda: self.animator._skill_confirmation is not None)
        self.assertIn(("Patient", "You may check my throat."), self.animator._transcript)
        self.decide(True)
        await until(lambda: self.animator.evidence_open)
        self.assertIn("38.0", self.animator._test_result.results)
        self.assertEqual(len(self.sent("response.create")), 1)
        self.animator.close_test_result()
        await until(lambda: len(self.sent("response.create")) == 2)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["skipped", "completed"])
        self.assertEqual(len(self.sink.history), 1)
        self.processor.assert_awaited_once_with(pcm)
        self.assertFalse(self.animator._skills_busy)
        self.assertEqual(self.sent("response.create")[-1]["response"], {"tool_choice": "none"})

    async def test_helper_receives_executed_findings_not_catalog_art_or_hidden_scoring(self):
        await self.begin()
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F5), self.stop)
        self.assertEqual(self.animator._pokedex_context()["discovered_tests"], [])
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), self.stop)
        self.done([self.call()])
        await until(lambda: self.animator._skill_confirmation is not None)
        self.assertEqual(self.animator._pokedex_context()["discovered_tests"], [])
        self.decide(True)
        await until(lambda: self.animator.evidence_open)
        self.assertIsNotNone(self.animator._test_result.image_path)
        context = self.animator._pokedex_context()
        self.assertEqual(context["discovered_tests"], [
            {"name": name, "result": result}
            for name, result in self.animator.metrics.discovered_tests.items()
        ])
        self.assertIn("38.0", context["discovered_tests"][0]["result"])
        encoded = json.dumps(context)
        for hidden in ("thermometer.png", "skills/images", "image_path", "appropriateness", "rationale", "points"):
            self.assertNotIn(hidden, encoded)
        self.animator.close_test_result()
        await until(lambda: not self.animator._skills_busy)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F6), self.stop)
        self.assertTrue(self.animator._pokedex.open)

    async def test_sequential_confirm_cancel_and_replay_only_continue_once(self):
        await self.begin()
        first, second = self.call(), self.call("throat", "propose_throat_examination")
        self.done([first, first, second])
        self.done([first, second])
        await until(lambda: self.animator._skill_confirmation is not None)
        self.decide(True)
        await until(lambda: self.animator.evidence_open)
        self.assertIsNone(self.animator._skill_confirmation)
        self.assertEqual(len(self.animator.skill_engine.actions), 1)
        self.animator.close_test_result()
        await until(lambda: self.animator._skill_confirmation is not None)
        self.decide(False)
        await until(lambda: len(self.sent("response.create")) == 2)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed", "cancelled"])
        self.assertEqual(len(self.animator.metrics.discovered_tests), 1)
        score = self.animator.skill_engine.score
        self.runtime.handle({"type": "response.created", "response": {"id": "followup"}})
        self.done([first, second], response_id="followup")
        await until(lambda: "followup" not in self.runtime.responses)
        self.assertEqual(len(self.outputs()), 2)
        self.assertEqual(self.animator.skill_engine.score, score)
        self.assertEqual(len(self.sent("response.create")), 2)

    async def test_context_lookup_can_continue_to_proposal_then_disable_tools(self):
        await self.begin()
        self.animator.add_transcript("Patient", "Yes, you may examine my throat.")
        self.done([self.call("context", "get_skill_context")])
        await until(lambda: len(self.sent("response.create")) == 2)
        self.assertEqual(self.outputs()[0][1]["status"], "read_only")
        self.assertEqual(self.outputs()[0][1]["turns"][0]["index"], 0)
        self.assertEqual(self.sent("response.create")[-1]["response"], {"tool_choice": "auto"})
        self.runtime.handle({"type": "response.created", "response": {"id": "proposal"}})
        self.done([self.call()], response_id="proposal")
        await until(lambda: self.animator._skill_confirmation is not None)
        self.decide(False)
        await until(lambda: len(self.sent("response.create")) == 3)
        self.assertEqual(self.sent("response.create")[-1]["response"], {"tool_choice": "none"})
        self.assertEqual(self.animator.skill_engine.score, 0)

    async def defer_context(self, key, response_id="r"):
        before = len(self.sent("response.create"))
        await self.begin(response_id)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key), self.stop)
        self.done([self.call(response_id, "get_skill_context")], response_id=response_id)
        await until(lambda: any(call_id == response_id for call_id, _ in self.outputs()))
        await asyncio.sleep(.02)
        self.assertIn(response_id, self.runtime.responses)
        self.assertFalse(self.animator._skills_busy)
        self.assertEqual(len(self.sent("response.create")), before + 1)
        return before

    async def test_context_continuation_resumes_once_after_each_modal_closes(self):
        for index, key in enumerate((pygame.K_F6, pygame.K_F5)):
            with self.subTest(key=key):
                response_id = f"context-{index}"
                before = await self.defer_context(key, response_id)
                self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), self.stop)
                await until(lambda: len(self.sent("response.create")) == before + 2)
                self.assertEqual(self.sent("response.create")[-1]["response"], {"tool_choice": "auto"})
                self.done([self.call(response_id, "get_skill_context")], response_id=response_id)
                await asyncio.sleep(.02)
                self.assertEqual(len(self.sent("response.create")), before + 2)
                self.assertEqual(sum(call_id == response_id for call_id, _ in self.outputs()), 1)
                followup = f"followup-{index}"
                self.runtime.handle({"type": "response.created", "response": {"id": followup}})
                self.done(response_id=followup)
                await until(lambda: not self.runtime.responses)

    async def test_interruption_during_each_modal_deferral_suppresses_old_continuation(self):
        for index, key in enumerate((pygame.K_F6, pygame.K_F5)):
            with self.subTest(key=key):
                response_id = f"context-{index}"
                before = await self.defer_context(key, response_id)
                await self.runtime.interrupt(new_turn=True)
                await until(lambda: response_id not in self.runtime.responses)
                self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), self.stop)
                await asyncio.sleep(.02)
                self.assertEqual(len(self.sent("response.create")), before + 1)
                self.assertEqual(self.runtime.continuations, 0)
                self.assertFalse(self.tasks[2].done())

    async def test_shutdown_and_finish_during_deferral_suppress_continuation(self):
        before = await self.defer_context(pygame.K_F6)
        self.stop.set()
        await until(lambda: not self.runtime.responses)
        self.animator._pokedex.hide()
        self.assertEqual(len(self.sent("response.create")), before + 1)
        self.stop.clear()
        before = await self.defer_context(pygame.K_F5, "finish")
        self.animator.show_win()
        await until(lambda: not self.runtime.responses)
        self.assertEqual(len(self.sent("response.create")), before + 1)

    async def test_streamed_partial_or_cancelled_calls_never_ask_for_approval(self):
        for number, status in enumerate(("completed", "cancelled", None)):
            response_id = f"r-{number}"
            await self.begin(response_id)
            self.runtime.handle({
                "response_id": response_id, **self.call(),
                "type": "response.function_call_arguments.done",
            })
            if status == "completed":
                self.runtime.handle({"type": "response.done", "response": {"id": response_id, "status": status}})
            elif status is None:
                self.runtime.handle({"type": "response.done", "response": {"id": response_id, "output": [self.call()]}})
            else:
                self.done([self.call()], response_id=response_id, status=status)
            await until(lambda: response_id not in self.runtime.responses)
            self.assertIsNone(self.animator._skill_confirmation)
            self.assertFalse(self.animator._skills_busy)
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.assertEqual(self.outputs(), [])

    async def test_interrupt_pending_confirmation_cancels_rest_without_execution(self):
        await self.begin()
        self.done([self.call(), self.call("throat", "propose_throat_examination")])
        await until(lambda: self.animator._skill_confirmation is not None)
        await self.runtime.interrupt(new_turn=True)
        await until(lambda: len(self.outputs()) == 2)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["cancelled", "cancelled"])
        self.assertIsNone(self.animator._skill_confirmation)
        self.assertFalse(self.animator._skills_busy)
        self.assertEqual(self.animator.skill_engine.score, 0)
        self.assertEqual(self.animator.metrics.discovered_tests, {})
        self.assertEqual(len(self.sent("response.create")), 1)
        self.assertFalse(self.tasks[2].done())

    async def test_interrupt_report_keeps_completed_result_and_cancels_remaining(self):
        await self.begin()
        self.done([self.call(), self.call("throat", "propose_throat_examination")])
        await until(lambda: self.animator._skill_confirmation is not None)
        self.decide(True)
        await until(lambda: self.animator.evidence_open)
        score = self.animator.skill_engine.score
        await self.runtime.interrupt(new_turn=True)
        await until(lambda: len(self.outputs()) == 2)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed", "cancelled"])
        self.assertFalse(self.animator.evidence_open)
        self.assertEqual(self.animator.skill_engine.score, score)
        self.assertEqual(len(self.animator.metrics.discovered_tests), 1)
        self.assertEqual(len(self.sent("response.create")), 1)

    async def test_confirmed_skill_audio_uses_runtime_and_close_keeps_next_skill_current(self):
        self.evidence_audio["temperature"] = COUGH_CLIP
        await self.begin()
        self.done([self.call(), self.call("throat", "propose_throat_examination")])
        await until(lambda: self.animator._skill_confirmation is not None)
        self.assertEqual(self.sink.history, [])
        self.decide(True)
        await until(lambda: bool(self.sink.history))
        self.assertEqual(self.runtime.playback.active[0].kind, "evidence")
        self.assertEqual(self.sink.history[0], read_pcm_clip(PROJECT_ROOT / COUGH_CLIP))
        self.sink.ready = True
        await until(lambda: self.runtime.playback.started)
        self.animator.close_test_result()
        await until(lambda: self.animator._skill_confirmation is not None)
        self.assertEqual(self.outputs()[0][1]["audio_status"], "interrupted")
        self.decide(False)
        await until(lambda: len(self.sent("response.create")) == 2)
        self.assertEqual([result["status"] for _, result in self.outputs()], ["completed", "cancelled"])
        self.assertEqual(self.runtime.playback.queued_bytes, 0)

    async def test_session_shutdown_during_evidence_does_not_swallow_worker_cancellation(self):
        await self.begin()
        self.done([self.call()])
        await until(lambda: self.animator._skill_confirmation is not None)
        self.decide(True)
        await until(lambda: self.animator.evidence_open)
        self.tasks[2].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(self.tasks[2], .5)
        self.assertFalse(self.animator._skills_busy)
        self.assertFalse(self.animator.evidence_open)
        self.assertEqual(len(self.animator.metrics.discovered_tests), 1)

    async def test_pokedex_remains_closable_while_proposal_waits_and_draft_survives(self):
        await self.begin()
        self.animator._text_input = "Patient draft"
        self.animator._set_text_focus(True)
        self.animator._open_pokedex()
        self.done([self.call()])
        await asyncio.sleep(.03)
        self.assertTrue(self.animator._skills_busy)
        self.assertTrue(self.animator._pokedex.open)
        self.assertIsNone(self.animator._skill_confirmation)
        self.animator.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="Helper draft"), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), self.stop)
        await until(lambda: self.animator._skill_confirmation is not None)
        self.assertFalse(self.animator._pokedex.open)
        self.assertEqual(self.animator._pokedex.draft, "Helper draft")
        self.decide(False)
        await until(lambda: not self.animator._skills_busy)
        self.assertEqual(self.animator._text_input, "Patient draft")
        self.assertTrue(self.animator._text_focused)

    async def test_give_up_and_copilot_toolbar_and_modals_do_not_steal_input(self):
        self.assertLessEqual(
            self.animator._status_font.size("GIVE UP")[0] + 16,
            self.animator._diagnose_button.width,
        )
        self.assertFalse(self.animator._diagnose_button.colliderect(self.animator._pokedex_button))
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F5), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F6), self.stop)
        self.assertFalse(self.animator._pokedex.open)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F6), self.stop)
        self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_F5), self.stop)
        self.assertIsNone(self.animator._skill_browser)
        self.animator.draw()


if __name__ == "__main__":
    unittest.main()
