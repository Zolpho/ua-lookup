# MMTEL Device Filter and Sort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep `/mmtel-monitor` problem-only while identifying Apple and other safely recognizable devices and adding persistent browser-side filtering and sorting for device metadata.

**Architecture:** Extend the pure `parse_device_metadata()` contract with a vendor-neutral `os_version` value, leaving Contact classification and report selection unchanged. Enhance the existing Flask/Jinja monitor template with semantic row data and dependency-free JavaScript that filters and stably sorts the two existing problem tables without backend requests.

**Tech Stack:** Python 3.11, Flask/Jinja, `unittest`, vanilla browser JavaScript, CSS

**Spec:** `docs/superpowers/specs/2026-11-16-mmtel-device-filter-sort-design.md`

## Global Constraints

- `/mmtel-monitor` remains problem-only: `MMTEL_PRESENT` registrations are never rendered.
- Preserve the 24-hour query, latest-positive-registration selection, Contact classification, five-minute backend cache, and five-minute page refresh.
- Do not add external JavaScript dependencies or vendor metadata requests.
- Do not infer firmware, release date, or security patch data not present in the User-Agent or trusted Samsung catalog.
- Preserve the complete User-Agent and the separate **Confirmed missing MMTEL** and **Unable to verify** sections.
- Filter and sort locally in the browser; no backend request is triggered by either control.

## Review Focus

- Apple versions with multiple components such as `iOS/27.0.1 iPhone` must retain the full version and identify `Apple iPhone`; Task 1 pins this.
- Empty and unrecognized User-Agents must remain reportable without invented metadata; Task 1 pins this.
- HTML-sensitive characters in User-Agent and device fields must remain escaped when copied to row attributes; Task 2 pins the use of Jinja escaping and rendering behavior.
- Duplicate or blank sort values must preserve the existing newest-first relative order; Task 2 pins original indexes as a stable tie-breaker.
- Disabled or unavailable `sessionStorage` must not break rendering, filtering, or sorting; Task 2 pins guarded storage access.

---

## File structure

- Modify `mmtel_monitor.py`: generalize parsed device metadata from Android-only naming to `os_version` and add exact Apple parsing.
- Modify `test_mmtel_monitor.py`: pin Apple, existing Android, unknown User-Agent, and problem-only report behavior.
- Modify `app.py`: rename the monitor column, add the filter/sort markup and styles, expose escaped row metadata, and add browser-side state/behavior.
- Create `test_mmtel_monitor_ui.py`: inspect the monitor template structurally without importing the production app or requiring service environment variables.

### Task 1: Generalize device metadata without changing problem selection

**Files:**
- Modify: `mmtel_monitor.py`
- Modify: `test_mmtel_monitor.py`

**Interfaces:**
- Consumes: `get_android_version(user_agent: str) -> str` and the existing trusted `SAMSUNG_FIRMWARE` catalog.
- Produces: `parse_device_metadata(user_agent: str | None) -> dict[str, str]` with keys `model`, `build_number`, `os_version`, `security`, `fw_release_date`, and `security_patch_level`.

- [ ] **Step 1: Write failing metadata and report tests**

Add tests asserting:

- `iOS/27.0.1 iPhone` returns model `Apple iPhone`, OS version `iOS 27.0.1`, and blank build/security/catalog fields.
- The known Samsung example still returns `Galaxy A56`, `A566BXXS6AYGE`, and `Android 15` through `os_version` with its existing catalog fields.
- The Pixel example still returns `Pixel8`, `BP4A.260205.001`, and `Android 16` through `os_version`.
- An unrecognized User-Agent returns six blank metadata values while the report retains the full User-Agent.
- A newer `MMTEL_PRESENT` registration remains absent from both report buckets.

- [ ] **Step 2: Run the focused tests and verify the new assertions fail**

Run: `python3 -m unittest -v test_mmtel_monitor.DeviceMetadataTests test_mmtel_monitor.ReportTests`

Expected: failures mention missing `os_version`, truncated Apple version, or missing Apple device identity; existing problem-selection assertions continue to pass.

- [ ] **Step 3: Implement the vendor-neutral metadata contract**

In `parse_device_metadata()`:

- Parse `iOS/<numeric dotted version> iPhone` case-insensitively before the generic inference path.
- Return `Apple iPhone` and `iOS <full dotted version>` for that exact family.
- Rename the local/output `android_version` value to `os_version`.
- Preserve all current Samsung catalog, Pixel, HyperOS, and Fairphone build extraction behavior.
- Keep unrecognized values blank and do not add fallback guesses.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `python3 -m unittest -v test_mmtel_monitor.DeviceMetadataTests test_mmtel_monitor.ReportTests`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the metadata change**

```bash
git add mmtel_monitor.py test_mmtel_monitor.py
git commit -m "Recognize Apple devices in MMTEL monitor"
```

### Task 2: Add persistent browser-side filter and stable sorting

**Files:**
- Modify: `app.py`
- Create: `test_mmtel_monitor_ui.py`

**Interfaces:**
- Consumes: monitor report rows containing `model`, `build_number`, `os_version`, and `user_agent` from Task 1.
- Produces: one filter input; sortable `device`, `build`, `os`, and `user_agent` headers; row `data-*` values; and browser functions `loadMonitorState()`, `saveMonitorState(state)`, and `applyMonitorControls()`.

- [ ] **Step 1: Write failing structural template tests**

Create an AST-based template extractor for `MMTEL_MONITOR_HTML`, then add tests asserting:

- The column is `OS version`, uses `row.os_version`, and no longer contains `Android version` or `row.android_version`.
- A single input with id `monitor-filter` and the specified placeholder is present above both tables.
- Device, Build number, OS version, and User-Agent headers have sort controls with exact keys `device`, `build`, `os`, and `user_agent`.
- Rows expose escaped Jinja values in `data-device`, `data-build`, `data-os`, and `data-user-agent`, plus `data-original-index`.
- The script contains guarded `sessionStorage` reads/writes, uses one shared sort state for both tables, and uses `data-original-index` as the stable tie-breaker.
- The template still states problem-only semantics, retains both section headings, and keeps the 300-second refresh.

- [ ] **Step 2: Run the UI tests and verify they fail**

Run: `python3 -m unittest -v test_mmtel_monitor_ui`

Expected: failures identify the missing OS label, controls, row metadata, and browser behavior.

- [ ] **Step 3: Add monitor controls and semantic row data**

Update `MMTEL_MONITOR_HTML` to:

- Add a labeled search input with id `monitor-filter` and placeholder `Filter device, build, OS or User-Agent`.
- Change the macro to accept a stable section identifier and render each table with `data-monitor-table`.
- Render sortable button headers for Device, Build number, OS version, and User-Agent; put `aria-sort="none"` on their header cells.
- Attach escaped `data-device`, `data-build`, `data-os`, and `data-user-agent` values to every monitor row and `loop.index0` as `data-original-index`.
- Add a per-section visible-count element and an initially hidden no-match message.
- Keep all existing table columns and Contact evidence.

- [ ] **Step 4: Implement dependency-free browser behavior**

Add `loadMonitorState()`, `saveMonitorState(state)`, and `applyMonitorControls()` inside the existing page template:

- State shape is `{filter: string, sortKey: string, direction: "asc" | "desc"}` under session key `ua-mmtel-monitor-controls-v1`.
- Wrap all `sessionStorage` access in `try/catch` and fall back to default state on failure.
- Normalize filter and sortable values with `String(value || "").toLocaleLowerCase()`.
- Filter against the four row fields only.
- Sort each table independently with the same chosen key/direction and use numeric `data-original-index` as the stable tie-breaker.
- Update `aria-sort`, visible counts, and no-match messages after each change.
- Restore the saved state on `DOMContentLoaded`; input updates immediately; a header click selects ascending or toggles direction.

- [ ] **Step 5: Run the UI tests and verify they pass**

Run: `python3 -m unittest -v test_mmtel_monitor_ui`

Expected: all UI contract tests pass.

- [ ] **Step 6: Commit the UI change**

```bash
git add app.py test_mmtel_monitor_ui.py
git commit -m "Add MMTEL device filtering and sorting"
```

### Task 3: Full regression and deployable branch checkpoint

**Files:**
- Verify: `app.py`
- Verify: `mmtel_monitor.py`
- Verify: `test_mmtel_monitor.py`
- Verify: `test_mmtel_monitor_ui.py`

**Interfaces:**
- Consumes: the completed metadata and browser UI commits.
- Produces: a clean, reviewable branch containing the approved design and implementation with no service or database mutations.

- [ ] **Step 1: Compile all Python source**

Run: `python3 -m py_compile app.py mmtel_monitor.py test_mmtel_monitor.py test_mmtel_monitor_ui.py`

Expected: exit status 0 and no output.

- [ ] **Step 2: Run explicit test discovery**

Run: `python3 -m unittest discover -v`

Expected: all existing and new tests pass; no tests are skipped because discovery is explicit.

- [ ] **Step 3: Check patch quality and scope**

Run: `git diff origin/master...HEAD --check && git status --short && git diff --stat origin/master...HEAD`

Expected: no whitespace errors; clean worktree; changes limited to the approved design, plan, metadata, template, and their tests.

- [ ] **Step 4: Review problem-only and performance invariants**

Confirm in the final diff that:

- `if status == "MMTEL_PRESENT": continue` is unchanged.
- `_mmtel_monitor_cache` remains configured with `ttl_seconds=300`.
- `<meta http-equiv="refresh" content="300">` remains.
- No DuckLake SQL, UCN query, route, or health endpoint behavior changed.

- [ ] **Step 5: Record the final commit range and hashes**

Run: `git log --oneline origin/master..HEAD && sha256sum app.py mmtel_monitor.py test_mmtel_monitor.py test_mmtel_monitor_ui.py`

Expected: the design plus two implementation commits are present and the four final source/test hashes are available for deployment verification.
