from __future__ import annotations

import logging
import os
import shlex
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Annotated

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import DatabaseConfig, load_db_config

logger = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None
_db_config: DatabaseConfig | None = None


def _log_connection_details(cfg: DatabaseConfig) -> None:
    logger.info(
        "Connecting MCP server to PostgreSQL %s:%s/%s as %s",
        cfg.host,
        cfg.port,
        cfg.name,
        cfg.user,
    )


def _build_conninfo(cfg: DatabaseConfig) -> str:
    options = "-c statement_timeout=5000 -c default_transaction_read_only=on"
    settings = [
        f"host={cfg.host}",
        f"port={cfg.port}",
        f"dbname={cfg.name}",
        f"user={cfg.user}",
        f"password={cfg.password}",
        "connect_timeout=5",
        "application_name=portfolio_mcp",
        f"options={shlex.quote(options)}",
    ]
    return " ".join(settings)


def _get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Database connection pool is not available")
    return _pool


def _get_db_config() -> DatabaseConfig:
    if _db_config is None:
        raise RuntimeError("Database configuration is not available")
    return _db_config


def _serialize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()  # datetime/date/time
    if isinstance(value, (list, tuple)):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return str(value)


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _serialize_value(value) for key, value in row.items()} for row in rows]


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid port number: {value}") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Port must be between 1 and 65535 (received {port})")
    return port


SERVER_HOST = os.getenv("MCP_HOST", "127.0.0.1")
SERVER_PORT = _parse_port(os.getenv("MCP_PORT", "8000"))


@asynccontextmanager
async def _lifespan(_: FastMCP) -> Any:
    global _pool, _db_config

    _db_config = load_db_config()
    db_cfg = _db_config
    _log_connection_details(db_cfg)

    max_size = int(os.getenv("MCP_DB_POOL_MAX_SIZE", "8"))
    min_size = int(os.getenv("MCP_DB_POOL_MIN_SIZE", "1"))

    _pool = AsyncConnectionPool(
        conninfo=_build_conninfo(db_cfg),
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True},
    )
    try:
        yield {"database": db_cfg}
    finally:
        if _pool is not None:
            await _pool.close()
            _pool = None
        _db_config = None


server = FastMCP(
    name="portfolio-mcp",
    instructions="Read-only access to the portfolio Postgres database. Queries must be SELECT statements.",
    lifespan=_lifespan,
    host=SERVER_HOST,
    port=SERVER_PORT,
)


@server.tool(description="Run a read-only SQL query against the portfolio database.")
async def run_sql_query(
    sql: Annotated[str, Field(description="SQL query starting with SELECT or WITH")],
    limit: Annotated[int, Field(description="Maximum number of rows to return", ge=1, le=1000)] = 200,
    ctx: Context | None = None,
) -> dict[str, Any]:
    query = sql.strip()
    if not query:
        raise ValueError("SQL query must not be empty.")

    leading_token = query.split(None, 1)[0].upper()
    if leading_token not in {"SELECT", "WITH"}:
        raise ValueError("Only read-only SELECT or WITH queries are permitted.")

    pool = _get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query)
            raw_rows = await cursor.fetchmany(limit + 1)
            columns = [col.name for col in cursor.description] if cursor.description else []

    has_more = len(raw_rows) > limit
    if has_more:
        raw_rows = raw_rows[:limit]

    rows = _serialize_rows(raw_rows)

    if ctx:
        await ctx.report_progress(100, 100, "Query completed")
        cfg = _get_db_config()
        await ctx.debug(f"Database connection: {cfg.host}:{cfg.port}/{cfg.name}")

    return {
        "columns": columns,
        "row_count": len(rows),
        "truncated": has_more,
        "rows": rows,
    }


@server.tool(description="List tables available in the configured database schema.")
async def list_tables(
    schema: Annotated[
        str | None,
        Field(
            description="Optional schema to filter on (defaults to visible schemas). Use lowercase names."
        ),
    ] = None,
) -> dict[str, Any]:
    pool = _get_pool()

    conditions = [
        "table_type = 'BASE TABLE'",
        "table_schema NOT IN ('pg_catalog', 'information_schema')",
    ]
    params: dict[str, Any] = {}

    if schema:
        trimmed_schema = schema.strip()
        if trimmed_schema:
            conditions.append("table_schema = %(schema)s")
            params["schema"] = trimmed_schema

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE {where_clause}
        ORDER BY table_schema, table_name
    """

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            rows = await cursor.fetchall()

    return {"tables": _serialize_rows(rows)}


def main() -> None:
    logging.basicConfig(level=os.getenv("MCP_LOG_LEVEL", "INFO"))
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport not in {"stdio", "sse", "streamable-http"}:
        logger.warning("Unsupported MCP transport '%s'; falling back to stdio", transport)
        transport = "stdio"
    if transport != "stdio":
        logger.info("Starting MCP server with %s transport on %s:%s", transport, SERVER_HOST, SERVER_PORT)
    try:
        server.run(transport=transport)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        logger.info("Received shutdown request. Exiting MCP server.")


if __name__ == "__main__":
    main()
