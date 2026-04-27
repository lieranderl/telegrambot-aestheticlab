import unittest

from src.services.formatting import format_event_message


class FormatEventMessageTests(unittest.TestCase):
    def test_formats_cancelled_event(self) -> None:
        event = {
            "summary": "Hair Appointment",
            "status": "cancelled",
            "start": {"dateTime": "2026-03-11T10:00:00+01:00"},
            "end": {"dateTime": "2026-03-11T11:00:00+01:00"},
        }

        message = format_event_message(event, "Main Calendar")

        self.assertIn("❌ Event cancelled: Hair Appointment", message)
        self.assertIn("📂 Main Calendar", message)

    def test_formats_all_day_event_with_inclusive_end_date(self) -> None:
        event = {
            "summary": "Vacation",
            "status": "confirmed",
            "start": {"date": "2026-03-11"},
            "end": {"date": "2026-03-12"},
        }

        message = format_event_message(event, "Main Calendar")

        self.assertIn("🕑 2026-03-11 → 2026-03-11", message)


if __name__ == "__main__":
    unittest.main()
