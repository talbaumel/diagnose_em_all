"""Clipboard shortcuts shared by the game's single-line text fields."""

from __future__ import annotations

import subprocess
import sys
import unicodedata

import pygame


class ClipboardError(Exception):
    pass


def read_clipboard() -> str:
    try:
        if sys.platform == "darwin":
            # pygame.scrap does not provide a reliable native macOS clipboard.
            return subprocess.run(
                ["/usr/bin/pbpaste"], capture_output=True, encoding="utf-8",
                check=True, timeout=1,
            ).stdout
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        data = pygame.scrap.get(pygame.SCRAP_TEXT)
        return data.decode("utf-8").rstrip("\0") if data else ""
    except (OSError, subprocess.SubprocessError, UnicodeError, pygame.error) as error:
        raise ClipboardError("Could not paste from the clipboard. Try again or type the text.") from error


def write_clipboard(text: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["/usr/bin/pbcopy"], input=text, capture_output=True,
                encoding="utf-8", check=True, timeout=1,
            )
        else:
            if not pygame.scrap.get_init():
                pygame.scrap.init()
            pygame.scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8") + b"\0")
    except (OSError, subprocess.SubprocessError, UnicodeError, pygame.error) as error:
        raise ClipboardError("Could not copy to the clipboard. Your text has been kept.") from error


def single_line(text: str) -> str:
    text = " ".join(text.splitlines()).replace("\t", " ")
    return "".join(character for character in text if unicodedata.category(character) != "Cc")


class TextEditing:
    def __init__(self) -> None:
        self.selected_all = False
        self.error = ""

    def reset(self) -> None:
        self.selected_all = False
        self.error = ""

    def handle_event(self, event: pygame.event.Event, text: str) -> str | None:
        """Return updated text for handled input; None leaves other controls alone."""
        self.error = ""
        if event.type == pygame.TEXTINPUT:
            # Some backends also emit text for a modified shortcut key.
            modifiers = pygame.key.get_mods()
            if modifiers & pygame.KMOD_META or (
                modifiers & pygame.KMOD_CTRL and not modifiers & pygame.KMOD_ALT
            ):
                return text
            added = single_line(event.text)
            if not added:
                return text
            result = ("" if self.selected_all else text) + added
            self.selected_all = False
            return result
        if event.type != pygame.KEYDOWN:
            return None
        modifiers = getattr(event, "mod", 0)
        shortcut = modifiers & (pygame.KMOD_CTRL | pygame.KMOD_META) and not modifiers & pygame.KMOD_ALT
        if shortcut and event.key in (pygame.K_a, pygame.K_c, pygame.K_x, pygame.K_v):
            try:
                if event.key == pygame.K_a:
                    self.selected_all = bool(text)
                elif event.key == pygame.K_v:
                    added = single_line(read_clipboard())
                    if added:
                        text = ("" if self.selected_all else text) + added
                        self.selected_all = False
                elif self.selected_all:
                    write_clipboard(text)
                    if event.key == pygame.K_x:
                        text = ""
                        self.selected_all = False
            except ClipboardError as error:
                self.error = str(error)
            return text
        if not shortcut and event.key in (pygame.K_BACKSPACE, pygame.K_DELETE):
            text = "" if self.selected_all else text[:-1] if event.key == pygame.K_BACKSPACE else text
            self.selected_all = False
            return text
        if event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_HOME, pygame.K_END):
            self.selected_all = False
        return None
