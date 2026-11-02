# PRD-21204 · Code review

## Round 1 · 2026-11-02 · reviewed ee77d79 (base c799955)

Verdict: changes requested

### Findings

1. [must-fix] src/printix_mcp/tools.py, src/printix_mcp/printix_client.py — no logging exists anywhere in the service (`grep -rn "logging\|logger\|log\."` across `src/` returns zero matches), but the tech design's own "Non-functional → Observability" section (`docs/tickets/PRD-21204.md`) commits to: "log tool name, Printix HTTP status class, and the mapped error `code` (never request/response bodies, never credential header values) per call, so a failure can be triaged without the log itself becoming a secrets store." This is also an explicit item in `vendor-mcp-development-sop.md` §14 Checklist, "日志与敏感信息 (§6, §11)": "结构化日志带 request-id；不打印凭据 / token / 敏感业务数据". Neither the design's own promise nor the SOP checklist item is met — this is a tech-design-required change that was not implemented, not a judgment call (`legs/review.md` R3, "范围" row: "技术设计要求的改动没做"). Fix: add structured logging (tool name, Printix HTTP status class, mapped error `code`, latency) at the point each tool call completes in `tools.py` (or inside `PrintixClient._request` for the HTTP-status/error-code part), never logging header values or response bodies.

### Suggestions

1. [suggestion] src/printix_mcp/tools.py:34-35 (`_clamp_limit`) — a `limit` of `0` or a negative value is silently coerced up to `1` rather than surfaced as `invalid_argument`. No AC requires rejecting it, but it is a silent-correction edge case worth a one-line note in the README/tool description if intentional.
2. [suggestion] src/printix_mcp/printix_client.py:154-186 (`_request`) — the "total call budget per tool invocation stays under 120s including the token fetch" non-functional target (design §Non-functional) is not enforced by any overall deadline; it is only an emergent property of the per-attempt connect/read timeouts and retry caps. Worth a comment noting this is relied upon rather than actively bounded, in case a future change to `_MAX_RETRIES` or the timeouts silently breaks the budget.

### Independently verified (not just re-reading the self-test claims)

- `uv run pytest -q` on `ee77d79`: **20 passed**, 0 failed — matches the `[DEV]` stub and `PRD-21204.ac.json`.
- `grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .`: no output — repo is English-only.
- README's "MSPbots Connector" section (`README.md:11-81`) diffed byte-for-byte against `~/.claude/skills/vendor-mcp-development/reference/readme-connector-section.md`: identical, correctly placed before "## Authentication headers".
- Manual secret-pattern grep (`(token|secret|api[_-]?key|password|bearer)\s*[:=]\s*[A-Za-z0-9_-]{12,}`): only matches variable names (`client_secret`, `HEADER_CLIENT_SECRET`), no real credential values — confirms the self-test's gitleaks-unavailable fallback.
- `gitleaks` is indeed not installed on this machine (confirms the self-test's stated reason for skipping it).
- Credential handling (`auth_middleware.py`), per-request isolation (`ContextVar` + `finally` reset), tenant auto-resolution/ambiguity handling, and the error-code mapping in `printix_client.py` all match the tech design's Data & API contract and Key flow sections.
