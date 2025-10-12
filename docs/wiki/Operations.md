# Operations

## Daily

- Ensure Docker stack is running: `docker compose up -d`
- Check Grafana dashboard for freshness (minutes since last price update should stay green).

## Manual Imports

CSV files should be placed in `./imports/` (mounted as `/data/imports`).

```bash
# Flex
docker compose run --rm etl flex-import

# Prices
docker compose run --rm etl clear-cache
docker compose run --rm etl price-update

# Swissquote CSV
docker compose run --rm etl import-swissquote --file /data/imports/swissquote.csv --timezone Europe/Zurich --backfill-days 365

# IBKR Flex CSV
docker compose run --rm etl import-ibkr --file /data/imports/flex.csv

# Backfill / snapshots (automatically triggered by imports)
docker compose run --rm etl backfill --days 365 --snapshots
```

Import commands automatically trigger a 365-day price/FX backfill and snapshot rebuild (override with `--backfill-days` or skip via `--no-backfill`).

## Inspecting Data

```bash
# Example: latest prices
docker compose exec postgres \
  psql -U "$(cat secrets/postgres_app_user)" -d portfolio \
  -c "select instrument_id, as_of_utc, close from prices order by as_of_utc desc limit 10;"

# Example: latest snapshot
docker compose exec postgres \
  psql -U "$(cat secrets/postgres_app_user)" -d portfolio \
  -c "select snapshot_at, account_id, value_eur from portfolio_value_snapshot order by snapshot_at desc limit 10;"

# Example: latest portfolio snapshot
docker compose exec postgres \
  psql -U "$(cat secrets/postgres_app_user)" -d portfolio \
  -c "select * from portfolio_value_snapshot order by snapshot_at desc limit 10;"
```

## Maintenance

- **Clear yfinance cache:** `docker compose run --rm etl clear-cache`
- **Rebuild ETL image:** `docker compose build --no-cache etl`
- **Reset volumes:** `docker compose down -v` (warning: destroys all data).
- **Rotate secrets:** edit files under `secrets/` and rerun `docker compose build etl`.

## Troubleshooting

- If prices fail with “possibly delisted”, confirm `instruments.yfinance_symbol` matches Yahoo ticker.
- For repeated 429 (“Too Many Requests”) responses, clear cache and rerun with a smaller `PRICE_BATCH_SIZE`.
- Verify schedules via logs: `docker compose logs -f etl` (look for “Scheduler started ...”).
