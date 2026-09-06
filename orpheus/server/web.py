"""Same-origin local API and the two Orpheus application routes."""

import argparse
import asyncio
import fcntl
import io
import json
import re
import subprocess
import sys
import tempfile
import threading
import wave
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from .. import config
from ..domain import media, projects, review, takes
from ..ops import observability as obs
from .http import LocalHandler, RequestError
from . import jobs

LOCK = threading.RLock()
UPLOAD_LOCK = threading.Lock()
PROCESS = None
STATIC = config.ROOT / "frontend" / "dist"
DEFAULT_FEEDBACK = "Create a fitted alternative from these files."


def busy():
    projects.ROOT.mkdir(parents=True, exist_ok=True)
    with (projects.ROOT / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return PROCESS is not None and PROCESS.poll() is None


@contextmanager
def mutation():
    # ponytail: one local editing operation at a time; use per-project jobs for multiple users.
    with LOCK:
        if busy():
            raise BlockingIOError()
        with (projects.ROOT / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def start(project_id, feedback):
    global PROCESS
    projects.load(project_id)
    if (
        not isinstance(feedback, str)
        or not 1 <= len(feedback) <= config.MAX_FEEDBACK_CHARS
    ):
        raise ValueError("Feedback must be 1 to 500 characters.")
    with LOCK:
        if busy():
            raise BlockingIOError()
        if config.RUNTIME_MODE == "cloud_run":
            jobs.dispatch(project_id, feedback)
            return
        with (projects.project_dir(project_id) / "worker.log").open("ab") as output:
            PROCESS = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "orpheus.server.worker",
                    project_id,
                    "--feedback",
                    feedback,
                ],
                cwd=config.ROOT,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )


def project_list():
    rows, errors = [], []
    for path in sorted(
        projects.PROJECTS.glob("*/project.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            doc = json.loads(path.read_text())
            doc["turn_details"] = [
                json.loads((path.parent / (tid + "-turn.json")).read_text())
                for tid in doc["turns"]
                if (path.parent / (tid + "-turn.json")).exists()
            ]
            doc["assisted_candidates"] = [
                json.loads(p.read_text())
                for p in sorted(path.parent.glob("*-assisted.json"))
            ]
            doc["human_reviews"] = [
                json.loads(p.read_text())
                for p in sorted(path.parent.glob("*-human.json"))
            ]
            rows.append(doc)
        except (ValueError, KeyError, OSError):
            errors.append(
                {
                    "project_id": path.parent.name,
                    "error": "Project receipt is unreadable; files retained.",
                }
            )
    return {"projects": rows, "running": busy(), "errors": errors}


def public_config():
    from ..agent.perception import MODEL
    from ..agent.provider import MODELS, PROFILE, VERTEX_MODEL

    controller_models = [VERTEX_MODEL] if PROFILE == "vertex" else list(MODELS)

    return {
        "max_file_bytes": config.MAX_FILE_BYTES,
        "max_duration_s": config.MAX_DURATION_S,
        "max_brief_chars": config.MAX_BRIEF_CHARS,
        "max_feedback_chars": config.MAX_FEEDBACK_CHARS,
        "max_controller_calls": config.MAX_CONTROLLER_CALLS,
        "audio_enabled": config.AUDIO_ENABLED,
        "controller_models": controller_models,
        "provider_profile": PROFILE,
        "audio_model": MODEL,
        "storage": config.STORAGE_BACKEND,
        "session_backend": config.SESSION_BACKEND,
        "memory_bank": config.MEMORY_BANK_ENABLED,
        "runtime_mode": config.RUNTIME_MODE,
        "inference_destination": "Vertex Gemini with explicit OpenRouter failover when configured; audio observation through its configured provider",
    }


def upload_files(form, directory, names):
    result = {}
    for field in names:
        item = form.get(field)
        if not isinstance(item, tuple) or len(item) != 2 or not item[0]:
            raise ValueError("Choose one file for each required input.")
        filename, data = item
        if not 0 < len(data) <= config.MAX_FILE_BYTES:
            raise RequestError(
                "Each file must be nonempty and within the configured limit.", 413
            )
        filename = Path(filename.replace("\\", "/")).name
        suffix = Path(filename).suffix.lower()
        allowed = {
            "video": (".mp4", ".mov", ".webm", ".mkv"),
            "sfx": (".wav", ".mp3", ".m4a", ".flac", ".ogg"),
            "audio": (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4"),
        }[field]
        if suffix not in allowed:
            raise ValueError("Unsupported media extension.")
        path = directory / (field + suffix)
        path.write_bytes(data)
        result[field] = (path, filename)
    return result


class Handler(LocalHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.local_request()
            self.get_route()
        except RequestError as error:
            self.send_json({"error": str(error)}, error.status)
        except FileNotFoundError:
            self.send_json({"error": "Project or artifact not found."}, 404)
        except (ValueError, KeyError, TypeError, IndexError):
            self.send_json(
                {"error": "Invalid project, candidate or media window."}, 400
            )
        except (OSError, subprocess.SubprocessError):
            self.send_json(
                {
                    "error": "Local media is unavailable. Check the project files and FFmpeg."
                },
                422,
            )

    def get_route(self):
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route in ("/", "/workspace"):
            self.send_file(STATIC, "index.html")
        elif route.startswith("/assets/"):
            self.send_file(STATIC, route.lstrip("/"))
        elif route == "/api/config":
            self.send_json(public_config())
        elif route == "/api/projects":
            self.send_json(project_list())
        elif route == "/api/observability":
            self.send_json(obs.status())
        elif route == "/api/takes":
            pid = parse_qs(parsed.query)["project_id"][0]
            projects.load(pid)
            self.send_json(
                {
                    "takes": takes.list_takes(pid),
                    "experiments": [
                        json.loads(p.read_text())
                        for p in sorted(
                            projects.project_dir(pid).glob("*-experiment.json")
                        )
                    ],
                }
            )
        elif route in ("/api/snippet", "/api/waveform"):
            self.audio_data(route, parse_qs(parsed.query))
        elif route.startswith("/projects/"):
            self.project_file(route.removeprefix("/projects/"))
        else:
            raise RequestError("Route not found.", 404)

    def project_file(self, relative):
        allowed = re.fullmatch(
            r"([a-f0-9]{16})/(video\.mp4|poster\.jpg|original\.wav|sfx\.wav|events\.jsonl|"
            r"[a-f0-9]{12}\.(?:mp4|wav|json)|[a-f0-9]{12}-turn\.json|takes/[a-f0-9]{12}\.wav)",
            relative,
        )
        if not allowed:
            raise RequestError("Artifact not found.", 404)
        pid, name = allowed.groups()
        if name == "poster.jpg":
            case = projects.load(pid)
            poster = projects.project_dir(pid) / name
            with LOCK:
                if not poster.exists():
                    frames = projects.frames(
                        case,
                        [min(0.5, case["seconds"] / 2)],
                        projects.project_dir(pid) / "frames",
                    )
                    poster.write_bytes(frames[0][1].read_bytes())
        self.send_file(projects.PROJECTS, relative)

    def audio_data(self, route, query):
        case = projects.load(query["project_id"][0])
        if route == "/api/snippet":
            source = media.read_audio(case["sfx_path"])
            start, end = float(query["start"][0]), float(query["end"][0])
            if (
                not np.isfinite([start, end]).all()
                or not 0 <= start < end <= len(source) / media.RATE
            ):
                raise ValueError("Invalid source window")
            samples = source[round(start * media.RATE) : round(end * media.RATE)]
            if not len(samples):
                raise ValueError("Empty source window")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setparams(
                    (1, 2, media.RATE, len(samples), "NONE", "not compressed")
                )
                wav.writeframes(
                    (np.clip(samples, -1, 1) * 32767).round().astype("<i2").tobytes()
                )
            self.send_bytes(output.getvalue(), "audio/wav")
            return
        role = query["role"][0]
        if role not in ("original", "sfx", "candidate"):
            raise ValueError("Invalid waveform role")
        if role == "candidate":
            cid = query["candidate_id"][0]
            review.candidate(case, cid)
            path = projects.project_dir(case["id"]) / (cid + ".wav")
        else:
            path = case["original_path" if role == "original" else "sfx_path"]
        samples = media.read_audio(path)
        buckets = np.array_split(np.abs(samples), min(600, len(samples)))
        self.send_json(
            {
                "duration_s": len(samples) / media.RATE,
                "sample_rate": media.RATE,
                "peaks": [round(float(chunk.max()), 5) for chunk in buckets],
            }
        )

    def do_POST(self):
        try:
            self.local_request(mutation=True)
            route = urlparse(self.path).path
            if route in ("/api/projects", "/api/takes"):
                if not UPLOAD_LOCK.acquire(blocking=False):
                    raise RequestError(
                        "Another upload is being prepared. Retry when it finishes.", 409
                    )
                try:
                    self.upload(route)
                finally:
                    UPLOAD_LOCK.release()
                return
            self.json_action(route)
        except RequestError as error:
            self.send_json({"error": str(error)}, error.status)
        except BlockingIOError:
            self.send_json(
                {"error": "An agent run or edit is active. Wait for it to finish."}, 409
            )
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            self.send_json(
                {
                    "error": "Invalid input. Check the selected project, values and file types."
                },
                400,
            )
        except FileNotFoundError:
            self.send_json({"error": "Project or artifact not found."}, 404)
        except RuntimeError:
            self.send_json(
                {"error": "Cloud worker is unavailable. Check deployment configuration."},
                503,
            )
        except (OSError, subprocess.SubprocessError):
            self.send_json(
                {
                    "error": "Media operation failed. Completed files remain available; check FFmpeg and disk space."
                },
                422,
            )

    def upload(self, route):
        take = route == "/api/takes"
        allowed = (
            {"project_id", "audio", "brief", "start_s", "clock"}
            if take
            else {"video", "sfx", "context", "style"}
        )
        form = self.read_form(
            config.MAX_FILE_BYTES * (1 if take else 2) + 20000, allowed
        )
        with tempfile.TemporaryDirectory(prefix="orpheus-upload-") as temporary:
            files = upload_files(
                form, Path(temporary), ["audio"] if take else ["video", "sfx"]
            )
            if take:
                with mutation():
                    result = takes.add_take(
                        form["project_id"],
                        files["audio"][0],
                        form.get("brief", ""),
                        float(form.get("start_s", "0")),
                        form.get("clock", "uploaded"),
                    )
            else:
                result = {
                    "project": projects.create(
                        files["video"][0],
                        files["sfx"][0],
                        form.get("context", ""),
                        form.get("style", ""),
                        files["video"][1],
                        files["sfx"][1],
                    )
                }
        self.send_json(result, 201)

    def json_action(self, route):
        if route not in (
            "/api/run",
            "/api/review",
            "/api/assist",
            "/api/takes/fit",
            "/api/grafana",
        ):
            raise RequestError("Route not found.", 404)
        data = self.read_json(100000 if route == "/api/assist" else 3000)
        if route == "/api/run":
            start(data["project_id"], data.get("feedback", DEFAULT_FEEDBACK))
            self.send_json({"started": True, "project_id": data["project_id"]}, 202)
        elif route == "/api/grafana":
            projects.load(data["project_id"])
            self.send_json(
                asyncio.run(
                    obs.investigate(
                        data["project_id"],
                        data.get("topic", "history"),
                        data.get("candidate_id", ""),
                    )
                )
            )
        elif route == "/api/takes/fit":
            with LOCK:
                with mutation():
                    doc = takes.fitting_project(data["project_id"], data["take_id"])
                start(
                    doc["id"],
                    "Query Grafana takes for the parent project. Compare recorded takes, propose one evidence-linked prop or performance experiment, then fit this selected take to picture and compare measurements through Grafana.",
                )
            self.send_json({"project": doc}, 201)
        else:
            with mutation():
                result = (
                    review.save_review(data)
                    if route == "/api/review"
                    else review.assist(data)
                )
            self.send_json(result, 201)


def main():
    parser = argparse.ArgumentParser(description="Run the local Orpheus workbench.")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    args = parser.parse_args()
    projects.ROOT.mkdir(parents=True, exist_ok=True)
    projects.PROJECTS.mkdir(parents=True, exist_ok=True)
    if not (STATIC / "index.html").is_file():
        parser.error(
            "Build the interface first: cd frontend && npm ci && npm run build"
        )
    server = ThreadingHTTPServer((config.SERVER_HOST, args.port), Handler)
    display_host = "127.0.0.1" if config.RUNTIME_MODE == "local" else config.SERVER_HOST
    print(f"Orpheus: http://{display_host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
