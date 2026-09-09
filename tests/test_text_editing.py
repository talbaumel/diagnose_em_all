import asyncio
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pygame

from src.care_plan_ui import CareOrderForm
from src.realtime_conversation import PatientAnimator, Test
from src.text_editing import ClipboardError, TextEditing, read_clipboard, write_clipboard


def shortcut(key, modifier=pygame.KMOD_META):
    return pygame.event.Event(pygame.KEYDOWN, key=key, mod=modifier)


class ClipboardBackendTests(unittest.TestCase):
    @patch("src.text_editing.sys.platform", "darwin")
    @patch("src.text_editing.subprocess.run")
    def test_mac_uses_native_unicode_clipboard_without_shell(self, run):
        text = "PCR שלום Привет"
        run.return_value = SimpleNamespace(stdout=text)
        self.assertEqual(read_clipboard(), text)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pbpaste"])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["timeout"], 1)
        write_clipboard(text)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/pbcopy"])
        self.assertEqual(run.call_args.kwargs["input"], text)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("src.text_editing.sys.platform", "linux")
    @patch("src.text_editing.pygame.scrap")
    def test_sdl_backend_initializes_and_preserves_unicode(self, scrap):
        scrap.get_init.return_value = False
        scrap.get.return_value = "שלום\0".encode()
        self.assertEqual(read_clipboard(), "שלום")
        scrap.init.assert_called_once()
        write_clipboard("Привет")
        scrap.put.assert_called_once_with(pygame.SCRAP_TEXT, "Привет\0".encode())
        scrap.get.return_value = None
        self.assertEqual(read_clipboard(), "")

    @patch("src.text_editing.sys.platform", "darwin")
    def test_native_failures_are_explicit(self):
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("pbpaste", 1),
                      subprocess.CalledProcessError(1, "pbpaste")):
            with self.subTest(error=error), patch("src.text_editing.subprocess.run", side_effect=error):
                with self.assertRaises(ClipboardError):
                    read_clipboard()
                with self.assertRaises(ClipboardError):
                    write_clipboard("retained")


class TextEditingTests(unittest.TestCase):
    def setUp(self):
        self.editor = TextEditing()

    def test_paste_appends_and_select_all_replaces_for_both_platform_shortcuts(self):
        for modifier in (pygame.KMOD_META, pygame.KMOD_CTRL):
            with self.subTest(modifier=modifier), patch("src.text_editing.read_clipboard", return_value="PCR"):
                self.editor.reset()
                self.assertEqual(self.editor.handle_event(shortcut(pygame.K_v, modifier), "Run "), "Run PCR")
                self.editor.handle_event(shortcut(pygame.K_a, modifier), "Old draft")
                self.assertTrue(self.editor.selected_all)
                self.assertEqual(self.editor.handle_event(shortcut(pygame.K_v, modifier), "Old draft"), "PCR")
                self.assertFalse(self.editor.selected_all)

    def test_copy_cut_and_delete_selected_text(self):
        with patch("src.text_editing.write_clipboard") as write:
            self.editor.handle_event(shortcut(pygame.K_c), "draft")
            write.assert_not_called()
            self.editor.handle_event(shortcut(pygame.K_a), "draft")
            self.assertEqual(self.editor.handle_event(shortcut(pygame.K_c), "draft"), "draft")
            write.assert_called_once_with("draft")
            self.assertEqual(self.editor.handle_event(shortcut(pygame.K_x), "draft"), "")
        for key in (pygame.K_BACKSPACE, pygame.K_DELETE):
            self.editor.handle_event(shortcut(pygame.K_a), "draft")
            self.assertEqual(self.editor.handle_event(shortcut(key, 0), "draft"), "")

    def test_typed_unicode_replaces_selection_and_altgr_is_not_a_shortcut(self):
        self.editor.handle_event(shortcut(pygame.K_a), "draft")
        event = pygame.event.Event(pygame.TEXTINPUT, text="שלום Привет")
        with patch("pygame.key.get_mods", return_value=0):
            self.assertEqual(self.editor.handle_event(event, "draft"), "שלום Привет")
        with patch("src.text_editing.read_clipboard") as read:
            self.assertIsNone(self.editor.handle_event(shortcut(pygame.K_v, pygame.KMOD_CTRL | pygame.KMOD_ALT), ""))
            read.assert_not_called()
        with patch("pygame.key.get_mods", return_value=pygame.KMOD_CTRL | pygame.KMOD_ALT):
            self.assertEqual(self.editor.handle_event(event, ""), "שלום Привет")
        with patch("pygame.key.get_mods", return_value=pygame.KMOD_META):
            self.assertEqual(self.editor.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="v"), "draft"), "draft")

    def test_multiline_clipboard_is_text_not_submit_events(self):
        with patch("src.text_editing.read_clipboard", return_value="Run PCR\r\non\tone swab.\0\x1b"):
            self.assertEqual(self.editor.handle_event(shortcut(pygame.K_v), ""), "Run PCR on one swab.")
        self.assertIsNone(self.editor.handle_event(shortcut(pygame.K_RETURN, 0), "draft"))

    def test_empty_clipboard_or_failure_does_not_delete_selection(self):
        self.editor.handle_event(shortcut(pygame.K_a), "draft")
        with patch("src.text_editing.read_clipboard", return_value=""):
            self.assertEqual(self.editor.handle_event(shortcut(pygame.K_v), "draft"), "draft")
        self.assertTrue(self.editor.selected_all)
        for key, function in ((pygame.K_v, "read_clipboard"), (pygame.K_x, "write_clipboard")):
            with patch(f"src.text_editing.{function}", side_effect=ClipboardError("Clipboard unavailable")):
                self.assertEqual(self.editor.handle_event(shortcut(key), "draft"), "draft")
                self.assertEqual(self.editor.error, "Clipboard unavailable")
                self.assertTrue(self.editor.selected_all)


class TextFieldIntegrationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"HAS_BOT_ID": "bot-test", "HAS_SCENARIO": "clinical", "HAS_DIRECT_LINE_SECRET": "test-secret"})
        environment.start()
        self.addCleanup(environment.stop)
        pygame.init()
        self.addCleanup(pygame.quit)
        self.window = pygame.display.set_mode((480, 480))
        self.animator = PatientAnimator(0, window=self.window, disease="common cold")
        self.addCleanup(self.animator.close)
        self.stop = asyncio.Event()

    def event(self, key, modifier=pygame.KMOD_META):
        self.animator.handle_event(shortcut(key, modifier), self.stop)

    def test_chat_paste_is_focused_draft_only_and_copy_works(self):
        self.animator._set_text_focus(True)
        with patch("src.text_editing.read_clipboard", return_value="Run the respiratory PCR panel.\nPlease."):
            self.event(pygame.K_v)
        self.assertEqual(self.animator._text_input, "Run the respiratory PCR panel. Please.")
        self.assertTrue(self.animator._text_messages.empty())
        self.assertEqual(self.animator.metrics.discovered_tests, {})
        self.event(pygame.K_a)
        with patch("src.text_editing.write_clipboard") as write:
            self.event(pygame.K_c)
            write.assert_called_once_with(self.animator._text_input)
        self.animator.draw()
        self.event(pygame.K_RETURN, 0)
        self.assertEqual(self.animator._text_messages.get_nowait(), "Run the respiratory PCR panel. Please.")

    def test_unfocused_and_evidence_modal_do_not_read_clipboard(self):
        with patch("src.text_editing.read_clipboard") as read:
            self.event(pygame.K_v)
            self.animator._set_text_focus(True)
            self.animator.show_test_result(Test("Evidence", "Not an input"))
            self.event(pygame.K_v)
            self.animator.close_test_result()
            self.animator.handle_event(pygame.event.Event(pygame.WINDOWFOCUSLOST), self.stop)
            self.event(pygame.K_v)
            read.assert_not_called()

    def test_diagnosis_paste_does_not_submit_or_change_chat(self):
        self.animator._text_input = "Keep this draft"
        self.animator._open_diagnosis()
        with patch("src.text_editing.read_clipboard", return_value="common cold\n"):
            self.event(pygame.K_v)
        self.assertEqual(self.animator._diagnosis_input, "common cold")
        self.assertFalse(self.animator._diagnosis_confirmed.is_set())
        self.assertEqual(self.animator._text_input, "Keep this draft")
        self.event(pygame.K_a)
        self.animator.draw()
        self.event(pygame.K_RETURN, 0)
        self.assertTrue(self.animator._diagnosis_confirmed.is_set())

    def test_prescription_and_referral_text_fields_but_not_urgency(self):
        for kind in ("prescription", "referral"):
            form = CareOrderForm(kind)
            with patch("src.text_editing.read_clipboard", return_value="Pasted directions"):
                self.assertIsNone(form.handle_event(shortcut(pygame.K_v), (480, 480)))
                self.assertEqual(form.values[0], "Pasted directions")
                form.handle_event(shortcut(pygame.K_a), (480, 480))
                form.draw(self.animator._screen)
                form.focus(1)
                form.handle_event(shortcut(pygame.K_v), (480, 480))
                self.assertEqual(form.values[0], form.values[1])
                self.assertFalse(form.closed)
            if kind == "referral":
                form.focus(2)
                with patch("src.text_editing.read_clipboard") as read:
                    form.handle_event(shortcut(pygame.K_v), (480, 480))
                    read.assert_not_called()
                self.assertEqual(form.values[2], "Routine")

    def test_helper_paste_stays_local_and_preserves_patient_draft(self):
        self.animator._text_input = "Patient draft"
        self.animator._open_pokedex()
        with patch("src.text_editing.read_clipboard", return_value="Which virus?\n"):
            self.event(pygame.K_v)
        self.assertEqual(self.animator._pokedex.draft, "Which virus?")
        self.assertIsNone(self.animator._pokedex.task)
        self.event(pygame.K_a)
        self.animator._pokedex.draw(self.animator._screen)
        self.event(pygame.K_ESCAPE, 0)
        self.assertEqual(self.animator._text_input, "Patient draft")

    def test_clipboard_errors_are_visible_and_preserve_drafts(self):
        self.animator._text_input = "Keep this"
        self.animator._set_text_focus(True)
        with patch("src.text_editing.read_clipboard", side_effect=ClipboardError("Clipboard unavailable")):
            self.event(pygame.K_v)
        self.assertEqual(self.animator._text_input, "Keep this")
        self.assertIn(("Case", "Clipboard unavailable"), self.animator._transcript)
        with patch("src.text_editing.read_clipboard", side_effect=ClipboardError("Clipboard unavailable")):
            self.animator._open_diagnosis()
            self.event(pygame.K_v)
            self.assertEqual(self.animator._diagnosis_feedback, "Clipboard unavailable")
            self.animator._close_diagnosis()
            form = CareOrderForm("prescription")
            form.handle_event(shortcut(pygame.K_v), (480, 480))
            self.assertEqual(form.error, "Clipboard unavailable")
            self.assertFalse(form.closed)
            self.animator._open_pokedex()
            self.event(pygame.K_v)
            self.assertEqual(self.animator._pokedex.status, "Clipboard unavailable")
            self.assertIn(("Input", "Clipboard unavailable"), self.animator._pokedex.messages)
