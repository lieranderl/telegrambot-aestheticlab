import unittest

from src.models import CalendarEntry, ChannelMapping, WatchRegistration
from src.services.registration import RegistrationService


class FakeCalendarGateway:
    def __init__(self, register_results=None, stop_errors=None) -> None:
        self.register_results = list(register_results or [])
        self.register_calls = []
        self.stop_calls = []
        self.stop_errors = stop_errors or {}

    def register_watch(self, calendar_id: str, address: str) -> WatchRegistration:
        self.register_calls.append((calendar_id, address))
        result = self.register_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        self.stop_calls.append((channel_id, resource_id))
        exc = self.stop_errors.get(channel_id)
        if exc:
            raise exc


class FakeSecretStore:
    def __init__(self, mappings=None, reset_error: Exception | None = None) -> None:
        self.upserts = []
        self.mappings = list(mappings or [])
        self.reset_calls = 0
        self.reset_error = reset_error

    def upsert_channel_mapping(self, *args) -> None:
        self.upserts.append(args)

    def load_channel_mappings(self):
        return list(self.mappings)

    def reset_channel_mapping_secret(self) -> None:
        self.reset_calls += 1
        if self.reset_error:
            raise self.reset_error


class RegistrationServiceTests(unittest.TestCase):
    def test_register_all_success(self) -> None:
        calendar = CalendarEntry("calendar@example.com", "Main")
        gateway = FakeCalendarGateway(
            [
                WatchRegistration(
                    channel_id="channel",
                    resource_id="resource",
                    payload={"id": "channel"},
                )
            ]
        )
        secrets = FakeSecretStore()
        service = RegistrationService(gateway, secrets, [calendar], "https://webhook")

        result = service.register_all()

        self.assertEqual(result["errors"], None)
        self.assertEqual(
            secrets.upserts[0], ("channel", "resource", "calendar@example.com", "Main")
        )

    def test_register_all_resets_secret_on_destroyed_failures(self) -> None:
        calendar = CalendarEntry("calendar@example.com", "Main")
        gateway = FakeCalendarGateway([RuntimeError("DESTROYED")])
        secrets = FakeSecretStore()
        service = RegistrationService(gateway, secrets, [calendar], "https://webhook")

        result = service.register_all()

        self.assertIsNotNone(result["errors"])
        self.assertEqual(secrets.reset_calls, 1)

    def test_cleanup_all_returns_no_channels_when_empty(self) -> None:
        service = RegistrationService(
            FakeCalendarGateway(),
            FakeSecretStore(),
            [],
            "https://webhook",
        )

        result = service.cleanup_all()

        self.assertEqual(result["msg"], "no channels to clean")

    def test_cleanup_all_collects_stop_errors(self) -> None:
        mappings = [ChannelMapping("channel", "resource", "calendar", "Main")]
        gateway = FakeCalendarGateway(stop_errors={"channel": RuntimeError("boom")})
        secrets = FakeSecretStore(mappings=mappings)
        service = RegistrationService(gateway, secrets, [], "https://webhook")

        result = service.cleanup_all()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(secrets.reset_calls, 1)
        self.assertEqual(len(result["errors"]), 1)

    def test_reset_secret_handles_failure(self) -> None:
        service = RegistrationService(
            FakeCalendarGateway(),
            FakeSecretStore(reset_error=RuntimeError("boom")),
            [],
            "https://webhook",
        )

        result = service.reset_secret()

        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
