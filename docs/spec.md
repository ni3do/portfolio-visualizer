# Portfolio Dashboard Specification

## 1. Architecture & Runtime
### 1.1 Docker Compose Services
- `postgres`: PostgreSQL 15 with persistent `pg_data` volume and healthcheck.
- `etl`: Python 3.11 (APS cheduler) image sharing code between CLI and scheduler.
- `grafana`: Grafana OSS with pre-provisioned datasource/dashboards.
- `mcp`: Read-only Model Context Protocol server (streamable HTTP on `localhost:8000/mcp`).

### 1.2 Networking & Secrets
- Internal service network, Grafana exposed on `:3000`, MCP on `:8000`.
- Secrets provided via Docker secrets (files under `/run/secrets/*`); managed locally with `./scripts/create-dev-secrets.sh`.
- Required secrets: Postgres app user/password, Grafana admin user/password, IBKR Flex token & query ID.

### 1.3 Persistent Storage & Backups
- Volumes: `pg_data`, `grafana_data`, `etl_data` (`/data/flex_archive`, price cache).
- Nightly `pg_dump` of `portfolio` database into an on-host path (retain 14 days). Document rotation/restore steps.

## 2. Data Sources & Ingestion
### 2.1 IBKR Flex Web Service
- Fetch CSV statements via Flex token & query ID.
- Parse instruments, transactions, cash movements, FX rates; archive raw files under `/data/flex_archive`.
- Support on-demand imports via CLI (`flex-import`, `import-ibkr`).

### 2.2 Swissquote CSV Import
- Treat as first-class ingestion path with parity to Flex importer.
- CLI command `import-swissquote` parses timezone/delimiter variants, stores transactions/cash data, triggers backfill pipeline.

### 2.3 Market Data (yfinance)
- Primary price source. Respect rate limits, cache, and allow ticker overrides via `instruments.yfinance_symbol`.

### 2.4 MCP Exposure
- Provide read-only SQL access (`run_sql_query`) for debugging/analytics over HTTP (`Accept: application/json, text/event-stream` with session headers).

## 3. Database Schema

- `portfolio_value_snapshot` rows shall store:
  - `snapshot_at TIMESTAMPTZ` (UTC top-of-hour),
  - `account_id TEXT`,
  - `positions_value_eur NUMERIC`,
  - `cash_value_eur NUMERIC`,
  - `nav_eur NUMERIC`,
  - `unrealized_pnl_eur NUMERIC`,
  - `realized_pnl_eur NUMERIC`,
  - `delta_eur NUMERIC`,
  - `created_at TIMESTAMPTZ` (default NOW()).
  Primary key: (`snapshot_at`, `account_id`).
- `realized_pnl_fifo` table structure:
  - (`account_id TEXT`, `instrument_id BIGINT`, `lot_opened_at TIMESTAMPTZ`, `lot_closed_at TIMESTAMPTZ`, `close_snapshot_at TIMESTAMPTZ`, `qty_closed NUMERIC`, `proceeds_ccy NUMERIC`, `proceeds_eur NUMERIC`, `cost_ccy NUMERIC`, `cost_eur NUMERIC`, `pnl_ccy NUMERIC`, `pnl_eur NUMERIC`, `created_at TIMESTAMPTZ default NOW()`)
  Primary key: (`account_id`, `instrument_id`, `lot_opened_at`, `lot_closed_at`).
- `data_gaps` schema:
  - (`gap_type TEXT`, `target_timestamp TIMESTAMPTZ`, `detected_at TIMESTAMPTZ default NOW()`, `instrument_id BIGINT`, `account_id TEXT`, `details JSONB`)
  Primary key: (`gap_type`, `target_timestamp`, `instrument_id`, `account_id`). Allowed `gap_type`: `price`, `fx_rate`, `instrument_metadata`.
- Core tables:
  - `instruments` (extend to log metadata completeness; missing ticker metadata is captured in `data_gaps`).
  - `transactions`, `cash_movements` (source-of-truth for FIFO cost basis and cash balances).
  - `prices`, `fx_rates` (FX now sourced exclusively from yfinance).
  - `positions_snapshot` (per-account, per-instrument shares/cost basis at snapshot timestamp).
  - `portfolio_value_snapshot` (per-account totals with required columns: `positions_value_eur`, `cash_value_eur`, `nav_eur`, `unrealized_pnl_eur`, `realized_pnl_eur`, `snapshot_at`, `account_id`; retain `ret`/`drawdown` if needed for Grafana).
- New table: `realized_pnl_fifo`
  - Columns: `account_id`, `instrument_id`, `lot_opened_at`, `lot_closed_at`, `qty_closed`, `proceeds_ccy`, `cost_ccy`, `pnl_ccy`, `pnl_eur`, `close_snapshot_at`.
  - Populated by FIFO matcher whenever sells/partial sells occur.
- New table: `data_gaps` tracking missing inputs.
  - Columns: `gap_type TEXT`, `target_timestamp TIMESTAMPTZ`, `detected_at TIMESTAMPTZ`, `instrument_id BIGINT NULL`, `account_id TEXT NULL`, `details JSONB`.
  - Primary key on (`gap_type`, `target_timestamp`, `instrument_id`, `account_id`) for dedupe.
  - ETL jobs append rows for missing prices, FX rates, instrument metadata, or other blockers and delete entries once the data is supplied (e.g., price obtained, metadata set).
- Maintain indexes aligned with query patterns (e.g., `data_gaps(gap_type, target_timestamp)`, `realized_pnl_fifo(account_id, instrument_id, lot_closed_at)`).

- Cash balance per account is derived as: cash_value_eur = previous_snapshot_cash + Σ cash_movements.amount (converted to EUR at movement timestamp FX) + Σ transactions.net_amount for trades since previous snapshot (converted to EUR at trade timestamp FX).
- Unrealized PnL per snapshot: unrealized_pnl_eur = positions_value_eur - Σ open lot cost in EUR at snapshot time (uses FIFO queue state).
- Realized PnL accumulation uses FIFO matching; pseudocode and behaviour defined in section 4.5.
- `delta_eur` = nav_eur - prior nav_eur for same account (absolute change).
- FX rates are sourced from yfinance; if the desired date is missing, use the most recent prior business day (max 1 day lookback). The raw Flex FX data is stored for reference but not used in valuations.
## 4. ETL Workloads
### 4.1 Flex Importer
- Scheduled daily 18:00/18:30 CET; CLI for manual runs.
- Upserts parsed entities, runs schema migrations, triggers optional backfill.
- Logs metrics & writes missing-data gaps when expected FX rates or instruments are absent.

### 4.2 Swissquote Importer
- Mirrors Flex behaviour (schema ensure, upserts, optional backfill).
- Supports manual CSV ingestion; integrate missing-data logging.

### 4.3 Price Updater
- 15-minute schedule (:00/:15/:30/:45 CET).
- Builds holdings-target list, fetches yfinance data, upserts `prices`.
- Logs gaps when ticker metadata is missing or yfinance returns no data (record in `data_gaps`).
- Converts non-EUR valuations in downstream snapshot stage.

### 4.4 FX Updater
- 15-minute offset schedule (:10/:25/:40/:55 CET).
- Source FX exclusively from yfinance; store close-of-day rate for each required currency pair.
- If the desired date is missing, fall back to most recent prior rate (record a `data_gaps` entry so the true rate can be backfilled later).

### 4.5 Snapshot Recompute
### 4.5 Snapshot Recompute
- Input preparation:
  - Pull transactions sorted ascending by `date_time_utc` per account.
  - Maintain FIFO lot queue per account/instrument (as described below).
  - Fetch latest prices ≤ snapshot timestamp; log `price` gap if unavailable.
  - Fetch FX rate for price timestamp; if missing, use last business day ≤ timestamp (max 1 day lookback) and log `fx_rate` gap.
- FIFO realized PnL algorithm (per account):
  1. Sort all transactions by `date_time_utc`.
  2. For each instrument maintain a FIFO queue of open lots. Each lot stores:
     - remaining quantity,
     - cost per unit in instrument currency,
     - cost per unit in EUR (cost_ccy × FX at trade time),
     - timestamp opened.
  3. When processing a **buy** (quantity > 0):
     - Compute `cost_ccy = -net_amount` (positive cash invested).
     - Append a new lot with the fields above; do not merge lots.
  4. When processing a **sell** (quantity < 0):
     - Let `sell_qty = abs(quantity)`.
     - Compute proceeds in both currencies (`net_amount` in trade ccy, convert with FX at trade time).
     - Repeatedly match `sell_qty` against the head of the FIFO queue:
       * `matched = min(lot.remaining_qty, sell_qty)`.
       * Reduce `lot.remaining_qty` and `sell_qty` accordingly.
       * Derive cost consumed: `cost_ccy = matched × lot.cost_per_unit_ccy`, same for EUR.
       * Record a realized PnL entry containing account, instrument, lot opened/closed timestamps, matched quantity, proceeds, cost, and PnL in both currencies, proportionally allocating proceeds if the trade closed multiple lots.
       * Remove the lot from the queue when `remaining_qty` reaches zero.
     - If `sell_qty` remains after all lots are exhausted (short position or data gap), insert a `data_gaps` record (`gap_type = instrument_metadata`) capturing instrument and quantity.
  5. Persist each realized entry to `realized_pnl_fifo` after processing.
  6. For each instrument, the queue now represents open positions used for unrealized PnL and valuation.
- After processing transactions up to snapshot time:
  - Persist new realized entries into `realized_pnl_fifo` (dedupe by PK).
  - Update positions queue state for remaining lots.
  - Compute cash balance using trade net amounts and cash movements since previous snapshot (see rules above).
- `realized_pnl_eur` in `portfolio_value_snapshot` = cumulative sum of realized entries pnl_eur where `lot_closed_at <= snapshot_at`.
- `realized_pnl_eur` in `portfolio_value_snapshot` = prior cumulative realized PnL (`previous_realized(account)`) plus the sum of new `pnl_eur` values from `realized_pnl_fifo` entries where `lot_closed_at <= snapshot_at`.
- `positions_value_eur` = Σ (shares * price.close * fx_at_snapshot) for remaining lots.
- `unrealized_pnl_eur` = positions_value_eur - Σ (remaining lot cost in EUR).
- Record and clear `data_gaps` entries accordingly.
- Hourly (:05/:20/:35/:50 CET) plus manual CLI.
- Builds per-account state:
  - Replays transactions to maintain running positions and FIFO cost lots.
  - Aggregates cash balances from `cash_movements`.
  - Converts instrument values to EUR using latest price ≤ snapshot timestamp and FX rate (falling back to previous day when same-day rate unavailable).
  - Computes unrealized PnL (`positions_value_eur - cost_basis_eur`) and includes realized PnL reported by FIFO matcher.
- Persists:
  - `positions_snapshot` entries (shares, cost basis).
  - `portfolio_value_snapshot` rows with positions value, cash value, NAV, unrealized/realized PnL, and simple delta versus prior snapshot (no percent return).
  - `realized_pnl_fifo` rows for closed lots, tagging the snapshot in which the close occurred.
- Emits `data_gaps` entries for any missing price, FX rate, or instrument metadata that prevents valuation, and removes gaps when values become available.

### 4.6 Backfill Service
- CLI-driven historical refresh (prices, FX, snapshots) with configurable days and scopes.
- Overwrite existing prices/FX/snapshots when re-running backfills (no append-only behaviour).
- Remove `data_gaps` rows as soon as backfill supplies the missing data.

### 4.7 Instrument Metadata Updater
- Daily 03:30 CET job plus CLI.
- Refreshes sector, region, exchange; persists to `instruments`.

## 5. Monitoring & Dashboards
### 5.1 Grafana Overview
- Provisioned dashboards for portfolio value, PnL, exposures, trades.
- Datasource uses read-only Postgres credentials.

### 5.2 Missing Data Board
- Dashboard visualising outstanding gaps from `data_gaps`.
- Panels must include:
  1. Total gap count by `gap_type` (price, fx_rate, instrument_metadata).
  2. Table with instrument/account identifiers, `target_timestamp`, and actionable `details` (e.g., ticker to backfill).
  3. Time series of gap count over past 7 days.
  4. Age of oldest gap (highlight when > 24h).

### 5.3 Alerts & Logging
- Log warnings for missing price/FX/snapshot/instrument data alongside entries in `data_gaps`.
- Optional Grafana alerting (future scope) off gap counts or stale price data.

## 6. Operations & Maintenance
### 6.1 Scheduler Cadence
- Maintain APScheduler cron setup as documented; jobs must be idempotent and safe under delayed execution.
### 6.2 Secret Rotation
- Document process for updating secrets (rerun helper script locally; orchestrator-managed in prod).
### 6.3 Backup & Restore
- Nightly `pg_dump` script with retention policy; outline restore steps into a fresh database.
### 6.4 Health & Observability
- Continue container healthchecks (Postgres `pg_isready`, Grafana HTTP).
- Encourage log shipping/rotation; integrate MCP and ETL logs into troubleshooting docs.
