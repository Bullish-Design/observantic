#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.4.0",
#     "eventic>=1.1.0",
#     "requests>=2.31.0",
# ]
# ///
"""
Webhook server example for Observantic.
Receives HTTP POST/PUT webhooks and prints them; ships with an auth demo.
"""

from __future__ import annotations

import json
import threading
import time

import requests
from eventic import App, Stream
from eventic.sql import SQLite
from pydantic import BaseModel

from observantic import WebhookEventBase


class WebhookEvent(BaseModel):
    """One emitted webhook request (the stream's state model)."""

    path: str = ""
    method: str = ""
    headers: dict[str, str] = {}
    body: bytes | str | dict = b""
    source_ip: str = ""


webhooks = Stream(WebhookEvent, name="webhooks")
app = App(id="webhook-demo", streams=[webhooks])


class WebhookReceiver(WebhookEventBase):
    """Receive webhooks; each request emits a WebhookEvent."""

    port: int = 8888
    webhook_paths: list[str] = ["/webhook", "/api/event"]
    require_auth_header: str | None = "X-API-Key"
    require_auth_value: str | None = "secret-123"

    def on_webhook_received(self, event):
        try:
            if isinstance(event.body, dict):
                data = event.body
            else:
                data = json.loads(event.body)
            print(f"🔔 Webhook received: {data}")
        except (json.JSONDecodeError, TypeError):
            print(f"🔔 Non-JSON webhook: {str(event.body)[:50]}...")

    def on_start(self):
        print(f"Server running at http://localhost:{self.port}")
        print(f"Endpoints: {', '.join(self.webhook_paths)}")


def send_test_webhooks() -> None:
    """Send some test webhooks."""
    time.sleep(1)  # Wait for server startup

    print("\n📤 Sending test webhooks...")

    # Valid webhook
    try:
        r = requests.post(
            "http://localhost:8888/webhook",
            json={"event": "test", "value": 42},
            headers={"X-API-Key": "secret-123"},
        )
        print(f"  Response: {r.status_code}")
    except Exception as e:
        print(f"  Error: {e}")

    # Invalid auth
    try:
        r = requests.post(
            "http://localhost:8888/webhook",
            json={"event": "unauthorized"},
            headers={"X-API-Key": "wrong"},
        )
        print(f"  Unauthorized: {r.status_code} (expected 401)")
    except Exception as e:
        print(f"  Error: {e}")


def main():
    """Run webhook server demo."""
    print("🚀 Webhook Server Demo")
    print("Starting server on port 8888...")

    test_thread = threading.Thread(target=send_test_webhooks)
    test_thread.daemon = True
    test_thread.start()

    store = SQLite("webhooks.db")
    runtime = app.bind(store)
    server = WebhookReceiver(stream=webhooks, auto_persist=True)
    server.bind(runtime)
    server.start_watching()

    print("Press Ctrl+C to stop\n")
    try:
        test_thread.join()
        time.sleep(5)
    except KeyboardInterrupt:
        print("\n✅ Server stopped")
    finally:
        server.stop_watching()
        store.close()


if __name__ == "__main__":
    main()
