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

    def test_cloud_runtime_checks_configured_host_and_origin(self):
        handler = object.__new__(web.Handler)
        handler.server = SimpleNamespace(server_port=8080)
        handler.headers = Message()
        handler.headers["Host"] = "orpheus-agentic.web.app"
        handler.headers["Origin"] = "https://orpheus-agentic.web.app"
        with (
            patch.object(web.config, "RUNTIME_MODE", "cloud_run"),
            patch.object(web.config, "ALLOWED_HOSTS", {"orpheus-agentic.web.app"}),
            patch.object(web.config, "ALLOWED_ORIGINS", {"https://orpheus-agentic.web.app"}),
        ):
            handler.local_request(mutation=True)
            handler.headers.replace_header("Origin", "https://evil.example")
            with self.assertRaises(web.RequestError):
                handler.local_request(mutation=True)


if __name__ == "__main__":
    unittest.main()
