import unittest

from lead_intake.events import CREATE, Delivery, validate
from lead_intake.features import FEATURES, from_history, from_intake


class EventAndFeatureTests(unittest.TestCase):
    def test_create_normalizes_valid_contact_values_and_preserves_postcode(self):
        row = {
            "event_id": "event-a", "external_id": "lead-a", "event_type": CREATE,
            "occurred_at_utc": "2026-11-24T11:00:00-01:00", "source": "chat",
            "first_name": "  Ana   María ", "last_name": " Silva ", "email": " ANA@MAIL.EXAMPLE ",
            "phone": "030 / 12345678", "postcode": "01067", "country": "de",
            "language": "pt", "project_stage": "planning", "service": "structural",
            "message": "We would like to test a new building design.",
        }
        event, reasons = validate(Delivery(row, [], None))
        self.assertFalse(reasons)
        self.assertEqual(event.fields["first_name"], "Ana María")
        self.assertEqual(event.fields["email"], "ana@mail.example")
        self.assertEqual(event.fields["phone"], "+493012345678")
        self.assertEqual(event.fields["postcode"], "01067")
        self.assertTrue(event.clock[0].startswith("2026-11-24T12:00:00"))

    def test_invalid_create_reports_all_applicable_errors(self):
        event, reasons = validate(Delivery({
            "event_id": "a", "external_id": "b", "event_type": CREATE, "source": "fax",
            "occurred_at_utc": "2026-11-24T11:00:00", "first_name": "Alice", "email": " a  @host.test ",
            "phone": "123", "postcode": "123", "country": "DE", "language": "unknown",
            "project_stage": "planning", "service": "structural", "message": "asdf!",
        }, [], None))
        self.assertIsNone(event)
        self.assertEqual(set(reasons), {
            "invalid occurred_at_utc", "missing last_name", "invalid source", "invalid language",
            "invalid email", "invalid phone", "invalid German postcode (expected five digits)",
            "placeholder-only message",
        })

    def test_training_and_serving_adapters_produce_equivalent_precontact_input(self):
        common = {"language": " DE ", "project_stage": "planning", "service": "structural"}
        intake = {
            **common, "source": "chat", "utm_source": " Google ", "utm_campaign": "Search_Statik",
            "phone": "+493012345678", "message": "  Test message  ", "external_id": "x",
            "minutes_to_first_contact": "22",
        }
        historical = {
            **common, "channel": "google", "campaign": "search_statik", "phone_given": "yes",
            "message_length": "12", "lead_id": "y", "won": "1",
            "contract_value_eur_net": "500", "minutes_to_first_contact": "834", "weekend": "yes",
        }
        self.assertEqual(from_intake(intake), from_history(historical))
        self.assertEqual(tuple(from_history(historical)), FEATURES)
        optional = from_intake({"source": "partner_api"})
        self.assertEqual((optional["channel"], optional["campaign"]), ("partner", "unknown"))
        self.assertEqual((optional["phone_given"], optional["message_length"]), (0, 0))
        self.assertEqual(from_intake({"source": "web_form"})["channel"], "unknown")


if __name__ == "__main__":
    unittest.main()
