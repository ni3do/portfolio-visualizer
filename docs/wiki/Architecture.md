# Architecture

```text
┌──────────┐    Flex XML/CSV     ┌──────────────┐
│  IBKR    │ ─────────────────▶  │  ETL (Python)│
└──────────┘                    └──────┬───────┘
                                        │  yfinance REST
                                        ▼
                                 ┌──────────────┐
                                 │   Postgres   │
                                 └────────┬─────┘
                                          │
                                          ▼
                                 ┌──────────────┐
                                 │   Grafana    │
                                 └──────────────┘
```

## Containers

- **Postgres (`pv_postgres`)**: Stores canonical data. Tables are created lazily by the ETL service (`ensure_schema`). Backups are handled via `pg_dump` (not yet automated in repo).
- **ETL (`pv_etl`)**: Python 3.11 application (see `etl/app`). Uses `APScheduler` in `scheduler` mode, or can run one-off commands: `flex-import`, `price-update`, `snapshot-recompute`, and `clear-cache` (yfinance).
- **Grafana (`pv_grafana`)**: Provisions the `Portfolio Postgres` datasource and the `Portfolio Monitoring` dashboard.

- One-off CLI importers exist for IBKR and Swissquote statements; see [Operations](./Operations.md) for commands.

## Data Flow

1. **Flex Import** (`etl/app/importers/ibkr_flex.py`)
   - Fetches Flex statements via IBKR Web Service tokens.
   - Parses trades, cash movements, instruments, corporate actions.
   - Upserts into `instruments`, `transactions`, `cash_movements`, `fx_rates`.
   - Archives raw CSV in `/data/flex_archive` for audit.

2. **Price Update** (`etl/app/prices.py`)
   - Retrieves current prices via `yfinance` (fast_info → fallback history).
   - Clears yfinance caches each run to avoid stale cookies.
   - Writes to `prices` (primary key: `as_of_utc`, `instrument_id`).

3. **Snapshot Recompute** (`etl/app/snapshots.py`)
   - Rebuilds holdings from `transactions` up to the target timestamp (hourly cadence).
   - Joins latest prices + FX rates.
   - Persists `positions_snapshot` & `portfolio_value_snapshot`, including return and drawdown.

4. **Monitoring**
   - Grafana queries Postgres directly for freshness metrics, recent ingest counts, and portfolio value history. See [Monitoring & Dashboards](./Monitoring.md).

## Schedules (default)

| Job                | Schedule                             |
|--------------------|---------------------------------------|
| Price Update       | Every 15 minutes (00/15/30/45)        |
| Snapshot Recompute | 5 minutes after each price update     |
| Flex Import        | Daily 18:00 Europe/Amsterdam (retry 18:30) |

Schedules are defined in `etl/app/main.py` via `BlockingScheduler`.
