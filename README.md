# UA Lookup — User-Agent Lookup via Homer SIPCapture

A lightweight Flask web application for looking up the **SIP User-Agent** (device type and OS version) of a mobile subscriber by **MSISDN** or **IMSI**, querying the [HOMER SIPCapture](https://github.com/sipcapture/homer) PostgreSQL database directly.

---

## Overview

When a subscriber makes a VoLTE or WiFi Calling call, their device includes a `User-Agent` header in the SIP INVITE/BYE message. This tool queries the Homer database to surface that information, helping support teams and network engineers quickly identify which device and OS version a subscriber is using — without needing access to HSS/HLR or device management systems.

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
        │  SIP INVITE/BYE with User-Agent header
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
```

### Homer Docker stack (relevant services)

| Service | Role |
|---|---|
| `heplify-server` | Receives HEP traffic, writes to DB |
| `db` | PostgreSQL container (`homer_data`) |

The app runs **on the Docker host** and connects to the DB container via its Docker bridge IP (`172.19.0.6`), since port 5432 is not exposed externally.

---

## Project Structure

```
/opt/ua-lookup/
├── app.py                    # Flask application — main entry point
├── ua_mappings.py            # Device/OS version mapping (Samsung, Pixel, Fairphone, iOS)
├── venv/                     # Python virtual environment
└── /var/log/ua-lookup.log    # Application log file
/etc/systemd/system/
└── ua-lookup.service         # systemd unit file
```

---

## Requirements

- Python 3.9+
- Homer SIPCapture stack running with `heplify-server` and PostgreSQL
- Network access from the app host to the Docker bridge IP of the DB container

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

### Database — `app.py`

```python
DB_CONFIG = {
    "host":     "172.19.0.6",   # Docker bridge IP of the db container
    "port":     5432,
    "dbname":   "homer_db_name",
    "user":     "root",
    "password": "homer_db_password",
}
```

> To find the current Docker bridge IP of the DB container:
> ```bash
> docker inspect db --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
> ```
> Update `DB_CONFIG` if it changes after a container restart.

### Filtered User-Agents — `app.py`

The following UA patterns are excluded from results (network infrastructure, not end devices):

```python
UA_EXCLUDE = re.compile(r"YATE|OpenSIPS|dispatcher|nx-sbc-ocs|NexSBC|VoLTE/WFC", re.IGNORECASE)
```

---

## Homer Database Tables

| Table | Content | Used for |
|---|---|---|
| `hep_proto_1_call` | INVITE/BYE messages | Device UA lookup |
| `hep_proto_1_registration` | REGISTER messages | Last IMS registration |

Both tables are **partitioned by 12-hour intervals** (e.g. `hep_proto_1_call_20260306_0000`). The parent table is queried directly — PostgreSQL fans out to the relevant partitions automatically.

### Key JSONB fields

```
data_header->>'from_user'    — calling MSISDN or IMSI
data_header->>'to_user'      — called MSISDN or IMSI
data_header->>'user_agent'   — SIP User-Agent string
data_header->>'method'       — SIP method (INVITE, BYE, REGISTER...)
protocol_header->>'srcIp'    — source IP of the SIP packet (APN/PDN address)
```

---

## Installation

### 1. Copy files

```bash
mkdir -p /opt/ua-lookup
cp app.py ua_mappings.py /opt/ua-lookup/
```

### 2. Create virtual environment and install deps

```bash
cd /opt/ua-lookup
python3 -m venv venv
source venv/bin/activate
pip install flask psycopg2-binary
```

### 3. Install systemd service

```bash
cp ua-lookup.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ua-lookup
systemctl status ua-lookup
```

### 4. Verify

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
ExecStart=/opt/ua-lookup/venv/bin/python app.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

---

## Usage

Open `http://<host>:5000` in a browser.

Enter a **MSISDN** or **IMSI** in the search box:

| Input | Example |
|---|---|
| MSISDN without prefix | `41784807070` |
| MSISDN with + prefix | `+41784807070` |
| IMSI (15 digits) | `228580000347027` |

The `+` prefix is stripped automatically before querying.

### Results — 📞 Recent Calls (INVITE/BYE)

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

### Results — 🔓 Last IMS Registration

Queries `hep_proto_1_registration`, last **7 days**. Shows the single most recent REGISTER record.

- **IMSI input** → direct fast query on `data_header->'from_user'`
- **MSISDN input** → scans `raw` field (last 2 hours) to find the IMSI, then queries by IMSI

---

## OS Version Mapping (`ua_mappings.py`)

The `get_android_version(ua)` function maps raw UA strings to human-readable OS versions:

| UA string | Decoded output |
|---|---|
| `SM-S936B-S936BXXS7BYLR Samsung IMS 6.0` | `Galaxy S25+ — Android 15` |
| `SM-S921B-S921BXXSCCZA1 Samsung IMS 6.0` | `Galaxy S24 — Android 16` |
| `Google_Pixel8_Android 16_BP4A.260205.001` | `Pixel8 — Android 16` |
| `Fairphone_Fairphone 6_FP6.QREL.15.151.0` | `Fairphone 6 — Android 15` |
| `iOS/26.3 iPhone` | `iOS 26` |

### Supported Samsung models

| Series | Models |
|---|---|
| Galaxy S25 (Android 15) | S931B, S936B, S938B |
| Galaxy S24 (Android 14) | S921B, S926B, S928B |
| Galaxy S23 (Android 13) | S911B, S916B, S918B |
| Galaxy S22 (Android 12) | S901B, S906B, S908B |
| Galaxy A-series | A566B, A556B, A546B, A536B, A336B |
| Galaxy Z Fold/Flip | F956B, F946B, F936B, F741B, F731B, F721B |

### Samsung firmware decoding logic

The firmware build string (e.g. `S936BXXS7BYLR`) encodes the OS update level in the **4th character from the end**:
- `A` = shipped (launch) Android version
- `B` = 1st major OS update, `C` = 2nd, etc.
- QPR releases also increment this letter (~2–3 per major Android version)

The offset is divided by ~3 to estimate major Android version jumps.

### Adding a new Samsung model

Edit `SAMSUNG_MODELS` in `ua_mappings.py`:
```python
"S931B": ("Galaxy S25", 15),   # model_code: (device name, launch Android version)
```
Then restart the service — no changes to `app.py` needed.

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
- **MSISDN registration lookup** — scans `raw` SIP text (limited to last 2 hours to avoid timeout); IMSI lookup is fast (indexed)
- **Samsung model coverage** — `ua_mappings.py` must be manually updated for new device models
- **Docker bridge IP** — `172.19.0.6` may change if the `db` container is recreated; update `DB_CONFIG` accordingly
- **Statement timeout** — queries are capped at 15 seconds to prevent the app from hanging on heavy scans

