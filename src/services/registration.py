import logging

from ..models import CalendarEntry

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

    def register_all(self) -> dict[str, object]:
        results: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []

        for calendar in self._calendars:
            try:
                watch = self._calendar_gateway.register_watch(
                    calendar.calendar_id, self._webhook_url
                )
                self._secret_store.upsert_channel_mapping(
                    watch.channel_id,
                    watch.resource_id,
                    calendar.calendar_id,
                    calendar.label,
                )
                results.append({"label": calendar.label, "watch": watch.payload})
                logger.info("Registered watch for %s", calendar.label)
            except Exception as exc:
                message = (
                    f"Calendar not found or not shared: {calendar.label} "
                    f"({calendar.calendar_id}). {exc}"
                )
                logger.error(message)
                errors.append({"label": calendar.label, "error": message})

        if len(errors) == len(self._calendars) and any(
            "DESTROYED" in entry.get("error", "") for entry in errors
        ):
            logger.info(
                "All registrations failed due to secret issues, attempting to reset secret..."
            )
            self._secret_store.reset_channel_mapping_secret()

        return {"channels": results, "errors": errors or None}

    def cleanup_all(self) -> dict[str, object]:
        mappings = self._secret_store.load_channel_mappings()
        if not mappings:
            return {"status": "ok", "msg": "no channels to clean"}

        errors: list[str] = []
        for mapping in mappings:
            try:
                self._calendar_gateway.stop_channel(
                    mapping.channel_id, mapping.resource_id
                )
                logger.info(
                    "Stopped channel %s (%s)", mapping.channel_id, mapping.label
                )
            except Exception as exc:
                message = (
                    f"Failed to stop {mapping.channel_id} ({mapping.label}): {exc}"
                )
                logger.error(message)
                errors.append(message)

        self._secret_store.reset_channel_mapping_secret()
        return {"status": "ok", "errors": errors or None}

    def reset_secret(self) -> dict[str, object]:
        try:
            self._secret_store.reset_channel_mapping_secret()
            return {"status": "ok", "msg": "Channel mapping secret has been reset"}
        except Exception as exc:
            logger.error("Failed to reset secret: %s", exc)
            return {"status": "error", "error": str(exc)}
