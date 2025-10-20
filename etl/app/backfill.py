from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import yfinance as yf

from . import db
from .config import SnapshotSettings
from .models import FxRate, HourlyPrice, Price
from .utils import clear_yfinance_cache

logger = logging.getLogger(__name__)



class BackfillService:
    def __init__(self, snapshot_settings: SnapshotSettings, pool):
        self.snapshot_settings = snapshot_settings
        self.pool = pool

    def run(
        self,
        *,
        days: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        include_prices: bool = True,
        include_fx: bool = True,
        include_snapshots: bool = False,
    ) -> None:
        if not include_prices and not include_fx:
            include_prices = include_fx = True

        clear_yfinance_cache()

        end_utc = (
            end.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            if end
            else datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        )
        if start is not None:
            start_utc = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        elif days is not None:
            start_utc = (end_utc - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        else:
            raise ValueError("Either days or start must be provided for backfill")

        if start_utc > end_utc:
            start_utc, end_utc = end_utc, start_utc

        if include_prices:
            self._backfill_prices(start_utc, end_utc)
            self._backfill_prices_hourly(start_utc, end_utc)
        if include_fx:
            self._backfill_fx(start_utc, end_utc)
        if include_snapshots:
            self._backfill_snapshots(start_utc, end_utc)

    def _backfill_prices(self, start: datetime, end: datetime) -> None:
        targets = db.get_all_price_targets(self.pool)
        if not targets:
            logger.info("No instruments with tickers available for price backfill")
            return

        batch: list[Price] = []
        cleared_instruments: set[int] = set()

        for target in targets:
            ticker = target.ticker
            try:
                history = yf.Ticker(ticker).history(
                    start=start.date(),
                    end=end.date(),
                    interval="1d",
                    auto_adjust=False,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("yfinance history error for %s: %s", ticker, exc)
                continue

            if history.empty:
                logger.warning("No historical prices returned for %s", ticker)
                continue

            history = history.dropna(subset=["Close"])
            for ts, row in history.iterrows():
                ts = _to_utc_datetime(ts)
                try:
                    close = Decimal(str(row["Close"]))
                except Exception:  # pylint: disable=broad-except
                    continue
                batch.append(
                    Price(
                        instrument_id=target.instrument_id,
                        as_of_utc=ts,
                        close=close,
                        currency=target.currency,
                        source="yfinance",
                    )
                )

            if batch:
                db.upsert_prices(self.pool, batch)
                logger.info("Backfilled %s price points for %s", len(batch), ticker)
                cleared_instruments.add(target.instrument_id)
                batch.clear()

        for instrument_id in cleared_instruments:
            db.delete_data_gap(self.pool, "price", instrument_id=instrument_id)

    def _backfill_prices_hourly(self, start: datetime, end: datetime) -> None:
        targets = db.get_all_price_targets(self.pool)
        if not targets:
            return

        hourly_start = max(start, end - timedelta(days=30))
        batch: list[HourlyPrice] = []
        cleared_instruments: set[int] = set()

        for target in targets:
            ticker = target.ticker
            try:
                history = yf.Ticker(ticker).history(
                    start=hourly_start.replace(tzinfo=None),
                    end=end.replace(tzinfo=None),
                    interval="60m",
                    auto_adjust=False,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("yfinance hourly history error for %s: %s", ticker, exc)
                continue

            if history.empty:
                continue

            history = history.dropna(subset=["Close"])
            if history.empty:
                continue

            currency = history.attrs.get("currency", None) or target.currency
            for ts, row in history.iterrows():
                dt = _to_utc_datetime(ts)
                try:
                    close = Decimal(str(row["Close"]))
                except Exception:  # pylint: disable=broad-except
                    continue
                batch.append(
                    HourlyPrice(
                        instrument_id=target.instrument_id,
                        as_of_utc=dt,
                        close=close,
                        currency=currency,
                        source="yfinance",
                    )
                )

            if batch:
                db.upsert_prices_hourly(self.pool, batch)
                logger.info(
                    "Backfilled %s hourly price points for %s", len(batch), ticker
                )
                cleared_instruments.add(target.instrument_id)
                batch.clear()

        for instrument_id in cleared_instruments:
            db.delete_data_gap(self.pool, "price", instrument_id=instrument_id)

    def _backfill_fx(self, start: datetime, end: datetime) -> None:
        base = self.snapshot_settings.base_currency
        currencies = [
            ccy for ccy in db.list_instrument_currencies(self.pool) if ccy and ccy != base
        ]
        if not currencies:
            logger.info("No non-base currencies found; skipping FX backfill")
            return

        cleared_pairs: set[str] = set()

        for currency in currencies:
            rate_rows: list[FxRate] = []
            pair = f"{base}{currency}=X"
            invert = True
            history = _fetch_history(pair, start, end)
            if history.empty:
                pair = f"{currency}{base}=X"
                invert = False
                history = _fetch_history(pair, start, end)

            if history.empty:
                logger.warning("No FX data for %s/%s", currency, base)
                continue

            history = history.dropna(subset=["Close"])
            for ts, row in history.iterrows():
                dt = _to_utc_datetime(ts).date()
                price = Decimal(str(row["Close"]))
                if invert:
                    if price == 0:
                        continue
                    rate = Decimal("1") / price
                else:
                    rate = price
                rate_rows.append(
                    FxRate(
                        date_utc=dt,
                        from_ccy=currency,
                        to_ccy=base,
                        rate=rate,
                        source="yfinance",
                    )
                )

            if rate_rows:
                db.upsert_fx_rates(self.pool, rate_rows)
                logger.info("Backfilled %s FX points for %s/%s", len(rate_rows), currency, base)
                cleared_pairs.add(f"{currency}->{base}")

        for pair in cleared_pairs:
            db.delete_data_gap(
                self.pool,
                "fx_rate",
                instrument_id=None,
                account_id=pair,
            )

    def _backfill_snapshots(self, start_ts: datetime, end_ts: datetime) -> None:
        from .snapshots import SnapshotRecalculator  # local import

        snapshotper = SnapshotRecalculator(self.snapshot_settings, self.pool)
        current = start_ts
        while current <= end_ts:
            snapshotper.run(current)
            current += timedelta(hours=1)


def _fetch_history(ticker: str, start: datetime, end: datetime):
    try:
        return yf.Ticker(ticker).history(
            start=start.date(),
            end=end.date(),
            interval="1d",
            auto_adjust=False,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("yfinance history error for %s: %s", ticker, exc)
        return yf.Ticker(ticker).history(period="1y", interval="1d", auto_adjust=False)


def _to_utc_datetime(timestamp) -> datetime:
    if hasattr(timestamp, "to_pydatetime"):
        dt = timestamp.to_pydatetime()
    else:
        dt = datetime.fromisoformat(str(timestamp))
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)
