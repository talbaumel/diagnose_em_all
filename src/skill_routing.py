"""Collect complete realtime tool batches without executing partial/cancelled output."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SkillCallBatches:
    seen: set[str] = field(default_factory=set)
    cancelled: set[str] = field(default_factory=set)
    active_response: str | None = None
    generation: int = 0

    def cancel(self) -> None:
        self.generation += 1
        if self.active_response:
            self.cancelled.add(self.active_response)

    def collect(self, event: dict) -> list[dict]:
        kind = event.get("type")
        if kind == "response.created":
            self.active_response = event.get("response", {}).get("id")
        elif kind == "response.done":
            response = event.get("response", {})
            response_id = response.get("id")
            if self.active_response == response_id:
                self.active_response = None
            if response.get("status") != "completed" or response_id in self.cancelled:
                return []
            calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
            unique = []
            for call in calls:
                call_id = call.get("call_id")
                if isinstance(call_id, str) and call_id not in self.seen:
                    self.seen.add(call_id)
                    unique.append(call)
            return unique
        return []
