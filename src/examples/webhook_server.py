#!/usr/bin/env python3
# /// script
# dependencies = [
#     "observantic @ git+https://github.com/Bullish-Design/observantic",
#     "eventic @ git+https://github.com/Bullish-Design/eventic",
#     "python-dotenv>=1.0.0",
#     "typer>=0.12.0",
# ]
# ///
"""
Production webhook server using Observantic.
Logs all webhooks to JSONL. Persistence to Eventic is optional and enabled
by calling observantic.init(...) with a reachable database.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from eventic import Record

from observantic import WebhookEventBase, init

app = typer.Typer()


class WebhookLogger(Record, WebhookEventBase):
    """Production webhook logger with JSONL output.

    All configuration is passed to the constructor (C-07) — never assigned
    on the class after creation.
    """

    # Eventic Record fields (defaults so no store/DB is required).
    endpoint: str = "/webhook"
    payload: dict | str = {}
    timestamp: float = 0.0

    # WebhookEventBase configuration (annotated overrides).
    port: int = 8000
    host: str = "0.0.0.0"
    webhook_paths: list[str] = ["/webhook", "/api/webhook"]
    require_auth_header: str | None = None
    require_auth_value: str | None = None
    parse_json_body: bool = True

    # Logger configuration — private, set via constructor kwargs.
    _log_file: Path = Path("/data/webhooks.jsonl")
    _request_count: int = 0

    def on_webhook_received(self, event):
        """Process and log incoming webhook."""
        self._request_count += 1

        try:
            # Create comprehensive log entry
            log_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
                f"✓ [{self._request_count}] {event.method} {event.path} "
                f"from {event.source_ip}"
            )
            print(f"  Headers: {len(event.headers)} | Body: {body_preview}")

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
        print("🚀 Webhook Server Started")
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
        """Log errors (never leaks to clients; see webhook monitor)."""
        error_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": str(error),
            "error_type": type(error).__name__,
            "event_path": getattr(event, "path", None) if event else None,
        }

        try:
            with open(self._log_file.parent / "errors.jsonl", "a") as f:
                f.write(json.dumps(error_entry) + "\n")
        except Exception:
            pass

        print(f"❌ Error: {error}")


# Global instance for signal handling
server_instance: WebhookLogger | None = None


def signal_handler(sig, frame):
    """Handle shutdown signals gracefully."""
    print("\n\n🛑 Shutdown signal received...")
    if server_instance and server_instance._watching:
        server_instance.stop_watching()
    sys.exit(0)


@app.command()
def main(
    port: int = typer.Option(
        8000, "--port", "-p", help="Port to listen on", envvar="WEBHOOK_PORT"
    ),
    host: str = typer.Option(
        "0.0.0.0", "--host", "-h", help="Host to bind to", envvar="WEBHOOK_HOST"
    ),
    paths: str = typer.Option(
        "/webhook",
        "--paths",
        help="Comma-separated webhook paths",
        envvar="WEBHOOK_PATHS",
    ),
    log_file: Path = typer.Option(
        Path("/data/webhooks.jsonl"),
        "--log-file",
        "-l",
        help="Path to JSONL log file",
        envvar="WEBHOOK_LOG_FILE",
    ),
    database_url: str = typer.Option(
        "postgresql://eventic_user:eventic_pass@localhost:5432/eventic_db",
        "--database-url",
        "-d",
        help="PostgreSQL database URL",
        envvar="DATABASE_URL",
    ),
    auth_header: str | None = typer.Option(
        None,
        "--auth-header",
        help="Required auth header name (e.g., X-API-Key)",
        envvar="WEBHOOK_AUTH_HEADER",
    ),
    auth_value: str | None = typer.Option(
        None,
        "--auth-value",
        help="Required auth header value",
        envvar="WEBHOOK_AUTH_VALUE",
    ),
    parse_json: bool = typer.Option(
        True,
        "--parse-json/--no-parse-json",
        help="Auto-parse JSON request bodies",
        envvar="WEBHOOK_PARSE_JSON",
    ),
):
    """Run production webhook server with Observantic."""
    global server_instance

    # Initialize Eventic (optional). If the database is unreachable the
    # server still runs — persistence is simply unavailable (auto_persist is
    # off by default; hooks receive events regardless).
    try:
        init(name="webhook-server", database_url=database_url)
        print(f"🔌 Eventic initialized @ {database_url}")
    except Exception as e:
        print(f"⚠️  Eventic unavailable ({e}); persistence disabled")

    # Configure the server entirely via constructor kwargs (C-07).
    server_instance = WebhookLogger(
        port=port,
        host=host,
        webhook_paths=[p.strip() for p in paths.split(",")],
        parse_json_body=parse_json,
        require_auth_header=auth_header,
        require_auth_value=auth_value,
    )
    # Private attributes are not init kwargs in pydantic 2.11; assign after
    # construction (private attrs are assignable even on frozen Records).
    server_instance._log_file = log_file

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
    app()
