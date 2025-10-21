# Portfolio Dashboard Specification

## 0. Context & Transition
- The repository historically shipped a minimal Grafana dashboard backed by the Python ETL described in `implementation-plan.md` and the wiki. That legacy flow remains valuable for reference deployments.
- We are now migrating to a bespoke stack: FastAPI for the backend and an Angular 17 PWA (with Angular Material) for the frontend, as detailed in `docs/visualizer-architecture.md`.
- This specification bridges both worlds: it documents the target application requirements while preserving the ingestion, schema, and operational guidance needed by the ETL and any remaining Grafana panels.

## 1. Target Architecture (FastAPI + Angular)
### 1.1 Docker Compose Services
- `postgres`: PostgreSQL 15 with persistent `pg_data` volume and healthcheck.
- `etl`: Python 3.11 image running APScheduler jobs and CLI importers.
- `visualizer-api`: FastAPI application exposing REST endpoints that replace Grafana SQL queries.
- `visualizer-web`: Angular 17 PWA built with Angular Material; served by nginx in production, `ng serve` in dev profile.
- `grafana` (legacy/optional): retained behind a Compose profile for side-by-side validation until the Angular UI reaches parity.
- `mcp`: Read-only Model Context Protocol server (HTTP on `localhost:8000/mcp`) for ad-hoc SQL access; confirm whether it remains standalone or is proxied by the new API.

### 1.2 Networking & Secrets
- Internal bridge network; expose Angular on `:8120` (or `:4200` for dev) and FastAPI on `:8080`. Grafana remains on `:3000` when enabled, MCP on `:8000`.
- Secrets injected via Docker secrets under `/run/secrets/*`, provisioned by `./scripts/create-dev-secrets.sh` or equivalent Make targets.
- Required secrets: Postgres app user/password, JWT signing secret for the API, Grafana admin user/password (legacy), IBKR Flex token & query ID, optional OAuth/OIDC client config if an external IdP is selected.

### 1.3 Persistent Storage & Backups
- Volumes: `pg_data`, `etl_data` (`/data/flex_archive`, price cache), `grafana_data` (legacy dashboards), and an `angular_dist` bind mount for local dev if hot reloading is needed.
- Nightly `pg_dump` of the `portfolio` database into a host path (retain 14 days). Document rotation/restore steps alongside credentials management.

### 1.4 Data Flow Overview
1. **ETL ingestion** pulls Flex, Swissquote, yfinance data into Postgres (see §2 & §4).
2. **FastAPI service** reads from Postgres using async SQLAlchemy/SQLModel, applies caching, and exposes REST endpoints defined in §1.5.
3. **Angular PWA** consumes the API via typed services and RxJS stores, rendering dashboards that supersede Grafana panels.
4. **Optional Grafana** can be run in parallel for regression checks until decommissioned.

### 1.5 API Surface
All endpoints are prefixed with `/api` and return JSON unless noted.

| Endpoint | Method | Description | Backing Query / Notes |
| --- | --- | --- | --- |
| `/healthz` | GET | Liveness check for Compose/ingress probes. | Static JSON response. |
| `/auth/login` | POST | Issues JWT (HTTP-only cookie) after credential validation. | Depends on final auth provider decision. |
| `/portfolio/value` | GET | Portfolio NAV time series (`from`, `to`, `interval` params). | Derived from Grafana panel 1 SQL with parameterised buckets. |
| `/portfolio/unrealized` | GET | Latest unrealized PnL per instrument with filters. | Shared valuation pipeline from panels 2/7. |
| `/portfolio/exposure/country` | GET | Country allocation snapshot with EUR totals & weights. | Grafana panel 4 SQL. |
| `/portfolio/exposure/sector` | GET | Sector allocation snapshot. | Grafana panel 5 SQL. |
| `/portfolio/exposure/currency` | GET | Currency exposure snapshot. | Grafana panel 6 SQL. |
| `/portfolio/positions` | GET | Paginated holdings table (sort/filter params). | Extended panel 7 SQL + pagination helper. |
| `/transactions/recent` | GET | Recent trades (`limit` param, default 25). | Grafana panel 3 SQL. |
| `/metrics/cache` | GET | Cache hit/miss diagnostics (protected). | Optional; expose only in ops profile. |

### 1.6 Frontend Modules & UX
- **Layout shell**: Toolbar, filters, navigation drawer, responsive grid using Angular Material (`mat-sidenav`, `mat-toolbar`, `mat-grid-list`).
- **Feature modules**: `dashboard` (NAV & exposures), `positions`, `trades`, `auth`, `settings`. Configure lazy loading with Angular routing.
- **Visualization**: Adopt `ng2-charts` (Chart.js) or `ngx-charts` for time series, donut, and bar charts with Material theming. Tables use `MatTable` with virtual scroll and filtering.
- **State & caching**: Centralize API interactions in Angular services using `HttpClient` + RxJS `shareReplay`. Persist offline data through Angular service worker data groups or IndexedDB helpers.
- **PWA**: Enable `@angular/pwa` for service worker, install prompts, and background sync of snapshot data. Provide offline banners when data is stale.

### 1.7 Authentication & Authorization
- Introduce a `visualizer_ro` Postgres role for read-only access; ETL retains elevated privileges.
- API authenticates via JWT stored in HTTP-only cookies. Login form posts to `/auth/login`; refresh token strategy TBD once IdP decision is final.
- Support optional basic auth for internal-only deployments. Document how tokens/credentials are rotated and stored in secrets.
- Angular guards protect routes, refresh tokens transparently, and surface logout when tokens expire.

### 1.8 Deployment & Compose Updates
- Add `visualizer-api` and `visualizer-web` services to `docker-compose.yml`, with build contexts `services/api/` and `services/web/` respectively.
- Provide profiles: `dev` (Angular HMR + FastAPI autoreload) and `prod` (nginx-served static build).
- Move `grafana` behind a `legacy` profile so it runs only when explicitly requested (`docker compose --profile legacy up`).
- Extend secrets definitions with `visualizer_api_env` (JWT secret, API settings) and reuse existing Postgres credentials.

### 1.9 Open Questions
- **Auth provider**: Decide between self-managed credentials versus an external IdP (Auth0/Okta). Document the final approach here and in `docs/visualizer-architecture.md`.
- **MCP integration**: Determine whether the FastAPI service should proxy or consolidate the existing `mcp` read-only SQL server.
- **Dashboard parity**: Confirm acceptance criteria for decommissioning Grafana (e.g., metric-by-metric validation checklist, user sign-off process).

## 2. Data Sources & Ingestion
### 2.1 IBKR Flex Web Service
- Fetch CSV/XML statements via Flex token & query ID.
- Parse instruments, transactions, cash movements, FX rates; archive raw files under `/data/flex_archive`.
- Support on-demand imports via CLI (`flex-import`, `import-ibkr`).

### 2.2 Swissquote CSV Import
- Treat as first-class ingestion path with parity to Flex importer.
- CLI command `import-swissquote` parses timezone/delimiter variants, stores transactions/cash data, and triggers the snapshot pipeline.

### 2.3 Market Data (yfinance)
- Primary price source. Respect rate limits, cache, and allow ticker overrides via `instruments.yfinance_symbol`.
- Capture both daily (`1d`) and hourly (`60m`) candles for held instruments. Hourly bars backfill the trailing 30 days and roll forward for intraday PnL. Persist with source timestamp, close price, and currency; fall back to daily close when `60m` data is unavailable. Retain hourly data indefinitely for now and document storage monitoring in operations runbooks.

### 2.4 MCP Exposure
- Provide read-only SQL access (`run_sql_query`) for debugging/analytics over HTTP (`Accept: application/json, text/event-stream`).

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
  - (`account_id TEXT`, `instrument_id BIGINT`, `lot_opened_at TIMESTAMPTZ`, `lot_closed_at TIMESTAMPTZ`, `close_snapshot_at TIMESTAMPTZ`, `qty_closed NUMERIC`, `proceeds_ccy NUMERIC`, `proceeds_eur NUMERIC`, `cost_ccy NUMERIC`, `cost_eur NUMERIC`, `pnl_ccy NUMERIC`, `pnl_eur NUMERIC`, `created_at TIMESTAMPTZ default NOW()`).
  Primary key: (`account_id`, `instrument_id`, `lot_opened_at`, `lot_closed_at`).
- `data_gaps` schema:
  - (`gap_type TEXT`, `target_timestamp TIMESTAMPTZ`, `detected_at TIMESTAMPTZ default NOW()`, `instrument_id BIGINT`, `account_id TEXT`, `details JSONB`).
  Primary key: (`gap_type`, `target_timestamp`, `instrument_id`, `account_id`). Allowed `gap_type`: `price`, `fx_rate`, `instrument_metadata`.
- Core tables:
  - `instruments` (extend to log metadata completeness; missing ticker metadata is captured in `data_gaps`).
  - `transactions`, `cash_movements` (source-of-truth for FIFO cost basis and cash balances).
  - `prices`, `fx_rates` (FX now sourced exclusively from yfinance).
  - `prices_hourly` (60-minute bars for intraday PnL; mirrors `prices` schema).
  - `positions_snapshot` (per-account, per-instrument shares/cost basis at snapshot timestamp).
  - `portfolio_value_snapshot` (per-account totals with required columns listed above; retain `ret`/`drawdown` if needed for historical Grafana parity).
- Maintain indexes aligned with query patterns (e.g., `data_gaps(gap_type, target_timestamp)`, `realized_pnl_fifo(account_id, instrument_id, lot_closed_at)`).
- Cash balance per account is derived as: `cash_value_eur = previous_snapshot_cash + Σ cash_movements.amount (converted to EUR at movement timestamp FX) + Σ transactions.net_amount for trades since previous snapshot (converted to EUR at trade timestamp FX)`.
- Unrealized PnL per snapshot: `unrealized_pnl_eur = positions_value_eur - Σ open lot cost in EUR at snapshot time` (uses FIFO queue state).
- Realized PnL accumulation uses FIFO matching; see §4.5 for algorithm detail.
- `delta_eur = nav_eur - prior nav_eur` for the same account (absolute change).
- FX rates are sourced from yfinance; if the desired date is missing, use the most recent prior business day (max 1 day lookback). Retain raw Flex FX data for audits.

## 4. ETL Workloads
### 4.1 Flex Importer
- Scheduled daily 18:00/18:30 CET; CLI available for manual runs.
- Upserts parsed entities, runs schema migrations, triggers optional backfill.
- Logs metrics & writes missing-data gaps when expected FX rates or instruments are absent.

### 4.2 Swissquote Importer
- Mirrors Flex behaviour (schema ensure, upserts, optional backfill).
- Supports manual CSV ingestion; integrates missing-data logging.

### 4.3 Price Updater
- 15-minute cadence (:00/:15/:30/:45 CET).
- Builds holdings-target list, fetches yfinance data, upserts `prices`.
- Logs gaps when ticker metadata is missing or yfinance returns no data (record in `data_gaps`).
- Converts non-EUR valuations in downstream snapshot stage.

### 4.4 FX Updater
- 15-minute offset schedule (:10/:25/:40/:55 CET).
- Source FX exclusively from yfinance; store close-of-day rate for each required currency pair.
- If the desired date is missing, fall back to the most recent prior rate (record a `data_gaps` entry so the true rate can be backfilled later).

### 4.5 Snapshot Recompute
- **Input preparation:**
  - Pull transactions sorted ascending by `date_time_utc` per account.
  - Maintain FIFO lot queue per account/instrument.
  - Fetch latest prices ≤ snapshot timestamp; log `price` gap if unavailable.
  - Fetch FX rate for price timestamp; if missing, use last business day ≤ timestamp (max 1 day lookback) and log `fx_rate` gap.
- **FIFO realised PnL algorithm (per account):**
  1. Sort all transactions by `date_time_utc`.
  2. For each instrument maintain a FIFO queue of open lots storing remaining quantity, cost per unit in trade currency/EUR, and timestamp opened.
  3. **Buys** (`quantity > 0`): compute `cost_ccy = -net_amount`, append a new lot.
  4. **Sells** (`quantity < 0`):
     - `sell_qty = abs(quantity)`.
     - Compute proceeds in both currencies (`net_amount` in trade ccy, convert with FX at trade time`).
     - Repeatedly match against the FIFO queue, calculating costs and realised PnL per lot.
     - Remove lots when depleted; if `sell_qty` remains, record a `data_gaps` entry (possible short position or data issue).
  5. Persist each realised entry to `realized_pnl_fifo` after processing.
  6. Remaining queue state represents open positions for unrealised PnL.
- **Post-processing:**
  - Persist new entries into `realized_pnl_fifo` (dedupe by PK).
  - Update positions queue state for remaining lots.
  - Compute cash balance using trade net amounts and cash movements since previous snapshot.
  - Compute `positions_value_eur`, `unrealized_pnl_eur`, and cumulative `realized_pnl_eur` for `portfolio_value_snapshot`.
  - Emit/clear `data_gaps` entries accordingly.
- Schedule hourly (:05/:20/:35/:50 CET) plus manual CLI invocations.

### 4.6 Backfill Service
- CLI-driven historical refresh (prices, FX, snapshots) with configurable days/scopes.
- Overwrite existing prices/FX/snapshots when re-running backfills (no append-only behaviour).
- Remove `data_gaps` rows as soon as backfill supplies the missing data.

### 4.7 Instrument Metadata Updater
- Daily 03:30 CET job plus CLI.
- Refreshes sector, region, exchange; persists to `instruments`.

### 4.8 Performance & Returns
- Track per-instrument and per-account cash flows:
  - Trades contribute signed cash movements (already loaded via `transactions.net_amount`).
  - External cash movements (`cash_movements`) classify deposits/withdrawals, fees, interest.
  - Store normalized cash-flow records in base currency alongside the originating currency for audit.
- Maintain realized/unrealized PnL:
  - `realized_pnl_fifo` continues to capture lot closures (cost/proceeds in both native and EUR).
  - Snapshots compute unrealized PnL from mark-to-market minus remaining lot cost.
- Return calculations (all in base currency unless stated):
  - **Simple Return (positions)**: `(value_eur - cost_eur) / cost_eur` using open-lot cost and latest mark-to-market.
  - **Holdings Change**: per-snapshot delta for each instrument (`value_eur - prior_value_eur`) and percent change vs. prior snapshot to power intraday charts.
  - **Portfolio Absolute Change**: `nav_eur - prior_nav_eur`.
  - **Portfolio Percent Change**: `(nav_eur / prior_nav_eur) - 1` (skip when denominator is zero or missing).
  - **Time-Weighted Return (TWR)**:
    - Identify sub-periods between external cash flows.
    - For each sub-period, compute `(ending_nav - net_contributions) / starting_nav - 1`.
    - Chain (1 + sub-period return) across the requested horizon; expose daily, MTD, YTD, and since-inception values.
  - **Money-Weighted Return (MWR / IRR)**:
    - Use all cash flows (transactions + external) and ending NAV.
    - Solve via Newton-Raphson or bisection nightly; store annualized and non-annualized figures for dashboards and MCP.
  - **Cumulative Realized PnL**: sum of `realized_pnl_fifo.pnl_eur`, both life-to-date and per-period (day/week/month).
  - **Contribution / Withdrawal Summary**: maintain running totals of deposits, withdrawals, fees, dividends for reporting and to reconcile returns.
- Persist aggregated return metrics per snapshot for NAV and per-instrument snapshots for positions (e.g., percentage change since prior snapshot, since start-of-day, and cumulative since inception).
- Grafana dashboards expose:
  1. Per-position PnL (realized/unrealized) and percentage returns.
  2. Portfolio NAV with absolute/percentage change, rolling TWR, and drawdown.
  3. Cash-flow table summarising contributions, withdrawals, fees, and dividends.
- MCP and CLI provide query endpoints (e.g., `pnl-report`, `returns-report`) summarising realized lots, open PnL, and return metrics over arbitrary periods. CLI includes:
  - `returns-report` for on-demand TWR, MWR/IRR, simple returns, and contribution breakdowns over custom date ranges/accounts (CSV and table output).
  - `position-pnl` for instrument-level unrealized/realized PnL and hourly return series.

## 5. Monitoring & Dashboards
### 5.1 Angular Visualisations (Target State)
- Recreate Grafana overview panels as Angular components backed by the endpoints in §1.5.
- Provide drill-down routes for exposures, positions, and trades with Material data tables and chart components.
- Implement discrepancy banners comparing API results vs. Grafana (when legacy profile enabled) to aid cutover validation.

### 5.2 Legacy Grafana Reference
- Provisioned dashboards for portfolio value, PnL, exposures, trades. Datasource uses read-only Postgres credentials.
- Missing data dashboard must include:
  1. Total gap count by `gap_type` (price, fx_rate, instrument_metadata).
  2. Table with instrument/account identifiers, `target_timestamp`, and actionable `details`.
  3. Time series of gap count over past 7 days.
  4. Age of oldest gap (highlight when > 24h).

### 5.3 Alerts & Logging
- Log warnings for missing price/FX/snapshot/instrument data alongside entries in `data_gaps`.
- Optional alerting (Grafana or future Angular integrations) for stale prices, outstanding gaps, or failed ETL runs.

## 6. Operations & Maintenance
### 6.1 Scheduler Cadence
- Maintain APScheduler cron setup as documented; jobs must be idempotent and safe under delayed execution.

### 6.2 Secret Rotation
- Document process for updating secrets (rerun helper script locally; orchestrator-managed in production). Include JWT rotation steps once auth provider is finalised.

### 6.3 Backup & Restore
- Nightly `pg_dump` script with retention policy; outline restore steps into a fresh database.

### 6.4 Health & Observability
- Continue container healthchecks (Postgres `pg_isready`, FastAPI `/healthz`, Angular nginx HTTP check, Grafana HTTP when legacy profile active).
- Encourage log shipping/rotation; integrate MCP, ETL, API, and frontend logs into troubleshooting docs.
