from __future__ import annotations

import time

import pygame

from src.care_plan import REFERRAL_URGENCIES, Prescription, Referral
from src.game_ui import wrap_text


class CareOrderForm:
    def __init__(self, kind: str) -> None:
        if kind not in ("prescription", "referral"):
            raise ValueError("Unknown care order type")
        self.kind = kind
        self.values = ["", "", "" if kind == "prescription" else "Routine"]
        self.labels = ("Medication", "Directions (dose, route, frequency)", "Reason") if kind == "prescription" else ("Referral destination", "Reason", "Urgency")
        self.fields = tuple(pygame.Rect(48, 132 + index * 66, 384, 38) for index in range(3))
        self.cancel_button = pygame.Rect(48, 366, 140, 36)
        self.submit_button = pygame.Rect(204, 366, 228, 36)
        self.urgency_buttons = tuple(pygame.Rect(48 + index * 132, 264, 120, 38) for index in range(3))
        self.closed = False
        self.error = ""
        self.focus_index = 0
        self.font = pygame.font.SysFont("Avenir Next", 15)
        self.label_font = pygame.font.SysFont("Avenir Next", 11, bold=True)
        self.title_font = pygame.font.SysFont("Avenir Next", 16, bold=True)
        self.focus(0)

    def focus(self, index: int) -> None:
        self.focus_index = index
        if 0 <= index < 3 and not (self.kind == "referral" and index == 2):
            pygame.key.start_text_input()
        else:
            pygame.key.stop_text_input()

    def submit(self) -> Prescription | Referral | None:
        try:
            values = [value.strip() for value in self.values]
            order = Prescription(*values) if self.kind == "prescription" else Referral(*values)
        except ValueError as error:
            self.error = str(error)
            return None
        self.closed = True
        pygame.key.stop_text_input()
        return order

    def handle_event(self, event: pygame.event.Event, window_size: tuple[int, int]) -> Prescription | Referral | None:
        if self.closed:
            return None
        key = event.key if event.type == pygame.KEYDOWN else None
        if key == pygame.K_ESCAPE:
            self.closed = True
            pygame.key.stop_text_input()
        elif key == pygame.K_TAB:
            step = -1 if getattr(event, "mod", 0) & pygame.KMOD_SHIFT else 1
            self.focus((self.focus_index + step) % 5)
        elif key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.focus_index == 3:
                self.closed = True
                pygame.key.stop_text_input()
            elif self.focus_index in (2, 4):
                return self.submit()
            elif self.focus_index in (0, 1):
                self.focus(self.focus_index + 1)
        elif self.kind == "referral" and self.focus_index == 2 and key in (pygame.K_LEFT, pygame.K_RIGHT):
            index = REFERRAL_URGENCIES.index(self.values[2])
            self.values[2] = REFERRAL_URGENCIES[(index + (1 if key == pygame.K_RIGHT else -1)) % 3]
        elif 0 <= self.focus_index < 3 and not (self.kind == "referral" and self.focus_index == 2):
            if event.type == pygame.TEXTINPUT:
                self.values[self.focus_index] += event.text
                self.error = ""
            elif key == pygame.K_BACKSPACE:
                self.values[self.focus_index] = self.values[self.focus_index][:-1]
                self.error = ""
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            position = (event.pos[0] * 480 / window_size[0], event.pos[1] * 480 / window_size[1])
            if self.cancel_button.collidepoint(position):
                self.closed = True
                pygame.key.stop_text_input()
            elif self.submit_button.collidepoint(position):
                return self.submit()
            else:
                for index, rectangle in enumerate(self.fields):
                    if rectangle.collidepoint(position):
                        self.focus(index)
                if self.kind == "referral":
                    for index, rectangle in enumerate(self.urgency_buttons):
                        if rectangle.collidepoint(position):
                            self.values[2] = REFERRAL_URGENCIES[index]
        return None

    def draw(self, screen: pygame.Surface) -> None:
        ink, paper, teal, coral = (20, 32, 38), (247, 249, 244), (52, 132, 121), (238, 105, 82)
        overlay = pygame.Surface(screen.get_size(), pygame.SRCALPHA)
        overlay.fill((8, 20, 23, 190))
        screen.blit(overlay, (0, 0))
        pygame.draw.rect(screen, paper, (28, 60, 424, 360), border_radius=8)
        pygame.draw.rect(screen, teal, (28, 60, 424, 5))
        title = "PRESCRIPTION" if self.kind == "prescription" else "REFERRAL"
        screen.blit(self.title_font.render(title, True, ink), (48, 82))
        for index, (label, rectangle) in enumerate(zip(self.labels, self.fields)):
            screen.blit(self.label_font.render(label, True, teal), (rectangle.x, rectangle.y - 19))
            if self.kind == "referral" and index == 2:
                for urgency, button in zip(REFERRAL_URGENCIES, self.urgency_buttons):
                    selected = self.values[2] == urgency
                    pygame.draw.rect(screen, teal if selected else (220, 232, 228), button, border_radius=5)
                    if selected and self.focus_index == 2:
                        pygame.draw.rect(screen, coral, button, width=2, border_radius=5)
                    rendered = self.label_font.render(urgency, True, paper if selected else ink)
                    screen.blit(rendered, rendered.get_rect(center=button.center))
                continue
            pygame.draw.rect(screen, (255, 255, 251), rectangle, border_radius=5)
            pygame.draw.rect(screen, coral if self.focus_index == index else (127, 151, 146), rectangle, width=2, border_radius=5)
            area = rectangle.inflate(-16, -8)
            rendered = self.font.render(self.values[index], True, ink)
            position = rendered.get_rect(midleft=(area.x, area.centery))
            if position.width > area.width:
                position.right = area.right - 3
            previous_clip = screen.get_clip()
            screen.set_clip(area)
            screen.blit(rendered, position)
            if self.focus_index == index and int(time.monotonic() * 2) % 2 == 0:
                cursor = min(position.right + 2, area.right - 2)
                pygame.draw.line(screen, coral, (cursor, area.y + 2), (cursor, area.bottom - 2), 2)
            screen.set_clip(previous_clip)
        for index, line in enumerate(wrap_text(self.error, self.label_font, 384)):
            screen.blit(self.label_font.render(line, True, ink), (48, 318 + index * 16))
        for focus, button, label in ((3, self.cancel_button, "Cancel"), (4, self.submit_button, "Issue Prescription" if self.kind == "prescription" else "Send Referral")):
            pygame.draw.rect(screen, teal if focus == 4 else (91, 119, 116), button, border_radius=5)
            if self.focus_index == focus:
                pygame.draw.rect(screen, coral, button, width=2, border_radius=5)
            rendered = self.font.render(label, True, paper)
            screen.blit(rendered, rendered.get_rect(center=button.center))