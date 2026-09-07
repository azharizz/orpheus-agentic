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

import httpx
import numpy as np

from ..config import OBSERVABILITY_DIR as STORE

CONFIG = STORE / "local.json"
DB = STORE / "outbox.sqlite"


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
        "dashboard_url": stored.get("dashboard_url")
        or os.environ.get("ORPHEUS_GRAFANA_DASHBOARD_URL", "").strip(),
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


def connect():
    STORE.mkdir(exist_ok=True)
    db = sqlite3.connect(DB, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT NOT NULL, trace TEXT, logs_sent INTEGER DEFAULT 0, trace_sent INTEGER DEFAULT 0)"
    )
    return db


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
    }
    for key in (
        "candidate_id",
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
        "category",
        "error_type",
        "failure_code",
        "operation",
        "provenance",
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
        "media_s",
        "rms_dbfs",
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
            if cfg.get("otlp_url") and cfg.get("telemetry_token"):
                try:
                    body = _metrics_otlp()
                    client.post(
                        cfg["otlp_url"] + "/otlp/v1/metrics",
                        json=body,
                        auth=httpx.BasicAuth(
                            cfg.get("otlp_user", "1820129"), cfg["telemetry_token"]
                        ),
                    ).raise_for_status()
                    sent += len(body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"])
                except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                    errors.append(
                        type(exc).__name__
                        + ":"
                        + str(getattr(getattr(exc, "response", None), "status_code", "network"))
                    )
    return {"sent": sent, "errors": errors, "pending": pending()}


def pending():
    with connect() as db:
        return db.execute(
            "SELECT count(*) FROM events WHERE logs_sent=0 OR trace_sent=0"
        ).fetchone()[0]


def _metrics_otlp():
    metrics = []
    timestamp = str(int(time.time() * 1e9))
    for line in metrics_text().splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+)', line)
        if not match:
            continue
        name, labels, value = match.groups()
        attributes = []
        for label in (labels or "").split(","):
            if not label:
                continue
            key, raw = label.split("=", 1)
            attributes.append({"key": key, "value": {"stringValue": raw.strip('"')}})
        metrics.append(
            {
                "name": name,
                "gauge": {
                    "dataPoints": [
                        {
                            "timeUnixNano": timestamp,
                            "asDouble": float(value),
                            "attributes": attributes,
                        }
                    ]
                },
            }
        )
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "orpheus"}}
                    ]
                },
                "scopeMetrics": [{"scope": {"name": "orpheus.metrics"}, "metrics": metrics}],
            }
        ]
    }


def metrics_text():
    # ponytail: scan the local experiment archive; pre-aggregate if scrape latency approaches 5s.
    with connect() as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events")]
    counts = {}
    failures = {}
    tokens = 0
    cost = 0
    latest = {}
    for r in rows:
        counts[r["event"]] = counts.get(r["event"], 0) + 1
        if r["event"] in ("model_failed", "audio_failed"):
            key = str(r.get("status_code", "unknown"))
            failures[key] = failures.get(key, 0) + 1
        if r["event"] in ("model_response", "audio_response"):
            tokens += r.get("total_token_count", r.get("total_tokens", 0))
            cost += r.get("cost", 0)
        if r["event"] in ("candidate", "candidate_timing_measured"):
            latest = r
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
    return "\n".join(lines) + "\n"


async def investigate(project_id, topic="history", candidate_id=""):
    """All evidence reads go through the official Grafana MCP server, not direct Loki APIs."""
    cfg = config()
    if not cfg:
        return {
            "status": "disabled",
            "warning": "Local Grafana not configured; no MCP evidence available.",
        }
    if not re.fullmatch("[a-f0-9]{16}", project_id):
        raise ValueError("Invalid project")
    if topic not in ("history", "failures", "sound", "takes", "runtime"):
        raise ValueError("Unknown investigation topic")
    if candidate_id and not re.fullmatch("[a-f0-9]{12}", candidate_id):
        raise ValueError("Invalid candidate")
    await asyncio.to_thread(flush)
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": "Bearer " + cfg["mcp_token"]}
    if os.environ.get("ORPHEUS_RUNTIME_MODE") == "cloud_run":
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.id_token import fetch_id_token

            audience = cfg["mcp_url"].split("/mcp", 1)[0]
            headers["X-Serverless-Authorization"] = "Bearer " + fetch_id_token(
                Request(), audience
            )
        except Exception as exc:
            raise RuntimeError("Private Grafana MCP identity unavailable") from exc

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
    if candidate_id:
        selector += ' | candidate_id="' + candidate_id + '"'
    started = time.time()
    try:
        async with asyncio.timeout(45):
            async with streamablehttp_client(
                cfg["mcp_url"], headers=headers
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
                            "datasourceUid": cfg["prometheus_datasource_uid"],
                            "expr": 'up{job="orpheus"} or orpheus_export_pending or orpheus_provider_failures_total or ALERTS',
                            "queryType": "instant",
                            "endTime": "now",
                        }
                        if topic == "runtime"
                        else {
                            "datasourceUid": cfg["loki_datasource_uid"],
                            "logql": selector,
                            "limit": 100,
                            "format": "compact",
                            "startRfc3339": "now-14d",
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
