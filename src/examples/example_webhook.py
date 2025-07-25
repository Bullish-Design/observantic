#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic>=0.2.0",
#     "eventic>=0.1.5",
#     "requests>=2.31.0",
# ]
# ///
"""
Webhook server example for Observantic.
Demonstrates receiving HTTP POST webhooks.
"""

from __future__ import annotations

import json
import threading
import time

import requests
from eventic import Record
from observantic import WebhookEventBase, init


# Initialize Eventic
init(name="webhook-demo", database_url="postgresql://user:pass@localhost/demo")


class WebhookEvent(Record, WebhookEventBase):
    """Receive webhooks and store as Records."""

    endpoint: str
    payload: dict | str
    timestamp: float

    # Configure server
    port = 8888
    webhook_paths = ["/webhook", "/api/event"]
    require_auth_header = "X-API-Key"
    require_auth_value = "secret-123"

    def on_webhook_received(self, event):
        """Process received webhook."""
        try:
            if isinstance(event.body, dict):
                data = event.body
            else:
                data = json.loads(event.body)
            print(f"🔔 Webhook received: {data}")
        except (json.JSONDecodeError, TypeError):
            print(f"🔔 Non-JSON webhook: {event.body[:50]}...")

    def on_start(self):
        """Called when server starts."""
        print(f"Server running at http://localhost:{self.port}")
        print(f"Endpoints: {', '.join(self.webhook_paths)}")


def send_test_webhooks():
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

    # Start test sender in background
    test_thread = threading.Thread(target=send_test_webhooks)
    test_thread.daemon = True
    test_thread.start()

    server = WebhookEvent()
    server.start_watching()

    print("Press Ctrl+C to stop\n")

    try:
        test_thread.join()  # Wait for tests
        time.sleep(5)  # Keep running a bit
    except KeyboardInterrupt:
        print("\n✅ Server stopped")
    finally:
        server.stop_watching()


if __name__ == "__main__":
    main()
