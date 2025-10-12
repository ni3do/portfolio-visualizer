# Minimal Portfolio Dashboard (Grafana + Postgres + Flex + yfinance)

## Goals
- Single dashboard (mobile + desktop) for portfolio exposures and performance.
- No IBKR local gateway. Use Flex Web Service for history; yfinance for prices.
- 15-minute price updates, daily IBKR ingest. Dockerized. Simple to operate.

## Components (Docker Compose)
- `postgres`: PostgreSQL 15+ with a persistent volume.
- `etl`: Python 3.11 container running APScheduler jobs:
  - Flex Import (daily)
  - Price Updater (every 15 min)
  - Snapshot Recompute (nightly + post-price run)
- `grafana`: Grafana OSS, provisioned with:
  - PostgreSQL datasource
  - Starter dashboard JSON(s)
- (optional) `proxy`: Nginx/Traefik for HTTPS + auth in front of Grafana.

## Data Sources
- **IBKR Flex Web Service** (CSV/XML) via tokenized URL (no local gateway).
- **Market data**: `yfinance` for delayed quotes; batch symbols; cache responses.
- **FX rates**: ECB (preferred) or alternative daily feed; fail the ingest if required currency pairs are missing for the day.

## Scheduling (APScheduler)
- Prices: every 15 minutes at :00, :15, :30, :45 (Europe/Amsterdam timezone).
- Flex Import: daily at 18:00 Europe/Amsterdam (retry at 18:30).
- Snapshots: nightly at 18:30 Europe/Amsterdam and after each price job.
- Retries: exponential backoff with jitter; alert on repeated failures.

## Database (PostgreSQL) — Logical Schema
- `instruments(instrument_id PK, symbol, name, currency, asset_class, sector, country, region, primary_exchange, created_at)`
- `transactions(trade_id PK, account_id, date_time_utc, type, instrument_id FK, qty, price, currency, fees, net_amount, source, raw_flex_id)`
- `cash_movements(id PK, account_id, date_time_utc, currency, amount, type, source)`
- `prices(as_of_utc, instrument_id FK, close, currency, source, PRIMARY KEY(as_of_utc, instrument_id))`
- `fx_rates(date_utc, from_ccy, to_ccy, rate, source, PRIMARY KEY(date_utc, from_ccy, to_ccy))`  _(required for v1; add guardrail alert if data missing)_
- `positions_snapshot(snapshot_at TIMESTAMPTZ, account_id, instrument_id, shares, cost_basis_ccy, cost_basis_eur, PRIMARY KEY(snapshot_at, account_id, instrument_id))`
- `portfolio_value_snapshot(snapshot_at TIMESTAMPTZ, account_id, value_eur, ret, drawdown, PRIMARY KEY(snapshot_at, account_id))`

**Indexes**
- `transactions(date_time_utc)`, `prices(instrument_id, as_of_utc)`, `positions_snapshot(snapshot_at)`.
- Consider partial index on recent `prices` (last 30 d) for faster queries.

## ETL Jobs (high-level)
### 1) Flex Import (daily)
- Fetch Flex CSV/XML via token URL (token + format configured in IBKR portal).
- Parse to `transactions`, `cash_movements`, `instruments`:
  - Upsert by (`trade_id`) for transactions; de-dup by (`account_id`,`date_time`,`amount`) for cash.
  - Maintain symbol↔instrument_id map; fill metadata (sector/region) when available.
- Capture corporate actions (splits, dividends, symbol changes) from Flex notes; flag gaps that require manual enrichment so holdings/cost basis stay aligned.
- Persist cash activity for dividends, withholding tax, deposits/withdrawals, and fees (attach descriptive text for Grafana tables).
- Store FX rates section daily for downstream valuation; alert if currency coverage is incomplete.
- Provide on-demand CLI importers for IBKR Flex files (`import-ibkr`) and Swissquote CSVs (`import-swissquote`) to standardise historical data ingestion.
- Provide on-demand backfill command to load historical prices/FX via yfinance for dashboard history.
- Store the raw Flex file (dated) in `/data/flex_archive` for audits.

### 2) Price Updater (every 15 min)
- Build symbol list from `instruments` with holdings present (shares>0) + watchlist/benchmarks.
- Batch by exchange/market if needed; call yfinance; upsert into `prices`.
- Normalize all price timestamps to UTC and store the original exchange time for audits; explicit conversion avoids off-by-one joins around markets that close after 00:00 UTC.
- Allow manual override via `instruments.yfinance_symbol` when IBKR symbols do not map 1:1 to Yahoo tickers; default to the Flex symbol when unset.
- Optionally compute 1D/1W/1M returns for holdings as convenience columns.
- Throttle to respect rate limits; cache last ETag if possible.

### 3) Snapshot Recompute
- From `transactions` + `cash_movements` compute daily shares per instrument per account.
- Join with `prices` (latest <= snapshot timestamp) and `fx_rates` (daily). if either is missing, capture a failed snapshot with alert.
- Write `positions_snapshot` and `portfolio_value_snapshot` (also compute drawdown series).
- Maintain a recompute watermark so the job can resume after failures; prefer incremental roll-ups over full rebuilds on every price update to keep runtime predictable (full rebuild only for backfills).
- Store hourly snapshots (top-of-hour) to support intraday monitoring and net-worth charts.
- Base currency defaults to EUR; allow override via config for users with different reporting currency.

## Grafana
- Provision PostgreSQL datasource (read-only user).
- Dashboards:
  - **Overview**: total value (EUR), 1D/MTD/YTD return, value line vs benchmark, drawdown.
  - **Exposures**: stacked area or treemap by asset_class/sector/region; table of weights.
  - **Performance**: daily returns, rolling vol (30/90/252d).
  - **Holdings**: table with weight, last price timestamp, 1D/1W/1M change.
- Variables: `account`, `base_ccy (EUR)`, `date_range`.

## Security
- Run Grafana behind reverse proxy with TLS (Let’s Encrypt) and auth (basic/OIDC).
- Secrets: DB creds, Grafana admin, Flex token → Docker secrets mounted read-only (files under `/run/secrets/*`); document rotation via `docker secret update` and ensure local dev uses the same path conventions (helper script generates secrets for compose overrides if needed).

## Ops & Backups
- Nightly `pg_dump` to `/backup` (retain 14 days).
- Container healthchecks: Postgres TCP, ETL `/healthz`, Grafana HTTP 200.
- Logs shipped to files; rotate weekly.

## Environments & Config
- `.env` reserved for non-secret defaults (timezone, log level, feature flags) with Docker secrets supplying all credentials; provide a fallback `.env.local` template for air-gapped demos but call out the security trade-offs explicitly.
- Runtime env vars: tune `PRICE_BATCH_SIZE`, `PRICE_HISTORY_INTERVAL`, `SNAPSHOT_BASE_CCY`, `SNAPSHOT_TIMEZONE` per deployment.
- Volumes:
  - `pg_data` (Postgres)
  - `etl_data` (`/data/flex_archive`, small cache for prices)
  - `grafana_data` (dashboards, plugins)

## Milestones
- **M0 (Day 1–2):** Compose up Postgres + Grafana; datasource connected; empty dashboards.
- **M1 (Day 2–3):** Flex Import populates `transactions`/`instruments`; one user’s historical data loaded.
- **M2 (Day 3–4):** Price Updater running 15-min; `positions_snapshot`/`portfolio_value_snapshot` filled.
- **M3 (Day 5):** Overview + Exposures dashboards complete; mobile view tested.
- **M4 (Day 6+):** Add benchmark(s), basic alerts (e.g., drawdown > X%), optional FX.

## Notes / Caveats
- `yfinance` is **unofficial**; acceptable for research dashboards but can break/limit. Keep the interface swappable for a paid feed later.
- IBKR **Client Portal API requires a local gateway**; we intentionally avoid it. Flex Web Service is the supported batch path.
- Normalize to **UTC** in DB; render in Europe/Amsterdam in Grafana.
