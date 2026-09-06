"""Validated local cue catalog; profiles select IDs, never arbitrary files."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "assets/audio/cue_catalog.json"


@dataclass(frozen=True)
class Cue:
    id: str
    kind: str
    file: str
    sha256: str
    caption: str
    contexts: tuple[str, ...]

    @property
    def path(self) -> Path:
        path = ROOT / self.file
        if not path.resolve().is_relative_to(ROOT / "assets/audio") or path.resolve() != path:
            raise ValueError("Cue must be a local audio asset without symlink redirection")
        return path

    def verify(self) -> None:
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise ValueError(f"Cue hash mismatch: {self.id}")


def load_catalog() -> dict[str, Cue]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("cues"), dict):
        raise ValueError("Invalid cue catalog")
    result = {}
    for identifier, value in data["cues"].items():
        if (not isinstance(identifier, str) or not identifier.isidentifier()
                or not isinstance(value, dict)):
            raise ValueError("Invalid cue catalog entry")
        if value.get("status") not in ("gameplay_trial", "approved"):
            raise ValueError("Only explicitly promoted cues belong in the runtime catalog")
        if value.get("kind") not in ("cough", "sniffle", "sneeze", "throat_clear", "sigh"):
            raise ValueError("Unknown cue kind")
        for key in ("file", "sha256", "caption", "provenance", "license", "review"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"Cue catalog requires {key}")
        if len(value["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in value["sha256"]):
            raise ValueError("Invalid cue hash")
        contexts = value.get("contexts")
        if not isinstance(contexts, list) or not contexts or any(
            item not in ("internal_pause", "requested") for item in contexts
        ):
            raise ValueError("Invalid cue contexts")
        if value.get("gain_db") != 0:
            raise ValueError("Catalog files must already have their reviewed playback level")
        cue = Cue(identifier, value["kind"], value["file"], value["sha256"],
                  value["caption"], tuple(contexts))
        cue.path
        result[identifier] = cue
    return result


@dataclass(frozen=True)
class CueChoice:
    id: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.isidentifier():
            raise ValueError("Cue choice requires a catalog ID")
        if (isinstance(self.weight, bool) or not isinstance(self.weight, (int, float))
                or not math.isfinite(self.weight) or not 0 < self.weight <= 100):
            raise ValueError("Cue weight must be finite, positive and at most 100")

    @classmethod
    def from_json(cls, data: object) -> CueChoice:
        if not isinstance(data, dict):
            raise ValueError("Cue selection must be an object")
        try:
            return cls(**data)
        except TypeError as error:
            raise ValueError(f"Invalid cue selection: {error}") from error
