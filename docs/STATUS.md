# Orpheus delivery status

Updated 2026-09-07. This is a local, single-user production-quality baseline with a managed GCP deployment slice. It is not a perceptual sound-quality certificate.

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
- Agent Engine Runtime is deployed as reasoning engine `5166883865117065216`. Managed Sessions and selective Memory Bank are enabled in the API/job worker; the managed runtime coordinates explicit turns while the existing full media workflow remains in `orpheus-worker`.
- Grafana MCP is deployed as private Cloud Run service `grafana-mcp`. Its Grafana Viewer token and MCP caller token are in Secret Manager; the app can query Grafana Cloud without exposing either token to the browser.

## Known limits

- The paid parity run is evidence of wiring and real-provider behavior only. Its own receipt keeps the result provisional: source character mismatch, timing/listening uncertainty, unresolved acoustic hypotheses, and whole-soundtrack replacement are disclosed for human review.
- Browser capture still depends on the user's microphone permission and hardware. Automated tests cover cancellation, final chunk delivery, cleanup, mute restoration, and upload boundaries without granting that permission.
- Grafana remains a separate operational UI. The application links to it and queries the official MCP service; it does not imitate Grafana's theme.
- The media worker still uses Cloud Run's project-scoped job and does not use Cloud SQL for product records yet. Agent Engine Sessions now hold ADK event history, while Cloud Storage holds media and artifacts. Cloud SQL remains deferred to `project-cb6f73d4-12f4-4aa6-98b`.
- Grafana MCP is query-connected, but Loki/Tempo writer export is not configured. The service starts with a read-only Grafana Viewer token and the app uses MCP-only observability when the local Grafana file is absent.
- Cloud SQL is deferred to the later target project `project-cb6f73d4-12f4-4aa6-98b`.

## Layout

`orpheus/domain/` contains project, media and sound-editing services; `orpheus/agent/` contains the ADK workflow and prompts; `orpheus/server/` contains the HTTP API, Cloud Run Job dispatcher and worker; and `orpheus/ops/` contains telemetry and Grafana helpers. `frontend/src/` is grouped into `app/`, `features/`, `media/`, `state/`, `styles/`, `assets/` and `tests/`. The top-level `observability/` directory contains isolated Grafana/Loki/Tempo/Prometheus assets. `tests/` uses generated temporary media and never depends on the reference lab at runtime.
