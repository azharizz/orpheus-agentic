# GCP Architecture Implementation Plan

- **Goal:** Move Orpheus toward the documented local-first GCP architecture while preserving the v2 product behavior and keeping cloud spend gated.
- **Architecture:** Firebase Hosting serves the two-page frontend; Cloud Run API serves the HTTP application; a Cloud Run Job owns bounded media processing; Agent Engine Runtime owns the ADK agent and managed sessions; Memory Bank is opt-in for durable cross-session context; Cloud Storage and Cloud SQL are production persistence; Grafana Cloud is the hosted observability backend with a private `grafana-mcp` adapter.
- **Constraints:** Local single-user operation remains the default. Legacy and v1 compatibility stays removed. Provider failover remains explicit. No React `useEffect`. No billing-account linking, API enablement, resource creation, or paid parity call from this change. Firebase rewrites stay disabled until a real Cloud Run URL exists. Source files remain below 800 lines. Cloud adapters must fail closed when their optional credentials are absent.
- **Validation:** Run frontend tests/build, Python tests in the project environment, Dockerfile syntax/build checks where available, `git diff --check`, and a read-only deployment preflight. Any cloud command that mutates billing, APIs, IAM, storage, SQL, jobs, or services is excluded from this pass.

## Tasks

1. **Make the HTTP runtime Cloud Run compatible.**
   - Add an explicit runtime mode and host/port resolution in `orpheus/config.py`.
   - Bind the server to `0.0.0.0` only in Cloud Run mode and keep `127.0.0.1` locally.
   - Make request origin checks use configured public origin in Cloud Run mode while retaining the localhost safety check locally.
   - Keep worker startup local-only; Cloud Run receives a single process and uses the configured managed-session boundary.
   - Add focused request/runtime tests.

2. **Package the API as a deployable container.**
   - Add one minimal `Dockerfile` for the API, a `.dockerignore`, and a non-mutating `deploy/check.sh` preflight.
   - Use the existing `requirements.txt`; do not add a framework or a second server.
   - Document required runtime environment and the exact `gcloud run deploy` command without executing it.
   - Verify the image can at least parse/build locally when Docker and network dependencies are available.

3. **Put media work behind a Cloud Run Job boundary.**
   - Extract the existing bounded media operation into a callable job entry point.
   - Add a tiny launcher that submits a named Cloud Run Job through the official v2 REST endpoint when configured, while keeping the local subprocess path as the default.
   - Add a job image definition and an example job manifest; do not create or execute a remote job.
   - Keep output/status handling idempotent and bounded by the existing request model.
   - Add a local dry-run check for payload construction.

4. **Separate persistence boundaries without rewriting the domain.**
   - Add environment-driven storage/database configuration for Cloud Storage and Cloud SQL URLs.
   - Keep the filesystem adapter for local mode and make cloud adapters explicit opt-ins with clear startup errors when credentials are missing.
   - Add SQL schema/migration notes for project/session/asset metadata only; media bytes remain object storage.
   - Do not duplicate domain logic or introduce a generic repository framework.

5. **Add managed model/session configuration.**
   - Add Google/Vertex model configuration as the contest profile and keep OpenRouter failover in the post-contest profile.
   - Keep provider selection declarative through environment/config values.
   - Make managed session persistence selectable without changing the v2 agent contract.
   - Add configuration validation that catches an invalid contest profile before startup.

6. **Define the private Grafana MCP adapter deployment.**
   - Add a minimal adapter container/manifest and environment example for a private Cloud Run service.
   - Keep Grafana credentials server-side, redact token-like values in API responses, and expose only the existing read-only observability contract.
   - Document low-frequency ingestion defaults and the free-plan retention/capacity assumptions.
   - Do not subscribe to the Marketplace listing or change the existing Grafana organization.

7. **Prepare Firebase Hosting for the API rewrite.**
   - Keep the current static routes for `/` and `/workspace`.
   - Add a commented/example rewrite target and a deploy-time environment checklist, but leave production rewrites disabled until the Cloud Run URL is known.
   - Verify the deployed static site continues to return both pages and a 404 for `/api` while the backend is intentionally absent.

8. **Run verification and checkpoint.**
   - Run all available local checks and the read-only GCP preflight.
   - Update `docs/STATUS.md` with what is implemented, what remains gated, and the exact next cloud actions.
   - Commit the implementation as one reviewable checkpoint.
