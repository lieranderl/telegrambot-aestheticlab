import asyncio
import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

from ..errors import StateStoreConflictError, StateStoreUnavailableError
from ..models import CalendarState, ChannelMapping

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _calendar_doc_id(calendar_id: str) -> str:
    return hashlib.sha1(calendar_id.encode("utf-8")).hexdigest()


def _delivery_doc_id(calendar_id: str, event_id: str, event_version: str) -> str:
    raw = f"{calendar_id}|{event_id}|{event_version}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _encode_value(value: object) -> dict[str, object]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, Mapping):
        return {
            "mapValue": {
                "fields": {key: _encode_value(item) for key, item in value.items()}
            }
        }
    if isinstance(value, list):
        return {"arrayValue": {"values": [_encode_value(item) for item in value]}}
    raise TypeError(f"Unsupported Firestore value: {type(value)!r}")


def _decode_value(value: Mapping[str, object]) -> object:
    if "nullValue" in value:
        return None
    if "booleanValue" in value:
        return bool(value["booleanValue"])
    if "integerValue" in value:
        return int(str(value["integerValue"]))
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "stringValue" in value:
        return str(value["stringValue"])
    if "mapValue" in value:
        fields = value.get("mapValue", {}).get("fields", {})
        if isinstance(fields, Mapping):
            return {key: _decode_value(item) for key, item in fields.items()}
    if "arrayValue" in value:
        values = value.get("arrayValue", {}).get("values", [])
        if isinstance(values, list):
            return [_decode_value(item) for item in values if isinstance(item, Mapping)]
    return None


def _document_fields(document: Mapping[str, object]) -> dict[str, object]:
    fields = document.get("fields", {})
    if not isinstance(fields, Mapping):
        return {}
    return {key: _decode_value(value) for key, value in fields.items() if isinstance(value, Mapping)}


class FirestoreStateStore:
    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: Credentials,
        project_id: str,
        collection_prefix: str,
    ) -> None:
        self._client = client
        self._credentials = credentials
        self._project_id = project_id
        self._collection_prefix = collection_prefix.strip().replace("-", "_")
        self._base_url = (
            f"https://firestore.googleapis.com/v1/projects/{project_id}"
            "/databases/(default)/documents"
        )

    def _collection(self, name: str) -> str:
        return f"{self._collection_prefix}_{name}"

    async def _access_token(self) -> str:
        def refresh() -> str:
            if not self._credentials.valid or self._credentials.expired:
                self._credentials.refresh(GoogleAuthRequest())
            token = self._credentials.token
            if not token:
                raise StateStoreUnavailableError("Google credentials did not yield an access token")
            return token

        return await asyncio.to_thread(refresh)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        allow_not_found: bool = False,
    ) -> dict[str, object] | None:
        token = await self._access_token()
        response = await self._client.request(
            method,
            f"{self._base_url}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            json=json_body,
        )

        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code in {409, 412}:
            raise StateStoreConflictError(response.text)
        if response.status_code not in expected_statuses:
            raise StateStoreUnavailableError(
                f"Firestore request failed ({response.status_code}): {response.text}"
            )
        if not response.content:
            return {}
        data = response.json()
        if isinstance(data, dict):
            return data
        return {}

    async def get_calendar_state(self, calendar_id: str) -> CalendarState | None:
        document = await self._request(
            "GET",
            f"{self._collection('calendar_states')}/{_calendar_doc_id(calendar_id)}",
            allow_not_found=True,
        )
        if document is None:
            return None

        fields = _document_fields(document)
        return CalendarState(
            calendar_id=str(fields.get("calendar_id") or calendar_id),
            label=str(fields.get("label") or ""),
            sync_token=fields.get("sync_token") if isinstance(fields.get("sync_token"), str) else None,
            update_time=document.get("updateTime") if isinstance(document.get("updateTime"), str) else None,
        )

    async def save_sync_token(
        self,
        calendar_id: str,
        label: str,
        sync_token: str,
        *,
        expected_update_time: str | None = None,
    ) -> bool:
        params: dict[str, object] = {}
        if expected_update_time:
            params["currentDocument.updateTime"] = expected_update_time
        document = {
            "fields": {
                "calendar_id": _encode_value(calendar_id),
                "label": _encode_value(label),
                "sync_token": _encode_value(sync_token),
                "updated_at": _encode_value(_now_iso()),
            }
        }
        try:
            await self._request(
                "PATCH",
                f"{self._collection('calendar_states')}/{_calendar_doc_id(calendar_id)}",
                params=params or None,
                json_body=document,
                expected_statuses=(200,),
            )
            return True
        except StateStoreConflictError:
            return False

    async def seed_sync_token(self, calendar_id: str, label: str, sync_token: str) -> None:
        await self._request(
            "PATCH",
            f"{self._collection('calendar_states')}/{_calendar_doc_id(calendar_id)}",
            json_body={
                "fields": {
                    "calendar_id": _encode_value(calendar_id),
                    "label": _encode_value(label),
                    "sync_token": _encode_value(sync_token),
                    "updated_at": _encode_value(_now_iso()),
                }
            },
            expected_statuses=(200,),
        )

    async def load_channel_mappings(self) -> list[ChannelMapping]:
        path = self._collection("channels")
        documents: list[ChannelMapping] = []
        page_token: str | None = None

        while True:
            params: dict[str, object] = {"pageSize": 200}
            if page_token:
                params["pageToken"] = page_token
            payload = await self._request(
                "GET",
                path,
                params=params,
                allow_not_found=True,
            )
            if payload is None:
                return []

            for item in payload.get("documents", []):
                if not isinstance(item, Mapping):
                    continue
                fields = _document_fields(item)
                documents.append(
                    ChannelMapping(
                        channel_id=str(fields.get("channel_id") or ""),
                        resource_id=str(fields.get("resource_id") or ""),
                        calendar_id=str(fields.get("calendar_id") or ""),
                        label=str(fields.get("label") or ""),
                        token=str(fields.get("token") or ""),
                        expiration_ms=(
                            int(fields["expiration_ms"])
                            if isinstance(fields.get("expiration_ms"), int)
                            else None
                        ),
                    )
                )

            next_page_token = payload.get("nextPageToken")
            if not isinstance(next_page_token, str) or not next_page_token:
                break
            page_token = next_page_token

        return documents

    async def lookup_channel(self, channel_id: str) -> ChannelMapping | None:
        document = await self._request(
            "GET",
            f"{self._collection('channels')}/{channel_id}",
            allow_not_found=True,
        )
        if document is None:
            return None

        fields = _document_fields(document)
        return ChannelMapping(
            channel_id=str(fields.get("channel_id") or channel_id),
            resource_id=str(fields.get("resource_id") or ""),
            calendar_id=str(fields.get("calendar_id") or ""),
            label=str(fields.get("label") or ""),
            token=str(fields.get("token") or ""),
            expiration_ms=(
                int(fields["expiration_ms"])
                if isinstance(fields.get("expiration_ms"), int)
                else None
            ),
        )

    async def upsert_channel_mapping(self, mapping: ChannelMapping) -> None:
        await self._request(
            "PATCH",
            f"{self._collection('channels')}/{mapping.channel_id}",
            json_body={
                "fields": {
                    "channel_id": _encode_value(mapping.channel_id),
                    "resource_id": _encode_value(mapping.resource_id),
                    "calendar_id": _encode_value(mapping.calendar_id),
                    "label": _encode_value(mapping.label),
                    "token": _encode_value(mapping.token),
                    "expiration_ms": _encode_value(mapping.expiration_ms)
                    if mapping.expiration_ms is not None
                    else _encode_value(None),
                    "updated_at": _encode_value(_now_iso()),
                }
            },
            expected_statuses=(200,),
        )

    async def delete_channel_mapping(self, channel_id: str) -> None:
        await self._request(
            "DELETE",
            f"{self._collection('channels')}/{channel_id}",
            expected_statuses=(200,),
            allow_not_found=True,
        )

    async def mark_delivery_attempt(
        self,
        calendar_id: str,
        event_id: str,
        event_version: str,
    ) -> bool:
        doc_id = _delivery_doc_id(calendar_id, event_id, event_version)
        params = {"currentDocument.exists": "false"}
        try:
            await self._request(
                "PATCH",
                f"{self._collection('deliveries')}/{doc_id}",
                params=params,
                json_body={
                    "fields": {
                        "calendar_id": _encode_value(calendar_id),
                        "event_id": _encode_value(event_id),
                        "event_version": _encode_value(event_version),
                        "created_at": _encode_value(_now_iso()),
                    }
                },
                expected_statuses=(200,),
            )
            return True
        except StateStoreConflictError:
            return False

    async def clear_delivery_attempt(
        self,
        calendar_id: str,
        event_id: str,
        event_version: str,
    ) -> None:
        await self._request(
            "DELETE",
            f"{self._collection('deliveries')}/{_delivery_doc_id(calendar_id, event_id, event_version)}",
            expected_statuses=(200,),
            allow_not_found=True,
        )
