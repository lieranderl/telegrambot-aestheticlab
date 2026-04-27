import asyncio
import logging
import secrets

from ..errors import (
    StateStoreUnavailableError,
    WebhookAuthenticationError,
    WebhookProcessingError,
)
from ..utils.ids import safe_suffix_from_cal_id
from .formatting import format_event_message

logger = logging.getLogger(__name__)


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

        try:
            calendar_state = await self._secret_store.get_calendar_state(
                mapping.calendar_id
            )
        except StateStoreUnavailableError:
            raise

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

        sent = 0
        skipped = 0
        for event in delta.items:
            message = format_event_message(event, mapping.label) or ""
            if not message:
                skipped += 1
                continue

            event_id = str(event.get("id") or "")
            if not event_id:
                skipped += 1
                logger.warning(
                    "event=calendar_event_without_id calendar_hash=%s label=%s",
                    calendar_hash,
                    mapping.label,
                )
                continue
            event_version = self._event_version(event)
            first_attempt = await self._secret_store.mark_delivery_attempt(
                mapping.calendar_id,
                event_id,
                event_version,
            )
            if not first_attempt:
                skipped += 1
                logger.info(
                    "event=telegram_delivery_duplicate calendar_hash=%s event_id=%s",
                    calendar_hash,
                    event_id,
                )
                continue

            try:
                await self._telegram_gateway.send_message(message)
                sent += 1
            except Exception as exc:
                await self._secret_store.clear_delivery_attempt(
                    mapping.calendar_id,
                    event_id,
                    event_version,
                )
                logger.error(
                    "event=telegram_delivery_failed calendar_hash=%s event_id=%s error=%s",
                    calendar_hash,
                    event_id,
                    exc,
                )
                raise WebhookProcessingError(str(exc)) from exc

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
