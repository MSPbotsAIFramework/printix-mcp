"""Non-credential runtime configuration.

Credentials are never read here -- they arrive per-request in HTTP headers
(see ``auth_middleware.py``). This module only covers the handful of
environment-driven settings that are safe to share across every request.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings, read once at startup.

    ``extra="ignore"`` is required: the runtime environment injects
    variables this project never declared, and a strict model would crash
    on startup instead of ignoring them (SOP S1.2).
    """

    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080

    printix_api_base_url: str = "https://api.printix.net/cloudprint"
    printix_token_url: str = "https://auth.printix.net/oauth/token"


settings = Settings()
