"""Bounded voice processing with optional length-prefixed worker messages."""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time

import numpy as np

from src.voice_processing import (
    InsufficientVoicingError, Preset, RATE, active_mask, analyze, match_levels,
    pcm16, resynthesize, rms,
)
from src.voice_profile import VoiceProfile

MAX_PCM_BYTES = RATE * 2 * 30


def process_pcm(pcm: bytes, profile: VoiceProfile) -> tuple[bytes, dict]:
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_PCM_BYTES:
        raise ValueError("Voice input must be nonempty PCM16, at most 30 seconds")
    if not profile.enabled:
        return pcm, {"status": "disabled"}
    started = time.perf_counter()
    source = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    if rms(source) < 1e-4:
        return pcm, {"status": "unchanged_quiet", "reason": "Insufficient signal for voiced processing"}
    # Pad short replies for WORLD analysis only. Playback always keeps the original sample count.
    padded = np.pad(source, (0, max(0, RATE // 2 - len(source))))
    try:
        analysis = analyze(padded, pitch_method=profile.pitch_method)
    except InsufficientVoicingError as error:
        return pcm, {"status": "unchanged_unvoiced", "reason": str(error)}
    preset = Preset("npc_voice", profile.strength, profile.band, profile.pitch_method, profile.edge_ms)
    rendered, timing = resynthesize(analysis, preset)
    rendered = rendered[:len(source)]
    matched, _ = match_levels([rendered], active_mask(source))
    output = pcm16(matched[0])
    if len(output) != len(pcm):
        raise ValueError("Voice processing changed the speech duration")
    return output, {
        "status": "processed", "samples": len(source),
        "analysis_padding_samples": len(padded) - len(source),
        "processing_ms": (time.perf_counter() - started) * 1000, **timing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    profile = VoiceProfile.from_json(json.loads(args.profile))
    while True:
        if args.stream:
            header = sys.stdin.buffer.read(4)
            if not header:
                return
            if len(header) != 4:
                raise ValueError("Truncated voice request header")
            size = struct.unpack("!I", header)[0]
            if not size or size % 2 or size > MAX_PCM_BYTES:
                raise ValueError("Invalid voice request size")
            pcm = sys.stdin.buffer.read(size)
            if len(pcm) != size:
                raise ValueError("Truncated voice request audio")
        else:
            pcm = sys.stdin.buffer.read(MAX_PCM_BYTES + 1)
        output, metadata = process_pcm(pcm, profile)
        payload = json.dumps(metadata, allow_nan=False).encode("utf-8") + b"\n" + output
        if args.stream:
            sys.stdout.buffer.write(struct.pack("!I", len(payload)))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        if not args.stream:
            return


if __name__ == "__main__":
    main()
