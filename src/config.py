import logging
import os
from dataclasses import dataclass

from .models import CalendarEntry

logger = logging.getLogger(__name__)


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_calendar_entries(raw_calendars: str) -> list[CalendarEntry]:
    entries: list[CalendarEntry] = []

    for pair in raw_calendars.split(";"):
        pair = pair.strip()
        if not pair:
            continue

        try:
            calendar_id, label = pair.split("|", 1)
        except ValueError:
            logger.warning("Invalid CALENDAR_IDS entry (ignored): %s", pair)
            continue

        calendar_id = calendar_id.strip()
        label = label.strip()
        if not calendar_id or not label:
            logger.warning("Incomplete CALENDAR_IDS entry (ignored): %s", pair)
            continue

        entries.append(CalendarEntry(calendar_id=calendar_id, label=label))

    return entries


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    webhook_url: str
    raw_calendars: str
    state_collection_prefix: str = "calendar_telegram"
    renewal_lead_minutes: int = 120
    google_cloud_project: str | None = None
    gcp_project: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        values = {
            "telegram_token": os.getenv("TELEGRAM_TOKEN"),
            "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID"),
            "webhook_url": os.getenv("WEBHOOK_URL"),
            "raw_calendars": os.getenv("CALENDAR_IDS", ""),
            "state_collection_prefix": os.getenv(
                "STATE_COLLECTION_PREFIX", "calendar_telegram"
            ),
            "renewal_lead_minutes": os.getenv("RENEWAL_LEAD_MINUTES", "120"),
            "google_cloud_project": os.getenv("GOOGLE_CLOUD_PROJECT"),
            "gcp_project": os.getenv("GCP_PROJECT"),
        }
        missing = [
            env_name
            for env_name, value in (
                ("TELEGRAM_TOKEN", values["telegram_token"]),
                ("TELEGRAM_CHAT_ID", values["telegram_chat_id"]),
                ("WEBHOOK_URL", values["webhook_url"]),
                ("CALENDAR_IDS", values["raw_calendars"]),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required env: {', '.join(missing)}")

        return cls(
            telegram_token=values["telegram_token"] or "",
            telegram_chat_id=values["telegram_chat_id"] or "",
            webhook_url=values["webhook_url"] or "",
            raw_calendars=values["raw_calendars"],
            state_collection_prefix=values["state_collection_prefix"]
            or "calendar_telegram",
            renewal_lead_minutes=int(values["renewal_lead_minutes"] or "120"),
            google_cloud_project=values["google_cloud_project"],
            gcp_project=values["gcp_project"],
        )

    @property
    def calendars(self) -> list[CalendarEntry]:
        return parse_calendar_entries(self.raw_calendars)

    @property
    def calendar_labels(self) -> dict[str, str]:
        return {entry.calendar_id: entry.label for entry in self.calendars}

    @property
    def project_id(self) -> str | None:
        return self.google_cloud_project or self.gcp_project
