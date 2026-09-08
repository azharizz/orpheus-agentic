"""Short-lived Cloud Storage transfer URLs for hosted media."""

import re
from os import environ
from datetime import timedelta

from .. import config

UPLOAD_FIELDS = {"video", "sfx", "audio"}
DOWNLOAD_RE = re.compile(
    r"(?:video\.mp4|poster\.jpg|original\.wav|sfx\.wav|events\.jsonl|[a-f0-9]{12}\.(?:mp4|wav|json)|[a-f0-9]{12}-turn\.json|takes/[a-f0-9]{12}\.wav)"
)


def _bucket():
    if config.STORAGE_BACKEND != "gcs" or not config.GCS_BUCKET:
        raise RuntimeError("Cloud Storage is not configured")
    from google.cloud import storage

    return storage.Client(project=config.GOOGLE_CLOUD_PROJECT).bucket(config.GCS_BUCKET)


def _signed(project_id, object_name, method, content_type=None):
    bucket = _bucket()
    blob = bucket.blob(object_name)
    kwargs = {
        "version": "v4",
        "expiration": timedelta(minutes=15),
        "method": method,
    }
    if content_type:
        kwargs["content_type"] = content_type
    try:
        url = blob.generate_signed_url(**kwargs)
    except AttributeError:
        # Cloud Run's metadata credentials cannot sign locally; use IAM signBlob.
        from google.auth.transport.requests import Request

        credentials = bucket.client._credentials
        if hasattr(credentials, "with_scopes"):
            credentials = credentials.with_scopes(
                ["https://www.googleapis.com/auth/cloud-platform"]
            )
        credentials.refresh(Request())
        email = getattr(credentials, "service_account_email", "")
        if not email or email == "default":
            email = environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "").strip()
        if not email:
            raise RuntimeError("A service-account email is required for signed URLs")
        url = blob.generate_signed_url(
            credentials=credentials,
            service_account_email=email,
            access_token=credentials.token,
            **kwargs,
        )
    return {
        "url": url,
        "method": method,
        "expires_in_s": 900,
        "object": object_name,
    }


def upload_url(project_id, field, content_type="application/octet-stream"):
    if not re.fullmatch(r"[a-f0-9]{16}", project_id) or field not in UPLOAD_FIELDS:
        raise ValueError("Invalid media upload target")
    if not isinstance(content_type, str) or not re.fullmatch(
        r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", content_type
    ):
        raise ValueError("Invalid media content type")
    return _signed(
        project_id,
        f"projects/{project_id}/uploads/{field}",
        "PUT",
        content_type,
    )


def download_url(project_id, name):
    if not re.fullmatch(r"[a-f0-9]{16}", project_id) or not DOWNLOAD_RE.fullmatch(name):
        raise ValueError("Invalid media download target")
    return _signed(project_id, f"projects/{project_id}/{name}", "GET")
