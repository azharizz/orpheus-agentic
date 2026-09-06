"""Bounded, independent audio evidence. Model labels never become verified facts."""

import asyncio
import base64
import hashlib
import io
import json
import math
import re
import subprocess
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from ..config import AUDIO_CALL_LIMIT, AUDIO_ENABLED, AUDIO_MAX_TOKENS, VALUES
from ..config import AUDIO_MODEL as MODEL
from ..domain import media
from ..domain.projects import atomic
from .provider import provider_config

MAX_INVENTORY_EVENTS = 100
PROMPT = (Path(__file__).with_name("prompts") / "audio.md").read_text(encoding="utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def failure_code(exc):
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "audio_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "provider_rejected"
    if isinstance(exc, json.JSONDecodeError):
        return "malformed_json"
    # Only internally authored messages map to public codes; never return exception text.
    return {
        "Audio modality unavailable": "audio_unavailable",
        "Wrong media identity": "wrong_media",
        "Invalid finite interval": "invalid_timing_or_duration",
        "Stale evidence": "stale_evidence",
        "Unconfirmed requested audio model": "wrong_or_missing_model",
        "Incomplete audio response": "incomplete_response",
        "Model cannot invent calibrated loudness measurements": "fabricated_measurement",
    }.get(str(exc), "invalid_evidence_or_transport")


def finite(v, lo, hi):
    if (
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(v)
        or not lo <= v <= hi
    ):
        raise ValueError("Invalid finite interval")
    return v


def window(case, role, start, end):
    if role not in ("target", "source", "candidate"):
        raise ValueError("Role must be target, source or candidate")
    path = (
        case["candidate_path"]
        if role == "candidate"
        else case["original_path"]
        if role == "target"
        else case["sfx_path"]
    )
    raw = path.read_bytes()
    if role == "candidate":
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-vn",
                "-t",
                "30",
                "-f",
                "f32le",
                "-ac",
                "1",
                "-ar",
                str(media.RATE),
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=180,
        ).stdout
        a = np.frombuffer(decoded, dtype="<f4").astype(float)
        if not np.all(np.isfinite(a)):
            raise ValueError("Nonfinite decoded audio")
    else:
        a = media.read_audio(path)
    duration = min(30, len(a) / media.RATE, case["seconds"] if role == "target" else 30)
    finite(start, 0, duration)
    finite(end, start + 0.02, float("inf"))
    requested_end = end
    # Media duration is sample-clock truth; callers commonly pass the rounded
    # video duration (for example 8.400s for an 8.383s decoded WAV).
    end = min(end, duration)
    finite(end, start + 0.02, duration)
    lo, hi = round(start * media.RATE), round(end * media.RATE)
    a = a[lo:hi]
    stream = io.BytesIO()
    with wave.open(stream, "wb") as w:
        w.setparams((1, 2, media.RATE, len(a), "NONE", "not compressed"))
        w.writeframes(
            np.clip(np.round(a * 32768), -32768, 32767).astype("<i2").tobytes()
        )
    data = stream.getvalue()
    receipt = {
        "role": role,
        "file_sha256": digest(raw),
        "window_sha256": digest(data),
        "start_s": lo / media.RATE,
        "end_s": hi / media.RATE,
        "duration_s": len(a) / media.RATE,
        "file_duration_s": duration,
        "format": "wav",
        "sample_rate": media.RATE,
        "channels": 1,
        "sample_width_bytes": 2,
        "bytes": len(data),
        "clock": "local audio seconds; global=start_s+local",
        "synthetic_missing_audio": role == "target"
        and case.get("has_original_audio") is False,
    }
    if requested_end > end:
        receipt["requested_end_s"] = requested_end
        receipt["end_clamped_to_audio_s"] = end
    receipt["media_id"] = (
        role + "-" + digest(json.dumps(receipt, sort_keys=True).encode())[:20]
    )
    return receipt, data, a


def gaps(intervals, start, end):
    cursor = start
    out = []
    for lo, hi in sorted(intervals):
        if lo > cursor:
            out.append([cursor, lo])
        cursor = max(cursor, hi)
    if cursor < end:
        out.append([cursor, end])
    return out


def validate_inventory(doc, r):
    if not isinstance(doc, dict) or set(doc) != {
        "media_id",
        "duration_s",
        "audio_access",
        "events",
    }:
        raise ValueError("Invalid inventory schema")
    if doc["media_id"] != r["media_id"]:
        raise ValueError("Wrong media identity")
    finite(doc["duration_s"], r["duration_s"] - 0.001, r["duration_s"] + 0.001)
    if doc["audio_access"] != "available":
        raise ValueError("Audio modality unavailable")
    rows = doc["events"]
    if not isinstance(rows, list) or len(rows) > MAX_INVENTORY_EVENTS:
        raise ValueError("Inventory row cap exceeded")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "kind",
            "label",
            "onset_range_s",
            "offset_range_s",
            "evidence",
        }:
            raise ValueError("Invalid event schema")
        if row["kind"] not in (
            "impulse",
            "texture",
            "composite",
            "transition",
            "pause",
            "unknown",
        ):
            raise ValueError("Unknown event kind")
        for key, limit in [("label", 160), ("evidence", 500)]:
            if not isinstance(row[key], str) or not 1 <= len(row[key]) <= limit:
                raise ValueError("Invalid description")
            if re.search(
                r"[-+]?\d+(?:\.\d+)?\s*(?:LUFS|dBFS|dBTP)\b", row[key], re.IGNORECASE
            ):
                raise ValueError("Model cannot invent calibrated loudness measurements")
        for key in ("onset_range_s", "offset_range_s"):
            v = row[key]
            if not isinstance(v, list) or len(v) != 2:
                raise ValueError("Expected interval pair")
            finite(v[0], 0, r["duration_s"])
            finite(v[1], v[0], r["duration_s"])
        if (
            row["offset_range_s"][0] < row["onset_range_s"][0]
            or row["offset_range_s"][1] < row["onset_range_s"][1]
        ):
            raise ValueError("Offset precedes onset")
        ident = digest((r["media_id"] + json.dumps(row, sort_keys=True)).encode())[:20]
        event = {
            **row,
            "id": r["role"] + "-" + ident,
            "media_id": r["media_id"],
            "status": "hypothesis",
            "provenance": "audio_model",
            "local_onset_range_s": row["onset_range_s"],
            "local_offset_range_s": row["offset_range_s"],
        }
        for key in ("onset_range_s", "offset_range_s"):
            event[key] = [round(t + r["start_s"], 6) for t in row[key]]
        result.append(event)
    if len({e["id"] for e in result}) != len(result):
        raise ValueError("Duplicate events")
    return {
        "media_id": r["media_id"],
        "role": r["role"],
        "window_s": [r["start_s"], r["end_s"]],
        "events": sorted(result, key=lambda e: e["onset_range_s"][0]),
        "unresolved_ranges_s": gaps(
            [
                (e["onset_range_s"][0], e["offset_range_s"][1])
                for e in result
                if e["kind"] != "unknown"
            ],
            r["start_s"],
            r["end_s"],
        ),
        "status": "hypotheses_only",
        "cap_reached": len(rows) == MAX_INVENTORY_EVENTS,
    }


def measurements(a, r):
    hop = media.RATE // 100
    levels = [
        media.db(np.sqrt(np.mean(a[i : i + hop] ** 2))) for i in range(0, len(a), hop)
    ]
    return {
        "media_id": r["media_id"],
        "window_s": [r["start_s"], r["end_s"]],
        "rms_dbfs": media.db(np.sqrt(np.mean(a * a))),
        "sample_peak_dbfs": media.db(np.max(np.abs(a))),
        "bin_s": 0.01,
        "rms_envelope_dbfs": [round(v, 2) for v in levels],
        "digital_silence": bool(np.all(a == 0)),
        "warning": "Measured dBFS, not integrated LUFS, semantic labels or visual synchronization.",
    }


def reconcile(evidence, reviews, previous):
    """Compare provenance-bearing evidence without promoting semantic certainty."""
    r = evidence["receipt"]
    m = evidence["measurements"]
    rows = []
    impacts = [e for e in evidence["inventory"]["events"] if e["kind"] == "impulse"]
    spacing = np.diff([sum(e["onset_range_s"]) / 2 for e in impacts])
    regular = len(impacts) >= 4 and float(np.std(spacing)) < 0.001
    for event in evidence["inventory"]["events"]:
        conflicts = []
        support = []
        start, end = event["onset_range_s"]
        if regular and event["kind"] == "impulse":
            conflicts.append(
                "Highly regular proposed schedule needs localized validation; periodic machines can be real, regularity is not proof of error"
            )
        if m["digital_silence"] and event["kind"] not in ("pause", "unknown"):
            conflicts.append("Audio action proposed in measured digital silence")
        lo = max(0, round((start - r["start_s"] - 0.1) / 0.01))
        hi = min(
            len(m["rms_envelope_dbfs"]), round((end - r["start_s"] + 0.1) / 0.01) + 1
        )
        bins = m["rms_envelope_dbfs"][lo:hi]
        change = max(bins) - min(bins) if bins else 0
        if event["kind"] in ("impulse", "transition"):
            if change >= 6:
                support.append(
                    "Local RMS variation >=6dB; supports acoustic change only"
                )
            else:
                conflicts.append(
                    "No >=6dB local RMS variation; boundary needs review, quiet sounds may be valid"
                )
        matched = [v for v in reviews if v["event_id"] == event["id"]]
        for v in matched:
            modality = (
                "Source review"
                if v.get("provenance") == "controller_audio_hypothesis"
                else "Visual"
            )
            if v["onset_range_s"][1] < start or v["onset_range_s"][0] > end:
                conflicts.append(modality + " and acoustic onset ranges disagree")
            else:
                support.append(
                    modality + " hypothesis overlaps acoustic onset uncertainty"
                )
            if v["kind"] != event["kind"]:
                conflicts.append(modality + " and acoustic event kinds disagree")
        old_ids = []
        for old in previous:
            if (
                old["receipt"]["role"] != r["role"]
                or old["receipt"]["file_sha256"] != r["file_sha256"]
            ):
                continue
            for other in old["inventory"]["events"]:
                if other["id"] == event["id"]:
                    continue
                # Overlap suggests a possible revision, not established event correspondence.
                if max(other["onset_range_s"][0], start) <= min(
                    other["offset_range_s"][1], event["offset_range_s"][1]
                ):
                    old_ids.append(other["id"])
                    if other["kind"] != event["kind"]:
                        conflicts.append(
                            "Overlapping prior hypothesis differs; correspondence unresolved"
                        )
        rows.append(
            {
                "event_id": event["id"],
                "status": "unresolved" if conflicts else "hypothesis",
                "conflicts": list(dict.fromkeys(conflicts)),
                "support": support,
                "visual_review_ids": [
                    v["id"]
                    for v in matched
                    if v.get("provenance") != "controller_audio_hypothesis"
                ],
                "audio_review_ids": [
                    v["id"]
                    for v in matched
                    if v.get("provenance") == "controller_audio_hypothesis"
                ],
                "prior_overlap_ids": old_ids,
                "verified": False,
                "suggested_review_window_s": [
                    max(0, start - 0.3),
                    min(r["file_duration_s"], end + 0.3),
                ]
                if conflicts
                else None,
            }
        )
    return {
        "evidence_id": evidence["evidence_id"],
        "media_id": r["media_id"],
        "events": rows,
        "status": "unresolved"
        if any(v["conflicts"] for v in rows)
        else "hypotheses_only",
        "warning": "Signal support is not semantic proof. Model agreement is not independent truth. No automatic timeline shifts.",
    }


class Perception:
    def __init__(self, case, folder, log):
        self.case = case
        self.folder = folder
        self.log = log
        self.calls = 0
        values = VALUES
        self.enabled = AUDIO_ENABLED
        self.model = values.get("ORPHEUS_AUDIO_MODEL", MODEL)
        if self.model != MODEL:
            raise ValueError("Only the approved Gemini audio model is enabled")

    def availability(self):
        return {
            "enabled": self.enabled,
            "requested_model": self.model,
            "network_calls": self.calls,
            "network_call_limit": 6,
            "window_limit_s": 30,
            "output_token_ceiling": AUDIO_MAX_TOKENS,
            "timeout_s": 180,
            "warning": "Separate audio model, not controller hearing. Hypotheses only; no quality approval.",
        }

    async def request(self, r, data):
        base, key = provider_config()
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": AUDIO_MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "provider": {"require_parameters": True},
            "messages": [
                {"role": "system", "content": PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(r)},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(data).decode(),
                                "format": "wav",
                            },
                        },
                    ],
                },
            ],
        }
        # No redirects, automatic retries or model fallbacks; raw media stays out of logs.
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            async with client.stream(
                "POST",
                base + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            ) as response:
                response.raise_for_status()
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ValueError("Provider response too large")
                    chunks.append(chunk)
        return json.loads(b"".join(chunks))

    async def inspect(self, role, start, end, case=None):
        if not self.enabled:
            return {
                "status": "disabled",
                "warning": "Local-only signal tools remain; no audio model inference.",
            }
        r = None
        started = time.monotonic()
        try:
            r, data, a = window(self.case if case is None else case, role, start, end)
            if r["synthetic_missing_audio"]:
                return {
                    "status": "unavailable",
                    "receipt": r,
                    "warning": "Target has no original audio; use visual hypotheses.",
                }
            cache_key = digest(
                json.dumps(
                    {"receipt": r, "model": self.model, "prompt": PROMPT, "version": 1},
                    sort_keys=True,
                ).encode()
            )
            directory = self.folder / "perception"
            directory.mkdir(exist_ok=True)
            path = directory / (cache_key + ".json")
            if path.exists():
                saved = json.loads(path.read_text())
                if saved["receipt"] != r or saved["requested_model"] != self.model:
                    raise ValueError("Stale evidence")
                saved["inventory"] = validate_inventory(saved["raw_observation"], r)
                saved["cache_hit"] = True
                self.log(
                    "audio_cache_hit",
                    evidence_id=cache_key,
                    receipt=r,
                    requested_model=self.model,
                )
                return saved
            if self.calls >= AUDIO_CALL_LIMIT:
                return {
                    "status": "budget_exhausted",
                    "warning": "Keep uninspected regions unresolved; no automatic retry.",
                }
            self.calls += 1
            self.log(
                "audio_request",
                receipt=r,
                requested_model=self.model,
                attempt=self.calls,
            )
            async with asyncio.timeout(180):
                response = await self.request(r, data)
            # Log only allowlisted routing/usage, even when model content is malformed.
            served = response.get("model")
            provider = response.get("provider")
            usage = response.get("usage") or {}
            safe_usage = {
                k: v
                for k, v in usage.items()
                if k in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
                and isinstance(v, (int, float))
                and math.isfinite(v)
            }
            details = usage.get("prompt_tokens_details") or {}
            if isinstance(details.get("audio_tokens"), (int, float)):
                safe_usage["audio_tokens"] = details["audio_tokens"]
            self.log(
                "audio_response",
                requested_model=self.model,
                served_model=served,
                provider=provider,
                usage=safe_usage,
                elapsed_s=round(time.monotonic() - started, 3),
                receipt=r,
            )
            if not isinstance(served, str) or served.removeprefix(
                "google/"
            ) != self.model.removeprefix("google/"):
                raise ValueError("Unconfirmed requested audio model")
            choice = response["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete audio response")
            content = choice["message"]["content"]
            if not isinstance(content, str) or len(content) > 100000:
                raise ValueError("Invalid response content")
            raw = json.loads(content)
            inventory = validate_inventory(raw, r)
            result = {
                "status": "ok",
                "evidence_id": cache_key,
                "receipt": r,
                "requested_model": self.model,
                "served_model": served,
                "provider": provider,
                "usage": safe_usage,
                "cache_hit": False,
                "prompt_sha256": digest(PROMPT.encode()),
                "raw_observation": raw,
                "inventory": inventory,
                "measurements": measurements(a, r),
                "warning": "Untrusted acoustic hypotheses; actual audio access/tokens do not prove labels or timing.",
            }
            atomic(path, result)
            self.log("audio_inventory", evidence_id=cache_key, inventory=inventory)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "failure_code": failure_code(exc),
                "status_code": getattr(
                    getattr(exc, "response", None), "status_code", None
                ),
                "receipt": r,
                "warning": "No usable audio evidence; do not infer successful understanding.",
            }
            self.log("audio_failed", **result)
            return result
