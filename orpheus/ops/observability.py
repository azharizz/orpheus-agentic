"""Local Grafana evidence plane. No raw prompts/media or credentials are exported."""

import asyncio
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager

import httpx
import numpy as np

from ..config import OBSERVABILITY_DIR as STORE

CONFIG = STORE / "local.json"
DB = STORE / "outbox.sqlite"
DASHBOARD_PATH = "/d/orpheus/agentic-foley-control-room"
EVIDENCE_CONTRACT_SCHEMA = "orpheus.part-evidence/1"
MAX_LENS_ROWS = 500
ENVELOPE_RESOLUTIONS = (1, 10, 60)
COVERAGE_PAGE = 100
LIVE_STALE_S = 120


def _dashboard_url(url):
    if url and not url.endswith(DASHBOARD_PATH):
        return url.split("/d/")[0] + DASHBOARD_PATH
    return url


def config():
    if os.environ.get("ORPHEUS_GRAFANA_ENABLED") == "0":
        return None
    stored = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    mcp_url = str(
        stored.get("mcp_url") or os.environ.get("ORPHEUS_GRAFANA_MCP_URL", "")
    ).strip().rstrip("/")
    mcp_token = str(
        stored.get("mcp_token") or os.environ.get("ORPHEUS_GRAFANA_MCP_TOKEN", "")
    ).strip()
    if not mcp_url or not mcp_token:
        return None
    telemetry_token = str(
        stored.get("telemetry_token")
        or os.environ.get("ORPHEUS_GRAFANA_TELEMETRY_TOKEN", "")
    ).strip()
    return {
        "mcp_url": mcp_url,
        "mcp_token": mcp_token,
        "dashboard_url": _dashboard_url(
            stored.get("dashboard_url")
            or os.environ.get("ORPHEUS_GRAFANA_DASHBOARD_URL", "").strip()
        ),
        "telemetry_token": telemetry_token,
        "loki_url": stored.get("loki_url")
        or os.environ.get("ORPHEUS_GRAFANA_LOKI_URL", "").strip().rstrip("/"),
        "loki_user": str(
            stored.get("loki_user")
            or os.environ.get("ORPHEUS_GRAFANA_LOKI_USER", "1777916")
        ).strip(),
        "otlp_url": stored.get("otlp_url")
        or os.environ.get("ORPHEUS_GRAFANA_OTLP_URL", "").strip().rstrip("/"),
        "otlp_user": str(
            stored.get("otlp_user")
            or os.environ.get("ORPHEUS_GRAFANA_OTLP_USER", "1820129")
        ).strip(),
        "loki_datasource_uid": str(
            stored.get("loki_datasource_uid")
            or os.environ.get("ORPHEUS_GRAFANA_LOKI_DATASOURCE_UID", "orpheus-loki")
        ).strip(),
        "prometheus_datasource_uid": str(
            stored.get("prometheus_datasource_uid")
            or os.environ.get(
                "ORPHEUS_GRAFANA_PROMETHEUS_DATASOURCE_UID", "orpheus-prometheus"
            )
        ).strip(),
    }


def part_context(project_id, family_id=None, part_start_s=None, part_end_s=None):
    """The one selected picture Part the agent and reviewer both argue about."""
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid telemetry project")
    if not finite(part_start_s) or not finite(part_end_s) or part_end_s <= part_start_s:
        raise ValueError("Part must span a positive picture range")
    context = {"part_start_s": part_start_s, "part_end_s": part_end_s}
    if family_id is not None:
        context["family_id"] = token(family_id)
    return context


def part_selector(project_id, part=None, candidate_id="", events=None):
    """Bounded LogQL for one Part. Time filtering stays label-side; ranges are numeric."""
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project")
    if candidate_id and not re.fullmatch("[a-f0-9]{12}", candidate_id):
        raise ValueError("Invalid candidate")
    stream = '{service_name="orpheus"'
    if events:
        stream += ',event=~"' + "|".join(token(e) for e in events) + '"'
    selector = stream + '} | json | project_id="' + project_id + '"'
    if candidate_id:
        selector += ' | candidate_id="' + candidate_id + '"'
    if part:
        start, end = part["part_start_s"], part["part_end_s"]
        if not finite(start) or not finite(end):
            raise ValueError("Part must span a numeric picture range")
        if part.get("family_id"):
            selector += ' | family_id="' + token(part["family_id"]) + '"'
        selector += f" | part_end_s >= {start} | part_start_s <= {end}"
    return selector


def part_lens(project_id, part, candidate_id="", limit=MAX_LENS_ROWS):
    """One selected Part, one shared read: what the agent claims and what was measured."""
    rows = read_events(project_id, newest_first=True)
    start, end = part["part_start_s"], part["part_end_s"]
    family = part.get("family_id")
    inside = []
    for row in rows:
        if candidate_id and row.get("candidate_id") != candidate_id:
            continue
        if family and row.get("family_id") not in (None, family):
            continue
        low = row.get("part_start_s", row.get("output_start_s", row.get("start_s")))
        high = row.get("part_end_s", row.get("output_end_s", row.get("end_s", low)))
        if not finite(low):
            continue
        if not finite(high):
            high = low
        if high >= start and low <= end:
            inside.append(row)
    inside.sort(key=lambda r: r.get("observed_at", 0), reverse=True)
    decisions = [r for r in inside if r["event"] in DECISION_EVENTS]
    measured = [r for r in inside if r["event"] in ("sound_event", "candidate_timing_measured")]
    errors = [r["timing_error_ms"] for r in measured if finite(r.get("timing_error_ms"))]
    return {
        "evidence_contract": EVIDENCE_CONTRACT_SCHEMA,
        "project_id": project_id,
        "part": {"part_start_s": start, "part_end_s": end, "family_id": family},
        "candidate_id": candidate_id,
        "selector": part_selector(project_id, part, candidate_id),
        "rows": inside[:limit],
        "row_count": len(inside),
        "truncated": len(inside) > limit,
        "decisions": ledger(decisions),
        "measured_events": len(measured),
        "max_abs_timing_error_ms": max((abs(v) for v in errors), default=None),
        "unresolved": unresolved(inside),
        "warning": "Measured rows describe the export. Absence of rows is not evidence of correctness.",
    }


DECISION_EVENTS = (
    "deterministic_baseline",
    "candidate",
    "selection",
    "human_review",
    "movie_candidate",
    "session_saved",
    "failed",
)


def ledger(rows):
    """Who decided what, on which evidence. Model and human provenance stay distinct."""
    entries = []
    for row in sorted(rows, key=lambda r: r.get("observed_at", 0)):
        entries.append(
            {
                "event": row["event"],
                "observed_at": row.get("observed_at"),
                "owner": "human" if row["event"] == "human_review" else "agent",
                "decision": row.get("verdict") or row.get("decision") or row.get("status"),
                "candidate_id": row.get("candidate_id"),
                "evidence_id": row.get("evidence_id"),
                "receipt_id": row.get("receipt_id"),
                "trace_id": row.get("trace_id"),
            }
        )
    return entries


def unresolved(rows):
    """Explicit unknowns beat a confident blank panel."""
    states = []
    if not rows:
        states.append("no_evidence_rows")
    if pending():
        states.append("pending_exports")
    if not any(r["event"] in DECISION_EVENTS for r in rows):
        states.append("no_decision_owner")
    if any(r.get("tool_error") or r["event"].endswith("failed") for r in rows):
        states.append("failures_present")
    return states


def read_events(project_id=None, newest_first=False):
    with connect() as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events")]
    if project_id:
        rows = [r for r in rows if r.get("project_id") == project_id]
    rows.sort(key=lambda r: r.get("observed_at", 0), reverse=newest_first)
    return rows


@contextmanager
def connect():
    STORE.mkdir(exist_ok=True)
    db = sqlite3.connect(DB, timeout=10)
    try:
        with db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT NOT NULL, trace TEXT, logs_sent INTEGER DEFAULT 0, trace_sent INTEGER DEFAULT 0)"
            )
            yield db
    finally:
        db.close()


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def numeric(values):
    return {
        k: v
        for k, v in (values or {}).items()
        if isinstance(k, str)
        and re.fullmatch(r"[a-zA-Z0-9_]+", k)
        and (finite(v) or isinstance(v, bool))
    }


def token(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_./:-]{1,120}", value)
        else "unknown"
    )


def envelope_levels(db, resolutions=ENVELOPE_RESOLUTIONS):
    """Long films need coarse overviews that still show transients, so keep min and max."""
    levels = {}
    for seconds in resolutions:
        bins = max(1, int(seconds * 100))
        if len(db) < bins:
            continue
        usable = len(db) - len(db) % bins
        block = db[:usable].reshape(-1, bins)
        levels[f"{seconds}s"] = [
            {
                "media_s": round(index * seconds, 3),
                "rms_dbfs": round(float(row.mean()), 2),
                "min_dbfs": round(float(row.min()), 2),
                "max_dbfs": round(float(row.max()), 2),
            }
            for index, row in enumerate(block)
        ]
    return levels


def chunk_index(rows, span_s=60):
    """Coarse map of where evidence lives, so a long film is seekable without a full scan."""
    chunks = {}
    for row in rows:
        start = row.get("part_start_s", row.get("output_start_s", row.get("start_s")))
        if not finite(start):
            continue
        key = int(start // span_s) * span_s
        chunk = chunks.setdefault(
            key, {"chunk_start_s": key, "chunk_end_s": key + span_s, "rows": 0, "events": {}}
        )
        chunk["rows"] += 1
        chunk["events"][row["event"]] = chunk["events"].get(row["event"], 0) + 1
    return [chunks[key] for key in sorted(chunks)]


def coverage_timeline(project_id, page=0, page_size=COVERAGE_PAGE, span_s=60):
    """Paginated coverage: which spans of picture have evidence and which are unexamined."""
    rows = read_events(project_id)
    chunks = chunk_index(rows, span_s)
    total = len(chunks)
    window = chunks[page * page_size : (page + 1) * page_size]
    for chunk in window:
        chunk["state"] = "covered" if chunk["rows"] else "unexamined"
    return {
        "project_id": project_id,
        "page": page,
        "page_size": page_size,
        "span_s": span_s,
        "total_chunks": total,
        "has_more": (page + 1) * page_size < total,
        "chunks": window,
        "warning": "Unexamined spans are unknown, not silent or correct.",
    }


def sound_profile(path):
    """Signal descriptors, not silence/noise/material/quality ground truth."""
    from ..domain import media

    a = media.read_audio(path)
    if not len(a) or not np.isfinite(a).all():
        raise ValueError("Invalid audio samples")
    env = media.envelope(a)
    db = 20 * np.log10(np.maximum(env, 1e-9))
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a * a)))
    # ponytail: fixed energy threshold is a descriptor, not speech/activity detection.
    active = db > -50
    spectrum = np.abs(np.fft.rfft(a[: min(len(a), 48000 * 30)])) ** 2
    freq = np.fft.rfftfreq(min(len(a), 48000 * 30), 1 / 48000)
    total = max(float(spectrum.sum()), 1e-20)
    return {
        "duration_s": len(a) / 48000,
        "rms_dbfs": media.db(rms),
        "sample_peak_dbfs": media.db(peak),
        "crest_db": media.db(peak) - media.db(rms),
        "body_dbfs": media.body_level(a),
        "floor_p10_dbfs": float(np.percentile(db, 10)),
        "p90_dbfs": float(np.percentile(db, 90)),
        "dynamic_p90_p10_db": float(np.percentile(db, 90) - np.percentile(db, 10)),
        "active_fraction_above_minus50": float(np.mean(active)),
        "near_full_scale_samples": int(np.count_nonzero(np.abs(a) >= 0.999)),
        "dc_offset": float(np.mean(a)),
        "spectral_centroid_hz": float(np.dot(freq, spectrum) / total),
        "low_band_fraction_below250": float(spectrum[freq < 250].sum() / total),
        "high_band_fraction_above4000": float(spectrum[freq > 4000].sum() / total),
        "envelope": [
            {"media_s": round(i / 100, 3), "rms_dbfs": round(float(db[i]), 2)}
            for i in range(0, len(db), max(1, math.ceil(len(db) / 120)))
        ],
        "envelope_levels": envelope_levels(db),
        "provenance": "signal_measurement",
        "warning": "Floor percentile includes intentional quiet; activity threshold is not semantic coverage. Mono PCM analysis, not acoustic calibration.",
    }


def enqueue(project_id, event, fields=None, turn_id="local", timestamp=None):
    if not config():
        return None
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid telemetry project")
    fields = fields or {}
    timestamp = time.time() if timestamp is None else timestamp
    if not finite(timestamp):
        raise ValueError("Invalid telemetry time")
    payload = {
        "project_id": project_id,
        "turn_id": token(turn_id),
        "event": token(event),
        "observed_at": timestamp,
        "trace_id": hashlib.sha256((project_id + turn_id).encode()).hexdigest()[:32],
        "evidence_contract": EVIDENCE_CONTRACT_SCHEMA,
    }
    for key in (
        "candidate_id",
        "family_id",
        "event_id",
        "mapping_id",
        "take_id",
        "parent_project_id",
        "requested_model",
        "served_model",
        "name",
        "status",
        "decision",
        "verdict",
        "role",
        "kind",
        "category",
        "error_type",
        "failure_code",
        "operation",
        "provenance",
        "topic",
        "receipt_id",
        "outcome",
    ):
        if key in fields:
            payload[key] = token(fields[key])
    if "id" in fields and re.fullmatch("[a-f0-9]{12}", str(fields["id"])):
        payload["candidate_id"] = fields["id"]
    for key in (
        "cycle",
        "elapsed_s",
        "status_code",
        "start_s",
        "end_s",
        "target_contact_s",
        "source_landmark_s",
        "gain_db",
        "clock_uncertainty_ms",
        "part_start_s",
        "part_end_s",
    ):
        if finite(fields.get(key)):
            payload[key] = fields[key]
    for key in ("metrics", "usage", "measurements"):
        if isinstance(fields.get(key), dict):
            payload.update(numeric(fields[key]))
    if isinstance(fields.get("profile"), dict):
        payload.update(numeric(fields["profile"]))
    response = fields.get("response")
    if isinstance(response, dict):
        payload["tool_error"] = bool(
            response.get("error")
            or response.get("status") in ("failed", "unavailable", "budget_exhausted")
        )
        if finite(response.get("status_code")):
            payload["status_code"] = response["status_code"]
    if event == "tool_result":
        payload["outcome"] = "failed" if payload.get("tool_error") else "completed"
    if isinstance(fields.get("timing"), dict):
        payload.update(numeric(fields["timing"]))
    coverage = fields.get("coverage")
    if isinstance(coverage, dict):
        payload["unmapped_hypotheses"] = len(coverage.get("unmapped_target_ids", []))
        payload["missing_output_rows"] = len(coverage.get("missing_output_ids", []))
    if "audio_sha256" in fields and re.fullmatch(
        "[a-f0-9]{64}", str(fields["audio_sha256"])
    ):
        payload["audio_sha256"] = fields["audio_sha256"]
    # Free-text descriptions, tool args/results, filenames, URLs and human notes never leave the app.
    for key in (
        "event_count",
        "time_s",
        "media_s",
        "rms_dbfs",
        "peak_dbfs",
        "body_dbfs",
        "crest_db",
        "timing_error_ms",
        "output_start_s",
        "output_end_s",
        "source_anchor_s",
        "target_anchor_s",
        "rendered_peak_s",
        "normalization_db",
        "decoded_mix_body_dbfs",
        "decoded_mix_peak_dbfs",
        "source_body_dbfs",
        "source_duration_s",
        "target_duration_s",
        "duration_mismatch_s",
        "trimmed_samples",
    ):
        if finite(fields.get(key)):
            payload[key] = fields[key]
    if event == "candidate":
        payload["event_count"] = len(fields.get("event_metrics", []))
    raw = json.dumps(payload, sort_keys=True, allow_nan=False)
    ident = hashlib.sha256(raw.encode()).hexdigest()
    payload["evidence_id"] = ident
    duration = (
        max(0, fields.get("elapsed_s", 0)) if finite(fields.get("elapsed_s", 0)) else 0
    )
    attrs = [
        {"key": k, "value": {"stringValue": str(v)}}
        for k, v in payload.items()
        if isinstance(v, (str, int, float, bool))
    ]
    span = {
        "traceId": payload["trace_id"],
        "spanId": ident[:16],
        "name": "orpheus." + token(event),
        "kind": 1,
        "startTimeUnixNano": str(int((timestamp - duration) * 1e9)),
        "endTimeUnixNano": str(int(timestamp * 1e9) + 1),
        "attributes": attrs,
        "status": {"code": 2 if "failed" in event or payload.get("tool_error") else 1},
    }
    root = hashlib.sha256((project_id + turn_id + ":root").encode()).hexdigest()[:16]
    if event == "turn_finished":
        span["spanId"] = root
    elif turn_id != "local":
        span["parentSpanId"] = root
    if event in ("model_response", "audio_response"):
        span["kind"] = 3
        for key, value in [
            ("gen_ai.operation.name", "chat"),
            ("gen_ai.request.model", payload.get("requested_model")),
            ("gen_ai.response.model", payload.get("served_model")),
            (
                "gen_ai.usage.input_tokens",
                payload.get("prompt_token_count", payload.get("prompt_tokens")),
            ),
            (
                "gen_ai.usage.output_tokens",
                payload.get("candidates_token_count", payload.get("completion_tokens")),
            ),
        ]:
            if value is not None:
                span["attributes"].append(
                    {
                        "key": key,
                        "value": {"intValue": str(value)}
                        if finite(value)
                        else {"stringValue": str(value)},
                    }
                )
    trace = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "orpheus"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "orpheus.evidence", "version": "1"},
                        "spans": [span],
                    }
                ],
            }
        ]
    }
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO events(id,payload,trace) VALUES (?,?,?)",
            (ident, json.dumps(payload), json.dumps(trace)),
        )
    return ident


def emit(project_id, event, fields=None, turn_id="local", timestamp=None):
    """Export receipts never take down editing; persistent outbox is retried by collector."""
    try:
        ident = enqueue(project_id, event, fields, turn_id, timestamp)
        if ident and event in ("sound_profile", "take_recorded"):
            for index, m in enumerate(
                (fields or {}).get("profile", {}).get("envelope", [])
            ):
                enqueue(
                    project_id,
                    "sound_envelope",
                    {
                        **m,
                        "role": (fields or {}).get("role", "take"),
                        "take_id": (fields or {}).get("take_id", "unknown"),
                    },
                    turn_id,
                    (timestamp or time.time()) + index * 0.000001,
                )
        if ident and event in ("candidate", "candidate_timing_measured"):
            fitted = {
                r.get("mapping_id"): r for r in (fields or {}).get("fitted_impacts", [])
            }
            for index, m in enumerate((fields or {}).get("event_metrics", [])):
                row = {
                    **numeric(m),
                    **numeric(fitted.get(m.get("mapping_id"), {})),
                    "candidate_id": fields.get(
                        "candidate_id", fields.get("id", "unknown")
                    ),
                    "mapping_id": m.get("mapping_id", str(index)),
                }
                bounds = m.get("output_range_s", [])
                if len(bounds) == 2:
                    row.update(output_start_s=bounds[0], output_end_s=bounds[1])
                enqueue(
                    project_id,
                    "sound_event",
                    row,
                    turn_id,
                    (timestamp or time.time()) + index * 0.000001,
                )
        return ident
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def flush(limit=500):
    cfg = config()
    if not cfg:
        return {"enabled": False}
    sent = 0
    errors = []
    # Short DB transactions; network outages must not lock the editor's outbox.
    with (STORE / "export.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"sent": 0, "busy": True, "pending": pending()}
        with connect() as db:
            rows = db.execute(
                "SELECT id,payload,trace,logs_sent,trace_sent FROM events WHERE logs_sent=0 OR trace_sent=0 ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        with httpx.Client(timeout=8, trust_env=False) as client:
            destinations = [
                (
                    "loki_url",
                    3,
                    "logs_sent",
                    "/loki/api/v1/push",
                    httpx.BasicAuth(cfg.get("loki_user", ""), cfg["telemetry_token"])
                    if cfg.get("telemetry_token")
                    else None,
                ),
                (
                    "otlp_url",
                    4,
                    "trace_sent",
                    "/otlp/v1/traces",
                    httpx.BasicAuth(cfg.get("otlp_user", ""), cfg["telemetry_token"])
                    if cfg.get("telemetry_token")
                    else None,
                ),
            ]
            if cfg.get("tempo_url") and not cfg.get("otlp_url"):
                destinations.append(("tempo_url", 4, "trace_sent", "/v1/traces", None))
            for backend, flag, column, endpoint, auth in destinations:
                if not cfg.get(backend):
                    continue
                batch = [r for r in rows if not r[flag]]
                if not batch:
                    continue
                try:
                    if column == "logs_sent":
                        streams = {}
                        now = time.time_ns()
                        for index, (_, raw, *_) in enumerate(batch):
                            event = json.loads(raw)["event"]
                            # Loki clock = ingestion; observed_at retains original experiment time.
                            streams.setdefault(event, []).append(
                                [str(now + index), raw]
                            )
                        body = {
                            "streams": [
                                {
                                    "stream": {"service_name": "orpheus", "event": k},
                                    "values": v,
                                }
                                for k, v in streams.items()
                            ]
                        }
                    else:
                        body = {
                            "resourceSpans": [
                                s
                                for r in batch
                                for s in json.loads(r[2])["resourceSpans"]
                            ]
                        }
                    client.post(cfg[backend] + endpoint, json=body, auth=auth).raise_for_status()
                    with connect() as db:
                        db.executemany(
                            "UPDATE events SET " + column + "=1 WHERE id=?",
                            [(r[0],) for r in batch],
                        )
                    sent += len(batch)
                except httpx.HTTPError as exc:
                    errors.append(
                        type(exc).__name__
                        + ":"
                        + str(
                            getattr(
                                getattr(exc, "response", None), "status_code", "network"
                            )
                        )
                    )
    return {"sent": sent, "errors": errors, "pending": pending()}


def pending():
    # Telemetry must never take down a request; an unreadable outbox is unknown, not zero.
    try:
        with connect() as db:
            return db.execute(
                "SELECT count(*) FROM events WHERE logs_sent=0 OR trace_sent=0"
            ).fetchone()[0]
    except (sqlite3.Error, OSError):
        return None


def metrics_text():
    # ponytail: scan the local experiment archive; pre-aggregate if scrape latency approaches 5s.
    rows = read_events()
    counts = {}
    failures = {}
    tokens = 0
    cost = 0
    latest = {}
    projects = {}
    for r in rows:
        counts[r["event"]] = counts.get(r["event"], 0) + 1
        project = projects.setdefault(
            r["project_id"],
            {"events": {}, "providers": {}, "tools": {}, "failures": 0, "tokens": 0, "cost": 0, "run_state": 0},
        )
        project["events"][r["event"]] = project["events"].get(r["event"], 0) + 1
        if r["event"] in ("model_failed", "audio_failed"):
            key = str(r.get("status_code", "unknown"))
            failures[key] = failures.get(key, 0) + 1
            project["failures"] += 1
        if r["event"] in ("model_response", "audio_response"):
            usage = r.get("total_token_count", r.get("total_tokens", 0))
            charge = r.get("cost", 0)
            tokens += usage
            cost += charge
            project["tokens"] += usage
            project["cost"] += charge
        if r["event"] in (
            "model_response", "audio_response", "model_failed", "audio_failed"
        ):
            provider = r.get("requested_model", "unknown")
            outcome = "failed" if r["event"].endswith("failed") else "served"
            key = (provider, outcome)
            project["providers"][key] = project["providers"].get(key, 0) + 1
        if r["event"] == "deterministic_baseline":
            project["baseline"] = r
            project["run_state"] = 0
            project["run_started_at"] = r["observed_at"]
            project.pop("run_finished_at", None)
        if r["event"] == "session_saved":
            project["run_state"] = 1
            project["run_finished_at"] = r["observed_at"]
        if r["event"] == "failed":
            project["run_state"] = -1
            project["run_finished_at"] = r["observed_at"]
        if r["event"] == "tool_result":
            key = (r.get("name", "unknown"), "failed" if r.get("tool_error") else "completed")
            project["tools"][key] = project["tools"].get(key, 0) + 1
        if r["event"] in ("candidate", "candidate_timing_measured"):
            latest = r
        if r["event"] == "candidate":
            project["candidate"] = r
        if r["event"] == "candidate_timing_measured":
            project["timing"] = r
        if r["event"] == "selection":
            project["selection"] = r
        if r["event"] == "human_review":
            project["review"] = r
        if r["event"] == "movie_analysis":
            project["movie_analysis"] = r
        if r["event"] in ("movie_progress", "render_progress"):
            if r.get("observed_at", 0) >= project.get("live", {}).get("observed_at", 0):
                project["live"] = r
        if r["event"] == "family_range" and r.get("mapping_id"):
            project.setdefault("ranges", {})[r["mapping_id"]] = r
        if r["event"] == "movie_candidate":
            project["movie_candidate"] = r
    lines = ["# TYPE orpheus_events_total counter"]
    lines += [f'orpheus_events_total{{event="{key}"}} {n}' for key, n in counts.items()]
    lines += ["# TYPE orpheus_provider_failures_total counter"] + [
        f'orpheus_provider_failures_total{{code="{key}"}} {n}'
        for key, n in failures.items()
    ]
    lines += [
        f"orpheus_export_pending {pending()}",
        f"orpheus_reported_tokens_total {tokens}",
        f"orpheus_reported_cost_usd_total {cost}",
    ]
    for key in (
        "integrated_lufs",
        "true_peak_dbtp",
        "clipped_samples",
        "event_count",
        "max_abs_peak_error_ms",
        "unmapped_hypotheses",
        "missing_output_rows",
    ):
        if finite(latest.get(key)):
            lines.append(f"orpheus_latest_candidate_{key} {latest[key]}")
    if latest.get("trace_id"):
        lines.append(
            f'orpheus_latest_candidate_trace{{trace_id="{latest["trace_id"]}",'
            f'candidate_id="{token(str(latest.get("candidate_id", "unknown")))}"}} 1'
        )
    selection_states = {"unsuitable": -1, "needs_human_review": 1}
    review_states = {"rejected": -1, "reject": -1, "approved": 1, "approve": 1}
    measured = (
        "integrated_lufs",
        "true_peak_dbtp",
        "clipped_samples",
        "event_count",
        "accepted_events",
        "max_abs_peak_error_ms",
        "picture_unchanged",
    )
    for project_id, project in projects.items():
        labels = f'project_id="{project_id}"'
        lines += [
            f"orpheus_project_candidate_state{{{labels}}} "
            + str(selection_states.get(project.get("selection", {}).get("decision"), 0)),
            f"orpheus_project_review_state{{{labels}}} "
            + str(review_states.get(project.get("review", {}).get("verdict"), 0)),
            f"orpheus_project_provider_failures_total{{{labels}}} {project['failures']}",
            f"orpheus_project_reported_tokens_total{{{labels}}} {project['tokens']}",
            f"orpheus_project_reported_cost_usd_total{{{labels}}} {project['cost']}",
            f"orpheus_project_run_state{{{labels}}} {project['run_state']}",
        ]
        analysis = project.get("movie_analysis", {})
        for key in ("progress", "events", "suggestions", "noise_regions", "duration_s"):
            value = analysis.get(key)
            if finite(value):
                lines.append(f"orpheus_movie_{key}{{{labels}}} {value}")
        buckets = {}
        for row in project.get("ranges", {}).values():
            key = token(str(row.get("status", "unknown")))
            buckets[key] = buckets.get(key, 0) + 1
        for key, count in buckets.items():
            lines.append(f'orpheus_family_ranges{{{labels},status="{key}"}} {count}')
        live = project.get("live", {})
        if live:
            age = time.time() - live.get("observed_at", 0)
            running = int(live.get("progress") != 100 and age < LIVE_STALE_S)
            phase = token(str(live.get("name", "unknown")))
            lines += [
                f'orpheus_live_running{{{labels},kind="{token(str(live["event"]))}",'
                f'phase="{phase}"}} {running}',
                f"orpheus_live_progress{{{labels}}} {live.get('progress', 0)}",
                f"orpheus_live_age_seconds{{{labels}}} {round(age, 1)}",
            ]
            for key in ("scanned_s", "duration_s", "buckets", "events", "noise_regions"):
                if finite(live.get(key)):
                    lines.append(f"orpheus_live_{key}{{{labels}}} {live[key]}")
        if finite(project.get("run_started_at")) and finite(project.get("run_finished_at")):
            lines.append(
                f"orpheus_project_run_duration_seconds{{{labels}}} "
                + str(project["run_finished_at"] - project["run_started_at"])
            )
        lines += [
            f'orpheus_project_events_total{{{labels},event="{event}"}} {count}'
            for event, count in project["events"].items()
        ]
        lines += [
            f'orpheus_project_provider_events_total{{{labels},model="{model}",outcome="{outcome}"}} {count}'
            for (model, outcome), count in project["providers"].items()
        ]
        lines += [
            f'orpheus_project_tool_events_total{{{labels},tool="{tool}",outcome="{outcome}"}} {count}'
            for (tool, outcome), count in project["tools"].items()
        ]
        for stage in ("baseline", "candidate"):
            row = project.get(stage, {})
            for key in measured:
                value = row.get(key, project.get("timing", {}).get(key))
                if isinstance(value, bool):
                    value = int(value)
                if finite(value):
                    lines.append(f"orpheus_{stage}_{key}{{{labels}}} {value}")
    return "\n".join(lines) + "\n"


def mcp_bearer(cfg):
    """Cloud Run guards the MCP service with IAM, so present an identity token there."""
    url = cfg.get("mcp_url", "")
    if ".run.app" not in url:
        return cfg["mcp_token"]
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        audience = url.split("/mcp")[0]
        return google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience
        )
    except Exception:
        return cfg["mcp_token"]


async def investigate(project_id, topic="history", candidate_id="", part=None):
    """All evidence reads go through the official Grafana MCP server, not direct Loki APIs."""
    cfg = config()
    if not cfg:
        return {
            "status": "disabled",
            "warning": "Local Grafana not configured; no MCP evidence available.",
        }
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project")
    if topic not in ("history", "failures", "sound", "takes", "runtime", "part"):
        raise ValueError("Unknown investigation topic")
    if candidate_id and not re.fullmatch("[a-f0-9]{12}", candidate_id):
        raise ValueError("Invalid candidate")
    if topic == "part" and not part:
        raise ValueError("Part topic requires a selected Part")
    try:
        # Evidence export must never abort the turn it is describing.
        await asyncio.to_thread(flush)
    except Exception:
        pass
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    selector = (
        '{service_name="orpheus",event!="sound_envelope"} | json | project_id="'
        + project_id
        + '"'
    )
    if topic == "takes":
        selector = (
            '{service_name="orpheus",event=~"take_recorded|take_experiment|human_review"} | json | parent_project_id="'
            + project_id
            + '"'
        )
    elif topic == "failures":
        selector += ' | event=~".*failed|tool_result|session_saved"'
    elif topic == "sound":
        selector += (
            ' | event=~"candidate|candidate_timing_measured|sound_event|sound_profile"'
        )
    elif topic == "part":
        selector = part_selector(
            project_id,
            part,
            candidate_id,
            events=DECISION_EVENTS + ("sound_event", "candidate_timing_measured"),
        )
    if candidate_id and topic != "part":
        selector += ' | candidate_id="' + candidate_id + '"'
    started = time.time()
    try:
        async with asyncio.timeout(45):
            async with streamablehttp_client(
                cfg["mcp_url"], headers={"Authorization": "Bearer " + mcp_bearer(cfg)}
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    catalog = await session.list_tools()
                    available = {t.name: t for t in catalog.tools}
                    name = (
                        "query_prometheus" if topic == "runtime" else "query_loki_logs"
                    )
                    if name not in available:
                        return {
                            "status": "failed",
                            "error": "MCP tool unavailable",
                            "tool": name,
                        }
                    arguments = (
                        {
                            "datasourceUid": cfg.get("prometheus_datasource_uid") or "orpheus-prometheus",
                            "expr": 'up{job="orpheus"} or orpheus_export_pending or orpheus_provider_failures_total or ALERTS',
                            "queryType": "instant",
                            "endTime": "now",
                        }
                        if topic == "runtime"
                        else {
                            "datasourceUid": cfg.get("loki_datasource_uid") or "orpheus-loki",
                            "logql": selector,
                            "limit": MAX_LENS_ROWS if topic == "part" else 100,
                            "format": "compact",
                            "startRfc3339": "now-24h" if topic == "part" else "now-14d",
                            "endRfc3339": "now",
                        }
                    )
                    result = await session.call_tool(name, arguments)
                    blocks = [c.text for c in result.content if hasattr(c, "text")]
                    response = "\n".join(blocks)
                    if result.isError:
                        return {
                            "status": "failed",
                            "error": "MCP query rejected",
                            "detail": response[:500],
                        }
                    decoded = json.loads(response)
                    evidence = []
                    if name == "query_loki_logs":
                        for stream in decoded.get("streams", []):
                            for entry in stream.get("lines", []):
                                evidence.append(json.loads(entry["line"]))
                        evidence.sort(
                            key=lambda r: r.get("observed_at", 0), reverse=True
                        )
                    else:
                        evidence = decoded.get("data", [])
                    compact = json.dumps(evidence, separators=(",", ":"))
                    # Bounded model context; retain full query receipt locally.
                    report = {
                        "status": "ok",
                        "topic": topic,
                        "project_id": project_id,
                        "candidate_id": candidate_id,
                        "tool": name,
                        "query": arguments,
                        "data": compact[:24000],
                        "evidence_count": len(evidence),
                        "truncated": len(compact) > 24000,
                        "queried_at": time.time(),
                        "pending_exports": pending(),
                        "warning": "Grafana measurements and model/human provenance are distinct. Empty results or pending exports are not success. Telemetry cannot establish perceptual truth.",
                    }
                    if topic == "part":
                        lens = part_lens(project_id, part, candidate_id)
                        report["part"] = lens["part"]
                        report["decisions"] = lens["decisions"]
                        report["max_abs_timing_error_ms"] = lens["max_abs_timing_error_ms"]
                        report["unresolved"] = lens["unresolved"]
                        report["gates"] = gates(project_id)["checks"]
                    report["receipt_id"] = hashlib.sha256(
                        json.dumps(report, sort_keys=True).encode()
                    ).hexdigest()[:20]
                    receipts = STORE / "receipts"
                    receipts.mkdir(exist_ok=True)
                    (receipts / (report["receipt_id"] + ".json")).write_text(
                        json.dumps({**report, "data": response})
                    )
                    emit(
                        project_id,
                        "grafana_query",
                        {
                            "name": name,
                            "status": "ok",
                            "elapsed_s": time.time() - started,
                        },
                    )
                    return report
    except Exception as exc:
        return {
            "status": "unavailable",
            "error_type": type(exc).__name__,
            "warning": "Grafana MCP unavailable; no evidence inferred, no automatic paid retry.",
        }


def status():
    cfg = config()
    return {
        "enabled": bool(cfg),
        "dashboard_url": cfg.get("dashboard_url") if cfg else None,
        "pending_exports": pending() if cfg else None,
        "mcp_url": cfg.get("mcp_url") if cfg else None,
    }


def gates(project_id=None):
    """Every gate reports pass/fail/unresolved. Unknown never reads as pass."""
    cfg = config()
    checks = {}
    if not cfg:
        checks["runtime"] = "unresolved"
        checks["mcp"] = "unresolved"
        checks["export"] = "unresolved"
    else:
        backlog = pending()
        checks["export"] = "pass" if backlog == 0 else "fail"
        checks["mcp"] = "pass" if cfg.get("mcp_url") and cfg.get("mcp_token") else "unresolved"
        checks["runtime"] = "pass" if CONFIG.exists() else "unresolved"
    if project_id:
        rows = read_events(project_id)
        checks["decision_owner"] = (
            "pass" if any(r["event"] in DECISION_EVENTS for r in rows) else "unresolved"
        )
        checks["failures"] = (
            "fail"
            if any(r.get("tool_error") or r["event"].endswith("failed") for r in rows)
            else "pass"
        )
    return {
        "evidence_contract": EVIDENCE_CONTRACT_SCHEMA,
        "checks": checks,
        "blocking": sorted(k for k, v in checks.items() if v != "pass"),
        "warning": "An unresolved gate is not a pass. Do not treat missing evidence as approval.",
    }


def snapshot(project_id, part=None, label=""):
    """Immutable evidence bundle: content-addressed so a report cannot drift from its rows."""
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project")
    rows = read_events(project_id)
    body = {
        "evidence_contract": EVIDENCE_CONTRACT_SCHEMA,
        "project_id": project_id,
        "label": token(label) if label else "",
        "part": part,
        "lens": {k: v for k, v in part_lens(project_id, part).items() if k != "rows"}
        if part
        else None,
        "gates": gates(project_id),
        "coverage": coverage_timeline(project_id),
        "decisions": ledger([r for r in rows if r["event"] in DECISION_EVENTS]),
        "row_count": len(rows),
        "rows": rows,
    }
    raw = json.dumps(body, sort_keys=True, allow_nan=False)
    body["snapshot_id"] = hashlib.sha256(raw.encode()).hexdigest()[:20]
    body["captured_at"] = time.time()
    folder = STORE / "snapshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (body["snapshot_id"] + ".json")
    if not path.exists():
        path.write_text(json.dumps(body, sort_keys=True))
        path.chmod(0o444)
    return {k: v for k, v in body.items() if k != "rows"} | {"path": str(path)}


def static_report(snapshot_id):
    """Static export: reads back an immutable snapshot, never a live query."""
    if not re.fullmatch("[a-f0-9]{20}", snapshot_id):
        raise ValueError("Invalid snapshot")
    path = STORE / "snapshots" / (snapshot_id + ".json")
    if not path.exists():
        return {"status": "missing", "snapshot_id": snapshot_id}
    body = json.loads(path.read_text())
    lens = body.get("lens") or {}
    return {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "captured_at": body.get("captured_at"),
        "project_id": body["project_id"],
        "part": body.get("part"),
        "gates": body["gates"],
        "decisions": body["decisions"],
        "row_count": body["row_count"],
        "max_abs_timing_error_ms": lens.get("max_abs_timing_error_ms"),
        "unresolved": lens.get("unresolved", body["gates"]["blocking"]),
    }
