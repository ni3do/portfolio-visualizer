# Minimal Portfolio Dashboard

This repository follows the implementation plan in `implementation-plan.md`. Stage **M0** sets up the base Docker Compose stack with PostgreSQL and Grafana, pre-provisioned with an empty dashboard and a PostgreSQL datasource.

## Prerequisites

- Docker Engine 24+ and Docker Compose Plugin.
- `openssl` **or** Python 3 (for generating secrets).

## Secrets Setup

Secrets are mounted into containers via Docker secrets and stored as plain files under `./secrets/`, which is `.gitignore`'d.

1. Generate development secrets (idempotent):

   ```bash
   ./scripts/create-dev-secrets.sh
   ```

   Use `--overwrite` to regenerate existing files if needed.

2. Inspect the files under `./secrets/` to confirm they look correct (one value per file, no trailing newline changes).

For production, create secrets through your orchestrator (`docker secret create`, Swarm, or Kubernetes) and ensure they resolve to the same secret names used in `docker-compose.yml`.

## Running Stage M0

1. Start the stack:

   ```bash
   docker compose up -d
   ```

2. Confirm both containers are healthy:

   ```bash
   docker compose ps
   ```

3. Navigate to Grafana at [http://localhost:3000](http://localhost:3000) and sign in with the credentials from `secrets/grafana_admin_user` and `secrets/grafana_admin_password`.

4. Verify that the **Portfolio Postgres** datasource is connected (Grafana → Connections → Data sources). It should point at `postgres:5432/portfolio`.

5. The provisioned **Overview** dashboard is empty by design and acts as the scaffold for later milestones.

## Cleaning Up

```bash
docker compose down
```

Use `docker compose down -v` to remove the persistent volumes (`pg_data`, `grafana_data`) if you want a clean slate.

## Stage M1 – Flex Import

1. Replace the placeholder contents in `secrets/ibkr_flex_token` and `secrets/ibkr_flex_query_id` with the values from the IBKR Flex Web Service (see guide below).
2. Rebuild the ETL image after changing secrets or code:

   ```bash
   docker compose build etl
   ```

3. Run a one-off Flex import to validate connectivity and parsing:

   ```bash
   docker compose run --rm etl flex-import
   ```

   Check the logs for the number of instruments, transactions, cash movements, and FX rates imported.

4. Once satisfied, start the long-running scheduler:

   ```bash
   docker compose up -d etl
   ```

   The scheduler triggers the Flex import daily at 18:00 Europe/Amsterdam (with a retry at 18:30). View logs via `docker compose logs -f etl`.

The raw Flex files are archived under the `etl_data` volume (`./secrets` keeps credentials; `./data/flex_archive` inside the container).

## Stage M2 – Price Updater (in progress)

1. Rebuild the ETL image to pick up the yfinance dependencies:

   ```bash
   docker compose build etl
   ```

2. Trigger a one-off price run to populate `prices`:

   ```bash
   docker compose run --rm etl price-update
   ```

   The logs will report how many instruments were updated and how many tickers failed. Inspect the results with:

   ```bash
   docker compose exec postgres \
     psql -U "$(cat secrets/postgres_app_user)" -d portfolio \
     -c "select instrument_id, as_of_utc, close, currency from prices order by as_of_utc desc limit 10;"
   ```

3. Backfill historical prices/FX if needed:

   ```bash
   docker compose run --rm etl backfill --days 365 --snapshots
   ```

   Use `--prices-only`, `--fx-only`, or `--snapshots` to target specific datasets.

4. Recompute a snapshot (optional, runs automatically after each price cycle):

   ```bash
   docker compose run --rm etl snapshot-recompute
   ```

   Inspect results:

   ```bash
   docker compose exec postgres \
     psql -U "$(cat secrets/postgres_app_user)" -d portfolio \
     -c "select snapshot_at, account_id, instrument_id, shares, cost_basis_eur from positions_snapshot order by snapshot_at desc limit 10;"
   ```

Snapshots now run on an hourly cadence (each snapshot captures prices at the top of the hour).

5. Restart the long-running service to enable the 15-minute schedule:

   ```bash
   docker compose up -d etl
   ```

   Prices are refreshed every 15 minutes; the Flex import still runs daily at 18:00 Europe/Amsterdam and snapshots trail each price cycle.

### Ticker Mapping

- By default the importer stores the IBKR symbol in `instruments.symbol`. Set `instruments.yfinance_symbol` manually to the corresponding Yahoo Finance ticker (e.g., `update instruments set yfinance_symbol = 'NOVO-B.CO' where symbol = 'NOVOBc';`).
- Environment knobs:
  - `PRICE_BATCH_SIZE` (default `16`)
  - `PRICE_HISTORY_PERIOD` (default `5d`)
  - `PRICE_HISTORY_INTERVAL` (default `15m`)
  - `PRICE_SOURCE` (default `yfinance`)
  - `SNAPSHOT_BASE_CCY` (default `EUR`)
  - `SNAPSHOT_TIMEZONE` (default `Europe/Amsterdam`)
  - `YF_CACHE_DIR` (default `/tmp/yfinance_cache` inside the container)

If yfinance starts rate-limiting, clear cached cookies and rerun the importer:

```bash
docker compose run --rm etl clear-cache
docker compose run --rm etl price-update
```

## Manual Imports (IBKR & Swissquote)

Imports automatically trigger a 365-day price/FX backfill and hourly snapshot rebuild unless `--no-backfill` is provided.

1. Drop the CSV file into `./imports/` (automatically mounted at `/data/imports` inside the ETL container).
2. Run one of the import commands (both trigger price/FX/snapshot backfill unless `--no-backfill` is set):
   ```bash
   docker compose run --rm etl import-ibkr --file /data/imports/flex.csv
   docker compose run --rm etl import-swissquote --file /data/imports/swissquote.csv --timezone Europe/Zurich --delimiter ";"
   ```
3. Inspect data or rerun a snapshot if required. A default 365-day backfill (prices, FX, snapshots) executes automatically. Adjust via `--backfill-days` or skip with `--no-backfill`.

   Flags:
   - `--backfill-days`: override the default 365-day window.
   - `--no-backfill`: skip automatic backfill/snapshot rebuild.
   - `--delimiter` / `--timezone`: adjust Swissquote parsing if needed.

## Monitoring & Dashboards

- The Grafana dashboard `Portfolio Monitoring` (see `grafana/dashboards/overview.json`) shows price staleness, ingest activity, snapshot freshness, and account coverage.
- Access Grafana at http://localhost:3000 and import the provisioned dashboard automatically after `docker compose up -d`.
- Panels refresh every minute; most queries use the `prices`, `portfolio_value_snapshot`, and related tables, so ensure those tables are populated first.

## Documentation

More detailed notes live under `docs/wiki/`:

- [Home](docs/wiki/Home.md)
- [Architecture](docs/wiki/Architecture.md)
- [ETL Jobs](docs/wiki/ETL-Jobs.md)
- [Monitoring & Dashboards](docs/wiki/Monitoring.md)
- [Operations](docs/wiki/Operations.md)

## IBKR Flex Web Service Setup

1. Sign in to the Interactive Brokers Client Portal and navigate to **Performance & Reports → Flex Queries**.
2. Create a **Flex Statement** query:
   - Sections: include at minimum **Trades**, **Cash Transactions**, **Corporate Actions**, **Dividends**, **Withholding Tax**, and enable **Include Currency Rates** so FX pairs are present.
   - Date range: choose `Last 30 Days` or a custom range that suits your backfill requirements.
   - Output format: `CSV`.
   - Time zone: set to `UTC` for consistency with the database.
   - Save the query and note the **Query ID**.
3. From the same page, open the **Flex Web Service** tab, create a new token, and record the generated **Flex Web Service Token**. Tokens expire periodically (default 90 days), so plan rotation.
4. Update `secrets/ibkr_flex_query_id` with the Query ID and `secrets/ibkr_flex_token` with the token. Keep these secret—anyone with both values can download your statements.
5. If you rotate the token or adjust the query, regenerate the secrets and redeploy (`docker compose build etl && docker compose up -d etl`).

## Next Steps

- Verify Flex imports populate `transactions` and `instruments`, then backfill historical data as needed.
- Implement the price updater and snapshot recompute jobs (M2) before fleshing out Grafana dashboards (M3).
