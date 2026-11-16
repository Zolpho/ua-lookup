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


MMTEL_SERVICE = "3gpp-service.ims.icsi.mmtel"


# Official Swiss Samsung release data for the Galaxy A56 build family observed
# in IMS User-Agent headers. Source:
# https://doc.samsungmobile.com/SM-A566B/033284250424/wel.html
SAMSUNG_FIRMWARE = {
    "A566BXXUEDZJ1": ("Android 17", "2026-10-19", "2026-10-05"),
    "A566BXXSECZI2": ("Android 16", "2026-09-16", "2026-09-05"),
    "A566BXXSDCZHB": ("Android 16", "2026-08-31", "2026-08-05"),
    "A566BXXSCCZG7": ("Android 16", "2026-07-27", "2026-07-05"),
    "A566BXXSBCZF5": ("Android 16", "2026-06-22", "2026-06-05"),
    "A566BXXSACZE2": ("Android 16", "2026-05-21", "2026-05-05"),
    "A566BXXUACZDR": ("Android 16", "2026-05-19", "2026-04-05"),
    "A566BXXSABZD1": ("Android 16", "2026-04-20", "2026-04-05"),
    "A566BXXU9BZC4": ("Android 16", "2026-03-30", "2026-03-05"),
    "A566BXXS9BZB6": ("Android 16", "2026-03-03", "2026-02-05"),
    "A566BXXS8BZA7": ("Android 16", "2026-01-26", "2026-01-01"),
    "A566BXXS7BYL3": ("Android 16", "2025-12-16", "2025-12-01"),
    "A566BXXS7BYK3": ("Android 16", "2025-11-24", "2025-11-01"),
    "A566BXXU6BYIF": ("Android 16", "2025-10-20", "2025-10-01"),
    "A566BXXU6BYI5": ("Android 16", "2025-09-25", "2025-09-01"),
    "A566BXXS6AYGE": ("Android 15", "2025-08-05", "2025-08-01"),
    "A566BXXS5AYFA": ("Android 15", "2025-07-15", "2025-07-01"),
    "A566BXXU4AYE7": ("Android 15", "2025-06-09", "2025-06-01"),
    "A566BXXU3AYDK": ("Android 15", "2025-05-19", "2025-05-01"),
    "A566BXXS2AYD3": ("Android 15", "2025-04-24", "2025-04-01"),
}


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


def parse_device_metadata(user_agent: str | None) -> dict[str, str]:
    """Extract build details and add exact catalog metadata when known."""
    ua = (user_agent or "").strip()
    inferred = get_android_version(ua)
    model, android_version = _split_device_and_android(inferred)
    build_number = ""
    security = ""
    release_date = ""
    patch_level = ""

    samsung = re.match(
        r"SM-([A-Z0-9]+)-([A-Z0-9]+)\s+Samsung\s+IMS",
        ua,
        re.IGNORECASE,
    )
    if samsung:
        model_code = samsung.group(1).upper()
        build_number = samsung.group(2).upper()
        if not model:
            model = f"SM-{model_code}"
        catalog = SAMSUNG_FIRMWARE.get(build_number)
        if catalog:
            android_version, release_date, patch_level = catalog
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
        "android_version": android_version,
        "security": security,
        "fw_release_date": release_date,
        "security_patch_level": patch_level,
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
