import asyncio
import logging
import secrets
import time

from ..models import CalendarEntry, ChannelMapping
from ..utils.ids import safe_suffix_from_cal_id

logger = logging.getLogger(__name__)


class RegistrationService:
    def __init__(
        self,
        calendar_gateway,
        secret_store,
        calendars: list[CalendarEntry],
        webhook_url: str,
    ) -> None:
        self._calendar_gateway = calendar_gateway
        self._secret_store = secret_store
        self._calendars = calendars
        self._webhook_url = webhook_url

    async def register_all(self) -> dict[str, object]:
        results: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        existing = await self._secret_store.load_channel_mappings()

        for calendar in self._calendars:
            try:
                token = secrets.token_urlsafe(32)
                watch = await asyncio.to_thread(
                    self._calendar_gateway.register_watch,
                    calendar.calendar_id,
                    self._webhook_url,
                    token,
                )
                await self._secret_store.upsert_channel_mapping(
                    ChannelMapping(
                        channel_id=watch.channel_id,
                        resource_id=watch.resource_id,
                        calendar_id=calendar.calendar_id,
                        label=calendar.label,
                        token=watch.token,
                        expiration_ms=watch.expiration_ms,
                    )
                )
                for stale_mapping in existing:
                    if (
                        stale_mapping.calendar_id == calendar.calendar_id
                        and stale_mapping.channel_id != watch.channel_id
                    ):
                        try:
                            await asyncio.to_thread(
                                self._calendar_gateway.stop_channel,
                                stale_mapping.channel_id,
                                stale_mapping.resource_id,
                            )
                        finally:
                            await self._secret_store.delete_channel_mapping(
                                stale_mapping.channel_id
                            )
                results.append({"label": calendar.label, "watch": watch.payload})
                logger.info(
                    "event=calendar_watch_registered calendar_hash=%s label=%s channel_id=%s expiration_ms=%s",
                    safe_suffix_from_cal_id(calendar.calendar_id),
                    calendar.label,
                    watch.channel_id,
                    watch.expiration_ms,
                )
            except Exception as exc:
                message = (
                    f"Calendar not found or not shared: {calendar.label} "
                    f"({calendar.calendar_id}). {exc}"
                )
                logger.error(
                    "event=calendar_watch_registration_failed calendar_hash=%s label=%s error=%s",
                    safe_suffix_from_cal_id(calendar.calendar_id),
                    calendar.label,
                    exc,
                )
                errors.append({"label": calendar.label, "error": message})

        return {"channels": results, "errors": errors or None}

    async def cleanup_all(self) -> dict[str, object]:
        mappings = await self._secret_store.load_channel_mappings()
        if not mappings:
            return {"status": "ok", "msg": "no channels to clean"}

        errors: list[str] = []
        for mapping in mappings:
            try:
                await asyncio.to_thread(
                    self._calendar_gateway.stop_channel,
                    mapping.channel_id,
                    mapping.resource_id,
                )
                logger.info(
                    "event=calendar_channel_stopped calendar_hash=%s label=%s channel_id=%s",
                    safe_suffix_from_cal_id(mapping.calendar_id),
                    mapping.label,
                    mapping.channel_id,
                )
            except Exception as exc:
                message = (
                    f"Failed to stop {mapping.channel_id} ({mapping.label}): {exc}"
                )
                logger.error(
                    "event=calendar_channel_stop_failed calendar_hash=%s label=%s channel_id=%s error=%s",
                    safe_suffix_from_cal_id(mapping.calendar_id),
                    mapping.label,
                    mapping.channel_id,
                    exc,
                )
                errors.append(message)

            await self._secret_store.delete_channel_mapping(mapping.channel_id)

        return {"status": "ok", "errors": errors or None}

    async def renew_expiring_channels(
        self,
        within_minutes: int,
    ) -> dict[str, object]:
        threshold_ms = int(time.time() * 1000) + (within_minutes * 60 * 1000)
        expiring_soon_threshold_ms = int(time.time() * 1000) + (24 * 60 * 60 * 1000)
        mappings = await self._secret_store.load_channel_mappings()
        results: list[dict[str, object]] = []
        errors: list[str] = []

        for mapping in mappings:
            calendar_hash = safe_suffix_from_cal_id(mapping.calendar_id)
            if (
                mapping.expiration_ms is None
                or mapping.expiration_ms <= expiring_soon_threshold_ms
            ):
                logger.warning(
                    "event=calendar_channel_expiring calendar_hash=%s label=%s channel_id=%s expiration_ms=%s",
                    calendar_hash,
                    mapping.label,
                    mapping.channel_id,
                    mapping.expiration_ms,
                )

            if (
                mapping.expiration_ms is not None
                and mapping.expiration_ms > threshold_ms
            ):
                continue

            try:
                token = secrets.token_urlsafe(32)
                watch = await asyncio.to_thread(
                    self._calendar_gateway.register_watch,
                    mapping.calendar_id,
                    self._webhook_url,
                    token,
                )
                await self._secret_store.upsert_channel_mapping(
                    ChannelMapping(
                        channel_id=watch.channel_id,
                        resource_id=watch.resource_id,
                        calendar_id=mapping.calendar_id,
                        label=mapping.label,
                        token=watch.token,
                        expiration_ms=watch.expiration_ms,
                    )
                )
                await asyncio.to_thread(
                    self._calendar_gateway.stop_channel,
                    mapping.channel_id,
                    mapping.resource_id,
                )
                await self._secret_store.delete_channel_mapping(mapping.channel_id)
                results.append(
                    {
                        "calendar_id": mapping.calendar_id,
                        "previous_channel_id": mapping.channel_id,
                        "new_channel_id": watch.channel_id,
                        "new_expiration_ms": watch.expiration_ms,
                    }
                )
                logger.info(
                    "event=calendar_channel_renewed calendar_hash=%s label=%s previous_channel_id=%s new_channel_id=%s new_expiration_ms=%s",
                    calendar_hash,
                    mapping.label,
                    mapping.channel_id,
                    watch.channel_id,
                    watch.expiration_ms,
                )
            except Exception as exc:
                message = (
                    f"Failed to renew channel {mapping.channel_id} "
                    f"({mapping.label}): {exc}"
                )
                logger.error(
                    "event=calendar_channel_renewal_failure calendar_hash=%s label=%s channel_id=%s expiration_ms=%s error=%s",
                    calendar_hash,
                    mapping.label,
                    mapping.channel_id,
                    mapping.expiration_ms,
                    exc,
                )
                errors.append(message)

        return {"status": "ok", "renewed": results, "errors": errors or None}
