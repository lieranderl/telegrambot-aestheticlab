import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Instance IDs of recurring events look like "<series id>_20261105T143000Z"
# (or "_20261105" for all-day series).
_INSTANCE_ID_RE = re.compile(r"^(?P<series>.+)_(?P<stamp>\d{8}(?:T\d{6}Z)?)$")


def series_id(event: Mapping[str, object]) -> str | None:
    recurring_event_id = event.get("recurringEventId")
    if isinstance(recurring_event_id, str) and recurring_event_id:
        return recurring_event_id
    match = _INSTANCE_ID_RE.match(str(event.get("id") or ""))
    return match.group("series") if match else None


def has_details(event: Mapping[str, object]) -> bool:
    return bool(event.get("summary") or event.get("start"))


def _parse_moment(value: object) -> datetime | None:
    if not isinstance(value, Mapping):
        return None
    try:
        if value.get("dateTime"):
            parsed = datetime.fromisoformat(str(value["dateTime"]))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if value.get("date"):
            return datetime.combine(
                date.fromisoformat(str(value["date"])), time.min, UTC
            )
    except ValueError:
        return None
    return None


def _instance_moment(event: Mapping[str, object]) -> datetime | None:
    match = _INSTANCE_ID_RE.match(str(event.get("id") or ""))
    if not match:
        return None
    stamp = match.group("stamp")
    try:
        if "T" in stamp:
            return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return datetime.strptime(stamp, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def event_start(event: Mapping[str, object]) -> datetime | None:
    return (
        _parse_moment(event.get("start"))
        or _parse_moment(event.get("originalStartTime"))
        or _instance_moment(event)
    )


def event_end(event: Mapping[str, object]) -> datetime | None:
    # All-day end dates are exclusive, so midnight of that date is the real end.
    return _parse_moment(event.get("end")) or event_start(event)


def _event_zone(event: Mapping[str, object]) -> tzinfo:
    """Timezone in which the event's calendar date is judged."""
    for key in ("start", "end", "originalStartTime"):
        value = event.get(key)
        if not isinstance(value, Mapping):
            continue
        zone_name = value.get("timeZone")
        if isinstance(zone_name, str) and zone_name:
            try:
                return ZoneInfo(zone_name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        if value.get("dateTime"):
            parsed = _parse_moment(value)
            if parsed is not None and parsed.tzinfo is not None:
                return parsed.tzinfo
    return UTC


def last_event_date(event: Mapping[str, object]) -> date | None:
    """Last calendar day the event occupies, in the event's own timezone."""
    end = event.get("end")
    if isinstance(end, Mapping) and end.get("date") and not end.get("dateTime"):
        try:
            # All-day end dates are exclusive.
            return date.fromisoformat(str(end["date"])) - timedelta(days=1)
        except ValueError:
            return None
    moment = event_end(event)
    if moment is None:
        return None
    return moment.astimezone(_event_zone(event)).date()


def is_past(event: Mapping[str, object], now: datetime) -> bool:
    """True unless the event falls on today or a later date.

    Events whose date cannot be determined are treated as past, so Telegram
    only ever hears about changes for today and the future.
    """
    last_day = last_event_date(event)
    if last_day is None:
        return True
    return last_day < now.astimezone(_event_zone(event)).date()


def group_by_series(
    events: list[dict[str, object]],
) -> list[list[dict[str, object]]]:
    """Group instances of the same recurring series, keeping first-seen order.

    Cancelled and changed occurrences are grouped separately so one message
    never describes both actions.
    """
    groups: list[list[dict[str, object]]] = []
    by_series: dict[tuple[str, bool], list[dict[str, object]]] = {}
    for event in events:
        series = series_id(event)
        if series is None:
            groups.append([event])
            continue
        key = (series, event.get("status") == "cancelled")
        if key in by_series:
            by_series[key].append(event)
        else:
            by_series[key] = [event]
            groups.append(by_series[key])
    for group in groups:
        group.sort(
            key=lambda item: event_start(item) or datetime.max.replace(tzinfo=UTC)
        )
    return groups
