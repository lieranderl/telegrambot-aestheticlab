import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .models import CalendarEntry

logger = logging.getLogger(__name__)
_COLLECTION_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]+$")


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
    delivery_ttl_days: int = 30
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
            "delivery_ttl_days": os.getenv("DELIVERY_TTL_DAYS", "30"),
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

        webhook_url = values["webhook_url"] or ""
        parsed_webhook_url = urlparse(webhook_url)
        if parsed_webhook_url.scheme != "https" or not parsed_webhook_url.netloc:
            raise RuntimeError("WEBHOOK_URL must be an absolute https URL")

        calendars = parse_calendar_entries(values["raw_calendars"] or "")
        if not calendars:
            raise RuntimeError("CALENDAR_IDS must contain at least one valid entry")

        state_collection_prefix = (
            (values["state_collection_prefix"] or "calendar_telegram")
            .strip()
            .replace("-", "_")
        )
        if not _COLLECTION_PREFIX_RE.fullmatch(state_collection_prefix):
            raise RuntimeError(
                "STATE_COLLECTION_PREFIX can contain only letters, numbers, and underscores"
            )

        try:
            renewal_lead_minutes = int(values["renewal_lead_minutes"] or "120")
            delivery_ttl_days = int(values["delivery_ttl_days"] or "30")
        except ValueError as exc:
            raise RuntimeError(
                "RENEWAL_LEAD_MINUTES and DELIVERY_TTL_DAYS must be integers"
            ) from exc

        if renewal_lead_minutes <= 0:
            raise RuntimeError("RENEWAL_LEAD_MINUTES must be greater than zero")
        if delivery_ttl_days <= 0:
            raise RuntimeError("DELIVERY_TTL_DAYS must be greater than zero")

        return cls(
            telegram_token=values["telegram_token"] or "",
            telegram_chat_id=values["telegram_chat_id"] or "",
            webhook_url=webhook_url,
            raw_calendars=values["raw_calendars"],
            state_collection_prefix=state_collection_prefix,
            renewal_lead_minutes=renewal_lead_minutes,
            delivery_ttl_days=delivery_ttl_days,
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
