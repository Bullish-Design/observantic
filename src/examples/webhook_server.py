#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic @ git+https://github.com/Bullish-Design/observantic",
#     "eventic @ git+https://github.com/Bullish-Design/eventic",
#     "python-dotenv>=1.0.0",
#     "click>=8.1.0",
# ]
# ///
"""
Production webhook server using Observantic.
Logs all webhooks to JSONL and persists to Eventic.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv
from eventic import Record, Eventic
from observantic import WebhookEventBase, init

load_dotenv()


class WebhookLogger(Record, WebhookEventBase):
    """Production webhook logger with JSONL output."""

    # Record fields
    endpoint: str = "/webhook"
    payload: dict | str = {}
    timestamp: float = 0.0

    # WebhookEventBase configuration (class variables)
    # These are set dynamically in main()
    port: int = 8000
    host: str = "0.0.0.0"
    webhook_paths: list[str] = ["/webhook", "/api/webhook"]
    require_auth_header: Optional[str] = None
    require_auth_value: Optional[str] = None
    parse_json_body: bool = True

    # Logger configuration
    _log_file: Path = Path("/data/webhooks.jsonl")
    _request_count: int = 0

    def on_webhook_received(self, event):
        """Process and log incoming webhook."""
        self._request_count += 1

        try:
            # Create comprehensive log entry
            log_entry = {
                "id": str(self.id),
                "version": self.version,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "request_number": self._request_count,
                "path": event.path,
                "method": event.method,
                "headers": event.headers,
                "source_ip": event.source_ip,
                "query_params": event.query_params,
                "body": event.body,
                "body_type": type(event.body).__name__,
                "body_size": len(str(event.body)),
            }

            # Ensure directory exists
            self._log_file.parent.mkdir(parents=True, exist_ok=True)

            # Append to JSONL
            with open(self._log_file, "a") as f:
                f.write(json.dumps(log_entry, default=str) + "\n")

            # Console output with details
            body_preview = self._format_body_preview(event.body)
            print(
                f"✓ [{self._request_count}] {event.method} {event.path} from {event.source_ip}"
            )
            print(f"  Headers: {len(event.headers)} | Body: {body_preview}")

            # Update record fields for persistence
            self.endpoint = event.path
            self.payload = (
                event.body if isinstance(event.body, dict) else str(event.body)
            )
            self.timestamp = time.time()

        except Exception as e:
            print(f"✗ Error processing webhook: {e}")
            self.on_error(e, event)

    def _format_body_preview(self, body, max_length=100):
        """Format body preview for console output."""
        if isinstance(body, dict):
            preview = json.dumps(body, separators=(",", ":"))
        else:
            preview = str(body)

        if len(preview) > max_length:
            return preview[:max_length] + "..."
        return preview

    def on_start(self):
        """Called when server starts."""
        print(f"\n{'=' * 50}")
        print(f"🚀 Webhook Server Started")
        print(f"{'=' * 50}")
        print(f"📡 Listening on: http://{self.host}:{self.port}")
        print(f"🔗 Endpoints: {', '.join(self.webhook_paths)}")
        print(f"📝 Logging to: {self._log_file}")
        if self.require_auth_header:
            print(f"🔐 Auth required: {self.require_auth_header}")
        print(f"{'=' * 50}\n")

    def on_stop(self):
        """Called when server stops."""
        print(f"\n✅ Server stopped after {self._request_count} requests")

    def on_error(self, error: Exception, event=None):
        """Log errors."""
        error_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "error": str(error),
            "error_type": type(error).__name__,
            "event_path": getattr(event, "path", None) if event else None,
        }

        try:
            with open(self._log_file.parent / "errors.jsonl", "a") as f:
                f.write(json.dumps(error_entry) + "\n")
        except:
            pass

        print(f"❌ Error: {error}")


# Global instance for signal handling
server_instance: Optional[WebhookLogger] = None


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    print("\n\n🛑 Shutdown signal received...")
    if server_instance and server_instance._watching:
        server_instance.stop_watching()
    sys.exit(0)


@click.command()
@click.option(
    "--port", default=8000, type=int, help="Port to listen on", envvar="WEBHOOK_PORT"
)
@click.option(
    "--host", default="0.0.0.0", help="Host to bind to", envvar="WEBHOOK_HOST"
)
@click.option(
    "--paths",
    default="/webhook,/api/webhook",
    help="Comma-separated webhook paths",
    envvar="WEBHOOK_PATHS",
)
@click.option(
    "--log-file",
    default="/data/webhooks.jsonl",
    type=click.Path(),
    help="Path to JSONL log file",
    envvar="WEBHOOK_LOG_FILE",
)
@click.option(
    "--database-url",
    default="postgresql://eventic_user:eventic_pass@pinix:5432/eventic_db",
    help="PostgreSQL database URL",
    envvar="DATABASE_URL",
)
@click.option(
    "--auth-header",
    help="Required auth header name (e.g., X-API-Key)",
    envvar="WEBHOOK_AUTH_HEADER",
)
@click.option(
    "--auth-value", help="Required auth header value", envvar="WEBHOOK_AUTH_VALUE"
)
@click.option(
    "--parse-json/--no-parse-json",
    default=True,
    help="Auto-parse JSON request bodies",
    envvar="WEBHOOK_PARSE_JSON",
)
def main(
    port, host, paths, log_file, database_url, auth_header, auth_value, parse_json
):
    """Run production webhook server with Observantic."""
    global server_instance

    # Initialize Eventic first
    print(f"🔌 Connecting to database...")
    eventic_instance = init(name="webhook-server", database_url=database_url)

    # Launch Eventic in standalone mode
    print(f"🚀 Launching Eventic...")
    eventic_instance.launch()

    # Configure WebhookLogger class variables
    WebhookLogger.port = port
    WebhookLogger.host = host
    WebhookLogger.webhook_paths = [p.strip() for p in paths.split(",")]
    WebhookLogger._log_file = Path(log_file)
    WebhookLogger.parse_json_body = parse_json

    if auth_header and auth_value:
        WebhookLogger.require_auth_header = auth_header
        WebhookLogger.require_auth_value = auth_value

    # Create server instance
    server_instance = WebhookLogger()

    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start watching (this starts the HTTP server in a background thread)
    server_instance.start_watching()

    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if server_instance._watching:
            server_instance.stop_watching()


if __name__ == "__main__":
    main()
