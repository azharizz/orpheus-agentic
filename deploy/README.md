# GCP deployment

This directory contains the deployable definitions. The current project has a lean hosted slice; the managed Agent Engine and Grafana Cloud adapter remain gated by missing runtime prerequisites.

1. Run `./deploy/check.sh orpheus-agentic`. It only reads project, billing, API, Cloud Run, Job, and SQL state.
2. Link a billing account only when you accept the trial-credit risk. A free-trial project still needs billing enabled before Cloud Run, Agent Engine, Cloud SQL, Artifact Registry, Secret Manager, or Cloud Storage provisioning.
3. Enable only the APIs required by the architecture after the billing decision.
4. Build and publish `Dockerfile` to Artifact Registry.
5. Create the `orpheus-worker` Job from `cloud-run-job.yaml`, replacing `IMAGE_URI` and setting the Secret Manager reference.
6. Deploy the ADK workflow to Agent Engine Runtime using [`agent-engine.md`](agent-engine.md), then record its reasoning-engine ID.
7. Deploy the API from the same image with `ORPHEUS_RUNTIME_MODE=cloud_run`, `ORPHEUS_CLOUD_RUN_JOB=orpheus-worker`, `ORPHEUS_PUBLIC_ORIGIN`, and the Vertex/Cloud SQL/Agent Engine settings in `.env.example`.
8. Merge the `/api/**` rewrite from `firebase.rewrite.example.json` into `firebase.json` only after the API URL/service exists, then deploy Firebase Hosting.

The deployed command history is intentionally kept outside the repository because it contains project-specific resource names. Do not execute the Agent Engine step until `agent-engine.md` has a standalone `root_agent` entrypoint, and do not deploy the Grafana adapter until a scoped Grafana Cloud service-account token is stored in Secret Manager.
