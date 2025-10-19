from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import db

logger = logging.getLogger(__name__)


DecimalZero = Decimal("0")
DecimalOne = Decimal("1")


@dataclass
class ReturnMetrics:
    account_id: str
    start_at: datetime
    end_at: datetime
    nav_start: Decimal
    nav_end: Decimal
    absolute_change: Decimal
    percent_change: Optional[Decimal]
    twr: Optional[Decimal]
    sub_periods: List[Tuple[datetime, Decimal]]
    mwr: Optional[float]
    mwr_annualized: Optional[float]
    contributions: Decimal
    withdrawals: Decimal
    net_flow: Decimal
    realized_pnl_change: Decimal


@dataclass
class PositionPnlRow:
    instrument_id: int
    symbol: str
    shares: Decimal
    price: Optional[Decimal]
    price_currency: Optional[str]
    price_as_of: Optional[datetime]
    value_eur: Optional[Decimal]
    cost_eur: Decimal
    unrealized_eur: Optional[Decimal]
    simple_return: Optional[Decimal]
    hourly_return: Optional[Decimal]


class FxConverter:
    def __init__(self, base_currency: str, pool) -> None:
        self.base_currency = base_currency
        self.pool = pool
        self._cache: Dict[Tuple[str, datetime.date], Optional[Decimal]] = {}

    def rate(self, from_currency: str, timestamp: datetime) -> Optional[Decimal]:
        currency = (from_currency or "").upper()
        if not currency or currency == self.base_currency:
            return DecimalOne

        key = (currency, timestamp.date())
        if key in self._cache:
            return self._cache[key]

        direct = db.get_fx_rate_on_or_before(
            self.pool, currency, self.base_currency, timestamp.date()
        )
        if direct:
            self._cache[key] = direct.rate
            return direct.rate

        inverse = db.get_fx_rate_on_or_before(
            self.pool, self.base_currency, currency, timestamp.date()
        )
        if inverse and inverse.rate != 0:
            rate = DecimalOne / inverse.rate
            self._cache[key] = rate
            return rate

        self._cache[key] = None
        return None

    def convert(self, amount: Decimal, from_currency: str, timestamp: datetime) -> Optional[Decimal]:
        rate = self.rate(from_currency, timestamp)
        if rate is None:
            return None
        return amount * rate


class PerformanceCalculator:
    def __init__(self, pool, base_currency: str) -> None:
        self.pool = pool
        self.base_currency = base_currency
        self.fx = FxConverter(base_currency, pool)

    def compute_account_returns(
        self, account_id: str, start_at: datetime, end_at: datetime
    ) -> ReturnMetrics:
        if start_at >= end_at:
            raise ValueError("start_at must be before end_at")

        snapshots = db.get_portfolio_snapshots_range(self.pool, account_id, start_at, end_at)
        if not snapshots:
            raise ValueError("No snapshots available in the requested range")

        nav_start = Decimal(str(snapshots[0]["nav_eur"] or 0))
        nav_end = Decimal(str(snapshots[-1]["nav_eur"] or 0))
        absolute_change = nav_end - nav_start
        percent_change = None
        if nav_start != 0:
            percent_change = (nav_end / nav_start) - DecimalOne

        flows = self._load_cash_flows(account_id, snapshots[0]["snapshot_at"], snapshots[-1]["snapshot_at"])
        contributions = self._sum_positive(flows)
        withdrawals_raw = self._sum_negative(flows)
        withdrawals = abs(withdrawals_raw)
        net_flow = contributions + withdrawals_raw

        flows_by_interval = self._map_flows_to_intervals(flows, snapshots)
        sub_periods: List[Tuple[datetime, Decimal]] = []
        product = DecimalOne
        for idx in range(1, len(snapshots)):
            start_snapshot = snapshots[idx - 1]
            end_snapshot = snapshots[idx]
            start_nav = Decimal(str(start_snapshot["nav_eur"] or 0))
            end_nav = Decimal(str(end_snapshot["nav_eur"] or 0))
            flow = flows_by_interval[idx]
            if start_nav == 0:
                continue
            period_return = ((end_nav - flow) / start_nav) - DecimalOne
            sub_periods.append((end_snapshot["snapshot_at"], period_return))
            product *= (DecimalOne + period_return)

        twr = (product - DecimalOne) if sub_periods else None

        realized_start = Decimal(str(snapshots[0]["realized_pnl_eur"] or 0))
        realized_end = Decimal(str(snapshots[-1]["realized_pnl_eur"] or 0))
        realized_delta = realized_end - realized_start

        cash_flows = self._build_cash_flow_series(nav_start, nav_end, flows, snapshots)
        mwr, mwr_annualized = self._money_weighted_return(
            cash_flows, snapshots[0]["snapshot_at"], snapshots[-1]["snapshot_at"]
        )

        return ReturnMetrics(
            account_id=account_id,
            start_at=snapshots[0]["snapshot_at"],
            end_at=snapshots[-1]["snapshot_at"],
            nav_start=nav_start,
            nav_end=nav_end,
            absolute_change=absolute_change,
            percent_change=percent_change,
            twr=twr,
            sub_periods=sub_periods,
            mwr=mwr,
            mwr_annualized=mwr_annualized,
            contributions=contributions,
            withdrawals=withdrawals,
            net_flow=net_flow,
            realized_pnl_change=realized_delta,
        )

    def build_position_report(
        self,
        account_id: str,
        as_of: Optional[datetime] = None,
        hourly_window: int = 24,
    ) -> tuple[datetime, List[PositionPnlRow]]:
        if as_of is None:
            snapshot_at = db.get_latest_positions_snapshot_time(self.pool, account_id)
            if snapshot_at is None:
                raise ValueError("No position snapshots available for account")
        else:
            snapshot_at = as_of

        positions = db.get_positions_at_snapshot(self.pool, account_id, snapshot_at)
        if not positions:
            raise ValueError("No positions recorded for the selected snapshot")

        instrument_ids = [row["instrument_id"] for row in positions]
        instrument_map = db.get_instruments_by_ids(self.pool, instrument_ids)
        price_map = db.get_latest_prices_with_hourly(self.pool, instrument_ids, snapshot_at)

        window_start = snapshot_at - timedelta(hours=hourly_window)
        rows: List[PositionPnlRow] = []
        for row in positions:
            instrument_id = row["instrument_id"]
            shares = Decimal(str(row["shares"]))
            cost_eur = Decimal(str(row["cost_basis_eur"] or 0))
            instrument_info = instrument_map.get(instrument_id, {})
            symbol = (
                instrument_info.get("symbol")
                or instrument_info.get("yfinance_symbol")
                or str(instrument_id)
            )

            price_entry = price_map.get(instrument_id)
            value_eur = None
            unrealized = None
            simple_return = None
            if price_entry:
                value_ccy = shares * price_entry.close
                converted = self.fx.convert(value_ccy, price_entry.currency, snapshot_at)
                if converted is not None:
                    value_eur = converted
                    unrealized = converted - cost_eur
                    if cost_eur != 0:
                        simple_return = unrealized / cost_eur

            hourly_return = None
            hourly_prices = db.get_hourly_prices_between(
                self.pool, instrument_id, window_start, snapshot_at
            )
            if hourly_prices:
                first_close = Decimal(str(hourly_prices[0]["close"]))
                last_close = Decimal(str(hourly_prices[-1]["close"]))
                if first_close != 0:
                    hourly_return = (last_close - first_close) / first_close

            rows.append(
                PositionPnlRow(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    shares=shares,
                    price=price_entry.close if price_entry else None,
                    price_currency=price_entry.currency if price_entry else None,
                    price_as_of=price_entry.as_of_utc if price_entry else None,
                    value_eur=value_eur,
                    cost_eur=cost_eur,
                    unrealized_eur=unrealized,
                    simple_return=simple_return,
                    hourly_return=hourly_return,
                )
            )

        return snapshot_at, rows

    def _load_cash_flows(
        self, account_id: str, start_at: datetime, end_at: datetime
    ) -> List[Tuple[datetime, Decimal]]:
        rows = db.get_cash_movements_between(self.pool, account_id, start_at, end_at)
        flows: List[Tuple[datetime, Decimal]] = []
        for row in rows:
            amount = Decimal(str(row["amount"]))
            currency = row.get("currency") or self.base_currency
            converted = self.fx.convert(amount, currency, row["date_time_utc"])
            if converted is None:
                logger.warning(
                    "Missing FX rate for cash movement %s %s at %s",
                    amount,
                    currency,
                    row["date_time_utc"],
                )
                continue
            flows.append((row["date_time_utc"], converted))

        flows.sort(key=lambda item: item[0])
        return flows

    @staticmethod
    def _sum_positive(flows: Iterable[Tuple[datetime, Decimal]]) -> Decimal:
        total = DecimalZero
        for _, amount in flows:
            if amount >= 0:
                total += amount
        return total

    @staticmethod
    def _sum_negative(flows: Iterable[Tuple[datetime, Decimal]]) -> Decimal:
        total = DecimalZero
        for _, amount in flows:
            if amount < 0:
                total += amount
        return total

    @staticmethod
    def _map_flows_to_intervals(
        flows: List[Tuple[datetime, Decimal]],
        snapshots: Sequence[Dict[str, object]],
    ) -> List[Decimal]:
        if len(snapshots) < 2:
            return [DecimalZero] * len(snapshots)

        totals = [DecimalZero for _ in snapshots]
        idx = 1
        for flow_time, amount in flows:
            while idx < len(snapshots) and flow_time > snapshots[idx]["snapshot_at"]:
                idx += 1
            if idx < len(snapshots):
                totals[idx] += amount
        return totals

    @staticmethod
    def _build_cash_flow_series(
        nav_start: Decimal,
        nav_end: Decimal,
        flows: List[Tuple[datetime, Decimal]],
        snapshots: Sequence[Dict[str, object]],
    ) -> List[Tuple[datetime, float]]:
        series: List[Tuple[datetime, float]] = []
        start_at = snapshots[0]["snapshot_at"]
        end_at = snapshots[-1]["snapshot_at"]

        series.append((start_at, float(-nav_start)))
        for flow_time, amount in flows:
            series.append((flow_time, float(-amount)))
        series.append((end_at, float(nav_end)))
        series.sort(key=lambda item: item[0])
        return series

    @staticmethod
    def _money_weighted_return(
        cash_flows: List[Tuple[datetime, float]],
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[Optional[float], Optional[float]]:
        if len(cash_flows) < 2:
            return None, None

        cash_flows.sort(key=lambda item: item[0])
        start = cash_flows[0][0]

        def npv(rate: float) -> float:
            total = 0.0
            for ts, amount in cash_flows:
                years = (ts - start).total_seconds() / (365.0 * 24 * 3600)
                try:
                    total += amount / ((1 + rate) ** years)
                except OverflowError:
                    return float("inf")
            return total

        low = -0.999
        high = 10.0
        npv_low = npv(low)
        npv_high = npv(high)
        if npv_low == 0:
            irr = low
        elif npv_high == 0:
            irr = high
        elif npv_low * npv_high > 0:
            return None, None
        else:
            irr = None
            for _ in range(100):
                mid = (low + high) / 2
                value = npv(mid)
                if abs(value) < 1e-6:
                    irr = mid
                    break
                if value * npv_low < 0:
                    high = mid
                    npv_high = value
                else:
                    low = mid
                    npv_low = value
            if irr is None:
                irr = (low + high) / 2

        total_days = max(1, (end_at - start_at).days)
        annualized = (1 + irr) ** (365 / total_days) - 1 if total_days > 0 else None
        return irr, annualized
