"""Runtime settings shared by ingestion, agents and the HTTP API."""

import os
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = Path(__file__).resolve().parent
VALUES = {**dotenv_values(ROOT / ".env"), **os.environ}
DATA_DIR = Path(VALUES.get("ORPHEUS_DATA_DIR", ROOT / "data")).expanduser().resolve()
PROJECTS = DATA_DIR / "projects"
RUNTIME_MODE = str(VALUES.get("ORPHEUS_RUNTIME_MODE", "local")).lower()
if RUNTIME_MODE not in {"local", "cloud_run"}:
    raise ValueError("ORPHEUS_RUNTIME_MODE must be local or cloud_run")
OBSERVABILITY_DIR = DATA_DIR / "observability"

STORAGE_BACKEND = str(VALUES.get("ORPHEUS_STORAGE_BACKEND", "local")).lower()
if STORAGE_BACKEND not in {"local", "gcs"}:
    raise ValueError("ORPHEUS_STORAGE_BACKEND must be local or gcs")
GCS_BUCKET = str(VALUES.get("ORPHEUS_GCS_BUCKET", "")).strip()
METADATA_BACKEND = str(VALUES.get("ORPHEUS_METADATA_BACKEND", "filesystem")).lower()
if METADATA_BACKEND not in {"filesystem", "cloud_sql"}:
    raise ValueError("ORPHEUS_METADATA_BACKEND must be filesystem or cloud_sql")
METADATA_DATABASE_URL = str(
    VALUES.get("ORPHEUS_METADATA_DATABASE_URL", "")
).strip()
OWNER_SECRET = str(VALUES.get("ORPHEUS_OWNER_SECRET", "")).strip()
DATABASE_URL = str(
    VALUES.get("ORPHEUS_SESSION_DATABASE_URL", "sqlite+aiosqlite:///" + str(DATA_DIR / "sessions.sqlite"))
).strip()
GOOGLE_CLOUD_PROJECT = str(
    VALUES.get("GOOGLE_CLOUD_PROJECT", VALUES.get("GCLOUD_PROJECT", ""))
).strip()
GOOGLE_CLOUD_LOCATION = str(VALUES.get("GOOGLE_CLOUD_LOCATION", "us-central1")).strip()
PUBLIC_ORIGIN = str(
    VALUES.get(
        "ORPHEUS_PUBLIC_ORIGIN",
        "http://127.0.0.1:8766" if RUNTIME_MODE == "local" else "",
    )
).rstrip("/")
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in str(VALUES.get("ORPHEUS_ALLOWED_ORIGINS", PUBLIC_ORIGIN)).split(",")
    if origin.strip()
}
ALLOWED_HOSTS = {
    host.strip()
    for host in str(VALUES.get("ORPHEUS_ALLOWED_HOSTS", "")).split(",")
    if host.strip()
}
if PUBLIC_ORIGIN:
    from urllib.parse import urlparse

    public_host = urlparse(PUBLIC_ORIGIN).netloc
    if public_host:
        ALLOWED_HOSTS.add(public_host)
JOB_NAME = str(VALUES.get("ORPHEUS_CLOUD_RUN_JOB", "")).strip()
SESSION_BACKEND = str(VALUES.get("ORPHEUS_SESSION_BACKEND", "database")).lower()
if SESSION_BACKEND not in {"database", "agent_engine"}:
    raise ValueError("ORPHEUS_SESSION_BACKEND must be database or agent_engine")
AGENT_ENGINE_ID = str(VALUES.get("ORPHEUS_AGENT_ENGINE_ID", "")).strip()
MEMORY_BANK_ENABLED = str(VALUES.get("ORPHEUS_MEMORY_BANK", "0")).lower() in (
    "1",
    "true",
    "yes",
)
if RUNTIME_MODE == "cloud_run" and not PUBLIC_ORIGIN:
    raise ValueError("ORPHEUS_PUBLIC_ORIGIN is required in cloud_run mode")
if STORAGE_BACKEND == "gcs" and not GCS_BUCKET:
    raise ValueError("ORPHEUS_GCS_BUCKET is required for the gcs storage backend")
if RUNTIME_MODE == "cloud_run" and METADATA_BACKEND == "cloud_sql" and not METADATA_DATABASE_URL:
    raise ValueError("ORPHEUS_METADATA_DATABASE_URL is required for cloud_sql metadata")
if SESSION_BACKEND == "agent_engine" and not AGENT_ENGINE_ID:
    raise ValueError("ORPHEUS_AGENT_ENGINE_ID is required for the agent_engine session backend")
OBSERVABILITY_ASSETS = ROOT / "observability"
VIDEO_UPLOAD_LIMIT_BYTES = int(
    VALUES.get("ORPHEUS_MAX_VIDEO_BYTES", 20 * 1024 * 1024 * 1024)
)
AUDIO_UPLOAD_LIMIT_BYTES = int(
    VALUES.get("ORPHEUS_MAX_AUDIO_BYTES", 100 * 1024 * 1024)
)
FREE_DISK_MARGIN_BYTES = int(
    VALUES.get("ORPHEUS_FREE_DISK_MARGIN_BYTES", 512 * 1024 * 1024)
)
if min(VIDEO_UPLOAD_LIMIT_BYTES, AUDIO_UPLOAD_LIMIT_BYTES, FREE_DISK_MARGIN_BYTES) <= 0:
    raise ValueError("Media and free-disk limits must be positive")
UPLOAD_LIMIT_BYTES = AUDIO_UPLOAD_LIMIT_BYTES
MEDIA_SECONDS = None
SAMPLE_RATE = 48000
MAX_TAKE_DURATION_S = int(VALUES.get("ORPHEUS_MAX_TAKE_SECONDS", 30))
if not 1 <= MAX_TAKE_DURATION_S <= 300:
    raise ValueError("Take duration must be between 1 and 300 seconds")
CONTROLLER_MODELS = [
    "meta/muse-spark-1.3-contributor",
    "qwen/qwen3.8-flash",
    "deepseek/deepseek-v4-flash-vision-exp",
]
CONTROLLER_MAX_TOKENS = 30000
AUDIO_MODEL = "google/gemini-2.5-flash-lite"
AUDIO_MAX_TOKENS = 30000
PROVIDER_TIMEOUT_SECONDS = 180
TURN_TIMEOUT_SECONDS = 1800
MAX_CONTROLLER_CALLS = 40
MAX_CYCLES = 5
AUDIO_CALL_LIMIT = 6


def prepare_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS.mkdir(parents=True, exist_ok=True)
    OBSERVABILITY_DIR.mkdir(parents=True, exist_ok=True)


MAX_FILE_BYTES = VIDEO_UPLOAD_LIMIT_BYTES
MAX_DURATION_S = MEDIA_SECONDS
MAX_BRIEF_CHARS = 240
MAX_FEEDBACK_CHARS = 500
SERVER_HOST = "0.0.0.0" if RUNTIME_MODE == "cloud_run" else "127.0.0.1"
SERVER_PORT = int(
    VALUES.get("PORT", 8080)
    if RUNTIME_MODE == "cloud_run"
    else VALUES.get("ORPHEUS_SERVER_PORT", 8766)
)
if not 1024 <= SERVER_PORT <= 65535:
    raise ValueError("Orpheus server port must be between 1024 and 65535")

AUDIO_ENABLED = str(
    VALUES.get("ORPHEUS_AUDIO_ENABLED", "0" if RUNTIME_MODE == "cloud_run" else "1")
).lower() in (
    "1",
    "true",
    "yes",
)

# Separate loopback ports keep the reference lab and clean application independent.
GRAFANA_PORTS = {
    "GRAFANA": 13000,
    "LOKI": 13100,
    "PROMETHEUS": 19090,
    "TEMPO": 13200,
    "OTLP": 14319,
    "MCP": 18001,
    "METRICS": 19464,
}
GRAFANA_PORTS = {
    name: int(VALUES.get("ORPHEUS_" + name + "_PORT", value))
    for name, value in GRAFANA_PORTS.items()
}
if any(not 1024 <= port <= 65535 for port in GRAFANA_PORTS.values()) or len(
    set(GRAFANA_PORTS.values())
) != len(GRAFANA_PORTS):
    raise ValueError(
        "Orpheus observability ports must be distinct values from 1024 to 65535"
    )
