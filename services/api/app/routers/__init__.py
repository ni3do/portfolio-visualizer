from fastapi import APIRouter

from . import health, instruments, portfolio, trades

api_router = APIRouter(prefix="/api")

api_router.include_router(health.router, tags=["health"])
api_router.include_router(instruments.router, tags=["instruments"])
api_router.include_router(portfolio.router, tags=["portfolio"])
api_router.include_router(trades.router, tags=["trades"])
