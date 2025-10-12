# Monitoring & Dashboards

Grafana is provisioned automatically (see `grafana/provisioning`). The main dashboard is `Portfolio Monitoring` (uid `overview`).

## Panels

| Panel | Description | Query Source |
|-------|-------------|--------------|
| Minutes Since Last Price Update | Stat showing age of the most recent row in `prices`. | `SELECT EXTRACT(EPOCH FROM (NOW() - MAX(as_of_utc))) / 60 ...` |
| Stale Instruments (Top 10) | Table of symbols with the oldest price timestamps. | Join `prices` + `instruments`. |
| Prices Ingested per Day | Time series of counts grouped by `created_at`. | `prices`. |
| Latest Snapshot per Account | Table with last `portfolio_value_snapshot` entry per account and hours since refresh. | `portfolio_value_snapshot`. |
| Accounts Missing Today | Stat highlighting any account without a snapshot for `CURRENT_DATE`. | `portfolio_value_snapshot`. |
| Current Positions | Table showing latest shares, cost basis, current price, and unrealised P&L in EUR. | `positions_snapshot`, `prices`, `instruments`. |
| Snapshots Recorded per Hour | Time series of `portfolio_value_snapshot` records. | `portfolio_value_snapshot`. |
| Portfolio Value (EUR) | Net worth curve aggregated across accounts (hourly resolution). | `portfolio_value_snapshot`. |

## Access

- URL: `http://localhost:3000`
- Provisioning credentials come from `secrets/grafana_admin_user` and `secrets/grafana_admin_password`.

## Alerts (future work)

Consider wiring Grafana alert rules for:
- Price staleness > 60 minutes.
- Portfolio snapshot missing for current date.
- Large daily drawdown in `portfolio_value_snapshot`.

Alert rules would require configuring a contact point (email/webhook) via Grafana UI; not included in this repo.
