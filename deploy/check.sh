#!/usr/bin/env bash
set -euo pipefail

project="${1:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -z "$project" ]]; then
  echo "usage: $0 PROJECT_ID" >&2
  exit 2
fi
command -v gcloud >/dev/null || { echo "gcloud is required" >&2; exit 2; }

echo "Project"
gcloud --quiet projects describe "$project" --format='value(projectId,projectNumber,lifecycleState)'
echo "Billing (read-only)"
gcloud --quiet billing projects describe "$project" --format='value(billingAccountName,billingEnabled)' || true
echo "Required services (enabled only)"
gcloud --quiet services list --project="$project" --enabled \
  --filter='config.name:(run.googleapis.com OR aiplatform.googleapis.com OR sqladmin.googleapis.com OR artifactregistry.googleapis.com OR secretmanager.googleapis.com)' \
  --format='value(config.name)' || true
echo "Cloud Run services (read-only)"
gcloud --quiet run services list --project="$project" --format='value(metadata.name,metadata.location)' || true
echo "Cloud Run jobs (read-only)"
gcloud --quiet run jobs list --project="$project" --format='value(metadata.name,metadata.location)' || true
echo "Cloud SQL instances (read-only)"
gcloud --quiet sql instances list --project="$project" --format='value(name,region,databaseVersion,state)' || true
