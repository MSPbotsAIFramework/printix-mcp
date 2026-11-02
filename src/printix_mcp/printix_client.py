"""Thin async client for the Printix Cloud Print API.

One instance per request, built from the credentials bound to that
request's ``ContextVar`` (see ``auth_middleware.py``). Never shared,
never cached across requests: a fresh OAuth2 access token is fetched on
every request and discarded afterwards (SOP S3.4 forbids caching a
derived token across requests in a shared, multi-tenant container).

Reference: https://printix.github.io/ (Printix Cloud Print API), read
directly during implementation of PRD-21204.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Literal

import httpx

from .logging_utils import elapsed_ms, log_event
from .settings import settings

ErrorCode = Literal[
    "not_configured",
    "unauthorized",
    "not_found",
    "invalid_argument",
    "rate_limited",
    "upstream_error",
]

_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 30.0
_MAX_RETRIES = 2  # extra attempts for 429 / 5xx only, per SOP S5
_GET_PRINTER_SCAN_PAGE_SIZE = 200
_GET_PRINTER_SCAN_MAX_PAGES = 50
_TENANT_ID_RE = re.compile(r"/tenants/([^/?]+)")


class PrintixError(Exception):
    """Maps 1:1 onto the tool-level error envelope (see ``tools.py``)."""

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.retryable = retryable

    def to_envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


class PrintixClient:
    """One Printix tenant session, scoped to a single incoming MCP request."""

    def __init__(self, client_id: str, client_secret: str, tenant_id: str | None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._given_tenant_id = tenant_id
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=_READ_TIMEOUT, pool=_CONNECT_TIMEOUT
            ),
        )
        self._token: str | None = None
        self._tenant_id: str | None = None

    async def __aenter__(self) -> PrintixClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    # -- token and tenant setup ---------------------------------------------

    async def _ensure_token(self) -> None:
        if self._token is None:
            self._token = await self._fetch_token()

    async def _ensure_ready(self) -> None:
        """Every public method calls this first: token, then tenant."""
        await self._ensure_token()
        if self._tenant_id is None:
            self._tenant_id = await self._resolve_tenant()

    async def _fetch_token(self) -> str:
        start = time.monotonic()
        try:
            response = await self._http.post(
                settings.printix_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            error = PrintixError(
                "upstream_error", f"Timed out contacting Printix's token endpoint: {exc}", retryable=True
            )
            _log_printix_call("POST token", start, attempt=0, error_code=error.code)
            raise error from exc
        except httpx.HTTPError as exc:
            error = PrintixError(
                "upstream_error", f"Network error contacting Printix's token endpoint: {exc}", retryable=True
            )
            _log_printix_call("POST token", start, attempt=0, error_code=error.code)
            raise error from exc

        if response.status_code >= 500:
            error = PrintixError(
                "upstream_error", f"Printix's token endpoint returned HTTP {response.status_code}.", retryable=True
            )
        elif response.status_code == 429:
            error = PrintixError(
                "rate_limited", "Printix's token endpoint is rate-limiting this credential pair.", retryable=True
            )
        elif response.status_code != 200:
            # OAuth2 client-credentials rejections (bad client_id/secret) come back
            # as 400 or 401 depending on the failure; either way this is "Printix
            # rejected the credentials", not a transport problem (AC5/TC5).
            error = PrintixError(
                "unauthorized", "Printix rejected the provided client_id/client_secret pair.", retryable=False
            )
        else:
            error = None

        if error is not None:
            _log_printix_call("POST token", start, attempt=0, status_code=response.status_code, error_code=error.code)
            raise error

        body = response.json()
        token = body.get("access_token")
        if not token:
            no_token_error = PrintixError(
                "upstream_error", "Printix's token response had no access_token field.", retryable=True
            )
            _log_printix_call(
                "POST token", start, attempt=0, status_code=response.status_code, error_code=no_token_error.code
            )
            raise no_token_error
        _log_printix_call("POST token", start, attempt=0, status_code=response.status_code)
        return token

    async def _resolve_tenant(self) -> str:
        if self._given_tenant_id:
            return self._given_tenant_id

        root = await self._request("GET", settings.printix_api_base_url)
        tenant_ids: list[str] = []
        for link in (root.get("_links") or {}).values():
            href = link.get("href") if isinstance(link, dict) else None
            tenant_id = _tenant_id_from_href(href) if href else None
            if tenant_id and tenant_id not in tenant_ids:
                tenant_ids.append(tenant_id)

        if len(tenant_ids) == 1:
            return tenant_ids[0]
        if not tenant_ids:
            raise PrintixError(
                "invalid_argument", "This credential pair is not associated with any Printix tenant."
            )
        raise PrintixError(
            "invalid_argument",
            "This credential pair can see more than one Printix tenant; pass "
            f"X-Printix-Tenant-Id to pick one. Found: {', '.join(tenant_ids)}.",
        )

    # -- HTTP plumbing --------------------------------------------------------

    async def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self._ensure_token()
        last_error: PrintixError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                )
            except httpx.TimeoutException as exc:
                last_error = PrintixError("upstream_error", f"Timed out calling Printix: {exc}", retryable=True)
                _log_printix_call(method, start, attempt=attempt, error_code=last_error.code)
                await asyncio.sleep(min(2**attempt, 5))
                continue
            except httpx.HTTPError as exc:
                last_error = PrintixError("upstream_error", f"Network error calling Printix: {exc}", retryable=True)
                _log_printix_call(method, start, attempt=attempt, error_code=last_error.code)
                await asyncio.sleep(min(2**attempt, 5))
                continue

            if response.status_code < 300:
                _log_printix_call(method, start, attempt=attempt, status_code=response.status_code)
                return response.json() if response.content else {}

            code, retryable = _status_to_code(response.status_code)
            message = _error_message_from_body(response) or f"Printix returned HTTP {response.status_code}."
            last_error = PrintixError(code, message, retryable=retryable)
            _log_printix_call(method, start, attempt=attempt, status_code=response.status_code, error_code=code)
            if retryable and attempt < _MAX_RETRIES:
                await asyncio.sleep(_retry_after_seconds(response) or min(2**attempt, 5))
                continue
            raise last_error

        assert last_error is not None
        raise last_error

    # -- printers / print queues ----------------------------------------------

    async def list_printers(self, *, page: int = 0, page_size: int = 50) -> dict[str, Any]:
        await self._ensure_ready()
        return await self._request(
            "GET",
            f"{settings.printix_api_base_url}/tenants/{self._tenant_id}/printers",
            params={"page": page, "pageSize": page_size},
        )

    async def get_printer(self, printer_id: str) -> dict[str, Any] | None:
        """Return one print queue's full properties, or ``None`` if not found.

        Printix exposes a single queue only via a two-segment path
        (``.../printers/{printerId}/queues/{queueId}``) discovered through a
        HATEOAS ``self`` link, not a flat ``.../printers/{id}``. Guessing the
        printer-id half from only the opaque queue id the list tool hands
        back is not possible, so this scans the (per the tech design's
        Non-functional note, typically small) print-queue list instead --
        the list response already carries the same full
        properties/capabilities as the single-item endpoint.
        """
        await self._ensure_ready()
        page = 0
        for _ in range(_GET_PRINTER_SCAN_MAX_PAGES):
            body = await self._request(
                "GET",
                f"{settings.printix_api_base_url}/tenants/{self._tenant_id}/printers",
                params={"page": page, "pageSize": _GET_PRINTER_SCAN_PAGE_SIZE},
            )
            for printer in body.get("printers", []):
                if printer.get("id") == printer_id:
                    return printer
            page_info = body.get("page") or {}
            if page_info.get("number", page) + 1 >= page_info.get("totalPages", 1):
                break
            page += 1
        return None

    # -- sites -----------------------------------------------------------------

    async def list_sites(self, *, page: int = 0, page_size: int = 50) -> dict[str, Any]:
        await self._ensure_ready()
        body = await self._request(
            "GET",
            f"{settings.printix_api_base_url}/tenants/{self._tenant_id}/sites",
            params={"page": page, "pageSize": page_size},
        )
        # Unlike printers and workstations, Printix's Query Sites response has no
        # bare "id" field per item -- only a "_links.self.href" pointing at
        # ".../sites/{siteId}". Each item needs that id injected so the agent
        # has something to pass to printix_get_site.
        for site in body.get("sites", []):
            site_id = _id_from_self_link(site)
            if site_id:
                site.setdefault("id", site_id)
        return body

    async def get_site(self, site_id: str) -> dict[str, Any] | None:
        await self._ensure_ready()
        try:
            body = await self._request(
                "GET", f"{settings.printix_api_base_url}/tenants/{self._tenant_id}/sites/{site_id}"
            )
        except PrintixError as exc:
            if exc.code == "not_found":
                return None
            raise
        # Printix's Retrieve Site response does not echo back the id it was
        # fetched by (only name/path/timestamps/_links); the caller already
        # knows it, so it is added back for a self-describing tool result.
        body.setdefault("id", site_id)
        return body

    # -- workstations ------------------------------------------------------------

    async def list_workstations(self, *, page: int = 0, page_size: int = 50) -> dict[str, Any]:
        await self._ensure_ready()
        return await self._request(
            "GET",
            f"{settings.printix_api_base_url}/tenants/{self._tenant_id}/workstations",
            params={"page": page, "pageSize": page_size},
        )

    async def get_workstation(self, workstation_id: str) -> dict[str, Any] | None:
        await self._ensure_ready()
        try:
            return await self._request(
                "GET",
                f"{settings.printix_api_base_url}/tenants/{self._tenant_id}/workstations/{workstation_id}",
            )
        except PrintixError as exc:
            if exc.code == "not_found":
                return None
            raise


def _tenant_id_from_href(href: str) -> str | None:
    match = _TENANT_ID_RE.search(href)
    return match.group(1) if match else None


def _id_from_self_link(item: dict[str, Any]) -> str | None:
    href = (((item.get("_links") or {}).get("self") or {}).get("href")) if isinstance(item, dict) else None
    if not href:
        return None
    return href.rstrip("/").rsplit("/", 1)[-1] or None


def _log_printix_call(
    method: str, start: float, *, attempt: int, status_code: int | None = None, error_code: ErrorCode | None = None
) -> None:
    """Log one outbound Printix HTTP call: status class and mapped error code only.

    Never the request/response body, query params, or any header value
    (SOP S6/S11) -- that is also why only ``method`` (not the full URL, which
    can carry tenant/resource ids) is logged here.
    """
    log_event(
        "printix_http_call",
        method=method,
        attempt=attempt,
        status_class=(f"{status_code // 100}xx" if status_code is not None else "n/a"),
        error_code=error_code,
        latency_ms=elapsed_ms(start),
    )


def _status_to_code(status_code: int) -> tuple[ErrorCode, bool]:
    if status_code in (401, 403):
        return "unauthorized", False
    if status_code == 404:
        return "not_found", False
    if status_code in (400, 422):
        return "invalid_argument", False
    if status_code == 429:
        return "rate_limited", True
    if status_code >= 500:
        return "upstream_error", True
    return "upstream_error", False


def _error_message_from_body(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


__all__ = ["PrintixClient", "PrintixError", "ErrorCode"]
