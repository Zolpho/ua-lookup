import unittest

from cell_location import decode_eutran_cell, google_lte_payload


class DecodeEutranCellTests(unittest.TestCase):
    def test_decodes_swisscom_pani_value(self):
        cell = decode_eutran_cell("2280102be11ca605")

        self.assertEqual(cell.mcc, 228)
        self.assertEqual(cell.mnc, 1)
        self.assertEqual(cell.tac, 702)
        self.assertEqual(cell.eci, 18_654_725)
        self.assertEqual(cell.enb_id, 72_870)
        self.assertEqual(cell.cell_id, 5)
        self.assertEqual(cell.tac_hex, "02be")
        self.assertEqual(cell.eci_hex, "11ca605")

    def test_decodes_winterthur_registration_value(self):
        cell = decode_eutran_cell("228010e17124f404")

        self.assertEqual(cell.mcc, 228)
        self.assertEqual(cell.mnc, 1)
        self.assertEqual(cell.tac, 3607)
        self.assertEqual(cell.eci, 19_198_980)
        self.assertEqual(cell.enb_id, 74_996)
        self.assertEqual(cell.cell_id, 4)

    def test_decodes_3gpp_two_digit_mnc_example(self):
        cell = decode_eutran_cell("1112233C476B4321")

        self.assertEqual(cell.mcc, 111)
        self.assertEqual(cell.mnc, 22)
        self.assertEqual(cell.tac, 0x33C4)
        self.assertEqual(cell.eci, 0x76B4321)

    def test_decodes_three_digit_mnc(self):
        cell = decode_eutran_cell("31026012340abcde1")

        self.assertEqual(cell.mcc, 310)
        self.assertEqual(cell.mnc, 260)
        self.assertEqual(cell.tac, 0x1234)
        self.assertEqual(cell.eci, 0x0ABCDE1)

    def test_decodes_24_bit_5gc_tac(self):
        cell = decode_eutran_cell("22801abcdef11ca605")

        self.assertEqual(cell.mcc, 228)
        self.assertEqual(cell.mnc, 1)
        self.assertEqual(cell.tac, 0xABCDEF)
        self.assertEqual(cell.eci, 0x11CA605)

    def test_rejects_unsupported_length(self):
        with self.assertRaisesRegex(ValueError, "16, 17, 18, or 19"):
            decode_eutran_cell("2280111ca605")

    def test_rejects_non_decimal_mcc_or_mnc(self):
        with self.assertRaisesRegex(ValueError, "invalid digits"):
            decode_eutran_cell("22a0102be11ca605")


class GoogleLtePayloadTests(unittest.TestCase):
    def test_uses_full_eci_and_correct_tac(self):
        cell = decode_eutran_cell("2280102be11ca605")

        payload = google_lte_payload(cell.mcc, cell.mnc, cell.tac, cell.eci)

        self.assertEqual(
            payload,
            {
                "radioType": "lte",
                "considerIp": False,
                "cellTowers": [
                    {
                        "mobileCountryCode": 228,
                        "mobileNetworkCode": 1,
                        "cellId": 18_654_725,
                        "locationAreaCode": 702,
                    }
                ],
            },
        )

    def test_omits_unsupported_24_bit_lte_tac(self):
        payload = google_lte_payload(228, 1, 0xABCDEF, 0x11CA605)

        self.assertNotIn("locationAreaCode", payload["cellTowers"][0])

    def test_rejects_eci_larger_than_28_bits(self):
        with self.assertRaisesRegex(ValueError, "28 bits"):
            google_lte_payload(228, 1, 702, 0x10000000)


if __name__ == "__main__":
    unittest.main()
