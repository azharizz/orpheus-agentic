# GCP deployment

This directory contains the deployable definitions for the hosted slice in GCP project `orpheus-agentic`.

1. Run `./deploy/check.sh orpheus-agentic`. It only reads project, billing, API, Cloud Run, Job, and SQL state.
2. Link a billing account only when you accept the trial-credit risk. A free-trial project still needs billing enabled before Cloud Run, Agent Engine, Cloud SQL, Artifact Registry, Secret Manager, or Cloud Storage provisioning.
3. Enable only the APIs required by the architecture after the billing decision.
4. Build and publish `Dockerfile` to Artifact Registry.
5. Create the `orpheus-worker` Job from `cloud-run-job.yaml`, replacing `IMAGE_URI` and setting the Secret Manager reference.
6. Deploy the ADK coordinator to Agent Engine Runtime using [`agent-engine.md`](agent-engine.md); the current reasoning-engine ID is `5166883865117065216`.
7. Deploy the API and worker with `ORPHEUS_SESSION_BACKEND=agent_engine`, `ORPHEUS_AGENT_ENGINE_ID=5166883865117065216`, `ORPHEUS_MEMORY_BANK=1`, and the Cloud SQL metadata URL/owner secret. The worker keeps the full deterministic media workflow in a Cloud Run Job and falls back to durable Cloud SQL sessions only for a managed Session quota outage.
8. Merge the `/api/**` rewrite from `firebase.rewrite.example.json` into `firebase.json` only after the API URL/service exists, then deploy Firebase Hosting.

The current Grafana adapter is Cloud Run service `grafana-mcp`, with its Grafana Viewer token and MCP caller token stored in Secret Manager. Keep Cloud Run IAM invocation checks enabled and grant `roles/run.invoker` only to `orpheus-api` and `orpheus-media`; do not grant `allUsers`. Do not put either token in Firebase or frontend configuration. Hosted Grafana datasource UIDs are `grafanacloud-logs` and `grafanacloud-prom`; local defaults remain `orpheus-loki` and `orpheus-prometheus`. Cloud SQL product records use the PostgreSQL instance in `project-cb6f73d4-12f4-4aa6-98b`.

## Browser uploads

Large media never crosses the API: the browser asks for a signed PUT
(`/api/media/staging-url`) and sends the file straight to Cloud Storage.
That cross-origin PUT needs bucket CORS and a service account that can
sign URLs.

```sh
gcloud storage buckets update gs://BUCKET --cors-file=deploy/gcs-cors.json
gcloud iam service-accounts add-iam-policy-binding SA_EMAIL \
  --member="serviceAccount:SA_EMAIL" --role="roles/iam.serviceAccountTokenCreator"
```

## Publishing the dashboard

`grafana-service-account-token` holds the read-only Viewer token that
`grafana-mcp` presents to Grafana; do not overwrite it. Dashboard
publishing needs a separate Editor token in
`grafana-dashboard-writer-token`:

```sh
printf %s "$EDITOR_TOKEN" | gcloud secrets versions add \
  grafana-dashboard-writer-token --data-file=- --project=PROJECT_ID

GRAFANA_SERVICE_ACCOUNT_TOKEN=$(gcloud secrets versions access latest \
  --secret=grafana-dashboard-writer-token --project=PROJECT_ID) \
ORPHEUS_GRAFANA_LOKI_DATASOURCE_UID=grafanacloud-logs \
ORPHEUS_GRAFANA_PROMETHEUS_DATASOURCE_UID=grafanacloud-prom \
ORPHEUS_PUBLIC_ORIGIN=https://YOUR-APP.web.app \
python -m orpheus.ops.grafana publish
```

Grafana Cloud always sanitizes panel HTML, so the generator emits native
barchart and table panels when `ORPHEUS_PUBLIC_ORIGIN` is set, and the
richer canvas panels only for the local Docker stack.
