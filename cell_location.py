from dataclasses import dataclass
import re


_EUTRAN_LAYOUTS = {
    # total length: (MNC decimal digits, TAC hexadecimal digits)
    16: (2, 4),   # EPC, two-digit MNC
    17: (3, 4),   # EPC, three-digit MNC
    18: (2, 6),   # 5GC, two-digit MNC
    19: (3, 6),   # 5GC, three-digit MNC
}


@dataclass(frozen=True)
class EutranCell:
    """Decoded 3GPP E-UTRAN P-Access-Network-Info cell identity."""

    mcc: int
    mnc: int
    tac: int
    eci: int
    enb_id: int
    cell_id: int
    tac_hex: str
    eci_hex: str


def decode_eutran_cell(value):
    """Decode MCC + MNC + TAC + ECI from an E-UTRAN P-ANI value.

    TS 24.229 encodes this field as ASCII, not as packed BCD bytes. The total
    length identifies whether the MNC has two or three decimal digits and
    whether the TAC is the four-digit EPC or six-digit 5GC form.
    """
    value = (value or "").strip()
    layout = _EUTRAN_LAYOUTS.get(len(value))
    if layout is None:
        raise ValueError(
            "E-UTRAN cell value must be 16, 17, 18, or 19 characters"
        )

    mnc_len, tac_len = layout
    decimal_len = 3 + mnc_len
    expected = rf"[0-9]{{{decimal_len}}}[0-9A-Fa-f]{{{tac_len + 7}}}"
    if not re.fullmatch(expected, value):
        raise ValueError("E-UTRAN cell value contains invalid digits")

    tac_start = decimal_len
    eci_start = tac_start + tac_len
    tac_hex = value[tac_start:eci_start]
    eci_hex = value[eci_start:]
    eci = int(eci_hex, 16)

    return EutranCell(
        mcc=int(value[:3]),
        mnc=int(value[3:decimal_len]),
        tac=int(tac_hex, 16),
        eci=eci,
        enb_id=eci >> 8,
        cell_id=eci & 0xFF,
        tac_hex=tac_hex.lower(),
        eci_hex=eci_hex.lower(),
    )


def google_lte_payload(mcc, mnc, tac, eci):
    """Build a Google Geolocation API request for one LTE serving cell."""
    if not 0 <= mcc <= 999:
        raise ValueError("MCC must be between 0 and 999")
    if not 0 <= mnc <= 999:
        raise ValueError("MNC must be between 0 and 999")
    if not 0 <= tac <= 0xFFFFFF:
        raise ValueError("LTE TAC must fit in 24 bits")
    if not 0 <= eci <= 0x0FFFFFFF:
        raise ValueError("LTE ECI must fit in 28 bits")
    tower = {
        "mobileCountryCode": mcc,
        "mobileNetworkCode": mnc,
        "cellId": eci,
    }
    # Google documents a 16-bit LTE TAC. E-UTRAN attached to a 5GC can carry
    # a 24-bit TAC, so omit that optional field rather than send an invalid one.
    if tac <= 0xFFFF:
        tower["locationAreaCode"] = tac
    return {
        "radioType": "lte",
        "considerIp": False,
        "cellTowers": [tower],
    }
