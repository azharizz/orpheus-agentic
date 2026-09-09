"""Validate uploads and preserve originals beside separate prepared media."""

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from ..config import (
    DATA_DIR,
    FREE_DISK_MARGIN_BYTES,
    PROJECTS,
    VIDEO_UPLOAD_LIMIT_BYTES,
    prepare_storage,
)
from . import media

ROOT = DATA_DIR

prepare_storage()


def atomic(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def project_dir(project_id):
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project ID")
    return PROJECTS / project_id


def remove(project_id):
    folder = project_dir(project_id)
    if not folder.is_dir() or folder.is_symlink():
        raise FileNotFoundError(project_id)
    shutil.rmtree(folder)


def load(project_id):
    path = project_dir(project_id)
    doc = json.loads((path / "project.json").read_text())
    if doc.get("schema") != "orpheus.v3":
        raise ValueError("Unsupported project schema")
    source_name = doc["preparation"]["original_files"]["video"]
    return {
        **doc,
        "video_path": path / "video.mp4",
        "source_video_path": path / source_name,
        "original_path": path / "original.wav",
        "mix_path": path / "mix.wav",
    }


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _needs_staging(path):
    """gcsfuse rejects the backward seeks encoders use to patch headers."""
    from .. import config

    return config.STORAGE_BACKEND == "gcs" and str(path).startswith(str(config.DATA_DIR))


def ff(*args):
    args = list(args)
    target = Path(str(args[-1])) if args else None
    if target is None or not _needs_staging(target):
        _run_ff(args)
        return
    with tempfile.TemporaryDirectory(prefix="orpheus-ff-") as staging:
        local = Path(staging) / target.name
        _run_ff([*args[:-1], local])
        target.parent.mkdir(parents=True, exist_ok=True)
        publish(local, target)


def _run_ff(args):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", *map(str, args)],
        check=True,
        capture_output=True,
        timeout=7200,
    )


def publish(local, target):
    """Copy a finished file to storage in one sequential pass."""
    with open(local, "rb") as source, open(target, "wb") as sink:
        shutil.copyfileobj(source, sink, 8 * 1024 * 1024)


def probe(path):
    return json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    )


def frames(case, times, directory):
    """Decode bounded timestamp seeks without enumerating every frame in a film."""
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for t in times:
        if (
            not isinstance(t, (int, float))
            or not math.isfinite(t)
            or not 0 <= t < case["seconds"]
        ):
            raise ValueError("Frame outside clipped window")
        requested = min(float(t), max(0.0, case["seconds"] - 0.001))
        actual, path = requested, None
        for attempt in (requested, max(0.0, case["seconds"] - 0.25)):
            candidate = directory / f"frame-{attempt:.6f}.jpg"
            try:
                if not candidate.exists() or not candidate.stat().st_size:
                    ff(
                        "-ss", attempt, "-i", case["video_path"], "-frames:v", 1,
                        "-vf", "scale=512:-2,format=yuvj420p", "-threads", 1,
                        "-q:v", 4, candidate,
                    )
                if candidate.exists() and candidate.stat().st_size:
                    actual, path = attempt, candidate
                    break
            except subprocess.SubprocessError:
                candidate.unlink(missing_ok=True)
        if path is None:
            raise ValueError("Frame could not be decoded")
        result.append((actual, path))
    return result


def _inspect_upload(video, context, style):
    video = Path(video)
    if len(context) > 240 or len(style) > 240:
        raise ValueError("Context and sound brief are limited to 240 characters each")
    if video.suffix.lower() not in (".mp4", ".mov", ".webm", ".mkv"):
        raise ValueError("Unsupported file extension")
    if not video.is_file() or not 0 < video.stat().st_size <= VIDEO_UPLOAD_LIMIT_BYTES:
        raise ValueError("Video must be nonempty and within the configured limit")
    metadata = probe(video)
    if not any(stream["codec_type"] == "video" for stream in metadata["streams"]):
        raise ValueError("Target must contain video")
    seconds = float(metadata["format"]["duration"])
    if not math.isfinite(seconds) or seconds < 0.5:
        raise ValueError("Video must be at least 0.5 seconds with a finite duration")
    has_audio = any(stream["codec_type"] == "audio" for stream in metadata["streams"])
    channels = int(next((stream.get("channels", 1) for stream in metadata["streams"] if stream["codec_type"] == "audio"), 1))
    pcm_bytes = round(seconds * media.RATE * 2 * (1 + (min(2, channels) if has_audio else 1)))
    required = video.stat().st_size * 2 + pcm_bytes + FREE_DISK_MARGIN_BYTES
    PROJECTS.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(PROJECTS).free < required:
        raise ValueError("Insufficient free disk space for originals and prepared media")
    return video, metadata, seconds, has_audio, channels


def intake(video, context="", style="", video_name=None):
    """Validate and retain an upload quickly; derived media is prepared in background."""
    video, metadata, seconds, has_audio, channels = _inspect_upload(video, context, style)
    PROJECTS.mkdir(parents=True, exist_ok=True)
    pid = uuid.uuid4().hex[:16]
    folder = project_dir(pid)
    folder.mkdir(parents=True)
    originals = folder / "originals"
    originals.mkdir()
    source = originals / ("video" + video.suffix.lower())
    shutil.copyfile(video, source)
    doc = {
        "schema": "orpheus.v3",
        "id": pid,
        "seconds": seconds,
        "input_duration_s": seconds,
        "context": context,
        "style": style,
        "has_original_audio": has_audio,
        "video_name": (video_name or video.name)[:180],
        "source_hashes": {"video": digest(source)},
        "prepared_hashes": {},
        "rights": "User-supplied local test media; no redistribution licence inferred.",
        "preparation": {
            "video_truncated": False,
            "progress": 0,
            "original_files": {"video": "originals/" + source.name},
            "input_channels": channels,
            "warning": "Private byte-for-byte original retained; proxy and PCM are derived in the background.",
        },
        "input_warnings": ["mono_analysis_copy"] + ([] if has_audio else ["no_original_audio"]),
        "status": "preparing",
        "turns": [],
    }
    atomic(folder / "project.json", doc)
    return doc


def prepare(project_id):
    """Create full-duration proxy and PCM copies; safe to retry after interruption."""
    folder = project_dir(project_id)
    doc = json.loads((folder / "project.json").read_text())
    source = folder / doc["preparation"]["original_files"]["video"]
    channels = int(doc["preparation"]["input_channels"])
    try:
        doc["status"] = "preparing"
        doc["preparation"]["progress"] = 10
        atomic(folder / "project.json", doc)
        ff("-protocol_whitelist", "file,pipe", "-i", source, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", folder / "video.mp4")
        doc["preparation"]["progress"] = 60
        atomic(folder / "project.json", doc)
        if doc["has_original_audio"]:
            ff("-protocol_whitelist", "file,pipe", "-i", source, "-map", "0:a:0", "-vn", "-ar", media.RATE, "-ac", 1, "-c:a", "pcm_s16le", folder / "original.wav")
            ff("-protocol_whitelist", "file,pipe", "-i", source, "-map", "0:a:0", "-vn", "-ar", media.RATE, "-ac", min(2, channels), "-c:a", "pcm_s16le", folder / "mix.wav")
        else:
            ff("-f", "lavfi", "-i", f"anullsrc=r={media.RATE}:cl=mono", "-t", doc["seconds"], folder / "original.wav")
            shutil.copyfile(folder / "original.wav", folder / "mix.wav")
        files = ["video.mp4", "original.wav", "mix.wav"]
        doc["prepared_hashes"] = {name: digest(folder / name) for name in files}
        doc["preparation"].update(
            progress=100,
            analysis_format="Full-duration 48kHz mono PCM16 analysis copy",
            mix_format=f"Full-duration 48kHz {min(2, channels) if doc['has_original_audio'] else 1}-channel PCM16 working mix",
        )
        doc["status"] = "ready"
        atomic(folder / "project.json", doc)
        return doc
    except Exception:
        doc["status"] = "preparation_failed"
        atomic(folder / "project.json", doc)
        atomic(folder / "preparation_failed.json", {"error": "Media preparation failed; no agent called"})
        raise


def create(video, context="", style="", video_name=None):
    """Synchronous compatibility for scripts and tests; the web API uses background preparation."""
    return prepare(intake(video, context, style, video_name)["id"])
