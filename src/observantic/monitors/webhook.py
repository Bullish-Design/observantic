"""
HTTP webhook monitoring mixin.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict
from urllib.parse import urlparse

from pydantic import BaseModel

from ..core.base import EventWatcher
from ..exceptions import WatcherException


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


class WebhookEventBase(EventWatcher):
    """HTTP webhook monitoring mixin."""
    
    # Configuration
    port: int = 8080
    host: str = "0.0.0.0"
    webhook_paths: list[str] = ["/webhook"]  # Paths to accept
    require_auth_header: str | None = None  # e.g., "X-API-Key"
    require_auth_value: str | None = None
    parse_json_body: bool = True
    
    _server: HTTPServer | None = None
    _server_thread: threading.Thread | None = None
    
    def start_watching(self, path: str | None = None, **kwargs) -> None:
        """
        Begin monitoring for webhooks.
        Path parameter is ignored - uses host:port instead.
        """
        super().start_watching(path or f"{self.host}:{self.port}")
        
        try:
            # Create HTTP server with custom handler
            handler_class = self._create_handler_class()
            self._server = HTTPServer((self.host, self.port), handler_class)
            
            # Run server in background thread
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True
            )
            self._server_thread.start()
            
        except Exception as e:
            self._watching = False
            raise WatcherException(
                f"Failed to start webhook server: {e}"
            ) from e
    
    def stop_watching(self) -> None:
        """Stop webhook server."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
            self._server_thread = None
        
        super().stop_watching()
    
    def _create_handler_class(self):
        """Create custom handler class with access to self."""
        parent = self
        
        class WebhookHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                """Handle POST requests."""
                self._handle_request("POST")
            
            def do_GET(self):
                """Handle GET requests."""
                self._handle_request("GET")
            
            def do_PUT(self):
                """Handle PUT requests."""
                self._handle_request("PUT")
            
            def _handle_request(self, method: str):
                """Process incoming webhook request."""
                parsed_url = urlparse(self.path)
                path = parsed_url.path
                
                # Check if path is allowed
                if path not in parent.webhook_paths:
                    self.send_error(404, "Not Found")
                    return
                
                # Check authentication if required
                if parent.require_auth_header:
                    auth_value = self.headers.get(parent.require_auth_header)
                    if auth_value != parent.require_auth_value:
                        self.send_error(401, "Unauthorized")
                        return
                
                # Read body
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                
                # Parse body if JSON
                parsed_body: bytes | str | dict = body
                if parent.parse_json_body and body:
                    content_type = self.headers.get('Content-Type', '')
                    if 'application/json' in content_type:
                        try:
                            parsed_body = json.loads(body.decode('utf-8'))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            parsed_body = body.decode('utf-8', errors='ignore')
                    else:
                        try:
                            parsed_body = body.decode('utf-8')
                        except UnicodeDecodeError:
                            pass
                
                # Parse query parameters
                query_params = {}
                if parsed_url.query:
                    for param in parsed_url.query.split('&'):
                        if '=' in param:
                            key, value = param.split('=', 1)
                            query_params[key] = value
                
                # Create event
                event = WebhookEvent(
                    path=path,
                    method=method,
                    headers=dict(self.headers),
                    body=parsed_body,
                    query_params=query_params,
                    timestamp=datetime.now(),
                    source_ip=self.client_address[0]
                )
                
                # Dispatch to hook
                try:
                    parent._dispatch_hook("on_webhook_received", event)
                    
                    # Send success response
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"status": "ok"}')
                    
                except Exception as e:
                    # Send error response
                    self.send_error(500, str(e))
            
            def log_message(self, format, *args):
                """Override to suppress default logging."""
                # Can be customized or pass to parent's logging
                pass
        
        return WebhookHandler
    
    # Override in subclasses
    def on_webhook_received(self, event: WebhookEvent) -> None:
        """Called when webhook is received."""
        pass
