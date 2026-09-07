"""Bounded HTTP primitives for the loopback application."""

import json
import mimetypes
import re
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler

from .. import config
from . import auth


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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
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
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "owner_cookie", None):
            self.send_header("Set-Cookie", auth.cookie_header(self.owner_cookie))
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

    def read_form(self, limit, allowed):
        content_type = self.headers.get("Content-Type", "")
        if self.headers.get_content_type() != "multipart/form-data":
            raise RequestError("Choose files using the upload form.")
        # ponytail: one bounded upload in memory; use streaming multipart for larger local limits.
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + self.read_body(limit)
        )
        if not message.is_multipart() or message.defects:
            raise RequestError("Malformed multipart upload.")
        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if (
                name not in allowed
                or name in fields
                or part.is_multipart()
                or part.defects
            ):
                raise RequestError("Unexpected or repeated upload field.")
            if part.get_content_disposition() != "form-data":
                raise RequestError("Invalid upload field.")
            filename = part.get_filename()
            raw = part.get_payload(decode=True)
            if raw is None:
                raise RequestError("Invalid upload content.")
            fields[name] = (
                (filename, raw) if filename is not None else raw.decode("utf-8")
            )
        return fields
