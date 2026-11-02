"""The six read-only Printix tools exposed to the Agent Platform.

Each tool builds a fresh ``PrintixClient`` from the credentials the
middleware bound to this request (see ``auth_middleware.get_credentials``),
uses it exactly once, and closes it -- nothing is cached or reused across
calls (SOP S3.4).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .auth_middleware import get_credentials
from .printix_client import PrintixClient, PrintixError

_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

_READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, destructiveHint=False)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _error_envelope(exc: PrintixError) -> str:
    return _dumps(exc.to_envelope())


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_LIMIT))


def _page_from_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        page = int(cursor)
    except ValueError:
        return 0
    return max(page, 0)


def _has_more(page_info: dict[str, Any]) -> bool:
    number = page_info.get("number", 0)
    total_pages = page_info.get("totalPages", 1)
    return (number + 1) < total_pages


def _paginated_result(key: str, items: list[dict[str, Any]], page_info: dict[str, Any], page: int) -> dict[str, Any]:
    has_more = _has_more(page_info)
    return {
        key: items,
        "has_more": has_more,
        "next_cursor": str(page + 1) if has_more else None,
    }


async def _new_client() -> PrintixClient:
    creds = get_credentials()
    return PrintixClient(creds.client_id, creds.client_secret, creds.tenant_id)


def register(mcp: FastMCP) -> None:
    """Register all Printix tools on the given FastMCP app."""

    @mcp.tool(
        description=(
            "List a Printix tenant's print queues.\n"
            "Args: limit (max rows, default 50, max 200), cursor (opaque page token from a previous call's next_cursor)."
        ),
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def printix_list_printers(limit: int = _DEFAULT_LIMIT, cursor: str | None = None) -> str:
        page = _page_from_cursor(cursor)
        page_size = _clamp_limit(limit)
        try:
            async with await _new_client() as client:
                body = await client.list_printers(page=page, page_size=page_size)
        except PrintixError as exc:
            return _error_envelope(exc)

        printers = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "status": p.get("connectionStatus"),
                "location": p.get("location"),
            }
            for p in body.get("printers", [])
        ]
        return _dumps(_paginated_result("printers", printers, body.get("page") or {}, page))

    @mcp.tool(
        description="Get one Printix print queue's full properties and capabilities by id.\n"
        "Args: printer_id (required, from printix_list_printers).",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def printix_get_printer(printer_id: str) -> str:
        try:
            async with await _new_client() as client:
                printer = await client.get_printer(printer_id)
        except PrintixError as exc:
            return _error_envelope(exc)

        if printer is None:
            return _dumps(
                {"error": {"code": "not_found", "message": f"No print queue '{printer_id}' was found in this tenant.", "retryable": False}}
            )
        return _dumps(
            {
                "id": printer.get("id"),
                "name": printer.get("name"),
                "status": printer.get("connectionStatus"),
                "location": printer.get("location"),
                "model": printer.get("model"),
                "vendor": printer.get("vendor"),
                "serial_no": printer.get("serialNo"),
                "printer_sign_id": printer.get("printerSignId"),
                "capabilities": printer.get("capabilities"),
            }
        )

    @mcp.tool(
        description=(
            "List a Printix tenant's sites.\n"
            "Args: limit (max rows, default 50, max 200), cursor (opaque page token from a previous call's next_cursor)."
        ),
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def printix_list_sites(limit: int = _DEFAULT_LIMIT, cursor: str | None = None) -> str:
        page = _page_from_cursor(cursor)
        page_size = _clamp_limit(limit)
        try:
            async with await _new_client() as client:
                body = await client.list_sites(page=page, page_size=page_size)
        except PrintixError as exc:
            return _error_envelope(exc)

        sites = [{"id": s.get("id"), "name": s.get("name"), "path": s.get("path")} for s in body.get("sites", [])]
        return _dumps(_paginated_result("sites", sites, body.get("page") or {}, page))

    @mcp.tool(
        description="Get one Printix site's details by id.\n"
        "Args: site_id (required, from printix_list_sites).",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def printix_get_site(site_id: str) -> str:
        try:
            async with await _new_client() as client:
                site = await client.get_site(site_id)
        except PrintixError as exc:
            return _error_envelope(exc)

        if site is None:
            return _dumps(
                {"error": {"code": "not_found", "message": f"No site '{site_id}' was found in this tenant.", "retryable": False}}
            )
        return _dumps(
            {
                "id": site.get("id"),
                "name": site.get("name"),
                "path": site.get("path"),
                "setup_finished": site.get("setupFinished"),
                "created": site.get("created"),
                "modified": site.get("modified"),
            }
        )

    @mcp.tool(
        description=(
            "List a Printix tenant's workstations.\n"
            "Args: limit (max rows, default 50, max 200), cursor (opaque page token from a previous call's next_cursor)."
        ),
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def printix_list_workstations(limit: int = _DEFAULT_LIMIT, cursor: str | None = None) -> str:
        page = _page_from_cursor(cursor)
        page_size = _clamp_limit(limit)
        try:
            async with await _new_client() as client:
                body = await client.list_workstations(page=page, page_size=page_size)
        except PrintixError as exc:
            return _error_envelope(exc)

        workstations = [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "active": w.get("active"),
                "last_connect_time": w.get("lastConnectTime"),
            }
            for w in body.get("workstations", [])
        ]
        return _dumps(_paginated_result("workstations", workstations, body.get("page") or {}, page))

    @mcp.tool(
        description="Get one Printix workstation's details by id.\n"
        "Args: workstation_id (required, from printix_list_workstations).",
        annotations=_READ_ONLY,
        structured_output=False,
    )
    async def printix_get_workstation(workstation_id: str) -> str:
        try:
            async with await _new_client() as client:
                workstation = await client.get_workstation(workstation_id)
        except PrintixError as exc:
            return _error_envelope(exc)

        if workstation is None:
            return _dumps(
                {
                    "error": {
                        "code": "not_found",
                        "message": f"No workstation '{workstation_id}' was found in this tenant.",
                        "retryable": False,
                    }
                }
            )
        return _dumps(
            {
                "id": workstation.get("id"),
                "name": workstation.get("name"),
                "active": workstation.get("active"),
                "last_connect_time": workstation.get("lastConnectTime"),
                "last_disconnect_time": workstation.get("lastDisconnectTime"),
            }
        )


__all__ = ["register"]
