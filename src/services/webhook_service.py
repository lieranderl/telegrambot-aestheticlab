import asyncio
import logging

from ..errors import (
    StateStoreUnavailableError,
    WebhookAuthenticationError,
    WebhookProcessingError,
)
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
        logger.info("Webhook: state=%s, channel=%s", resource_state, channel_id)

        mapping = await self._secret_store.lookup_channel(channel_id)
        if not mapping:
            logger.warning(
                "No channel mapping found for %s (register again?)", channel_id
            )
            return {"status": "ok", "msg": "unknown channel"}

        if mapping.token != channel_token or mapping.resource_id != resource_id:
            raise WebhookAuthenticationError(
                "Google webhook headers did not match stored channel metadata"
            )

        if resource_state == "sync":
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
                "No sync token for %s. Seeding without notifications…", mapping.label
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
                logger.warning("Sync token invalid for %s. Re-seeding…", mapping.label)
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

            logger.error("Error fetching deltas for %s: %s", mapping.label, exc)
            raise WebhookProcessingError(str(exc)) from exc

        sent = 0
        for event in delta.items:
            message = format_event_message(event, mapping.label) or ""
            if not message:
                continue

            event_id = str(event.get("id") or "")
            if not event_id:
                logger.warning("Skipping event without id for %s", mapping.label)
                continue
            event_version = self._event_version(event)
            first_attempt = await self._secret_store.mark_delivery_attempt(
                mapping.calendar_id,
                event_id,
                event_version,
            )
            if not first_attempt:
                logger.info("Skipping duplicate delivery for event %s", event_id)
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
                logger.error("Failed to send Telegram message: %s", exc)
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
                    "Sync token for %s changed concurrently; keeping newer stored value",
                    mapping.label,
                )

        return {"status": "ok", "sent": sent}
