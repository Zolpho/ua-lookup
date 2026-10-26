from flask import Flask, request, render_template_string, jsonify
import logging
import re
import json as _json
import time
import urllib.error
import urllib.parse
import urllib.request
import subprocess
import os
import datetime
import duckdb
from ua_mappings import get_android_version
from cell_location import decode_eutran_cell
from registration_history import pair_successful_registrations

def _require_env(key):
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key} (set it in /opt/ua-lookup/config.env)")
    return val

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/ua-lookup.log")
    ]
)
log = logging.getLogger(__name__)
app = Flask(__name__)

GOOGLE_API_KEY   = _require_env("GOOGLE_API_KEY")
DUCKLAKE_CATALOG = os.environ.get("DUCKLAKE_CATALOG", "/data/homer/homer_catalog.sqlite")
DUCKLAKE_DATA    = os.environ.get("DUCKLAKE_DATA",    "/data/homer/parquet")

def _get_duck():
    con = duckdb.connect(config={"home_directory": "/tmp"})
    con.execute("LOAD ducklake")
    con.execute(f"""
        ATTACH 'ducklake:sqlite:{DUCKLAKE_CATALOG}'
        AS homer_lake (DATA_PATH '{DUCKLAKE_DATA}', READ_ONLY TRUE)
    """)
    return con

UCN_NODES = [n.strip() for n in os.environ.get("UCN_NODES", "").split(",") if n.strip()]
UCN_USER  = os.environ.get("UCN_USER", "root")
UCN_DB    = os.environ.get("UCN_DB", "/etc/yate/ucn/ucn.db")
UCN_HOSTNAMES = {
    pair.split(":", 1)[0].strip(): pair.split(":", 1)[1].strip()
    for pair in os.environ.get("UCN_HOSTNAMES", "").split(",")
    if ":" in pair
}

UA_EXCLUDE = re.compile(r"YATE|OpenSIPS|dispatcher|nx-sbc-ocs|NexSBC|VoLTE/WFC", re.IGNORECASE)
_google_cache: dict = {}
GOOGLE_SUCCESS_CACHE_TTL_SEC = int(
    os.environ.get("GOOGLE_SUCCESS_CACHE_TTL_SEC", "86400")
)
GOOGLE_FAILURE_CACHE_TTL_SEC = int(
    os.environ.get("GOOGLE_FAILURE_CACHE_TTL_SEC", "300")
)

RAT_TYPE_MAP = {
    "0": "ANY", "1": "UTRAN", "2": "GERAN", "3": "WLAN", "4": "GAN",
    "5": "HSPA", "6": "EUTRAN", "7": "VIRTUAL", "8": "NB-IoT",
    "9": "NRTRAN", "10": "NR",
}

_UCN_SCRIPT = (
    "import sqlite3,json,sys\n"
    "DB=sys.argv[1]; q=sys.argv[2]\n"
    "try:\n"
    " con=sqlite3.connect(\"file:\"+DB+\"?mode=ro\",uri=True)\n"
    " con.row_factory=sqlite3.Row\n"
    " existing=set(c[1] for c in con.execute(\"PRAGMA table_info(cs_regs)\").fetchall())\n"
    " def col(name,expr=None):\n"
    "  return (expr or (\"r.\"+name)) if name in existing else (\"NULL AS \"+name)\n"
    " sql=\"\"\"\n"
    "  SELECT c.imsi, c.msisdn,\n"
    "   COALESCE(c.hlr,c.diam_host,c.diam_realm) AS hss,\n"
    "   COALESCE(r.temploc,r.location) AS pcscf,\n"
    "   r.imei, r.cell_split, r.cell_type,\n"
    "   r.rip, r.rport_s,\n"
    "   DATETIME(r.expires) AS expires,\n"
    "   r.reg_interval,\n"
    "   {time_created},\n"
    "   {time_updated},\n"
    "   {type}\n"
    "  FROM cs_core c\n"
    "  LEFT JOIN cs_regs r ON r.imsi=c.imsi\n"
    "  WHERE c.msisdn=? OR c.imsi LIKE ?||'@%' OR c.imsi_detected=?\n"
    "  LIMIT 1\"\"\"\n"
    " row=con.execute(sql.format(\n"
    "  time_created=col('time_created',\"DATETIME(r.time_created,'unixepoch') AS time_created\"),\n"
    "  time_updated=col('time_updated',\"DATETIME(r.time_updated,'unixepoch') AS time_updated\"),\n"
    "  type=col('type')\n"
    " ), (q,q,q)).fetchone()\n"
    " print(json.dumps(dict(row)) if row else json.dumps({\"error\":\"not found\"}))\n"
    " con.close()\n"
    "except Exception as e:\n"
    " print(json.dumps({\"error\":str(e)}))\n"
)


def _resolve_cell_from_vlr(cell_split, cell_type):
    if not cell_split:
        return {"label": "-", "url": ""}

    parts = cell_split.split("/")
    if len(parts) != 4:
        return {"label": cell_split, "url": ""}

    try:
        mcc = int(parts[0])
        mnc = int(parts[1])
        tac = int(parts[2], 16)
        ecgi = int(parts[3], 16)
        enb = ecgi >> 8
        cid = ecgi & 0xFF

        label = f"{cell_type or 'e-utran'} eNB:{enb} cell:{cid}"
        location = _google_lookup(mcc, mnc, tac, ecgi)
        url = _cell_map_url(
            location,
            mcc=mcc,
            mnc=mnc,
            tac=tac,
            eci=ecgi,
            enb_id=enb,
            cell_id=cid,
        )
        return {"label": label, "url": url}
    except Exception as exc:
        log.warning(
            "Could not resolve VLR cell_split=%r type=%r: %s",
            cell_split, cell_type, exc,
        )
        return {"label": cell_split, "url": ""}

def vlr_lookup(query):
    if not UCN_NODES:
        return None
    q = query.lstrip("+")
    for node in UCN_NODES:
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=no",
                 "-o", "BatchMode=yes", f"{UCN_USER}@{node}",
                 f"python3 - {UCN_DB} {q}"],
                input=_UCN_SCRIPT, capture_output=True, text=True, timeout=30
            )
            json_lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
            if not json_lines:
                continue
            data = _json.loads(json_lines[-1])
            if "error" in data:
                log.debug("UCN %s: %s (q=%s)", node, data["error"], q)
                continue
            if data.get("imsi") and "@" in str(data["imsi"]):
                data["imsi"] = data["imsi"].split("@")[0]
            pcscf = data.get("pcscf") or ""
            data["pcscf"] = re.sub(r"^sip/sip:[^@]+@", "", pcscf)
            cell = _resolve_cell_from_vlr(data.get("cell_split"), data.get("cell_type"))
            data["cell_label"] = cell["label"]
            data["cell_url"]   = cell["url"]
            hostname = UCN_HOSTNAMES.get(node, node)
            data["ucn_node"]       = node
            data["ucn_node_label"] = f"{hostname} ({node})"
            log.info("UCN hit for %s on %s IMEI=%s", q, node, data.get("imei"))
            return data
        except Exception as exc:
            log.warning("UCN query failed node=%s q=%s: %s", node, q, exc)
    return None

def bearer_lookup(query):
    if not UCN_NODES:
        return []
    q = query.lstrip("+")
    results = []
    sql = (
        "SELECT b.msisdn,b.imsi,b.ipv4,b.address,"
        "b.apn,b.rat_type,b.component,"
        "r.location,r.cell_type,"
        "datetime(b.time_created/1000,'unixepoch') "
        "FROM bearers b "
        "LEFT JOIN cs_regs r ON r.imsi LIKE '%'||b.imsi||'%' "
        f"WHERE b.msisdn='{q}'"
    )
    for node in UCN_NODES:
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=no",
                 "-o", "BatchMode=yes", f"{UCN_USER}@{node}",
                 f"sqlite3 {UCN_DB} \"{sql}\""],
                capture_output=True, text=True, timeout=10
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) < 10:
                    continue
                ims_raw = parts[7].strip()
                ims_ip  = re.sub(r"^sip/sip:[^@]+@", "", ims_raw)
                ims_ip  = ims_ip.split(":")[0] if ims_ip else "-"
                rat_raw   = parts[5].strip()
                rat_label = RAT_TYPE_MAP.get(rat_raw, rat_raw) if rat_raw else "-"
                hostname  = UCN_HOSTNAMES.get(node, node)
                results.append({
                    "node":          f"{hostname} ({node})",
                    "msisdn":        parts[0].strip(),
                    "imsi":          parts[1].strip(),
                    "internet_ip":   parts[2].strip() or "-",
                    "sgw_ip":        parts[3].strip() or "-",
                    "apn":           parts[4].strip() or "-",
                    "rat_type":      rat_label,
                    "component":     parts[6].strip() or "-",
                    "ims_ip":        ims_ip or "-",
                    "cell_type":     parts[8].strip() or "-",
                    "session_start": parts[9].strip() or "-",
                })
        except Exception as exc:
            log.warning("Bearer query failed node=%s q=%s: %s", node, q, exc)
    return results

def _decode_plmn_ecgi(cell_value):
    """Decode ASCII MCC+MNC+TAC+ECI from an E-UTRAN P-ANI value."""
    try:
        cell = decode_eutran_cell(cell_value)
    except ValueError as exc:
        log.warning(
            "Invalid E-UTRAN P-ANI value %r: %s",
            cell_value,
            exc,
        )
        return None
    return (
        cell.mcc,
        cell.mnc,
        cell.tac,
        cell.enb_id,
        cell.cell_id,
        cell.eci,
    )



def _google_lookup(mcc, mnc, tac, ecgi):
    """Return Google's estimated device position and uncertainty radius."""
    key = (mcc, mnc, tac, ecgi)
    now = time.monotonic()

    cached = _google_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    result = None
    try:
        payload = _json.dumps({
            "radioType": "lte",
            "considerIp": False,
            "cellTowers": [{
                "mobileCountryCode": mcc,
                "mobileNetworkCode": mnc,
                "locationAreaCode": tac,
                "cellId": ecgi,
            }],
        }).encode()

        req = urllib.request.Request(
            "https://www.googleapis.com/geolocation/v1/geolocate"
            f"?key={GOOGLE_API_KEY}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=4) as resp:
            data = _json.loads(resp.read())

        if "location" in data:
            accuracy = data.get("accuracy")
            result = {
                "lat": float(data["location"]["lat"]),
                "lng": float(data["location"]["lng"]),
                "accuracy_m": (
                    float(accuracy) if accuracy is not None else None
                ),
            }
            log.info(
                "Google cell MCC=%d MNC=%d TAC=%d ECI=%d "
                "-> %.6f,%.6f accuracy=%s m",
                mcc,
                mnc,
                tac,
                ecgi,
                result["lat"],
                result["lng"],
                (
                    f'{result["accuracy_m"]:.3f}'
                    if result["accuracy_m"] is not None
                    else "unknown"
                ),
            )
        else:
            log.info(
                "Google cell MCC=%d MNC=%d TAC=%d ECI=%d -> no match: %s",
                mcc, mnc, tac, ecgi, data,
            )

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        log.warning(
            "Google cell lookup failed MCC=%d MNC=%d TAC=%d ECI=%d: "
            "HTTP %d %s",
            mcc, mnc, tac, ecgi, exc.code, body,
        )
    except Exception as exc:
        log.warning(
            "Google cell lookup failed MCC=%d MNC=%d TAC=%d ECI=%d: %s",
            mcc, mnc, tac, ecgi, exc,
        )

    ttl = (
        GOOGLE_SUCCESS_CACHE_TTL_SEC
        if result is not None
        else GOOGLE_FAILURE_CACHE_TTL_SEC
    )

    if len(_google_cache) >= 4096:
        _google_cache.clear()

    _google_cache[key] = (now + ttl, result)
    return result


def _cell_map_url(
    location,
    *,
    mcc,
    mnc,
    tac,
    eci,
    enb_id,
    cell_id,
):
    if not location:
        return ""

    params = {
        "lat": location["lat"],
        "lng": location["lng"],
        "accuracy": (
            location["accuracy_m"]
            if location.get("accuracy_m") is not None
            else 0
        ),
        "mcc": mcc,
        "mnc": mnc,
        "tac": tac,
        "eci": eci,
        "enb": enb_id,
        "cell": cell_id,
    }
    return "/cell-map?" + urllib.parse.urlencode(params)


def _wgs84_to_lv95(lng, lat):
    """Approximate WGS84 to Swiss LV95 conversion for map deep links."""
    lat_aux = (lat * 3600.0 - 169028.66) / 10000.0
    lng_aux = (lng * 3600.0 - 26782.5) / 10000.0

    east = (
        2600072.37
        + 211455.93 * lng_aux
        - 10938.51 * lng_aux * lat_aux
        - 0.36 * lng_aux * lat_aux ** 2
        - 44.54 * lng_aux ** 3
    )
    north = (
        1200147.07
        + 308807.95 * lat_aux
        + 3745.25 * lng_aux ** 2
        + 76.63 * lat_aux ** 2
        - 194.56 * lng_aux ** 2 * lat_aux
        + 119.79 * lat_aux ** 3
    )
    return east, north


def resolve_cell(raw_header):
    if not raw_header:
        return {"label": "-", "url": ""}

    at = raw_header.split(";")[0].strip()
    at_short = (
        at.replace("3GPP-", "")
        .replace("-FDD", "")
        .replace("-TDD", "")
    )

    # Preserve the existing Wi-Fi Calling presentation.
    if "IEEE-802.11" in raw_header or "i-wlan-node-id" in raw_header:
        match = re.search(
            r"i-wlan-node-id=([0-9a-fA-F:]+)",
            raw_header,
            re.IGNORECASE,
        )
        bssid = match.group(1) if match else "unknown"
        if re.fullmatch(r"f+", bssid.replace(":", "")):
            return {"label": "📶 WiFi Calling", "url": ""}
        return {"label": f"📶 WiFi {bssid}", "url": ""}

    # Do not submit non-LTE P-ANI values as LTE towers.
    if at.upper() not in {
        "3GPP-E-UTRAN-FDD",
        "3GPP-E-UTRAN-TDD",
    }:
        return {
            "label": at_short or raw_header,
            "url": "",
        }

    match = re.search(
        r"utran-cell-id-3gpp=([0-9a-fA-F]+)",
        raw_header,
        re.IGNORECASE,
    )
    if not match:
        return {"label": at_short or raw_header, "url": ""}

    parsed = _decode_plmn_ecgi(match.group(1))
    if not parsed:
        return {"label": at_short, "url": ""}

    mcc, mnc, tac, enb_id, cell_id, ecgi = parsed
    label = f"{at_short} eNB:{enb_id} cell:{cell_id}"
    location = _google_lookup(mcc, mnc, tac, ecgi)
    url = _cell_map_url(
        location,
        mcc=mcc,
        mnc=mnc,
        tac=tac,
        eci=ecgi,
        enb_id=enb_id,
        cell_id=cell_id,
    )
    return {"label": label, "url": url}

def _fmt_gap(seconds):
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    h = seconds // 3600; m = (seconds % 3600) // 60
    return f"{h}h {m}m" if m else f"{h}h"

def _classify_event(row, prev, backward_secs):
    if prev is None:
        return "❓ First in window", "ev-unknown"
    badges = []
    if prev["status"] == "UNREGISTERED":
        if backward_secs < 90:
            badges.append(("🔄 Reboot", "ev-reboot"))
        else:
            badges.append(("✈️ Airplane / Off", "ev-airplane"))
    else:
        try:    expires_val = int(prev["expires"])
        except: expires_val = 0
        if expires_val > 0 and abs(backward_secs - expires_val) / expires_val <= 0.25:
            badges.append(("🔁 Periodic", "ev-periodic"))
        elif backward_secs > 300:
            badges.append(("📵 Coverage loss", "ev-coverage"))
    if (prev.get("user_agent") and prev["user_agent"] != "-"
            and row["user_agent"] != prev["user_agent"]):
        badges.append(("🆕 UA changed", "ev-ua"))
    pc = prev.get("cell_label") or "-"
    tc = row.get("cell_label") or "-"
    if pc not in ("-", "") and tc not in ("-", "") and pc != tc:
        badges.append(("📍 Cell changed", "ev-cell"))
    if not badges:
        return "", ""
    return " · ".join(b[0] for b in badges), badges[0][1]

def _add_gaps_and_classify(rows):
    n = len(rows)
    for i, row in enumerate(rows):
        if i > 0:
            fwd_secs = int((rows[i-1]["create_date"] - row["create_date"]).total_seconds())
            row["gap"] = _fmt_gap(fwd_secs) if fwd_secs >= 0 else ""
        else:
            row["gap"] = ""
        if row["status"] == "REGISTERED":
            prev     = rows[i+1] if i+1 < n else None
            bwd_secs = int((row["create_date"] - prev["create_date"]).total_seconds()) \
                       if prev is not None else 0
            row["event"], row["event_class"] = _classify_event(row, prev, bwd_secs)
        else:
            row["event"] = ""
            row["event_class"] = ""
    for row in rows:
        row["create_date"] = str(row["create_date"])
    return rows

def _hdr(payload: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\s*:\s*(.+)$", payload, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""

def ua_lookup(msisdn: str, days: int = 1) -> list:
    q = msisdn.lstrip("+")
    pat = f"%{q}%"
    sql = f"""
        SELECT timestamp, caller, callee, src_ip, method,
               data_extra, payload
        FROM homer_lake.main.hep_proto_1_call
        WHERE timestamp >= NOW() - INTERVAL '{days} days'
          AND (caller LIKE ? OR callee LIKE ? OR payload LIKE ?)
          AND method IN ('INVITE', 'BYE', 'UPDATE')
        ORDER BY timestamp DESC
        LIMIT 2000
    """
    con = _get_duck()
    try:
        rows = con.execute(sql, [pat, pat, pat]).fetchall()
    finally:
        con.close()
    cols = ["timestamp", "caller", "callee", "src_ip", "method", "data_extra", "payload"]

    out, seen = [], set()
    for raw in rows:
        r = dict(zip(cols, raw))
        caller  = (r.get("caller") or "")
        callee  = (r.get("callee") or "")
        payload = (r.get("payload") or "")
        if q not in caller and q not in callee and q not in payload:
            continue
        ts  = str(r["timestamp"])
        key = (ts, r.get("src_ip"), caller, callee)
        if key in seen:
            continue
        seen.add(key)
        de = r.get("data_extra") or {}
        if isinstance(de, str):
            try: de = _json.loads(de)
            except: de = {}
        ua = (de.get("user_agent") or "").strip()
        if not ua:
            ua = _hdr(payload, "User-Agent")
        if not ua or UA_EXCLUDE.search(ua):
            continue
        pan  = _hdr(payload, "P-Access-Network-Info")
        cell = resolve_cell(pan)
        out.append({
            "create_date": ts,
            "user_agent":  ua,
            "os_version":  get_android_version(ua),
            "from_user":   caller,
            "to_user":     callee,
            "src_ip":      r.get("src_ip", ""),
            "method":      r.get("method", ""),
            "cell_label":  cell["label"],
            "cell_url":    cell["url"],
        })
    return out

def _msisdn_to_imsi(msisdn: str) -> str:
    """Try to find the IMSI for a MSISDN by querying the aor index in homer."""
    q = msisdn.lstrip("+")
    pat = f"%{q}%"
    try:
        con = _get_duck()
        try:
            row = con.execute("""
                SELECT aor FROM homer_lake.main.hep_proto_1_registration
                WHERE aor IS NOT NULL AND payload LIKE ?
                LIMIT 1
            """, [pat]).fetchone()
        finally:
            con.close()
        if row and row[0]:
            # aor is like 228580015006014@ims.mnc058...
            return row[0].split("@")[0]
    except Exception as exc:
        log.debug("IMSI lookup failed for %s: %s", msisdn, exc)
    return q  # fallback to original input

def reg_lookup(msisdn: str, days: int = 7) -> list:
    q = msisdn.lstrip("+")
    # Resolve MSISDN to IMSI — aor field stores IMSI, not MSISDN
    imsi = _msisdn_to_imsi(q)
    pat = f"%{imsi}%"
    log.info("reg_lookup: msisdn=%s imsi=%s", q, imsi)
    sql = f"""
        WITH relevant_sessions AS (
            SELECT DISTINCT session_id
            FROM homer_lake.main.hep_proto_1_registration
            WHERE timestamp >= NOW() - INTERVAL '{days} days'
              AND (aor LIKE ? OR payload LIKE ?)
        )
        SELECT
            registration.timestamp,
            registration.session_id,
            registration.aor,
            registration.src_ip,
            registration.user_agent,
            registration.expires,
            registration.contact,
            registration.payload
        FROM homer_lake.main.hep_proto_1_registration AS registration
        INNER JOIN relevant_sessions
            ON relevant_sessions.session_id = registration.session_id
        WHERE registration.timestamp >= NOW() - INTERVAL '{days} days'
        ORDER BY registration.timestamp DESC
        LIMIT 20000
    """
    con = _get_duck()
    try:
        rows = con.execute(sql, [pat, pat]).fetchall()
    finally:
        con.close()
    cols = [
        "timestamp",
        "session_id",
        "aor",
        "src_ip",
        "user_agent",
        "expires",
        "contact",
        "payload",
    ]
    records = [dict(zip(cols, raw)) for raw in rows]
    events = pair_successful_registrations(records, imsi)
    log.info(
        "reg_lookup: paired %d successful event(s) from %d raw row(s)",
        len(events),
        len(records),
    )

    out = []
    for event in events:
        registration = event["request"]
        response = event["response"]
        aor = registration.get("aor") or response.get("aor") or ""
        payload = registration.get("payload") or ""
        ua = (
            registration.get("user_agent")
            or response.get("user_agent")
            or ""
        ).strip()
        expires = response.get("expires")
        if expires in (None, ""):
            expires = registration.get("expires")
        try:
            expires_val = int(expires or 0)
        except (TypeError, ValueError):
            expires_val = 0
        status = "UNREGISTERED" if expires_val == 0 else "REGISTERED"
        pan  = _hdr(payload, "P-Access-Network-Info")
        cell = resolve_cell(pan)
        try:
            cd = registration["timestamp"]
            if not hasattr(cd, "tzinfo"):
                cd = datetime.datetime.fromisoformat(str(cd).replace("Z", "+00:00"))
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            cd = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        out.append({
            "create_date": cd,
            "status":      status,
            "imsi":        aor,
            "src_ip":      registration.get("src_ip", ""),
            "user_agent":  ua if ua and not UA_EXCLUDE.search(ua) else "-",
            "expires":     expires_val,
            "contact":     response.get("contact") or registration.get("contact") or "",
            "cell_label":  cell["label"],
            "cell_url":    cell["url"],
            "reason":      _hdr(response.get("payload") or payload, "Reason"),
        })
    out.sort(key=lambda x: x["create_date"], reverse=True)
    return _add_gaps_and_classify(out)

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UA Lookup{% if query %} — {{ query }}{% endif %}</title>
<style>
:root{--bg:#f7f6f2;--surf:#fff;--bd:#ddd;--text:#1a1a1a;--muted:#666;
      --pri:#01696f;--prih:#0c4e54;--err:#a12c7b;--warn:#964219;--r:6px;
      --font:system-ui,-apple-system,sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--text);
     font-size:15px;line-height:1.5;padding:1.5rem 1rem}
h1{font-size:1.4rem;font-weight:700;color:var(--pri);margin-bottom:1rem}
h2{font-size:1.05rem;font-weight:600;margin:1.5rem 0 .5rem;border-bottom:2px solid var(--pri);
   padding-bottom:.25rem;color:#222}
form{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.5rem;align-items:center}
input[type=text]{padding:.45rem .75rem;border:1px solid var(--bd);border-radius:var(--r);
                 font-size:.95rem;width:220px}
input[type=number]{padding:.45rem .5rem;border:1px solid var(--bd);border-radius:var(--r);
                   font-size:.95rem;width:80px}
button{padding:.45rem 1rem;background:var(--pri);color:#fff;border:none;
       border-radius:var(--r);cursor:pointer;font-size:.95rem}
button:hover{background:var(--prih)}
label{font-size:.9rem;color:var(--muted)}
.err{color:var(--err);background:#fdf0f7;border:1px solid #e8b8d8;
     border-radius:var(--r);padding:.6rem .9rem;margin-bottom:1rem}
.warnbox{color:var(--warn);background:#fdf5ee;border:1px solid #e8c8a8;
         border-radius:var(--r);padding:.6rem .9rem;margin-bottom:1rem;font-size:.88rem}
table{border-collapse:collapse;width:100%;font-size:.84rem;margin-bottom:.75rem}
th{background:var(--pri);color:#fff;padding:.4rem .6rem;text-align:left;font-weight:600;
   white-space:nowrap}
td{padding:.35rem .6rem;border-bottom:1px solid var(--bd);vertical-align:top}
tr:hover td{background:#f0f8f8}
tr.unreg td{color:#999;font-style:italic}
.bearer-th{background:#2c6e6f}
.badge{display:inline-block;padding:.1rem .45rem;border-radius:20px;
       font-size:.75rem;font-weight:600;white-space:nowrap}
.ev-reboot{background:#d4edda;color:#155724}
.ev-airplane{background:#cce5ff;color:#004085}
.ev-coverage{background:#fff3cd;color:#856404}
.ev-periodic{background:#e2e3e5;color:#383d41}
.ev-ua{background:#d1ecf1;color:#0c5460}
.ev-cell{background:#fde8d8;color:#7b3200}
.ev-unknown{background:#f8d7da;color:#721c24}
.tag{display:inline-block;padding:.05rem .4rem;border-radius:3px;
     font-size:.8rem;background:#e8f4f5;color:#01696f;font-weight:500}
.mono{font-family:monospace;font-size:.82rem}
.nodata{color:var(--muted);font-style:italic;font-size:.88rem;padding:.3rem 0}
details{margin-top:.75rem}
details summary{cursor:pointer;font-size:.88rem;color:var(--pri);
                padding:.3rem 0;user-select:none}
details[open] summary{margin-bottom:.4rem}
@media(max-width:768px){
  table{font-size:.76rem}th,td{padding:.28rem .35rem}
  input[type=text]{width:100%}}
</style>
</head>
<body>
<h1>📡 UA &amp; Registration Lookup</h1>
<form method="get" action="/">
  <input type="text" name="q" value="{{ query|e }}"
         placeholder="MSISDN or IMSI" required autocomplete="off">
  <label>Days&nbsp;<input type="number" name="days"
         value="{{ days }}" min="1" max="90"></label>
  <button type="submit">Search</button>
</form>

{% if error %}<div class="err">⚠ {{ error }}</div>{% endif %}

{% if query %}

<h2>📶 Active Bearers &amp; IP Addresses</h2>
{% if bearer_rows %}
<table>
  <tr>
    <th class="bearer-th">Node</th>
    <th class="bearer-th">IMS IP</th>
    <th class="bearer-th">Internet IP</th>
    <th class="bearer-th">SGW / ePDG IP</th>
    <th class="bearer-th">APN</th>
    <th class="bearer-th">RAT</th>
    <th class="bearer-th">Component</th>
    <th class="bearer-th">Cell Type</th>
    <th class="bearer-th">Session Start</th>
  </tr>
  {% for b in bearer_rows %}
  <tr>
    <td>{{ b.node }}</td>
    <td class="mono">{{ b.ims_ip }}</td>
    <td class="mono">{{ b.internet_ip }}</td>
    <td class="mono">{{ b.sgw_ip }}</td>
    <td><span class="tag">{{ b.apn }}</span></td>
    <td>{{ b.rat_type }}</td>
    <td>{{ b.component }}</td>
    <td>{{ b.cell_type }}</td>
    <td>{{ b.session_start }}</td>
  </tr>
  {% endfor %}
</table>
{% else %}
<p class="nodata">No active bearer found for <strong>{{ query }}</strong> on any UCN node.</p>
{% endif %}

<h2>📱 Latest Registration User-Agent</h2>
{% if device_row %}
<table style="max-width:1000px">
  <tr><th>User-Agent</th><th>Observed at</th><th>Access / Cell</th></tr>
  <tr>
    <td><strong>{{ device_row.user_agent }}</strong></td>
    <td>{{ device_row.create_date }}</td>
    <td>{% if device_row.cell_url %}
      <a href="{{ device_row.cell_url }}" target="_blank">
        📍 {{ device_row.cell_label }}</a>
      {% else %}{{ device_row.cell_label }}{% endif %}</td>
  </tr>
</table>
{% else %}
<p class="nodata">No User-Agent was found in successful IMS registrations for
  <strong>{{ query }}</strong> during the selected period.</p>
{% endif %}

<h2>📋 Registration History
  <small style="font-weight:400;font-size:.85rem">
    (last {{ days }} day{{ 's' if days != 1 else '' }})
  </small>
</h2>
{% if reg_rows %}
<table>
  <tr>
    <th>Timestamp</th><th>Gap ↑</th><th>Event</th><th>Status</th>
    <th>AoR / IMSI</th><th>Src IP</th><th>User-Agent</th>
    <th>Expires (s)</th><th>Contact</th><th>Cell</th>
  </tr>
  {% for r in reg_rows %}
  <tr class="{{ 'unreg' if r.status == 'UNREGISTERED' else '' }}">
    <td>{{ r.create_date }}</td>
    <td style="white-space:nowrap">{{ r.gap }}</td>
    <td>{% if r.event %}<span class="badge {{ r.event_class }}">{{ r.event }}</span>{% endif %}</td>
    <td>{{ r.status }}</td>
    <td class="mono">{{ r.imsi }}</td>
    <td class="mono">{{ r.src_ip }}</td>
    <td>{{ r.user_agent }}</td>
    <td>{{ r.expires }}</td>
    <td style="font-size:.78rem;max-width:180px;overflow:hidden;
               text-overflow:ellipsis;white-space:nowrap">{{ r.contact }}</td>
    <td>{% if r.cell_url %}<a href="{{ r.cell_url }}" target="_blank">
        📍 {{ r.cell_label }}</a>{% else %}{{ r.cell_label }}{% endif %}</td>
  </tr>
  {% if r.status == 'UNREGISTERED' and r.reason %}
  <tr class="unreg">
    <td></td><td colspan="9" style="font-size:.8rem;color:#888">
      Reason: {{ r.reason }}</td>
  </tr>
  {% endif %}
  {% endfor %}
</table>
{% else %}
<p class="nodata">No registration record found for <strong>{{ query }}</strong>
  in the last {{ days }} days.</p>
{% endif %}

{% if vlr %}
<div class="warnbox">
        <p class="vlr-note">📡 Live VLR data sourced from YATE SQLite on <strong>{{ vlr.ucn_node_label }}</strong>. Reflects current registration state — User-Agent not stored in VLR.</p>
  User-Agent not stored in this DB.
</div>
<h2>🗃 VLR Record</h2>
<table style="max-width:700px">
  <tr><th colspan="4">Subscriber</th></tr>
  <tr><td><b>IMSI</b></td><td class="mono">{{ vlr.imsi or "—" }}</td>
      <td><b>MSISDN</b></td><td>{{ vlr.msisdn or "—" }}</td></tr>
  <tr><td><b>IMEI</b></td><td class="mono">{{ vlr.imei or "—" }}</td>
      <td><b>Type</b></td><td>{{ vlr.type or "—" }}</td></tr>
  <tr><td><b>P-CSCF</b></td><td class="mono">{{ vlr.pcscf or "—" }}</td>
      <td><b>Reg interval</b></td><td>{{ vlr.reg_interval or "—" }}s</td></tr>
  <tr><td><b>Registered</b></td><td>{{ vlr.time_created or "—" }}</td>
      <td><b>Last updated</b></td><td>{{ vlr.time_updated or "—" }}</td></tr>
  <tr><td><b>HSS</b></td><td>{{ vlr.hss or "—" }}</td>
      <td><b>Expires</b></td><td>{{ vlr.expires or "—" }}</td></tr>
  <tr><td><b>Cell</b></td>
      <td colspan="3">{% if vlr.cell_url %}
        <a href="{{ vlr.cell_url }}" target="_blank">📍 {{ vlr.cell_label }}</a>
        {% else %}{{ vlr.cell_label or "—" }}{% endif %}</td></tr>
</table>
{% endif %}

<details>
  <summary>ℹ️ Legend — event badges &amp; notes</summary>
  <table style="max-width:700px;margin-top:.4rem">
    <tr><th>Badge</th><th>Meaning</th><th>How detected</th></tr>
    <tr><td><span class="badge ev-reboot">🔄 Reboot</span></td>
        <td>Device rebooted</td>
        <td>UNREGISTERED preceded this reg, gap &lt; 90 s</td></tr>
    <tr><td><span class="badge ev-airplane">✈️ Airplane / Off</span></td>
        <td>Airplane mode or powered off</td>
        <td>UNREGISTERED preceded this reg, gap ≥ 90 s</td></tr>
    <tr><td><span class="badge ev-coverage">📵 Coverage loss</span></td>
        <td>Device lost signal without deregistering</td>
        <td>Previous event REGISTERED, gap &gt; 5 min</td></tr>
    <tr><td><span class="badge ev-periodic">🔁 Periodic</span></td>
        <td>Normal keep-alive re-registration</td>
        <td>Gap ≈ Expires (±25%), previous REGISTERED</td></tr>
    <tr><td><span class="badge ev-ua">🆕 UA changed</span></td>
        <td>OS update / new device / SIM swap</td>
        <td>Different UA string vs previous row</td></tr>
    <tr><td><span class="badge ev-cell">📍 Cell changed</span></td>
        <td>Device moved to different antenna</td>
        <td>Different cell ID vs previous row</td></tr>
    <tr><td><span class="badge ev-unknown">❓ First in window</span></td>
        <td>Oldest event in selected window</td>
        <td>No previous row to compare — extend days</td></tr>
  </table>
  <p style="font-size:.82rem;color:var(--muted);margin-top:.5rem">
    Multiple badges can appear on one row (e.g. ✈️ Airplane / Off · 🆕 UA changed).<br>
    UNREGISTERED rows are dimmed; any SIP <code>Reason:</code> header is shown beneath them.<br>
    UA visible only for VoLTE/WiFi Calling. CS calls via MGCF show YATE — filtered out.<br>
    <b>Src IP</b> = APN/PDN address from PGW, not a public IP.
  </p>
</details>

{% endif %}
</body>
</html>"""



_BAKOM_CACHE = {}
_BAKOM_API = (
    "https://api3.geo.admin.ch/"
    "rest/services/ech/MapServer/identify"
)
_BAKOM_LAYER = "ch.bakom.standorte-mobilfunkanlagen"

_PLMN_OPERATOR = {
    (228, 1): "Swisscom",
    (228, 2): "Sunrise",
    (228, 3): "Salt",
}


def _lv95_to_wgs84(east, north):
    """Approximate Swiss LV95 to WGS84 conversion."""
    y_aux = (east - 2600000.0) / 1000000.0
    x_aux = (north - 1200000.0) / 1000000.0

    lng_aux = (
        2.6779094
        + 4.728982 * y_aux
        + 0.791484 * y_aux * x_aux
        + 0.1306 * y_aux * x_aux ** 2
        - 0.0436 * y_aux ** 3
    )
    lat_aux = (
        16.9023892
        + 3.238272 * x_aux
        - 0.270978 * y_aux ** 2
        - 0.002528 * x_aux ** 2
        - 0.0447 * y_aux ** 2 * x_aux
        - 0.0140 * x_aux ** 3
    )

    lng = lng_aux * 100.0 / 36.0
    lat = lat_aux * 100.0 / 36.0
    return lat, lng


def _bakom_feature_coordinates(feature):
    geometry = feature.get("geometry") or {}

    coordinates = geometry.get("coordinates")
    if isinstance(coordinates, list) and len(coordinates) >= 2:
        return float(coordinates[0]), float(coordinates[1])

    if "x" in geometry and "y" in geometry:
        return float(geometry["x"]), float(geometry["y"])

    properties = (
        feature.get("properties")
        or feature.get("attributes")
        or {}
    )
    coordinate_text = properties.get("koord")
    if coordinate_text:
        east, north = coordinate_text.split(",", 1)
        return float(east), float(north)

    return None


def _query_bakom_sites(east, north, radius_m):
    envelope = (
        east - radius_m,
        north - radius_m,
        east + radius_m,
        north + radius_m,
    )

    parameters = {
        "geometryType": "esriGeometryEnvelope",
        "geometry": ",".join(
            f"{value:.3f}" for value in envelope
        ),
        "geometryFormat": "geojson",
        "layers": f"all:{_BAKOM_LAYER}",
        "tolerance": 0,
        "returnGeometry": "true",
        "sr": 2056,
        "lang": "en",
        "limit": 200,
    }

    request_url = (
        _BAKOM_API + "?" + urllib.parse.urlencode(parameters)
    )
    api_request = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "ua-lookup/BAKOM-candidate-lookup"
        },
    )

    with urllib.request.urlopen(api_request, timeout=6) as response:
        return _json.loads(response.read())


def _bakom_candidate_sites(lat, lng, mcc, mnc, accuracy_m):
    """Return nearby reported sites, never a confirmed serving site."""
    cache_key = (
        round(lat, 5),
        round(lng, 5),
        mcc,
        mnc,
        round(accuracy_m or 0.0),
    )
    now = time.monotonic()

    cached = _BAKOM_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    operator = _PLMN_OPERATOR.get((mcc, mnc))
    east, north = _wgs84_to_lv95(lng, lat)

    result = {
        "sites": [],
        "total_candidates": 0,
        "search_radius_m": 2000,
        "operator": operator or f"MCC {mcc} / MNC {mnc}",
        "api_count": 0,
        "truncated": False,
        "error": "",
    }

    try:
        candidates = []

        # Use the smaller window in populated areas. Only expand when
        # no operator/4G candidate is found.
        for radius_m in (2000, 5000):
            data = _query_bakom_sites(
                east,
                north,
                radius_m,
            )
            features = data.get("results", [])
            candidates = []

            for feature in features:
                properties = (
                    feature.get("properties")
                    or feature.get("attributes")
                    or {}
                )

                coordinates = _bakom_feature_coordinates(feature)
                if not coordinates:
                    continue

                site_east, site_north = coordinates
                distance_m = (
                    (site_east - east) ** 2
                    + (site_north - north) ** 2
                ) ** 0.5

                # The envelope is square; keep only the circular radius.
                if distance_m > radius_m:
                    continue

                station = str(
                    properties.get("station")
                    or properties.get("label")
                    or feature.get("featureId")
                    or "Unknown site"
                )
                technology = str(
                    properties.get("techno_en") or ""
                )

                if operator and not station.casefold().startswith(
                    operator.casefold() + " "
                ):
                    continue

                if "4G" not in technology:
                    continue

                site_lat, site_lng = _lv95_to_wgs84(
                    site_east,
                    site_north,
                )

                site_type = str(
                    properties.get("typ_en") or ""
                )
                power = str(
                    properties.get("power_en") or ""
                )

                if "Indoor" in site_type:
                    category = "Indoor / small-cell candidate"
                elif "Tunnel" in site_type:
                    category = "Tunnel candidate"
                else:
                    category = "Outdoor candidate"

                candidates.append({
                    "station": station,
                    "distance_m": round(distance_m, 1),
                    "inside_google_radius": bool(
                        accuracy_m
                        and accuracy_m > 0
                        and distance_m <= accuracy_m
                    ),
                    "technology": technology,
                    "power": power,
                    "site_type": site_type,
                    "category": category,
                    "site_sheet": str(
                        properties.get("bewilligung_en") or ""
                    ),
                    "feature_id": (
                        feature.get("featureId")
                        or feature.get("id")
                    ),
                    "lat": round(site_lat, 7),
                    "lng": round(site_lng, 7),
                    "lv95_east": round(site_east),
                    "lv95_north": round(site_north),
                })

            result["search_radius_m"] = radius_m
            result["api_count"] = len(features)
            result["truncated"] = len(features) >= 200

            if candidates or radius_m == 5000:
                break

        candidates.sort(key=lambda site: site["distance_m"])
        result["total_candidates"] = len(candidates)
        result["sites"] = candidates[:8]

        log.info(
            "BAKOM candidates MCC=%d MNC=%d around %.6f,%.6f: "
            "%d matching site(s), showing %d within %dm%s",
            mcc,
            mnc,
            lat,
            lng,
            result["total_candidates"],
            len(result["sites"]),
            result["search_radius_m"],
            " (API truncated)" if result["truncated"] else "",
        )

    except Exception as exc:
        result["error"] = str(exc)
        log.warning(
            "BAKOM candidate lookup failed MCC=%d MNC=%d "
            "around %.6f,%.6f: %s",
            mcc, mnc, lat, lng, exc,
        )

    ttl = 604800 if not result["error"] else 300

    if len(_BAKOM_CACHE) >= 2048:
        _BAKOM_CACHE.clear()

    _BAKOM_CACHE[cache_key] = (now + ttl, result)
    return result


CELL_MAP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>LTE cell location</title>
  <link rel="stylesheet"
        href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html,body{min-height:100%;margin:0;background:#1e1e1e;color:#ddd;
              font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
    .page{min-height:100vh;display:flex;flex-direction:column}
    .info{padding:14px 18px;background:#252525;border-bottom:1px solid #444}
    h2{margin:0 0 8px;color:#7ec8e3;font-size:18px}
    .summary{color:#ddd;margin:4px 0}
    .warning{color:#e3b97e;font-size:12px;margin:7px 0 0}
    .identifiers{color:#aaa;font-size:12px;margin-top:7px}
    .links{display:flex;flex-wrap:wrap;gap:7px;margin-top:11px}
    .links a{color:#fff;background:#3b4e5a;border:1px solid #607d8b;
             border-radius:4px;padding:6px 10px;text-decoration:none;
             font-size:12px}
    .links a:hover{background:#526d7a}
    .bakom-status{margin-top:11px;padding:8px 10px;background:#302a20;
                  border-left:3px solid #f59e0b;font-size:12px}
    .bakom-error{color:#e37e7e}
    details.candidates{margin-top:8px;font-size:12px}
    details.candidates summary{cursor:pointer;color:#fbbf24}
    .table-wrap{overflow-x:auto;margin-top:7px}
    .candidate-table{border-collapse:collapse;width:100%;font-size:11px}
    .candidate-table th,.candidate-table td{
      border-bottom:1px solid #444;padding:5px 7px;text-align:left;
      vertical-align:top;white-space:nowrap
    }
    .candidate-table th{color:#fbbf24;background:#292929}
    .inside{display:inline-block;color:#93c5fd;border:1px solid #3b82f6;
            border-radius:3px;padding:1px 4px;margin-left:4px}
    #map{width:100%;height:62vh;min-height:430px;flex:1}
    .estimate-label{background:#202b33;color:#fff;border:1px solid #7ec8e3;
                    border-radius:4px;padding:4px 7px;box-shadow:none}
    .estimate-label:before{display:none}
    .tower-label{background:#382d18;color:#fff;border:1px solid #f59e0b;
                 border-radius:4px;padding:3px 5px;box-shadow:none}
    .tower-label:before{display:none}
    @media(max-width:700px){
      .info{padding:11px}
      #map{height:58vh;min-height:380px}
      .candidate-table{font-size:10px}
    }
  </style>
</head>
<body>
<div class="page">
  <section class="info">
    <h2>📍 Google-estimated LTE location</h2>
    <div class="summary">
      {{ "%.7f"|format(lat) }}, {{ "%.7f"|format(lng) }}
      {% if accuracy > 0 %}
        &nbsp;·&nbsp; uncertainty radius ≈ ±{{ "%.0f"|format(accuracy) }} m
      {% else %}
        &nbsp;·&nbsp; accuracy radius unavailable
      {% endif %}
    </div>
    <div class="identifiers">
      MCC {{ mcc }} · MNC {{ mnc }} · TAC {{ tac }} ·
      ECI {{ eci }} (0x{{ eci_hex }}) ·
      eNB {{ enb }} · cell {{ cell_id }}
    </div>
    <p class="warning">
      Blue is Google's estimated device area. Orange markers are nearby
      operator-reported BAKOM sites—not confirmed serving antennas.
      A serving macro site can be outside the blue circle.
    </p>

    <div class="bakom-status">
      {% if bakom_error %}
        <span class="bakom-error">
          BAKOM lookup unavailable: {{ bakom_error }}
        </span>
      {% elif bakom_sites %}
        BAKOM found {{ bakom_total }} matching
        {{ bakom_operator }} 4G candidate site(s) within
        {{ "%.1f"|format(bakom_radius / 1000) }} km;
        showing the nearest {{ bakom_sites|length }}.
        {% if bakom_truncated %}
          API limit reached—the returned set may be incomplete.
        {% endif %}

        <details class="candidates">
          <summary>Show candidate-site details</summary>
          <div class="table-wrap">
            <table class="candidate-table">
              <thead>
                <tr>
                  <th>Distance</th>
                  <th>Station</th>
                  <th>Candidate type</th>
                  <th>Technology</th>
                  <th>Power</th>
                  <th>Site sheet</th>
                </tr>
              </thead>
              <tbody>
                {% for site in bakom_sites %}
                <tr>
                  <td>
                    {{ "%.0f"|format(site.distance_m) }} m
                    {% if site.inside_google_radius %}
                      <span class="inside">inside blue radius</span>
                    {% endif %}
                  </td>
                  <td>{{ site.station }}</td>
                  <td>{{ site.category }}</td>
                  <td>{{ site.technology }}</td>
                  <td>{{ site.power }}</td>
                  <td>{{ site.site_sheet or "—" }}</td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </details>
      {% else %}
        No matching {{ bakom_operator }} 4G BAKOM site was found within
        {{ "%.1f"|format(bakom_radius / 1000) }} km.
      {% endif %}
    </div>

    <div class="links">
      <a href="{{ bakom_url }}" target="_blank"
         rel="noopener noreferrer" referrerpolicy="no-referrer">
        BAKOM antenna sites
      </a>
      <a href="{{ cellmapper_url }}" target="_blank"
         rel="noopener noreferrer" referrerpolicy="no-referrer">
        CellMapper — search eNB {{ enb }}
      </a>
      <a href="{{ antenna_url }}" target="_blank"
         rel="noopener noreferrer" referrerpolicy="no-referrer">
        Swiss Antenna Map
      </a>
      <a href="{{ google_url }}" target="_blank"
         rel="noopener noreferrer" referrerpolicy="no-referrer">
        Google Maps
      </a>
      <a href="{{ osm_url }}" target="_blank"
         rel="noopener noreferrer" referrerpolicy="no-referrer">
        OpenStreetMap
      </a>
    </div>
  </section>
  <div id="map"></div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const lat = {{ lat|tojson }};
  const lng = {{ lng|tojson }};
  const accuracy = {{ accuracy|tojson }};
  const bakomSites = {{ bakom_sites|tojson }};
  const map = L.map("map").setView([lat, lng], 16);

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">' +
      'OpenStreetMap contributors</a>'
  }).addTo(map);

  const boundsLayers = [];
  const estimateMarker = L.marker([lat, lng]).addTo(map);
  boundsLayers.push(estimateMarker);

  estimateMarker.bindTooltip(
    "Google estimate",
    {permanent:true, direction:"top", className:"estimate-label"}
  );

  let estimateCircle = null;
  if (accuracy > 0) {
    estimateCircle = L.circle([lat, lng], {
      radius: accuracy,
      color: "#3b82f6",
      weight: 2,
      fillColor: "#60a5fa",
      fillOpacity: 0.18
    }).addTo(map);

    estimateCircle.bindPopup(
      "Google uncertainty radius: approximately ±" +
      Math.round(accuracy) + " m"
    );
    boundsLayers.push(estimateCircle);
  }

  bakomSites.forEach(function(site) {
    const candidate = L.circleMarker(
      [site.lat, site.lng],
      {
        radius: site.inside_google_radius ? 8 : 6,
        color: "#b45309",
        weight: 2,
        fillColor: "#f59e0b",
        fillOpacity: 0.85
      }
    ).addTo(map);

    candidate.bindTooltip(
      escapeHtml(site.station),
      {direction:"top", className:"tower-label"}
    );

    candidate.bindPopup(
      "<strong>" + escapeHtml(site.station) + "</strong><br>" +
      escapeHtml(site.category) + "<br>" +
      "Distance from Google estimate: " +
      Math.round(site.distance_m) + " m<br>" +
      escapeHtml(site.technology) + "<br>" +
      escapeHtml(site.power) + "<br>" +
      "BAKOM LV95: " +
      site.lv95_east + ", " + site.lv95_north
    );

    boundsLayers.push(candidate);
  });

  if (boundsLayers.length > 1) {
    map.fitBounds(
      L.featureGroup(boundsLayers).getBounds(),
      {padding:[30,30],maxZoom:16}
    );
  } else if (estimateCircle) {
    map.fitBounds(
      estimateCircle.getBounds(),
      {padding:[30,30],maxZoom:17}
    );
  } else {
    map.setView([lat, lng], 16);
  }
</script>
</body>
</html>"""


@app.route("/cell-map")

def cell_map():
    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
        accuracy = float(request.args.get("accuracy", "0"))
        mcc = int(request.args["mcc"])
        mnc = int(request.args["mnc"])
        tac = int(request.args["tac"])
        eci = int(request.args["eci"])
        enb = int(request.args["enb"])
        cell_id = int(request.args["cell"])
    except (KeyError, TypeError, ValueError):
        return "Invalid or incomplete cell-map parameters", 400

    if not (
        -90.0 <= lat <= 90.0
        and -180.0 <= lng <= 180.0
        and 0.0 <= accuracy <= 2000000.0
        and 0 <= mcc <= 999
        and 0 <= mnc <= 999
        and 0 <= tac <= 16777215
        and 0 <= eci <= 268435455
        and enb >= 0
        and 0 <= cell_id <= 255
    ):
        return "Cell-map parameters outside valid ranges", 400

    east, north = _wgs84_to_lv95(lng, lat)
    bakom = _bakom_candidate_sites(
        lat,
        lng,
        mcc,
        mnc,
        accuracy,
    )

    osm_url = (
        "https://www.openstreetmap.org/"
        + "?" + urllib.parse.urlencode({
            "mlat": lat,
            "mlon": lng,
            "zoom": 17,
        })
    )
    google_url = (
        "https://www.google.com/maps/search/"
        + "?" + urllib.parse.urlencode({
            "api": 1,
            "query": f"{lat},{lng}",
        })
    )
    bakom_url = (
        "https://map.geo.admin.ch/#/map?"
        + urllib.parse.urlencode({
            "lang": "en",
            "center": f"{east:.0f},{north:.0f}",
            "z": 10,
            "topic": "funksender",
            "layers": "ch.bakom.standorte-mobilfunkanlagen",
            "bgLayer": "ch.swisstopo.pixelkarte-farbe",
            "catalogNodes": "ech",
        })
    )
    cellmapper_url = (
        "https://www.cellmapper.net/map?"
        + urllib.parse.urlencode({
            "MCC": mcc,
            "MNC": mnc,
            "type": "LTE",
            "latitude": lat,
            "longitude": lng,
            "zoom": 16,
            "showTowers": "true",
            "showTowerLabels": "true",
        })
    )

    return render_template_string(
        CELL_MAP_HTML,
        lat=lat,
        lng=lng,
        accuracy=accuracy,
        mcc=mcc,
        mnc=mnc,
        tac=tac,
        eci=eci,
        eci_hex=f"{eci:07x}",
        enb=enb,
        cell_id=cell_id,
        osm_url=osm_url,
        google_url=google_url,
        bakom_url=bakom_url,
        cellmapper_url=cellmapper_url,
        antenna_url=(
            "https://carteantennesuisse.ch/"
            "carte-interactive-antennes-suisse/"
        ),
        bakom_sites=bakom["sites"],
        bakom_total=bakom["total_candidates"],
        bakom_radius=bakom["search_radius_m"],
        bakom_operator=bakom["operator"],
        bakom_truncated=bakom["truncated"],
        bakom_error=bakom["error"],
    )


@app.route("/")
def index():
    query = (request.args.get("q") or "").strip()
    try:
        days = max(1, min(30, int(request.args.get("days", 1))))
    except ValueError:
        days = 1

    if not query:
        return render_template_string(HTML, query="", days=days, error=None,
                                      reg_rows=[], device_row=None,
                                      vlr=None, bearer_rows=[])

    error = None
    reg_rows = []
    device_row = None
    vlr = None
    bearer_rows = []

    try:
        reg_rows = reg_lookup(query, days)
        device_row = next(
            (
                row for row in reg_rows
                if row.get("user_agent") not in (None, "", "-")
            ),
            None,
        )
    except Exception as exc:
        log.exception("DuckDB lookup failed for %s", query)
        error = str(exc)

    if True:  # always run VLR
        vlr = vlr_lookup(query)

    try:
        bearer_rows = bearer_lookup(query)
    except Exception as exc:
        log.warning("Bearer lookup failed for %s: %s", query, exc)

    return render_template_string(HTML, query=query, days=days, error=error,
                                  reg_rows=reg_rows, device_row=device_row,
                                  vlr=vlr, bearer_rows=bearer_rows)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
