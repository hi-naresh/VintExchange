"""Application factory.

Injected dependencies (tests, the demo script) are used as-is and never closed
by the app. Otherwise the lifespan builds and later closes the dependencies for
the configured profile.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.agents.protocol import LLMClient
from app.api.dependencies import (
    build_event_sinks,
    build_llm,
    build_market,
    build_order_sink,
    build_repository,
    describe_catalog,
)
from app.api.routes import router
from app.config import Settings
from app.domain.models import PolicyRejected
from app.market.protocol import MarketPriceProvider
from app.repositories.protocol import NotFound, Repository, RepositoryConflict
from app.services.container import Services
from app.services.payments import PaymentGateway, SimulatedPaymentGateway

STATIC_DIR = Path(__file__).with_name("static")


def create_app(settings: Settings | None = None, repository: Repository | None = None,
               llm: LLMClient | None = None, market: MarketPriceProvider | None = None,
               payment: PaymentGateway | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "services", None) is not None:
            yield  # fully injected: nothing owned
            return
        async with httpx.AsyncClient(timeout=10.0) as http:
            owned_repository = repository is None
            repo = repository or await build_repository(settings, http)
            app.state.services = Services(
                settings=settings, repository=repo, llm=llm or build_llm(settings, http),
                market=market or build_market(settings, repo, http),
                payment=payment or SimulatedPaymentGateway(),
                order_sink=build_order_sink(settings, http),
                event_sinks=build_event_sinks(settings, http),
                catalog_description=describe_catalog(settings),
            )
            try:
                yield
            finally:
                await app.state.services.commerce.drain()
                await app.state.services.recorder.drain()
                if owned_repository:
                    await repo.close()
                app.state.services = None

    init_lock = asyncio.Lock()

    async def ensure_services() -> Services:
        async with init_lock:
            if getattr(app.state, "services", None) is None:
                http = httpx.AsyncClient(timeout=10.0)  # lives as long as the instance
                repo = repository or await build_repository(settings, http)
                app.state.services = Services(
                    settings=settings, repository=repo, llm=llm or build_llm(settings, http),
                    market=market or build_market(settings, repo, http),
                    payment=payment or SimulatedPaymentGateway(),
                    order_sink=build_order_sink(settings, http),
                    event_sinks=build_event_sinks(settings, http),
                    catalog_description=describe_catalog(settings),
                )
            return app.state.services

    app = FastAPI(
        title="Trading Agentic Commerce",
        version="0.1.0",
        summary="Grok proposes; deterministic code disposes. Payment is simulated.",
        lifespan=lifespan,
    )
    app.state.ensure_services = ensure_services
    if repository is not None:
        app.state.services = Services(
            settings=settings, repository=repository, llm=llm or build_llm(settings, None),
            market=market or build_market(settings, repository, None),  # type: ignore[arg-type]
            payment=payment or SimulatedPaymentGateway(),
            catalog_description=describe_catalog(settings),
        )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
            allow_methods=["GET", "POST"], allow_headers=["Content-Type"],
        )

    @app.exception_handler(PolicyRejected)
    async def policy_rejected(_: Request, error: PolicyRejected) -> JSONResponse:
        return JSONResponse(status_code=422, content={
            "reason_code": error.decision.code.value, "reason": error.decision.reason,
            "request_id": error.request_id,
        })

    @app.exception_handler(NotFound)
    async def not_found(_: Request, error: NotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RepositoryConflict)
    async def conflict(_: Request, error: RepositoryConflict) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "transaction conflict",
                                                      "reason": str(error)[:200]})

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
