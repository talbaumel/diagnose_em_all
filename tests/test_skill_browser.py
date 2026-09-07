from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from src.skill_browser import ADVANCED_TEST_IDS, AdvancedTestDrawer, EquipmentDrawer, SkillBrowser
from src.diagnostic_skills import load_catalog
from src.skill_activity import SUPPORTED_ACTIVITIES


def skill(index=0, **changes):
    fields = dict(
        id=f"skill-{index}", name=f"Skill {index}", category="Examination",
        description="A neutral description. " * 20, aliases=("Alternate name", "Other name"),
        examples=("Please examine the patient.", "Perform an examination.", "Check the patient.", "Hidden fourth"),
        note="Illustration is not evidence.", icon=None,
        parameters={"type": "object", "required": ["site"],
                    "properties": {"site": {"type": "string", "description": "Area to examine"},
                                   "optional": {"type": "boolean"}}},
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


class SkillBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def setUp(self):
        self.browser = SkillBrowser(skill(i) for i in range(86))
        self.surface = pygame.Surface((480, 480))

    def key(self, key):
        return self.browser.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key))

    def click(self, point, physical=(999, 999)):
        return self.browser.handle_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=physical), point
        )

    def test_list_navigation_and_bounds(self):
        for key, index in ((pygame.K_UP, 0), (pygame.K_DOWN, 1), (pygame.K_PAGEDOWN, 13),
                           (pygame.K_PAGEUP, 1), (pygame.K_END, 85), (pygame.K_DOWN, 85),
                           (pygame.K_HOME, 0)):
            with self.subTest(key=key):
                self.assertIsNone(self.key(key))
                self.assertEqual(self.browser.selected, index)
                self.assertLessEqual(self.browser.list_offset, index)
                self.assertLess(index, self.browser.list_offset + 12)
                self.browser.draw(self.surface)

    def test_temperature_art_and_button_start_activity_without_drafting(self):
        self.browser = SkillBrowser([skill(id="temperature", name="Temperature", parameters={})])
        self.key(pygame.K_RETURN)
        self.assertEqual(self.key(pygame.K_RETURN), ("activity", "temperature"))
        self.assertEqual(self.click(self.browser.ART_RECT.center), ("activity", "temperature"))
        self.assertEqual(self.click(self.browser.REQUEST_RECT.center), ("activity", "temperature"))
        self.browser.draw(self.surface)

    def test_drawer_click_or_enter_picks_instrument_without_details_step(self):
        self.browser = EquipmentDrawer([skill(id="temperature", name="Temperature", parameters={})])
        self.assertEqual(self.key(pygame.K_RETURN), ("activity", "temperature"))
        self.assertEqual(self.click((80, 140)), ("activity", "temperature"))
        self.assertIsNone(self.click((80, 220)))
        self.assertFalse(self.browser.details_open)
        self.assertEqual(self.key(pygame.K_ESCAPE), ("close", ""))
        self.assertEqual(self.key(pygame.K_F7), ("close", ""))
        self.assertEqual(self.click(self.browser.BACK_RECT.center), ("close", ""))
        self.browser.draw(self.surface)

    def test_drawer_pages_and_scrolls_many_instruments(self):
        self.browser = EquipmentDrawer(skill(i) for i in range(31))
        for key, index in ((pygame.K_DOWN, 1), (pygame.K_PAGEDOWN, 5),
                           (pygame.K_END, 30), (pygame.K_DOWN, 30),
                           (pygame.K_PAGEUP, 26), (pygame.K_HOME, 0)):
            self.key(key)
            self.assertEqual(self.browser.selected, index)
            self.assertLessEqual(self.browser.list_offset, index)
            self.assertLess(index, self.browser.list_offset + self.browser.ROWS)
            self.browser.draw(self.surface)
        self.browser.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-3))
        self.assertEqual(self.browser.selected, 9)
        self.key(pygame.K_END)
        self.click((80, self.browser.LIST_RECT.bottom - 10))
        self.assertEqual(self.browser.selected_skill.id, "skill-30")

    def test_empty_drawer_cannot_start_a_procedure(self):
        self.browser = EquipmentDrawer([])
        self.assertIsNone(self.key(pygame.K_RETURN))
        self.assertIsNone(self.click((80, 140)))
        self.key(pygame.K_END)
        self.browser.draw(self.surface)
        self.assertEqual(self.key(pygame.K_ESCAPE), ("close", ""))

    def test_advanced_forms_have_real_ids_and_are_separate_from_instruments(self):
        catalog = load_catalog()
        self.assertTrue(ADVANCED_TEST_IDS <= {s.id for s in catalog})
        self.assertFalse(ADVANCED_TEST_IDS & SUPPORTED_ACTIVITIES)
        self.assertTrue({"chest_x_ray", "complete_blood_count", "biopsy",
                         "viral_metagenomic_sequencing"} <= ADVANCED_TEST_IDS)
        self.assertFalse({"sexual_health_history", "genetic_counseling", "range_of_motion"} & ADVANCED_TEST_IDS)
        self.browser = AdvancedTestDrawer(s for s in catalog if s.id in ADVANCED_TEST_IDS)
        self.key(pygame.K_END)
        self.assertEqual(self.browser.selected, len(self.browser.catalog) - 1)
        self.key(pygame.K_RETURN)
        action = self.key(pygame.K_RETURN)
        self.assertEqual(action[0], "draft")
        self.assertTrue(action[1])
        self.browser.draw(self.surface)
        self.assertEqual(self.key(pygame.K_F8), ("close", ""))

    def test_last_page_has_twelve_rows(self):
        self.key(pygame.K_END)
        self.assertEqual(self.browser.list_offset, 74)
        self.click((40, self.browser.LIST_RECT.bottom - 1))
        self.assertEqual(self.browser.selected_skill.id, "skill-85")
        self.assertTrue(self.browser.details_open)

    def test_mouse_wheel_modern_legacy_and_flipped(self):
        for event, selected in (
            (pygame.event.Event(pygame.MOUSEWHEEL, y=-2), 6),
            (pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=4), 3),
            (pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=5), 6),
            (pygame.event.Event(pygame.MOUSEWHEEL, y=1, flipped=True), 9),
        ):
            self.browser.handle_event(event)
            self.assertEqual(self.browser.selected, selected)

    def test_details_escape_then_close_preserves_list(self):
        self.key(pygame.K_END)
        self.assertIsNone(self.key(pygame.K_RETURN))
        self.assertTrue(self.browser.details_open)
        self.assertIsNone(self.key(pygame.K_ESCAPE))
        self.assertFalse(self.browser.details_open)
        self.assertEqual((self.browser.selected, self.browser.list_offset), (85, 74))
        self.assertEqual(self.key(pygame.K_ESCAPE), ("close", ""))

    def test_logical_click_opens_and_button_only_returns_draft(self):
        self.assertIsNone(self.click((40, 78 + 3 * 27 + 10)))
        self.assertEqual(self.browser.selected, 3)
        self.assertEqual(self.click(self.browser.REQUEST_RECT.center),
                         ("draft", "Please examine the patient."))
        self.assertTrue(self.browser.details_open)
        self.assertEqual(self.key(pygame.K_RETURN), ("draft", "Please examine the patient."))
        self.assertIsNone(self.click(self.browser.BACK_RECT.center))
        self.assertFalse(self.browser.details_open)
        self.assertEqual(self.click(self.browser.BACK_RECT.center), ("close", ""))

    def test_unscaled_event_position_and_outside_click(self):
        self.assertIsNone(self.click((470, 470)))
        self.assertFalse(self.browser.details_open)
        self.browser.handle_event(pygame.event.Event(pygame.MOUSEBUTTONUP, button=1, pos=(40, 90)))
        self.assertTrue(self.browser.details_open)

    def test_details_scroll_keys_wheel_and_reset(self):
        self.key(pygame.K_RETURN)
        self.key(pygame.K_DOWN)
        self.assertEqual(self.browser.detail_scroll, 1)
        self.key(pygame.K_PAGEDOWN)
        self.assertEqual(self.browser.detail_scroll, 9)
        self.key(pygame.K_PAGEUP)
        self.assertEqual(self.browser.detail_scroll, 1)
        self.key(pygame.K_UP)
        self.assertEqual(self.browser.detail_scroll, 0)
        self.key(pygame.K_END)
        limit = self.browser._detail_limit
        self.assertGreater(limit, 0)
        self.assertEqual(self.browser.detail_scroll, limit)
        self.browser.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-100))
        self.assertEqual(self.browser.detail_scroll, limit)
        self.browser.draw(self.surface)
        self.key(pygame.K_HOME)
        self.assertEqual(self.browser.detail_scroll, 0)
        self.key(pygame.K_END)
        self.key(pygame.K_ESCAPE)
        self.key(pygame.K_RETURN)
        self.assertEqual(self.browser.detail_scroll, 0)

    def test_all_detail_content_is_wrapped_and_only_three_examples(self):
        self.key(pygame.K_RETURN)
        content = "\n".join(self.browser._detail_lines)
        for text in ("Skill 0", "Examination", "skill-0", "neutral description", "Alternate name",
                     "Please examine", "Perform an", "Check the", "Illustration is not evidence",
                     "Required parameters", "site", "Area to examine"):
            self.assertIn(text, content)
        self.assertNotIn("Hidden fourth", content)
        self.assertNotIn("optional", content)
        self.assertTrue(all(self.browser._font.size(line)[0] <= self.browser.TEXT_RECT.width - 8
                            for line in self.browser._detail_lines))

    def test_empty_catalog_draw_and_events(self):
        self.browser = SkillBrowser([])
        for key in (pygame.K_RETURN, pygame.K_END, pygame.K_HOME, pygame.K_DOWN, pygame.K_PAGEDOWN):
            self.assertIsNone(self.key(key))
        self.click((40, 90))
        self.assertFalse(self.browser.details_open)
        self.assertIsNone(self.browser.selected_skill)
        self.browser.draw(self.surface)
        self.assertEqual(self.key(pygame.K_ESCAPE), ("close", ""))

    def test_missing_optional_content_and_fallback_request(self):
        self.browser = SkillBrowser([skill(examples=(), note="", aliases=(), parameters={})])
        self.key(pygame.K_RETURN)
        self.browser.draw(self.surface)
        self.assertIn("No examples supplied.", self.browser._detail_lines)
        self.assertEqual(self.key(pygame.K_RETURN), ("draft", "Please perform Skill 0."))

    def test_art_is_lazy_scaled_cached_and_shared(self):
        path = Path(__file__).resolve().parent / "conceptual.png"
        self.browser = SkillBrowser([skill(icon=path), skill(1, icon=path)])
        image = pygame.Surface((600, 300), pygame.SRCALPHA)
        with patch("src.skill_browser.pygame.image.load", return_value=image) as load:
            self.browser.draw(self.surface)
            load.assert_not_called()
            self.key(pygame.K_RETURN)
            self.browser.draw(self.surface)
            self.browser.draw(self.surface)
            self.assertEqual(self.browser._art_cache[path].get_size(), (120, 60))
            self.key(pygame.K_ESCAPE)
            self.key(pygame.K_DOWN)
            self.key(pygame.K_RETURN)
            self.browser.draw(self.surface)
            load.assert_called_once_with(str(path))

    def test_unreadable_art_falls_back_without_repeated_load(self):
        path = Path(__file__).resolve().parent / "missing.png"
        self.browser = SkillBrowser([skill(icon=path)])
        self.key(pygame.K_RETURN)
        with patch("src.skill_browser.pygame.image.load", side_effect=pygame.error("invalid")) as load:
            self.browser.draw(self.surface)
            self.browser.draw(self.surface)
            load.assert_called_once()
        self.assertIsNone(self.browser._art_cache[path])

    def test_draw_preserves_clip_and_bounds_with_long_content(self):
        self.browser = SkillBrowser([skill(name="Very long name " * 60, description="X" * 1200)])
        self.surface.fill((255, 0, 255))
        clip = pygame.Rect(12, 12, 456, 456)
        self.surface.set_clip(clip)
        self.key(pygame.K_RETURN)
        self.key(pygame.K_END)
        self.browser.draw(self.surface)
        self.assertEqual(self.surface.get_clip(), clip)
        self.assertEqual(self.surface.get_at((0, 0)), (255, 0, 255, 255))
        self.assertTrue(all(self.browser._font.size(line)[0] <= self.browser.TEXT_RECT.width - 8
                            for line in self.browser._detail_lines))

    def test_browsing_does_not_mutate_catalog(self):
        original = [vars(entry).copy() for entry in self.browser.catalog]
        self.key(pygame.K_RETURN)
        self.browser.draw(self.surface)
        self.key(pygame.K_RETURN)
        self.assertEqual([vars(entry) for entry in self.browser.catalog], original)


if __name__ == "__main__":
    unittest.main()
