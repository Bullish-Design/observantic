#!/usr/bin/env python
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "observantic",
#     "eventic>=0.1.5",
#     "requests>=2.31.0",
# ]
# ///
"""
Webhook monitoring demonstration for Observantic.
"""

from __future__ import annotations

import time
import requests
import threading
from datetime import datetime

from eventic import Eventic, Record
from observantic import WebhookEventBase


# Initialize Eventic
Eventic.init(
    name="webhook-demo",
    database_url="postgresql://user:pass@localhost/webhook_demo"
)


class WebhookRecord(Record, WebhookEventBase):
    """Monitor webhooks and create Records for each request."""
    
    endpoint: str
    method: str
    payload: dict | str | None = None
    source_ip: str
    timestamp: datetime
    
    # Webhook configuration
    port = 8888
    webhook_paths = ["/webhook", "/api/webhook"]
    require_auth_header = "X-API-Key"
    require_auth_value = "secret-key-123"
    
    def on_webhook_received(self, event):
        """Create Record for each webhook."""
        print(f"🔔 Webhook received: {event.method} {event.path}")
        print(f"   From: {event.source_ip}")
        print(f"   Body: {event.body}")
        
        WebhookRecord(
            endpoint=event.path,
            method=event.method,
            payload=event.body,
            source_ip=event.source_ip,
            timestamp=event.timestamp
        )
    
    def on_start(self):
        """Called when webhook server starts."""
        print(f"🌐 Webhook server started on port {self.port}")
        print(f"   Endpoints: {', '.join(self.webhook_paths)}")
        print(f"   Auth required: {'Yes' if self.require_auth_header else 'No'}\n")


class GithubWebhook(Record, WebhookEventBase):
    """Example: GitHub webhook monitoring."""
    
    event_type: str  # push, pull_request, etc.
    repository: str
    user: str
    action: str | None = None
    
    port = 8889
    webhook_paths = ["/github"]
    
    def on_webhook_received(self, event):
        """Process GitHub webhooks."""
        if not isinstance(event.body, dict):
            return
        
        # Extract GitHub event type from headers
        event_type = event.headers.get("X-GitHub-Event", "unknown")
        
        # Extract common fields
        repo = event.body.get("repository", {}).get("full_name", "unknown")
        user = event.body.get("sender", {}).get("login", "unknown")
        action = event.body.get("action")
        
        print(f"🐙 GitHub event: {event_type}")
        print(f"   Repository: {repo}")
        print(f"   User: {user}")
        if action:
            print(f"   Action: {action}")
        
        GithubWebhook(
            event_type=event_type,
            repository=repo,
            user=user,
            action=action
        )


def send_test_webhooks():
    """Send test webhook requests."""
    time.sleep(1)  # Wait for server to start
    
    print("\n📤 Sending test webhooks...\n")
    
    # Test 1: Valid webhook with auth
    try:
        response = requests.post(
            "http://localhost:8888/webhook",
            json={"message": "Hello from test!", "user": "alice"},
            headers={"X-API-Key": "secret-key-123"}
        )
        print(f"✅ Test 1: {response.status_code} - Valid webhook")
    except Exception as e:
        print(f"❌ Test 1 failed: {e}")
    
    time.sleep(0.5)
    
    # Test 2: Invalid auth
    try:
        response = requests.post(
            "http://localhost:8888/webhook",
            json={"message": "Unauthorized attempt"},
            headers={"X-API-Key": "wrong-key"}
        )
        print(f"🔒 Test 2: {response.status_code} - Invalid auth (expected)")
    except Exception as e:
        print(f"❌ Test 2 failed: {e}")
    
    time.sleep(0.5)
    
    # Test 3: Different endpoint
    try:
        response = requests.post(
            "http://localhost:8888/api/webhook",
            json={"action": "update", "id": 123},
            headers={"X-API-Key": "secret-key-123"}
        )
        print(f"✅ Test 3: {response.status_code} - Alternative endpoint")
    except Exception as e:
        print(f"❌ Test 3 failed: {e}")
    
    time.sleep(0.5)
    
    # Test 4: GitHub-style webhook
    try:
        response = requests.post(
            "http://localhost:8889/github",
            json={
                "action": "opened",
                "pull_request": {"number": 42},
                "repository": {"full_name": "user/repo"},
                "sender": {"login": "developer"}
            },
            headers={"X-GitHub-Event": "pull_request"}
        )
        print(f"✅ Test 4: {response.status_code} - GitHub webhook")
    except Exception as e:
        print(f"❌ Test 4 failed: {e}")


def main():
    """Run the webhook demonstration."""
    print("🚀 Observantic Webhook Demo\n")
    
    # Start webhook monitors
    webhook_monitor = WebhookRecord()
    webhook_monitor.start_watching()
    
    github_monitor = GithubWebhook()
    github_monitor.start_watching()
    
    # Send test webhooks in background
    test_thread = threading.Thread(target=send_test_webhooks)
    test_thread.start()
    
    # Let webhooks process
    test_thread.join()
    time.sleep(1)
    
    # Show results
    print("\n📊 Results:")
    webhook_records = WebhookRecord._store.find_by_properties({})
    github_records = GithubWebhook._store.find_by_properties({})
    
    print(f"  - General webhooks received: {len(webhook_records)}")
    print(f"  - GitHub webhooks received: {len(github_records)}")
    
    # Show some records
    if webhook_records:
        record = WebhookRecord.hydrate(webhook_records[0])
        print(f"\n📝 Sample webhook record:")
        print(f"   Endpoint: {record.endpoint}")
        print(f"   Method: {record.method}")
        print(f"   Payload: {record.payload}")
    
    # Cleanup
    webhook_monitor.stop_watching()
    github_monitor.stop_watching()
    
    print("\n✅ Demo complete!")


if __name__ == "__main__":
    main()
