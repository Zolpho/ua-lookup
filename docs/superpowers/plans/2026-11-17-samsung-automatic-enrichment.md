# Samsung Automatic Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Discover and exactly verify unknown Samsung firmware automatically on a persistent six-hour schedule.

**Architecture:** A standalone worker reuses the monitor's read-only registration query, constructs predictable official Samsung model/CSC document URLs, and delegates exact Samsung parsing and atomic catalog updates to the guarded importer. Systemd runs the worker every six hours; Flask remains local-catalog-only.

**Tech Stack:** Python 3.11 standard library, DuckDB/DuckLake through the existing application query, JSON state, `urllib.request`, `unittest`, systemd.

**Spec:** `docs/superpowers/specs/2026-11-17-samsung-automatic-enrichment-design.md`

## Global Constraints

- Accept metadata only for an exact official Samsung model/build match.
- Never call Samsung from the Flask rendering path.
- Preserve all existing MMTEL, DuckLake, UCN, five-minute page-cache, and UI behavior.
- Keep the last valid catalog on every failure.
- Do not persist subscriber identifiers in enrichment state.

## Review Focus

- Invalid CSC input must never create an off-domain or malformed request.
- A correct model page missing the requested build must never mutate the catalog.
- One candidate failure must not stop later candidates or identities.
- Repeated timer activation must not overlap or produce excessive repeat probes.

---

### Task 1: Exact targeted import

**Files:**
- Modify: `samsung_firmware_admin.py`
- Modify: `test_samsung_firmware_admin.py`

**Interfaces:**
- Produces: `import_exact_official_url(identity, url, ...) -> FirmwareRecord`.

- [ ] Write failing tests for exact acceptance, wrong build rejection, and no-write-on-rejection.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Implement the smallest exact-target importer using existing guarded fetch/parser/atomic writers.
- [ ] Run the focused tests and the full Samsung importer suite.

### Task 2: Automatic worker

**Files:**
- Create: `samsung_firmware_enrich.py`
- Create: `test_samsung_firmware_enrich.py`

**Interfaces:**
- Consumes: `import_exact_official_url`, monitor Contact classification, exact catalog lookup, and injected registration loader/candidate/import dependencies.
- Produces: bounded official CSC probing, unknown-problem identity selection, retry state, locking, and CLI `main()`.

- [ ] Write failing tests for CSC validation and bounds, official URL construction, exact candidate iteration, retry timing, problem-only selection, and failure isolation.
- [ ] Run the focused tests and verify missing-feature failures.
- [ ] Implement minimal worker functions and CLI.
- [ ] Run the focused tests and complete suite.

### Task 3: Six-hour systemd schedule and operations

**Files:**
- Create: `ua-lookup-samsung-enrichment.service`
- Create: `ua-lookup-samsung-enrichment.timer`
- Create: `test_samsung_enrichment_systemd.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: worker CLI and `/opt/ua-lookup/config.env`.
- Produces: persistent six-hour schedule and deployment/diagnostic instructions.

- [ ] Write a failing static contract test for unit paths, environment file, six-hour interval, persistence, and overlap protection.
- [ ] Run it and verify unit files are missing.
- [ ] Add the two units and operator documentation.
- [ ] Run focused and full tests, compile all Python sources, and run `git diff --check`.
