import datetime
import unittest

from registration_history import pair_successful_registrations


UTC = datetime.timezone.utc
SUBSCRIBER = "228580015006014"
SESSION = "D7IA1JjSe6llRo6qDWg2cVmY"


def row(timestamp, payload, **values):
    return {
        "timestamp": timestamp,
        "session_id": values.get("session_id", SESSION),
        "aor": values.get("aor", f"{SUBSCRIBER}@ims.example"),
        "src_ip": values.get("src_ip", "100.96.203.141"),
        "user_agent": values.get("user_agent", "test handset"),
        "expires": values.get("expires"),
        "contact": values.get("contact"),
        "payload": payload,
    }


class PairSuccessfulRegistrationsTests(unittest.TestCase):
    def test_reused_session_keeps_each_events_own_access_info(self):
        first = datetime.datetime(2026, 9, 24, 9, 52, 36, 788988, tzinfo=UTC)
        later = datetime.datetime(2026, 9, 26, 10, 0, 0, tzinfo=UTC)
        rows = [
            row(
                first,
                "REGISTER sip:ims.example SIP/2.0\r\n"
                "CSeq: 354 REGISTER\r\n"
                "P-Access-Network-Info: 3GPP-E-UTRAN-FDD;"
                "utran-cell-id-3gpp=228010e17124f404\r\n",
            ),
            # An authentication retry may omit P-ANI. The request from this
            # exchange that carries it must still be selected.
            row(
                first + datetime.timedelta(milliseconds=300),
                "REGISTER sip:ims.example SIP/2.0\r\nCSeq: 356 REGISTER\r\n",
            ),
            row(
                first + datetime.timedelta(milliseconds=545),
                "SIP/2.0 200 OK\r\nCSeq: 356 REGISTER\r\n",
                expires="86400",
            ),
            row(
                later,
                "REGISTER sip:ims.example SIP/2.0\r\n"
                "CSeq: 410 REGISTER\r\n"
                "P-Access-Network-Info: 3GPP-E-UTRAN-FDD;"
                "utran-cell-id-3gpp=22801057e11ede03\r\n",
            ),
            row(
                later + datetime.timedelta(milliseconds=500),
                "SIP/2.0 200 OK\r\nCSeq: 410 REGISTER\r\n",
                expires="86400",
            ),
        ]

        events = pair_successful_registrations(reversed(rows), SUBSCRIBER)

        self.assertEqual(len(events), 2)
        self.assertIn("228010e17124f404", events[0]["request"]["payload"])
        self.assertIn("22801057e11ede03", events[1]["request"]["payload"])

    def test_does_not_pair_response_outside_exchange_window(self):
        request_time = datetime.datetime(2026, 9, 24, 9, 52, tzinfo=UTC)
        rows = [
            row(
                request_time,
                "REGISTER sip:ims.example SIP/2.0\r\nCSeq: 1 REGISTER\r\n",
            ),
            row(
                request_time + datetime.timedelta(seconds=11),
                "SIP/2.0 200 OK\r\nCSeq: 1 REGISTER\r\n",
                expires="86400",
            ),
        ]

        self.assertEqual(pair_successful_registrations(rows, SUBSCRIBER), [])


if __name__ == "__main__":
    unittest.main()
