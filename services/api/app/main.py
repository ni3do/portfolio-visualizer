from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import load_settings
from .database import close_pool, init_pool
from .routers import api_router


def create_app() -> FastAPI:
    settings = load_settings()

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    logger = logging.getLogger(__name__)
    logger.info("Starting Visualizer API with log level %s", settings.log_level)

    app = FastAPI(
        title="Portfolio Visualizer API",
        version="0.1.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"status": "ok"}

    @app.on_event("startup")
    def _startup() -> None:
        init_pool(settings)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        close_pool()

    return app


app = create_app()
