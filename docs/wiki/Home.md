# Portfolio Visualizer

Welcome! This wiki explains how the portfolio visualizer is put together and how to operate it day to day. The codebase ships as a Docker Compose stack with three containers:

- **Postgres** – central store for instruments, transactions, prices, positions, and portfolio metrics.
- **ETL** – Python service running scheduled importers (IBKR Flex, yfinance prices) and snapshot recomputations.
- **Grafana** – dashboards for monitoring data freshness, portfolio exposure, and performance.

If you only have a few minutes, read the [Operations](./Operations.md) page for the critical commands. For a deeper tour, start with [Architecture](./Architecture.md), then dive into individual ETL jobs and dashboards.

- One-off imports (`import-ibkr`, `import-swissquote`) automatically backfill 365 days of prices/FX and snapshots unless `--no-backfill` is used.
## Key Links

- [Architecture](./Architecture.md)
- [ETL Jobs](./ETL-Jobs.md)
- [Monitoring & Dashboards](./Monitoring.md)
- [Operations](./Operations.md)

Happy investing!
