# Repository Guidelines

## Project Structure & Module Organization
Core ETL code sits in `etl/app/`, with importers, pricing, FX, snapshots, and shared config/db/logger modules split per file. Container packaging resides in `etl/Dockerfile`, `pyproject.toml`, and `uv.lock`. Long-form docs live in `docs/wiki/`, Grafana JSON in `grafana/dashboards/`, and helper utilities (e.g., `create-dev-secrets.sh`, `check_yf_tickers.py`) in `scripts/`. Use `imports/` for raw CSVs and keep credentials under the gitignored `secrets/` directory.

## Build, Test, and Development Commands
Run `./scripts/create-dev-secrets.sh` before spinning up services. Use `docker compose up -d` to launch PostgreSQL, Grafana, and the ETL scheduler, and `docker compose ps` to confirm health. Rebuild the ETL image after code or dependency changes with `docker compose build etl`. Execute one-off jobs via `docker compose run --rm etl <command>` (e.g., `flex-import`, `price-update`, `snapshot-recompute`). Shut the stack down with `docker compose down` (add `-v` to drop volumes).

## Coding Style & Naming Conventions
Python modules target 3.11 with type hints and 4-space indentation; mirror the patterns in `config.py` and `main.py`. Use snake_case for functions and variables, PascalCase for classes (`FlexImporter`, `PriceUpdater`), and kebab-case for CLI subcommands to match argparse definitions. Keep logging through `logger.configure_logging` and `logging.getLogger(__name__)`. Format Grafana JSON with 2-space indentation and keep panel/datasource IDs stable.

## Testing Guidelines
Place unit tests alongside features under an `etl/tests/` package using `pytest`. Run them locally with `uv run pytest` (requires Astral `uv`) or inside the container via `docker compose run --rm etl pytest`. For integration checks, rely on domain commands such as `flex-import` or `price-update` against the compose stack; store sample inputs in `imports/` and document expected row counts. Prioritise coverage of parsing edge cases, scheduler triggers, and DB interactions built on `psycopg`.

## Commit & Pull Request Guidelines
Follow the concise, imperative style seen in `git log` (`Add instrument metadata refresh`, `Add fx rate fetching…`). Keep each commit focused, bundling related config updates when necessary. Pull requests should state intent, list validation steps, link issues, and attach Grafana screenshots when dashboards change. Call out any secrets, migrations, or cron adjustments reviewers must replicate.

## Secrets & Configuration Tips
Never commit files under `secrets/`; document new keys in `README.md` and update `scripts/create-dev-secrets.sh` when adding credentials. Expose new configuration via environment variables, thread them through `config.py`, and document defaults. Scrub logs and example data for sensitive fields before submitting PRs.
