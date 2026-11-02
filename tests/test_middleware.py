"""CredentialsMiddleware: header extraction, 401 on missing headers, and
the ContextVar reset/isolation that AC9 (multi-tenant isolation) rests on.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from printix_mcp.auth_middleware import (
    HEADER_CLIENT_ID,
    HEADER_CLIENT_SECRET,
    HEADER_TENANT_ID,
    CredentialsMiddleware,
    get_credentials,
)


async def _echo_credentials(_request: Request) -> JSONResponse:
    creds = get_credentials()
    return JSONResponse({"client_id": creds.client_id, "client_secret": creds.client_secret, "tenant_id": creds.tenant_id})


def _app() -> Starlette:
    app = Starlette(routes=[Route("/probe", _echo_credentials)])
    # Wrap the whole app so CredentialsMiddleware guards the one route under test.
    app.add_middleware(CredentialsMiddleware)
    return app


async def _get(headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/probe", headers=headers)


async def test_missing_credential_headers_returns_401_with_required_list() -> None:
    response = await _get({})
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "missing_required_headers"
    assert HEADER_CLIENT_ID in body["required"]
    assert HEADER_CLIENT_SECRET in body["required"]


async def test_missing_only_secret_still_returns_401_and_lists_just_that_header() -> None:
    response = await _get({HEADER_CLIENT_ID: "abc"})
    assert response.status_code == 401
    assert response.json()["required"] == [HEADER_CLIENT_SECRET]


async def test_credentials_land_in_context_and_tenant_header_is_optional() -> None:
    response = await _get({HEADER_CLIENT_ID: "cid", HEADER_CLIENT_SECRET: "secret"})
    assert response.status_code == 200
    assert response.json() == {"client_id": "cid", "client_secret": "secret", "tenant_id": None}


async def test_tenant_header_is_read_when_present() -> None:
    response = await _get({HEADER_CLIENT_ID: "cid", HEADER_CLIENT_SECRET: "secret", HEADER_TENANT_ID: "tenant-a"})
    assert response.json()["tenant_id"] == "tenant-a"


def test_get_credentials_raises_outside_any_request_context() -> None:
    with pytest.raises(LookupError):
        get_credentials()


async def test_concurrent_requests_from_different_tenants_do_not_leak() -> None:
    """TC9: two concurrent requests, two different credential pairs, each
    must only ever observe its own tenant's credentials -- never a mix.
    """

    seen: dict[str, str | None] = {}

    async def handler(request: Request) -> JSONResponse:
        creds = get_credentials()
        # Yield control mid-request so the two calls genuinely interleave;
        # if credentials were shared state, this is where they would leak.
        await asyncio.sleep(0.01)
        seen[request.headers[HEADER_CLIENT_ID]] = creds.client_id
        return JSONResponse({"client_id": creds.client_id})

    app = Starlette(routes=[Route("/probe", handler)])
    app.add_middleware(CredentialsMiddleware)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        results = await asyncio.gather(
            http.get("/probe", headers={HEADER_CLIENT_ID: "tenant-a", HEADER_CLIENT_SECRET: "s"}),
            http.get("/probe", headers={HEADER_CLIENT_ID: "tenant-b", HEADER_CLIENT_SECRET: "s"}),
        )

    bodies = [json.loads(r.text) for r in results]
    client_ids = {b["client_id"] for b in bodies}
    assert client_ids == {"tenant-a", "tenant-b"}
    assert seen == {"tenant-a": "tenant-a", "tenant-b": "tenant-b"}

    # And the ContextVar must not leak outside of any request afterward.
    with pytest.raises(LookupError):
        get_credentials()
