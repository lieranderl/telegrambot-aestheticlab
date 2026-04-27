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
                "DELIVERY_TTL_DAYS": "14",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.telegram_token, "token")
        self.assertEqual(settings.calendar_labels, {"one@example.com": "One"})
        self.assertEqual(settings.project_id, "project-a")
        self.assertEqual(settings.state_collection_prefix, "prefix_a")
        self.assertEqual(settings.renewal_lead_minutes, 30)
        self.assertEqual(settings.delivery_ttl_days, 14)

    def test_from_env_raises_for_missing_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("TELEGRAM_TOKEN", str(ctx.exception))

    def test_from_env_raises_when_all_calendar_entries_are_invalid(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "WEBHOOK_URL": "https://example.com/webhook",
                "CALENDAR_IDS": "invalid;|missing",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("CALENDAR_IDS", str(ctx.exception))

    def test_from_env_raises_for_non_https_webhook_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "WEBHOOK_URL": "http://example.com/webhook",
                "CALENDAR_IDS": "one@example.com|One",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("WEBHOOK_URL", str(ctx.exception))

    def test_from_env_raises_for_invalid_numeric_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "WEBHOOK_URL": "https://example.com/webhook",
                "CALENDAR_IDS": "one@example.com|One",
                "RENEWAL_LEAD_MINUTES": "bad",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("must be integers", str(ctx.exception))

    def test_from_env_raises_for_nonpositive_retention(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "WEBHOOK_URL": "https://example.com/webhook",
                "CALENDAR_IDS": "one@example.com|One",
                "DELIVERY_TTL_DAYS": "0",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("DELIVERY_TTL_DAYS", str(ctx.exception))

    def test_from_env_raises_for_invalid_collection_prefix(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_TOKEN": "token",
                "TELEGRAM_CHAT_ID": "chat",
                "WEBHOOK_URL": "https://example.com/webhook",
                "CALENDAR_IDS": "one@example.com|One",
                "STATE_COLLECTION_PREFIX": "bad/prefix",
            },
            clear=True,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                Settings.from_env()

        self.assertIn("STATE_COLLECTION_PREFIX", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
