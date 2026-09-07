"""Persistent ADK project session with bounded, separately logged feedback turns."""

import argparse
import asyncio
import fcntl
import hashlib
import json
import subprocess
import time
import uuid

from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event, EventActions
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.memory.vertex_ai_memory_bank_service import (
    VertexAiMemoryBankService,
)
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, VertexAiSessionService
from google.genai import types

from ..agent.workflow import build
from ..agent.workflow_common import PROMPT_DIR, frame_parts, runtime_prompt
from ..config import (
    DATA_DIR as ROOT,
    DATABASE_URL,
    AGENT_ENGINE_ID,
    GOOGLE_CLOUD_LOCATION,
    GOOGLE_CLOUD_PROJECT,
    MAX_CONTROLLER_CALLS,
    METADATA_DATABASE_URL,
    PACKAGE_DIR,
    RUNTIME_MODE,
    TURN_TIMEOUT_SECONDS,
    SESSION_BACKEND,
    MEMORY_BANK_ENABLED,
)
from ..domain.projects import atomic, frames, load, project_dir
from ..ops import observability as obs
from . import metadata

APP = "orpheus"


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
    if SESSION_BACKEND == "agent_engine":
        return VertexAiSessionService(
            project=GOOGLE_CLOUD_PROJECT,
            location=GOOGLE_CLOUD_LOCATION,
            agent_engine_id=AGENT_ENGINE_ID,
        )
    return DatabaseSessionService(db_url=DATABASE_URL)


def session_fallback_url():
    return METADATA_DATABASE_URL if RUNTIME_MODE == "cloud_run" else DATABASE_URL


def memory_service():
    if not MEMORY_BANK_ENABLED:
        return None
    return VertexAiMemoryBankService(
        project=GOOGLE_CLOUD_PROJECT,
        location=GOOGLE_CLOUD_LOCATION,
        agent_engine_id=AGENT_ENGINE_ID,
    )


async def load_project_memory(service, project_id):
    if service is None:
        return []
    response = await service.search_memory(
        app_name=APP,
        user_id=project_id,
        query="Orpheus sound design context and style preferences",
    )
    memories = []
    for entry in response.memories[:8]:
        text = " ".join(
            part.text.strip()
            for part in entry.content.parts
            if part.text and part.text.strip()
        )
        if text:
            memories.append(text[:500])
    return memories


async def save_project_memory(service, project_id, case):
    if service is None:
        return
    content = (
        f"Project context: {case['context']}\n"
        f"Project style: {case['style']}"
    )
    await service.add_memory(
        app_name=APP,
        user_id=project_id,
        memories=[
            MemoryEntry(
                content=types.Content(
                    role="user",
                    parts=[types.Part(text=content[:2000])],
                ),
                custom_metadata={"source": "project_settings"},
            )
        ],
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


async def execute(pid, feedback="Create a fitted alternative from these files.", run_key=""):
    with (ROOT / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another paid run is active")
        await metadata.initialize()
        recover_orphaned_turns(pid)
        return await run_turn(pid, feedback, run_key)


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


async def run_turn(pid, feedback, run_key=""):
    if not isinstance(feedback, str) or not 1 <= len(feedback) <= 500:
        raise ValueError("Feedback is 1..500 characters")
    case = load(pid)
    folder = project_dir(pid)
    doc = json.loads((folder / "project.json").read_text())
    tid = uuid.uuid4().hex[:12]
    turn = {
        "id": tid,
        "status": "running",
        "started_at": time.time(),
        "feedback": feedback,
        "models_used": [],
        "cycles": 0,
        "candidates": [],
        "selection": None,
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
    if metadata.enabled():
        await metadata.sync_project(doc, doc.get("owner_id", "local"))
        if run_key:
            await metadata.update_run(pid, doc.get("owner_id", "local"), run_key, "running")
    provider_failed = False
    event_rows = []
    last_checkpoint = 0.0

    def checkpoint(force=False):
        nonlocal last_checkpoint
        now = time.monotonic()
        if RUNTIME_MODE == "cloud_run" and not force and now - last_checkpoint < 2:
            return
        (folder / "events.jsonl").write_text(
            "".join(json.dumps(row, default=str) + "\n" for row in event_rows)
        )
        atomic(folder / (tid + "-turn.json"), turn)
        last_checkpoint = now

    def log(event, **fields):
        nonlocal provider_failed
        if event == "model_failed":
            provider_failed = True
        if event == "model_response":
            provider_failed = False
        row = {"ts": time.time(), "turn_id": tid, "event": event, **fields}
        event_rows.append(row)
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
        checkpoint()
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
    memory = None
    runner = None
    phase = "session_initialization"
    try:
        service = session_service()
        phase = "input_validation"
        for name, digest in doc["prepared_hashes"].items():
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
                raise ValueError("Prepared input changed")
        phase = "session"
        memory = memory_service()
        try:
            session = await service.get_session(
                app_name=APP, user_id="local", session_id=pid
            )
            if session is None:
                session = await service.create_session(
                    app_name=APP,
                    user_id="local",
                    session_id=pid,
                    state={"candidates": [], "notes": []},
                )
        except Exception as exc:
            if SESSION_BACKEND != "agent_engine":
                raise
            log("session_backend_fallback", error_type=type(exc).__name__)
            try:
                await service.close()
            except Exception:
                pass
            service = DatabaseSessionService(db_url=session_fallback_url())
            session = await service.get_session(
                app_name=APP, user_id="local", session_id=pid
            )
            if session is None:
                session = await service.create_session(
                    app_name=APP,
                    user_id="local",
                    session_id=pid,
                    state={"candidates": [], "notes": []},
                )
        prior_events = len(session.events) if session else 0
        repaired = await repair_interrupted_tools(service, session)
        if repaired:
            log("interrupted_tools_recovered", count=repaired)
        human_reviews = [
            json.loads(p.read_text())
            for p in sorted(
                folder.glob("*-human.json"), key=lambda p: p.stat().st_mtime
            )
        ][-20:]
        memory_context = []
        if memory is not None:
            try:
                memory_context = await load_project_memory(memory, pid)
                log("memory_loaded", count=len(memory_context))
            except Exception as exc:
                log("memory_load_failed", error_type=type(exc).__name__)
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
                        "memory_context": memory_context,
                    }
                ),
            ),
        )
        log(
            "session_loaded",
            session_id=pid,
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
                    }
                ),
            ),
        )
        for role, name in [("target", "original.wav"), ("source", "sfx.wav")]:
            obs.emit(
                pid,
                "sound_profile",
                {"role": role, "profile": obs.sound_profile(folder / name)},
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
        runner = Runner(
            app_name=APP,
            agent=agent,
            session_service=service,
            memory_service=memory,
        )
        times = [float(t) for t in range(int(case["seconds"]))] + [
            max(0, case["seconds"] - 0.06)
        ]
        phase = "overview"
        overview = frames(case, times, folder / "frames")
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=runtime_prompt(
                        "user_request",
                        feedback=feedback,
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
                session_id=pid,
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
            app_name=APP, user_id="local", session_id=pid
        )
        turn["status"] = "review_required" if turn["selection"] else "incomplete"
        if not turn["selection"]:
            turn["incomplete_reason"] = (
                "Loop ended without a selection; do not interpret generated candidates as approval."
            )
        elif memory is not None:
            try:
                await save_project_memory(memory, pid, case)
                log("memory_saved", scope=pid)
            except Exception as exc:
                log("memory_save_failed", error_type=type(exc).__name__)
        turn["persistent_state"] = {
            "session_id": pid,
            "event_count": len(session.events),
            "notes": session.state.get("notes", []),
            "arrangement_id": session.state.get("arrangement", {}).get("id")
            if isinstance(session.state.get("arrangement"), dict)
            else None,
            "candidate_count": len(session.state.get("candidates", [])),
        }
        log("session_saved", **turn["persistent_state"])
    except Exception as exc:
        turn["status"] = "failed"
        turn["error_type"] = type(exc).__name__
        turn["failure"] = failure_info(exc, phase, provider_failed)
        log("failed", **turn["failure"])
    finally:
        for resource in (runner, service):
            if resource is None:
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
        checkpoint(force=True)
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
        if RUNTIME_MODE == "cloud_run":
            try:
                obs.flush()
            except Exception:
                pass
        doc["status"] = turn["status"]
        atomic(folder / "project.json", doc)
        if metadata.enabled():
            try:
                owner_id = doc.get("owner_id", "local")
                run_status = turn["status"]
                if run_key and await metadata.run_canceled(pid, owner_id, run_key):
                    run_status = "canceled"
                if run_key:
                    await metadata.update_run(pid, owner_id, run_key, run_status)
                await metadata.sync_turn(pid, turn, owner_id)
                await metadata.sync_project(doc, owner_id)
            except metadata.MetadataError:
                turn["catalog_sync"] = "failed"
                atomic(folder / (tid + "-turn.json"), turn)
    return turn


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument(
        "--feedback", default="Create a fitted alternative from these files."
    )
    ap.add_argument("--run-key", default="")
    a = ap.parse_args()
    asyncio.run(execute(a.project, a.feedback, a.run_key))
