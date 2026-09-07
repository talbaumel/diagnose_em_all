from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import pygame

from src.diagnostic_skills import SkillEngine, load_catalog
from src.realtime_conversation import (
    PatientAnimator, Test, _patient_instructions, _resolve_skill_call, _test_tools,
)
from src.skill_confirmation import SkillConfirmation


class ToolSchemaTests(unittest.TestCase):
    def test_pcr_exposes_covid_and_flu_without_antigen_substitution(self):
        tool = next(tool for tool in _test_tools([])[0] if tool["name"] == "propose_targeted_pathogen_pcr")
        self.assertEqual(tool["parameters"]["properties"]["target"]["enum"],
                         ["SARS_CoV_2", "influenza_A_B", "respiratory_viral_panel"])
        self.assertEqual(set(tool["parameters"]["required"]), {"specimen", "target"})
        for phrase in ("COVID PCR", "flu PCR", "influenza PCR", "never substitute antigen"):
            self.assertIn(phrase, tool["description"])
        for phrase in ("one proposal", "does not cover every virus", "Do not split a panel",
                       "clarify whether it is intended", "without upgrading to the broader panel"):
            self.assertIn(phrase, tool["description"])

    def test_every_patient_gets_identical_neutral_stable_tools(self):
        empty_tools, mapping = _test_tools([])
        tools, other = _test_tools([Test("temperature", "37 C")])
        self.assertEqual(tools, empty_tools)
        self.assertEqual(mapping, other)
        self.assertEqual(len(tools), len(load_catalog()))
        for skill, tool in zip(load_catalog(), tools):
            self.assertEqual(tool["name"], f"propose_{skill.id}")
            self.assertLessEqual(len(tool["name"]), 64)
            self.assertEqual(tool["parameters"], skill.parameters)
            self.assertFalse(tool["parameters"]["additionalProperties"])
            for phrase in (*skill.aliases, *skill.examples):
                self.assertIn(phrase, tool["description"])
            self.assertNotIn("points", tool["description"].lower())

    def test_prompt_guardrails_are_not_claimed_as_live_routing_proof(self):
        instructions = _patient_instructions("Fictional patient", "common cold")
        for concept in ("negation", "hypotheticals", "specimen", "consent", "unnecessary", "confirmation"):
            self.assertIn(concept, instructions)
        self.assertNotIn("matching tool immediately", instructions)


class SkillIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pygame.init()
        self.window = pygame.display.set_mode((480, 480))
        self.animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.animator.skill_engine = SkillEngine("COMMON_COLD_KID", [Test("temperature", "37.6 C")])
        self.stop = asyncio.Event()

    async def asyncTearDown(self):
        self.animator.close()
        pygame.quit()

    def call(self, arguments="{}", name="propose_temperature"):
        return {"name": name, "arguments": arguments, "call_id": "test-call"}

    async def resolve(self, call=None, confirmed=True):
        async def close_evidence():
            self.animator.close_test_result()
        with patch.object(self.animator, "confirm_skill", new=AsyncMock(return_value=confirmed)), patch.object(
            self.animator, "wait_for_evidence_close", new=close_evidence,
        ):
            return await _resolve_skill_call(call or self.call(), _test_tools([])[1], self.animator)

    async def test_cancellation_is_neither_discovery_nor_points(self):
        result = await self.resolve(confirmed=False)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.animator.metrics.discovered_tests, {})
        self.assertEqual(self.animator.skill_engine.score, 0)
        self.assertFalse(any(action["status"] == "completed" for action in self.animator.skill_engine.actions))

    async def test_voice_tool_calls_use_each_instrument_without_opening_drawer(self):
        from src.skill_activity import INSTRUMENTS, InstrumentActivity
        from tests.audio_fakes import until
        for skill_id in INSTRUMENTS:
            task = asyncio.create_task(_resolve_skill_call(
                self.call(name=f"propose_{skill_id}"), _test_tools([])[1], self.animator,
            ))
            try:
                await until(lambda: isinstance(self.animator._skill_confirmation, InstrumentActivity))
                activity = self.animator._skill_confirmation
                self.assertEqual(activity.skill_id, skill_id)
                self.assertEqual(activity.state, "idle")
                self.assertIsNone(self.animator._skill_browser)
                for key in (pygame.K_RETURN, pygame.K_TAB, pygame.K_RETURN):
                    self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key), self.stop)
                self.animator._advance_skill_activity(activity.measurement_started + activity.MEASURE_SECONDS)
                await until(lambda: self.animator.evidence_open)
                self.animator.close_test_result()
                result = await task
                self.assertEqual(result["status"], "completed")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_pcr_clarifies_before_confirmation_and_cancels_without_result(self):
        with patch.object(self.animator, "confirm_skill", new=AsyncMock()) as confirm:
            for parameters in ({}, {"specimen": "nasal_swab"}, {"target": "influenza_A_B"},
                               {"target": "respiratory_viral_panel"}):
                result = await _resolve_skill_call(
                    self.call(json.dumps(parameters), "propose_targeted_pathogen_pcr"),
                    _test_tools([])[1], self.animator,
                )
                self.assertEqual(result["status"], "clarification_required")
            confirm.assert_not_awaited()
        for target in ("SARS_CoV_2", "influenza_A_B", "respiratory_viral_panel"):
            result = await self.resolve(self.call(
                json.dumps({"specimen": "nasal_swab", "target": target}),
                "propose_targeted_pathogen_pcr",
            ), confirmed=False)
            self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.animator.metrics.discovered_tests, {})
        self.assertEqual(self.animator.skill_engine.score, 0)

    async def test_only_explicit_confirmation_completes_and_repeats_do_not_farm(self):
        self.animator.add_transcript("You", "Please measure the temperature.")
        first = await self.resolve()
        before = self.animator.skill_engine.score
        second = await self.resolve()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(self.animator.skill_engine.score, before)
        self.assertEqual(len(self.animator.metrics.discovered_tests), 1)
        self.assertEqual(self.animator.metrics.diagnostic_skills, self.animator.skill_engine.summary())
        self.assertEqual(self.animator.metrics.skill_requests[0]["status"], "completed")

    async def test_bad_parameters_and_unknown_ids_clarify_before_confirmation(self):
        with patch.object(self.animator, "confirm_skill", new=AsyncMock()) as confirm:
            for request in (self.call("not json"), self.call("[]"), self.call('{"invented":true}'), self.call(name="propose_missing")):
                result = await _resolve_skill_call(request, _test_tools([])[1], self.animator)
                self.assertEqual(result["status"], "clarification_required")
                self.assertEqual(result["points"], 0)
            confirm.assert_not_awaited()
        self.assertEqual(self.animator.skill_engine.score, 0)

    async def test_pending_confirmation_blocks_finish_text_and_microphone(self):
        self.animator._skills_busy = True
        self.animator._text_input = "Never send while approving"
        self.animator._diagnosis_confirmed.set()
        self.animator._push_to_talk.set()
        self.animator._submit_text()
        self.animator._finish_consultation()
        self.assertFalse(self.animator.won)
        self.assertFalse(self.animator.push_to_talk)
        self.assertTrue(self.animator._text_messages.empty())

    async def test_confirmation_keyboard_defaults_cancel_and_explicit_tab_confirms(self):
        proposal = self.animator.skill_engine.prepare("throat_examination", {}, ())
        for confirm in (False, True):
            pending = asyncio.create_task(self.animator.confirm_skill(proposal))
            await asyncio.sleep(0)
            self.animator.draw()
            if confirm:
                self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_TAB), self.stop)
            self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), self.stop)
            self.assertEqual(await pending, confirm)
        self.assertEqual(self.animator.skill_engine.score, 0)

    async def test_interrupted_confirmation_never_executes(self):
        pending = asyncio.create_task(_resolve_skill_call(self.call(), _test_tools([])[1], self.animator))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.animator.skill_engine.score, 0)
        self.assertEqual(self.animator.metrics.discovered_tests, {})
        self.assertIsNone(self.animator._skill_confirmation)

    async def test_modal_wait_preserves_typing_and_cancellation_invalidates_proposal(self):
        current = True
        self.animator._open_diagnosis()
        with patch("pygame.key.stop_text_input") as stop_typing, patch.object(
            self.animator, "confirm_skill", new=AsyncMock(return_value=True),
        ) as confirm:
            pending = asyncio.create_task(_resolve_skill_call(
                self.call(), _test_tools([])[1], self.animator, lambda: current,
            ))
            await asyncio.sleep(0)
            stop_typing.assert_not_called()
            current = False
            result = await asyncio.wait_for(pending, 1)
            confirm.assert_not_awaited()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.animator.skill_engine.score, 0)
        self.assertTrue(self.animator._diagnosis_open)

    async def test_cancellation_after_confirmation_before_execution_is_respected(self):
        current = True

        async def confirmation(proposal):
            nonlocal current
            current = False
            return True

        with patch.object(self.animator, "confirm_skill", new=confirmation):
            result = await _resolve_skill_call(self.call(), _test_tools([])[1], self.animator, lambda: current)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.animator.metrics.discovered_tests, {})

    async def test_browser_is_universal_and_drafts_not_executes(self):
        self.animator._text_input = "Existing draft."
        self.animator._set_text_focus(True)
        self.animator._open_skills()
        self.assertEqual(len(self.animator._skill_browser.catalog), len(load_catalog()))
        self.assertFalse(self.animator.push_to_talk)
        for key in (pygame.K_END, pygame.K_RETURN, pygame.K_RETURN):
            self.animator.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key), self.stop)
        self.assertIsNone(self.animator._skill_browser)
        self.assertTrue(self.animator._text_input.startswith("Existing draft."))
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(self.animator.skill_engine.score, 0)

    async def test_image_plus_text_report_retains_descriptive_evidence(self):
        self.animator.show_test_result(Test(
            "Temperature", "Authoritative thermometer evidence; descriptive report.",
            "data/sprites/tests/thermometer.png",
        ))
        self.animator.draw()
        self.assertIsNotNone(self.animator._test_result_image)
        self.assertIn("descriptive report", self.animator._test_result.results)

    async def test_used_results_include_findings_and_rationale(self):
        result = await self.resolve()
        self.animator._show_discovered_tests()
        text = self.animator._test_result.results
        self.assertIn("38.0", text)
        self.assertIn(result["rationale"], text)
        self.assertIn("Appropriate-use points", text)

    async def test_context_tool_exposes_real_indices_not_hidden_case_or_score(self):
        self.animator.add_transcript("Case", "System status not a patient assertion")
        self.animator.add_transcript("You", "May I examine your ankle?")
        self.animator.add_transcript("Patient", "Yes.")
        result = await _resolve_skill_call(self.call(name="get_skill_context"), _test_tools([])[1], self.animator)
        self.assertEqual([turn["index"] for turn in result["turns"]], [1, 2])
        self.assertEqual(result["status"], "read_only")
        self.assertNotIn("score", result)
        self.assertEqual(self.animator.skill_engine.actions, [])
        self.assertEqual(self.animator.metrics.discovered_tests, {})


if __name__ == "__main__":
    unittest.main()
