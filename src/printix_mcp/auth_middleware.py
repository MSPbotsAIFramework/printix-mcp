"""Per-request Printix credential handling.

Credentials arrive on every request as HTTP headers and are never cached,
never written to disk, and never shared across requests. This is the
mechanical basis for AC9 (multi-tenant isolation): the request-scoped
``ContextVar`` is the only place a credential tuple ever lives, and it is
reset in a ``finally`` block regardless of outcome.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

HEADER_CLIENT_ID = "X-Printix-Client-Id"
HEADER_CLIENT_SECRET = "X-Printix-Client-Secret"
HEADER_TENANT_ID = "X-Printix-Tenant-Id"

_REQUIRED_HEADERS = (HEADER_CLIENT_ID, HEADER_CLIENT_SECRET)


@dataclass(frozen=True, slots=True)
class PrintixCredentials:
    """The credential tuple for exactly one in-flight request."""

    client_id: str
    client_secret: str
    tenant_id: str | None


_credentials_var: ContextVar[PrintixCredentials | None] = ContextVar("printix_credentials", default=None)


def get_credentials() -> PrintixCredentials:
    """Read the credentials set by the middleware for the current request.

    Raises ``LookupError`` if called outside of a request that passed
    through ``CredentialsMiddleware`` -- that should be unreachable in
    production because the middleware guards every ``/mcp`` call, but
    tests exercise this function directly too.
    """
    creds = _credentials_var.get()
    if creds is None:
        raise LookupError("no Printix credentials bound to the current request context")
    return creds


class CredentialsMiddleware:
    """ASGI middleware: extract Printix credential headers or reject with 401.

    Implemented as raw ASGI (not Starlette's ``BaseHTTPMiddleware``) so the
    ``ContextVar`` set here is visible to the tool call that runs later in
    the same request -- ``BaseHTTPMiddleware`` can run the downstream
    response body in a different task and break that propagation.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        client_id = request.headers.get(HEADER_CLIENT_ID)
        client_secret = request.headers.get(HEADER_CLIENT_SECRET)
        tenant_id = request.headers.get(HEADER_TENANT_ID) or None

        received = {HEADER_CLIENT_ID: client_id, HEADER_CLIENT_SECRET: client_secret}
        missing = [name for name in _REQUIRED_HEADERS if not received[name]]
        if missing:
            body = json.dumps({"error": "missing_required_headers", "required": missing}).encode("utf-8")
            response = Response(content=body, status_code=401, media_type="application/json")
            await response(scope, receive, send)
            return

        assert client_id is not None
        assert client_secret is not None
        token = _credentials_var.set(
            PrintixCredentials(client_id=client_id, client_secret=client_secret, tenant_id=tenant_id)
        )
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(token)


__all__ = [
    "HEADER_CLIENT_ID",
    "HEADER_CLIENT_SECRET",
    "HEADER_TENANT_ID",
    "PrintixCredentials",
    "CredentialsMiddleware",
    "get_credentials",
]
