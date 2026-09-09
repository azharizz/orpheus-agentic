"""Same-origin local API and the two Orpheus application routes."""

import argparse
import asyncio
import fcntl
import json
import re
import subprocess
import sys
import tempfile
import threading
import traceback
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import config
from ..domain import families, family_agent, media, movie, projects, review, takes
from ..ops import observability as obs
from . import auth, jobs, metadata, storage
from .http import LocalHandler, RequestError

LOCK = threading.RLock()
UPLOAD_LOCK = threading.Lock()
PROCESS = None
INDEX_THREAD = None
INDEX_QUEUE = []
PREP_THREAD = None
STATIC = config.ROOT / "frontend" / "dist"
DEFAULT_FEEDBACK = "Create a fitted alternative from these files."


def start_observability():
    if not obs.config():
        return
    from ..config import GRAFANA_PORTS
    from ..ops.grafana import MetricsHandler

    def serve_metrics():
        try:
            ThreadingHTTPServer(("127.0.0.1", GRAFANA_PORTS["METRICS"]), MetricsHandler).serve_forever()
        except OSError:
            pass

    def export():
        while True:
            try:
                obs.flush()
            except (OSError, ValueError):
                pass
            time.sleep(2)

    threading.Thread(target=serve_metrics, daemon=True, name="orpheus-metrics").start()
    threading.Thread(target=export, daemon=True, name="orpheus-observability").start()


def busy():
    projects.ROOT.mkdir(parents=True, exist_ok=True)
    if config.RUNTIME_MODE != "cloud_run":
        # A flock on a shared object mount is never released when a container dies.
        with (projects.ROOT / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    if config.RUNTIME_MODE == "cloud_run" and jobs.active():
        # Hosted turns run in a separate Cloud Run Job; local handles cannot see them.
        return True
    return (PROCESS is not None and PROCESS.poll() is None) or (
        INDEX_THREAD is not None and INDEX_THREAD.is_alive()
    ) or (PREP_THREAD is not None and PREP_THREAD.is_alive())


def start_index(project_id):
    global INDEX_THREAD
    with LOCK:
        if project_id not in INDEX_QUEUE:
            INDEX_QUEUE.append(project_id)
        if INDEX_THREAD is not None and INDEX_THREAD.is_alive():
            return True

        def run():
            global INDEX_THREAD
            while True:
                with LOCK:
                    if not INDEX_QUEUE:
                        INDEX_THREAD = None
                        return
                    pending = INDEX_QUEUE.pop(0)
                try:
                    families.build_index(pending)
                except Exception:
                    pass

        INDEX_THREAD = threading.Thread(target=run, daemon=True, name="orpheus-index")
        INDEX_THREAD.start()
        return True


def start_render(project_id, family_id, take_id, owner_id="local"):
    """A full-movie render cannot finish inside the hosted request window."""
    global PREP_THREAD
    with LOCK:
        if PREP_THREAD is not None and PREP_THREAD.is_alive():
            raise BlockingIOError()

        def run():
            global PREP_THREAD
            try:
                families.render(project_id, family_id, take_id)
                if metadata.enabled():
                    metadata.sync_project_now(projects.load(project_id), owner_id)
            except Exception:
                traceback.print_exc()
            finally:
                with LOCK:
                    PREP_THREAD = None

        PREP_THREAD = threading.Thread(target=run, daemon=True, name="orpheus-render")
        PREP_THREAD.start()


def start_prepare(project_id, owner_id="local"):
    global PREP_THREAD
    with LOCK:
        if PREP_THREAD is not None and PREP_THREAD.is_alive():
            raise BlockingIOError()

        def run():
            global PREP_THREAD
            try:
                prepared = projects.prepare(project_id)
                if metadata.enabled():
                    # The hosted list reads Cloud SQL; a finished project must land there.
                    metadata.sync_project_now(prepared or projects.load(project_id), owner_id)
                families.build_index(project_id)
            except Exception:
                # A silent worker thread is undebuggable in a hosted log.
                traceback.print_exc()
            finally:
                with LOCK:
                    PREP_THREAD = None

        PREP_THREAD = threading.Thread(target=run, daemon=True, name="orpheus-prepare")
        PREP_THREAD.start()


@contextmanager
def mutation():
    # ponytail: one local editing operation at a time; use per-project jobs for multiple users.
    with LOCK:
        if busy():
            raise BlockingIOError()
        if config.RUNTIME_MODE == "cloud_run":
            # The in-process LOCK is the guard here; a mount flock cannot be released.
            yield
            return
        with (projects.ROOT / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield


def start(project_id, family_id, feedback):
    global PROCESS
    family_agent.load_case(project_id, family_id)
    if (
        not isinstance(feedback, str)
        or not 1 <= len(feedback) <= config.MAX_FEEDBACK_CHARS
    ):
        raise ValueError("Feedback must be 1 to 500 characters.")
    with LOCK:
        if busy():
            raise BlockingIOError()
        if config.RUNTIME_MODE == "cloud_run":
            return jobs.dispatch(project_id, feedback, family_id=family_id)
        with (projects.project_dir(project_id) / "worker.log").open("ab") as output:
            PROCESS = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "orpheus.server.worker",
                    project_id,
                    "--family-id",
                    family_id,
                    "--feedback",
                    feedback,
                ],
                cwd=config.ROOT,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )


def project_list(owner_id="local"):
    if metadata.enabled():
        try:
            rows = asyncio.run(metadata.list_projects(owner_id))
        except metadata.MetadataError as exc:
            raise RequestError(str(exc), 503) from exc
        for doc in rows:
            doc["turn_details"] = []
            doc["assisted_candidates"] = []
            doc["human_reviews"] = []
        return {"projects": rows, "running": busy(), "errors": []}
    rows, errors = [], []
    for path in sorted(
        projects.PROJECTS.glob("*/project.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            doc = json.loads(path.read_text())
            if doc.get("schema") != "orpheus.v3":
                raise ValueError("Unsupported project schema")
            doc["turn_details"] = [
                json.loads((path.parent / (tid + "-turn.json")).read_text())
                for tid in doc["turns"]
                if (path.parent / (tid + "-turn.json")).exists()
            ]
            doc["human_reviews"] = [
                json.loads(p.read_text())
                for p in sorted(path.parent.glob("*-human.json"))
            ]
            doc["similarity_index"] = families.index_status(doc["id"])
            rows.append(doc)
        except (ValueError, KeyError, OSError):
            errors.append(
                {
                    "project_id": path.parent.name,
                    "error": "Project receipt is unreadable; files retained.",
                }
            )
    return {"projects": rows, "running": busy(), "errors": errors}


def assert_owner(handler, project_id):
    if not re.fullmatch(r"[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project ID")
    if metadata.enabled():
        try:
            owned = asyncio.run(metadata.owns(project_id, handler.owner_id))
        except metadata.MetadataError as exc:
            raise RequestError(str(exc), 503) from exc
        if not owned:
            raise FileNotFoundError(project_id)
        return
    if config.RUNTIME_MODE != "cloud_run":
        return
    case = projects.load(project_id)
    if case.get("owner_id") not in (None, handler.owner_id):
        raise FileNotFoundError(project_id)


def public_config():
    from ..agent.perception import MODEL
    from ..agent.provider import FAILOVER, MODELS, PROFILE, VERTEX_MODELS, provider_config

    try:
        provider_config()
        provider_ready = True
    except ValueError:
        provider_ready = False

    return {
        "max_file_bytes": config.MAX_FILE_BYTES,
        "max_duration_s": config.MAX_DURATION_S,
        "max_audio_bytes": config.AUDIO_UPLOAD_LIMIT_BYTES,
        "max_take_duration_s": config.MAX_TAKE_DURATION_S,
        "free_disk_margin_bytes": config.FREE_DISK_MARGIN_BYTES,
        "max_brief_chars": config.MAX_BRIEF_CHARS,
        "max_feedback_chars": config.MAX_FEEDBACK_CHARS,
        "max_controller_calls": config.MAX_CONTROLLER_CALLS,
        "audio_enabled": config.AUDIO_ENABLED,
        "controller_models": (list(VERTEX_MODELS) + (list(MODELS) if FAILOVER else [])) if PROFILE == "vertex" else list(MODELS),
        "provider_ready": provider_ready,
        "audio_model": MODEL if config.AUDIO_ENABLED else None,
        "storage": config.STORAGE_BACKEND,
        "runtime_mode": config.RUNTIME_MODE,
        "provider_profile": PROFILE,
        "session_backend": config.SESSION_BACKEND,
        "memory_bank": config.MEMORY_BANK_ENABLED,
        "inference_destination": (
            "Google Gemini through Vertex AI only"
            if PROFILE == "vertex" and not FAILOVER
            else "Vertex Gemini first, then OpenRouter failover"
            if PROFILE == "vertex"
            else "Configured controller providers; audio observation through OpenRouter when enabled"
        ),
    }


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
        elif route in ("/favicon.svg", "/favicon.ico", "/favicon-32.png", "/apple-touch-icon.png"):
            self.send_file(STATIC, route.lstrip("/"))
        elif route == "/api/config":
            self.send_json(public_config())
        elif route == "/api/projects":
            self.send_json(project_list(self.owner_id))
        elif route == "/api/observability":
            pid = parse_qs(parsed.query).get("project_id", [None])[0]
            self.send_json(obs.status() | {"gates": obs.gates(pid)})
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
        elif route == "/api/families":
            pid = parse_qs(parsed.query)["project_id"][0]
            projects.load(pid)
            self.send_json(
                {
                    "families": families.list_families(pid),
                    "index": families.index_status(pid),
                }
            )
        elif route == "/api/movie":
            pid = parse_qs(parsed.query)["project_id"][0]
            self.send_json(movie.status(pid))
        elif route == "/api/waveform":
            self.audio_data(route, parse_qs(parsed.query))
        elif route.startswith("/embed/"):
            self.embed_panel(route.removeprefix("/embed/"))
        elif route.startswith("/projects/"):
            self.project_file(route.removeprefix("/projects/"))
        else:
            raise RequestError("Route not found.", 404)

    def embed_panel(self, slug):
        """Serve one dashboard panel standalone so Grafana Cloud can iframe it."""
        from ..ops.grafana import PANEL_SLUGS, panel_html

        if slug not in PANEL_SLUGS:
            raise RequestError("Panel not found.", 404)
        origin = config.PUBLIC_ORIGIN or f"http://127.0.0.1:{config.SERVER_PORT}"
        markup = panel_html(slug, origin, config.SERVER_PORT)
        # Grafana interpolates ${var} in panel content but not inside a framed
        # document, so resolve the placeholders from the iframe query instead.
        query = parse_qs(urlparse(self.path).query)
        for name in ("project", "part_start", "part_end"):
            value = query.get(name, [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{0,64}", value):
                raise RequestError("Panel parameter is not valid.", 400)
            markup = markup.replace("${" + name + "}", value)
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<style>html,body{margin:0;height:100%;background:#111217;"
            "color:#C7CBD1;font:400 12px/1.4 system-ui}</style>"
            + markup
        )
        self.send_bytes(body.encode(), "text/html; charset=utf-8")

    def project_file(self, relative):
        allowed = re.fullmatch(
            r"([a-f0-9]{16})/(video\.mp4|poster\.jpg|original\.wav|events\.jsonl|"
            r"[a-f0-9]{12}\.(?:mp4|wav|json)|[a-f0-9]{12}-master\.(?:mp4|mkv)|"
            r"[a-f0-9]{12}-turn\.json|takes/[a-f0-9]{12}\.wav|"
            r"previews/[a-f0-9]{12}(?:-original|-mix)?\.(?:mp4|wav))",
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
        if config.STORAGE_BACKEND == "gcs" and name != "poster.jpg":
            # Firebase Hosting cannot proxy large media; hand the browser a signed URL.
            self.send_response(302)
            self.send_header("Location", storage.download_url(pid, name)["url"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_file(projects.PROJECTS, relative)

    def audio_data(self, route, query):
        case = projects.load(query["project_id"][0])
        role = query["role"][0]
        if role not in ("original", "candidate"):
            raise ValueError("Invalid waveform role")
        if role == "candidate":
            cid = query["candidate_id"][0]
            candidate = review.candidate(case, cid, allow_audition=True)
            path = projects.project_dir(case["id"]) / (cid + ".wav")
        else:
            path = case["original_path"]
        start_s = float(query.get("start_s", [0])[0])
        end_s = query.get("end_s", [None])[0]
        offset = 0
        if role == "candidate":
            offset = float(candidate.get("timeline_offset_s", 0))
            start_s = max(0, start_s - offset)
            duration = float(candidate.get("preview_duration_s", 0))
            end_s = duration if end_s is None else min(duration, max(0, float(end_s) - offset))
            if not 0 <= start_s < end_s:
                raise ValueError("Waveform range does not overlap this candidate")
        bins = int(query.get("bins", [600])[0])
        result = media.waveform(path, bins, start_s, None if end_s is None else float(end_s))
        if role == "candidate":
            for key in ("start_s", "end_s"):
                if key in result:
                    result[key] += offset
        self.send_json(result)

    def do_POST(self):
        try:
            self.local_request(mutation=True)
            route = urlparse(self.path).path
            if route == "/api/media/staging-url":
                data = self.read_json(3000)
                self.send_json(
                    storage.staging_upload_url(
                        data["object"],
                        data.get("content_type", "application/octet-stream"),
                    )
                )
                return
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
            # The reply stays generic; the operator log needs the real cause.
            traceback.print_exc()
            self.send_json(
                {
                    "error": "Invalid input. Check the selected project, values and file types."
                },
                400,
            )
        except FileNotFoundError:
            self.send_json({"error": "Project or artifact not found."}, 404)
        except (OSError, subprocess.SubprocessError):
            self.send_json(
                {
                    "error": "Media operation failed. Completed files remain available; check FFmpeg and disk space."
                },
                422,
            )

    def do_DELETE(self):
        try:
            self.local_request(mutation=True)
            route = urlparse(self.path).path
            match = re.fullmatch(r"/api/projects/([a-f0-9]{16})", route)
            if not match:
                raise RequestError("Route not found.", 404)
            project_id = match.group(1)
            assert_owner(self, project_id)
            with mutation():
                try:
                    projects.remove(project_id)
                except FileNotFoundError:
                    # The record can outlive its media; the row must still go.
                    pass
                if metadata.enabled():
                    try:
                        metadata.delete_project_now(project_id, self.owner_id)
                    except metadata.MetadataError as exc:
                        raise RequestError(str(exc), 503) from exc
            self.send_json({"deleted": project_id})
        except RequestError as error:
            self.send_json({"error": str(error)}, error.status)
        except BlockingIOError:
            self.send_json(
                {"error": "An agent run or edit is active. Wait for it to finish."}, 409
            )
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            self.send_json({"error": "Invalid project ID."}, 400)
        except FileNotFoundError:
            self.send_json({"error": "Project or artifact not found."}, 404)
        except OSError:
            self.send_json({"error": "Project could not be deleted. Check local files."}, 422)

    def upload(self, route):
        take = route == "/api/takes"
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)

        def value(name, default=""):
            values = query.get(name, [default])
            if len(values) != 1:
                raise ValueError("Repeated upload field")
            return values[0]

        filename = Path(value("filename").replace("\\", "/")).name
        suffix = Path(filename).suffix.lower()
        allowed = (
            (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm", ".mp4")
            if take
            else (".mp4", ".mov", ".webm", ".mkv")
        )
        if suffix not in allowed:
            raise ValueError("Unsupported media extension")
        staged = value("staged")
        with tempfile.TemporaryDirectory(prefix="orpheus-upload-") as temporary:
            path = Path(temporary) / (("audio" if take else "video") + suffix)
            if staged:
                # Large media never crosses the API; the browser PUTs it to GCS first.
                storage.fetch_staged(staged, path)
            else:
                self.read_file(
                    config.AUDIO_UPLOAD_LIMIT_BYTES if take else config.VIDEO_UPLOAD_LIMIT_BYTES,
                    path,
                )
            if take:
                family_id = value("family_id")
                if not family_id:
                    raise ValueError("Replacement take requires a sound family")
                with mutation():
                    result = takes.add_take(
                        value("project_id"),
                        path,
                        value("brief"),
                        float(value("start_s", "0")),
                        value("clock", "uploaded"),
                        family_id,
                    )
            else:
                project = projects.intake(path, context=value("context"), style=value("style"), video_name=filename)
                if config.RUNTIME_MODE == "cloud_run":
                    # The worker and the hosted list must agree on who owns this project.
                    project["owner_id"] = self.owner_id
                    projects.atomic(
                        projects.project_dir(project["id"]) / "project.json", project
                    )
                # Hosted requests must return well inside the 60s edge timeout.
                if config.RUNTIME_MODE == "cloud_run" or project["seconds"] >= 300:
                    start_prepare(project["id"], self.owner_id)
                else:
                    project = projects.prepare(project["id"])
                    start_index(project["id"])
                if metadata.enabled():
                    try:
                        metadata.sync_project_now(project, self.owner_id)
                    except metadata.MetadataError as exc:
                        raise RequestError(str(exc), 503) from exc
                result = {"project": project}
        self.send_json(result, 201)

    def json_action(self, route):
        if route not in (
            "/api/run",
            "/api/review",
            "/api/families",
            "/api/families/search",
            "/api/families/examples",
            "/api/families/review",
            "/api/families/preview",
            "/api/families/render",
            "/api/movie/analyze",
            "/api/movie/review",
            "/api/movie/render",
            "/api/grafana",
            "/api/grafana/part",
            "/api/grafana/coverage",
            "/api/grafana/snapshot",
        ):
            raise RequestError("Route not found.", 404)
        data = self.read_json(100000 if route == "/api/families/review" else 3000)
        if route == "/api/run":
            from ..agent.provider import provider_config

            family_id = data.get("family_id")
            if not family_id:
                raise RequestError(
                    "Choose a sound family and replacement take before running fitting.",
                    409,
                )
            if data.get("consent") is not True:
                raise RequestError("Confirm the paid fitting run before starting it.", 409)
            if not obs.config():
                raise RequestError(
                    "Start the local Grafana stack before agent fitting. The agent requires Grafana MCP evidence.",
                    409,
                )
            try:
                provider_config()
            except ValueError as exc:
                raise RequestError(str(exc), 409) from exc
            try:
                family_agent.load_case(data["project_id"], family_id)
            except (ValueError, FileNotFoundError) as exc:
                raise RequestError(str(exc), 409) from exc
            start(
                data["project_id"],
                family_id,
                data.get("feedback", DEFAULT_FEEDBACK),
            )
            self.send_json(
                {
                    "started": True,
                    "project_id": data["project_id"],
                    "family_id": family_id,
                },
                202,
            )
        elif route == "/api/movie/analyze":
            with mutation():
                result = movie.analyze(data["project_id"], resume=data.get("resume", True))
            self.send_json(result, 201)
        elif route == "/api/movie/review":
            with mutation():
                result = movie.review(data["project_id"], data["item_id"], data["decision"])
            self.send_json(result, 201)
        elif route == "/api/movie/render":
            with mutation():
                result = movie.render_draft(data["project_id"])
            self.send_json(result, 201)
        elif route == "/api/grafana":
            projects.load(data["project_id"])
            part = data.get("part")
            self.send_json(
                asyncio.run(
                    obs.investigate(
                        data["project_id"],
                        data.get("topic", "history"),
                        data.get("candidate_id", ""),
                        obs.part_context(
                            data["project_id"],
                            part.get("family_id"),
                            part.get("part_start_s"),
                            part.get("part_end_s"),
                        )
                        if part
                        else None,
                    )
                )
            )
        elif route == "/api/grafana/part":
            projects.load(data["project_id"])
            part = data["part"]
            self.send_json(
                obs.part_lens(
                    data["project_id"],
                    obs.part_context(
                        data["project_id"],
                        part.get("family_id"),
                        part.get("part_start_s"),
                        part.get("part_end_s"),
                    ),
                    data.get("candidate_id", ""),
                )
            )
        elif route == "/api/grafana/coverage":
            projects.load(data["project_id"])
            self.send_json(
                obs.coverage_timeline(
                    data["project_id"],
                    page=int(data.get("page", 0)),
                    span_s=float(data.get("span_s", 60)),
                )
            )
        elif route == "/api/grafana/snapshot":
            projects.load(data["project_id"])
            part = data.get("part")
            self.send_json(
                obs.snapshot(
                    data["project_id"],
                    obs.part_context(
                        data["project_id"],
                        part.get("family_id"),
                        part.get("part_start_s"),
                        part.get("part_end_s"),
                    )
                    if part
                    else None,
                    data.get("label", ""),
                ),
                201,
            )
        elif route == "/api/families":
            with mutation():
                result = families.create(
                    data["project_id"], data["name"], data["seed_range_s"],
                    defer=data.get("defer") is True,
                )
            self.send_json(result, 201)
        elif route == "/api/families/search":
            with mutation():
                result = families.search(data["project_id"], data["family_id"])
            self.send_json(result, 201)
        elif route == "/api/families/examples":
            with mutation():
                result = families.add_example(
                    data["project_id"], data["family_id"], data["range_s"]
                )
            self.send_json(result, 201)
        elif route == "/api/families/review":
            decisions = data.get("decisions")
            if (
                not isinstance(decisions, list)
                or any(
                    not isinstance(row, dict)
                    or set(row) != {"match_id", "decision"}
                    or row["decision"] not in ("accepted", "rejected")
                    for row in decisions
                )
                or len({row["match_id"] for row in decisions}) != len(decisions)
            ):
                raise ValueError("Invalid match decision batch")
            accepted = [
                row["match_id"]
                for row in decisions
                if row.get("decision") == "accepted"
            ]
            rejected = [
                row["match_id"]
                for row in decisions
                if row.get("decision") == "rejected"
            ]
            with mutation():
                result = families.review(
                    data["project_id"], data["family_id"], accepted, rejected
                )
            self.send_json(result, 201)
        elif route == "/api/families/preview":
            with mutation():
                result = families.preview_match(
                    data["project_id"], data["family_id"], data["match_id"]
                )
            self.send_json(result, 201)
        elif route == "/api/families/render":
            with mutation():
                family = families.get(data["project_id"], data["family_id"])
                if family.get("scope") == "part":
                    result = family_agent.render_baseline(
                        data["project_id"], data["family_id"], persist=True
                    )
                elif config.RUNTIME_MODE == "cloud_run":
                    start_render(
                        data["project_id"], data["family_id"], data.get("take_id"), self.owner_id
                    )
                    result = {"status": "rendering", "family_id": data["family_id"]}
                else:
                    result = families.render(
                        data["project_id"], data["family_id"], data.get("take_id")
                    )
            self.send_json(result, 201)
        elif route == "/api/review":
            with mutation():
                result = review.save_review(data)
            self.send_json(result, 201)


def main():
    parser = argparse.ArgumentParser(description="Run the local Orpheus workbench.")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    args = parser.parse_args()
    projects.ROOT.mkdir(parents=True, exist_ok=True)
    projects.PROJECTS.mkdir(parents=True, exist_ok=True)
    if metadata.enabled():
        asyncio.run(metadata.initialize())
    if not (STATIC / "index.html").is_file():
        parser.error(
            "Build the interface first: cd frontend && npm ci && npm run build"
        )
    server = ThreadingHTTPServer((config.SERVER_HOST, args.port), Handler)
    start_observability()
    print(f"Orpheus: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
