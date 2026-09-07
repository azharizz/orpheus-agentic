import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orpheus.domain import projects


class Foundation(unittest.TestCase):
    def test_managed_session_events_have_invocation_ids(self):
        from google.adk.events import EventActions
        from orpheus.server.worker import session_event

        event = session_event(
            author="OrpheusEditor",
            actions=EventActions(state_delta={"diagnostic": True}),
        )
        self.assertTrue(event.invocation_id)

    def test_preparation_discloses_source_truncation_and_no_audio(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            video, sfx = (folder / "target.mp4", folder / "source.wav")
            projects.ff(
                "-f",
                "lavfi",
                "-i",
                "color=s=64x64:r=5",
                "-t",
                0.6,
                "-c:v",
                "libx264",
                video,
            )
            projects.ff(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=300:sample_rate=48000",
                "-t",
                31,
                sfx,
            )
            with patch.object(projects, "PROJECTS", folder / "projects"):
                p = projects.create(video, sfx)
            self.assertEqual(p["preparation"]["sfx_input_duration_s"], 31)
            self.assertTrue(p["preparation"]["sfx_truncated"])
            self.assertFalse(p["preparation"]["video_truncated"])
            self.assertIn("no_original_audio", p["input_warnings"])
            self.assertIn("source_truncated", p["input_warnings"])
            self.assertIn("mono_analysis_copy", p["input_warnings"])
            for name, source in [("video", video), ("sfx", sfx)]:
                self.assertEqual(
                    (
                        folder
                        / "projects"
                        / p["id"]
                        / p["preparation"]["original_files"][name]
                    ).read_bytes(),
                    source.read_bytes(),
                )

    def test_initialization_failure_is_persisted(self):
        import asyncio
        import json

        from orpheus.server import worker

        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            doc = {"id": "a" * 16, "status": "ready", "turns": []}
            projects.atomic(folder / "project.json", doc)
            with (
                patch.object(worker, "load", return_value=doc),
                patch.object(worker, "project_dir", return_value=folder),
                patch.object(
                    worker, "session_service", side_effect=RuntimeError("secret")
                ),
            ):
                result = asyncio.run(worker.run_turn("a" * 16, "test"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure"]["phase"], "session_initialization")
            self.assertEqual(
                json.loads((folder / "project.json").read_text())["status"], "failed"
            )
            self.assertNotIn("secret", (folder / "events.jsonl").read_text())

    def test_orphaned_turn_recovery(self):
        import json

        from orpheus.server import worker

        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            doc = {"id": "a" * 16, "status": "running", "turns": ["b" * 12]}
            projects.atomic(folder / "project.json", doc)
            with (
                patch.object(worker, "load", return_value=doc),
                patch.object(worker, "project_dir", return_value=folder),
            ):
                worker.recover_orphaned_turns("a" * 16)
            self.assertEqual(
                json.loads((folder / ("b" * 12 + "-turn.json")).read_text())["status"],
                "interrupted",
            )
            self.assertEqual(
                json.loads((folder / "project.json").read_text())["status"],
                "interrupted",
            )

    def test_cleanup_failure_is_persisted(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock

        from orpheus.server import worker

        service = AsyncMock()
        service.get_session.side_effect = ValueError("secret")
        service.close.side_effect = RuntimeError("secret")
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            doc = {
                "id": "a" * 16,
                "status": "ready",
                "turns": [],
                "prepared_hashes": {},
            }
            projects.atomic(folder / "project.json", doc)
            with (
                patch.object(worker, "load", return_value=doc),
                patch.object(worker, "project_dir", return_value=folder),
                patch.object(worker, "session_service", return_value=service),
            ):
                result = asyncio.run(worker.run_turn("a" * 16, "test"))
            self.assertEqual(result["failure"]["phase"], "cleanup")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                json.loads((folder / "project.json").read_text())["status"], "failed"
            )
            self.assertNotIn("secret", str(result))

    def test_oversized_summaries_are_rejected_before_work(self):
        from types import SimpleNamespace

        from orpheus.agent.workflow import build

        with tempfile.TemporaryDirectory() as d:
            agent = build({}, Path(d), lambda *a, **k: None)
            tools = {t.__name__: t for t in agent.sub_agents[0].tools}
            ctx = SimpleNamespace(state={})
            self.assertIn(
                "error", tools["render_plan"](-22, "balanced", "x" * 1001, ctx)
            )
            self.assertIn(
                "error", tools["finish"]("", "unsuitable", "x" * 3001, [], ctx)
            )
            self.assertIn(
                "error", tools["finish"]("", "unsuitable", "reason", ["x"] * 16, ctx)
            )

    def test_failure_categories_do_not_include_exception_text(self):
        from google.adk.agents.invocation_context import LlmCallsLimitExceededError

        from orpheus.server.worker import failure_info

        for exc, phase, expected in [
            (LlmCallsLimitExceededError("secret"), "agent", "model_call_budget"),
            (TimeoutError("secret"), "agent", "turn_timeout"),
            (subprocess.TimeoutExpired("secret", 1), "overview", "media_timeout"),
            (subprocess.CalledProcessError(1, "secret"), "agent", "media_processing"),
            (RuntimeError("secret"), "agent", "unexpected_failure"),
        ]:
            result = failure_info(exc, phase)
            self.assertEqual(result["category"], expected)
            self.assertNotIn("secret", str(result))
        self.assertEqual(
            failure_info(RuntimeError("secret"), "agent", True)["category"],
            "provider_failure",
        )

    def test_inspection_discloses_limits(self):
        from orpheus.agent.workflow_common import inspection_scope

        result = inspection_scope(180, 180, 12, 9)
        self.assertTrue(result["candidate_cap_may_have_omitted_events"])
        self.assertEqual(result["audio_event_cap"], 100)
        self.assertEqual(result["source_option_cap"], 24)
        self.assertEqual(result["plan_row_limit"], 100)
        self.assertEqual(result["review_centers_per_cycle"], 20)
        self.assertEqual(result["review_centers_per_turn"], 100)
        self.assertEqual(result["previous_candidates_omitted_from_view"], 4)
        self.assertEqual(result["project_notes_in_view"], 9)
        self.assertFalse(
            inspection_scope(2, 3, 1, 0)["candidate_cap_may_have_omitted_events"]
        )


if __name__ == "__main__":
    unittest.main()
