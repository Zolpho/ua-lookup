import datetime
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from samsung_firmware import FirmwareRecord, SamsungIdentity
from samsung_firmware_enrich import (
    build_official_urls,
    eligible_identities,
    exclusive_lock,
    main,
    parse_csc_candidates,
    run_enrichment,
    unknown_problem_identities,
)


IDENTITY = SamsungIdentity("SM-A165F", "SM-A165F", "A165FXXUBEZJ7")
SOURCE = "https://doc.samsungmobile.com/SM-A165F/031857241225/roh.html"
NOW = datetime.datetime(2026, 11, 17, 10, 0, tzinfo=datetime.timezone.utc)


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


class OfficialCandidateTests(unittest.TestCase):
    def test_builds_only_official_model_csc_document_urls(self):
        self.assertEqual(
            build_official_urls(IDENTITY, ("EUX", "AUT", "SWC")),
            [
                "https://doc.samsungmobile.com/SM-A165F/EUX/doc.html",
                "https://doc.samsungmobile.com/SM-A165F/AUT/doc.html",
                "https://doc.samsungmobile.com/SM-A165F/SWC/doc.html",
            ],
        )

    def test_url_builder_rejects_unvalidated_csc_input(self):
        with self.assertRaisesRegex(ValueError, "CSC"):
            build_official_urls(IDENTITY, ("EUX", "../XSG"))

    def test_csc_candidates_are_normalized_deduplicated_and_bounded(self):
        self.assertEqual(
            parse_csc_candidates(" eux, AUT,swc,EUX "),
            ("EUX", "AUT", "SWC"),
        )
        with self.assertRaisesRegex(ValueError, "CSC"):
            parse_csc_candidates("EUX,../XSG")
        with self.assertRaisesRegex(ValueError, "at most"):
            parse_csc_candidates(",".join(f"X{index:02d}" for index in range(17)))

    def test_main_rejects_invalid_csc_before_refresh_or_registration_query(self):
        with mock.patch.dict(
            os.environ, {"SAMSUNG_CSC_CANDIDATES": "EUX,../XSG"}
        ), mock.patch(
            "samsung_firmware_enrich.refresh_registered_sources"
        ) as refresh, mock.patch(
            "samsung_firmware_enrich.load_unknown_problem_identities"
        ) as load_identities:
            self.assertEqual(main(), 2)

        refresh.assert_not_called()
        load_identities.assert_not_called()


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
                Path(directory) / "state.json",
                csc_candidates=("EUX",),
                candidate_builder=lambda _identity, _cscs: [
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

        def candidate_builder(identity, _cscs):
            if identity == IDENTITY:
                raise OSError("candidate generation unavailable")
            return ["https://doc.samsungmobile.com/SM-S936B/XSG/doc.html"]

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            summary = run_enrichment(
                [IDENTITY, second],
                state_path,
                csc_candidates=("EUX",),
                candidate_builder=candidate_builder,
                importer=lambda identity, _url: verified_record(identity),
                now=NOW,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(state["attempts"]["SM-A165F|A165FXXUBEZJ7"]["status"], "error")
        self.assertEqual(state["attempts"]["SM-S936B|S936BXXUDDZJB"]["status"], "verified")

    def test_missing_csc_pages_are_recorded_as_not_found(self):
        def missing_page(_identity, url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as directory:
            summary = run_enrichment(
                [IDENTITY],
                Path(directory) / "state.json",
                csc_candidates=("EUX", "AUT"),
                importer=missing_page,
                now=NOW,
            )

        self.assertEqual(
            summary,
            {"attempted": 1, "verified": 0, "not_found": 1, "errors": 0},
        )

    def test_respects_per_run_identity_limit(self):
        identities = [
            SamsungIdentity("SM-A165F", "Galaxy A16", f"A165FBUILD{index}")
            for index in range(12)
        ]
        with tempfile.TemporaryDirectory() as directory:
            summary = run_enrichment(
                identities,
                Path(directory) / "state.json",
                csc_candidates=("EUX",),
                candidate_builder=lambda _identity, _cscs: [],
                importer=lambda _identity, _url: None,
                now=NOW,
                max_identities=5,
            )

        self.assertEqual(summary["attempted"], 5)
        self.assertEqual(summary["not_found"], 5)


if __name__ == "__main__":
    unittest.main()
