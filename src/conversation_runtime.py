"""Realtime event routing; network reception never waits for speaker playback."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from src.audio_playback import AudioPlaybackError, MAX_AUDIO_BYTES, PlaybackController, Segment
from src.patient_performance import CoughPolicy, PerformanceProfile, read_pcm_clip
from src.speech_processor import SpeechProcessor
from src.cue_catalog import load_catalog
from src.cue_timing import internal_pauses, insert_cue

LOGGER = logging.getLogger(__name__)
EvidencePresenter = Callable[[object], Awaitable[dict]]
SkillHandler = Callable[[dict, Callable[[], bool], EvidencePresenter], Coroutine[Any, Any, dict]]


def _task_is_cancelling() -> bool:
    task = asyncio.current_task()
    if task is None:
        return False
    cancelling = getattr(task, "cancelling", None)
    return bool(cancelling()) if cancelling is not None else bool(getattr(task, "_must_cancel", False))


@dataclass
class Response:
    id: str
    generation: int
    parts: dict[tuple[str, int], dict] = field(default_factory=dict)
    tickets: list[asyncio.Future] = field(default_factory=list)
    calls: dict[str, dict] = field(default_factory=dict)
    finished: bool = False
    cancelled: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(eq=False)
class PendingSpeech:
    segment: Segment
    ticket: asyncio.Future[str]


class ConversationRuntime:
    def __init__(self, websocket, animator, stop, tests_by_tool, sink, *,
                 profile: PerformanceProfile | None = None, clock=time.monotonic,
                 speech_processor: Callable[[bytes], Awaitable[bytes]] | None = None,
                 skill_tools: dict[str, str] | None = None,
                 skill_handler: SkillHandler | None = None):
        self.websocket = websocket
        self.animator = animator
        self.stop = stop
        self.tests = tests_by_tool
        self.skill_tools = skill_tools or {}
        self.skill_handler = skill_handler
        self.seen_calls: set[str] = set()
        self.seen_responses: set[str] = set()
        self.pending_skills: set[str] = set()
        self.skill_task: asyncio.Task | None = None
        self.profile = profile
        self.inline_cues = profile is not None and profile.cues is not None
        self.speech_processor = speech_processor
        self._owned_speech_processor: SpeechProcessor | None = None
        if self.speech_processor is None and profile and profile.voice and profile.voice.enabled:
            self._owned_speech_processor = SpeechProcessor(profile.voice, persistent=True)
            self.speech_processor = self._owned_speech_processor
        self.policy = CoughPolicy(profile, clock) if profile else None
        try:
            self.cough_pcm = read_pcm_clip(profile.clip_path) if profile else b""
            self.catalog = load_catalog() if self.inline_cues else {}
            self.cue_audio = {}
            if profile and profile.cues is not None:
                for choice in profile.cues:
                    cue = self.catalog[choice.id]
                    cue.verify()
                    self.cue_audio[choice.id] = read_pcm_clip(cue.path)
        except (OSError, ValueError, EOFError) as error:
            raise AudioPlaybackError(f"Cough audio unavailable: {error}") from error
        self.playback = PlaybackController(sink, self._started, self._ended, self._cue_marker)
        self.responses: dict[str, Response] = {}
        self.boundaries: asyncio.Queue[Response] = asyncio.Queue(maxsize=16)
        self.user_requests: asyncio.Queue[tuple[int, str | None]] = asyncio.Queue(maxsize=8)
        self.idle = asyncio.Event()
        self.idle.set()
        self.requested_generation: int | None = None
        self.active_id: str | None = None
        self.user_talking = False
        self.buffered_bytes = 0
        self.continuations = 0
        self.truncate_supported = True
        self.truncate_events: set[str] = set()
        self._truncate_serial = 0
        self.speech_queue: asyncio.Queue[PendingSpeech] = asyncio.Queue(maxsize=32)
        self.pending_speech: list[PendingSpeech] = []
        self.processing_bytes = 0
        self.processing_task: asyncio.Future | None = None
        self.active_cue_segment: Segment | None = None

    @property
    def blocked(self) -> bool:
        return bool(self.user_talking or self.animator.push_to_talk or self.animator.evidence_open
                    or self.animator.won or self.animator._menu is not None or self.stop.is_set()
                    or self.pending_skills
                    or getattr(self.animator, "_diagnosis_open", False) is True
                    or getattr(self.animator, "skills_modal", False) is True
                    or getattr(getattr(self.animator, "_pokedex", None), "open", False) is True)

    def _is_skill_call(self, name: str) -> bool:
        return self.skill_handler is not None and isinstance(name, str) and (
            name in self.skill_tools or name == "get_skill_context" or name.startswith("propose_")
        )

    def _release_skills(self, response: Response) -> None:
        self.pending_skills.discard(response.id)
        if self.skill_handler is not None:
            self.animator._skills_busy = bool(self.pending_skills)

    async def send(self, event: dict) -> None:
        await self.websocket.send(json.dumps(event))

    def _started(self, segment: Segment) -> None:
        self.animator.add_transcript("Patient", segment.caption, f"{segment.item_id}:{segment.content_index}")
        self.animator.set_talking(segment.kind == "speech")
        if segment.kind == "cough" or segment.kind == "cue":
            LOGGER.info("Audible requested/standalone cue %s in %s", segment.cue_id or segment.kind, segment.response_id)
            self.animator._state = "worried"
            if self.policy:
                self.policy.started(segment.cue_id)

    def _cue_marker(self, segment: Segment, starting: bool) -> None:
        LOGGER.info("Audible cue %s %s in %s", segment.cue_id, "start" if starting else "end", segment.response_id)
        if starting:
            self.active_cue_segment = segment
            self.animator.set_talking(False)
            self.animator._state = "worried"
            self.animator.add_transcript("Patient", segment.cue_caption,
                                         f"{segment.item_id}:{segment.content_index}:cue")
            if self.policy:
                self.policy.started(segment.cue_id)
        else:
            self.active_cue_segment = None
            self.animator.set_talking(True)
            # Full segment transcript, not fabricated word-level alignment.
            self.animator.add_transcript("Patient", segment.caption, f"{segment.item_id}:{segment.content_index}")

    def _ended(self, segment: Segment, status: str) -> None:
        self.animator.set_talking(False)
        if status == "interrupted":
            # A full caption denotes a segment, not word alignment. Don't leave
            # its unspoken remainder displayed as if the patient said it.
            text = "[cue interrupted]" if self.active_cue_segment is segment or segment.kind == "cue" else (
                "[cough interrupted]" if segment.kind == "cough" else "[speech interrupted]"
            )
            self.animator.add_transcript("Patient", text, f"{segment.item_id}:{segment.content_index}")
        self.active_cue_segment = None

    async def interrupt(self, *, new_turn: bool = False) -> None:
        truncations = self.playback.interrupt()
        if self.skill_task is not None:
            self.skill_task.cancel()
        for job in self.pending_speech:
            truncations.append({
                "type": "conversation.item.truncate", "item_id": job.segment.item_id,
                "content_index": job.segment.content_index, "audio_end_ms": 0,
            })
        self._clear_processing()
        for response in self.responses.values():
            for (item_id, content_index), part in response.parts.items():
                if part["pcm"]:
                    truncations.append({
                        "type": "conversation.item.truncate", "item_id": item_id,
                        "content_index": content_index, "audio_end_ms": 0,
                    })
            response.parts.clear()
        self.buffered_bytes = 0
        if new_turn:
            self.continuations = 0
            if self.policy:
                self.policy.new_turn()
        if not self.idle.is_set():
            event = {"type": "response.cancel"}
            if self.active_id:
                event["response_id"] = self.active_id
            await self.send(event)
        if self.truncate_supported:
            for event in truncations:
                self._truncate_serial += 1
                event_id = f"truncate-{self._truncate_serial}"
                event["event_id"] = event_id
                self.truncate_events.add(event_id)
                await self.send(event)

    def _clear_processing(self) -> None:
        if self.processing_task is not None:
            self.processing_task.cancel()
        for job in self.pending_speech:
            if not job.ticket.done():
                job.ticket.set_result("skipped")
        self.pending_speech.clear()
        self.processing_bytes = 0
        while not self.speech_queue.empty():
            self.speech_queue.get_nowait()

    async def process_speech(self) -> None:
        """One processor at a time, independent of WebSocket reception and DAC playback."""
        try:
            while True:
                job = await self.speech_queue.get()
                segment = job.segment
                forwarded = False
                try:
                    if segment.generation != self.playback.generation:
                        continue
                    async def prepare() -> bytes:
                        audio = (await self.speech_processor(segment.pcm)
                                 if self.speech_processor is not None else segment.pcm)
                        if self.inline_cues:
                            # Wait for all tool calls before choosing a spontaneous action.
                            response = self.responses.get(segment.response_id)
                            if response is not None:
                                await response.ready.wait()
                        return audio
                    self.processing_task = asyncio.create_task(prepare())
                    try:
                        audio = await asyncio.shield(self.processing_task)
                    except asyncio.CancelledError:
                        if not self.processing_task.done():
                            self.processing_task.cancel()
                            await asyncio.gather(self.processing_task, return_exceptions=True)
                            raise
                        # An interruption cancelled the old utterance, not the processor loop.
                        continue
                    if segment.generation != self.playback.generation or self.stop.is_set():
                        continue
                    if len(audio) != len(segment.pcm):
                        raise AudioPlaybackError("NPC speech processing changed duration")
                    insertion = None
                    cue_id = None
                    response = self.responses.get(segment.response_id)
                    if self.inline_cues and response is not None:
                        if response.cancelled:
                            continue
                        if (not response.calls and not self.continuations and not self.blocked
                                and self.profile and self.profile.cues and self.policy):
                            # Pure bounded analysis, off the event loop. Old results are generation-checked.
                            self.processing_task = asyncio.create_task(asyncio.to_thread(internal_pauses, audio))
                            try:
                                pauses = await asyncio.shield(self.processing_task)
                            except asyncio.CancelledError:
                                if not self.processing_task.done():
                                    self.processing_task.cancel()
                                    await asyncio.gather(self.processing_task, return_exceptions=True)
                                    raise
                                continue
                            except ValueError as error:
                                raise AudioPlaybackError(f"Cannot plan NPC cue timing: {error}") from error
                            if segment.generation != self.playback.generation or self.blocked:
                                continue
                            if pauses:
                                cue_id = self.policy.choose(self.profile.cues)
                                if cue_id and self.policy.reserve(spontaneous=True, blocked=self.blocked):
                                    pause = max(pauses, key=lambda gap: gap.end-gap.start)
                                    self.processing_task = asyncio.create_task(asyncio.to_thread(
                                        insert_cue, audio, self.cue_audio[cue_id], pause,
                                    ))
                                    try:
                                        insertion = await asyncio.shield(self.processing_task)
                                    except asyncio.CancelledError:
                                        if not self.processing_task.done():
                                            self.processing_task.cancel()
                                            await asyncio.gather(self.processing_task, return_exceptions=True)
                                            raise
                                        continue
                                    except ValueError as error:
                                        raise AudioPlaybackError(f"Cannot insert NPC cue: {error}") from error
                                    if segment.generation != self.playback.generation or self.blocked:
                                        continue
                                    audio = insertion.pcm
                                    LOGGER.info("Inline cue %s in response %s at %.3fs",
                                                cue_id, response.id, insertion.spans[1].output_start / 24000)
                                else:
                                    cue_id = None
                            else:
                                LOGGER.info("No internal pause for spontaneous cue in %s; skipping", response.id)
                    if len(audio) + self.buffered_bytes + self.playback.queued_bytes + (
                        self.processing_bytes - len(segment.pcm)
                    ) > MAX_AUDIO_BYTES:
                        raise AudioPlaybackError("Inline cue exceeds patient audio buffer budget")
                    playback_ticket = self.playback.enqueue(Segment(
                        segment.generation, segment.response_id, segment.item_id,
                        audio, segment.caption, segment.kind, segment.content_index,
                        insertion, cue_id, self.catalog[cue_id].caption if cue_id else "",
                    ))
                    def completed(ticket: asyncio.Future[str], result=job.ticket) -> None:
                        if not result.done():
                            if ticket.cancelled():
                                result.cancel()
                            else:
                                result.set_result(ticket.result())
                    playback_ticket.add_done_callback(completed)
                    forwarded = True
                finally:
                    self.processing_task = None
                    if job in self.pending_speech:
                        self.pending_speech.remove(job)
                        self.processing_bytes -= len(segment.pcm)
                    if not forwarded and not job.ticket.done():
                        job.ticket.set_result("skipped")
        finally:
            self._clear_processing()
            if self._owned_speech_processor is not None:
                await self._owned_speech_processor.aclose()

    def queue_user(self, text: str | None) -> None:
        try:
            self.user_requests.put_nowait((self.playback.generation, text))
        except asyncio.QueueFull as error:
            raise AudioPlaybackError("Too many queued clinician turns; please retry") from error

    async def request_response(self, generation: int, *, continuation: bool = False,
                               allow_tools: bool = False) -> None:
        while True:
            if generation != self.playback.generation or self.animator.won or self.stop.is_set():
                return
            await self.idle.wait()
            if generation != self.playback.generation or self.animator.won or self.stop.is_set():
                return
            if not self.blocked:
                break
            if not continuation:
                return
            # Keep the completed batch's continuation while a local modal is
            # open. A new turn or shutdown invalidates it instead of replaying it.
            await asyncio.sleep(0.005)
        self.idle.clear()
        self.requested_generation = generation
        response = {}
        if continuation:
            # Context lookup may precede a proposal; the final spoken follow-up
            # cannot recursively order more procedures or performance cues.
            response["tool_choice"] = "auto" if allow_tools else "none"
        await self.send({"type": "response.create", "response": response})

    async def send_user_requests(self) -> None:
        while True:
            generation, text = await self.user_requests.get()
            await self.idle.wait()
            while (
                generation == self.playback.generation and self.blocked
                and not self.animator.won and not self.stop.is_set()
            ):
                await asyncio.sleep(0.005)
            if generation != self.playback.generation or self.blocked:
                continue
            if text is not None:
                await self.send({
                    "type": "conversation.item.create",
                    "item": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": text}]},
                })
            await self.request_response(generation)

    def _flush_part(self, response: Response, key: tuple[str, int]) -> None:
        part = response.parts.pop(key, None)
        if not part or not part["pcm"]:
            return
        audio = bytes(part["pcm"])
        self.buffered_bytes -= len(audio)
        if response.generation != self.playback.generation:
            return
        if not part["text"].strip():
            raise AudioPlaybackError("Patient audio arrived without a matching segment transcript")
        segment = Segment(
            response.generation, response.id, key[0], audio, part["text"],
            content_index=key[1],
        )
        if self.speech_processor is None and not self.inline_cues:
            response.tickets.append(self.playback.enqueue(segment))
        else:
            if self.speech_queue.full():
                raise AudioPlaybackError("NPC voice processing queue overflow")
            ticket = asyncio.get_running_loop().create_future()
            job = PendingSpeech(segment, ticket)
            self.speech_queue.put_nowait(job)
            self.pending_speech.append(job)
            self.processing_bytes += len(audio)
            response.tickets.append(ticket)

    def handle(self, event: dict) -> None:
        """Called by the receiver. Only bounded memory/queue operations here."""
        kind = event.get("type")
        if kind == "response.created":
            response_id = event["response"]["id"]
            if response_id in self.seen_responses:
                return
            if self.requested_generation is None or self.active_id is not None:
                raise AudioPlaybackError("Unexpected concurrent patient response")
            self.seen_responses.add(response_id)
            self.active_id = response_id
            self.responses[response_id] = Response(response_id, self.requested_generation)
            return
        response_id = event.get("response_id") or event.get("response", {}).get("id")
        response = self.responses.get(response_id)
        if response is None or response.finished:
            return
        if kind == "response.done":
            response.finished = True
            response.ready.set()
            if response_id == self.active_id:
                self.active_id = None
                self.requested_generation = None
                self.idle.set()
            status = event.get("response", {}).get("status", "completed")
            if status not in ("completed", "cancelled"):
                raise AudioPlaybackError(f"Patient response {status}; retry the consultation")
            if status == "cancelled":
                response.cancelled = True
                for part in response.parts.values():
                    self.buffered_bytes -= len(part["pcm"])
                response.parts.clear()
                response.calls.clear()
            else:
                # The terminal output is authoritative, not a partial arguments
                # event. Legacy cue streams can omit output; skills never may.
                output = event.get("response", {}).get("output")
                candidates = (
                    [call for call in output if call.get("type") == "function_call"]
                    if output is not None else [
                        call for call in response.calls.values()
                        if not self._is_skill_call(call.get("name", ""))
                    ]
                )
                response.calls.clear()
                for call in candidates:
                    call_id = call.get("call_id")
                    if not isinstance(call_id, str) or not isinstance(call.get("name"), str):
                        continue
                    if call_id in self.seen_calls:
                        continue
                    if self._is_skill_call(call["name"]) and event["response"].get("status") != "completed":
                        continue
                    if len(response.calls) >= 8:
                        raise AudioPlaybackError("Too many patient tool calls")
                    self.seen_calls.add(call_id)
                    response.calls[call_id] = call
                if response.generation == self.playback.generation:
                    if any(self._is_skill_call(call["name"]) for call in response.calls.values()):
                        self.pending_skills.add(response.id)
                        self.animator._skills_busy = True
                        self.animator._push_to_talk.clear()
                    for key in tuple(response.parts):
                        self._flush_part(response, key)
            try:
                self.boundaries.put_nowait(response)
            except asyncio.QueueFull as error:
                raise AudioPlaybackError("Patient response queue overflow") from error
            return
        if response.generation != self.playback.generation:
            return
        if kind == "response.function_call_arguments.done":
            if len(response.calls) >= 8 and event["call_id"] not in response.calls:
                raise AudioPlaybackError("Too many patient tool calls")
            response.calls[event["call_id"]] = event
            return
        if kind in ("response.output_audio.delta", "response.output_audio_transcript.delta",
                    "response.output_audio_transcript.done"):
            key = (event["item_id"], event.get("content_index", 0))
            if len(response.parts) >= 32 and key not in response.parts:
                raise AudioPlaybackError("Too many patient audio segments")
            part = response.parts.setdefault(key, {"pcm": bytearray(), "text": ""})
            if kind == "response.output_audio.delta":
                try:
                    pcm = base64.b64decode(event["delta"], validate=True)
                except (binascii.Error, ValueError) as error:
                    raise AudioPlaybackError("Invalid patient PCM encoding") from error
                if len(pcm) % 2:
                    raise AudioPlaybackError("Invalid patient PCM frame")
                self.buffered_bytes += len(pcm)
                if self.buffered_bytes + self.playback.queued_bytes + self.processing_bytes > MAX_AUDIO_BYTES:
                    raise AudioPlaybackError("Patient audio buffer overflow")
                part["pcm"].extend(pcm)
            elif kind.endswith(".delta"):
                part["text"] += event.get("delta", "")
            else:
                part["text"] = event.get("transcript", "")
            if len(part["text"]) > 32_000:
                raise AudioPlaybackError("Patient transcript buffer overflow")
        elif kind == "response.content_part.done":
            self._flush_part(response, (event["item_id"], event.get("content_index", 0)))

    async def tool_result(self, call_id: str, output: dict) -> None:
        await self.send({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id,
                     "output": json.dumps(output)},
        })

    async def cough(self, response: Response, *, spontaneous: bool = False) -> str:
        if self.inline_cues:
            return "skipped" if spontaneous else await self.requested_cue(response, "cough")
        if not self.policy or response.generation != self.playback.generation:
            return "skipped"
        if not self.policy.reserve(spontaneous=spontaneous, blocked=self.blocked):
            return "skipped"
        return await self.playback.enqueue(Segment(
            response.generation, response.id, f"{response.id}:cough", self.cough_pcm,
            "[coughs]", "cough",
        ))

    async def requested_cue(self, response: Response, kind: str) -> str:
        if (not self.profile or self.profile.cues is None or not self.policy
                or response.generation != self.playback.generation):
            return "skipped"
        cue_id = self.policy.choose(self.profile.cues, kind=kind)
        if not cue_id or not self.policy.reserve(blocked=self.blocked):
            LOGGER.info("Requested %s skipped: unavailable, blocked, cooldown or already used this turn", kind)
            return "skipped"
        cue = self.catalog[cue_id]
        return await self.playback.enqueue(Segment(
            response.generation, response.id, f"{response.id}:cue", self.cue_audio[cue_id],
            cue.caption, "cue", cue_id=cue_id,
        ))

    async def present_evidence(self, response: Response, call_id: str, test) -> dict:
        self.animator.show_test_result(test)
        result = {}
        close = None
        try:
            if test.audio_path is not None:
                try:
                    pcm = read_pcm_clip(test.audio_path)
                except (OSError, ValueError) as error:
                    raise AudioPlaybackError(f"Test audio unavailable: {error}") from error
                ticket = self.playback.enqueue(Segment(
                    response.generation, response.id, call_id, pcm, "[test audio]", "evidence",
                ))
                close = asyncio.create_task(self.animator.wait_for_evidence_close())
                done, _ = await asyncio.wait({ticket, close}, return_when=asyncio.FIRST_COMPLETED)
                if close in done and not ticket.done():
                    # Dismissing a report stops its sound, not the clinician's turn.
                    self.playback.interrupt()
                    response.generation = self.playback.generation
                result["audio_status"] = await ticket
            await self.animator.wait_for_evidence_close()
            return result
        finally:
            if close is not None:
                close.cancel()
                await asyncio.gather(close, return_exceptions=True)

    async def finish_responses(self) -> None:
        while True:
            response = await self.boundaries.get()
            try:
                if response.cancelled:
                    if response.tickets:
                        await asyncio.gather(*response.tickets)
                    continue
                calls = list(response.calls.values())
                calls.sort(key=lambda call: call["name"] != "propose_you_win")
                diagnostic = any(
                    call["name"] in self.tests or self._is_skill_call(call["name"])
                    and call["name"] != "get_skill_context" for call in calls
                )
                context_only = bool(calls) and all(call["name"] == "get_skill_context" for call in calls)
                for call in calls:
                    name = call["name"]
                    if name != "propose_you_win" and response.tickets and not self.animator.won:
                        await asyncio.gather(*response.tickets)
                    current = response.generation == self.playback.generation
                    if not current or self.stop.is_set() or self.animator.won:
                        result = (
                            {"status": "cancelled", "result": "Response interrupted before action.", "points": 0}
                            if self._is_skill_call(name) else {"status": "skipped"}
                        )
                        await self.tool_result(call["call_id"], result)
                    elif self._is_skill_call(name):
                        handler = self.skill_handler
                        if handler is None:
                            raise RuntimeError("Skill handler is unavailable")
                        self.skill_task = asyncio.create_task(handler(
                            call, lambda: response.generation == self.playback.generation
                            and not self.stop.is_set() and not self.animator.won,
                            lambda test: self.present_evidence(response, call["call_id"], test),
                        ))
                        try:
                            result = await asyncio.shield(self.skill_task)
                            if _task_is_cancelling():
                                raise asyncio.CancelledError
                        except asyncio.CancelledError:
                            if _task_is_cancelling() or not self.skill_task.done():
                                self.skill_task.cancel()
                                await asyncio.gather(self.skill_task, return_exceptions=True)
                                raise
                            result = {"status": "cancelled", "result": "Response interrupted before action.", "points": 0}
                        finally:
                            self.skill_task = None
                        await self.tool_result(call["call_id"], result)
                        if name == "propose_you_win" and self.animator.won:
                            await self.interrupt()
                    elif name in self.tests:
                        test = self.tests[name]
                        self.animator.metrics.discover_test(
                            test.description, test.results
                        )
                        result = {"test": test.description, "result": test.results}
                        result.update(await self.present_evidence(response, call["call_id"], test))
                        await self.tool_result(call["call_id"], result)
                    elif self.inline_cues and any(
                        cue.kind == name and cue.id in self.cue_audio for cue in self.catalog.values()
                    ):
                        status = "skipped" if diagnostic else await self.requested_cue(response, name)
                        await self.tool_result(call["call_id"], {"status": status})
                    elif name == "cough" and self.profile:
                        status = "skipped" if diagnostic else await self.cough(response)
                        await self.tool_result(call["call_id"], {"status": status})
                    else:
                        await self.tool_result(call["call_id"], {"status": "skipped", "reason": "unknown tool"})
                if response.tickets and not self.animator.won:
                    await asyncio.gather(*response.tickets)
                self._release_skills(response)
                if response.generation != self.playback.generation or self.animator.won or self.stop.is_set():
                    continue
                if calls:
                    if self.continuations < 2:
                        allow_tools = context_only and self.continuations == 0
                        self.continuations += 1
                        await self.request_response(response.generation, continuation=True, allow_tools=allow_tools)
                elif self.continuations == 0 and response.tickets:
                    await self.cough(response, spontaneous=True)
            finally:
                self._release_skills(response)
                self.responses.pop(response.id, None)
