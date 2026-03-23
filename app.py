from flask import Flask, request, render_template_string, jsonify
import psycopg2
import psycopg2.extras
import logging
import re
import json as _json
import urllib.request
import subprocess
import os
from ua_mappings import get_android_version


def _require_env(key):
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

# UCN SQLite fallback — add to config.env:
#   UCN_NODES=10.70.1.12,10.70.1.22,10.70.1.112,10.70.1.113,10.70.1.16,10.70.1.26,10.70.1.18,10.70.1.115
#   UCN_USER=root
#   UCN_DB=/etc/yate/ucn/ucn.db
UCN_NODES = [n.strip() for n in os.environ.get("UCN_NODES", "").split(",") if n.strip()]
UCN_USER  = os.environ.get("UCN_USER", "root")
UCN_DB    = os.environ.get("UCN_DB", "/etc/yate/ucn/ucn.db")
UCN_HOSTNAMES = {
    pair.split(":", 1)[0].strip(): pair.split(":", 1)[1].strip()
    for pair in os.environ.get("UCN_HOSTNAMES", "").split(",")
    if ":" in pair
}

UA_EXCLUDE   = re.compile(r"YATE|OpenSIPS|dispatcher|nx-sbc-ocs|NexSBC|VoLTE/WFC", re.IGNORECASE)
_google_cache = {}

# Remote Python sent to stdin over SSH.
# Detects optional columns at runtime — works across all 8 UCN schema variants.
_UCN_SCRIPT = (
    "import sqlite3,json,sys\n"
    "DB=sys.argv[1]; q=sys.argv[2]\n"
    "try:\n"
    "    con=sqlite3.connect(\"file:\"+DB+\"?mode=ro\",uri=True)\n"
    "    con.row_factory=sqlite3.Row\n"
    "    existing=set(c[1] for c in con.execute(\"PRAGMA table_info(cs_regs)\").fetchall())\n"
    "    def col(name,expr=None):\n"
    "        return (expr or (\"r.\"+name)) if name in existing else (\"NULL AS \"+name)\n"
    "    sql=\"\"\"\n"
    "        SELECT c.imsi, c.msisdn,\n"
    "               COALESCE(c.hlr,c.diam_host,c.diam_realm) AS hss,\n"
    "               COALESCE(r.temploc,r.location)            AS pcscf,\n"
    "               r.imei, r.cell_split, r.cell_type,\n"
    "               r.rip, r.rport_s,\n"
    "               DATETIME(r.expires)                       AS expires,\n"
    "               r.reg_interval,\n"
    "               {time_created},\n"
    "               {time_updated},\n"
    "               {type}\n"
    "        FROM cs_core c\n"
    "        LEFT JOIN cs_regs r ON r.imsi=c.imsi\n"
    "        WHERE c.msisdn=? OR c.imsi LIKE ?||'@%' OR c.imsi_detected=?\n"
    "        LIMIT 1\"\"\"\n"
    "    row=con.execute(sql.format(\n"
    "        time_created=col('time_created',\"DATETIME(r.time_created,'unixepoch') AS time_created\"),\n"
    "        time_updated=col('time_updated',\"DATETIME(r.time_updated,'unixepoch') AS time_updated\"),\n"
    "        type=col('type')\n"
    "    ), (q,q,q)).fetchone()\n"
    "    print(json.dumps(dict(row)) if row else json.dumps({\"error\":\"not found\"}))\n"
    "    con.close()\n"
    "except Exception as e:\n"
    "    print(json.dumps({\"error\":str(e)}))\n"
)



def _resolve_cell_from_vlr(cell_split, cell_type):
    """Parse UCN cell_split ('MCC/MNC/TAC_hex/ECI_dec') and attempt Google lookup."""
    if not cell_split:
        return {"label": "-", "url": ""}
    parts = cell_split.split("/")
    if len(parts) != 4:
        return {"label": cell_split, "url": ""}
    try:
        mcc  = int(parts[0])
        mnc  = int(parts[1])
        tac  = int(parts[2], 16)
        ecgi = int(parts[3], 16)
        enb  = ecgi >> 8
        cid  = ecgi & 0xFF
        label  = f"{cell_type or 'e-utran'} eNB:{enb} cell:{cid}"
        coords = _google_lookup(mcc, mnc, tac, ecgi)
        url = (f"https://www.openstreetmap.org/?mlat={coords[0]}&mlon={coords[1]}&zoom=16"
               if coords else "")
        return {"label": label, "url": url}
    except Exception:
        return {"label": cell_split, "url": ""}


def vlr_lookup(query):
    """Query UCN nodes in order, return first SQLite hit or None.
    Called only when Homer has no registration data for the subscriber.
    """
    if not UCN_NODES:
        return None
    q = query.lstrip("+")
    for node in UCN_NODES:
        try:
            r = subprocess.run(
                ["ssh",
                 "-o", "ConnectTimeout=4",
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "BatchMode=yes",
                 f"{UCN_USER}@{node}",
                 f"python3 - {UCN_DB} {q}"],
                input=_UCN_SCRIPT,
                capture_output=True, text=True, timeout=8
            )
            json_lines = [l for l in r.stdout.splitlines() if l.strip().startswith("{")]
            if not json_lines:
                continue
            data = _json.loads(json_lines[-1])
            if "error" in data:
                log.debug("UCN %s: %s (q=%s)", node, data["error"], q)
                continue
            # Normalise IMSI — strip @domain suffix
            if data.get("imsi") and "@" in str(data["imsi"]):
                data["imsi"] = data["imsi"].split("@")[0]
            # pcscf: strip sip/sip:user@ prefix, keep IP:port
            pcscf = data.get("pcscf") or ""
            data["pcscf"] = re.sub(r"^sip/sip:[^@]+@", "", pcscf)
            # Cell resolution via Google
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


def _decode_plmn_ecgi(cell_hex):
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


def resolve_cell(raw_header):
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


def _fmt_gap(seconds):
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m"
    h = seconds // 3600;  m = (seconds % 3600) // 60
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
        try:
            expires_val = int(prev["expires"])
        except (ValueError, TypeError):
            expires_val = 0
        if expires_val > 0 and abs(backward_secs - expires_val) / expires_val <= 0.25:
            badges.append(("🔁 Periodic", "ev-periodic"))
        elif backward_secs > 300:
            badges.append(("📵 Coverage loss", "ev-coverage"))
    if prev.get("user_agent") and prev["user_agent"] != "-" \
            and row["user_agent"] != prev["user_agent"]:
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
            fwd_secs   = int((rows[i-1]["create_date"] - row["create_date"]).total_seconds())
            row["gap"] = _fmt_gap(fwd_secs) if fwd_secs >= 0 else ""
        else:
            row["gap"] = ""
        if row["status"] == "REGISTERED":
            prev     = rows[i+1] if i+1 < n else None
            bwd_secs = int((row["create_date"] - prev["create_date"]).total_seconds()) \
                       if prev is not None else 0
            row["event"], row["event_class"] = _classify_event(row, prev, bwd_secs)
        else:
            row["event"]       = ""
            row["event_class"] = ""
    for row in rows:
        row["create_date"] = str(row["create_date"])
    return rows


HTML = """<!DOCTYPE html>
<html><head>
  <meta charset="utf-8"><title>UA Lookup - Homer DB</title>
  <style>
    body{font-family:monospace;background:#1e1e1e;color:#ddd;padding:2em;max-width:1800px}
    h2,h3{color:#7ec8e3}
    input[type=text]{background:#2d2d2d;border:1px solid #555;color:#fff;padding:6px 10px;width:320px;font-size:14px}
    input[type=submit]{background:#7ec8e3;border:none;padding:6px 16px;font-size:14px;cursor:pointer;color:#000}
    table{border-collapse:collapse;margin-top:1em;width:100%}
    th{background:#333;color:#7ec8e3;padding:8px 12px;text-align:left;white-space:nowrap}
    td{padding:7px 12px;border-bottom:1px solid #333;vertical-align:top}
    tr:hover td{background:#2a2a2a}
    tr.dereg td{opacity:.4}
    tr.dereg:hover td{opacity:.75}
    tr.is-periodic td{opacity:.55}
    tr.is-periodic:hover td{opacity:.85}
    tr.hide-periodic{display:none}
    .reg{color:#7aee9a}.unreg{color:#e37e7e}.warn{color:#e3b97e;margin-top:1em}
    .gap{color:#888;font-size:.82em;white-space:nowrap}
    .reason{color:#888;font-size:.78em;display:block;margin-top:2px}
    .ev-reboot{color:#60a5fa}.ev-airplane{color:#a78bfa}.ev-coverage{color:#f87171}
    .ev-periodic{color:#6b7280}.ev-ua{color:#34d399}.ev-cell{color:#fbbf24}.ev-unknown{color:#9ca3af}
    .info{color:#888;font-size:12px;margin-top:.5em}
    .infobox{margin-top:2em;border:1px solid #444;border-left:4px solid #7ec8e3;background:#252525;padding:1em 1.5em;font-size:13px;color:#aaa}
    .infobox h3{color:#7ec8e3;margin:0 0 .7em;font-size:14px}
    .infobox table{margin-top:.5em;font-size:12px}
    .infobox th{background:#2d2d2d;color:#7ec8e3;padding:5px 10px}
    .infobox td{padding:5px 10px;border-bottom:1px solid #333;opacity:1}
    .yes{color:#7aee9a}.no{color:#e37e7e}.warn2{color:#e3b97e}
    .docs-link{font-size:.78rem;font-weight:600;color:#888;border:1px solid #444;border-radius:6px;padding:5px 12px;text-decoration:none;white-space:nowrap}
    .docs-link:hover{border-color:#7ec8e3;color:#7ec8e3}
    .btn-hist{background:#065f46;border:none;color:#fff;padding:5px 14px;font-size:13px;cursor:pointer;border-radius:4px;margin-top:1em}
    .btn-hist:hover{background:#047857}
    .hist-days{background:#2d2d2d;border:1px solid #555;color:#fff;padding:4px 8px;font-size:13px;border-radius:4px}
    #hist-status{font-size:12px;color:#888;margin-left:.5em}
    #hist-box{margin-top:1em;border-top:1px solid #444;padding-top:1em;display:none}
    #hist-box h3{color:#34d399;margin:0 0 .5em}
    .toggle-bar{display:flex;align-items:center;gap:1.2em;margin:.7em 0;font-size:13px;color:#aaa}
    .toggle-bar label{display:flex;align-items:center;gap:.4em;cursor:pointer}
    .toggle-bar input[type=checkbox]{accent-color:#7ec8e3;width:14px;height:14px;cursor:pointer}
    .cell-info{color:#a78bfa;font-size:.85em}
    .cell-info a{color:#a78bfa;text-decoration:none}
    .cell-info a:hover{color:#c4b5fd;text-decoration:underline}
    .vlr-box{margin-top:1.5em;border:1px solid #555;border-left:4px solid #f0a500;background:#252525;padding:1em 1.5em}
    .vlr-box h3{color:#f0a500;margin:0 0 .8em;font-size:15px;display:flex;align-items:center;gap:.6em}
    .vlr-badge{font-size:.7em;background:#3a2e00;color:#f0a500;border:1px solid #6b4e00;border-radius:4px;padding:2px 8px;font-weight:normal}
    .vlr-box table{margin-top:.3em;font-size:13px}
    .vlr-box th{background:#2d2d2d;color:#f0a500;padding:6px 12px;white-space:nowrap}
    .vlr-box td{padding:6px 12px;border-bottom:1px solid #333}
    .vlr-note{color:#666;font-size:.8em;margin-top:.8em;font-style:italic}
  </style>
  <script>
    function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
    function cellHtml(label,url){
      if(!url) return esc(label);
      return '<a href="'+esc(url)+'" target="_blank" rel="noopener">&#128205; '+esc(label)+'</a>';
    }
    function togglePeriodic(cb){
      document.querySelectorAll('#hist-body tr.is-periodic').forEach(function(tr){
        tr.classList.toggle('hide-periodic', cb.checked);
      });
      var count=document.querySelectorAll('#hist-body tr.is-periodic').length;
      document.getElementById('periodic-count').textContent=cb.checked&&count?'('+count+' hidden)':'';
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

    {% if vlr %}
    <div class="vlr-box">
      <h3>&#128225; Live VLR Registration <span class="vlr-badge">{{ vlr.ucn_node_label }}</span></h3>
      <table>
        <tr>
          <th>IMSI</th><td>{{ vlr.imsi or "-" }}</td>
          <th>MSISDN</th><td>{{ vlr.msisdn or "-" }}</td>
          <th>IMEI</th><td>{{ vlr.imei or "-" }}</td>
          <th>Type</th><td>{{ vlr.type or "-" }}</td>
        </tr><tr>
          <th>P-CSCF</th><td>{{ vlr.pcscf or "-" }}</td>
          <th>Reg interval</th><td>{{ vlr.reg_interval or "-" }}s</td>
          <th>Expires</th><td colspan="3">{{ vlr.expires or "-" }}</td>
        </tr><tr>
          <th>Registered</th><td>{{ vlr.time_created or "-" }}</td>
          <th>Last updated</th><td>{{ vlr.time_updated or "-" }}</td>
          <th>HSS</th><td colspan="3">{{ vlr.hss or "-" }}</td>
        </tr><tr>
          <th>Cell</th>
          <td colspan="7" class="cell-info">
            {% if vlr.cell_url %}<a href="{{ vlr.cell_url }}" target="_blank">&#128205; {{ vlr.cell_label }}</a>
            {% else %}{{ vlr.cell_label or "-" }}{% endif %}
          </td>
        </tr>
      </table>
      <p class="vlr-note">&#9888;&#65039; No SIP capture in Homer for this subscriber &mdash;
        data sourced live from YATE VLR SQLite on {{ vlr.ucn_node_label }}.
        User-Agent not stored in this DB.</p>
    </div>
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
      <div class="toggle-bar">
        <label><input type="checkbox" id="hide-periodic" onchange="togglePeriodic(this)">
          Hide periodic re-registrations <span id="periodic-count" style="color:#6b7280"></span>
        </label>
      </div>
      <table id="hist-table">
        <thead><tr>
          <th>Timestamp</th><th>Gap &#8593;</th><th>Event</th><th>Status</th><th>IMSI</th>
          <th>Src IP (APN)</th><th>User-Agent</th><th>Expires (s)</th><th>Contact</th><th>Cell</th>
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
        var hideCb=document.getElementById('hide-periodic');
        status.textContent='Loading...';
        box.style.display='none'; tbody.innerHTML=''; empty.style.display='none';
        fetch('/history?q={{ query|urlencode }}&days='+days)
          .then(function(r){return r.json();})
          .then(function(rows){
            status.textContent='';
            if(!rows.length){empty.style.display='block';}
            else{rows.forEach(function(r){
              var isReg=r.status==='REGISTERED';
              var statusCls=isReg?'reg':'unreg';
              var cls=[];
              if(!isReg) cls.push('dereg');
              if(r.event_class==='ev-periodic') cls.push('is-periodic');
              if(hideCb.checked && r.event_class==='ev-periodic') cls.push('hide-periodic');
              var rowCls=cls.length?' class="'+cls.join(' ')+'"':'';
              var gapHtml=r.gap?'<span class="gap">'+esc(r.gap)+'</span>':'';
              var evHtml=r.event?'<span class="'+esc(r.event_class)+'">'+esc(r.event)+'</span>':'';
              if(!isReg && r.reason) evHtml+='<span class="reason">'+esc(r.reason)+'</span>';
              tbody.innerHTML+=
                '<tr'+rowCls+'>'+
                '<td>'+esc(r.create_date)+'</td>'+
                '<td>'+gapHtml+'</td>'+
                '<td>'+evHtml+'</td>'+
                '<td class="'+statusCls+'">'+esc(r.status)+'</td>'+
                '<td>'+esc(r.imsi)+'</td>'+
                '<td>'+esc(r.src_ip)+'</td>'+
                '<td>'+esc(r.user_agent)+'</td>'+
                '<td>'+esc(r.expires)+'</td>'+
                '<td>'+esc(r.contact)+'</td>'+
                '<td class="cell-info">'+cellHtml(r.cell_label,r.cell_url)+'</td>'+
                '</tr>';
            });
            var pc=document.querySelectorAll('#hist-body tr.is-periodic').length;
            document.getElementById('periodic-count').textContent=
              hideCb.checked&&pc?'('+pc+' hidden)':'';}
            box.style.display='block';
          })
          .catch(function(e){status.textContent='Error: '+e;});
      }
    </script>

  {% endif %}{% endif %}

  <div class="infobox">
    <h3>&#9889; Registration History &mdash; Event column legend</h3>
    <table>
      <tr><th>Badge</th><th>Meaning</th><th>How detected</th></tr>
      <tr><td>🔄 Reboot</td><td>Device rebooted</td><td>UNREGISTERED preceded this reg, gap &lt; 90s</td></tr>
      <tr><td>✈️ Airplane / Off</td><td>Airplane mode or powered off intentionally</td><td>UNREGISTERED preceded this reg, gap &ge; 90s</td></tr>
      <tr><td>📵 Coverage loss</td><td>Device lost signal or crashed without deregistering</td><td>Previous event was REGISTERED, gap &gt; 5 min, no Expires:0</td></tr>
      <tr><td>🔁 Periodic</td><td>Normal keep-alive re-registration</td><td>Gap &asymp; Expires value (&plusmn;25%), previous event was REGISTERED</td></tr>
      <tr><td>🆕 UA changed</td><td>User-Agent changed since last event</td><td>Different UA string vs previous row (OS update, new device, SIM swap)</td></tr>
      <tr><td>📍 Cell changed</td><td>Device moved to a different antenna</td><td>Different cell ID vs previous row</td></tr>
      <tr><td>❓ First in window</td><td>Oldest event in selected time window</td><td>No previous row to compare against &mdash; extend the window for more context</td></tr>
    </table>
    <p style="margin-top:.8em;color:#666;font-size:.85em;">
      Multiple badges can appear on one row (e.g. <span class="ev-airplane">&#9992;&#65039; Airplane / Off</span> &middot; <span class="ev-ua">&#128;&#141; UA changed</span>).
      UNREGISTERED rows are dimmed &mdash; any SIP <code>Reason:</code> header is shown beneath them.
    </p>
  </div>

  <div class="infobox" style="margin-top:1em;">
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


def lookup(pattern):
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
        "create_date":  row["create_date"],
        "imsi":         row["imsi"]        or "",
        "src_ip":       row["src_ip"]      or "",
        "user_agent":   row["user_agent"]  or "-",
        "expires":      expires,
        "status":       "REGISTERED" if expires != "0" else "UNREGISTERED",
        "contact":      row["contact"]     or "-",
        "reason":       (row["reason"]     or "").strip(),
        "cell_label":   cell["label"],
        "cell_url":     cell["url"],
        "gap":          "",
        "event":        "",
        "event_class":  "",
    }


REG_SELECT = """
    SELECT create_date,
           protocol_header->>'srcIp'                                  AS src_ip,
           data_header->>'from_user'                                   AS imsi,
           data_header->>'user_agent'                                  AS user_agent,
           substring(raw FROM 'Expires: ([0-9]+)')                     AS expires,
           substring(raw FROM 'Contact: ([^\\r\\n]+)')                 AS contact,
           substring(raw FROM 'P-Access-Network-Info: ([^\\r\\n]+)')   AS access_info,
           substring(raw FROM 'Reason: ([^\\r\\n]+)')                  AS reason
    FROM hep_proto_1_registration
    WHERE create_date > NOW() - %(interval)s::interval
      AND data_header->>'from_user' = %(imsi)s
      AND raw NOT LIKE 'SIP/2.0%%'
    ORDER BY create_date DESC
"""


def lookup_registration(pattern):
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


def lookup_registration_history(pattern, days):
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
    return _add_gaps_and_classify(rows)


@app.route("/", methods=["GET"])
def index():
    query = request.args.get("q", "").strip()
    results, reg_results, vlr, error = [], [], None, None
    if query:
        try:
            log.info("Lookup: %s", query)
            results     = lookup(query)
            reg_results = lookup_registration(query)
            log.info("  -> calls=%d regs=%d", len(results), len(reg_results))
            if not reg_results and UCN_NODES:
                vlr = vlr_lookup(query)
                log.info("  -> vlr=%s", "hit on " + vlr["ucn_node"] if vlr else "miss")
        except Exception as e:
            log.exception("DB error")
            error = str(e)
    return render_template_string(HTML, query=query, results=results,
                                  reg_results=reg_results, vlr=vlr, error=error)


@app.route("/history", methods=["GET"])
def history():
    query = request.args.get("q", "").strip()
    days  = max(1, min(int(request.args.get("days", 7)), 30))
    if not query:
        return jsonify([])
    try:
        rows = lookup_registration_history(query, days)
        log.info("History: %s last %dd -> %d event(s)", query, days, len(rows))
    except Exception as e:
        log.exception("History DB error")
        return jsonify({"error": str(e)}), 500
    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

