from __future__ import annotations

import json
import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pygame

from src.animation_assets import load_atlas
from src.game_progress import Progress, ProgressStore
from src.game_ui import ChoiceMenu
from src.patient_performance import PerformanceProfile
from src.voice_profile import VoiceProfile
from src.cue_catalog import CueChoice
from src.realtime_conversation import (
    HOSPITAL_MAP_IMAGE,
    PLAYER_FRAME_SIZE,
    PLAYER_SPRITE_SHEET,
    SCREEN_SIZE,
    UI_CORAL,
    UI_GOLD,
    UI_INK,
    UI_MINT,
    UI_MUTED,
    UI_PANEL,
    UI_TEAL,
    UI_WHITE,
    WINDOW_SIZE,
    ConversationResult,
    PatientType,
    Test,
    animation_frame,
    extract_character,
    normalize_character_frames,
    patient_sprite_layout,
    start_consultation,
)


WORLD_MAP_SIZE = (960, 960)
OVERWORLD_CHARACTER_HEIGHT = 92
PLAYER_DIRECTION_SHEET = PLAYER_SPRITE_SHEET.with_name("dr_ash_directions.png")
PLAYER_DIRECTION_ROWS = {"down": 0, "left": 1, "right": 2, "up": 3}
PLAYER_DIRECTION_FRAME_ORDER = {
    "down": (0, 1, 2, 3),
    "left": (0, 2, 3, 2),
    "right": (0, 2, 3, 2),
    "up": (0, 1, 2, 3),
}
PLAYER_RIGHT_ARTIFACT_RECT = (0, 46, PLAYER_FRAME_SIZE[0], 3)
PLAYER_START = (480, 480)
PLAYER_SPEED = 220
PLAYER_SIDE_WALK_CYCLE_DISTANCE = 80
INTERACTION_DISTANCE = 88
PATIENT_COLLISION_DISTANCE = 28
SCENE_FADE_MS = 280
WALKABLE_AREAS = (
    pygame.Rect(0, 398, 960, 138),
    pygame.Rect(405, 0, 140, 960),
)


@dataclass(frozen=True)
class PatientScenario:
    system_prompts: str | tuple[str, ...]
    disease: str
    patient_type: PatientType
    tests: tuple[Test, ...]
    source: Path
    performance_profile: PerformanceProfile | None = None

    def conversation_parameters(self) -> dict[str, Any]:
        parameters = {
            "system_prompts": self.system_prompts,
            "disease": self.disease,
            "patient_type": self.patient_type,
            "tests": self.tests,
        }
        if self.performance_profile is not None:
            parameters["performance_profile"] = self.performance_profile
        return parameters


@dataclass(frozen=True)
class HospitalRoom:
    name: str
    floor: pygame.Rect
    door: pygame.Rect
    unlock_after: PatientType | None = None
    obstacles: tuple[pygame.Rect, ...] = ()
    additional_doors: tuple[pygame.Rect, ...] = ()

    @property
    def doors(self) -> tuple[pygame.Rect, ...]:
        return (self.door, *self.additional_doors)


HOSPITAL_ROOMS = (
    HospitalRoom(
        "Pediatrics",
        pygame.Rect(28, 28, 365, 367),
        pygame.Rect(390, 180, 35, 120),
        obstacles=(
            pygame.Rect(38, 48, 125, 145),
            pygame.Rect(278, 48, 95, 145),
            pygame.Rect(350, 75, 40, 120),
        ),
        additional_doors=(pygame.Rect(180, 370, 90, 60),),
    ),
    HospitalRoom(
        "Diagnostics Lab",
        pygame.Rect(567, 28, 365, 367),
        pygame.Rect(535, 180, 35, 120),
        obstacles=(
            pygame.Rect(580, 34, 310, 125),
            pygame.Rect(895, 140, 37, 190),
        ),
        additional_doors=(pygame.Rect(690, 370, 90, 60),),
    ),
    HospitalRoom(
        "Patient Ward",
        pygame.Rect(28, 578, 365, 348),
        pygame.Rect(390, 690, 35, 120),
        PatientType.COMMON_COLD_KID,
        obstacles=(
            pygame.Rect(32, 590, 65, 190),
            pygame.Rect(220, 590, 160, 105),
            pygame.Rect(335, 665, 58, 120),
            pygame.Rect(28, 825, 55, 95),
        ),
        additional_doors=(pygame.Rect(180, 530, 90, 60),),
    ),
    HospitalRoom(
        "Pharmacy Lounge",
        pygame.Rect(567, 578, 365, 348),
        pygame.Rect(535, 690, 35, 120),
        PatientType.STOMACHACHE_TEEN,
        obstacles=(
            pygame.Rect(590, 580, 320, 155),
            pygame.Rect(580, 825, 60, 100),
            pygame.Rect(650, 855, 195, 60),
        ),
        additional_doors=(pygame.Rect(690, 530, 90, 60),),
    ),
)

PATIENT_POSITIONS = (
    (225, 300),
    (110, 300),
    (650, 350),
    (355, 350),
    (120, 820),
    (850, 302),
    (220, 740),
    (750, 820),
    (310, 850),
    (835, 750),
    (915, 820),
)
PATIENT_ROOM_INDEXES = (0, 0, 1, 0, 2, 1, 2, 3, 2, 3, 3)


def load_patient_scenario(path: Path) -> PatientScenario:
    with path.open(encoding="utf-8") as prompt_file:
        data = json.load(prompt_file)

    if not isinstance(data, dict):
        raise ValueError(f"Patient prompt must contain a JSON object: {path}")

    expected_keys = {"system_prompts", "disease", "patient_type", "tests"}
    if not expected_keys <= set(data) or set(data) - expected_keys - {"performance_profile"}:
        raise ValueError(
            f"Patient prompt must contain {sorted(expected_keys)} and optional performance_profile: {path}"
        )

    system_prompts = data["system_prompts"]
    if isinstance(system_prompts, str):
        if not system_prompts.strip():
            raise ValueError(f"system_prompts must not be empty: {path}")
    elif isinstance(system_prompts, list) and all(
        isinstance(prompt, str) and prompt.strip() for prompt in system_prompts
    ):
        system_prompts = tuple(system_prompts)
    else:
        raise ValueError(f"system_prompts must be a string or string list: {path}")

    disease = data["disease"]
    if not isinstance(disease, str) or not disease.strip():
        raise ValueError(f"disease must be a non-empty string: {path}")

    patient_type_name = data["patient_type"]
    if not isinstance(patient_type_name, str):
        raise ValueError(f"patient_type must be an enum member name: {path}")
    try:
        patient_type = PatientType[patient_type_name]
    except KeyError as error:
        raise ValueError(
            f"Unknown patient_type {patient_type_name!r}: {path}"
        ) from error

    test_data = data["tests"]
    if not isinstance(test_data, list) or not test_data:
        raise ValueError(f"tests must be a non-empty list: {path}")
    try:
        tests = tuple(Test(**test) for test in test_data)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid test configuration: {path}") from error

    profile = None
    if "performance_profile" in data:
        try:
            if not isinstance(data["performance_profile"], dict):
                raise ValueError("performance_profile must be an object")
            profile_data = dict(data["performance_profile"])
            if "voice" in profile_data:
                profile_data["voice"] = VoiceProfile.from_json(profile_data["voice"])
            if "cues" in profile_data:
                if not isinstance(profile_data["cues"], list):
                    raise ValueError("cues must be a list")
                profile_data["cues"] = tuple(CueChoice.from_json(item) for item in profile_data["cues"])
            profile = PerformanceProfile(**profile_data)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid performance profile: {path}: {error}") from error

    return PatientScenario(
        system_prompts=system_prompts,
        disease=disease,
        patient_type=patient_type,
        tests=tests,
        source=path,
        performance_profile=profile,
    )


def load_patient_scenarios(paths: Sequence[Path]) -> tuple[PatientScenario, ...]:
    scenarios = tuple(load_patient_scenario(path) for path in paths)
    if not scenarios:
        raise ValueError("At least one patient prompt is required")

    patient_types = [scenario.patient_type for scenario in scenarios]
    if len(set(patient_types)) != len(patient_types):
        raise ValueError("Each patient_type may appear in only one prompt file")

    patient_order = {
        patient_type: index for index, patient_type in enumerate(PatientType)
    }
    return tuple(
        sorted(scenarios, key=lambda scenario: patient_order[scenario.patient_type])
    )


class HospitalNavigator:
    def __init__(
        self,
        scenarios: Sequence[PatientScenario],
        diagnosed: set[PatientType],
        player_position: tuple[float, float] = PLAYER_START,
        *,
        window: pygame.Surface | None = None,
        screen: pygame.Surface | None = None,
        completion_announced: bool = False,
        notice: str = "",
    ) -> None:
        pygame.init()
        pygame.display.set_caption("Diagnose Em' All - Hospital")
        self._owns_display = window is None
        self._window = (
            pygame.display.set_mode(WINDOW_SIZE) if window is None else window
        )
        self._screen = pygame.Surface(SCREEN_SIZE) if screen is None else screen
        self._clock = pygame.time.Clock()
        self._scenarios = tuple(scenarios)
        if any(
            self._patient_index(scenario.patient_type) >= len(PATIENT_POSITIONS)
            for scenario in self._scenarios
        ):
            raise ValueError("Every patient requires a position in PATIENT_POSITIONS")
        self._diagnosed = diagnosed
        self._player_position = pygame.Vector2(player_position)
        if not self._can_stand(self._player_position):
            self._player_position = pygame.Vector2(PLAYER_START)
        self.completion_announced = completion_announced
        self.restart_requested = False
        self._notice = notice
        self._notice_until = 8.0
        self._menu: ChoiceMenu | None = None
        self._menu_kind = ""
        self._pause_button = pygame.Rect(308, 15, 28, 28)
        if self._scenarios and all(scenario.patient_type in diagnosed for scenario in self._scenarios) and not completion_announced:
            self._menu_kind = "complete"
            self._menu = ChoiceMenu("ALL CASES CLOSED", ("Continue Exploring", "New Game"), f"{len(self._scenarios)} patients helped. Hospital rounds complete.")
        self._facing = "down"
        self._walking = False
        self._animation_time = 0.0
        self._walk_time = 0.0
        self._walk_distance = 0.0
        self._camera_position = self._camera_target()
        self._greeting_patient: PatientType | None = None
        self._greeting_until = 0.0
        self._scene_started_at = pygame.time.get_ticks()

        hospital_map = pygame.image.load(str(HOSPITAL_MAP_IMAGE)).convert()
        self._world = pygame.transform.smoothscale(hospital_map, WORLD_MAP_SIZE)
        self._player_sheet = pygame.image.load(
            str(PLAYER_DIRECTION_SHEET)
        ).convert_alpha()
        raw_player_frames = {
            direction: tuple(
                self._load_player_frame(direction, frame_index)
                for frame_index in range(4)
            )
            for direction in PLAYER_DIRECTION_ROWS
        }
        player_source_size = (
            max(frame.get_width() for frames in raw_player_frames.values() for frame in frames),
            max(frame.get_height() for frames in raw_player_frames.values() for frame in frames),
        )
        normalized_player_frames = {
            direction: normalize_character_frames(
                frames,
                OVERWORLD_CHARACTER_HEIGHT,
                player_source_size,
            )
            for direction, frames in raw_player_frames.items()
        }
        self._player_frames = {
            direction: tuple(
                normalized_player_frames[direction][frame_index]
                for frame_index in PLAYER_DIRECTION_FRAME_ORDER[direction]
            )
            for direction in PLAYER_DIRECTION_ROWS
        }
        self._player_idle_frames: dict[str, tuple[pygame.Surface, ...]] | None = None
        self._load_player_atlases()
        self._patient_frames = {}
        for scenario in self._scenarios:
            raw_patient_frames = {
                state: self._load_patient_frames(scenario.patient_type, state)
                for state in ("idle", "talking", "worried", "relieved")
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
            self._patient_frames[scenario.patient_type] = {
                state: normalize_character_frames(
                    frames,
                    OVERWORLD_CHARACTER_HEIGHT,
                    patient_source_size,
                )
                for state, frames in raw_patient_frames.items()
            }

        self._title_font = pygame.font.SysFont("Avenir Next", 16, bold=True)
        self._label_font = pygame.font.SysFont("Avenir Next", 15, bold=True)
        self._eyebrow_font = pygame.font.SysFont("Avenir Next", 9, bold=True)
        self._small_font = pygame.font.SysFont("Avenir Next", 11)
        self._count_font = pygame.font.SysFont("Avenir Next", 13, bold=True)

    @property
    def player_position(self) -> tuple[float, float]:
        return self._player_position.x, self._player_position.y

    def _patient_index(self, patient_type: PatientType) -> int:
        return list(PatientType).index(patient_type)

    def _load_patient_frames(
        self,
        patient_type: PatientType,
        state: str,
    ) -> tuple[pygame.Surface, ...]:
        patient_index = self._patient_index(patient_type)
        (
            sheet_path,
            (grid_left, grid_right),
            state_bounds,
            frame_count,
            frame_padding,
        ) = patient_sprite_layout(patient_index)
        sheet = pygame.image.load(str(sheet_path)).convert_alpha()
        grid_width = grid_right - grid_left
        top, bottom = state_bounds[state]
        frames = []
        for frame_index in range(frame_count):
            left = max(
                0,
                grid_left
                + round(frame_index * grid_width / frame_count)
                - frame_padding,
            )
            right = min(
                sheet.get_width(),
                grid_left
                + round((frame_index + 1) * grid_width / frame_count)
                + frame_padding,
            )
            frame = sheet.subsurface(
                pygame.Rect(
                    left,
                    top,
                    right - left,
                    bottom - top,
                )
            ).copy()
            frames.append(extract_character(frame))
        return tuple(frames)

    def _load_player_frame(
        self,
        direction: str,
        frame_index: int,
    ) -> pygame.Surface:
        row = PLAYER_DIRECTION_ROWS[direction]
        frame = self._player_sheet.subsurface(
            pygame.Rect(
                frame_index * PLAYER_FRAME_SIZE[0],
                row * PLAYER_FRAME_SIZE[1],
                *PLAYER_FRAME_SIZE,
            )
        ).copy()
        if direction == "right":
            frame.fill((0, 0, 0, 0), PLAYER_RIGHT_ARTIFACT_RECT)
        return extract_character(frame)

    def _player_frame(self, animation_time: float) -> pygame.Surface:
        if not self._walking and self._player_idle_frames is not None:
            frames = self._player_idle_frames[self._facing]
            sequence = (frames[0],) * 14 + (frames[1], frames[2], frames[3], frames[1])
            return animation_frame(sequence, self._animation_time, 6)
        if self._walking and self._facing in ("left", "right") and self._player_idle_frames is not None:
            frames = self._player_frames[self._facing]
            return animation_frame(frames, self._walk_distance / PLAYER_SIDE_WALK_CYCLE_DISTANCE, len(frames))
        return animation_frame(
            self._player_frames[self._facing],
            animation_time,
            7,
        )

    def _load_player_atlases(self) -> None:
        directory = PLAYER_DIRECTION_SHEET.parent
        try:
            walking = load_atlas(directory / "dr_ash_walk_prepared.png", 6)
            idle = load_atlas(directory / "dr_ash_idle_atlas.png", 4)
            frames = [frame for atlas in (walking, idle) for row in atlas for frame in row]
            source_size = (max(frame.get_width() for frame in frames), max(frame.get_height() for frame in frames))
            prepared = [
                {
                    direction: normalize_character_frames(atlas[index], OVERWORLD_CHARACTER_HEIGHT, source_size)
                    for direction, index in PLAYER_DIRECTION_ROWS.items()
                }
                for atlas in (walking, idle)
            ]
            self._player_frames, self._player_idle_frames = prepared
        except (OSError, ValueError, pygame.error) as error:
            warnings.warn(f"Using original player sprites: {error}", RuntimeWarning, stacklevel=2)

    def _scenario_position(self, scenario: PatientScenario) -> pygame.Vector2:
        patient_index = self._patient_index(scenario.patient_type)
        return pygame.Vector2(PATIENT_POSITIONS[patient_index])

    def _scenario_room(self, scenario: PatientScenario) -> HospitalRoom:
        patient_index = self._patient_index(scenario.patient_type)
        return HOSPITAL_ROOMS[PATIENT_ROOM_INDEXES[patient_index]]

    def _room_unlocked(self, room: HospitalRoom) -> bool:
        if room.unlock_after is None or room.unlock_after in self._diagnosed:
            return True
        return not any(
            scenario.patient_type == room.unlock_after
            for scenario in self._scenarios
        )

    def _walkable_areas(self) -> tuple[pygame.Rect, ...]:
        room_areas = tuple(
            area
            for room in HOSPITAL_ROOMS
            if self._room_unlocked(room)
            for area in (room.floor, *room.doors)
        )
        return (*WALKABLE_AREAS, *room_areas)

    def _can_stand(self, position: pygame.Vector2) -> bool:
        half_width = 15
        half_height = 10
        foot_points = (
            (position.x - half_width, position.y - half_height),
            (position.x + half_width, position.y - half_height),
            (position.x - half_width, position.y + half_height),
            (position.x + half_width, position.y + half_height),
        )
        if not all(
            any(area.collidepoint(point) for area in self._walkable_areas())
            for point in foot_points
        ):
            return False

        footprint = pygame.Rect(0, 0, half_width * 2, half_height * 2)
        footprint.center = round(position.x), round(position.y)
        if any(
            obstacle.colliderect(footprint)
            for room in HOSPITAL_ROOMS
            if self._room_unlocked(room)
            for obstacle in room.obstacles
        ):
            return False

        return all(
            position.distance_to(self._scenario_position(scenario))
            >= PATIENT_COLLISION_DISTANCE
            for scenario in self._scenarios
        )

    def move(self, direction: pygame.Vector2, elapsed_seconds: float) -> None:
        if direction.length_squared() == 0:
            self._walking = False
            return

        previous_position = self._player_position.copy()
        if abs(direction.x) > abs(direction.y):
            self._facing = "left" if direction.x < 0 else "right"
        else:
            self._facing = "up" if direction.y < 0 else "down"
        direction = direction.normalize()

        distance = PLAYER_SPEED * elapsed_seconds
        horizontal = self._player_position + pygame.Vector2(direction.x * distance, 0)
        if self._can_stand(horizontal):
            self._player_position = horizontal
        vertical = self._player_position + pygame.Vector2(0, direction.y * distance)
        if self._can_stand(vertical):
            self._player_position = vertical
        self._walking = self._player_position != previous_position

    def nearest_patient(self) -> PatientScenario | None:
        available_scenarios = tuple(
            scenario
            for scenario in self._scenarios
            if self._room_unlocked(self._scenario_room(scenario))
        )
        if not available_scenarios:
            return None
        nearest = min(
            available_scenarios,
            key=lambda scenario: self._player_position.distance_to(
                self._scenario_position(scenario)
            ),
        )
        distance = self._player_position.distance_to(self._scenario_position(nearest))
        return nearest if distance <= INTERACTION_DISTANCE else None

    def nearby_locked_room(self) -> HospitalRoom | None:
        locked_rooms = tuple(
            room for room in HOSPITAL_ROOMS if not self._room_unlocked(room)
        )
        if not locked_rooms:
            return None

        def distance_to_room(room: HospitalRoom) -> float:
            return min(
                self._player_position.distance_to(door.center)
                for door in room.doors
            )

        nearest = min(
            locked_rooms,
            key=distance_to_room,
        )
        return nearest if distance_to_room(nearest) <= 76 else None

    def update(self, direction: pygame.Vector2, elapsed_seconds: float) -> None:
        previous_position = self._player_position.copy()
        self.move(direction, elapsed_seconds)
        self._animation_time += elapsed_seconds
        self._walk_time = self._walk_time + elapsed_seconds if self._walking else 0.0
        self._walk_distance = self._walk_distance + self._player_position.distance_to(previous_position) if self._walking else 0.0
        self._camera_position = self._camera_position.lerp(
            self._camera_target(), 1 - math.exp(-elapsed_seconds / 0.10)
        )
        nearby = self.nearest_patient()
        patient_type = nearby.patient_type if nearby else None
        if patient_type != self._greeting_patient:
            self._greeting_patient = patient_type
            self._greeting_until = self._animation_time + 1.6

    def _camera(self) -> pygame.Vector2:
        return self._camera_position.copy()

    def _camera_target(self) -> pygame.Vector2:
        return pygame.Vector2(
            max(
                0,
                min(
                    self._player_position.x - SCREEN_SIZE[0] / 2,
                    WORLD_MAP_SIZE[0] - SCREEN_SIZE[0],
                ),
            ),
            max(
                0,
                min(
                    self._player_position.y - SCREEN_SIZE[1] / 2,
                    WORLD_MAP_SIZE[1] - SCREEN_SIZE[1],
                ),
            ),
        )

    def _draw_marker(self, position: pygame.Vector2) -> None:
        bounce = round(math.sin(self._animation_time * 4) * 2)
        center = (round(position.x), round(position.y) + bounce)
        pygame.draw.circle(self._screen, UI_INK, center, 13)
        pygame.draw.circle(self._screen, UI_TEAL, center, 11)
        check_points = (
            (center[0] - 5, center[1]),
            (center[0] - 1, center[1] + 4),
            (center[0] + 6, center[1] - 5),
        )
        pygame.draw.lines(
            self._screen,
            UI_WHITE,
            False,
            check_points,
            2,
        )

    def _draw_interaction_ring(self, rectangle: pygame.Rect) -> None:
        pulse = (math.sin(self._animation_time * 5) + 1) / 2
        ring = pygame.Rect(0, 0, round(50 + pulse * 8), round(16 + pulse * 3))
        ring.center = (rectangle.centerx, rectangle.bottom - 2)
        glow = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        pygame.draw.ellipse(glow, (*UI_GOLD, 44), ring.inflate(12, 8))
        pygame.draw.ellipse(glow, (*UI_GOLD, 205), ring, width=2)
        self._screen.blit(glow, (0, 0))

    def _draw_interaction_badge(self, rectangle: pygame.Rect) -> None:
        bounce = round(math.sin(self._animation_time * 5) * 3)
        center = (rectangle.centerx, rectangle.top - 15 + bounce)
        pygame.draw.circle(self._screen, UI_INK, (center[0] + 2, center[1] + 3), 15)
        pygame.draw.circle(self._screen, UI_GOLD, center, 14)
        pygame.draw.line(
            self._screen,
            UI_INK,
            (center[0], center[1] - 7),
            (center[0], center[1] + 2),
            3,
        )
        pygame.draw.circle(self._screen, UI_INK, (center[0], center[1] + 7), 2)

    def _draw_walk_dust(self, rectangle: pygame.Rect) -> None:
        movement = {
            "down": pygame.Vector2(0, 1),
            "left": pygame.Vector2(-1, 0),
            "right": pygame.Vector2(1, 0),
            "up": pygame.Vector2(0, -1),
        }[self._facing]
        dust = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        for offset in (0.0, 0.5):
            phase = (self._animation_time * 4.5 + offset) % 1
            behind = movement * -(8 + phase * 12)
            center = (
                round(rectangle.centerx + behind.x),
                round(rectangle.bottom - 3 + behind.y * 0.25),
            )
            radius = round(2 + phase * 3)
            alpha = round(78 * (1 - phase))
            pygame.draw.circle(dust, (225, 235, 226, alpha), center, radius)
        self._screen.blit(dust, (0, 0))

    def _draw_room_entrances(self, camera: pygame.Vector2) -> None:
        entrance_layer = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        for room in HOSPITAL_ROOMS:
            unlocked = self._room_unlocked(room)
            accent = UI_TEAL if unlocked else UI_CORAL
            for room_door in room.doors:
                door = room_door.move(-round(camera.x), -round(camera.y))
                if not self._screen.get_rect().colliderect(door.inflate(120, 120)):
                    continue

                horizontal = door.width > door.height
                threshold = pygame.Rect(
                    0,
                    0,
                    door.width - 12 if horizontal else 14,
                    14 if horizontal else door.height - 12,
                )
                threshold.center = door.center
                pygame.draw.rect(
                    entrance_layer,
                    (*accent, 72),
                    threshold.inflate(4 if horizontal else 10, 10 if horizontal else 4),
                    border_radius=5,
                )
                pygame.draw.rect(
                    entrance_layer,
                    (*accent, 220),
                    threshold,
                    width=3,
                    border_radius=4,
                )

                if horizontal:
                    direction = -1 if room.floor.centery < room_door.centery else 1
                    chevron_y = door.centery + direction * 28
                    for offset in (0, direction * 9):
                        point_y = chevron_y + offset
                        points = (
                            (door.centerx, point_y + direction * 7),
                            (door.centerx - 7, point_y - direction * 4),
                            (door.centerx + 7, point_y - direction * 4),
                        )
                        pygame.draw.polygon(entrance_layer, (*accent, 210), points)
                    continue

                direction = -1 if room.floor.centerx < room_door.centerx else 1
                chevron_x = door.centerx + direction * 28
                for offset in (0, direction * 9):
                    point_x = chevron_x + offset
                    points = (
                        (point_x + direction * 7, door.centery),
                        (point_x - direction * 4, door.centery - 7),
                        (point_x - direction * 4, door.centery + 7),
                    )
                    pygame.draw.polygon(entrance_layer, (*accent, 210), points)

        self._screen.blit(entrance_layer, (0, 0))

    def _draw_room_labels(self, camera: pygame.Vector2) -> None:
        label_layer = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        for room in HOSPITAL_ROOMS:
            door = room.door.move(-round(camera.x), -round(camera.y))
            if not self._screen.get_rect().colliderect(door.inflate(120, 40)):
                continue

            accent = UI_TEAL if self._room_unlocked(room) else UI_CORAL
            direction = -1 if room.floor.centerx < room.door.centerx else 1
            label = self._eyebrow_font.render(room.name.upper(), True, UI_WHITE)
            plaque = label.get_rect()
            plaque.inflate_ip(14, 10)
            plaque.center = (
                door.centerx + direction * (plaque.width // 2 + 14),
                door.centery - plaque.height // 2 - 14,
            )
            pygame.draw.rect(
                label_layer,
                (*UI_INK, 226),
                plaque,
                border_radius=4,
            )
            pygame.draw.rect(
                label_layer,
                (*accent, 245),
                (plaque.x, plaque.y, 4, plaque.height),
                border_top_left_radius=4,
                border_bottom_left_radius=4,
            )
            label_layer.blit(label, label.get_rect(center=plaque.center))

        self._screen.blit(label_layer, (0, 0))

    def _draw_room_locks(self, camera: pygame.Vector2) -> None:
        for room in HOSPITAL_ROOMS:
            if self._room_unlocked(room):
                continue
            for room_door in room.doors:
                door = room_door.move(-round(camera.x), -round(camera.y))
                if not self._screen.get_rect().colliderect(door):
                    continue

                horizontal = door.width > door.height
                barrier = pygame.Rect(
                    0,
                    0,
                    door.width - 8 if horizontal else 12,
                    12 if horizontal else door.height - 8,
                )
                barrier.center = door.center
                pygame.draw.rect(
                    self._screen,
                    UI_INK,
                    barrier.inflate(4, 4),
                    border_radius=4,
                )
                pygame.draw.rect(self._screen, UI_CORAL, barrier, border_radius=3)
                if horizontal:
                    stripe_positions = range(barrier.left + 8, barrier.right - 4, 12)
                    for stripe in stripe_positions:
                        pygame.draw.line(
                            self._screen,
                            UI_GOLD,
                            (stripe, barrier.top + 2),
                            (stripe + 6, barrier.bottom - 2),
                            2,
                        )
                else:
                    stripe_positions = range(barrier.top + 8, barrier.bottom - 4, 12)
                    for stripe in stripe_positions:
                        pygame.draw.line(
                            self._screen,
                            UI_GOLD,
                            (barrier.left + 2, stripe),
                            (barrier.right - 2, stripe + 6),
                            2,
                        )

                center = barrier.center
                pygame.draw.circle(self._screen, UI_INK, center, 14)
                pygame.draw.circle(self._screen, UI_CORAL, center, 11)
                pygame.draw.arc(
                    self._screen,
                    UI_WHITE,
                    pygame.Rect(center[0] - 5, center[1] - 7, 10, 10),
                    0,
                    math.pi,
                    2,
                )
                pygame.draw.rect(
                    self._screen,
                    UI_WHITE,
                    (center[0] - 6, center[1] - 1, 12, 9),
                    border_radius=2,
                )

    def _draw_scene_fade(self) -> None:
        elapsed = pygame.time.get_ticks() - self._scene_started_at
        if elapsed >= SCENE_FADE_MS:
            return
        progress = max(0.0, elapsed / SCENE_FADE_MS)
        alpha = round(255 * (1 - progress) ** 2)
        overlay = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        overlay.fill((*UI_INK, alpha))
        self._screen.blit(overlay, (0, 0))

    def _fade_out(self) -> None:
        snapshot = self._screen.copy()
        overlay = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        started_at = pygame.time.get_ticks()
        while True:
            progress = (pygame.time.get_ticks() - started_at) / SCENE_FADE_MS
            if progress >= 1:
                progress = 1
            self._screen.blit(snapshot, (0, 0))
            overlay.fill((*UI_INK, round(255 * progress * progress)))
            self._screen.blit(overlay, (0, 0))
            pygame.transform.scale(
                self._screen,
                self._window.get_size(),
                self._window,
            )
            pygame.display.flip()
            if progress >= 1:
                return
            self._clock.tick(60)

    def _draw_header(self) -> None:
        header = pygame.Surface((SCREEN_SIZE[0], 58), pygame.SRCALPHA)
        header.fill((*UI_INK, 232))
        self._screen.blit(header, (0, 0))
        pygame.draw.rect(self._screen, UI_CORAL, (0, 0, SCREEN_SIZE[0], 3))

        icon = pygame.Rect(14, 12, 32, 32)
        pygame.draw.rect(self._screen, UI_CORAL, icon, border_radius=6)
        pygame.draw.rect(self._screen, UI_WHITE, (26, 18, 8, 20), border_radius=2)
        pygame.draw.rect(self._screen, UI_WHITE, (20, 24, 20, 8), border_radius=2)
        eyebrow = self._eyebrow_font.render("ST. ALDER HOSPITAL", True, UI_GOLD)
        title = self._title_font.render("DIAGNOSE EM' ALL", True, UI_WHITE)
        self._screen.blit(eyebrow, (56, 9))
        self._screen.blit(title, (56, 23))
        pygame.draw.rect(self._screen, UI_PANEL, self._pause_button, border_radius=5)
        for horizontal in (317, 325):
            pygame.draw.rect(self._screen, UI_MINT, (horizontal, 23, 3, 12))
        mouse = pygame.mouse.get_pos()
        mouse_position = (mouse[0] * SCREEN_SIZE[0] / self._window.get_width(), mouse[1] * SCREEN_SIZE[1] / self._window.get_height())
        if self._pause_button.collidepoint(mouse_position) and self._menu is None:
            tooltip = self._small_font.render("Pause", True, UI_WHITE)
            rectangle = tooltip.get_rect(midtop=(322, 61)).inflate(12, 8)
            pygame.draw.rect(self._screen, UI_PANEL, rectangle, border_radius=4)
            self._screen.blit(tooltip, tooltip.get_rect(center=rectangle.center))

        count = self._count_font.render(
            f"{len(self._diagnosed):02d} / {len(self._scenarios):02d}",
            True,
            UI_WHITE,
        )
        cases = self._eyebrow_font.render("CASES", True, UI_MUTED)
        self._screen.blit(cases, cases.get_rect(topright=(464, 10)))
        self._screen.blit(count, count.get_rect(topright=(464, 21)))
        progress_track = pygame.Rect(354, 43, 110, 5)
        pygame.draw.rect(self._screen, (55, 75, 77), progress_track, border_radius=3)
        if self._diagnosed:
            progress_width = round(
                progress_track.width
                * len(self._diagnosed)
                / len(self._scenarios)
            )
            pygame.draw.rect(
                self._screen,
                UI_TEAL,
                (progress_track.x, progress_track.y, progress_width, 5),
                border_radius=3,
            )

    def _draw_return_icon(self, center: tuple[int, int]) -> None:
        pygame.draw.line(
            self._screen,
            UI_INK,
            (center[0] + 7, center[1] - 6),
            (center[0] + 7, center[1] + 3),
            2,
        )
        pygame.draw.line(
            self._screen,
            UI_INK,
            (center[0] + 7, center[1] + 3),
            (center[0] - 6, center[1] + 3),
            2,
        )
        pygame.draw.polygon(
            self._screen,
            UI_INK,
            (
                (center[0] - 8, center[1] + 3),
                (center[0] - 2, center[1] - 2),
                (center[0] - 2, center[1] + 8),
            ),
        )

    def _draw_patient_prompt(self) -> None:
        shadow = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        pygame.draw.rect(
            shadow,
            (8, 20, 23, 90),
            (14, 418, 456, 54),
            border_radius=7,
        )
        self._screen.blit(shadow, (0, 0))
        panel = pygame.Rect(12, 414, 456, 54)
        pygame.draw.rect(self._screen, UI_PANEL, panel, border_radius=7)
        pygame.draw.rect(
            self._screen,
            UI_GOLD,
            (panel.x, panel.y, 5, panel.height),
            border_top_left_radius=7,
            border_bottom_left_radius=7,
        )
        nearby = self.nearest_patient()
        completed = nearby is not None and nearby.patient_type in self._diagnosed
        eyebrow = self._eyebrow_font.render("CASE CLOSED" if completed else "READY FOR CONSULT", True, UI_GOLD)
        number = self._patient_index(nearby.patient_type) + 1 if nearby else 0
        patient_name = self._label_font.render(f"PATIENT {number:02d}", True, UI_WHITE)
        self._screen.blit(eyebrow, (30, 422))
        self._screen.blit(patient_name, (30, 437))

        action = self._small_font.render("REVISIT" if completed else "DIAGNOSE", True, UI_MINT)
        action_right = 447
        action_center_y = panel.centery
        self._screen.blit(
            action,
            action.get_rect(midright=(action_right, action_center_y)),
        )
        key_center = (action_right - action.get_width() - 19, action_center_y)
        pygame.draw.circle(self._screen, UI_GOLD, key_center, 13)
        self._draw_return_icon(key_center)

    def _draw_locked_room_prompt(self, room: HospitalRoom) -> None:
        panel = pygame.Rect(12, 414, 456, 54)
        shadow = pygame.Surface(SCREEN_SIZE, pygame.SRCALPHA)
        pygame.draw.rect(
            shadow,
            (8, 20, 23, 90),
            panel.move(2, 4),
            border_radius=7,
        )
        self._screen.blit(shadow, (0, 0))
        pygame.draw.rect(self._screen, UI_PANEL, panel, border_radius=7)
        pygame.draw.rect(
            self._screen,
            UI_CORAL,
            (panel.x, panel.y, 5, panel.height),
            border_top_left_radius=7,
            border_bottom_left_radius=7,
        )
        eyebrow = self._eyebrow_font.render("ACCESS RESTRICTED", True, UI_CORAL)
        room_name = self._label_font.render(room.name.upper(), True, UI_WHITE)
        self._screen.blit(eyebrow, (30, 422))
        self._screen.blit(room_name, (30, 437))

        if room.unlock_after is not None:
            patient_number = list(PatientType).index(room.unlock_after) + 1
            requirement = self._small_font.render(
                f"HELP PATIENT {patient_number:02d}",
                True,
                UI_GOLD,
            )
            self._screen.blit(
                requirement,
                requirement.get_rect(midright=(447, panel.centery)),
            )

    def draw(self) -> None:
        camera = self._camera()
        self._screen.blit(self._world, (-round(camera.x), -round(camera.y)))
        self._draw_room_entrances(camera)
        self._draw_room_locks(camera)

        player_frame = self._player_frame(
            self._walk_time if self._walking else 0.0
        )
        drawables: list[
            tuple[float, pygame.Surface, pygame.Rect, PatientType | None]
        ] = []
        for scenario in self._scenarios:
            world_position = self._scenario_position(scenario)
            screen_position = world_position - camera
            phase = self._animation_time + self._patient_index(scenario.patient_type) * 0.73
            if scenario.patient_type in self._diagnosed:
                patient_state = "relieved"
            elif (
                scenario.patient_type == self._greeting_patient
                and self._animation_time < self._greeting_until
            ):
                patient_state = "talking"
            else:
                patient_state = "idle" if phase % 10 < 3 else "worried"
            patient_frames = self._patient_frames[scenario.patient_type][patient_state]
            patient_frame = animation_frame(
                patient_frames,
                phase,
                5,
            )
            drawables.append(
                (
                    world_position.y,
                    patient_frame,
                    patient_frame.get_rect(midbottom=screen_position),
                    scenario.patient_type,
                )
            )
        player_screen_position = self._player_position - camera
        drawables.append(
            (
                self._player_position.y,
                player_frame,
                player_frame.get_rect(midbottom=player_screen_position),
                None,
            )
        )

        nearby = self.nearest_patient()
        locked_room = self.nearby_locked_room()
        for _, surface, rectangle, patient_type in sorted(
            drawables,
            key=lambda item: item[0],
        ):
            is_nearby = nearby is not None and patient_type == nearby.patient_type
            if is_nearby:
                self._draw_interaction_ring(rectangle)
            if patient_type is None and self._walking:
                self._draw_walk_dust(rectangle)
            shadow = pygame.Rect(0, 0, max(24, rectangle.width - 16), 12)
            shadow.center = rectangle.midbottom
            pygame.draw.ellipse(self._screen, (78, 103, 97), shadow)
            draw_rectangle = rectangle
            if patient_type is None and self._walking and self._facing not in ("left", "right"):
                walk_lift = round(
                    abs(math.sin(self._walk_time * math.pi * 7)) * 2
                )
                draw_rectangle = rectangle.move(0, -walk_lift)
            self._screen.blit(surface, draw_rectangle)
            if patient_type in self._diagnosed:
                self._draw_marker(pygame.Vector2(rectangle.centerx, rectangle.top - 10))
            if is_nearby:
                self._draw_interaction_badge(rectangle)

        self._draw_room_labels(camera)
        self._draw_header()

        if nearby:
            self._draw_patient_prompt()
        elif locked_room:
            self._draw_locked_room_prompt(locked_room)

        self._draw_scene_fade()

        if self._notice and self._animation_time < self._notice_until:
            banner = pygame.Rect(12, 66, 456, 32)
            pygame.draw.rect(self._screen, UI_PANEL, banner, border_radius=5)
            text = self._small_font.render(self._notice, True, UI_GOLD)
            self._screen.blit(text, text.get_rect(center=banner.center))
        if self._menu is not None:
            self._menu.draw(self._screen)

        pygame.transform.scale(self._screen, self._window.get_size(), self._window)
        pygame.display.flip()

    def run(self) -> PatientScenario | None:
        while True:
            elapsed_seconds = min(self._clock.tick(60) / 1000, 0.05)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return None
                if event.type == pygame.WINDOWFOCUSLOST and self._menu is None:
                    self._pause()
                if self._menu is not None:
                    choice = self._menu.handle_event(event, self._window.get_size())
                    if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                        choice = "Resume" if self._menu_kind == "pause" else "Cancel"
                    if choice is not None and self._choose_menu(choice):
                        return None
                    continue
                if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    position = (event.pos[0] * SCREEN_SIZE[0] / self._window.get_width(), event.pos[1] * SCREEN_SIZE[1] / self._window.get_height())
                    if self._pause_button.collidepoint(position):
                        self._pause()
                        continue
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self._pause()
                        continue
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        nearby = self.nearest_patient()
                        if nearby:
                            self._fade_out()
                            return nearby

            if self._menu is not None:
                self.draw()
                continue
            keys = pygame.key.get_pressed()
            direction = pygame.Vector2(
                int(keys[pygame.K_RIGHT] or keys[pygame.K_d])
                - int(keys[pygame.K_LEFT] or keys[pygame.K_a]),
                int(keys[pygame.K_DOWN] or keys[pygame.K_s])
                - int(keys[pygame.K_UP] or keys[pygame.K_w]),
            )
            self.update(direction, elapsed_seconds)
            self.draw()

    def _pause(self) -> None:
        self._walking = False
        self._menu_kind = "pause"
        self._menu = ChoiceMenu("HOSPITAL PAUSED", ("Resume", "New Game", "Save & Quit"), f"{len(self._diagnosed)} of {len(self._scenarios)} cases closed.")

    def _choose_menu(self, choice: str) -> bool:
        if choice == "New Game":
            self._menu_kind = "reset"
            self._menu = ChoiceMenu("START NEW ROUNDS?", ("Cancel", "Reset Progress"), "Completed cases for this patient roster will be reset.")
        elif choice == "Reset Progress":
            self.restart_requested = True
            return True
        elif choice == "Save & Quit":
            return True
        elif choice in ("Resume", "Continue Exploring", "Cancel"):
            if self._menu_kind == "complete" or choice == "Continue Exploring":
                self.completion_announced = True
            self._menu = None
            self._menu_kind = ""
            self._clock.tick()
        return False

    def close(self) -> None:
        if self._owns_display:
            pygame.quit()


def start_hospital_game(scenarios: Sequence[PatientScenario], *, save_path: Path | None = None) -> None:
    store = ProgressStore((scenario.patient_type.value for scenario in scenarios), save_path)
    progress = store.load()
    diagnosed = {PatientType(value) for value in progress.diagnosed}
    player_position = progress.position
    notice = store.warning
    pygame.init()
    window = pygame.display.set_mode(WINDOW_SIZE)
    screen = pygame.Surface(SCREEN_SIZE)

    try:
        while True:
            navigator = HospitalNavigator(
                scenarios,
                diagnosed,
                player_position,
                window=window,
                screen=screen,
                completion_announced=progress.completion_announced,
                notice=notice,
            )
            selected_patient = navigator.run()
            player_position = navigator.player_position
            progress = Progress({patient.value for patient in diagnosed}, player_position, navigator.completion_announced)
            navigator.close()
            if navigator.restart_requested:
                diagnosed.clear()
                progress = Progress()
                player_position = PLAYER_START
                store.save(progress, reset=True)
                notice = store.warning
                continue
            saved = store.save(progress)
            notice = store.warning
            if selected_patient is None:
                if not saved:
                    navigator._menu = ChoiceMenu("SAVE UNAVAILABLE", ("Return to Hospital", "Quit Without Saving"), store.warning)
                    navigator._menu_kind = "save_error"
                    navigator.draw()
                    while True:
                        event = pygame.event.wait()
                        if event.type == pygame.QUIT:
                            return
                        choice = navigator._menu.handle_event(event, window.get_size())
                        navigator.draw()
                        if choice == "Quit Without Saving":
                            return
                        if choice == "Return to Hospital":
                            break
                    continue
                return

            result = start_consultation(
                **selected_patient.conversation_parameters(),
                window=window,
                screen=screen,
            )
            if result == ConversationResult.QUIT:
                return
            if result in (ConversationResult.SOLVED, ConversationResult.SOLVED_QUIT):
                diagnosed.add(selected_patient.patient_type)
                progress.diagnosed = {patient.value for patient in diagnosed}
                store.save(progress)
                unlocked = next((room.name for room in HOSPITAL_ROOMS if room.unlock_after == selected_patient.patient_type), None)
                notice = store.warning or (f"{unlocked} unlocked" if unlocked else "Case closed. Progress saved.")
                if result == ConversationResult.SOLVED_QUIT:
                    return
    finally:
        pygame.quit()