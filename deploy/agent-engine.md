# Agent Engine Runtime deployment

The deployed Agent Engine entrypoint is a small coordinator. It receives an
explicit run request, checks the bounded feedback contract, and submits the
full deterministic FFmpeg/NumPy workflow to the `orpheus-worker` Cloud Run Job.
It does not carry media files or replace the worker's tool contract.

Deploy it separately from the HTTP and media containers. The deployment command
used for project `orpheus-agentic` was:

```sh
PROJECT_ID=orpheus-agentic
REGION=us-central1
adk deploy agent_engine \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --agent_engine_id=5166883865117065216 \
  --display_name="Orpheus" \
  agent_engine
```

The deployed reasoning engine ID is `5166883865117065216`. Set
`ORPHEUS_SESSION_BACKEND=agent_engine` and
`ORPHEUS_AGENT_ENGINE_ID=5166883865117065216` for the worker/API image so ADK
uses `VertexAiSessionService`; local development continues to use the SQLite
`DatabaseSessionService`.

Hosted Memory Bank is enabled selectively (`ORPHEUS_MEMORY_BANK=1`) for the
project's explicit context/style preferences. Sessions contain workflow event
history; Memory Bank is not a replacement for project records, media, or
Grafana telemetry. Local development keeps the default disabled setting.

The `adk deploy agent_engine` command requires a billing-enabled Google Cloud
project and the relevant Agent Platform APIs. The deployment uses the linked
GCP free-trial billing account; no paid media/inference parity run was started
as part of this deployment.
