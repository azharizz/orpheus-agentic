from tests.support import load_case

"Offline boundary checks; ORPHEUS_LIVE_MCP=1 additionally tests real local MCP in ADK."
import asyncio
import copy
import hashlib
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import numpy as np

from orpheus.domain import families, family_agent, projects, takes
from orpheus.ops import observability as o


class EvidenceChecks(unittest.TestCase):
    def test_part_contract_is_exact_and_events_carry_context(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(o, "STORE", Path(tmp)),
            patch.object(o, "DB", Path(tmp) / "db"),
            patch.object(o, "config", return_value={"enabled": True}),
        ):
            context = o.part_context("1234567890abcdef", family_id="abcdef123456", part_start_s=12, part_end_s=27)
            o.emit("1234567890abcdef", "candidate", {"id": "0123456789ab", **context, "metrics": {"clipped_samples": 0}})
            with o.connect() as db:
                row = json.loads(db.execute("SELECT payload FROM events").fetchone()[0])
            self.assertEqual(row["evidence_contract"], o.EVIDENCE_CONTRACT_SCHEMA)
            self.assertEqual(row["family_id"], "abcdef123456")
            self.assertEqual(row["part_start_s"], 12)
            with self.assertRaises(ValueError):
                o.part_context("1234567890abcdef", part_start_s=3, part_end_s=3)

    def test_config_repairs_legacy_dashboard_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "local.json"
            path.write_text(json.dumps({
                "dashboard_url": "http://127.0.0.1:13000/d/orpheus/foley-evidence",
                "mcp_url": "http://127.0.0.1:18001/mcp",
                "mcp_token": "test-token-not-real",
            }))
            with patch.object(o, "CONFIG", path), patch.dict(os.environ, {"ORPHEUS_GRAFANA_ENABLED": "1"}):
                value = o.config()
            self.assertTrue(value["dashboard_url"].endswith("/d/orpheus/agentic-foley-control-room"))

    def test_project_metrics_compare_baseline_agent_and_review_state(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(o, "STORE", Path(tmp)),
            patch.object(o, "DB", Path(tmp) / "db"),
            patch.object(o, "config", return_value={"enabled": True}),
        ):
            pid = "1234567890abcdef"
            o.emit(pid, "deterministic_baseline", {"metrics": {
                "integrated_lufs": -26.2, "true_peak_dbtp": -0.5,
                "picture_unchanged": True, "clipped_samples": 0,
            }, "measurements": {"accepted_events": 9}}, timestamp=1000)
            o.emit(pid, "candidate", {"metrics": {
                "integrated_lufs": -28.4, "true_peak_dbtp": -4.3,
                "picture_unchanged": True, "clipped_samples": 0,
            }, "measurements": {"accepted_events": 9}})
            o.emit(pid, "selection", {"decision": "needs_human_review"})
            o.emit(pid, "human_review", {"verdict": "approve"})
            o.emit(pid, "tool_result", {"name": "render_arrangement", "response": {"status": "ok"}})
            o.emit(pid, "session_saved", {}, timestamp=1012.5)
            o.emit(pid, "movie_analysis", {"status": "review_required", "measurements": {"progress": 100, "events": 42, "suggestions": 3, "noise_regions": 2, "duration_s": 1800}})
            o.emit(pid, "family_movie_search", {"family_id": "footsteps", "matches": 20})
            metrics = o.metrics_text()
            self.assertIn(f'orpheus_baseline_integrated_lufs{{project_id="{pid}"}} -26.2', metrics)
            self.assertIn(f'orpheus_candidate_integrated_lufs{{project_id="{pid}"}} -28.4', metrics)
            self.assertIn(f'orpheus_project_candidate_state{{project_id="{pid}"}} 1', metrics)
            self.assertIn(f'orpheus_project_review_state{{project_id="{pid}"}} 1', metrics)
            self.assertIn(f'orpheus_project_run_state{{project_id="{pid}"}} 1', metrics)
            self.assertIn(f'orpheus_project_tool_events_total{{project_id="{pid}",tool="render_arrangement",outcome="completed"}} 1', metrics)
            self.assertIn(f'orpheus_movie_progress{{project_id="{pid}"}} 100', metrics)
            self.assertIn(f'orpheus_movie_events{{project_id="{pid}"}} 42', metrics)
            self.assertIn(f'orpheus_movie_suggestions{{project_id="{pid}"}} 3', metrics)

    def test_redaction_idempotence_outage_and_signal(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(o, "STORE", Path(tmp)),
            patch.object(o, "DB", Path(tmp) / "db"),
            patch.object(
                o,
                "config",
                return_value={
                    "loki_url": "http://127.0.0.1:1",
                    "tempo_url": "http://127.0.0.1:1",
                },
            ),
        ):
            fields = {
                "requested_model": "test/model",
                "prompt": "secret-credential",
                "response": {"error": "secret-credential"},
                "metrics": {"integrated_lufs": -23, "invalid": float("nan")},
                "source_path": "private.wav",
            }
            one = o.emit("1234567890abcdef", "tool_result", fields, "turn", 1000)
            self.assertEqual(
                one, o.emit("1234567890abcdef", "tool_result", fields, "turn", 1000)
            )
            self.assertEqual(o.pending(), 1)
            with o.connect() as db:
                raw = db.execute("SELECT payload FROM events").fetchone()[0]
            self.assertNotIn("secret", raw)
            self.assertNotIn("private", raw)
            self.assertNotIn("NaN", raw)
            self.assertTrue(json.loads(raw)["tool_error"])
            self.assertEqual(json.loads(raw)["outcome"], "failed")
            self.assertTrue(o.flush()["errors"])
            self.assertEqual(o.pending(), 1)
            profile = o.sound_profile(load_case()["sfx_path"])
            self.assertLessEqual(len(profile["envelope"]), 120)
            self.assertGreater(profile["duration_s"], 0)
            with self.assertRaises(ValueError):
                o.enqueue("../", "bad")
            with patch.object(
                httpx.Client,
                "post",
                return_value=httpx.Response(
                    204, request=httpx.Request("POST", "http://local")
                ),
            ):
                self.assertEqual(o.flush()["pending"], 0)

    @unittest.skipUnless(
        os.environ.get("ORPHEUS_LIVE_MCP") == "1",
        "Opt-in real local services; no paid model",
    )
    def test_real_adk_mcp_take_experiment(self):
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_response import LlmResponse
        from google.adk.runners import Runner
        from google.adk.sessions import DatabaseSessionService
        from google.genai import types

        from orpheus.agent import workflow

        calls = []
        results = []
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(projects, "PROJECTS", Path(tmp)),
        ):
            doc = projects.create(
                load_case()["video_path"],
                context="Synthetic controller integration; not a recorded human performance",
            )
            pid = doc["id"]
            families.build_index(pid)
            family = families.create(pid, "synthetic steps", [0.7, 1.1])
            source = load_case()["sfx_path"]
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            first = takes.add_take(
                pid, source, "Imported existing SFX, validation fixture",
                family_id=family["id"],
            )
            takes.add_take(
                pid,
                source,
                "Same recording, storage comparison fixture; not an independent take",
                family_id=family["id"],
            )
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            proposal = {
                "take_id": first["id"],
                "change": "Move microphone farther away in the NEXT human recording",
                "expected_effect": "Test whether body level decreases; maintain recorder gain",
                "reason": "Controlled test proposal; identical fixture takes cannot establish perceptual improvement",
            }

            class Scripted(BaseLlm):
                model: str = "scripted-local-mcp-integration-no-provider"

                async def generate_content_async(self, llm_request, stream=False):
                    n = len(calls)
                    calls.append(n)
                    actions = [
                        ("query_grafana", {"topic": "takes", "candidate_id": ""}),
                        (
                            "propose_take_experiment",
                            {"proposal_json": json.dumps(proposal)},
                        ),
                        ("query_grafana", {"topic": "runtime", "candidate_id": ""}),
                    ]
                    if n < len(actions):
                        name, args = actions[n]
                        part = types.Part(
                            function_call=types.FunctionCall(
                                name=name, args=args, id=str(n)
                            )
                        )
                    else:
                        part = types.Part(
                            text="Integration complete. No sound-quality judgment."
                        )
                    yield LlmResponse(content=types.Content(role="model", parts=[part]))

            async def run():
                folder = projects.project_dir(pid)
                case = family_agent.load_case(pid, family["id"])
                service = DatabaseSessionService(
                    db_url="sqlite+aiosqlite:///" + str(Path(tmp) / "sessions.sqlite")
                )
                await service.create_session(
                    app_name="test",
                    user_id="local",
                    session_id="mcp",
                    state={"cycle": 0, "notes": []},
                )
                with patch.object(workflow, "ControllerModel", return_value=Scripted()):
                    agent = workflow.build(case, folder, lambda *a, **kw: None)
                runner = Runner(app_name="test", agent=agent, session_service=service)
                async for ev in runner.run_async(
                    user_id="local",
                    session_id="mcp",
                    new_message=types.Content(
                        role="user",
                        parts=[
                            types.Part(text="Verify MCP and one take proposal only.")
                        ],
                    ),
                ):
                    for part in ev.content.parts if ev.content else []:
                        if part.function_response:
                            results.append(
                                {
                                    "name": part.function_response.name,
                                    "response": part.function_response.response,
                                }
                            )
                session = await service.get_session(
                    app_name="test", user_id="local", session_id="mcp"
                )
                self.assertEqual(
                    session.state["grafana_receipts"]["takes:"]["status"], "ok"
                )
                self.assertGreaterEqual(
                    session.state["grafana_receipts"]["takes:"]["evidence_count"], 2
                )
                self.assertEqual(
                    session.state["grafana_receipts"]["runtime:"]["status"], "ok"
                )
                self.assertEqual(
                    len(list(projects.project_dir(pid).glob("*-experiment.json"))), 1
                )
                self.assertTrue(
                    all(("error" not in r["response"] for r in results)), results
                )
                await runner.close()
                await service.close()

            asyncio.run(run())
            (projects.ROOT / "diagnostics").mkdir(parents=True, exist_ok=True)
            projects.atomic(
                projects.ROOT / "diagnostics/LOCAL_GRAFANA_ADK_VERIFICATION.json",
                {
                    "controller": "scripted; no paid inference or quality claim",
                    "real_adk": True,
                    "real_official_mcp": True,
                    "family_scoped_fitting": True,
                    "original_preserved": True,
                    "tool_results": results,
                },
            )


if __name__ == "__main__":
    unittest.main()


class DashboardContract(unittest.TestCase):
    def test_generated_dashboard_references_only_provisioned_targets(self):
        from orpheus.config import OBSERVABILITY_ASSETS
        from orpheus.ops import grafana

        doc = json.loads((OBSERVABILITY_ASSETS / "dashboards" / "foley.json").read_text())
        self.assertTrue(grafana.validate(doc))
        for mutate in (
            lambda d: d.__setitem__("uid", "drifted"),
            lambda d: d["panels"][1].__setitem__("id", d["panels"][0]["id"]),
            lambda d: d["panels"][0].pop("id"),
            lambda d: next(
                p for p in d["panels"] if p.get("targets")
            )["targets"][0]["datasource"].__setitem__("uid", "absent"),
        ):
            broken = copy.deepcopy(doc)
            mutate(broken)
            with self.assertRaises(ValueError):
                grafana.validate(broken)

    def test_datasource_uids_match_provisioning(self):
        import re

        from orpheus.config import OBSERVABILITY_ASSETS
        from orpheus.ops import grafana

        text = (OBSERVABILITY_ASSETS / "provisioning" / "datasources" / "local.yaml").read_text()
        self.assertEqual(set(re.findall(r"^\s*uid:\s*(\S+)", text, re.M)), grafana.DATASOURCE_UIDS)

    def test_committed_dashboard_matches_generator(self):
        from orpheus.ops import grafana

        self.assertEqual(grafana.check_committed()["status"], "ok")

    def test_alert_and_recording_rules_are_wellformed(self):
        import yaml

        from orpheus.config import OBSERVABILITY_ASSETS

        groups = yaml.safe_load((OBSERVABILITY_ASSETS / "rules.yaml").read_text())["groups"]
        self.assertIn("orpheus-evidence", {g["name"] for g in groups})
        recorded = {r["record"] for g in groups for r in g["rules"] if r.get("record")}
        for group in groups:
            for rule in group["rules"]:
                self.assertTrue(rule.get("alert") or rule.get("record"))
                self.assertTrue(rule["expr"].strip())
                for ref in re.findall(r"orpheus:[a-z_:0-9]+", rule["expr"]):
                    self.assertIn(ref, recorded)


class PartLens(unittest.TestCase):
    def store(self, tmp):
        return (
            patch.object(o, "STORE", Path(tmp)),
            patch.object(o, "DB", Path(tmp) / "db"),
            patch.object(o, "config", return_value={"enabled": True}),
        )

    def test_lens_scopes_to_the_selected_part_and_names_decision_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                near = o.part_context(pid, family_id="abcdef123456", part_start_s=10, part_end_s=20)
                far = o.part_context(pid, family_id="abcdef123456", part_start_s=900, part_end_s=910)
                o.emit(pid, "candidate", {"id": "0123456789ab", **near}, timestamp=1000)
                o.emit(pid, "human_review", {"verdict": "approved", **near}, timestamp=1001)
                o.emit(pid, "candidate", {"id": "0123456789ab", **far}, timestamp=1002)
                lens = o.part_lens(pid, near)
                self.assertEqual(lens["row_count"], 2)
                self.assertEqual(lens["evidence_contract"], o.EVIDENCE_CONTRACT_SCHEMA)
                self.assertEqual(
                    [d["owner"] for d in lens["decisions"]], ["agent", "human"]
                )
                self.assertEqual(lens["decisions"][-1]["decision"], "approved")

    def test_selector_is_bounded_and_rejects_bad_scope(self):
        pid = "1234567890abcdef"
        part = {"part_start_s": 5, "part_end_s": 9, "family_id": "abcdef123456"}
        overlap = o.part_selector(pid, part, "0123456789ab")
        self.assertIn('project_id="' + pid + '"', overlap)
        self.assertIn('candidate_id="0123456789ab"', overlap)
        self.assertIn("part_end_s >= 5", overlap)
        self.assertIn("part_start_s <= 9", overlap)
        with self.assertRaises(ValueError):
            o.part_selector("nope", part)
        with self.assertRaises(ValueError):
            o.part_selector(pid, part, "bad-candidate")
        with self.assertRaises(ValueError):
            o.part_selector(pid, {"part_start_s": None, "part_end_s": 9})
        injected = o.part_selector(pid, {**part, "family_id": 'x"} | evil | {a="b'})
        self.assertNotIn("evil", injected)
        self.assertIn('family_id="unknown"', injected)

    def test_unresolved_states_never_read_as_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                self.assertIn("no_evidence_rows", o.unresolved([]))
                self.assertIn("no_decision_owner", o.unresolved([]))
                rows = [{"event": "tool_result", "tool_error": True}]
                self.assertIn("failures_present", o.unresolved(rows))

    def test_gates_report_unresolved_when_grafana_is_absent(self):
        with patch.object(o, "config", return_value=None):
            result = o.gates()
            self.assertEqual(set(result["checks"].values()), {"unresolved"})
            self.assertEqual(
                result["blocking"], sorted(["runtime", "mcp", "export"])
            )

    def test_envelope_levels_keep_transients_and_skip_short_audio(self):
        db = np.linspace(-60, -10, 100 * 185)
        levels = o.envelope_levels(db)
        self.assertEqual([len(levels[k]) for k in ("1s", "10s", "60s")], [185, 18, 3])
        coarse = levels["60s"][0]
        self.assertLess(coarse["min_dbfs"], coarse["max_dbfs"])
        self.assertEqual(o.envelope_levels(np.linspace(-60, -10, 50)), {})

    def test_coverage_pages_and_marks_span_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                for index in range(3):
                    o.emit(
                        pid,
                        "candidate",
                        {"id": "0123456789ab", "part_start_s": index * 60, "part_end_s": index * 60 + 5},
                        timestamp=1000 + index,
                    )
                page = o.coverage_timeline(pid, page=0, page_size=2, span_s=60)
                self.assertEqual(page["total_chunks"], 3)
                self.assertTrue(page["has_more"])
                self.assertEqual(len(page["chunks"]), 2)
                self.assertEqual({c["state"] for c in page["chunks"]}, {"covered"})
                self.assertFalse(o.coverage_timeline(pid, page=1, page_size=2, span_s=60)["has_more"])

    def test_snapshot_is_immutable_and_report_reads_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                part = o.part_context(pid, part_start_s=10, part_end_s=20)
                o.emit(pid, "human_review", {"verdict": "approved", **part}, timestamp=1000)
                first = o.snapshot(pid, part, label="review-1")
                again = o.snapshot(pid, part, label="review-1")
                self.assertEqual(first["snapshot_id"], again["snapshot_id"])
                path = Path(first["path"])
                self.assertEqual(path.stat().st_mode & 0o222, 0)
                back = o.static_report(first["snapshot_id"])
                self.assertEqual(back["status"], "ok")
                self.assertEqual(back["decisions"][0]["owner"], "human")
                self.assertEqual(o.static_report("a" * 20)["status"], "missing")
                with self.assertRaises(ValueError):
                    o.static_report("short")

    def test_part_routes_are_reachable_and_reject_bad_input(self):
        import threading
        from http.server import ThreadingHTTPServer

        from orpheus.domain import projects

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "local.json"
            cfg.write_text(json.dumps({
                "dashboard_url": "http://127.0.0.1:13000" + o.DASHBOARD_PATH,
                "mcp_url": "http://127.0.0.1:1/mcp",
                "mcp_token": "t",
            }))
            pid = "1234567890abcdef"
            with (
                patch.object(projects, "ROOT", root),
                patch.object(projects, "PROJECTS", root / "projects"),
                patch.object(projects, "load", lambda ident: {"id": ident}),
                patch.object(o, "STORE", root),
                patch.object(o, "DB", root / "db"),
                patch.object(o, "CONFIG", cfg),
                patch.dict(os.environ, {"ORPHEUS_GRAFANA_ENABLED": "1"}),
            ):
                from orpheus.server import web

                part = o.part_context(pid, part_start_s=10, part_end_s=20)
                o.emit(pid, "human_review", {"verdict": "approved", **part}, timestamp=1000)
                server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{server.server_port}"
                client = httpx.Client(base_url=base, headers={"Origin": base}, trust_env=False, timeout=30)
                try:
                    body = {"project_id": pid, "part": {"part_start_s": 10, "part_end_s": 20}}
                    lens = client.post("/api/grafana/part", json=body)
                    self.assertEqual(lens.status_code, 200, lens.text)
                    self.assertEqual([d["owner"] for d in lens.json()["decisions"]], ["human"])
                    self.assertEqual(client.post("/api/grafana/coverage", json={"project_id": pid}).status_code, 200)
                    self.assertEqual(client.post("/api/grafana/snapshot", json=body).status_code, 201)
                    inverted = {"project_id": pid, "part": {"part_start_s": 20, "part_end_s": 10}}
                    self.assertEqual(client.post("/api/grafana/part", json=inverted).status_code, 400)
                finally:
                    client.close()
                    server.shutdown()
                    server.server_close()

    def test_render_receipt_and_review_land_in_the_same_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                accepted = [{"range_s": [12.0, 13.5]}, {"range_s": [40.0, 41.0]}]
                covered = [bound for item in accepted for bound in item["range_s"]]
                receipt = {"id": "0123456789ab", "family_id": "abcdef123456"}
                receipt.update(o.part_context(pid, "abcdef123456", min(covered), max(covered)))
                self.assertEqual((receipt["part_start_s"], receipt["part_end_s"]), (12.0, 41.0))
                o.emit(pid, "candidate", {**receipt, "measurements": {"accepted_events": 2}}, timestamp=1000)
                o.emit(pid, "human_review", {
                    "candidate_id": receipt["id"], "verdict": "approved",
                    **{k: receipt[k] for k in ("part_start_s", "part_end_s", "family_id")},
                }, timestamp=1001)
                lens = o.part_lens(pid, o.part_context(pid, "abcdef123456", 12.0, 41.0))
                self.assertEqual(lens["row_count"], 2)
                self.assertEqual(
                    [(d["owner"], d["event"]) for d in lens["decisions"]],
                    [("agent", "candidate"), ("human", "human_review")],
                )
                self.assertEqual(lens["decisions"][-1]["decision"], "approved")

    def test_live_progress_reports_running_then_goes_idle_when_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                o.emit(pid, "movie_progress", {
                    "status": "analyzing", "name": "signal_scan",
                    "measurements": {"progress": 40, "scanned_s": 720, "duration_s": 1800},
                }, timestamp=time.time())
                text = o.metrics_text()
                self.assertIn('phase="signal_scan"', text)
                self.assertRegex(text, r"orpheus_live_running\{[^}]*\} 1")
                self.assertRegex(text, r"orpheus_live_progress\{[^}]*\} 40")

        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                o.emit(pid, "movie_progress", {
                    "status": "analyzing", "name": "signal_scan",
                    "measurements": {"progress": 55},
                }, timestamp=time.time() - o.LIVE_STALE_S - 60)
                self.assertRegex(o.metrics_text(), r"orpheus_live_running\{[^}]*\} 0")

    def test_finished_work_is_not_reported_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, c = self.store(tmp)
            with a, b, c:
                pid = "1234567890abcdef"
                o.emit(pid, "render_progress", {
                    "name": "complete", "status": "running",
                    "measurements": {"progress": 100},
                }, timestamp=time.time())
                self.assertRegex(o.metrics_text(), r"orpheus_live_running\{[^}]*\} 0")

    def test_dashboard_may_read_waveforms_but_not_mutate(self):
        import threading
        from http.server import ThreadingHTTPServer

        from orpheus.config import GRAFANA_PORTS
        from orpheus.domain import projects

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(projects, "ROOT", root),
                patch.object(projects, "PROJECTS", root / "projects"),
            ):
                from orpheus.server import web

                server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{server.server_port}"
                dash = f"http://127.0.0.1:{GRAFANA_PORTS['GRAFANA']}"
                client = httpx.Client(base_url=base, trust_env=False, timeout=20)
                try:
                    allowed = client.get("/api/projects", headers={"Origin": dash})
                    self.assertEqual(
                        allowed.headers.get("access-control-allow-origin"), dash
                    )
                    stranger = client.get(
                        "/api/projects", headers={"Origin": "http://evil.example"}
                    )
                    self.assertIsNone(stranger.headers.get("access-control-allow-origin"))
                    blocked = client.post(
                        "/api/grafana", json={}, headers={"Origin": dash}
                    )
                    self.assertEqual(blocked.status_code, 403)
                finally:
                    client.close()
                    server.shutdown()
                    server.server_close()

