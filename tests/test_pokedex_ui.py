import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import pygame

from src.has_pokedex import PokedexError
from src.pokedex_ui import PokedexPanel


class PokedexPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pygame.init()
        self.screen = pygame.display.set_mode((480, 480))
        self.client = AsyncMock()
        self.panel = PokedexPanel(self.client)
        self.panel.show()

    async def asyncTearDown(self):
        await self.panel.shutdown()
        pygame.quit()

    async def test_send_receives_reply_and_retains_history_on_reopen(self):
        self.client.ask.return_value = "Ask about duration. " * 100
        self.panel.draft = "What should I ask?"
        self.panel.submit({"transcript": []})
        self.panel.submit({})
        await self.panel.task
        self.client.ask.assert_awaited_once()
        self.assertEqual(self.panel.draft, "")
        self.assertEqual(self.panel.messages[-1][0], "Dragon Simulator Assist")
        self.panel.draw(self.screen)
        self.assertGreater(self.panel.max_scroll, 0)
        self.panel.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_PAGEUP), (0, 0), {})
        self.assertGreater(self.panel.scroll, 0)
        self.panel.hide()
        self.panel.show()
        self.assertEqual(len(self.panel.messages), 2)

    async def test_missing_configuration_disables_open_and_submit(self):
        with patch.dict("os.environ", {}, clear=True):
            panel = PokedexPanel()
            panel.draft = "Help"
            with patch.object(panel.client, "ask", new_callable=AsyncMock) as ask:
                panel.show()
                panel.submit({})
                self.assertFalse(panel.available)
                self.assertFalse(panel.open)
                self.assertFalse(panel.focused)
                self.assertIsNone(panel.task)
                self.assertEqual(panel.messages, [])
                ask.assert_not_called()

    async def test_close_cancels_request_and_keeps_draft(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def answer(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.client.ask.side_effect = answer
        self.panel.draft = "Help"
        self.panel.submit({})
        await started.wait()
        self.panel.hide()
        await self.panel.shutdown()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.panel.draft, "Help")
        self.assertFalse(self.panel.open)

    async def test_failure_keeps_question_and_allows_manual_retry(self):
        self.client.ask.side_effect = [PokedexError("Unavailable"), "Try this."]
        self.panel.draft = "Help"
        self.panel.submit({})
        await self.panel.task
        self.assertEqual(self.panel.draft, "Help")
        self.panel.submit({})
        await self.panel.task
        self.assertEqual(self.panel.messages[-1], ("Dragon Simulator Assist", "Try this."))

    async def test_focus_loss_does_not_accept_text(self):
        self.panel.focus(False)
        self.panel.handle_event(pygame.event.Event(pygame.TEXTINPUT, text="hidden"), (0, 0), {})
        self.assertEqual(self.panel.draft, "")

    async def test_logo_background_is_transparent_at_all_sizes(self):
        for logo in (self.panel.logo, self.panel.button_logo, self.panel.header_logo):
            self.assertIsNotNone(logo)
            width, height = logo.get_size()
            for position in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
                self.assertEqual(logo.get_at(position).a, 0)
            opaque_pixels = pygame.mask.from_surface(logo).count()
            self.assertGreater(opaque_pixels, width * height * 0.1)
            self.assertLess(opaque_pixels, width * height * 0.9)

    async def test_mouse_send_and_return_to_patient(self):
        self.assertIsNotNone(self.panel.logo)
        self.assertEqual(self.panel.button_logo.get_size(), (24, 24))
        self.assertEqual(self.panel.header_logo.get_size(), (42, 42))
        self.client.ask.return_value = "Ask about symptom duration."
        self.panel.draft = "What next?"
        click = pygame.event.Event(pygame.MOUSEBUTTONUP, button=1)
        self.panel.handle_event(click, self.panel.send_button.center, {"transcript": []})
        await self.panel.task
        self.client.ask.assert_awaited_once_with("What next?", {"transcript": []})
        self.assertEqual(self.panel.messages[-1][0], "Dragon Simulator Assist")
        self.panel.handle_event(click, self.panel.close_button.center, {})
        self.assertFalse(self.panel.open)