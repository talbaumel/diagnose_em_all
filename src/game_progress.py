from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable


def default_save_path() -> Path:
    if sys.platform == "darwin":
        folder = Path.home() / "Library/Application Support/DiagnoseEmAll"
    elif sys.platform == "win32":
        folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "DiagnoseEmAll"
    else:
        folder = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "diagnose_em_all"
    return folder / "progress.json"


@dataclass
class Progress:
    diagnosed: set[str] = field(default_factory=set)
    position: tuple[float, float] = (480.0, 480.0)
    completion_announced: bool = False
    skill_scores: dict[str, int] = field(default_factory=dict)


def valid_skill_score(score: object) -> bool:
    return type(score) is int and -100 <= score <= 100


class ProgressStore:
    def __init__(self, roster: Iterable[str], path: Path | None = None) -> None:
        self.path = path if path is not None else default_save_path()
        self.roster = frozenset(roster)
        self.key = hashlib.sha256(json.dumps(sorted(self.roster)).encode()).hexdigest()
        self.warning = ""

    def _skill_scores(self, scores: object, diagnosed: set[str]) -> dict[str, int]:
        if not isinstance(scores, dict) or any(
            not isinstance(patient, str)
            or patient not in self.roster
            or patient not in diagnosed
            or not valid_skill_score(score)
            for patient, score in scores.items()
        ):
            raise ValueError("Invalid skill scores")
        return dict(scores)

    def _read(self) -> dict:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "campaigns": {}}
        if (
            not isinstance(document, dict)
            or type(document.get("version")) is not int
            or document["version"] != 1
            or not isinstance(document.get("campaigns"), dict)
        ):
            raise ValueError("Unsupported save format")
        return document

    def load(self) -> Progress:
        try:
            slot = self._read()["campaigns"].get(self.key, {})
            if not isinstance(slot, dict):
                raise ValueError("Invalid campaign data")
            diagnosed = slot.get("diagnosed", [])
            if not isinstance(diagnosed, list) or not all(isinstance(value, str) for value in diagnosed):
                raise ValueError("Invalid completed cases")
            position = slot.get("position", [480.0, 480.0])
            if not (
                isinstance(position, list)
                and len(position) == 2
                and all(type(value) in (int, float) and math.isfinite(value) for value in position)
            ):
                position = [480.0, 480.0]
            self.warning = ""
            completed = set(diagnosed) & self.roster
            try:
                skill_scores = self._skill_scores(slot.get("skill_scores", {}), completed)
            except ValueError:
                skill_scores = {}
                self.warning = "Skill scores unreadable. Existing save preserved."
            return Progress(
                completed,
                (float(position[0]), float(position[1])),
                slot.get("completion_announced") is True,
                skill_scores,
            )
        except (OSError, ValueError, UnicodeError):
            self.warning = "Save unavailable. Existing data preserved."
            return Progress()

    def save(self, progress: Progress, *, reset: bool = False) -> bool:
        temporary: Path | None = None
        try:
            try:
                document = self._read()
                slot = document["campaigns"].get(self.key, {})
                if not isinstance(slot, dict):
                    raise ValueError("Invalid campaign data")
                diagnosed = slot.get("diagnosed", [])
                if not isinstance(diagnosed, list) or not all(isinstance(value, str) for value in diagnosed):
                    raise ValueError("Invalid completed cases")
            except (ValueError, UnicodeError):
                if not reset:
                    raise
                backup = self.path.with_name(self.path.name + ".bak")
                if backup.exists():
                    raise ValueError("Save backup already exists")
                backup.write_bytes(self.path.read_bytes())
                document = {"version": 1, "campaigns": {}}
            if not reset:
                # Optional data cannot make old cases disappear or be silently overwritten.
                self._skill_scores(slot.get("skill_scores", {}), set(diagnosed) & self.roster)
            completed = progress.diagnosed & self.roster
            skill_scores = self._skill_scores(progress.skill_scores, completed)
            document["campaigns"][self.key] = {
                "diagnosed": sorted(completed),
                "position": list(progress.position),
                "completion_announced": progress.completion_announced,
                "skill_scores": skill_scores,
            }
            payload = json.dumps(document, indent=2, allow_nan=False) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            self.warning = ""
            return True
        except (OSError, ValueError, UnicodeError):
            self.warning = "Progress not saved. Previous save preserved."
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass