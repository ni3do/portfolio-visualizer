from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel


def _decimal_to_float(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


class TimeSeriesPoint(BaseModel):
    timestamp: datetime
    value: float


class PortfolioSeriesResponse(BaseModel):
    points: List[TimeSeriesPoint]


class ExposureSlice(BaseModel):
    label: str
    value_eur: float
    weight: float


class ExposureResponse(BaseModel):
    slices: List[ExposureSlice]
    total_eur: float


class PortfolioPosition(BaseModel):
    account_id: str
    symbol: str
    name: Optional[str] = None
    shares: float
    currency: str
    market_value_eur: Optional[float] = None
    cost_basis_eur: Optional[float] = None
    unrealized_pnl_eur: Optional[float] = None
    weight: Optional[float] = None
    last_price: Optional[float] = None
    last_price_as_of: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: dict, *, portfolio_total: Optional[Decimal]) -> "PortfolioPosition":
        total = portfolio_total or Decimal(0)
        market_value = row.get("market_value_eur")
        weight = None
        if market_value and total:
            weight = float(market_value / total)
        return cls(
            account_id=row["account_id"],
            symbol=row["symbol"],
            name=row.get("name"),
            shares=float(row["shares"]) if row.get("shares") is not None else 0.0,
            currency=row.get("instrument_ccy") or row.get("currency") or "",
            market_value_eur=_decimal_to_float(market_value),
            cost_basis_eur=_decimal_to_float(row.get("cost_basis_eur")),
            unrealized_pnl_eur=_decimal_to_float(row.get("unrealized_pnl_eur")),
            weight=weight,
            last_price=_decimal_to_float(row.get("last_price")),
            last_price_as_of=row.get("last_price_as_of"),
        )


class PositionsResponse(BaseModel):
    positions: List[PortfolioPosition]
    total_eur: float


class RecentTrade(BaseModel):
    executed_at: datetime
    account_id: str
    symbol: str
    trade_type: str
    quantity: float
    price: float
    currency: str
    net_amount: float
    fees: float


class TradesResponse(BaseModel):
    trades: List[RecentTrade]


class CacheMetrics(BaseModel):
    hits: int
    misses: int
    backend: str


class UnrealizedItem(BaseModel):
    symbol: str
    name: Optional[str] = None
    market_value_eur: Optional[float] = None
    unrealized_pnl_eur: float


class UnrealizedResponse(BaseModel):
    items: List[UnrealizedItem]


class DividendEntry(BaseModel):
    payment_date: datetime
    account_id: str
    amount: float
    amount_base: float
    currency: str
    description: Optional[str] = None
    fx_rate: Optional[float] = None


class DividendsResponse(BaseModel):
    dividends: List[DividendEntry]
    total_amount_base: float


class ReturnPoint(BaseModel):
    timestamp: datetime
    nav: float
    delta: float
    return_pct: Optional[float]


class ReturnsResponse(BaseModel):
    points: List[ReturnPoint]
