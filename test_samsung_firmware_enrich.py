import datetime
import json
import tempfile
import unittest
from pathlib import Path

from samsung_firmware import FirmwareRecord, SamsungIdentity
from samsung_firmware_enrich import (
    build_search_query,
    eligible_identities,
    exclusive_lock,
    run_enrichment,
    search_official_urls,
    unknown_problem_identities,
)


IDENTITY = SamsungIdentity("SM-A165F", "SM-A165F", "A165FXXUBEZJ7")
SOURCE = "https://doc.samsungmobile.com/SM-A165F/031857241225/roh.html"
NOW = datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.timezone.utc)


class FakeSearchResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def verified_record(identity=IDENTITY):
    return FirmwareRecord(
        model_code=identity.model_code,
        device_name=identity.device_name,
        build_number=identity.build_number,
        android_version="Android 17",
        release_date="2026-11-02",
        security_patch_level="2026-10-05",
        source_url=SOURCE,
        verified_at=NOW.isoformat(),
    )


class BraveDiscoveryTests(unittest.TestCase):
    def test_query_contains_exact_model_build_and_domain(self):
        self.assertEqual(
            build_search_query(IDENTITY),
            'site:doc.samsungmobile.com "SM-A165F" "A165FXXUBEZJ7"',
        )

    def test_search_returns_only_official_same_model_urls(self):
        seen = {}

        def opener(request, timeout):
            seen["request"] = request
            seen["timeout"] = timeout
            return FakeSearchResponse(
                {
                    "web": {
                        "results": [
                            {"url": SOURCE},
                            {"url": "https://evil.example/SM-A165F/doc.html"},
                            {
                                "url": "https://doc.samsungmobile.com.evil.example/SM-A165F/doc.html"
                            },
                            {"url": "https://doc.samsungmobile.com/SM-S936B/EUX/doc.html"},
                        ]
                    }
                }
            )

        urls = search_official_urls(IDENTITY, "secret", opener=opener)

        self.assertEqual(urls, [SOURCE])
        self.assertEqual(seen["request"].get_header("X-subscription-token"), "secret")
        self.assertEqual(seen["timeout"], 10)

    def test_search_rejects_oversized_response(self):
        class OversizedResponse(FakeSearchResponse):
            def __init__(self):
                self.body = b"x" * 21

        with self.assertRaisesRegex(ValueError, "large"):
            search_official_urls(
                IDENTITY,
                "secret",
                opener=lambda _request, _timeout: OversizedResponse(),
                max_bytes=20,
            )


class IdentitySelectionTests(unittest.TestCase):
    def test_selects_only_unknown_samsung_problem_rows_once(self):
        rows = [
            {
                "contact": "+g.3gpp.smsip",
                "user_agent": "SM-A165F-A165FXXUBEZJ7 Samsung IMS 6.0",
            },
            {
                "contact": "+g.3gpp.smsip",
                "user_agent": "SM-A165F-A165FXXUBEZJ7 Samsung IMS 6.0",
            },
            {
                "contact": '+g.3gpp.icsi-ref="urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel"',
                "user_agent": "SM-S936B-S936BXXUDDZJB Samsung IMS 6.0",
            },
            {"contact": "+g.3gpp.smsip", "user_agent": "iOS/27.0.1 iPhone"},
        ]

        self.assertEqual(unknown_problem_identities(rows, {}), [IDENTITY])

    def test_excludes_exact_catalog_hit(self):
        rows = [
            {
                "contact": "+g.3gpp.smsip",
                "user_agent": "SM-A165F-A165FXXUBEZJ7 Samsung IMS 6.0",
            }
        ]
        catalog = {(IDENTITY.model_code, IDENTITY.build_number): verified_record()}

        self.assertEqual(unknown_problem_identities(rows, catalog), [])

    def test_not_found_waits_24_hours_but_error_retries_after_6(self):
        state = {
            "attempts": {
                "SM-A165F|A165FXXUBEZJ7": {
                    "status": "not_found",
                    "attempted_at": (NOW - datetime.timedelta(hours=23)).isoformat(),
                }
            }
        }
        self.assertEqual(eligible_identities([IDENTITY], state, NOW, 24, 6), [])

        state["attempts"]["SM-A165F|A165FXXUBEZJ7"]["status"] = "error"
        state["attempts"]["SM-A165F|A165FXXUBEZJ7"]["attempted_at"] = (
            NOW - datetime.timedelta(hours=6)
        ).isoformat()
        self.assertEqual(eligible_identities([IDENTITY], state, NOW, 24, 6), [IDENTITY])

    def test_verified_state_does_not_hide_a_now_missing_catalog_record(self):
        state = {
            "attempts": {
                "SM-A165F|A165FXXUBEZJ7": {
                    "status": "verified",
                    "attempted_at": NOW.isoformat(),
                }
            }
        }

        self.assertEqual(eligible_identities([IDENTITY], state, NOW, 24, 6), [IDENTITY])


class EnrichmentRunTests(unittest.TestCase):
    def test_exclusive_lock_rejects_overlapping_run(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "enrichment.lock"
            with exclusive_lock(lock_path):
                with self.assertRaises(BlockingIOError):
                    with exclusive_lock(lock_path):
                        self.fail("overlapping lock unexpectedly acquired")

    def test_tries_candidates_until_exact_import_succeeds(self):
        visited = []

        def importer(identity, url):
            visited.append(url)
            if url.endswith("wrong.html"):
                raise ValueError("no exact model/build")
            return verified_record(identity)

        with tempfile.TemporaryDirectory() as directory:
            summary = run_enrichment(
                [IDENTITY],
                "secret",
                Path(directory) / "state.json",
                searcher=lambda _identity, _key: [
                    "https://doc.samsungmobile.com/SM-A165F/wrong.html",
                    SOURCE,
                ],
                importer=importer,
                now=NOW,
            )

        self.assertEqual(visited, [
            "https://doc.samsungmobile.com/SM-A165F/wrong.html",
            SOURCE,
        ])
        self.assertEqual(
            summary,
            {"attempted": 1, "verified": 1, "not_found": 0, "errors": 0},
        )

    def test_failure_for_one_identity_does_not_stop_next(self):
        second = SamsungIdentity("SM-S936B", "Galaxy S25+", "S936BXXUDDZJB")

        def searcher(identity, _key):
            if identity == IDENTITY:
                raise OSError("search unavailable")
            return ["https://doc.samsungmobile.com/SM-S936B/XSG/doc.html"]

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            summary = run_enrichment(
                [IDENTITY, second],
                "secret",
                state_path,
                searcher=searcher,
                importer=lambda identity, _url: verified_record(identity),
                now=NOW,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(state["attempts"]["SM-A165F|A165FXXUBEZJ7"]["status"], "error")
        self.assertEqual(state["attempts"]["SM-S936B|S936BXXUDDZJB"]["status"], "verified")

    def test_respects_per_run_search_limit(self):
        identities = [
            SamsungIdentity("SM-A165F", "Galaxy A16", f"A165FBUILD{index}")
            for index in range(12)
        ]
        with tempfile.TemporaryDirectory() as directory:
            summary = run_enrichment(
                identities,
                "secret",
                Path(directory) / "state.json",
                searcher=lambda _identity, _key: [],
                importer=lambda _identity, _url: None,
                now=NOW,
                max_searches=10,
            )

        self.assertEqual(summary["attempted"], 10)
        self.assertEqual(summary["not_found"], 10)


if __name__ == "__main__":
    unittest.main()
