from __future__ import annotations

import logging
from typing import Generator, Optional

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import ApiSettings, load_settings

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None


def init_pool(settings: Optional[ApiSettings] = None) -> None:
    global _pool

    if _pool is not None:
        return

    cfg = settings or load_settings()
    conninfo = (
        f"host={cfg.db.host} port={cfg.db.port} dbname={cfg.db.name} "
        f"user={cfg.db.user} password={cfg.db.password} connect_timeout=10"
    )

    _pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
    )
    logger.info(
        "Connection pool initialised for host=%s db=%s", cfg.db.host, cfg.db.name
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        logger.info("Connection pool closed")


def get_pool() -> ConnectionPool:
    if _pool is None:
        init_pool()
    assert _pool is not None
    return _pool


def get_db_connection() -> Generator[Connection, None, None]:
    pool = get_pool()
    with pool.connection() as conn:
        yield conn

