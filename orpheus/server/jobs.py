"""Submit explicit turns through Agent Engine or the lean Cloud Run path."""

import re
from urllib.parse import quote

from .. import config


def payload(project_id, feedback, run_key=None):
    if not re.fullmatch(r"[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project ID")
    if not isinstance(feedback, str) or not 1 <= len(feedback) <= config.MAX_FEEDBACK_CHARS:
        raise ValueError("Feedback must be 1 to 500 characters.")
    args = [project_id, "--feedback", feedback]
    if run_key:
        if not re.fullmatch(r"[a-f0-9]{16,64}", run_key):
            raise ValueError("Invalid run key")
        args += ["--run-key", run_key]
    return {"overrides": {"containerOverrides": [{"args": args}]}}


def _submission(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        if value.get("status") == "submitted":
            return value
        for child in value.values():
            found = _submission(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _submission(child)
            if found:
                return found
    return None


def _dispatch_agent_engine(project_id, feedback, run_key=None):
    import vertexai
    from vertexai import agent_engines

    vertexai.init(
        project=config.GOOGLE_CLOUD_PROJECT,
        location=config.GOOGLE_CLOUD_LOCATION,
    )
    resource = (
        f"projects/{config.GOOGLE_CLOUD_PROJECT}/locations/"
        f"{config.GOOGLE_CLOUD_LOCATION}/reasoningEngines/{config.AGENT_ENGINE_ID}"
    )
    message = (
        "Explicit run request already approved by the Orpheus API. "
        "Call submit_orpheus_worker immediately with "
        f"project_id={project_id}, feedback={feedback!r}, run_key={run_key or 'generated'}."
    )
    try:
        engine = agent_engines.get(resource)
        for event in engine.stream_query(
            user_id="local",
            session_id=project_id,
            message=message,
        ):
            result = _submission(event)
            if result:
                return {
                    "status": "submitted",
                    "operation": result.get("operation"),
                    "runtime": "agent_engine",
                }
    except Exception as exc:
        raise RuntimeError("Agent Engine dispatch unavailable") from exc
    raise RuntimeError("Agent Engine did not submit the worker job")


def _dispatch_cloud_run(project_id, feedback, run_key=None):
    if config.RUNTIME_MODE != "cloud_run":
        raise RuntimeError("Cloud Run Job dispatch is only available in cloud_run mode")
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
        response = AuthorizedSession(credentials).post(url, json=payload(project_id, feedback, run_key), timeout=20)
    except Exception as exc:
        raise RuntimeError("Cloud Run Job credentials or transport unavailable") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"Cloud Run Job dispatch failed ({response.status_code})")
    try:
        result = response.json()
    except ValueError:
        result = {}
    return {"status": "submitted", "operation": result.get("name") if isinstance(result, dict) else None}


def dispatch(project_id, feedback, run_key=None):
    """Submit one bounded worker turn to the selected hosted runtime."""
    payload(project_id, feedback, run_key)
    if config.RUNTIME_MODE != "cloud_run":
        raise RuntimeError("Hosted dispatch is only available in cloud_run mode")
    if not config.GOOGLE_CLOUD_PROJECT:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required")
    if config.AGENT_ENGINE_ID:
        try:
            return (
                _dispatch_agent_engine(project_id, feedback, run_key)
                if run_key
                else _dispatch_agent_engine(project_id, feedback)
            )
        except RuntimeError:
            # ponytail: keep one bounded runtime escape hatch for transient Agent Engine quota failures.
            result = _dispatch_cloud_run(project_id, feedback, run_key)
            result["runtime"] = "cloud_run_fallback"
            return result
    if not config.JOB_NAME:
        raise RuntimeError("ORPHEUS_CLOUD_RUN_JOB is required")
    return _dispatch_cloud_run(project_id, feedback, run_key)


def cancel(operation):
    if not operation:
        return False
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        response = AuthorizedSession(credentials).post(
            "https://run.googleapis.com/v2/" + operation.lstrip("/") + ":cancel",
            timeout=20,
        )
        return response.status_code < 400
    except Exception:
        return False
