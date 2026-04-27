import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.gateways.calendar_api import CalendarGateway


class FakeExecutable:
    def __init__(self, response, collector=None, payload=None) -> None:
        self._response = response
        self._collector = collector
        self._payload = payload

    def execute(self):
        if self._collector is not None and self._payload is not None:
            self._collector.append(self._payload)
        return self._response


class FakeEventsAPI:
    def __init__(self, list_responses, watch_response) -> None:
        self.list_responses = list(list_responses)
        self.watch_response = watch_response
        self.watch_calls = []

    def list(self, **kwargs):
        return FakeExecutable(self.list_responses.pop(0))

    def watch(self, **kwargs):
        return FakeExecutable(self.watch_response, self.watch_calls, kwargs)


class FakeChannelsAPI:
    def __init__(self) -> None:
        self.stop_calls = []

    def stop(self, **kwargs):
        return FakeExecutable({}, self.stop_calls, kwargs)


class FakeService:
    def __init__(self, list_responses, watch_response) -> None:
        self._events = FakeEventsAPI(list_responses, watch_response)
        self._channels = FakeChannelsAPI()

    def events(self):
        return self._events

    def channels(self):
        return self._channels


class CalendarGatewayTests(unittest.TestCase):
    def test_get_initial_sync_token_walks_pages(self) -> None:
        service = FakeService(
            [
                {"nextPageToken": "page-2"},
                {"nextSyncToken": "sync-token"},
            ],
            {"id": "channel", "resourceId": "resource"},
        )
        gateway = CalendarGateway(service)

        token = gateway.get_initial_sync_token("calendar")

        self.assertEqual(token, "sync-token")

    def test_fetch_delta_aggregates_items(self) -> None:
        service = FakeService(
            [
                {"items": [{"id": 1}], "nextPageToken": "page-2"},
                {"items": [{"id": 2}], "nextSyncToken": "next-token"},
            ],
            {"id": "channel", "resourceId": "resource"},
        )
        gateway = CalendarGateway(service)

        delta = gateway.fetch_delta("calendar", "sync-token")

        self.assertEqual(delta.items, [{"id": 1}, {"id": 2}])
        self.assertEqual(delta.next_sync_token, "next-token")

    def test_register_watch_returns_typed_result(self) -> None:
        service = FakeService([], {"id": "channel", "resourceId": "resource"})
        gateway = CalendarGateway(service)

        with patch("src.gateways.calendar_api.uuid.uuid4", return_value="uuid-1"):
            registration = gateway.register_watch(
                "calendar",
                "https://example.com",
                "token-1",
            )

        self.assertEqual(registration.channel_id, "channel")
        self.assertEqual(registration.resource_id, "resource")
        self.assertEqual(registration.token, "token-1")
        self.assertEqual(
            service.events().watch_calls[0]["body"],
            {
                "id": "uuid-1",
                "type": "web_hook",
                "address": "https://example.com",
                "token": "token-1",
            },
        )

    def test_stop_channel_forwards_body(self) -> None:
        service = FakeService([], {"id": "channel", "resourceId": "resource"})
        gateway = CalendarGateway(service)

        gateway.stop_channel("channel", "resource")

        self.assertEqual(
            service.channels().stop_calls,
            [{"body": {"id": "channel", "resourceId": "resource"}}],
        )


if __name__ == "__main__":
    unittest.main()
