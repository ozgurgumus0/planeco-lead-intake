import unittest
from dataclasses import replace
from pathlib import Path

from lead_intake.files import parse_time
from lead_intake.routing import assign, load_roster

DATA = Path(__file__).resolve().parents[1] / "data"


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roster = load_roster(DATA / "sales_team.csv")

    def lead(self, **fields):
        return {"source": "web_form", "service": "building_permit", "language": "de", **fields}

    def test_partner_manager_language_conflict_goes_to_queue(self):
        arrival = parse_time("2026-11-24T11:00:00Z")
        lead = self.lead(source="partner_api", language="pt")
        result = assign(lead, 0.8, arrival, self.roster, {})
        self.assertEqual((result.owner, result.reason, result.lisbon_text), ("rep_06", "queue", ""))
        accepted = assign({**lead, "language": "en"}, 0.8, arrival, self.roster, {})
        self.assertTrue(accepted.active)
        self.assertEqual(accepted.owner, "rep_06")

    def test_absence_is_inclusive_and_later_eligible_shift_is_used(self):
        rep = self.roster["rep_02"]
        result = assign(self.lead(), 0.2, parse_time("2026-11-25T11:00:00Z"), {rep.rep_id: rep}, {})
        self.assertEqual(result.lisbon_text, "2026-11-26 09:00")

    def test_weekend_and_lisbon_daylight_saving(self):
        arrival = parse_time("2026-07-18T17:00:00Z")
        result = assign(self.lead(), 0.4, arrival, self.roster, {})
        self.assertEqual(result.owner, "rep_04")
        self.assertEqual(result.lisbon_text, "2026-07-20 08:00")
        self.assertEqual(result.at_utc, "2026-07-20T07:00:00Z")

    def test_eligible_at_arrival_beats_preference_for_seniority(self):
        result = assign(self.lead(), 0.8, parse_time("2026-11-24T08:12:00Z"), self.roster, {})
        self.assertEqual(result.owner, "rep_04")
        self.assertIn("available at arrival", result.reason)

    def test_high_score_prefers_comparably_loaded_senior_and_ties_are_stable(self):
        senior = replace(self.roster["rep_01"], rep_id="rep_z")
        mid = replace(self.roster["rep_04"], rep_id="rep_a")
        roster = {rep.rep_id: rep for rep in (senior, mid)}
        arrival = parse_time("2026-11-24T11:00:00Z")
        self.assertEqual(assign(self.lead(), 0.2, arrival, roster, {}).owner, "rep_a")
        close = assign(self.lead(), 0.3, arrival, roster, {"rep_z": 1})
        self.assertEqual(close.owner, "rep_z")
        self.assertIn("seniority preference", close.reason)
        self.assertEqual(assign(self.lead(), 0.3, arrival, roster, {"rep_z": 3}).owner, "rep_a")

    def test_capacity_overflow_is_never_an_active_assignment(self):
        loads = {rep.rep_id: rep.maximum for rep in self.roster.values()}
        result = assign(self.lead(), 0.95, parse_time("2026-11-24T11:00:00Z"), self.roster, loads)
        self.assertFalse(result.active)
        self.assertEqual(result.reason, "queue")


if __name__ == "__main__":
    unittest.main()
