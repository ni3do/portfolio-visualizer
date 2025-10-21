import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional


def _read_file(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret file {path} is empty")
    return value


def _resolve_env(name: str, *, required: bool = True) -> Optional[str]:
    direct = os.getenv(name)
    if direct:
        return direct.strip()
    file_key = f"{name}_FILE"
    file_path = os.getenv(file_key)
    if file_path:
        return _read_file(file_path)
    if required:
        raise RuntimeError(f"Missing configuration for {name} (or {file_key})")
    return None


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password: str


@dataclass(frozen=True)
class ApiSettings:
    log_level: str
    db: DatabaseConfig
    auth: AuthConfig
    cors_origins: List[str]
    base_currency: str


@lru_cache(maxsize=1)
def load_settings() -> ApiSettings:
    db = DatabaseConfig(
        host=os.getenv("PORTFOLIO_DB_HOST", "postgres"),
        port=int(os.getenv("PORTFOLIO_DB_PORT", "5432")),
        name=os.getenv("PORTFOLIO_DB_NAME", "portfolio"),
        user=_resolve_env("PORTFOLIO_DB_USER"),
        password=_resolve_env("PORTFOLIO_DB_PASSWORD"),
    )

    auth = AuthConfig(
        username=_resolve_env("VISUALIZER_BASIC_AUTH_USER"),
        password=_resolve_env("VISUALIZER_BASIC_AUTH_PASSWORD"),
    )

    log_level = os.getenv("VISUALIZER_LOG_LEVEL", "INFO").upper()
    cors_raw = os.getenv(
        "VISUALIZER_CORS_ORIGINS",
        "http://localhost:4200,http://localhost:8120,https://portfolio.siwachter.com",
    )
    cors_origins = [origin.strip() for origin in cors_raw.split(",") if origin.strip()]
    base_currency = os.getenv("VISUALIZER_BASE_CURRENCY", "EUR").upper()

    return ApiSettings(
        log_level=log_level,
        db=db,
        auth=auth,
        cors_origins=cors_origins,
        base_currency=base_currency,
    )
