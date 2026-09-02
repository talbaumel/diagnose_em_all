# ruff: noqa: I001

"""Voice conversation client for the Azure OpenAI Realtime API.

Install dependencies with:
    python -m pip install azure-identity pygame sounddevice websockets

Authenticate before running with:
    az login
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pygame
import sounddevice as sd
import websockets
from azure.identity import AzureCliCredential


REALTIME_URL = (
    "wss://tabaumel-resource.openai.azure.com/openai/v1/realtime"
    "?model=gpt-realtime-2.1"
)
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
REALTIME_OPEN_TIMEOUT_SECONDS = 30
REALTIME_CONNECT_ATTEMPTS = 3
SAMPLE_RATE = 24_000
CHANNELS = 1
BLOCK_DURATION_MS = 100
SPRITE_SIZE = 56
ANIMATION_FPS = 8
RELIEVED_DURATION_SECONDS = 5
SPRITE_SHEET = Path(__file__).parents[1] / "data" / "sprites" / "patients.png"
PROJECT_ROOT = Path(__file__).parents[1]
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


class PatientType(str, Enum):
    COMMON_COLD_KID = "Common Cold Kid"
    STOMACHACHE_TEEN = "Stomachache Teen"
    MIGRAINE_SUFFERER = "Migraine Sufferer"
    ALLERGIES_PATIENT = "Allergies Patient"
    SPRAINED_ANKLE_ATHLETE = "Sprained Ankle Athlete"
    ANXIOUS_ADULT = "Anxious Adult"
    FEVERISH_PATIENT = "Feverish Patient"
    RASH_PATIENT = "Rash Patient"
    ELDERLY_WITH_BACK_PAIN = "Elderly with Back Pain"
    SLEEP_DEPRIVED_WORKER = "Sleep-Deprived Worker"


@dataclass(frozen=True)
class Test:
    description: str
    results: str

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("Test description must not be empty")
        if not self.results.strip():
            raise ValueError("Test results must not be empty")

    @property
    def image_path(self) -> Path | None:
        path = Path(self.results).expanduser()
        if path.suffix.casefold() not in IMAGE_SUFFIXES:
            return None
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"Evidence image not found: {path}")
        return path


PATIENT_TYPES = tuple(patient_type.value for patient_type in PatientType)
PATIENT_PANEL_X = (9, 313, 614, 923, 1232)
SPRITE_ROW_Y = (221, 725)
STATE_ROWS = {"idle": 0, "talking": 1, "worried": 2, "relieved": 3}


def _is_inactive_cancellation(error: dict[str, Any]) -> bool:
    message = str(error.get("message", "")).casefold()
    return "cancellation failed" in message and "no active response" in message


def _combine_prompts(system_prompts: str | Sequence[str]) -> str:
    if isinstance(system_prompts, str):
        prompt = system_prompts.strip()
    else:
        prompt = "\n\n".join(part.strip() for part in system_prompts if part.strip())

    if not prompt:
        raise ValueError("system_prompts must contain at least one non-empty prompt")
    return prompt


def _patient_index(patient_type: PatientType) -> int:
    if not isinstance(patient_type, PatientType):
        raise TypeError("patient_type must be a PatientType enum member")
    return list(PatientType).index(patient_type)


def _test_tools(tests: Sequence[Test]) -> tuple[list[dict[str, Any]], dict[str, Test]]:
    tools: list[dict[str, Any]] = []
    tests_by_tool: dict[str, Test] = {}
    for index, test in enumerate(tests, start=1):
        if not isinstance(test, Test):
            raise TypeError("tests must contain only Test instances")
        slug = re.sub(r"[^a-z0-9]+", "_", test.description.casefold()).strip("_")
        tool_name = f"perform_test_{index}_{slug or 'diagnostic'}"[:64]
        tests_by_tool[tool_name] = test
        tools.append(
            {
                "type": "function",
                "name": tool_name,
                "description": (
                    f"Perform the {test.description} test. Call this only when the "
                    "clinician asks to perform this test."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            }
        )
    return tools, tests_by_tool


@asynccontextmanager
async def _realtime_connection(
    headers: dict[str, str],
) -> AsyncIterator[Any]:
    websocket: Any
    for attempt in range(1, REALTIME_CONNECT_ATTEMPTS + 1):
        try:
            websocket = await websockets.connect(
                REALTIME_URL,
                additional_headers=headers,
                max_size=None,
                open_timeout=REALTIME_OPEN_TIMEOUT_SECONDS,
            )
            break
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if attempt == REALTIME_CONNECT_ATTEMPTS:
                raise ConnectionError(
                    "Azure OpenAI Realtime did not complete the WebSocket "
                    f"handshake after {REALTIME_CONNECT_ATTEMPTS} attempts. "
                    "Check the deployment endpoint and network access settings."
                ) from exc
            await asyncio.sleep(attempt)

    try:
        yield websocket
    finally:
        await websocket.close()


class PatientAnimator:
    def __init__(self, patient_index: int) -> None:
        pygame.init()
        pygame.display.set_caption("Patient Conversation")
        self._screen = pygame.display.set_mode((480, 480))
        self._sheet = pygame.image.load(str(SPRITE_SHEET)).convert_alpha()
        self._patient_index = patient_index
        self._state = "idle"
        self._relieved_until = 0.0
        self._test_result: Test | None = None
        self._test_result_image: pygame.Surface | None = None
        self._evidence_closed = asyncio.Event()
        self._evidence_closed.set()
        self._test_close_button = pygame.Rect(340, 35, 68, 28)
        self._won = False
        self._running = True
        self._push_to_talk = threading.Event()
        self._text_messages: asyncio.Queue[str] = asyncio.Queue()
        self._text_input = ""
        self._text_focused = False
        self._text_input_rect = pygame.Rect(20, 435, 370, 34)
        self._text_send_button = pygame.Rect(398, 435, 62, 34)
        self._status_font = pygame.font.SysFont("Avenir Next", 18)
        self._text_font = pygame.font.SysFont("Avenir Next", 16)
        self._test_title_font = pygame.font.SysFont("Avenir Next", 18, bold=True)
        self._test_result_font = pygame.font.SysFont("Avenir Next", 24, bold=True)
        self._win_font = pygame.font.SysFont("Avenir Next", 42, bold=True)

    @property
    def push_to_talk(self) -> bool:
        return self._push_to_talk.is_set()

    @property
    def won(self) -> bool:
        return self._won

    @property
    def evidence_open(self) -> bool:
        return self._test_result is not None

    def set_talking(self, talking: bool) -> None:
        self._state = "talking" if talking else "idle"

    def show_relieved(self) -> None:
        self._relieved_until = time.monotonic() + RELIEVED_DURATION_SECONDS

    def show_win(self) -> None:
        self._won = True
        self.show_relieved()

    def show_test_result(self, test: Test) -> None:
        self._test_result = test
        image_path = test.image_path
        self._test_result_image = (
            pygame.image.load(str(image_path)).convert_alpha() if image_path else None
        )
        self._push_to_talk.clear()
        self._evidence_closed.clear()

    async def wait_for_evidence_close(self) -> None:
        await self._evidence_closed.wait()

    async def next_text_message(self) -> str:
        return await self._text_messages.get()

    def _submit_text(self) -> None:
        message = self._text_input.strip()
        if not message:
            return
        self._text_messages.put_nowait(message)
        self._text_input = ""

    def _set_text_focus(self, focused: bool) -> None:
        self._text_focused = focused
        self._push_to_talk.clear()
        if focused:
            pygame.key.start_text_input()
        else:
            pygame.key.stop_text_input()

    def close_test_result(self) -> None:
        self._test_result = None
        self._test_result_image = None
        self._evidence_closed.set()

    def _current_state(self) -> str:
        if time.monotonic() < self._relieved_until:
            return "relieved"
        return self._state

    def _frame(self, state: str, frame_index: int) -> pygame.Surface:
        column = self._patient_index % 5
        row = self._patient_index // 5
        rectangle = pygame.Rect(
            PATIENT_PANEL_X[column] + 68 + frame_index * SPRITE_SIZE,
            SPRITE_ROW_Y[row] + STATE_ROWS[state] * SPRITE_SIZE,
            SPRITE_SIZE,
            SPRITE_SIZE,
        )
        return self._sheet.subsurface(rectangle)

    def _centered_frame(self, state: str, frame_index: int) -> pygame.Surface:
        frame = self._frame(state, frame_index)
        character_mask = pygame.mask.from_threshold(
            frame,
            pygame.Color(0, 0, 0),
            threshold=pygame.Color(165, 165, 165, 255),
        )
        bounds = character_mask.get_bounding_rects()
        if not bounds:
            return frame

        character_bounds = bounds[0]
        for rectangle in bounds[1:]:
            character_bounds.union_ip(rectangle)
        character_bounds.inflate_ip(4, 4)
        character_bounds = character_bounds.clip(frame.get_rect())
        return frame.subsurface(character_bounds)

    async def run(self, stop: asyncio.Event) -> None:
        frame_index = 0
        frame_interval = 1 / ANIMATION_FPS
        next_frame = time.monotonic()

        while not stop.is_set() and self._running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._running = False
                    stop.set()
                elif event.type == pygame.TEXTINPUT and self._text_focused:
                    self._text_input += event.text
                elif event.type == pygame.KEYDOWN and self._text_focused:
                    if event.key == pygame.K_RETURN:
                        self._submit_text()
                    elif event.key == pygame.K_BACKSPACE:
                        self._text_input = self._text_input[:-1]
                    elif event.key == pygame.K_ESCAPE:
                        self._set_text_focus(False)
                elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_LSHIFT,
                    pygame.K_RSHIFT,
                ):
                    self._push_to_talk.set()
                elif event.type == pygame.KEYUP and event.key in (
                    pygame.K_LSHIFT,
                    pygame.K_RSHIFT,
                ):
                    self._push_to_talk.clear()
                elif (
                    event.type == pygame.MOUSEBUTTONUP
                    and event.button == 1
                ):
                    if self._text_input_rect.collidepoint(event.pos):
                        self._set_text_focus(True)
                    elif self._text_send_button.collidepoint(event.pos):
                        self._submit_text()
                    elif (
                        self._test_result
                        and self._test_close_button.collidepoint(event.pos)
                    ):
                        self.close_test_result()
                    else:
                        self._set_text_focus(False)

            now = time.monotonic()
            if now >= next_frame:
                frame_index = (frame_index + 1) % 4
                next_frame = now + frame_interval

            state = self._current_state()
            frame = self._centered_frame(state, frame_index)
            self._screen.fill((238, 244, 241))
            self._screen.blit(frame, frame.get_rect(center=(240, 235)))

            if self._test_result and not self.won:
                popup = pygame.Rect(30, 25, 420, 365)
                pygame.draw.rect(self._screen, (255, 255, 255), popup)
                pygame.draw.rect(self._screen, (55, 101, 79), popup, width=2)
                title = self._test_title_font.render(
                    self._test_result.description.upper(), True, (55, 101, 79)
                )
                close = self._status_font.render("Close", True, (255, 255, 255))
                pygame.draw.rect(
                    self._screen, (55, 101, 79), self._test_close_button
                )
                self._screen.blit(title, title.get_rect(center=(185, 50)))
                self._screen.blit(
                    close, close.get_rect(center=self._test_close_button.center)
                )
                if self._test_result_image:
                    available_size = (380, 290)
                    image = self._test_result_image
                    scale = min(
                        1.0,
                        available_size[0] / image.get_width(),
                        available_size[1] / image.get_height(),
                    )
                    if scale < 1.0:
                        image = pygame.transform.smoothscale(
                            image,
                            (
                                round(image.get_width() * scale),
                                round(image.get_height() * scale),
                            ),
                        )
                    self._screen.blit(image, image.get_rect(center=(240, 225)))
                else:
                    result = self._test_result_font.render(
                        self._test_result.results, True, (31, 40, 36)
                    )
                    self._screen.blit(result, result.get_rect(center=(240, 210)))

            if self.won:
                status_text = "YOU WIN"
                status = self._win_font.render(status_text, True, (33, 126, 76))
            else:
                status_text = (
                    "EVIDENCE PAUSED"
                    if self.evidence_open
                    else "TYPING"
                    if self._text_focused
                    else "LISTENING"
                    if self.push_to_talk
                    else state.upper()
                )
                status = self._status_font.render(status_text, True, (55, 101, 79))
            self._screen.blit(status, status.get_rect(center=(240, 410)))

            input_border = (33, 126, 76) if self._text_focused else (138, 153, 146)
            pygame.draw.rect(self._screen, (255, 255, 255), self._text_input_rect)
            pygame.draw.rect(
                self._screen, input_border, self._text_input_rect, width=2
            )
            input_text = self._text_input or "Type a message..."
            input_color = (31, 40, 36) if self._text_input else (117, 128, 123)
            rendered_input = self._text_font.render(input_text, True, input_color)
            input_area = self._text_input_rect.inflate(-16, -8)
            input_x = input_area.x
            if rendered_input.get_width() > input_area.width:
                input_x = input_area.right - rendered_input.get_width()
            previous_clip = self._screen.get_clip()
            self._screen.set_clip(input_area)
            self._screen.blit(
                rendered_input,
                rendered_input.get_rect(midleft=(input_x, input_area.centery)),
            )
            self._screen.set_clip(previous_clip)

            pygame.draw.rect(self._screen, (55, 101, 79), self._text_send_button)
            send_text = self._text_font.render("Send", True, (255, 255, 255))
            self._screen.blit(
                send_text, send_text.get_rect(center=self._text_send_button.center)
            )
            pygame.display.flip()
            await asyncio.sleep(1 / 60)

        stop.set()

    def close(self) -> None:
        pygame.key.stop_text_input()
        pygame.quit()


async def _run_conversation(
    system_prompt: str,
    patient_index: int,
    tests: Sequence[Test],
) -> None:
    credential = AzureCliCredential()
    animator: PatientAnimator | None = None
    test_tools, tests_by_tool = _test_tools(tests)
    try:
        token = await asyncio.to_thread(
            credential.get_token,
            AZURE_OPENAI_SCOPE,
        )
        headers = {"Authorization": f"Bearer {token.token}"}
        stop = asyncio.Event()

        async with _realtime_connection(headers) as websocket:
            animator = PatientAnimator(patient_index)
            await websocket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": (
                                f"{system_prompt}\n\n"
                                "When the clinician asks to perform one of the "
                                "available diagnostic tests, call its matching tool "
                                "immediately. Do not describe or invent the test result "
                                "yourself. "
                                "When the clinician correctly diagnoses the patient, "
                                "call win exactly once."
                            ),
                            "output_modalities": ["audio"],
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "win",
                                    "description": (
                                        "Declare that the clinician correctly diagnosed "
                                        "the patient. Call only for the correct diagnosis."
                                    ),
                                    "parameters": {
                                        "type": "object",
                                        "properties": {},
                                        "additionalProperties": False,
                                    },
                                },
                                *test_tools,
                            ],
                            "tool_choice": "auto",
                            "audio": {
                                "input": {
                                    "transcription": {"model": "whisper-1"},
                                    "format": {
                                        "type": "audio/pcm",
                                        "rate": SAMPLE_RATE,
                                    },
                                    "turn_detection": {
                                        "type": "server_vad",
                                        "threshold": 0.5,
                                        "prefix_padding_ms": 300,
                                        "silence_duration_ms": 500,
                                        "create_response": True,
                                        "interrupt_response": False,
                                    },
                                },
                                "output": {
                                    "voice": "alloy",
                                    "format": {
                                        "type": "audio/pcm",
                                        "rate": SAMPLE_RATE,
                                    },
                                },
                            },
                        },
                    }
                )
            )

            async for raw_message in websocket:
                event = json.loads(raw_message)
                if event.get("type") == "error":
                    error = event.get("error", {})
                    raise RuntimeError(
                        f"Realtime API error: {error.get('message', error)}"
                    )
                if event.get("type") == "session.updated":
                    break

            loop = asyncio.get_running_loop()
            audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
            assistant_speaking = asyncio.Event()
            cancelled_response = asyncio.Event()

            def enqueue_audio(audio: bytes) -> None:
                try:
                    audio_queue.put_nowait(audio)
                except asyncio.QueueFull:
                    pass

            def microphone_callback(
                input_data: Any,
                _frames: int,
                _time_info: Any,
                status: sd.CallbackFlags,
            ) -> None:
                if status:
                    loop.call_soon_threadsafe(print, f"Microphone warning: {status}")
                audio = bytes(input_data)
                if not animator.push_to_talk or animator.won or animator.evidence_open:
                    audio = b"\x00" * len(audio)
                loop.call_soon_threadsafe(enqueue_audio, audio)

            block_size = SAMPLE_RATE * BLOCK_DURATION_MS // 1000
            with (
                sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=block_size,
                    channels=CHANNELS,
                    dtype="int16",
                    callback=microphone_callback,
                ),
                sd.RawOutputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                ) as output_stream,
            ):
                print(
                    "Conversation started. Diagnose the patient or press Ctrl+C."
                )

                async def send_microphone_audio() -> None:
                    while not stop.is_set():
                        audio = await audio_queue.get()
                        if animator.push_to_talk and assistant_speaking.is_set():
                            cancelled_response.set()
                            assistant_speaking.clear()
                            animator.set_talking(False)
                            await websocket.send(
                                json.dumps({"type": "response.cancel"})
                            )
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(audio).decode("ascii"),
                                }
                            )
                        )

                async def send_text_messages() -> None:
                    while not stop.is_set():
                        message = await animator.next_text_message()
                        print(f"\nYou: {message}")
                        if assistant_speaking.is_set():
                            cancelled_response.set()
                            assistant_speaking.clear()
                            animator.set_talking(False)
                            await websocket.send(
                                json.dumps({"type": "response.cancel"})
                            )
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": message,
                                            }
                                        ],
                                    },
                                }
                            )
                        )
                        await websocket.send(json.dumps({"type": "response.create"}))

                async def receive_events() -> None:
                    win_reported = False
                    async for raw_message in websocket:
                        event = json.loads(raw_message)
                        event_type = event.get("type")

                        if event_type == "response.output_audio.delta":
                            if cancelled_response.is_set():
                                continue
                            if not assistant_speaking.is_set():
                                assistant_speaking.set()
                                while not audio_queue.empty():
                                    audio_queue.get_nowait()
                            animator.set_talking(True)
                            audio = base64.b64decode(event["delta"])
                            await asyncio.to_thread(output_stream.write, audio)
                        elif event_type in {
                            "response.output_audio.done",
                            "response.done",
                        }:
                            animator.set_talking(False)
                            assistant_speaking.clear()
                            cancelled_response.clear()
                        elif event_type == "response.output_audio_transcript.delta":
                            print(event.get("delta", ""), end="", flush=True)
                        elif (
                            event_type == "response.function_call_arguments.done"
                            and event.get("name") == "win"
                            and not win_reported
                        ):
                            win_reported = True
                            animator.show_win()
                            print("\nYOU WIN")
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": event["call_id"],
                                            "output": json.dumps({"won": True}),
                                        },
                                    }
                                )
                            )
                            await asyncio.sleep(RELIEVED_DURATION_SECONDS)
                            stop.set()
                            return
                        elif (
                            event_type == "response.function_call_arguments.done"
                            and event.get("name") in tests_by_tool
                        ):
                            test = tests_by_tool[event["name"]]
                            animator.show_test_result(test)
                            print(f"\n{test.description}: {test.results}")
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": event["call_id"],
                                            "output": json.dumps(
                                                {
                                                    "test": test.description,
                                                    "result": test.results,
                                                }
                                            ),
                                        },
                                    }
                                )
                            )
                            await animator.wait_for_evidence_close()
                            await websocket.send(json.dumps({"type": "response.create"}))
                        elif event_type == (
                            "conversation.item.input_audio_transcription.completed"
                        ):
                            transcript = event.get("transcript", "")
                            print(f"\nYou: {transcript}")
                        elif event_type == "error":
                            error = event.get("error", {})
                            if _is_inactive_cancellation(error):
                                cancelled_response.clear()
                                continue
                            raise RuntimeError(
                                f"Realtime API error: {error.get('message', error)}"
                            )

                audio_sender = asyncio.create_task(send_microphone_audio())
                text_sender = asyncio.create_task(send_text_messages())
                receiver = asyncio.create_task(receive_events())
                animation = asyncio.create_task(animator.run(stop))
                done, pending = await asyncio.wait(
                    {audio_sender, text_sender, receiver, animation},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
    finally:
        if animator is not None:
            animator.close()
        credential.close()


def strat_conversation(
    system_prompts: str | Sequence[str],
    patient_type: PatientType,
    tests: Sequence[Test],
) -> None:
    """Start a patient conversation with diagnostic tools and result popups."""
    try:
        asyncio.run(
            _run_conversation(
                _combine_prompts(system_prompts),
                _patient_index(patient_type),
                tests,
            )
        )
    except KeyboardInterrupt:
        print("\nConversation ended.")


if __name__ == "__main__":
    strat_conversation(
        "You are a concise, helpful voice assistant.",
        PatientType.COMMON_COLD_KID,
        [Test("temperature", "38")],
    )