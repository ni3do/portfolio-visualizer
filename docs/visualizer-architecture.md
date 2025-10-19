# Portfolio Visualizer Replacement Architecture

## 1. Modernized Stack Overview
- **Backend**: Python 3.11 + FastAPI application served by Uvicorn. FastAPI aligns with the existing Python ETL codebase, enables async database access via SQLModel/SQLAlchemy, and offers automatic OpenAPI docs for the PWA team.
- **Frontend**: Angular 17 Progressive Web App generated with Angular CLI. Angular's opinionated structure fits the multi-dashboard requirements, and the built-in PWA toolkit (service workers, asset versioning) simplifies offline snapshots and push-style refreshes.
- **Database**: Continue to use PostgreSQL 15 as the system of record. Read-only queries remain routed through the existing application role.
- **Runtime**: Docker Compose orchestrates Postgres, ETL, the new `visualizer-api` service, and a `visualizer-web` frontend container. Grafana becomes optional (dev-only) or is fully removed once the PWA reaches feature parity.
- **Documentation Alignment**: This architecture replaces the Grafana-centric flow described in `docs/spec.md` and `implementation-plan.md`. As those documents are updated, cross-link the relevant sections so the team can trace requirements from the legacy dashboards to the new API + PWA implementation.

## 2. Grafana Dashboard Audit
Each Grafana panel must be reimplemented via API endpoints and frontend components. Queries below operate against the `portfolio` database.

| Panel | Purpose / Metric | SQL Source |
| --- | --- | --- |
| Portfolio Value (EUR) | Hourly aggregated NAV in EUR | `SELECT $__timeGroup(snapshot_at::timestamp, '1h') AS time, SUM(value_eur) AS portfolio_value_eur FROM portfolio_value_snapshot GROUP BY 1 ORDER BY 1;`
| Unrealized PnL by Position | Latest unrealized PnL per instrument in base currency | CTE chain combining `positions_snapshot`, `instruments`, `prices`, `fx_rates` to compute market value & unrealized PnL (see panel id 2 SQL). |
| Recent Trades | 25 most recent transactions including quantity, price, fees | `SELECT t.date_time_utc, t.account_id, i.symbol, t.type, t.qty, t.price, t.currency, t.net_amount, t.fees FROM transactions t JOIN instruments i ... ORDER BY t.date_time_utc DESC LIMIT 25;`
| Country Exposure | Portfolio value breakdown by issuer country | Same valuation CTE as panel 2 with grouping on `i.country` (panel id 4 SQL). |
| Sector Exposure | Portfolio value breakdown by sector | Same valuation CTE as panel 2 with grouping on `i.sector` (panel id 5 SQL). |
| Currency Exposure | Market value by instrument currency | Same valuation CTE as panel 2 with grouping on instrument currency (panel id 6 SQL). |
| Open Positions | Tabular holdings view including weights, last price, PnL | Extended valuation CTE returning columns for quantity, price timestamps, cost basis, PnL, sector, country (panel id 7 SQL). |

> **Note:** Panels 2, 4, 5, 6, and 7 share nearly identical CTE pipelines for establishing the latest snapshot, prices, and FX rates. Refactoring these calculations into database views or reusable API service functions will reduce duplication.
>
> **Traceability tip:** Capture the SQL for these panels in `docs/spec.md` (see §3) so the Angular feature modules can reference the canonical calculations when building charts and tables.

## 3. Backend Service Design (FastAPI)
- **Framework Rationale**: The ETL is already Python-based with mature SQLAlchemy models; FastAPI lets us reuse schemas and domain logic. Async endpoints allow efficient parallelization of price & FX queries.
- **Database Access**: Use SQLAlchemy 2.0 or SQLModel with async `asyncpg` driver. Create read-only sessions bound to the existing Postgres application role.
- **Serialization**: Pydantic models surface typed responses. Start with lightweight in-process TTL caches for aggregated queries; consider Redis later only if multiple API replicas need to share cache state.
- **Background Tasks**: Provide hooks to trigger ETL refreshes (optional) through FastAPI background tasks, but keep heavy data processing in the ETL container.

## 4. Frontend PWA (Angular + Material Design)
- **Layout**: Recreate Grafana's overview as Angular routes with a shared layout shell (toolbar, filters, responsive grid). Utilize Angular Material's `mat-sidenav`, `mat-toolbar`, and `mat-grid-list` to deliver consistent Material UI styling.
- **Data Fetching**: Centralize API calls in Angular services backed by the RxJS `HttpClient`. Use `shareReplay` caching on observables, and persist key datasets in IndexedDB via Angular service worker data groups or `@ngx-pwa/local-storage`.
- **Visualization**: Adopt Material-themed charting libraries like `ngx-charts` or `ng2-charts` (Chart.js) for time series, donut, and bar charts. Build data tables with `MatTable` plus virtual scrolling, sorting, and filtering modules.
- **PWA Enhancements**: Leverage Angular's `@angular/pwa` tooling for offline caching, update notifications, and background sync of snapshot data.

## 5. API Surface
All endpoints are prefixed with `/api`. Responses default to JSON.

| Endpoint | Method | Description | Backing Query / Notes |
| --- | --- | --- | --- |
| `/healthz` | GET | Liveness check returning service status. | Simple constant response; used by Compose healthcheck. |
| `/portfolio/value` | GET | Returns timeseries NAV (defaults to last 90 days). Query params: `from`, `to`, `interval`. | Builds on panel 1 SQL with parameterized bucket size. |
| `/portfolio/unrealized` | GET | Latest unrealized PnL per instrument; supports filters (`account_id`, `sector`, `country`). | Uses shared valuation service derived from panel 2 SQL. |
| `/portfolio/exposure/country` | GET | Country allocation snapshot with EUR totals and percentages. | Derived from panel 4 SQL, adds computed weights. |
| `/portfolio/exposure/sector` | GET | Sector allocation snapshot. | Derived from panel 5 SQL. |
| `/portfolio/exposure/currency` | GET | Currency exposure snapshot. | Derived from panel 6 SQL. |
| `/portfolio/positions` | GET | Paginated holdings table including cost basis, current price, PnL, weights. Supports sort & filter parameters. | Based on panel 7 SQL with pagination wrappers. |
| `/transactions/recent` | GET | List of recent trades (`limit` param, default 25). | Panel 3 SQL with limit parameter. |
| `/metrics/cache` | GET | Debug endpoint showing cache hit/miss counts (optional, protected). | In-memory metrics. |

## 6. Authentication & Authorization
- **Identity**: Issue a dedicated `visualizer_ro` Postgres role with `SELECT` permissions only. The API uses this role; the ETL retains elevated rights.
- **API Auth**: Introduce JWT-based session tokens issued by a lightweight auth service or rely on Auth0/Okta integration (depending on org policy). For self-hosted setups, FastAPI's OAuth2 password flow backed by Postgres `app_users` table with bcrypt hashes is sufficient.
- **Secret Management**: Reuse Docker secrets for DB credentials. Store JWT signing key as a new secret (`visualizer_jwt_secret`).
- **Frontend Access**: PWA obtains tokens via login form; tokens stored in `httpOnly` cookies to prevent XSS. Support optional basic auth fallback for internal deployments.

## 7. Caching & Aggregation Strategy
- **Database Views**: Create SQL views (or materialized views refreshed hourly) encapsulating the complex valuation CTE shared across panels. This reduces response time and simplifies API code.
- **Application Cache**: Begin with per-endpoint in-memory caching (60-second TTL) inside the FastAPI process. Evaluate Redis or another distributed cache only if we scale to multiple API replicas or need cross-instance invalidation.
- **Client Cache**: Angular services memoize via RxJS caching and Angular service worker data groups, mirroring Grafana's 5-minute refresh cadence while supporting offline fallbacks.

## 8. Docker Compose Adjustments
- **New Services**:
  - `visualizer-api`: Builds from `./services/api` (FastAPI). Mounts shared code, exposes `:8080`, depends on Postgres. Healthcheck hitting `/healthz`.
  - `visualizer-web`: Multi-stage build (Node 20) that compiles the Angular workspace and serves the production bundle via `nginx` on port `:8081`. Provide a dev profile that runs `ng serve` with HMR on `:4200` when needed.
- **Optional Services**:
  - `grafana`: Move behind a Compose profile (`profiles: ['legacy']`) so it runs only when explicitly requested (`docker compose --profile legacy up`). Eventually removable.
- **Secrets**: Add `visualizer_api_env` secret (contains JWT secret & API config) and reuse `postgres_app_user/_password` for DB access.
- **Networking**: Place API and web on same default network; configure CORS between web (frontend) and API. Frontend container proxies API via internal hostname `visualizer-api:8080`.

## 9. Migration Checklist
1. Scaffold FastAPI service with shared DB models and valuation view helpers.
2. Extract Grafana SQL into database views or SQLAlchemy queries.
3. Build Angular PWA replicating dashboard panels using API endpoints and Angular Material components.
4. Implement JWT auth + login page; integrate Angular interceptors for token handling and RxJS-based data caching.
5. Update Docker Compose & secrets; document rollout process.
6. Decommission Grafana once new UI validated.

## 10. Detailed Implementation Outline

### Phase 0 – Foundations & Project Setup
1. **Repository preparation**
   - Create `services/api/` and `services/web/` directories with their own `README.md` files describing local development commands.
   - Add `.editorconfig`, shared lint configurations (`ruff.toml`, `pyproject.toml`, `eslint.config.js`), and pre-commit hooks to enforce formatting parity across backend and frontend.
2. **Environment configuration**
   - Introduce `.env.example` files for both services detailing required environment variables (DB URL, JWT secrets, API base URL, etc.).
   - Define Docker secrets in `docker-compose.yml` and ensure local `make` targets or scripts exist to provision them from `secrets/`.
3. **CI/CD bootstrapping**
   - Update GitHub Actions (or equivalent) to lint and test both the FastAPI and Angular workspaces on every push.

### Phase 1 – Database & Data Access Layer
1. **SQL artifacts**
   - Create SQL views/materialized views for valuation pipelines (`portfolio_latest_positions`, `portfolio_exposure_country`, etc.) under `etl/sql/views/` with migration scripts.
   - Provide refresh functions or ETL hooks to update materialized views after each ETL run.
2. **SQLAlchemy models**
   - Generate Pydantic/SQLModel schemas for the new views in `services/api/app/models/`.
   - Centralize DB session management in `services/api/app/db.py` with async engine, session factory, and dependency injection utilities.
3. **Unit coverage**
   - Add tests validating SQL view outputs against fixture datasets using pytest + a temporary Postgres schema spun up via `pytest-postgresql` or Docker.

### Phase 2 – FastAPI Service Build-Out
1. **Project scaffolding**
   - Initialize FastAPI app with routers stored under `services/api/app/routers/` and domain services in `services/api/app/services/`.
   - Implement `main.py` to load settings from environment variables (`pydantic-settings`) and mount routers.
2. **Endpoint development**
   - Translate each Grafana query into a dedicated service function returning typed Pydantic responses; leverage shared valuation utilities for exposures to avoid duplication.
   - Implement pagination helpers, filter parsing, and error handling (404s for missing instruments, validation errors).
3. **Caching & performance**
   - Wrap read-heavy service calls with an in-memory TTL cache (e.g., `fastapi-cache2` or custom `asyncio` cache) and expose metrics via `/metrics/cache`.
   - Add OpenAPI tags, response models, and examples to aid frontend consumption.
4. **Testing**
   - Write API tests using `httpx.AsyncClient` + `pytest` hitting an ephemeral Postgres seeded with fixture data.
   - Validate authentication guards via dependency overrides in tests.

### Phase 3 – Authentication & Authorization
1. **User management**
   - Add `app_users` table migration (if not already present) and password hashing utilities using `passlib`.
   - Provide CLI scripts in `services/api/scripts/` to create users/service accounts.
2. **JWT implementation**
   - Build token issuance endpoint (`/api/auth/login`) returning HTTP-only cookies and refresh tokens as needed.
   - Configure middleware/guards for protected routes, and document token rotation strategy.
3. **Secret handling**
   - Document rotation procedures for DB credentials and JWT secrets in `docs/runbooks/auth.md`.

### Phase 4 – Angular PWA Development
1. **Workspace setup**
   - Use Angular CLI to generate the project under `services/web/` with strict TypeScript mode and PWA support (`ng add @angular/pwa`).
   - Configure Angular Material theme, typography, and global styles matching product branding.
2. **Core modules**
   - Establish feature modules (`dashboard`, `positions`, `trades`, `auth`) with routing definitions and lazy-loading.
   - Implement shared UI components (cards, charts, filters) in `shared/` module, integrating `ngx-charts`/`ng2-charts` wrappers.
3. **State management & services**
   - Create API services using `HttpClient` with typed interfaces (`PortfolioValue`, `ExposureSlice`, etc.) in `services/web/src/app/api/`.
   - Introduce RxJS stores (e.g., `ComponentStore` or signals) for caching and shareReplay semantics.
4. **PWA/offline features**
   - Configure Angular service worker data groups for API caching and background sync.
   - Provide an offline snapshot view that displays the last successful fetch with timestamp.
5. **Testing & quality**
   - Add unit tests via Jasmine/Karma or Jest, plus end-to-end tests using Cypress or Playwright hitting a mocked API.

### Phase 5 – Integration, Observability & Deployment
1. **Compose integration**
   - Wire `docker-compose.yml` to build and network the API and web containers, ensuring `.env` overrides for local dev.
   - Add `Makefile` or `scripts/dev.sh` for common workflows (`make dev`, `make test`, `make lint`).
2. **Observability**
   - Instrument FastAPI with Prometheus metrics (`prometheus-fastapi-instrumentator`) and structured logging.
   - Configure frontend logging/reporting (Sentry or equivalent) and document environment variable hooks.
3. **Documentation & handoff**
   - Update `docs/` with API reference, frontend component catalog, and runbooks for deployments.
   - Provide rollout checklist covering blue/green deploy, data validation against Grafana, and stakeholder sign-off.

### Phase 6 – Cutover & Grafana Decommissioning
1. **Parallel run**
   - Operate Grafana and the new PWA concurrently, comparing key metrics daily using automated diff scripts.
   - Gather user feedback, log any discrepancies, and patch API/frontend accordingly.
2. **Final switch**
   - Update DNS or load balancer to point to the new web frontend.
   - Disable Grafana service in Compose, archive dashboards, and document rollback steps.

## 11. Alignment & Open Questions

- **Spec integration**: `docs/spec.md` now carries both legacy Grafana requirements and the new FastAPI + Angular architecture. Ensure future updates keep the endpoint tables and Angular module plans in sync with the API surface defined above.
- **Implementation plan**: `implementation-plan.md` still documents the minimal Grafana deployment. Confirm whether we should revise that plan to mirror this migration roadmap or maintain it as a legacy reference for existing deployments.
- **MCP service**: The repository introduces an `mcp` container in Compose. Determine if the new API should expose MCP capabilities directly, proxy them, or leave the existing service untouched.
- **Auth provider**: Decision between in-house JWT issuance and external IdP (Auth0/Okta) remains open. Capture the final choice in both this architecture doc and `docs/spec.md` once stakeholders confirm.
- **Caching strategy**: Redis is intentionally deferred. If we later scale to multiple API replicas, revisit §7 to detail the distributed cache rollout plan.
