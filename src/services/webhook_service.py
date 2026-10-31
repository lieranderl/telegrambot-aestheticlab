import asyncio
import logging
import secrets
from datetime import UTC, datetime

from ..errors import (
    WebhookAuthenticationError,
    WebhookProcessingError,
)
from ..models import ChannelMapping, EventSnapshot
from ..utils.ids import safe_suffix_from_cal_id
from .event_changes import (
    event_end,
    group_by_series,
    has_details,
    is_past,
    series_id,
)
from .formatting import format_event_message

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WebhookService:
    def __init__(self, calendar_gateway, secret_store, telegram_gateway) -> None:
        self._calendar_gateway = calendar_gateway
        self._secret_store = secret_store
        self._telegram_gateway = telegram_gateway

    @staticmethod
    def _event_version(event: dict[str, object]) -> str:
        updated = str(event.get("updated") or "")
        status = str(event.get("status") or "")
        return updated or status or "unknown"

    async def handle_webhook(
        self,
        channel_id: str,
        channel_token: str,
        resource_id: str,
        resource_state: str,
    ) -> dict[str, object]:
        logger.info(
            "event=webhook_received resource_state=%s channel_id=%s",
            resource_state,
            channel_id,
        )

        mapping = await self._secret_store.lookup_channel(channel_id)
        if not mapping:
            logger.warning(
                "event=webhook_unknown_channel channel_id=%s",
                channel_id,
            )
            return {"status": "ok", "msg": "unknown channel"}

        calendar_hash = safe_suffix_from_cal_id(mapping.calendar_id)
        if not secrets.compare_digest(
            mapping.token, channel_token
        ) or not secrets.compare_digest(mapping.resource_id, resource_id):
            logger.warning(
                "event=webhook_auth_failed channel_id=%s calendar_hash=%s",
                channel_id,
                calendar_hash,
            )
            raise WebhookAuthenticationError(
                "Google webhook headers did not match stored channel metadata"
            )

        if resource_state == "sync":
            logger.info(
                "event=webhook_sync_ignored channel_id=%s calendar_hash=%s label=%s",
                channel_id,
                calendar_hash,
                mapping.label,
            )
            return {"status": "ok", "msg": "sync handshake ignored"}

        calendar_state = await self._secret_store.get_calendar_state(
            mapping.calendar_id
        )

        sync_token = calendar_state.sync_token if calendar_state else None
        if not sync_token:
            logger.info(
                "event=calendar_sync_seed_start calendar_hash=%s label=%s",
                calendar_hash,
                mapping.label,
            )
            new_sync_token = await asyncio.to_thread(
                self._calendar_gateway.get_initial_sync_token,
                mapping.calendar_id,
            )
            if new_sync_token:
                await self._secret_store.seed_sync_token(
                    mapping.calendar_id,
                    mapping.label,
                    new_sync_token,
                )
                logger.info(
                    "event=calendar_sync_seeded calendar_hash=%s label=%s",
                    calendar_hash,
                    mapping.label,
                )
            return {"status": "ok", "msg": "seeded sync token"}

        try:
            delta = await asyncio.to_thread(
                self._calendar_gateway.fetch_delta,
                mapping.calendar_id,
                sync_token,
            )
        except Exception as exc:
            message = str(exc)
            if "410" in message or "syncToken" in message.lower():
                logger.warning(
                    "event=calendar_sync_token_invalid calendar_hash=%s label=%s",
                    calendar_hash,
                    mapping.label,
                )
                new_sync_token = await asyncio.to_thread(
                    self._calendar_gateway.get_initial_sync_token,
                    mapping.calendar_id,
                )
                if new_sync_token:
                    await self._secret_store.seed_sync_token(
                        mapping.calendar_id,
                        mapping.label,
                        new_sync_token,
                    )
                return {"status": "ok", "msg": "reseeded"}

            logger.error(
                "event=calendar_delta_fetch_failed calendar_hash=%s label=%s error=%s",
                calendar_hash,
                mapping.label,
                exc,
            )
            raise WebhookProcessingError(str(exc)) from exc

        now = _utc_now()
        snapshot_cache: dict[str, EventSnapshot | None] = {}
        upcoming: list[dict[str, object]] = []
        sent = 0
        skipped = 0
        for event in delta.items:
            if not event.get("id"):
                skipped += 1
                logger.warning(
                    "event=calendar_event_without_id calendar_hash=%s label=%s",
                    calendar_hash,
                    mapping.label,
                )
                continue
            if event.get("status") == "cancelled" and not has_details(event):
                event = await self._fill_cancelled_event(
                    mapping.calendar_id, event, snapshot_cache
                )
                if not has_details(event):
                    skipped += 1
                    logger.info(
                        "event=calendar_cancelled_event_without_details calendar_hash=%s event_id=%s",
                        calendar_hash,
                        event.get("id"),
                    )
                    continue
            if is_past(event, now):
                skipped += 1
                continue
            upcoming.append(event)

        for group in group_by_series(upcoming):
            if await self._deliver(mapping, calendar_hash, group):
                sent += 1
            else:
                skipped += 1

        if delta.next_sync_token:
            updated = await self._secret_store.save_sync_token(
                mapping.calendar_id,
                mapping.label,
                delta.next_sync_token,
                expected_update_time=(
                    calendar_state.update_time if calendar_state else None
                ),
            )
            if not updated:
                logger.info(
                    "event=calendar_sync_token_concurrent_update calendar_hash=%s label=%s",
                    calendar_hash,
                    mapping.label,
                )

        logger.info(
            "event=webhook_processed status=ok channel_id=%s calendar_hash=%s label=%s sent=%s skipped=%s delta_items=%s",
            channel_id,
            calendar_hash,
            mapping.label,
            sent,
            skipped,
            len(delta.items),
        )
        return {"status": "ok", "sent": sent}

    async def _fill_cancelled_event(
        self,
        calendar_id: str,
        event: dict[str, object],
        snapshot_cache: dict[str, EventSnapshot | None],
    ) -> dict[str, object]:
        """Restore title/time for a cancelled event that arrived as a bare ID."""
        event_id = str(event["id"])
        snapshot = await self._cached_snapshot(calendar_id, event_id, snapshot_cache)
        if snapshot:
            return {
                **event,
                "summary": snapshot.summary,
                "start": snapshot.start,
                "end": snapshot.end,
            }

        series = series_id(event)
        if series:
            series_snapshot = await self._cached_snapshot(
                calendar_id, series, snapshot_cache
            )
            if series_snapshot:
                # The series snapshot holds another occurrence's time; only the
                # title carries over, the time comes from this instance.
                filled: dict[str, object] = {
                    **event,
                    "summary": series_snapshot.summary,
                }
                if event.get("originalStartTime"):
                    filled["start"] = event["originalStartTime"]
                return filled

        try:
            stored = await asyncio.to_thread(
                self._calendar_gateway.get_event, calendar_id, event_id
            )
        except Exception as exc:  # noqa: BLE001 - details are best-effort
            logger.warning(
                "event=calendar_event_lookup_failed calendar_hash=%s event_id=%s error=%s",
                safe_suffix_from_cal_id(calendar_id),
                event_id,
                exc,
            )
            return event
        if not stored:
            return event
        return {**stored, **{key: value for key, value in event.items() if value}}

    async def _cached_snapshot(
        self,
        calendar_id: str,
        event_id: str,
        snapshot_cache: dict[str, EventSnapshot | None],
    ) -> EventSnapshot | None:
        if event_id not in snapshot_cache:
            snapshot_cache[event_id] = await self._secret_store.get_event_snapshot(
                calendar_id, event_id
            )
        return snapshot_cache[event_id]

    async def _deliver(
        self,
        mapping: ChannelMapping,
        calendar_hash: str,
        group: list[dict[str, object]],
    ) -> bool:
        """Send one message for a single event or a whole recurring series.

        Each occurrence keeps its own delivery marker, so overlapping deltas
        from concurrent webhooks never announce the same occurrence twice.
        """
        fresh: list[tuple[dict[str, object], str]] = []
        for item in group:
            item_id = str(item["id"])
            item_version = self._event_version(item)
            if await self._secret_store.mark_delivery_attempt(
                mapping.calendar_id, item_id, item_version
            ):
                fresh.append((item, item_version))
            else:
                logger.info(
                    "event=telegram_delivery_duplicate calendar_hash=%s event_id=%s",
                    calendar_hash,
                    item_id,
                )
        if not fresh:
            return False

        event = fresh[0][0]
        delivery_id = str(event["id"])
        if len(group) > 1:
            delivery_id = series_id(event) or delivery_id
        message = format_event_message(event, mapping.label, occurrences=len(fresh))
        try:
            await self._telegram_gateway.send_message(message)
        except Exception as exc:
            for item, item_version in fresh:
                await self._secret_store.clear_delivery_attempt(
                    mapping.calendar_id,
                    str(item["id"]),
                    item_version,
                )
            logger.error(
                "event=telegram_delivery_failed calendar_hash=%s event_id=%s error=%s",
                calendar_hash,
                delivery_id,
                exc,
            )
            raise WebhookProcessingError(str(exc)) from exc

        start = event.get("start")
        end = event.get("end")
        try:
            await self._secret_store.save_event_snapshot(
                mapping.calendar_id,
                delivery_id,
                EventSnapshot(
                    summary=str(event.get("summary") or ""),
                    start=dict(start) if isinstance(start, dict) else {},
                    end=dict(end) if isinstance(end, dict) else {},
                ),
                event_end(event),
            )
        except Exception as exc:  # noqa: BLE001 - the message is already sent
            logger.warning(
                "event=calendar_event_snapshot_failed calendar_hash=%s event_id=%s error=%s",
                calendar_hash,
                delivery_id,
                exc,
            )
        return True
