"""Structured JSON logs, request ids and Prometheus metrics."""
import json
import logging
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.requests import Request

log = logging.getLogger("ledger")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "route"])
LEDGER_TRANSACTIONS = Counter("ledger_transactions_total", "Money movements", ["type", "outcome"])
LEDGER_REJECTIONS = Counter("ledger_rejections_total", "Business-rule rejections", ["code"])
OUTBOX_DELIVERIES = Counter("outbox_deliveries_total", "Outbox delivery attempts", ["result"])


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                "msg": record.getMessage()}
        data.update(getattr(record, "fields", {}))
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("ledger")
    if not any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level.upper())
    root.propagate = False


def event(msg: str, **fields) -> None:
    log.info(msg, extra={"fields": fields})


def route_template(request: Request) -> str:
    """'/api/v1/accounts/{account_id}' instead of the raw URL, so metric labels stay bounded."""
    if request.scope.get("route") is None:
        return "unmatched"
    # Nested routers only expose their relative path, so rebuild the template from the URL:
    # swap each matched path parameter's value for its {name}.
    path = request.url.path
    for name, value in request.path_params.items():
        path = path.replace(str(value), "{" + name + "}")
    return path


async def observe_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        elapsed = time.perf_counter() - started
        route = route_template(request)
        HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(elapsed)
        if route not in ("/health", "/metrics"):
            event("request", request_id=request_id, method=request.method, route=route,
                  path=request.url.path, status=status, ms=round(elapsed * 1000, 1),
                  client_id=getattr(request.state, "client_id", None))
