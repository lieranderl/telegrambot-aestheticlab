import unittest

from src.models import ChannelMapping, SyncDelta
from src.services.webhook_service import WebhookService


class FakeSecretStore:
    def __init__(self, mapping: ChannelMapping | None, sync_token: str | None) -> None:
        self.mapping = mapping
        self.sync_token = sync_token
        self.saved_tokens: list[tuple[str, str]] = []

    def lookup_channel(self, channel_id: str) -> ChannelMapping | None:
        if self.mapping and self.mapping.channel_id == channel_id:
            return self.mapping
        return None

    def get_sync_token(self, calendar_id: str) -> str | None:
        return self.sync_token

    def save_sync_token(self, calendar_id: str, token: str) -> None:
        self.saved_tokens.append((calendar_id, token))


class FakeCalendarGateway:
    def __init__(
        self, delta: SyncDelta | None = None, initial_sync_token: str | None = None
    ) -> None:
        self.delta = delta
        self.initial_sync_token = initial_sync_token

    def get_initial_sync_token(self, calendar_id: str) -> str | None:
        return self.initial_sync_token

    def fetch_delta(self, calendar_id: str, sync_token: str) -> SyncDelta:
        if self.delta is None:
            raise AssertionError("delta was not configured")
        return self.delta


class FakeTelegramGateway:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.messages: list[str] = []

    async def send_message(self, text: str) -> None:
        if self.should_fail:
            raise RuntimeError("telegram failed")
        self.messages.append(text)


class WebhookServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_channel_returns_ok(self) -> None:
        service = WebhookService(
            FakeCalendarGateway(),
            FakeSecretStore(None, None),
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("missing", "exists")

        self.assertEqual(result["msg"], "unknown channel")

    async def test_first_sync_seeds_token_without_sending(self) -> None:
        mapping = ChannelMapping("channel", "resource", "calendar", "Main")
        secrets = FakeSecretStore(mapping, None)
        service = WebhookService(
            FakeCalendarGateway(initial_sync_token="seed-token"),
            secrets,
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "exists")

        self.assertEqual(result["msg"], "seeded sync token")
        self.assertEqual(secrets.saved_tokens, [("calendar", "seed-token")])

    async def test_sync_token_updates_only_after_successful_send(self) -> None:
        mapping = ChannelMapping("channel", "resource", "calendar", "Main")
        secrets = FakeSecretStore(mapping, "old-token")
        service = WebhookService(
            FakeCalendarGateway(
                delta=SyncDelta(
                    items=[
                        {
                            "summary": "Appointment",
                            "status": "confirmed",
                            "start": {"dateTime": "2026-03-11T10:00:00+01:00"},
                            "end": {"dateTime": "2026-03-11T11:00:00+01:00"},
                        }
                    ],
                    next_sync_token="new-token",
                )
            ),
            secrets,
            FakeTelegramGateway(should_fail=True),
        )

        result = await service.handle_webhook("channel", "exists")

        self.assertEqual(result["sent"], 0)
        self.assertEqual(secrets.saved_tokens, [])


if __name__ == "__main__":
    unittest.main()
