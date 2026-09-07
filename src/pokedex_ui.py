from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pygame
from azure.core.exceptions import AzureError

from src.game_ui import draw_spinner, wrap_text
from src.has_pokedex import HASPokedex, PokedexError
from src.text_editing import TextEditing


class PokedexPanel:
    def __init__(self, client: HASPokedex | None = None) -> None:
        self.client = client or HASPokedex()
        self.open = False
        self.focused = False
        self.draft = ""
        self.editing = TextEditing()
        self.messages: list[tuple[str, str]] = []
        self.task: asyncio.Task | None = None
        self.scroll = 0
        self.max_scroll = 0
        self.status = "Clinical reference"
        self.input_rect = pygame.Rect(18, 400, 344, 40)
        self.send_button = pygame.Rect(374, 400, 88, 40)
        self.close_button = pygame.Rect(332, 12, 130, 30)
        self.font = pygame.font.SysFont("Avenir Next", 15)
        self.label_font = pygame.font.SysFont("Avenir Next", 11, bold=True)
        self.title_font = pygame.font.SysFont("Avenir Next", 20, bold=True)
        logo_path = Path(__file__).resolve().parents[1] / "data/sprites/ui/dragon_copilot_logo_transparent.png"
        self.logo = pygame.image.load(str(logo_path)).convert_alpha() if logo_path.is_file() else None
        self.button_logo = pygame.transform.smoothscale(self.logo, (24, 24)) if self.logo is not None else None
        self.header_logo = pygame.transform.smoothscale(self.logo, (42, 42)) if self.logo is not None else None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def focus(self, focused: bool) -> None:
        self.editing.reset()
        self.focused = focused
        if focused:
            pygame.key.start_text_input()
        else:
            pygame.key.stop_text_input()

    def show(self) -> None:
        self.open = True
        self.focus(True)

    def cancel(self) -> None:
        if self.busy:
            self.task.cancel()
            self.status = "Request cancelled"

    def hide(self) -> None:
        self.cancel()
        self.open = False
        self.focus(False)

    async def shutdown(self) -> None:
        self.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)

    def submit(self, context: dict) -> None:
        question = self.draft.strip()
        if not question or self.busy:
            return
        self.messages.append(("You", question))
        self.scroll = 0
        self.status = "Consulting Dragon Copilot..."

        async def answer() -> None:
            try:
                text = await self.client.ask(question, context)
            except PokedexError as error:
                self.messages.append(("Connection", str(error)))
                self.status = "Request failed"
            except AzureError:
                self.messages.append(("Connection", "HAS Key Vault access failed. Check Azure sign-in and permission to read the configured secret."))
                self.status = "Sign-in or access required"
            else:
                self.messages.append(("Dragon Copilot", text))
                if self.draft.strip() == question:
                    self.draft = ""
                    self.editing.reset()
                self.status = "Clinical reference"
            self.scroll = 0

        self.task = asyncio.create_task(answer())

    def handle_event(self, event: pygame.event.Event, position: tuple[int, int], context: dict) -> None:
        key = event.key if event.type == pygame.KEYDOWN else None
        if key in (pygame.K_ESCAPE, pygame.K_F6):
            self.hide()
        elif event.type == pygame.MOUSEWHEEL:
            self.scroll = max(0, min(self.max_scroll, self.scroll + event.y * 60))
        elif key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
            self.scroll = max(0, min(self.max_scroll, self.scroll + (240 if key == pygame.K_PAGEUP else -240)))
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self.close_button.collidepoint(position):
                self.hide()
            elif self.send_button.collidepoint(position):
                if self.busy:
                    self.cancel()
                else:
                    self.submit(context)
            else:
                self.focus(self.input_rect.collidepoint(position))
        elif key == pygame.K_TAB:
            self.focus(True)
        elif self.focused:
            if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.submit(context)
            else:
                updated = self.editing.handle_event(event, self.draft)
                if updated is not None:
                    self.draft = updated
                    if self.editing.error:
                        self.status = "Clipboard unavailable"
                        self.messages.append(("Input", self.editing.error))
                        self.scroll = 0

    def draw(self, screen: pygame.Surface) -> None:
        paper, ink, teal, coral = (247, 249, 244), (20, 32, 38), (52, 132, 121), (238, 105, 82)
        screen.fill(paper)
        pygame.draw.rect(screen, (22, 43, 46), (0, 0, 480, 82))
        pygame.draw.rect(screen, coral, (0, 79, 112, 3))
        if self.header_logo is not None:
            screen.blit(self.header_logo, (18, 15))
        screen.blit(self.title_font.render("Dragon Copilot", True, paper), (70, 12))
        status = self.label_font.render(self.status, True, (193, 231, 215))
        status_rect = status.get_rect(topleft=(70, 48))
        screen.blit(status, status_rect)
        if self.busy:
            draw_spinner(screen, (status_rect.right + 10, status_rect.centery), 6, (193, 231, 215))
        for button, label in ((self.close_button, "Return to Patient"), (self.send_button, "Cancel" if self.busy else "Send")):
            enabled = button == self.close_button or self.busy or bool(self.draft.strip())
            pygame.draw.rect(screen, teal if enabled else (91, 119, 116), button, border_radius=5)
            rendered = self.label_font.render(label, True, paper)
            screen.blit(rendered, rendered.get_rect(center=button.center))
        area = pygame.Rect(18, 94, 432, 290)
        lines: list[tuple[str, bool]] = []
        for speaker, message in self.messages:
            lines.append((speaker.upper(), True))
            lines.extend((line, False) for line in wrap_text(message, self.font, area.width))
            lines.append(("", False))
        self.max_scroll = max(0, len(lines) * 21 - area.height)
        self.scroll = min(self.scroll, self.max_scroll)
        offset = self.max_scroll - self.scroll
        previous_clip = screen.get_clip()
        screen.set_clip(area)
        for index, (text, heading) in enumerate(lines):
            font = self.label_font if heading else self.font
            screen.blit(font.render(text, True, teal if heading else ink), (area.x, area.y + index * 21 - offset))
        screen.set_clip(previous_clip)
        if self.max_scroll:
            pygame.draw.rect(screen, (193, 211, 207), (459, area.y, 3, area.height))
            pygame.draw.rect(screen, teal, (459, area.y + round((area.height - 30) * offset / self.max_scroll), 3, 30))
        pygame.draw.rect(screen, (255, 255, 251), self.input_rect, border_radius=5)
        pygame.draw.rect(screen, coral if self.focused else teal, self.input_rect, width=2, border_radius=5)
        input_area = self.input_rect.inflate(-16, -10)
        text = self.font.render(self.draft or "Ask Dragon Copilot...", True, ink)
        rectangle = text.get_rect(midleft=(input_area.x, input_area.centery))
        if rectangle.width > input_area.width:
            rectangle.right = input_area.right - 3
        screen.set_clip(input_area)
        if self.focused and self.editing.selected_all:
            pygame.draw.rect(screen, (193, 231, 215), rectangle)
        screen.blit(text, rectangle)
        if self.focused and int(time.monotonic() * 2) % 2 == 0:
            cursor = min(rectangle.right + 2, input_area.right - 2) if self.draft else input_area.x
            pygame.draw.line(screen, coral, (cursor, input_area.y), (cursor, input_area.bottom), 2)
        screen.set_clip(previous_clip)
        screen.blit(self.label_font.render("AI guidance / Fictional patients only", True, teal), (18, 454))