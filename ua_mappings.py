import re

SAMSUNG_MODELS = {
    # S25 series – launched Android 15
    "S931B": ("Galaxy S25",       15),
    "S936B": ("Galaxy S25+",      15),
    "S938B": ("Galaxy S25 Ultra", 15),
    # S24 series – launched Android 14
    "S921B": ("Galaxy S24",       14),
    "S926B": ("Galaxy S24+",      14),
    "S928B": ("Galaxy S24 Ultra", 14),
    # S23 series – launched Android 13
    "S911B": ("Galaxy S23",       13),
    "S916B": ("Galaxy S23+",      13),
    "S918B": ("Galaxy S23 Ultra", 13),
    # S22 series – launched Android 12
    "S901B": ("Galaxy S22",       12),
    "S906B": ("Galaxy S22+",      12),
    "S908B": ("Galaxy S22 Ultra", 12),
    # A-series
    "A566B": ("Galaxy A56",  15),
    "A556B": ("Galaxy A55",  14),
    "A546B": ("Galaxy A54",  13),
    "A536B": ("Galaxy A53",  12),
    "A336B": ("Galaxy A33",  12),
    # Z Fold/Flip
    "F956B": ("Galaxy Z Fold6", 14),
    "F946B": ("Galaxy Z Fold5", 13),
    "F936B": ("Galaxy Z Fold4", 12),
    "F741B": ("Galaxy Z Flip6", 14),
    "F731B": ("Galaxy Z Flip5", 13),
    "F721B": ("Galaxy Z Flip4", 12),
}

def _samsung_android(model_code: str, firmware: str) -> str:
    info = SAMSUNG_MODELS.get(model_code)
    if not info:
        for prefix, val in SAMSUNG_MODELS.items():
            if model_code.startswith(prefix[:4]):
                info = val
                break
    if not info:
        return "Android (unknown)"
    device_name, launch_android = info
    build = re.sub(r'^[A-Z0-9]+XX[A-Z]?', '', firmware)
    android_ver = launch_android
    if len(build) >= 4:
        letter = build[-4].upper()
        if letter.isalpha():
            offset = ord(letter) - ord('A')
            major_bumps = round(offset / 3) if offset > 3 else (offset // 2)
            android_ver = launch_android + major_bumps
    return f"{device_name} \u2014 Android {android_ver}"

def get_android_version(ua: str) -> str:
    if not ua:
        return ""
    # Google Pixel / generic Android
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
    m = re.match(r'SM-([A-Z0-9]+)-([A-Z0-9]+) Samsung IMS', ua)
    if m:
        return _samsung_android(m.group(1), m.group(2))
    # iOS
    m = re.search(r'iOS/(\d+)', ua)
    if m:
        return f"iOS {m.group(1)}"
    return ""

