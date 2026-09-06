import hashlib
import json

from google.adk.tools import ToolContext
from google.genai import types

from ..config import MAX_CONTROLLER_CALLS
from ..domain import arrangement, takes
from ..ops import observability as obs
from .workflow_common import (
    COMPLETION_WARNING_CALL,
    INSPECTION_STOP_CALL,
    frame_parts,
    runtime_prompt,
)


class StateTools:
    async def query_grafana(
        self, topic: str, candidate_id: str, tool_context: ToolContext
    ) -> dict:
        """Investigate CURRENT project through official Grafana MCP. topic history/failures/sound/takes queries Loki; runtime queries Prometheus health, backlog, failures and active alerts. candidate_id empty or saved render ID. MUST query history before new experiments and sound AFTER measuring a candidate. Query runtime/failures to diagnose degraded service; never equate failed perception with absent sound. For recording sessions query takes to compare recordings. Raw media stays local. Returns evidence and pending-export status. Empty/failed queries are not a quality pass; avoid repeating unchanged failures."""
        pid = (
            self.case.get("take_parent_project_id", self.case["id"])
            if topic == "takes"
            else self.case["id"]
        )
        if candidate_id and candidate_id not in [
            c["id"] for c in tool_context.state.get("candidates", [])
        ]:
            return {"error": "Unknown candidate"}
        result = await obs.investigate(pid, topic, candidate_id)
        receipts = dict(tool_context.state.get("grafana_receipts", {}))
        receipts[topic + ":" + candidate_id] = result
        tool_context.state["grafana_receipts"] = receipts
        self.log(
            "grafana_investigation",
            topic=topic,
            candidate_id=candidate_id,
            status=result["status"],
            receipt_id=result.get("receipt_id"),
        )
        if topic == "takes":
            result = {
                **result,
                "take_context": [
                    {
                        k: t[k]
                        for k in (
                            "id",
                            "brief",
                            "picture_start_s",
                            "clock",
                            "clock_uncertainty",
                        )
                    }
                    for t in takes.list_takes(pid)
                ],
            }
        return result

    def propose_take_experiment(
        self, proposal_json: str, tool_context: ToolContext
    ) -> dict:
        """Save one reversible recording experiment AFTER query_grafana(topic=takes,candidate_id='') succeeds. JSON exactly take_id,change,expected_effect,reason (text <=500). Name one prop OR performance OR mic-position change, the measurable prediction and relevant uncertainty. No automatic recording, prop identity assertion or artistic approval. Creator performs and auditions the next take."""
        receipt = tool_context.state.get("grafana_receipts", {}).get("takes:", {})
        if receipt.get("status") != "ok" or not receipt.get("evidence_count"):
            return {
                "error": "Query takes through Grafana MCP first; no evidence-based proposal without nonempty evidence."
            }
        try:
            return takes.save_experiment(
                self.case.get("take_parent_project_id", self.case["id"]),
                json.loads(proposal_json),
                receipt["receipt_id"],
            )
        except (ValueError, KeyError, TypeError) as exc:
            return {"error": str(exc)[:300]}

    def source_signature(self, row):
        return hashlib.sha256(
            json.dumps(
                {
                    "source_id": row.get("source_id"),
                    "source_range_s": row.get("source_range_s"),
                    "source_file_sha256": hashlib.sha256(
                        self.case["sfx_path"].read_bytes()
                    ).hexdigest(),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:20]

    def impact_signature(self, row):
        return hashlib.sha256(
            json.dumps(
                {
                    k: row.get(k)
                    for k in (
                        "target_id",
                        "source_id",
                        "target_range_s",
                        "source_range_s",
                        "kind",
                        "disposition",
                        "anchor",
                        "target_anchor_s",
                        "source_anchor_s",
                    )
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:20]

    def mapping_context(self, tool_context: ToolContext) -> dict:
        """Read the saved arrangement, valid mapping row IDs, candidate IDs, and missing receipts. MUST use when resuming an arrangement or recovering ID/evidence errors; use returned mapping_id for row operations, not arrangement_id. No edits are made."""
        state = tool_context.state
        current = state.get("arrangement")
        candidates = state.get("candidates", [])
        if not isinstance(current, dict):
            return {
                "arrangement_id": None,
                "valid_mapping_ids": [],
                "rows": [],
                "candidate_ids": [c.get("id") for c in candidates if c.get("id")],
                "next_step": "Save a non-empty provisional arrangement first.",
            }
        rows = []
        missing = []
        identities = arrangement.identities(self.case)
        delivered = [
            t
            for t in state.get("delivered_centers", [])
            if isinstance(t, (int, float)) and (not isinstance(t, bool))
        ]
        for row in current.get("rows", []):
            mapping_id = row.get("id")
            if not mapping_id:
                continue
            used_impact = (
                row.get("kind") == "impact" and row.get("disposition") == "use"
            )
            source_review = state.get("source_option_reviews", {}).get(mapping_id)
            source_ready = bool(
                source_review
                and source_review.get("source_signature") == self.source_signature(row)
            )
            fit = state.get("impact_fit_receipts", {}).get(mapping_id)
            fit_ready = bool(
                fit
                and fit.get("fit_signature") == self.impact_signature(row)
                and (fit.get("input_hashes") == identities)
            )
            contact = row.get("target_anchor_s")
            temporal_ready = bool(
                isinstance(contact, (int, float))
                and (not isinstance(contact, bool))
                and any(abs(contact - t) <= 0.2 for t in delivered)
            )
            nearby_reviews = [
                review
                for review in state.get("event_reviews", [])
                if isinstance(review, dict)
                and isinstance(review.get("center_s"), (int, float))
                and (not isinstance(review.get("center_s"), bool))
                and isinstance(contact, (int, float))
                and (not isinstance(contact, bool))
                and (abs(contact - review["center_s"]) <= 0.2)
            ]
            review_ready = bool(
                nearby_reviews
                and nearby_reviews[-1].get("verdict") in ("target", "uncertain")
            )
            evidence_state = (
                "ready"
                if not used_impact
                or (
                    row.get("anchor") == "contact"
                    and source_ready
                    and fit_ready
                    and temporal_ready
                    and review_ready
                )
                else "incomplete"
            )
            if used_impact and evidence_state == "incomplete":
                missing.append(mapping_id)
            rows.append(
                {
                    "mapping_id": mapping_id,
                    "target_id": row.get("target_id"),
                    "source_id": row.get("source_id"),
                    "kind": row.get("kind"),
                    "disposition": row.get("disposition"),
                    "anchor": row.get("anchor"),
                    "target_range_s": row.get("target_range_s"),
                    "source_range_s": row.get("source_range_s"),
                    "target_anchor_s": row.get("target_anchor_s"),
                    "source_anchor_s": row.get("source_anchor_s"),
                    "source_inspection": "ready" if source_ready else "missing",
                    "impact_fit": "ready"
                    if fit_ready
                    else "not_required"
                    if not used_impact
                    else "missing",
                    "temporal_review": "ready"
                    if temporal_ready
                    else "not_required"
                    if not used_impact
                    else "missing",
                    "event_review": "ready"
                    if review_ready
                    else "not_required"
                    if not used_impact
                    else "missing",
                    "evidence_state": evidence_state,
                }
            )
        return {
            "arrangement_id": current.get("id"),
            "valid_mapping_ids": [r["mapping_id"] for r in rows],
            "rows": rows,
            "missing_evidence_ids": missing,
            "candidate_ids": [c.get("id") for c in candidates if c.get("id")],
            "next_step": "Use mapping_id row IDs above to complete missing evidence, then render_arrangement and measure_candidate."
            if missing
            else "Call render_arrangement, then measure_candidate with the returned candidate_id.",
        }

    def retain_evidence_receipts(self, state, result):
        rows = {r["id"]: r for r in result["rows"]}
        state["source_option_reviews"] = {
            mid: receipt
            for mid, receipt in state.get("source_option_reviews", {}).items()
            if mid in rows
            and receipt.get("source_signature") == self.source_signature(rows[mid])
        }
        state["impact_fit_receipts"] = {
            mid: {**receipt, "arrangement_id": result["id"]}
            for mid, receipt in state.get("impact_fit_receipts", {}).items()
            if mid in rows
            and receipt.get("fit_signature") == self.impact_signature(rows[mid])
        }

    def impact_gate(self, state):
        arrangement_state = state.get("arrangement")
        if not isinstance(arrangement_state, dict):
            return None
        missing = []
        for row in arrangement_state.get("rows", []):
            if row.get("kind") != "impact" or row.get("disposition") != "use":
                continue
            issues = []
            if row.get("anchor") != "contact" or not all(
                key in row for key in ("target_anchor_s", "source_anchor_s")
            ):
                issues.append("explicit contact/source anchors")
            fit = state.get("impact_fit_receipts", {}).get(row.get("id"))
            if (
                not fit
                or fit.get("fit_signature") != self.impact_signature(row)
                or fit.get("input_hashes") != arrangement.identities(self.case)
            ):
                issues.append("inspect_mapping_source then fit_mapping_impact")
            contact = row.get("target_anchor_s")
            if not isinstance(contact, (int, float)) or isinstance(contact, bool):
                issues.append("target contact anchor")
                contact = None
            delivered = [
                t
                for t in state.get("delivered_centers", [])
                if isinstance(t, (int, float)) and (not isinstance(t, bool))
            ]
            if contact is not None and (
                not any(abs(contact - t) <= 0.2 for t in delivered)
            ):
                issues.append("delivered temporal review near contact")
            reviews = [
                r
                for r in state.get("event_reviews", [])
                if isinstance(r, dict)
                and isinstance(r.get("center_s"), (int, float))
                and (not isinstance(r.get("center_s"), bool))
                and (contact is not None)
                and (abs(contact - r["center_s"]) <= 0.2)
            ]
            if not reviews or reviews[-1].get("verdict") not in ("target", "uncertain"):
                issues.append("record_event_review target/uncertain verdict")
            if issues:
                missing.append(row.get("id", "<unnamed>") + ": " + ", ".join(issues))
        return (
            {
                "error": "impact evidence gate: " + "; ".join(missing),
                "missing_mapping_ids": [item.split(":", 1)[0] for item in missing],
                "valid_mapping_ids": [
                    r.get("id")
                    for r in arrangement_state.get("rows", [])
                    if r.get("id")
                ],
                "arrangement_id": arrangement_state.get("id"),
                "next_step": "mapping_id is a row ID, not arrangement_id. Complete the listed source inspection/fitting and temporal review before render_arrangement.",
            }
            if missing
            else None
        )

    def strategy_fingerprint(self, result):
        """Fingerprint meaningful candidate strategy changes; gain-only edits are not diversity."""
        rows = (
            result.get("arrangement", {}).get("rows", [])
            if isinstance(result, dict)
            else []
        )
        if not rows:
            dimensions = {
                "coverage": [
                    result.get("target_interval_s"),
                    result.get("render_mode"),
                ],
                "source": [
                    result.get("source_interval_s"),
                    result.get("repeat"),
                    result.get("crossfade_s"),
                ],
                "dynamics": [result.get("render_mode")],
            }
            key = hashlib.sha256(
                json.dumps(dimensions, sort_keys=True).encode()
            ).hexdigest()[:20]
            return {
                "key": key,
                "dimension_hashes": {
                    name: hashlib.sha256(
                        json.dumps(value, sort_keys=True).encode()
                    ).hexdigest()[:12]
                    for name, value in dimensions.items()
                },
                "coverage_count": 1,
                "source_count": 1,
                "shape_summary": ["texture"],
            }
        coverage = [
            (
                r.get("id"),
                r.get("kind"),
                r.get("disposition"),
                r.get("target_range_s"),
                r.get("target_anchor_s"),
            )
            for r in rows
        ]
        source = [
            (
                r.get("id"),
                r.get("source_id"),
                r.get("source_range_s"),
                r.get("source_anchor_s"),
            )
            for r in rows
            if r.get("disposition") == "use"
        ]
        dynamics = [
            (r.get("id"), r.get("shape"), r.get("target_body_dbfs"))
            for r in rows
            if r.get("disposition") == "use"
        ]
        dimensions = {"coverage": coverage, "source": source, "dynamics": dynamics}
        key = hashlib.sha256(
            json.dumps(dimensions, sort_keys=True).encode()
        ).hexdigest()[:20]
        return {
            "key": key,
            "dimension_hashes": {
                name: hashlib.sha256(
                    json.dumps(value, sort_keys=True).encode()
                ).hexdigest()[:12]
                for name, value in dimensions.items()
            },
            "coverage_count": len(coverage),
            "source_count": len(source),
            "shape_summary": sorted(
                {r.get("shape") for r in rows if r.get("disposition") == "use"}
            ),
        }

    def strategy_reserve_message(self, state):
        candidates = state.get("candidates", [])
        keys = {
            c.get("strategy", {}).get("key")
            for c in candidates
            if c.get("strategy", {}).get("key")
        }
        if len(keys) < 2:
            return runtime_prompt("strategy_reserve_few")
        return runtime_prompt("strategy_reserve_many", count=len(keys))

    def cycle(self, callback_context):
        s = callback_context.state
        s["cycle"] = s.get("cycle", 0) + 1
        s["rendered"] = False
        s["cycle_reviews"] = 0
        s["waveform_calls"] = 0
        self.log("loop_cycle", cycle=s["cycle"])

    def before_model(self, callback_context, llm_request):
        s = callback_context.state
        starts = [
            i
            for i, c in enumerate(llm_request.contents)
            if c.role == "user"
            and any((p.text or "").startswith("User request:") for p in c.parts or [])
        ]
        if starts:
            llm_request.contents = llm_request.contents[starts[-1] :]
        if s.get("controller_calls", 0) == 0:
            snapshot = {
                "mapping_context": self.mapping_context(callback_context),
                "notes": s.get("notes", [])[-12:],
                "human_reviews": s.get("human_reviews", []),
                "user_preferences": s.get("user_preferences", {}),
                "memory_context": s.get("memory_context", []),
                "candidates": [
                    {
                        k: c.get(k)
                        for k in (
                            "id",
                            "audio_sha256",
                            "flags",
                            "hypothesis",
                            "strategy",
                        )
                    }
                    for c in s.get("candidates", [])[-8:]
                ],
                "catalog": {
                    role: dict(list(events.items())[-100:])
                    for role, events in arrangement.catalog(
                        self.case, s.get("audio_evidence", {})
                    ).items()
                },
                "catalog_view_limit_per_role": 100,
                "lessons_version": "mixed-landmarks-1",
                "warning": "Notes are provisional. Only human_ui receipts are human judgments; preserve approved mappings unless user feedback/evidence contradicts them, and explain revisions. Older history stays in SQLite/log artifacts.",
            }
            self.log("context_snapshot", snapshot=snapshot)
        else:
            snapshot = {
                "mapping_context": self.mapping_context(callback_context),
                "candidate_strategies": [
                    c.get("strategy")
                    for c in s.get("candidates", [])[-8:]
                    if c.get("strategy")
                ],
            }
        snapshot["audio_budget"] = self.audio.availability()
        snapshot["grafana"] = {
            "enabled": bool(obs.config()),
            "startup": s.get("grafana_startup"),
            "query_receipts": {
                k: {a: v.get(a) for a in ("status", "receipt_id", "pending_exports")}
                for k, v in s.get("grafana_receipts", {}).items()
            },
            "take_parent_project_id": self.case.get("take_parent_project_id"),
            "take_brief": self.case.get("take_brief"),
        }
        snapshot["candidate_audio_review_status"] = {
            key: {k: value.get(k) for k in ("status", "evidence_id", "warning")}
            for key, value in s.get("candidate_audio_reviews", {}).items()
        }
        llm_request.contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=runtime_prompt(
                            "project_state", snapshot=json.dumps(snapshot)
                        )
                    )
                ],
            )
        )
        s["controller_calls"] = s.get("controller_calls", 0) + 1
        if s["controller_calls"] >= COMPLETION_WARNING_CALL:
            llm_request.contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=runtime_prompt(
                                "completion_reserve",
                                remaining=max(
                                    0, MAX_CONTROLLER_CALLS - s["controller_calls"]
                                ),
                            )
                        )
                    ],
                )
            )
        llm_request.contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=runtime_prompt(
                            "runtime_status",
                            cycle=s.get("cycle"),
                            renders=s.get("turn_renders", 0),
                            notes=s.get("notes", []),
                        )
                    )
                ],
            )
        )
        llm_request.contents.append(
            types.Content(
                role="user", parts=[types.Part(text=self.strategy_reserve_message(s))]
            )
        )
        if s.get("empty_arrangement_attempts", 0) >= 2:
            current = (
                s.get("arrangement") if isinstance(s.get("arrangement"), dict) else {}
            )
            ids = [r.get("id") for r in current.get("rows", []) if r.get("id")]
            action = (
                'Call inspect_mapping_source(mapping_id="'
                + ids[0]
                + '") and then fit_mapping_impact if the mapping is an impact; otherwise provide a non-empty JSON array or render the saved mapping.'
                if ids
                else "Provide a non-empty JSON array or finish unsuitable."
            )
            llm_request.contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part(text=runtime_prompt("recovery", action=action))],
                )
            )
        if self.pending:
            llm_request.contents.append(
                types.Content(role="user", parts=frame_parts(self.pending))
            )
            self.pending.clear()
            delivered = list(
                dict.fromkeys([*s.get("delivered_centers", []), *s.get("reviewed", [])])
            )
            for receipt in self.pending_receipts:
                delivered.extend(frame["time_s"] for frame in receipt["frames"])
            s["delivered_centers"] = list(dict.fromkeys(delivered))
            s["delivered_frame_receipts"] = [
                *s.get("delivered_frame_receipts", []),
                *self.pending_receipts,
            ]
            self.pending_receipts.clear()

    def before_tool(self, tool, args, tool_context):
        if tool_context.state.get(
            "controller_calls", 0
        ) >= INSPECTION_STOP_CALL and tool.name in (
            "inspect_scene",
            "inspect_audio",
            "review_window",
            "review_moments",
            "inspect_signal",
        ):
            return {
                "error": "Completion reserve active. Use saved evidence to save/render mapping, measure, and finish. No new broad inspection."
            }

    def after_tool(self, tool, args, tool_context, tool_response):
        s = tool_context.state
        if isinstance(tool_response, dict) and "error" in tool_response:
            signature = hashlib.sha256(
                json.dumps(
                    [tool.name, args, tool_response["error"]], sort_keys=True
                ).encode()
            ).hexdigest()
            failures = dict(s.get("tool_failures", {}))
            failures[signature] = failures.get(signature, 0) + 1
            s["tool_failures"] = failures
            if failures[signature] >= 3:
                tool_response = {
                    **tool_response,
                    "recovery": "Repeated identical failed call. Change arguments using actual IDs/schema; or finish unsuitable with empty candidate_id. Do not repeat.",
                }
            return tool_response
