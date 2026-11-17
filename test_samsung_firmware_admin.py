import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from samsung_firmware_admin import (
    SamsungRedirectHandler,
    fetch_samsung_page,
    import_exact_official_url,
    import_official_url,
    main,
    parse_samsung_page,
    refresh_registered_sources,
    validate_samsung_url,
    write_catalog_atomically,
)
from samsung_firmware import SamsungIdentity


PAGE = """<!doctype html>
<html><body>
<div>This update includes improvements to functions as listed below.</div>
<h1>Galaxy A16 (SM-A165F)</h1>
<section>
  <b>Build Number :</b> A165FXXUBEZJ7
  <b>Android version :</b> C(Android 17)
  <b>Release Date :</b> 2026-11-02
  <b>Security patch level :</b> 2026-10-05
</section>
<section>
  <b>Build Number :</b> A165FXXSBDZH6
  <b>Android version :</b> B(Android 16)
  <b>Release Date :</b> 2026-09-17
  <b>Security patch level :</b> 2026-08-05
</section>
</body></html>"""

SOURCE = "https://doc.samsungmobile.com/SM-A165F/031857241225/roh.html"


class FakeHeaders:
    def __init__(self, content_type="text/html", charset="utf-8"):
        self.content_type = content_type
        self.charset = charset

    def get_content_type(self):
        return self.content_type

    def get_content_charset(self, failobj=None):
        return self.charset or failobj


class FakeResponse:
    def __init__(self, body, url=SOURCE, content_type="text/html"):
        self.body = body
        self.url = url
        self.headers = FakeHeaders(content_type)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url


class SamsungPageParserTests(unittest.TestCase):
    def test_extracts_multiple_exact_records(self):
        records = parse_samsung_page(
            PAGE,
            SOURCE,
            "2026-11-17T08:00:00+00:00",
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].model_code, "SM-A165F")
        self.assertEqual(records[0].device_name, "Galaxy A16")
        self.assertEqual(records[0].build_number, "A165FXXUBEZJ7")
        self.assertEqual(records[0].android_version, "Android 17")
        self.assertEqual(records[0].release_date, "2026-11-02")
        self.assertEqual(records[0].security_patch_level, "2026-10-05")
        self.assertEqual(records[0].source_url, SOURCE)

    def test_rejects_page_model_different_from_source_model(self):
        with self.assertRaisesRegex(ValueError, "model"):
            parse_samsung_page(
                PAGE,
                SOURCE.replace("SM-A165F", "SM-S936B"),
                "2026-11-17T08:00:00+00:00",
            )

    def test_rejects_page_without_complete_records(self):
        with self.assertRaisesRegex(ValueError, "record"):
            parse_samsung_page(
                "<html><h1>Galaxy A16 (SM-A165F)</h1></html>",
                SOURCE,
                "2026-11-17T08:00:00+00:00",
            )


class SamsungUrlBoundaryTests(unittest.TestCase):
    def test_accepts_and_normalizes_official_https_url(self):
        self.assertEqual(validate_samsung_url(SOURCE), SOURCE)

    def test_rejects_non_https_and_deceptive_hosts(self):
        rejected = (
            SOURCE.replace("https://", "http://"),
            "//doc.samsungmobile.com/SM-A165F/doc.html",
            "https://doc.samsungmobile.com.evil.example/SM-A165F/doc.html",
            "https://doc.samsungmobile.com@evil.example/SM-A165F/doc.html",
            "https://%64oc.samsungmobile.com/SM-A165F/doc.html",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_samsung_url(url)

    def test_redirect_handler_rejects_off_domain_hop(self):
        handler = SamsungRedirectHandler()

        with self.assertRaises(ValueError):
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://evil.example/firmware",
            )


class SamsungFetchTests(unittest.TestCase):
    @mock.patch("samsung_firmware_admin.urllib.request.build_opener")
    def test_fetches_bounded_html_and_returns_final_url(self, build_opener):
        build_opener.return_value.open.return_value = FakeResponse(PAGE.encode())

        html, final_url = fetch_samsung_page(SOURCE)

        self.assertEqual(html, PAGE)
        self.assertEqual(final_url, SOURCE)

    @mock.patch("samsung_firmware_admin.urllib.request.build_opener")
    def test_rejects_non_html_response(self, build_opener):
        build_opener.return_value.open.return_value = FakeResponse(
            b"{}",
            content_type="application/json",
        )

        with self.assertRaisesRegex(ValueError, "HTML"):
            fetch_samsung_page(SOURCE)

    @mock.patch("samsung_firmware_admin.urllib.request.build_opener")
    def test_rejects_response_larger_than_limit(self, build_opener):
        build_opener.return_value.open.return_value = FakeResponse(b"x" * 21)

        with self.assertRaisesRegex(ValueError, "large"):
            fetch_samsung_page(SOURCE, max_bytes=20)

    @mock.patch("samsung_firmware_admin.urllib.request.build_opener")
    def test_rejects_off_domain_final_url(self, build_opener):
        build_opener.return_value.open.return_value = FakeResponse(
            PAGE.encode(),
            url="https://evil.example/firmware",
        )

        with self.assertRaises(ValueError):
            fetch_samsung_page(SOURCE)


class SamsungCatalogWriteTests(unittest.TestCase):
    def _records(self):
        return parse_samsung_page(
            PAGE,
            SOURCE,
            "2026-11-17T08:00:00+00:00",
        )

    def test_successful_write_atomically_replaces_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text("old", encoding="utf-8")

            write_catalog_atomically(path, self._records())
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["records"]), 2)

    @mock.patch("samsung_firmware_admin.os.replace", side_effect=OSError("stop"))
    def test_failed_replace_preserves_previous_catalog(self, _replace):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text("previous-valid-catalog", encoding="utf-8")

            with self.assertRaises(OSError):
                write_catalog_atomically(path, self._records())

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "previous-valid-catalog",
            )

    def test_import_registers_source_and_writes_records(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            sources_path = Path(directory) / "sources.json"

            count = import_official_url(
                SOURCE,
                catalog_path,
                sources_path,
                fetcher=lambda _url: (PAGE, SOURCE),
                verified_at="2026-11-17T08:00:00+00:00",
            )

            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            sources = json.loads(sources_path.read_text(encoding="utf-8"))

        self.assertEqual(count, 2)
        self.assertEqual(sources, {"schema_version": 1, "sources": [SOURCE]})
        self.assertEqual(len(catalog["records"]), 2)

    def test_exact_import_accepts_only_requested_model_and_build(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            sources_path = Path(directory) / "sources.json"

            record = import_exact_official_url(
                SamsungIdentity("SM-A165F", "Galaxy A16", "A165FXXUBEZJ7"),
                SOURCE,
                catalog_path,
                sources_path,
                fetcher=lambda _url: (PAGE, SOURCE),
                verified_at="2026-11-17T08:00:00+00:00",
            )
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

        self.assertEqual(record.model_code, "SM-A165F")
        self.assertEqual(record.build_number, "A165FXXUBEZJ7")
        self.assertEqual(len(catalog["records"]), 1)

    def test_exact_import_rejects_missing_build_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            sources_path = Path(directory) / "sources.json"
            catalog_path.write_text("previous", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exact model/build"):
                import_exact_official_url(
                    SamsungIdentity("SM-A165F", "Galaxy A16", "A165FXXUZZZZ9"),
                    SOURCE,
                    catalog_path,
                    sources_path,
                    fetcher=lambda _url: (PAGE, SOURCE),
                    verified_at="2026-11-17T08:00:00+00:00",
                )

            self.assertEqual(catalog_path.read_text(encoding="utf-8"), "previous")
            self.assertFalse(sources_path.exists())

    def test_exact_import_rejects_requested_model_different_from_page(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "model"):
                import_exact_official_url(
                    SamsungIdentity("SM-S936B", "Galaxy S25+", "A165FXXUBEZJ7"),
                    SOURCE,
                    Path(directory) / "catalog.json",
                    Path(directory) / "sources.json",
                    fetcher=lambda _url: (PAGE, SOURCE),
                    verified_at="2026-11-17T08:00:00+00:00",
                )

    def test_refresh_uses_only_registered_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            sources_path = Path(directory) / "sources.json"
            sources_path.write_text(
                json.dumps({"schema_version": 1, "sources": [SOURCE]}),
                encoding="utf-8",
            )
            visited = []

            count = refresh_registered_sources(
                catalog_path,
                sources_path,
                fetcher=lambda url: (visited.append(url) or PAGE, url),
                verified_at="2026-11-17T08:00:00+00:00",
            )

        self.assertEqual(visited, [SOURCE])
        self.assertEqual(count, 2)


class SamsungAdminCliTests(unittest.TestCase):
    @mock.patch("samsung_firmware_admin.import_official_url", return_value=3)
    def test_import_url_command_dispatches_guarded_import(self, importer):
        output = io.StringIO()

        with redirect_stdout(output):
            result = main(["import-url", SOURCE])

        self.assertEqual(result, 0)
        importer.assert_called_once_with(SOURCE)
        self.assertIn("Imported 3 verified", output.getvalue())

    @mock.patch("samsung_firmware_admin.refresh_registered_sources", return_value=2)
    def test_refresh_command_dispatches_registered_sources(self, refresher):
        output = io.StringIO()

        with redirect_stdout(output):
            result = main(["refresh"])

        self.assertEqual(result, 0)
        refresher.assert_called_once_with()
        self.assertIn("Imported 2 verified", output.getvalue())


if __name__ == "__main__":
    unittest.main()
