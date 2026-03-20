from flask import Flask, request, render_template_string, jsonify
import psycopg2
import psycopg2.extras
import logging
import re
import json as _json
import urllib.request
import os
from ua_mappings import get_android_version


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}  (set it in /opt/ua-lookup/config.env)")
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

DB_CONFIG = {
    "host":     _require_env("DB_HOST"),
    "port":     int(os.environ.get("DB_PORT", "5432")),
    "dbname":   _require_env("DB_NAME"),
    "user":     _require_env("DB_USER"),
    "password": _require_env("DB_PASSWORD"),
}
GOOGLE_API_KEY = _require_env("GOOGLE_API_KEY")

UA_EXCLUDE   = re.compile(r"YATE|OpenSIPS|dispatcher|nx-sbc-ocs|NexSBC|VoLTE/WFC", re.IGNORECASE)
_google_cache: dict = {}


def _decode_plmn_ecgi(cell_hex: str):
    if len(cell_hex) < 16:
        return None
    b      = bytes.fromhex(cell_hex[:16])
    mcc    = (b[0]&0x0F)*100 + ((b[0]>>4)&0x0F)*10 + ((b[1]>>4)&0x0F)
    mnc_d3 = b[1] & 0x0F
    _d1    = b[2] & 0x0F;        mnc_d1 = _d1 if _d1 <= 9 else 0
    _d2    = (b[2]>>4) & 0x0F;   mnc_d2 = _d2 if _d2 <= 9 else 0
    mnc    = mnc_d1*10+mnc_d2 if mnc_d3 == 0xF else mnc_d3*100+mnc_d1*10+mnc_d2
    tac    = (b[3]<<8) | b[4]
    eci_28 = ((b[4]<<24)|(b[5]<<16)|(b[6]<<8)|b[7]) & 0x0FFFFFFF
    return mcc, mnc, tac, eci_28>>8, eci_28&0xFF, eci_28


def _google_lookup(mcc, mnc, tac, ecgi):
    key = (mcc, mnc, tac, ecgi)
    if key in _google_cache:
        return _google_cache[key]
    result = None
    try:
        payload = _json.dumps({
            "radioType": "lte", "considerIp": False,
            "cellTowers": [{"mobileCountryCode": mcc, "mobileNetworkCode": mnc,
                            "locationAreaCode": tac, "cellId": ecgi}]
        }).encode()
        req = urllib.request.Request(
            f"https://www.googleapis.com/geolocation/v1/geolocate?key={GOOGLE_API_KEY}",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = _json.loads(resp.read())
        if "location" in data:
            result = (float(data["location"]["lat"]), float(data["location"]["lng"]))
            log.info("Google cell MCC=%d MNC=%d TAC=%d ECI=%d -> %.6f,%.6f",
                     mcc, mnc, tac, ecgi, result[0], result[1])
        else:
            log.info("Google cell MCC=%d MNC=%d TAC=%d ECI=%d -> no match: %s",
                     mcc, mnc, tac, ecgi, data)
    except Exception as exc:
        log.warning("Google cell lookup failed MCC=%d MNC=%d TAC=%d ECI=%d: %s",
                    mcc, mnc, tac, ecgi, exc)
    _google_cache[key] = result
    return result


def resolve_cell(raw_header: str) -> dict:
    if not raw_header:
        return {"label": "-", "url": ""}
    at       = raw_header.split(";")[0].strip()
    at_short = at.replace("3GPP-", "").replace("-FDD", "").replace("-TDD", "")
    m = re.search(r"utran-cell-id-3gpp=([0-9a-fA-F]+)", raw_header, re.IGNORECASE)
    if not m:
        return {"label": at_short or raw_header, "url": ""}
    parsed = _decode_plmn_ecgi(m.group(1))
    if not parsed:
        return {"label": at_short, "url": ""}
    mcc, mnc, tac, enb_id, cell_id, ecgi = parsed
    label  = f"{at_short} eNB:{enb_id} cell:{cell_id}"
    coords = _google_lookup(mcc, mnc, tac, ecgi)
    url    = (f"https://www.openstreetmap.org/?mlat={coords[0]}&mlon={coords[1]}&zoom=16"
              if coords else "")
    return {"label": label, "url": url}


def _fmt_gap(seconds: int) -> str:
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    h = seconds // 3600;  m = (seconds % 3600) // 60
    return f"{h}h {m}m" if m else f"{h}h"


def _add_gaps(rows: list) -> list:
    """Rows sorted DESC (newest first).
    Pass 1: compute gaps while all create_dates are still datetime objects.
    Pass 2: stringify create_date (must be separate — mixing str and datetime breaks subtraction).
    """
    for i, row in enumerate(rows):
        if i > 0:
            delta      = rows[i-1]["create_date"] - row["create_date"]
            secs       = int(delta.total_seconds())
            row["gap"] = _fmt_gap(secs) if secs >= 0 else ""
    for row in rows:
        row["create_date"] = str(row["create_date"])
    return rows


HTML = """<!DOCTYPE html>
<html><head>
  <meta charset="utf-8"><title>UA Lookup - Homer DB</title>
  <style>
    body{font-family:monospace;background:#1e1e1e;color:#ddd;padding:2em;max-width:1600px}
    h2,h3{color:#7ec8e3}
    input[type=text]{background:#2d2d2d;border:1px solid #555;color:#fff;padding:6px 10px;width:320px;font-size:14px}
    input[type=submit]{background:#7ec8e3;border:none;padding:6px 16px;font-size:14px;cursor:pointer;color:#000}
    table{border-collapse:collapse;margin-top:1em;width:100%}
    th{background:#333;color:#7ec8e3;padding:8px 12px;text-align:left;white-space:nowrap}
    td{padding:7px 12px;border-bottom:1px solid #333}
    tr:hover td{background:#2a2a2a}
    tr.dereg td{opacity:.45}
    tr.dereg:hover td{opacity:.7}
    .reg{color:#7aee9a}.unreg{color:#e37e7e}.warn{color:#e3b97e;margin-top:1em}
    .gap{color:#888;font-size:.82em;white-space:nowrap}
    .info{color:#888;font-size:12px;margin-top:.5em}
    .infobox{margin-top:2em;border:1px solid #444;border-left:4px solid #7ec8e3;background:#252525;padding:1em 1.5em;font-size:13px;color:#aaa}
    .infobox h3{color:#7ec8e3;margin:0 0 .7em;font-size:14px}
    .infobox table{margin-top:.5em;font-size:12px}
    .infobox th{background:#2d2d2d;color:#7ec8e3;padding:5px 10px}
    .infobox td{padding:5px 10px;border-bottom:1px solid #333}
    .yes{color:#7aee9a}.no{color:#e37e7e}.warn2{color:#e3b97e}
    .docs-link{font-size:.78rem;font-weight:600;color:#888;border:1px solid #444;border-radius:6px;padding:5px 12px;text-decoration:none;white-space:nowrap}
    .docs-link:hover{border-color:#7ec8e3;color:#7ec8e3}
    .btn-hist{background:#065f46;border:none;color:#fff;padding:5px 14px;font-size:13px;cursor:pointer;border-radius:4px;margin-top:1em}
    .btn-hist:hover{background:#047857}
    .hist-days{background:#2d2d2d;border:1px solid #555;color:#fff;padding:4px 8px;font-size:13px;border-radius:4px}
    #hist-status{font-size:12px;color:#888;margin-left:.5em}
    #hist-box{margin-top:1em;border-top:1px solid #444;padding-top:1em;display:none}
    #hist-box h3{color:#34d399;margin:0 0 .5em}
    .cell-info{color:#a78bfa;font-size:.85em}
    .cell-info a{color:#a78bfa;text-decoration:none}
    .cell-info a:hover{color:#c4b5fd;text-decoration:underline}
  </style>
  <script>
    function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
    function cellHtml(label,url){
      if(!url) return esc(label);
      return '<a href="'+esc(url)+'" target="_blank" rel="noopener">&#128205; '+esc(label)+'</a>';
    }
  </script>
</head><body>
  <h2 style="display:flex;align-items:center;gap:1em;">
    &#128241; User-Agent Lookup &mdash; Homer DB
    <a class="docs-link" href="https://wiki.nexphone.ch/display/NETVOIP/User-Agent+Lookup" target="_blank">&#128214; Docs</a>
  </h2>
  <form method="get">
    <input type="text" name="q" placeholder="MSISDN or IMSI (partial ok)" value="{{ query }}" autofocus>
    <input type="submit" value="Search">
    <span class="info">&nbsp;Searches INVITE/BYE records, last 24 hours</span>
  </form>

  {% if query %}{% if error %}
    <p class="warn">&#9888; {{ error }}</p>
  {% else %}

    <h3 style="margin-top:1.5em;">&#128222; Recent Calls (INVITE/BYE)</h3>
    {% if not results %}
      <p class="warn">No call UA found for <b>{{ query }}</b> &mdash; subscriber may not have VoLTE active.</p>
    {% else %}
      <table>
        <tr><th>Timestamp</th><th>User-Agent</th><th>OS Version</th><th>From</th><th>To</th><th>Src IP (APN)</th><th>Method</th></tr>
        {% for r in results %}<tr>
          <td>{{ r.create_date }}</td><td>{{ r.user_agent }}</td><td>{{ r.os_version }}</td>
          <td>{{ r.from_user }}</td><td>{{ r.to_user }}</td><td>{{ r.src_ip }}</td><td>{{ r.method }}</td>
        </tr>{% endfor %}
      </table>
    {% endif %}

    <h3 style="margin-top:2em;">&#128275; Last IMS Registration</h3>
    {% if not reg_results %}
      <p class="warn">No registration record found for <b>{{ query }}</b> in the last 7 days.</p>
    {% else %}
      <table>
        <tr><th>Timestamp</th><th>Status</th><th>IMSI</th><th>Src IP (APN)</th><th>User-Agent</th><th>Expires (s)</th><th>Contact</th><th>Cell</th></tr>
        {% for r in reg_results %}<tr>
          <td>{{ r.create_date }}</td>
          <td class="{{ 'reg' if r.status == 'REGISTERED' else 'unreg' }}">{{ r.status }}</td>
          <td>{{ r.imsi }}</td><td>{{ r.src_ip }}</td><td>{{ r.user_agent }}</td>
          <td>{{ r.expires }}</td><td>{{ r.contact }}</td>
          <td class="cell-info">
            {% if r.cell_url %}<a href="{{ r.cell_url }}" target="_blank">&#128205; {{ r.cell_label }}</a>
            {% else %}{{ r.cell_label }}{% endif %}
          </td>
        </tr>{% endfor %}
      </table>
    {% endif %}

    <div style="display:flex;align-items:center;gap:.8em;margin-top:1em;">
      <button class="btn-hist" onclick="loadHistory()">&#128197; Registration History</button>
      <label style="color:#aaa;font-size:13px;">Last
        <select class="hist-days" id="hist-days">
          <option value="1">1 day</option><option value="3">3 days</option>
          <option value="7" selected>7 days</option><option value="14">14 days</option>
          <option value="30">30 days</option>
        </select>
      </label>
      <span id="hist-status"></span>
    </div>
    <div id="hist-box">
      <h3>&#128197; Registration History</h3>
      <table id="hist-table">
        <thead><tr>
          <th>Timestamp</th><th>Gap &#8593;</th><th>Status</th><th>IMSI</th><th>Src IP (APN)</th>
          <th>User-Agent</th><th>Expires (s)</th><th>Contact</th><th>Cell</th>
        </tr></thead>
        <tbody id="hist-body"></tbody>
      </table>
      <p id="hist-empty" style="display:none;color:#e3b97e;">No registration events found.</p>
    </div>
    <script>
      function loadHistory(){
        var days=document.getElementById('hist-days').value;
        var status=document.getElementById('hist-status');
        var box=document.getElementById('hist-box');
        var tbody=document.getElementById('hist-body');
        var empty=document.getElementById('hist-empty');
        status.textContent='Loading...';
        box.style.display='none'; tbody.innerHTML=''; empty.style.display='none';
        fetch('/history?q={{ query|urlencode }}&days='+days)
          .then(function(r){return r.json();})
          .then(function(rows){
            status.textContent='';
            if(!rows.length){empty.style.display='block';}
            else{rows.forEach(function(r){
              var isReg=r.status==='REGISTERED';
              var cls=isReg?'reg':'unreg';
              var rowCls=isReg?'':' class="dereg"';
              var gapHtml=r.gap?'<span class="gap">'+esc(r.gap)+'</span>':'';
              tbody.innerHTML+=
                '<tr'+rowCls+'>'+
                '<td>'+esc(r.create_date)+'</td>'+
                '<td>'+gapHtml+'</td>'+
                '<td class="'+cls+'">'+esc(r.status)+'</td>'+
                '<td>'+esc(r.imsi)+'</td>'+
                '<td>'+esc(r.src_ip)+'</td>'+
                '<td>'+esc(r.user_agent)+'</td>'+
                '<td>'+esc(r.expires)+'</td>'+
                '<td>'+esc(r.contact)+'</td>'+
                '<td class="cell-info">'+cellHtml(r.cell_label,r.cell_url)+'</td>'+
                '</tr>';
            });}
            box.style.display='block';
          })
          .catch(function(e){status.textContent='Error: '+e;});
      }
    </script>

  {% endif %}{% endif %}

  <div class="infobox">
    <h3>&#8505; How User-Agent detection works</h3>
    <p>UA visible only when device originates SIP directly into IMS core (VoLTE/WiFi Calling). CS calls via MGCF show YATE &mdash; filtered out.</p>
    <table>
      <tr><th>Scenario</th><th>UA visible?</th><th>What you see</th></tr>
      <tr><td>VoLTE (4G/5G)</td><td class="yes">&#10003; Yes</td><td>iOS/16.7 iPhone, Samsung IMS 6.0 ...</td></tr>
      <tr><td>WiFi Calling</td><td class="yes">&#10003; Yes</td><td>iOS/16.7 iPhone ...</td></tr>
      <tr><td>CS Fallback / 2G/3G</td><td class="no">&#10007; No</td><td>YATE/6.4.1 (filtered)</td></tr>
      <tr><td>IMS REGISTER</td><td class="warn2">&#9888; Sometimes</td><td>May be stripped by SBC</td></tr>
    </table>
    <p style="margin-top:.8em;"><b>Src IP</b> = APN/PDN address from PGW, not a public IP.</p>
  </div>
</body></html>"""


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    cur  = conn.cursor()
    cur.execute("SET statement_timeout = '15s'")
    cur.execute("SET TIME ZONE 'Europe/Zurich'")
    cur.close()
    return conn


def lookup(pattern: str):
    pattern = pattern.lstrip("+")
    sql = """
        SELECT create_date,
               protocol_header->>'srcIp'  AS src_ip,
               data_header->>'from_user'  AS from_user,
               data_header->>'to_user'    AS to_user,
               data_header->>'user_agent' AS user_agent,
               data_header->>'method'     AS method
        FROM hep_proto_1_call
        WHERE create_date > NOW() - INTERVAL '24 hours'
          AND (data_header->>'from_user' IN (%(exact)s, %(with_plus)s)
               OR data_header->>'to_user' IN (%(exact)s, %(with_plus)s))
          AND data_header->>'user_agent' IS NOT NULL
          AND data_header->>'user_agent' != ''
          AND data_header->>'method' IN ('INVITE', 'BYE')
        ORDER BY create_date DESC LIMIT 500
    """
    seen = {}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, {"exact": pattern, "with_plus": f"+{pattern}"})
            rows = cur.fetchall()
    for row in rows:
        ua = (row["user_agent"] or "").strip()
        if not ua or UA_EXCLUDE.search(ua):
            continue
        if ua not in seen:
            seen[ua] = {
                "create_date": row["create_date"],
                "user_agent":  ua,
                "os_version":  get_android_version(ua),
                "from_user":   row["from_user"] or "",
                "to_user":     row["to_user"]   or "",
                "src_ip":      row["src_ip"]    or "",
                "method":      row["method"]    or "",
            }
    return sorted(seen.values(), key=lambda x: str(x["create_date"]), reverse=True)


def _reg_row_to_dict(row):
    expires = (row["expires"] or "0").strip()
    cell    = resolve_cell(row["access_info"] or "")
    return {
        "create_date": row["create_date"],   # kept as datetime; caller stringifies
        "imsi":        row["imsi"]       or "",
        "src_ip":      row["src_ip"]     or "",
        "user_agent":  row["user_agent"] or "-",
        "expires":     expires,
        "status":      "REGISTERED" if expires != "0" else "UNREGISTERED",
        "contact":     row["contact"]    or "-",
        "cell_label":  cell["label"],
        "cell_url":    cell["url"],
        "gap":         "",
    }


REG_SELECT = """
    SELECT create_date,
           protocol_header->>'srcIp'                                  AS src_ip,
           data_header->>'from_user'                                   AS imsi,
           data_header->>'user_agent'                                  AS user_agent,
           substring(raw FROM 'Expires: ([0-9]+)')                     AS expires,
           substring(raw FROM 'Contact: ([^\r\n]+)')                 AS contact,
           substring(raw FROM 'P-Access-Network-Info: ([^\r\n]+)')   AS access_info
    FROM hep_proto_1_registration
    WHERE create_date > NOW() - %(interval)s::interval
      AND data_header->>'from_user' = %(imsi)s
      AND raw NOT LIKE 'SIP/2.0%%'
    ORDER BY create_date DESC
"""


def lookup_registration(pattern: str):
    pattern = pattern.lstrip("+")
    is_imsi = bool(re.fullmatch(r"228[0-9]{12}", pattern))
    if is_imsi:
        reg_pattern = pattern
    else:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT data_header->>'from_user' AS imsi
                    FROM hep_proto_1_registration
                    WHERE create_date > NOW() - INTERVAL '2 hours'
                      AND raw ILIKE %(pat)s
                    ORDER BY create_date DESC LIMIT 1
                """, {"pat": f"%{pattern}%"})
                row = cur.fetchone()
        if not row or not row["imsi"]:
            return []
        reg_pattern = row["imsi"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(REG_SELECT + "LIMIT 1",
                        {"imsi": reg_pattern, "interval": "7 days"})
            rows = [_reg_row_to_dict(r) for r in cur.fetchall()]
    for r in rows:
        r["create_date"] = str(r["create_date"])
    return rows


def lookup_registration_history(pattern: str, days: int):
    pattern = pattern.lstrip("+")
    is_imsi = bool(re.fullmatch(r"228[0-9]{12}", pattern))
    if is_imsi:
        reg_pattern = pattern
    else:
        existing = lookup_registration(pattern)
        if not existing:
            return []
        reg_pattern = existing[0]["imsi"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(REG_SELECT,
                        {"imsi": reg_pattern, "interval": f"{days} days"})
            rows = [_reg_row_to_dict(r) for r in cur.fetchall()]
    return _add_gaps(rows)   # two-pass: gaps first (datetime), then stringify


@app.route("/", methods=["GET"])
def index():
    query = request.args.get("q", "").strip()
    results, reg_results, error = [], [], None
    if query:
        try:
            log.info(f"Lookup: {query}")
            results     = lookup(query)
            reg_results = lookup_registration(query)
            log.info(f"  -> calls={len(results)} regs={len(reg_results)}")
        except Exception as e:
            log.exception("DB error")
            error = str(e)
    return render_template_string(HTML, query=query, results=results,
                                  reg_results=reg_results, error=error)


@app.route("/history", methods=["GET"])
def history():
    query = request.args.get("q", "").strip()
    days  = max(1, min(int(request.args.get("days", 7)), 30))
    if not query:
        return jsonify([])
    try:
        rows = lookup_registration_history(query, days)
        log.info(f"History: {query} last {days}d -> {len(rows)} event(s)")
    except Exception as e:
        log.exception("History DB error")
        return jsonify({"error": str(e)}), 500
    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

