import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from src.errors import WebhookAuthenticationError, WebhookProcessingError
from src.models import CalendarState, ChannelMapping, EventSnapshot, SyncDelta
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
        self.snapshots: dict[str, EventSnapshot] = {}
        self.snapshot_lookups: list[str] = []
        self.saved_snapshots: list[tuple[str, EventSnapshot, datetime | None]] = []

    async def lookup_channel(self, channel_id: str) -> ChannelMapping | None:
        if self.mapping and self.mapping.channel_id == channel_id:
            return self.mapping
        return None

    async def get_calendar_state(self, calendar_id: str) -> CalendarState | None:
        return self.calendar_state

    async def seed_sync_token(
        self, calendar_id: str, label: str, sync_token: str
    ) -> None:
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

    async def get_event_snapshot(
        self, calendar_id: str, event_id: str
    ) -> EventSnapshot | None:
        self.snapshot_lookups.append(event_id)
        return self.snapshots.get(event_id)

    async def save_event_snapshot(
        self,
        calendar_id: str,
        event_id: str,
        snapshot: EventSnapshot,
        event_end: datetime | None = None,
    ) -> None:
        self.saved_snapshots.append((event_id, snapshot, event_end))


class FakeCalendarGateway:
    def __init__(
        self,
        delta: SyncDelta | None = None,
        initial_sync_token: str | None = None,
        stored_events: dict[str, dict] | None = None,
    ) -> None:
        self.delta = delta
        self.initial_sync_token = initial_sync_token
        self.stored_events = stored_events or {}
        self.get_event_calls: list[str] = []

    def get_event(self, calendar_id: str, event_id: str) -> dict | None:
        self.get_event_calls.append(event_id)
        stored = self.stored_events.get(event_id)
        if isinstance(stored, Exception):
            raise stored
        return stored

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


NOW = datetime(2026, 3, 1, tzinfo=UTC)


class WebhookServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clock = patch("src.services.webhook_service._utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

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

        result = await service.handle_webhook(
            "missing", "token-1", "resource", "exists"
        )

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

        result = await service.handle_webhook(
            "channel", "token-1", "resource", "exists"
        )

        self.assertEqual(result["msg"], "seeded sync token")
        self.assertEqual(
            state_store.seeded_tokens, [("calendar", "Main", "seed-token")]
        )

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

        result = await service.handle_webhook(
            "channel", "token-1", "resource", "exists"
        )

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

        result = await service.handle_webhook(
            "channel", "token-1", "resource", "exists"
        )

        self.assertEqual(result["msg"], "reseeded")
        self.assertEqual(
            state_store.seeded_tokens, [("calendar", "Main", "seed-token")]
        )

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

        result = await service.handle_webhook(
            "channel", "token-1", "resource", "exists"
        )

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

        result = await service.handle_webhook(
            "channel", "token-1", "resource", "exists"
        )

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

        async def stale_save(
            calendar_id, label, sync_token, *, expected_update_time=None
        ):
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

        result = await service.handle_webhook(
            "channel", "token-1", "resource", "exists"
        )

        self.assertEqual(result["status"], "ok")


class WebhookEventChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clock = patch("src.services.webhook_service._utc_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def _service(self, items, state_store=None, gateway=None, telegram=None):
        self.state_store = state_store or FakeStateStore(
            ChannelMapping("channel", "resource", "calendar", "Main", "token-1", 123),
            CalendarState("calendar", "Main", "old-token", "update-1"),
        )
        self.gateway = gateway or FakeCalendarGateway()
        self.gateway.delta = SyncDelta(items=items, next_sync_token="new-token")
        self.telegram = telegram or FakeTelegramGateway()
        return WebhookService(self.gateway, self.state_store, self.telegram)

    async def _handle(self, service):
        return await service.handle_webhook("channel", "token-1", "resource", "exists")

    async def test_past_events_are_skipped(self) -> None:
        service = self._service(
            [
                {
                    "id": "old",
                    "summary": "Done",
                    "start": {"dateTime": "2026-02-01T10:00:00Z"},
                    "end": {"dateTime": "2026-02-01T11:00:00Z"},
                }
            ]
        )

        result = await self._handle(service)

        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.state_store.mark_calls, [])
        self.assertEqual(self.state_store.saved_tokens[0][2], "new-token")

    async def test_recurring_series_change_sends_one_message(self) -> None:
        items = [
            {
                "id": f"series_202603{day:02d}T090000Z",
                "recurringEventId": "series",
                "updated": "2026-02-28T10:00:00Z",
                "summary": "Weekly nails",
                "status": "confirmed",
                "start": {"dateTime": f"2026-03-{day:02d}T09:00:00Z"},
                "end": {"dateTime": f"2026-03-{day:02d}T10:00:00Z"},
            }
            for day in (16, 2, 9)
        ]
        service = self._service(items)

        result = await self._handle(service)

        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn("(3 occurrences)", self.telegram.messages[0])
        self.assertIn("2026-03-02 09:00", self.telegram.messages[0])
        self.assertEqual(self.state_store.mark_calls[0][1], "series")
        snapshot_id, snapshot, event_end = self.state_store.saved_snapshots[0]
        self.assertEqual(snapshot_id, "series")
        self.assertEqual(snapshot.summary, "Weekly nails")
        self.assertEqual(event_end, datetime(2026, 3, 2, 10, 0, tzinfo=UTC))

    async def test_cancelled_event_uses_stored_snapshot(self) -> None:
        service = self._service([{"id": "e1", "status": "cancelled"}])
        self.state_store.snapshots["e1"] = EventSnapshot(
            "Gel nails",
            {"dateTime": "2026-03-05T14:30:00+01:00"},
            {"dateTime": "2026-03-05T15:30:00+01:00"},
        )

        result = await self._handle(service)

        self.assertEqual(result["sent"], 1)
        message = self.telegram.messages[0]
        self.assertIn("❌ <b>Event cancelled</b>", message)
        self.assertIn("Gel nails", message)
        self.assertIn("2026-03-05 14:30 → 2026-03-05 15:30", message)
        self.assertEqual(self.gateway.get_event_calls, [])

    async def test_cancelled_series_uses_series_title_and_instance_time(self) -> None:
        items = [
            {
                "id": f"series_202603{day:02d}T090000Z",
                "status": "cancelled",
                "originalStartTime": {"dateTime": f"2026-03-{day:02d}T10:00:00+01:00"},
            }
            for day in (2, 9)
        ]
        service = self._service(items)
        self.state_store.snapshots["series"] = EventSnapshot(
            "Weekly nails", {"dateTime": "2026-02-23T10:00:00+01:00"}, {}
        )

        result = await self._handle(service)

        self.assertEqual(result["sent"], 1)
        message = self.telegram.messages[0]
        self.assertIn("Recurring event cancelled</b> (2 occurrences)", message)
        self.assertIn("Weekly nails", message)
        self.assertIn("🕑 <b>Next:</b> 2026-03-02 10:00", message)
        # The series snapshot is looked up once and reused for every instance.
        self.assertEqual(self.state_store.snapshot_lookups.count("series"), 1)

    async def test_cancelled_series_without_original_start_keeps_title(self) -> None:
        service = self._service(
            [{"id": "series_20260302T090000Z", "status": "cancelled"}]
        )
        self.state_store.snapshots["series"] = EventSnapshot("Weekly", {}, {})

        result = await self._handle(service)

        self.assertEqual(result["sent"], 1)
        self.assertIn("Weekly", self.telegram.messages[0])

    async def test_cancelled_event_falls_back_to_calendar_lookup(self) -> None:
        gateway = FakeCalendarGateway(
            stored_events={
                "e1": {
                    "id": "e1",
                    "status": "cancelled",
                    "summary": "Hair styling",
                    "start": {"dateTime": "2026-03-05T14:30:00+01:00"},
                    "end": {"dateTime": "2026-03-05T15:30:00+01:00"},
                }
            }
        )
        service = self._service(
            [{"id": "e1", "status": "cancelled", "updated": "v2"}], gateway=gateway
        )

        result = await self._handle(service)

        self.assertEqual(result["sent"], 1)
        self.assertIn("Hair styling", self.telegram.messages[0])
        self.assertEqual(self.state_store.mark_calls[0][2], "v2")

    async def test_cancelled_event_without_any_details_is_skipped(self) -> None:
        for stored in (None, RuntimeError("calendar down")):
            with self.subTest(stored=stored):
                gateway = FakeCalendarGateway(stored_events={"e1": stored})
                service = self._service(
                    [{"id": "e1", "status": "cancelled"}], gateway=gateway
                )

                result = await self._handle(service)

                self.assertEqual(result["sent"], 0)
                self.assertEqual(self.telegram.messages, [])
                self.assertEqual(gateway.get_event_calls, ["e1"])

    async def test_snapshot_failure_does_not_fail_delivery(self) -> None:
        service = self._service(
            [
                {
                    "id": "e1",
                    "summary": "Nails",
                    "start": {"dateTime": "2026-03-05T14:30:00Z"},
                    "end": {"dateTime": "2026-03-05T15:30:00Z"},
                }
            ]
        )

        async def broken_save(*args, **kwargs):
            raise RuntimeError("firestore down")

        self.state_store.save_event_snapshot = broken_save

        result = await self._handle(service)

        self.assertEqual(result["sent"], 1)


if __name__ == "__main__":
    unittest.main()
