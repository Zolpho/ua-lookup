import datetime
import csv
import io
import re
import unittest

from ems_export import (
    build_ems_records,
    ems_csv_filename,
    ems_event_id,
    render_ems_csv,
)


UTC = datetime.timezone.utc
FIELDS = {
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
}
FIELD_ORDER = [
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
]


def problem_row(**overrides):
    row = {
        "timestamp": datetime.datetime(2026, 11, 18, 10, 0, tzinfo=UTC),
        "timestamp_zurich": "2026-11-18 11:00:00.000000 CET",
        "msisdn": "41791234567",
        "imsi": "228580000000001",
        "user_agent": "SM-A566B-A566BXXS5AYFA Samsung IMS 6.0",
        "contact": "<sip:subscriber@example>;g.3gpp.smsip",
        "status": "MMTEL_MISSING",
        "model": "Galaxy A56",
        "build_number": "A566BXXS5AYFA",
        "os_version": "Android 15",
        "source_url": "https://doc.samsungmobile.com/secret",
        "research_url": "https://www.google.com/search?q=secret",
    }
    row.update(overrides)
    return row


class EmsRecordTests(unittest.TestCase):
    def test_normalizes_only_the_exact_export_fields(self):
        records = build_ems_records(
            {
                "missing_mmtel": [problem_row()],
                "unable_to_verify": [
                    problem_row(
                        imsi="228580000000999",
                        status="CONTACT_MISSING",
                    )
                ],
            }
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(set(records[0]), FIELDS)
        self.assertEqual(records[0]["msisdn"], "41791234567")
        self.assertEqual(records[0]["imsi"], "228580000000001")
        self.assertEqual(records[0]["registered_at"], "2026-11-18T10:00:00+00:00")
        self.assertEqual(
            records[0]["registered_at_zurich"],
            "2026-11-18 11:00:00.000000 CET",
        )
        self.assertEqual(records[0]["device"], "Galaxy A56")
        self.assertEqual(records[0]["build_number"], "A566BXXS5AYFA")
        self.assertEqual(records[0]["os_version"], "Android 15")
        self.assertEqual(
            records[0]["user_agent"],
            "SM-A566B-A566BXXS5AYFA Samsung IMS 6.0",
        )
        self.assertEqual(records[0]["reason"], "MMTEL_MISSING")

    def test_skips_invalid_subscriber_identifiers_status_and_timestamp(self):
        invalid_rows = [
            problem_row(msisdn=""),
            problem_row(msisdn="1234567"),
            problem_row(msisdn="1" * 16),
            problem_row(msisdn="+41791234567"),
            problem_row(imsi=""),
            problem_row(imsi="22858A"),
            problem_row(status="CONTACT_MISSING"),
            problem_row(timestamp="not-a-timestamp"),
        ]

        self.assertEqual(build_ems_records({"missing_mmtel": invalid_rows}), [])

    def test_converts_naive_and_aware_timestamps_to_utc(self):
        plus_two = datetime.timezone(datetime.timedelta(hours=2))
        records = build_ems_records(
            {
                "missing_mmtel": [
                    problem_row(
                        imsi="228580000000001",
                        timestamp=datetime.datetime(2026, 11, 18, 12, 0),
                    ),
                    problem_row(
                        imsi="228580000000002",
                        timestamp=datetime.datetime(
                            2026, 11, 18, 12, 0, tzinfo=plus_two
                        ),
                    ),
                ]
            }
        )

        self.assertEqual(
            [record["registered_at"] for record in records],
            ["2026-11-18T12:00:00+00:00", "2026-11-18T10:00:00+00:00"],
        )

    def test_orders_newest_first_then_imsi_ascending(self):
        instant = datetime.datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
        records = build_ems_records(
            {
                "missing_mmtel": [
                    problem_row(imsi="228580000000003", timestamp=instant),
                    problem_row(
                        imsi="228580000000004",
                        timestamp=instant + datetime.timedelta(minutes=1),
                    ),
                    problem_row(imsi="228580000000001", timestamp=instant),
                ]
            }
        )

        self.assertEqual(
            [record["imsi"] for record in records],
            ["228580000000004", "228580000000001", "228580000000003"],
        )

    def test_event_id_is_stable_full_sha256(self):
        first = ems_event_id("228580000000001", "2026-11-18T10:00:00+00:00")
        second = ems_event_id("228580000000001", "2026-11-18T10:00:00+00:00")

        self.assertEqual(first, second)
        self.assertRegex(first, re.compile(r"^mmtel_[0-9a-f]{64}$"))
        self.assertNotIn("228580000000001", first)


class EmsCsvTests(unittest.TestCase):
    def test_csv_has_bom_exact_header_and_crlf_rows(self):
        record = build_ems_records({"missing_mmtel": [problem_row()]})[0]

        payload = render_ems_csv([record])
        text = payload.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text, newline="")))

        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(rows[0], FIELD_ORDER)
        self.assertEqual(len(rows), 2)
        self.assertIn("\r\n", text)

    def test_csv_quotes_special_text_and_preserves_formula_like_data(self):
        record = build_ems_records(
            {
                "missing_mmtel": [
                    problem_row(
                        model='=HYPERLINK("https://example.test","Device, one")',
                        user_agent='Agent "quoted"\nsecond line',
                    )
                ]
            }
        )[0]

        payload = render_ems_csv(
            [
                {
                    **record,
                    "contact": "must not be exported",
                    "source_url": "must not be exported",
                    "research_url": "must not be exported",
                }
            ]
        )
        parsed = list(
            csv.DictReader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        )[0]

        self.assertEqual(
            parsed["device"],
            '=HYPERLINK("https://example.test","Device, one")',
        )
        self.assertEqual(parsed["user_agent"], 'Agent "quoted"\nsecond line')
        self.assertEqual(set(parsed), set(FIELD_ORDER))
        self.assertNotIn(b"must not be exported", payload)

    def test_empty_csv_contains_only_the_header(self):
        payload = render_ems_csv([])
        rows = list(
            csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline=""))
        )

        self.assertEqual(rows, [FIELD_ORDER])

    def test_filename_uses_utc_timestamp(self):
        plus_two = datetime.timezone(datetime.timedelta(hours=2))
        now = datetime.datetime(2026, 11, 18, 13, 14, 15, tzinfo=plus_two)

        self.assertEqual(
            ems_csv_filename(now),
            "mmtel-missing-ems-20261118T111415Z.csv",
        )


if __name__ == "__main__":
    unittest.main()
