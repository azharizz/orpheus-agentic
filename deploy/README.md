# GCP deployment

This directory contains the deployable definitions for the hosted slice in GCP project `orpheus-agentic`.

1. Run `./deploy/check.sh orpheus-agentic`. It only reads project, billing, API, Cloud Run, Job, and SQL state.
2. Link a billing account only when you accept the trial-credit risk. A free-trial project still needs billing enabled before Cloud Run, Agent Engine, Cloud SQL, Artifact Registry, Secret Manager, or Cloud Storage provisioning.
3. Enable only the APIs required by the architecture after the billing decision.
4. Build and publish `Dockerfile` to Artifact Registry.
5. Create the `orpheus-worker` Job from `cloud-run-job.yaml`, replacing `IMAGE_URI` and setting the Secret Manager reference.
6. Deploy the ADK coordinator to Agent Engine Runtime using [`agent-engine.md`](agent-engine.md); the current reasoning-engine ID is `5166883865117065216`.
7. Deploy the API and worker with `ORPHEUS_SESSION_BACKEND=agent_engine`, `ORPHEUS_AGENT_ENGINE_ID=5166883865117065216`, and `ORPHEUS_MEMORY_BANK=1`. The worker keeps the full deterministic media workflow in a Cloud Run Job.
8. Merge the `/api/**` rewrite from `firebase.rewrite.example.json` into `firebase.json` only after the API URL/service exists, then deploy Firebase Hosting.

The current Grafana adapter is Cloud Run service `grafana-mcp`, with its Grafana Viewer token and MCP caller token stored in Secret Manager. Do not put either token in Firebase or frontend configuration. Cloud SQL product records are deferred to `project-cb6f73d4-12f4-4aa6-98b`.
