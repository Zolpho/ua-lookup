"""Automatically discover and exactly verify Samsung firmware metadata."""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Mapping

from mmtel_monitor import classify_contact
from samsung_firmware import (
    FirmwareRecord,
    SamsungIdentity,
    get_firmware_catalog,
    lookup_samsung_firmware,
    parse_samsung_user_agent,
)
from samsung_firmware_admin import (
    _write_json_atomically,
    import_exact_official_url,
    refresh_registered_sources,
    validate_samsung_url,
)


log = logging.getLogger(__name__)

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_STATE_PATH = Path(
    os.environ.get(
        "SAMSUNG_ENRICH_STATE",
        "/var/lib/ua-lookup/samsung-firmware-enrichment-state.json",
    )
)
DEFAULT_LOCK_PATH = Path(
    os.environ.get(
        "SAMSUNG_ENRICH_LOCK",
        "/var/lib/ua-lookup/samsung-firmware-enrichment.lock",
    )
)


def build_search_query(identity: SamsungIdentity) -> str:
    return (
        f'site:doc.samsungmobile.com "{identity.model_code}" '
        f'"{identity.build_number}"'
    )


def search_official_urls(
    identity: SamsungIdentity,
    api_key: str,
    opener: Callable | None = None,
    timeout_seconds: float = 10,
    max_bytes: int = 1_000_000,
) -> list[str]:
    """Return untrusted official-domain candidates for one exact identity."""
    if not str(api_key or "").strip():
        raise ValueError("BRAVE_SEARCH_API_KEY is required")
    parameters = urllib.parse.urlencode(
        {
            "q": build_search_query(identity),
            "count": 5,
            "country": "CH",
            "search_lang": "en",
            "spellcheck": 0,
        }
    )
    request = urllib.request.Request(
        f"{BRAVE_SEARCH_ENDPOINT}?{parameters}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": str(api_key).strip(),
            "User-Agent": "ua-lookup-samsung-enrichment/1.0",
        },
    )
    if opener is None:
        opener = lambda req, timeout: urllib.request.urlopen(req, timeout=timeout)
    with opener(request, timeout_seconds) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("Brave Search response is too large")
    payload = json.loads(body.decode("utf-8"))
    results = payload.get("web", {}).get("results", [])
    if not isinstance(results, list):
        raise ValueError("Brave Search response has invalid results")

    candidates: list[str] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        try:
            candidate = validate_samsung_url(result.get("url", ""))
        except ValueError:
            continue
        parts = [
            part
            for part in urllib.parse.urlsplit(candidate).path.split("/")
            if part
        ]
        if not parts or parts[0].upper() != identity.model_code.upper():
            continue
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def unknown_problem_identities(
    registrations: Iterable[Mapping],
    catalog: Mapping[tuple[str, str], FirmwareRecord],
) -> list[SamsungIdentity]:
    """Select unique unknown Samsung identities from problem registrations."""
    identities: list[SamsungIdentity] = []
    seen: set[tuple[str, str]] = set()
    for registration in registrations:
        if classify_contact(registration.get("contact")) == "MMTEL_PRESENT":
            continue
        identity = parse_samsung_user_agent(registration.get("user_agent"))
        if identity is None:
            continue
        key = (identity.model_code, identity.build_number)
        if key in seen or lookup_samsung_firmware(identity, catalog) is not None:
            continue
        seen.add(key)
        identities.append(identity)
    return identities


def _identity_key(identity: SamsungIdentity) -> str:
    return f"{identity.model_code.upper()}|{identity.build_number.upper()}"


def _load_state(path: Path) -> dict:
    if not Path(path).is_file():
        return {"schema_version": 1, "attempts": {}}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(
            payload.get("attempts"), dict
        ):
            raise ValueError("unsupported enrichment state schema")
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Ignoring invalid Samsung enrichment state %s: %s", path, exc)
        return {"schema_version": 1, "attempts": {}}


def _attempt_time(value: object) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def eligible_identities(
    identities: Iterable[SamsungIdentity],
    state: Mapping,
    now: datetime.datetime,
    not_found_retry_hours: int,
    error_retry_hours: int,
) -> list[SamsungIdentity]:
    attempts = state.get("attempts", {}) if isinstance(state, Mapping) else {}
    eligible: list[SamsungIdentity] = []
    for identity in identities:
        previous = attempts.get(_identity_key(identity), {})
        status = previous.get("status")
        if status == "verified":
            # The caller already confirmed that the exact catalog record is
            # absent. Re-discover immediately if a catalog was removed or
            # restored from an older backup.
            eligible.append(identity)
            continue
        attempted_at = _attempt_time(previous.get("attempted_at"))
        retry_hours = (
            not_found_retry_hours if status == "not_found" else error_retry_hours
        )
        if attempted_at is not None and now - attempted_at < datetime.timedelta(
            hours=retry_hours
        ):
            continue
        eligible.append(identity)
    return eligible


def run_enrichment(
    identities: Iterable[SamsungIdentity],
    api_key: str,
    state_path: Path = DEFAULT_STATE_PATH,
    searcher: Callable[[SamsungIdentity, str], list[str]] = search_official_urls,
    importer: Callable[
        [SamsungIdentity, str], FirmwareRecord
    ] = import_exact_official_url,
    now: datetime.datetime | None = None,
    max_searches: int = 10,
    not_found_retry_hours: int = 24,
    error_retry_hours: int = 6,
) -> dict[str, int]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    state = _load_state(Path(state_path))
    selected = eligible_identities(
        identities,
        state,
        now,
        not_found_retry_hours,
        error_retry_hours,
    )[:max(0, max_searches)]
    summary = {"attempted": 0, "verified": 0, "not_found": 0, "errors": 0}
    attempts = state["attempts"]

    for identity in selected:
        summary["attempted"] += 1
        key = _identity_key(identity)
        status = "not_found"
        try:
            candidates = searcher(identity, api_key)
        except Exception as exc:
            log.warning(
                "Samsung discovery failed for %s %s: %s",
                identity.model_code,
                identity.build_number,
                exc,
            )
            status = "error"
            candidates = []

        for candidate in candidates:
            try:
                importer(identity, candidate)
            except ValueError as exc:
                log.info(
                    "Samsung candidate rejected for %s %s: %s",
                    identity.model_code,
                    identity.build_number,
                    exc,
                )
                continue
            except Exception as exc:
                log.warning(
                    "Samsung candidate failed for %s %s: %s",
                    identity.model_code,
                    identity.build_number,
                    exc,
                )
                status = "error"
                continue
            status = "verified"
            break

        attempts[key] = {
            "attempted_at": now.astimezone(datetime.timezone.utc).isoformat(),
            "status": status,
        }
        summary_key = (
            "verified"
            if status == "verified"
            else "errors"
            if status == "error"
            else "not_found"
        )
        summary[summary_key] += 1

    _write_json_atomically(Path(state_path), state)
    return summary


def load_unknown_problem_identities() -> list[SamsungIdentity]:
    """Load unknown identities through the application's read-only query."""
    from app import _query_latest_successful_registrations

    registrations = _query_latest_successful_registrations()
    return unknown_problem_identities(registrations, get_firmware_catalog())


@contextmanager
def exclusive_lock(path: Path = DEFAULT_LOCK_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        log.error("BRAVE_SEARCH_API_KEY is required")
        return 2
    try:
        with exclusive_lock():
            try:
                refreshed = refresh_registered_sources()
                log.info("Refreshed %d registered Samsung firmware record(s)", refreshed)
            except Exception as exc:
                log.warning("Registered Samsung source refresh failed: %s", exc)
            identities = load_unknown_problem_identities()
            summary = run_enrichment(
                identities,
                api_key,
                max_searches=_positive_int("SAMSUNG_ENRICH_MAX_SEARCHES", 10),
                not_found_retry_hours=_positive_int(
                    "SAMSUNG_ENRICH_NOT_FOUND_RETRY_HOURS", 24
                ),
                error_retry_hours=_positive_int("SAMSUNG_ENRICH_ERROR_RETRY_HOURS", 6),
            )
    except BlockingIOError:
        log.info("Samsung enrichment is already running; skipping overlap")
        return 0
    log.info(
        "Samsung enrichment attempted=%d verified=%d not_found=%d errors=%d",
        summary["attempted"],
        summary["verified"],
        summary["not_found"],
        summary["errors"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
