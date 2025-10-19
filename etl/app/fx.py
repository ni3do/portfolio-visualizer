from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Tuple

import yfinance as yf

from . import db
from .models import DataGap, FxRate
from .utils import clear_yfinance_cache

logger = logging.getLogger(__name__)


class FxUpdater:
    def __init__(self, settings, pool):
        self.settings = settings
        self.pool = pool

    def run(self) -> None:
        clear_yfinance_cache()

        base = self.settings.base_currency
        currencies = [
            ccy
            for ccy in db.list_instrument_currencies(self.pool)
            if ccy and ccy != base
        ]

        if not currencies:
            logger.info("No non-base currencies found for FX update")
            return

        fx_rates: list[FxRate] = []
        missing: list[str] = []

        for currency in currencies:
            rate = self._fetch_rate(currency, base)
            if rate:
                fx_rates.append(rate)
            else:
                missing.append(currency)
            time.sleep(1)

        if fx_rates:
            db.upsert_fx_rates(self.pool, fx_rates)
            logger.info(
                "FX update stored %d rates (errors=%d)", len(fx_rates), len(missing)
            )
        else:
            logger.warning(
                "No FX rates captured from yfinance for %d currencies", len(currencies)
            )

        if missing:
            logger.warning("Failed to fetch FX rates for %d currencies", len(missing))

        gap_timestamp = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        if missing:
            gaps = [
                DataGap(
                    gap_type="fx_rate",
                    target_timestamp=gap_timestamp,
                    instrument_id=None,
                    account_id=f"{ccy}->{base}",
                    details={"from_currency": ccy, "to_currency": base},
                    detected_at=gap_timestamp,
                )
                for ccy in missing
            ]
            db.upsert_data_gaps(self.pool, gaps)

        if fx_rates:
            succeeded = {rate.from_ccy for rate in fx_rates}
            for currency in succeeded:
                db.delete_data_gap(
                    self.pool,
                    "fx_rate",
                    instrument_id=None,
                    account_id=f"{currency}->{base}",
                )

    def _fetch_rate(self, currency: str, base: str) -> FxRate | None:
        history, invert = self._fetch_history_pair(base, currency)
        if history is None or history.empty:
            logger.warning("No FX data available for %s/%s", currency, base)
            return None

        history = history.dropna(subset=["Close"])
        if history.empty:
            logger.warning("yfinance FX history missing Close values for %s/%s", currency, base)
            return None

        last_row = history.tail(1).iloc[0]
        index = history.tail(1).index[0]

        try:
            price = Decimal(str(last_row["Close"]))
        except (InvalidOperation, KeyError):
            logger.warning("Invalid FX price for %s/%s", currency, base)
            return None

        if invert:
            if price == 0:
                logger.warning("Cannot invert zero FX price for %s/%s", currency, base)
                return None
            rate = Decimal("1") / price
        else:
            rate = price

        date_utc = self._to_date(index)

        return FxRate(
            date_utc=date_utc,
            from_ccy=currency,
            to_ccy=base,
            rate=rate,
            source=self.settings.source,
        )

    def _fetch_history_pair(self, base: str, currency: str) -> Tuple[Any | None, bool]:
        primary = f"{base}{currency}=X"
        history = self._fetch_history(primary)
        if history is not None and not history.empty:
            return history, True

        secondary = f"{currency}{base}=X"
        history = self._fetch_history(secondary)
        if history is not None and not history.empty:
            return history, False

        return None, False

    def _fetch_history(self, ticker: str):
        try:
            return yf.Ticker(ticker).history(
                period=self.settings.history_period,
                interval=self.settings.history_interval,
                auto_adjust=False,
            )
        except Exception as exc:  # pylint: disable=broad-except
            message = str(exc).lower()
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429 or "too many requests" in message:
                logger.warning(
                    "yfinance rate limited for %s, skipping FX fetch", ticker
                )
                return None
            logger.warning("yfinance FX history error for %s: %s", ticker, exc)
            return None

    @staticmethod
    def _to_date(timestamp) -> date:
        if hasattr(timestamp, "to_pydatetime"):
            dt = timestamp.to_pydatetime()
        else:
            dt = datetime.fromisoformat(str(timestamp))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date()
