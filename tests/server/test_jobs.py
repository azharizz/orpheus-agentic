import unittest
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch

from orpheus.server import jobs, web


class JobChecks(unittest.TestCase):
    def test_payload_is_bounded_and_overrides_worker_args(self):
        data = jobs.payload("a" * 16, "make the alternate take")
        self.assertEqual(
            data["overrides"]["containerOverrides"][0]["args"],
            ["a" * 16, "--feedback", "make the alternate take"],
        )
        with self.assertRaises(ValueError):
            jobs.payload("bad", "x")
        with self.assertRaises(ValueError):
            jobs.payload("a" * 16, "")

    def test_cloud_runtime_dispatches_without_spawning_local_worker(self):
        with (
            patch.object(web.config, "RUNTIME_MODE", "cloud_run"),
            patch.object(web.projects, "load", return_value={"id": "a" * 16}),
            patch.object(web.jobs, "dispatch") as dispatch,
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web.start("a" * 16, "run")
        dispatch.assert_called_once_with("a" * 16, "run")
        popen.assert_not_called()

    def test_cloud_runtime_uses_agent_engine_when_configured(self):
        with (
            patch.object(jobs.config, "RUNTIME_MODE", "cloud_run"),
            patch.object(jobs.config, "GOOGLE_CLOUD_PROJECT", "orpheus-agentic"),
            patch.object(jobs.config, "AGENT_ENGINE_ID", "5166883865117065216"),
            patch.object(
                jobs,
                "_dispatch_agent_engine",
                return_value={"status": "submitted", "runtime": "agent_engine"},
            ) as dispatch,
        ):
            result = jobs.dispatch("a" * 16, "run")
        self.assertEqual(result["runtime"], "agent_engine")
        dispatch.assert_called_once_with("a" * 16, "run")

    def test_cloud_runtime_falls_back_to_job_when_agent_engine_is_unavailable(self):
        with (
            patch.object(jobs.config, "RUNTIME_MODE", "cloud_run"),
            patch.object(jobs.config, "GOOGLE_CLOUD_PROJECT", "orpheus-agentic"),
            patch.object(jobs.config, "AGENT_ENGINE_ID", "engine"),
            patch.object(jobs, "_dispatch_agent_engine", side_effect=RuntimeError),
            patch.object(
                jobs,
                "_dispatch_cloud_run",
                return_value={"status": "submitted", "operation": "op"},
            ) as dispatch,
        ):
            result = jobs.dispatch("a" * 16, "run", "b" * 16)
        self.assertEqual(result["runtime"], "cloud_run_fallback")
        dispatch.assert_called_once_with("a" * 16, "run", "b" * 16)

    def test_submission_result_can_be_found_in_runtime_event(self):
        result = jobs._submission(
            {"event": {"tool": {"response": {"status": "submitted", "operation": "op"}}}}
        )
        self.assertEqual(result["operation"], "op")

    def test_cloud_runtime_checks_configured_host_and_origin(self):
        handler = object.__new__(web.Handler)
        handler.server = SimpleNamespace(server_port=8080)
        handler.headers = Message()
        handler.headers["Host"] = "orpheus-agentic.web.app"
        handler.headers["Origin"] = "https://orpheus-agentic.web.app"
        with (
            patch.object(web.config, "RUNTIME_MODE", "cloud_run"),
            patch.object(web.config, "OWNER_SECRET", "test-secret"),
            patch.object(web.config, "ALLOWED_HOSTS", {"orpheus-agentic.web.app"}),
            patch.object(web.config, "ALLOWED_ORIGINS", {"https://orpheus-agentic.web.app"}),
        ):
            handler.local_request(mutation=True)
            handler.headers.replace_header("Origin", "https://evil.example")
            with self.assertRaises(web.RequestError):
                handler.local_request(mutation=True)


if __name__ == "__main__":
    unittest.main()
