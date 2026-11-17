# UA Lookup — User-Agent Lookup via Homer SIPCapture

A lightweight Flask web application for looking up the **SIP User-Agent** (device type and OS version) of a mobile subscriber by **MSISDN** or **IMSI**, querying the [HOMER SIPCapture](https://github.com/sipcapture/homer) PostgreSQL database directly.

---

## Overview

When a subscriber makes a VoLTE or WiFi Calling call, their device includes a `User-Agent` header in the SIP INVITE/BYE message. This tool queries the Homer database to surface that information, helping support teams and network engineers quickly identify which device and OS version a subscriber is using — without needing access to HSS/HLR or device management systems.

It also provides a full **IMS registration history** with automatic event classification (coverage loss, reboots, airplane mode, periodic keep-alives) and **cell geolocation** via the Google Geolocation API.

### What it can detect

| Scenario | UA visible? | Example |
|---|---|---|
| VoLTE call (4G/5G) | ✅ Yes | `iOS/26.3 iPhone`, `SM-S936B Samsung IMS 6.0` |
| WiFi Calling (WFC) | ✅ Yes | `iOS/26.3 iPhone` |
| CS Fallback (CSFB) | ❌ No | `YATE/6.4.1` (filtered out) |
| 2G/3G CS via MGCF | ❌ No | `YATE/6.4.1` (filtered out) |
| IMS REGISTER | ⚠️ Sometimes | UA may be stripped by SBC/proxy |

> **Note:** Subscribers on 2G/3G or with VoLTE disabled will return no results.
> The device UA is never injected into SIP by the network core for CS calls.

---

## Architecture

```
Subscriber device (VoLTE/WFC)
        │  SIP INVITE/BYE/REGISTER with User-Agent header
        ▼
  OpenSIPS / YATE IMS Core
        │  HEP mirror via heplify agent
        ▼
  heplify-server (Docker)  ← listens on UDP/TCP 9060-9061
        │  writes to PostgreSQL
        ▼
  PostgreSQL (Docker) — homer_data database
        │
        ▼
  ua-lookup (app.py)  ──► Flask web UI — port 5000
        │
        ▼
  Google Geolocation API  ← cell tower → GPS coordinates
```

### Homer Docker stack (relevant services)

| Service | Role |
|---|---|
| `heplify-server` | Receives HEP traffic, writes to DB |
| `db` | PostgreSQL container (`homer_data`) |

The app runs **on the Docker host** and connects to the DB container via its Docker bridge IP, since port 5432 is not exposed externally.

---

## Project Structure

```
/opt/ua-lookup/
├── app.py                    # Flask application — main entry point
├── ua_mappings.py            # Device/OS mapping for Pixel, Xiaomi, Fairphone and iOS
├── samsung_firmware.py       # Exact, verified Samsung firmware metadata lookup
├── samsung_firmware_admin.py # Guarded operator importer for official Samsung pages
├── data/
│   └── samsung_firmware_catalog.json # Bundled verified Samsung records
├── config.env                # Credentials and secrets (NOT committed to VCS)
├── venv/                     # Python virtual environment
└── /var/log/ua-lookup.log    # Application log file
/etc/systemd/system/
└── ua-lookup.service         # systemd unit file
```

---

## Requirements

- Python **3.8+**
- Homer SIPCapture stack running with `heplify-server` and PostgreSQL
- Network access from the app host to the Docker bridge IP of the DB container
- Google Cloud project with the **Geolocation API** enabled (for cell tower → GPS resolution)

### Python dependencies

```
flask
psycopg2-binary
```

Install:
```bash
cd /opt/ua-lookup
python3 -m venv venv
source venv/bin/activate
pip install flask psycopg2-binary
```

---

## Configuration

All credentials are stored in `/opt/ua-lookup/config.env` and loaded by the systemd service via `EnvironmentFile`. **Never commit this file to VCS.**

```ini
# /opt/ua-lookup/config.env
DB_HOST=172.19.0.6          # Docker bridge IP of the db container
DB_PORT=5432                # optional, defaults to 5432
DB_NAME=homer_data
DB_USER=root
DB_PASSWORD=your_password
GOOGLE_API_KEY=your_key     # Google Geolocation API key
```

> To find the current Docker bridge IP of the DB container:
> ```bash
> docker inspect db --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
> ```
> Update `DB_HOST` in `config.env` if it changes after a container restart.

### Filtered User-Agents

The following UA patterns are excluded from results (network infrastructure, not end devices):

```python
UA_EXCLUDE = re.compile(r"YATE|OpenSIPS|dispatcher|nx-sbc-ocs|NexSBC|VoLTE/WFC", re.IGNORECASE)
```

---

## Homer Database Tables

| Table | Content | Used for |
|---|---|---|
| `hep_proto_1_call` | INVITE/BYE messages | Device UA lookup |
| `hep_proto_1_registration` | REGISTER messages | IMS registration history |

Both tables are **partitioned by 12-hour intervals** (e.g. `hep_proto_1_call_20260306_0000`). The parent table is queried directly — PostgreSQL fans out to the relevant partitions automatically.

### Key JSONB fields

```
data_header->''from_user''    — calling MSISDN or IMSI
data_header->''to_user''      — called MSISDN or IMSI
data_header->''user_agent''   — SIP User-Agent string
data_header->''method''       — SIP method (INVITE, BYE, REGISTER...)
protocol_header->''srcIp''    — source IP of the SIP packet (APN/PDN address)
raw                           — full raw SIP message text (used for substring extraction)
```

The `raw` field is used to extract headers not in `data_header`: `Expires:`, `Contact:`, `P-Access-Network-Info:`, `Reason:`.

---

## Installation

### 1. Copy files

```bash
mkdir -p /opt/ua-lookup
cp app.py ua_mappings.py /opt/ua-lookup/
```

### 2. Create config.env

```bash
cat > /opt/ua-lookup/config.env << 'EOF'
DB_HOST=172.19.0.6
DB_PORT=5432
DB_NAME=homer_data
DB_USER=root
DB_PASSWORD=changeme
GOOGLE_API_KEY=changeme
EOF
chmod 600 /opt/ua-lookup/config.env
```

### 3. Create virtual environment and install deps

```bash
cd /opt/ua-lookup
python3 -m venv venv
source venv/bin/activate
pip install flask psycopg2-binary
```

### 4. Install systemd service

```bash
cp ua-lookup.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ua-lookup
systemctl status ua-lookup
```

### 5. Verify

```bash
curl http://localhost:5000
```

---

## systemd Service (`ua-lookup.service`)

```ini
[Unit]
Description=UA Lookup - Homer DB
After=network.target docker.service

[Service]
WorkingDirectory=/opt/ua-lookup
EnvironmentFile=/opt/ua-lookup/config.env
ExecStart=/opt/ua-lookup/venv/bin/python app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

## HTTP Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Main web UI — search form and results |
| `GET` | `/history?q=<msisdn_or_imsi>&days=<1-30>` | JSON array of registration history rows |

---

## Usage

Open `http://<host>:5000` in a browser.

Enter a **MSISDN** or **IMSI** in the search box:

| Input | Example |
|---|---|
| MSISDN without prefix | `4178480xxxx` |
| MSISDN with + prefix | `+4178480xxxx` |
| IMSI (15 digits) | `228580000xxxxxx` |

The `+` prefix is stripped automatically before querying.

### 📞 Recent Calls (INVITE/BYE)

Queries `hep_proto_1_call`, last **24 hours**. Shows one row per unique User-Agent seen, most recent first.

| Column | Description |
|---|---|
| Timestamp | When the SIP message was captured |
| User-Agent | Raw SIP UA string |
| OS Version | Decoded device name and Android/iOS version |
| From | Calling party MSISDN |
| To | Called party MSISDN or IMSI |
| Src IP (APN) | Device's PDN/APN IP assigned by PGW |
| Method | INVITE or BYE |

### 🔓 Last IMS Registration

Queries `hep_proto_1_registration`, last **7 days**. Shows the most recent REGISTER record.

- **IMSI input** → direct indexed query on `data_header->>'from_user'`
- **MSISDN input** → scans `raw` field (last 2 hours) to resolve IMSI, then queries by IMSI

> If a subscriber has no registration record but has call records, the SBC is likely mirroring only SIP responses (200 OK / 401) to Homer, not the REGISTER requests. See [Diagnostics](#diagnostics).

### 📅 Registration History

Click **Registration History** and select a time window (1–30 days). Loads all REGISTER events as a JSON-rendered table with:

- **Gap ↑** — time elapsed since the previous event
- **Event** — automatic classification badge(s) — see legend below
- UNREGISTERED rows are dimmed; any SIP `Reason:` header is shown beneath them
- **Hide periodic** checkbox — filters out routine keep-alive rows

---

## Event Column — Badge Legend

| Badge | Meaning | How detected |
|---|---|---|
| 🔄 Reboot | Device rebooted | UNREGISTERED preceded this reg, gap < 90s |
| ✈️ Airplane / Off | Airplane mode or powered off | UNREGISTERED preceded this reg, gap ≥ 90s |
| 📵 Coverage loss | Lost signal without deregistering | Previous event was REGISTERED, gap > 5 min |
| 🔁 Periodic | Normal keep-alive re-registration | Gap ≈ Expires value (±25%), prev was REGISTERED |
| 🆕 UA changed | User-Agent changed since last event | Different UA string vs previous row |
| 📍 Cell changed | Device moved to a different antenna | Different cell ID vs previous row |
| ❓ First in window | Oldest event in selected window | No prior row — extend the window for more context |

Multiple badges can appear on one row (e.g. `✈️ Airplane / Off · 🆕 UA changed`).

> **iOS note:** Apple devices do not send `REGISTER Expires: 0` when entering airplane mode or powering off. All iOS disappearances will appear as 📵 Coverage loss regardless of actual cause.

---

## Cell Geolocation

When a `P-Access-Network-Info` header is present in a REGISTER message, the app:

1. Parses the `utran-cell-id-3gpp` hex value to extract **MCC, MNC, TAC, eNB ID, cell ID**
2. Queries the **Google Geolocation API** (`googleapis.com/geolocation/v1/geolocate`) with the LTE cell tower info
3. Displays the result as `📍 E-UTRAN eNB:<id> cell:<id>` with a clickable **OpenStreetMap** link

Coordinates are cached in memory for the lifetime of the process to avoid redundant API calls.

---

## Device and OS Version Mapping

Non-Samsung User-Agents are decoded by `get_android_version(ua)` in
`ua_mappings.py`. Samsung firmware metadata is accepted only when the exact
model and build number exist in the verified local catalog.

| UA string | Decoded output |
|---|---|
| `SM-A165F-A165FXXUBEZJ7 Samsung IMS 6.0` | `Galaxy A16 — Android 17` (verified) |
| `SM-A546B-A546BXXUNGZJ5 Samsung IMS 6.0` | `Unknown — not verified` |
| `Xiaomi_Xiaomi 15T Pro_OS3.0.13.0.WOSEUXM` | `Xiaomi 15T Pro — Android 16` |
| `Google_Pixel8_Android 16_BP4A.260205.001` | `Pixel8 — Android 16` |
| `Fairphone_Fairphone 6_FP6.QREL.15.151.0` | `Fairphone 6 — Android 15` |
| `iOS/26.3 iPhone` | `iOS 26` |

### Samsung firmware verification

Samsung build strings are not decoded heuristically. The monitor performs an
exact `(model code, build number)` lookup against records obtained from
`doc.samsungmobile.com`. A match shows the verified Android version, firmware
release date, security patch level and a link to the official record. An
unknown build shows **Unknown — not verified** and a one-click Google search
restricted to the official Samsung documentation domain.

After locating the exact official English firmware page, an operator can add
it to the runtime catalog without editing application source:

```bash
cd /opt/ua-lookup
./venv/bin/python samsung_firmware_admin.py import-url \
  'https://doc.samsungmobile.com/SM-A165F/EUX/doc.html'
```

The importer accepts HTTPS pages only from the exact
`doc.samsungmobile.com` host, validates redirects and content, requires the
page model/build to match, and writes atomically. Registered pages can be
refreshed later:

```bash
./venv/bin/python samsung_firmware_admin.py refresh
```

Runtime records and their source registry are stored in
`/var/lib/ua-lookup/samsung-firmware-cache.json` and
`/var/lib/ua-lookup/samsung-firmware-sources.json`. Page rendering never
contacts any external service.

#### Automatic Samsung enrichment

`samsung_firmware_enrich.py` probes Samsung's predictable official document
path for unknown builds shown by the missing-MMTEL monitor. It does not use a
search provider, an LLM, or an additional API key. Metadata is committed only
when the official Samsung page contains an exact match for both the complete
model code and complete firmware build number.

Optional limits (shown with their defaults):

```ini
SAMSUNG_CSC_CANDIDATES=EUX,AUT,SWC
SAMSUNG_ENRICH_MAX_IDENTITIES=5
SAMSUNG_ENRICH_NOT_FOUND_RETRY_HOURS=24
SAMSUNG_ENRICH_ERROR_RETRY_HOURS=6
```

`SAMSUNG_CSC_CANDIDATES` is a comma-separated, ordered list of three-character
Samsung sales/region codes observed in the operator population. Add another
CSC when an unresolved device is known to use it; invalid values and lists
longer than 16 entries are rejected before any network request.

Install the one-shot service and persistent six-hour timer:

```bash
install -m 0644 ua-lookup-samsung-enrichment.service \
  /etc/systemd/system/ua-lookup-samsung-enrichment.service
install -m 0644 ua-lookup-samsung-enrichment.timer \
  /etc/systemd/system/ua-lookup-samsung-enrichment.timer
systemctl daemon-reload
systemctl start ua-lookup-samsung-enrichment.service
systemctl enable --now ua-lookup-samsung-enrichment.timer
```

Verify the last run and next scheduled run:

```bash
systemctl status ua-lookup-samsung-enrichment.service --no-pager
systemctl list-timers ua-lookup-samsung-enrichment.timer --no-pager
journalctl -u ua-lookup-samsung-enrichment.service -n 100 --no-pager
```

The worker processes newest problem registrations first, probes at most five
eligible identities per run by default, and stores only model/build retry
metadata in `/var/lib/ua-lookup/samsung-firmware-enrichment-state.json`. For
each identity it tries the configured official CSC pages in order. A file lock
prevents overlapping runs. Existing verified records remain untouched when
Samsung, parsing, or exact-match validation fails.

### Xiaomi / Redmi / POCO (HyperOS)

Format: `{Brand}_{Device}_{OS_version}.{7-letter-suffix}`

The **first letter of the 7-letter suffix** encodes Android version:

| Letter | Android |
|---|---|
| S | 12 |
| T | 13 |
| U | 14 |
| V | 15 |
| W | 16 |
| X | 17 |

Example: `OS3.0.13.0.WOSEUXM` → `W` → Android 16

---

## Database Indexes

Run once on the Homer DB for fast lookups (without these, queries scan millions of rows):

```bash
docker exec -it db psql -U root -d homer_data -c "
CREATE INDEX IF NOT EXISTS idx_call_data_from_user
    ON hep_proto_1_call ((data_header->>'from_user'));

CREATE INDEX IF NOT EXISTS idx_call_data_to_user
    ON hep_proto_1_call ((data_header->>'to_user'));

CREATE INDEX IF NOT EXISTS idx_reg_data_from_user
    ON hep_proto_1_registration ((data_header->>'from_user'));"
```

> PostgreSQL automatically applies parent table indexes to all existing and future partitions.

---

## Diagnostics

### Subscriber not appearing in registration results

Run this on the Homer DB to find the cause:

```sql
-- 1. REGISTER requests present?
SELECT count(*), min(create_date)::text, max(create_date)::text
FROM hep_proto_1_registration
WHERE data_header->'from_user' = 'IMSI'
  AND raw NOT LIKE 'SIP/2.0%';

-- 2. Only responses stored? (SBC mirroring issue)
SELECT count(*), substring(raw FROM 1 FOR 30) AS first_line
FROM hep_proto_1_registration
WHERE create_date > NOW() - INTERVAL '2 days'
  AND data_header->'from_user' = 'IMSI'
  AND raw LIKE 'SIP/2.0%'
GROUP BY 2;

-- 3. Device making VoLTE calls at all?
SELECT count(*), min(create_date)::text, max(create_date)::text
FROM hep_proto_1_call
WHERE create_date > NOW() - INTERVAL '7 days'
  AND (data_header->>'from_user' IN ('msisdn', '+msisdn')
    OR data_header->>'to_user'   IN ('msisdn', '+msisdn'));
```

| Result | Likely cause |
|---|---|
| Check 1 = 0 rows, Check 2 > 0 rows | SBC only mirrors responses to Homer, not REGISTER requests — fix HEP export profile on the SBC |
| All checks = 0 | No VoLTE activity; subscriber on CS/2G/3G or VoLTE not provisioned in HSS |
| Check 1 > 0 but app shows nothing | MSISDN→IMSI lookup window (2h) too narrow; search by IMSI directly |

---

## Logs

```bash
# Live systemd journal
journalctl -u ua-lookup -f

# Log file
tail -f /var/log/ua-lookup.log
```

Log format: `YYYY-MM-DD HH:MM:SS [INFO/ERROR] message`

---

## Limitations

- **VoLTE only** — subscribers on 2G/3G or with VoLTE/WFC disabled return no call UA results
- **24-hour call window** — INVITE/BYE data older than 24 hours is not searched
- **MSISDN registration lookup** — scans `raw` SIP text limited to last 2 hours to avoid timeout; IMSI lookup is fast (indexed)
- **iOS deregistration** — Apple devices do not send `Expires: 0`; airplane mode / power off appear as 📵 Coverage loss
- **Samsung firmware coverage** — only exact model/build pairs imported from official Samsung documentation are labelled; unmatched builds remain `Unknown — not verified`
- **Cell geolocation accuracy** — coordinates are approximate (Google uses tower triangulation, not GPS); accuracy varies by cell density
- **Statement timeout** — queries capped at 15 seconds; very old partitions may time out on `raw ILIKE` scans
- **Docker bridge IP** — may change if the `db` container is recreated; update `DB_HOST` in `config.env`

