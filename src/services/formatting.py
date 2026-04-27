from collections.abc import Mapping
from datetime import date, datetime, timedelta


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


def format_event_message(event: Mapping[str, object], label: str) -> str | None:
    summary = str(event.get("summary") or "No title")
    status = str(event.get("status") or "")

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

    location = str(event.get("location") or "—")
    description = str(event.get("description") or "—")

    if status == "cancelled":
        return "\n".join(
            [
                f"❌ Event cancelled: {summary}",
                f"🕑 {start_value} → {end_value}",
                f"📍 {location}",
                "",
                f"📂 {label}",
            ]
        )

    summary_line = f"📅 {summary}"
    if status:
        summary_line += f" ({status.upper()})"

    return "\n".join(
        [
            summary_line,
            f"🕑 {start_value} → {end_value}",
            f"📍 {location}",
            f"📝 {description}",
            "",
            f"📂 {label}",
        ]
    )
