# Orpheus delivery status

Updated 2026-09-06. This is a local, single-user production-quality baseline with a lean hosted deployment slice. It is not a perceptual sound-quality certificate.

## Verified

- Clean package exposes the complete v2 workflow tool inventory: 28 tools, with no v1 or legacy HTTP routes.
- Backend suite: 73 tests pass, one opt-in live MCP test is skipped by default.
- Live MCP proof: a real ADK run queried the official Loki/Prometheus MCP service and wrote a linked take experiment receipt.
- Isolated Grafana stack: setup is idempotent, credentials remain in runtime storage, the collector is scraped by Prometheus, and the lab containers remain separate.
- Authorized paid parity run: one turn, capped at 40 controller calls. OpenRouter served two controller models through explicit failover; the run completed with two measured candidates and `review_required`. No approval was invented.
- HTTP coverage includes media preparation, origin checks, retired routes, range requests, waveforms, takes, explicit run dispatch, exact review identity, assisted revision lineage, and stale-audio rejection.
- Frontend: 14 Node lifecycle/transport/domain checks pass and the Vite production build passes.
- Browser QA covered landing/workspace at desktop and mobile widths, picture playback, independent-source playback, source/picture exclusivity, candidate switching, evidence rail, review disclosures, and the explicit paid-run consent gate.
- Impeccable detector returned no findings for `frontend/src` after the bounded visual pass. Self-hosted Barlow Condensed and Source Sans 3 assets are licensed and bundled.
- No `useEffect`, imperative DOM queries, reference-lab imports, or source file over 800 lines in application code.
- Cloud-ready runtime mode is explicit: the API accepts Cloud Run's `PORT`, binds to `0.0.0.0`, validates configured public origins, and dispatches long turns to a named Cloud Run Job. `Dockerfile`, `deploy/cloud-run-job.yaml`, and read-only `deploy/check.sh` are included.
- Hosted slice is live in GCP project `orpheus-agentic`: Firebase Hosting serves the two-page frontend, `/api/**` rewrites to Cloud Run `orpheus-api`, and Cloud Run Job `orpheus-worker` is deployed with the pinned image digest. Media uses the private `orpheus-agentic-media` bucket; provider fallback uses Secret Manager.

## Known limits

- The paid parity run is evidence of wiring and real-provider behavior only. Its own receipt keeps the result provisional: source character mismatch, timing/listening uncertainty, unresolved acoustic hypotheses, and whole-soundtrack replacement are disclosed for human review.
- Browser capture still depends on the user's microphone permission and hardware. Automated tests cover cancellation, final chunk delivery, cleanup, mute restoration, and upload boundaries without granting that permission.
- Grafana remains a separate operational UI. The application links to it and queries the official MCP service; it does not imitate Grafana's theme.
- The deployed slice deliberately uses Cloud Run's project-scoped worker with SQLite sessions in instance-local `/tmp`; it does not claim Agent Engine Runtime, Agent Engine Sessions, Memory Bank, or Cloud SQL product-record migration. The GCS FUSE mount persists media, but SQLite is not a shared database. A standalone Agent Engine `root_agent` entrypoint and a real relational migration remain required for the managed architecture.
- Grafana MCP is not deployed yet. The known Grafana Cloud stack URL is `crimsonagave361.grafana.net`, but no Grafana Cloud service-account token is present in the workspace. The app therefore runs with Grafana disabled in the hosted slice rather than using a fake credential.
- Cloud SQL is deferred to the later target project `project-cb6f73d4-12f4-4aa6-98b`.

## Layout

`orpheus/domain/` contains project, media and sound-editing services; `orpheus/agent/` contains the ADK workflow and prompts; `orpheus/server/` contains the HTTP API, Cloud Run Job dispatcher and worker; and `orpheus/ops/` contains telemetry and Grafana helpers. `frontend/src/` is grouped into `app/`, `features/`, `media/`, `state/`, `styles/`, `assets/` and `tests/`. The top-level `observability/` directory contains isolated Grafana/Loki/Tempo/Prometheus assets. `tests/` uses generated temporary media and never depends on the reference lab at runtime.
