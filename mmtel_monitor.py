"""Pure helpers for the missing-MMTEL registration monitor."""

from __future__ import annotations

import datetime
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from time_display import format_zurich_datetime
from ua_mappings import get_android_version
from samsung_firmware import (
    get_firmware_catalog,
    lookup_samsung_firmware,
    parse_samsung_user_agent,
    samsung_research_url,
)


MMTEL_SERVICE = "3gpp-service.ims.icsi.mmtel"


def classify_contact(contact: str | None) -> str:
    """Classify only the device-advertised REGISTER Contact header."""
    if not contact or not str(contact).strip():
        return "CONTACT_MISSING"
    decoded = urllib.parse.unquote(str(contact)).lower()
    if MMTEL_SERVICE in decoded:
        return "MMTEL_PRESENT"
    return "MMTEL_MISSING"


def registration_expires(
    expires: Any,
    contact: str | None,
    payload: str | None,
) -> int:
    """Return the effective REGISTER expiry in seconds."""
    if expires not in (None, ""):
        try:
            return int(expires)
        except (TypeError, ValueError):
            pass

    header = re.search(
        r"(?im)^Expires\s*:\s*([0-9]+)\s*$",
        str(payload or ""),
    )
    if header:
        return int(header.group(1))

    parameter = re.search(
        r"(?i)(?:^|;)\s*expires\s*=\s*([0-9]+)",
        str(contact or ""),
    )
    if parameter:
        return int(parameter.group(1))

    return 1


def _split_device_and_android(value: str) -> tuple[str, str]:
    if " — " in value:
        model, android = value.rsplit(" — ", 1)
        return model, android
    if value.startswith("Android ") or value.startswith("iOS "):
        return "", value
    return value, ""


def parse_device_metadata(user_agent: str | None, samsung_catalog=None) -> dict[str, str]:
    """Extract build details and add exact catalog metadata when known."""
    ua = (user_agent or "").strip()
    samsung_identity = parse_samsung_user_agent(ua)
    firmware = None
    apple = re.search(r"\biOS/(\d+(?:\.\d+)*)\s+iPhone\b", ua, re.IGNORECASE)
    if apple:
        model = "Apple iPhone"
        os_version = f"iOS {apple.group(1)}"
    elif samsung_identity:
        catalog = (
            get_firmware_catalog()
            if samsung_catalog is None
            else samsung_catalog
        )
        firmware = lookup_samsung_firmware(samsung_identity, catalog)
        model = firmware.device_name if firmware else samsung_identity.device_name
        os_version = firmware.android_version if firmware else "Unknown — not verified"
    else:
        inferred = get_android_version(ua)
        model, os_version = _split_device_and_android(inferred)
    build_number = ""
    security = ""
    release_date = ""
    patch_level = ""
    metadata_status = ""
    source_url = ""
    research_url = ""

    if samsung_identity:
        model_code = samsung_identity.model_code.removeprefix("SM-")
        build_number = samsung_identity.build_number
        if firmware:
            release_date = firmware.release_date
            patch_level = firmware.security_patch_level
            metadata_status = "verified"
            source_url = firmware.source_url
        else:
            metadata_status = "unverified"
            research_url = samsung_research_url(samsung_identity)
        build_kind = build_number[len(model_code) + 2:len(model_code) + 3]
        if build_kind == "S":
            security = "Security-only update"
        elif build_kind == "U":
            security = "Feature / maintenance update"
    else:
        pixel = re.search(r"Android[_ ]\d+_([A-Z0-9.]+)", ua, re.IGNORECASE)
        if pixel:
            build_number = pixel.group(1)
        else:
            hyperos = re.search(r"_(OS\d+(?:\.\d+){3}\.[A-Z]{7})", ua)
            if hyperos:
                build_number = hyperos.group(1)
            else:
                fairphone = re.search(r"_(FP\d+\.QREL\.\d+\.[A-Z0-9.]+)", ua)
                if fairphone:
                    build_number = fairphone.group(1)

    return {
        "model": model,
        "build_number": build_number,
        "os_version": os_version,
        "security": security,
        "fw_release_date": release_date,
        "security_patch_level": patch_level,
        "metadata_status": metadata_status,
        "source_url": source_url,
        "research_url": research_url,
    }


def _timestamp(value: Any) -> datetime.datetime:
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def build_monitor_report(
    registrations: Iterable[Mapping[str, Any]],
    msisdn_by_imsi: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Keep the latest row per IMSI and split confirmed/unknown failures."""
    latest: dict[str, Mapping[str, Any]] = {}
    for registration in registrations:
        if registration_expires(
            registration.get("expires"),
            registration.get("contact"),
            registration.get("payload"),
        ) <= 0:
            continue
        imsi = str(registration.get("imsi") or "").split("@", 1)[0].strip()
        if not imsi:
            continue
        previous = latest.get(imsi)
        if previous is None or _timestamp(registration["timestamp"]) > _timestamp(
            previous["timestamp"]
        ):
            latest[imsi] = registration

    report = {"missing_mmtel": [], "unable_to_verify": []}
    for imsi, registration in latest.items():
        status = classify_contact(registration.get("contact"))
        if status == "MMTEL_PRESENT":
            continue
        metadata = parse_device_metadata(registration.get("user_agent"))
        row = {
            "timestamp": _timestamp(registration["timestamp"]),
            "timestamp_zurich": format_zurich_datetime(
                _timestamp(registration["timestamp"])
            ),
            "msisdn": str(msisdn_by_imsi.get(imsi) or ""),
            "imsi": imsi,
            "user_agent": str(registration.get("user_agent") or ""),
            "contact": str(registration.get("contact") or ""),
            "status": status,
            **metadata,
        }
        bucket = (
            "missing_mmtel" if status == "MMTEL_MISSING" else "unable_to_verify"
        )
        report[bucket].append(row)

    for rows in report.values():
        rows.sort(key=lambda row: row["timestamp"], reverse=True)
    return report


def parse_ucn_subscriber_lines(output: str) -> dict[str, str]:
    """Parse read-only UCN JSON-lines output into an IMSI/MSISDN map."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        imsi = str(row.get("imsi") or "").split("@", 1)[0].strip()
        msisdn = str(row.get("msisdn") or "").lstrip("+").strip()
        if imsi and msisdn:
            result.setdefault(imsi, msisdn)
    return result


class MonitorCache:
    """Small thread-safe cache used to protect DuckLake and the UCN nodes."""

    def __init__(
        self,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._expires_at = 0.0
        self._value: Any = None
        self._lock = threading.Lock()

    def get(self, loader: Callable[[], Any], force: bool = False) -> Any:
        now = self.clock()
        if not force and self._value is not None and now < self._expires_at:
            return self._value
        with self._lock:
            now = self.clock()
            if not force and self._value is not None and now < self._expires_at:
                return self._value
            value = loader()
            self._value = value
            self._expires_at = now + self.ttl_seconds
            return value
