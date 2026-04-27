import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.errors import StateStoreConflictError, StateStoreUnavailableError
from src.gateways.firestore_state_store import (
    FirestoreStateStore,
    _decode_value,
    _document_fields,
    _encode_value,
)
from src.models import ChannelMapping


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.content = b"1" if payload is not None else b""

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, url, headers=None, params=None, json=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        return self.responses.pop(0)


class FakeCredentials:
    def __init__(self):
        self.valid = True
        self.expired = False
        self.token = "token-1"
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.valid = True
        self.expired = False
        self.token = "token-2"


class FirestoreHelpersTests(unittest.TestCase):
    def test_encode_and_decode_round_trip(self):
        payload = {
            "text": "value",
            "count": 2,
            "enabled": True,
            "nested": {"field": "x"},
            "items": ["a", 1],
            "empty": None,
        }

        encoded = {key: _encode_value(value) for key, value in payload.items()}
        decoded = {key: _decode_value(value) for key, value in encoded.items()}

        self.assertEqual(decoded, payload)

    def test_document_fields_decodes_firestore_document(self):
        document = {
            "fields": {
                "channel_id": {"stringValue": "channel"},
                "expiration_ms": {"integerValue": "123"},
            }
        }

        self.assertEqual(
            _document_fields(document),
            {"channel_id": "channel", "expiration_ms": 123},
        )


class FirestoreStateStoreRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_refreshes_expired_credentials(self):
        credentials = FakeCredentials()
        credentials.valid = False
        credentials.expired = True
        client = FakeAsyncClient([FakeResponse(200, {"ok": True})])
        store = FirestoreStateStore(client, credentials, "project", "prefix")

        response = await store._request("GET", "channels/demo")

        self.assertEqual(response, {"ok": True})
        self.assertEqual(credentials.refresh_calls, 1)
        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Bearer token-2")

    async def test_request_raises_conflict_for_precondition_failures(self):
        store = FirestoreStateStore(
            FakeAsyncClient([FakeResponse(409, text="conflict")]),
            FakeCredentials(),
            "project",
            "prefix",
        )

        with self.assertRaises(StateStoreConflictError):
            await store._request("PATCH", "channels/demo")

    async def test_request_raises_unavailable_for_unexpected_status(self):
        store = FirestoreStateStore(
            FakeAsyncClient([FakeResponse(500, text="boom")]),
            FakeCredentials(),
            "project",
            "prefix",
        )

        with self.assertRaises(StateStoreUnavailableError):
            await store._request("GET", "channels/demo")

    async def test_request_returns_none_for_not_found_when_allowed(self):
        store = FirestoreStateStore(
            FakeAsyncClient([FakeResponse(404, text="missing")]),
            FakeCredentials(),
            "project",
            "prefix",
        )

        response = await store._request(
            "GET",
            "channels/demo",
            allow_not_found=True,
        )

        self.assertIsNone(response)


class FirestoreStateStoreBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_calendar_state_returns_none_when_missing(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(return_value=None)

        result = await store.get_calendar_state("calendar@example.com")

        self.assertIsNone(result)

    async def test_get_calendar_state_returns_typed_state(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(
            return_value={
                "fields": {
                    "calendar_id": {"stringValue": "calendar@example.com"},
                    "label": {"stringValue": "Main"},
                    "sync_token": {"stringValue": "sync-1"},
                },
                "updateTime": "2026-04-27T12:00:00Z",
            }
        )

        state = await store.get_calendar_state("calendar@example.com")

        self.assertEqual(state.calendar_id, "calendar@example.com")
        self.assertEqual(state.sync_token, "sync-1")
        self.assertEqual(state.update_time, "2026-04-27T12:00:00Z")

    async def test_save_sync_token_returns_false_on_conflict(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(side_effect=StateStoreConflictError("conflict"))

        updated = await store.save_sync_token(
            "calendar@example.com",
            "Main",
            "sync-2",
            expected_update_time="old",
        )

        self.assertFalse(updated)

    async def test_load_channel_mappings_returns_empty_when_missing(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(return_value=None)

        self.assertEqual(await store.load_channel_mappings(), [])

    async def test_load_channel_mappings_decodes_documents(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(
            side_effect=[
                {
                    "documents": [
                        {
                            "fields": {
                                "channel_id": {"stringValue": "channel-1"},
                                "resource_id": {"stringValue": "resource-1"},
                                "calendar_id": {"stringValue": "calendar@example.com"},
                                "label": {"stringValue": "Main"},
                                "token": {"stringValue": "token-1"},
                                "expiration_ms": {"integerValue": "123"},
                            }
                        }
                    ]
                }
            ]
        )

        mappings = await store.load_channel_mappings()

        self.assertEqual(
            mappings,
            [
                ChannelMapping(
                    channel_id="channel-1",
                    resource_id="resource-1",
                    calendar_id="calendar@example.com",
                    label="Main",
                    token="token-1",
                    expiration_ms=123,
                )
            ],
        )

    async def test_load_channel_mappings_handles_pagination(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(
            side_effect=[
                {
                    "documents": [
                        {
                            "fields": {
                                "channel_id": {"stringValue": "channel-1"},
                                "resource_id": {"stringValue": "resource-1"},
                                "calendar_id": {"stringValue": "calendar-1"},
                                "label": {"stringValue": "One"},
                                "token": {"stringValue": "token-1"},
                            }
                        }
                    ],
                    "nextPageToken": "page-2",
                },
                {
                    "documents": [
                        {
                            "fields": {
                                "channel_id": {"stringValue": "channel-2"},
                                "resource_id": {"stringValue": "resource-2"},
                                "calendar_id": {"stringValue": "calendar-2"},
                                "label": {"stringValue": "Two"},
                                "token": {"stringValue": "token-2"},
                            }
                        },
                        "bad-item",
                    ]
                },
            ]
        )

        mappings = await store.load_channel_mappings()

        self.assertEqual([mapping.channel_id for mapping in mappings], ["channel-1", "channel-2"])

    async def test_lookup_channel_returns_none_when_missing(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(return_value=None)

        self.assertIsNone(await store.lookup_channel("missing"))

    async def test_lookup_channel_decodes_document(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(
            return_value={
                "fields": {
                    "resource_id": {"stringValue": "resource"},
                    "calendar_id": {"stringValue": "calendar"},
                    "label": {"stringValue": "Main"},
                    "token": {"stringValue": "token"},
                }
            }
        )

        mapping = await store.lookup_channel("channel")

        self.assertEqual(mapping.channel_id, "channel")
        self.assertEqual(mapping.token, "token")

    async def test_seed_and_mark_delivery_success_paths(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(return_value={})

        await store.seed_sync_token("calendar", "Main", "sync-1")
        first_attempt = await store.mark_delivery_attempt("calendar", "event", "version")

        self.assertTrue(first_attempt)

    async def test_mark_delivery_attempt_returns_false_on_duplicate(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(side_effect=StateStoreConflictError("exists"))

        first_attempt = await store.mark_delivery_attempt("cal", "event", "version")

        self.assertFalse(first_attempt)

    async def test_upsert_and_delete_delegate_to_firestore(self):
        store = FirestoreStateStore(
            FakeAsyncClient([]),
            FakeCredentials(),
            "project",
            "prefix",
        )
        store._request = AsyncMock(return_value={})

        mapping = ChannelMapping(
            channel_id="channel-1",
            resource_id="resource-1",
            calendar_id="calendar@example.com",
            label="Main",
            token="token-1",
            expiration_ms=123,
        )
        await store.upsert_channel_mapping(mapping)
        await store.delete_channel_mapping("channel-1")
        await store.clear_delivery_attempt("cal", "event", "version")

        self.assertEqual(store._request.await_count, 3)


if __name__ == "__main__":
    unittest.main()
