"""Bounded HTTP primitives for the loopback application."""

import json
import mimetypes
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler

from .. import config
from . import auth


def embedders():
    """Grafana Cloud iframes the panel pages, so name its origin as a frame parent."""
    from ..ops import observability as obs

    url = ((obs.config() or {}).get("dashboard_url") or "").strip()
    match = re.match(r"(https://[^/]+)", url)
    return match.group(1) if match else "'none'"


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class LocalHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        embed = self.path.startswith("/embed/")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'%s; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:%s; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors %s"
            % (
                " 'unsafe-inline'" if embed else "",
                # The panel player follows a signed redirect to object storage.
                " https://storage.googleapis.com" if embed else "",
                embedders() if embed else "'none'",
            ),
        )
        super().end_headers()

    def local_request(self, mutation=False):
        host = self.headers.get("Host", "").split(",", 1)[0].strip()
        if config.RUNTIME_MODE == "local":
            expected = f"127.0.0.1:{self.server.server_port}"
            if host != expected:
                raise RequestError("Open Orpheus using its 127.0.0.1 address.", 403)
            if mutation and self.headers.get("Origin") != "http://" + expected:
                raise RequestError("A same-origin local request is required.", 403)
            auth.resolve(self)
            return
        if host not in config.ALLOWED_HOSTS:
            raise RequestError("Request host is not configured for Orpheus.", 403)
        if mutation:
            origin = self.headers.get("Origin", "").rstrip("/")
            if origin not in config.ALLOWED_ORIGINS:
                raise RequestError("A configured same-origin request is required.", 403)
        auth.resolve(self)

    def send_bytes(self, data, content_type, status=200):
        from ..config import GRAFANA_PORTS

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        dashboard = f"http://127.0.0.1:{GRAFANA_PORTS['GRAFANA']}"
        if self.command in ("GET", "HEAD") and self.headers.get("Origin") == dashboard:
            self.send_header("Access-Control-Allow-Origin", dashboard)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, data, status=200):
        self.send_bytes(
            json.dumps(data, allow_nan=False).encode(), "application/json", status
        )

    def send_file(self, base, relative):
        path = (base / relative).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            raise RequestError("File not found.", 404)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            try:
                if not match or not any(match.groups()):
                    raise ValueError()
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    if int(last) <= 0:
                        raise ValueError()
                    start = max(0, size - int(last))
                if not 0 <= start <= end < size:
                    raise ValueError()
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def read_body(self, limit):
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            raise RequestError("One Content-Length is required.")
        try:
            length = int(lengths[0])
        except ValueError:
            raise RequestError("Invalid request length.") from None
        if length > limit:
            raise RequestError("Upload exceeds the configured size limit.", 413)
        if length <= 0:
            raise RequestError("Request body is empty.")
        try:
            data = self.rfile.read(length)
        except TimeoutError:
            raise RequestError("Upload timed out. Retry the upload.", 408) from None
        if len(data) != length:
            raise RequestError("Upload was interrupted.")
        return data

    def read_json(self, limit):
        if self.headers.get_content_type() != "application/json":
            raise RequestError("Send an application/json object.")

        def invalid_constant(value):
            raise RequestError("Numbers must be finite.")

        data = json.loads(self.read_body(limit), parse_constant=invalid_constant)
        if not isinstance(data, dict):
            raise RequestError("Expected a JSON object.")
        return data

    def read_file(self, limit, destination):
        """Stream one bounded request body to disk without buffering the upload."""
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            raise RequestError("One Content-Length is required.")
        try:
            length = int(lengths[0])
        except ValueError:
            raise RequestError("Invalid request length.") from None
        if not 0 < length <= limit:
            raise RequestError("Upload exceeds the configured size limit.", 413)
        destination = Path(destination)
        remaining = length
        try:
            with destination.open("wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RequestError("Upload was interrupted.")
                    output.write(chunk)
                    remaining -= len(chunk)
        except TimeoutError:
            destination.unlink(missing_ok=True)
            raise RequestError("Upload timed out. Retry the upload.", 408) from None
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination
