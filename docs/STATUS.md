# Orpheus delivery status

Updated 2026-09-07. This is a local, single-user production-quality baseline with a managed GCP deployment slice. It is not a perceptual sound-quality certificate.

## Verified

- Clean package exposes the complete v2 workflow tool inventory: 28 tools, with no v1 or legacy HTTP routes.
- Backend suite: 83 tests pass, one opt-in live MCP test is skipped by default.
- Live MCP proof: a real ADK run queried the official Loki/Prometheus MCP service and wrote a linked take experiment receipt.
- Isolated Grafana stack: setup is idempotent, credentials remain in runtime storage, the collector is scraped by Prometheus, and the lab containers remain separate.
- Authorized paid hosted parity run: one turn, capped at 40 controller calls. Vertex `gemini-3.0-flash` exhausted its quota and the explicit OpenRouter failover served 23 controller responses. The worker completed with two measured candidates, three successful Grafana investigations, and `review_required`; no approval was invented. The preferred candidate passed engineering checks but still requires human listening.
- HTTP coverage includes media preparation, origin checks, retired routes, range requests, waveforms, takes, explicit run dispatch, exact review identity, assisted revision lineage, and stale-audio rejection.
- Frontend: 14 Node lifecycle/transport/domain checks pass and the Vite production build passes.
- Browser QA covered landing/workspace at desktop and mobile widths, picture playback, independent-source playback, source/picture exclusivity, candidate switching, evidence rail, review disclosures, and the explicit paid-run consent gate.
- Impeccable detector returned no findings for `frontend/src` after the bounded visual pass. Self-hosted Barlow Condensed and Source Sans 3 assets are licensed and bundled.
- No `useEffect`, imperative DOM queries, reference-lab imports, or source file over 800 lines in application code.
- Cloud-ready runtime mode is explicit: the API accepts Cloud Run's `PORT`, binds to `0.0.0.0`, validates configured public origins, and dispatches long turns to a named Cloud Run Job. `Dockerfile`, `deploy/cloud-run-job.yaml`, and read-only `deploy/check.sh` are included.
- Hosted slice is live in GCP project `orpheus-agentic`: Firebase Hosting serves the two-page frontend, `/api/**` rewrites to Cloud Run `orpheus-api`, and Cloud Run Job `orpheus-worker` is deployed with the pinned image digest. Media uses the private `orpheus-agentic-media` bucket; provider fallback uses Secret Manager.
- Agent Engine Runtime is deployed as reasoning engine `5166883865117065216`. Managed Sessions and selective Memory Bank are enabled in the API/job worker; the managed runtime coordinates explicit turns while the existing full media workflow remains in `orpheus-worker`.
- Grafana MCP is deployed as a Cloud Run IAM-protected service `grafana-mcp`; only the API and media service accounts can invoke it. Its Grafana Viewer token and MCP caller token are in Secret Manager; the app can query Grafana Cloud without exposing either token to the browser.

## Known limits

- The paid parity run is evidence of wiring and real-provider behavior only. Its own receipt keeps the result provisional: source character mismatch, timing/listening uncertainty, unresolved acoustic hypotheses, and whole-soundtrack replacement are disclosed for human review.
- Browser capture still depends on the user's microphone permission and hardware. Automated tests cover cancellation, final chunk delivery, cleanup, mute restoration, and upload boundaries without granting that permission.
- Grafana remains a separate operational UI. The application links to it and queries the official MCP service; it does not imitate Grafana's theme.
- The media worker uses Cloud Run's project-scoped Job. Hosted project, receipt, and run metadata now mirror to Cloud SQL in `project-cb6f73d4-12f4-4aa6-98b`; Agent Engine Sessions hold ADK event history and Cloud Storage holds media and artifacts.
- Grafana MCP is query-connected and redacted Loki/OTLP writer export is configured. The service starts with a read-only Grafana token and the app keeps credentials server-side.
- Agent Engine Session quota can return `429`/`ServerError`; hosted dispatch and the worker have an explicit bounded Cloud Run/Cloud SQL fallback so a transient managed quota outage does not strand an approved run. Memory Bank search succeeded in the parity turn, while the final write returned a service-side `ServerError`; the managed Agent Engine and Memory Bank remain deployed and enabled, but quota/error health must be rechecked before treating managed persistence as available.
- The parity worker initially hit GCS FUSE per-object mutation throttling; hosted checkpoints now batch `events.jsonl` and turn receipt writes to a two-second minimum and flush the final state once.

## Layout

`orpheus/domain/` contains project, media and sound-editing services; `orpheus/agent/` contains the ADK workflow and prompts; `orpheus/server/` contains the HTTP API, Cloud Run Job dispatcher and worker; and `orpheus/ops/` contains telemetry and Grafana helpers. `frontend/src/` is grouped into `app/`, `features/`, `media/`, `state/`, `styles/`, `assets/` and `tests/`. The top-level `observability/` directory contains isolated Grafana/Loki/Tempo/Prometheus assets. `tests/` uses generated temporary media and never depends on the reference lab at runtime.
