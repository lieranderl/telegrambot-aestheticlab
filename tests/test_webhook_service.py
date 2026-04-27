import unittest

from src.errors import WebhookAuthenticationError, WebhookProcessingError
from src.models import CalendarState, ChannelMapping, SyncDelta
from src.services.webhook_service import WebhookService


class FakeStateStore:
    def __init__(
        self,
        mapping: ChannelMapping | None,
        calendar_state: CalendarState | None,
        duplicate_delivery: bool = False,
    ) -> None:
        self.mapping = mapping
        self.calendar_state = calendar_state
        self.duplicate_delivery = duplicate_delivery
        self.seeded_tokens: list[tuple[str, str, str]] = []
        self.saved_tokens: list[tuple[str, str, str, str | None]] = []
        self.cleared_deliveries: list[tuple[str, str, str]] = []
        self.mark_calls: list[tuple[str, str, str]] = []

    async def lookup_channel(self, channel_id: str) -> ChannelMapping | None:
        if self.mapping and self.mapping.channel_id == channel_id:
            return self.mapping
        return None

    async def get_calendar_state(self, calendar_id: str) -> CalendarState | None:
        return self.calendar_state

    async def seed_sync_token(self, calendar_id: str, label: str, sync_token: str) -> None:
        self.seeded_tokens.append((calendar_id, label, sync_token))

    async def save_sync_token(
        self,
        calendar_id: str,
        label: str,
        sync_token: str,
        *,
        expected_update_time: str | None = None,
    ) -> bool:
        self.saved_tokens.append((calendar_id, label, sync_token, expected_update_time))
        return True

    async def mark_delivery_attempt(
        self,
        calendar_id: str,
        event_id: str,
        event_version: str,
    ) -> bool:
        self.mark_calls.append((calendar_id, event_id, event_version))
        return not self.duplicate_delivery

    async def clear_delivery_attempt(
        self,
        calendar_id: str,
        event_id: str,
        event_version: str,
    ) -> None:
        self.cleared_deliveries.append((calendar_id, event_id, event_version))


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
    def _mapping(self) -> ChannelMapping:
        return ChannelMapping(
            "channel",
            "resource",
            "calendar",
            "Main",
            "token-1",
            123,
        )

    async def test_unknown_channel_returns_ok(self) -> None:
        service = WebhookService(
            FakeCalendarGateway(),
            FakeStateStore(None, None),
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("missing", "token-1", "resource", "exists")

        self.assertEqual(result["msg"], "unknown channel")

    async def test_invalid_token_raises(self) -> None:
        service = WebhookService(
            FakeCalendarGateway(),
            FakeStateStore(self._mapping(), None),
            FakeTelegramGateway(),
        )

        with self.assertRaises(WebhookAuthenticationError):
            await service.handle_webhook("channel", "wrong", "resource", "exists")

    async def test_first_sync_seeds_token_without_sending(self) -> None:
        state_store = FakeStateStore(self._mapping(), None)
        service = WebhookService(
            FakeCalendarGateway(initial_sync_token="seed-token"),
            state_store,
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(result["msg"], "seeded sync token")
        self.assertEqual(state_store.seeded_tokens, [("calendar", "Main", "seed-token")])

    async def test_sync_handshake_is_ignored(self) -> None:
        service = WebhookService(
            FakeCalendarGateway(),
            FakeStateStore(self._mapping(), None),
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "token-1", "resource", "sync")

        self.assertEqual(result["msg"], "sync handshake ignored")

    async def test_sync_token_updates_after_successful_send(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )
        service = WebhookService(
            FakeCalendarGateway(
                delta=SyncDelta(
                    items=[
                        {
                            "id": "event-1",
                            "updated": "2026-03-11T09:00:00Z",
                            "summary": "Appointment",
                            "status": "confirmed",
                            "start": {"dateTime": "2026-03-11T10:00:00+01:00"},
                            "end": {"dateTime": "2026-03-11T11:00:00+01:00"},
                        }
                    ],
                    next_sync_token="new-token",
                )
            ),
            state_store,
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(result["sent"], 1)
        self.assertEqual(
            state_store.saved_tokens,
            [("calendar", "Main", "new-token", "update-1")],
        )

    async def test_invalid_sync_token_reseeds(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )
        gateway = FakeCalendarGateway(initial_sync_token="seed-token")

        def broken_fetch(calendar_id: str, sync_token: str):
            raise RuntimeError("410 syncToken expired")

        gateway.fetch_delta = broken_fetch
        service = WebhookService(gateway, state_store, FakeTelegramGateway())

        result = await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(result["msg"], "reseeded")
        self.assertEqual(state_store.seeded_tokens, [("calendar", "Main", "seed-token")])

    async def test_duplicate_delivery_is_skipped(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
            duplicate_delivery=True,
        )
        service = WebhookService(
            FakeCalendarGateway(
                delta=SyncDelta(
                    items=[
                        {
                            "id": "event-1",
                            "updated": "2026-03-11T09:00:00Z",
                            "summary": "Appointment",
                            "status": "confirmed",
                            "start": {"dateTime": "2026-03-11T10:00:00+01:00"},
                            "end": {"dateTime": "2026-03-11T11:00:00+01:00"},
                        }
                    ],
                    next_sync_token="new-token",
                )
            ),
            state_store,
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(result["sent"], 0)

    async def test_missing_event_id_is_skipped(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )
        service = WebhookService(
            FakeCalendarGateway(
                delta=SyncDelta(
                    items=[
                        {
                            "updated": "2026-03-11T09:00:00Z",
                            "summary": "Appointment",
                            "status": "confirmed",
                            "start": {"dateTime": "2026-03-11T10:00:00+01:00"},
                            "end": {"dateTime": "2026-03-11T11:00:00+01:00"},
                        }
                    ],
                    next_sync_token="new-token",
                )
            ),
            state_store,
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(result["sent"], 0)

    async def test_fetch_error_raises_processing_error(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )
        gateway = FakeCalendarGateway(initial_sync_token="seed-token")

        def broken_fetch(calendar_id: str, sync_token: str):
            raise RuntimeError("boom")

        gateway.fetch_delta = broken_fetch
        service = WebhookService(gateway, state_store, FakeTelegramGateway())

        with self.assertRaises(WebhookProcessingError):
            await service.handle_webhook("channel", "token-1", "resource", "exists")

    async def test_send_failure_clears_delivery_marker_and_raises(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )
        service = WebhookService(
            FakeCalendarGateway(
                delta=SyncDelta(
                    items=[
                        {
                            "id": "event-1",
                            "updated": "2026-03-11T09:00:00Z",
                            "summary": "Appointment",
                            "status": "confirmed",
                            "start": {"dateTime": "2026-03-11T10:00:00+01:00"},
                            "end": {"dateTime": "2026-03-11T11:00:00+01:00"},
                        }
                    ],
                    next_sync_token="new-token",
                )
            ),
            state_store,
            FakeTelegramGateway(should_fail=True),
        )

        with self.assertRaises(WebhookProcessingError):
            await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(
            state_store.cleared_deliveries,
            [("calendar", "event-1", "2026-03-11T09:00:00Z")],
        )

    async def test_concurrent_sync_token_update_is_tolerated(self) -> None:
        state_store = FakeStateStore(
            self._mapping(),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )

        async def stale_save(calendar_id, label, sync_token, *, expected_update_time=None):
            return False

        state_store.save_sync_token = stale_save
        service = WebhookService(
            FakeCalendarGateway(
                delta=SyncDelta(
                    items=[],
                    next_sync_token="new-token",
                )
            ),
            state_store,
            FakeTelegramGateway(),
        )

        result = await service.handle_webhook("channel", "token-1", "resource", "exists")

        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
