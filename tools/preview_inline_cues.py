"""Place unapproved cue candidates inside speech for listening review, not gameplay."""

from __future__ import annotations

import argparse
import hashlib
import json
import wave
from datetime import datetime, timezone
from pathlib import Path

from src.cue_timing import RATE, insert_cue, internal_pauses


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (
            1, 2, RATE, "NONE",
        ):
            raise ValueError(f"Expected mono 24 kHz PCM16 WAV: {path}")
        count = audio.getnframes()
        if not 0 < count <= RATE * 30:
            raise ValueError("Preview input must be 0-30 seconds")
        pcm = audio.readframes(count)
        if len(pcm) != count * 2:
            raise ValueError("Truncated preview input")
        return pcm


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_pcm(path: Path, pcm: bytes) -> dict:
    if not pcm or len(pcm) % 2:
        raise ValueError("Cannot save empty or malformed PCM")
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm)
    return {"file": path.name, "sha256": file_hash(path), "samples": len(pcm) // 2,
            "duration_seconds": len(pcm) / (2 * RATE)}


def build(root: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Use a new preview directory; refusing to overwrite {output}")
    source_manifest = root / "SOURCES.json"
    sources = json.loads(source_manifest.read_text())
    voice_path = (root / sources["voice_reference"]["prepared_file"]).resolve()
    if not voice_path.is_relative_to(root.resolve()):
        raise ValueError("Voice reference must stay within the candidate asset folder")
    speech = read_pcm(voice_path)
    pauses = internal_pauses(speech)
    if not pauses:
        raise ValueError("No safe internal pause found; refusing to append cues after the response")
    longest = max(pauses, key=lambda pause: pause.end - pause.start)
    results = []
    for cue in sources["cues"]:
        cue_path = (root / cue["file"]).resolve()
        if not cue_path.is_relative_to(root.resolve()) or file_hash(cue_path) != cue["sha256"]:
            raise ValueError("Candidate path or hash does not match the source manifest")
        # Sniffs use the first internal gap; longer actions get the longest gap.
        pause = pauses[0] if cue["kind"] == "sniffle" else pauses[-1] if cue["kind"] == "sigh" else longest
        result = insert_cue(speech, read_pcm(cue_path), pause)
        results.append((cue, result))
    if not results:
        raise ValueError("No cue candidates available")
    output.mkdir(parents=True)
    records = []
    playlist = bytearray()
    playlist_timeline = []
    for number, (cue, result) in enumerate(results, start=1):
        path = output / f"{cue['id']}_inline.wav"
        record = {
            "number": number, "cue": cue["id"], "kind": cue["kind"], "caption": cue["caption"],
            "cue_source_file": str((root / cue["file"]).resolve()), "cue_sha256": cue["sha256"],
            "cue_start_seconds": result.spans[1].output_start / RATE,
            "cue_end_seconds": result.spans[1].output_end / RATE,
            "speech_resumes": True, "approved_for_gameplay": False,
            "timing": result.metadata(), **save_pcm(path, result.pcm),
        }
        start = len(playlist) / (2 * RATE)
        playlist_timeline.append({"number": number, "cue": cue["id"], "start_seconds": start,
                                  "cue_start_seconds": start + record["cue_start_seconds"],
                                  "end_seconds": start + record["duration_seconds"]})
        playlist.extend(result.pcm)
        playlist.extend(bytes(RATE * 2))
        records.append(record)
    del playlist[-RATE * 2:]
    manifest = {
        "schema_version": 1, "status": "ready_for_listening", "approved_for_gameplay": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str(source_manifest.resolve()), "source_manifest_sha256": file_hash(source_manifest),
        "voice_file": str(voice_path.resolve()), "voice_sha256": file_hash(voice_path),
        "timing_code_sha256": file_hash(Path(__file__).resolve().parents[1] / "src/cue_timing.py"),
        "tool_sha256": file_hash(Path(__file__)),
        "detector": {"threshold_dbfs": -42, "minimum_pause_ms": 180, "margin_ms": 50},
        "detected_internal_pauses": [{"start_seconds": pause.start / RATE, "end_seconds": pause.end / RATE}
                                     for pause in pauses],
        "clips": records, "playlist": {**save_pcm(output / "listen_inline.wav", bytes(playlist)),
                                      "timeline": playlist_timeline},
        "limitations": [
            "Pause detection is acoustic only, not word or sentence alignment. Quiet consonants remain a risk.",
            "Only detected internal quiet samples are replaced; speech before/after is bit-exact.",
            "Each preview intentionally contains one cue to compare timing; this is not a gameplay frequency.",
            "No source audio is mixed over speech and no symptom clip is passed through the voice effect.",
            "New candidate sounds and this timing path are not enabled in the runtime yet.",
            "Live integration must consume the timing map for interruption/caption timing and share the cue budget.",
            "Clip and dataset licenses remain in the parent pack; generated GPT voice is not CC0.",
        ],
    }
    (output / "review.json").write_text(json.dumps({
        "instructions": "Compare internal placement with the older after-speech preview. "
                        "Judge whether the cue feels like a natural interruption and speech resumes cleanly. "
                        "Flag any cut syllables, unnatural pauses or mismatched voice. No approval is assumed.",
        "reviewer": "",
        "items": [{"number": r["number"], "cue": r["cue"], "natural_placement": None,
                   "speech_cut_or_masked": None, "matches_character": None,
                   "approved_for_gameplay": False, "notes": ""} for r in records],
    }, indent=2) + "\n")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("assets/audio/cue_candidates"))
    parser.add_argument("--output", type=Path, default=Path("assets/audio/cue_candidates/inline_audition"))
    args = parser.parse_args()
    result = build(args.root, args.output)
    print(f"Prepared {len(result['clips'])} within-speech previews in {args.output}")
    print("Start with sniffle_02_inline.wav. Cue candidates and inline runtime timing remain disabled.")


if __name__ == "__main__":
    main()
