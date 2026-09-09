"""Prepare a local, explicitly unapproved cue audition pack. No network or gameplay changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np
from numpy.typing import NDArray

from src.voice_processing import RATE, dbfs, pcm16, rms

CC0 = "http://creativecommons.org/publicdomain/zero/1.0/"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_wave(path: Path) -> tuple[NDArray[np.float64], int]:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        if (source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (1, 2, "NONE"):
            raise ValueError(f"Expected mono PCM16 WAV: {path}")
        count = source.getnframes()
        if rate not in (24000, 44100) or not 0 < count <= rate * 120:
            raise ValueError(f"Unexpected sample rate or duration: {path}")
        pcm = source.readframes(count)
        if len(pcm) != count * 2:
            raise ValueError(f"Truncated WAV: {path}")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0, rate


def metrics(signal: NDArray[np.float64], rate: int) -> dict:
    return {
        "samples": len(signal), "sample_rate": rate, "duration_seconds": len(signal) / rate,
        "peak_dbfs": dbfs(float(np.max(np.abs(signal)))), "rms_dbfs": dbfs(rms(signal)),
        "full_scale_samples": int(np.count_nonzero(np.abs(signal) >= 32767 / 32768)),
    }


def active_rms(signal: NDArray[np.float64]) -> float:
    if not len(signal) or not np.all(np.isfinite(signal)):
        raise ValueError("Cue must contain finite audio")
    block = RATE // 50
    padded = np.pad(signal, (0, (-len(signal)) % block))
    levels = np.sqrt(np.mean(padded.reshape(-1, block) ** 2, axis=1))
    if float(levels.max()) < 1e-5:
        raise ValueError("Cue is too quiet to prepare reliably")
    selected = np.repeat(levels >= float(levels.max()) * 0.1, block)[:len(signal)]
    return rms(signal[selected])


def prepare_levels(signal: NDArray[np.float64], target: float) -> tuple[NDArray[np.float64], dict]:
    if not math.isfinite(target) or not -40 <= target <= -20:
        raise ValueError("Cue target RMS must be between -40 and -20 dBFS")
    signal = signal.copy()
    fade = min(int(RATE * .01), len(signal) // 2)
    if fade < 2:
        raise ValueError("Cue is too short to fade safely")
    signal[:fade] *= np.linspace(0, 1, fade)
    signal[-fade:] *= np.linspace(1, 0, fade)
    level = active_rms(signal)
    peak = float(np.max(np.abs(signal)))
    gain = min(10 ** (target / 20) / level, 10 ** (12 / 20), 10 ** (-3 / 20) / peak)
    output = signal * gain
    return output, {
        "gain_db": dbfs(gain), "target_active_rms_dbfs": target,
        "measured_active_rms_dbfs": dbfs(active_rms(output)),
        "peak_ceiling_dbfs": -3, "maximum_gain_db": 12, "edge_fades_ms": 10,
        "loudness_method": "20 ms source-relative active-frame RMS; not perceptual LUFS",
    }


def save_wave(path: Path, signal: NDArray[np.float64]) -> dict:
    pcm = pcm16(signal)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm)
    saved = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    return {"sha256": digest(path), **metrics(saved, RATE)}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def build(root: Path, metadata_zip: Path, voice: Path) -> dict:
    plan = json.loads((root / "PREPARATION.json").read_text())
    for path in (root / "prepared", root / "audition", root / "SOURCES.json"):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing candidate output: {path}")
    with zipfile.ZipFile(metadata_zip) as archive:
        all_info = json.loads(archive.read("FSD50K.metadata/dev_clips_info_FSD50K.json"))
    sources = {}
    for source_id in plan["source_ids"]:
        if not re.fullmatch(r"\d+", source_id):
            raise ValueError("Source IDs must be numeric")
        info = all_info[source_id]
        if info["license"] != CC0:
            raise ValueError(f"Source {source_id} is not explicitly CC0 in the official metadata")
        path = root / "originals" / f"{source_id}.wav"
        original, rate = load_wave(path)
        source_metrics = metrics(original, rate)
        rejected = plan["rejected_sources"].get(source_id)
        if source_metrics["full_scale_samples"] and not rejected:
            raise ValueError(f"Source {source_id} reaches full scale; reject or review it before preparation")
        sources[source_id] = {
            "file": f"originals/{source_id}.wav", "sha256": digest(path), **source_metrics,
            "title": info["title"], "creator": info["uploader"],
            "creator_description": info["description"], "tags": info["tags"],
            "clip_license": "CC0-1.0", "license_url": CC0,
            "source_page": f"https://freesound.org/people/{quote(info['uploader'])}/sounds/{source_id}/",
            "download_url": f"{plan['mirror']}/resolve/{plan['mirror_revision']}/clips/dev/{source_id}.wav",
            "download_kind": "Untouched FSD50K distributed WAV, not the creator's original upload or a preview",
            "technical_status": "rejected" if rejected else "candidate",
            "rejection_reason": rejected, "approved_for_gameplay": False,
            "privacy_consent_and_character_fit": "not_reviewed",
        }
    identifiers = set()
    for cue in plan["cues"]:
        if not re.fullmatch(r"[a-z]+(?:_[a-z0-9]+)*", cue["id"]) or cue["id"] in identifiers:
            raise ValueError("Cue IDs must be unique safe filenames")
        identifiers.add(cue["id"])
        if cue["kind"] not in ("sniffle", "sneeze", "throat_clear", "sigh"):
            raise ValueError("Unknown cue kind")
        source = sources[cue["source_id"]]
        if source["technical_status"] == "rejected":
            raise ValueError("A rejected source cannot be used in a prepared cue")
        start, end = cue["start_seconds"], cue["end_seconds"]
        if not (math.isfinite(start) and math.isfinite(end)
                and 0 <= start < end <= source["duration_seconds"] and .1 <= end-start <= 10):
            raise ValueError("Cue trim must be 0.1-10 seconds inside its source")
    reference, rate = load_wave(voice)
    if rate != RATE:
        raise ValueError("Reference voice must be 24 kHz")
    (root / "prepared").mkdir()
    (root / "audition").mkdir()
    ffmpeg_version = subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
    cues = []
    for cue in plan["cues"]:
        source_path = root / sources[cue["source_id"]]["file"]
        filter_text = f"atrim=start={cue['start_seconds']}:end={cue['end_seconds']},asetpts=PTS-STARTPTS"
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source_path),
                   "-af", filter_text, "-ac", "1", "-ar", str(RATE), "-f", "f64le", "pipe:1"]
        decoded = subprocess.run(command, check=True, capture_output=True).stdout
        signal = np.frombuffer(decoded, dtype="<f8").astype(np.float64)
        expected = round((cue["end_seconds"] - cue["start_seconds"]) * RATE)
        if abs(len(signal) - expected) > 2:
            raise ValueError("Unexpected resampling duration")
        prepared, levels = prepare_levels(signal, cue["target_active_rms_dbfs"])
        filename = f"prepared/{cue['id']}.wav"
        record = {
            **cue, "file": filename, **levels, **save_wave(root / filename, prepared),
            "processing_command": command, "approved_for_gameplay": False,
            "review_status": "unreviewed", "selection_method": plan["selection_method"],
        }
        cues.append(record)

    solo_parts = []
    context_parts = []
    solo_timeline = []
    context_timeline = []
    solo_frames = context_frames = 0
    for index, cue in enumerate(cues, start=1):
        signal, _ = load_wave(root / cue["file"])
        solo_timeline.append({"number": index, "cue": cue["id"], "start_seconds": solo_frames / RATE,
                              "end_seconds": (solo_frames + len(signal)) / RATE})
        context_timeline.append({
            "number": index, "cue": cue["id"], "voice_start_seconds": context_frames / RATE,
            "cue_start_seconds": (context_frames + len(reference) + RATE // 4) / RATE,
            "end_seconds": (context_frames + len(reference) + RATE // 4 + len(signal)) / RATE,
        })
        solo_parts.extend((signal, np.zeros(RATE)))
        context_parts.extend((reference, np.zeros(RATE // 4), signal, np.zeros(RATE)))
        solo_frames += len(signal) + RATE
        context_frames += len(reference) + RATE // 4 + len(signal) + RATE
    solo = save_wave(root / "audition/cues_only.wav", np.concatenate(solo_parts[:-1]))
    context = save_wave(root / "audition/voice_then_cues.wav", np.concatenate(context_parts[:-1]))
    save_wave(root / "audition/voice_reference.wav", reference)
    manifest = {
        "schema_version": 1, "status": "prepared_for_review", "approved_for_gameplay": False,
        "retrieved_and_prepared_at": datetime.now(timezone.utc).isoformat(),
        "dataset": plan["dataset"], "dataset_url": plan["dataset_url"],
        "dataset_license": "CC-BY-4.0", "dataset_license_url": "https://creativecommons.org/licenses/by/4.0/",
        "dataset_attribution": "FSD50K by Eduardo Fonseca, Xavier Favory, Jordi Pons, Frederic Font and Xavier Serra, "
                               "Music Technology Group, Universitat Pompeu Fabra. See LICENSE-DATASET.txt. "
                               "Clip creators are credited per source. No endorsement implied.",
        "citation": "Fonseca et al., FSD50K: An Open Dataset of Human-Labeled Sound Events, arXiv:2010.00475.",
        "metadata_url": plan["metadata_url"], "metadata_archive_sha256": digest(metadata_zip),
        "mirror": plan["mirror"], "mirror_revision": plan["mirror_revision"],
        "preparation_plan_sha256": digest(root / "PREPARATION.json"),
        "tool_sha256": digest(Path(__file__)), "ffmpeg_version": ffmpeg_version,
        "sources": sources, "cues": cues,
        "voice_reference": {"source": str(voice.resolve()), "source_sha256": digest(voice),
                            "prepared_file": "audition/voice_reference.wav",
                            "rights": "User-authorized generated GPT game voice; not CC0 or FSD50K audio",
                            "processing": "PCM16 re-export only; no new voice conversion"},
        "auditions": {
            "cues_only": {"file": "audition/cues_only.wav", "timeline": solo_timeline, **solo},
            "with_voice": {"file": "audition/voice_then_cues.wav", "timeline": context_timeline, **context},
        },
        "cautions": [
            "Source descriptions and labels are not verified ages, diagnoses or permission to clone a voice.",
            "CC0 covers copyright, not all privacy, publicity or consent questions.",
            "Sources use multiple contributors; the sniff/throat-clear/sigh subset shares an uploader, not a verified performer.",
            "No human listening, incidental-speech screening, clinical review or child-character fit is claimed.",
            "Rejected originals are retained for provenance and never used in audition playlists.",
            "This pack is outside the runtime allowlist. Approval and shared-rate-limit integration are separate steps.",
            "The voice-context preview uses deliberate after-speech boundaries, not a promise of final cue timing.",
        ],
    }
    write_json(root / "audition/review.json", {
        "reviewer": "", "instructions": "Use the numbered order in SOURCES.json. Choose compatible, "
                                      "natural cues; flag speech, music, distortion, mismatched age/timbre, "
                                      "excessive loudness or overly dramatic delivery. No automatic approval.",
        "items": [{"number": index, "id": cue["id"], "matches_kid": None, "natural": None,
                   "incidental_speech_or_music": None, "level_appropriate": None, "notes": "",
                   "approved_for_gameplay": False} for index, cue in enumerate(cues, start=1)],
    })
    write_json(root / "SOURCES.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/audio/cue_candidates"))
    parser.add_argument("--metadata-zip", type=Path, required=True)
    parser.add_argument("--voice", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.root, args.metadata_zip, args.voice)
    print(f"Prepared {len(result['cues'])} candidate cues in {args.root}; NOT approved or enabled.")


if __name__ == "__main__":
    main()
