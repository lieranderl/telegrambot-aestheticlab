import unittest
from unittest.mock import patch

from src.models import CalendarEntry, ChannelMapping, WatchRegistration
from src.services.registration import RegistrationService

MAIN = CalendarEntry("calendar", "Main")


class FakeCalendarGateway:
    def __init__(self, register_results=None, stop_errors=None) -> None:
        self.register_results = list(register_results or [])
        self.register_calls = []
        self.stop_calls = []
        self.stop_errors = stop_errors or {}

    def register_watch(
        self,
        calendar_id: str,
        address: str,
        token: str,
    ) -> WatchRegistration:
        self.register_calls.append((calendar_id, address, token))
        result = self.register_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        self.stop_calls.append((channel_id, resource_id))
        exc = self.stop_errors.get(channel_id)
        if exc:
            raise exc


class FakeStateStore:
    def __init__(self, mappings=None) -> None:
        self.upserts = []
        self.deleted = []
        self.mappings = list(mappings or [])

    async def upsert_channel_mapping(self, mapping: ChannelMapping) -> None:
        self.upserts.append(mapping)

    async def load_channel_mappings(self):
        return list(self.mappings)

    async def delete_channel_mapping(self, channel_id: str) -> None:
        self.deleted.append(channel_id)


class RegistrationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_all_success(self) -> None:
        calendar = CalendarEntry("calendar@example.com", "Main")
        gateway = FakeCalendarGateway(
            [
                WatchRegistration(
                    channel_id="channel",
                    resource_id="resource",
                    token="token-1",
                    expiration_ms=123,
                    payload={"id": "channel"},
                )
            ]
        )
        state_store = FakeStateStore()
        service = RegistrationService(
            gateway,
            state_store,
            [calendar],
            "https://webhook",
        )

        with patch(
            "src.services.registration.secrets.token_urlsafe", return_value="token-1"
        ):
            result = await service.register_all()

        self.assertEqual(result["errors"], None)
        self.assertEqual(state_store.upserts[0].token, "token-1")
        self.assertEqual(
            gateway.register_calls[0][:2], ("calendar@example.com", "https://webhook")
        )

    async def test_register_all_replaces_existing_channel_for_same_calendar(
        self,
    ) -> None:
        calendar = CalendarEntry("calendar@example.com", "Main")
        existing = ChannelMapping(
            channel_id="old-channel",
            resource_id="old-resource",
            calendar_id="calendar@example.com",
            label="Main",
            token="old-token",
            expiration_ms=1,
        )
        gateway = FakeCalendarGateway(
            [
                WatchRegistration(
                    channel_id="new-channel",
                    resource_id="new-resource",
                    token="new-token",
                    expiration_ms=2,
                    payload={"id": "new-channel"},
                )
            ]
        )
        state_store = FakeStateStore(mappings=[existing])
        service = RegistrationService(
            gateway, state_store, [calendar], "https://webhook"
        )

        with patch(
            "src.services.registration.secrets.token_urlsafe", return_value="new-token"
        ):
            await service.register_all()

        self.assertEqual(gateway.stop_calls, [("old-channel", "old-resource")])
        self.assertEqual(state_store.deleted, ["old-channel"])

    async def test_cleanup_all_returns_no_channels_when_empty(self) -> None:
        service = RegistrationService(
            FakeCalendarGateway(),
            FakeStateStore(),
            [],
            "https://webhook",
        )

        result = await service.cleanup_all()

        self.assertEqual(result["msg"], "no channels to clean")

    async def test_cleanup_all_collects_stop_errors(self) -> None:
        mappings = [
            ChannelMapping(
                "channel",
                "resource",
                "calendar",
                "Main",
                "token",
                123,
            )
        ]
        gateway = FakeCalendarGateway(stop_errors={"channel": RuntimeError("boom")})
        state_store = FakeStateStore(mappings=mappings)
        service = RegistrationService(gateway, state_store, [], "https://webhook")

        result = await service.cleanup_all()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(state_store.deleted, ["channel"])
        self.assertEqual(len(result["errors"]), 1)

    async def test_register_all_collects_registration_errors(self) -> None:
        calendar = CalendarEntry("calendar@example.com", "Main")
        gateway = FakeCalendarGateway([RuntimeError("boom")])
        service = RegistrationService(
            gateway,
            FakeStateStore(),
            [calendar],
            "https://webhook",
        )

        result = await service.register_all()

        self.assertEqual(len(result["errors"]), 1)

    async def test_renew_expiring_channels_success(self) -> None:
        mappings = [
            ChannelMapping(
                "channel",
                "resource",
                "calendar",
                "Main",
                "token",
                1,
            )
        ]
        gateway = FakeCalendarGateway(
            [
                WatchRegistration(
                    channel_id="new-channel",
                    resource_id="new-resource",
                    token="new-token",
                    expiration_ms=2,
                    payload={"id": "new-channel"},
                )
            ]
        )
        state_store = FakeStateStore(mappings=mappings)
        service = RegistrationService(gateway, state_store, [MAIN], "https://webhook")

        with patch(
            "src.services.registration.secrets.token_urlsafe", return_value="new-token"
        ):
            result = await service.renew_expiring_channels(120)

        self.assertEqual(result["renewed"][0]["new_channel_id"], "new-channel")
        self.assertEqual(state_store.deleted, ["channel"])

    async def test_renew_expiring_channels_skips_fresh_entries(self) -> None:
        mappings = [
            ChannelMapping(
                "channel",
                "resource",
                "calendar",
                "Main",
                "token",
                9_999_999_999_999,
            )
        ]
        service = RegistrationService(
            FakeCalendarGateway(),
            FakeStateStore(mappings=mappings),
            [MAIN],
            "https://webhook",
        )

        result = await service.renew_expiring_channels(120)

        self.assertEqual(result["renewed"], [])

    async def test_renew_expiring_channels_collects_errors(self) -> None:
        mappings = [
            ChannelMapping(
                "channel",
                "resource",
                "calendar",
                "Main",
                "token",
                None,
            )
        ]
        gateway = FakeCalendarGateway([RuntimeError("boom")])
        service = RegistrationService(
            gateway,
            FakeStateStore(mappings=mappings),
            [MAIN],
            "https://webhook",
        )

        result = await service.renew_expiring_channels(120)

        self.assertEqual(len(result["errors"]), 1)

    async def test_renew_drops_mapping_when_old_channel_stop_fails(self) -> None:
        mappings = [ChannelMapping("old", "res", "calendar", "Main", "t", 1)]
        gateway = FakeCalendarGateway(
            [WatchRegistration("new", "new-res", "t2", 2, {"id": "new"})],
            stop_errors={"old": RuntimeError("404 channel not found")},
        )
        state_store = FakeStateStore(mappings=mappings)
        service = RegistrationService(gateway, state_store, [MAIN], "https://webhook")

        result = await service.renew_expiring_channels(120)

        self.assertEqual(result["errors"], None)
        self.assertEqual(result["renewed"][0]["new_channel_id"], "new")
        self.assertEqual(state_store.deleted, ["old"])

    async def test_renew_keeps_one_channel_per_calendar(self) -> None:
        mappings = [
            ChannelMapping("stale", "r1", "calendar", "Main", "t", 1),
            ChannelMapping("fresh", "r2", "calendar", "Main", "t", 9_999_999_999_999),
            ChannelMapping("older", "r3", "calendar", "Main", "t", None),
        ]
        gateway = FakeCalendarGateway()
        state_store = FakeStateStore(mappings=mappings)
        service = RegistrationService(gateway, state_store, [MAIN], "https://webhook")

        result = await service.renew_expiring_channels(120)

        self.assertEqual(result["renewed"], [])
        self.assertEqual(sorted(result["retired"]), ["older", "stale"])
        self.assertEqual(gateway.register_calls, [])
        self.assertEqual(sorted(state_store.deleted), ["older", "stale"])

    async def test_renew_retires_channels_for_unconfigured_calendars(self) -> None:
        mappings = [ChannelMapping("gone", "r", "removed", "Old", "t", 1)]
        gateway = FakeCalendarGateway()
        state_store = FakeStateStore(mappings=mappings)
        service = RegistrationService(gateway, state_store, [MAIN], "https://webhook")

        result = await service.renew_expiring_channels(120)

        self.assertEqual(result["retired"], ["gone"])
        self.assertEqual(gateway.stop_calls, [("gone", "r")])
        self.assertEqual(gateway.register_calls, [])
        self.assertEqual(state_store.deleted, ["gone"])


if __name__ == "__main__":
    unittest.main()
