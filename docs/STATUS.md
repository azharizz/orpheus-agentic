# Orpheus delivery status

Updated 2026-09-06. This is a local, single-user production-quality baseline with a cloud-ready deployment boundary. It is not a perceptual sound-quality certificate.

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

## Known limits

- The paid parity run is evidence of wiring and real-provider behavior only. Its own receipt keeps the result provisional: source character mismatch, timing/listening uncertainty, unresolved acoustic hypotheses, and whole-soundtrack replacement are disclosed for human review.
- Browser capture still depends on the user's microphone permission and hardware. Automated tests cover cancellation, final chunk delivery, cleanup, mute restoration, and upload boundaries without granting that permission.
- Grafana remains a separate operational UI. The application links to it and queries the official MCP service; it does not imitate Grafana's theme.
- The Cloud Run boundary is implemented, but no billing account was linked, API was enabled, IAM grant was made, image was pushed, service/job was created, or Firebase API rewrite was enabled. Those are deliberate external-state gates.
- Agent Engine session selection and Vertex model selection are implemented behind configuration. The current project-scoped worker still needs a standalone Agent Engine `root_agent` entrypoint before managed runtime deployment; Memory Bank consent flow and Cloud SQL product-record migration also require the cloud project and credentials. Local filesystem JSON and local ADK SQLite remain the verified default.

## Layout

`orpheus/domain/` contains project, media and sound-editing services; `orpheus/agent/` contains the ADK workflow and prompts; `orpheus/server/` contains the HTTP API, Cloud Run Job dispatcher and worker; and `orpheus/ops/` contains telemetry and Grafana helpers. `frontend/src/` is grouped into `app/`, `features/`, `media/`, `state/`, `styles/`, `assets/` and `tests/`. The top-level `observability/` directory contains isolated Grafana/Loki/Tempo/Prometheus assets. `tests/` uses generated temporary media and never depends on the reference lab at runtime.
