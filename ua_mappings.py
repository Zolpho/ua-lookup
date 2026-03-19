import re

SAMSUNG_MODELS = {
    # S25 series - launched Android 15
    "S931B":  ("Galaxy S25",       15),
    "S936B":  ("Galaxy S25+",      15),
    "S938B":  ("Galaxy S25 Ultra", 15),
    # S24 series - launched Android 14
    "S921B":  ("Galaxy S24",       14),
    "S926B":  ("Galaxy S24+",      14),
    "S928B":  ("Galaxy S24 Ultra", 14),
    # S23 series - launched Android 13
    "S911B":  ("Galaxy S23",       13),
    "S916B":  ("Galaxy S23+",      13),
    "S918B":  ("Galaxy S23 Ultra", 13),
    # S23 US variants
    "S911U":  ("Galaxy S23",       13),
    "S911U1": ("Galaxy S23",       13),
    "S916U":  ("Galaxy S23+",      13),
    "S916U1": ("Galaxy S23+",      13),
    "S918U":  ("Galaxy S23 Ultra", 13),
    "S918U1": ("Galaxy S23 Ultra", 13),
    # S22 series - launched Android 12
    "S901B":  ("Galaxy S22",       12),
    "S906B":  ("Galaxy S22+",      12),
    "S908B":  ("Galaxy S22 Ultra", 12),
    # A-series
    "A566B":  ("Galaxy A56",  15),
    "A556B":  ("Galaxy A55",  14),
    "A546B":  ("Galaxy A54",  13),
    "A536B":  ("Galaxy A53",  12),
    "A336B":  ("Galaxy A33",  12),
    # Z Fold/Flip
    "F956B":  ("Galaxy Z Fold6", 14),
    "F946B":  ("Galaxy Z Fold5", 13),
    "F936B":  ("Galaxy Z Fold4", 12),
    "F741B":  ("Galaxy Z Flip6", 14),
    "F731B":  ("Galaxy Z Flip5", 13),
    "F721B":  ("Galaxy Z Flip4", 12),
}

# Samsung firmware Android version letter → offset from launch version
# Ground-truth from doc.samsungmobile.com / sammobile.com:
#   A = launch (offset 0)
#   B = +0 (same major, minor QPR)
#   C = +1
#   D = +2
#   E = +3
#   F = +4  (extrapolated)
SAMSUNG_ANDROID_OFFSET = {
    "A": 0, "B": 0, "C": 1, "D": 2, "E": 3, "F": 4, "G": 5,
}

# Xiaomi HyperOS firmware first letter of 7-letter suffix → Android version
# Ground-truth: WOSEUXM (Xiaomi 15T Pro, Android 16), VOSEUXM (Xiaomi 14T, Android 15)
XIAOMI_ANDROID_LETTER = {
    "S": 12, "T": 13, "U": 14, "V": 15, "W": 16, "X": 17,
}


def _samsung_android(model_code: str, firmware: str) -> str:
    # Lookup: try exact match, then 6-char, 5-char, 4-char prefix
    info = SAMSUNG_MODELS.get(model_code)
    if not info:
        for length in (6, 5, 4):
            for prefix, val in SAMSUNG_MODELS.items():
                if model_code.startswith(prefix[:length]) and len(prefix) >= length:
                    info = val
                    break
            if info:
                break
    if not info:
        return "Android (unknown)"

    device_name, launch_android = info

    # Extract Android letter using year marker (X=2024, Y=2025, Z=2026, ...)
    # Samsung build format: ...[ANDROID][YEAR(X/Y/Z)][MONTH(A-L)][BUILD]
    m = re.search(r'([A-Z])([W-Z])([A-L])[A-Z0-9]+$', firmware)
    if m:
        android_letter = m.group(1).upper()
        offset = SAMSUNG_ANDROID_OFFSET.get(android_letter, 0)
        android_ver = launch_android + offset
    else:
        android_ver = launch_android

    return f"{device_name} \u2014 Android {android_ver}"


def _xiaomi_android(brand: str, device: str, firmware: str) -> str:
    """
    Decode Android version from Xiaomi/Redmi/POCO HyperOS firmware string.
    Format: OS{major}.{minor}.{patch}.0.{7-letter-code}
    The first letter of the 7-letter suffix encodes the Android base version.
    Example: OS3.0.13.0.WOSEUXM -> W = Android 16
    """
    m = re.search(r'\.([A-Z])[A-Z]{6}$', firmware)
    if m:
        android_ver = XIAOMI_ANDROID_LETTER.get(m.group(1).upper(), "?")
    else:
        android_ver = "?"
    return f"{brand} {device} \u2014 Android {android_ver}"


def get_android_version(ua: str) -> str:
    if not ua:
        return ""

    # Xiaomi / Redmi / POCO HyperOS:
    # "Xiaomi_Xiaomi 15T Pro_OS3.0.13.0.WOSEUXM"
    # "Redmi_Redmi Note 14 Pro_OS3.0.6.0.WOROEUXM"
    # "POCO_POCO F7 Pro_OS3.0.4.0.WOPOEUXM"
    m = re.match(r'(Xiaomi|Redmi|POCO)_([^_]+)_(OS\d+\.\d+\.\d+\.\d+\.[A-Z]{7})', ua, re.IGNORECASE)
    if m:
        return _xiaomi_android(m.group(1), m.group(2), m.group(3))

    # Google Pixel / generic Android: "Google_Pixel8_Android 16_BP4A.260205.001"
    m = re.search(r'Android[_ ](\d+)', ua, re.IGNORECASE)
    if m:
        dev = re.match(r'[A-Za-z]+_([^_]+)_Android', ua)
        device = dev.group(1) if dev else ""
        return f"{device} \u2014 Android {m.group(1)}" if device else f"Android {m.group(1)}"

    # Fairphone: "Fairphone_Fairphone 6_FP6.QREL.15.151.0"
    m = re.search(r'FP\d+\.QREL\.(\d+)\.', ua)
    if m:
        dev = re.match(r'[A-Za-z]+_([^_]+)_FP', ua)
        device = dev.group(1) if dev else "Fairphone"
        return f"{device} \u2014 Android {m.group(1)}"

    # Samsung IMS: "SM-S936B-S936BXXS7BYLR Samsung IMS 6.0"
    #              "SM-S916U1-S916U1UES7EZB6 Samsung IMS 6.0"
    m = re.match(r'SM-([A-Z0-9]+)-([A-Z0-9]+) Samsung IMS', ua)
    if m:
        return _samsung_android(m.group(1), m.group(2))

    # iOS: "iOS/26.3 iPhone"
    m = re.search(r'iOS/(\d+)', ua)
    if m:
        return f"iOS {m.group(1)}"

    return ""


if __name__ == "__main__":
    tests = [
        # Xiaomi / Redmi / POCO
        ("Xiaomi_Xiaomi 15T Pro_OS3.0.13.0.WOSEUXM",   "Xiaomi Xiaomi 15T Pro \u2014 Android 16"),
        ("Xiaomi_Xiaomi 14T_OS2.0.208.0.VOSEUXM",       "Xiaomi Xiaomi 14T \u2014 Android 15"),
        ("Redmi_Redmi Note 14 Pro_OS3.0.6.0.WOROEUXM",  "Redmi Redmi Note 14 Pro \u2014 Android 16"),
        ("POCO_POCO F7 Pro_OS3.0.4.0.WOPOEUXM",         "POCO POCO F7 Pro \u2014 Android 16"),
        # Samsung EU
        ("SM-S936B-S936BXXS7BYLR Samsung IMS 6.0",      "Galaxy S25+ \u2014 Android 15"),
        ("SM-S921B-S921BXXSCCZA1 Samsung IMS 6.0",      "Galaxy S24 \u2014 Android 15"),
        # Samsung US (the failing case)
        ("SM-S916U1-S916U1UES7EZB6 Samsung IMS 6.0",   "Galaxy S23+ \u2014 Android 16"),
        ("SM-S916U1-S916U1UES6DYI3 Samsung IMS 6.0",   "Galaxy S23+ \u2014 Android 15"),
        # Generic / Fairphone / iOS
        ("Google_Pixel8_Android 16_BP4A.260205.001",    "Pixel8 \u2014 Android 16"),
        ("Fairphone_Fairphone 6_FP6.QREL.15.151.0",    "Fairphone 6 \u2014 Android 15"),
        ("iOS/26.3 iPhone",                              "iOS 26"),
    ]
    all_ok = True
    for ua, expected in tests:
        result = get_android_version(ua)
        status = "OK  " if result == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"{status}: {ua!r}\n      -> {result!r}  (expected: {expected!r})\n")
    print("All tests passed!" if all_ok else "Some tests FAILED.")

