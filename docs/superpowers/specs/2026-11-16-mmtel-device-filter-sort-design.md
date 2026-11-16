# MMTEL problem monitor: device identity, filtering, and sorting

Date: 2026-11-16

## Intent

Keep `/mmtel-monitor` a problem-only view while making Apple and other non-Android registrations understandable and making a large problem list quick to inspect. A device is still shown only when its latest positive registration in the 24-hour window is confirmed missing MMTEL or its Contact header cannot be verified.

## Goals

- Preserve both existing problem sections: **Confirmed missing MMTEL** and **Unable to verify**.
- Continue excluding registrations whose Contact header advertises MMTEL.
- Recognize Apple registrations such as `iOS/27.0.1 iPhone` as device `Apple iPhone` with OS version `iOS 27.0.1`.
- Retain the existing Samsung, Pixel, Xiaomi/Redmi/POCO, and Fairphone identification behavior.
- For other recognizable User-Agent families, show a conservative device label and OS version only when those values are present in the User-Agent.
- Never infer firmware, release-date, or security-patch data that the User-Agent or trusted catalog does not provide. Missing values remain `—`.
- Preserve and display the complete User-Agent.
- Add one case-insensitive browser-side filter covering Device, Build number, OS version, and User-Agent.
- Make those four columns sortable by clicking their headers. The first click sorts ascending; another click reverses the direction.
- Apply the same filter and sort choice to both problem tables without combining their status categories.
- Keep the current five-minute server cache and page refresh behavior.

## Chosen approach

Filtering and sorting will run in small, dependency-free JavaScript in the browser. The server continues to generate the same two problem datasets and does not rerun DuckLake or UCN queries when an operator types or changes sorting. This is preferable to server-side query parameters because the full 24-hour problem list is already present on the page, and it avoids extra backend load.

Each table row will expose normalized, escaped values through `data-*` attributes for:

- device
- build number
- OS version
- full User-Agent

The controls operate on those values. Each section will show its visible count after filtering and a clear no-match message when all rows in that section are hidden.

The filter text, sort column, and direction will be stored in `sessionStorage`. They will be restored after the automatic five-minute reload in the same browser tab. If JavaScript is unavailable, all rows remain visible in the existing newest-registration-first order.

## Data contract

`parse_device_metadata()` will expose a general `os_version` field. The monitor template will use `os_version` and label the column **OS version** rather than **Android version**.

Examples:

| User-Agent | Device | Build number | OS version |
|---|---|---|---|
| `iOS/27.0.1 iPhone` | `Apple iPhone` | `—` | `iOS 27.0.1` |
| `SM-A566B-A566BXXS6AYGE Samsung IMS 6.0` | `Galaxy A56` | `A566BXXS6AYGE` | `Android 15` |
| unknown/unrecognized value | `—` unless safely recognizable | `—` | `—` unless explicitly present |

Existing trusted Samsung catalog enrichment remains unchanged. The full User-Agent always remains available even when no structured fields can be derived.

## UI behavior

- A single search field appears above both sections with the hint “Filter device, build, OS or User-Agent”.
- The Device, Build number, OS version, and User-Agent headers are keyboard-accessible sort buttons.
- The active header communicates ascending or descending order visually and through `aria-sort`.
- Initial order remains newest registration first.
- Sorting is stable; equal values keep their existing newest-first relationship.
- Clearing the search restores all problem rows without a network request.
- Automatic refresh continues every five minutes and restores the current controls from `sessionStorage`.

## Non-goals

- Do not show healthy MMTEL-present registrations.
- Do not change the registration query, Contact classification, latest-registration selection, 24-hour window, cache duration, or refresh interval.
- Do not add vendor-site lookups or live external metadata requests.
- Do not guess a handset model, build, firmware release date, or patch level from incomplete evidence.
- Do not add a JavaScript framework or table library.

## Verification

- Unit tests cover full Apple version/model parsing, existing Android parsing, unknown User-Agent behavior, and continued exclusion of MMTEL-present registrations.
- Template checks cover the renamed OS column, filter controls, sortable headers, and problem-only wording.
- The complete test suite must pass with explicit discovery.
- Deployment verification should confirm the service health endpoint, a live monitor response, preserved five-minute refresh/cache behavior, and that no ordinary lookup query behavior changed.
