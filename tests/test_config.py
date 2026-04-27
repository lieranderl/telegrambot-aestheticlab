import os
import unittest
from unittest.mock import patch

from src.config import Settings, parse_calendar_entries


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
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.telegram_token, "token")
        self.assertEqual(settings.calendar_labels, {"one@example.com": "One"})
        self.assertEqual(settings.project_id, "project-a")

    def test_from_env_raises_for_missing_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("TELEGRAM_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
