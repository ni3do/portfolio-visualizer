# MCP Server for Portfolio Database

The project ships with a Model Context Protocol (MCP) server that exposes read-only access to the PostgreSQL database. The server uses the [`mcp`](https://pypi.org/project/mcp/) Python package together with the existing Psycopg connection pool. Two tools are currently provided:

- `run_sql_query(sql, limit=200)` – executes a read-only `SELECT`/`WITH` statement and returns rows (truncated when `limit` is exceeded).
- `list_tables(schema=None)` – lists base tables visible to the configured database user.

Both tools enforce a read-only connection (`default_transaction_read_only=on`) and respect the same environment variables used by the ETL service to resolve database credentials.

## Running Locally (stdio transport)

1. Ensure the database secrets are available via environment variables or files (e.g. source the generated secrets):

   ```bash
   export PORTFOLIO_DB_HOST=localhost
   export PORTFOLIO_DB_PORT=5432
   export PORTFOLIO_DB_NAME=portfolio
   export PORTFOLIO_DB_USER="$(cat secrets/postgres_app_user)"
   export PORTFOLIO_DB_PASSWORD="$(cat secrets/postgres_app_password)"
   ```

2. Launch the server using Astral `uv` (this reads the entry point defined in `pyproject.toml`):

   ```bash
   cd etl
   uv run portfolio-mcp
   ```

   The process stays attached to the terminal and speaks MCP over stdio.

## Running in Docker (streamable HTTP transport)

The Compose stack now contains a dedicated `mcp` service that reuses the ETL image.

```bash
docker compose build mcp
docker compose up -d postgres mcp
```

The container listens on `http://localhost:8800/mcp` (configurable via `MCP_PORT` and the compose port mapping) and uses the streamable HTTP transport.

Useful environment knobs for the container:

- `MCP_TRANSPORT` (`stdio` | `sse` | `streamable-http`, defaults to `stdio`)
- `MCP_HOST` / `MCP_PORT` (defaults `127.0.0.1:8000`)
- `MCP_DB_POOL_MIN_SIZE` / `MCP_DB_POOL_MAX_SIZE` (defaults `1` / `8`)
- `MCP_LOG_LEVEL` (`INFO` by default)

## Hooking into Codex CLI

The Codex CLI can spawn the MCP server either directly (stdio) or via Docker. Add the server to your Codex configuration (usually `~/.config/codex/mcp.json`) similar to the snippet below:

```jsonc
{
  "mcpServers": {
    "portfolio-db": {
      "command": "uv",
      "args": ["run", "portfolio-mcp"],
      "env": {
        "PORTFOLIO_DB_HOST": "localhost",
        "PORTFOLIO_DB_PORT": "5432",
        "PORTFOLIO_DB_NAME": "portfolio",
        "PORTFOLIO_DB_USER_FILE": "/Users/<you>/repo/portfolio-visualizer/secrets/postgres_app_user",
        "PORTFOLIO_DB_PASSWORD_FILE": "/Users/<you>/repo/portfolio-visualizer/secrets/postgres_app_password"
      }
    }
  }
}
```

To delegate to Docker instead, replace the `command`/`args` pair with:

```jsonc
"command": "docker",
"args": ["compose", "run", "--rm", "--no-deps", "mcp"]
```

In both cases Codex CLI manages the server lifecycle and streams MCP messages over stdio.

### Verifying the connection

1. Start Codex CLI with the `portfolio-db` MCP server enabled.
2. Ask the assistant to run a query, for example:

   ```
   /mcp portfolio-db.run_sql_query {"sql": "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"}
   ```

   The response should list the tables available to the portfolio database user.
