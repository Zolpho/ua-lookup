import datetime
import unittest

from time_display import format_utc_text_as_zurich, format_zurich_datetime


class ZurichTimeDisplayTests(unittest.TestCase):
    def test_winter_utc_is_displayed_as_cet(self):
        value = datetime.datetime(
            2026, 11, 4, 12, 30, 45, 123456,
            tzinfo=datetime.timezone.utc,
        )

        self.assertEqual(
            format_zurich_datetime(value),
            "2026-11-04 13:30:45.123456 CET",
        )

    def test_summer_utc_is_displayed_as_cest(self):
        value = datetime.datetime(
            2026, 9, 24, 9, 52, 36, 788988,
            tzinfo=datetime.timezone.utc,
        )

        self.assertEqual(
            format_zurich_datetime(value),
            "2026-09-24 11:52:36.788988 CEST",
        )

    def test_naive_database_timestamp_is_treated_as_utc(self):
        value = datetime.datetime(2026, 11, 4, 12, 30)

        self.assertEqual(
            format_zurich_datetime(value),
            "2026-11-04 13:30:00.000000 CET",
        )

    def test_winter_sqlite_text_is_displayed_as_cet(self):
        self.assertEqual(
            format_utc_text_as_zurich("2026-11-04 14:10:05"),
            "2026-11-04 15:10:05.000000 CET",
        )

    def test_summer_sqlite_text_is_displayed_as_cest(self):
        self.assertEqual(
            format_utc_text_as_zurich("2026-09-24 09:52:36"),
            "2026-09-24 11:52:36.000000 CEST",
        )

    def test_timezone_aware_text_is_converted(self):
        self.assertEqual(
            format_utc_text_as_zurich("2026-11-04T14:10:05Z"),
            "2026-11-04 15:10:05.000000 CET",
        )

    def test_empty_and_unrecognised_values_are_preserved(self):
        for value in (None, "", "-", "not-a-time"):
            with self.subTest(value=value):
                self.assertEqual(format_utc_text_as_zurich(value), value)


if __name__ == "__main__":
    unittest.main()
