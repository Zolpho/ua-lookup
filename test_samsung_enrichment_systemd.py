import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class SamsungEnrichmentSystemdTests(unittest.TestCase):
    def test_oneshot_service_uses_protected_application_environment(self):
        unit = (ROOT / "ua-lookup-samsung-enrichment.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("Type=oneshot", unit)
        self.assertIn("WorkingDirectory=/opt/ua-lookup", unit)
        self.assertIn("EnvironmentFile=/opt/ua-lookup/config.env", unit)
        self.assertIn(
            "ExecStart=/opt/ua-lookup/venv/bin/python "
            "/opt/ua-lookup/samsung_firmware_enrich.py",
            unit,
        )
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("StateDirectory=ua-lookup", unit)
        self.assertIn("StateDirectoryMode=0750", unit)

    def test_timer_is_persistent_and_runs_every_six_hours(self):
        unit = (ROOT / "ua-lookup-samsung-enrichment.timer").read_text(
            encoding="utf-8"
        )

        self.assertIn("OnBootSec=10min", unit)
        self.assertIn("OnUnitActiveSec=6h", unit)
        self.assertIn("RandomizedDelaySec=5min", unit)
        self.assertIn("Persistent=true", unit)
        self.assertIn("Unit=ua-lookup-samsung-enrichment.service", unit)
        self.assertIn("WantedBy=timers.target", unit)


if __name__ == "__main__":
    unittest.main()
