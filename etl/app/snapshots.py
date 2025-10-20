from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, DefaultDict, Dict, Iterable, List, Optional, Tuple

from zoneinfo import ZoneInfo

from . import db
from .models import DataGap, PortfolioValueSnapshot, PositionSnapshot, RealizedPnlLot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotSettings:
    base_currency: str
    timezone: str


class DataGapRecorder:
    def __init__(self) -> None:
        self._pending: Dict[Tuple[str, datetime, Optional[int], Optional[str]], DataGap] = {}
        self._clears: set[Tuple[str, datetime, Optional[int], Optional[str]]] = set()

    def record_gap(
        self,
        gap_type: str,
        target_timestamp: datetime,
        *,
        instrument_id: Optional[int] = None,
        account_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        key = (gap_type, target_timestamp, instrument_id, account_id)
        self._clears.discard(key)
        self._pending[key] = DataGap(
            gap_type=gap_type,
            target_timestamp=target_timestamp,
            instrument_id=instrument_id,
            account_id=account_id,
            details=details or {},
            detected_at=datetime.now(timezone.utc),
        )

    def clear_gap(
        self,
        gap_type: str,
        target_timestamp: datetime,
        *,
        instrument_id: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> None:
        key = (gap_type, target_timestamp, instrument_id, account_id)
        self._pending.pop(key, None)
        self._clears.add(key)

    def flush(self, pool) -> None:
        if self._pending:
            db.upsert_data_gaps(pool, self._pending.values())
        for gap_type, target_ts, instrument_id, account_id in self._clears:
            db.delete_data_gap(
                pool,
                gap_type,
                target_timestamp=target_ts,
                instrument_id=instrument_id,
                account_id=account_id,
            )
        self._pending.clear()
        self._clears.clear()


class FxResolver:
    def __init__(self, pool, base_currency: str) -> None:
        self.pool = pool
        self.base_currency = base_currency
        self._cache: Dict[Tuple[str, date], Optional[Decimal]] = {}

    def rate(self, currency: str, timestamp: datetime) -> Optional[Decimal]:
        currency = (currency or "").upper()
        if not currency or currency == self.base_currency:
            return Decimal("1")

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
            rate = Decimal("1") / inverse.rate
            self._cache[key] = rate
            return rate

        self._cache[key] = None
        return None

    def convert(self, amount: Decimal, currency: str, timestamp: datetime) -> Optional[Decimal]:
        rate = self.rate(currency, timestamp)
        if rate is None:
            return None
        return amount * rate


class SnapshotRecalculator:
    def __init__(self, settings: SnapshotSettings, pool):
        self.settings = settings
        self.pool = pool
        self.tz = ZoneInfo(settings.timezone)

    def run(self, target_ts: datetime | None = None) -> None:
        target_ts = target_ts or datetime.now(self.tz)
        target_ts = target_ts.astimezone(self.tz).replace(minute=0, second=0, microsecond=0)
        cutoff_utc = target_ts.astimezone(timezone.utc)

        transactions = db.get_transactions_up_to(self.pool, cutoff_utc)
        cash_movements = db.get_cash_movements_up_to(self.pool, cutoff_utc)
        if not transactions and not cash_movements:
            logger.info("No portfolio activity found up to %s; skipping snapshot", cutoff_utc)
            return

        gap_recorder = DataGapRecorder()

        fx_resolver = FxResolver(self.pool, self.settings.base_currency)

        missing_price_logged: set[int] = set()
        try:
            (
                positions_state,
                realized_entries,
                realized_totals,
            ) = self._build_positions_and_realized(
                transactions, fx_resolver, target_ts, gap_recorder
            )

            instrument_ids = {instrument_id for (_, instrument_id) in positions_state.keys()}
            instrument_meta = db.get_instruments_by_ids(self.pool, list(instrument_ids))
            price_map = (
                db.get_latest_prices_with_hourly(self.pool, list(instrument_ids), cutoff_utc)
                if instrument_ids
                else {}
            )

            positions: List[PositionSnapshot] = []
            positions_totals: DefaultDict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            cost_totals: DefaultDict[str, Decimal] = defaultdict(lambda: Decimal("0"))
            accounts: set[str] = set()

            if not price_map and positions_state:
                for account_id, instrument_id in positions_state:
                    gap_recorder.record_gap(
                        "price",
                        target_ts,
                        instrument_id=instrument_id,
                        account_id=account_id,
                        details={"reason": "missing_price_snapshot"},
                    )
                logger.warning("Missing price data for snapshot timestamp %s", target_ts)
                price_map = {}

            for (account_id, instrument_id), state in positions_state.items():
                shares: Decimal = state["shares"]
                if shares == 0:
                    accounts.add(account_id)
                    continue

                price = price_map.get(instrument_id)
                if not price:
                    if instrument_id not in missing_price_logged:
                        meta = instrument_meta.get(instrument_id, {})
                        symbol = meta.get("yfinance_symbol") or meta.get("symbol") or "?"
                        logger.warning(
                            "No price for instrument %s (%s) at %s",
                            instrument_id,
                            symbol,
                            target_ts,
                        )
                        missing_price_logged.add(instrument_id)
                    gap_recorder.record_gap(
                        "price",
                        target_ts,
                        instrument_id=instrument_id,
                        account_id=account_id,
                        details={"reason": "missing_price_snapshot"},
                    )
                    continue

                gap_recorder.clear_gap(
                    "price",
                    target_ts,
                    instrument_id=instrument_id,
                    account_id=account_id,
                )

                fx_rate = fx_resolver.rate(price.currency, target_ts)
                if fx_rate is None:
                    logger.warning(
                        "Missing FX rate %s/%s for instrument %s at %s",
                        price.currency,
                        self.settings.base_currency,
                        instrument_id,
                        target_ts,
                    )
                    gap_recorder.record_gap(
                        "fx_rate",
                        target_ts,
                        instrument_id=instrument_id,
                        account_id=account_id,
                        details={
                            "from_currency": price.currency,
                            "to_currency": self.settings.base_currency,
                        },
                    )
                    continue

                gap_recorder.clear_gap(
                    "fx_rate",
                    target_ts,
                    instrument_id=instrument_id,
                    account_id=account_id,
                )

                value_ccy = shares * price.close
                value_eur = value_ccy * fx_rate
                cost_ccy = state["cost_ccy"]
                cost_eur = state["cost_eur"]

                positions.append(
                    PositionSnapshot(
                        snapshot_at=target_ts,
                        account_id=account_id,
                        instrument_id=instrument_id,
                        shares=shares,
                        cost_basis_ccy=cost_ccy,
                        cost_basis_eur=cost_eur,
                    )
                )

                positions_totals[account_id] += value_eur
                cost_totals[account_id] += cost_eur
                accounts.add(account_id)

            cash_totals = self._compute_cash_balances(
                transactions, cash_movements, fx_resolver, gap_recorder
            )
            accounts.update(cash_totals.keys())
            accounts.update(realized_totals.keys())
            accounts.update(account_id for (account_id, _) in positions_state.keys())

            db.replace_positions_snapshot(self.pool, target_ts, positions)

            if not accounts:
                logger.info("No accounts to snapshot at %s", target_ts)
                db.replace_portfolio_value_snapshot(self.pool, target_ts, [])
                db.replace_realized_pnl_fifo(self.pool, target_ts, [])
                return

            account_list = sorted(accounts)
            summaries = db.get_portfolio_history_summary(self.pool, account_list, target_ts)

            portfolio_rows: List[PortfolioValueSnapshot] = []
            for account_id in account_list:
                positions_value = positions_totals.get(account_id, Decimal("0"))
                cost_value = cost_totals.get(account_id, Decimal("0"))
                cash_value = cash_totals.get(account_id, Decimal("0"))
                nav = positions_value + cash_value
                unrealized = positions_value - cost_value
                realized_increment = realized_totals.get(account_id, Decimal("0"))

                summary = summaries.get(account_id, {})
                prev_nav = summary.get("prev_nav") or Decimal("0")
                prev_realized = summary.get("prev_realized") or Decimal("0")
                max_value = summary.get("max_value")

                realized_cumulative = prev_realized + realized_increment
                delta = nav - prev_nav

                ret_value = None
                if prev_nav not in (None, Decimal("0")):
                    ret_value = (nav - prev_nav) / prev_nav

                peak_candidate = nav
                if max_value not in (None, Decimal("0")) and max_value > nav:
                    peak_candidate = max_value
                peak = peak_candidate if peak_candidate != 0 else nav
                drawdown = None
                if peak not in (None, Decimal("0")):
                    drawdown = (nav - peak) / peak

                portfolio_rows.append(
                    PortfolioValueSnapshot(
                        snapshot_at=target_ts,
                        account_id=account_id,
                        positions_value_eur=positions_value,
                        cash_value_eur=cash_value,
                        nav_eur=nav,
                        unrealized_pnl_eur=unrealized,
                        realized_pnl_eur=realized_cumulative,
                        delta_eur=delta,
                        ret=ret_value,
                        drawdown=drawdown,
                    )
                )

            db.replace_portfolio_value_snapshot(self.pool, target_ts, portfolio_rows)
            db.replace_realized_pnl_fifo(self.pool, target_ts, realized_entries)

            logger.info(
                "Snapshot recompute completed for %s (%d positions, %d accounts, %d realized lots)",
                target_ts,
                len(positions),
                len(portfolio_rows),
                len(realized_entries),
            )
        finally:
            gap_recorder.flush(self.pool)

    def _build_positions_and_realized(
        self,
        transactions: Iterable[Dict[str, Any]],
        fx_resolver: FxResolver,
        close_snapshot_at: datetime,
        gap_recorder: DataGapRecorder,
    ) -> Tuple[
        Dict[Tuple[str, int], Dict[str, Any]],
        List[RealizedPnlLot],
        DefaultDict[str, Decimal],
    ]:
        state: Dict[Tuple[str, int], Dict[str, Any]] = {}
        insufficient_lot_logged: set[Tuple[str, int]] = set()
        short_position_logged: set[Tuple[str, int]] = set()
        realized_entries: List[RealizedPnlLot] = []
        realized_totals: DefaultDict[str, Decimal] = defaultdict(lambda: Decimal("0"))

        for row in transactions:
            qty = Decimal(row["qty"])
            if qty == 0:
                continue

            account_id = row["account_id"]
            instrument_id = row["instrument_id"]
            trade_dt: datetime = row["date_time_utc"]
            currency = row.get("currency") or self.settings.base_currency
            price = Decimal(row["price"])

            key = (account_id, instrument_id)
            entry = state.setdefault(
                key,
                {
                    "shares": Decimal("0"),
                    "cost_ccy": Decimal("0"),
                    "cost_eur": Decimal("0"),
                    "currency": currency,
                    "lots": [],
                },
            )
            entry["currency"] = currency
            lots: List[Dict[str, Any]] = entry["lots"]

            if qty > 0:
                cost_ccy = qty * price
                cost_eur = fx_resolver.convert(cost_ccy, currency, trade_dt)
                if cost_eur is None:
                    logger.warning(
                        "Missing FX rate to value buy trade for instrument %s (%s) at %s",
                        instrument_id,
                        currency,
                        trade_dt,
                    )
                    gap_recorder.record_gap(
                        "fx_rate",
                        trade_dt,
                        instrument_id=instrument_id,
                        account_id=account_id,
                        details={
                            "from_currency": currency,
                            "to_currency": fx_resolver.base_currency,
                        },
                    )
                    continue
                gap_recorder.clear_gap(
                    "fx_rate",
                    trade_dt,
                    instrument_id=instrument_id,
                    account_id=account_id,
                )

                lots.append(
                    {
                        "qty": qty,
                        "cost_ccy": cost_ccy,
                        "cost_eur": cost_eur,
                        "opened_at": trade_dt,
                    }
                )
                entry["shares"] += qty
                entry["cost_ccy"] += cost_ccy
                entry["cost_eur"] += cost_eur
                continue

            # Sell path ----------------------------------------------------
            sell_qty = abs(qty)
            if sell_qty == 0:
                continue

            proceeds_ccy_total = sell_qty * price
            proceeds_eur_total = fx_resolver.convert(proceeds_ccy_total, currency, trade_dt)
            if proceeds_eur_total is None:
                logger.warning(
                    "Missing FX rate to value sell trade for instrument %s (%s) at %s",
                    instrument_id,
                    currency,
                    trade_dt,
                )
                gap_recorder.record_gap(
                    "fx_rate",
                    trade_dt,
                    instrument_id=instrument_id,
                    account_id=account_id,
                    details={
                        "from_currency": currency,
                        "to_currency": fx_resolver.base_currency,
                    },
                )
                continue
            gap_recorder.clear_gap(
                "fx_rate",
                trade_dt,
                instrument_id=instrument_id,
                account_id=account_id,
            )

            remaining = sell_qty
            while remaining > 0 and lots:
                lot = lots[0]
                lot_qty: Decimal = lot["qty"]
                match_qty = lot_qty if lot_qty <= remaining else remaining

                cost_ccy_unit = lot["cost_ccy"] / lot_qty
                cost_eur_unit = lot["cost_eur"] / lot_qty
                cost_ccy = cost_ccy_unit * match_qty
                cost_eur = cost_eur_unit * match_qty

                proportion = match_qty / sell_qty
                proceeds_ccy = proceeds_ccy_total * proportion
                proceeds_eur = proceeds_eur_total * proportion

                realized_entries.append(
                    RealizedPnlLot(
                        account_id=account_id,
                        instrument_id=instrument_id,
                        lot_opened_at=lot["opened_at"],
                        lot_closed_at=trade_dt,
                        close_snapshot_at=close_snapshot_at,
                        qty_closed=match_qty,
                        proceeds_ccy=proceeds_ccy,
                        proceeds_eur=proceeds_eur,
                        cost_ccy=cost_ccy,
                        cost_eur=cost_eur,
                        pnl_ccy=proceeds_ccy - cost_ccy,
                        pnl_eur=proceeds_eur - cost_eur,
                    )
                )
                realized_totals[account_id] += proceeds_eur - cost_eur

                lot["qty"] -= match_qty
                lot["cost_ccy"] -= cost_ccy
                lot["cost_eur"] -= cost_eur
                remaining -= match_qty

                if lot["qty"] <= 0:
                    lots.pop(0)
                else:
                    lots[0] = lot

                entry["cost_ccy"] -= cost_ccy
                entry["cost_eur"] -= cost_eur

            if remaining > 0:
                key = (account_id, instrument_id)
                if key not in insufficient_lot_logged:
                    logger.warning(
                        "Sell quantity %s for instrument %s on account %s exceeded available lots",
                        sell_qty,
                        instrument_id,
                        account_id,
                    )
                    insufficient_lot_logged.add(key)
                gap_recorder.record_gap(
                    "position_lot_shortage",
                    trade_dt,
                    instrument_id=instrument_id,
                    account_id=account_id,
                    details={"qty_missing": str(remaining)},
                )
            else:
                gap_recorder.clear_gap(
                    "position_lot_shortage",
                    trade_dt,
                    instrument_id=instrument_id,
                    account_id=account_id,
                )

            entry["shares"] += qty  # qty is negative here
            if entry["shares"] < 0:
                key = (account_id, instrument_id)
                if key not in short_position_logged:
                    logger.warning(
                        "Account %s instrument %s has short position %s shares",
                        account_id,
                        instrument_id,
                        entry["shares"],
                    )
                    short_position_logged.add(key)
                gap_recorder.record_gap(
                    "position_short",
                    trade_dt,
                    instrument_id=instrument_id,
                    account_id=account_id,
                    details={"shares": str(entry["shares"])},
                )
            else:
                gap_recorder.clear_gap(
                    "position_short",
                    trade_dt,
                    instrument_id=instrument_id,
                    account_id=account_id,
                )

        return state, realized_entries, realized_totals

    def _compute_cash_balances(
        self,
        transactions: Iterable[Dict[str, Any]],
        cash_movements: Iterable[Dict[str, Any]],
        fx_resolver: FxResolver,
        gap_recorder: DataGapRecorder,
    ) -> DefaultDict[str, Decimal]:
        totals: DefaultDict[str, Decimal] = defaultdict(lambda: Decimal("0"))

        for row in transactions:
            net_amount = Decimal(row["net_amount"])
            if net_amount == 0:
                continue
            converted = fx_resolver.convert(net_amount, row.get("currency"), row["date_time_utc"])
            if converted is None:
                logger.warning(
                    "Missing FX rate for transaction cash impact (%s %s on %s)",
                    net_amount,
                    row.get("currency"),
                    row["date_time_utc"],
                )
                gap_recorder.record_gap(
                    "fx_rate",
                    row["date_time_utc"],
                    instrument_id=row.get("instrument_id"),
                    account_id=row["account_id"],
                    details={
                        "from_currency": row.get("currency"),
                        "to_currency": fx_resolver.base_currency,
                        "context": "transaction_cash_flow",
                    },
                )
                continue
            gap_recorder.clear_gap(
                "fx_rate",
                row["date_time_utc"],
                instrument_id=row.get("instrument_id"),
                account_id=row["account_id"],
            )
            totals[row["account_id"]] += converted

        for row in cash_movements:
            amount = Decimal(row["amount"])
            if amount == 0:
                continue
            converted = fx_resolver.convert(amount, row.get("currency"), row["date_time_utc"])
            if converted is None:
                logger.warning(
                    "Missing FX rate for cash movement (%s %s on %s)",
                    amount,
                    row.get("currency"),
                    row["date_time_utc"],
                )
                gap_recorder.record_gap(
                    "fx_rate",
                    row["date_time_utc"],
                    instrument_id=None,
                    account_id=row["account_id"],
                    details={
                        "from_currency": row.get("currency"),
                        "to_currency": fx_resolver.base_currency,
                        "context": "cash_movement",
                    },
                )
                continue
            gap_recorder.clear_gap(
                "fx_rate",
                row["date_time_utc"],
                instrument_id=None,
                account_id=row["account_id"],
            )
            totals[row["account_id"]] += converted

        return totals
