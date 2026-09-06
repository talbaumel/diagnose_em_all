"""Universal fictional diagnostic catalog and deterministic, evidence-gated scoring.

The caller interprets speech with its model, then obtains human confirmation.
This module never routes free text to a skill or accepts model-authored findings.
Catalog illustrations are UI decoration; only original case assets are evidence.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "skills"
Transcript = Sequence[tuple[str, str]]


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    category: str
    description: str
    aliases: tuple[str, ...]
    examples: tuple[str, ...]
    note: str
    icon: Path | None
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ScoringConfig:
    indicated: int = 2
    alternative: int = 1
    unnecessary: int = -1
    minimum: int = -20
    maximum: int = 20
    goal_budget: int = 2

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if type(value) is not int:
                raise ValueError("Scoring configuration must contain integers.")
        if not (0 <= self.alternative <= self.indicated <= 10):
            raise ValueError("Use modest rewards: 0 <= alternative <= indicated <= 10.")
        if not (-10 <= self.unnecessary <= 0):
            raise ValueError("Unnecessary-order points must be between -10 and 0.")
        if not (-100 <= self.minimum <= 0 <= self.maximum <= 100):
            raise ValueError("Score bounds must straddle zero within [-100, 100].")
        if not (0 <= self.goal_budget <= 10):
            raise ValueError("Per-goal reward budget must be between 0 and 10.")


LEGACY_NAMES = (
    "Epworth sleepiness scale", "abdominal examination", "ankle X-ray",
    "ankle examination", "electrocardiogram", "genital examination",
    "hydration assessment", "lumbar spine X-ray", "nasal examination",
    "neurological examination", "rapid influenza test", "skin examination",
    "skin patch test", "sleep diary review", "throat examination",
    "thyroid function test", "urinalysis", "urine nucleic acid amplification test",
)
LEGACY_ALIASES = {
    "epworth_sleepiness_scale": ("Epworth", "ESS", "daytime sleepiness questionnaire"),
    "abdominal_examination": ("abdominal exam", "examine the abdomen"),
    "ankle_x_ray": ("ankle radiograph", "ankle X-ray"),
    "ankle_examination": ("ankle exam", "examine the injured ankle"),
    "electrocardiogram": ("ECG", "EKG", "electrocardiogram"),
    "genital_examination": ("genital exam", "external genital examination"),
    "hydration_assessment": ("hydration status", "dehydration assessment"),
    "lumbar_spine_x_ray": ("lumbar radiograph", "lower-back X-ray"),
    "nasal_examination": ("nasal exam", "examine the nose"),
    "neurological_examination": ("neurological exam", "neuro exam"),
    "rapid_influenza_test": ("rapid flu test", "influenza antigen test"),
    "skin_examination": ("skin exam", "inspect the skin"),
    "skin_patch_test": ("patch testing", "contact-allergy patch test"),
    "sleep_diary_review": ("review sleep diary", "review existing sleep log"),
    "throat_examination": ("throat exam", "examine the throat"),
    "thyroid_function_test": ("thyroid function", "TSH and free T4"),
    "urinalysis": ("urine dipstick and microscopy", "urine analysis"),
    "urine_nucleic_acid_amplification_test": ("urine NAAT", "combined chlamydia and gonorrhea urine NAAT"),
}


def _id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


TURN = {"type": "integer", "minimum": 0,
        "description": "Zero-based index in the supplied full transcript, not a claimed fact."}
EXCHANGE = {
    "type": "array", "items": TURN, "minItems": 2, "maxItems": 24,
    "uniqueItems": True,
    "description": "Indices of actual clinician question/review and substantive patient response. "
                   "Do not cite a request to do a future history or diary.",
}

# These are the only modeled variants. Broad requests require clarification;
# specifying an unsupported variant never silently substitutes a different test.
FIELDS = {
    "neurovascular_examination": {"site": _enum("ankle")},
    "range_of_motion": {"site": _enum("ankle")},
    "weight_bearing_assessment": {"site": _enum("ankle")},
    "strength_and_reflex_testing": {"site": _enum("legs")},
    "joint_examination": {"site": _enum("ankle")},
    "breast_examination": {"site": _enum("breast")},
    "skin_scraping": {"site": _enum("clothing_contact_rash"), "method": _enum("KOH")},
    "chest_ct": {"protocol": _enum("noncontrast")},
    "spine_mri": {"site": _enum("lumbar")},
    "ultrasound": {"site": _enum("abdomen")},
    "biopsy": {"site": _enum("clothing_contact_rash"), "procedure": _enum("skin_punch")},
    "histopathology": {"specimen": _enum("skin_punch")},
    "immunohistochemistry": {"specimen": _enum("tumor_tissue"), "panel": _enum("lineage_panel")},
    "er_pr_her2_testing": {"specimen": _enum("breast_tumor"),
                         "markers": {"type": "array", "items": _enum("ER", "PR", "HER2"),
                                     "minItems": 1, "maxItems": 3, "uniqueItems": True}},
    "bone_marrow_examination": {"procedure": _enum("aspirate", "trephine_biopsy")},
    "flow_cytometry": {"specimen": _enum("peripheral_blood"), "analysis": _enum("B_cell_clonality")},
    "joint_fluid_analysis": {"site": _enum("ankle"), "specimen": _enum("synovial_fluid"),
                            "analysis": _enum("cell_count_and_crystals")},
    "tumor_mutation_panel": {"specimen": _enum("tumor_tissue"), "panel": _enum("somatic_driver_panel")},
    "karyotype": {"specimen": _enum("peripheral_blood")},
    "fish": {"specimen": _enum("tumor_tissue"), "target": _enum("HER2")},
    "molecular_testing": {"target": _enum("unspecified"), "method": _enum("unspecified")},
    "germline_genetic_testing": {"specimen": _enum("blood"), "panel": _enum("hereditary_cancer")},
    "chromosomal_microarray": {"specimen": _enum("blood")},
    "gene_sequencing": {"specimen": _enum("blood"), "target": _enum("BRCA1"),
                        "purpose": _enum("germline")},
    "autoimmune_antibodies": {"panel": _enum("ANA")},
    "inflammatory_markers": {"marker": _enum("CRP")},
    "targeted_allergy_testing": {"allergen": _enum("pollen"), "method": _enum("specific_IgE")},
    "gonorrhea_naat": {"specimen": _enum("urine")},
    "hiv_testing": {"test_type": _enum("fourth_generation")},
    "syphilis_testing": {"algorithm": _enum("treponemal_screen_with_reflex")},
    "stool_pathogen_testing": {"specimen": _enum("stool"), "panel": _enum("viral_PCR")},
    "celiac_serology": {"panel": _enum("tTG_IgA_and_total_IgA")},
    "microbiome_profiling_research": {"specimen": _enum("stool")},
    "sample_culture": {"specimen": _enum("urine"), "organism_class": _enum("bacteria")},
    "antibiotic_susceptibility": {"specimen": _enum("bacterial_isolate")},
    "targeted_pathogen_pcr": {"specimen": _enum("nasal_swab"), "target": _enum("SARS_CoV_2")},
    "pathogen_sequencing": {"specimen": _enum("bacterial_isolate")},
    "sleep_study": {"method": _enum("laboratory_polysomnography")},
    "viral_metagenomic_sequencing": {"specimen": _enum("nasal_swab"), "workflow": _enum("DNA", "RNA", "DNA_and_RNA")},
    "genital_examination": {"site": _enum("external_genital")},
    "skin_patch_test": {"allergen": _enum("detergent_fragrance")},
    "urine_nucleic_acid_amplification_test": {"specimen": _enum("urine")},
}

CONSENT = frozenset({
    "breast_examination", "biopsy", "bone_marrow_examination", "joint_fluid_analysis",
    "germline_genetic_testing", "gene_sequencing", "gonorrhea_naat", "hiv_testing",
    "syphilis_testing", "sexual_health_history", "genital_examination",
    "urine_nucleic_acid_amplification_test", "skin_patch_test",
})
HISTORIES = {
    "functional_assessment": r"walk|stand|dress|activit|work|move|function",
    "family_pedigree": r"mother|father|sister|brother|parent|famil|relative",
    "genetic_counseling": r"genetic|inherit|variant|genom|test.*risk",
    "headache_diary": r"headache|migraine",
    "allergy_trigger_history": r"pollen|detergent|allerg|outdoor|trigger|season",
    "panic_symptom_assessment": r"heart|chest|panic|fear|dizz|trembl|episode",
    "exposure_history_review": r"meal|food|contact|expos|travel|detergent|partner|sick",
    "work_sleep_schedule_review": r"sleep|bed|shift|screen|work|hour",
    "sleep_apnea_screening": r"snor|apnea|apnoea|gasp|breath.*sleep|sleep.*breath",
    "sexual_health_history": r"sex|partner|condom|discharge|urina",
    "sleep_diary_review": r"sleep|bed|hour",
    "epworth_sleepiness_scale": r"epworth|doz|sleepiness|24|fifteen",
}

CASE_CUES = {
    "COMMON_COLD_KID": r"runny nose|sore throat|cough|cold",
    "STOMACHACHE_TEEN": r"diarrh|stomach|abdomin|cramp|nausea",
    "MIGRAINE_SUFFERER": r"headache|migraine|throbbing|light.{0,15}hurt",
    "ALLERGIES_PATIENT": r"sneez|itchy.{0,12}eyes|watery.{0,12}eyes|runny nose|pollen",
    "SPRAINED_ANKLE_ATHLETE": r"ankle|rolled.{0,15}foot",
    "ANXIOUS_ADULT": r"heart.{0,15}rac|chest.{0,15}tight|panic|trembl|dizz|palpitation",
    "FEVERISH_PATIENT": r"fever|chills|body aches|cough",
    "RASH_PATIENT": r"rash|itch|detergent",
    "ELDERLY_WITH_BACK_PAIN": r"back.{0,15}pain|back.{0,15}stiff",
    "SLEEP_DEPRIVED_WORKER": r"sleep|exhaust|tired|bedtime",
    "ECCENTRIC_NEIGHBOR": r"urina|discharge|dysuria|sex|partner",
}

CONDITIONS = {
    "ankle_x_ray": (
        r"(?:cannot|can't|unable to) (?:walk|bear weight|take (?:four|4) steps)|bony tenderness",
        "Imaging needs documented inability to take four steps or focal bony tenderness; "
        "painful walking alone is not an Ottawa-rule indication."),
    "chest_x_ray": (
        r"short(?:ness)? of breath|cough(?:ing)? up blood|breathless",
        "Chest imaging needs a lower-respiratory red flag, not fever or cough alone."),
    "spine_mri": (
        r"(?:new|progressive) (?:leg )?weakness|urinary retention|saddle (?:numbness|anesthesia)",
        "MRI requires a disclosed neurologic red flag; gradual mechanical pain alone is insufficient."),
    "lumbar_spine_x_ray": (
        r"fell|fall|trauma|history of cancer",
        "Lumbar radiography requires a disclosed fracture or malignancy concern; age alone is insufficient."),
    "ultrasound": (
        r"right upper.{0,15}pain|pain.{0,15}right upper|localized.{0,15}abdomin",
        "Abdominal ultrasound needs localized symptoms; short-lived diffuse diarrhea alone is insufficient."),
    "electrolytes": (
        r"(?:cannot|can't|unable to) keep.{0,15}(?:fluid|water)|faint(?:ed|ing)|no urine",
        "Electrolytes require significant fluid-loss concern, not mild diarrhea alone."),
    "complete_blood_count": (
        r"bloody (?:stool|diarrhea)|blood in (?:my )?stool|faint(?:ed|ing)",
        "A blood count for acute diarrhea needs bleeding or systemic-severity concern."),
    "inflammatory_markers": (
        r"blood in (?:my )?stool|bloody (?:stool|diarrhea)|back pain.{0,20}fever|fever.{0,20}back pain",
        "Inflammatory markers need an inflammatory or systemic red flag in this presentation."),
    "stool_pathogen_testing": (
        r"bloody (?:stool|diarrhea)|blood in (?:my )?stool|outbreak|(?:seven|7|eight|8|ten|10) days|immunocompromised",
        "Stool pathogen testing needs a severe, persistent, immunocompromised, or outbreak context."),
    "skin_patch_test": (
        r"persist|recurr|keeps coming back|not (?:getting )?better|(?:stopped|avoided|changed).{0,30}detergent",
        "Patch testing is conditional on persistent/recurrent rash or uncertainty after trigger avoidance."),
    "sleep_study": (
        r"snor|stop(?:s|ped)? breathing|witnessed apnea|witnessed apnoea|gasp",
        "A sleep study needs apnea suspicion; a short sleep schedule alone does not justify it."),
}

# Semantic units prevent repeating subcomponents under a compound order.
UNITS = {
    "urine_nucleic_acid_amplification_test": {"chlamydia_urine", "gonorrhea_urine"},
    "gonorrhea_naat": {"gonorrhea_urine"},
    "neurological_examination": {"neurological_screen", "leg_strength_reflexes"},
    "strength_and_reflex_testing": {"leg_strength_reflexes"},
    "ankle_examination": {"ankle_inspection"},
    "joint_examination": {"ankle_inspection"},
    "hydration_assessment": {"hydration_mucosa", "capillary_refill", "blood_pressure"},
    "capillary_refill": {"capillary_refill"},
    "blood_pressure": {"blood_pressure"},
    "skin_examination": {"skin_inspection", "rash_distribution"},
    "rash_distribution_assessment": {"rash_distribution"},
    "urinalysis": {"urine_microscopy"},
    "sample_culture": {"urine_culture"},
}
GOALS = {
    "gonorrhea_naat": "urine_sti", "urine_nucleic_acid_amplification_test": "urine_sti",
    "neurological_examination": "neurology", "strength_and_reflex_testing": "neurology",
    "ankle_examination": "ankle_inspection", "joint_examination": "ankle_inspection",
    "hydration_assessment": "hydration", "capillary_refill": "hydration", "blood_pressure": "hydration",
    "skin_examination": "skin_pattern", "rash_distribution_assessment": "skin_pattern",
    "biopsy": "skin_pathology", "histopathology": "skin_pathology",
    "work_sleep_schedule_review": "sleep_schedule", "sleep_diary_review": "sleep_schedule",
}


def _schema(skill_id: str) -> dict[str, Any]:
    properties = copy.deepcopy(FIELDS.get(skill_id, {}))
    if skill_id in HISTORIES:
        properties["evidence_turns"] = copy.deepcopy(EXCHANGE)
    if skill_id in CONSENT:
        properties["consent_turn"] = copy.deepcopy(TURN)
        properties["consent_turn"]["description"] += (
            " Must identify an explicit patient agreement immediately after a clinician "
            "request naming this procedure/discussion; ordering confirmation is not consent.")
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def load_catalog() -> tuple[Skill, ...]:
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    phrases = json.loads((DATA / "phrases.json").read_text(encoding="utf-8"))
    catalog = []
    for item in manifest:
        skill_id = ("viral_metagenomic_sequencing" if item["name"] == "Viral metagenomic sequencing"
                    else _id(Path(item["filename"]).stem))
        phrasing = phrases[item["name"]]
        note = phrasing.get("note", "")
        if skill_id in HISTORIES:
            note += " Requires actual completed clinician–patient exchange with transcript references."
        if skill_id in CONSENT:
            note += " Explicit patient consent is required independently of order confirmation."
        catalog.append(Skill(
            skill_id, item["name"], item["category"],
            f"Propose {item['name'].lower()} for human confirmation; do not execute from speech alone.",
            tuple(phrasing["aliases"]), tuple(phrasing["examples"]), note.strip(),
            (DATA / "images" / item["filename"]).resolve(), _schema(skill_id)))
    for name in LEGACY_NAMES:
        skill_id = _id(name)
        note = ("Requires actual completed review and patient exchange, not an invented questionnaire."
                if skill_id in HISTORIES else "Preserves the distinct original case investigation.")
        if skill_id == "urine_nucleic_acid_amplification_test":
            note += " This combined urine assay includes chlamydia and gonorrhea; do not also order gonorrhea NAAT."
        elif skill_id == "neurological_examination":
            note += " Includes strength and reflexes; a combined request already covers these components."
        elif skill_id == "skin_patch_test":
            note += " Delayed contact allergy testing, not an immediate pollen-IgE test; conditional on persistent or uncertain dermatitis."
        catalog.append(Skill(
            skill_id, name[0].upper() + name[1:], "Original case investigations",
            f"Propose {name.lower()} for human confirmation.", (name, *LEGACY_ALIASES[skill_id]),
            (
                (f"Review the completed {name} with the patient." if skill_id in HISTORIES else f"Perform {name}."),
                (f"Let's discuss the existing {name} together." if skill_id in HISTORIES else f"Please arrange {name}."),
                (f"Go through the recorded {name} with me." if skill_id in HISTORIES else f"I would like to request {name}."),
            ),
            note, None, _schema(skill_id)))
    ids = [skill.id for skill in catalog]
    if len(ids) != len(set(ids)):
        raise ValueError("Catalog IDs must be unique.")
    return tuple(catalog)


def _validate(value: Any, schema: Mapping[str, Any], path: str = "parameters") -> None:
    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict:
            raise ValueError(f"{path} must be an object.")
        extra = set(value) - set(schema["properties"])
        missing = set(schema.get("required", ())) - set(value)
        if extra:
            raise ValueError(f"{path}: unexpected keys: {', '.join(sorted(map(str, extra)))}.")
        if missing:
            raise ValueError(f"Clarify {', '.join(sorted(missing))}; required for this skill.")
        for key, child in schema["properties"].items():
            if key in value:
                _validate(value[key], child, f"{path}.{key}")
    elif kind == "string":
        if type(value) is not str or value not in schema["enum"]:
            raise ValueError(f"{path}: supported values are {schema['enum']}; clarify the request.")
    elif kind == "integer":
        if type(value) is not int or value < schema.get("minimum", 0):
            raise ValueError(f"{path} must be a nonnegative integer transcript index.")
    elif kind == "array":
        if type(value) is not list or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError(f"{path} requires {schema['minItems']}–{schema['maxItems']} items.")
        for item in value:
            _validate(item, schema["items"], path)
        if schema.get("uniqueItems") and len(value) != len(set(value)):
            raise ValueError(f"{path} must not repeat items.")
    else:
        raise ValueError(f"Unsupported internal schema type: {kind}.")


def _transcript(transcript: Transcript) -> tuple[tuple[str, str], ...]:
    result = []
    for turn in transcript:
        if not isinstance(turn, (tuple, list)) or len(turn) != 2:
            raise ValueError("Transcript requires (role, text) pairs.")
        role, text = turn
        if not isinstance(role, str) or not isinstance(text, str):
            raise ValueError("Transcript roles and text must be strings.")
        role = role.lower()
        role = {
            "doctor": "clinician", "user": "clinician", "you": "clinician",
            "assistant": "patient", "case": "system", "diagnosis": "system",
            "skill": "system",
        }.get(role, role)
        if role not in {"clinician", "patient", "system"}:
            raise ValueError(f"Unknown transcript role: {role}.")
        result.append((role, text.strip()))
    return tuple(result)


def _affirmed(text: str, pattern: str) -> bool:
    """Check evidence, never intent routing; uncertainty and local negation fail closed."""
    for sentence in re.findall(r"[^.!?;\n]+[.!?;\n]?", text.lower()):
        if sentence.rstrip().endswith("?"):
            continue
        for clause in re.split(r"\bbut\b|\band (?=i |my )", sentence):
            if _affirmed_clause(clause, pattern):
                return True
    return False


def _affirmed_clause(clause: str, pattern: str) -> bool:
    for match in re.finditer(pattern, clause):
        prefix = clause[:match.start()]
        nearby = prefix
        if re.search(r"\b(no|not|never|deny|denies|without|don't|doesn't|isn't|haven't|maybe|might|if)\b", nearby):
            continue
        return True
    return False


def _history_detail(text: str, topic: str) -> bool:
    """A promise to discuss a topic is not clinical information about that topic."""
    prospective = (
        r"\b(?:can|could|may|will|would|should|want|like|ready|happy|agree|consent|let'?s)\b"
        r".{0,50}\b(?:review|discuss|talk|go over|look at|check|assess|start|complete|perform|take)\b"
        r"|\b(?:reviewing|discussing|talking about|going over)\b"
    )
    for sentence in re.findall(r"[^.!?;\n]+[.!?;\n]?", text):
        if sentence.rstrip().endswith("?") or re.search(prospective, sentence, re.I):
            continue
        if len(re.findall(r"\w+", sentence)) >= 4 and re.search(topic, sentence, re.I):
            return True
    return False


def _previous_dialogue_index(transcript: tuple[tuple[str, str], ...], index: int) -> int | None:
    return next((prior for prior in range(index - 1, -1, -1)
                 if transcript[prior][0] in {"clinician", "patient"}), None)


class SkillEngine:
    def __init__(self, patient_type: str, legacy_tests: Sequence[Any] = (),
                 scoring: ScoringConfig | None = None):
        fixtures = json.loads((DATA / "cases.json").read_text(encoding="utf-8"))
        if patient_type not in fixtures["case_order"]:
            raise ValueError(f"Unsupported fictional case: {patient_type}.")
        self.patient_type = patient_type
        self.scoring = scoring or ScoringConfig()
        if not isinstance(self.scoring, ScoringConfig):
            raise ValueError("scoring must be a ScoringConfig instance.")
        self._catalog = load_catalog()
        self._by_id = {skill.id: skill for skill in self._catalog}
        rows = fixtures["rows"]
        if set(rows) != set(self._by_id):
            raise ValueError("Every catalog skill requires an explicit fixture row.")
        index = fixtures["case_order"].index(patient_type)
        self._case: dict[str, dict[str, str]] = {}
        for skill_id, row in rows.items():
            if len(row["outcomes"]) != 11 or len(row["relevance"]) != 11:
                raise ValueError(f"Incomplete explicit case grid for {skill_id}.")
            for outcome in row["outcomes"]:
                if outcome not in row["results"]:
                    raise ValueError(f"Missing authored outcome for {skill_id}.")
            if any(code not in fixtures["classes"] for code in row["relevance"]):
                raise ValueError(f"Missing appropriateness class for {skill_id}.")
            self._case[skill_id] = {"result": row["results"][row["outcomes"][index]],
                                   "relevance": row["relevance"][index]}
        self._actions: list[dict[str, Any]] = []
        self._prepared: dict[str, tuple[dict[str, Any], tuple[tuple[str, str], ...]]] = {}
        self._executed_proposals: set[str] = set()
        self._units: set[str] = set()
        self._completed: set[str] = set()
        self._goal_rewards: dict[str, int] = {}
        self._goal_penalties: set[str] = set()
        self._raw_score = 0
        self._legacy: dict[str, str] = {}
        # The supplied legacy objects identify original case evidence only.
        # Findings remain authoritative from repository fixtures, not caller/model strings.
        authored = {}
        for path in (ROOT / "data" / "prompts").glob("*.json"):
            case = json.loads(path.read_text(encoding="utf-8"))
            if case.get("patient_type") == patient_type:
                authored = {_id(t["description"]): t["results"] for t in case["tests"]}
                break
        self._legacy = authored

    @property
    def catalog(self) -> tuple[Skill, ...]:
        return copy.deepcopy(self._catalog)

    @property
    def actions(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._actions)

    @property
    def review_records(self) -> dict[str, str]:
        """Existing diary/questionnaire records the patient may discuss when asked."""
        return {self._by_id[skill_id].name: result for skill_id, result in self._legacy.items()
                if skill_id in HISTORIES}

    @property
    def score(self) -> int:
        return max(self.scoring.minimum, min(self.scoring.maximum, self._raw_score))

    def _exchange(self, skill_id: str, parameters: dict[str, Any],
                  transcript: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
        indices = parameters["evidence_turns"]
        if indices != sorted(indices) or any(index >= len(transcript) for index in indices):
            raise ValueError("Evidence turns must be valid transcript indices in chronological order.")
        selected = [(index, *transcript[index]) for index in indices]
        if any(role == "system" for _, role, _ in selected):
            raise ValueError("History references must cite actual clinician/patient dialogue, not game annotations.")
        topic = HISTORIES[skill_id]
        pairs = []
        prior_indices = {}
        for index, role, text in selected:
            prior_index = _previous_dialogue_index(transcript, index)
            if role != "patient" or prior_index is None or prior_index not in indices:
                continue
            prior_role, prior_text = transcript[prior_index]
            if prior_role != "clinician" or not re.search(topic, prior_text, re.I):
                continue
            # Bare assent is consent, not medical history or a completed review.
            if not _history_detail(text, topic):
                continue
            if re.search(r"\b(?:prefer not to|refuse|won't discuss|will discuss|ask me later)\b", text, re.I):
                continue
            pairs.append(index)
            prior_indices[index] = prior_index
        if not pairs:
            raise ValueError(f"Complete an actual relevant clinician question/review and substantive patient response for {self._by_id[skill_id].name}; "
                             "permission or a request alone is not a completed history.")
        combined = " ".join(transcript[prior_indices[index]][1] + " " + transcript[index][1] for index in pairs)
        if skill_id in {"headache_diary", "sleep_diary_review"}:
            patient_words = " ".join(transcript[index][1] for index in pairs)
            existing = re.search(r"\b(?:diary|log|record|entries)\b.{0,25}\b(?:shows?|says?|records?|lists?|contain|last|yesterday)|"
                                 r"\b(?:recorded|logged|wrote|reviewed)\b", patient_words, re.I)
            future = re.search(r"\b(?:will|going to|haven't|have not|not yet|don't have|do not have)\b", patient_words, re.I)
            if not existing or future:
                raise ValueError("Review an existing diary with the patient; starting a future diary is not completion.")
        if skill_id == "epworth_sleepiness_scale":
            patient_words = " ".join(transcript[index][1] for index in pairs)
            if not re.search(r"\b(?:score|total)\b.{0,20}\b(?:[0-9]|fifteen)|\b(?:15|fifteen)\s*(?:out of|/)\s*24", patient_words, re.I):
                raise ValueError("Review an actual completed Epworth score with the patient; an incomplete questionnaire has no total.")
        if skill_id == "genetic_counseling":
            clinician_words = " ".join(transcript[prior_indices[index]][1] for index in pairs)
            if len(clinician_words.split()) < 15 or not re.search(r"risk|limit|implication|uncertain|consent", combined, re.I):
                raise ValueError("Counseling requires substantive discussion of testing implications and a patient response.")
        return [{"turn": index, "role": role, "text": text} for index, role, text in selected]

    def _consent(self, skill: Skill, parameters: dict[str, Any],
                 transcript: tuple[tuple[str, str], ...]) -> None:
        index = parameters["consent_turn"]
        if not 0 < index < len(transcript):
            raise ValueError("Consent must cite an existing patient answer after a clinician request.")
        role, text = transcript[index]
        prior_index = _previous_dialogue_index(transcript, index)
        if prior_index is None:
            raise ValueError("Consent needs an actual preceding clinician request.")
        prior_role, prior = transcript[prior_index]
        affirmative = re.fullmatch(
            r"(?:yes|yes please|yes,? (?:i (?:agree|consent)|you (?:may|can))|"
            r"i (?:agree|consent)|okay|ok|you (?:may|can)|go ahead)[.! ]*", text, re.I)
        names = (skill.name, *skill.aliases, skill.id.replace("_", " "))
        named = any(name.casefold() in prior.casefold() for name in names)
        permission = re.search(r"\b(may|can|consent|permission|agree|okay|ok|allow)\b", prior, re.I)
        if role != "patient" or prior_role != "clinician" or not affirmative or not named or not permission:
            raise ValueError(f"Obtain explicit patient consent to {skill.name} and cite that answer; "
                             "an order-confirmation click or model consent flag is insufficient.")
        withdrawal = (
            r"\b(?:withdraw|revoke).{0,20}consent\b|\bchanged my mind\b|\bnot now\b|"
            r"\b(?:don't|do not).{0,20}(?:do|perform|test|examine)\b|\brefuse\b|"
            r"\bstop (?:the|this|that)\b"
        )
        if any(later_role == "patient" and re.search(withdrawal, later_text, re.I)
               for later_role, later_text in transcript[index + 1:]):
            raise ValueError("Patient consent was subsequently withdrawn or deferred; obtain fresh specific consent.")

    def _check(self, skill_id: str, parameters: dict[str, Any],
               transcript: tuple[tuple[str, str], ...]) -> Skill:
        if not isinstance(skill_id, str) or skill_id not in self._by_id:
            raise ValueError("Unknown stable skill ID; use a catalog skill, not free text.")
        skill = self._by_id[skill_id]
        _validate(parameters, skill.parameters)
        if skill_id in CONSENT:
            self._consent(skill, parameters, transcript)
        if skill_id in HISTORIES:
            self._exchange(skill_id, parameters, transcript)
        if skill_id == "histopathology" and self._case[skill_id]["relevance"] != "N" and "biopsy" not in self._completed:
            raise ValueError("Histopathology requires the previously collected skin punch specimen; order and confirm biopsy first.")
        return skill

    def prepare(self, skill_id: str, parameters: dict[str, Any], transcript: Transcript) -> dict[str, Any]:
        turns = _transcript(transcript)
        skill = self._check(skill_id, parameters, turns)
        entry = self._case[skill_id]
        proposal = {"proposal_id": secrets.token_hex(16), "skill_id": skill.id,
                    "name": skill.name, "parameters": copy.deepcopy(parameters),
                    "requires_confirmation": True,
                    "availability": "unavailable" if entry["relevance"] == "N" else "available",
                    "reason": entry["result"] if entry["relevance"] == "N" else
                    "Fictional investigation; interpretation and score use evidence known at confirmation."}
        self._prepared[proposal["proposal_id"]] = (copy.deepcopy(proposal), turns)
        return proposal

    def _points(self, skill_id: str, transcript: tuple[tuple[str, str], ...]) -> tuple[int, str, str]:
        relevance = self._case[skill_id]["relevance"]
        patient_texts = [text for role, text in transcript if role == "patient"]
        known = any(_affirmed(text, CASE_CUES[self.patient_type]) for text in patient_texts)
        # Objective completed examinations can establish the problem independently
        # of a patient's ability to name it.
        objective = {
            "STOMACHACHE_TEEN": {"abdominal_examination", "hydration_assessment"},
            "ALLERGIES_PATIENT": {"nasal_examination", "eye_examination"},
            "SPRAINED_ANKLE_ATHLETE": {"ankle_examination", "joint_examination"},
            "FEVERISH_PATIENT": {"temperature", "rapid_influenza_test"},
            "RASH_PATIENT": {"skin_examination", "rash_distribution_assessment"},
            "ECCENTRIC_NEIGHBOR": {"urinalysis", "genital_examination"},
        }
        known = known or bool(self._completed & objective.get(self.patient_type, set()))
        if relevance == "B":
            return self.scoring.alternative, "Reasonable low-burden baseline assessment; not a mandatory test.", "baseline"
        if relevance == "U":
            return self.scoring.unnecessary, "Available fictional result, but no indication in this case; avoid unnecessary testing.", "unnecessary"
        if relevance in {"I", "A"}:
            if not known:
                return 0, "The relevant complaint has not yet been disclosed or established; no hindsight credit.", "insufficient_evidence"
            if skill_id in {"hiv_testing", "syphilis_testing"}:
                risk = any(_affirmed(text, r"new partner|unprotected sex|condom broke|without a condom") for text in patient_texts)
                if not risk:
                    return 0, "Adjunct STI screening credit needs an actual disclosed exposure, not a presumed sexual history.", "insufficient_evidence"
            points = self.scoring.indicated if relevance == "I" else self.scoring.alternative
            return points, ("Useful discrimination or negative rule-out for the known presenting problem."
                            if relevance == "I" else "Reasonable alternative or adjunct, not a required checklist item."), "indicated" if relevance == "I" else "alternative"
        if relevance == "C":
            pattern, reason = CONDITIONS[skill_id]
            indicated = any(_affirmed(text, pattern) for text in patient_texts)
            negative = (
                skill_id == "ankle_x_ray" and
                bool(self._completed & {"ankle_examination", "joint_examination"}) and
                "weight_bearing_assessment" in self._completed
            )
            if negative and indicated:
                last_exam_turn = max(
                    (action.get("confirmed_transcript_length", 0) for action in self._actions
                     if action["status"] == "completed" and action["skill_id"] in
                     {"ankle_examination", "joint_examination", "weight_bearing_assessment"}),
                    default=0)
                # A newly reported deterioration after the reassuring exam may
                # justify escalation; the old result must not veto it forever.
                negative = not any(role == "patient" and _affirmed(text, pattern)
                                   for role, text in transcript[last_exam_turn:])
            if negative:
                return self.scoring.unnecessary, "Completed examination supplies a negative rule-out against this indication. " + reason, "unnecessary"
            if known and indicated:
                return self.scoring.indicated, "Conditional indication is present in actual patient evidence. " + reason, "conditional"
            return self.scoring.unnecessary, "Condition not established at confirmed order. " + reason, "unnecessary"
        return 0, self._case[skill_id]["result"], "unavailable"

    def _record(self, skill_id: str, status: str, result: str, rationale: str,
                parameters: dict[str, Any], points: int = 0, **context: Any) -> dict[str, Any]:
        record = {"status": status, "skill_id": skill_id, "name": self._by_id[skill_id].name,
                  "parameters": copy.deepcopy(parameters), "result": result, "image_path": None,
                  "points": points, "rationale": rationale, "score": self.score, **context}
        self._actions.append(copy.deepcopy(record))
        return copy.deepcopy(record)

    def _result(self, skill_id: str, parameters: dict[str, Any],
                turns: tuple[tuple[str, str], ...]) -> tuple[str, str | None, list[dict[str, Any]]]:
        result = self._case[skill_id]["result"]
        image_path = None
        reviewed = []
        if skill_id in HISTORIES:
            reviewed = self._exchange(skill_id, parameters, turns)
            if skill_id in self._legacy:
                result += "\nExisting case record reviewed: " + self._legacy[skill_id]
            result += "\n" + "\n".join(f"{item['role']}: {item['text']}" for item in reviewed)
        elif skill_id in self._legacy:
            original = self._legacy[skill_id]
            if not original.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                result = original
        if skill_id == "temperature" and self.patient_type == "COMMON_COLD_KID":
            # Validate the original evidence before changing any scoring ledger.
            # A missing image must not be replaced by the decorative catalog icon.
            try:
                directory = (ROOT / "data/sprites/tests").resolve(strict=True)
                image = (directory / "thermometer.png").resolve(strict=True)
                if image.parent != directory or not image.is_file():
                    raise ValueError("Original thermometer evidence path is not a supported local file.")
                with image.open("rb") as handle:
                    if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                        raise ValueError("Original thermometer evidence is not a readable PNG image.")
            except OSError as exc:
                raise ValueError("Original thermometer evidence is missing or unreadable; no test or score was recorded.") from exc
            image_path = str(image)
            result = self._case[skill_id]["result"]
        if skill_id == "viral_metagenomic_sequencing" and parameters["workflow"] == "DNA":
            result = (
                "Simulated DNA-only nasal metagenomic analysis assigns no DNA virus in this authored specimen. "
                "This workflow does not assay RNA viruses and cannot exclude influenza, rhinovirus or another RNA infection."
            )
        return result, image_path, reviewed

    def execute(self, proposal: dict[str, Any], transcript: Transcript) -> dict[str, Any]:
        if not isinstance(proposal, dict) or not isinstance(proposal.get("proposal_id"), str):
            raise ValueError("Execute only an engine-prepared, human-confirmed proposal.")
        saved = self._prepared.get(proposal["proposal_id"])
        if saved is None or proposal != saved[0]:
            raise ValueError("Unknown or altered proposal; prepare again rather than trusting model-authored fields.")
        turns = _transcript(transcript)
        if turns[:len(saved[1])] != saved[1]:
            raise ValueError("Transcript snapshot changed; prepare the proposal again.")
        skill_id, parameters = proposal["skill_id"], proposal["parameters"]
        self._check(skill_id, parameters, turns)
        entry = self._case[skill_id]
        if entry["relevance"] == "N":
            self._executed_proposals.add(proposal["proposal_id"])
            return self._record(skill_id, "unavailable", entry["result"], entry["result"], parameters)
        units = UNITS.get(skill_id, {skill_id})
        if skill_id == "urinalysis" and self.patient_type == "ECCENTRIC_NEIGHBOR":
            units = units | {"urine_culture"}
        if skill_id == "viral_metagenomic_sequencing":
            workflow = parameters["workflow"]
            units = {f"nasal_viral_{molecule}" for molecule in ("DNA", "RNA")
                     if workflow == "DNA_and_RNA" or workflow == molecule}
        if units <= self._units:
            self._executed_proposals.add(proposal["proposal_id"])
            return self._record(skill_id, "duplicate", "Already completed; no new test or score.",
                                "Duplicate or fully covered component of a completed order.", parameters,
                                covered_units=sorted(units))
        result, image_path, reviewed = self._result(skill_id, parameters, turns)
        points, rationale, appropriateness = self._points(skill_id, turns)
        goal = GOALS.get(skill_id, skill_id)
        if skill_id in {"urinalysis", "sample_culture"} and self.patient_type == "ECCENTRIC_NEIGHBOR":
            goal = "original_compound_urine_assessment"
        if points > 0:
            remaining = max(0, self.scoring.goal_budget - self._goal_rewards.get(goal, 0))
            awarded = min(points, remaining)
            self._goal_rewards[goal] = self._goal_rewards.get(goal, 0) + awarded
            if awarded != points:
                rationale += " Shared clinical-goal reward budget prevents compound/alternative double scoring."
            points = awarded
        elif points < 0:
            if goal in self._goal_penalties:
                points = 0
                rationale += " Linked/overlapping order already charged; no duplicate penalty."
            self._goal_penalties.add(goal)
        before = self.score
        known_context = {
            "patient_turns": [{"turn": index, "text": text} for index, (role, text) in enumerate(turns) if role == "patient"],
            "prior_completed_skills": sorted(self._completed),
        }
        self._raw_score += points
        self._completed.add(skill_id)
        self._executed_proposals.add(proposal["proposal_id"])
        new_units = units - self._units
        self._units.update(units)
        return self._record(
            skill_id, "completed", result, rationale, parameters, points,
            image_path=image_path, appropriateness=appropriateness, goal=goal,
            new_units=sorted(new_units), score_change=self.score - before,
            evidence_turns=reviewed or [], confirmed_transcript_length=len(turns),
            known_context=known_context,
            fictional=True,
        )

    def cancel(self, proposal: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(proposal, dict):
            raise ValueError("Cancel an engine-prepared proposal.")
        saved = self._prepared.get(proposal.get("proposal_id", ""))
        if saved is None or proposal != saved[0]:
            raise ValueError("Unknown or altered proposal.")
        if proposal["proposal_id"] in self._executed_proposals:
            raise ValueError("This proposal was already executed; cancellation cannot undo an actual action.")
        self._prepared.pop(proposal["proposal_id"])
        return self._record(proposal["skill_id"], "cancelled", "Order cancelled; nothing performed.",
                            "Cancellation does not change the score.", proposal["parameters"])

    def summary(self) -> dict[str, Any]:
        config = self.scoring
        return {
            "score": self.score, "raw_score": self._raw_score, "patient_type": self.patient_type,
            "formula": f"score = clamp(sum(action.points), {config.minimum}, {config.maximum}); "
                       f"indicated={config.indicated}, alternative/baseline={config.alternative}, "
                       f"unnecessary={config.unnecessary}; per-goal positive budget={config.goal_budget}; "
                       "duplicate/unavailable/cancelled/insufficient-evidence=0; "
                       "linked burdens charged once; only evidence known at confirmation counts.",
            "config": vars(config).copy(), "actions": self.actions,
            "completed_skills": sorted(self._completed),
        }
