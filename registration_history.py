"""Pair raw SIP registration messages into successful registration events."""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Mapping
from typing import Any


def _header(payload: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\s*:\s*(.+)$",
        payload,
        re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _timestamp(value: Any) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def pair_successful_registrations(
    rows: Iterable[Mapping[str, Any]],
    subscriber: str,
    max_delay_seconds: float = 10.0,
) -> list[dict[str, Mapping[str, Any]]]:
    """Match every successful REGISTER response with its actual request.

    SIP Call-ID/session IDs can be reused for days. Pairing within a short time
    window prevents a later P-Access-Network-Info value from being attached to
    an older event. If an authentication retry omits P-ANI, prefer the closest
    request in the same exchange that contains it.
    """
    ordered = sorted(rows, key=lambda row: _timestamp(row["timestamp"]))
    pending: dict[str, list[Mapping[str, Any]]] = {}
    events: list[dict[str, Mapping[str, Any]]] = []

    for row in ordered:
        payload = str(row.get("payload") or "")
        session_id = str(row.get("session_id") or "")
        if not session_id:
            continue

        row_time = _timestamp(row["timestamp"])
        if payload.lstrip().startswith("REGISTER "):
            aor = str(row.get("aor") or "")
            if subscriber not in aor and subscriber not in payload:
                continue
            current = pending.setdefault(session_id, [])
            current[:] = [
                request
                for request in current
                if 0
                <= (row_time - _timestamp(request["timestamp"])).total_seconds()
                <= max_delay_seconds
            ]
            current.append(row)
            continue

        if not payload.lstrip().startswith("SIP/2.0 200"):
            continue
        cseq = _header(payload, "CSeq")
        if "REGISTER" not in cseq.upper():
            continue

        candidates = [
            request
            for request in pending.get(session_id, [])
            if 0
            <= (row_time - _timestamp(request["timestamp"])).total_seconds()
            <= max_delay_seconds
        ]
        if not candidates:
            continue

        with_access_info = [
            request
            for request in candidates
            if _header(
                str(request.get("payload") or ""),
                "P-Access-Network-Info",
            )
        ]
        request = max(
            with_access_info or candidates,
            key=lambda candidate: _timestamp(candidate["timestamp"]),
        )
        events.append({"request": request, "response": row})

        # A 200 response completes this exchange. Earlier authentication
        # requests must not leak into a later registration event.
        pending[session_id] = [
            candidate
            for candidate in pending.get(session_id, [])
            if _timestamp(candidate["timestamp"]) > row_time
        ]

    return events
