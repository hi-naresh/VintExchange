"""Environment-backed runtime settings.

The profile is explicit: a connected profile with missing credentials fails at
startup with a ConfigurationError naming the missing variables. The app never
silently falls back from connected to offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQLITE_PREFIX = "sqlite+aiosqlite:///"


class ConfigurationError(ValueError):
    """Raised when the selected profile is missing required configuration."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    profile: Literal["offline", "connected"] = "offline"
    database_url: str = f"{SQLITE_PREFIX}data/vintexchange.db"

    llm_mode: Literal["fake", "replay", "grok"] = "fake"
    market_mode: Literal["fake", "tavily"] = "fake"

    merchant_timeout_seconds: float = Field(default=8.0, gt=0)
    negotiation_timeout_seconds: float = Field(default=30.0, gt=0)
    market_timeout_seconds: float = Field(default=3.0, gt=0)
    max_rounds: int = Field(default=3, ge=1, le=3)
    negotiation_enabled: bool = True
    reference_ttl_hours: int = Field(default=24, gt=0)

    replay_cache_path: str = "data/replay_cache.json"
    replay_record: bool = False
    replay_fallback: Literal["", "fake"] = ""

    supabase_url: str = ""
    supabase_secret_key: SecretStr = SecretStr("")
    supabase_publishable_key: str = ""

    grok_base_url: str = "https://api.x.ai/v1"
    grok_api_key: SecretStr = SecretStr("")
    grok_model: str = "grok-4"

    tavily_api_key: SecretStr = SecretStr("")

    cors_origins: str = ""

    # Demo seed theme: "fleek" (wholesale secondhand clothing lots) or "electronics".
    demo_theme: Literal["fleek", "electronics"] = "fleek"
    # Business model: the exchange charges merchants a take rate per fill (basis points).
    take_rate_bps: int = Field(default=150, ge=0, le=2_000)

    # Commerce stack --------------------------------------------------------
    catalog_source: Literal["seed", "shopify"] = "seed"
    shopify_store_domain: str = ""
    shopify_storefront_token: SecretStr = SecretStr("")
    # Optional Admin API token (write_draft_orders) to mirror fills as draft orders.
    shopify_admin_token: SecretStr = SecretStr("")
    shopify_api_version: str = "2025-07"
    # Floor used when a product has no `floor:<pounds>` tag, as a share of list price.
    shopify_default_floor_ratio: float = Field(default=0.9, gt=0, le=1)

    posthog_api_key: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        # Raised outside pydantic validation so callers get ConfigurationError itself.
        self._validate_profile()

    def _validate_profile(self) -> None:
        missing: list[str] = []
        if self.profile == "connected":
            if not self.supabase_url:
                missing.append("SUPABASE_URL")
            if not self.supabase_secret_key.get_secret_value():
                missing.append("SUPABASE_SECRET_KEY")
            if not self.supabase_publishable_key:
                missing.append("SUPABASE_PUBLISHABLE_KEY")
        if self.llm_mode == "grok" or self.profile == "connected":
            if not self.grok_base_url:
                missing.append("GROK_BASE_URL")
            if not self.grok_api_key.get_secret_value():
                missing.append("GROK_API_KEY")
        if self.market_mode == "tavily" and not self.tavily_api_key.get_secret_value():
            missing.append("TAVILY_API_KEY")
        if self.catalog_source == "shopify":
            if not self.shopify_store_domain:
                missing.append("SHOPIFY_STORE_DOMAIN")
            if not self.shopify_storefront_token.get_secret_value():
                missing.append("SHOPIFY_STOREFRONT_TOKEN")
        if missing:
            raise ConfigurationError(
                f"profile={self.profile} llm_mode={self.llm_mode} market_mode={self.market_mode} "
                f"is missing required configuration: {', '.join(sorted(set(missing)))}"
            )
        if self.profile == "offline" and not self.database_url.startswith(SQLITE_PREFIX):
            raise ConfigurationError("offline profile requires a sqlite+aiosqlite DATABASE_URL")

    @property
    def sqlite_path(self) -> Path:
        raw = self.database_url.removeprefix(SQLITE_PREFIX)
        path = Path(raw)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
