"""Exact, locally cached Samsung firmware metadata."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ua_mappings import SAMSUNG_MODELS


log = logging.getLogger(__name__)

DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "data" / "samsung_firmware_catalog.json"
DEFAULT_RUNTIME_PATH = Path(
    os.environ.get(
        "SAMSUNG_FIRMWARE_CACHE",
        "/var/lib/ua-lookup/samsung-firmware-cache.json",
    )
)


@dataclass(frozen=True)
class SamsungIdentity:
    model_code: str
    device_name: str
    build_number: str


@dataclass(frozen=True)
class FirmwareRecord:
    model_code: str
    device_name: str
    build_number: str
    android_version: str
    release_date: str
    security_patch_level: str
    source_url: str
    verified_at: str


def parse_samsung_user_agent(user_agent: str | None) -> SamsungIdentity | None:
    match = re.match(
        r"SM-([A-Z0-9]+)-([A-Z0-9]+)\s+Samsung\s+IMS",
        str(user_agent or "").strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    short_code = match.group(1).upper()
    model_code = f"SM-{short_code}"
    model = SAMSUNG_MODELS.get(short_code)
    device_name = str(model[0]) if model else model_code
    return SamsungIdentity(
        model_code=model_code,
        device_name=device_name,
        build_number=match.group(2).upper(),
    )


def _validate_record_source(record: FirmwareRecord) -> None:
    parsed = urllib.parse.urlsplit(record.source_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != "doc.samsungmobile.com"
        or not path_parts
        or path_parts[0].upper() != record.model_code
    ):
        raise ValueError(
            "Samsung catalog source must be an official page for the record model"
        )


def _read_catalog(path: Path) -> dict[tuple[str, str], FirmwareRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("records"), list
    ):
        raise ValueError(f"unsupported Samsung catalog schema in {path}")
    records: dict[tuple[str, str], FirmwareRecord] = {}
    for raw in payload["records"]:
        record = FirmwareRecord(
            model_code=str(raw["model_code"]).upper(),
            device_name=str(raw["device_name"]),
            build_number=str(raw["build_number"]).upper(),
            android_version=str(raw["android_version"]),
            release_date=str(raw["release_date"]),
            security_patch_level=str(raw["security_patch_level"]),
            source_url=str(raw["source_url"]),
            verified_at=str(raw["verified_at"]),
        )
        _validate_record_source(record)
        records[(record.model_code, record.build_number)] = record
    return records


def load_firmware_catalog(
    seed_path: Path,
    runtime_path: Path | None,
) -> dict[tuple[str, str], FirmwareRecord]:
    records = _read_catalog(Path(seed_path))
    if runtime_path is None or not Path(runtime_path).is_file():
        return records
    try:
        records.update(_read_catalog(Path(runtime_path)))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Ignoring invalid Samsung runtime catalog %s: %s", runtime_path, exc)
    return records


_catalog_lock = threading.Lock()
_catalog_signature: tuple[object, ...] | None = None
_catalog_value: Mapping[tuple[str, str], FirmwareRecord] | None = None


def _path_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


def get_firmware_catalog() -> Mapping[tuple[str, str], FirmwareRecord]:
    global _catalog_signature, _catalog_value
    signature = (
        _path_signature(DEFAULT_SEED_PATH),
        _path_signature(DEFAULT_RUNTIME_PATH),
    )
    with _catalog_lock:
        if _catalog_value is None or signature != _catalog_signature:
            _catalog_value = load_firmware_catalog(
                DEFAULT_SEED_PATH,
                DEFAULT_RUNTIME_PATH,
            )
            _catalog_signature = signature
        return _catalog_value


def lookup_samsung_firmware(
    identity: SamsungIdentity,
    catalog: Mapping[tuple[str, str], FirmwareRecord],
) -> FirmwareRecord | None:
    return catalog.get((identity.model_code.upper(), identity.build_number.upper()))


def samsung_research_url(identity: SamsungIdentity) -> str:
    query = (
        f'site:doc.samsungmobile.com "{identity.model_code}" '
        f'"{identity.build_number}"'
    )
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})
