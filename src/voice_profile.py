"""Dependency-free configuration for optional NPC speech processing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class VoiceProfile:
    enabled: bool = False
    processor: str = "world"
    pitch_method: Literal["dio", "harvest"] = "harvest"
    strength: float = 0.8
    band: Literal["high", "broad"] = "broad"
    edge_ms: float = 0.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("voice.enabled must be a boolean")
        if self.processor != "world":
            raise ValueError("voice.processor must be world")
        if self.pitch_method not in ("dio", "harvest"):
            raise ValueError("voice.pitch_method must be dio or harvest")
        if self.band not in ("high", "broad"):
            raise ValueError("voice.band must be high or broad")
        maximum = 0.2 if self.band == "high" else 0.8
        for name, value, upper in (("strength", self.strength, maximum), ("edge_ms", self.edge_ms, 30)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= upper):
                raise ValueError(f"voice.{name} must be finite and between 0 and {upper}")

    @classmethod
    def from_json(cls, value: object) -> VoiceProfile:
        if not isinstance(value, dict):
            raise ValueError("voice must be an object")
        try:
            return cls(**value)
        except TypeError as error:
            raise ValueError(f"Invalid voice configuration: {error}") from error
