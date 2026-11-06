"""User-facing timestamp formatting."""

import datetime
from zoneinfo import ZoneInfo


ZURICH_TIMEZONE = ZoneInfo("Europe/Zurich")


def format_zurich_datetime(value: datetime.datetime) -> str:
    """Format an instant in Zurich local time, including CET/CEST."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(ZURICH_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S.%f %Z"
    )


def format_utc_text_as_zurich(value):
    """Convert a SQLite UTC timestamp string to Zurich display time.

    UCN SQLite's ``datetime`` function returns timezone-less text. Those
    values represent UTC, so parse them as UTC before applying the Zurich
    timezone. Empty placeholders and unrecognised values are preserved.
    """
    if value in (None, "", "-"):
        return value

    text = str(value).strip()
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text

    return format_zurich_datetime(parsed)
