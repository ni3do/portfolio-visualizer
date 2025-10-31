from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Optional


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
    industry: Optional[str] = None
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
class HourlyPrice:
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
    positions_value_eur: Optional[Decimal] = None
    cash_value_eur: Optional[Decimal] = None
    nav_eur: Optional[Decimal] = None
    unrealized_pnl_eur: Optional[Decimal] = None
    realized_pnl_eur: Optional[Decimal] = None
    delta_eur: Optional[Decimal] = None
    flow_eur: Optional[Decimal] = None
    ret: Optional[Decimal] = None
    drawdown: Optional[Decimal] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class PositionTicker:
    account_id: str
    instrument_id: int
    ticker: str
    shares: Decimal
    currency: str


@dataclass(frozen=True)
class InstrumentMetadataTarget:
    instrument_id: int
    ticker: str


@dataclass(frozen=True)
class RealizedPnlLot:
    account_id: str
    instrument_id: int
    lot_opened_at: datetime
    lot_closed_at: datetime
    close_snapshot_at: datetime
    qty_closed: Decimal
    proceeds_ccy: Decimal
    proceeds_eur: Decimal
    cost_ccy: Decimal
    cost_eur: Decimal
    pnl_ccy: Decimal
    pnl_eur: Decimal
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class DataGap:
    gap_type: str
    target_timestamp: datetime
    detected_at: Optional[datetime]
    instrument_id: Optional[int]
    account_id: Optional[str]
    details: Optional[Dict[str, object]]
