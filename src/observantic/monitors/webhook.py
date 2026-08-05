"""
HTTP webhook monitoring mixin.

Hardened server: ThreadingHTTPServer with daemon threads, bounded body reads
(Content-Length validation + size cap), socket timeouts, tracked connections
for a bounded shutdown, constant-time auth comparison, and no exception
leakage to clients (C-05, C-06, H-11, H-17).
"""

from __future__ import annotations

import hmac
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlparse

from pydantic import BaseModel, Field, PrivateAttr

from ..core import EventWatcher
from ..exceptions import ConfigurationException, WatcherException

logger = logging.getLogger("observantic.webhook")


@dataclass
class WebhookEvent:
    """Represents an incoming webhook event."""

    path: str
    method: str
    headers: dict[str, str]
    body: bytes | str | dict
    query_params: dict[str, str]
    timestamp: datetime
    source_ip: str


class WebhookRecord(BaseModel):
    """HTTP webhook event record."""

    path: str
    method: str
    headers: dict[str, str]
    body: bytes | str | dict
    timestamp: float = Field(default_factory=time.time)
    source_ip: str = ""

    model_config = {"frozen": True, "extra": "forbid", "arbitrary_types_allowed": True}


class _ConnectionTrackingMixIn:
    """Track live sockets so stop_watching() can unblock stuck handlers."""

    _connections: set[socket.socket] = set()
    _conn_lock: threading.Lock = threading.Lock()

    def process_request(self, request, client_address):
        with self._conn_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        finally:
            with self._conn_lock:
                self._connections.discard(request)

    def close_all_connections(self):
        with self._conn_lock:
            for sock in list(self._connections):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            self._connections.clear()


class _WebhookServer(_ConnectionTrackingMixIn, ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WebhookEventBase(EventWatcher):
    """HTTP webhook monitoring mixin."""

    port: int = Field(default=8080, description="Port to listen on")
    host: str = Field(default="0.0.0.0", description="Host to bind to")
    webhook_paths: list[str] = Field(
        default=["/webhook"], description="Paths to accept"
    )
    require_auth_header: str | None = Field(
        default=None, description="Header name for auth (e.g., 'X-API-Key')"
    )
    require_auth_value: str | None = Field(
        default=None, description="Expected auth header value"
    )
    parse_json_body: bool = Field(
        default=True, description="Auto-parse JSON request bodies"
    )
    max_body_bytes: int = Field(
        default=1_048_576, description="Max request body (413 above this)"
    )
    allowed_methods: list[str] = Field(
        default=["POST", "PUT"], description="HTTP methods accepted as webhooks"
    )

    _server: _WebhookServer | None = PrivateAttr(default=None)
    _server_thread: threading.Thread | None = PrivateAttr(default=None)

    # Hook errors become HTTP 500s (generic), never leak, never kill the server.
    raise_on_hook_error: bool = True

    # ---- state machine extension points ---------------------------------- #

    def start_watching(self, path: str | None = None, **kwargs: Any) -> None:
        """Begin monitoring for webhooks; `path` is ignored (host:port)."""
        super().start_watching(path or f"{self.host}:{self.port}", **kwargs)

    def _validate_start(self, path: str | None) -> None:
        if (self.require_auth_header is None) != (self.require_auth_value is None):
            raise ConfigurationException(
                "require_auth_header and require_auth_value must be set together"
            )

    def _start_impl(self, path: str | None = None, **kwargs: Any) -> None:
        handler_class = self._create_handler_class()
        try:
            self._server = _WebhookServer((self.host, self.port), handler_class)
        except Exception as e:
            raise WatcherException(f"Failed to start webhook server: {e}") from e
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._server_thread.start()

    def _stop_impl(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close_all_connections()  # unblock any stuck handler (C-05)
            server.shutdown()  # now returns promptly
            server.server_close()
        thread = self._server_thread
        self._server_thread = None
        if thread is not None:
            thread.join(timeout=5)

    def _default_record_model(self) -> type[Any]:
        return WebhookRecord

    # ---- handler --------------------------------------------------------- #

    def _create_handler_class(self):
        parent = self

        class WebhookHandler(BaseHTTPRequestHandler):
            timeout = 30  # socket read timeout → idle clients reaped (C-05)
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self._handle_request("GET")

            def do_POST(self):
                self._handle_request("POST")

            def do_PUT(self):
                self._handle_request("PUT")

            def handle(self) -> None:
                """Suppress stdlib tracebacks on keep-alive disconnects (H-11).

                ``BaseHTTPRequestHandler.handle`` reads the *next* request line
                after a response; an abrupt client close there raises inside
                socketserver's thread wrapper and prints a traceback. Catch and
                log it instead.
                """
                try:
                    super().handle()
                except (ConnectionError, BrokenPipeError, TimeoutError) as e:
                    logger.debug("webhook connection closed: %s", e)

            def _handle_request(self, method: str) -> None:
                try:
                    if method not in parent.allowed_methods:
                        self._send_json(405, {"error": "method not allowed"})
                        return

                    parsed_url = urlparse(self.path)
                    path = parsed_url.path
                    if path not in parent.webhook_paths:
                        self._send_json(404, {"error": "not found"})
                        return

                    if not self._authorized():
                        self._send_json(401, {"error": "unauthorized"})
                        return

                    status, body = self._read_body()
                    if status is not None:
                        message = "too large" if status == 413 else "bad request"
                        self._send_json(status, {"error": message})
                        return

                    parsed_body = self._parse_body(body)
                    query_params = dict(
                        parse_qsl(parsed_url.query, keep_blank_values=True)
                    )
                    headers = {k: v for k, v in self.headers.items()}

                    event = WebhookEvent(
                        path=path,
                        method=method,
                        headers=headers,
                        body=parsed_body,
                        query_params=query_params,
                        timestamp=datetime.now(),
                        source_ip=self.client_address[0],
                    )

                    parent._emit(
                        path=path,
                        method=method,
                        headers=headers,
                        body=parsed_body,
                        source_ip=self.client_address[0],
                    )

                    error = parent._dispatch_hook("on_webhook_received", event)
                    if error is not None:
                        # Generic 500 — real exception already logged via
                        # on_error (H-11).
                        self._send_json(500, {"error": "internal"})
                    else:
                        self._send_json(200, {"status": "ok"})
                except (ConnectionError, BrokenPipeError, TimeoutError):
                    logger.debug(
                        "webhook client disconnected during %s %s", method, self.path
                    )
                except OSError:  # TimeoutError is an OSError subclass
                    logger.debug("webhook socket error during %s %s", method, self.path)
                except Exception:
                    logger.exception(
                        "webhook handler error on %s %s", method, self.path
                    )
                    try:
                        self._send_json(500, {"error": "internal"})
                    except Exception:
                        pass

            def _read_body(self) -> tuple[int | None, bytes]:
                """Strict, capped Content-Length handling (C-06)."""
                raw = self.headers.get("Content-Length")
                if raw is None:
                    return None, b""  # explicit: no body, no silent read
                try:
                    length = int(raw)
                except ValueError:
                    return 400, b""  # invalid header → 400 (was: crash)
                if length < 0:
                    return 400, b""
                if length > parent.max_body_bytes:
                    return 413, b""  # too large → 413, no read
                return None, self.rfile.read(length)

            def _parse_body(self, body: bytes) -> bytes | str | dict:
                if not body:
                    return b""
                if parent.parse_json_body:
                    content_type = self.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        try:
                            return json.loads(body.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            return body.decode("utf-8", errors="ignore")
                    try:
                        return body.decode("utf-8")
                    except UnicodeDecodeError:
                        return body
                return body

            def _authorized(self) -> bool:
                if not parent.require_auth_header:
                    return True
                got = self.headers.get(parent.require_auth_header, "")
                return hmac.compare_digest(got, parent.require_auth_value or "")

            def _send_json(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args):
                logger.info("%s - %s", self.address_string(), format % args)

        return WebhookHandler
