# Samsung Firmware Verification Design

## Intent

Replace speculative Samsung Android-version inference with exact metadata from
official `doc.samsungmobile.com` firmware records. The MMTEL problem page must
never present an estimated Samsung Android version as fact.

## Goals

- Match Samsung devices by the exact model code and firmware build extracted
  from the IMS User-Agent.
- Display the official Android version, firmware release date, security patch
  level, and source page when an exact verified record exists.
- Display **Unknown — not verified** when no exact verified record exists.
- Preserve the existing MMTEL classification, 24-hour query, five-minute page
  cache, filtering, sorting, DuckLake access, and UCN lookup behavior.
- Keep Google and Samsung requests out of the page-rendering path.

## Non-goals

- Guessing Samsung Android versions from build letters, launch versions, or
  arithmetic offsets.
- Scraping Google search-result HTML.
- Adding an unauthenticated web endpoint that accepts arbitrary source URLs.
- Treating a third-party firmware site as authoritative.
- Changing metadata handling for Apple, Pixel, Xiaomi, Redmi, POCO, or
  Fairphone devices in this change.

## Current Problem

`ua_mappings.py` derives Samsung Android versions from a model launch version
and letters found near the end of the firmware build. Those letters are not a
safe Android-major-version formula. The extrapolation can therefore produce
Android 18 or Android 19 even when Samsung's official page identifies the
build as Android 17.

`mmtel_monitor.py` currently has exact official metadata for a limited set of
Galaxy A56 builds. Other Samsung builds can fall through to the unsafe
heuristic.

## Approaches Considered

### 1. Continue inference with corrected offsets

Rejected. A larger mapping still assumes Samsung build-letter semantics are
stable across models and regions. It can silently become wrong again.

### 2. Live lookup while rendering the monitor

Rejected. Samsung or network latency would delay or break the operational
page. The page refreshes every five minutes and must remain independent of
external-site availability.

### 3. Verified local catalog with operator-driven discovery

Selected. The application reads a local exact-match catalog. Unknown Samsung
rows provide a one-click Google search for the exact build, restricted to
`doc.samsungmobile.com`. An operator identifies the official page, after which
an administrative importer validates and caches its records.

## Selected Design

### Exact identity

For Samsung IMS User-Agents, parse:

- model code, including the `SM-` prefix for display and source validation;
- complete firmware build number;
- complete original User-Agent.

The lookup key is `(model_code, build_number)`. A build number alone is not
accepted when the model does not also match.

### Verified catalog

Create a Samsung-specific catalog component. Each verified record stores:

- model code and device name;
- exact build number;
- Android version;
- firmware release date;
- security patch level;
- official Samsung source URL;
- retrieval timestamp.

Existing hand-verified A56 records move into the same catalog contract. The
catalog is read locally by the application and has no network dependency.

### Discovery and import

For an unmatched Samsung build, the UI shows **Unknown — not verified** and a
link opening a Google query equivalent to:

`site:doc.samsungmobile.com "<exact build number>"`

Google is discovery-only and its result is never trusted as metadata.

The operator supplies the resulting official Samsung URL to a command-line
importer. The importer must:

1. accept HTTPS URLs only;
2. require the hostname to be exactly `doc.samsungmobile.com`;
3. reject redirects outside that hostname;
4. extract firmware records from the official page;
5. require the page model to match the requested model;
6. require an exact build match before publishing a record;
7. write the catalog atomically, preserving the previous valid catalog if
   retrieval or validation fails.

There is no web mutation endpoint. This avoids adding authentication and SSRF
risk to the existing internal Flask application.

### Refresh behavior

Known official source pages can be refreshed by the same command-line tool.
The first implementation may run it manually during source registration. A
daily systemd timer can refresh registered sources after the parser is proven
against production pages; this is not required for rendering the monitor.

The last verified record remains usable if Samsung is unavailable. New or
unmatched builds remain **Unknown — not verified** until confirmed.

### UI behavior

Samsung rows use these rules:

- exact catalog hit: show verified Android version, release date, security
  patch level, and an official-source link;
- no exact hit: show **Unknown — not verified** for OS metadata and provide the
  discovery link;
- never display a Samsung Android version calculated from firmware letters.

The existing device, build, OS, and User-Agent filters and sort controls remain
unchanged.

## Testing

- Regression tests proving builds previously inferred as Android 18/19 now
  render as **Unknown — not verified** without a catalog record.
- Exact fixture tests for the two verified examples:
  - `A165FXXUBEZJ7` -> Android 17, release `2026-11-02`, patch `2026-10-05`;
  - `S936BXXUDDZJB` -> Android 17, release `2026-11-05`, patch `2026-10-05`.
- Importer tests for exact model/build validation, host restrictions,
  off-domain redirects, malformed pages, and atomic-write failure handling.
- Existing 41-test suite must remain green.
- A production read-only audit will compare displayed Samsung rows against the
  catalog and confirm that no inferred Samsung Android version remains.

## Rollout

Deploy the safe display behavior and verified catalog together. The absence of
an official match degrades only metadata display; it does not affect MMTEL
classification, registration queries, or page availability. Rollback is the
normal application source rollback and does not require data migration.
