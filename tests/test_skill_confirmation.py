import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

from src.diagnostic_skills import load_catalog
from src.skill_confirmation import SkillConfirmation


class SkillConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def setUp(self):
        self.screen = pygame.Surface((480, 480))
        self.confirmation = SkillConfirmation(
            "Urinalysis", {}, "Please do a urine analysis.", skill_id="urinalysis",
        )

    def key(self, key):
        return self.confirmation.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key), (-1, -1))

    def test_simple_confirmation_is_short_and_has_specimen_illustration(self):
        dialog = self.confirmation
        self.assertIsNotNone(dialog.art)
        self.assertLess(len(dialog.text.split()), 15)
        self.assertNotIn("{}", dialog.text)
        self.assertNotIn("Your request", dialog.text)
        dialog.draw(self.screen)
        self.assertEqual(dialog.max_scroll, 0)
        generic = SkillConfirmation("Another skill", {}, "")
        self.assertNotEqual(pygame.image.tobytes(dialog.art, "RGBA"), pygame.image.tobytes(generic.art, "RGBA"))

    def test_clinical_options_stay_visible_and_full_context_is_in_details(self):
        self.confirmation = SkillConfirmation("Urine NAAT", {"specimen": "urine", "consent_turn": 17},
                                              "Check for infection", skill_id="urine_nucleic_acid_amplification_test")
        self.assertEqual(self.confirmation.text, "Specimen: urine")
        self.assertIsNone(self.key(pygame.K_d))
        self.assertIn("Check for infection", self.confirmation.text)
        self.assertIn('"consent_turn": 17', self.confirmation.text)
        self.assertIn("consent", self.confirmation.text)
        self.confirmation.draw(self.screen)
        self.key(pygame.K_PAGEDOWN)
        self.assertGreater(self.confirmation.scroll, 0)
        self.assertIsNone(self.key(pygame.K_d))
        self.assertEqual(self.confirmation.scroll, 0)
        self.assertEqual(self.confirmation.text, "Specimen: urine")

    def test_disclosure_does_not_approve_and_cancel_remains_default(self):
        rect = self.confirmation.DETAILS_RECT
        self.assertIsNone(self.confirmation.handle_event(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1), rect.center,
        ))
        self.assertTrue(self.confirmation.details_open)
        self.assertIs(self.key(pygame.K_RETURN), False)
        self.key(pygame.K_TAB)
        self.assertIs(self.key(pygame.K_RETURN), True)
        self.assertIs(self.key(pygame.K_ESCAPE), False)
        for index, button in enumerate(self.confirmation.buttons):
            self.assertEqual(self.confirmation.handle_event(
                pygame.event.Event(pygame.MOUSEBUTTONUP, button=1), button.center,
            ), bool(index))

    def test_selected_sequencing_art_is_loaded_once_and_aspect_ratio_is_preserved(self):
        skill = next(s for s in load_catalog() if s.id == "viral_metagenomic_sequencing")
        self.assertEqual(skill.icon.name, "viral-nanopore-sequencing-illustration.png")
        with patch("src.skill_art.pygame.image.load", wraps=pygame.image.load) as load:
            dialog = SkillConfirmation(skill.name, {"specimen": "nasal_swab", "workflow": "RNA"}, "",
                                       skill_id=skill.id, icon=skill.icon)
            for _ in range(3):
                dialog.draw(self.screen)
            load.assert_called_once_with(str(skill.icon))
        self.assertEqual(dialog.art.get_size(), (176, 117))
        self.assertIn("Specimen: nasal swab", dialog.text)
        self.assertIn("Workflow: RNA", dialog.text)

    def test_missing_image_uses_placeholder_without_blocking_confirmation(self):
        with self.assertLogs("src.skill_art", level="WARNING"):
            dialog = SkillConfirmation("Skill", {}, "", icon=Path("/missing/skill.png"))
        dialog.draw(self.screen)
        self.assertIsNotNone(dialog.art)
        self.assertIs(dialog.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE), (-1, -1)), False)

    def test_long_content_scrolls_without_covering_controls_or_changing_clip(self):
        dialog = SkillConfirmation("Long skill name " * 40, {"target": "Long target " * 120}, "Request " * 300)
        clip = pygame.Rect(0, 0, 480, 480)
        self.screen.set_clip(clip)
        dialog.draw(self.screen)
        self.assertEqual(self.screen.get_clip(), clip)
        self.assertGreater(dialog.max_scroll, 0)
        dialog.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d), (-1, -1))
        dialog.draw(self.screen)
        self.assertIn("Request " * 300, dialog.text)
        for control in (*dialog.buttons, dialog.DETAILS_RECT):
            self.assertFalse(control.colliderect(dialog.CONTENT_RECT))
        self.assertTrue(all(dialog.name_font.size(line)[0] <= 236 for line in dialog.name_lines))


if __name__ == "__main__":
    unittest.main()
