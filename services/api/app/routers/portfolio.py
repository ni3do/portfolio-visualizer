from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from .. import repositories
from ..config import load_settings
from ..database import get_db_connection
from ..models import (
    ExposureResponse,
    ExposureSlice,
    DividendsResponse,
    PortfolioPosition,
    PortfolioSeriesResponse,
    PositionsResponse,
    ReturnsResponse,
    TimeSeriesPoint,
    UnrealizedItem,
    UnrealizedResponse,
)
from ..security import get_current_username

router = APIRouter(prefix="/portfolio")


@router.get(
    "/value",
    response_model=PortfolioSeriesResponse,
    summary="Portfolio NAV time series",
)
def portfolio_value_series(
    *,
    start: Optional[datetime] = Query(
        default=None,
        alias="from",
        description="Start timestamp (UTC). Defaults to 90 days ago.",
    ),
    end: Optional[datetime] = Query(
        default=None,
        alias="to",
        description="End timestamp (UTC). Defaults to now.",
    ),
    interval: str = Query(
        default="1h",
        description="Aggregation interval. Supported: 1h, 1d.",
        pattern="^([1-9][0-9]*)(h|d)$",
    ),
    account_id: Optional[str] = Query(
        default=None,
        description="Filter by account identifier.",
    ),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> PortfolioSeriesResponse:
    if start and end and start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )

    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=90)

    rows = repositories.fetch_portfolio_nav_series(
        conn,
        start=start,
        end=end,
        interval=interval,
        account_id=account_id,
    )

    points = [
        TimeSeriesPoint(timestamp=row["bucket"], value=float(row["nav_eur"] or 0))
        for row in rows
    ]

    return PortfolioSeriesResponse(points=points)


@router.get(
    "/unrealized",
    response_model=UnrealizedResponse,
    summary="Latest unrealized PnL per instrument",
)
def portfolio_unrealized(
    *,
    account_id: Optional[str] = Query(default=None, description="Filter by account."),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> UnrealizedResponse:
    rows = repositories.fetch_unrealized_pnl(conn, account_id=account_id)
    items = [
        UnrealizedItem(
            symbol=row["symbol"],
            name=row.get("name"),
            market_value_eur=float(row["market_value_eur"])
            if row.get("market_value_eur") is not None
            else None,
            unrealized_pnl_eur=float(row["unrealized_pnl_eur"])
            if row.get("unrealized_pnl_eur") is not None
            else 0.0,
        )
        for row in rows
    ]
    return UnrealizedResponse(items=items)


def _exposure_response(rows: list[dict]) -> ExposureResponse:
    total_dec = sum(Decimal(row["total_eur"] or 0) for row in rows)
    total = float(total_dec)
    slices = [
        ExposureSlice(
            label=row["label"],
            value_eur=float(row["total_eur"] or 0),
            weight=float(Decimal(row["total_eur"] or 0) / total_dec) if total_dec else 0.0,
        )
        for row in rows
    ]
    return ExposureResponse(slices=slices, total_eur=total)


@router.get(
    "/exposure/{dimension}",
    response_model=ExposureResponse,
    summary="Portfolio exposure snapshot",
)
def portfolio_exposure(
    *,
    dimension: str,
    account_id: Optional[str] = Query(default=None, description="Filter by account."),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> ExposureResponse:
    try:
        rows = repositories.fetch_exposure(
            conn, dimension=dimension, account_id=account_id
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return _exposure_response(rows)


@router.get(
    "/dividends",
    response_model=DividendsResponse,
    summary="Dividend cash flows",
)
def portfolio_dividends(
    *,
    start: Optional[datetime] = Query(
        default=None,
        alias="from",
        description="Start timestamp (UTC) filter.",
    ),
    end: Optional[datetime] = Query(
        default=None,
        alias="to",
        description="End timestamp (UTC) filter.",
    ),
    account_id: Optional[str] = Query(default=None, description="Filter by account."),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> DividendsResponse:
    if start and end and start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )

    settings = load_settings()
    rows = repositories.fetch_dividends(
        conn,
        base_currency=settings.base_currency,
        start=start,
        end=end,
        account_id=account_id,
    )

    dividends = []
    total_base = 0.0
    for row in rows:
        amount = float(row["amount"] or 0.0)
        currency = (row.get("currency") or settings.base_currency).upper()
        fx_rate = row.get("fx_rate")
        if currency == settings.base_currency:
            rate = 1.0
        else:
            rate = float(fx_rate) if fx_rate is not None else None

        if rate is not None:
            amount_base = amount * rate
            total_base += amount_base
        else:
            amount_base = amount

        dividends.append(
            {
                "payment_date": row["date_time_utc"],
                "account_id": row["account_id"],
                "amount": amount,
                "amount_base": amount_base,
                "currency": currency,
                "description": row.get("description"),
                "fx_rate": rate,
            }
        )

    return DividendsResponse(
        dividends=dividends,
        total_amount_base=total_base,
    )


@router.get(
    "/returns",
    response_model=ReturnsResponse,
    summary="Portfolio return series",
)
def portfolio_returns(
    *,
    start: Optional[datetime] = Query(
        default=None,
        alias="from",
        description="Start timestamp (UTC). Defaults to 30 days ago.",
    ),
    end: Optional[datetime] = Query(
        default=None,
        alias="to",
        description="End timestamp (UTC). Defaults to now.",
    ),
    interval: str = Query(
        default="1d",
        description="Aggregation interval. Supported: 1h, 1d.",
        pattern="^([1-9][0-9]*)(h|d)$",
    ),
    account_id: Optional[str] = Query(default=None, description="Filter by account."),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> ReturnsResponse:
    settings = load_settings()
    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=30)
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )

    rows = repositories.fetch_returns_series(
        conn,
        base_currency=settings.base_currency,
        start=start,
        end=end,
        interval=interval,
        account_id=account_id,
    )

    points = []
    for row in rows:
        nav = float(row.get("nav_eur") or 0.0)
        delta = float(row.get("delta_eur") or 0.0)
        pct = row.get("return_pct")
        points.append(
            {
                "timestamp": row["bucket"],
                "nav": nav,
                "delta": delta,
                "return_pct": float(pct) if pct is not None else None,
            }
        )

    return ReturnsResponse(points=points)


@router.get(
    "/positions",
    response_model=PositionsResponse,
    summary="Paginated holdings table",
)
def portfolio_positions(
    *,
    account_id: Optional[str] = Query(default=None, description="Filter by account."),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> PositionsResponse:
    rows = repositories.fetch_positions(conn, account_id=account_id)
    total = repositories.fetch_portfolio_totals(conn, account_id=account_id)
    if total is None:
        total = sum((row.get("market_value_eur") or 0) for row in rows)

    positions = [
        PortfolioPosition.from_row(row, portfolio_total=total) for row in rows
    ]
    return PositionsResponse(
        positions=positions,
        total_eur=float(total or 0),
    )
