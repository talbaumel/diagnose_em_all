"""Verify or reproduce the hash-pinned roster audition from its source manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from tools.prepare_cue_candidates import digest, prepare_levels, save_wave

ROOT = Path(__file__).resolve().parents[1] / "data/audio/roster_candidates"


def progress(label: str, done: int, total: int, started: float) -> None:
    filled = round(20 * done / total)
    print(f"[{'#' * filled}{'.' * (20 - filled)}] {done}/{total} "
          f"{label} | elapsed {time.monotonic() - started:.1f}s", flush=True)


def verify(root: Path) -> dict:
    manifest = json.loads((root / "SOURCES.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    total = len(manifest["sources"]) + len(manifest["cues"])
    done = 0
    for source in manifest["sources"].values():
        if source["clip_license"] != "CC0-1.0":
            raise ValueError("Roster source must have verified CC0 rights")
        path = root / source["file"]
        if not path.resolve().is_relative_to(root.resolve()) or digest(path) != source["sha256"]:
            raise ValueError(f"Source path/hash mismatch: {path}")
        done += 1
        progress(f"source {path.name}", done, total, started)
    for cue in manifest["cues"]:
        source = manifest["sources"][cue["source_id"]]
        if source["technical_status"] == "rejected" or source["full_scale_samples"]:
            raise ValueError(f"Rejected source selected by {cue['id']}")
        path = root / cue["file"]
        if not path.resolve().is_relative_to(root.resolve()) or digest(path) != cue["sha256"]:
            raise ValueError(f"Cue path/hash mismatch: {path}")
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                raise ValueError(f"Wrong cue format: {path}")
            pcm = wav.readframes(wav.getnframes())
            if len(pcm) != wav.getnframes() * 2:
                raise ValueError(f"Truncated cue: {path}")
        if not 4800 <= len(pcm) <= 480000 or pcm[:2] != bytes(2) or pcm[-2:] != bytes(2):
            raise ValueError(f"Invalid cue duration/fades: {path}")
        done += 1
        progress(f"cue {cue['id']}", done, total, started)
    return manifest


def fetch_missing(root: Path) -> None:
    manifest = json.loads((root / "SOURCES.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    prefix = "https://huggingface.co/datasets/Fhrozen/FSD50k/resolve/" + manifest["mirror_revision"] + "/clips/"
    for index, source in enumerate(manifest["sources"].values(), start=1):
        path = root / source["file"]
        if not path.resolve().is_relative_to((root / "originals").resolve()):
            raise ValueError("Source must stay in originals/")
        if path.exists():
            if digest(path) != source["sha256"]:
                raise ValueError(f"Existing original changed: {path}")
            progress(f"preserved {path.name}", index, len(manifest["sources"]), started)
            continue
        if source["clip_license"] != "CC0-1.0" or not source["download_url"].startswith(prefix):
            raise ValueError("Source must be licensed and from the pinned dataset mirror")
        with urllib.request.urlopen(source["download_url"], timeout=30) as response:
            payload = response.read(12_000_001)
        if len(payload) > 12_000_000 or hashlib.sha256(payload).hexdigest() != source["sha256"]:
            raise ValueError("Downloaded source hash/size mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            output.write(payload)
        progress(f"downloaded {path.name}", index, len(manifest["sources"]), started)


def rebuild(root: Path, output: Path) -> None:
    manifest = verify(root)
    if output.exists():
        raise FileExistsError("Use a fresh output directory; auditions are never overwritten")
    output.mkdir(parents=True)
    parts: list[NDArray[np.float64]] = []
    timeline = []
    elapsed = 0.0
    started = time.monotonic()
    for index, cue in enumerate(manifest["cues"], start=1):
        source = manifest["sources"][cue["source_id"]]
        start, end = cue["start_seconds"], cue["end_seconds"]
        if not 0 <= start < end <= source["duration_seconds"] or not 0.1 <= end - start <= 10:
            raise ValueError("Invalid source trim")
        command = [
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(root / source["file"]),
            "-af", f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS",
            "-ac", "1", "-ar", "24000", "-f", "f64le", "pipe:1",
        ]
        decoded = subprocess.run(command, check=True, capture_output=True, timeout=30).stdout
        signal, _ = prepare_levels(np.frombuffer(decoded, dtype="<f8").copy(),
                                   cue["target_active_rms_dbfs"])
        result = save_wave(output / f"{cue['id']}.wav", signal)
        if result["sha256"] != cue["sha256"]:
            raise ValueError(f"Reproduction differs for {cue['id']}; review tool/ffmpeg versions")
        timeline.append({"cue": cue["id"], "caption": cue["caption"], "start_seconds": elapsed,
                         "end_seconds": elapsed + len(signal) / 24000})
        parts.extend((signal, np.zeros(24000)))
        elapsed += len(signal) / 24000 + 1
        progress(f"rebuilt {cue['id']}", index, len(manifest["cues"]), started)
    save_wave(output / "listen_all.wav", np.concatenate(parts[:-1]))
    (output / "playlist.json").write_text(json.dumps({
        "approved_for_gameplay": False, "timeline": timeline,
        "note": "Cue-only comparison; character-voice and clinical listening review still required.",
    }, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "fetch", "rebuild"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "fetch":
        fetch_missing(args.root)
    elif args.action == "rebuild":
        if args.output is None:
            parser.error("rebuild requires --output pointing to a new directory")
        rebuild(args.root, args.output)
    else:
        print(f"Verified {len(verify(args.root)['cues'])} hash-pinned roster candidates.")


if __name__ == "__main__":
    main()