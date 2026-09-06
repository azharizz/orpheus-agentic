# Orpheus

A local Foley workbench: prepare picture and independent sound, explicitly run fitting, audition alternatives, inspect evidence, and retain human judgments. Fit, Record and Review share one workspace.

## Run locally

Requires Python 3.11, FFmpeg/FFprobe, and Node.js 22.12 or newer.

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
npm ci --prefix frontend
npm run build --prefix frontend
.venv/bin/python -m orpheus
```

Open http://127.0.0.1:8766. Set your OpenRouter key in `.env` before requesting paid fitting. Uploading prepares local media without starting inference. Runtime settings live in `orpheus/config.py`; project media and sessions live under `data/` (or `ORPHEUS_DATA_DIR`). The lab is not a runtime dependency.

The interface serves two routes: `/` and `/workspace`. Inputs are clipped to the configured opening window; fitted output replaces the whole soundtrack. Originals remain private. Browser microphone capture requires explicit permission and does not save until you choose Save.

## Check

```sh
.venv/bin/python -m unittest discover -s tests -t .
npm test --prefix frontend
npm run build --prefix frontend
```

The default tests use synthetic media and scripted model responses. They establish processing and workflow wiring, not autonomous perceptual quality. The optional real Grafana MCP check requires running services and `ORPHEUS_LIVE_MCP=1`.

The Python package is grouped by responsibility: `orpheus/domain/` owns project and media operations, `orpheus/agent/` owns ADK workflow tools and prompts, `orpheus/server/` owns the loopback API and worker, and `orpheus/ops/` owns telemetry and Grafana helpers. The frontend follows the same boundary in `frontend/src/` with `app/`, `features/`, `media/`, `state/`, `styles/`, `assets/`, and `tests/` folders.

## GCP deployment shape

The repository includes a Cloud Run compatible API image (`Dockerfile`), a Cloud Run Job manifest for long worker turns (`deploy/cloud-run-job.yaml`), a managed Agent Engine coordinator (`agent_engine/`), and a read-only project preflight (`deploy/check.sh`). The hosted deployment uses Agent Engine Sessions, selective Memory Bank, Cloud Storage media, and a private Grafana MCP adapter; Cloud SQL product records remain deferred. Keep provider credentials server-side and use the values documented in [`deploy/README.md`](deploy/README.md).

```sh
docker build -t REGION-docker.pkg.dev/PROJECT_ID/orpheus/api:TAG .
./deploy/check.sh PROJECT_ID
```

`deploy/check.sh` only reads project, billing, service, job, and SQL state. It never enables APIs, links billing, creates resources, or deploys an image. Google Cloud free-trial credits still require a billing account linked to the project before billable services such as Cloud Run, Agent Engine, or Cloud SQL can be provisioned.

## Grafana

Provisioning, dashboards and Docker Compose are in `observability/`; the official MCP service is read-only. See `python -m orpheus.ops.grafana --help` for setup and collector commands. Credentials and the evidence outbox belong in runtime storage and must not be committed.

## Status

Read `docs/STATUS.md` for verified checks and remaining work. Read [`docs/GCP_ARCHITECTURE.md`](docs/GCP_ARCHITECTURE.md) for service boundaries, Grafana placement, contest constraints, and cost controls.
