import datetime
import unittest

from mmtel_monitor import (
    MonitorCache,
    build_monitor_report,
    classify_contact,
    parse_device_metadata,
    parse_ucn_subscriber_lines,
    registration_expires,
)


UTC = datetime.timezone.utc


class ContactClassificationTests(unittest.TestCase):
    def test_percent_encoded_mmtel_icsi_is_present(self):
        contact = (
            '<sip:228580000388062@100.97.103.139:6300>;+sip.instance="x";'
            '+g.3gpp.icsi-ref="urn%3Aurn-7%3A3gpp-service.ims.icsi.mmtel";'
            '+g.3gpp.smsip'
        )

        self.assertEqual(classify_contact(contact), "MMTEL_PRESENT")

    def test_sms_only_contact_is_missing_mmtel(self):
        contact = (
            '<sip:228580015002144@100.96.174.111:6800>;+sip.instance="x";'
            '+g.3gpp.smsip'
        )

        self.assertEqual(classify_contact(contact), "MMTEL_MISSING")

    def test_empty_contact_is_unverifiable(self):
        self.assertEqual(classify_contact(""), "CONTACT_MISSING")


class DeviceMetadataTests(unittest.TestCase):
    def test_apple_iphone_keeps_full_ios_version(self):
        metadata = parse_device_metadata("iOS/27.0.1 iPhone")

        self.assertEqual(
            metadata,
            {
                "model": "Apple iPhone",
                "build_number": "",
                "os_version": "iOS 27.0.1",
                "security": "",
                "fw_release_date": "",
                "security_patch_level": "",
            },
        )

    def test_known_samsung_build_uses_official_firmware_metadata(self):
        metadata = parse_device_metadata(
            "SM-A566B-A566BXXS6AYGE Samsung IMS 6.0"
        )

        self.assertEqual(metadata["model"], "Galaxy A56")
        self.assertEqual(metadata["build_number"], "A566BXXS6AYGE")
        self.assertEqual(metadata["os_version"], "Android 15")
        self.assertEqual(metadata["security"], "Security-only update")
        self.assertEqual(metadata["fw_release_date"], "2025-08-05")
        self.assertEqual(metadata["security_patch_level"], "2025-08-01")

    def test_unknown_build_keeps_inferred_fields_and_blank_catalog_fields(self):
        metadata = parse_device_metadata(
            "Google_Pixel8_Android 16_BP4A.260205.001"
        )

        self.assertEqual(metadata["model"], "Pixel8")
        self.assertEqual(metadata["build_number"], "BP4A.260205.001")
        self.assertEqual(metadata["os_version"], "Android 16")
        self.assertEqual(metadata["security"], "")
        self.assertEqual(metadata["fw_release_date"], "")
        self.assertEqual(metadata["security_patch_level"], "")

    def test_unrecognized_user_agent_does_not_invent_metadata(self):
        metadata = parse_device_metadata("VendorPhone IMS client/9.4")

        self.assertEqual(
            metadata,
            {
                "model": "",
                "build_number": "",
                "os_version": "",
                "security": "",
                "fw_release_date": "",
                "security_patch_level": "",
            },
        )


class ReportTests(unittest.TestCase):
    def test_contact_only_expires_zero_is_a_deregistration(self):
        self.assertEqual(
            registration_expires(
                None,
                "<sip:228580000000001@example>;expires=0;+g.3gpp.smsip",
                "REGISTER sip:example SIP/2.0\r\n",
            ),
            0,
        )

    def test_later_deregistration_does_not_hide_latest_positive_registration(self):
        rows = [
            {
                "timestamp": datetime.datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
                "imsi": "228580000000001",
                "user_agent": "SM-A566B-A566BXXS6AYGE Samsung IMS 6.0",
                "contact": "<sip:a@b>;expires=0;+g.3gpp.smsip",
                "expires": None,
                "payload": "REGISTER sip:example SIP/2.0\r\n",
            },
            {
                "timestamp": datetime.datetime(2026, 11, 16, 8, 0, tzinfo=UTC),
                "imsi": "228580000000001",
                "user_agent": "SM-A566B-A566BXXS6AYGE Samsung IMS 6.0",
                "contact": "<sip:a@b>;expires=600;+g.3gpp.smsip",
                "expires": None,
                "payload": "REGISTER sip:example SIP/2.0\r\n",
            },
        ]

        report = build_monitor_report(rows, {})

        self.assertEqual(len(report["missing_mmtel"]), 1)
        self.assertEqual(
            report["missing_mmtel"][0]["timestamp"],
            datetime.datetime(2026, 11, 16, 8, 0, tzinfo=UTC),
        )

    def test_latest_registration_per_imsi_is_classified_and_sorted(self):
        rows = [
            {
                "timestamp": datetime.datetime(2026, 11, 16, 8, 0, tzinfo=UTC),
                "imsi": "228580000000001",
                "user_agent": "SM-A566B-A566BXXS6AYGE Samsung IMS 6.0",
                "contact": "<sip:a@b>;+g.3gpp.smsip",
            },
            {
                "timestamp": datetime.datetime(2026, 11, 16, 7, 0, tzinfo=UTC),
                "imsi": "228580000000001",
                "user_agent": "old agent",
                "contact": "<sip:a@b>;3gpp-service.ims.icsi.mmtel",
            },
            {
                "timestamp": datetime.datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
                "imsi": "228580000000002",
                "user_agent": "iOS/27.0.1 iPhone",
                "contact": "",
            },
            {
                "timestamp": datetime.datetime(2026, 11, 16, 9, 30, tzinfo=UTC),
                "imsi": "228580000000003",
                "user_agent": "SM-A566B-A566BXXS5AYFA Samsung IMS 6.0",
                "contact": "<sip:a@b>;3gpp-service.ims.icsi.mmtel",
            },
        ]

        report = build_monitor_report(
            rows,
            {
                "228580000000001": "41790000001",
                "228580000000002": "41790000002",
            },
        )

        self.assertEqual(
            [row["imsi"] for row in report["missing_mmtel"]],
            ["228580000000001"],
        )
        self.assertEqual(
            [row["imsi"] for row in report["unable_to_verify"]],
            ["228580000000002"],
        )
        self.assertEqual(report["missing_mmtel"][0]["msisdn"], "41790000001")
        self.assertNotIn(
            "228580000000003",
            [row["imsi"] for row in report["missing_mmtel"]],
        )

    def test_report_preserves_complete_unrecognized_user_agent(self):
        user_agent = "VendorPhone IMS client/9.4 (custom build)"
        rows = [
            {
                "timestamp": datetime.datetime(2026, 11, 16, 10, 0, tzinfo=UTC),
                "imsi": "228580000000004",
                "user_agent": user_agent,
                "contact": "<sip:a@b>;+g.3gpp.smsip",
            },
        ]

        report = build_monitor_report(rows, {})

        self.assertEqual(report["missing_mmtel"][0]["user_agent"], user_agent)
        self.assertEqual(report["missing_mmtel"][0]["model"], "")
        self.assertEqual(report["missing_mmtel"][0]["os_version"], "")

    def test_latest_mmtel_present_registration_is_not_a_problem(self):
        imsi = "228580000000005"
        rows = [
            {
                "timestamp": datetime.datetime(2026, 11, 16, 9, 0, tzinfo=UTC),
                "imsi": imsi,
                "user_agent": "iOS/27.0.1 iPhone",
                "contact": "<sip:a@b>;+g.3gpp.smsip",
            },
            {
                "timestamp": datetime.datetime(2026, 11, 16, 10, 0, tzinfo=UTC),
                "imsi": imsi,
                "user_agent": "iOS/27.0.1 iPhone",
                "contact": (
                    '<sip:a@b>;+g.3gpp.icsi-ref="urn%3Aurn-7%3A'
                    '3gpp-service.ims.icsi.mmtel";+g.3gpp.smsip'
                ),
            },
        ]

        report = build_monitor_report(rows, {})

        self.assertEqual(report["missing_mmtel"], [])
        self.assertEqual(report["unable_to_verify"], [])


class UcnMappingTests(unittest.TestCase):
    def test_json_lines_are_normalized_to_imsi_msisdn_map(self):
        output = "\n".join(
            [
                '{"imsi":"228580000000001@ims.example","msisdn":"41790000001"}',
                "not-json",
                '{"imsi":"228580000000002","msisdn":"+41790000002"}',
            ]
        )

        self.assertEqual(
            parse_ucn_subscriber_lines(output),
            {
                "228580000000001": "41790000001",
                "228580000000002": "41790000002",
            },
        )


class MonitorCacheTests(unittest.TestCase):
    def test_loader_runs_once_within_five_minute_ttl(self):
        now = [1000.0]
        calls = []
        cache = MonitorCache(ttl_seconds=300, clock=lambda: now[0])

        first = cache.get(lambda: calls.append("load") or {"version": 1})
        now[0] += 299
        second = cache.get(lambda: calls.append("load") or {"version": 2})
        now[0] += 2
        third = cache.get(lambda: calls.append("load") or {"version": 3})

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 1)
        self.assertEqual(third["version"], 3)
        self.assertEqual(calls, ["load", "load"])


if __name__ == "__main__":
    unittest.main()
