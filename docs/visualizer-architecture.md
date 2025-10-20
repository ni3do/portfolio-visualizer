# Portfolio Visualizer Architecture

## 0. Context & Goals
- Replace the legacy Grafana-only dashboard with a FastAPI backend and Angular 17 PWA while retaining the Python ETL and database foundation.
- Keep feature parity with existing dashboards during the transition; use this document alongside `docs/spec.md` for requirement traceability.
- Enable a modular deployment that lets teams run Grafana in parallel for regression testing until the Angular UI is validated.

## 1. Target Architecture (FastAPI + Angular)
### 1.1 Docker Compose Services
- `postgres`: PostgreSQL 15 with persistent `pg_data` volume and healthcheck probes.
- `etl`: Python 3.11 container running APScheduler jobs and CLI importers.
- `visualizer-api`: FastAPI application (Uvicorn) exposing REST endpoints that supersede Grafana SQL.
- `visualizer-web`: Angular 17 PWA built with Angular Material, served via nginx in production and `ng serve` in the `dev` profile.
- `grafana` (legacy profile): optional reference dashboards until the Angular UI reaches parity.
- `mcp`: standalone read-only Model Context Protocol server (`http://localhost:8000/mcp`) for internal debugging; not proxied by the API.

### 1.2 Networking & Secrets
- Compose services share an internal bridge network; expose FastAPI on `:8080`, Angular on `:8081` (`:4200` in dev), Grafana on `:3000`, MCP on `:8000`.
- Secrets live under `/run/secrets/*` and are provisioned by `./scripts/create-dev-secrets.sh` or Make targets.
- Required secrets: Postgres app credentials, JWT signing key, Grafana admin credentials (legacy), IBKR Flex token & query ID, optional OAuth/OIDC client config.

### 1.3 Persistent Storage & Backups
- Volumes: `pg_data`, `etl_data` (`/data/flex_archive`, price cache), `grafana_data`, and an `angular_dist` bind mount for local HMR if needed.
- Nightly `pg_dump` of the `portfolio` database stored on the host with 14-day retention; document restore walkthroughs with credentials handling.

### 1.4 Data Flow Overview
1. **ETL ingestion** pulls IBKR Flex, Swissquote, and yfinance data into Postgres (see §2 and §4).
2. **FastAPI (`visualizer-api`)** reads through async SQLModel/SQLAlchemy sessions, applies caching, and exposes endpoints listed in §1.5.
3. **Angular PWA** consumes those endpoints via typed services and RxJS stores to render dashboards and tables.
4. **Legacy Grafana** can run concurrently for validation during the cutover period.

### 1.5 API Surface
All endpoints are prefixed with `/api` and respond with JSON unless stated otherwise.

| Endpoint | Method | Description | Backing Query / Notes |
| --- | --- | --- | --- |
| `/healthz` | GET | Liveness check for Compose and ingress probes. | Static JSON response. |
| `/auth/login` | POST | Issues JWT in an HTTP-only cookie after credential validation. | Flow depends on final auth provider choice. |
| `/portfolio/value` | GET | Portfolio NAV time series (`from`, `to`, `interval`). | Parameterised version of Grafana panel 1 SQL. |
| `/portfolio/unrealized` | GET | Latest unrealized PnL per instrument with filters. | Shared valuation pipeline from panels 2 and 7. |
| `/portfolio/exposure/country` | GET | Country allocation snapshot with EUR totals and weights. | Grafana panel 4 SQL. |
| `/portfolio/exposure/sector` | GET | Sector allocation snapshot. | Grafana panel 5 SQL. |
| `/portfolio/exposure/currency` | GET | Currency exposure snapshot. | Grafana panel 6 SQL. |
| `/portfolio/positions` | GET | Paginated holdings table with sort and filter params. | Extended panel 7 SQL plus pagination helpers. |
| `/transactions/recent` | GET | Recent trades (`limit`, default 25). | Grafana panel 3 SQL. |
| `/metrics/cache` | GET | Cache hit/miss diagnostics (protected). | Optional ops-only endpoint. |

### 1.6 Frontend Modules & UX
- Layout shell: Angular Material `mat-sidenav`, `mat-toolbar`, and responsive `mat-grid-list`.
- Feature modules: `dashboard`, `positions`, `trades`, `auth`, `settings` with lazy-loaded routes.
- Visualization: Prefer `ng2-charts` (Chart.js) or `ngx-charts` for time series, donut, and bar charts; tables use `MatTable` with virtual scrolling and filtering.
- State & caching: Centralize API calls in services using `HttpClient` + RxJS `shareReplay`; persist offline data through service worker data groups or IndexedDB helpers.
- PWA: Enable `@angular/pwa` for service workers, install prompts, background sync, and stale-data banners.
- Production nginx build proxies `/api/*` to the FastAPI service; dev profile uses CORS-enabled direct calls to `http://localhost:8080`.

### 1.7 Authentication & Authorization
- Introduce a read-only Postgres role `visualizer_ro`; ETL retains elevated roles for writes.
- FastAPI exposes basic auth for the initial rollout; document credential rotation in secrets management.
- Keep the `/auth/login` scaffolding in place for a future JWT flow (per `docs/spec.md`) once an IdP decision is made.
- Angular route guards will be wired for the eventual JWT flow; in the interim, protect routes via interceptors that attach basic auth credentials.

### 1.8 Deployment Profiles & Compose Updates
- Extend `docker-compose.yml` with `visualizer-api` and `visualizer-web` services under `services/api/` and `services/web/`, plus an optional `visualizer-web-dev` profile for HMR.
- Compose profiles: `dev` (Angular HMR via `visualizer-web-dev`, FastAPI can be overridden with `--reload`) and `prod` (nginx-serving built PWA).
- Move `grafana` behind a `legacy` profile (`docker compose --profile legacy up`) and add secrets `visualizer_basic_auth_user` / `visualizer_basic_auth_password` alongside existing Postgres credentials.
- Expose FastAPI CORS origins via `VISUALIZER_CORS_ORIGINS` (comma-separated, defaults to `http://localhost:4200,http://localhost:8081`).

### 1.9 Open Integration Questions
- Finalise the long-term auth provider (self-managed JWT vs. external IdP) and update both this doc and `docs/spec.md` when ready.
- Document any future Grafana cutover checklist once the owner approves decommissioning.

## 2. Data Sources & Ingestion
### 2.1 IBKR Flex Web Service
- Retrieve CSV/XML statements using Flex token and query ID.
- Parse instruments, transactions, cash movements, and FX rates; archive raw files under `/data/flex_archive`.
- Support manual ingestion through `flex-import`/`import-ibkr`.

### 2.2 Swissquote CSV Import
- Provide parity with the Flex importer, including schema ensure, upserts, and optional backfills.
- CLI command `import-swissquote` handles delimiter/timezone variants, writes transactions and cash data, and triggers snapshot recomputation.

### 2.3 Market Data via yfinance
- Primary source for prices and FX; respect rate limits and allow override via `instruments.yfinance_symbol`.
- Capture both daily (`1d`) and hourly (`60m`) candles for held instruments; retain hourly data for intraday PnL and document storage monitoring.
- Persist source timestamp, close price, and currency; fall back to daily close when hourly bars are unavailable.

### 2.4 MCP Exposure
- Maintain the standalone MCP server (`run_sql_query` endpoint) strictly for internal debugging; FastAPI does not proxy or surface MCP capabilities to the frontend.

## 3. Database & Read Models
- `portfolio_value_snapshot` contains `snapshot_at`, `account_id`, `positions_value_eur`, `cash_value_eur`, `nav_eur`, `unrealized_pnl_eur`, `realized_pnl_eur`, `delta_eur`, and `created_at` with PK on (`snapshot_at`, `account_id`).
- `realized_pnl_fifo` tracks FIFO lot closures with timestamps, quantities, proceeds, costs, PnL in native and EUR currencies; PK (`account_id`, `instrument_id`, `lot_opened_at`, `lot_closed_at`).
- `data_gaps` records missing price, FX, or metadata entries with `gap_type`, `target_timestamp`, `detected_at`, optional `instrument_id`/`account_id`, and `details`.
- Additional core tables: `instruments`, `transactions`, `cash_movements`, `prices`, `prices_hourly`, `fx_rates`, `positions_snapshot`.
- Maintain indexes tuned for API access patterns (e.g., `data_gaps(gap_type, target_timestamp)`, `realized_pnl_fifo(account_id, instrument_id, lot_closed_at)`).
- Cash value derives from the previous snapshot cash plus net trades and cash movements (converted to EUR at their FX rates).
- Unrealized PnL is computed as positions market value minus remaining open-lot cost; `delta_eur` equals `nav_eur` minus prior NAV for the same account.

## 4. ETL Workloads & Scheduling
### 4.1 Flex Importer
- Runs daily at 18:00/18:30 CET with manual CLI entry points.
- Upserts parsed entities, enforces schema migrations, and emits `data_gaps` when expected FX or instrument data is missing.

### 4.2 Swissquote Importer
- Mirrors Flex cadence and behaviour, supporting manual CSV ingestion and missing-data logging.

### 4.3 Price Updater
- Executes every 15 minutes (:00/:15/:30/:45 CET); builds holdings targets, fetches yfinance data, and upserts `prices`.
- Logs `data_gaps` when tickers lack metadata or yfinance fails to return data; downstream snapshot stage converts non-EUR valuations.

### 4.4 FX Updater
- Runs on a 15-minute offset (:10/:25/:40/:55 CET); sources FX exclusively from yfinance.
- Falls back to the most recent prior rate when same-day data is missing and records a `data_gaps` entry for later backfill.

### 4.5 Snapshot Recompute
- Prepares FIFO queues per account/instrument, processes transactions chronologically, and matches buys/sells against queues.
- Persists realized entries to `realized_pnl_fifo`, updates open lot state, computes cash balances, positions value, and unrealized PnL.
- Emits `data_gaps` when prices, FX, or positions are incomplete; scheduled hourly (:05/:20/:35/:50 CET) with manual CLI triggers.

### 4.6 Backfill Service
- CLI-driven refresh for prices, FX, and snapshots with configurable scope.
- Overwrites historical rows rather than append-only behaviour and clears `data_gaps` once missing data is supplied.

### 4.7 Instrument Metadata Updater
- Runs daily at 03:30 CET (and via CLI) to refresh sector, region, exchange fields in `instruments`.

### 4.8 Performance & Returns
- Track cash flows from transactions and external movements, storing normalized values in native and base currencies.
- Maintain realized/unrealized PnL via FIFO and snapshots.
- Compute simple returns, holdings change, NAV deltas, TWR, MWR/IRR, cumulative realized PnL, and contribution/withdrawal summaries; persist metrics per snapshot and instrument for dashboards, MCP, and CLI reports.

## 5. Grafana Parity & Validation
- Recreate Grafana panels as API endpoints and Angular components; map portfolio value, unrealized PnL, recent trades, country/sector/currency exposures, and open positions to the endpoints in §1.5.
- Capture the canonical SQL for legacy panels in `docs/spec.md` to ensure traceability between Grafana queries and API logic.
- Maintain the missing-data dashboard with counts by `gap_type`, detailed table view, seven-day trend, and oldest gap age.
- Operate Grafana under the `legacy` profile for side-by-side checks until the repository owner explicitly approves decommissioning.

## 6. Monitoring & Observability
- Angular components should surface discrepancy banners when API data diverges from Grafana during parallel runs.
- Log warnings for missing price/FX/snapshot/instrument data alongside `data_gaps` entries.
- Consider Prometheus instrumentation for FastAPI (`prometheus-fastapi-instrumentator`) and structured logging for ETL/API/frontend.
- Optional alerting (Grafana or future Angular tooling) for stale prices, outstanding gaps, or failed ETL jobs.

## 7. Operations & Maintenance
- Preserve APScheduler cron cadence; jobs must be idempotent and resilient to delayed execution.
- Document secret rotation (rerun helper script locally, orchestrator-managed in production) and include JWT rotation once auth is final.
- Maintain nightly `pg_dump` backup scripts with retention policy and documented restore steps.
- Keep container health checks (`pg_isready`, `/healthz`, Angular/nginx HTTP checks, Grafana HTTP when enabled) and encourage log shipping/rotation.

## 8. Delivery & Cutover Strategy
- Extend repo scripts to build/run the API and web services, plus convenience targets for tests and linting.
- Run FastAPI and Angular in parallel with Grafana, comparing key metrics daily via automated diff scripts and capturing discrepancies.
- When parity is validated, switch ingress or DNS to the Angular frontend, disable the `legacy` profile, archive Grafana dashboards, and document rollback steps.

## 9. Open Questions & Next Steps
- Finalise the long-term auth provider decision (see §1.9) and reflect it across docs.
- Capture the agreed Grafana decommissioning checklist once the owner signs off.
- Update `implementation-plan.md` once delivery phases and cutover checklists are final.
- Ensure new configuration surfaces through environment variables, documented defaults, and updates to `scripts/create-dev-secrets.sh`.
