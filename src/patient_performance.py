"""Opt-in patient performances; only packaged, allowlisted sounds may be played."""

from __future__ import annotations

import math
import random
import wave
from dataclasses import dataclass, field
from collections.abc import Mapping
from types import MappingProxyType
from pathlib import Path

from src.voice_profile import VoiceProfile
from src.cue_catalog import CueChoice, LOCAL_CUE_EVENTS, load_catalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COUGH_CLIP = "assets/audio/coughvid/dry_01_short.wav"
ALLOWED_CLIPS = frozenset({COUGH_CLIP})


def read_pcm_clip(path: Path) -> bytes:
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getframerate() != 24_000
                or source.getsampwidth() != 2
                or source.getcomptype() != "NONE"
                or not 0 < source.getnframes() <= 24_000 * 10
            ):
                raise ValueError("Clip must be non-empty mono 24 kHz PCM16 WAV, at most 10 seconds")
            audio = source.readframes(source.getnframes())
            if len(audio) != source.getnframes() * 2:
                raise ValueError("Clip PCM data is truncated")
            return audio
    except (wave.Error, EOFError) as error:
        raise ValueError(f"Invalid WAV clip {path}: {error}") from error


@dataclass(frozen=True)
class PerformanceProfile:
    cough_clip: str = COUGH_CLIP
    cooldown_seconds: float = 20.0
    spontaneous_every_turns: int = 3
    voice: VoiceProfile | None = None
    cues: tuple[CueChoice, ...] | None = None
    delivery: str = "Speak naturally, using the persona's age, personality and emotional state."
    cue_selection: str = "weighted"
    max_spontaneous_cues: int | None = None
    event_cues: Mapping[str, tuple[CueChoice, ...]] = field(default_factory=dict)
    event_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_spontaneous_cues is not None and (
            type(self.max_spontaneous_cues) is not int or self.max_spontaneous_cues < 0
        ):
            raise ValueError("max_spontaneous_cues must be a nonnegative integer or None")
        if not isinstance(self.event_cues, Mapping) or not isinstance(self.event_limits, Mapping):
            raise ValueError("event_cues and event_limits must be mappings")
        if any(event not in LOCAL_CUE_EVENTS for event in (*self.event_cues, *self.event_limits)):
            raise ValueError("Unknown local cue event")
        if any(event not in self.event_cues for event in self.event_limits):
            raise ValueError("event_limits requires a configured event")
        if any(type(limit) is not int or limit < 0 for limit in self.event_limits.values()):
            raise ValueError("event_limits must contain nonnegative integers")
        if self.event_cues:
            catalog = load_catalog()
            for choices in self.event_cues.values():
                if (not isinstance(choices, tuple) or not 0 < len(choices) <= 8
                        or any(not isinstance(choice, CueChoice) for choice in choices)):
                    raise ValueError("event_cues requires one to eight validated cue choices")
                if len({choice.id for choice in choices}) != len(choices):
                    raise ValueError("Duplicate event cue IDs")
                if any(choice.id not in catalog or "event" not in catalog[choice.id].contexts
                       for choice in choices):
                    raise ValueError("Event cue requires a catalog ID with event context")
        object.__setattr__(self, "event_cues", MappingProxyType(dict(self.event_cues)))
        object.__setattr__(self, "event_limits", MappingProxyType({
            event: self.event_limits.get(event, 1) for event in self.event_cues
        }))
        if self.cue_selection not in ("weighted", "cycle"):
            raise ValueError("cue_selection must be weighted or cycle")
        if not isinstance(self.delivery, str) or not self.delivery.strip() or len(self.delivery) > 2000:
            raise ValueError("delivery must be a nonempty string of at most 2000 characters")
        if self.cues is not None:
            if not isinstance(self.cues, tuple) or len(self.cues) > 8 or any(
                not isinstance(cue, CueChoice) for cue in self.cues
            ):
                raise ValueError("cues must contain at most eight validated cue choices")
            catalog = load_catalog()
            if len({cue.id for cue in self.cues}) != len(self.cues):
                raise ValueError("Duplicate cue IDs")
            if any(cue.id not in catalog for cue in self.cues):
                raise ValueError("Unknown cue catalog ID")
            if any(not {"internal_pause", "requested"}.intersection(catalog[cue.id].contexts)
                   for cue in self.cues):
                raise ValueError("Regular cues require internal_pause or requested context")
        if self.voice is not None and not isinstance(self.voice, VoiceProfile):
            raise ValueError("voice must be a validated VoiceProfile")
        if not isinstance(self.cough_clip, str) or self.cough_clip not in ALLOWED_CLIPS:
            raise ValueError("Performance clip is not in the local allowlist")
        if self.clip_path != PROJECT_ROOT / self.cough_clip:
            raise ValueError("Performance clip must not be a symlink outside its allowlisted path")
        if (
            isinstance(self.cooldown_seconds, bool)
            or not isinstance(self.cooldown_seconds, (int, float))
            or not math.isfinite(self.cooldown_seconds)
            or self.cooldown_seconds < 5
        ):
            raise ValueError("cooldown_seconds must be finite and at least 5")
        minimum_turns = 1 if self.cues is not None or self.event_cues else 2
        if type(self.spontaneous_every_turns) is not int or self.spontaneous_every_turns < minimum_turns:
            raise ValueError(f"spontaneous_every_turns must be an integer of at least {minimum_turns}")

    @property
    def clip_path(self) -> Path:
        return (PROJECT_ROOT / self.cough_clip).resolve()

    @property
    def instructions(self) -> str:
        if self.cues is not None or self.event_cues:
            filter_note = (
                "The local processor supplies voice texture; use natural comfortable speech, "
                "not imitated hoarseness, nasal resonance, whispering or breath noise. "
                if self.voice and self.voice.enabled else ""
            )
            return self.delivery + " " + filter_note + (
                "Keep replies concise with natural phrase pauses. Local bodily cues are handled separately. "
                "Use the available bodily-action tools only when explicitly requested by the clinician, "
                "once without a spoken introduction. Never synthesize or narrate those sounds. "
                "Do not request spontaneous cues. After a tool result continue only if an answer remains; "
                "do not announce tools or call another bodily-action tool in the same turn."
            )
        delivery = (
            "Use a comfortable natural speaking voice with normal breathing and natural phrasing. "
            "The local processor supplies the voice texture; do not imitate hoarseness, "
            "nasal resonance, whispering or extra breath noise. "
            if self.voice is not None and self.voice.enabled else
            self.delivery + " "
        )
        return delivery + (
            "Keep replies "
            "short. When asked to cough or let the doctor hear your cough, call cough "
            "once without a spoken introduction. Never synthesize, spell or narrate "
            "cough sounds. The local cough is a bodily action, not a diagnostic test. "
            "After its result, continue naturally only if something still needs an "
            "answer; do not announce tools, playback, success or failure, and never "
            "call cough again in the same turn. Occasional spontaneous coughs are "
            "handled locally; do not request them yourself."
        )

    @property
    def cue_tools(self) -> list[dict]:
        if self.cues is None and not self.event_cues:
            return [COUGH_TOOL]
        catalog = load_catalog()
        kinds = dict.fromkeys(catalog[choice.id].kind for choice in self.cues or ()
                              if "requested" in catalog[choice.id].contexts)
        return [{
            "type": "function", "name": kind,
            "description": f"Perform {kind.replace('_', ' ')} once, only when the clinician explicitly requests it. No spoken introduction.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        } for kind in kinds]


COUGH_TOOL = {
    "type": "function",
    "name": "cough",
    "description": "Cough naturally once, only when the clinician asks to hear a cough. No spoken introduction.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


class CuePolicy:
    def __init__(self, profile: PerformanceProfile, clock) -> None:
        self.profile = profile
        self.clock = clock
        self.turn = 0
        self.used_turn = -1
        self.last_started = float("-inf")
        self.last_started_turn: int | None = None
        self.last_cue: str | None = None
        self.played_in_cycle: set[str] = set()
        self.spontaneous_count = 0
        self.event_counts: dict[str, int] = {}

    def new_turn(self) -> None:
        self.turn += 1

    def available(self, *, spontaneous: bool = False, blocked: bool = False,
                  event: str | None = None, reserved: bool = False) -> bool:
        if (blocked or (self.turn == 0 and event is None)
                or self.last_started_turn == self.turn
                or (not reserved and self.used_turn == self.turn)):
            return False
        if self.clock() - self.last_started < self.profile.cooldown_seconds:
            return False
        if event is not None:
            if event not in self.profile.event_cues:
                return False
            if self.event_counts.get(event, 0) >= self.profile.event_limits[event]:
                return False
        if spontaneous:
            if (self.profile.max_spontaneous_cues is not None
                    and self.spontaneous_count >= self.profile.max_spontaneous_cues):
                return False
            if self.profile.cues is None:
                if (self.turn - 1) % self.profile.spontaneous_every_turns:
                    return False
            elif (self.last_started_turn is not None
                  and self.turn - self.last_started_turn < self.profile.spontaneous_every_turns):
                return False
        return True

    def reserve(self, *, spontaneous: bool = False, blocked: bool = False,
                event: str | None = None) -> bool:
        if not self.available(spontaneous=spontaneous, blocked=blocked, event=event):
            return False
        self.used_turn = self.turn
        return True

    def release(self, turn: int) -> None:
        if self.used_turn == turn and self.last_started_turn != turn:
            self.used_turn = -1

    def choose(self, choices: tuple[CueChoice, ...], *, kind: str | None = None,
               event: str | None = None) -> str | None:
        catalog = load_catalog()
        context = "event" if event else "requested" if kind else "internal_pause"
        eligible = [choice for choice in choices if context in catalog[choice.id].contexts
                    and (kind is None or catalog[choice.id].kind == kind)]
        if kind is None and event is None and self.profile.cue_selection == "cycle":
            unplayed = [choice for choice in eligible if choice.id not in self.played_in_cycle]
            if unplayed:
                eligible = unplayed
            else:
                self.played_in_cycle.clear()
        alternatives = [choice for choice in eligible if choice.id != self.last_cue]
        if alternatives:
            eligible = alternatives
        if not eligible:
            return None
        return random.choices([choice.id for choice in eligible], weights=[choice.weight for choice in eligible])[0]

    def started(self, cue_id: str | None = None, *, spontaneous: bool = False,
                event: str | None = None) -> None:
        self.last_started = self.clock()
        self.last_started_turn = self.turn
        self.used_turn = self.turn
        if spontaneous:
            self.spontaneous_count += 1
        if event is not None:
            self.event_counts[event] = self.event_counts.get(event, 0) + 1
        if cue_id is not None:
            self.last_cue = cue_id
            self.played_in_cycle.add(cue_id)


# Compatibility for existing cough-only profiles and callers.
CoughPolicy = CuePolicy
