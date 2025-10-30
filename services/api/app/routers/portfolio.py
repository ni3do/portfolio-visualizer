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
    PortfolioReturnMetrics,
    PortfolioSeriesResponse,
    PositionReturnBreakdown,
    PositionsResponse,
    ReturnsOverviewResponse,
    ReturnsResponse,
    TimeSeriesPoint,
    UnrealizedItem,
    UnrealizedResponse,
)
from ..security import get_current_username

ReturnKey = tuple[str, int]

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
    "/returns/overview",
    response_model=ReturnsOverviewResponse,
    summary="Portfolio return metrics",
)
def portfolio_returns_overview(
    *,
    start: Optional[datetime] = Query(
        default=None,
        alias="from",
        description="Start timestamp (UTC). Defaults to 1 year ago.",
    ),
    end: Optional[datetime] = Query(
        default=None,
        alias="to",
        description="End timestamp (UTC). Defaults to now.",
    ),
    account_id: Optional[str] = Query(default=None, description="Filter by account."),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> ReturnsOverviewResponse:
    settings = load_settings()
    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=365)
    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )

    positions_rows = repositories.fetch_positions(conn, account_id=account_id)
    as_of = repositories.fetch_latest_positions_snapshot_time(conn, account_id=account_id)
    if as_of is None:
        as_of = end

    total_market = 0.0
    total_cost = 0.0
    for row in positions_rows:
        total_market += float(row.get("market_value_eur") or 0.0)
        total_cost += float(row.get("cost_basis_eur") or 0.0)

    position_series = repositories.fetch_position_return_timeseries(
        conn,
        base_currency=settings.base_currency,
        start=start,
        end=end,
        account_id=account_id,
    )
    position_twr = _calculate_position_time_weighted_returns(position_series)

    positions: list[PositionReturnBreakdown] = []
    for row in positions_rows:
        market_value = float(row.get("market_value_eur") or 0.0)
        cost_basis = float(row.get("cost_basis_eur") or 0.0)
        key: ReturnKey = (row["account_id"], row["instrument_id"])
        price_return = (market_value / cost_basis - 1.0) if cost_basis else None
        weight = (market_value / total_market) if total_market else None
        positions.append(
            PositionReturnBreakdown(
                account_id=row["account_id"],
                symbol=row["symbol"],
                name=row.get("name"),
                market_value_eur=market_value,
                cost_basis_eur=cost_basis,
                price_return_pct=price_return,
                time_weighted_return_pct=position_twr.get(key),
                weight=weight,
            )
        )

    total_delta = total_market - total_cost
    portfolio_price_return = (total_market / total_cost - 1.0) if total_cost else None

    returns_rows = repositories.fetch_returns_series(
        conn,
        base_currency=settings.base_currency,
        start=start,
        end=end,
        interval="1d",
        account_id=account_id,
    )
    portfolio_twr = _compound_time_weighted_returns(returns_rows)

    portfolio_metrics = PortfolioReturnMetrics(
        market_value_eur=total_market,
        cost_basis_eur=total_cost,
        delta_eur=total_delta,
        price_return_pct=portfolio_price_return,
        time_weighted_return_pct=portfolio_twr,
    )

    return ReturnsOverviewResponse(
        as_of=as_of,
        portfolio=portfolio_metrics,
        positions=positions,
    )


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


def _compound_time_weighted_returns(rows: list[dict]) -> Optional[float]:
    total = 1.0
    has_values = False
    for row in rows:
        pct = row.get("return_pct")
        if pct is None:
            continue
        has_values = True
        total *= 1 + float(pct)
    if not has_values:
        return None
    return total - 1


def _calculate_position_time_weighted_returns(
    rows: list[dict],
) -> dict[ReturnKey, float]:
    grouped: dict[ReturnKey, list[dict]] = {}
    for row in rows:
        key: ReturnKey = (row["account_id"], row["instrument_id"])
        grouped.setdefault(key, []).append(row)

    results: dict[ReturnKey, float] = {}
    for key, entries in grouped.items():
        entries.sort(key=lambda item: item["bucket"])
        prev_nav: Optional[float] = None
        product = 1.0
        has_values = False
        for entry in entries:
            nav = float(entry.get("nav_eur") or 0.0)
            if prev_nav is None:
                prev_nav = nav
                continue
            contribution = float(entry.get("contribution_eur") or 0.0)
            denominator = prev_nav + contribution
            if denominator == 0:
                prev_nav = nav
                continue
            period_return = (nav - (prev_nav + contribution)) / denominator
            product *= 1 + period_return
            has_values = True
            prev_nav = nav
        if has_values:
            results[key] = product - 1
    return results
