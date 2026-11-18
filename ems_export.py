"""Read-only EMS exports for confirmed missing-MMTEL registrations."""

from __future__ import annotations

import datetime
import csv
import hashlib
import hmac
import ipaddress
import io
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from flask import Blueprint, Response, jsonify, request


_MSISDN_RE = re.compile(r"[0-9]{8,15}")
_IMSI_RE = re.compile(r"[0-9]+")
EMS_EXPORT_FIELDS = (
    "event_id",
    "msisdn",
    "imsi",
    "registered_at",
    "registered_at_zurich",
    "device",
    "build_number",
    "os_version",
    "user_agent",
    "reason",
)


def ems_event_id(imsi: str, registered_at: str) -> str:
    """Return a deterministic opaque ID for one registration event."""
    value = f"{imsi}\0{registered_at}".encode("utf-8")
    return "mmtel_" + hashlib.sha256(value).hexdigest()


def _utc_timestamp(value: Any) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def build_ems_records(report: Mapping[str, Any]) -> list[dict[str, str]]:
    """Normalize actionable monitor rows into the bounded EMS schema."""
    normalized: list[tuple[datetime.datetime, dict[str, str]]] = []
    rows = report.get("missing_mmtel", []) if isinstance(report, Mapping) else []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("status") != "MMTEL_MISSING":
            continue
        msisdn = str(row.get("msisdn") or "").strip()
        imsi = str(row.get("imsi") or "").strip()
        if not _MSISDN_RE.fullmatch(msisdn) or not _IMSI_RE.fullmatch(imsi):
            continue
        try:
            timestamp = _utc_timestamp(row.get("timestamp"))
        except (TypeError, ValueError):
            continue
        registered_at = timestamp.isoformat()
        record = {
            "event_id": ems_event_id(imsi, registered_at),
            "msisdn": msisdn,
            "imsi": imsi,
            "registered_at": registered_at,
            "registered_at_zurich": str(row.get("timestamp_zurich") or ""),
            "device": str(row.get("model") or ""),
            "build_number": str(row.get("build_number") or ""),
            "os_version": str(row.get("os_version") or ""),
            "user_agent": str(row.get("user_agent") or ""),
            "reason": "MMTEL_MISSING",
        }
        normalized.append((timestamp, record))

    normalized.sort(key=lambda item: item[1]["imsi"])
    normalized.sort(key=lambda item: item[0], reverse=True)
    records: list[dict[str, str]] = []
    seen_msisdns: set[str] = set()
    for _timestamp_value, record in normalized:
        if record["msisdn"] in seen_msisdns:
            continue
        seen_msisdns.add(record["msisdn"])
        records.append(record)
    return records


def render_ems_csv(records: Iterable[Mapping[str, str]]) -> bytes:
    """Serialize normalized records as a bounded UTF-8 CSV document."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=EMS_EXPORT_FIELDS,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue().encode("utf-8-sig")


def ems_csv_filename(now: datetime.datetime) -> str:
    """Return a UTC timestamped filename for one operator download."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    utc_now = now.astimezone(datetime.timezone.utc)
    return f"mmtel-missing-ems-{utc_now:%Y%m%dT%H%M%SZ}.csv"


def parse_ems_allowed_ips(
    value: str,
) -> frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Parse an exact-address EMS allowlist without accepting networks."""
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for raw_address in str(value or "").split(","):
        candidate = raw_address.strip()
        if not candidate:
            continue
        try:
            addresses.add(ipaddress.ip_address(candidate))
        except ValueError as exc:
            raise ValueError(
                f"invalid EMS_ALLOWED_IPS address: {candidate!r}"
            ) from exc
    return frozenset(addresses)


def create_ems_blueprint(
    report_loader: Callable[[], Mapping[str, Any]],
    api_token: str,
    allowed_ips: Iterable[str] | str = (),
) -> Blueprint:
    """Create the operator CSV and guarded EMS polling routes."""
    if len(str(api_token)) < 32:
        raise ValueError("EMS_API_TOKEN must contain at least 32 characters")
    if isinstance(allowed_ips, str):
        allowed = parse_ems_allowed_ips(allowed_ips)
    else:
        allowed = parse_ems_allowed_ips(",".join(str(value) for value in allowed_ips))

    blueprint = Blueprint("ems_export", __name__)

    def api_response(payload: Mapping[str, Any], status: int = 200) -> Response:
        response = jsonify(payload)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/mmtel-monitor/export.csv")
    def download_csv() -> Response:
        report = report_loader()
        records = build_ems_records(report)
        payload = render_ems_csv(records)
        filename = ems_csv_filename(datetime.datetime.now(datetime.timezone.utc))
        response = Response(payload, content_type="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{filename}"'
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/api/v1/mmtel-missing")
    def api_missing_mmtel() -> Response:
        authorization = request.headers.get("Authorization", "")
        expected = f"Bearer {api_token}"
        if not hmac.compare_digest(authorization, expected):
            response = api_response({"error": "unauthorized"}, 401)
            response.headers["WWW-Authenticate"] = "Bearer"
            return response

        if allowed:
            try:
                remote_address = ipaddress.ip_address(request.remote_addr or "")
            except ValueError:
                return api_response({"error": "forbidden"}, 403)
            if remote_address not in allowed:
                return api_response({"error": "forbidden"}, 403)

        try:
            report = report_loader()
        except Exception:
            return api_response({"error": "service_unavailable"}, 503)

        records = build_ems_records(report)
        return api_response(
            {
                "schema_version": 1,
                "generated_at": str(report.get("generated_at") or ""),
                "count": len(records),
                "records": records,
            }
        )

    return blueprint
