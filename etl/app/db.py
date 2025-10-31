from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from collections import defaultdict
from typing import DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.types.json import Json

from .config import DatabaseConfig
from .models import (
    CashMovement,
    DataGap,
    FxRate,
    Instrument,
    InstrumentMetadataTarget,
    PortfolioValueSnapshot,
    PositionSnapshot,
    PositionTicker,
    Price,
    HourlyPrice,
    PriceTarget,
    RealizedPnlLot,
    Transaction,
)

logger = logging.getLogger(__name__)

_ANY = object()
_GLOBAL_ACCOUNT_ID = "__GLOBAL__"


ALLOWED_METADATA_COLUMNS = {
    "name",
    "currency",
    "asset_class",
    "sector",
    "industry",
    "country",
    "region",
    "primary_exchange",
}

ALLOWED_EXPOSURE_DIMENSIONS = {"sector", "region", "country"}


RENAME_LEGACY_SNAPSHOTS = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'positions_snapshot' AND column_name = 'date_utc'
    ) THEN
        ALTER TABLE positions_snapshot RENAME COLUMN date_utc TO snapshot_at;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'positions_snapshot') THEN
        BEGIN
            ALTER TABLE positions_snapshot
                ALTER COLUMN snapshot_at TYPE TIMESTAMPTZ USING snapshot_at::timestamp;
        EXCEPTION
            WHEN undefined_column THEN NULL;
        END;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'portfolio_value_snapshot' AND column_name = 'date_utc'
    ) THEN
        ALTER TABLE portfolio_value_snapshot RENAME COLUMN date_utc TO snapshot_at;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'portfolio_value_snapshot') THEN
        BEGIN
            ALTER TABLE portfolio_value_snapshot
                ALTER COLUMN snapshot_at TYPE TIMESTAMPTZ USING snapshot_at::timestamp;
        EXCEPTION
            WHEN undefined_column THEN NULL;
        END;
    END IF;
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
        industry TEXT,
        country TEXT,
        region TEXT,
        primary_exchange TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    ALTER TABLE instruments
        ADD COLUMN IF NOT EXISTS industry TEXT;
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
    CREATE TABLE IF NOT EXISTS prices_hourly (
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
    CREATE INDEX IF NOT EXISTS idx_prices_hourly_instrument_time
    ON prices_hourly (instrument_id, as_of_utc DESC);
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
        positions_value_eur NUMERIC,
        cash_value_eur NUMERIC,
        nav_eur NUMERIC,
        unrealized_pnl_eur NUMERIC,
        realized_pnl_eur NUMERIC,
        delta_eur NUMERIC,
        ret NUMERIC,
        drawdown NUMERIC,
        value_eur NUMERIC,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (snapshot_at, account_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_portfolio_value_snapshot_account_date
    ON portfolio_value_snapshot (account_id, snapshot_at DESC);
    """,
    """
    CREATE TABLE IF NOT EXISTS instrument_exposure_overrides (
        instrument_id BIGINT NOT NULL REFERENCES instruments(instrument_id) ON DELETE CASCADE,
        dimension TEXT NOT NULL,
        label TEXT NOT NULL,
        weight NUMERIC NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (instrument_id, dimension, label)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_instrument_exposure_overrides_dimension
    ON instrument_exposure_overrides (dimension, label);
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
    """
    CREATE INDEX IF NOT EXISTS idx_fx_rates_pair_date
    ON fx_rates (from_ccy, to_ccy, date_utc DESC);
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS positions_value_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS cash_value_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS nav_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS unrealized_pnl_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS realized_pnl_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS delta_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS flow_eur NUMERIC;
    """,
    """
    ALTER TABLE portfolio_value_snapshot
        ADD COLUMN IF NOT EXISTS value_eur NUMERIC;
    """,
    """
    CREATE TABLE IF NOT EXISTS realized_pnl_fifo (
        account_id TEXT NOT NULL,
        instrument_id BIGINT NOT NULL REFERENCES instruments(instrument_id),
        lot_opened_at TIMESTAMPTZ NOT NULL,
        lot_closed_at TIMESTAMPTZ NOT NULL,
        close_snapshot_at TIMESTAMPTZ NOT NULL,
        qty_closed NUMERIC NOT NULL,
        proceeds_ccy NUMERIC NOT NULL,
        proceeds_eur NUMERIC NOT NULL,
        cost_ccy NUMERIC NOT NULL,
        cost_eur NUMERIC NOT NULL,
        pnl_ccy NUMERIC NOT NULL,
        pnl_eur NUMERIC NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (account_id, instrument_id, lot_opened_at, lot_closed_at)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_realized_pnl_fifo_snapshot
        ON realized_pnl_fifo (close_snapshot_at, account_id, instrument_id);
    """,
    """
    CREATE TABLE IF NOT EXISTS data_gaps (
        gap_type TEXT NOT NULL,
        target_timestamp TIMESTAMPTZ NOT NULL,
        detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        instrument_id BIGINT REFERENCES instruments(instrument_id),
        account_id TEXT,
        details JSONB,
        PRIMARY KEY (gap_type, target_timestamp, instrument_id, account_id)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_data_gaps_type_timestamp
        ON data_gaps (gap_type, target_timestamp DESC);
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
            asset_class, sector, industry, country, region, primary_exchange
        ) VALUES (
            %(instrument_id)s, %(symbol)s, %(yfinance_symbol)s, %(name)s, %(currency)s,
            %(asset_class)s, %(sector)s, %(industry)s, %(country)s, %(region)s, %(primary_exchange)s
        )
        ON CONFLICT (instrument_id) DO UPDATE SET
            symbol = EXCLUDED.symbol,
            yfinance_symbol = COALESCE(
                NULLIF(instruments.yfinance_symbol, ''),
                NULLIF(EXCLUDED.yfinance_symbol, '')
            ),
            name = EXCLUDED.name,
            currency = EXCLUDED.currency,
            asset_class = EXCLUDED.asset_class,
            sector = COALESCE(EXCLUDED.sector, instruments.sector),
            industry = COALESCE(EXCLUDED.industry, instruments.industry),
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
            "industry": inst.industry,
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


def get_latest_position_tickers(pool: ConnectionPool) -> List[PositionTicker]:
    query = """
    WITH latest_snapshot AS (
        SELECT snapshot_at
        FROM positions_snapshot
        ORDER BY snapshot_at DESC
        LIMIT 1
    )
    SELECT
        ps.account_id,
        ps.instrument_id,
        ps.shares,
        COALESCE(NULLIF(i.yfinance_symbol, ''), NULLIF(i.symbol, '')) AS ticker,
        i.currency
    FROM positions_snapshot ps
    JOIN latest_snapshot ls ON ls.snapshot_at = ps.snapshot_at
    JOIN instruments i ON i.instrument_id = ps.instrument_id
    WHERE ps.shares <> 0
      AND COALESCE(NULLIF(i.yfinance_symbol, ''), NULLIF(i.symbol, '')) IS NOT NULL
    ORDER BY ps.account_id, ps.instrument_id;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    return [
        PositionTicker(
            account_id=row["account_id"],
            instrument_id=row["instrument_id"],
            ticker=row["ticker"],
            shares=row["shares"],
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


def get_instruments_by_ids(
    pool: ConnectionPool, instrument_ids: Sequence[int]
) -> Dict[int, Dict[str, object]]:
    if not instrument_ids:
        return {}

    query = """
    SELECT instrument_id,
           symbol,
           yfinance_symbol,
           name,
           currency,
           asset_class
    FROM instruments
    WHERE instrument_id = ANY(%s);
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (list(instrument_ids),))
            rows = cur.fetchall()

    return {row["instrument_id"]: row for row in rows}


def list_instrument_metadata_targets(
    pool: ConnectionPool, include_all: bool = False
) -> List[InstrumentMetadataTarget]:
    query = [
        "SELECT",
        "    instrument_id,",
        "    COALESCE(NULLIF(yfinance_symbol, ''), NULLIF(symbol, '')) AS ticker",
        "FROM instruments",
        "WHERE COALESCE(NULLIF(yfinance_symbol, ''), NULLIF(symbol, '')) IS NOT NULL",
    ]

    if not include_all:
        query.append("  AND (")
        query.append("        NULLIF(sector, '') IS NULL")
        query.append("     OR NULLIF(country, '') IS NULL")
        query.append("     OR NULLIF(name, '') IS NULL")
        query.append("     OR NULLIF(primary_exchange, '') IS NULL")
        query.append("     OR NULLIF(asset_class, '') IS NULL")
        query.append(
            "     OR (UPPER(COALESCE(asset_class, '')) = 'ETF' AND NOT EXISTS ("
        )
        query.append(
            "            SELECT 1 FROM instrument_exposure_overrides ie"
        )
        query.append(
            "            WHERE ie.instrument_id = instruments.instrument_id"
        )
        query.append("          ))")
        query.append("    )")

    query.append("ORDER BY instrument_id;")
    sql_query = "\n".join(query)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_query)
            rows = cur.fetchall()

    return [
        InstrumentMetadataTarget(
            instrument_id=row["instrument_id"],
            ticker=row["ticker"],
        )
        for row in rows
    ]


def update_instrument_metadata(
    pool: ConnectionPool, instrument_id: int, metadata: Dict[str, object]
) -> int:
    filtered = {
        key: value
        for key, value in metadata.items()
        if key in ALLOWED_METADATA_COLUMNS and value not in (None, "")
    }

    if not filtered:
        return 0

    assignments = [
        sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder(column))
        for column in filtered.keys()
    ]

    query = sql.SQL(
        "UPDATE instruments SET {assignments} WHERE instrument_id = %(instrument_id)s"
    ).format(assignments=sql.SQL(", ").join(assignments))

    params: Dict[str, object] = dict(filtered)
    params["instrument_id"] = instrument_id

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rowcount = cur.rowcount
        conn.commit()

    logger.debug(
        "Instrument %s metadata updated with fields: %s",
        instrument_id,
        sorted(filtered.keys()),
    )

    return rowcount


def clear_instrument_mapping(pool: ConnectionPool, instrument_id: int) -> bool:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE instruments
                   SET yfinance_symbol = NULL
                 WHERE instrument_id = %s
                   AND COALESCE(NULLIF(yfinance_symbol, ''), '') <> ''
                RETURNING instrument_id
                """,
                (instrument_id,),
            )
            updated = cur.rowcount
        conn.commit()

    if updated:
        logger.info(
            "Cleared yfinance mapping for instrument %s due to missing prices",
            instrument_id,
        )
        return True
    return False


def replace_instrument_exposures(
    pool: ConnectionPool,
    instrument_id: int,
    exposures: Optional[Dict[str, Sequence[Tuple[str, float]]]],
) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if not exposures:
                cur.execute(
                    "DELETE FROM instrument_exposure_overrides WHERE instrument_id = %s",
                    (instrument_id,),
                )
                conn.commit()
                logger.debug(
                    "Cleared all exposure overrides for instrument %s", instrument_id
                )
                return

            for dimension, entries in exposures.items():
                if dimension not in ALLOWED_EXPOSURE_DIMENSIONS:
                    logger.debug(
                        "Skipping unsupported exposure dimension %s for instrument %s",
                        dimension,
                        instrument_id,
                    )
                    continue

                cur.execute(
                    """
                    DELETE FROM instrument_exposure_overrides
                    WHERE instrument_id = %s AND dimension = %s
                    """,
                    (instrument_id, dimension),
                )

                if not entries:
                    continue

                payload: list[Dict[str, object]] = []
                for label, weight in entries:
                    if label is None or label == "":
                        continue
                    if weight is None:
                        continue
                    try:
                        weight_decimal = Decimal(str(weight))
                    except Exception:  # pylint: disable=broad-except
                        logger.debug(
                            "Skipping invalid weight %s for %s/%s",
                            weight,
                            instrument_id,
                            label,
                        )
                        continue
                    payload.append(
                        {
                            "instrument_id": instrument_id,
                            "dimension": dimension,
                            "label": label,
                            "weight": weight_decimal,
                        }
                    )

                if payload:
                    cur.executemany(
                        """
                        INSERT INTO instrument_exposure_overrides (
                            instrument_id, dimension, label, weight
                        )
                        VALUES (
                            %(instrument_id)s,
                            %(dimension)s,
                            %(label)s,
                            %(weight)s
                        )
                        ON CONFLICT (instrument_id, dimension, label) DO UPDATE
                        SET weight = EXCLUDED.weight,
                            updated_at = NOW();
                        """,
                        payload,
                    )

        conn.commit()


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


def upsert_prices_hourly(pool: ConnectionPool, prices: Iterable[HourlyPrice]) -> int:
    prices = list(prices)
    if not prices:
        return 0

    query = sql.SQL(
        """
        INSERT INTO prices_hourly (
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

    logger.info("Upserted %s hourly prices", len(rows))
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


def get_cash_movements_up_to(pool: ConnectionPool, cutoff: datetime):
    query = """
    SELECT account_id, date_time_utc, currency, amount
    FROM cash_movements
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


def get_latest_hourly_prices(
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
    FROM prices_hourly
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


def get_latest_prices_with_hourly(
    pool: ConnectionPool, instrument_ids: Sequence[int], cutoff: datetime
) -> Dict[int, Price]:
    if not instrument_ids:
        return {}

    latest: Dict[int, Price] = get_latest_hourly_prices(pool, instrument_ids, cutoff)
    missing = [inst for inst in instrument_ids if inst not in latest]
    if missing:
        latest.update(get_latest_prices(pool, missing, cutoff))
    return latest


def get_hourly_prices_between(
    pool: ConnectionPool,
    instrument_id: int,
    start_at: datetime,
    end_at: datetime,
) -> List[Dict[str, object]]:
    query = """
    SELECT as_of_utc, close, currency, source
    FROM prices_hourly
    WHERE instrument_id = %s
      AND as_of_utc BETWEEN %s AND %s
    ORDER BY as_of_utc ASC;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (instrument_id, start_at, end_at))
            return cur.fetchall()


def get_hourly_prices_between_bulk(
    pool: ConnectionPool,
    instrument_ids: Sequence[int],
    start_at: datetime,
    end_at: datetime,
) -> Dict[int, List[Dict[str, object]]]:
    if not instrument_ids:
        return {}

    query = """
    SELECT instrument_id, as_of_utc, close, currency, source
    FROM prices_hourly
    WHERE instrument_id = ANY(%s)
      AND as_of_utc BETWEEN %s AND %s
    ORDER BY instrument_id, as_of_utc ASC;
    """

    results: DefaultDict[int, List[Dict[str, object]]] = defaultdict(list)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (list(instrument_ids), start_at, end_at))
            for row in cur.fetchall():
                results[row["instrument_id"]].append(row)

    return dict(results)


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


def get_fx_rate_on_or_before(
    pool: ConnectionPool, from_ccy: str, to_ccy: str, target_date: date
) -> Optional[FxRate]:
    query = """
    SELECT date_utc, rate, source
    FROM fx_rates
    WHERE from_ccy = %s AND to_ccy = %s AND date_utc <= %s
    ORDER BY date_utc DESC
    LIMIT 1;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (from_ccy, to_ccy, target_date))
            row = cur.fetchone()

    if not row:
        fallback_query = """
        SELECT date_utc, rate, source
        FROM fx_rates
        WHERE from_ccy = %s AND to_ccy = %s AND date_utc >= %s
        ORDER BY date_utc ASC
        LIMIT 1;
        """

        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(fallback_query, (from_ccy, to_ccy, target_date))
                row = cur.fetchone()

        if not row:
            return None

    return FxRate(
        date_utc=row["date_utc"],
        from_ccy=from_ccy,
        to_ccy=to_ccy,
        rate=row["rate"],
        source=row["source"],
    )


def upsert_data_gaps(pool: ConnectionPool, gaps: Iterable[DataGap]) -> int:
    gaps = list(gaps)
    if not gaps:
        return 0

    query = sql.SQL(
        """
        INSERT INTO data_gaps (
            gap_type,
            target_timestamp,
            instrument_id,
            account_id,
            details,
            detected_at
        ) VALUES (
            %(gap_type)s,
            %(target_timestamp)s,
            %(instrument_id)s,
            %(account_id)s,
            %(details)s,
            %(detected_at)s
        )
        ON CONFLICT (gap_type, target_timestamp, instrument_id, account_id)
        DO UPDATE SET
            detected_at = EXCLUDED.detected_at,
            details = EXCLUDED.details;
        """
    )

    now = datetime.now(timezone.utc)
    rows = [
        {
            "gap_type": gap.gap_type,
            "target_timestamp": gap.target_timestamp,
            "instrument_id": gap.instrument_id,
            "account_id": gap.account_id or _GLOBAL_ACCOUNT_ID,
            "details": Json(gap.details) if gap.details is not None else None,
            "detected_at": gap.detected_at or now,
        }
        for gap in gaps
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
        conn.commit()

    logger.debug("Upserted %d data gap records", len(rows))
    return len(rows)


def delete_data_gap(
    pool: ConnectionPool,
    gap_type: str,
    *,
    target_timestamp: Optional[datetime] = _ANY,  # type: ignore[assignment]
    instrument_id: Optional[int] = _ANY,  # type: ignore[assignment]
    account_id: Optional[str] = _ANY,  # type: ignore[assignment]
) -> int:
    conditions = ["gap_type = %(gap_type)s"]
    params: Dict[str, object] = {"gap_type": gap_type}

    if target_timestamp is not _ANY:
        if target_timestamp is None:
            conditions.append("target_timestamp IS NULL")
        else:
            conditions.append("target_timestamp = %(target_timestamp)s")
            params["target_timestamp"] = target_timestamp

    if instrument_id is not _ANY:
        if instrument_id is None:
            conditions.append("instrument_id IS NULL")
        else:
            conditions.append("instrument_id = %(instrument_id)s")
            params["instrument_id"] = instrument_id

    if account_id is not _ANY:
        if account_id is None:
            conditions.append("account_id = %(account_id)s")
            params["account_id"] = _GLOBAL_ACCOUNT_ID
        else:
            conditions.append("account_id = %(account_id)s")
            params["account_id"] = account_id

    where_clause = " AND ".join(conditions)
    query = f"DELETE FROM data_gaps WHERE {where_clause}"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            deleted = cur.rowcount
        conn.commit()

    if deleted:
        logger.debug("Deleted %d data gap rows for type %s", deleted, gap_type)
    return deleted


def get_portfolio_snapshots_range(
    pool: ConnectionPool,
    account_id: str,
    start_at: datetime,
    end_at: datetime,
) -> List[Dict[str, object]]:
    query = """
    SELECT snapshot_at,
           nav_eur,
           positions_value_eur,
           cash_value_eur,
           unrealized_pnl_eur,
           realized_pnl_eur,
           delta_eur,
           ret,
           drawdown
    FROM portfolio_value_snapshot
    WHERE account_id = %s
      AND snapshot_at BETWEEN %s AND %s
    ORDER BY snapshot_at ASC;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (account_id, start_at, end_at))
            return cur.fetchall()


def get_cash_movements_between(
    pool: ConnectionPool,
    account_id: str,
    start_at: datetime,
    end_at: datetime,
) -> List[Dict[str, object]]:
    query = """
    SELECT movement_id,
           date_time_utc,
           currency,
           amount,
           movement_type,
           description
    FROM cash_movements
    WHERE account_id = %s
      AND date_time_utc > %s
      AND date_time_utc <= %s
    ORDER BY date_time_utc ASC;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (account_id, start_at, end_at))
            return cur.fetchall()


def get_positions_at_snapshot(
    pool: ConnectionPool, account_id: str, snapshot_at: datetime
) -> List[Dict[str, object]]:
    query = """
    SELECT instrument_id,
           shares,
           cost_basis_ccy,
           cost_basis_eur
    FROM positions_snapshot
    WHERE account_id = %s
      AND snapshot_at = %s
    ORDER BY instrument_id;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (account_id, snapshot_at))
            return cur.fetchall()


def get_latest_positions_snapshot_time(pool: ConnectionPool, account_id: str) -> Optional[datetime]:
    query = """
    SELECT snapshot_at
    FROM positions_snapshot
    WHERE account_id = %s
    ORDER BY snapshot_at DESC
    LIMIT 1;
    """

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (account_id,))
            row = cur.fetchone()
            return row["snapshot_at"] if row else None


def replace_positions_snapshot(
    pool: ConnectionPool, snapshot_at: datetime, positions: Iterable[PositionSnapshot]
) -> None:
    positions = list(positions)

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
            "snapshot_at": snapshot_at,
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
            if rows:
                cur.executemany(insert, rows)
        conn.commit()


def replace_portfolio_value_snapshot(
    pool: ConnectionPool, snapshot_at: datetime, portfolios: Iterable[PortfolioValueSnapshot]
) -> None:
    portfolios = list(portfolios)

    delete = "DELETE FROM portfolio_value_snapshot WHERE snapshot_at = %s"
    insert = sql.SQL(
        """
        INSERT INTO portfolio_value_snapshot (
            snapshot_at,
            account_id,
            positions_value_eur,
            cash_value_eur,
            nav_eur,
            unrealized_pnl_eur,
            realized_pnl_eur,
            delta_eur,
            flow_eur,
            ret,
            drawdown,
            value_eur
        ) VALUES (
            %(snapshot_at)s,
            %(account_id)s,
            %(positions_value_eur)s,
            %(cash_value_eur)s,
            %(nav_eur)s,
            %(unrealized_pnl_eur)s,
            %(realized_pnl_eur)s,
            %(delta_eur)s,
            %(flow_eur)s,
            %(ret)s,
            %(drawdown)s,
            %(value_eur)s
        );
        """
    )

    rows = [
        {
            "snapshot_at": snapshot_at,
            "account_id": pv.account_id,
            "positions_value_eur": pv.positions_value_eur,
            "cash_value_eur": pv.cash_value_eur,
            "nav_eur": pv.nav_eur,
            "unrealized_pnl_eur": pv.unrealized_pnl_eur,
            "realized_pnl_eur": pv.realized_pnl_eur,
            "delta_eur": pv.delta_eur,
            "flow_eur": pv.flow_eur,
            "ret": pv.ret,
            "drawdown": pv.drawdown,
            "value_eur": pv.nav_eur,
        }
        for pv in portfolios
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(delete, (snapshot_at,))
            if rows:
                cur.executemany(insert, rows)
        conn.commit()


def replace_realized_pnl_fifo(
    pool: ConnectionPool, snapshot_at: datetime, lots: Iterable[RealizedPnlLot]
) -> None:
    lots = list(lots)

    delete = "DELETE FROM realized_pnl_fifo WHERE close_snapshot_at = %s"
    insert = sql.SQL(
        """
        INSERT INTO realized_pnl_fifo (
            account_id,
            instrument_id,
            lot_opened_at,
            lot_closed_at,
            close_snapshot_at,
            qty_closed,
            proceeds_ccy,
            proceeds_eur,
            cost_ccy,
            cost_eur,
            pnl_ccy,
            pnl_eur
        ) VALUES (
            %(account_id)s,
            %(instrument_id)s,
            %(lot_opened_at)s,
            %(lot_closed_at)s,
            %(close_snapshot_at)s,
            %(qty_closed)s,
            %(proceeds_ccy)s,
            %(proceeds_eur)s,
            %(cost_ccy)s,
            %(cost_eur)s,
            %(pnl_ccy)s,
            %(pnl_eur)s
        );
        """
    )

    rows = [
        {
            "account_id": lot.account_id,
            "instrument_id": lot.instrument_id,
            "lot_opened_at": lot.lot_opened_at,
            "lot_closed_at": lot.lot_closed_at,
            "close_snapshot_at": lot.close_snapshot_at,
            "qty_closed": lot.qty_closed,
            "proceeds_ccy": lot.proceeds_ccy,
            "proceeds_eur": lot.proceeds_eur,
            "cost_ccy": lot.cost_ccy,
            "cost_eur": lot.cost_eur,
            "pnl_ccy": lot.pnl_ccy,
            "pnl_eur": lot.pnl_eur,
        }
        for lot in lots
    ]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(delete, (snapshot_at,))
            if rows:
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
            COALESCE(nav_eur, value_eur) AS prev_nav,
            realized_pnl_eur AS prev_realized,
            cash_value_eur AS prev_cash,
            positions_value_eur AS prev_positions,
            snapshot_at
        FROM portfolio_value_snapshot
        WHERE account_id = ANY(%s) AND snapshot_at < %s
        ORDER BY account_id, snapshot_at DESC
    ),
    peaks AS (
        SELECT account_id, MAX(COALESCE(nav_eur, value_eur)) AS max_value
        FROM portfolio_value_snapshot
        WHERE account_id = ANY(%s) AND snapshot_at < %s
        GROUP BY account_id
    )
    SELECT l.account_id,
           l.prev_nav,
           l.prev_realized,
           l.prev_cash,
           l.prev_positions,
           p.max_value,
           l.snapshot_at AS prev_snapshot_at
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
        prev_nav = row.get("prev_nav")
        summary[account_id] = {
            "prev_nav": prev_nav,
            "prev_value": prev_nav,
            "prev_realized": row.get("prev_realized"),
            "prev_cash": row.get("prev_cash"),
            "prev_positions": row.get("prev_positions"),
            "max_value": row.get("max_value"),
            "prev_snapshot_at": row.get("prev_snapshot_at"),
        }
    return summary

