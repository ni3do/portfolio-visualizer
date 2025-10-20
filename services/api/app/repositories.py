from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from psycopg import Connection


def fetch_portfolio_nav_series(
    conn: Connection,
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    interval: str,
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    bucket = {"1h": "hour", "1d": "day"}.get(interval, "hour")
    params: Dict[str, object] = {"start": start, "end": end}

    sql = f"""
        SELECT
            date_trunc('{bucket}', snapshot_at) AS bucket,
            SUM(nav_eur) AS nav_eur
        FROM portfolio_value_snapshot
        WHERE (%(start)s IS NULL OR snapshot_at >= %(start)s)
          AND (%(end)s IS NULL OR snapshot_at <= %(end)s)
    """
    if account_id:
        sql += " AND account_id = %(account_id)s\n"
        params["account_id"] = account_id

    sql += "GROUP BY bucket ORDER BY bucket"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def _build_position_values_cte(account_id: Optional[str]) -> str:
    account_filter = ""
    if account_id:
        account_filter = "AND ps.account_id = %(account_id)s"

    return f"""
WITH base_currency AS (
    SELECT COALESCE(
        (SELECT to_ccy FROM fx_rates ORDER BY date_utc DESC LIMIT 1),
        'EUR'
    ) AS code
),
latest_snapshot AS (
    SELECT MAX(snapshot_at) AS snapshot_at FROM positions_snapshot
),
latest_prices AS (
    SELECT DISTINCT ON (instrument_id)
        instrument_id,
        close,
        currency,
        as_of_utc
    FROM prices
    ORDER BY instrument_id, as_of_utc DESC
),
latest_fx AS (
    SELECT from_ccy, to_ccy, rate
    FROM (
        SELECT fr.*,
               ROW_NUMBER() OVER (PARTITION BY from_ccy, to_ccy ORDER BY date_utc DESC) AS rn
        FROM fx_rates fr
    ) ranked
    WHERE rn = 1
),
position_values AS (
    SELECT
        ps.account_id,
        ps.instrument_id,
        ps.shares,
        ps.cost_basis_eur,
        i.symbol,
        NULLIF(i.name, '') AS name,
        i.country,
        i.sector,
        COALESCE(lp.currency, i.currency) AS instrument_ccy,
        lp.close AS last_price,
        lp.as_of_utc AS last_price_as_of,
        bc.code AS base_ccy,
        CASE
            WHEN lp.close IS NULL THEN NULL
            WHEN COALESCE(lp.currency, i.currency) = bc.code THEN ps.shares * lp.close
            WHEN fx.rate IS NULL THEN NULL
            ELSE ps.shares * lp.close * fx.rate
        END AS market_value_eur,
        CASE
            WHEN lp.close IS NULL THEN NULL
            WHEN COALESCE(lp.currency, i.currency) = bc.code THEN (ps.shares * lp.close) - ps.cost_basis_eur
            WHEN fx.rate IS NULL THEN NULL
            ELSE (ps.shares * lp.close * fx.rate) - ps.cost_basis_eur
        END AS unrealized_pnl_eur
    FROM positions_snapshot ps
    JOIN latest_snapshot ls ON ls.snapshot_at = ps.snapshot_at
    JOIN instruments i ON i.instrument_id = ps.instrument_id
    LEFT JOIN latest_prices lp ON lp.instrument_id = ps.instrument_id
    CROSS JOIN base_currency bc
    LEFT JOIN latest_fx fx
        ON fx.from_ccy = COALESCE(lp.currency, i.currency)
       AND fx.to_ccy = bc.code
    WHERE ps.shares <> 0
    {account_filter}
)
"""


def fetch_positions(
    conn: Connection,
    *,
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    cte = _build_position_values_cte(account_id)
    sql = cte + """
SELECT
    account_id,
    instrument_id,
    symbol,
    name,
    instrument_ccy,
    shares,
    cost_basis_eur,
    market_value_eur,
    unrealized_pnl_eur,
    last_price,
    last_price_as_of
FROM position_values
ORDER BY market_value_eur DESC NULLS LAST;
"""
    params: Dict[str, object] = {}
    if account_id:
        params["account_id"] = account_id

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def fetch_unrealized_pnl(
    conn: Connection,
    *,
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    cte = _build_position_values_cte(account_id)
    sql = cte + """
SELECT
    symbol,
    COALESCE(name, symbol) AS name,
    SUM(market_value_eur) AS market_value_eur,
    SUM(unrealized_pnl_eur) AS unrealized_pnl_eur
FROM position_values
WHERE unrealized_pnl_eur IS NOT NULL
GROUP BY symbol, name
ORDER BY unrealized_pnl_eur DESC NULLS LAST;
"""
    params: Dict[str, object] = {}
    if account_id:
        params["account_id"] = account_id

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def fetch_exposure(
    conn: Connection,
    *,
    dimension: str,
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    dimension_map = {
        "country": "COALESCE(position_values.country, 'Unassigned')",
        "sector": "COALESCE(position_values.sector, 'Unassigned')",
        "currency": "COALESCE(position_values.instrument_ccy, 'Unassigned')",
    }
    if dimension not in dimension_map:
        raise ValueError(f"Unsupported exposure dimension: {dimension}")

    label_expr = dimension_map[dimension]
    cte = _build_position_values_cte(account_id)
    sql = cte + f"""
SELECT
    {label_expr} AS label,
    SUM(market_value_eur) AS total_eur
FROM position_values
WHERE market_value_eur IS NOT NULL
GROUP BY label
ORDER BY total_eur DESC NULLS LAST;
"""
    params: Dict[str, object] = {}
    if account_id:
        params["account_id"] = account_id

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def fetch_portfolio_totals(
    conn: Connection,
    *,
    account_id: Optional[str],
) -> Optional[Decimal]:
    sql = """
        SELECT SUM(nav_eur) AS total
        FROM portfolio_value_snapshot
        WHERE snapshot_at = (
            SELECT MAX(snapshot_at) FROM portfolio_value_snapshot
        )
    """
    params: Dict[str, object] = {}
    if account_id:
        sql += " AND account_id = %(account_id)s"
        params["account_id"] = account_id

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if not row:
        return None
    return row.get("total")


def fetch_recent_trades(
    conn: Connection,
    *,
    limit: int,
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    params: Dict[str, object] = {"limit": limit}
    sql = """
        SELECT
            t.date_time_utc AS executed_at,
            t.account_id,
            i.symbol,
            t.type AS trade_type,
            t.qty,
            t.price,
            t.currency,
            t.net_amount,
            t.fees
        FROM transactions t
        JOIN instruments i ON i.instrument_id = t.instrument_id
    """
    if account_id:
        sql += "WHERE t.account_id = %(account_id)s\n"
        params["account_id"] = account_id
    else:
        sql += "WHERE 1=1\n"

    sql += "ORDER BY t.date_time_utc DESC LIMIT %(limit)s"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows
