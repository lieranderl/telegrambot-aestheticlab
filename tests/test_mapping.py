import unittest

from src.models import ChannelMapping
from src.utils.ids import safe_suffix_from_cal_id, sync_secret_id_for


class ChannelMappingTests(unittest.TestCase):
    def test_round_trip_line_serialization(self) -> None:
        mapping = ChannelMapping(
            channel_id="channel-1",
            resource_id="resource-1",
            calendar_id="calendar@example.com",
            label="Main",
        )

        parsed = ChannelMapping.from_line(mapping.to_line())

        self.assertEqual(parsed, mapping)

    def test_invalid_line_returns_none(self) -> None:
        self.assertIsNone(ChannelMapping.from_line("not|enough|parts"))


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
