"""Local instrument practice; the caller owns clinical results and scoring."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import pygame

from src.skill_art import load_instrument_art


@dataclass(frozen=True)
class Instrument:
    label: str
    title: str
    target: str
    contact: str


INSTRUMENTS = {
    "temperature": Instrument("Thermometer", "Take a temperature", "mouth", "silver tip"),
    "oxygen_saturation": Instrument("Pulse oximeter", "Check blood oxygen", "finger", "clip"),
    "lung_auscultation": Instrument("Stethoscope", "Listen to the lungs", "chest", "chestpiece"),
    "ear_examination": Instrument("Otoscope", "Examine the ears", "ear", "lighted tip"),
}
SUPPORTED_ACTIVITIES = frozenset(INSTRUMENTS)
MEASURE_SECONDS = 2.5

# Talking frame zero, normalized to the cropped whole-character alpha bounds.
MOUTH_LANDMARKS = {
    "COMMON_COLD_KID": (.51, .44),
    "STOMACHACHE_TEEN": (.36, .42),
    "MIGRAINE_SUFFERER": (.46, .44),
    "ALLERGIES_PATIENT": (.50, .43),
    "SPRAINED_ANKLE_ATHLETE": (.50, .405),
    "ANXIOUS_ADULT": (.47, .40),
    "FEVERISH_PATIENT": (.45, .51),
    "RASH_PATIENT": (.48, .45),
    "ELDERLY_WITH_BACK_PAIN": (.38, .40),
    "SLEEP_DEPRIVED_WORKER": (.55, .415),
    "ECCENTRIC_NEIGHBOR": (.56, .445),
}

# Visible ear, upper chest, and exposed hand in the same cropped talking pose.
BODY_LANDMARKS = {
    "COMMON_COLD_KID": {"ear": (.72, .38), "chest": (.53, .58), "finger": (.86, .55)},
    "STOMACHACHE_TEEN": {"ear": (.58, .40), "chest": (.43, .55), "finger": (.50, .65)},
    "MIGRAINE_SUFFERER": {"ear": (.68, .39), "chest": (.51, .57), "finger": (.84, .55)},
    "ALLERGIES_PATIENT": {"ear": (.71, .40), "chest": (.52, .58), "finger": (.57, .56)},
    "SPRAINED_ANKLE_ATHLETE": {"ear": (.70, .37), "chest": (.52, .56), "finger": (.75, .65)},
    "ANXIOUS_ADULT": {"ear": (.67, .38), "chest": (.50, .54), "finger": (.62, .65)},
    "FEVERISH_PATIENT": {"ear": (.70, .45), "chest": (.49, .67), "finger": (.43, .69)},
    "RASH_PATIENT": {"ear": (.69, .41), "chest": (.50, .61), "finger": (.20, .61)},
    "ELDERLY_WITH_BACK_PAIN": {"ear": (.62, .35), "chest": (.46, .55), "finger": (.14, .57)},
    "SLEEP_DEPRIVED_WORKER": {"ear": (.77, .40), "chest": (.53, .57), "finger": (.76, .70)},
    "ECCENTRIC_NEIGHBOR": {"ear": (.70, .41), "chest": (.55, .62), "finger": (.20, .54)},
}

_INK = (20, 32, 38)
_IVORY = (247, 249, 244)
_WHITE = (255, 255, 251)
_TEAL = (52, 132, 121)
_MINT = (193, 231, 215)
_CORAL = (238, 105, 82)
_BLUE = (47, 112, 162)
_SILVER = (188, 203, 211)


def draw_thermometer(
    surface: pygame.Surface, tip: tuple[float, float], scale: float = 1.0,
) -> None:
    """Draw leftward from the silver contact tip, with an unfilled display."""
    draw_instrument(surface, "temperature", tip, scale)


def draw_instrument(
    surface: pygame.Surface, skill_id: str, tip: tuple[float, float], scale: float = 1.0,
) -> None:
    """The contact point follows the cursor; device displays contain no readings."""
    if skill_id not in INSTRUMENTS:
        raise ValueError(f"Unknown instrument: {skill_id!r}")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Instrument scale must be finite and positive")
    art, contact = load_instrument_art(skill_id)
    size = (max(1, round(art.get_width() * scale)), max(1, round(art.get_height() * scale)))
    image = pygame.transform.scale(art, size)
    anchor = (
        min(size[0] - 1, max(0, round((contact[0] + .5) * size[0] / art.get_width() - .5))),
        min(size[1] - 1, max(0, round((contact[1] + .5) * size[1] / art.get_height() - .5))),
    )
    surface.blit(image, (round(tip[0]) - anchor[0], round(tip[1]) - anchor[1]))


class InstrumentActivity:
    """Mouse/keyboard placement followed by a monotonic, update-driven timer.

    Pass the patient's talking frame zero and its PatientType.name. Event
    positions are logical viewport coordinates, not physical window pixels.
    Only update() reports completion; no case results or callbacks live here.
    """

    MEASURE_SECONDS = MEASURE_SECONDS

    def __init__(self, patient_frame: pygame.Surface, patient_type: str, skill_id: str) -> None:
        if skill_id not in INSTRUMENTS:
            raise ValueError(f"Unknown instrument: {skill_id!r}")
        self.skill_id = skill_id
        self.instrument = INSTRUMENTS[skill_id]
        if patient_type not in MOUTH_LANDMARKS:
            raise ValueError(f"Unknown instrument patient type: {patient_type!r}")
        bounds = patient_frame.get_bounding_rect(min_alpha=17)
        if not bounds.width or not bounds.height:
            raise ValueError("Patient frame must contain a visible character")
        cropped = patient_frame.subsurface(bounds)
        factor = min(200 / bounds.width, 242 / bounds.height)
        size = (max(1, round(bounds.width * factor)), max(1, round(bounds.height * factor)))
        self.patient_frame = pygame.transform.scale(cropped, size)
        self.patient_rect = self.patient_frame.get_rect(midbottom=(328, 354))
        anchor_x, anchor_y = (MOUTH_LANDMARKS[patient_type] if skill_id == "temperature"
                              else BODY_LANDMARKS[patient_type][self.instrument.target])
        target = (
            round(self.patient_rect.left + size[0] * anchor_x),
            round(self.patient_rect.top + size[1] * anchor_y),
        )
        self.target_rect = pygame.Rect(0, 0, 26, 22)
        self.target_rect.center = target
        self.tray_rect = pygame.Rect(24, 282, 164, 72)
        self.cancel_rect = pygame.Rect(366, 426, 90, 34)
        art, contact = load_instrument_art(skill_id)
        tray_art_rect = art.get_rect(center=(self.tray_rect.centerx, self.tray_rect.bottom - 26))
        self._tray_tip = (tray_art_rect.x + contact[0], tray_art_rect.y + contact[1])
        self.font = pygame.font.SysFont("Avenir Next", 15)
        self.heading = pygame.font.SysFont("Avenir Next", 24, bold=True)
        self.label_font = pygame.font.SysFont("Avenir Next", 14, bold=True)
        self._reset()

    def _reset(self) -> None:
        self.state = "idle"
        self.tip: tuple[float, float] = self._tray_tip
        self.measurement_started: float | None = None
        self._progress = 0.0
        self._pickup_press = False
        self.message = f"Pick up the {self.instrument.label.lower()}."

    def _pick_up(self, position: tuple[float, float]) -> None:
        self.state = "picked"
        self.tip = position
        self.message = f"Move the {self.instrument.contact} to the {self.instrument.target}."

    def _place(self) -> None:
        if self.target_rect.collidepoint(self.tip):
            self.tip = self.target_rect.center
            self.state = "measuring"
            self.measurement_started = time.monotonic()
            self._progress = 0.0
            self.message = "Hold still while the measurement runs."
        else:
            self.message = f"Not quite! Aim the {self.instrument.contact} at the {self.instrument.target}."

    def handle_event(
        self, event: pygame.event.Event, position: tuple[float, float],
    ) -> bool | None:
        key = event.key if event.type == pygame.KEYDOWN else None
        mouse_button = event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP)
        if (
            key == pygame.K_ESCAPE
            or (mouse_button and event.button == 3)
            or (mouse_button and event.button == 1 and self.cancel_rect.collidepoint(position))
        ):
            if self.state != "complete":
                self._reset()
            return False
        if event.type == pygame.WINDOWFOCUSLOST:
            if self.state != "complete":
                self._reset()
                self.message = "Paused. Pick up the instrument to retry."
            return None
        if self.state in ("measuring", "complete"):
            return None
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.state == "idle":
                self._pick_up(self._tray_tip)
            else:
                self._place()
        elif self.state == "picked" and key == pygame.K_TAB:
            self.tip = self.target_rect.center
        elif self.state == "picked" and key in (
            pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN,
        ):
            dx, dy = {
                pygame.K_LEFT: (-5, 0), pygame.K_RIGHT: (5, 0),
                pygame.K_UP: (0, -5), pygame.K_DOWN: (0, 5),
            }[key]
            self.tip = (max(100, min(478, self.tip[0] + dx)),
                        max(12, min(398, self.tip[1] + dy)))
        elif event.type == pygame.MOUSEMOTION and self.state == "picked":
            self.tip = position
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            if self.state == "idle" and self.tray_rect.collidepoint(position):
                self._pick_up(position)
                self._pickup_press = True
            elif self.state == "picked":
                self.tip = position
                self._pickup_press = False
                self._place()
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1 and self.state == "picked":
            self.tip = position
            # The release of a tray click keeps the tool picked for click-to-place.
            if not (self._pickup_press and self.tray_rect.collidepoint(position)):
                self._place()
            self._pickup_press = False
        return None

    def update(self, now: float) -> bool:
        """Advance using the same monotonic clock as placement; completion sticks."""
        if self.state == "measuring" and self.measurement_started is not None:
            elapsed = max(0.0, now - self.measurement_started)
            self._progress = min(1.0, elapsed / self.MEASURE_SECONDS)
            if elapsed >= self.MEASURE_SECONDS:
                self.state = "complete"
                self.message = "Measurement complete."
        return self.state == "complete"

    def _text(
        self, surface: pygame.Surface, text: str, position: tuple[int, int],
        *, label: bool = False, color: tuple[int, int, int] = _INK,
    ) -> None:
        font = self.label_font if label else self.font
        surface.blit(font.render(text, True, color), position)

    def draw(self, surface: pygame.Surface) -> None:
        """Render the entire 480px modal without advancing the measurement."""
        surface.fill(_IVORY)
        pygame.draw.rect(surface, _TEAL, (0, 0, 480, 6))
        surface.blit(self.heading.render(self.instrument.title, True, _INK), (24, 22))
        self._text(surface, self.message, (24, 61))
        instruction = {
            "idle": "Click the tray, or press Enter, to begin.",
            "picked": "Click or release at the highlight. Tab helps you aim.",
            "measuring": "Keep the instrument in place until the bar fills.",
            "complete": "Your care team will review the result.",
        }[self.state]
        self._text(surface, instruction, (24, 83))

        pygame.draw.rect(surface, (226, 239, 232), (20, 111, 440, 251))
        pygame.draw.rect(surface, _MINT, (20, 347, 440, 15))
        surface.blit(self.patient_frame, self.patient_rect)
        pygame.draw.rect(surface, _WHITE, self.tray_rect)
        pygame.draw.rect(surface, _TEAL, self.tray_rect, width=2)
        self._text(surface, "INSTRUMENT TRAY", (34, 290), label=True, color=_TEAL)
        if self.state != "idle":
            self._text(surface, "Instrument in use", (34, 323), label=True)

        if self.state == "picked":
            on_target = self.target_rect.collidepoint(self.tip)
            pygame.draw.rect(surface, _TEAL if on_target else _CORAL,
                             self.target_rect.inflate(6, 6), width=2)
        if self.skill_id == "temperature":
            # A simple open mouth also clarifies the tissue-covered allergy sprite.
            marker = pygame.Rect(0, 0, 10, 7)
            marker.center = self.target_rect.center
            pygame.draw.rect(surface, _CORAL, marker.inflate(4, 4))
            pygame.draw.rect(surface, _INK, marker)
            pygame.draw.rect(surface, _IVORY, (marker.x + 2, marker.y, 6, 2))
        draw_instrument(surface, self.skill_id, self.tip)

        status = {
            "idle": "Ready to pick up", "picked": f"Place the {self.instrument.contact}",
            "measuring": "Measuring... hold in place", "complete": "Measurement complete",
        }[self.state]
        self._text(surface, status, (24, 373), label=True, color=_TEAL)
        progress_rect = pygame.Rect(24, 397, 432, 10)
        pygame.draw.rect(surface, _MINT, progress_rect)
        if self._progress:
            pygame.draw.rect(surface, _TEAL, (
                progress_rect.x, progress_rect.y,
                round(progress_rect.width * self._progress), progress_rect.height,
            ))
        self._text(surface, f"Enter: pick / place   Tab: {self.instrument.target}", (24, 424))
        self._text(surface, "Arrows: move   Esc: cancel", (24, 445))
        pygame.draw.rect(surface, _WHITE, self.cancel_rect)
        pygame.draw.rect(surface, _TEAL, self.cancel_rect, width=2)
        text = self.label_font.render("Cancel", True, _INK)
        surface.blit(text, text.get_rect(center=self.cancel_rect.center))


class ThermometerActivity(InstrumentActivity):
    def __init__(self, patient_frame: pygame.Surface, patient_type: str) -> None:
        super().__init__(patient_frame, patient_type, "temperature")
        self.mouth_rect = self.target_rect
