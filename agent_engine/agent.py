"""Managed Agent Engine entrypoint for asynchronous Orpheus turns."""

import os
import re
from urllib.parse import quote

from google.adk.agents import LlmAgent
from google.auth import default
from google.auth.transport.requests import AuthorizedSession


PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "orpheus-agentic")
REGION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
JOB = os.environ.get("ORPHEUS_CLOUD_RUN_JOB", "orpheus-worker")
MODEL = os.environ.get("ORPHEUS_VERTEX_MODEL", "gemini-3.0-flash")


def submit_orpheus_worker(project_id: str, feedback: str) -> dict:
    """Submit one explicitly requested, bounded media turn to Cloud Run Job."""
    if not re.fullmatch(r"[a-f0-9]{16}", project_id):
        return {"error": "Invalid project ID"}
    if not isinstance(feedback, str) or not 1 <= len(feedback) <= 500:
        return {"error": "Feedback must be 1 to 500 characters"}
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    url = (
        "https://run.googleapis.com/v2/projects/"
        + quote(PROJECT, safe="")
        + "/locations/"
        + quote(REGION, safe="")
        + "/jobs/"
        + quote(JOB, safe="")
        + ":run"
    )
    response = AuthorizedSession(credentials).post(
        url,
        json={"overrides": {"containerOverrides": [{"args": [project_id, "--feedback", feedback]}]}},
        timeout=20,
    )
    if response.status_code >= 400:
        return {"error": f"Cloud Run Job dispatch failed ({response.status_code})"}
    body = response.json() if response.content else {}
    return {"status": "submitted", "operation": body.get("name")}


root_agent = LlmAgent(
    name="orpheus_runtime",
    description="Coordinates explicit Orpheus media turns through the bounded Cloud Run worker.",
    model=MODEL,
    instruction=(
        "You are the Orpheus managed runtime coordinator. Explain the current "
        "workflow state, ask for explicit confirmation before any paid turn, and "
        "call submit_orpheus_worker only when the user has clearly requested a run. "
        "A submitted job is not approval or proof of perceptual quality."
    ),
    tools=[submit_orpheus_worker],
)
