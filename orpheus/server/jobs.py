"""Cloud Run Job dispatch with a local-only fallback kept in ``web``."""

import re
from urllib.parse import quote

from .. import config


def payload(project_id, feedback):
    if not re.fullmatch(r"[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project ID")
    if not isinstance(feedback, str) or not 1 <= len(feedback) <= config.MAX_FEEDBACK_CHARS:
        raise ValueError("Feedback must be 1 to 500 characters.")
    return {"overrides": {"containerOverrides": [{"args": [project_id, "--feedback", feedback]}]}}


def dispatch(project_id, feedback):
    """Submit one bounded worker turn to the configured Cloud Run Job."""
    if config.RUNTIME_MODE != "cloud_run":
        raise RuntimeError("Cloud Run Job dispatch is only available in cloud_run mode")
    if not config.GOOGLE_CLOUD_PROJECT or not config.JOB_NAME:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT and ORPHEUS_CLOUD_RUN_JOB are required")
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        url = (
            "https://run.googleapis.com/v2/projects/"
            + quote(config.GOOGLE_CLOUD_PROJECT, safe="")
            + "/locations/"
            + quote(config.GOOGLE_CLOUD_LOCATION, safe="")
            + "/jobs/"
            + quote(config.JOB_NAME, safe="")
            + ":run"
        )
        response = AuthorizedSession(credentials).post(url, json=payload(project_id, feedback), timeout=20)
    except Exception as exc:
        raise RuntimeError("Cloud Run Job credentials or transport unavailable") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Cloud Run Job dispatch failed ({response.status_code})")
    try:
        result = response.json()
    except ValueError:
        result = {}
    return {"status": "submitted", "operation": result.get("name") if isinstance(result, dict) else None}
