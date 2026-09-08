"""Local character reactions and persistent effects volume, separate from speech."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.game_progress import default_save_path


def examination_cue_event(patient: str, skill: str) -> str | None:
    if patient == "SPRAINED_ANKLE_ATHLETE" and skill in {
        "ankle_examination", "joint_examination", "range_of_motion",
        "weight_bearing_assessment", "gait_assessment",
    }:
        return "ankle_examination"
    if patient == "ELDERLY_WITH_BACK_PAIN" and skill == "gait_assessment":
        return "back_examination"
    return None


class EffectsSettings:
    LEVELS = (1.0, 0.5, 0.25, 0.0)

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else default_save_path().with_name("audio-settings.json")
        self.volume = 1.0
        self.warning = ""

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or set(data) != {"version", "effects_volume"}):
                raise ValueError("Unsupported audio settings")
            volume = data["effects_volume"]
            if type(volume) not in (int, float) or volume not in self.LEVELS:
                raise ValueError("Invalid effects volume")
            self.volume = float(volume)
            self.warning = ""
        except FileNotFoundError:
            self.warning = ""
        except (OSError, ValueError, UnicodeError) as error:
            self.warning = f"Audio settings unavailable; existing file preserved: {error}"

    def cycle(self) -> None:
        volume = self.LEVELS[(self.LEVELS.index(self.volume) + 1) % len(self.LEVELS)]
        temporary: Path | None = None
        try:
            if self.warning:
                raise ValueError("Resolve the unreadable audio settings file before saving changes.")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix="audio-settings-", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump({"version": 1, "effects_volume": volume}, output)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.volume = volume
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@dataclass
class CueReaction:
    event: str = ""
    started: float = 0.0
    duration: float = 0.0

    def start(self, event: str, duration: float, now: float) -> None:
        if event not in {"ankle_examination", "back_examination", "rash_fidget", "back_posture"}:
            raise ValueError(f"Unknown character reaction: {event}")
        if not math.isfinite(duration) or not 0 < duration <= 10:
            raise ValueError("Reaction duration must be positive and at most ten seconds")
        self.event, self.started, self.duration = event, now, duration

    def stop(self) -> None:
        self.event = ""

    def pose(self, now: float) -> tuple[float, int]:
        if not self.event or not 0 <= now - self.started < self.duration:
            return 0.0, 0
        phase = (now - self.started) / self.duration
        envelope = math.sin(math.pi * phase)
        if self.event == "rash_fidget":
            return math.sin(phase * math.tau * 2) * envelope * 2, 0
        if self.event == "ankle_examination":
            return -3 * envelope, round(2 * envelope)
        return 3 * envelope, 0
