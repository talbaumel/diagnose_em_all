from __future__ import annotations

import unittest

from src.skill_routing import SkillCallBatches


def call(call_id="one", name="propose_temperature", arguments="{}"):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def done(output, response_id="r1", status="completed"):
    return {"type": "response.done", "response": {"id": response_id, "status": status, "output": output}}


class SkillBatchTests(unittest.TestCase):
    def test_partial_arguments_never_execute(self):
        batches = SkillCallBatches()
        self.assertEqual(batches.collect({
            "type": "response.function_call_arguments.done", "response_id": "r1",
            **{key: value for key, value in call().items() if key != "type"},
        }), [])
        self.assertEqual(batches.seen, set())
        self.assertEqual(batches.collect(done([call()])), [call()])

    def test_compound_distinct_calls_preserved_with_single_batch(self):
        batches = SkillCallBatches()
        calls = [call(), call("two", "propose_blood_pressure")]
        self.assertEqual(batches.collect(done(calls)), calls)
        self.assertEqual(batches.collect(done(calls)), [])

    def test_cancelled_or_failed_response_never_executes(self):
        for status in ("cancelled", "failed", "incomplete"):
            with self.subTest(status=status):
                batches = SkillCallBatches()
                self.assertEqual(batches.collect(done([call()], status=status)), [])
                self.assertEqual(batches.seen, set())

    def test_locally_interrupted_response_cannot_execute_even_if_completed(self):
        batches = SkillCallBatches()
        batches.collect({"type": "response.created", "response": {"id": "r1"}})
        batches.cancel()
        self.assertEqual(batches.generation, 1)
        self.assertEqual(batches.collect(done([call()])), [])
        self.assertEqual(batches.collect(done([call("new")], "r2")), [call("new")])

    def test_duplicate_call_id_in_compound_output_processed_once(self):
        batches = SkillCallBatches()
        self.assertEqual(batches.collect(done([call(), call()])), [call()])

    def test_final_empty_output_does_not_execute_abandoned_partial_call(self):
        batches = SkillCallBatches()
        batches.collect({"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "one"})
        self.assertEqual(batches.collect(done([])), [])

    def test_malformed_call_id_not_queued(self):
        batches = SkillCallBatches()
        self.assertEqual(batches.collect(done([call(None)])), [])

    def test_transcript_mentions_never_dispatch_actions(self):
        batches = SkillCallBatches()
        for text in (
            "Do not check my temperature.",
            "Would a temperature measurement be useful?",
            "Explain temperature measurement.",
            "Please check temperature.",
        ):
            self.assertEqual(batches.collect({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": text,
            }), [])
        self.assertEqual(batches.seen, set())


if __name__ == "__main__":
    unittest.main()
