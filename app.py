from flask import Flask, request, render_template_string, jsonify
import psycopg2
import psycopg2.extras
import logging
import re
from ua_mappings import get_android_version

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
    "host":     "172.19.0.6",
    "port":     5432,
    "dbname":   "homer_data",
    "user":     "root",
    "password": "homerSeven",
}

UA_EXCLUDE = re.compile(r"YATE|OpenSIPS|dispatcher|nx-sbc-ocs|NexSBC|VoLTE/WFC", re.IGNORECASE)

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>UA Lookup - Homer DB</title>
  <style>
    body { font-family: monospace; background: #1e1e1e; color: #ddd; padding: 2em; max-width: 1400px; }
    h2, h3 { color: #7ec8e3; }
    input[type=text] {
      background: #2d2d2d; border: 1px solid #555; color: #fff;
      padding: 6px 10px; width: 320px; font-size: 14px;
    }
    input[type=submit] {
      background: #7ec8e3; border: none; padding: 6px 16px;
      font-size: 14px; cursor: pointer; color: #000;
    }
    table { border-collapse: collapse; margin-top: 1em; width: 100%; }
    th { background: #333; color: #7ec8e3; padding: 8px 12px; text-align: left; white-space: nowrap; }
    td { padding: 7px 12px; border-bottom: 1px solid #333; }
    tr:hover td { background: #2a2a2a; }
    .reg   { color: #7aee9a; }
    .unreg { color: #e37e7e; }
    .warn  { color: #e3b97e; margin-top: 1em; }
    .info  { color: #888; font-size: 12px; margin-top: 0.5em; }
    .infobox {
      margin-top: 2em; border: 1px solid #444;
      border-left: 4px solid #7ec8e3; background: #252525;
      padding: 1em 1.5em; font-size: 13px; color: #aaa;
    }
    .infobox h3 { color: #7ec8e3; margin: 0 0 0.7em 0; font-size: 14px; }
    .infobox table { margin-top: 0.5em; font-size: 12px; }
    .infobox th { background: #2d2d2d; color: #7ec8e3; padding: 5px 10px; }
    .infobox td { padding: 5px 10px; border-bottom: 1px solid #333; }
    .yes  { color: #7aee9a; }
    .no   { color: #e37e7e; }
    .warn2 { color: #e3b97e; }
    .docs-link {
      font-size: 0.78rem; font-weight: 600; color: #888;
      border: 1px solid #444; border-radius: 6px; padding: 5px 12px;
      text-decoration: none; transition: border-color .15s, color .15s; white-space: nowrap;
    }
    .docs-link:hover { border-color: #7ec8e3; color: #7ec8e3; text-decoration: none; }
    .btn-hist       { background:#065f46; border:none; color:#fff; padding:5px 14px;
                      font-size:13px; cursor:pointer; border-radius:4px; margin-top:1em; }
    .btn-hist:hover { background:#047857; }
    .hist-days      { background:#2d2d2d; border:1px solid #555; color:#fff;
                      padding:4px 8px; font-size:13px; border-radius:4px; }
    #hist-status    { font-size:12px; color:#888; margin-left:0.5em; }
    #hist-box       { margin-top:1em; border-top:1px solid #444; padding-top:1em; display:none; }
    #hist-box h3    { color:#34d399; margin:0 0 0.5em; }
    #hist-table td.reg   { color:#7aee9a; }
    #hist-table td.unreg { color:#e37e7e; }
  </style>
  <script>
    /* Defined in <head> so it is always available regardless of Jinja2 conditionals.
       Escapes <sip:IP:port> and similar values before inserting into innerHTML,
       preventing the browser from interpreting them as HTML tags. */
    function esc(s) {
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    }
  </script>
</head>
<body>
  <h2 style="display:flex; align-items:center; gap:1em;">
    &#128241; User-Agent Lookup &mdash; Homer DB
    <a class="docs-link" href="https://wiki.nexphone.ch/display/NETVOIP/User-Agent+Lookup" target="_blank" rel="noopener">&#128214; Docs</a>
  </h2>
  <form method="get">
    <input type="text" name="q" placeholder="MSISDN or IMSI (partial ok)"
           value="{{ query }}" autofocus>
    <input type="submit" value="Search">
    <span class="info">&nbsp;Searches INVITE/BYE records, last 24 hours</span>
  </form>

  {% if query %}
    {% if error %}
      <p class="warn">&#9888; {{ error }}</p>
    {% else %}

      <h3 style="margin-top:1.5em;">&#128222; Recent Calls (INVITE/BYE)</h3>
      {% if not results %}
        <p class="warn">No call UA found for <b>{{ query }}</b> &mdash; subscriber may not have VoLTE active (see note below)</p>
      {% else %}
        <table>
          <tr>
            <th>Timestamp</th><th>User-Agent</th><th>OS Version</th>
            <th>From</th><th>To</th><th>Src IP (APN)</th><th>Method</th>
          </tr>
          {% for r in results %}
          <tr>
            <td>{{ r.create_date }}</td><td>{{ r.user_agent }}</td><td>{{ r.os_version }}</td>
            <td>{{ r.from_user }}</td><td>{{ r.to_user }}</td><td>{{ r.src_ip }}</td><td>{{ r.method }}</td>
          </tr>
          {% endfor %}
        </table>
      {% endif %}

      <h3 style="margin-top:2em;">&#128275; Last IMS Registration</h3>
      {% if not reg_results %}
        <p class="warn">No registration record found for <b>{{ query }}</b> in the last 7 days</p>
      {% else %}
        <table>
          <tr>
            <th>Timestamp</th><th>Status</th><th>IMSI</th>
            <th>Src IP (APN)</th><th>User-Agent</th><th>Expires (s)</th><th>Contact</th>
          </tr>
          {% for r in reg_results %}
          <tr>
            <td>{{ r.create_date }}</td>
            <td class="{{ 'reg' if r.status == 'REGISTERED' else 'unreg' }}">{{ r.status }}</td>
            <td>{{ r.imsi }}</td><td>{{ r.src_ip }}</td><td>{{ r.user_agent }}</td>
            <td>{{ r.expires }}</td><td>{{ r.contact }}</td>
          </tr>
          {% endfor %}
        </table>
      {% endif %}

      <!-- Registration History -->
      <div style="display:flex;align-items:center;gap:0.8em;margin-top:1em;">
        <button class="btn-hist" onclick="loadHistory()">&#128197; Registration History</button>
        <label style="color:#aaa;font-size:13px;">Last
          <select class="hist-days" id="hist-days">
            <option value="1">1 day</option>
            <option value="3">3 days</option>
            <option value="7" selected>7 days</option>
            <option value="14">14 days</option>
            <option value="30">30 days</option>
          </select>
        </label>
        <span id="hist-status"></span>
      </div>
      <div id="hist-box">
        <h3>&#128197; Registration History</h3>
        <table id="hist-table">
          <thead>
            <tr>
              <th>Timestamp</th><th>Status</th><th>IMSI</th>
              <th>Src IP (APN)</th><th>User-Agent</th><th>Expires (s)</th><th>Contact</th>
            </tr>
          </thead>
          <tbody id="hist-body"></tbody>
        </table>
        <p id="hist-empty" style="display:none;color:#e3b97e;">No registration events found for this period.</p>
      </div>
      <script>
        function loadHistory() {
          var days   = document.getElementById('hist-days').value;
          var status = document.getElementById('hist-status');
          var box    = document.getElementById('hist-box');
          var tbody  = document.getElementById('hist-body');
          var empty  = document.getElementById('hist-empty');
          status.textContent  = 'Loading...';
          box.style.display   = 'none';
          tbody.innerHTML     = '';
          empty.style.display = 'none';
          fetch('/history?q={{ query|urlencode }}&days=' + days)
            .then(function(r) { return r.json(); })
            .then(function(rows) {
              status.textContent = '';
              if (!rows.length) {
                empty.style.display = 'block';
              } else {
                rows.forEach(function(r) {
                  var cls = r.status === 'REGISTERED' ? 'reg' : 'unreg';
                  tbody.innerHTML +=
                    '<tr><td>' + esc(r.create_date) + '</td>' +
                    '<td class="' + cls + '">' + esc(r.status)     + '</td>' +
                    '<td>'        + esc(r.imsi)       + '</td>' +
                    '<td>'        + esc(r.src_ip)     + '</td>' +
                    '<td>'        + esc(r.user_agent) + '</td>' +
                    '<td>'        + esc(r.expires)    + '</td>' +
                    '<td>'        + esc(r.contact)    + '</td></tr>';
                });
              }
              box.style.display = 'block';
            })
            .catch(function(e) { status.textContent = 'Error: ' + e; });
        }
      </script>

    {% endif %}
  {% endif %}

  <div class="infobox">
    <h3>&#8505; How User-Agent detection works</h3>
    <p>The User-Agent is only visible when the <b>device itself</b> originates the SIP message
    directly into the IMS core (VoLTE/WiFi Calling). For circuit-switched calls (2G/3G/CSFB),
    the MSC/MGCF converts the call to SIP and sets its own UA &mdash; the device UA is never seen.</p>
    <table>
      <tr><th>Scenario</th><th>UA visible?</th><th>What you see</th></tr>
      <tr><td>VoLTE call (4G/5G)</td><td class="yes">&#10003; Yes</td><td>iOS/26.3 iPhone, Samsung IMS 6.0, ...</td></tr>
      <tr><td>WiFi Calling (WFC)</td><td class="yes">&#10003; Yes</td><td>iOS/26.3 iPhone, ...</td></tr>
      <tr><td>CS Fallback (CSFB)</td><td class="no">&#10007; No</td><td>YATE/6.4.1 or empty (filtered out)</td></tr>
      <tr><td>2G/3G CS via MGCF</td><td class="no">&#10007; No</td><td>YATE/6.4.1 or empty (filtered out)</td></tr>
      <tr><td>IMS REGISTER</td><td class="warn2">&#9888; Sometimes</td><td>UA may be stripped by SBC/proxy</td></tr>
    </table>
    <p style="margin-top:0.8em;">
      <b>Src IP</b> is the device&#39;s <b>APN/PDN address</b> assigned by the PGW &mdash; not a public IP.
      It changes per PDN session. No UA result = subscriber likely on 2G/3G or VoLTE not enabled.
    </p>
  </div>
</body>
</html>
"""


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '15s'")
    cur.close()
    return conn


def lookup(pattern: str):
    pattern = pattern.lstrip("+")
    sql = """
        SELECT
            create_date,
            protocol_header->>'srcIp'         AS src_ip,
            data_header->>'from_user'          AS from_user,
            data_header->>'to_user'            AS to_user,
            data_header->>'user_agent'         AS user_agent,
            data_header->>'method'             AS method
        FROM hep_proto_1_call
        WHERE create_date > NOW() - INTERVAL '24 hours'
          AND (
              data_header->>'from_user' IN (%(exact)s, %(with_plus)s)
              OR data_header->>'to_user' IN (%(exact)s, %(with_plus)s)
          )
          AND data_header->>'user_agent' IS NOT NULL
          AND data_header->>'user_agent' != ''
          AND data_header->>'method' IN ('INVITE', 'BYE')
        ORDER BY create_date DESC
        LIMIT 500
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
        key = ua
        if key not in seen:
            seen[key] = {
                "create_date": row["create_date"],
                "user_agent":  ua,
                "os_version":  get_android_version(ua),
                "from_user":   row["from_user"] or "",
                "to_user":     row["to_user"] or "",
                "src_ip":      row["src_ip"] or "",
                "method":      row["method"] or "",
            }
    return sorted(seen.values(), key=lambda x: str(x["create_date"]), reverse=True)


def lookup_registration(pattern: str):
    pattern = pattern.lstrip("+")
    is_imsi = bool(re.match(r'^228\d{12}$', pattern))

    if is_imsi:
        reg_pattern = pattern
    else:
        imsi_sql = """
            SELECT data_header->>'from_user' AS imsi
            FROM hep_proto_1_registration
            WHERE create_date > NOW() - INTERVAL '2 hours'
              AND raw ILIKE %(pat)s
            ORDER BY create_date DESC
            LIMIT 1
        """
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(imsi_sql, {"pat": f"%{pattern}%"})
                row = cur.fetchone()
        if not row or not row["imsi"]:
            return []
        reg_pattern = row["imsi"]

    reg_sql = """
        SELECT
            create_date,
            protocol_header->>'srcIp'               AS src_ip,
            data_header->>'from_user'                AS imsi,
            data_header->>'user_agent'               AS user_agent,
            substring(raw FROM 'Expires: ([0-9]+)')  AS expires,
            substring(raw FROM 'Contact: ([^\r\n]+)') AS contact
        FROM hep_proto_1_registration
        WHERE create_date > NOW() - INTERVAL '7 days'
          AND data_header->>'from_user' = %(imsi)s
        ORDER BY create_date DESC
        LIMIT 1
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(reg_sql, {"imsi": reg_pattern})
            rows = cur.fetchall()

    results = []
    for row in rows:
        expires = (row["expires"] or "0").strip()
        results.append({
            "create_date": row["create_date"],
            "imsi":        row["imsi"] or "",
            "src_ip":      row["src_ip"] or "",
            "user_agent":  row["user_agent"] or "-",
            "expires":     expires,
            "status":      "REGISTERED" if expires != "0" else "UNREGISTERED",
            "contact":     row["contact"] or "-",
        })
    return results


def lookup_registration_history(pattern: str, days: int):
    pattern = pattern.lstrip("+")
    is_imsi = bool(re.match(r'^228\d{12}$', pattern))

    if is_imsi:
        reg_pattern = pattern
    else:
        # Reuse lookup_registration() for IMSI resolution — uses the fast
        # 2-hour window, avoids slow raw ILIKE scan over large date ranges
        existing = lookup_registration(pattern)
        if not existing:
            return []
        reg_pattern = existing[0]["imsi"]

    reg_sql = """
        SELECT
            create_date,
            protocol_header->>'srcIp'                AS src_ip,
            data_header->>'from_user'                 AS imsi,
            data_header->>'user_agent'                AS user_agent,
            substring(raw FROM 'Expires: ([0-9]+)')   AS expires,
            substring(raw FROM 'Contact: ([^\r\n]+)') AS contact
        FROM hep_proto_1_registration
        WHERE create_date > NOW() - %(interval)s::interval
          AND data_header->>'from_user' = %(imsi)s
        ORDER BY create_date DESC
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(reg_sql, {"imsi": reg_pattern, "interval": f"{days} days"})
            rows = cur.fetchall()

    results = []
    for row in rows:
        expires = (row["expires"] or "0").strip()
        results.append({
            "create_date": str(row["create_date"]),
            "imsi":        row["imsi"] or "",
            "src_ip":      row["src_ip"] or "",
            "user_agent":  row["user_agent"] or "-",
            "expires":     expires,
            "status":      "REGISTERED" if expires != "0" else "UNREGISTERED",
            "contact":     row["contact"] or "-",
        })
    return results


@app.route("/", methods=["GET"])
def index():
    query = request.args.get("q", "").strip()
    results = []
    reg_results = []
    error = None

    if query:
        try:
            log.info(f"Lookup: {query}")
            results = lookup(query)
            log.info(f"  -> call UA: {len(results)} result(s)")
            reg_results = lookup_registration(query)
            log.info(f"  -> registration: {len(reg_results)} result(s)")
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

