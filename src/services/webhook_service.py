import logging

from .formatting import format_event_message

logger = logging.getLogger(__name__)


class WebhookService:
    def __init__(self, calendar_gateway, secret_store, telegram_gateway) -> None:
        self._calendar_gateway = calendar_gateway
        self._secret_store = secret_store
        self._telegram_gateway = telegram_gateway

    async def handle_webhook(
        self, channel_id: str, resource_state: str
    ) -> dict[str, object]:
        logger.info("Webhook: state=%s, channel=%s", resource_state, channel_id)

        mapping = self._secret_store.lookup_channel(channel_id)
        if not mapping:
            logger.warning(
                "No channel mapping found for %s (register again?)", channel_id
            )
            return {"status": "ok", "msg": "unknown channel"}

        if resource_state == "sync":
            return {"status": "ok", "msg": "sync handshake ignored"}

        sync_token = self._secret_store.get_sync_token(mapping.calendar_id)
        if not sync_token:
            logger.info(
                "No sync token for %s. Seeding without notifications…", mapping.label
            )
            new_sync_token = self._calendar_gateway.get_initial_sync_token(
                mapping.calendar_id
            )
            if new_sync_token:
                self._secret_store.save_sync_token(mapping.calendar_id, new_sync_token)
            return {"status": "ok", "msg": "seeded sync token"}

        try:
            delta = self._calendar_gateway.fetch_delta(mapping.calendar_id, sync_token)
        except Exception as exc:
            message = str(exc)
            if "410" in message or "syncToken" in message.lower():
                logger.warning("Sync token invalid for %s. Re-seeding…", mapping.label)
                new_sync_token = self._calendar_gateway.get_initial_sync_token(
                    mapping.calendar_id
                )
                if new_sync_token:
                    self._secret_store.save_sync_token(
                        mapping.calendar_id, new_sync_token
                    )
                return {"status": "ok", "msg": "reseeded"}

            logger.error("Error fetching deltas for %s: %s", mapping.label, exc)
            return {"status": "error", "error": str(exc)}

        sent = 0
        all_sent = True
        for event in delta.items:
            message = format_event_message(event, mapping.label) or ""
            if not message:
                continue

            try:
                await self._telegram_gateway.send_message(message)
                sent += 1
            except Exception as exc:
                all_sent = False
                logger.error("Failed to send Telegram message: %s", exc)

        if delta.next_sync_token and all_sent:
            self._secret_store.save_sync_token(
                mapping.calendar_id, delta.next_sync_token
            )
        elif delta.next_sync_token:
            logger.warning(
                "Skipping sync token update for %s because at least one message failed",
                mapping.label,
            )

        return {"status": "ok", "sent": sent}
