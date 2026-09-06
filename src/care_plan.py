from __future__ import annotations

from dataclasses import dataclass, field


REFERRAL_URGENCIES = ("Routine", "Urgent", "Emergency")


@dataclass(frozen=True)
class Prescription:
    medication: str
    directions: str
    reason: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (self.medication, self.directions, self.reason)):
            raise ValueError("Medication, directions and reason are required")

    def message(self) -> str:
        return f"I am prescribing {self.medication}. Directions: {self.directions}. Reason: {self.reason}."


@dataclass(frozen=True)
class Referral:
    specialty: str
    reason: str
    urgency: str = "Routine"

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (self.specialty, self.reason)):
            raise ValueError("Referral destination and reason are required")
        if self.urgency not in REFERRAL_URGENCIES:
            raise ValueError("Choose a valid referral urgency")

    def message(self) -> str:
        return f"I am referring you to {self.specialty}. Reason: {self.reason}. Urgency: {self.urgency}."


@dataclass
class CarePlan:
    prescriptions: list[Prescription] = field(default_factory=list)
    referrals: list[Referral] = field(default_factory=list)

    def add(self, order: Prescription | Referral) -> bool:
        orders = self.prescriptions if isinstance(order, Prescription) else self.referrals
        if order in orders:
            return False
        orders.append(order)
        return True

    def summary(self) -> str:
        prescriptions = "\n\n".join(order.message() for order in self.prescriptions) or "No prescriptions recorded."
        referrals = "\n\n".join(order.message() for order in self.referrals) or "No referrals recorded."
        return f"PRESCRIPTIONS\n{prescriptions}\n\nREFERRALS\n{referrals}"