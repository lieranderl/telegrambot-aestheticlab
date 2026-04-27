import uuid

from ..models import SyncDelta, WatchRegistration


class CalendarGateway:
    def __init__(self, service) -> None:
        self._service = service

    def get_initial_sync_token(self, calendar_id: str) -> str | None:
        page_token = None

        while True:
            response = (
                self._service.events()
                .list(
                    calendarId=calendar_id,
                    singleEvents=True,
                    showDeleted=True,
                    maxResults=2500,
                    pageToken=page_token,
                )
                .execute()
            )

            page_token = response.get("nextPageToken")
            if not page_token:
                return response.get("nextSyncToken")

    def fetch_delta(self, calendar_id: str, sync_token: str) -> SyncDelta:
        items: list[dict] = []
        page_token = None
        last_response: dict | None = None

        while True:
            response = (
                self._service.events()
                .list(
                    calendarId=calendar_id,
                    singleEvents=True,
                    showDeleted=True,
                    syncToken=sync_token,
                    maxResults=2500,
                    pageToken=page_token,
                )
                .execute()
            )
            last_response = response
            items.extend(response.get("items", []))
            page_token = response.get("nextPageToken")

            if not page_token:
                break

        return SyncDelta(
            items=items,
            next_sync_token=(last_response or {}).get("nextSyncToken"),
        )

    def register_watch(
        self,
        calendar_id: str,
        address: str,
        token: str,
    ) -> WatchRegistration:
        body = {
            "id": str(uuid.uuid4()),
            "type": "web_hook",
            "address": address,
            "token": token,
        }
        watch = (
            self._service.events().watch(calendarId=calendar_id, body=body).execute()
        )
        expiration_ms = watch.get("expiration")
        return WatchRegistration(
            channel_id=watch.get("id", ""),
            resource_id=watch.get("resourceId", ""),
            token=token,
            expiration_ms=int(expiration_ms) if expiration_ms else None,
            payload=watch,
        )

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        self._service.channels().stop(
            body={"id": channel_id, "resourceId": resource_id}
        ).execute()
