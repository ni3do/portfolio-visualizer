from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Sequence

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DatabaseConfig
from .models import (
    CashMovement,
    FxRate,
    Instrument,
    PortfolioValueSnapshot,
    PositionSnapshot,
    Price,
    PriceTarget,
    Transaction,
)

logger = logging.getLogger(__name__)


RENAME_LEGACY_SNAPSHOTS = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'positions_snapshot' AND column_name = 'date_utc'
    ) THEN
        ALTER TABLE positions_snapshot RENAME COLUMN date_utc TO snapshot_at;
    END IF;
    BEGIN
        ALTER TABLE positions_snapshot
            ALTER COLUMN snapshot_at TYPE TIMESTAMPTZ USING snapshot_at::timestamp;
    EXCEPTION
        WHEN undefined_column THEN NULL;
    END;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'portfolio_value_snapshot' AND column_name = 'date_utc'
    ) THEN
        ALTER TABLE portfolio_value_snapshot RENAME COLUMN date_utc TO snapshot_at;
    END IF;
    BEGIN
        ALTER TABLE portfolio_value_snapshot
            ALTER COLUMN snapshot_at TYPE TIMESTAMPTZ USING snapshot_at::timestamp;
    EXCEPTION
        WHEN undefined_column THEN NULL;
    END;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'portfolio_value_snapshot' AND column_name = 'ret_day'
    ) THEN
        ALTER TABLE portfolio_value_snapshot RENAME COLUMN ret_day TO ret;
    END IF;
END
$$;
"""


SCHEMA_STATEMENTS = [
    RENAME_LEGACY_SNAPSHOTS,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        instrument_id BIGINT PRIMARY KEY,
        symbol TEXT NOT NULL,
        yfinance_symbol TEXT,
        name TEXT,
        currency TEXT NOT NULL,
        asset_class TEXT,
        sector TEXT,
        country TEXT,
        region TEXT,
        primary_exchange TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        trade_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        date_time_utc TIMESTAMPTZ NOT NULL,
        type TEXT NOT NULL,
        instrument_id BIGINT REFERENCES instruments(instrument_id),
        qty NUMERIC NOT NULL,
        price NUMERIC NOT NULL,
        currency TEXT NOT NULL,
        fees NUMERIC NOT NULL,
        net_amount NUMERIC NOT NULL,
        source TEXT NOT NULL,
        raw_flex_id TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS cash_movements (
        movement_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        date_time_utc TIMESTAMPTZ NOT NULL,
        currency TEXT NOT NULL,
        amount NUMERIC NOT NULL,
        movement_type TEXT,
        description TEXT,
        source TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_transactions_date_time
    ON transactions (date_time_utc);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_cash_movements_date_time
    ON cash_movements (date_time_utc);
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_movements_composite
    ON cash_movements (account_id, date_time_utc, amount);
    """,
    """
    CREATE TABLE IF NOT EXISTS prices (
        as_of_utc TIMESTAMPTZ NOT NULL,
        instrument_id BIGINT NOT NULL REFERENCES instruments(instrument_id),
        close NUMERIC NOT NULL,
        currency TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (as_of_utc, instrument_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_prices_instrument_time
    ON prices (instrument_id, as_of_utc DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS positions_snapshot (
        snapshot_at TIMESTAMPTZ NOT NULL,
        account_id TEXT NOT NULL,
        instrument_id BIGINT NOT NULL REFERENCES instruments(instrument_id),
        shares NUMERIC NOT NULL,
        cost_basis_ccy NUMERIC,
        cost_basis_eur NUMERIC,
        PRIMARY KEY (snapshot_at, account_id, instrument_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_positions_snapshot_account_date
    ON positions_snapshot (account_id, snapshot_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_value_snapshot (
        snapshot_at TIMESTAMPTZ NOT NULL,
        account_id TEXT NOT NULL,
        value_eur NUMERIC NOT NULL,
        ret NUMERIC,
        drawdown NUMERIC,
        PRIMARY KEY (snapshot_at, account_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_portfolio_value_snapshot_account_date
    ON portfolio_value_snapshot (account_id, snapshot_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS fx_rates (
        date_utc DATE NOT NULL,
        from_ccy TEXT NOT NULL,
        to_ccy TEXT NOT NULL,
        rate NUMERIC NOT NULL,
        source TEXT NOT NULL,
        PRIMARY KEY (date_utc, from_ccy, to_ccy)
    );
    """,
]


def create_pool(cfg: DatabaseConfig) -> ConnectionPool:
    conninfo = (
        f"host={cfg.host} port={cfg.port} dbname={cfg.name} "
        f"user={cfg.user} password={cfg.password} connect_timeout=10"
    )
    return ConnectionPool(
        conninfo,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": dict_row},
    )


def ensure_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for stmt in SCHEMA_STATEMENTS:
                cur.execute(stmt)
        conn.commit()
    logger.info("Schema ensured")


def upsert_instruments(pool: ConnectionPool, instruments: Iterable[Instrument]) -> int:
    instruments = list(instruments)
    if not instruments:
        return 0

    query = sql.SQL(
        """
        INSERT INTO instruments (
            instrument_id, symbol, yfinance_symbol, name, currency,
            asset_class, sector, country, region, primary_exchange
        ) VALUES (
            %(instrument_id)s, %(symbol)s, %(yfinance_symbol)s, %(name)s, %(currency)s,
            %(asset_class)s, %(sector)s, %(country)s, %(region)s, %(primary_exchange)s
        )
        ON CONFLICT (instrument_id) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            yfinance_symbol = COALESCE(instruments.yfinance_symbol, NULLIF(EXCLUDED.yfinance_symbol, '')),
            name = EXCLUDED.name,
            currency = EXCLUDED.currency,
            asset_class = EXCLUDED.asset_class,
            sector = COALESCE(EXCLUDED.sector, instruments.sector),
            country = COALESCE(EXCLUDED.country, instruments.country),
            region = COALESCE(EXCLUDED.region, instruments.region),
            primary_exchange = EXCLUDED.primary_exchange;
        """
    )

    rows = [
        {
            "instrument_id": inst.instrument_id,
            "symbol": inst.symbol,
            "yfinance_symbol": inst.yfinance_symbol,
            "name": inst.name,
            "currency": inst.currency,
            "asset_class": inst.asset_class,
            "sector": inst.sector,
            "country": inst.country,
            "region": inst.region,
            "primary_exchange": inst.primary_exchange,
        }
        for inst in instruments
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()

    logger.info("Upserted %s instruments", len(rows))
    return len(rows)


def upsert_transactions(pool: ConnectionPool, transactions: Iterable[Transaction]) -> int:
    transactions = list(transactions)
    if not transactions:
        return 0

    query = sql.SQL(
        """
        INSERT INTO transactions (
            trade_id, account_id, date_time_utc, type,
            instrument_id, qty, price, currency,
            fees, net_amount, source, raw_flex_id
        )
        VALUES (
            %(trade_id)s, %(account_id)s, %(date_time_utc)s, %(type)s,
            %(instrument_id)s, %(qty)s, %(price)s, %(currency)s,
            %(fees)s, %(net_amount)s, %(source)s, %(raw_flex_id)s
        )
        ON CONFLICT (trade_id) DO UPDATE SET
            account_id = EXCLUDED.account_id,
            date_time_utc = EXCLUDED.date_time_utc,
            type = EXCLUDED.type,
            instrument_id = EXCLUDED.instrument_id,
            qty = EXCLUDED.qty,
            price = EXCLUDED.price,
            currency = EXCLUDED.currency,
            fees = EXCLUDED.fees,
            net_amount = EXCLUDED.net_amount,
            source = EXCLUDED.source,
            raw_flex_id = EXCLUDED.raw_flex_id;
        """
    )

    rows = [
        {
            "trade_id": tx.trade_id,
            "account_id": tx.account_id,
            "date_time_utc": tx.date_time_utc,
            "type": tx.type,
            "instrument_id": tx.instrument_id,
            "qty": tx.qty,
            "price": tx.price,
            "currency": tx.currency,
            "fees": tx.fees,
            "net_amount": tx.net_amount,
            "source": tx.source,
            "raw_flex_id": tx.raw_flex_id,
        }
        for tx in transactions
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()

    logger.info("Upserted %s transactions", len(rows))
    return len(rows)


def upsert_cash_movements(
    pool: ConnectionPool, movements: Iterable[CashMovement]
) -> int:
    movements = list(movements)
    if not movements:
        return 0

    query = sql.SQL(
        """
        INSERT INTO cash_movements (
            movement_id, account_id, date_time_utc, currency,
            amount, movement_type, description, source
        )
        VALUES (
            %(movement_id)s, %(account_id)s, %(date_time_utc)s, %(currency)s,
            %(amount)s, %(movement_type)s, %(description)s, %(source)s
        )
        ON CONFLICT (movement_id) DO UPDATE SET
            account_id = EXCLUDED.account_id,
            date_time_utc = EXCLUDED.date_time_utc,
            currency = EXCLUDED.currency,
            amount = EXCLUDED.amount,
            movement_type = EXCLUDED.movement_type,
            description = EXCLUDED.description,
            source = EXCLUDED.source;
        """
    )

    rows = [
        {
            "movement_id": cm.movement_id,
            "account_id": cm.account_id,
            "date_time_utc": cm.date_time_utc,
            "currency": cm.currency,
            "amount": cm.amount,
            "movement_type": cm.movement_type,
            "description": cm.description,
            "source": cm.source,
        }
        for cm in movements
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()

    logger.info("Upserted %s cash movements", len(rows))
    return len(rows)


def upsert_fx_rates(pool: ConnectionPool, rates: Iterable[FxRate]) -> int:
    rates = list(rates)
    if not rates:
        return 0

    query = sql.SQL(
        """
        INSERT INTO fx_rates (
            date_utc, from_ccy, to_ccy, rate, source
        )
        VALUES (
            %(date_utc)s, %(from_ccy)s, %(to_ccy)s, %(rate)s, %(source)s
        )
        ON CONFLICT (date_utc, from_ccy, to_ccy) DO UPDATE SET
            rate = EXCLUDED.rate,
            source = EXCLUDED.source;
        """
    )

    rows = [
        {
            "date_utc": fx.date_utc,
            "from_ccy": fx.from_ccy,
            "to_ccy": fx.to_ccy,
            "rate": fx.rate,
            "source": fx.source,
        }
        for fx in rates
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()

    logger.info("Upserted %s FX rates", len(rows))
    return len(rows)


def get_price_targets(pool: ConnectionPool) -> List[PriceTarget]:
    query = """
    WITH holdings AS (
        SELECT instrument_id, SUM(qty) AS qty
        FROM transactions
        GROUP BY instrument_id
        HAVING SUM(qty) <> 0
    )
    SELECT
        i.instrument_id,
        COALESCE(NULLIF(i.yfinance_symbol, ''), NULLIF(i.symbol, '')) AS ticker,
        i.currency,
        i.asset_class
    FROM instruments i
    JOIN holdings h ON h.instrument_id = i.instrument_id
    WHERE COALESCE(NULLIF(i.yfinance_symbol, ''), NULLIF(i.symbol, '')) IS NOT NULL
      AND COALESCE(i.asset_class, '') NOT IN ('CASH', 'FX', 'FOREX')
    ORDER BY i.instrument_id;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    return [
        PriceTarget(
            instrument_id=row["instrument_id"],
            ticker=row["ticker"],
            currency=row["currency"],
            asset_class=row.get("asset_class"),
        )
        for row in rows
    ]


def get_all_price_targets(pool: ConnectionPool) -> List[PriceTarget]:
    query = """
    SELECT
        i.instrument_id,
        COALESCE(NULLIF(i.yfinance_symbol, ''), NULLIF(i.symbol, '')) AS ticker,
        i.currency
    FROM instruments i
    WHERE COALESCE(NULLIF(i.yfinance_symbol, ''), NULLIF(i.symbol, '')) IS NOT NULL
    ORDER BY i.instrument_id;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    return [
        PriceTarget(
            instrument_id=row["instrument_id"],
            ticker=row["ticker"],
            currency=row["currency"],
        )
        for row in rows
    ]


def list_instrument_currencies(pool: ConnectionPool) -> List[str]:
    query = "SELECT DISTINCT currency FROM instruments WHERE currency IS NOT NULL ORDER BY currency;"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    return [row["currency"] for row in rows]


def upsert_prices(pool: ConnectionPool, prices: Iterable[Price]) -> int:
    prices = list(prices)
    if not prices:
        return 0

    query = sql.SQL(
        """
        INSERT INTO prices (
            as_of_utc, instrument_id, close, currency, source
        )
        VALUES (
            %(as_of_utc)s, %(instrument_id)s, %(close)s, %(currency)s, %(source)s
        )
        ON CONFLICT (as_of_utc, instrument_id) DO UPDATE SET
            close = EXCLUDED.close,
            currency = EXCLUDED.currency,
            source = EXCLUDED.source;
        """
    )

    rows = [
        {
            "as_of_utc": price.as_of_utc,
            "instrument_id": price.instrument_id,
            "close": price.close,
            "currency": price.currency,
            "source": price.source,
        }
        for price in prices
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()

    logger.info("Upserted %s prices", len(rows))
    return len(rows)


def get_transactions_up_to(pool: ConnectionPool, cutoff: datetime):
    query = """
    SELECT account_id, instrument_id, date_time_utc, qty, price, fees, net_amount, currency
    FROM transactions
    WHERE date_time_utc <= %s
    ORDER BY date_time_utc ASC;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (cutoff,))
            rows = cur.fetchall()
    return rows


def get_latest_prices(
    pool: ConnectionPool, instrument_ids: Sequence[int], cutoff: datetime
) -> Dict[int, Price]:
    if not instrument_ids:
        return {}

    query = """
    SELECT DISTINCT ON (instrument_id)
        instrument_id,
        as_of_utc,
        close,
        currency,
        source
    FROM prices
    WHERE instrument_id = ANY(%s) AND as_of_utc <= %s
    ORDER BY instrument_id, as_of_utc DESC;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (list(instrument_ids), cutoff))
            rows = cur.fetchall()

    return {
        row["instrument_id"]: Price(
            instrument_id=row["instrument_id"],
            as_of_utc=row["as_of_utc"],
            close=row["close"],
            currency=row["currency"],
            source=row["source"],
        )
        for row in rows
    }


def get_fx_rates_for_date(pool: ConnectionPool, target_date: date):
    query = """
    SELECT from_ccy, to_ccy, rate, source
    FROM fx_rates
    WHERE date_utc = %s;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (target_date,))
            rows = cur.fetchall()

    return {
        (row["from_ccy"], row["to_ccy"]): FxRate(
            date_utc=target_date,
            from_ccy=row["from_ccy"],
            to_ccy=row["to_ccy"],
            rate=row["rate"],
            source=row["source"],
        )
        for row in rows
    }


def replace_positions_snapshot(
    pool: ConnectionPool, positions: Iterable[PositionSnapshot]
) -> None:
    positions = list(positions)
    if not positions:
        return

    snapshot_at = positions[0].snapshot_at

    delete = "DELETE FROM positions_snapshot WHERE snapshot_at = %s"
    insert = sql.SQL(
        """
        INSERT INTO positions_snapshot (
            snapshot_at, account_id, instrument_id, shares, cost_basis_ccy, cost_basis_eur
        ) VALUES (
            %(snapshot_at)s, %(account_id)s, %(instrument_id)s, %(shares)s,
            %(cost_basis_ccy)s, %(cost_basis_eur)s
        );
        """
    )

    rows = [
        {
            "snapshot_at": pos.snapshot_at,
            "account_id": pos.account_id,
            "instrument_id": pos.instrument_id,
            "shares": pos.shares,
            "cost_basis_ccy": pos.cost_basis_ccy,
            "cost_basis_eur": pos.cost_basis_eur,
        }
        for pos in positions
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(delete, (snapshot_at,))
            cur.executemany(insert, rows)
        conn.commit()


def replace_portfolio_value_snapshot(
    pool: ConnectionPool, portfolios: Iterable[PortfolioValueSnapshot]
) -> None:
    portfolios = list(portfolios)
    if not portfolios:
        return

    snapshot_at = portfolios[0].snapshot_at

    delete = "DELETE FROM portfolio_value_snapshot WHERE snapshot_at = %s"
    insert = sql.SQL(
        """
        INSERT INTO portfolio_value_snapshot (
            snapshot_at, account_id, value_eur, ret, drawdown
        ) VALUES (
            %(snapshot_at)s, %(account_id)s, %(value_eur)s, %(ret)s, %(drawdown)s
        );
        """
    )

    rows = [
        {
            "snapshot_at": pv.snapshot_at,
            "account_id": pv.account_id,
            "value_eur": pv.value_eur,
            "ret": pv.ret,
            "drawdown": pv.drawdown,
        }
        for pv in portfolios
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(delete, (snapshot_at,))
            cur.executemany(insert, rows)
        conn.commit()


def get_portfolio_history_summary(
    pool: ConnectionPool, account_ids: Sequence[str], snapshot_at: datetime
) -> Dict[str, Dict[str, Decimal]]:
    if not account_ids:
        return {}

    query = """
    WITH latest AS (
        SELECT DISTINCT ON (account_id)
            account_id,
            value_eur,
            snapshot_at
        FROM portfolio_value_snapshot
        WHERE account_id = ANY(%s) AND snapshot_at < %s
        ORDER BY account_id, snapshot_at DESC
    ),
    peaks AS (
        SELECT account_id, MAX(value_eur) AS max_value
        FROM portfolio_value_snapshot
        WHERE account_id = ANY(%s) AND snapshot_at < %s
        GROUP BY account_id
    )
    SELECT l.account_id,
           l.value_eur AS prev_value,
           p.max_value
    FROM latest l
    FULL OUTER JOIN peaks p ON p.account_id = l.account_id;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (list(account_ids), snapshot_at, list(account_ids), snapshot_at))
            rows = cur.fetchall()

    summary: Dict[str, Dict[str, Decimal]] = {}
    for row in rows:
        account_id = row.get("account_id")
        if account_id is None:
            continue
        summary[account_id] = {
            "prev_value": row.get("prev_value"),
            "max_value": row.get("max_value"),
        }
    return summary
