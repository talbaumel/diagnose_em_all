"""Hash-bound, all-roster listening signoff; candidates remain opt-in until reviewed."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_PATH = ROOT / "assets/audio/roster_review.json"
AUDITION_ENV = "DIAGNOSE_AUDITION_ROSTER_CUES"
REVIEW_PATIENTS = (
    "COMMON_COLD_KID", "STOMACHACHE_TEEN", "MIGRAINE_SUFFERER", "ALLERGIES_PATIENT",
    "SPRAINED_ANKLE_ATHLETE", "ANXIOUS_ADULT", "FEVERISH_PATIENT", "RASH_PATIENT",
    "ELDERLY_WITH_BACK_PAIN", "SLEEP_DEPRIVED_WORKER", "ECCENTRIC_NEIGHBOR",
)


def roster_review_complete(hashes: dict[str, str], path: Path = REVIEW_PATH) -> bool:
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if (not isinstance(review, dict) or review.get("schema_version") != 1
            or not isinstance(review.get("cues"), dict)
            or not isinstance(review.get("patients"), dict)):
        raise ValueError("Invalid roster audio review; preserve the file and correct its schema.")
    if not hashes or set(review["cues"]) != set(hashes):
        return False
    for cue_id, digest in hashes.items():
        item = review["cues"][cue_id]
        if not isinstance(item, dict):
            raise ValueError(f"Invalid review record for {cue_id}")
        if (item.get("sha256") != digest or item.get("natural_and_clean") is not True
                or item.get("character_fit") is not True
                or item.get("clinical_appropriate") is not True
                or not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip()):
            return False
    if set(review["patients"]) != set(REVIEW_PATIENTS):
        return False
    for item in review["patients"].values():
        if not isinstance(item, dict):
            raise ValueError("Invalid patient audio review record")
        if (item.get("live_playtest_passed") is not True
                or item.get("muted_playtest_passed") is not True
                or not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip()):
            return False
    return True


def roster_audio_enabled() -> tuple[bool, str]:
    audition = os.environ.get(AUDITION_ENV, "0")
    if audition not in ("0", "1"):
        raise ValueError(f"{AUDITION_ENV} must be 0 or 1")
    if audition == "1":
        return True, "Roster audio audition: unreviewed candidate effects; not release-approved."
    from src.cue_catalog import load_catalog
    hashes = {
        cue.id: cue.sha256 for cue in load_catalog().values()
        if cue.file.startswith("assets/audio/roster_candidates/")
    }
    if roster_review_complete(hashes):
        return True, ""
    return False, (
        "New character effects are awaiting full-roster listening review. "
        "Ordinary dialogue remains available; use the roster audio audition task to review candidates."
    )
