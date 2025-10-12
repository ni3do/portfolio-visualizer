from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class Instrument:
    instrument_id: int
    symbol: str
    yfinance_symbol: Optional[str]
    name: Optional[str]
    currency: str
    asset_class: Optional[str]
    primary_exchange: Optional[str]
    sector: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None


@dataclass(frozen=True)
class Transaction:
    trade_id: str
    account_id: str
    date_time_utc: datetime
    type: str
    instrument_id: int
    qty: Decimal
    price: Decimal
    currency: str
    fees: Decimal
    net_amount: Decimal
    source: str
    raw_flex_id: Optional[str]


@dataclass(frozen=True)
class CashMovement:
    movement_id: str
    account_id: str
    date_time_utc: datetime
    currency: str
    amount: Decimal
    movement_type: Optional[str]
    description: Optional[str]
    source: str


@dataclass(frozen=True)
class FxRate:
    date_utc: date
    from_ccy: str
    to_ccy: str
    rate: Decimal
    source: str


@dataclass(frozen=True)
class PriceTarget:
    instrument_id: int
    ticker: str
    currency: str
    asset_class: Optional[str] = None


@dataclass(frozen=True)
class Price:
    instrument_id: int
    as_of_utc: datetime
    close: Decimal
    currency: str
    source: str


@dataclass(frozen=True)
class PositionSnapshot:
    snapshot_at: datetime
    account_id: str
    instrument_id: int
    shares: Decimal
    cost_basis_ccy: Decimal
    cost_basis_eur: Decimal


@dataclass(frozen=True)
class PortfolioValueSnapshot:
    snapshot_at: datetime
    account_id: str
    value_eur: Decimal
    ret: Optional[Decimal]
    drawdown: Optional[Decimal]
