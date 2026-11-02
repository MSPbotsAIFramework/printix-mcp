"""FastMCP app factory: the Streamable HTTP /mcp endpoint plus /health.

See SOP S7 for the two pitfalls this module exists to avoid: DNS-rebinding
protection must be off (the service sits behind an internal proxy, not on
localhost), and the MCP app's lifespan must be wired to the *outer*
Starlette app explicitly -- ``Mount()`` alone does not run a sub-app's
lifespan, which otherwise surfaces as "/mcp" failing with "Task group is
not initialized" while "/health" keeps returning 200.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .auth_middleware import CredentialsMiddleware
from .tools import register as register_tools

SERVICE_INSTRUCTIONS = (
    "Printix is cloud print management for an MSP's managed tenants. This service "
    "exposes read-only lookups for a tenant's print queues (printers), sites, and "
    "workstations. Typical flow: call a printix_list_* tool to find an id, then "
    "printix_get_* with that id for full details. All tools require the "
    "X-Printix-Client-Id and X-Printix-Client-Secret headers for the target "
    "tenant's Printix Administrator API credentials; X-Printix-Tenant-Id is only "
    "needed when that credential pair can see more than one Printix tenant. "
    "This service never submits, modifies, or deletes anything in Printix."
)


async def _health(_request: Request) -> JSONResponse:
    """Pure local probe -- never calls Printix (SOP S1.1)."""
    return JSONResponse({"status": "ok"})


def create_app() -> Starlette:
    mcp = FastMCP(
        name="printix-mcp",
        instructions=SERVICE_INSTRUCTIONS,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    register_tools(mcp)

    # Calling streamable_http_app() lazily creates mcp.session_manager; it must
    # happen before the lambda below is used as the outer app's lifespan.
    mcp_app = mcp.streamable_http_app()

    return Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Mount("/", app=mcp_app, middleware=[Middleware(CredentialsMiddleware)]),
        ],
        lifespan=lambda _app: mcp.session_manager.run(),
    )


__all__ = ["create_app", "SERVICE_INSTRUCTIONS"]
