"""Real loopback HTTP coverage, using disposable generated media only."""

import io
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import httpx

from orpheus.domain import families, movie, projects, takes
from orpheus.server import web
from orpheus.server.http import LocalHandler
from tests.support import load_case


class HttpChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="orpheus-http-")
        root = Path(cls.folder.name)
        cls.patches = [
            patch.object(projects, "ROOT", root),
            patch.object(projects, "PROJECTS", root / "projects"),
        ]
        for item in cls.patches:
            item.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.client = httpx.Client(
            base_url=cls.base, headers={"Origin": cls.base}, trust_env=False, timeout=60
        )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        for item in reversed(cls.patches):
            item.stop()
        cls.folder.cleanup()

    def test_origin_routes_and_config(self):
        for route in ("/v1/", "/v2/", "/legacy-lab/", "/sessions.sqlite", "/.env"):
            self.assertEqual(self.client.get(route).status_code, 404)
        self.assertEqual(
            self.client.get(
                "/api/projects", headers={"Host": "evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/run", json={}, headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post("/api/run", json=[]).status_code, 400)
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["storage"], "local")
        self.assertNotIn("key", response.text.lower())

    def test_delete_project_requires_same_origin_and_removes_local_media(self):
        pid = "0123456789abcdef"
        folder = projects.project_dir(pid)
        folder.mkdir(parents=True)
        (folder / "project.json").write_text("{}")
        response = self.client.delete(
            f"/api/projects/{pid}", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 403, response.text)
        response = self.client.delete(f"/api/projects/{pid}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"deleted": pid})
        self.assertFalse(folder.exists())

    def test_movie_routes_are_deterministic_and_return_review_state(self):
        source = load_case()
        doc = projects.create(source["video_path"])
        pid = doc["id"]
        response = self.client.get("/api/movie", params={"project_id": pid})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_started")
        with patch.object(movie, "analyze", return_value={"status": "review_required"}):
            response = self.client.post("/api/movie/analyze", json={"project_id": pid})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "review_required")
        self.assertEqual(self.client.post("/api/movie/run", json={"project_id": pid}).status_code, 404)

    def test_family_search_route_delegates_to_guarded_domain_action(self):
        with patch.object(families, "search", return_value={"status": "review_required"}) as search:
            response = self.client.post(
                "/api/families/search",
                json={"project_id": "1234567890abcdef", "family_id": "footsteps"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        search.assert_called_once_with("1234567890abcdef", "footsteps")

    def test_family_example_route_records_human_range(self):
        with patch.object(families, "add_example", return_value={"id": "footsteps"}) as add:
            response = self.client.post("/api/families/examples", json={
                "project_id": "1234567890abcdef", "family_id": "footsteps", "range_s": [10, 10.5],
            })
        self.assertEqual(response.status_code, 201, response.text)
        add.assert_called_once_with("1234567890abcdef", "footsteps", [10, 10.5])

    def test_match_preview_route_builds_a_bounded_audition(self):
        with patch.object(families, "preview_match", return_value={"id": "a" * 12}) as preview:
            response = self.client.post(
                "/api/families/preview",
                json={"project_id": "1234567890abcdef", "family_id": "footsteps", "match_id": "match-1"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        preview.assert_called_once_with("1234567890abcdef", "footsteps", "match-1")

    def test_candidate_waveform_maps_movie_clock_to_part_clock(self):
        case = {"id": "1234567890abcdef"}
        with (
            patch.object(projects, "load", return_value=case),
            patch.object(web.review, "candidate", return_value={"timeline_offset_s": 154.34, "preview_duration_s": 15}),
            patch.object(web.media, "waveform", return_value={"peaks": [], "start_s": 5.49, "end_s": 15}) as waveform,
        ):
            response = self.client.get("/api/waveform", params={
                "project_id": case["id"], "role": "candidate",
                "candidate_id": "c21e7cc06e83", "start_s": 159.83,
                "end_s": 170, "bins": 100,
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertAlmostEqual(waveform.call_args.args[2], 5.49)
        self.assertAlmostEqual(waveform.call_args.args[3], 15)
        self.assertAlmostEqual(response.json()["start_s"], 159.83)
        self.assertAlmostEqual(response.json()["end_s"], 169.34)

    def test_prepare_take_and_seek_without_inference(self):
        case = load_case()
        with patch.object(web, "start") as start:
            response = self.client.post(
                "/api/projects",
                params={"filename": "scene.mp4", "context": "A synthetic test scene"},
                content=case["video_path"].read_bytes(),
                headers={"Content-Type": "video/mp4"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            doc = response.json()["project"]
            pid = doc["id"]
            self.assertEqual(doc["turns"], [])
            start.assert_not_called()
            for name in ("video.mp4", "original.wav"):
                response = self.client.get(
                    f"/projects/{pid}/{name}", headers={"Range": "bytes=0-99"}
                )
                self.assertEqual(response.status_code, 206)
                self.assertEqual(len(response.content), 100)
            self.assertEqual(
                self.client.get(
                    f"/projects/{pid}/video.mp4", headers={"Range": "bytes=-0"}
                ).status_code,
                416,
            )
            self.assertEqual(
                self.client.get(f"/projects/{pid}/originals/video.mp4").status_code, 404
            )
            self.assertEqual(
                self.client.get(f"/projects/{pid}/poster.jpg").status_code, 200
            )
            response = self.client.get(
                "/api/waveform", params={"project_id": pid, "role": "original"}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["peaks"])
            response = self.client.get(
                "/api/waveform",
                params={"project_id": pid, "role": "original", "start_s": 1, "end_s": 2, "bins": 32},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["start_s"], 1)
            self.assertLessEqual(len(response.json()["peaks"]), 32)
            for _ in range(100):
                status = self.client.get(
                    "/api/families", params={"project_id": pid}
                ).json()["index"]
                if status["status"] == "ready":
                    break
                time.sleep(0.02)
            self.assertEqual(status["status"], "ready")
            response = self.client.post(
                "/api/families",
                json={
                    "project_id": pid,
                    "name": "Footsteps",
                    "seed_range_s": [0.1, 0.5],
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            family_id = response.json()["id"]
            response = self.client.post(
                "/api/takes",
                params={
                    "filename": "take.wav",
                    "project_id": pid,
                    "family_id": family_id,
                    "start_s": "0",
                    "brief": "New take",
                },
                content=case["sfx_path"].read_bytes(),
                headers={"Content-Type": "audio/wav"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            tid = response.json()["id"]
            self.assertEqual(
                self.client.get(f"/projects/{pid}/takes/{tid}.wav").status_code, 200
            )
            self.assertEqual(
                len(
                    self.client.get("/api/takes", params={"project_id": pid}).json()[
                        "takes"
                    ]
                ),
                1,
            )
            start.assert_not_called()
            response = self.client.post("/api/run", json={"project_id": pid})
            self.assertEqual(response.status_code, 409, response.text)
            start.assert_not_called()
            response = self.client.post(
                "/api/run", json={"project_id": pid, "family_id": family_id}
            )
            self.assertEqual(response.status_code, 409, response.text)
            start.assert_not_called()
            response = self.client.post(
                "/api/run",
                json={"project_id": pid, "family_id": family_id, "consent": True},
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("Grafana MCP", response.json()["error"])
            start.assert_not_called()
            with (
                patch.object(web.obs, "config", return_value={"mcp_url": "local"}),
                patch(
                    "orpheus.agent.provider.provider_config",
                    side_effect=ValueError(
                        "AGENT_PROVIDER_API_KEY missing from Orpheus/.env"
                    ),
                ),
            ):
                response = self.client.post(
                    "/api/run",
                    json={"project_id": pid, "family_id": family_id, "consent": True},
                )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("AGENT_PROVIDER_API_KEY", response.json()["error"])
            with (
                patch.object(web.obs, "config", return_value={"mcp_url": "local"}),
                patch("orpheus.agent.provider.provider_config", return_value=("https://openrouter.ai/api/v1", "test")),
            ):
                response = self.client.post(
                    "/api/run",
                    json={"project_id": pid, "family_id": family_id, "consent": True},
                )
            self.assertEqual(response.status_code, 202, response.text)
            start.assert_called_once_with(pid, family_id, web.DEFAULT_FEEDBACK)

    def test_duplicate_upload_field_rejected(self):
        response = self.client.post(
            "/api/projects?filename=a.mp4&filename=b.mp4",
            content=b"x",
            headers={"Content-Type": "video/mp4"},
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_file_body_is_read_in_bounded_chunks(self):
        class Tracked(io.BytesIO):
            largest = 0

            def read(self, size=-1):
                self.largest = max(self.largest, size)
                return super().read(size)

        body = Tracked(b"x" * (3 * 1024 * 1024 + 7))
        headers = Message()
        headers["Content-Length"] = str(len(body.getbuffer()))
        handler = object.__new__(LocalHandler)
        handler.headers, handler.rfile = headers, body
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "upload"
            handler.read_file(len(body.getbuffer()), output)
            self.assertEqual(output.stat().st_size, len(body.getbuffer()))
        self.assertLessEqual(body.largest, 1024 * 1024)

    def test_review_identity_and_removed_assisted_revision(self):
        source = load_case()
        doc = projects.create(source["video_path"])
        families.build_index(doc["id"])
        family = families.create(doc["id"], "shoe", [0.4, 0.9])
        takes.add_take(doc["id"], source["sfx_path"], family_id=family["id"])
        rendered = families.render(doc["id"], family["id"])
        folder = projects.project_dir(doc["id"])
        payload = {"project_id": doc["id"], "candidate_id": rendered["id"]}
        response = self.client.post(
            "/api/review", json={**payload, "verdict": "approve"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["audio_sha256"], rendered["audio_sha256"])
        self.assertEqual(
            self.client.post("/api/assist", json={**payload, "rows": []}).status_code,
                404,
            )
        receipt_path = folder / (rendered["id"] + ".json")
        receipt = receipt_path.read_text()
        invalid = json.loads(receipt)
        invalid["schema"] = "arrangement-render.v1"
        receipt_path.write_text(json.dumps(invalid))
        self.assertEqual(
            self.client.post(
                "/api/review", json={**payload, "verdict": "reject"}
            ).status_code,
            400,
        )
        receipt_path.write_text(receipt)
        with (folder / (rendered["id"] + ".wav")).open("ab") as stream:
            stream.write(b"changed")
        response = self.client.post(
            "/api/review", json={**payload, "verdict": "reject"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"],
            "Invalid input. Check the selected project, values and file types.",
        )

    def test_long_upload_starts_background_preparation(self):
        case = load_case()
        project = {"id": "1234567890abcdef", "seconds": 300, "status": "preparing"}
        with patch.object(projects, "intake", return_value=project), patch.object(web, "start_prepare") as start_prepare:
            response = self.client.post(
                "/api/projects?filename=movie.mp4",
                content=case["video_path"].read_bytes(),
                headers={"Content-Type": "video/mp4"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["project"]["status"], "preparing")
        start_prepare.assert_called_once_with(project["id"], "local")


if __name__ == "__main__":
    unittest.main()
