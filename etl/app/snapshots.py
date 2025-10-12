from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, Tuple

from zoneinfo import ZoneInfo

from . import db
from .models import PortfolioValueSnapshot, PositionSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotSettings:
    base_currency: str
    timezone: str


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
        if not transactions:
            logger.info("No transactions found up to %s; skipping snapshot", cutoff_utc)
            return

        positions_state = self._build_positions(transactions)
        if not positions_state:
            logger.info("No open positions to snapshot at %s", target_ts)
            return

        instrument_ids = {instrument_id for _, instrument_id in positions_state.keys()}
        price_map = db.get_latest_prices(self.pool, list(instrument_ids), cutoff_utc)

        if not price_map:
            logger.warning("Missing price data for snapshot timestamp %s", target_ts)
            return

        fx_rates = db.get_fx_rates_for_date(self.pool, target_ts.date())
        base_ccy = self.settings.base_currency

        positions: list[PositionSnapshot] = []
        portfolio_totals: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

        for (account_id, instrument_id), state in positions_state.items():
            shares = state["shares"]
            if shares == 0:
                continue

            price = price_map.get(instrument_id)
            if not price:
                logger.warning("No price for instrument %s at %s", instrument_id, target_ts)
                continue

            value_ccy = shares * price.close
            cost_basis_ccy = state["cost_ccy"]
            instrument_ccy = price.currency
            fx_rate = self._resolve_fx_rate(fx_rates, instrument_ccy, base_ccy, target_ts)
            if fx_rate is None:
                logger.warning(
                    "Missing FX rate %s/%s for %s", instrument_ccy, base_ccy, target_ts
                )
                continue

            value_base = value_ccy * fx_rate
            cost_base = cost_basis_ccy * fx_rate

            positions.append(
                PositionSnapshot(
                    snapshot_at=target_ts,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    shares=shares,
                    cost_basis_ccy=cost_basis_ccy,
                    cost_basis_eur=cost_base,
                )
            )

            portfolio_totals[account_id] += value_base

        if not positions:
            logger.info("No valued positions after price/FX filters for %s", target_ts)
            return

        db.replace_positions_snapshot(self.pool, positions)

        summaries = db.get_portfolio_history_summary(
            self.pool, list(portfolio_totals.keys()), target_ts
        )

        portfolio_rows: list[PortfolioValueSnapshot] = []
        for account_id, value in portfolio_totals.items():
            summary = summaries.get(account_id, {})
            prev_value = summary.get("prev_value")
            max_value = summary.get("max_value")

            ret_value = None
            if prev_value not in (None, Decimal("0")):
                ret_value = (value - prev_value) / prev_value

            peak_candidate = max_value if max_value and max_value > value else value
            peak = peak_candidate if peak_candidate != 0 else value
            drawdown = None
            if peak not in (None, Decimal("0")):
                drawdown = (value - peak) / peak

            portfolio_rows.append(
                PortfolioValueSnapshot(
                    snapshot_at=target_ts,
                    account_id=account_id,
                    value_eur=value,
                    ret=ret_value,
                    drawdown=drawdown,
                )
            )

        db.replace_portfolio_value_snapshot(self.pool, portfolio_rows)
        logger.info(
            "Snapshot recompute completed for %s (%d positions, %d accounts)",
            target_ts,
            len(positions),
            len(portfolio_rows),
        )

    def _build_positions(self, transactions: Iterable[Dict[str, object]]) -> Dict[Tuple[str, int], Dict[str, Decimal]]:
        state: Dict[Tuple[str, int], Dict[str, Decimal]] = {}

        for row in transactions:
            account_id = row["account_id"]
            instrument_id = row["instrument_id"]
            qty = Decimal(row["qty"])
            net_amount = Decimal(row["net_amount"])

            key = (account_id, instrument_id)
            entry = state.setdefault(
                key,
                {
                    "shares": Decimal("0"),
                    "cost_ccy": Decimal("0"),
                },
            )

            shares = entry["shares"]
            cost_ccy = entry["cost_ccy"]

            if qty >= 0:
                cost_increment = -net_amount if net_amount < 0 else net_amount
                entry["shares"] = shares + qty
                entry["cost_ccy"] = cost_ccy + cost_increment
            else:
                sell_qty = abs(qty)
                if shares > 0:
                    avg_cost = cost_ccy / shares if shares != 0 else Decimal("0")
                    entry["cost_ccy"] = max(Decimal("0"), cost_ccy - (avg_cost * sell_qty))
                entry["shares"] = shares + qty
                if entry["shares"] <= 0:
                    entry["cost_ccy"] = Decimal("0")

        return state

    def _resolve_fx_rate(
        self,
        fx_rates: Dict[tuple[str, str], object],
        from_ccy: str,
        to_ccy: str,
        target_ts: datetime,
    ) -> Decimal | None:
        if from_ccy == to_ccy:
            return Decimal("1")

        direct = fx_rates.get((from_ccy, to_ccy))
        if direct:
            return direct.rate

        inverse = fx_rates.get((to_ccy, from_ccy))
        if inverse and inverse.rate != 0:
            return Decimal("1") / inverse.rate

        if from_ccy == self.settings.base_currency:
            return Decimal("1")

        logger.debug(
            "FX rate %s/%s missing for %s", from_ccy, to_ccy, target_ts
        )
        return None
