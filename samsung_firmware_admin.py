"""Administrative importer for official Samsung firmware records."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import tempfile
import urllib.request
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from samsung_firmware import FirmwareRecord


DEFAULT_CACHE_PATH = Path(
    os.environ.get(
        "SAMSUNG_FIRMWARE_CACHE",
        "/var/lib/ua-lookup/samsung-firmware-cache.json",
    )
)
DEFAULT_SOURCES_PATH = Path(
    os.environ.get(
        "SAMSUNG_FIRMWARE_SOURCES",
        "/var/lib/ua-lookup/samsung-firmware-sources.json",
    )
)


def validate_samsung_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("Samsung source URL must use HTTPS")
    if parsed.netloc.lower() != "doc.samsungmobile.com":
        raise ValueError("Samsung source hostname must be doc.samsungmobile.com")
    if not parsed.path.startswith("/"):
        raise ValueError("Samsung source URL must contain an absolute path")
    return urlunsplit(("https", "doc.samsungmobile.com", parsed.path, parsed.query, ""))


class SamsungRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validated = validate_samsung_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, validated)


def fetch_samsung_page(
    url: str,
    timeout_seconds: float = 10,
    max_bytes: int = 2_000_000,
) -> tuple[str, str]:
    normalized = validate_samsung_url(url)
    opener = urllib.request.build_opener(SamsungRedirectHandler())
    request = urllib.request.Request(
        normalized,
        headers={"User-Agent": "ua-lookup-samsung-metadata/1.0"},
    )
    with opener.open(request, timeout=timeout_seconds) as response:
        final_url = validate_samsung_url(response.geturl())
        if response.headers.get_content_type() != "text/html":
            raise ValueError("Samsung source did not return HTML")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("Samsung source response is too large")
        charset = response.headers.get_content_charset("utf-8")
    return body.decode(charset, errors="strict"), final_url


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)

    def text(self) -> str:
        return " ".join(self.parts)


def parse_samsung_page(
    html: str,
    source_url: str,
    verified_at: str,
) -> list[FirmwareRecord]:
    normalized_url = validate_samsung_url(source_url)
    path_parts = [part for part in urlsplit(normalized_url).path.split("/") if part]
    if not path_parts or not re.fullmatch(r"SM-[A-Z0-9]+", path_parts[0], re.I):
        raise ValueError("Samsung source URL does not identify a model")
    source_model = path_parts[0].upper()

    extractor = _TextExtractor()
    extractor.feed(str(html or ""))
    text = re.sub(r"\s+", " ", extractor.text()).strip()
    heading = re.search(
        r"\b(Galaxy\s+[^()]+?)\s*\((SM-[A-Z0-9]+)\)",
        text,
        re.I,
    )
    if not heading:
        raise ValueError("Samsung page model was not found")
    device_name = heading.group(1).strip()
    page_model = heading.group(2).upper()
    if page_model != source_model:
        raise ValueError("Samsung page model does not match source URL model")

    pattern = re.compile(
        r"Build Number\s*:\s*([A-Z0-9]+)\s+"
        r"Android version\s*:\s*[A-Z]?\s*\(?(Android\s+\d+)\)?\s+"
        r"Release Date\s*:\s*(\d{4}-\d{2}-\d{2})\s+"
        r"Security patch level\s*:\s*(\d{4}-\d{2}-\d{2})",
        re.I,
    )
    records = [
        FirmwareRecord(
            model_code=page_model,
            device_name=device_name,
            build_number=match.group(1).upper(),
            android_version=re.sub(r"\s+", " ", match.group(2)).title(),
            release_date=match.group(3),
            security_patch_level=match.group(4),
            source_url=normalized_url,
            verified_at=verified_at,
        )
        for match in pattern.finditer(text)
    ]
    if not records:
        raise ValueError("Samsung page contains no complete firmware record")
    for record in records:
        build_model = record.build_number[: len(page_model.removeprefix("SM-"))]
        if build_model != page_model.removeprefix("SM-"):
            raise ValueError("Samsung record build does not match page model")
    return records


def _catalog_payload(records: Iterable[FirmwareRecord]) -> dict:
    unique = {
        (record.model_code, record.build_number): record
        for record in records
    }
    return {
        "schema_version": 1,
        "records": [
            asdict(unique[key])
            for key in sorted(unique)
        ],
    }


def _write_json_atomically(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def write_catalog_atomically(
    path: Path,
    records: Iterable[FirmwareRecord],
) -> None:
    _write_json_atomically(Path(path), _catalog_payload(records))


def _load_runtime_records(path: Path) -> list[FirmwareRecord]:
    if not Path(path).is_file():
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("records"), list
    ):
        raise ValueError("unsupported Samsung runtime catalog schema")
    return [FirmwareRecord(**record) for record in payload["records"]]


def _load_sources(path: Path) -> list[str]:
    if not Path(path).is_file():
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("sources"), list
    ):
        raise ValueError("unsupported Samsung source registry schema")
    return [validate_samsung_url(url) for url in payload["sources"]]


def import_official_url(
    url: str,
    catalog_path: Path = DEFAULT_CACHE_PATH,
    sources_path: Path = DEFAULT_SOURCES_PATH,
    fetcher: Callable[[str], tuple[str, str]] = fetch_samsung_page,
    verified_at: str | None = None,
) -> int:
    normalized = validate_samsung_url(url)
    html, final_url = fetcher(normalized)
    timestamp = verified_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    imported = parse_samsung_page(html, final_url, timestamp)
    existing = _load_runtime_records(catalog_path)
    write_catalog_atomically(catalog_path, [*existing, *imported])
    sources = sorted(set([*_load_sources(sources_path), final_url]))
    _write_json_atomically(
        sources_path,
        {"schema_version": 1, "sources": sources},
    )
    return len(imported)


def refresh_registered_sources(
    catalog_path: Path = DEFAULT_CACHE_PATH,
    sources_path: Path = DEFAULT_SOURCES_PATH,
    fetcher: Callable[[str], tuple[str, str]] = fetch_samsung_page,
    verified_at: str | None = None,
) -> int:
    timestamp = verified_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
    refreshed: list[FirmwareRecord] = []
    for source in _load_sources(sources_path):
        html, final_url = fetcher(source)
        refreshed.extend(parse_samsung_page(html, final_url, timestamp))
    if refreshed:
        write_catalog_atomically(catalog_path, refreshed)
    return len(refreshed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    import_parser = commands.add_parser("import-url")
    import_parser.add_argument("url")
    commands.add_parser("refresh")
    args = parser.parse_args(argv)
    count = (
        import_official_url(args.url)
        if args.command == "import-url"
        else refresh_registered_sources()
    )
    print(f"Imported {count} verified Samsung firmware record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
