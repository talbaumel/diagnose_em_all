from __future__ import annotations

import unittest

from src.care_plan import CarePlan, Prescription, Referral


class CarePlanTests(unittest.TestCase):
    def test_records_multiple_orders_without_duplicates(self):
        plan = CarePlan()
        prescription = Prescription("Example medication", "Directions entered by player", "Symptom relief")
        referral = Referral("Physiotherapy", "Mobility assessment", "Routine")
        self.assertTrue(plan.add(prescription))
        self.assertTrue(plan.add(referral))
        self.assertFalse(plan.add(prescription))
        self.assertEqual(plan.prescriptions, [prescription])
        self.assertEqual(plan.referrals, [referral])
        self.assertIn("Physiotherapy", plan.summary())
        self.assertIn("Directions entered by player", prescription.message())

    def test_rejects_incomplete_prescriptions_and_referrals(self):
        for values in (("", "directions", "reason"), ("medication", " ", "reason"), ("medication", "directions", "")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Prescription(*values)
        with self.assertRaises(ValueError):
            Referral("", "reason")
        with self.assertRaises(ValueError):
            Referral("Clinic", "reason", "whenever")

    def test_empty_care_plan_is_valid(self):
        plan = CarePlan()
        self.assertEqual(plan.prescriptions, [])
        self.assertEqual(plan.referrals, [])
        self.assertIn("No prescriptions recorded", plan.summary())


if __name__ == "__main__":
    unittest.main()