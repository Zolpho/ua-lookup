import json
import tempfile
import unittest
from pathlib import Path

from samsung_firmware import (
    SamsungIdentity,
    load_firmware_catalog,
    lookup_samsung_firmware,
    parse_samsung_user_agent,
)


def _catalog(record):
    return {"schema_version": 1, "records": [record]}


def _record(**overrides):
    record = {
        "model_code": "SM-A165F",
        "device_name": "Galaxy A16",
        "build_number": "A165FXXUBEZJ7",
        "android_version": "Android 17",
        "release_date": "2026-11-02",
        "security_patch_level": "2026-10-05",
        "source_url": (
            "https://doc.samsungmobile.com/SM-A165F/031857241225/roh.html"
        ),
        "verified_at": "2026-11-17T08:00:00+00:00",
    }
    record.update(overrides)
    return record


class SamsungIdentityTests(unittest.TestCase):
    def test_preserves_complete_model_and_build(self):
        identity = parse_samsung_user_agent(
            "SM-A546B-A546BXXUNGZJ5 Samsung IMS 6.0"
        )

        self.assertEqual(
            identity,
            SamsungIdentity(
                model_code="SM-A546B",
                device_name="Galaxy A54",
                build_number="A546BXXUNGZJ5",
            ),
        )

    def test_non_samsung_user_agent_is_not_parsed(self):
        self.assertIsNone(parse_samsung_user_agent("iOS/27.0.1 iPhone"))


class FirmwareCatalogTests(unittest.TestCase):
    def test_exact_model_and_build_match_returns_record(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "seed.json"
            seed.write_text(json.dumps(_catalog(_record())), encoding="utf-8")

            catalog = load_firmware_catalog(seed, None)
            result = lookup_samsung_firmware(
                SamsungIdentity("SM-A165F", "Galaxy A16", "A165FXXUBEZJ7"),
                catalog,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.android_version, "Android 17")

    def test_same_build_for_wrong_model_does_not_match(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "seed.json"
            seed.write_text(json.dumps(_catalog(_record())), encoding="utf-8")

            catalog = load_firmware_catalog(seed, None)
            result = lookup_samsung_firmware(
                SamsungIdentity("SM-S936B", "Galaxy S25+", "A165FXXUBEZJ7"),
                catalog,
            )

        self.assertIsNone(result)

    def test_malformed_runtime_catalog_keeps_seed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "seed.json"
            runtime = Path(directory) / "runtime.json"
            seed.write_text(json.dumps(_catalog(_record())), encoding="utf-8")
            runtime.write_text("{broken", encoding="utf-8")

            with self.assertLogs("samsung_firmware", level="WARNING"):
                catalog = load_firmware_catalog(seed, runtime)

        self.assertIn(("SM-A165F", "A165FXXUBEZJ7"), catalog)

    def test_runtime_exact_record_overrides_seed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "seed.json"
            runtime = Path(directory) / "runtime.json"
            seed.write_text(json.dumps(_catalog(_record())), encoding="utf-8")
            runtime.write_text(
                json.dumps(
                    _catalog(
                        _record(
                            release_date="2026-11-03",
                            verified_at="2026-11-18T08:00:00+00:00",
                        )
                    )
                ),
                encoding="utf-8",
            )

            catalog = load_firmware_catalog(seed, runtime)

        self.assertEqual(
            catalog[("SM-A165F", "A165FXXUBEZJ7")].release_date,
            "2026-11-03",
        )

    def test_unsafe_runtime_source_url_does_not_override_seed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "seed.json"
            runtime = Path(directory) / "runtime.json"
            seed.write_text(json.dumps(_catalog(_record())), encoding="utf-8")
            runtime.write_text(
                json.dumps(
                    _catalog(
                        _record(
                            source_url="javascript:alert(1)",
                            release_date="2099-01-01",
                        )
                    )
                ),
                encoding="utf-8",
            )

            with self.assertLogs("samsung_firmware", level="WARNING"):
                catalog = load_firmware_catalog(seed, runtime)

        self.assertEqual(
            catalog[("SM-A165F", "A165FXXUBEZJ7")].release_date,
            "2026-11-02",
        )


if __name__ == "__main__":
    unittest.main()
