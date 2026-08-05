"""Webhook monitor tests (Step 8): robustness, size caps, auth, shutdown."""

from __future__ import annotations

import http.client
import json
import socket
import time

import pytest

from observantic import WebhookEventBase
from observantic.exceptions import ConfigurationException


def request(port, method="POST", path="/webhook", body=None, headers=None, timeout=5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return (
            resp.status,
            data.decode("utf-8", errors="replace"),
            dict(resp.getheaders()),
        )
    finally:
        conn.close()


@pytest.fixture
def server(free_port):
    from pydantic import PrivateAttr

    class W(WebhookEventBase):
        _received: list = PrivateAttr(default_factory=list)
        _errors: list = PrivateAttr(default_factory=list)

        @property
        def received(self):
            return self._received

        @property
        def errors(self):
            return self._errors

        def on_webhook_received(self, event):
            self._received.append(event)

        def on_error(self, error, event=None):
            self._errors.append((error, event))

    w = W(port=free_port, host="127.0.0.1")
    w.start_watching()
    yield w, free_port
    w.stop_watching()


def test_valid_post_200_and_hook_event(server):
    w, port = server
    body = json.dumps({"a": 1})
    status, data, _ = request(
        port,
        body=body,
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert json.loads(data) == {"status": "ok"}
    assert wait_for(lambda: len(w.received) == 1)
    evt = w.received[0]
    assert evt.path == "/webhook"
    assert evt.method == "POST"
    assert evt.body == {"a": 1}
    assert evt.source_ip.startswith("127.0.0.1")


def test_invalid_content_length_returns_400(server):
    w, port = server
    status, data, _ = request(port, headers={"Content-Length": "abc"})
    assert status == 400
    assert "bad request" in data


def test_negative_content_length_returns_400(server):
    w, port = server
    status, data, _ = request(port, headers={"Content-Length": "-5"})
    assert status == 400


def test_missing_content_length_empty_body_event(server):
    """No Content-Length → 200 with an empty-body event (no silent read)."""
    w, port = server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(("127.0.0.1", port))
    sock.sendall(
        b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\r\n"
        + json.dumps({"real": "body"}).encode()
    )
    raw = sock.recv(4096)
    sock.close()
    assert b"200" in raw.split(b"\r\n", 1)[0]
    assert wait_for(lambda: len(w.received) == 1)
    assert w.received[0].body == b""  # body bytes silently ignored by design


def test_huge_content_length_returns_413_and_stays_responsive(server):
    w, port = server
    status, data, _ = request(port, headers={"Content-Length": str(10**12)})
    assert status == 413
    assert "too large" in data
    # Server still serves subsequent requests
    status2, _, _ = request(port, body=b"ok", headers={"Content-Length": "2"})
    assert status2 == 200


def test_over_max_body_bytes_returns_413(server):
    w, port = server
    w.max_body_bytes = 16
    status, data, _ = request(port, body=b"x" * 100, headers={"Content-Length": "100"})
    assert status == 413


def test_wedged_client_does_not_block_server(server):
    """C-05: an idle client claiming a huge Content-Length must not block
    other requests, and stop_watching() must return promptly."""
    w, port = server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(("127.0.0.1", port))
    sock.sendall(
        b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999999\r\n\r\n"
    )
    # wedged client sends nothing else; connection stays open

    # Second request still served.
    status, _, _ = request(port, body=b"hi", headers={"Content-Length": "2"})
    assert status == 200

    # stop_watching() returns in < 1 s.
    start = time.time()
    w.stop_watching()
    assert time.time() - start < 1.0
    sock.close()


def test_auth_mismatch_401_and_match_200(free_port):
    class W(WebhookEventBase):
        pass

    w = W(
        port=free_port,
        host="127.0.0.1",
        require_auth_header="X-API-Key",
        require_auth_value="secret-123",
    )
    w.start_watching()
    try:
        status, _, _ = request(port=free_port, headers={"X-API-Key": "wrong"})
        assert status == 401
        status2, data, _ = request(
            port=free_port,
            body=b"{}",
            headers={"X-API-Key": "secret-123", "Content-Length": "2"},
        )
        assert status2 == 200
        assert "ok" in data
    finally:
        w.stop_watching()


def test_auth_config_validated_together(free_port):
    w = WebhookEventBase(
        port=free_port,
        host="127.0.0.1",
        require_auth_header="X-API-Key",  # value missing → invalid
    )
    with pytest.raises(ConfigurationException, match="together"):
        w.start_watching()
    assert w._watching is False


def test_get_is_405_by_default(server):
    w, port = server
    status, data, _ = request(port, method="GET")
    assert status == 405


def test_unknown_path_404(server):
    w, port = server
    status, _, _ = request(port, path="/nope")
    assert status == 404


def test_query_params_are_url_decoded(server):
    w, port = server
    status, _, _ = request(
        port,
        path="/webhook?name=hello%20world&tags=a+b&blank=",
        body=b"",
        headers={},
    )
    assert status == 200
    assert wait_for(lambda: len(w.received) == 1)
    assert w.received[0].query_params == {
        "name": "hello world",
        "tags": "a b",
        "blank": "",
    }


def test_raising_hook_returns_generic_500_and_keeps_serving(free_port):
    """H-11: hook failure → generic 500, real error goes to on_error, the
    server keeps serving subsequent requests."""
    received = []
    errors = []

    class W(WebhookEventBase):
        def on_webhook_received(self, event):
            received.append(event)
            raise ValueError("secret internal detail")

        def on_error(self, error, event=None):
            errors.append((error, event))

    w = W(port=free_port, host="127.0.0.1")
    w.start_watching()
    try:
        status, data, _ = request(
            port=free_port, body=b"{}", headers={"Content-Length": "2"}
        )
        assert status == 500
        assert "secret internal detail" not in data  # no leakage
        assert json.loads(data) == {"error": "internal"}

        # on_error got the real error AND the event object (H-12)
        assert wait_for(lambda: len(errors) == 1)
        err, evt = errors[0]
        assert isinstance(err, ValueError)
        assert type(evt).__name__ == "WebhookEvent"
        assert evt.path == "/webhook"

        # Server keeps serving
        status2, _, _ = request(
            port=free_port, body=b"{}", headers={"Content-Length": "2"}
        )
        assert status2 == 500
        assert len(received) == 2
    finally:
        w.stop_watching()


def test_client_disconnect_no_traceback(free_port, caplog):
    """H-11: abrupt disconnect produces a debug log line, not a stack trace."""
    import logging

    w = WebhookEventBase(port=free_port, host="127.0.0.1")
    w.start_watching()
    try:
        with caplog.at_level(logging.DEBUG, logger="observantic.webhook"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", free_port))
            sock.sendall(
                b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nshort"
            )
            sock.close()  # disconnect mid-body
            time.sleep(0.5)
        assert "disconnected" in caplog.text or "socket error" in caplog.text
        # Server still up
        status, _, _ = request(
            port=free_port, body=b"{}", headers={"Content-Length": "2"}
        )
        assert status == 200
    finally:
        w.stop_watching()


def test_stop_watching_idempotent(server):
    w, port = server
    w.stop_watching()
    w.stop_watching()  # no-op
    assert w._watching is False


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
