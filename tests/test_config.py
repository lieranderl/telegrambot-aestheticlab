import os
import unittest
from unittest.mock import patch

from src.config import Settings, _parse_bool, parse_calendar_entries


class ParseCalendarEntriesTests(unittest.TestCase):
    def test_parses_valid_entries_and_ignores_invalid_ones(self) -> None:
        entries = parse_calendar_entries(
            "one@example.com|One;invalid;two@example.com|Two"
        )

        self.assertEqual(
            [(entry.calendar_id, entry.label) for entry in entries],
            [("one@example.com", "One"), ("two@example.com", "Two")],
        )

    def test_ignores_incomplete_entries(self) -> None:
        entries = parse_calendar_entries("one@example.com|One;|NoId;two@example.com|")

        self.assertEqual(
            [(entry.calendar_id, entry.label) for entry in entries],
            [("one@example.com", "One")],
        )

    def test_ignores_empty_segments(self) -> None:
        entries = parse_calendar_entries("one@example.com|One;;")

        self.assertEqual(len(entries), 1)


class ParseBoolTests(unittest.TestCase):
    def test_parse_bool_defaults_when_missing(self) -> None:
        self.assertTrue(_parse_bool(None, default=True))

    def test_parse_bool_accepts_truthy_values(self) -> None:
        self.assertTrue(_parse_bool(" yes "))
        self.assertFalse(_parse_bool("no"))


class SettingsTests(unittest.TestCase):
    def test_from_env_loads_required_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "WEBHOOK_URL": "https://example.com/webhook",
                "CALENDAR_IDS": "one@example.com|One",
                "GOOGLE_CLOUD_PROJECT": "project-a",
                "STATE_COLLECTION_PREFIX": "prefix-a",
                "RENEWAL_LEAD_MINUTES": "30",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.telegram_token, "token")
        self.assertEqual(settings.calendar_labels, {"one@example.com": "One"})
        self.assertEqual(settings.project_id, "project-a")
        self.assertEqual(settings.state_collection_prefix, "prefix-a")
        self.assertEqual(settings.renewal_lead_minutes, 30)

    def test_from_env_raises_for_missing_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("TELEGRAM_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
