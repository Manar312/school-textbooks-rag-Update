"""One ASGI middleware that gives every request: an id, a log line, and Prometheus numbers.

It is a pure ASGI middleware (not `@app.middleware("http")`) because that style buffers streaming responses
badly; ours measures the *whole* time of an SSE answer, until the last byte is sent.
"""
import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import metrics

log = logging.getLogger("access")
_SKIP_METRICS = {"/metrics", "/health/live"}  # scrapes and probes would drown real traffic


class RequestMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = (headers.get(b"x-request-id") or b"").decode()[:64] or uuid.uuid4().hex[:12]
        method, started, status = scope["method"], time.perf_counter(), 500
        metrics.HTTP_IN_PROGRESS.inc()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.HTTP_IN_PROGRESS.dec()
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            # use the route TEMPLATE (/chat/sessions/{sid}/ask), never the real path: real paths contain UUIDs
            # and would create one Prometheus series per student chat (a "cardinality explosion").
            path = getattr(route, "path", None) or "unmatched"
            if scope["path"] not in _SKIP_METRICS:
                metrics.HTTP_REQUESTS.labels(method, path, str(status)).inc()
                metrics.HTTP_LATENCY.labels(method, path).observe(elapsed)
                log.info("%s %s -> %s %.0fms rid=%s", method, scope["path"], status, elapsed * 1000, rid)
