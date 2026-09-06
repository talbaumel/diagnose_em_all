import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from src.diagnostic_skills import (
    CONSENT, DATA, HISTORIES, ROOT, ScoringConfig, SkillEngine, load_catalog,
)


def values_for(skill):
    values = {}
    for name, schema in skill.parameters["properties"].items():
        if schema["type"] == "string":
            values[name] = schema["enum"][0]
        elif schema["type"] == "array":
            values[name] = ([schema["items"]["enum"][0]]
                            if schema["items"]["type"] == "string" else [0, 1])
        else:
            values[name] = 1
    return values


def perform(engine, skill_id, transcript=(), **parameters):
    return engine.execute(engine.prepare(skill_id, parameters, transcript), transcript)


def consent(skill, patient_statement="I have burning when urinating and a new partner."):
    return [("patient", patient_statement),
            ("clinician", f"May I perform {skill.name} with your consent?"),
            ("patient", "Yes.")]


class CatalogTests(unittest.TestCase):
    def test_exact_union_and_stable_identifiers(self):
        catalog = load_catalog()
        self.assertEqual(len(catalog), 86)
        self.assertEqual(len({skill.id for skill in catalog}), 86)
        self.assertIn("viral_metagenomic_sequencing", {skill.id for skill in catalog})
        for skill in catalog:
            self.assertRegex(skill.id, r"^[a-z][a-z0-9_]*$")
            self.assertIsInstance(skill.aliases, tuple)
            self.assertIsInstance(skill.examples, tuple)
            self.assertEqual(len(skill.examples), 3)
            self.assertFalse(skill.parameters["additionalProperties"])
        names = {skill.name.lower() for skill in catalog}
        tests = {test["description"].lower()
                 for path in (ROOT / "data/prompts").glob("*.json")
                 for test in json.loads(path.read_text())["tests"]}
        self.assertEqual(len(tests), 20)
        self.assertTrue(tests <= names)

    def test_only_manifest_art_is_copied(self):
        manifest = json.loads((DATA / "manifest.json").read_text())
        self.assertEqual(
            {item["filename"] for item in manifest},
            {path.name for path in (DATA / "images").iterdir()})
        self.assertEqual(len(list((DATA / "images").iterdir())), 68)
        viral = next(s for s in load_catalog() if s.id == "viral_metagenomic_sequencing")
        self.assertEqual(viral.icon.name, "viral-nanopore-sequencing-illustration.png")
        for skill in load_catalog():
            if skill.icon:
                self.assertTrue(skill.icon.is_absolute())
                self.assertTrue(skill.icon.is_file())

    def test_all_946_pairs_have_explicit_outcome_and_relevance(self):
        fixtures = json.loads((DATA / "cases.json").read_text())
        self.assertEqual(len(fixtures["case_order"]), 11)
        self.assertEqual(set(fixtures["rows"]), {s.id for s in load_catalog()})
        for skill_id, row in fixtures["rows"].items():
            with self.subTest(skill=skill_id):
                self.assertEqual(len(row["outcomes"]), 11)
                self.assertEqual(len(row["relevance"]), 11)
                for outcome, relevance in zip(row["outcomes"], row["relevance"]):
                    self.assertTrue(row["results"][outcome].strip())
                    self.assertIn(relevance, fixtures["classes"])
        for case in fixtures["case_order"]:
            self.assertEqual(len(SkillEngine(case).catalog), 86)

    def test_case_results_are_not_universal_normals(self):
        cold = perform(SkillEngine("COMMON_COLD_KID"), "temperature")
        flu = perform(SkillEngine("FEVERISH_PATIENT"), "temperature")
        allergy = perform(SkillEngine("ALLERGIES_PATIENT"), "temperature")
        self.assertIn("38.0", cold["result"])
        self.assertIn("39.2", flu["result"])
        self.assertIn("36.8", allergy["result"])
        for case, expected in [("RASH_PATIENT", "clothing"), ("MIGRAINE_SUFFERER", "No rash")]:
            self.assertIn(expected, perform(SkillEngine(case), "skin_examination")["result"])

    def test_unknown_case_fails_instead_of_defaulting(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            SkillEngine("unknown")


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.engine = SkillEngine("ANXIOUS_ADULT")
        self.turns = [("doctor", "What happens during an episode?"),
                      ("patient", "My heart races and my chest feels tight.")]

    def test_prepare_does_not_score_or_perform(self):
        proposal = self.engine.prepare("electrocardiogram", {}, self.turns)
        self.assertEqual(self.engine.score, 0)
        self.assertEqual(self.engine.actions, [])
        self.assertTrue(proposal["requires_confirmation"])
        self.assertNotIn("result", proposal)
        result = self.engine.execute(proposal, self.turns)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["points"], 2)

    def test_unknown_id_and_missing_or_unsupported_parameters(self):
        for skill, parameters in [
            ("please get an ECG", {}),
            ("ultrasound", {}),
            ("ultrasound", {"site": "thyroid"}),
            ("chest_ct", {"protocol": "angiogram"}),
            ("electrocardiogram", {"symptoms": ["chest pain"]}),
            ("electrocardiogram", {"consent": True}),
            ("electrocardiogram", []),
            ("inflammatory_markers", {"marker": "ESR_and_CRP"}),
            ("targeted_pathogen_pcr", {"specimen": "nasal_swab", "target": "influenza"}),
        ]:
            with self.subTest(skill=skill, parameters=parameters):
                with self.assertRaises(ValueError):
                    self.engine.prepare(skill, parameters, self.turns)
        self.assertEqual(self.engine.score, 0)

    def test_tampered_proposal_and_model_results_are_rejected(self):
        proposal = self.engine.prepare("electrocardiogram", {}, self.turns)
        for key, value in [("result", "Heart attack"), ("skill_id", "temperature"),
                           ("parameters", {"indicated": True}), ("name", "Altered")]:
            tampered = copy.deepcopy(proposal)
            tampered[key] = value
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "altered"):
                    self.engine.execute(tampered, self.turns)
        with self.assertRaises(ValueError):
            self.engine.execute({"skill_id": "electrocardiogram"}, self.turns)

    def test_snapshot_and_confirmation_time_revalidation(self):
        proposal = self.engine.prepare("electrocardiogram", {}, [])
        result = self.engine.execute(proposal, self.turns)
        self.assertEqual(result["points"], 2)
        other = SkillEngine("ANXIOUS_ADULT")
        proposal = other.prepare("electrocardiogram", {}, self.turns)
        changed = [("doctor", "New text"), self.turns[1]]
        with self.assertRaisesRegex(ValueError, "snapshot"):
            other.execute(proposal, changed)

    def test_cancellation_is_inert(self):
        proposal = self.engine.prepare("electrocardiogram", {}, self.turns)
        result = self.engine.cancel(proposal)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.engine.score, 0)
        with self.assertRaises(ValueError):
            self.engine.execute(proposal, self.turns)

    def test_executed_action_cannot_be_cancelled(self):
        proposal = self.engine.prepare("electrocardiogram", {}, self.turns)
        self.engine.execute(proposal, self.turns)
        snapshot = self.engine.summary()
        with self.assertRaisesRegex(ValueError, "already executed"):
            self.engine.cancel(proposal)
        self.assertEqual(self.engine.summary(), snapshot)
        self.assertEqual(self.engine.execute(proposal, self.turns)["status"], "duplicate")

    def test_defensive_catalog_and_actions(self):
        catalog = self.engine.catalog
        next(s for s in catalog if s.id == "electrocardiogram").parameters["properties"]["evil"] = {"type": "string"}
        with self.assertRaises(ValueError):
            self.engine.prepare("electrocardiogram", {"evil": "x"}, [])
        result = perform(self.engine, "temperature")
        result["points"] = 999
        self.engine.actions[0]["points"] = 999
        self.assertEqual(self.engine.actions[0]["points"], 1)


class EvidenceTests(unittest.TestCase):
    def test_every_original_case_result_survives_the_catalog_union(self):
        catalog = {skill.name.casefold(): skill for skill in load_catalog()}
        for path in (ROOT / "data/prompts").glob("*.json"):
            case = json.loads(path.read_text())
            for original in case["tests"]:
                skill = catalog[original["description"].casefold()]
                with self.subTest(case=case["patient_type"], skill=skill.id):
                    engine = SkillEngine(case["patient_type"], case["tests"])
                    parameters = values_for(skill)
                    turns = []
                    if skill.id in HISTORIES:
                        turns = [("You", f"Please review the existing {skill.name} with me."),
                                 ("Patient", original["results"])]
                        parameters["evidence_turns"] = [0, 1]
                    if skill.id in CONSENT:
                        turns = consent(skill)
                        parameters["consent_turn"] = 2
                    result = perform(engine, skill.id, turns, **parameters)
                    self.assertEqual(result["status"], "completed")
                    if original["results"].endswith(".png"):
                        self.assertEqual(Path(result["image_path"]), ROOT / original["results"])
                        self.assertNotEqual(result["result"], original["results"])
                    else:
                        self.assertIn(original["results"], result["result"])

    def test_original_image_is_evidence_not_generic_icon(self):
        engine = SkillEngine("COMMON_COLD_KID", [
            SimpleNamespace(description="temperature", results="fake/path.png")])
        result = perform(engine, "temperature")
        self.assertEqual(Path(result["image_path"]), ROOT / "data/sprites/tests/thermometer.png")
        self.assertIn("38.0", result["result"])
        self.assertNotEqual(result["image_path"], str(engine.catalog[0].icon))
        self.assertIsNone(perform(SkillEngine("FEVERISH_PATIENT"), "temperature")["image_path"])
        self.assertIsNone(perform(engine, "lung_auscultation")["image_path"])

    def test_bad_evidence_path_never_scores_or_completes(self):
        engine = SkillEngine("COMMON_COLD_KID")
        proposal = engine.prepare("temperature", {}, [])
        before = engine.summary()
        with patch.object(Path, "is_file", return_value=False):
            with self.assertRaisesRegex(ValueError, "evidence path"):
                engine.execute(proposal, [])
        self.assertEqual(engine.summary(), before)
        # A recovered asset still receives its original reward: no budget was spent.
        self.assertEqual(engine.execute(proposal, [])["points"], 1)

    def test_unreadable_corrupt_or_escaped_evidence_never_scores(self):
        engine = SkillEngine("COMMON_COLD_KID")
        proposal = engine.prepare("temperature", {}, [])
        before = engine.summary()
        with patch.object(Path, "open", side_effect=PermissionError("unreadable")):
            with self.assertRaisesRegex(ValueError, "unreadable"):
                engine.execute(proposal, [])
        with patch.object(Path, "open", mock_open(read_data=b"not-a-PNG")):
            with self.assertRaisesRegex(ValueError, "PNG"):
                engine.execute(proposal, [])
        with patch.object(Path, "resolve", side_effect=[
            ROOT / "data/sprites/tests", DATA / "images/temperature.png",
        ]):
            with self.assertRaisesRegex(ValueError, "evidence path"):
                engine.execute(proposal, [])
        self.assertEqual(engine.summary(), before)

    def test_legacy_results_are_repository_owned_not_model_authored(self):
        engine = SkillEngine("ANXIOUS_ADULT", [{"description": "electrocardiogram", "results": "Fabricated infarct"}])
        result = perform(engine, "electrocardiogram")
        self.assertIn("normal sinus rhythm", result["result"])
        self.assertNotIn("Fabricated", result["result"])

    def test_no_credit_for_hidden_case_or_clinician_claim(self):
        for turns in [[], [("doctor", "The patient has a racing heart.")],
                      [("patient", "I do not have a racing heart or chest tightness.")],
                      [("patient", "Do I have panic disorder?")],
                      [("patient", "Maybe I have panic attacks.")]]:
            with self.subTest(turns=turns):
                result = perform(SkillEngine("ANXIOUS_ADULT"), "electrocardiogram", turns)
                self.assertEqual(result["points"], 0)
        actual = [("patient", "I have panic episodes with a racing heart.")]
        self.assertEqual(perform(SkillEngine("ANXIOUS_ADULT"), "electrocardiogram", actual)["points"], 2)

    def test_negative_ruleout_receives_credit(self):
        engine = SkillEngine("ANXIOUS_ADULT")
        result = perform(engine, "electrocardiogram", [("patient", "My heart races during these episodes.")])
        self.assertEqual(result["points"], 2)
        self.assertIn("rule-out", result["rationale"])
        self.assertIn("normal sinus", result["result"])

    def test_many_new_skills_can_be_performed_unnecessarily(self):
        engine = SkillEngine("MIGRAINE_SUFFERER")
        ids = ["complete_blood_count", "blood_smear", "electrolytes", "ear_examination",
               "chest_x_ray", "lung_auscultation", "fecal_calprotectin", "gait_assessment"]
        for skill in ids:
            with self.subTest(skill=skill):
                result = perform(engine, skill)
                self.assertEqual(result["status"], "completed")
                self.assertTrue(result["result"])
        result = perform(engine, "chest_ct", protocol="noncontrast")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["points"], -1)

    def test_research_and_no_isolate_are_honest(self):
        engine = SkillEngine("FEVERISH_PATIENT")
        for skill_id, parameters in [
            ("microbiome_profiling_research", {"specimen": "stool"}),
            ("antibiotic_susceptibility", {"specimen": "bacterial_isolate"}),
            ("tumor_mutation_panel", {"specimen": "tumor_tissue", "panel": "somatic_driver_panel"}),
        ]:
            with self.subTest(skill=skill_id):
                proposal = engine.prepare(skill_id, parameters, [])
                self.assertEqual(proposal["availability"], "unavailable")
                result = engine.execute(proposal, [])
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["points"], 0)
                self.assertIsNone(result["image_path"])


class HistoryAndConsentTests(unittest.TestCase):
    def test_permission_or_prospective_discussion_is_not_completed_history(self):
        for answer in (
            "Yes, we can review my sleep schedule.",
            "I'm happy to discuss my sleep schedule.",
            "Let's talk about my sleep schedule.",
            "We are reviewing my sleep schedule.",
            "Could we discuss my sleep schedule?",
        ):
            with self.subTest(answer=answer):
                engine = SkillEngine("SLEEP_DEPRIVED_WORKER")
                with self.assertRaisesRegex(ValueError, "actual relevant"):
                    engine.prepare("work_sleep_schedule_review", {"evidence_turns": [0, 1]},
                                   [("You", "Can we review your work and sleep schedule?"), ("Patient", answer)])
                self.assertEqual(engine.score, 0)
                self.assertEqual(engine.actions, [])
        engine = SkillEngine("SLEEP_DEPRIVED_WORKER")
        turns = [("You", "Can we review your work and sleep schedule?"),
                 ("Patient", "Yes, we can review it. I sleep four hours after working late.")]
        result = perform(engine, "work_sleep_schedule_review", turns, evidence_turns=[0, 1])
        self.assertEqual(result["status"], "completed")

    def test_history_cannot_complete_from_request_or_bare_assent(self):
        engine = SkillEngine("ALLERGIES_PATIENT")
        for transcript in [[],
                           [("doctor", "Review your allergy triggers."), ("patient", "Okay.")],
                           [("doctor", "What are your allergy triggers?"), ("patient", "Please perform an unrelated test.")]]:
            with self.subTest(transcript=transcript):
                with self.assertRaises(ValueError):
                    engine.prepare("allergy_trigger_history", {"evidence_turns": [0, 1]}, transcript)
        self.assertEqual(engine.score, 0)

    def test_history_records_actual_exchange_not_invented_details(self):
        engine = SkillEngine("ALLERGIES_PATIENT")
        transcript = [("doctor", "When do your allergy symptoms happen?"),
                      ("patient", "My eyes itch when I go outside during pollen season.")]
        result = perform(engine, "allergy_trigger_history", transcript, evidence_turns=[0, 1])
        self.assertEqual(result["points"], 2)
        self.assertIn(transcript[1][1], result["result"])
        self.assertNotIn("detergent", result["result"])
        self.assertEqual(result["evidence_turns"][1]["turn"], 1)

    def test_history_indices_must_be_real_ordered_and_integer(self):
        engine = SkillEngine("ALLERGIES_PATIENT")
        turns = [("doctor", "What are your allergy triggers?"), ("patient", "Pollen gives me watery eyes outside.")]
        for indices in [[1, 0], [0, 99], [0, 0], [False, 1], ["0", "1"]]:
            with self.subTest(indices=indices):
                with self.assertRaises(ValueError):
                    engine.prepare("allergy_trigger_history", {"evidence_turns": indices}, turns)

    def test_future_diary_is_not_instant_completion(self):
        engine = SkillEngine("SLEEP_DEPRIVED_WORKER")
        for answer in ["I will start a sleep diary tomorrow.",
                       "I will keep a sleep diary.",
                       "I do not have a sleep diary."]:
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(ValueError, "diary"):
                    engine.prepare("sleep_diary_review", {"evidence_turns": [0, 1]},
                                   [("doctor", "Review your sleep diary."), ("patient", answer)])
        turns = [("doctor", "What does your sleep diary show?"),
                 ("patient", "My sleep diary shows 4.5 hours each night for three weeks.")]
        result = perform(engine, "sleep_diary_review", turns, evidence_turns=[0, 1])
        self.assertEqual(result["points"], 2)
        self.assertIn("4.5", result["result"])
        self.assertIn("Existing case record reviewed:", result["result"])
        self.assertIn("irregular bedtimes", result["result"])

    def test_epworth_requires_actual_reviewed_total(self):
        engine = SkillEngine("SLEEP_DEPRIVED_WORKER")
        incomplete = [("doctor", "Review the Epworth sleepiness scale."), ("patient", "I doze while reading books.")]
        with self.assertRaisesRegex(ValueError, "completed Epworth"):
            engine.prepare("epworth_sleepiness_scale", {"evidence_turns": [0, 1]}, incomplete)
        turns = [("doctor", "What is your completed Epworth sleepiness score?"),
                 ("patient", "My Epworth sleepiness score is 15 out of 24.")]
        result = perform(engine, "epworth_sleepiness_scale", turns, evidence_turns=[0, 1])
        self.assertIn("15 out of 24", result["result"])

    def test_consent_is_separate_specific_and_not_model_bool(self):
        engine = SkillEngine("ECCENTRIC_NEIGHBOR")
        skill = next(s for s in engine.catalog if s.id == "genital_examination")
        good = consent(skill)
        for bad in [
            [("doctor", "May I check your temperature?"), ("patient", "Yes.")],
            [("doctor", "May I perform genital examination?"), ("patient", "No.")],
            [("doctor", "May I perform genital examination?"), ("patient", "Yes, but not now.")],
            [("doctor", "The genital examination is done."), ("patient", "Yes.")],
        ]:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "consent"):
                    engine.prepare("genital_examination", {"site": "external_genital", "consent_turn": 1}, bad)
        with self.assertRaises(ValueError):
            engine.prepare("genital_examination", {"site": "external_genital", "consent": True}, good)
        result = perform(engine, "genital_examination", good, site="external_genital", consent_turn=2)
        self.assertEqual(result["status"], "completed")
        self.assertIn("urethral discharge", result["result"])

    def test_execute_rechecks_withdrawn_consent(self):
        engine = SkillEngine("ECCENTRIC_NEIGHBOR")
        skill = next(s for s in engine.catalog if s.id == "genital_examination")
        turns = consent(skill)
        proposal = engine.prepare("genital_examination",
                                  {"site": "external_genital", "consent_turn": 2}, turns)
        turns += [("patient", "I changed my mind; do not examine me.")]
        with self.assertRaisesRegex(ValueError, "withdrawn"):
            engine.execute(proposal, turns)
        self.assertEqual(engine.score, 0)
        self.assertEqual(engine.actions, [])

    def test_negative_history_answers_are_still_real_exchange(self):
        engine = SkillEngine("ANXIOUS_ADULT")
        turns = [("doctor", "Do these panic episodes involve fainting?"),
                 ("patient", "I have not fainted during any of my panic episodes.")]
        result = perform(engine, "panic_symptom_assessment", turns, evidence_turns=[0, 1])
        self.assertEqual(result["status"], "completed")
        self.assertIn("have not fainted", result["result"])

    def test_counseling_referral_is_not_completed_counseling(self):
        engine = SkillEngine("MIGRAINE_SUFFERER")
        turns = [("doctor", "I will refer you for genetic counseling."),
                 ("patient", "I will discuss genetic testing there.")]
        with self.assertRaises(ValueError):
            engine.prepare("genetic_counseling", {"evidence_turns": [0, 1]}, turns)


class ScoringTests(unittest.TestCase):
    def test_duplicate_and_component_overlap_in_both_orders(self):
        turns = [("patient", "My back pain is worse after activity.")]
        for first, second in [("neurological_examination", "strength_and_reflex_testing"),
                              ("strength_and_reflex_testing", "neurological_examination")]:
            with self.subTest(first=first):
                engine = SkillEngine("ELDERLY_WITH_BACK_PAIN")
                p1 = {"site": "legs"} if first == "strength_and_reflex_testing" else {}
                p2 = {"site": "legs"} if second == "strength_and_reflex_testing" else {}
                r1 = perform(engine, first, turns, **p1)
                r2 = perform(engine, second, turns, **p2)
                self.assertLessEqual(r1["points"] + r2["points"], 2)
                self.assertEqual(r2["status"], "duplicate" if first == "neurological_examination" else "completed")

    def test_combined_urine_naat_covers_gonorrhea(self):
        engine = SkillEngine("ECCENTRIC_NEIGHBOR")
        skills = {s.id: s for s in engine.catalog}
        turns = consent(skills["urine_nucleic_acid_amplification_test"])
        first = perform(engine, "urine_nucleic_acid_amplification_test", turns, specimen="urine", consent_turn=2)
        turns += [("clinician", "May I perform Gonorrhea NAAT?"), ("patient", "Yes.")]
        second = perform(engine, "gonorrhea_naat", turns, specimen="urine", consent_turn=4)
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["points"], 0)
        self.assertIn("negative for Neisseria", first["result"])

    def test_partial_sti_panel_never_orders_chlamydia(self):
        engine = SkillEngine("ECCENTRIC_NEIGHBOR")
        skills = {s.id: s for s in engine.catalog}
        turns = consent(skills["gonorrhea_naat"])
        first = perform(engine, "gonorrhea_naat", turns, specimen="urine", consent_turn=2)
        self.assertIn("Chlamydia was not assayed", first["result"])
        turns += [("clinician", "May I perform Urine nucleic acid amplification test?"), ("patient", "Yes.")]
        second = perform(engine, "urine_nucleic_acid_amplification_test", turns, specimen="urine", consent_turn=4)
        self.assertEqual(second["status"], "completed")
        self.assertLessEqual(first["points"] + second["points"], 2)
        self.assertEqual(second["new_units"], ["chlamydia_urine"])

    def test_joint_and_ankle_examination_are_same_anatomical_action(self):
        engine = SkillEngine("SPRAINED_ANKLE_ATHLETE")
        turns = [("patient", "I rolled my ankle yesterday.")]
        perform(engine, "joint_examination", turns, site="ankle")
        self.assertEqual(perform(engine, "ankle_examination", turns)["status"], "duplicate")

    def test_biopsy_and_histopathology_are_linked_not_double_charged(self):
        engine = SkillEngine("RASH_PATIENT")
        with self.assertRaisesRegex(ValueError, "specimen"):
            engine.prepare("histopathology", {"specimen": "skin_punch"}, [])
        skill = next(s for s in engine.catalog if s.id == "biopsy")
        turns = consent(skill, "I have an itchy rash after using detergent.")
        biopsy = perform(engine, "biopsy", turns, site="clothing_contact_rash", procedure="skin_punch", consent_turn=2)
        histology = perform(engine, "histopathology", turns, specimen="skin_punch")
        self.assertNotIn("spongiotic", biopsy["result"])
        self.assertIn("spongiotic", histology["result"])
        self.assertEqual(biopsy["points"] + histology["points"], -1)

    def test_conditional_ankle_radiograph_needs_more_than_pain(self):
        for statement, points in [
            ("My ankle hurts but I can take four steps.", -1),
            ("My ankle hurts and I cannot take four steps.", 2),
            ("My ankle hurts but I have no bony tenderness.", -1),
            ("My ankle hurts. Do I have bony tenderness?", -1),
        ]:
            with self.subTest(statement=statement):
                result = perform(SkillEngine("SPRAINED_ANKLE_ATHLETE"), "ankle_x_ray", [("patient", statement)])
                self.assertEqual(result["points"], points)

    def test_completed_ruleout_makes_ankle_imaging_unnecessary(self):
        engine = SkillEngine("SPRAINED_ANKLE_ATHLETE")
        turns = [("patient", "I hurt my ankle and cannot take four steps.")]
        perform(engine, "ankle_examination", turns)
        perform(engine, "weight_bearing_assessment", turns, site="ankle")
        result = perform(engine, "ankle_x_ray", turns)
        self.assertEqual(result["points"], -1)
        self.assertIn("negative rule-out", result["rationale"])

    def test_new_deterioration_after_exam_can_justify_escalation(self):
        engine = SkillEngine("SPRAINED_ANKLE_ATHLETE")
        turns = [("patient", "My ankle hurts.")]
        perform(engine, "ankle_examination", turns)
        perform(engine, "weight_bearing_assessment", turns, site="ankle")
        turns += [("patient", "It is worse now and I cannot take four steps.")]
        self.assertEqual(perform(engine, "ankle_x_ray", turns)["points"], 2)

    def test_no_retroactive_score_and_repeat_cannot_farm(self):
        engine = SkillEngine("ANXIOUS_ADULT")
        first = perform(engine, "electrocardiogram")
        self.assertEqual(first["points"], 0)
        result = perform(engine, "electrocardiogram", [("patient", "My heart races.")])
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(engine.score, 0)
        self.assertEqual(engine.actions[0]["points"], 0)

    def test_distinct_justified_pathogen_assays_are_not_suppressed(self):
        engine = SkillEngine("FEVERISH_PATIENT")
        turns = [("patient", "I have fever and body aches and a cough.")]
        rapid = perform(engine, "rapid_influenza_test", turns)
        pcr = perform(engine, "targeted_pathogen_pcr", turns, specimen="nasal_swab", target="SARS_CoV_2")
        self.assertEqual(rapid["points"] + pcr["points"], 3)
        self.assertEqual(pcr["status"], "completed")

    def test_legacy_compound_urinalysis_already_contains_culture(self):
        engine = SkillEngine("ECCENTRIC_NEIGHBOR")
        turns = [("Patient", "I have burning when urinating.")]
        first = perform(engine, "urinalysis", turns)
        second = perform(engine, "sample_culture", turns, specimen="urine", organism_class="bacteria")
        self.assertIn("culture", first["result"])
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["points"], 0)

    def test_new_metagenomics_is_performable_unnecessary_and_workflow_specific(self):
        for case, expected in (("FEVERISH_PATIENT", "influenza A"), ("COMMON_COLD_KID", "rhinovirus")):
            for workflow in ("RNA", "DNA_and_RNA", "DNA"):
                with self.subTest(case=case, workflow=workflow):
                    result = perform(SkillEngine(case), "viral_metagenomic_sequencing",
                                     specimen="nasal_swab", workflow=workflow)
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["points"], -1)
                    self.assertIsNone(result["image_path"])
                    if workflow == "DNA":
                        self.assertIn("does not assay RNA", result["result"])
                    else:
                        self.assertIn(expected, result["result"])

    def test_raw_penalties_survive_display_clamp(self):
        engine = SkillEngine("FEVERISH_PATIENT", scoring=ScoringConfig(minimum=-3, maximum=3))
        for skill_id in ("visual_acuity", "fundoscopic_examination", "eye_examination",
                         "ear_examination", "blood_smear", "fecal_calprotectin"):
            self.assertEqual(perform(engine, skill_id)["points"], -1)
        self.assertEqual(engine.score, -3)
        self.assertEqual(engine.summary()["raw_score"], -6)
        result = perform(engine, "rapid_influenza_test", [("Patient", "I have fever and chills.")])
        self.assertEqual(result["points"], 2)
        self.assertEqual(engine.summary()["raw_score"], -4)
        self.assertEqual(engine.score, -3)

    def test_metagenomic_workflow_components_are_not_lost_or_double_charged(self):
        engine = SkillEngine("FEVERISH_PATIENT")
        dna = perform(engine, "viral_metagenomic_sequencing", specimen="nasal_swab", workflow="DNA")
        combined = perform(engine, "viral_metagenomic_sequencing", specimen="nasal_swab", workflow="DNA_and_RNA")
        repeated = perform(engine, "viral_metagenomic_sequencing", specimen="nasal_swab", workflow="RNA")
        self.assertEqual(combined["status"], "completed")
        self.assertEqual(combined["new_units"], ["nasal_viral_RNA"])
        self.assertIn("influenza A", combined["result"])
        self.assertEqual(dna["points"] + combined["points"] + repeated["points"], -1)
        self.assertEqual(repeated["status"], "duplicate")

    def test_real_game_roles_preserve_indices_and_do_not_turn_clinician_claim_into_evidence(self):
        engine = SkillEngine("ANXIOUS_ADULT")
        turns = [("Case", "Status"), ("You", "You have chest tightness."),
                 ("Diagnosis", "panic disorder"), ("Skill", "Internal annotation")]
        result = perform(engine, "electrocardiogram", turns)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["known_context"]["patient_turns"], [])
        other = SkillEngine("ALLERGIES_PATIENT")
        turns = [("Case", "Status"), ("You", "When do your allergy symptoms happen?"),
                 ("Patient", "My eyes itch outdoors during pollen season.")]
        result = perform(other, "allergy_trigger_history", turns, evidence_turns=[1, 2])
        self.assertEqual(result["points"], 2)
        self.assertEqual(result["evidence_turns"][1]["turn"], 2)

    def test_game_annotations_do_not_break_real_dialogue_adjacency(self):
        turns = [("You", "When do your allergy symptoms happen?"),
                 ("Case", "A game status annotation."),
                 ("Patient", "My eyes itch outdoors during pollen season.")]
        result = perform(SkillEngine("ALLERGIES_PATIENT"), "allergy_trigger_history", turns, evidence_turns=[0, 2])
        self.assertEqual(result["points"], 2)
        self.assertEqual(result["evidence_turns"][1]["turn"], 2)
        with self.assertRaisesRegex(ValueError, "annotations"):
            perform(SkillEngine("ALLERGIES_PATIENT"), "allergy_trigger_history", turns, evidence_turns=[0, 1, 2])
        consent_turns = [("You", "May I perform genital examination?"),
                         ("Case", "Internal message."), ("Patient", "Yes.")]
        result = perform(SkillEngine("ECCENTRIC_NEIGHBOR"), "genital_examination",
                         consent_turns, site="external_genital", consent_turn=2)
        self.assertEqual(result["status"], "completed")

    def test_bounded_configurable_score_and_serializable_summary(self):
        engine = SkillEngine("FEVERISH_PATIENT", scoring=ScoringConfig(maximum=3, minimum=-3))
        turns = [("patient", "I have fever and body aches and a cough.")]
        for skill in ["temperature", "pulse_heart_rate", "respiratory_rate", "lung_auscultation"]:
            perform(engine, skill, turns)
        self.assertEqual(engine.score, 3)
        summary = engine.summary()
        self.assertEqual(summary["raw_score"], sum(a["points"] for a in summary["actions"]))
        self.assertIn("clamp", summary["formula"])
        json.dumps(summary)
        for kwargs in [{"indicated": 1000}, {"unnecessary": 3}, {"minimum": 1},
                       {"maximum": -1}, {"goal_budget": 11}, {"indicated": True}]:
            with self.assertRaises(ValueError):
                ScoringConfig(**kwargs)


class MatrixExecutionTests(unittest.TestCase):
    def test_all_non_history_case_actions_validate_and_return_explicit_results(self):
        fixtures = json.loads((DATA / "cases.json").read_text())
        for case in fixtures["case_order"]:
            engine = SkillEngine(case)
            for skill in engine.catalog:
                if skill.id in HISTORIES or skill.id == "histopathology":
                    continue
                with self.subTest(case=case, skill=skill.id):
                    parameters = values_for(skill)
                    turns = []
                    if skill.id in CONSENT:
                        turns = consent(skill)
                        parameters["consent_turn"] = 2
                    proposal = engine.prepare(skill.id, parameters, turns)
                    result = engine.execute(proposal, turns)
                    self.assertIn(result["status"], {"completed", "duplicate", "unavailable"})
                    self.assertIsInstance(result["result"], str)
                    self.assertTrue(result["result"])
                    self.assertIsInstance(result["points"], int)
                    json.dumps(result)


if __name__ == "__main__":
    unittest.main()
