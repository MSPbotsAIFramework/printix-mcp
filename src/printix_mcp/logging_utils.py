"""Structured, credential-safe logging for this service (PRD-21204 review round 1).

Every log line is a single JSON object on stdout so it can be ingested
without a custom parser, and every line carries the current request's
``request_id`` (bound once per HTTP request in ``auth_middleware.py``) so
the two call sites that log -- ``tools.py`` (tool name, outcome, latency)
and ``printix_client.py`` (Printix HTTP status class, mapped error code) --
can be correlated back into one call.

Per SOP S6/S11: never log request/response bodies, query parameters, or
any header value (especially ``X-Printix-Client-Id`` /
``X-Printix-Client-Secret`` / the bearer token) -- only the fields named in
each call site below.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("printix_mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
# Left at the default True (not False) so pytest's caplog fixture -- which
# attaches to the root logger -- can see these records in tests. Safe in
# production: nothing in this service's uvicorn/Starlette stack installs a
# root-logger handler, so propagation is a no-op there and prints nothing twice.

_request_id_var: ContextVar[str | None] = ContextVar("printix_request_id", default=None)


def new_request_id() -> str:
    """A short opaque id, unique enough to correlate one request's log lines."""
    return uuid.uuid4().hex[:12]


def bind_request_id(request_id: str) -> object:
    """Bind ``request_id`` for the current task; returns a token for ``reset_request_id``."""
    return _request_id_var.set(request_id)


def reset_request_id(token: object) -> None:
    _request_id_var.reset(token)  # type: ignore[arg-type]


def log_event(event: str, **fields: Any) -> None:
    """Emit one structured (JSON) log line tagged with the current request id.

    Callers must only pass the non-sensitive fields documented at each call
    site (tool name, HTTP status class, mapped error code, latency, page
    numbers) -- never credentials, header values, or request/response bodies.
    """
    payload: dict[str, Any] = {"event": event, "request_id": _request_id_var.get()}
    payload.update(fields)
    logger.info(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def elapsed_ms(start: float) -> int:
    return round((time.monotonic() - start) * 1000)


__all__ = ["log_event", "new_request_id", "bind_request_id", "reset_request_id", "elapsed_ms"]
