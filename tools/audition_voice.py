"""Offline WORLD comparisons; imports the same processing engine used by the game."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import shutil
import sys
import wave
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from src import voice_processing
from src.voice_processing import (
    Analysis, FloatArray, FRAME_PERIOD_MS, MAX_SECONDS, PitchMethod, Preset, RATE,
    active_mask, analyze, dbfs, match_levels, pcm16, resynthesize, rms,
)


PRESETS = (
    Preset("world_control", 0.0),
    Preset("breathiness_light", 0.04),
    Preset("breathiness_moderate", 0.12),
)

CALIBRATION_PRESETS = (
    PRESETS[0],
    PRESETS[2],
    Preset("broad_015", 0.15, "broad"),
    Preset("broad_040", 0.40, "broad"),
    Preset("broad_080_exaggerated", 0.80, "broad"),
)

REFINEMENT_PRESETS = (
    PRESETS[0],
    CALIBRATION_PRESETS[-1],
    Preset("broad_080_soft_edges", 0.80, "broad", edge_ms=20.0),
    Preset("harvest_control", 0.0, pitch_method="harvest"),
    Preset("broad_080_harvest", 0.80, "broad", pitch_method="harvest"),
    Preset("broad_080_harvest_soft_edges", 0.80, "broad", pitch_method="harvest", edge_ms=20.0),
)


def read_speech(path: Path) -> FloatArray:
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth(),
                source.getcomptype()) != (RATE, 1, 2, "NONE"):
            raise ValueError("Input must be mono 24 kHz PCM16 WAV; resample explicitly first")
        frames = source.getnframes()
        if not RATE // 2 <= frames <= RATE * MAX_SECONDS:
            raise ValueError(f"Input must be between 0.5 and {MAX_SECONDS} seconds")
        pcm = source.readframes(frames)
        if len(pcm) != frames * 2:
            raise ValueError("Input WAV is truncated")
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    if rms(audio) < 1e-4:
        raise ValueError("Input is silent or too quiet to analyze reliably")
    return audio


def summarize_analysis(analysis: Analysis) -> dict:
    voiced = analysis.f0 > 0
    spectrum = analysis.spectral_envelope[voiced]
    energy = float(spectrum.sum())
    if energy <= 0:
        raise ValueError("Voiced spectral envelope has no energy")
    frequencies = np.linspace(0, RATE / 2, spectrum.shape[1])
    adjacent = voiced[1:] & voiced[:-1]
    jumps = np.abs(12 * np.log2(analysis.f0[1:][adjacent] / analysis.f0[:-1][adjacent]))
    return {
        "pitch_method": analysis.pitch_method, "frame_period_ms": FRAME_PERIOD_MS,
        "f0_floor": 65, "f0_ceil": 600,
        "voiced_frame_fraction": float(np.mean(voiced)),
        "voicing_transitions": int(np.count_nonzero(np.diff(voiced))),
        "adjacent_pitch_jumps_over_6_semitones": int(np.count_nonzero(jumps > 6)),
        "voiced_envelope_energy_fraction_below_1khz":
            float(spectrum[:, frequencies < 1000].sum()) / energy,
    }


def difference_metrics(signal: FloatArray, control: FloatArray, mask: NDArray[np.bool_]) -> dict:
    if (len(signal) != len(control) or len(signal) != len(mask) or not np.any(mask)
            or not np.all(np.isfinite(signal)) or not np.all(np.isfinite(control))):
        raise ValueError("Difference measurement needs finite, equal-length signals and active frames")
    level = rms(control[mask])
    if level <= 1e-8:
        raise ValueError("Difference reference is silent")
    relative = rms((signal - control)[mask]) / level
    return {
        "active_residual_rms_ratio": relative,
        "active_residual_db_relative_to_control": dbfs(relative),
        "note": "Same-timeline, level-matched waveform difference, not a perceptual or medical score. "
                "Null dB with zero ratio means identical samples.",
    }


def write_wav(path: Path, audio: FloatArray) -> dict:
    pcm = pcm16(audio)
    with wave.open(str(path), "wb") as destination:
        destination.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        destination.writeframes(pcm)
    saved = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "samples": len(saved),
        "duration_seconds": len(saved) / RATE,
        "peak_dbfs": dbfs(float(np.max(np.abs(saved)))),
        "rms_dbfs": dbfs(rms(saved)),
        "clipped_samples": int(np.count_nonzero(np.abs(saved) >= 32767 / 32768)),
        "max_adjacent_sample_step": float(np.max(np.abs(np.diff(saved)))),
    }


def write_json(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audition(
    source: Path, output: Path, *, transcript: str, source_kind: str,
    source_note: str, rights_note: str, seed: int = 42, repeats: int = 3,
    calibration: bool = False,
    refinement: bool = False,
) -> dict:
    if not all(value.strip() for value in (transcript, source_kind, source_note, rights_note)):
        raise ValueError("Transcript, source kind, source note and rights note are required")
    if not 1 <= repeats <= 10:
        raise ValueError("Benchmark repeats must be between 1 and 10")
    if calibration and refinement:
        raise ValueError("Choose calibration or refinement, not both")
    if output.exists():
        raise FileExistsError(f"Use a new output directory; refusing to overwrite {output}")
    source_hash = file_hash(source)
    speech = read_speech(source)
    mask = active_mask(speech)
    signals = [speech]
    records: list[dict] = [{"condition": "original_level_matched", "parameters": {}}]
    presets = REFINEMENT_PRESETS if refinement else CALIBRATION_PRESETS if calibration else PRESETS
    mode = "naturalness_refinement" if refinement else "audibility_calibration" if calibration else "blind_comparison"
    trials: dict[str, list[float]] = {preset.name: [] for preset in presets}
    analysis_summaries: dict[str, dict] = {}
    pitch_methods: dict[PitchMethod, None] = {preset.pitch_method: None for preset in presets}
    for trial in range(repeats):
        analyses = {
            method: analyze(speech, pitch_method=method)
            for method in pitch_methods
        }
        if trial == 0:
            analysis_summaries = {method: summarize_analysis(value) for method, value in analyses.items()}
        for preset in presets:
            analysis = analyses[preset.pitch_method]
            rendered, timing = resynthesize(analysis, preset)
            trials[preset.name].append(timing["total_processor_ms"])
            if trial == 0:
                signals.append(rendered)
                records.append({
                    "condition": preset.name, "parameters": asdict(preset),
                    "analysis_ms": analysis.elapsed_ms, **timing,
                })
    matched, gains = match_levels(signals, mask)
    if file_hash(source) != source_hash:
        raise ValueError("Source changed during processing; retry with an immutable input")

    output.mkdir(parents=True, exist_ok=False)
    # The final manifest is the completion marker; partial folders are never valid packs.
    shutil.copyfile(source, output / "source_original.wav")
    if file_hash(output / "source_original.wav") != source_hash:
        raise ValueError("Source changed while preserving it; this incomplete pack must not be used")
    order = list(range(len(records)))
    if not (calibration or refinement):
        random.Random(seed).shuffle(order)
    review_items = []
    playlist: list[FloatArray] = []
    timeline = []
    position_seconds = 0.0
    saved_signals = [
        np.frombuffer(pcm16(signal), dtype="<i2").astype(np.float64) / 32768.0 for signal in matched
    ]
    control_indices = {preset.pitch_method: index for index, preset in enumerate(presets, start=1)
                       if preset.aperiodicity_strength == 0}
    listening_indices = {2, 3, 5, 6} if refinement else set(order)
    for position, index in enumerate(order, start=1):
        label = f"sample_{position:02d}"
        filename = f"{label}.wav"
        measurements = write_wav(output / filename, matched[index])
        saved = saved_signals[index]
        control_index = control_indices[presets[index - 1].pitch_method] if index else 1
        records[index].update({
            "label": label, "file": filename, "gain_db": dbfs(gains[index]),
            "active_rms_dbfs": dbfs(rms(saved[mask])), **measurements,
            "difference_vs_world_control": difference_metrics(
                saved, saved_signals[control_index], mask,
            ),
            "world_control_condition": records[control_index]["condition"],
        })
        if refinement:
            records[index]["difference_vs_preferred"] = difference_metrics(saved, saved_signals[2], mask)
        review_items.append({
            "label": label, "file": filename, "naturalness_1_to_5": None,
            "same_speaker_1_to_5": None, "breathiness_1_to_5": None,
            "roughness_1_to_5": None, "blocked_nose_quality_1_to_5": None,
            "cold_case_plausibility_1_to_5": None, "word_errors": None,
            "clicks_or_other_artifacts": None, "notes": "",
        })
        if calibration or refinement:
            review_items[-1].update({
                "condition": records[index]["condition"],
                "audible_difference_vs_world_control": None,
                "acceptable_for_further_voice_testing": None,
            })
        if refinement:
            review_items[-1].update({"less_robotic_than_preferred": None, "retains_cold_like_quality": None})
        if index in listening_indices:
            timeline.append({
                "label": label, "file": filename, "condition": records[index]["condition"],
                "start_seconds": position_seconds, "end_seconds": position_seconds + len(saved) / RATE,
            })
            position_seconds += len(saved) / RATE + 1
            playlist.extend((matched[index], np.zeros(RATE, dtype=np.float64)))
    playlist_name = ("listen_refinement.wav" if refinement else
                     "listen_calibration.wav" if calibration else "listen_blind.wav")
    playlist_metrics = write_wav(output / playlist_name, np.concatenate(playlist[:-1]))
    short_comparison = None
    if calibration or refinement:
        short_name = "preferred_then_combined.wav" if refinement else "control_then_exaggerated.wav"
        reference_index = 2 if refinement else 1
        short_comparison = {
            "file": short_name,
            "order": [records[reference_index]["condition"], presets[-1].name],
            "gap_seconds": 1,
            **write_wav(output / short_name,
                        np.concatenate((matched[reference_index], np.zeros(RATE), matched[-1]))),
        }
    instructions = (
        "Calibration is labeled and ordered, not blinded: original, WORLD control, previous high-band "
        "setting, then broad strengths 0.15, 0.40 and 0.80. Start with control_then_exaggerated.wav "
        "(control first, exaggerated second). Judge whether a difference is audible before judging "
        "acceptability. The strongest sample is intentionally exaggerated, not a symptom preset."
        if calibration else
        "Listen before opening manifest.json (the answer key)."
    )
    if refinement:
        instructions = (
            "Labeled refinement, not blinded. listen_refinement.wav order: preferred old sample 6 "
            "(sample_03), 20 ms effect-edge taper (sample_04), Harvest pitch (sample_06), "
            "Harvest plus taper (sample_07). All keep peak strength 0.80. "
            "preferred_then_combined.wav contains preferred first, combined candidate second. "
            "Also available: original (sample_01), DIO control (sample_02), Harvest control (sample_05). "
            "Judge less robotic AND retained cold-like quality; neither improvement is assumed. "
            "Harvest changes estimated pitch/voicing; taper reduces the effect on short voiced sounds."
        )
    write_json(output / "review.json", {
        "instructions": instructions + " "
                        "1=low/poor, 5=high/excellent; symptom ratings measure perceived amount, "
                        "not medical severity. Compare identity against source_original.wav. "
                        "Null means not yet reviewed. No condition is a validated illness.",
        "expected_transcript": transcript,
        "reviewer": "", "reviewer_role": "", "items": review_items,
    })
    manifest = {
        "schema_version": 1, "status": "complete", "medical_validation": "not_evaluated",
        "created_at": datetime.now(timezone.utc).isoformat(), "seed": seed,
        "mode": mode,
        "source": {
            "path": str(source.resolve()), "sha256": source_hash,
            "preserved_file": "source_original.wav", "kind": source_kind,
            "description": source_note, "rights": rights_note, "transcript": transcript,
            "clipped_samples": int(np.count_nonzero(np.abs(speech) >= 32767 / 32768)),
            "sample_rate": RATE, "samples": len(speech),
        },
        "environment": {
            "python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
            "numpy": version("numpy"), "pyworld": version("pyworld"),
            "setuptools": version("setuptools"),
            "processor_code_sha256": file_hash(Path(voice_processing.__file__)),
            "audition_code_sha256": file_hash(Path(__file__)),
        },
        "analysis": analysis_summaries["dio"],
        "analysis_by_pitch_method": analysis_summaries,
        "aperiodicity_method": {
            "formula": "ap += strength * frequency_weight * (1 - ap) * voiced_edge_weight",
            "high_band": "Weight ramps from zero at 1000 Hz to one at 4000 Hz",
            "broad_band": "Weight ramps from zero at DC to one at 500 Hz; offline experiments only",
            "unchanged_parameters": ["f0", "spectral_envelope", "unvoiced_aperiodicity"],
            "pitch_note": "Parameters are preserved relative to each selected analysis, not across pitch methods.",
            "edge_taper": "When edge_ms > 0, raised-cosine effect fade inside each voiced run, "
                          "zero at its first/last frames, full strength edge_ms inward. No speech gain fade.",
        },
        "level_matching": {
            "method": "Source-derived 20 ms active-frame RMS; not perceptual LUFS",
            "target_dbfs": -24, "sample_peak_ceiling_dbfs": -1,
            "shared_headroom_reduction": True,
        },
        "benchmark": {
            "note": "Full utterance, analysis + modification + synthesis; includes first run. "
                    "Not streaming latency or a statistically established p95 SLA.",
            "repeats": repeats,
            "conditions": {
                name: {"trials_ms": timings,
                       "observed_p95_ms": float(np.percentile(timings, 95)),
                       "median_real_time_factor": float(np.median(timings)) / (len(speech) / RATE * 1000)}
                for name, timings in trials.items()
            },
        },
        "limitations": [
            "Voiced aperiodicity probes noise-like source quality; not proven hoarseness.",
            "Broad calibration is not a set of medical severity presets; the strongest is exaggerated.",
            "No congestion transform, phoneme alignment, clinical review or neural conversion yet.",
            "No coughs or symptom clips mixed into these comparisons.",
            "Original comparison is gain-adjusted; source_original.wav is byte-for-byte untouched.",
            "Peak/RMS and adjacent-sample measurements cannot prove absence of audible artifacts.",
            "Native WORLD duration is trimmed/padded by at most one frame; no streaming time map.",
            "Harvest may change pitch/voicing and costs more analysis time; fewer transitions do not prove correctness.",
            "Soft edges taper the added effect on brief voiced sounds, not the speech amplitude.",
            "Naturalness and retention of the preferred symptom quality require listening; no automatic winner.",
        ],
        "conditions": records,
        "refinement_playlist" if refinement else "calibration_playlist" if calibration else "blind_playlist": {
            "file": playlist_name, "timeline": timeline, **playlist_metrics,
        },
    }
    if short_comparison is not None:
        manifest["short_comparison"] = short_comparison
    write_json(output / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory; never overwritten")
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--source-kind", choices=("azure-realtime", "consented-recording", "local-synthetic"),
                        required=True)
    parser.add_argument("--source-note", required=True, help="Voice/model/take or recording provenance")
    parser.add_argument("--rights-note", required=True, help="Permission/license and allowed use")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repeats", type=int, default=3)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--calibration", action="store_true",
                       help="Ordered six-way calibration with broader voiced effects; not illness presets")
    modes.add_argument("--refinement", action="store_true",
                       help="Compare preferred 0.80 effect with soft transitions and Harvest pitch; offline only")
    args = parser.parse_args()
    manifest = audition(
        args.input, args.output, transcript=args.transcript, source_kind=args.source_kind,
        source_note=args.source_note, rights_note=args.rights_note,
        seed=args.seed, repeats=args.repeats, calibration=args.calibration, refinement=args.refinement,
    )
    print(f"Saved {len(manifest['conditions'])} comparisons to {args.output}")
    if args.refinement:
        print("Start with preferred_then_combined.wav; preferred first, combined candidate second.")
        print("Four-way comparison: listen_refinement.wav. Improvements require listening review.")
    elif args.calibration:
        print("Start with control_then_exaggerated.wav; full ordered sweep: listen_calibration.wav.")
        print("Calibration only: strongest sample is deliberately exaggerated, not a symptom preset.")
    else:
        print("Listen to listen_blind.wav, fill review.json, then open manifest.json for the answer key.")
    print("Congestion, medical realism, audible differences and identity preservation are unverified.")


if __name__ == "__main__":
    main()
