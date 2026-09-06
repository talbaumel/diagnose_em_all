"""WORLD analysis and synthesis shared by the game worker and offline auditions."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Literal, TypeAlias

import numpy as np
import pyworld
from numpy.typing import NDArray

RATE = 24_000
FRAME_PERIOD_MS = 5.0
MAX_SECONDS = 30
FloatArray: TypeAlias = NDArray[np.float64]
PitchMethod = Literal["dio", "harvest"]


class InsufficientVoicingError(ValueError):
    pass


@dataclass(frozen=True)
class Preset:
    name: str
    aperiodicity_strength: float
    band: Literal["high", "broad"] = "high"
    pitch_method: PitchMethod = "dio"
    edge_ms: float = 0.0


@dataclass(frozen=True)
class Analysis:
    f0: FloatArray
    spectral_envelope: FloatArray
    aperiodicity: FloatArray
    sample_count: int
    elapsed_ms: float
    pitch_method: PitchMethod = "dio"


def rms(audio: FloatArray) -> float:
    return float(np.sqrt(np.mean(np.square(audio))))


def dbfs(value: float) -> float | None:
    return 20.0 * math.log10(value) if value > 0 else None


def active_mask(audio: FloatArray) -> NDArray[np.bool_]:
    block = RATE // 50
    padded = np.pad(audio, (0, (-len(audio)) % block))
    energy = np.sqrt(np.mean(padded.reshape(-1, block) ** 2, axis=1))
    threshold = max(10 ** (-45 / 20), float(energy.max()) * 0.05)
    mask = np.repeat(energy >= threshold, block)[:len(audio)]
    if not np.any(mask):
        raise ValueError("No active speech-level frames found")
    return mask


def analyze(audio: FloatArray, *, pitch_method: PitchMethod = "dio") -> Analysis:
    started = time.perf_counter()
    if pitch_method == "dio":
        initial, positions = pyworld.dio(
            audio, RATE, f0_floor=65.0, f0_ceil=600.0, frame_period=FRAME_PERIOD_MS,
        )
        f0 = pyworld.stonemask(audio, initial, positions, RATE)
    elif pitch_method == "harvest":
        f0, positions = pyworld.harvest(
            audio, RATE, f0_floor=65.0, f0_ceil=600.0, frame_period=FRAME_PERIOD_MS,
        )
    else:
        raise ValueError("Pitch method must be dio or harvest")
    spectral = pyworld.cheaptrick(audio, f0, positions, RATE)
    aperiodicity = pyworld.d4c(audio, f0, positions, RATE)
    if not all(np.all(np.isfinite(value)) for value in (f0, spectral, aperiodicity)):
        raise ValueError("WORLD analysis produced non-finite parameters")
    if np.count_nonzero(f0 > 0) < 5:
        raise InsufficientVoicingError("Too few voiced frames for a voice-quality comparison")
    return Analysis(f0, spectral, aperiodicity, len(audio),
                    (time.perf_counter() - started) * 1000, pitch_method)


def voiced_edge_weights(f0: FloatArray, edge_ms: float) -> FloatArray:
    if not math.isfinite(edge_ms) or not 0 <= edge_ms <= 30:
        raise ValueError("Voiced edge taper must be finite and between 0 and 30 ms")
    voiced = f0 > 0
    weights = voiced.astype(np.float64)
    if edge_ms == 0:
        return weights
    boundaries = np.diff(np.pad(voiced.astype(np.int8), (1, 1)))
    for start, stop in zip(np.flatnonzero(boundaries == 1), np.flatnonzero(boundaries == -1)):
        distance = np.minimum(np.arange(stop - start), np.arange(stop - start)[::-1])
        phase = np.minimum(distance * FRAME_PERIOD_MS / edge_ms, 1.0)
        # Taper the added effect, not speech amplitude; never carry noise into unvoiced frames.
        weights[start:stop] = 0.5 - 0.5 * np.cos(np.pi * phase)
    return weights


def modified_aperiodicity(
    analysis: Analysis, strength: float, *, band: Literal["high", "broad"] = "high",
    edge_ms: float = 0.0,
) -> FloatArray:
    if band not in ("high", "broad"):
        raise ValueError("Aperiodicity band must be high or broad")
    maximum = 0.2 if band == "high" else 0.8
    if not math.isfinite(strength) or not 0 <= strength <= maximum:
        raise ValueError(f"Aperiodicity strength must be finite and between 0 and {maximum}")
    ap = analysis.aperiodicity.copy()
    voiced = analysis.f0 > 0
    frequencies = np.linspace(0, RATE / 2, ap.shape[1])
    # Broad processing reaches low harmonics; DC and unvoiced frames stay untouched.
    weighting = (np.clip((frequencies - 1000) / 3000, 0, 1) if band == "high"
                 else np.clip(frequencies / 500, 0, 1))
    envelope = voiced_edge_weights(analysis.f0, edge_ms)
    ap[voiced] += strength * weighting * (1 - ap[voiced]) * envelope[voiced, None]
    return np.ascontiguousarray(ap)


def resynthesize(analysis: Analysis, preset: Preset) -> tuple[FloatArray, dict]:
    started = time.perf_counter()
    if preset.pitch_method != analysis.pitch_method:
        raise ValueError("Preset pitch method does not match its analysis")
    ap = modified_aperiodicity(analysis, preset.aperiodicity_strength,
                              band=preset.band, edge_ms=preset.edge_ms)
    output = pyworld.synthesize(
        analysis.f0, analysis.spectral_envelope, ap, RATE, FRAME_PERIOD_MS,
    )
    native_samples = len(output)
    # WORLD rounds to a frame boundary. Never hide a larger duration discrepancy.
    if abs(native_samples - analysis.sample_count) > RATE * FRAME_PERIOD_MS / 1000 + 1:
        raise ValueError("Unexpected WORLD duration change")
    output = np.pad(output, (0, max(0, analysis.sample_count - native_samples)))
    output = output[:analysis.sample_count].copy()
    if not np.all(np.isfinite(output)) or rms(output) < 1e-8:
        raise ValueError("Resynthesis produced silent or non-finite audio")
    processing_ms = (time.perf_counter() - started) * 1000
    return output, {
        "transform_and_synthesis_ms": processing_ms,
        "total_processor_ms": analysis.elapsed_ms + processing_ms,
        "native_output_samples": native_samples,
        "trimmed_samples": max(0, native_samples - analysis.sample_count),
        "padded_samples": max(0, analysis.sample_count - native_samples),
    }


def match_levels(
    signals: list[FloatArray], mask: NDArray[np.bool_],
) -> tuple[list[FloatArray], list[float]]:
    if not signals or not np.any(mask):
        raise ValueError("Level matching requires signals and active reference frames")
    levels = []
    for signal in signals:
        if len(signal) != len(mask) or not np.all(np.isfinite(signal)):
            raise ValueError("Comparison signals must be finite and have the same duration")
        level = rms(signal[mask])
        if level <= 1e-8:
            raise ValueError("Comparison signal is silent")
        levels.append(level)
    target = 10 ** (-24 / 20)
    gains = [target / level for level in levels]
    largest_peak = max(float(np.max(np.abs(signal))) * gain
                       for signal, gain in zip(signals, gains))
    # Lower every comparison equally if one would violate -1 dBFS sample-peak headroom.
    headroom = min(1.0, 10 ** (-1 / 20) / largest_peak)
    gains = [gain * headroom for gain in gains]
    return [signal * gain for signal, gain in zip(signals, gains)], gains


def pcm16(audio: FloatArray) -> bytes:
    if not np.all(np.isfinite(audio)) or np.max(np.abs(audio)) >= 1:
        raise ValueError("Cannot export non-finite or clipping audio")
    return np.rint(audio * 32767).astype("<i2").tobytes()
