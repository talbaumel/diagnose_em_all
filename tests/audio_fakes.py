"""Deterministic playback state for runtime tests without audio hardware."""

import asyncio


async def until(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Timed out waiting for voice runtime state")


class ControlledSink:
    def __init__(self):
        self.history = []
        self.ready = False
        self.done = False
        self.heard_ms = 0

    def begin(self, pcm):
        self.history.append(pcm)
        self.ready = self.done = False
        self.heard_ms = 0

    def state(self):
        return self.ready, self.done, self.heard_ms

    def abort(self):
        self.ready = self.done = False

    def close(self):
        pass
