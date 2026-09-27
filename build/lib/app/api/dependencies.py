"""Dependency construction for each runtime profile."""

from __future__ import annotations

import httpx
from fastapi import Request

from app.agents.fake import FakeLLMClient
from app.agents.protocol import LLMClient
from app.agents.replay import ReplayLLMClient
from app.config import PROJECT_ROOT, Settings
from app.market.fake import FakeMarketPriceProvider
from app.market.protocol import MarketPriceProvider
from app.repositories.protocol import Repository
from app.services.commerce import OrderSink
from app.services.container import Services
from app.services.events import EventSink


async def get_services(request: Request) -> Services:
    services: Services | None = getattr(request.app.state, "services", None)
    if services is None:
        # Serverless hosts (e.g. Vercel) may not run ASGI lifespan: build lazily once.
        ensure = getattr(request.app.state, "ensure_services", None)
        if ensure is None:  # pragma: no cover
            raise RuntimeError("application services are not initialized")
        services = await ensure()
    return services


async def build_repository(settings: Settings, http: httpx.AsyncClient) -> Repository:
    if settings.profile == "connected":
        from app.repositories.supabase import SupabaseRepository

        repository: Repository = SupabaseRepository(http, settings)
        await repository.initialize()
        return repository

    from app.commerce.catalog import load_catalog
    from app.repositories.sqlite import SQLiteRepository

    sqlite = await SQLiteRepository.connect(settings.sqlite_path)
    if not await sqlite.list_merchants():
        await load_catalog(sqlite, settings, http)
    return sqlite


def build_order_sink(settings: Settings, http: httpx.AsyncClient) -> OrderSink | None:
    if settings.catalog_source != "shopify" or not settings.shopify_admin_token.get_secret_value():
        return None
    from app.commerce.shopify import ShopifyDraftOrderSink

    return ShopifyDraftOrderSink(http, settings)


def build_event_sinks(settings: Settings, http: httpx.AsyncClient) -> list[EventSink]:
    if not settings.posthog_api_key:
        return []
    from app.analytics.posthog import PostHogSink

    return [PostHogSink(http, settings.posthog_api_key, settings.posthog_host)]


def describe_catalog(settings: Settings) -> str:
    if settings.catalog_source == "shopify":
        return f"Shopify: {settings.shopify_store_domain}"
    if settings.demo_theme == "fleek":
        return "Demo wholesale catalogue (Fleek-style lots)"
    return "Seeded demo catalogue"


def build_llm(settings: Settings, http: httpx.AsyncClient) -> LLMClient:
    cache_path = PROJECT_ROOT / settings.replay_cache_path
    fallback = FakeLLMClient() if settings.replay_fallback == "fake" else None
    if settings.llm_mode == "fake":
        return FakeLLMClient()
    if settings.llm_mode == "replay":
        return ReplayLLMClient(None, cache_path, fallback=fallback, model=settings.grok_model)
    from app.agents.grok import GrokLLMClient

    grok = GrokLLMClient(http, settings)
    if settings.replay_record:
        return ReplayLLMClient(grok, cache_path, record=True)
    return grok


def build_market(settings: Settings, repository: Repository,
                 http: httpx.AsyncClient) -> MarketPriceProvider:
    if settings.market_mode == "fake":
        return FakeMarketPriceProvider()
    from app.market.cache import CachedMarketPriceProvider
    from app.market.tavily import TavilyMarketPriceProvider

    tavily = TavilyMarketPriceProvider(http, settings.tavily_api_key,
                                       timeout_seconds=settings.market_timeout_seconds)
    return CachedMarketPriceProvider(tavily, repository, ttl_hours=settings.reference_ttl_hours,
                                     timeout_seconds=settings.market_timeout_seconds)
