from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from ..database import get_db_connection
from ..models import RecentTrade, TradesResponse
from ..repositories import fetch_recent_trades
from ..security import get_current_username

router = APIRouter(prefix="/transactions")


def _to_float(value: Decimal | None) -> float:
    if value is None:
        return 0.0
    return float(value)


@router.get(
    "/recent",
    response_model=TradesResponse,
    summary="Recent transactions",
)
def recent_transactions(
    *,
    limit: int = Query(
        default=25,
        ge=1,
        le=200,
        description="Maximum number of trades to return.",
    ),
    account_id: Optional[str] = Query(
        default=None, description="Filter by account identifier."
    ),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> TradesResponse:
    rows = fetch_recent_trades(conn, limit=limit, account_id=account_id)
    trades = [
        RecentTrade(
            executed_at=row["executed_at"],
            account_id=row["account_id"],
            symbol=row["symbol"],
            trade_type=row["trade_type"],
            quantity=_to_float(row.get("qty")),
            price=_to_float(row.get("price")),
            currency=row["currency"],
            net_amount=_to_float(row.get("net_amount")),
            fees=_to_float(row.get("fees")),
        )
        for row in rows
    ]
    return TradesResponse(trades=trades)

