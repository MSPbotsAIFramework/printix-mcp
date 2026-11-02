# printix-mcp

MCP server exposing read-only lookups for [Printix](https://www.printix.net/) (Tungsten
Automation / HP cloud print management) print queues, sites, and workstations to the
MSPbots Agent Platform.

- Transport: MCP Streamable HTTP (`POST /mcp`), plus `GET /health`.
- Stack: Python 3.12, [`mcp`](https://github.com/modelcontextprotocol/python-sdk) (FastMCP), `httpx`, `uv`.
- License: Apache-2.0.

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

Every `/mcp` request must carry the following HTTP headers:

| Header | Required | Meaning |
|---|---|---|
| `X-Printix-Client-Id` | yes | OAuth2 client ID of a Printix Cloud Print API credential pair, issued per tenant from the Printix Administrator dashboard. |
| `X-Printix-Client-Secret` | yes | OAuth2 client secret paired with the above. |
| `X-Printix-Tenant-Id` | no | Printix tenant id to operate on. Only needed when the credential pair can see more than one Printix tenant; when omitted and exactly one tenant is visible, it is auto-resolved. |

Missing `X-Printix-Client-Id` or `X-Printix-Client-Secret` returns HTTP 401 with the list
of missing header names in the body, before any Printix call is made.

### Getting credentials

In the Printix Administrator dashboard, generate a Cloud Print API client ID and secret
(see the Printix Administrator manual, "Cloud Print API" section). Note two constraints
from Printix itself:

- **Only one Cloud Print API credential set can be active per tenant at a time** --
  generating a new one invalidates the previous one.
- If the tenant's Secure Print configuration is "All users must print securely", the
  Cloud Print API will not work; use a Group-scoped secure-print policy instead.

## Tools

All six tools are read-only (`readOnlyHint: true`) -- this service never submits,
modifies, or deletes anything in Printix.

| Tool | Args | Returns |
|---|---|---|
| `printix_list_printers` | `limit` (default 50, max 200), `cursor` (optional) | Paginated list of `{id, name, status, location}` print queues. |
| `printix_get_printer` | `printer_id` (required) | One print queue's full properties and capabilities. |
| `printix_list_sites` | `limit`, `cursor` | Paginated list of `{id, name, path}` sites. |
| `printix_get_site` | `site_id` (required) | One site's full details. |
| `printix_list_workstations` | `limit`, `cursor` | Paginated list of `{id, name, active, last_connect_time}` workstations. |
| `printix_get_workstation` | `workstation_id` (required) | One workstation's full details. |

Source: [Printix Cloud Print API](https://printix.github.io/). Note: Printix's print-queue
and workstation resources do not carry a structured site reference in their API
responses -- print queues expose a free-text `location` field (not a site id) and
workstations expose none at all; see `docs/tickets/PRD-21204.md`'s Self-test section for
the full account of this gap against the originally designed field names.

## Local development

```bash
uv sync --group dev
uv run pytest
MCP_HTTP_PORT=8080 uv run python -m printix_mcp
```

With the server running, verify it end to end with real (or deliberately wrong, to
exercise the error path) Printix credentials:

```bash
curl -s http://localhost:8080/health

curl -s -X POST http://localhost:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'X-Printix-Client-Id: <client id>' -H 'X-Printix-Client-Secret: <client secret>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Docker:

```bash
docker build --platform linux/amd64 -t printix-mcp:dev .
docker run --rm -p 8080:8080 printix-mcp:dev
```
