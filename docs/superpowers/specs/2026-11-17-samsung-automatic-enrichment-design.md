# Samsung Automatic Enrichment Design

## Intent

Automatically enrich unknown Samsung firmware shown by the MMTEL problem
monitor without moving external network access into the Flask request path.

## Selected design

- A standalone worker reads the same latest-successful-registration query as
  the monitor and considers only Samsung problem rows whose exact model/build
  pair is absent from the verified catalog.
- Discovery uses the Brave Web Search API with a dedicated
  `BRAVE_SEARCH_API_KEY`. Search results are untrusted URL candidates only.
- Candidate URLs must use HTTPS and the exact host
  `doc.samsungmobile.com`. Redirects remain restricted to that host.
- A candidate is accepted only when the official page model equals the IMS
  model code and the page contains the complete firmware build as an exact
  match. No substring, prefix, CSC, build-letter, or Android-version inference
  is allowed.
- The worker writes verified records, source URLs, and retry state atomically.
  Existing verified data remains available on any search, network, parsing,
  or validation failure.
- A persistent systemd timer runs the worker every six hours. A non-blocking
  file lock prevents overlapping runs.

## Load and retry controls

- Process newest registrations first.
- Search at most `SAMSUNG_ENRICH_MAX_SEARCHES` identities per run (default 10).
- Retry a not-found identity after `SAMSUNG_ENRICH_NOT_FOUND_RETRY_HOURS`
  (default 24 hours); transient errors are retried on the next six-hour run.
- Search returns at most five URL candidates per identity.
- Store only Samsung model/build identities and attempt metadata in worker
  state; do not store IMSI, MSISDN, Contact, or the complete User-Agent.

## Failure behavior

- A missing API key is a configuration error and produces a non-zero worker
  exit without changing catalogs.
- A DuckLake query failure is a failed run and leaves all state/catalog files
  unchanged.
- Failure for one model/build does not prevent other eligible identities from
  being attempted.
- The application continues to show `Unknown — not verified` until an exact
  record is committed.

## Deployment

Install `ua-lookup-samsung-enrichment.service` and
`ua-lookup-samsung-enrichment.timer`, add `BRAVE_SEARCH_API_KEY` to the
existing protected `config.env`, run one manual service invocation, then
enable the timer. The timer uses `OnUnitActiveSec=6h`, `Persistent=true`, and
a small randomized delay.

## Verification

- Unit tests cover exact-match acceptance, model/build mismatch rejection,
  off-domain search results, response size limits, retry scheduling, problem
  row selection, and partial-failure isolation.
- Static tests pin the six-hour persistent systemd timer contract.
- The complete existing test suite, compile checks, and patch-format checks
  must pass before deployment.
