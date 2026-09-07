# ruff: noqa: I001

"""Voice conversation client for the Azure OpenAI Realtime API.

Install dependencies from the repository root with:
    uv sync

Sign in from the game when prompted, or authenticate beforehand with az login.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pygame
import sounddevice as sd
import websockets
from azure.core.credentials import AccessToken
from azure.core.exceptions import AzureError
from bidi import get_display
from websockets.exceptions import InvalidStatus, WebSocketException

from src.animation_assets import load_atlas
from src.azure_auth import AzureSignInRequired, GameCredential, clear_cached_token
from src.audio_playback import AudioPlaybackError, DeviceSink
from src.care_plan import Prescription, Referral
from src.care_plan_ui import CareOrderForm
from src.diagnostic_skills import SkillEngine, load_catalog
from src.skill_browser import ADVANCED_TEST_IDS, AdvancedTestDrawer, EquipmentDrawer, SkillBrowser
from src.skill_confirmation import SkillConfirmation
from src.skill_result import SkillResultPanel, SkillResultSummary
from src.skill_activity import INSTRUMENTS, SUPPORTED_ACTIVITIES, InstrumentActivity, ThermometerActivity
from src.conversation_runtime import ConversationRuntime, EvidencePresenter
from src.game_ui import ChoiceMenu, chat_font, is_rtl, draw_spinner, wrap_text
from src.patient_performance import PerformanceProfile
from src.patient_celebration import PatientCelebration
from src.pokedex_ui import PokedexPanel
from src.text_editing import TextEditing
from src.consultation_review import (
    REVIEW_TIMEOUT_SECONDS,
    SCORE_AXES,
    ConsultationMetrics,
    ConsultationScorecard,
    format_duration,
    score_consultation,
)

REALTIME_URL = (
    "wss://tabaumel-resource.openai.azure.com/openai/v1/realtime"
    "?model=gpt-realtime-2.1"
)
AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"
AZURE_TOKEN_REFRESH_MARGIN_SECONDS = 300
_cached_realtime_token: AccessToken | None = None
REALTIME_OPEN_TIMEOUT_SECONDS = 30
REALTIME_CONNECT_ATTEMPTS = 3
SAMPLE_RATE = 24_000
CHANNELS = 1
BLOCK_DURATION_MS = 100
ANIMATION_FPS = 6
RELIEVED_DURATION_SECONDS = 5
PROJECT_ROOT = Path(__file__).parents[1]
PATIENT_SPRITES_DIR = PROJECT_ROOT / "data" / "sprites" / "patients"
PLAYER_SPRITE_SHEET = PROJECT_ROOT / "data" / "sprites" / "players" / "dr_ash.png"
PLAYER_IDLE_SPRITE_SHEET = PLAYER_SPRITE_SHEET.with_name("dr_ash_idle_atlas.png")
PLAYER_FRAME_SIZE = (128, 200)
CONSULTATION_PLAYER_DIRECTION_ROW = 2
CONSULTATION_CHARACTER_HEIGHT = 124
HOSPITAL_MAP_IMAGE = (
    PROJECT_ROOT / "data" / "sprites" / "world" / "hospital_floor.png"
)
CONSULTATION_ROOM_IMAGE = (
    PROJECT_ROOT / "data" / "sprites" / "world" / "consultation_room.png"
)
WORLD_DISPLAY_SIZE = (480, 374)
SCREEN_SIZE = (480, 480)
WINDOW_SIZE = (960, 960)
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
UI_INK = (20, 32, 38)
UI_PANEL = (22, 43, 46)
UI_PAPER = (247, 249, 244)
UI_MINT = (193, 231, 215)
UI_TEAL = (52, 132, 121)
UI_CORAL = (238, 105, 82)
UI_GOLD = (244, 184, 72)
UI_MUTED = (127, 151, 146)
UI_WHITE = (255, 255, 251)
SCENE_FADE_SECONDS = 0.28


def extract_character(frame: pygame.Surface) -> pygame.Surface:
    frame = frame.convert_alpha()
    width, height = frame.get_size()
    pending: deque[tuple[int, int]] = deque()
    for horizontal in range(width):
        pending.extend(((horizontal, 0), (horizontal, height - 1)))
    for vertical in range(height):
        pending.extend(((0, vertical), (width - 1, vertical)))

    visited: set[tuple[int, int]] = set()
    background: set[tuple[int, int]] = set()
    while pending:
        position = pending.popleft()
        if position in visited:
            continue
        visited.add(position)
        red, green, blue, _ = frame.get_at(position)
        if max(red, green, blue) < 175 or max(red, green, blue) - min(
            red, green, blue
        ) > 55:
            continue
        background.add(position)
        horizontal, vertical = position
        for neighbor in (
            (horizontal - 1, vertical),
            (horizontal + 1, vertical),
            (horizontal, vertical - 1),
            (horizontal, vertical + 1),
        ):
            if 0 <= neighbor[0] < width and 0 <= neighbor[1] < height:
                pending.append(neighbor)

    for position in background:
        color = frame.get_at(position)
        frame.set_at(position, (*color[:3], 0))

    components = pygame.mask.from_surface(frame, threshold=16).connected_components()
    if not components:
        return frame
    character_mask = max(components, key=lambda component: component.count())
    alpha_mask = character_mask.to_surface(
        setcolor=(255, 255, 255, 255),
        unsetcolor=(255, 255, 255, 0),
    )
    frame.blit(alpha_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    character_bounds = character_mask.get_bounding_rects()[0]
    return frame.subsurface(character_bounds).copy()


def normalize_character_frames(
    frames: Sequence[pygame.Surface],
    target_height: int,
    source_size: tuple[int, int] | None = None,
) -> tuple[pygame.Surface, ...]:
    if not frames:
        raise ValueError("At least one character frame is required")

    source_width = max(frame.get_width() for frame in frames)
    source_height = max(frame.get_height() for frame in frames)
    if source_size is not None:
        source_width, source_height = source_size
    scale = target_height / source_height
    canvas_size = (max(1, round(source_width * scale)), target_height)

    normalized = []
    for frame in frames:
        scaled = pygame.transform.scale(
            frame,
            (
                max(1, round(frame.get_width() * scale)),
                max(1, round(frame.get_height() * scale)),
            ),
        )
        canvas = pygame.Surface(canvas_size, pygame.SRCALPHA)
        canvas.blit(scaled, scaled.get_rect(midbottom=(canvas_size[0] // 2, target_height)))
        normalized.append(canvas)
    return tuple(normalized)


def animation_frame(
    frames: Sequence[pygame.Surface],
    animation_time: float,
    frames_per_second: float,
) -> pygame.Surface:
    frame_index = math.floor(animation_time * frames_per_second) % len(frames)
    return frames[frame_index]


class ConversationResult(str, Enum):
    SOLVED = "solved"
    SOLVED_QUIT = "solved_quit"
    RETURNED = "returned"
    QUIT = "quit"


class RealtimeSessionError(RuntimeError):
    pass


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
    ECCENTRIC_NEIGHBOR = "Eccentric Neighbor"


PATIENT_VOICES = {
    PatientType.COMMON_COLD_KID: "echo",
    PatientType.STOMACHACHE_TEEN: "coral",
    PatientType.MIGRAINE_SUFFERER: "echo",
    PatientType.ALLERGIES_PATIENT: "coral",
    PatientType.SPRAINED_ANKLE_ATHLETE: "echo",
    PatientType.ANXIOUS_ADULT: "coral",
    PatientType.FEVERISH_PATIENT: "echo",
    PatientType.RASH_PATIENT: "echo",
    PatientType.ELDERLY_WITH_BACK_PAIN: "echo",
    PatientType.SLEEP_DEPRIVED_WORKER: "echo",
    PatientType.ECCENTRIC_NEIGHBOR: "echo",
}


@dataclass(frozen=True)
class Test:
    description: str
    results: str
    evidence_image: str | None = None
    audio: str | None = None
    skill_summary: SkillResultSummary | None = None

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("Test description must not be empty")
        if not self.results.strip():
            raise ValueError("Test results must not be empty")
        if self.audio is not None:
            if not isinstance(self.audio, str) or not self.audio.strip():
                raise ValueError("Test audio must be a non-empty WAV path")
            if Path(self.audio).suffix.casefold() != ".wav":
                raise ValueError("Test audio must be a WAV file")

    @property
    def image_path(self) -> Path | None:
        path = Path(self.evidence_image or self.results).expanduser()
        if path.suffix.casefold() not in IMAGE_SUFFIXES:
            return None
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            raise FileNotFoundError(f"Evidence image not found: {path}")
        return path

    @property
    def audio_path(self) -> Path | None:
        if self.audio is None:
            return None
        path = Path(self.audio).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path


PATIENT_TYPES = tuple(patient_type.value for patient_type in PatientType)
PATIENT_SPRITE_SHEETS = (
    "01_common_cold_kid.png",
    "02_stomachache_teen.png",
    "03_migraine_patient.png",
    "04_allergy_patient.png",
    "05_injured_athlete.png",
    "06_anxious_adult.png",
    "07_feverish_patient.png",
    "08_rash_patient.png",
    "09_older_patient_back_pain.png",
    "10_sleep_deprived_worker.png",
    "11_eccentric_neighbor.png",
)
PATIENT_FRAME_COUNTS = (4, 4, 4, 4, 3, 4, 4, 4, 4, 4, 4)
PATIENT_FRAME_PADDING = 12
SPRITE_GRID_LEFT = 73
SPRITE_GRID_RIGHT = 266
LOWER_ALIGNED_STATE_BOUNDS = {
    "idle": (288, 349),
    "talking": (350, 414),
    "worried": (415, 477),
    "relieved": (478, 542),
}
UPPER_ALIGNED_STATE_BOUNDS = {
    "idle": (269, 336),
    "talking": (337, 403),
    "worried": (404, 470),
    "relieved": (471, 537),
}
PATIENT_SPRITE_GRID_BOUNDS = ((SPRITE_GRID_LEFT, SPRITE_GRID_RIGHT),) * 10 + (
    (67, 267),
)
PATIENT_STATE_BOUNDS = (
    (LOWER_ALIGNED_STATE_BOUNDS,) * 5
    + (UPPER_ALIGNED_STATE_BOUNDS,) * 5
    + (
        {
            "idle": (250, 325),
            "talking": (326, 401),
            "worried": (403, 477),
            "relieved": (479, 553),
        },
    )
)
PATIENT_ATLAS_STATE_BOUNDS = {
    "idle": (0, 256),
    "talking": (256, 512),
    "worried": (512, 768),
    "relieved": (768, 1024),
}
PATIENT_FULL_BODY_FALLBACK_BOUNDS = {
    2: (118, 286),
    5: (105, 276),
}


def patient_sprite_layout(
    patient_index: int,
) -> tuple[
    Path,
    tuple[int, int],
    dict[str, tuple[int, int]],
    int,
    int,
]:
    legacy_path = PATIENT_SPRITES_DIR / PATIENT_SPRITE_SHEETS[patient_index]
    atlas_path = legacy_path.with_name(f"{legacy_path.stem}_atlas.png")
    if atlas_path.is_file():
        return (
            atlas_path,
            (0, 1024),
            PATIENT_ATLAS_STATE_BOUNDS,
            4,
            0,
        )

    fallback_bounds = PATIENT_FULL_BODY_FALLBACK_BOUNDS.get(patient_index)
    if fallback_bounds is not None:
        return (
            legacy_path,
            (60, 220),
            {state: fallback_bounds for state in PATIENT_ATLAS_STATE_BOUNDS},
            1,
            0,
        )

    return (
        legacy_path,
        PATIENT_SPRITE_GRID_BOUNDS[patient_index],
        PATIENT_STATE_BOUNDS[patient_index],
        PATIENT_FRAME_COUNTS[patient_index],
        PATIENT_FRAME_PADDING,
    )


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


def _patient_instructions(system_prompt: str, disease: str, review_records: dict[str, str] | None = None) -> str:
    disease = disease.strip()
    if not disease:
        raise ValueError("disease must not be empty")

    return (
        f"{system_prompt}\n\n"
        "This is a diagnostic game. Act only as the patient while the clinician "
        "tries to diagnose you. Describe your symptoms naturally, but never state, "
        "spell, confirm, or otherwise reveal your disease. "
        f"Your exact disease is: {disease}. "
        "Every patient has the same universal skills catalog. You may explain "
        "neutral tool names and the Skills browser, but never reveal which skills "
        "are relevant, scoring rules, or hidden findings. Interpret both voice and "
        "typed language semantically using tool aliases/examples, not exact phrases. "
        "Only propose a skill when the clinician explicitly intends an action now. "
        "Do not call tools for negation, cancellation, hypotheticals, education, "
        "questions about a procedure, or mere mentions. Clarify broad requests like "
        "'blood tests' or 'sequence it', and missing site, specimen, target or consent. "
        "Never invent parameters, consent, conversation evidence or prerequisites. "
        "An explicit unnecessary test is still selectable: do not filter requests "
        "to helpful tests. Proposals require local clinician confirmation; a tool "
        "call alone does not perform anything. History/review/counseling requires "
        "the actual relevant clinician-patient exchange, not its name or a referral. "
        "Before citing evidence_turns or consent_turn, call get_skill_context to "
        "read the game's actual indexed transcript. Use its exact indices; never "
        "guess indices from your own conversation item count. If the needed "
        "transcript is not present yet, clarify or wait rather than inventing it. "
        "Do not describe or invent the test "
        "result yourself. Diagnosis submissions are handled separately by the game. "
        "Do not judge guesses or declare a diagnosis correct during conversation. "
        "Respond to the clinician's prescriptions and referrals as the patient, "
        "asking relevant questions or expressing concerns. These are simulated care "
        "decisions, not real orders. Do not certify medication safety or invent "
        "allergies, age, weight or other medical facts absent from the case. "
        "Exception for existing historical documents: when asked to review one, "
        "you may quote the following authored pre-existing records. They are not "
        "newly performed tests; a review tool still requires an actual exchange. "
        f"Existing review records: {json.dumps(review_records or {})}"
    )


def _diagnosis_matches(submitted: str, disease: str) -> bool:
    submitted_words = re.findall(r"\w+", submitted.casefold().replace("_", " "))
    disease_words = re.findall(r"\w+", disease.casefold().replace("_", " "))
    print(f"Diagnosis check: submitted={submitted_words}, disease={disease_words}")
    return bool(disease_words) and submitted_words == disease_words


def _patient_index(patient_type: PatientType) -> int:
    if not isinstance(patient_type, PatientType):
        raise TypeError("patient_type must be a PatientType enum member")
    return list(PatientType).index(patient_type)


def _test_tools(tests: Sequence[Test]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The identical neutral catalog is exposed for every case."""
    if any(not isinstance(test, Test) for test in tests):
        raise TypeError("tests must contain only Test instances")
    tools: list[dict[str, Any]] = []
    tests_by_tool: dict[str, str] = {}
    for skill in load_catalog():
        tool_name = f"propose_{skill.id}"
        tests_by_tool[tool_name] = skill.id
        tools.append(
            {
                "type": "function",
                "name": tool_name,
                "description": (
                    f"Propose {skill.name}. {skill.description} "
                    f"Aliases: {', '.join(skill.aliases)}. "
                    f"Example requests: {' | '.join(skill.examples)}. {skill.note or ''} "
                    "Only for explicit current action intent. Requires local confirmation."
                ),
                "parameters": skill.parameters,
            }
        )
    return tools, tests_by_tool


SKILL_CONTEXT_TOOL = {
    "type": "function",
    "name": "get_skill_context",
    "description": (
        "Read the actual indexed clinician/patient transcript for evidence_turns "
        "and consent_turn. Read-only: performs no procedure, supplies no hidden "
        "case facts, and does not complete history or counseling."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _input_device_candidates() -> tuple[int | None, ...]:
    candidates: list[int | None] = [None]
    try:
        devices = sd.query_devices()
        default_input = sd.default.device[0]
    except sd.PortAudioError:
        return tuple(candidates)

    if isinstance(default_input, int) and default_input >= 0:
        candidates.append(default_input)
    candidates.extend(
        index
        for index, device in enumerate(devices)
        if device["max_input_channels"] >= CHANNELS and index not in candidates
    )
    return tuple(candidates)


@contextmanager
def _microphone_stream(callback: Any) -> Iterator[sd.RawInputStream | None]:
    stream: sd.RawInputStream | None = None
    last_error: sd.PortAudioError | None = None
    selected_device: int | None = None

    for device in _input_device_candidates():
        try:
            stream = sd.RawInputStream(
                device=device,
                samplerate=SAMPLE_RATE,
                blocksize=SAMPLE_RATE * BLOCK_DURATION_MS // 1000,
                channels=CHANNELS,
                dtype="int16",
                callback=callback,
            )
            stream.start()
            selected_device = device
            break
        except sd.PortAudioError as error:
            last_error = error
            if stream is not None:
                stream.close()
                stream = None

    if stream is None:
        print(
            "\nMicrophone unavailable; continuing with typed input. On macOS, "
            "enable Microphone access for Visual Studio Code - Insiders in "
            "System Settings > Privacy & Security > Microphone, then restart it. "
            f"Last PortAudio error: {last_error}"
        )
    elif selected_device is not None:
        device = sd.query_devices(selected_device)
        print(f"Recovered using microphone: {device['name']}")

    try:
        yield stream
    finally:
        if stream is not None:
            stream.close()


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
    def __init__(
        self,
        patient_index: int,
        *,
        window: pygame.Surface | None = None,
        screen: pygame.Surface | None = None,
        disease: str | None = None,
    ) -> None:
        pygame.init()
        pygame.display.set_caption("Patient Conversation")
        self._owns_display = window is None
        self._window = (
            pygame.display.set_mode(WINDOW_SIZE) if window is None else window
        )
        self._screen = pygame.Surface(SCREEN_SIZE) if screen is None else screen
        (
            sheet_path,
            self._sprite_grid,
            self._state_bounds,
            self._frame_count,
            self._frame_padding,
        ) = patient_sprite_layout(patient_index)
        self._sheet = pygame.image.load(str(sheet_path)).convert_alpha()
        raw_patient_frames = {
            state: tuple(
                extract_character(self._frame(state, frame_index))
                for frame_index in range(self._frame_count)
            )
            for state in self._state_bounds
        }
        patient_source_size = (
            max(
                frame.get_width()
                for frames in raw_patient_frames.values()
                for frame in frames
            ),
            max(
                frame.get_height()
                for frames in raw_patient_frames.values()
                for frame in frames
            ),
        )
        self._patient_frames = {
            state: normalize_character_frames(
                frames,
                CONSULTATION_CHARACTER_HEIGHT,
                patient_source_size,
            )
            for state, frames in raw_patient_frames.items()
        }
        facing_patient_frames = load_atlas(PLAYER_IDLE_SPRITE_SHEET, 4)[
            CONSULTATION_PLAYER_DIRECTION_ROW
        ]
        player_source_size = (
            max(frame.get_width() for frame in facing_patient_frames),
            max(frame.get_height() for frame in facing_patient_frames),
        )
        normalized_player_frames = normalize_character_frames(
            facing_patient_frames,
            CONSULTATION_CHARACTER_HEIGHT,
            player_source_size,
        )
        self._player_frames = {row: normalized_player_frames for row in range(2)}
        consultation_room = pygame.image.load(
            str(CONSULTATION_ROOM_IMAGE)
        ).convert()
        crop_height = min(
            consultation_room.get_height(),
            round(
                consultation_room.get_width()
                * WORLD_DISPLAY_SIZE[1]
                / WORLD_DISPLAY_SIZE[0]
            ),
        )
        self._world = pygame.transform.smoothscale(
            consultation_room.subsurface(
                pygame.Rect(0, 0, consultation_room.get_width(), crop_height)
            ),
            WORLD_DISPLAY_SIZE,
        )
        self._patient_number = patient_index + 1
        self._celebration = PatientCelebration(tuple(PatientType)[patient_index].name)
        self._celebration_layer = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        self._state = "idle"
        self._relieved_until = 0.0
        self._scene_started_at = time.monotonic()
        self._test_result: Test | None = None
        self._test_result_image: pygame.Surface | None = None
        self._skill_result_panel: SkillResultPanel | None = None
        self._evidence_closed = asyncio.Event()
        self._evidence_closed.set()
        self._test_close_button = pygame.Rect(400, 48, 30, 30)
        self._won = False
        self.metrics = ConsultationMetrics()
        self.skill_engine: SkillEngine | None = None
        self._skill_browser: SkillBrowser | None = None
        self._skill_confirmation: SkillConfirmation | InstrumentActivity | None = None
        self._skill_decision: asyncio.Future[bool] | None = None
        self._skills_busy = False
        self._local_skill_busy = False
        self._local_skill_requests: asyncio.Queue[str] = asyncio.Queue()
        self._local_skill_chat_was_focused = False
        self._equipment_drawer_hotspot = pygame.Rect(52, 151, 39, 28)
        self._advanced_drawer_hotspot = pygame.Rect(187, 182, 44, 25)
        self._drawer_hovered = False
        self._advanced_drawer_hovered = False
        self._skills_chat_was_focused = False
        self._skills_button = pygame.Rect(234, 384, 90, 28)
        self._discovered_tests_button = pygame.Rect(326, 34, 138, 24)
        self._review_open = False
        self._review_loading = False
        self._review_error = ""
        self._scorecard: ConsultationScorecard | None = None
        self._available_test_count = 0
        self._review_scroll = 0
        self._review_max_scroll = 0
        self._review_retry = asyncio.Event()
        self._review_retry_button = pygame.Rect(18, 428, 112, 36)
        self._review_return_button = pygame.Rect(144, 428, 318, 36)
        self._disease = disease
        self._diagnosis_confirmed = asyncio.Event()
        self._consultation_finished = asyncio.Event()
        self._diagnosis_open = False
        self._diagnosis_input = ""
        self._diagnosis_editing = TextEditing()
        self._diagnosis_feedback = ""
        self._diagnosis_focus = 0
        self._chat_was_focused = False
        self._diagnose_button = pygame.Rect(18, 384, 100, 28)
        self._care_button = pygame.Rect(126, 384, 100, 28)
        self._pokedex_button = pygame.Rect(332, 384, 130, 28)
        self._pokedex = PokedexPanel()
        self._pokedex_chat_was_focused = False
        self._care_form: CareOrderForm | None = None
        self._care_chat_was_focused = False
        self._diagnosis_input_rect = pygame.Rect(48, 178, 384, 40)
        self._diagnosis_cancel_button = pygame.Rect(48, 282, 140, 40)
        self._diagnosis_submit_button = pygame.Rect(204, 282, 228, 40)
        self._running = True
        self.quit_requested = False
        self.retry_requested = False
        self.sign_in_requested = False
        self._ready = True
        self._sending = False
        self._sent_until = 0.0
        self._menu: ChoiceMenu | None = None
        self._transcript: list[tuple[str, str]] = []
        self._transcript_indexes: dict[str, int] = {}
        self._transcript_lines: list[str] = []
        self._transcript_scroll = 0
        self._evidence_scroll = 0
        self._evidence_max_scroll = 0
        self._push_to_talk = threading.Event()
        self._microphone_available = True
        self._text_messages: asyncio.Queue[str] = asyncio.Queue()
        self._text_input = ""
        self._text_editing = TextEditing()
        self._text_focused = False
        self._text_input_rect = pygame.Rect(18, 418, 386, 40)
        self._text_send_button = pygame.Rect(414, 418, 48, 40)
        self._eyebrow_font = pygame.font.SysFont("Avenir Next", 10, bold=True)
        self._case_font = pygame.font.SysFont("Avenir Next", 16, bold=True)
        self._status_font = chat_font(11, bold=True)
        self._text_font = chat_font(15)
        self._test_title_font = pygame.font.SysFont("Avenir Next", 16, bold=True)
        self._test_result_font = pygame.font.SysFont("Avenir Next", 18, bold=True)
        self._win_font = pygame.font.SysFont("Avenir Next", 32, bold=True)

    def _screen_position(self, window_position: tuple[int, int]) -> tuple[int, int]:
        window_width, window_height = self._window.get_size()
        return (
            round(window_position[0] * SCREEN_SIZE[0] / window_width),
            round(window_position[1] * SCREEN_SIZE[1] / window_height),
        )

    @property
    def push_to_talk(self) -> bool:
        return self._push_to_talk.is_set() and not self._diagnosis_open and self._care_form is None and not self.skills_modal and not self._pokedex.open

    @property
    def skills_modal(self) -> bool:
        return (self._skills_busy or self._local_skill_busy
                or self._skill_browser is not None or self._skill_confirmation is not None)

    @property
    def won(self) -> bool:
        return self._won

    @property
    def evidence_open(self) -> bool:
        return self._test_result is not None

    def set_talking(self, talking: bool) -> None:
        self._state = "talking" if talking else "idle"
        if talking:
            self._sending = False

    def set_microphone_available(self, available: bool) -> None:
        self._microphone_available = available
        if not available:
            self._push_to_talk.clear()
            self._set_text_focus(True)

    def show_relieved(self) -> None:
        self._relieved_until = time.monotonic() + RELIEVED_DURATION_SECONDS

    def show_win(self) -> None:
        self._celebration.dismiss()
        self._pokedex.hide()
        self.metrics.finish()
        self._won = True
        self._diagnosis_open = False
        self._set_text_focus(False)
        self._menu = None
        self.show_relieved()

    def _submit_diagnosis(self) -> None:
        if not self._diagnosis_open or not self._ready or self.evidence_open or self.won or self._menu is not None or self._diagnosis_confirmed.is_set():
            return
        submitted = self._diagnosis_input.strip()
        if not submitted or self._disease is None:
            return
        self.add_transcript("Diagnosis", submitted)
        if _diagnosis_matches(submitted, self._disease):
            self._diagnosis_confirmed.set()
            self._celebration.start(time.monotonic())
            self._close_diagnosis()
            self.add_transcript("Case", "Diagnosis confirmed.")
        else:
            self._diagnosis_feedback = "Not confirmed. Review the evidence and try again."
            self.add_transcript("Case", self._diagnosis_feedback)

    def _open_diagnosis(self) -> None:
        if self.skills_modal or self._pokedex.open:
            return
        if not self._ready or self.won or self.evidence_open or self._menu is not None or self._disease is None or self._care_form is not None:
            return
        if self._diagnosis_confirmed.is_set():
            self._set_text_focus(False)
            plan = self.metrics.care_plan
            self._menu = ChoiceMenu("FINISH THIS VISIT?", ("Keep Consulting", "Finish Visit"), f"Diagnosis confirmed.\n{len(plan.prescriptions)} prescriptions / {len(plan.referrals)} referrals")
            return
        self._chat_was_focused = self._text_focused
        self._set_text_focus(False)
        self._diagnosis_open = True
        self._diagnosis_feedback = ""
        self._focus_diagnosis(0)

    def _focus_diagnosis(self, focus: int) -> None:
        self._diagnosis_editing.reset()
        self._diagnosis_focus = focus
        if focus == 0:
            pygame.key.start_text_input()
        else:
            pygame.key.stop_text_input()

    def _close_diagnosis(self) -> None:
        self._diagnosis_open = False
        self._set_text_focus(self._chat_was_focused)

    def _finish_consultation(self) -> None:
        if self.skills_modal:
            self.add_transcript("Case", "Resolve the pending skill request before finishing.")
            return
        if self._pokedex.open:
            return
        if not self._ready or self.won or not self._diagnosis_confirmed.is_set() or self.evidence_open or self._diagnosis_open or self._care_form is not None:
            return
        if self._sending or not self._text_messages.empty():
            self.add_transcript("Case", "Message still pending.")
            return
        self.show_win()
        self._consultation_finished.set()

    def _open_care_menu(self) -> None:
        if self.skills_modal or self._pokedex.open:
            return
        if not self._ready or self.won or self.evidence_open or self._diagnosis_open or self._menu is not None or self._care_form is not None:
            return
        self._care_chat_was_focused = self._text_focused
        self._set_text_focus(False)
        plan = self.metrics.care_plan
        self._menu = ChoiceMenu("CARE PLAN", ("Add Prescription", "Add Referral", "Review Orders", "Keep Consulting"), f"{len(plan.prescriptions)} prescriptions / {len(plan.referrals)} referrals")

    def _open_pokedex(self) -> None:
        if not self._ready or self.won or self.evidence_open or self._diagnosis_open or self._menu is not None or self._care_form is not None or self._pokedex.open or self.skills_modal:
            return
        self._pokedex_chat_was_focused = self._text_focused
        self._set_text_focus(False)
        self._pokedex.show()

    def _pokedex_context(self) -> dict:
        return {
            "transcript": [
                {"speaker": speaker, "text": text}
                for speaker, text in self._transcript
                if speaker in ("You", "Patient")
            ],
            "discovered_tests": [
                {"name": name, "result": "Image finding not provided to HAS." if Path(result).suffix.lower() in IMAGE_SUFFIXES else result}
                for name, result in self.metrics.discovered_tests.items()
            ],
            "care_plan": asdict(self.metrics.care_plan),
        }

    def _record_care_order(self, order: Prescription | Referral) -> None:
        if not self.metrics.care_plan.add(order):
            self.add_transcript("Case", "This order is already recorded.")
            return
        message = order.message()
        self._text_messages.put_nowait(message)
        self.add_transcript("You", message)
        self._sending = True

    def show_test_result(self, test: Test) -> None:
        if self._pokedex.open:
            self._pokedex.focus(False)
        self._diagnosis_open = False
        self._set_text_focus(False)
        self._evidence_scroll = 0
        self._test_result = test
        image_path = test.image_path
        self._test_result_image = None
        self._skill_result_panel = None
        if image_path:
            image = pygame.image.load(str(image_path)).convert_alpha()
            max_dimension = max(image.get_size())
            if max_dimension > 512:
                scale = 512 / max_dimension
                image = pygame.transform.smoothscale(
                    image,
                    (
                        round(image.get_width() * scale),
                        round(image.get_height() * scale),
                    ),
                )
            self._test_result_image = image
        if test.skill_summary is not None:
            skill = next(skill for skill in load_catalog() if skill.id == test.skill_summary.skill_id)
            self._skill_result_panel = SkillResultPanel(test.skill_summary, skill.icon, self._test_result_image)
        self._push_to_talk.clear()
        self._evidence_closed.clear()

    def _show_discovered_tests(self) -> None:
        reports = [f"{name}\n{result}" for name, result in self.metrics.discovered_tests.items()]
        if self.skill_engine is not None:
            reports = [f"Appropriate-use points: {self.skill_engine.score:+d} (separate from AI feedback)"]
            reports.extend(
                f"{action.get('name', action.get('skill_id', 'Skill'))}: "
                f"{action.get('status', '')} / {action.get('points', 0):+d}\n"
                f"Parameters: {json.dumps(action.get('parameters', {}))}\n"
                f"{action.get('result', '')}\n"
                f"{action.get('rationale', '')}"
                for action in self.skill_engine.actions
            )
        self.show_test_result(Test("Used skills and results", "\n\n".join(reports) if reports else "No skills used."))

    def _open_skills(self, *, drawer: str | None = None) -> None:
        if drawer not in (None, "equipment", "advanced"):
            raise ValueError(f"Unknown drawer: {drawer!r}")
        if not self._ready or self.won or self.evidence_open or self._menu or self._diagnosis_open or self._care_form or self.skills_modal or self._pokedex.open:
            return
        if drawer is not None and (self.skill_engine is None or self._sending):
            return
        self._skills_chat_was_focused = self._text_focused
        self._set_text_focus(False)
        if drawer == "equipment":
            self._skill_browser = EquipmentDrawer(skill for skill in load_catalog() if skill.id in SUPPORTED_ACTIVITIES)
        elif drawer == "advanced":
            self._skill_browser = AdvancedTestDrawer(skill for skill in load_catalog() if skill.id in ADVANCED_TEST_IDS)
        else:
            self._skill_browser = SkillBrowser(load_catalog())

    async def confirm_skill(self, proposal: dict) -> bool:
        request = next((text for speaker, text in reversed(self._transcript) if speaker == "You"), "")
        if proposal["skill_id"] in SUPPORTED_ACTIVITIES and proposal["availability"] == "available":
            frame = self._patient_frames["talking"][0]
            patient_type = tuple(PatientType)[self._patient_number - 1].name
            self._skill_confirmation = (
                ThermometerActivity(frame, patient_type) if proposal["skill_id"] == "temperature"
                else InstrumentActivity(frame, patient_type, proposal["skill_id"])
            )
            if self._local_skill_busy:
                self._skill_confirmation.handle_event(
                    pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN), (-1, -1),
                )
        else:
            skill = next(skill for skill in load_catalog() if skill.id == proposal["skill_id"])
            self._skill_confirmation = SkillConfirmation(
                proposal["name"], proposal["parameters"], request, skill_id=skill.id, icon=skill.icon,
            )
        self._skill_decision = asyncio.get_running_loop().create_future()
        try:
            return await self._skill_decision
        finally:
            self._skill_confirmation = None
            self._skill_decision = None

    def _advance_skill_activity(self, now: float) -> None:
        activity = self._skill_confirmation
        if isinstance(activity, InstrumentActivity) and activity.update(now):
            if self._skill_decision is not None and not self._skill_decision.done():
                self._skill_decision.set_result(True)

    def _request_local_skill(self, skill_id: str) -> None:
        if skill_id not in SUPPORTED_ACTIVITIES:
            self.add_transcript("Skill", "No hands-on activity is available for that skill yet.")
            return
        if (not self._ready or self.skill_engine is None or self.won or self.evidence_open
                or self._menu or self._diagnosis_open or self._care_form or self.skills_modal
                or self._pokedex.open or self._sending):
            return
        self._local_skill_chat_was_focused = self._text_focused
        self._set_text_focus(False)
        self._local_skill_busy = True
        label = INSTRUMENTS[skill_id].label.lower()
        self.add_transcript("You", f"[Selected the {label} for a hands-on examination.]")
        self._local_skill_requests.put_nowait(skill_id)

    def _draw_equipment_drawer_hint(self) -> None:
        if (not (self._drawer_hovered or self._advanced_drawer_hovered)
                or not self._ready or self.skill_engine is None
                or self.won or self.skills_modal or self.evidence_open or self._menu
                or self._diagnosis_open or self._care_form or self._pokedex.open):
            return
        drawer = (54, 157, 33, 15) if self._drawer_hovered else (189, 184, 40, 21)
        caption = "Open equipment drawer / F7" if self._drawer_hovered else "Advanced-test requests / F8"
        pygame.draw.rect(self._screen, UI_GOLD, drawer, width=2)
        label = self._status_font.render(caption, True, UI_WHITE)
        rect = label.get_rect(topleft=(24, 124)).inflate(12, 10)
        pygame.draw.rect(self._screen, UI_PANEL, rect, border_radius=4)
        self._screen.blit(label, (24, 124))

    async def wait_for_evidence_close(self) -> None:
        await self._evidence_closed.wait()

    async def next_text_message(self) -> str:
        return await self._text_messages.get()

    def _submit_text(self) -> None:
        if self.skills_modal or self._pokedex.open:
            return
        if not self._ready or self.evidence_open or self.won or self._menu is not None or self._diagnosis_open or self._care_form is not None:
            return
        message = self._text_input.strip()
        if not message:
            return
        self._text_messages.put_nowait(message)
        self.add_transcript("You", message)
        self._sending = True
        self._text_input = ""
        self._text_editing.reset()

    def add_transcript(self, speaker: str, text: str, item_id: str | None = None, *, append: bool = False) -> None:
        if not text:
            return
        previous_line_count = len(self._transcript_lines)
        key = f"{speaker}:{item_id}" if item_id else None
        if key is not None and key in self._transcript_indexes:
            index = self._transcript_indexes[key]
            previous = self._transcript[index][1] if append else ""
            self._transcript[index] = (speaker, previous + text)
        else:
            if key is not None:
                self._transcript_indexes[key] = len(self._transcript)
            self._transcript.append((speaker, text))
        self._transcript_lines = [
            line
            for author, message in self._transcript
            for line in wrap_text(f"{author}: {message}", self._status_font, 438)
        ]
        if self._transcript_scroll:
            self._transcript_scroll = max(0, self._transcript_scroll + len(self._transcript_lines) - previous_line_count)

    def show_error(self, message: str) -> None:
        self._pokedex.hide()
        self._ready = False
        self._care_form = None
        self._diagnosis_open = False
        self._set_text_focus(False)
        self._menu = ChoiceMenu("CONNECTION UNAVAILABLE", ("Retry", "Return to Hospital"), message)

    def show_audio_error(self, message: str) -> None:
        self.show_error(message)
        self._menu = ChoiceMenu("AUDIO UNAVAILABLE", ("Retry", "Return to Hospital"), message)

    def show_sign_in(self, message: str, *, pending: bool = False) -> None:
        self._pokedex.hide()
        self._ready = False
        self._set_text_focus(False)
        self._menu = ChoiceMenu(
            "SIGNING IN TO AZURE" if pending else "AZURE SIGN-IN REQUIRED",
            ("Return to Hospital",) if pending else ("Sign in to Azure", "Return to Hospital"),
            message,
        )

    def _draw_transcript(self) -> None:
        area = pygame.Rect(0, 290, 480, 84)
        pygame.draw.rect(self._screen, UI_PAPER, area)
        pygame.draw.rect(self._screen, UI_TEAL, (0, 290, 480, 2))
        visible_lines = 4
        self._transcript_scroll = min(self._transcript_scroll, max(0, len(self._transcript_lines) - visible_lines))
        end = len(self._transcript_lines) - self._transcript_scroll
        lines = self._transcript_lines[max(0, end - visible_lines):end]
        for index, line in enumerate(lines):
            text = self._status_font.render(get_display(line), True, UI_INK)
            text_x = 456 - text.get_width() if is_rtl(line) else 18
            self._screen.blit(text, (text_x, 301 + index * 17))
        if len(self._transcript_lines) > visible_lines:
            track = pygame.Rect(468, 300, 3, 64)
            pygame.draw.rect(self._screen, UI_MUTED, track)
            fraction = 1 - self._transcript_scroll / (len(self._transcript_lines) - visible_lines)
            pygame.draw.rect(self._screen, UI_TEAL, (468, 300 + round(48 * fraction), 3, 16))

    def _set_text_focus(self, focused: bool) -> None:
        self._text_editing.reset()
        focused = focused and not self._pokedex.open
        self._text_focused = focused
        self._push_to_talk.clear()
        if focused:
            pygame.key.start_text_input()
        else:
            pygame.key.stop_text_input()

    def close_test_result(self) -> None:
        self._test_result = None
        self._test_result_image = None
        self._skill_result_panel = None
        self._evidence_closed.set()
        if self._care_form is not None:
            self._care_form.focus(self._care_form.focus_index)
        if self._pokedex.open:
            self._pokedex.focus(True)

    def _current_state(self) -> str:
        if self._celebration.active(time.monotonic()):
            return "relieved"
        if time.monotonic() < self._relieved_until:
            return "relieved"
        return self._state

    def _frame(self, state: str, frame_index: int) -> pygame.Surface:
        grid_left, grid_right = self._sprite_grid
        grid_width = grid_right - grid_left
        left = max(
            0,
            grid_left
            + round(frame_index * grid_width / self._frame_count)
            - self._frame_padding,
        )
        right = min(
            self._sheet.get_width(),
            grid_left
            + round((frame_index + 1) * grid_width / self._frame_count)
            + self._frame_padding,
        )
        top, bottom = self._state_bounds[state]
        rectangle = pygame.Rect(
            left,
            top,
            right - left,
            bottom - top,
        )
        return self._sheet.subsurface(rectangle)

    def _centered_frame(self, state: str, animation_time: float) -> pygame.Surface:
        now = time.monotonic()
        if self._celebration.active(now):
            frames = self._patient_frames["relieved"]
            return frames[self._celebration.frame_index(now, len(frames))]
        return animation_frame(
            self._patient_frames[state],
            animation_time,
            ANIMATION_FPS,
        )

    def _player_frame(self, animation_time: float) -> pygame.Surface:
        row = 1 if self.push_to_talk else 0
        return animation_frame(
            self._player_frames[row],
            animation_time,
            ANIMATION_FPS,
        )

    def _status(self, state: str) -> tuple[str, tuple[int, int, int]]:
        if self.won:
            return "CASE SOLVED", UI_MINT
        if self.evidence_open:
            return "EVIDENCE", UI_GOLD
        if not self._ready:
            return "CONNECTING", UI_GOLD
        if self.push_to_talk:
            return "RELEASE SHIFT TO SEND", UI_CORAL
        if state == "talking" or (self._celebration.active(time.monotonic()) and self._state == "talking"):
            return "PATIENT SPEAKING", UI_MINT
        if self._sending:
            return "SENDING", UI_GOLD
        if time.monotonic() < self._sent_until:
            return "SENT", UI_MINT
        if self._text_focused and self._text_input:
            return "TYPING", UI_MINT
        if not self._microphone_available:
            return "TEXT MODE", UI_GOLD
        return "READY", UI_MINT

    def _draw_header(self, state: str) -> None:
        header = pygame.Surface((SCREEN_SIZE[0], 64), pygame.SRCALPHA)
        header.fill((*UI_INK, 226))
        self._screen.blit(header, (0, 0))
        pygame.draw.rect(self._screen, UI_CORAL, (0, 0, SCREEN_SIZE[0], 3))

        icon = pygame.Rect(16, 14, 34, 34)
        pygame.draw.rect(self._screen, UI_CORAL, icon, border_radius=6)
        pygame.draw.rect(self._screen, UI_WHITE, (29, 20, 8, 22), border_radius=2)
        pygame.draw.rect(self._screen, UI_WHITE, (23, 27, 20, 8), border_radius=2)

        eyebrow = self._eyebrow_font.render(
            f"CASE {self._patient_number:02d}",
            True,
            UI_GOLD,
        )
        patient_name = self._case_font.render("PATIENT ENCOUNTER", True, UI_WHITE)
        self._screen.blit(eyebrow, (59, 12))
        self._screen.blit(patient_name, (59, 28))

        elapsed = self._status_font.render(f"TIME {format_duration(self.metrics.elapsed_seconds)}", True, UI_GOLD)
        self._screen.blit(elapsed, elapsed.get_rect(topright=(464, 13)))
        pygame.draw.rect(self._screen, UI_TEAL, self._discovered_tests_button, border_radius=5)
        discovered = self._status_font.render(f"TESTS FOUND {len(self.metrics.discovered_tests)}", True, UI_WHITE)
        self._screen.blit(discovered, discovered.get_rect(center=self._discovered_tests_button.center))
        status_text, status_color = self._status(state)
        pygame.draw.rect(self._screen, UI_PANEL, (292, 70, 172, 22), border_radius=5)
        status = self._status_font.render(status_text, True, status_color)
        self._screen.blit(status, status.get_rect(center=(378, 81)))

    def _draw_shadow(self, center: tuple[int, int], width: int) -> None:
        shadow_layer = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        outer = pygame.Rect(0, 0, width, 16)
        outer.center = center
        pygame.draw.ellipse(shadow_layer, (13, 31, 34, 54), outer)
        pygame.draw.ellipse(
            shadow_layer,
            (13, 31, 34, 78),
            outer.inflate(-16, -6),
        )
        self._screen.blit(shadow_layer, (0, 0))

    def _draw_characters(
        self,
        patient_frame: pygame.Surface,
        player_frame: pygame.Surface,
        state: str,
    ) -> None:
        motion_time = time.monotonic()
        patient_bob = round(math.sin(motion_time * 2.6) * 2)
        lean, lift = self._celebration.pose(motion_time)
        if lean:
            patient_frame = pygame.transform.rotate(patient_frame, lean)
        player_bob = round(math.sin(motion_time * 2.2 + 1.4))
        patient_rect = patient_frame.get_rect(midbottom=(330, 273 + patient_bob - lift))
        player_rect = player_frame.get_rect(midbottom=(122, 283 + player_bob))
        if self.push_to_talk:
            aura = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
            pulse = (math.sin(motion_time * 7) + 1) / 2
            for index in range(2):
                phase = (pulse + index * 0.45) % 1
                ring = pygame.Rect(0, 0, round(78 + phase * 28), round(18 + phase * 8))
                ring.center = (player_rect.centerx, 276)
                pygame.draw.ellipse(
                    aura,
                    (*UI_CORAL, round(125 * (1 - phase))),
                    ring,
                    width=2,
                )
            self._screen.blit(aura, (0, 0))
        self._draw_shadow((patient_rect.centerx, 270 + patient_bob), 78)
        self._draw_shadow((player_rect.centerx, 279), 88)
        self._screen.blit(patient_frame, patient_rect)
        self._screen.blit(player_frame, player_rect)

        if state == "talking":
            bubble = pygame.Rect(patient_rect.right - 6, patient_rect.top - 14, 48, 28)
            pygame.draw.rect(self._screen, UI_WHITE, bubble, border_radius=14)
            pygame.draw.polygon(
                self._screen,
                UI_WHITE,
                (
                    (bubble.x + 7, bubble.bottom - 3),
                    (bubble.x + 3, bubble.bottom + 7),
                    (bubble.x + 17, bubble.bottom - 2),
                ),
            )
            for index, offset in enumerate((0, 10, 20)):
                dot_bob = round(math.sin(motion_time * 7 + index * 0.9) * 2)
                pygame.draw.circle(
                    self._screen,
                    UI_TEAL,
                    (bubble.x + 14 + offset, bubble.centery + dot_bob),
                    3,
                )

    def _draw_celebration(self) -> None:
        now = time.monotonic()
        if not self._celebration.active(now):
            return
        layer = self._celebration_layer
        layer.fill((0, 0, 0, 0))
        # Effects stay inside the scene, clear of status, transcripts and inputs.
        layer.set_clip(pygame.Rect(16, 96, 448, 194))
        self._celebration.draw_confetti(layer, now, (UI_CORAL, UI_GOLD, UI_MINT, UI_TEAL, UI_WHITE))
        bubble = pygame.Rect(182, 99, 280, 47)
        pygame.draw.rect(layer, (*UI_INK, 70), bubble.move(2, 3), border_radius=7)
        pygame.draw.rect(layer, UI_PAPER, bubble, border_radius=7)
        pygame.draw.rect(layer, UI_TEAL, bubble, width=2, border_radius=7)
        pygame.draw.polygon(layer, UI_TEAL, ((343, 145), (354, 154), (362, 145)))
        pygame.draw.polygon(layer, UI_PAPER, ((347, 144), (354, 149), (357, 144)))
        # A small pixel heart matches the existing handheld-game sprite style.
        for row, cells in enumerate(("0110110", "1111111", "1111111", "0111110", "0011100", "0001000")):
            for column, cell in enumerate(cells):
                if cell == "1":
                    pygame.draw.rect(layer, UI_CORAL, (196 + column * 2, 113 + row * 2, 2, 2))
        message = self._text_font.render(self._celebration.thanks.message, True, UI_INK)
        layer.blit(message, (218, 105))
        caption = self._status_font.render("Diagnosis confirmed", True, UI_TEAL)
        layer.blit(caption, (218, 126))
        layer.set_clip(None)
        layer.set_alpha(self._celebration.opacity(now))
        # Preserve Cocoa's framebuffer alpha when the overlay reaches full opacity.
        self._screen.blit(layer, (0, 0), special_flags=pygame.BLEND_ALPHA_SDL2)

    def _draw_send_icon(self) -> None:
        center_x, center_y = self._text_send_button.center
        pygame.draw.line(
            self._screen,
            UI_WHITE,
            (center_x - 8, center_y),
            (center_x + 7, center_y),
            3,
        )
        pygame.draw.polygon(
            self._screen,
            UI_WHITE,
            (
                (center_x + 10, center_y),
                (center_x + 2, center_y - 7),
                (center_x + 2, center_y + 7),
            ),
        )

    def _draw_console(self, state: str) -> None:
        pygame.draw.rect(self._screen, UI_PANEL, (0, 374, 480, 106))
        pygame.draw.rect(self._screen, UI_TEAL, (0, 374, 480, 3))
        pygame.draw.rect(self._screen, UI_CORAL, (0, 374, 92, 3))

        enabled = self._ready and self._disease is not None and not self.won
        pygame.draw.rect(self._screen, UI_TEAL if enabled else (91, 119, 116), self._diagnose_button, border_radius=5)
        label = self._status_font.render("FINISH VISIT" if self._diagnosis_confirmed.is_set() else "DIAGNOSE", True, UI_WHITE)
        self._screen.blit(label, label.get_rect(center=self._diagnose_button.center))
        pygame.draw.rect(self._screen, UI_TEAL if enabled else (91, 119, 116), self._care_button, border_radius=5)
        care_label = self._status_font.render("CARE PLAN", True, UI_WHITE)
        self._screen.blit(care_label, care_label.get_rect(center=self._care_button.center))
        pygame.draw.rect(self._screen, UI_TEAL if enabled else (91, 119, 116), self._skills_button, border_radius=5)
        skills_label = self._status_font.render("SKILLS / F5", True, UI_WHITE)
        self._screen.blit(skills_label, skills_label.get_rect(center=self._skills_button.center))
        helper_enabled = self._ready and not self.won
        helper_hovered = self._pokedex_button.collidepoint(self._screen_position(pygame.mouse.get_pos()))
        helper_color = UI_WHITE if helper_hovered else UI_PAPER
        pygame.draw.rect(self._screen, helper_color if helper_enabled else (193, 211, 207), self._pokedex_button, border_radius=5)
        pygame.draw.rect(self._screen, UI_TEAL, self._pokedex_button, width=1, border_radius=5)
        pokedex_label = self._status_font.render("Dragon Copilot", True, UI_INK)
        brand_width = pokedex_label.get_width() + 32
        brand_x = self._pokedex_button.centerx - brand_width // 2
        if self._pokedex.button_logo is not None:
            self._screen.blit(self._pokedex.button_logo, (brand_x, self._pokedex_button.y + 2))
        self._screen.blit(pokedex_label, pokedex_label.get_rect(midleft=(brand_x + 32, self._pokedex_button.centery)))
        status_text, status_color = self._status(state)
        compact_status = self._status_font.render(status_text, True, status_color)
        status_x = 462 - compact_status.get_width()
        pygame.draw.circle(self._screen, status_color, (status_x - 10, 468), 3)
        self._screen.blit(compact_status, (status_x, 461))

        input_border = UI_CORAL if self._text_focused else (91, 119, 116)
        pygame.draw.rect(
            self._screen,
            UI_PAPER,
            self._text_input_rect,
            border_radius=5,
        )
        pygame.draw.rect(
            self._screen,
            input_border,
            self._text_input_rect,
            width=2,
            border_radius=5,
        )
        input_text = self._text_input or "Message the patient..."
        input_color = UI_INK if self._text_input else (104, 123, 120)
        input_rtl = is_rtl(input_text)
        rendered_input = self._text_font.render(get_display(input_text), True, input_color)
        input_area = self._text_input_rect.inflate(-18, -8)
        input_x = input_area.x
        if input_rtl:
            input_x = max(input_area.x, input_area.right - rendered_input.get_width())
        elif rendered_input.get_width() > input_area.width:
            input_x = input_area.right - rendered_input.get_width()
        previous_clip = self._screen.get_clip()
        self._screen.set_clip(input_area)
        input_rect = rendered_input.get_rect(midleft=(input_x, input_area.centery))
        if self._text_focused and self._text_editing.selected_all:
            pygame.draw.rect(self._screen, (193, 231, 215), input_rect)
        self._screen.blit(rendered_input, input_rect)
        if (
            self._text_focused
            and int(time.monotonic() * 2) % 2 == 0
        ):
            cursor_x = min(input_rect.right + 2, input_area.right - 2) if self._text_input else input_area.x
            if input_rtl:
                cursor_x = max(input_area.x, input_rect.left - 2)
            pygame.draw.line(
                self._screen,
                UI_CORAL,
                (cursor_x, input_area.y + 3),
                (cursor_x, input_area.bottom - 3),
                2,
            )
        self._screen.set_clip(previous_clip)

        mouse_position = self._screen_position(pygame.mouse.get_pos())
        send_color = (
            (248, 126, 101)
            if self._text_send_button.collidepoint(mouse_position)
            else UI_CORAL
        )
        if not self._text_input.strip() or not self._ready:
            send_color = (91, 119, 116)
        pygame.draw.rect(
            self._screen,
            send_color,
            self._text_send_button,
            border_radius=5,
        )
        self._draw_send_icon()

    def _draw_diagnosis(self) -> None:
        overlay = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        overlay.fill((8, 20, 23, 166))
        self._screen.blit(overlay, (0, 0))
        pygame.draw.rect(self._screen, UI_PAPER, (28, 112, 424, 230), border_radius=8)
        pygame.draw.rect(self._screen, UI_TEAL, (28, 112, 424, 6))
        title = self._test_title_font.render("FINAL DIAGNOSIS", True, UI_INK)
        self._screen.blit(title, (48, 137))
        pygame.draw.rect(self._screen, UI_WHITE, self._diagnosis_input_rect, border_radius=5)
        pygame.draw.rect(self._screen, UI_CORAL if self._diagnosis_focus == 0 else UI_MUTED, self._diagnosis_input_rect, width=2, border_radius=5)
        area = self._diagnosis_input_rect.inflate(-18, -8)
        text = self._text_font.render(self._diagnosis_input or "Diagnosis", True, UI_INK if self._diagnosis_input else UI_MUTED)
        position = text.get_rect(midleft=(area.x, area.centery))
        if self._diagnosis_input and position.width > area.width:
            position.right = area.right - 3
        previous_clip = self._screen.get_clip()
        self._screen.set_clip(area)
        if self._diagnosis_focus == 0 and self._diagnosis_editing.selected_all:
            pygame.draw.rect(self._screen, (193, 231, 215), position)
        self._screen.blit(text, position)
        if self._diagnosis_focus == 0 and int(time.monotonic() * 2) % 2 == 0:
            cursor_x = min(position.right + 2, area.right - 2) if self._diagnosis_input else area.x
            pygame.draw.line(self._screen, UI_CORAL, (cursor_x, area.y + 3), (cursor_x, area.bottom - 3), 2)
        self._screen.set_clip(previous_clip)
        for index, line in enumerate(wrap_text(self._diagnosis_feedback, self._text_font, 384)):
            self._screen.blit(self._text_font.render(line, True, UI_INK), (48, 230 + index * 19))
        for focus, rectangle, label in (
            (1, self._diagnosis_cancel_button, "Cancel"),
            (2, self._diagnosis_submit_button, "Submit Diagnosis"),
        ):
            color = UI_TEAL if focus == 2 and self._diagnosis_input.strip() else (91, 119, 116)
            pygame.draw.rect(self._screen, color, rectangle, border_radius=5)
            if self._diagnosis_focus == focus:
                pygame.draw.rect(self._screen, UI_CORAL, rectangle, width=2, border_radius=5)
            rendered = self._text_font.render(label, True, UI_WHITE)
            self._screen.blit(rendered, rendered.get_rect(center=rectangle.center))

    def _handle_diagnosis_event(self, event: pygame.event.Event) -> None:
        key = event.key if event.type == pygame.KEYDOWN else None
        if key == pygame.K_ESCAPE:
            self._close_diagnosis()
        elif key == pygame.K_TAB:
            direction = -1 if getattr(event, "mod", 0) & pygame.KMOD_SHIFT else 1
            self._focus_diagnosis((self._diagnosis_focus + direction) % 3)
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self._diagnosis_focus == 1:
                self._close_diagnosis()
            elif self._diagnosis_focus in (0, 2):
                self._submit_diagnosis()
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            position = self._screen_position(event.pos)
            if self._diagnosis_input_rect.collidepoint(position):
                self._focus_diagnosis(0)
            elif self._diagnosis_cancel_button.collidepoint(position):
                self._close_diagnosis()
            elif self._diagnosis_submit_button.collidepoint(position):
                self._submit_diagnosis()
        elif self._diagnosis_focus == 0:
            updated = self._diagnosis_editing.handle_event(event, self._diagnosis_input)
            if updated is not None:
                self._diagnosis_input = updated
                self._diagnosis_feedback = self._diagnosis_editing.error

    def _draw_scene_fade(self) -> None:
        now = time.monotonic()
        intro_progress = (now - self._scene_started_at) / SCENE_FADE_SECONDS
        if intro_progress < 1:
            alpha = round(255 * (1 - max(0.0, intro_progress)) ** 2)
            overlay = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
            overlay.fill((*UI_INK, alpha))
            self._screen.blit(overlay, (0, 0))

    def _wrapped_lines(
        self,
        text: str,
        font: pygame.font.Font,
        max_width: int,
    ) -> list[str]:
        return wrap_text(text, font, max_width)

    def _draw_evidence(self) -> None:
        if self._test_result is None:
            return

        overlay = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        overlay.fill((8, 20, 23, 166))
        self._screen.blit(overlay, (0, 0))

        shadow = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        pygame.draw.rect(
            shadow,
            (4, 14, 16, 95),
            (34, 38, 420, 400),
            border_radius=8,
        )
        self._screen.blit(shadow, (0, 0))
        popup = pygame.Rect(28, 30, 424, 400)
        pygame.draw.rect(self._screen, UI_PAPER, popup, border_radius=8)
        pygame.draw.rect(
            self._screen,
            UI_PANEL,
            (popup.x, popup.y, popup.width, 66),
            border_top_left_radius=8,
            border_top_right_radius=8,
        )
        pygame.draw.rect(self._screen, UI_GOLD, (popup.x, popup.y, 6, 66))

        eyebrow = self._eyebrow_font.render("EVIDENCE REPORT", True, UI_GOLD)
        title = self._test_title_font.render(
            "TEST RESULT",
            True,
            UI_WHITE,
        )
        self._screen.blit(eyebrow, (48, 44))
        self._screen.blit(title, (48, 61))
        pygame.draw.rect(
            self._screen,
            (42, 66, 68),
            self._test_close_button,
            border_radius=5,
        )
        pygame.draw.line(
            self._screen,
            UI_WHITE,
            (408, 57),
            (422, 70),
            2,
        )
        pygame.draw.line(
            self._screen,
            UI_WHITE,
            (422, 57),
            (408, 70),
            2,
        )

        if self._skill_result_panel is not None:
            self._evidence_max_scroll = self._skill_result_panel.draw(self._screen, self._evidence_scroll)
            self._evidence_scroll = min(self._evidence_scroll, self._evidence_max_scroll)
            return

        content = pygame.Rect(48, 116, 384, 286)
        pygame.draw.rect(self._screen, (232, 241, 235), content, border_radius=6)
        title_lines = wrap_text(self._test_result.description, self._test_title_font, content.width - 44)
        result_lines = [] if self._test_result_image and not self._test_result.evidence_image else wrap_text(self._test_result.results, self._test_result_font, content.width - 44)
        title_height = len(title_lines) * 23 + 16
        result_height = len(result_lines) * 28
        image = None
        if self._test_result_image:
            image = self._test_result_image
            scale = min(
                content.width * 0.88 / image.get_width(),
            210 / image.get_height(),
            )
            image = pygame.transform.smoothscale(
                image,
                (
                    round(image.get_width() * scale),
                    round(image.get_height() * scale),
                ),
            )
            result_height += image.get_height() + 12
        self._evidence_max_scroll = max(0, title_height + result_height + 32 - content.height)
        self._evidence_scroll = min(self._evidence_scroll, self._evidence_max_scroll)
        previous_clip = self._screen.get_clip()
        self._screen.set_clip(content.inflate(-12, -12))
        start_y = content.y + 16 - self._evidence_scroll
        for index, line in enumerate(title_lines):
            self._screen.blit(self._test_title_font.render(line, True, UI_TEAL), (70, start_y + index * 23))
        start_y += title_height
        if image:
            self._screen.blit(image, image.get_rect(midtop=(content.centerx, start_y)))
            start_y += image.get_height() + 12
        for index, line in enumerate(result_lines):
            self._screen.blit(self._test_result_font.render(line, True, UI_INK), (70, start_y + index * 28))
        self._screen.set_clip(previous_clip)
        if self._evidence_max_scroll:
            pygame.draw.rect(self._screen, UI_MUTED, (440, 116, 3, 286))
            offset = round(246 * self._evidence_scroll / self._evidence_max_scroll)
            pygame.draw.rect(self._screen, UI_TEAL, (440, 116 + offset, 3, 40))

    def _draw_win(self) -> None:
        overlay = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        overlay.fill((8, 20, 23, 138))
        self._screen.blit(overlay, (0, 0))
        panel = pygame.Rect(62, 112, 356, 220)
        pygame.draw.rect(self._screen, UI_PAPER, panel, border_radius=8)
        pygame.draw.rect(self._screen, UI_TEAL, (62, 112, 356, 6))
        pygame.draw.circle(self._screen, UI_TEAL, (240, 175), 30)
        pygame.draw.lines(
            self._screen,
            UI_WHITE,
            False,
            ((226, 175), (236, 185), (256, 163)),
            5,
        )
        eyebrow = self._eyebrow_font.render(
            "DIAGNOSIS CONFIRMED",
            True,
            UI_TEAL,
        )
        win_text = self._win_font.render("CASE CLOSED", True, UI_INK)
        self._screen.blit(eyebrow, eyebrow.get_rect(center=(240, 225)))
        self._screen.blit(win_text, win_text.get_rect(center=(240, 260)))
        detail = self._text_font.render(
            "Diagnosis confirmed.",
            True,
            (83, 104, 101),
        )
        self._screen.blit(detail, detail.get_rect(center=(240, 300)))

    def _draw_review(self) -> None:
        self._screen.fill(UI_PAPER)
        pygame.draw.rect(self._screen, UI_PANEL, (0, 0, 480, 92))
        eyebrow = self._eyebrow_font.render(f"CASE {self._patient_number:02d} / MAI-Thinking-1", True, UI_GOLD)
        self._screen.blit(eyebrow, (18, 13))
        title = self._test_title_font.render("CONSULTATION REVIEW", True, UI_WHITE)
        self._screen.blit(title, (18, 33))
        metrics = self._status_font.render(
            f"TIME {format_duration(self.metrics.elapsed_seconds)}    TESTS DISCOVERED {len(self.metrics.discovered_tests)} / {self._available_test_count}",
            True, UI_MINT,
        )
        self._screen.blit(metrics, (18, 64))
        lines: list[tuple[str, pygame.font.Font, tuple[int, int, int]]] = []

        def append(text: str, *, heading: bool = False, compact: bool = False) -> None:
            font = self._status_font if heading else self._text_font
            color = UI_TEAL if heading else UI_INK
            for line in wrap_text(text, font, 420):
                lines.append((line, font, color))
            if not compact:
                lines.append(("", font, color))

        append("VISIT SCORES", heading=True)
        if self.skill_engine is not None:
            append(f"Appropriate-use points: {self.skill_engine.score:+d}", heading=True, compact=True)
        append(
            f"Tests discovered: {len(self.metrics.discovered_tests)}/{self._available_test_count}",
            heading=True, compact=True,
        )
        scores = {axis.key: axis for axis in self._scorecard.axes} if self._scorecard else {}
        for key, label in SCORE_AXES.items():
            if key == "diagnostic_reasoning":
                label = "Clinical knowledge"
            axis = scores.get(key)
            if self._review_loading:
                value = "Pending"
            elif self._review_error or axis is None:
                value = "Unavailable"
            elif axis.score is None:
                value = "0/100"
            else:
                value = f"{axis.score * 20}/100"
            append(f"{label}: {value}", heading=True, compact=True)
        append("")

        if self._review_loading:
            append("Scoring consultation...", heading=True)
        elif self._review_error:
            append("SCORING UNAVAILABLE", heading=True)
            append(self._review_error)
        elif self._scorecard is not None:
            append(self._scorecard.summary)
            for axis in self._scorecard.axes:
                score = "0/100 (insufficient evidence)" if axis.score is None else f"{axis.score * 20}/100"
                append(f"{SCORE_AXES[axis.key]}: {score}", heading=True)
                append(axis.feedback)
        if self.skill_engine is not None:
            append("DETERMINISTIC APPROPRIATE USE (NOT AI)", heading=True)
            append(str(self.skill_engine.summary()["formula"]))
            for action in self.skill_engine.actions:
                append(f"{action.get('name', action.get('skill_id', 'Skill'))}: {action.get('status', '')}, {action.get('points', 0):+d}")
                append(f"Parameters: {json.dumps(action.get('parameters', {}))}")
                append(action.get("result", ""))
                append(action.get("rationale", ""))
        append("USED SKILLS / DESCRIPTIVE RESULTS", heading=True)
        for name, result in self.metrics.discovered_tests.items():
            append(f"{name}: {result}")
        if not self.metrics.discovered_tests:
            append("No tests discovered.")
        append("PRESCRIPTIONS", heading=True)
        for prescription in self.metrics.care_plan.prescriptions:
            append(prescription.message())
        if not self.metrics.care_plan.prescriptions:
            append("No prescriptions recorded.")
        append("REFERRALS", heading=True)
        for referral in self.metrics.care_plan.referrals:
            append(referral.message())
        if not self.metrics.care_plan.referrals:
            append("No referrals recorded.")
        append("AI-generated game feedback", heading=True)
        area = pygame.Rect(18, 104, 430, 302)
        self._review_max_scroll = max(0, len(lines) * 21 - area.height)
        self._review_scroll = min(self._review_scroll, self._review_max_scroll)
        previous_clip = self._screen.get_clip()
        self._screen.set_clip(area)
        for index, (line, font, color) in enumerate(lines):
            self._screen.blit(font.render(line, True, color), (area.x, area.y + index * 21 - self._review_scroll))
        self._screen.set_clip(previous_clip)
        if self._review_max_scroll:
            pygame.draw.rect(self._screen, UI_MUTED, (459, 104, 3, 302))
            offset = round(262 * self._review_scroll / self._review_max_scroll)
            pygame.draw.rect(self._screen, UI_TEAL, (459, 104 + offset, 3, 40))
        pygame.draw.rect(self._screen, UI_PANEL, (0, 416, 480, 64))
        if self._review_error and not self._review_loading:
            pygame.draw.rect(self._screen, UI_CORAL, self._review_retry_button, border_radius=5)
            retry = self._text_font.render("Retry", True, UI_WHITE)
            self._screen.blit(retry, retry.get_rect(center=self._review_retry_button.center))
        button_color = UI_MUTED if self._review_loading else UI_TEAL
        pygame.draw.rect(self._screen, button_color, self._review_return_button, border_radius=5)
        button_text = "Scoring..." if self._review_loading else "Return to Hospital"
        label = self._text_font.render(button_text, True, UI_WHITE)
        label_rect = label.get_rect(center=self._review_return_button.center)
        if self._review_loading:
            label_rect.centerx += 10
            draw_spinner(self._screen, (label_rect.left - 14, label_rect.centery), 7, UI_WHITE)
        self._screen.blit(label, label_rect)

    def _handle_review_event(self, event: pygame.event.Event, stop: asyncio.Event) -> None:
        key = event.key if event.type == pygame.KEYDOWN else None
        clicked = event.type == pygame.MOUSEBUTTONUP and event.button == 1
        position = self._screen_position(event.pos) if clicked else (-1, -1)
        if key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_KP_ENTER) or (clicked and self._review_return_button.collidepoint(position)):
            if not self._review_loading:
                stop.set()
        elif self._review_error and not self._review_loading and (key == pygame.K_F5 or (clicked and self._review_retry_button.collidepoint(position))):
            self._review_loading = True
            self._review_retry.set()
        elif event.type == pygame.MOUSEWHEEL:
            self._review_scroll = max(0, min(self._review_max_scroll, self._review_scroll - event.y * 42))
        elif key in (pygame.K_DOWN, pygame.K_UP, pygame.K_PAGEDOWN, pygame.K_PAGEUP):
            delta = 126 if key in (pygame.K_DOWN, pygame.K_PAGEDOWN) else -126
            self._review_scroll = max(0, min(self._review_max_scroll, self._review_scroll + delta))

    def draw(self, animation_time: float = 0.0) -> None:
        if self._review_open:
            self._draw_review()
            pygame.transform.scale(self._screen, self._window.get_size(), self._window)
            pygame.display.flip()
            return
        state = self._current_state()
        patient_frame = self._centered_frame(state, animation_time)
        player_frame = self._player_frame(animation_time)

        self._screen.fill(UI_PAPER)
        self._screen.blit(self._world, (0, 0))
        self._draw_characters(patient_frame, player_frame, state)
        self._draw_equipment_drawer_hint()
        self._draw_celebration()
        self._draw_header(state)
        self._draw_transcript()
        self._draw_console(state)
        if self._care_form is not None and not self.won:
            self._care_form.draw(self._screen)
        if self._test_result and not self.won:
            self._draw_evidence()
        if self._diagnosis_open and not self.won:
            self._draw_diagnosis()
        if self.won:
            self._draw_win()
        self._draw_scene_fade()
        if self._menu is not None and not self.evidence_open and not self.won:
            self._menu.draw(self._screen)
        if self._skill_browser is not None:
            self._skill_browser.draw(self._screen)
        if self._skill_confirmation is not None:
            self._advance_skill_activity(time.monotonic())
            self._skill_confirmation.draw(self._screen)
        if self._pokedex.open and not self.evidence_open and not self.won:
            self._pokedex.draw(self._screen)

        pygame.transform.scale(self._screen, self._window.get_size(), self._window)
        pygame.display.flip()

    def handle_event(self, event: pygame.event.Event, stop: asyncio.Event) -> None:
        if event.type in (pygame.WINDOWLEAVE, pygame.WINDOWFOCUSLOST):
            self._drawer_hovered = False
            self._advanced_drawer_hovered = False
        if event.type == pygame.QUIT:
            self._celebration.dismiss()
            self._pokedex.hide()
            self.quit_requested = True
            self._running = False
            self._push_to_talk.clear()
            stop.set()
            return
        if event.type == pygame.WINDOWFOCUSLOST:
            if isinstance(self._skill_confirmation, InstrumentActivity):
                self._skill_confirmation.handle_event(event, (-1, -1))
            self._set_text_focus(False)
            if self._pokedex.open:
                self._pokedex.focus(False)
            if self._care_form is not None:
                self._care_form.focus(-1)
            if self._diagnosis_open:
                self._focus_diagnosis(-1)
            return
        if event.type == pygame.KEYUP and event.key in (pygame.K_LSHIFT, pygame.K_RSHIFT):
            self._push_to_talk.clear()
            return
        if self._review_open:
            self._handle_review_event(event, stop)
            return
        if self.won:
            return
        key = event.key if event.type == pygame.KEYDOWN else None
        clicked = event.type == pygame.MOUSEBUTTONUP and event.button == 1
        position = self._screen_position(event.pos) if hasattr(event, "pos") else (-1, -1)
        if event.type == pygame.MOUSEMOTION:
            self._drawer_hovered = self._equipment_drawer_hotspot.collidepoint(position)
            self._advanced_drawer_hovered = self._advanced_drawer_hotspot.collidepoint(position)
        if self._skill_confirmation is not None:
            decision = self._skill_confirmation.handle_event(event, position)
            if decision is not None and self._skill_decision is not None and not self._skill_decision.done():
                self._skill_decision.set_result(decision)
            return
        if self._skill_browser is not None:
            action = self._skill_browser.handle_event(event, position)
            if action is not None:
                self._skill_browser = None
                if action[0] == "activity":
                    self._set_text_focus(self._skills_chat_was_focused)
                    self._request_local_skill(action[1])
                    return
                if action[0] == "draft":
                    self._text_input = (self._text_input + " " + action[1]).strip()
                self._set_text_focus(action[0] == "draft" or self._skills_chat_was_focused)
            return
        if self.evidence_open:
            if key in (pygame.K_ESCAPE, pygame.K_RETURN, pygame.K_KP_ENTER) or (
                clicked and self._test_close_button.collidepoint(self._screen_position(event.pos))
            ):
                self.close_test_result()
            elif self._skill_result_panel is not None and self._skill_result_panel.toggle_details(event, position):
                self._evidence_scroll = 0
            elif event.type == pygame.MOUSEWHEEL:
                self._evidence_scroll = max(0, min(self._evidence_max_scroll, self._evidence_scroll - event.y * 36))
            elif key in (pygame.K_DOWN, pygame.K_PAGEDOWN, pygame.K_UP, pygame.K_PAGEUP):
                delta = 120 if key in (pygame.K_DOWN, pygame.K_PAGEDOWN) else -120
                self._evidence_scroll = max(0, min(self._evidence_max_scroll, self._evidence_scroll + delta))
            return
        if self._menu is not None:
            choice = self._menu.handle_event(event, self._window.get_size())
            if key == pygame.K_ESCAPE and "Keep Consulting" in self._menu.choices:
                choice = "Keep Consulting"
            if choice == "Keep Consulting":
                if self._menu.title == "CARE PLAN":
                    self._set_text_focus(self._care_chat_was_focused)
                self._menu = None
            elif choice == "Finish Visit":
                self._menu = None
                self._finish_consultation()
            elif choice in ("Add Prescription", "Add Referral"):
                self._menu = None
                self._care_form = CareOrderForm("prescription" if choice == "Add Prescription" else "referral")
            elif choice == "Review Orders":
                self._menu = None
                self.show_test_result(Test("Care plan", self.metrics.care_plan.summary()))
            elif choice in ("Return to Hospital", "Retry", "Sign in to Azure"):
                self.sign_in_requested = choice == "Sign in to Azure"
                self.retry_requested = choice in ("Retry", "Sign in to Azure")
                self._running = False
                stop.set()
            return
        if self._care_form is not None:
            order = self._care_form.handle_event(event, self._window.get_size())
            if order is not None:
                self._record_care_order(order)
            if self._care_form.closed:
                self._care_form = None
                self._set_text_focus(self._care_chat_was_focused)
            return
        if self._diagnosis_open:
            self._handle_diagnosis_event(event)
            return
        if self._pokedex.open:
            position = self._screen_position(event.pos) if clicked else (-1, -1)
            self._pokedex.handle_event(event, position, self._pokedex_context())
            if not self._pokedex.open:
                self._set_text_focus(self._pokedex_chat_was_focused)
            return
        if self._skills_busy or self._local_skill_busy:
            return
        if key == pygame.K_ESCAPE:
            if self._text_focused:
                self._set_text_focus(False)
            else:
                self._push_to_talk.clear()
                self._menu = ChoiceMenu("LEAVE THIS CONSULTATION?", ("Keep Consulting", "Return to Hospital"), "This unfinished case will not be marked complete.")
            return
        if not self._ready:
            return
        if key == pygame.K_F2:
            self._open_diagnosis()
        elif key == pygame.K_F3:
            self._show_discovered_tests()
        elif key == pygame.K_F4:
            self._open_care_menu()
        elif key == pygame.K_F5:
            self._open_skills()
        elif key == pygame.K_F6:
            self._open_pokedex()
        elif key == pygame.K_F7:
            self._open_skills(drawer="equipment")
        elif key == pygame.K_F8:
            self._open_skills(drawer="advanced")
        elif event.type == pygame.MOUSEWHEEL:
            self._transcript_scroll = max(0, self._transcript_scroll + event.y * 2)
        elif key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            self._transcript_scroll = max(0, self._transcript_scroll + (4 if key == pygame.K_PAGEUP else -4))
        elif self._text_focused and event.type in (pygame.TEXTINPUT, pygame.KEYDOWN):
            if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self._submit_text()
            else:
                updated = self._text_editing.handle_event(event, self._text_input)
                if updated is not None:
                    self._text_input = updated
                    if self._text_editing.error:
                        self.add_transcript("Case", self._text_editing.error)
        elif key in (pygame.K_LSHIFT, pygame.K_RSHIFT) and self._microphone_available:
            self._push_to_talk.set()
        elif clicked:
            position = self._screen_position(event.pos)
            if self._equipment_drawer_hotspot.collidepoint(position):
                self._open_skills(drawer="equipment")
            elif self._advanced_drawer_hotspot.collidepoint(position):
                self._open_skills(drawer="advanced")
            elif self._text_input_rect.collidepoint(position):
                self._set_text_focus(True)
            elif self._diagnose_button.collidepoint(position):
                self._open_diagnosis()
            elif self._care_button.collidepoint(position):
                self._open_care_menu()
            elif self._skills_button.collidepoint(position):
                self._open_skills()
            elif self._pokedex_button.collidepoint(position):
                self._open_pokedex()
            elif self._discovered_tests_button.collidepoint(position):
                self._show_discovered_tests()
            elif self._text_send_button.collidepoint(position):
                self._submit_text()
            else:
                self._set_text_focus(False)

    async def run(self, stop: asyncio.Event) -> None:
        animation_started_at = time.monotonic()
        while not stop.is_set() and self._running:
            for event in pygame.event.get():
                self.handle_event(event, stop)
            self.draw(time.monotonic() - animation_started_at)
            await asyncio.sleep(1 / 60)

        stop.set()

    def close(self) -> None:
        if self._skill_decision is not None and not self._skill_decision.done():
            self._skill_decision.cancel()
        self._celebration.dismiss()
        self._pokedex.hide()
        pygame.key.stop_text_input()
        if self._owns_display:
            pygame.quit()


@asynccontextmanager
async def _realtime_authorization(*, sign_in: bool = False) -> AsyncIterator[dict[str, str]]:
    global _cached_realtime_token
    token = _cached_realtime_token
    if sign_in or token is None or token.expires_on <= time.time() + AZURE_TOKEN_REFRESH_MARGIN_SECONDS:
        credential = GameCredential(sign_in=sign_in)
        try:
            token = await credential.get_token(AZURE_OPENAI_SCOPE)
        finally:
            await credential.close()
        _cached_realtime_token = token
    try:
        yield {"Authorization": f"Bearer {token.token}"}
    except (InvalidStatus, httpx.HTTPStatusError) as error:
        if error.response.status_code == 401 and _cached_realtime_token is token:
            _cached_realtime_token = None
            clear_cached_token(AZURE_OPENAI_SCOPE)
        raise


async def _resolve_skill_call(
    call: dict, skills_by_tool: dict[str, str], animator: PatientAnimator,
    is_current: Callable[[], bool] = lambda: True,
    *,
    present_result: EvidencePresenter | None = None,
    evidence_audio: dict[str, str] | None = None,
    origin: str = "conversation",
) -> dict:
    """Validate, obtain real local approval, then execute exactly one proposal."""
    engine = animator.skill_engine
    if engine is None:
        raise RuntimeError("Skill engine has not been initialized")
    if call.get("name") == "get_skill_context":
        try:
            if json.loads(call.get("arguments", "{}")) != {}:
                raise ValueError("get_skill_context takes no parameters.")
        except (ValueError, TypeError) as error:
            return {"status": "clarification_required", "result": str(error), "points": 0}
        return {
            "status": "read_only",
            "turns": [
                {"index": index, "role": "clinician" if speaker == "You" else "patient", "text": text}
                for index, (speaker, text) in enumerate(animator._transcript)
                if speaker in {"You", "Patient"}
            ],
            "result": "Only these recorded turns may be cited. A reference is not proof that a procedure was completed.",
        }
    record = {
        "call_id": call.get("call_id"),
        "tool": call.get("name"),
        "arguments": call.get("arguments"),
        "status": "pending",
        "origin": origin,
    }
    animator.metrics.skill_requests.append(record)
    proposal = None
    evidence = None
    result = None
    restore_focus: bool | None = None
    try:
        tool_name = call.get("name")
        skill_id = skills_by_tool.get(tool_name) if isinstance(tool_name, str) else None
        if skill_id is None:
            raise ValueError("Unknown skill. Choose a stable skill from the shared catalog.")
        arguments = call.get("arguments", "{}")
        if not isinstance(arguments, str):
            raise ValueError("Skill arguments must be a JSON object encoded as text.")
        parameters = json.loads(arguments)
        if not isinstance(parameters, dict):
            raise ValueError("Skill parameters must be an object.")
        proposal = engine.prepare(skill_id, parameters, tuple(animator._transcript))
        # Do not discard another modal or its draft when a tool response arrives.
        while animator._menu or animator._diagnosis_open or animator._care_form or animator._skill_browser or animator.evidence_open or animator._pokedex.open:
            if not is_current():
                break
            await asyncio.sleep(0.02)
        confirmed = False
        if is_current():
            restore_focus = animator._text_focused
            animator._set_text_focus(False)
            confirmed = await animator.confirm_skill(proposal)
            if skill_id in SUPPORTED_ACTIVITIES and proposal["availability"] == "available":
                record["interaction"] = {
                    "kind": "instrument_placement", "target": INSTRUMENTS[skill_id].target,
                    "completed": confirmed and is_current(),
                }
        if not confirmed or not is_current():
            engine.cancel(proposal)
            result = {"status": "cancelled", "name": proposal["name"], "result": "Cancelled before action; no procedure completed.", "points": 0}
        else:
            result = engine.execute(proposal, tuple(animator._transcript))
            record["status"] = result["status"]
            if result["status"] == "completed":
                prior = animator.metrics.discovered_tests.get(result["name"])
                if skill_id == "targeted_pathogen_pcr" and prior:
                    animator.metrics.discovered_tests[result["name"]] = prior + "\n\n" + result["result"]
                else:
                    animator.metrics.discover_test(result["name"], result["result"])
            explanation = f"{result['result']}\n\nAppropriate use: {result.get('points', 0):+d}\n{result.get('rationale', '')}"
            evidence = Test(
                result["name"], explanation, evidence_image=result.get("image_path"),
                audio=(evidence_audio or {}).get(result["name"].casefold()) if result["status"] == "completed" else None,
                skill_summary=SkillResultSummary(
                    skill_id, result["name"], result["result"], result["points"], result["rationale"],
                ) if result["status"] == "completed" else None,
            )
            if present_result is None:
                animator.show_test_result(evidence)
                await animator.wait_for_evidence_close()
            else:
                result.update(await present_result(evidence))
        record["status"] = result["status"]
        animator.add_transcript("Skill", f"{result.get('name', skill_id)}: {result['status']}. {result['result']}")
        return result
    except ValueError as error:
        record["status"] = "clarification"
        record["reason"] = str(error)
        animator.add_transcript("Skill", f"Not performed: {error}")
        return {"status": "clarification_required", "result": str(error), "points": 0}
    except asyncio.CancelledError:
        if evidence is not None and animator._test_result is evidence:
            animator.close_test_result()
        if record["status"] == "pending":
            record["status"] = "interrupted"
            if proposal is not None:
                engine.cancel(proposal)
        elif result is not None:
            # Interruption dismisses the report, not an already completed action.
            animator.add_transcript("Skill", f"{result.get('name', skill_id)}: {result['status']}. {result['result']}")
            return result
        raise
    finally:
        animator.metrics.diagnostic_skills = engine.summary()
        if restore_focus is not None:
            animator._set_text_focus(restore_focus)


async def _perform_local_skill(animator: PatientAnimator, runtime: ConversationRuntime, skill_id: str) -> None:
    """Run an explicit instrument choice without asking the LLM to interpret it."""
    restore_focus = animator._local_skill_chat_was_focused
    try:
        if skill_id not in SUPPORTED_ACTIVITIES:
            raise ValueError("Unsupported hands-on skill.")
        old_skill = runtime.skill_task
        await runtime.interrupt(new_turn=True)
        if old_skill is not None:
            try:
                await old_skill
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
        runtime.user_talking = False
        await runtime.send({"type": "input_audio_buffer.clear"})
        generation = runtime.playback.generation
        result = await _resolve_skill_call(
            {"name": f"propose_{skill_id}", "arguments": "{}", "call_id": f"local-{uuid4().hex}"},
            runtime.skill_tools, animator,
            lambda: generation == runtime.playback.generation and not runtime.stop.is_set() and not animator.won,
            origin="instrument",
        )
        if result["status"] != "cancelled" and not runtime.stop.is_set():
            # There is no model function call to answer. Send only observed facts,
            # not scoring, image paths, or a fabricated function-call output.
            await runtime.send({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user", "content": [{
                    "type": "input_text",
                    "text": "Game procedure record (not a new request): " + json.dumps({
                        key: result[key] for key in ("status", "name", "result") if key in result
                    }),
                }]},
            })
    finally:
        animator._local_skill_busy = False
        animator._set_text_focus(restore_focus)


async def _conversation_session(
    system_prompt: str,
    disease: str,
    patient_index: int,
    tests: Sequence[Test],
    animator: PatientAnimator,
    stop: asyncio.Event,
    *,
    sign_in: bool = False,
    performance_profile: PerformanceProfile | None = None,
) -> bool:
    test_tools, tests_by_tool = _test_tools(tests)
    animator.skill_engine = SkillEngine(tuple(PatientType)[patient_index].name, tests)
    animator._available_test_count = len(animator.skill_engine.catalog)
    if sign_in:
        animator.show_sign_in(
            "Finish Microsoft sign-in in your browser. This consultation will continue automatically.",
            pending=True,
        )
    authorization = _realtime_authorization(sign_in=True) if sign_in else _realtime_authorization()
    async with authorization as headers:
        if sign_in:
            animator._menu = None
        async with _realtime_connection(headers) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": _patient_instructions(
                                system_prompt, disease, animator.skill_engine.review_records
                            ) + ("\n\n" + performance_profile.instructions if performance_profile else ""),
                            "output_modalities": ["audio"],
                            "tools": [
                                *test_tools,
                                SKILL_CONTEXT_TOOL,
                                *(performance_profile.cue_tools if performance_profile else []),
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
                                        "create_response": False,
                                        "interrupt_response": False,
                                    },
                                },
                                "output": {
                                    "voice": PATIENT_VOICES[
                                        tuple(PatientType)[patient_index]
                                    ],
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

            while True:
                raw_message = await asyncio.wait_for(websocket.recv(), REALTIME_OPEN_TIMEOUT_SECONDS)
                event = json.loads(raw_message)
                if event.get("type") == "error":
                    error = event.get("error", {})
                    raise RealtimeSessionError(
                        f"Realtime API error: {error.get('message', error)}"
                    )
                if event.get("type") == "session.updated":
                    break

            loop = asyncio.get_running_loop()
            audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
            microphone_error: asyncio.Future[None] = loop.create_future()

            def enqueue_audio(audio: bytes) -> None:
                try:
                    audio_queue.put_nowait(audio)
                except asyncio.QueueFull:
                    if not microphone_error.done():
                        microphone_error.set_exception(RealtimeSessionError("Microphone queue overflow; retry the consultation"))

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

            sink = DeviceSink()
            try:
                runtime = ConversationRuntime(
                    websocket, animator, stop, {}, sink,
                    profile=performance_profile,
                    skill_tools=tests_by_tool,
                    skill_handler=lambda call, current, present: _resolve_skill_call(
                        call, tests_by_tool, animator, current,
                        present_result=present,
                        evidence_audio={test.description.casefold(): test.audio for test in tests if test.audio},
                    ),
                )
                await _run_connected_session(
                    websocket, animator, stop, runtime, microphone_callback,
                    audio_queue, microphone_error,
                )
                return animator.won
            finally:
                sink.close()
                if not microphone_error.done():
                    microphone_error.cancel()
                elif not microphone_error.cancelled():
                    microphone_error.exception()


async def _show_consultation_review(
    animator: PatientAnimator,
    system_prompt: str,
    disease: str,
    tests: Sequence[Test],
) -> None:
    animator._review_open = True
    animator._review_loading = True
    animator._review_error = ""
    animator._available_test_count = len(load_catalog())
    animator._running = True
    review_stop = asyncio.Event()

    async def request_scorecard() -> ConsultationScorecard:
        async with _realtime_authorization() as headers:
            return await score_consultation(
                tuple(animator._transcript),
                disease,
                system_prompt,
                animator.metrics,
                animator._available_test_count,
                headers,
            )

    async def generate_reviews() -> None:
        while not review_stop.is_set():
            animator._review_loading = True
            animator._review_error = ""
            try:
                animator._scorecard = await asyncio.wait_for(
                    request_scorecard(), REVIEW_TIMEOUT_SECONDS
                )
            except httpx.HTTPStatusError as error:
                animator._review_error = (
                    "Scoring service returned HTTP "
                    f"{error.response.status_code}. Retry scoring or return to the "
                    "hospital. Your diagnosis remains confirmed."
                )
            except (
                AzureError,
                httpx.RequestError,
                TimeoutError,
                asyncio.TimeoutError,
                ValueError,
            ):
                animator._review_error = (
                    "Scoring could not be completed. Retry scoring or return to the "
                    "hospital. Your diagnosis remains confirmed."
                )
            animator._review_loading = False
            animator._review_scroll = 0
            await animator._review_retry.wait()
            animator._review_retry.clear()

    animation = asyncio.create_task(animator.run(review_stop))
    scoring = asyncio.create_task(generate_reviews())
    try:
        done, _ = await asyncio.wait(
            {animation, scoring}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            task.result()
    finally:
        review_stop.set()
        for task in (animation, scoring):
            task.cancel()
        await asyncio.gather(animation, scoring, return_exceptions=True)
        animator._review_open = False


async def _run_connected_session(
    websocket, animator: PatientAnimator, stop: asyncio.Event,
    runtime: ConversationRuntime, microphone_callback, audio_queue, microphone_error,
) -> None:
    with _microphone_stream(microphone_callback) as input_stream:
        animator.set_microphone_available(input_stream is not None)
        animator._ready = True
        animator.metrics.start()
        print("Conversation started." if input_stream is not None else "Conversation started in text-only mode.")

        async def send_microphone_audio() -> None:
            while not stop.is_set():
                audio = await audio_queue.get()
                await runtime.send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio).decode("ascii"),
                })

        async def send_text_messages() -> None:
            while not stop.is_set():
                message = await animator.next_text_message()
                if animator.won:
                    continue
                print(f"\nYou: {message}")
                await runtime.interrupt(new_turn=True)
                runtime.user_talking = False
                while not audio_queue.empty():
                    audio_queue.get_nowait()
                await runtime.send({"type": "input_audio_buffer.clear"})
                await runtime.send({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": message}],
                    },
                })
                runtime.queue_user(None)
                animator._sending = False
                animator._sent_until = time.monotonic() + 1.2

        async def perform_instrument_requests() -> None:
            while not stop.is_set():
                skill_id = await animator._local_skill_requests.get()
                while not audio_queue.empty():
                    audio_queue.get_nowait()
                await _perform_local_skill(animator, runtime, skill_id)

        async def watch_push_to_talk() -> None:
            pressed = False
            while not stop.is_set():
                if animator.push_to_talk and not pressed:
                    runtime.user_talking = True
                    await runtime.interrupt(new_turn=True)
                pressed = animator.push_to_talk
                # VAD commits the voice turn; releasing Shift alone must not
                # create a response for silence.
                await asyncio.sleep(0.005)

        async def finish_consultation() -> None:
            await animator._consultation_finished.wait()
            await runtime.interrupt()
            await asyncio.sleep(RELIEVED_DURATION_SECONDS)
            stop.set()

        async def receive_events() -> None:
            async for raw_message in websocket:
                event = json.loads(raw_message)
                event_type = event.get("type")
                if animator._local_skill_busy and event_type in (
                    "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                    "input_audio_buffer.committed",
                ):
                    continue
                if animator.won and event_type != "conversation.item.input_audio_transcription.completed":
                    continue
                if event_type == "input_audio_buffer.speech_started":
                    if not runtime.user_talking:
                        runtime.user_talking = True
                        await runtime.interrupt(new_turn=True)
                elif event_type == "input_audio_buffer.speech_stopped":
                    runtime.user_talking = False
                elif event_type == "input_audio_buffer.committed":
                    runtime.user_talking = False
                    runtime.queue_user(None)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript", "")
                    animator.add_transcript("You", transcript, event.get("item_id"))
                    print(f"\nYou: {transcript}")
                elif event_type == "error":
                    error = event.get("error", {})
                    if _is_inactive_cancellation(error):
                        continue
                    if error.get("event_id") in runtime.truncate_events and error.get("code") in (
                        "unsupported_event", "unknown_event_type", "invalid_event",
                        "conversation_item_truncate_not_supported",
                    ):
                        runtime.truncate_supported = False
                        print("Server does not support heard-audio truncation.")
                        continue
                    raise RealtimeSessionError(f"Realtime API error: {error.get('message', error)}")
                else:
                    runtime.handle(event)
            if not stop.is_set():
                raise ConnectionError("The patient connection closed unexpectedly")

        tasks = {
            asyncio.create_task(send_text_messages()),
            asyncio.create_task(perform_instrument_requests()),
            asyncio.create_task(receive_events()),
            asyncio.create_task(stop.wait()),
            asyncio.create_task(runtime.playback.run()),
            asyncio.create_task(runtime.finish_responses()),
            asyncio.create_task(runtime.send_user_requests()),
            asyncio.create_task(runtime.process_speech()),
            asyncio.create_task(watch_push_to_talk()),
            asyncio.create_task(finish_consultation()),
        }
        if input_stream is not None:
            tasks.add(asyncio.create_task(send_microphone_audio()))
        try:
            done, _ = await asyncio.wait(tasks | {microphone_error}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _run_conversation(
    system_prompt: str,
    disease: str,
    patient_index: int,
    tests: Sequence[Test],
    *,
    window: pygame.Surface | None = None,
    screen: pygame.Surface | None = None,
    on_skill_score: Callable[[int], None] | None = None,
    performance_profile: PerformanceProfile | None = None,
) -> ConversationResult:
    sign_in = False
    while True:
        animator = PatientAnimator(patient_index, window=window, screen=screen, disease=disease)
        animator._ready = False
        stop = asyncio.Event()
        animation = asyncio.create_task(animator.run(stop))
        profile_options = {"performance_profile": performance_profile} if performance_profile is not None else {}
        session = asyncio.create_task(_conversation_session(system_prompt, disease, patient_index, tests, animator, stop, sign_in=sign_in, **profile_options))
        try:
            done, pending = await asyncio.wait({animation, session}, return_when=asyncio.FIRST_COMPLETED)
            if session in done:
                try:
                    session.result()
                except (AzureError, WebSocketException, OSError, TimeoutError, sd.PortAudioError, RealtimeSessionError, AudioPlaybackError) as error:
                    print(f"Consultation unavailable: {error}")
                    if not animator.won and not stop.is_set():
                        animator.close_test_result()
                        if isinstance(error, AzureSignInRequired):
                            animator.show_sign_in(str(error))
                        elif isinstance(error, InvalidStatus) and error.response.status_code in (401, 403):
                            animator.show_sign_in(
                                "Azure denied access. Sign in with an authorized account, or ask the resource owner for access."
                            )
                        elif isinstance(error, (AudioPlaybackError, sd.PortAudioError)):
                            animator.show_audio_error(str(error))
                        else:
                            animator.show_error("Check the network and audio output, then retry.")
                        await animation
            if animation in done:
                animation.result()
            if animator.won:
                stop.set()
                for task in (session, animation):
                    task.cancel()
                await asyncio.gather(session, animation, return_exceptions=True)
                if on_skill_score is not None and animator.skill_engine is not None:
                    on_skill_score(animator.skill_engine.score)
                if not animator.quit_requested:
                    await _show_consultation_review(animator, system_prompt, disease, tests)
                return ConversationResult.SOLVED_QUIT if animator.quit_requested else ConversationResult.SOLVED
            if animator.quit_requested:
                return ConversationResult.QUIT
            if not animator.retry_requested:
                return ConversationResult.RETURNED
            sign_in = animator.sign_in_requested
        finally:
            stop.set()
            animator._push_to_talk.clear()
            for task in (session, animation):
                task.cancel()
            await asyncio.gather(session, animation, return_exceptions=True)
            await animator._pokedex.shutdown()
            animator.close()


def start_consultation(
    system_prompts: str | Sequence[str],
    disease: str,
    patient_type: PatientType,
    tests: Sequence[Test],
    *,
    window: pygame.Surface | None = None,
    screen: pygame.Surface | None = None,
    on_skill_score: Callable[[int], None] | None = None,
    performance_profile: PerformanceProfile | None = None,
) -> ConversationResult:
    try:
        return asyncio.run(
            _run_conversation(
                _combine_prompts(system_prompts),
                disease,
                _patient_index(patient_type),
                tests,
                window=window,
                screen=screen,
                on_skill_score=on_skill_score,
                performance_profile=performance_profile,
            )
        )
    except KeyboardInterrupt:
        print("\nConversation ended.")
        return ConversationResult.RETURNED


def strat_conversation(
    system_prompts: str | Sequence[str],
    disease: str,
    patient_type: PatientType,
    tests: Sequence[Test],
    *,
    window: pygame.Surface | None = None,
    screen: pygame.Surface | None = None,
    performance_profile: PerformanceProfile | None = None,
) -> bool:
    """Start a patient conversation with diagnostic tools and result popups."""
    return start_consultation(
        system_prompts,
        disease,
        patient_type,
        tests,
        window=window,
        screen=screen,
        performance_profile=performance_profile,
    ) in (ConversationResult.SOLVED, ConversationResult.SOLVED_QUIT)


if __name__ == "__main__":
    strat_conversation(
        "You are a concise, helpful voice assistant.",
        "common cold",
        PatientType.COMMON_COLD_KID,
        [Test("temperature", "38")],
    )