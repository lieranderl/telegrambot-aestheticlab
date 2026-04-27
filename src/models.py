from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CalendarEntry:
    calendar_id: str
    label: str


@dataclass(frozen=True)
class ChannelMapping:
    channel_id: str
    resource_id: str
    calendar_id: str
    label: str
    token: str
    expiration_ms: int | None = None


@dataclass(frozen=True)
class CalendarState:
    calendar_id: str
    label: str
    sync_token: str | None
    update_time: str | None = None


@dataclass(frozen=True)
class SyncDelta:
    items: list[dict[str, Any]]
    next_sync_token: str | None


@dataclass(frozen=True)
class WatchRegistration:
    channel_id: str
    resource_id: str
    token: str
    expiration_ms: int | None
    payload: dict[str, Any]
