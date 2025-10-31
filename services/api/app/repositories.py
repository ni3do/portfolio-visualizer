from __future__ import annotations

from collections import defaultdict
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
        WITH ranked AS (
            SELECT
                account_id,
                date_trunc('{bucket}', snapshot_at) AS bucket,
                snapshot_at,
                nav_eur,
                ROW_NUMBER() OVER (
                    PARTITION BY account_id, date_trunc('{bucket}', snapshot_at)
                    ORDER BY snapshot_at DESC
                ) AS rn
            FROM portfolio_value_snapshot
            WHERE (%(start)s IS NULL OR snapshot_at >= %(start)s)
              AND (%(end)s IS NULL OR snapshot_at <= %(end)s)
    """

    if account_id:
        sql += " AND account_id = %(account_id)s\n"
        params["account_id"] = account_id

    sql += """
        )
        SELECT bucket, SUM(nav_eur) AS nav_eur
        FROM ranked
        WHERE rn = 1
        GROUP BY bucket
        ORDER BY bucket
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def fetch_unmapped_instruments(conn: Connection) -> List[Dict[str, object]]:
    sql = """
        WITH latest_snapshot AS (
            SELECT MAX(snapshot_at) AS snapshot_at FROM positions_snapshot
        ),
        holdings AS (
            SELECT
                ps.instrument_id,
                SUM(ps.shares) AS shares
            FROM positions_snapshot ps
            JOIN latest_snapshot ls ON ls.snapshot_at = ps.snapshot_at
            WHERE ps.shares <> 0
            GROUP BY ps.instrument_id
        ),
        transaction_instruments AS (
            SELECT DISTINCT t.instrument_id
            FROM transactions t
            WHERE t.instrument_id IS NOT NULL
        ),
        all_instruments AS (
            SELECT source.instrument_id, COALESCE(h.shares, 0) AS shares
            FROM (
                SELECT instrument_id FROM holdings
                UNION
                SELECT instrument_id FROM transaction_instruments
            ) source
            LEFT JOIN holdings h ON h.instrument_id = source.instrument_id
        )
        SELECT
            i.instrument_id,
            NULLIF(i.symbol, '') AS symbol,
            NULLIF(i.name, '') AS name,
            NULLIF(i.currency, '') AS currency,
            NULLIF(i.primary_exchange, '') AS primary_exchange,
            NULLIF(i.asset_class, '') AS asset_class,
            NULLIF(i.sector, '') AS sector,
            NULLIF(i.industry, '') AS industry,
            NULLIF(i.country, '') AS country,
            NULLIF(i.region, '') AS region,
            ai.shares
        FROM instruments i
        JOIN all_instruments ai ON ai.instrument_id = i.instrument_id
        WHERE COALESCE(NULLIF(TRIM(i.yfinance_symbol), ''), '') = ''
        ORDER BY ai.shares DESC NULLS LAST, i.symbol
    """

    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return rows


def fetch_mapped_instruments(conn: Connection) -> List[Dict[str, object]]:
    sql = """
        WITH latest_snapshot AS (
            SELECT MAX(snapshot_at) AS snapshot_at FROM positions_snapshot
        ),
        holdings AS (
            SELECT
                ps.instrument_id,
                SUM(ps.shares) AS shares
            FROM positions_snapshot ps
            JOIN latest_snapshot ls ON ls.snapshot_at = ps.snapshot_at
            WHERE ps.shares <> 0
            GROUP BY ps.instrument_id
        ),
        transaction_instruments AS (
            SELECT DISTINCT t.instrument_id
            FROM transactions t
            WHERE t.instrument_id IS NOT NULL
        ),
        all_instruments AS (
            SELECT source.instrument_id, COALESCE(h.shares, 0) AS shares
            FROM (
                SELECT instrument_id FROM holdings
                UNION
                SELECT instrument_id FROM transaction_instruments
            ) source
            LEFT JOIN holdings h ON h.instrument_id = source.instrument_id
        ),
        latest_prices AS (
            SELECT DISTINCT ON (instrument_id)
                instrument_id,
                close,
                currency,
                as_of_utc
            FROM prices
            ORDER BY instrument_id, as_of_utc DESC
        )
        SELECT
            i.instrument_id,
            NULLIF(i.symbol, '') AS symbol,
            NULLIF(i.name, '') AS name,
            NULLIF(i.currency, '') AS currency,
            NULLIF(i.primary_exchange, '') AS primary_exchange,
            NULLIF(i.asset_class, '') AS asset_class,
            NULLIF(i.sector, '') AS sector,
            NULLIF(i.industry, '') AS industry,
            NULLIF(i.country, '') AS country,
            NULLIF(i.region, '') AS region,
            NULLIF(TRIM(i.yfinance_symbol), '') AS yfinance_symbol,
            ai.shares,
            lp.close AS last_price,
            lp.as_of_utc AS last_price_as_of
        FROM instruments i
        JOIN all_instruments ai ON ai.instrument_id = i.instrument_id
        LEFT JOIN latest_prices lp ON lp.instrument_id = i.instrument_id
        WHERE COALESCE(NULLIF(TRIM(i.yfinance_symbol), ''), '') <> ''
        ORDER BY ai.shares DESC NULLS LAST, i.symbol
    """

    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return rows


def update_instrument_mapping(
    conn: Connection,
    *,
    instrument_id: int,
    yfinance_symbol: Optional[str],
) -> Dict[str, object]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE instruments
               SET yfinance_symbol = %(symbol)s
             WHERE instrument_id = %(instrument_id)s
         RETURNING instrument_id, NULLIF(TRIM(yfinance_symbol), '') AS yfinance_symbol
            """,
            {"symbol": yfinance_symbol, "instrument_id": instrument_id},
        )
        row = cur.fetchone()

    if row is None:
        conn.rollback()
        raise ValueError("Instrument not found")

    conn.commit()
    return row


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
        COALESCE(ps.cost_basis_eur, 0) AS cost_basis_eur,
        i.symbol,
        NULLIF(i.name, '') AS name,
        i.country,
        i.sector,
        i.region,
        i.asset_class,
        COALESCE(lp.currency, i.currency) AS instrument_ccy,
        lp.close AS last_price,
        lp.as_of_utc AS last_price_as_of,
        bc.code AS base_ccy,
        CASE
            WHEN lp.close IS NULL THEN COALESCE(ps.cost_basis_eur, 0)
            WHEN COALESCE(lp.currency, i.currency) = bc.code THEN ps.shares * lp.close
            WHEN fx.rate IS NULL THEN COALESCE(ps.cost_basis_eur, 0)
            ELSE ps.shares * lp.close * fx.rate
        END AS market_value_eur,
        CASE
            WHEN lp.close IS NULL THEN 0
            WHEN COALESCE(lp.currency, i.currency) = bc.code THEN (ps.shares * lp.close) - COALESCE(ps.cost_basis_eur, 0)
            WHEN fx.rate IS NULL THEN 0
            ELSE (ps.shares * lp.close * fx.rate) - COALESCE(ps.cost_basis_eur, 0)
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


def fetch_latest_positions_snapshot_time(
    conn: Connection, *, account_id: Optional[str]
) -> Optional[datetime]:
    params: Dict[str, object] = {}
    sql = "SELECT MAX(snapshot_at) AS snapshot_at FROM positions_snapshot"
    if account_id:
        sql += " WHERE account_id = %(account_id)s"
        params["account_id"] = account_id

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()

    if not row:
        return None
    return row.get("snapshot_at")


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
    if dimension not in {"country", "sector", "currency", "region", "industry"}:
        raise ValueError(f"Unsupported exposure dimension: {dimension}")

    rows = fetch_exposure_positions(conn, account_id=account_id)
    overrides = fetch_exposure_overrides(
        conn, [row["instrument_id"] for row in rows]
    )
    aggregated = _aggregate_exposures_with_overrides(rows, dimension, overrides)
    return aggregated


def fetch_exposure_positions(
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
    instrument_ccy,
    market_value_eur,
    country,
    region,
    sector,
    asset_class
FROM position_values
WHERE market_value_eur IS NOT NULL;
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


def fetch_position_return_timeseries(
    conn: Connection,
    *,
    base_currency: str,
    start: Optional[datetime],
    end: Optional[datetime],
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    params: Dict[str, object] = {
        "start": start,
        "end": end,
        "base_currency": base_currency,
    }

    sql = """
        WITH nav AS (
            SELECT
                ps.account_id,
                ps.instrument_id,
                date_trunc('day', ps.snapshot_at) AS bucket,
                SUM(
                    CASE
                        WHEN price.close IS NULL THEN COALESCE(ps.cost_basis_eur, 0)
                        WHEN COALESCE(price.currency, i.currency) = %(base_currency)s THEN ps.shares * price.close
                        WHEN fx.rate IS NULL THEN COALESCE(ps.cost_basis_eur, 0)
                        ELSE ps.shares * price.close * fx.rate
                    END
                ) AS nav_eur
            FROM positions_snapshot ps
            JOIN instruments i ON i.instrument_id = ps.instrument_id
            LEFT JOIN LATERAL (
                SELECT close, currency
                FROM prices
                WHERE instrument_id = ps.instrument_id
                  AND as_of_utc <= ps.snapshot_at
                ORDER BY as_of_utc DESC
                LIMIT 1
            ) price ON TRUE
            LEFT JOIN LATERAL (
                SELECT rate
                FROM fx_rates
                WHERE from_ccy = COALESCE(price.currency, i.currency)
                  AND to_ccy = %(base_currency)s
                  AND date_utc <= ps.snapshot_at::date
                ORDER BY date_utc DESC
                LIMIT 1
            ) fx ON TRUE
            WHERE ps.shares <> 0
              AND (%(start)s IS NULL OR ps.snapshot_at >= %(start)s)
              AND (%(end)s IS NULL OR ps.snapshot_at <= %(end)s)
    """

    if account_id:
        sql += "            AND ps.account_id = %(account_id)s\n"
        params["account_id"] = account_id

    sql += """
            GROUP BY ps.account_id, ps.instrument_id, date_trunc('day', ps.snapshot_at)
        ),
        contributions AS (
            SELECT
                t.account_id,
                t.instrument_id,
                date_trunc('day', t.date_time_utc) AS bucket,
                SUM(
                    CASE
                        WHEN t.currency = %(base_currency)s THEN -t.net_amount
                        WHEN fx.rate IS NULL THEN 0
                        ELSE -t.net_amount * fx.rate
                    END
                ) AS amount_base
            FROM transactions t
            LEFT JOIN LATERAL (
                SELECT rate
                FROM fx_rates
                WHERE from_ccy = t.currency
                  AND to_ccy = %(base_currency)s
                  AND date_utc <= t.date_time_utc::date
                ORDER BY date_utc DESC
                LIMIT 1
            ) fx ON TRUE
            WHERE t.instrument_id IS NOT NULL
              AND t.type NOT ILIKE 'DIV%%'
              AND (%(start)s IS NULL OR t.date_time_utc >= %(start)s)
              AND (%(end)s IS NULL OR t.date_time_utc <= %(end)s)
    """

    if account_id:
        sql += "            AND t.account_id = %(account_id)s\n"

    sql += """
            GROUP BY t.account_id, t.instrument_id, date_trunc('day', t.date_time_utc)
        )
        SELECT
            n.account_id,
            n.instrument_id,
            n.bucket,
            n.nav_eur,
            COALESCE(c.amount_base, 0) AS contribution_eur
        FROM nav n
        LEFT JOIN contributions c
          ON c.account_id = n.account_id
         AND c.instrument_id = n.instrument_id
         AND c.bucket = n.bucket
        ORDER BY n.account_id, n.instrument_id, n.bucket
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return rows


def fetch_exposure_overrides(
    conn: Connection,
    instrument_ids: List[int],
) -> Dict[tuple[int, str], List[Dict[str, object]]]:
    if not instrument_ids:
        return {}

    sql = """
        SELECT instrument_id, dimension, label, weight
        FROM instrument_exposure_overrides
        WHERE instrument_id = ANY(%(instrument_ids)s)
    """

    with conn.cursor() as cur:
        cur.execute(sql, {"instrument_ids": instrument_ids})
        rows = cur.fetchall()

    overrides: Dict[tuple[int, str], List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = (row["instrument_id"], row["dimension"])
        overrides[key].append(
            {
                "label": row.get("label"),
                "weight": row.get("weight"),
            }
        )
    return {key: value for key, value in overrides.items()}


def _aggregate_exposures(
    rows: List[Dict[str, object]],
    dimension: str,
) -> List[Dict[str, object]]:
    return _aggregate_exposures_with_overrides(rows, dimension, {})


def _aggregate_exposures_with_overrides(
    rows: List[Dict[str, object]],
    dimension: str,
    overrides: Dict[tuple[int, str], List[Dict[str, object]]],
) -> List[Dict[str, object]]:
    label_map = {
        "country": "country",
        "region": "region",
        "sector": "sector",
        "industry": "asset_class",
        "currency": "instrument_ccy",
    }
    label_key = label_map[dimension]

    totals: Dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        value = row.get("market_value_eur")
        if value is None:
            continue
        amount = Decimal(str(row["market_value_eur"] or 0))
        if amount == 0:
            continue
        instrument_id = row.get("instrument_id")
        override_entries = overrides.get((instrument_id, dimension), [])
        if override_entries:
            for override in override_entries:
                weight_raw = override.get("weight")
                if weight_raw is None:
                    continue
                weight = Decimal(str(weight_raw))
                if weight == 0:
                    continue
                label = override.get("label") or "Unassigned"
                totals[label] += amount * weight
        else:
            label = row.get(label_key) or "Unassigned"
            totals[label] += amount

    ordered = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    return [{"label": label, "total_eur": total} for label, total in ordered]


def build_exposure_sections(
    rows: List[Dict[str, object]],
    overrides: Dict[tuple[int, str], List[Dict[str, object]]],
) -> Dict[str, List[Dict[str, object]]]:
    dimensions = ("country", "region", "sector", "industry", "currency")
    return {
        dimension: _aggregate_exposures_with_overrides(rows, dimension, overrides)
        for dimension in dimensions
    }


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
            COALESCE(i.symbol, t.trade_id) AS symbol,
            t.type AS trade_type,
            t.qty,
            t.price,
            t.currency,
            t.net_amount,
            t.fees
        FROM transactions t
        LEFT JOIN instruments i ON i.instrument_id = t.instrument_id
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


def fetch_dividends(
    conn: Connection,
    *,
    base_currency: str,
    start: Optional[datetime],
    end: Optional[datetime],
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    params: Dict[str, object] = {
        "start": start,
        "end": end,
        "base_currency": base_currency,
    }

    sql = """
        WITH dividends AS (
            SELECT
                cm.movement_id,
                cm.account_id,
                cm.date_time_utc,
                cm.currency,
                cm.amount,
                cm.description,
                cm.movement_type
            FROM cash_movements cm
            WHERE (
                    LOWER(cm.movement_type) LIKE '%%dividend%%'
                 OR LOWER(COALESCE(cm.description, '')) LIKE '%%dividend%%'
                )
              AND (%(start)s IS NULL OR cm.date_time_utc >= %(start)s)
              AND (%(end)s IS NULL OR cm.date_time_utc <= %(end)s)
        )
        SELECT
            d.account_id,
            d.date_time_utc,
            d.currency,
            d.amount,
            d.description,
            fx.rate AS fx_rate
        FROM dividends d
        LEFT JOIN LATERAL (
            SELECT rate
            FROM fx_rates
            WHERE from_ccy = d.currency
              AND to_ccy = %(base_currency)s
              AND date_utc <= d.date_time_utc::date
            ORDER BY date_utc DESC
            LIMIT 1
        ) fx ON TRUE
    """

    if account_id:
        sql += " WHERE d.account_id = %(account_id)s"
        params["account_id"] = account_id

    sql += " ORDER BY d.date_time_utc DESC"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows


def fetch_returns_series(
    conn: Connection,
    *,
    start: Optional[datetime],
    end: Optional[datetime],
    interval: str,
    account_id: Optional[str],
) -> List[Dict[str, object]]:
    bucket = {"1h": "hour", "1d": "day"}.get(interval, "day")
    params: Dict[str, object] = {"start": start, "end": end}

    sql = f"""
        WITH ranked AS (
            SELECT
                account_id,
                date_trunc('{bucket}', snapshot_at) AS bucket,
                snapshot_at,
                nav_eur,
                delta_eur,
                COALESCE(flow_eur, 0) AS flow_eur,
                ROW_NUMBER() OVER (
                    PARTITION BY account_id, date_trunc('{bucket}', snapshot_at)
                    ORDER BY snapshot_at DESC
                ) AS rn
            FROM portfolio_value_snapshot
            WHERE (%(start)s IS NULL OR snapshot_at >= %(start)s)
              AND (%(end)s IS NULL OR snapshot_at <= %(end)s)
    """

    if account_id:
        sql += " AND account_id = %(account_id)s\n"
        params["account_id"] = account_id

    sql += """
        )
        SELECT
            bucket,
            SUM(nav_eur) AS nav_eur,
            SUM(delta_eur - flow_eur) AS delta_eur,
            CASE
                WHEN (SUM(nav_eur) - SUM(delta_eur) + SUM(flow_eur)) = 0 THEN NULL
                ELSE SUM(delta_eur - flow_eur)
                     / (SUM(nav_eur) - SUM(delta_eur) + SUM(flow_eur))
            END AS return_pct
        FROM ranked
        WHERE rn = 1
        GROUP BY bucket
        ORDER BY bucket
    """

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows
