"""Tool-catalogue snapshot, error-code mapping, pagination, and tenant
resolution -- all against a mocked Printix HTTP layer (respx), no network.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from starlette.types import ASGIApp

from printix_mcp.auth_middleware import HEADER_CLIENT_ID, HEADER_CLIENT_SECRET, HEADER_TENANT_ID
from printix_mcp.server import create_app


class _LifespanManager:
    """Drives ASGI lifespan startup/shutdown around an app under test.

    ``httpx.ASGITransport`` talks plain HTTP scopes only -- it never sends the
    ``lifespan`` events a real server (uvicorn) sends, so ``create_app()``'s
    ``lifespan=lambda app: mcp.session_manager.run()`` (the exact wiring
    SOP S7 requires) never starts its task group without this.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._receive_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._startup_complete = asyncio.Event()
        self._shutdown_complete = asyncio.Event()

    async def _receive(self) -> dict:
        return await self._receive_queue.get()

    async def _send(self, message: dict) -> None:
        if message["type"] == "lifespan.startup.complete":
            self._startup_complete.set()
        elif message["type"] == "lifespan.shutdown.complete":
            self._shutdown_complete.set()

    async def __aenter__(self) -> _LifespanManager:
        self._task = asyncio.create_task(self.app({"type": "lifespan"}, self._receive, self._send))
        await self._receive_queue.put({"type": "lifespan.startup"})
        await self._startup_complete.wait()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._receive_queue.put({"type": "lifespan.shutdown"})
        await self._shutdown_complete.wait()
        await self._task

TOKEN_URL = "https://auth.printix.net/oauth/token"
API_BASE = "https://api.printix.net/cloudprint"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
TENANT_B_ID = "22222222-2222-2222-2222-222222222222"

_BASE_HEADERS = {
    HEADER_CLIENT_ID: "placeholder-client-id",
    HEADER_CLIENT_SECRET: "placeholder-secret",
    HEADER_TENANT_ID: TENANT_ID,
}


async def _no_sleep(_seconds: float) -> None:
    """Replaces the client's retry backoff so the 429/5xx test doesn't sleep for real."""


def _mock_token() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "test-token", "expires_in": 3599})
    )


async def _post_mcp(payload: dict, headers: dict[str, str]) -> httpx.Response:
    app = create_app()
    async with _LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/mcp", json=payload, headers={"Accept": "application/json, text/event-stream", **headers}
            )


async def _call_tool(headers: dict[str, str], name: str, arguments: dict[str, object] | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return _parse_mcp_response(await _post_mcp(payload, headers))


async def _list_tools() -> list[dict]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    body = _parse_mcp_response(await _post_mcp(payload, _BASE_HEADERS))
    return body["result"]["tools"]


def _parse_mcp_response(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    text = response.text
    if text.startswith("event:"):
        # Streamable HTTP SSE framing: "event: message\ndata: {...}\n\n"
        line = next(l for l in text.splitlines() if l.startswith("data: "))
        return json.loads(line[len("data: ") :])
    return response.json()


def _tool_result_payload(body: dict) -> dict:
    """Unwrap a tools/call JSON-RPC envelope down to our own error/data JSON."""
    content = body["result"]["content"]
    assert len(content) == 1
    return json.loads(content[0]["text"])


# -- tools/list snapshot (SOP S10) -------------------------------------------------


async def test_tools_list_snapshot() -> None:
    tools = await _list_tools()
    names = {t["name"] for t in tools}
    assert names == {
        "printix_list_printers",
        "printix_get_printer",
        "printix_list_sites",
        "printix_get_site",
        "printix_list_workstations",
        "printix_get_workstation",
    }
    for tool in tools:
        assert len(tool["description"]) <= 500
        assert len(tool["description"].splitlines()[0]) <= 100
        assert tool["annotations"]["readOnlyHint"] is True


async def test_get_printer_required_args_in_schema() -> None:
    tools = {t["name"]: t for t in await _list_tools()}
    assert tools["printix_get_printer"]["inputSchema"]["required"] == ["printer_id"]
    assert tools["printix_get_site"]["inputSchema"]["required"] == ["site_id"]
    assert tools["printix_get_workstation"]["inputSchema"]["required"] == ["workstation_id"]


# -- AC4: missing headers --------------------------------------------------------------


async def test_missing_credential_headers_returns_401_with_required_list() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = await _post_mcp(payload, headers={})
    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "missing_required_headers"
    assert set(body["required"]) == {HEADER_CLIENT_ID, HEADER_CLIENT_SECRET}


# -- AC10: /health never requires credentials or calls Printix ----------------------------


async def test_health_endpoint_does_not_require_printix_credentials() -> None:
    app = create_app()
    async with _LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# -- AC1/AC8: list printers, pagination and empty results ----------------------------------


@respx.mock
async def test_list_printers_returns_paginated_queues() -> None:
    _mock_token()
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/printers").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "message": "OK",
                "page": {"number": 0, "size": 1, "totalElements": 2, "totalPages": 2},
                "printers": [
                    {
                        "id": "q-1",
                        "name": "Queue 1",
                        "connectionStatus": "ONLINE",
                        "location": "Office",
                    }
                ],
            },
        )
    )
    body = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_list_printers", {"limit": 1}))
    assert body["printers"] == [{"id": "q-1", "name": "Queue 1", "status": "ONLINE", "location": "Office"}]
    assert body["has_more"] is True
    assert body["next_cursor"] == "1"


@respx.mock
async def test_empty_tenant_returns_empty_list_not_error() -> None:
    _mock_token()
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/sites").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "page": {"number": 0, "size": 50, "totalElements": 0, "totalPages": 1}, "sites": []},
        )
    )
    body = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_list_sites"))
    assert body == {"sites": [], "has_more": False, "next_cursor": None}


# -- AC2/AC3: get by id, found and not found ------------------------------------------------


@respx.mock
async def test_get_printer_returns_full_properties() -> None:
    _mock_token()
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/printers").mock(
        return_value=httpx.Response(
            200,
            json={
                "page": {"number": 0, "size": 50, "totalElements": 1, "totalPages": 1},
                "printers": [
                    {
                        "id": "q-1",
                        "name": "Queue 1",
                        "connectionStatus": "ONLINE",
                        "location": "Office",
                        "model": "Anywhere Printer",
                        "vendor": "Printix",
                        "serialNo": "123",
                        "printerSignId": "ABC",
                        "capabilities": {"printer": {"copies": {"max": 100}}},
                    }
                ],
            },
        )
    )
    body = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_get_printer", {"printer_id": "q-1"}))
    assert body["id"] == "q-1"
    assert body["status"] == "ONLINE"
    assert body["capabilities"] == {"printer": {"copies": {"max": 100}}}


@respx.mock
async def test_get_unknown_id_returns_not_found_for_each_resource() -> None:
    _mock_token()
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/printers").mock(
        return_value=httpx.Response(
            200, json={"page": {"number": 0, "size": 200, "totalElements": 0, "totalPages": 1}, "printers": []}
        )
    )
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/sites/missing-site").mock(
        return_value=httpx.Response(404, json={"success": False, "message": "Not found"})
    )
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/workstations/missing-ws").mock(
        return_value=httpx.Response(404, json={"success": False, "message": "Not found"})
    )

    printer = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_get_printer", {"printer_id": "missing"}))
    site = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_get_site", {"site_id": "missing-site"}))
    workstation = _tool_result_payload(
        await _call_tool(_BASE_HEADERS, "printix_get_workstation", {"workstation_id": "missing-ws"})
    )

    for result in (printer, site, workstation):
        assert result["error"]["code"] == "not_found"
        assert result["error"]["retryable"] is False


# -- AC5: Printix itself rejects the credentials ------------------------------------------


@respx.mock
async def test_printix_rejects_credentials_returns_structured_unauthorized() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    body = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_list_printers"))
    assert body == {"error": {"code": "unauthorized", "message": "Printix rejected the provided client_id/client_secret pair.", "retryable": False}}


# -- AC6/AC7: tenant auto-resolution -----------------------------------------------------


@respx.mock
async def test_single_accessible_tenant_is_auto_resolved() -> None:
    _mock_token()
    respx.get(API_BASE).mock(
        return_value=httpx.Response(
            200,
            json={
                "_links": {
                    "acme.printix.net": {
                        "href": f"{API_BASE}/tenants/{TENANT_ID}/printers?page=0&pageSize=10",
                        "templated": True,
                    }
                }
            },
        )
    )
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/printers").mock(
        return_value=httpx.Response(
            200, json={"page": {"number": 0, "size": 50, "totalElements": 0, "totalPages": 1}, "printers": []}
        )
    )
    headers = {HEADER_CLIENT_ID: "cid", HEADER_CLIENT_SECRET: "secret"}  # no tenant header
    body = _tool_result_payload(await _call_tool(headers, "printix_list_printers"))
    assert body == {"printers": [], "has_more": False, "next_cursor": None}


@respx.mock
async def test_multiple_accessible_tenants_without_header_returns_invalid_argument() -> None:
    _mock_token()
    respx.get(API_BASE).mock(
        return_value=httpx.Response(
            200,
            json={
                "_links": {
                    "acme.printix.net": {"href": f"{API_BASE}/tenants/{TENANT_ID}/printers", "templated": True},
                    "beta.printix.net": {"href": f"{API_BASE}/tenants/{TENANT_B_ID}/printers", "templated": True},
                }
            },
        )
    )
    headers = {HEADER_CLIENT_ID: "cid", HEADER_CLIENT_SECRET: "secret"}  # no tenant header
    body = _tool_result_payload(await _call_tool(headers, "printix_list_printers"))
    assert body["error"]["code"] == "invalid_argument"
    assert TENANT_ID in body["error"]["message"]
    assert TENANT_B_ID in body["error"]["message"]


# -- error-code mapping sweep (rate limit, upstream 5xx) ------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_code", "expected_retryable"),
    [
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (503, "upstream_error", True),
    ],
)
@respx.mock
async def test_error_status_codes_map_to_expected_envelope(
    status: int, expected_code: str, expected_retryable: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("printix_mcp.printix_client.asyncio.sleep", _no_sleep)
    _mock_token()
    route = respx.get(f"{API_BASE}/tenants/{TENANT_ID}/workstations")
    route.mock(return_value=httpx.Response(status, json={"message": "nope"}))
    body = _tool_result_payload(await _call_tool(_BASE_HEADERS, "printix_list_workstations"))
    assert body["error"]["code"] == expected_code
    assert body["error"]["retryable"] is expected_retryable
    # retryable codes get retried up to _MAX_RETRIES extra times before giving up
    if expected_retryable:
        assert route.call_count >= 2


# -- PRD-21204 review round 1 must-fix: structured per-call logging, no secrets ------------


@respx.mock
async def test_successful_tool_call_logs_completion_without_credentials(caplog: pytest.LogCaptureFixture) -> None:
    _mock_token()
    respx.get(f"{API_BASE}/tenants/{TENANT_ID}/printers").mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "page": {"number": 0, "size": 50, "totalElements": 0, "totalPages": 1}, "printers": []},
        )
    )
    with caplog.at_level("INFO", logger="printix_mcp"):
        await _call_tool(_BASE_HEADERS, "printix_list_printers")

    tool_events = [json.loads(r.message) for r in caplog.records if r.name == "printix_mcp"]
    tool_call = next(e for e in tool_events if e["event"] == "tool_call")
    assert tool_call["tool"] == "printix_list_printers"
    assert tool_call["error_code"] is None
    assert isinstance(tool_call["latency_ms"], int)
    assert tool_call["request_id"]  # non-empty: set once per request by CredentialsMiddleware

    http_call = next(e for e in tool_events if e["event"] == "printix_http_call")
    assert http_call["status_class"] == "2xx"
    assert http_call["request_id"] == tool_call["request_id"]  # same request, correlatable

    # Never the credential values, never the bearer token, never a response body field.
    full_text = "\n".join(r.message for r in caplog.records if r.name == "printix_mcp")
    assert _BASE_HEADERS[HEADER_CLIENT_SECRET] not in full_text
    assert "test-token" not in full_text  # the mocked access_token from _mock_token()


@respx.mock
async def test_rejected_credentials_logs_unauthorized_without_leaking_secret(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    with caplog.at_level("INFO", logger="printix_mcp"):
        await _call_tool(_BASE_HEADERS, "printix_list_printers")

    events = [json.loads(r.message) for r in caplog.records if r.name == "printix_mcp"]
    tool_call = next(e for e in events if e["event"] == "tool_call")
    assert tool_call["error_code"] == "unauthorized"
    token_call = next(e for e in events if e["event"] == "printix_http_call")
    assert token_call["status_class"] == "4xx"
    assert token_call["error_code"] == "unauthorized"

    full_text = "\n".join(r.message for r in caplog.records if r.name == "printix_mcp")
    assert _BASE_HEADERS[HEADER_CLIENT_SECRET] not in full_text
