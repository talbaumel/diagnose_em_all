"""A short, local thank-you moment; never a consultation or scoring action."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pygame


CELEBRATION_SECONDS = 3.4
CONFETTI_COUNT = 54


@dataclass(frozen=True)
class ThankYou:
    message: str
    lift: float = 0
    lean: float = 0


THANK_YOUS = {
    "COMMON_COLD_KID": ThankYou("Thanks, doc! You're great!", lift=4, lean=2),
    "STOMACHACHE_TEEN": ThankYou("Thanks for helping me!", lean=2),
    "MIGRAINE_SUFFERER": ThankYou("Thank you for listening.", lean=1),
    "ALLERGIES_PATIENT": ThankYou("Thanks for your patience!", lift=2, lean=2),
    "SPRAINED_ANKLE_ATHLETE": ThankYou("Thanks for the help, doc!"),
    "ANXIOUS_ADULT": ThankYou("Thanks for hearing me out.", lean=1),
    "FEVERISH_PATIENT": ThankYou("Thanks for looking after me.", lean=1),
    "RASH_PATIENT": ThankYou("Thank you for being kind.", lean=2),
    "ELDERLY_WITH_BACK_PAIN": ThankYou("Much appreciated, doctor."),
    "SLEEP_DEPRIVED_WORKER": ThankYou("Thanks for your time, doc.", lean=1),
    "ECCENTRIC_NEIGHBOR": ThankYou("Thanks for being so kind.", lift=2, lean=2),
}


@dataclass(frozen=True)
class Confetti:
    x: float
    vx: float
    vy: float
    delay: float
    lifetime: float
    phase: float
    size: int
    color: int


class PatientCelebration:
    def __init__(self, patient_type: str) -> None:
        if patient_type not in THANK_YOUS:
            raise ValueError(f"Missing thank-you gesture for patient: {patient_type}")
        self.thanks = THANK_YOUS[patient_type]
        self.started_at: float | None = None
        self._dismissed = False
        self._particles: tuple[Confetti, ...] = ()

    def start(self, now: float) -> bool:
        if self.started_at is not None:
            return False
        self.started_at = now
        rng = random.Random(self.thanks.message)
        self._particles = tuple(
            Confetti(
                x=184 if index % 2 == 0 else 454,
                vx=rng.uniform(25, 90) * (1 if index % 2 == 0 else -1),
                vy=rng.uniform(-160, -85),
                delay=rng.uniform(0, 0.18),
                lifetime=rng.uniform(2, 2.9),
                phase=rng.uniform(0, math.tau),
                size=rng.randint(3, 5),
                color=index % 5,
            )
            for index in range(CONFETTI_COUNT)
        )
        return True

    def dismiss(self) -> None:
        self._dismissed = True
        self._particles = ()

    def active(self, now: float) -> bool:
        return self.elapsed(now) is not None

    def elapsed(self, now: float) -> float | None:
        if self.started_at is None or self._dismissed:
            return None
        elapsed = now - self.started_at
        return elapsed if 0 <= elapsed < CELEBRATION_SECONDS else None

    def frame_index(self, now: float, frame_count: int) -> int:
        if frame_count < 1:
            raise ValueError("A patient gesture needs at least one sprite frame.")
        elapsed = self.elapsed(now)
        if elapsed is None:
            return 0
        # Present the existing smile/thumbs-up poses, then settle into relief.
        sequence = (0, 1, 2, 3, 2, 1, 0)
        step = min(len(sequence) - 1, int(elapsed / 0.22))
        return min(sequence[step], frame_count - 1)

    def pose(self, now: float) -> tuple[float, int]:
        elapsed = self.elapsed(now)
        if elapsed is None:
            return 0.0, 0
        envelope = max(0.0, 1 - elapsed / 1.6)
        lean = math.sin(elapsed * math.tau) * self.thanks.lean * envelope
        lift = round(abs(math.sin(elapsed * math.tau)) * self.thanks.lift * envelope)
        return lean, lift

    def draw_confetti(
        self, layer: pygame.Surface, now: float, colors: tuple[tuple[int, int, int], ...],
    ) -> None:
        elapsed = self.elapsed(now)
        if elapsed is None:
            return
        for particle in self._particles:
            age = elapsed - particle.delay
            if not 0 <= age < particle.lifetime:
                continue
            x = particle.x + particle.vx * age + math.sin(age * 6 + particle.phase) * 7
            y = 206 + particle.vy * age + 70 * age * age
            alpha = round(255 * min(1.0, (particle.lifetime - age) / 0.5))
            width = max(1, round(particle.size * abs(math.cos(age * 8 + particle.phase))))
            pygame.draw.rect(
                layer, (*colors[particle.color], alpha),
                (round(x), round(y), width, particle.size),
            )

    def opacity(self, now: float) -> int:
        elapsed = self.elapsed(now)
        if elapsed is None:
            return 0
        return round(255 * min(1.0, elapsed / 0.15, (CELEBRATION_SECONDS - elapsed) / 0.45))
