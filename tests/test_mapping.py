import unittest

from src.models import CalendarState, ChannelMapping
from src.utils.ids import safe_suffix_from_cal_id, sync_secret_id_for


class ModelTests(unittest.TestCase):
    def test_channel_mapping_keeps_security_metadata(self) -> None:
        mapping = ChannelMapping(
            channel_id="channel-1",
            resource_id="resource-1",
            calendar_id="calendar@example.com",
            label="Main",
            token="token-1",
            expiration_ms=123,
        )

        self.assertEqual(mapping.token, "token-1")
        self.assertEqual(mapping.expiration_ms, 123)

    def test_calendar_state_keeps_update_time(self) -> None:
        state = CalendarState(
            calendar_id="calendar@example.com",
            label="Main",
            sync_token="sync-1",
            update_time="2026-04-27T12:00:00Z",
        )

        self.assertEqual(state.update_time, "2026-04-27T12:00:00Z")


class IdHelperTests(unittest.TestCase):
    def test_safe_suffix_is_stable(self) -> None:
        self.assertEqual(
            safe_suffix_from_cal_id("calendar@example.com"),
            safe_suffix_from_cal_id("calendar@example.com"),
        )

    def test_sync_secret_id_prefixes_hash(self) -> None:
        secret_id = sync_secret_id_for("calendar@example.com")
        self.assertTrue(secret_id.startswith("cal-sync-"))


if __name__ == "__main__":
    unittest.main()
