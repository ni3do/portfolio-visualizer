from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Sequence

import yfinance as yf

from . import db
from .models import DataGap, HourlyPrice, Price, PriceTarget
from .utils import clear_yfinance_cache

logger = logging.getLogger(__name__)


def chunked(seq: Sequence[PriceTarget], size: int) -> Iterable[Sequence[PriceTarget]]:
    for idx in range(0, len(seq), size):
        yield seq[idx : idx + size]


class PriceUpdater:
    def __init__(self, settings, pool):
        self.settings = settings
        self.pool = pool

    def run(self) -> None:
        clear_yfinance_cache()

        targets = db.get_price_targets(self.pool)
        if not targets:
            logger.info("No instruments with open holdings found for price update")
            return

        prices: List[Price] = []
        failed_targets: List[PriceTarget] = []
        hourly_prices: List[HourlyPrice] = []
        hourly_failures: List[PriceTarget] = []

        for batch in chunked(targets, self.settings.batch_size):
            (
                batch_prices,
                batch_failures,
                batch_hourly,
                batch_hourly_failures,
            ) = self._fetch_batch(list(batch))
            prices.extend(batch_prices)
            failed_targets.extend(batch_failures)
            hourly_prices.extend(batch_hourly)
            hourly_failures.extend(batch_hourly_failures)

        if prices:
            db.upsert_prices(self.pool, prices)
            logger.info(
                "Price update stored %d records (errors=%d)", len(prices), len(failed_targets)
            )
        else:
            logger.warning(
                "No prices captured from yfinance for %d targets", len(targets)
            )

        if hourly_prices:
            db.upsert_prices_hourly(self.pool, hourly_prices)
            logger.info(
                "Hourly price update stored %d records (errors=%d)",
                len(hourly_prices),
                len(hourly_failures),
            )
        elif targets:
            logger.warning("No hourly prices captured for any target instruments")

        if failed_targets:
            logger.warning("Failed to fetch prices for %d instruments", len(failed_targets))
        if hourly_failures:
            logger.warning(
                "Failed to fetch hourly prices for %d instruments", len(hourly_failures)
            )

        gap_timestamp = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        if failed_targets:
            gaps = [
                DataGap(
                    gap_type="price",
                    target_timestamp=gap_timestamp,
                    instrument_id=target.instrument_id,
                    account_id=None,
                    details={"ticker": target.ticker},
                    detected_at=gap_timestamp,
                )
                for target in failed_targets
            ]
            db.upsert_data_gaps(self.pool, gaps)

        hourly_failure_ids = {target.instrument_id for target in hourly_failures}
        if hourly_failures:
            gaps = [
                DataGap(
                    gap_type="price",
                    target_timestamp=gap_timestamp,
                    instrument_id=target.instrument_id,
                    account_id=None,
                    details={"ticker": target.ticker, "resolution": "60m"},
                    detected_at=gap_timestamp,
                )
                for target in hourly_failures
            ]
            db.upsert_data_gaps(self.pool, gaps)

        if prices:
            succeeded_instruments = {price.instrument_id for price in prices}
            for instrument_id in succeeded_instruments:
                if instrument_id in hourly_failure_ids:
                    continue
                db.delete_data_gap(
                    self.pool,
                    "price",
                    instrument_id=instrument_id,
                )

        if hourly_prices:
            succeeded_hourly = {price.instrument_id for price in hourly_prices}
            for instrument_id in succeeded_hourly:
                db.delete_data_gap(
                    self.pool,
                    "price",
                    instrument_id=instrument_id,
                )

    def _fetch_batch(
        self, targets: List[PriceTarget]
    ) -> tuple[
        List[Price],
        List[PriceTarget],
        List[HourlyPrice],
        List[PriceTarget],
    ]:
        tickers = [t.ticker for t in targets]
        logger.debug("Fetching price batch: %s", tickers)
        results: List[Price] = []
        failures: List[PriceTarget] = []
        hourly_results: List[HourlyPrice] = []
        hourly_failures: List[PriceTarget] = []

        for target in targets:
            price = self._fetch_price_yfinance(target)

            if price:
                results.append(price)
                logger.debug(
                    "Price fetched: ticker=%s value=%s currency=%s as_of=%s",
                    price.source,
                    price.close,
                    price.currency,
                    price.as_of_utc,
                )
            else:
                failures.append(target)
                logger.debug("No price captured for ticker %s", target.ticker)

            hourly_prices = self._fetch_hourly_history(target)
            if hourly_prices:
                hourly_results.extend(hourly_prices)
            else:
                hourly_failures.append(target)

            time.sleep(1)

        return results, failures, hourly_results, hourly_failures

    def _fetch_price_yfinance(self, target: PriceTarget) -> Price | None:
        ticker = target.ticker

        backoff = 1
        for attempt in range(3):
            ticker_client = yf.Ticker(ticker)

            try:
                info = getattr(ticker_client, "fast_info", None)
                if info is not None:
                    price = getattr(info, "last_price", None)
                    timestamp = getattr(info, "regular_market_time", None)
                    currency = getattr(info, "currency", None)
                    if price is not None:
                        return self._build_price(
                            target, price, timestamp, currency or target.currency
                        )
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug("fast_info failed for %s: %s", ticker, exc)

            try:
                history = ticker_client.history(
                    period="5d", interval="1d", auto_adjust=False
                )
            except Exception as exc:  # pylint: disable=broad-except
                message = str(exc).lower()
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 429 or "too many requests" in message:
                    logger.warning(
                        "yfinance rate limited %s (attempt %s), backing off %ss",
                        ticker,
                        attempt + 1,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue

                logger.warning("yfinance history error for %s: %s", ticker, exc)
                return None

            if history.empty:
                logger.warning("yfinance history empty for %s", ticker)
                return None

            history = history.dropna(subset=["Close"])
            if history.empty:
                logger.warning("yfinance history missing Close values for %s", ticker)
                return None

            last = history.tail(1).iloc[0]
            price = last["Close"]
            index = history.tail(1).index[0]
            if hasattr(index, "to_pydatetime"):
                dt = index.to_pydatetime()
                if dt.tzinfo:
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = datetime.now(timezone.utc)

            currency = history.attrs.get("currency", None) or target.currency
            return self._build_price(target, price, dt, currency)

        logger.warning("yfinance failed for %s after retries", ticker)
        return None

    def _fetch_hourly_history(self, target: PriceTarget) -> List[HourlyPrice]:
        ticker = target.ticker
        ticker_client = yf.Ticker(ticker)

        try:
            history = ticker_client.history(
                period="30d",
                interval="60m",
                auto_adjust=False,
            )
        except Exception as exc:  # pylint: disable=broad-except
            message = str(exc).lower()
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429 or "too many requests" in message:
                logger.warning(
                    "yfinance rate limited hourly fetch for %s, skipping", ticker
                )
            else:
                logger.debug("yfinance hourly history error for %s: %s", ticker, exc)
            return []

        if history.empty:
            logger.debug("Hourly history empty for %s", ticker)
            return []

        history = history.dropna(subset=["Close"])
        if history.empty:
            logger.debug("Hourly history missing Close values for %s", ticker)
            return []

        currency = history.attrs.get("currency", None) or target.currency
        results: List[HourlyPrice] = []
        for ts, row in history.iterrows():
            dt = self._normalize_timestamp(ts)
            try:
                close = Decimal(str(row["Close"]))
            except (InvalidOperation, KeyError):
                continue
            results.append(
                HourlyPrice(
                    instrument_id=target.instrument_id,
                    as_of_utc=dt,
                    close=close,
                    currency=currency,
                    source=self.settings.source,
                )
            )

        return results

    def _build_price(
        self, target: PriceTarget, price: float, timestamp, currency: str
    ) -> Price | None:
        try:
            close = Decimal(str(price))
        except InvalidOperation:
            logger.warning("Invalid price value %s for ticker %s", price, target.ticker)
            return None

        if isinstance(timestamp, datetime):
            as_of = (
                timestamp
                if timestamp.tzinfo
                else timestamp.replace(tzinfo=timezone.utc)
            )
        elif timestamp:
            as_of = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        else:
            as_of = datetime.now(timezone.utc)

        return Price(
            instrument_id=target.instrument_id,
            as_of_utc=as_of,
            close=close,
            currency=currency,
            source=self.settings.source,
        )

    @staticmethod
    def _normalize_timestamp(value) -> datetime:
        if hasattr(value, "to_pydatetime"):
            dt = value.to_pydatetime()
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=timezone.utc)
