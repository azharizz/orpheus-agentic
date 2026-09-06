# Hosted Architecture Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the remaining hosted Orpheus architecture paths while preserving the local single-user v2 workflow and its explicit provider/review boundaries.

**Architecture:** Cloud SQL PostgreSQL stores product metadata while Cloud Storage keeps media bytes. Cloud Run issues short-lived signed media URLs and derives a stable owner identity from an HMAC cookie for the current single-user deployment. Grafana Cloud receives redacted Loki/Tempo telemetry through Secret Manager credentials; Agent Engine, Sessions, Memory Bank, and the Cloud Run media Job remain the runtime boundaries.

**Tech Stack:** Python stdlib HTTP server, asyncpg, google-cloud-storage, Google Cloud CLI, Cloud SQL PostgreSQL, Cloud Run, Agent Engine, Grafana Cloud MCP, Firebase Hosting.

**Spec:** `docs/GCP_ARCHITECTURE.md`, `PRODUCT.md`, `DESIGN.md`

## Global Constraints

- Keep only the v2 workflow and no legacy/v1 HTTP routes.
- Local mode remains filesystem-backed and single-user by default.
- Hosted mode must fail closed for missing credentials and must not expose provider, Grafana, SQL, or signing secrets to the browser.
- Preserve explicit provider failover and human approval; no automatic approval or retry that spends model credit.
- Do not use React `useEffect`; keep application source files below 800 lines.
- Keep media bytes in private Cloud Storage and product metadata in Cloud SQL.
- Keep Grafana export redacted to metrics, logs, traces, and receipt identifiers.

---

### Task 1: Add hosted metadata and owner boundaries

**Files:**
- Create: `orpheus/server/metadata.py`
- Create: `orpheus/server/auth.py`
- Modify: `orpheus/config.py`
- Modify: `orpheus/server/web.py`
- Modify: `orpheus/domain/projects.py`
- Test: `tests/server/test_metadata.py`, `tests/server/test_auth.py`

**Interfaces:**
- `metadata.sync_project(doc, owner_id)`, `metadata.sync_turn(project_id, turn)`, and `metadata.list_projects(owner_id)` are no-ops in local mode and use asyncpg in hosted SQL mode.
- `auth.owner_id(handler)` returns a stable owner string and `auth.set_owner_cookie(handler, owner_id)` sets an HttpOnly, SameSite cookie.

- [ ] Add `ORPHEUS_METADATA_BACKEND` (`filesystem` or `cloud_sql`) and `ORPHEUS_OWNER_SECRET` validation in hosted mode.
- [ ] Implement one SQL connection helper with a small schema for owners, projects, turns, candidates, takes, reviews, and artifacts; use parameterized statements and `CREATE TABLE IF NOT EXISTS`.
- [ ] Mirror project/turn/review/take receipts at their existing write points so the current media workflow remains unchanged while SQL becomes the hosted catalog.
- [ ] Add owner cookie issuance and owner filtering to project listing and mutation routes.
- [ ] Run the focused tests and commit `feat: add hosted metadata and ownership`.

### Task 2: Add signed Cloud Storage transfer URLs

**Files:**
- Create: `orpheus/server/storage.py`
- Modify: `orpheus/server/web.py`
- Modify: `orpheus/config.py`
- Modify: `requirements-cloud.txt`
- Test: `tests/server/test_storage.py`

**Interfaces:**
- `storage.upload_url(project_id, field, owner_id)` and `storage.download_url(project_id, name, owner_id)` return `{url, method, expires_at, object}`.

- [ ] Validate project IDs, media field names, object names, and owner access before signing.
- [ ] Use `google.cloud.storage.Client` with V4 signed URLs and a 15-minute expiration; never return a service-account credential.
- [ ] Add `/api/media/upload-url` and `/api/media/download-url` routes while retaining the local multipart routes.
- [ ] Add Cloud Run Storage IAM and deployment environment for the bucket.
- [ ] Run unit tests with a fake signer and commit `feat: add signed media transfers`.

### Task 3: Provision Cloud SQL and connect hosted services

**Files:**
- Modify: `deploy/cloud-run-service.yaml`, `deploy/cloud-run-job.yaml`, `deploy/README.md`, `docs/GCP_ARCHITECTURE.md`, `docs/STATUS.md`
- Create: `deploy/cloud-sql.sql`

- [ ] Create a PostgreSQL instance, database, and least-privilege application user in `project-cb6f73d4-12f4-4aa6-98b`.
- [ ] Store the database URL/password in Secret Manager in the same target project and grant API/media service accounts access.
- [ ] Deploy the schema and configure Cloud Run API and Job with Cloud SQL connector settings and `ORPHEUS_METADATA_BACKEND=cloud_sql`.
- [ ] Verify project creation, turn synchronization, owner filtering, and restart persistence.
- [ ] Run the read-only deployment check and commit `feat: connect hosted product metadata`.

### Task 4: Export redacted telemetry to Grafana Cloud

**Files:**
- Modify: `orpheus/ops/observability.py`, `deploy/cloud-run-service.yaml`, `deploy/cloud-run-job.yaml`, `deploy/README.md`
- Test: `tests/ops/test_observability.py`

- [ ] Create a scoped Grafana Cloud writer credential with metrics/logs/traces write permissions.
- [ ] Store it in Secret Manager; configure the remote Loki and Tempo endpoints discovered from the Grafana stack.
- [ ] Preserve the SQLite outbox, batching, bounded retries, and redaction; add remote bearer/basic auth only inside the exporter.
- [ ] Send a synthetic redacted event and verify it appears through Grafana API/MCP.
- [ ] Commit `feat: export hosted telemetry to grafana cloud`.

### Task 5: Harden hosted lifecycle and prove MCP

**Files:**
- Modify: `orpheus/server/jobs.py`, `orpheus/server/web.py`, `orpheus/server/worker.py`, `orpheus/ops/observability.py`
- Test: `tests/server/test_jobs.py`, `tests/server/test_http.py`

- [ ] Add idempotency keys to run dispatch and refuse duplicate active runs for the same project.
- [ ] Persist cancellation and restart state in SQL and keep stale evidence rejected by hash.
- [ ] Execute a hosted MCP initialize/list-tools/query call through the private Cloud Run adapter using Cloud Run identity plus the caller token.
- [ ] Verify the Grafana receipt is linked to a real redacted event and no raw media/prompt data is exported.
- [ ] Commit `test: prove hosted lifecycle and grafana mcp`.

### Task 6: Run the capped hosted parity and final verification

**Files:**
- Modify: `docs/STATUS.md`, `docs/GCP_ARCHITECTURE.md`, `deploy/agent-engine.md`

- [ ] Upload the existing shoes test media to a hosted project through signed URLs.
- [ ] Run exactly one explicit paid turn with the existing 40-call cap and provider failover policy.
- [ ] Verify the worker receipt, Agent Engine Session, Memory Bank write, Cloud SQL catalog, Cloud Storage artifacts, Grafana evidence, and human-review gate.
- [ ] Run backend tests, frontend tests/build, compile checks, deployment checks, and `git diff --check`.
- [ ] Commit `docs: record completed hosted architecture verification`.
