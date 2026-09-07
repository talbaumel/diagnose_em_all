import unittest
from unittest.mock import patch

import pygame

from src.diagnostic_skills import ROOT, SkillEngine, load_catalog
from src.realtime_conversation import PatientType
from src.skill_result import SkillResultPanel, SkillResultSummary
from src.skill_art import thermometer_result_art


class SkillResultPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def panel(self, skill_id="temperature", result="Temperature is 36.8 C.", **kwargs):
        skill = next(skill for skill in load_catalog() if skill.id == skill_id)
        summary = SkillResultSummary(skill_id, skill.name, result, -1, "Full explanation remains available.")
        return SkillResultPanel(summary, skill.icon, **kwargs)

    def test_all_patient_temperatures_use_authored_readings_not_icon_numbers(self):
        for patient_type in PatientType:
            case = patient_type.name
            with self.subTest(case=case):
                engine = SkillEngine(case)
                result = engine.execute(engine.prepare("temperature", {}, ()), ())
                panel = self.panel(result=result["result"])
                expected = "38.0" if case == "COMMON_COLD_KID" else "39.2" if case == "FEVERISH_PATIENT" else "36.8"
                self.assertEqual(panel.reading, expected + " \N{DEGREE SIGN}C")
                self.assertEqual(panel.summary.result, result["result"])
                self.assertEqual(panel.draw(pygame.Surface((480, 480)), 0), 0)

    def test_art_loads_once_and_selected_sequencing_image_is_retained(self):
        with patch("src.skill_art.pygame.image.load", wraps=pygame.image.load) as load:
            panel = self.panel("viral_metagenomic_sequencing", "Authored organism report.")
            screen = pygame.Surface((480, 480))
            panel.draw(screen, 0)
            panel.draw(screen, 0)
            self.assertEqual(load.call_count, 1)
            self.assertTrue(load.call_args.args[0].endswith("viral-nanopore-sequencing-illustration.png"))

    def test_unrecognized_temperature_text_is_not_replaced_by_a_guess(self):
        panel = self.panel(result="Unable to obtain a numeric reading.")
        self.assertIsNone(panel.reading)
        self.assertIn("Unable to obtain", panel.report)

    def test_thermometer_reuses_original_casing_and_changes_only_display(self):
        images = [thermometer_result_art(reading)
                  for reading in ("36.8 \N{DEGREE SIGN}C", "38.0 \N{DEGREE SIGN}C",
                                  "39.2 \N{DEGREE SIGN}C", None)]
        self.assertTrue(all(image.get_size() == (312, 78) for image in images))
        self.assertEqual(len({pygame.image.tobytes(image, "RGBA") for image in images}), 4)
        for image in images[1:]:
            for region in (pygame.Rect(0, 0, 140, 78), pygame.Rect(228, 0, 84, 78)):
                self.assertEqual(pygame.image.tobytes(image.subsurface(region), "RGBA"),
                                 pygame.image.tobytes(images[0].subsurface(region), "RGBA"))

    def test_result_image_loads_once_and_original_evidence_is_unchanged(self):
        path = ROOT / "data/sprites/tests/thermometer.png"
        original = path.read_bytes()
        with patch("src.skill_art.pygame.image.load", wraps=pygame.image.load) as load:
            panel = self.panel()
            panel.draw(pygame.Surface((480, 480)), 0)
            panel.draw(pygame.Surface((480, 480)), 0)
            load.assert_called_once()
            self.assertTrue(load.call_args.args[0].endswith("data/sprites/tests/thermometer.png"))
        self.assertEqual(path.read_bytes(), original)
        with patch("src.skill_art.pygame.image.load", side_effect=FileNotFoundError("missing image")):
            with self.assertLogs("src.skill_art", level="WARNING"):
                self.assertIsNotNone(self.panel().art)

    def test_art_fallback_and_bounds_for_every_skill(self):
        with patch("src.skill_result.load_thumbnail", return_value=None):
            for skill in load_catalog():
                panel = self.panel(skill.id)
                self.assertLessEqual(panel.art.get_height(), 96)
                self.assertLessEqual(panel.art.get_width(), 312)
                self.assertGreater(panel.art.get_bounding_rect().width, 0)

    def test_details_preserve_long_findings_rationale_and_original_evidence(self):
        report = "An important clinical limitation. " * 200
        original = pygame.Surface((512, 512))
        original.fill((123, 45, 67))
        panel = self.panel("urinalysis", report, evidence=original)
        screen = pygame.Surface((480, 480))
        self.assertGreater(panel.draw(screen, 0), 0)
        self.assertTrue(panel.toggle_details(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_d), (-1, -1)))
        self.assertIn(report, panel.report)
        self.assertIn(panel.summary.rationale, panel.report)
        expected = pygame.transform.smoothscale(original, (180, 180))
        self.assertEqual(pygame.image.tobytes(panel.evidence, "RGBA"), pygame.image.tobytes(expected, "RGBA"))
        self.assertEqual(original.get_at((0, 0)), (123, 45, 67, 255))
        maximum = panel.draw(screen, 0)
        panel.draw(screen, maximum)
        self.assertEqual(screen.get_clip(), screen.get_rect())
        self.assertFalse(panel.REPORT_RECT.colliderect(panel.DETAILS_RECT))
        self.assertTrue(panel.toggle_details(
            pygame.event.Event(pygame.MOUSEBUTTONUP, button=1), panel.DETAILS_RECT.center,
        ))
        self.assertFalse(panel.details_open)


if __name__ == "__main__":
    unittest.main()
