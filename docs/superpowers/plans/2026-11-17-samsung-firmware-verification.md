# Samsung Firmware Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace inferred Samsung Android versions with exact, locally cached metadata verified against official `doc.samsungmobile.com` pages.

**Architecture:** A focused Samsung firmware module parses IMS identities, merges a bundled seed catalog with an optional runtime catalog, and returns only exact model/build matches. A separate guarded CLI imports official Samsung pages into the runtime catalog; the Flask page never performs external requests and gives unmatched builds a one-click Google discovery link.

**Tech Stack:** Python 3.11 standard library, Flask/Jinja already in the application, JSON catalogs, `unittest`, `urllib.request`, systemd-managed production service.

**Spec:** `docs/superpowers/specs/2026-11-17-samsung-firmware-verification-design.md`

## Global Constraints

- Never infer a Samsung Android version from firmware letters or launch-version offsets.
- An official record is accepted only when both model code and full build number match exactly.
- Unmatched Samsung firmware displays **Unknown — not verified**.
- Google is discovery-only; do not scrape Google results or trust them as metadata.
- Accept source pages only from HTTPS `doc.samsungmobile.com`, including every redirect hop.
- The MMTEL page performs no external network request while rendering.
- Preserve the 24-hour registration query, MMTEL classification, five-minute monitor cache, DuckLake access, UCN behavior, filtering, and sorting.
- Preserve non-Samsung metadata behavior.

## Review Focus

- A catalog entry with the right build but wrong model must remain unverified; pin this in Task 1.
- A malformed or partially downloaded runtime catalog must not hide the valid bundled catalog; pin this in Task 1.
- Encoded, off-domain, protocol-relative, or redirecting source URLs must not bypass the Samsung-only boundary; pin this in Task 2.
- A large or non-HTML response must be rejected before parsing or writing; pin this in Task 2.
- Jinja output must escape User-Agent-derived search URLs and official source URLs; pin this in Task 3.

---

### Task 1: Exact Samsung identity and catalog lookup

**Files:**
- Create: `samsung_firmware.py`
- Create: `data/samsung_firmware_catalog.json`
- Create: `test_samsung_firmware.py`
- Modify: `mmtel_monitor.py`
- Modify: `test_mmtel_monitor.py`

**Interfaces:**
- Produces: `SamsungIdentity(model_code: str, device_name: str, build_number: str)`.
- Produces: `FirmwareRecord(model_code: str, device_name: str, build_number: str, android_version: str, release_date: str, security_patch_level: str, source_url: str, verified_at: str)`.
- Produces: `parse_samsung_user_agent(user_agent: str | None) -> SamsungIdentity | None`.
- Produces: `load_firmware_catalog(seed_path: Path, runtime_path: Path | None) -> dict[tuple[str, str], FirmwareRecord]`.
- Produces: `get_firmware_catalog() -> Mapping[tuple[str, str], FirmwareRecord]`, using bundled `data/samsung_firmware_catalog.json`, optional `SAMSUNG_FIRMWARE_CACHE` (default `/var/lib/ua-lookup/samsung-firmware-cache.json`), and a thread-safe modification-time cache.
- Produces: `lookup_samsung_firmware(identity: SamsungIdentity, catalog: Mapping[tuple[str, str], FirmwareRecord]) -> FirmwareRecord | None`.
- Changes: `parse_device_metadata(user_agent, samsung_catalog=None)` returns existing fields plus `metadata_status`, `source_url`, and `research_url`.

- [ ] **Step 1: Write failing identity and exact-match catalog tests**

Add `unittest` cases proving Samsung parsing preserves the full model/build, exact model+build matches succeed, wrong-model collisions fail, malformed runtime JSON leaves the seed usable, runtime exact records override identical seed keys, and non-Samsung input returns `None`.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python3 -m unittest -v test_samsung_firmware test_mmtel_monitor.DeviceMetadataTests`

Expected: FAIL because `samsung_firmware` and the new metadata contract do not exist.

- [ ] **Step 3: Implement the domain module and seed catalog**

Use immutable dataclasses and JSON schema version `1`. Seed the existing verified A56 records plus these official fixtures:

- `SM-A165F` + `A165FXXUBEZJ7` -> Android 17, release `2026-11-02`, patch `2026-10-05`;
- `SM-S936B` + `S936BXXUDDZJB` -> Android 17, release `2026-11-05`, patch `2026-10-05`.

Runtime-catalog read failure must log/return seed data rather than failing monitor rendering.

- [ ] **Step 4: Replace Samsung inference inside `parse_device_metadata`**

For Samsung UAs, bypass `get_android_version`. An exact catalog hit fills official fields with `metadata_status="verified"`; a miss sets `os_version="Unknown — not verified"`, leaves official date/patch fields blank, sets `metadata_status="unverified"`, and builds a URL-encoded Google query for `site:doc.samsungmobile.com "<model>" "<build>"`. Use `A546BXXUNGZJ5` as the regression proving that the previous Android 19 inference is gone. Existing non-Samsung field values remain unchanged; the three new metadata fields may be blank for those rows.

- [ ] **Step 5: Run focused and full tests**

Run: `python3 -m unittest -v test_samsung_firmware test_mmtel_monitor.DeviceMetadataTests && python3 -m unittest discover -v`

Expected: focused tests PASS; full suite PASS with the expanded count.

- [ ] **Step 6: Commit Task 1**

```bash
git add samsung_firmware.py data/samsung_firmware_catalog.json \
  mmtel_monitor.py test_samsung_firmware.py test_mmtel_monitor.py
git commit -m "Use verified Samsung firmware metadata"
```

### Task 2: Guarded official-page importer

**Files:**
- Create: `samsung_firmware_admin.py`
- Create: `test_samsung_firmware_admin.py`
- Modify: `samsung_firmware.py`

**Interfaces:**
- Consumes: `FirmwareRecord` and catalog JSON schema from Task 1.
- Produces: `validate_samsung_url(url: str) -> str` returning a normalized URL or raising `ValueError`.
- Produces: `parse_samsung_page(html: str, source_url: str, verified_at: str) -> list[FirmwareRecord]`.
- Produces: `fetch_samsung_page(url: str, timeout_seconds: float = 10, max_bytes: int = 2_000_000) -> tuple[str, str]`, where the second value is the final validated URL.
- Produces CLI commands `import-url URL` and `refresh`, with runtime paths overridable by `SAMSUNG_FIRMWARE_CACHE` (default `/var/lib/ua-lookup/samsung-firmware-cache.json`) and `SAMSUNG_FIRMWARE_SOURCES` (default `/var/lib/ua-lookup/samsung-firmware-sources.json`).

- [ ] **Step 1: Write failing parser, URL-boundary, fetch-limit, and atomic-write tests**

Use local fixtures and fake openers—no internet. Cover exact English labels, multiple builds, model/build mismatch, empty pages, HTTPS/hostname enforcement, encoded/off-domain and protocol-relative URLs, an off-domain redirect, non-HTML content, response larger than 2,000,000 bytes, preservation of the prior catalog after failure, and successful atomic replacement.

- [ ] **Step 2: Run importer tests and confirm failure**

Run: `python3 -m unittest -v test_samsung_firmware_admin`

Expected: FAIL because the importer interfaces do not exist.

- [ ] **Step 3: Implement validated fetching and parsing**

Use only the standard library. Require final and intermediate redirect hosts to be exactly `doc.samsungmobile.com`; require `text/html`; cap bytes before decoding; parse the model heading and repeated Build Number / Android version / Release Date / Security patch level fields; reject the import if no complete exact records are produced.

- [ ] **Step 4: Implement source registration, refresh, and atomic JSON writes**

`import-url` stores the normalized official URL and all validated records from that page. `refresh` revisits only registered official URLs. Write temporary files in the destination directory, flush and `fsync`, then `os.replace`; never truncate the last valid catalog on failure. CLI output reports imported record counts without printing environment values or subscriber data.

- [ ] **Step 5: Run importer and full tests**

Run: `python3 -m unittest -v test_samsung_firmware_admin && python3 -m unittest discover -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add samsung_firmware.py samsung_firmware_admin.py \
  test_samsung_firmware_admin.py
git commit -m "Add guarded Samsung firmware importer"
```

### Task 3: Monitor presentation and operator discovery

**Files:**
- Modify: `app.py`
- Modify: `test_mmtel_monitor_ui.py`
- Modify: `test_default_search.py`

**Interfaces:**
- Consumes: `parse_device_metadata` fields `metadata_status`, `source_url`, and `research_url` from Task 1.
- Produces: verified-source and research actions in both MMTEL problem tables without changing client-side filter/sort keys.

- [ ] **Step 1: Write failing template contract tests**

Assert that verified Samsung rows expose an escaped `Official Samsung record` link; unverified Samsung rows show the exact text `Unknown — not verified` and an escaped `Find official firmware` link; non-Samsung rows show neither action; both problem sections use the same macro; existing four filter/sort keys remain unchanged.

- [ ] **Step 2: Run UI tests and confirm failure**

Run: `python3 -m unittest -v test_mmtel_monitor_ui test_default_search`

Expected: FAIL because the actions are not rendered.

- [ ] **Step 3: Update the shared monitor table macro**

Render the source link only for `metadata_status == "verified"` and the Google discovery link only for `metadata_status == "unverified"`. Use Jinja escaping and `rel="noopener noreferrer"`; do not add JavaScript network requests or alter table state persistence.

- [ ] **Step 4: Run UI, JavaScript syntax, and full tests**

Run: `python3 -m unittest -v test_mmtel_monitor_ui test_default_search && python3 -m unittest discover -v`

Also extract the embedded monitor JavaScript using the existing UI-test technique and run `node --check` when Node is installed.

Expected: all tests PASS; JavaScript parses successfully.

- [ ] **Step 5: Commit Task 3**

```bash
git add app.py test_mmtel_monitor_ui.py test_default_search.py
git commit -m "Show verified Samsung firmware sources"
```

### Task 4: End-to-end validation and deployment handoff

**Files:**
- Modify: `README.md` only if it already documents the monitor or operational commands.
- Create: a guarded deployment script outside the repository for the production operator.

**Interfaces:**
- Consumes: all Task 1–3 interfaces.
- Produces: a reviewable branch and a deployment script that does not import unknown external URLs automatically.

- [ ] **Step 1: Run source and behavior verification**

Run compile checks, `python3 -m unittest discover -v`, `git diff --check`, and a semantic scan confirming Samsung metadata no longer calls the launch-version/firmware-letter inference path. Confirm DuckLake SQL, MMTEL classification, UCN scripts, `/health`, and the 24-hour interval are unchanged.

- [ ] **Step 2: Validate representative metadata locally**

Exercise the two verified examples, one previously over-inferred Samsung build, one unknown Samsung build, and representative Apple/Pixel/Xiaomi/Fairphone inputs. Expected: exact Samsung hits show official values; unknown Samsung shows `Unknown — not verified`; other vendors retain current output.

- [ ] **Step 3: Review the complete branch**

Review the diff against the approved spec, focusing on host/redirect validation, atomic writes, catalog precedence, Jinja escaping, and absence of network access from request handlers. Resolve findings before handoff.

- [ ] **Step 4: Prepare guarded production deployment**

The script must verify the expected clean production HEAD and remote commit, create a rollback checkpoint, fast-forward only, compile and run the full suite before restart, restart `ua-lookup.service`, verify `/health` and `/mmtel-monitor`, confirm `Unknown — not verified` and discovery/source markers, and restore the prior commit if post-update validation fails. It must not invoke `import-url` or change runtime catalog files automatically.

- [ ] **Step 5: Commit any documentation-only adjustment and present the branch**

Expected: clean worktree, all tests passing, exact changed-file inventory documented, and production unchanged until the user runs the guarded deployment.
