"""Single output owner, with DAC-clock completion rather than write completion."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import sounddevice as sd
from src.cue_timing import CueInsertion

RATE = 24_000
BYTES_PER_SECOND = RATE * 2
MAX_AUDIO_BYTES = BYTES_PER_SECOND * 60


class AudioPlaybackError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    generation: int
    response_id: str
    item_id: str
    pcm: bytes
    caption: str
    kind: str = "speech"
    content_index: int = 0
    insertion: CueInsertion | None = None
    cue_id: str | None = None
    cue_caption: str = ""
    spontaneous: bool = False
    event: str | None = None
    allow_skill: bool = False
    source_pcm: bytes | None = None
    cue_turn: int | None = None

    def source_ms(self, heard_ms: int) -> int:
        if self.insertion is None:
            return heard_ms
        return self.insertion.source_sample_at(min(len(self.pcm) // 2, heard_ms * 24)) // 24


class AudioSink(Protocol):
    def begin(self, pcm: bytes) -> None: ...
    def state(self) -> tuple[bool, bool, int]: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


class DeviceSink:
    """The callback only moves PCM and timestamps under a lock; no UI/async calls.

    All stream lifecycle calls belong to the event-loop thread. abort() runs
    outside the callback lock, waits for callbacks to finish, then clears state.
    Thus a cancelled callback can never refill a restarted stream.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pcm = b""
        self._offset = 0
        self._start: float | None = None
        self._end: float | None = None
        self._error: str | None = None
        try:
            self.stream = sd.RawOutputStream(
                samplerate=RATE, channels=1, dtype="int16", blocksize=480,
                callback=self._callback,
            )
            self.stream.start()
        except (sd.PortAudioError, OSError) as error:
            if hasattr(self, "stream"):
                self.stream.close()
            raise AudioPlaybackError(f"Audio output unavailable: {error}") from error

    def _callback(self, output, frames, timing, status) -> None:
        output[:] = b"\0" * len(output)
        with self._lock:
            if status:
                self._error = f"Audio output underrun/status: {status}"
            count = min(len(output), len(self._pcm) - self._offset)
            if count:
                if self._start is None:
                    self._start = timing.outputBufferDacTime
                output[:count] = self._pcm[self._offset:self._offset + count]
                self._offset += count
                self._end = timing.outputBufferDacTime + count / BYTES_PER_SECOND

    def begin(self, pcm: bytes) -> None:
        with self._lock:
            self._pcm = pcm
            self._offset = 0
            self._start = self._end = None

    def state(self) -> tuple[bool, bool, int]:
        try:
            if not self.stream.active:
                raise AudioPlaybackError("Audio output device stopped; select an output device and retry.")
            now = self.stream.time
            with self._lock:
                if self._error:
                    raise AudioPlaybackError(self._error)
                started = self._start is not None and now >= self._start
                heard = min(len(self._pcm), max(0, int((now - self._start) * BYTES_PER_SECOND))) if started and self._start is not None else 0
                drained = self._offset == len(self._pcm) and self._end is not None and now >= self._end
                return started, drained, heard // 48
        except sd.PortAudioError as error:
            raise AudioPlaybackError(f"Audio output unavailable: {error}") from error

    def abort(self) -> None:
        try:
            self.stream.abort()
            with self._lock:
                self._pcm = b""
                self._offset = 0
                self._start = self._end = None
            self.stream.start()
        except sd.PortAudioError as error:
            raise AudioPlaybackError(f"Audio output unavailable: {error}") from error

    def close(self) -> None:
        try:
            self.stream.abort()
        finally:
            self.stream.close()


class PlaybackController:
    def __init__(self, sink: AudioSink, on_start: Callable[[Segment], None], on_end: Callable[[Segment, str], None],
                 on_cue: Callable[[Segment, bool], None] | None = None,
                 prepare: Callable[[Segment], Segment | None] | None = None,
                 can_start: Callable[[Segment], bool] | None = None) -> None:
        self.sink = sink
        self.on_start = on_start
        self.on_end = on_end
        self.generation = 0
        self.queue: asyncio.Queue[tuple[Segment, asyncio.Future[str]]] = asyncio.Queue(maxsize=32)
        self.queued_bytes = 0
        self.active: tuple[Segment, asyncio.Future[str]] | None = None
        self.started = False
        self.on_cue = on_cue
        self.cue_started = False
        self.cue_ended = False
        self.prepare = prepare
        self.can_start = can_start

    def _cue_progress(self, segment: Segment, heard_ms: int) -> None:
        if segment.insertion is None or self.on_cue is None:
            return
        span = segment.insertion.spans[1]
        if heard_ms * 24 >= span.output_start and not self.cue_started:
            self.cue_started = True
            self.on_cue(segment, True)
        if heard_ms * 24 >= span.output_end and self.cue_started and not self.cue_ended:
            self.cue_ended = True
            self.on_cue(segment, False)

    def enqueue(self, segment: Segment) -> asyncio.Future[str]:
        result = asyncio.get_running_loop().create_future()
        if segment.generation != self.generation:
            result.set_result("skipped")
            return result
        if not segment.pcm or len(segment.pcm) % 2:
            raise AudioPlaybackError("Invalid or empty PCM output segment")
        if self.queue.full() or self.queued_bytes + len(segment.pcm) > MAX_AUDIO_BYTES:
            raise AudioPlaybackError("Audio output queue overflow; please retry with shorter replies")
        self.queue.put_nowait((segment, result))
        self.queued_bytes += len(segment.pcm)
        return result

    def interrupt(self) -> list[dict]:
        """Invalidate first, abort device buffering, then settle every old ticket."""
        self.generation += 1
        truncations = []
        if self.active is not None:
            segment, result = self.active
            audible, _, heard_ms = self.sink.state()
            if audible and not self.started:
                self.started = True
                self.on_start(segment)
            self._cue_progress(segment, heard_ms)
            if segment.kind == "speech":
                truncations.append({
                    "type": "conversation.item.truncate", "item_id": segment.item_id,
                    "content_index": segment.content_index, "audio_end_ms": segment.source_ms(heard_ms),
                })
            self.sink.abort()
            if not result.done():
                result.set_result("interrupted" if self.started or heard_ms else "skipped")
            if self.started:
                self.on_end(segment, "interrupted")
            self.active = None
            self.started = False
            self.cue_started = self.cue_ended = False
        else:
            self.sink.abort()
        while not self.queue.empty():
            segment, result = self.queue.get_nowait()
            if segment.kind == "speech":
                truncations.append({
                    "type": "conversation.item.truncate", "item_id": segment.item_id,
                    "content_index": segment.content_index, "audio_end_ms": 0,
                })
            if not result.done():
                result.set_result("skipped")
        self.queued_bytes = 0
        return truncations

    async def run(self) -> None:
        try:
            while True:
                segment, result = await self.queue.get()
                original_bytes = len(segment.pcm)
                prepared = self.prepare(segment) if self.prepare and not result.done() else segment
                if result.done() or segment.generation != self.generation or prepared is None:
                    self.queued_bytes -= original_bytes
                    if not result.done():
                        result.set_result("skipped")
                    continue
                segment = prepared
                self.active = (segment, result)
                self.started = False
                self.cue_started = self.cue_ended = False
                self.sink.begin(segment.pcm)
                while segment.generation == self.generation:
                    if result.cancelled():
                        self.sink.abort()
                        if self.started:
                            self.on_end(segment, "interrupted")
                        self.active = None
                        self.queued_bytes -= original_bytes
                        break
                    started, drained, heard_ms = self.sink.state()
                    if not started and not self.started and self.can_start and not self.can_start(segment):
                        self.sink.abort()
                        result.set_result("skipped")
                        self.active = None
                        self.queued_bytes -= original_bytes
                        break
                    if started and not self.started:
                        self.started = True
                        self.on_start(segment)
                    if started:
                        self._cue_progress(segment, heard_ms)
                    if drained:
                        self.on_end(segment, "played")
                        result.set_result("played")
                        self.active = None
                        self.queued_bytes -= original_bytes
                        break
                    await asyncio.sleep(0.005)
        finally:
            self.interrupt()
