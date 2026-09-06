# Agent Engine Runtime deployment

The current Orpheus workflow is a project-scoped media worker: it receives a
project directory, runs deterministic FFmpeg/NumPy work, and writes a receipt.
It is not yet a standalone `root_agent` package that the ADK CLI can deploy
directly. Keep this file as the cloud gate and deployment contract; do not run
the command below until an Agent Engine entrypoint owns the same tool contract
without depending on the API container's local filesystem.

When that entrypoint exists, deploy it separately from the HTTP and media
containers. The deployment command is intentionally documented, not executed:

```sh
PROJECT_ID=orpheus-agentic
REGION=us-central1
adk deploy agent_engine \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --display_name="Orpheus" \
  orpheus
```

Record the numeric reasoning engine ID returned by the deployment as
`ORPHEUS_AGENT_ENGINE_ID`. Set `ORPHEUS_SESSION_BACKEND=agent_engine` for the
worker/API image so ADK uses `VertexAiSessionService`; local development
continues to use the SQLite `DatabaseSessionService`.

Memory Bank is deliberately disabled by default (`ORPHEUS_MEMORY_BANK=0`). Enable it only after the product has an explicit consent and retention policy for durable user preferences. Sessions contain the workflow event history; Memory Bank is not a replacement for project records, media, or Grafana telemetry.

The `adk deploy agent_engine` command requires a billing-enabled Google Cloud project and the relevant Agent Platform APIs. Do not run it until the project billing decision and cost guardrails are accepted.
