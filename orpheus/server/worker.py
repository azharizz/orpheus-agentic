"""Persistent ADK project session with bounded, separately logged feedback turns."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import subprocess
import time
import traceback
import uuid

import numpy as np
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.memory.vertex_ai_memory_bank_service import (
    VertexAiMemoryBankService,
)
from google.adk.sessions import DatabaseSessionService, VertexAiSessionService
from google.genai import types

from ..agent.workflow import build
from ..agent.workflow_common import PROMPT_DIR, frame_parts, runtime_prompt
from .. import config
from ..config import (
    DATA_DIR as ROOT,
    MAX_CONTROLLER_CALLS,
    PACKAGE_DIR,
    TURN_TIMEOUT_SECONDS,
)
from ..domain import families, family_agent
from ..domain.projects import atomic, digest as file_digest, frames, load, project_dir
from ..ops import observability as obs

APP = "orpheus"


def progress_for_event(event, fields, turn):
    """Persist the current agent step without inventing a completion percentage."""
    prior = turn.get("progress", {})
    phase, label = prior.get("phase", "preparing"), prior.get("label", "Preparing the confirmed Part")
    labels = {
        "deterministic_baseline": ("baseline", "Measuring the deterministic baseline"),
        "session_loaded": ("context", "Preparing the agent context"),
        "grafana_startup": ("grafana", "Reading Grafana history"),
        "loop_cycle": ("fitting", f"Agent fitting cycle {fields.get('cycle', 0)}"),
        "candidate": ("candidate", "Measuring the proposed replacement"),
        "selection": ("selection", "Preparing the selected candidate"),
        "session_saved": ("complete", "Candidate ready for human review" if turn.get("selection") else "Agent finished without a selected candidate"),
        "failed": ("failed", "Agent run stopped before a reviewable candidate"),
    }
    if event == "tool_call":
        name = str(fields.get("name", "")).lower()
        if "grafana" in name:
            phase, label = "grafana", "Reading Grafana evidence"
        elif "render" in name or "arrange" in name:
            phase, label = "fitting", "Fitting the replacement performance"
        elif "measure" in name or "timing" in name:
            phase, label = "measurement", "Measuring timing and loudness"
        elif "frame" in name or "inspect" in name or "audio" in name:
            phase, label = "inspection", "Inspecting the confirmed Part"
    elif event in labels:
        phase, label = labels[event]
    return {
        "status": turn.get("status", "running"),
        "phase": phase,
        "label": label,
        "updated_at": time.time(),
        "cycle": turn.get("cycles", 0),
        "candidate_count": len(turn.get("candidates", [])),
    }


def failure_info(exc, phase, provider_exhausted=False):
    """Safe categories only: exception text may contain credentials/media payloads."""
    if isinstance(exc, LlmCallsLimitExceededError):
        category = "model_call_budget"
    elif isinstance(exc, subprocess.TimeoutExpired):
        category = "media_timeout"
    elif isinstance(exc, TimeoutError):
        category = "turn_timeout"
    elif isinstance(exc, subprocess.SubprocessError):
        category = "media_processing"
    elif provider_exhausted and isinstance(exc, RuntimeError):
        category = "provider_failure"
    elif phase == "input_validation":
        category = "input_validation"
    else:
        category = "unexpected_failure"
    return {
        "category": category,
        "phase": phase,
        "error_type": type(exc).__name__,
        "guidance": "No quality approval. Review recorded tool/provider events before retrying; retries may use credit.",
    }


def session_service():
    if config.SESSION_BACKEND == "agent_engine":
        return VertexAiSessionService(
            project=config.GOOGLE_CLOUD_PROJECT,
            location=config.GOOGLE_CLOUD_LOCATION,
            agent_engine_id=config.AGENT_ENGINE_ID,
        )
    return DatabaseSessionService(db_url=session_fallback_url())


def session_fallback_url():
    if config.RUNTIME_MODE == "cloud_run" and config.METADATA_DATABASE_URL:
        return config.METADATA_DATABASE_URL
    return "sqlite+aiosqlite:///" + str(ROOT / "sessions.sqlite")


def memory_service():
    if not config.MEMORY_BANK_ENABLED:
        return None
    return VertexAiMemoryBankService(
        project=config.GOOGLE_CLOUD_PROJECT,
        location=config.GOOGLE_CLOUD_LOCATION,
        agent_engine_id=config.AGENT_ENGINE_ID,
    )


def active():
    with (ROOT / "worker.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


async def repair_interrupted_tools(service, session):
    """Record an explicit failure for calls interrupted before a tool response was saved."""
    pending = {}
    for event in session.events:
        for part in event.content.parts if event.content else []:
            if part.function_call:
                pending[part.function_call.id] = part.function_call
            if part.function_response:
                pending.pop(part.function_response.id, None)
    if pending:
        await service.append_event(
            session,
            Event(
                author="OrpheusEditor",
                content=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=call.id,
                                name=call.name,
                                response={
                                    "error": "Previous invocation interrupted; no successful result recorded. Reinspect if needed."
                                },
                            )
                        )
                        for call in pending.values()
                    ],
                ),
            ),
        )
    return len(pending)


async def execute(pid, family_id, feedback="Create a fitted alternative from these files."):
    with (ROOT / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another paid run is active")
        recover_orphaned_turns(pid)
        return await run_turn(pid, family_id, feedback)


def recover_orphaned_turns(pid):
    """Caller must own worker lock: no active worker can own these running turns."""
    folder = project_dir(pid)
    doc = load(pid)
    changed = False
    for tid in doc["turns"]:
        path = folder / (tid + "-turn.json")
        turn = (
            json.loads(path.read_text())
            if path.exists()
            else {"id": tid, "status": "running"}
        )
        if turn.get("status") != "running":
            continue
        turn.update(
            status="interrupted",
            finished_at=time.time(),
            failure={
                "category": "interrupted_worker",
                "phase": "recovery",
                "guidance": "Previous worker exited without a final receipt. No quality approval; reinspect before retry.",
            },
        )
        atomic(path, turn)
        changed = True
    if changed or doc["status"] == "running":
        manifest = json.loads((folder / "project.json").read_text())
        manifest["status"] = "interrupted"
        atomic(folder / "project.json", manifest)


async def run_turn(pid, family_id, feedback):
    if not isinstance(feedback, str) or not 1 <= len(feedback) <= 500:
        raise ValueError("Feedback is 1..500 characters")
    case = family_agent.load_case(pid, family_id)
    folder = project_dir(pid)
    doc = json.loads((folder / "project.json").read_text())
    tid = uuid.uuid4().hex[:12]
    turn = {
        "id": tid,
        "status": "running",
        "started_at": time.time(),
        "feedback": feedback,
        "family_id": family_id,
        "models_used": [],
        "cycles": 0,
        "candidates": [],
        "selection": None,
        "progress": {
            "status": "running",
            "phase": "preparing",
            "label": "Preparing the confirmed Part",
            "updated_at": time.time(),
            "cycle": 0,
            "candidate_count": 0,
        },
        "workflow_version": "orpheus",
        "max_controller_calls": MAX_CONTROLLER_CALLS,
        "code_hashes": {
            str(path.relative_to(PACKAGE_DIR)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(PACKAGE_DIR.rglob("*.py"))
        },
        "prompt_hashes": {
            str(path.relative_to(PACKAGE_DIR)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(PROMPT_DIR.glob("*.md"))
        },
    }
    doc["status"] = "running"
    doc["turns"].append(tid)
    atomic(folder / "project.json", doc)
    atomic(folder / (tid + "-turn.json"), turn)
    provider_failed = False

    def log(event, **fields):
        nonlocal provider_failed
        if event == "candidate":
            fields = {
                **fields,
                "family_id": family_id,
                "agent_layer_only": True,
            }
            atomic(folder / (fields["id"] + ".json"), fields)
        if event == "model_failed":
            provider_failed = True
        if event == "model_response":
            provider_failed = False
        row = {"ts": time.time(), "turn_id": tid, "event": event, **fields}
        with (folder / "events.jsonl").open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        obs.emit(pid, event, fields, tid, row["ts"])
        if event == "loop_cycle":
            turn["cycles"] = fields["cycle"]
        if event == "candidate":
            turn["candidates"].append(fields)
        if event == "selection":
            turn["selection"] = fields
        if event == "arrangement_saved":
            turn["arrangement"] = fields["arrangement"]
        if event == "model_response":
            model = (
                fields.get("served_model") or "unconfirmed:" + fields["requested_model"]
            )
            if model not in turn["models_used"]:
                turn["models_used"].append(model)
        if event == "audio_response":
            turn["audio_models_used"] = list(
                dict.fromkeys(
                    [
                        *turn.get("audio_models_used", []),
                        fields.get("served_model") or "unconfirmed",
                    ]
                )
            )
            turn["audio_calls"] = [*turn.get("audio_calls", []), fields]
        if event == "audio_reconciliation":
            turn["audio_evidence_ids"] = list(
                dict.fromkeys(
                    [*turn.get("audio_evidence_ids", []), fields["evidence_id"]]
                )
            )
        turn["progress"] = progress_for_event(event, fields, turn)
        atomic(folder / (tid + "-turn.json"), turn)
        print(
            json.dumps(
                {
                    k: v
                    for k, v in row.items()
                    if k
                    in ["event", "cycle", "served_model", "error_type", "status_code"]
                }
            ),
            flush=True,
        )

    service = None
    runner = None
    phase = "session_initialization"
    try:
        service = session_service()
        phase = "input_validation"
        for name, expected_digest in doc["prepared_hashes"].items():
            if file_digest(folder / name) != expected_digest:
                raise ValueError("Prepared input changed")
        if not obs.config():
            raise RuntimeError("Grafana MCP is required for agent coordination")
        phase = "deterministic_baseline"
        baseline = family_agent.render_baseline(pid, family_id)
        turn["deterministic_baseline"] = {
            key: baseline[key]
            for key in ("id", "audio_sha256", "render_mode", "metrics", "mix")
        }
        log(
            "deterministic_baseline",
            candidate_id=baseline["id"],
            metrics=baseline["metrics"],
            measurements={"accepted_events": len(case["accepted_ranges"])},
        )
        family = families.get(pid, family_id)
        for status, key in (
            ("accepted", "accepted_ranges"),
            ("rejected", "rejected_ranges"),
        ):
            for index, item in enumerate(family[key], 1):
                start, end = item["range_s"]
                log(
                    "family_range",
                    mapping_id=f"{status}-{index}",
                    status=status,
                    start_s=start,
                    end_s=end,
                    measurements={"range_duration_s": end - start},
                )
        phase = "session"
        session_key = pid + "-" + family_id + "-part"
        # Agent Engine assigns its own session ids, so keep our stable key mapped to one.
        managed = config.SESSION_BACKEND == "agent_engine"
        session_id = doc.get("session_ids", {}).get(session_key) if managed else session_key
        session = None
        if session_id:
            try:
                session = await service.get_session(
                    app_name=APP, user_id="local", session_id=session_id
                )
            except Exception:
                session = None
        prior_events = len(session.events) if session else 0
        if session is None:
            session = await service.create_session(
                app_name=APP,
                user_id="local",
                state={"candidates": [], "notes": []},
                **({} if managed else {"session_id": session_key}),
            )
            if managed:
                doc.setdefault("session_ids", {})[session_key] = session.id
                atomic(folder / "project.json", doc)
        session_id = session.id
        repaired = await repair_interrupted_tools(service, session)
        if repaired:
            log("interrupted_tools_recovered", count=repaired)
        human_reviews = [
            json.loads(p.read_text())
            for p in sorted(
                folder.glob("*-human.json"), key=lambda p: p.stat().st_mtime
            )
        ][-20:]
        # Human UI receipts are a separate trust domain; model notes never create them.
        await service.append_event(
            session,
            Event(
                author="OrpheusEditor",
                actions=EventActions(
                    state_delta={
                        "human_reviews": human_reviews,
                        "user_preferences": {
                            "context": case["context"],
                            "style": case["style"],
                            "feedback": feedback,
                        },
                    }
                ),
            ),
        )
        log(
            "session_loaded",
            session_id=session_id,
            prior_event_count=prior_events,
            prior_candidate_count=len(session.state.get("candidates", [])),
            notes=session.state.get("notes", []),
        )
        await service.append_event(
            session,
            Event(
                author="OrpheusEditor",
                actions=EventActions(
                    state_delta={
                        "cycle": 0,
                        "controller_calls": 0,
                        "tool_failures": {},
                        "rendered": False,
                        "turn_renders": 0,
                        "reviewed": [],
                        "delivered_centers": [],
                        "pending_verdicts": [],
                        "adaptive_frames": 0,
                        "signal_calls": 0,
                        "delivered_frame_receipts": [],
                        "audio_attempts": {},
                        "empty_arrangement_attempts": 0,
                        "inspected": False,
                        "selection": None,
                        "grafana_receipts": {},
                        "candidate_measurements": {},
                        "deterministic_baseline": turn["deterministic_baseline"],
                    }
                ),
            ),
        )
        for role, path in [
            ("target", case["original_path"]),
            ("source", case["sfx_path"]),
        ]:
            obs.emit(
                pid,
                "sound_profile",
                {"role": role, "profile": obs.sound_profile(path)},
                tid,
            )
        if obs.config():
            history = await obs.investigate(pid, "history")
            log("grafana_startup", status=history["status"])
            await service.append_event(
                session,
                Event(
                    author="OrpheusEditor",
                    actions=EventActions(state_delta={"grafana_startup": history}),
                ),
            )
        agent = build(case, folder, log)
        runner = Runner(app_name=APP, agent=agent, session_service=service)
        times = sorted(
            set(
                float(t)
                for t in np.linspace(
                    0, max(0, case["seconds"] - 0.06), min(12, max(2, int(case["seconds"])))
                )
            )
        )
        phase = "overview"
        overview = frames(case, times, folder / "frames")
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=runtime_prompt(
                        "user_request",
                        feedback=(
                            feedback
                            + " Work only inside the confirmed sound-family ranges: "
                            + json.dumps(case["accepted_ranges"])
                            + f". This is a {case['seconds']:.3f}s Part preview beginning at full-movie time {case.get('timeline_offset_s', 0):.3f}s; all tool timestamps use this local Part clock"
                            + ". The deterministic family baseline is "
                            + json.dumps(turn["deterministic_baseline"])
                            + ". Use it as the measured starting point. Query Grafana history before rendering, measure every agent candidate, then query Grafana sound evidence for that exact candidate before selection. Do not add, move, or extend sound outside confirmed ranges."
                        ),
                        context=case["context"],
                        style=case["style"],
                        seconds=case["seconds"],
                    )
                ),
                *frame_parts(overview),
            ],
        )
        phase = "agent"
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
            async for event in runner.run_async(
                user_id="local",
                session_id=session_id,
                new_message=message,
                run_config=RunConfig(max_llm_calls=MAX_CONTROLLER_CALLS),
            ):
                for part in event.content.parts if event.content else []:
                    if part.thought:
                        continue
                    if part.function_call:
                        log(
                            "tool_call",
                            **part.function_call.model_dump(
                                mode="json", exclude_none=True
                            ),
                        )
                    elif part.function_response:
                        log(
                            "tool_result",
                            **part.function_response.model_dump(
                                mode="json", exclude_none=True
                            ),
                        )
                    elif part.text:
                        log("agent_message", text=part.text)
        session = await service.get_session(
            app_name=APP, user_id="local", session_id=session_id
        )
        phase = "family_render"
        layers = list(turn["candidates"])
        if turn["selection"] and turn["selection"]["candidate_id"]:
            rendered = family_agent.render_selection(
                pid,
                family_id,
                turn["selection"]["candidate_id"],
                baseline=turn["deterministic_baseline"],
                grafana_evidence=turn["selection"].get("grafana_evidence", {}),
            )
            turn["agent_layers"] = layers
            turn["candidates"] = [rendered]
            turn["selection"] = {
                **turn["selection"],
                "agent_layer_candidate_id": turn["selection"]["candidate_id"],
                "candidate_id": rendered["id"],
                "family_id": family_id,
            }
        elif turn["selection"]:
            turn["agent_layers"] = layers
            turn["candidates"] = []
        turn["status"] = "review_required" if turn["selection"] else "incomplete"
        if not turn["selection"]:
            turn["incomplete_reason"] = (
                "Loop ended without a selection; do not interpret generated candidates as approval."
            )
        turn["persistent_state"] = {
            "session_id": session_id,
            "event_count": len(session.events),
            "notes": session.state.get("notes", []),
            "arrangement_id": session.state.get("arrangement", {}).get("id")
            if isinstance(session.state.get("arrangement"), dict)
            else None,
            "candidate_count": len(session.state.get("candidates", [])),
        }
        log("session_saved", **turn["persistent_state"])
    except Exception as exc:
        if family_id and turn.get("candidates"):
            turn["agent_layers"] = list(turn["candidates"])
            turn["candidates"] = []
        turn["status"] = "failed"
        turn["error_type"] = type(exc).__name__
        turn["failure"] = failure_info(exc, phase, provider_failed)
        log("failed", **turn["failure"])
    finally:
        for resource in (runner, service):
            # VertexAiSessionService has no close(); only shut down what can be shut down.
            if resource is None or not hasattr(resource, "close"):
                continue
            try:
                await resource.close()
            except Exception as exc:
                turn["cleanup_errors"] = [
                    *turn.get("cleanup_errors", []),
                    type(exc).__name__,
                ]
                turn["status"] = "failed"
                turn["failure"] = failure_info(exc, "cleanup")
        turn["finished_at"] = time.time()
        turn["progress"] = {
            **turn.get("progress", {}),
            "status": turn["status"],
            "phase": "failed" if turn["status"] == "failed" else "complete",
            "label": "Agent run stopped before a reviewable candidate" if turn["status"] == "failed" else ("Candidate ready for human review" if turn.get("selection") else "Agent finished without a selected candidate"),
            "updated_at": turn["finished_at"],
            "cycle": turn.get("cycles", 0),
            "candidate_count": len(turn.get("candidates", [])),
        }
        atomic(folder / (tid + "-turn.json"), turn)
        obs.emit(
            pid,
            "turn_finished",
            {
                "status": turn["status"],
                "elapsed_s": turn["finished_at"] - turn["started_at"],
            },
            tid,
        )
        doc["status"] = turn["status"]
        atomic(folder / "project.json", doc)
        from . import metadata

        if metadata.enabled():
            # The hosted workspace reads Cloud SQL; a finished turn must land there.
            try:
                # run_turn is already inside an event loop; await instead of asyncio.run.
                await metadata.sync_project(doc, doc.get("owner_id") or "local")
            except Exception:
                traceback.print_exc()
    return turn


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--family-id", required=True)
    ap.add_argument(
        "--feedback", default="Create a fitted alternative from these files."
    )
    a = ap.parse_args()
    asyncio.run(execute(a.project, a.family_id, a.feedback))
