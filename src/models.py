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

    def to_line(self) -> str:
        return f"{self.channel_id}|{self.resource_id}|{self.calendar_id}|{self.label}"

    @classmethod
    def from_line(cls, line: str) -> "ChannelMapping | None":
        parts = line.strip().split("|", 3)
        if len(parts) != 4:
            return None

        return cls(
            channel_id=parts[0],
            resource_id=parts[1],
            calendar_id=parts[2],
            label=parts[3],
        )


@dataclass(frozen=True)
class SyncDelta:
    items: list[dict[str, Any]]
    next_sync_token: str | None


@dataclass(frozen=True)
class WatchRegistration:
    channel_id: str
    resource_id: str
    payload: dict[str, Any]
