"""Automatically discover and exactly verify Samsung firmware metadata."""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
import re
import urllib.error
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

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
    fetch_samsung_page,
    import_exact_official_url,
    refresh_registered_sources,
    validate_samsung_url,
)


log = logging.getLogger(__name__)

DEFAULT_CSC_CANDIDATES = "EUX,AUT,SWC"
MAX_CSC_CANDIDATES = 16
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


def parse_csc_candidates(
    value: str = DEFAULT_CSC_CANDIDATES,
) -> tuple[str, ...]:
    """Validate and normalize the bounded Samsung CSC probe list."""
    candidates: list[str] = []
    seen: set[str] = set()
    for raw_candidate in str(value or "").split(","):
        candidate = raw_candidate.strip().upper()
        if not candidate:
            continue
        if not re.fullmatch(r"[A-Z0-9]{3}", candidate):
            raise ValueError(f"invalid Samsung CSC candidate: {raw_candidate!r}")
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    if not candidates:
        raise ValueError("at least one Samsung CSC candidate is required")
    if len(candidates) > MAX_CSC_CANDIDATES:
        raise ValueError(
            f"at most {MAX_CSC_CANDIDATES} Samsung CSC candidates are allowed"
        )
    return tuple(candidates)


def build_official_urls(
    identity: SamsungIdentity,
    csc_candidates: Iterable[str],
) -> list[str]:
    """Build validated official Samsung document URLs without web search."""
    normalized_cscs = parse_csc_candidates(
        ",".join(str(candidate) for candidate in csc_candidates)
    )
    return [
        validate_samsung_url(
            "https://doc.samsungmobile.com/"
            f"{identity.model_code.upper()}/{csc}/doc.html"
        )
        for csc in normalized_cscs
    ]


class _SamsungDocumentShellParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.english_documents: list[str] = []
        self.default_documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        values = dict(attrs)
        candidate = str(values.get("value") or "").strip()
        if tag.lower() == "option" and candidate.lower().endswith("/eng.html"):
            self.english_documents.append(candidate)
        if (
            tag.lower() == "input"
            and str(values.get("id") or "").lower() == "dflt_page"
            and candidate
        ):
            self.default_documents.append(candidate)


def resolve_official_document_url(
    identity: SamsungIdentity,
    shell_url: str,
    fetcher: Callable[[str], tuple[str, str]] = fetch_samsung_page,
) -> str:
    """Resolve Samsung's selector shell to its exact English document."""
    normalized_shell = validate_samsung_url(shell_url)
    html, final_shell_url = fetcher(normalized_shell)
    final_shell_url = validate_samsung_url(final_shell_url)

    parser = _SamsungDocumentShellParser()
    parser.feed(html)
    candidates = [*parser.english_documents, *parser.default_documents]
    expected_model = identity.model_code.upper()

    for raw_candidate in candidates:
        document_url = validate_samsung_url(urljoin(final_shell_url, raw_candidate))
        parsed = urlsplit(document_url)
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            len(path_parts) == 3
            and path_parts[0].upper() == expected_model
            and re.fullmatch(r"[0-9]{12}", path_parts[1])
            and path_parts[2].lower() == "eng.html"
            and not parsed.query
        ):
            return document_url

    if candidates:
        raise ValueError("Samsung English document does not match requested model")
    raise ValueError("Samsung shell contains no official English document")


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
    state_path: Path = DEFAULT_STATE_PATH,
    csc_candidates: tuple[str, ...] = parse_csc_candidates(),
    candidate_builder: Callable[
        [SamsungIdentity, Iterable[str]], list[str]
    ] = build_official_urls,
    resolver: Callable[
        [SamsungIdentity, str], str
    ] = resolve_official_document_url,
    importer: Callable[
        [SamsungIdentity, str], FirmwareRecord
    ] = import_exact_official_url,
    now: datetime.datetime | None = None,
    max_identities: int = 5,
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
    )[:max(0, max_identities)]
    summary = {"attempted": 0, "verified": 0, "not_found": 0, "errors": 0}
    attempts = state["attempts"]

    for identity in selected:
        summary["attempted"] += 1
        key = _identity_key(identity)
        status = "not_found"
        try:
            candidates = candidate_builder(identity, csc_candidates)
        except Exception as exc:
            log.warning(
                "Samsung candidate generation failed for %s %s: %s",
                identity.model_code,
                identity.build_number,
                exc,
            )
            status = "error"
            candidates = []

        for candidate in candidates:
            try:
                document_url = resolver(identity, candidate)
                importer(identity, document_url)
            except urllib.error.HTTPError as exc:
                if exc.code not in {404, 410}:
                    log.warning(
                        "Samsung candidate failed for %s %s: %s",
                        identity.model_code,
                        identity.build_number,
                        exc,
                    )
                    status = "error"
                else:
                    log.info(
                        "Samsung CSC page absent for %s %s: %s",
                        identity.model_code,
                        identity.build_number,
                        candidate,
                    )
                continue
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
    try:
        csc_candidates = parse_csc_candidates(
            os.environ.get("SAMSUNG_CSC_CANDIDATES", DEFAULT_CSC_CANDIDATES)
        )
        max_identities = _positive_int("SAMSUNG_ENRICH_MAX_IDENTITIES", 5)
        not_found_retry_hours = _positive_int(
            "SAMSUNG_ENRICH_NOT_FOUND_RETRY_HOURS", 24
        )
        error_retry_hours = _positive_int("SAMSUNG_ENRICH_ERROR_RETRY_HOURS", 6)
    except ValueError as exc:
        log.error("Invalid Samsung enrichment configuration: %s", exc)
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
                csc_candidates=csc_candidates,
                max_identities=max_identities,
                not_found_retry_hours=not_found_retry_hours,
                error_retry_hours=error_retry_hours,
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
