"""Pause-safe PCM cue placement and source timing; no model, device or network access."""

from __future__ import annotations

import math
from array import array
from dataclasses import asdict, dataclass
import sys

RATE = 24_000
MAX_SPEECH_SAMPLES = RATE * 30


def samples(pcm: bytes, maximum: int) -> array:
    if not pcm or len(pcm) % 2 or len(pcm) // 2 > maximum:
        raise ValueError("Expected nonempty, bounded mono 24 kHz PCM16 audio")
    result = array("h")
    result.frombytes(pcm)
    if sys.byteorder != "little":
        result.byteswap()
    return result


@dataclass(frozen=True)
class Pause:
    start: int
    end: int


@dataclass(frozen=True)
class TimingSpan:
    kind: str
    output_start: int
    output_end: int
    source_start: int
    source_end: int


@dataclass(frozen=True)
class CueInsertion:
    pcm: bytes
    pause: Pause
    removed_quiet: Pause
    spans: tuple[TimingSpan, ...]

    def source_sample_at(self, output_sample: int) -> int:
        if type(output_sample) is not int or not 0 <= output_sample <= len(self.pcm) // 2:
            raise ValueError("Output position is outside this utterance")
        if output_sample == len(self.pcm) // 2:
            return self.spans[-1].source_end
        for span in self.spans:
            if span.output_start <= output_sample < span.output_end:
                if span.kind == "cue":
                    return span.source_start
                return span.source_start + output_sample - span.output_start
        raise ValueError("Timing map does not cover output position")

    def metadata(self) -> dict:
        return {
            "sample_rate": RATE,
            "pause": asdict(self.pause), "removed_quiet": asdict(self.removed_quiet),
            "spans": [asdict(span) for span in self.spans],
            "mapping_note": "Speech spans map linearly to source samples. During the local cue, "
                            "source time holds at source_start; on resumed speech it skips only "
                            "the removed quiet interval. This is not word alignment.",
        }


def quiet_limit(threshold_dbfs: float) -> int:
    if (isinstance(threshold_dbfs, bool) or not isinstance(threshold_dbfs, (int, float))
            or not math.isfinite(threshold_dbfs) or not -60 <= threshold_dbfs <= -35):
        raise ValueError("Quiet threshold must be finite and between -60 and -35 dBFS")
    return int(32768 * 10 ** (threshold_dbfs / 20))


def internal_pauses(
    pcm: bytes, *, threshold_dbfs: float = -42, minimum_ms: int = 180,
) -> tuple[Pause, ...]:
    if type(minimum_ms) is not int or not 160 <= minimum_ms <= 1000:
        raise ValueError("Minimum pause must be an integer between 160 and 1000 ms")
    speech = samples(pcm, MAX_SPEECH_SAMPLES)
    limit = quiet_limit(threshold_dbfs)
    minimum = math.ceil(RATE * minimum_ms / 1000)
    active = [index for index, value in enumerate(speech) if abs(value) > limit]
    if not active:
        return ()
    first, last = active[0], active[-1]
    pauses = []
    start = 0
    for index in range(len(speech) + 1):
        if index < len(speech) and abs(speech[index]) <= limit:
            continue
        if index - start >= minimum and start > first and index <= last:
            pauses.append(Pause(start, index))
        start = index + 1
    return tuple(pauses)


def insert_cue(
    speech_pcm: bytes, cue_pcm: bytes, pause: Pause, *,
    threshold_dbfs: float = -42, margin_ms: int = 50,
) -> CueInsertion:
    speech = samples(speech_pcm, MAX_SPEECH_SAMPLES)
    cue = samples(cue_pcm, RATE * 10)
    limit = quiet_limit(threshold_dbfs)
    if pause not in internal_pauses(speech_pcm, threshold_dbfs=threshold_dbfs):
        raise ValueError("Cue placement must use a detected internal pause, never an arbitrary word offset")
    if type(margin_ms) is not int or not 20 <= margin_ms <= 70:
        raise ValueError("Quiet margin must be an integer between 20 and 70 ms")
    if len(cue) < RATE // 10 or max(abs(value) for value in cue) < 32:
        raise ValueError("Cue must be at least 100 ms and contain audible signal")
    if max(abs(value) for value in cue) >= 32767:
        raise ValueError("Cue reaches full scale")
    if cue[0] or cue[-1]:
        raise ValueError("Cue must have faded zero-valued edges before insertion")
    margin = round(RATE * margin_ms / 1000)
    available_start, available_end = pause.start + margin, pause.end - margin
    # Replace at most the cue length: a short cue must not erase a longer intentional pause.
    replace_count = min(len(cue), available_end - available_start)
    if replace_count <= 0:
        raise ValueError("Internal pause is too short for the requested margins")
    cut_start = available_start + (available_end - available_start - replace_count) // 2
    cut_end = cut_start + replace_count
    if any(abs(value) > limit for value in speech[cut_start:cut_end]):
        raise ValueError("Refusing to remove non-quiet speech")
    # Keep all speech outside the quiet interval bit-exact. Never overlap voice and bodily cues.
    output = speech_pcm[:cut_start * 2] + cue_pcm + speech_pcm[cut_end * 2:]
    cue_end = cut_start + len(cue)
    spans = (
        TimingSpan("speech", 0, cut_start, 0, cut_start),
        TimingSpan("cue", cut_start, cue_end, cut_start, cut_end),
        TimingSpan("speech", cue_end, len(output) // 2, cut_end, len(speech)),
    )
    return CueInsertion(output, pause, Pause(cut_start, cut_end), spans)
