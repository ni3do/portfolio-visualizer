# ETL Jobs

All jobs live under `etl/app`. This page summarises their behaviour, inputs, and tables touched.

## Flex Import (`FlexImporter`)

- **Command:** `docker compose run --rm etl flex-import`
- **Schedule:** Daily 18:00 Europe/Amsterdam + 18:30 retry.
- **Config:** `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, `FLEX_ARCHIVE_DIR`.
- **Tables:** `instruments`, `transactions`, `cash_movements`, `fx_rates`.
- **Artifacts:** Archives raw Flex CSV to `/data/flex_archive/flex_<reference>.csv`.
- **Notes:**
  - Corporate actions (splits/dividends) are captured in cash movements; enrich manually when metadata missing.
  - `yfinance_symbol` can be populated later for non-IBKR symbols.

## Price Update (`PriceUpdater`)

- **Command:** `docker compose run --rm etl price-update`
- **Schedule:** Every 15 minutes.
- **Config knobs:** `PRICE_BATCH_SIZE`, `PRICE_HISTORY_PERIOD`, `PRICE_HISTORY_INTERVAL`, `YF_CACHE_DIR`.
- **Workflow:**
  1. Clear yfinance caches (both `/tmp/yfinance_cache` and `~/.cache/yfinance`).
  2. Query holdings via `db.get_price_targets` (only open positions).
  3. For each ticker, call `fast_info`, fallback to `history(period="5d", interval="1d")`.
  4. Upsert into `prices` with UTC timestamps.
- **Operational tip:** run `docker compose run --rm etl clear-cache` before manual price updates if Yahoo starts returning `Too Many Requests`.

## Snapshot Recompute (`SnapshotRecalculator`)

- **Command:** `docker compose run --rm etl snapshot-recompute`
- **Schedule:** 5 minutes after each price update.
- **Outputs:**
  - `positions_snapshot(snapshot_at, account_id, instrument_id, shares, cost_basis_ccy, cost_basis_eur)`
  - `portfolio_value_snapshot(snapshot_at, account_id, value_eur, ret, drawdown)`
- **Logic:**
  1. Rebuild cumulative shares per instrument using `transactions` and `net_amount` for cost basis.
  2. Grab latest prices ≤ snapshot midnight and matching FX rates.
  3. Persist positions and aggregate portfolio metrics.
- **Assumptions:** FX rates exist for all instrument currencies (daily feed); otherwise the instrument is skipped.

## Supporting Commands

- `python -m app.main scheduler` (default in container) -> runs APScheduler loop.
- `docker compose run --rm etl flex-import --skip-schema` -> skip migrations if schema already ensured.
- `docker compose run --rm etl clear-cache` -> wipe yfinance cache directories without running a job.

## Historical Backfill (`BackfillService`)

- **Command:** `docker compose run --rm etl backfill --days 365 --snapshots`
- **Additional ranges:** use `--start-date YYYY-MM-DD` (and optional `--end-date`) to backfill an absolute window, e.g. `--start-date 2010-01-01 --snapshots`.
- **Options:** `--prices-only`, `--fx-only`
- **Behaviour:** Clears yfinance caches, downloads daily closes for each mapped instrument and FX pair, and upserts into `prices` and `fx_rates`.
- **Use cases:** Initial portfolio onboarding or refreshing gaps caused by downtime.
## Manual CSV Importers

- **IBKR Flex file:** `docker compose run --rm etl import-ibkr --file /data/imports/flex.csv`
- **Swissquote CSV:** `docker compose run --rm etl import-swissquote --file /data/imports/file.csv --timezone Europe/Zurich`

Both commands automatically trigger a 365-day price/FX backfill and hourly snapshot rebuild (disable via `--no-backfill`, tune with `--backfill-days`, or provide `--backfill-start-date` / `--backfill-end-date` for very old data).
