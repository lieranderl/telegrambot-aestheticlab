from collections.abc import Mapping
from datetime import date, datetime, timedelta
from html import escape

_MAX_CALENDAR_LABEL_LENGTH = 120
_MAX_SUMMARY_LENGTH = 240
# _MAX_LOCATION_LENGTH = 400
_MAX_DESCRIPTION_LENGTH = 2200


def _format_datetime(value: str) -> str:
    if not value or value == "?":
        return "?"

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _format_date_range(
    start: Mapping[str, str], end: Mapping[str, str]
) -> tuple[str, str]:
    start_date = start.get("date")
    end_date = end.get("date")
    if not start_date or not end_date:
        return "?", "?"

    start_value = date.fromisoformat(start_date).isoformat()
    end_value = (date.fromisoformat(end_date) - timedelta(days=1)).isoformat()
    return start_value, end_value


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _clean_text(value: object, default: str = "—", limit: int | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        text = default
    if limit is not None:
        text = _truncate_text(text, limit)
    return text


def _html(value: object, default: str = "—", limit: int | None = None) -> str:
    return escape(_clean_text(value, default, limit), quote=False)


def _format_description(value: object) -> str:
    description = _clean_text(value, limit=_MAX_DESCRIPTION_LENGTH)
    return escape(description, quote=False)


def format_event_message(event: Mapping[str, object], label: str) -> str | None:
    summary = _html(event.get("summary"), "No title", _MAX_SUMMARY_LENGTH)
    status = str(event.get("status") or "").strip()
    calendar_label = _html(label, limit=_MAX_CALENDAR_LABEL_LENGTH)

    start = event.get("start", {})
    end = event.get("end", {})
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        start = {}
        end = {}

    if start.get("date") and end.get("date"):
        start_value, end_value = _format_date_range(start, end)
    else:
        start_value = _format_datetime(
            str(start.get("dateTime") or start.get("date") or "?")
        )
        end_value = _format_datetime(str(end.get("dateTime") or end.get("date") or "?"))

    description = _format_description(event.get("description"))

    if status == "cancelled":
        return "\n".join(
            [
                f"📂 <b>{calendar_label}</b>",
                "❌ <b>Event cancelled</b>",
                "",
                f"📅 <b>{summary}</b>",
                f"🕑 <b>When:</b> {start_value} → {end_value}",
                "",
                f"📝 <b>Details:</b>\n{description}",
            ]
        )

    return "\n".join(
        [
            f"📂 <b>{calendar_label}</b>",
            "",
            f"📅 <b>{summary}</b>",
            f"🕑 <b>When:</b> {start_value} → {end_value}",
            "",
            f"📝 <b>Details:</b>\n{description}",
        ]
    )
