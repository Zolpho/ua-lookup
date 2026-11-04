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
