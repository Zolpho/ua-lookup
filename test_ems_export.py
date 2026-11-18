import datetime
import csv
import io
import re
import unittest

from flask import Flask

from ems_export import (
    build_ems_records,
    create_ems_blueprint,
    ems_csv_filename,
    ems_event_id,
    parse_ems_allowed_ips,
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


class EmsBlueprintTests(unittest.TestCase):
    TOKEN = "a" * 64

    def make_client(self, report=None, allowed_ips=()):
        calls = []

        def loader():
            calls.append("loaded")
            return report or {"missing_mmtel": [], "generated_at": "generated"}

        app = Flask(__name__)
        app.register_blueprint(
            create_ems_blueprint(loader, self.TOKEN, allowed_ips=allowed_ips)
        )
        return app.test_client(), calls

    def test_rejects_short_token_during_blueprint_creation(self):
        with self.assertRaisesRegex(ValueError, "32"):
            create_ems_blueprint(lambda: {}, "too-short")

    def test_parses_exact_ipv4_and_ipv6_addresses(self):
        allowed = parse_ems_allowed_ips("192.0.2.10, 2001:db8::10")

        self.assertEqual({str(address) for address in allowed}, {
            "192.0.2.10",
            "2001:db8::10",
        })

    def test_rejects_malformed_allowlist_entry(self):
        with self.assertRaisesRegex(ValueError, "EMS_ALLOWED_IPS"):
            parse_ems_allowed_ips("192.0.2.10,not-an-ip")

    def test_ipv4_mapped_ipv6_does_not_equal_plain_ipv4(self):
        allowed = parse_ems_allowed_ips("::ffff:192.0.2.10")
        plain_ipv4 = next(iter(parse_ems_allowed_ips("192.0.2.10")))

        self.assertNotIn(plain_ipv4, allowed)

    def test_invalid_authorization_forms_are_identical_and_do_not_load_report(self):
        invalid_headers = [
            {},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer wrong"},
            {"Authorization": f"bearer {self.TOKEN}"},
            {"Authorization": f"Bearer  {self.TOKEN}"},
            {"Authorization": f"Bearer {self.TOKEN} trailing"},
        ]
        client, calls = self.make_client()

        responses = [
            client.get("/api/v1/mmtel-missing", headers=headers)
            for headers in invalid_headers
        ]

        self.assertTrue(all(response.status_code == 401 for response in responses))
        self.assertTrue(
            all(response.get_json() == {"error": "unauthorized"} for response in responses)
        )
        self.assertTrue(
            all(response.headers.get("WWW-Authenticate") == "Bearer" for response in responses)
        )
        self.assertTrue(
            all(response.headers.get("Cache-Control") == "no-store" for response in responses)
        )
        self.assertEqual(calls, [])

    def test_query_token_is_ignored(self):
        client, calls = self.make_client()

        response = client.get(f"/api/v1/mmtel-missing?token={self.TOKEN}")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])

    def test_exact_bearer_token_succeeds(self):
        client, calls = self.make_client()

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["loaded"])

    def test_configured_out_of_list_ip_is_forbidden_before_loading_report(self):
        client, calls = self.make_client(allowed_ips="192.0.2.10")

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
            environ_base={"REMOTE_ADDR": "192.0.2.11"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "forbidden"})
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(calls, [])

    def test_configured_exact_ip_succeeds(self):
        client, calls = self.make_client(allowed_ips="192.0.2.10")

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
            environ_base={"REMOTE_ADDR": "192.0.2.10"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["loaded"])

    def test_no_allowlist_permits_any_source_ip(self):
        client, calls = self.make_client()

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
            environ_base={"REMOTE_ADDR": "203.0.113.99"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["loaded"])

    def test_csv_route_does_not_require_api_credentials(self):
        client, calls = self.make_client()

        response = client.get("/mmtel-monitor/export.csv")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["loaded"])

    def test_csv_route_returns_bom_schema_and_timestamped_attachment(self):
        report = {
            "missing_mmtel": [problem_row()],
            "generated_at": "2026-11-18 11:00:00.000000 CET",
        }
        client, calls = self.make_client(report=report)

        response = client.get("/mmtel-monitor/export.csv")
        parsed = list(
            csv.DictReader(
                io.StringIO(response.data.decode("utf-8-sig"), newline="")
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertEqual(response.content_type, "text/csv; charset=utf-8")
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        self.assertRegex(
            response.headers["Content-Disposition"],
            r'^attachment; filename="mmtel-missing-ems-[0-9]{8}T[0-9]{6}Z\.csv"$',
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(set(parsed[0]), set(FIELD_ORDER))
        self.assertEqual(calls, ["loaded"])

    def test_csv_route_returns_header_only_for_no_records(self):
        client, _calls = self.make_client(
            report={"missing_mmtel": [], "generated_at": "generated"}
        )

        response = client.get("/mmtel-monitor/export.csv")
        rows = list(
            csv.reader(
                io.StringIO(response.data.decode("utf-8-sig"), newline="")
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(rows, [FIELD_ORDER])

    def test_api_returns_versioned_schema_and_bounded_records(self):
        report = {
            "missing_mmtel": [problem_row()],
            "generated_at": "2026-11-18 11:00:00.000000 CET",
        }
        client, calls = self.make_client(report=report)

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(set(payload), {"schema_version", "generated_at", "count", "records"})
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["generated_at"], report["generated_at"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(set(payload["records"][0]), FIELDS)
        self.assertFalse(
            {"contact", "source_url", "research_url"} & set(payload["records"][0])
        )
        self.assertEqual(calls, ["loaded"])

    def test_api_returns_empty_success(self):
        client, _calls = self.make_client(
            report={"missing_mmtel": [], "generated_at": "generated"}
        )

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "schema_version": 1,
                "generated_at": "generated",
                "count": 0,
                "records": [],
            },
        )

    def test_api_loader_failure_is_generic_503(self):
        def failing_loader():
            raise RuntimeError("sensitive backend detail")

        app = Flask(__name__)
        app.register_blueprint(create_ems_blueprint(failing_loader, self.TOKEN))
        client = app.test_client()

        response = client.get(
            "/api/v1/mmtel-missing",
            headers={"Authorization": f"Bearer {self.TOKEN}"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "service_unavailable"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn(b"sensitive backend detail", response.data)


if __name__ == "__main__":
    unittest.main()
