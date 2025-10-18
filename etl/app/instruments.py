from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import yfinance as yf

from . import db
from .models import InstrumentMetadataTarget
from .utils import clear_yfinance_cache

logger = logging.getLogger(__name__)


class InstrumentMetadataUpdater:
    def __init__(self, settings, pool) -> None:
        self.settings = settings
        self.pool = pool

    def run(self, include_all: bool = False) -> None:
        if not getattr(self.settings, "source", None):
            logger.warning("Instrument metadata update skipped: no data source configured")
            return

        if self.settings.source != "yfinance":
            logger.warning(
                "Instrument metadata update skipped: unsupported source %s",
                self.settings.source,
            )
            return

        targets = db.list_instrument_metadata_targets(self.pool, include_all=include_all)
        if not targets:
            logger.info("No instruments require metadata refresh")
            return

        clear_yfinance_cache()

        updated = 0
        failed = 0

        for target in targets:
            metadata = self._fetch_from_yfinance(target)
            if metadata:
                try:
                    db.update_instrument_metadata(
                        self.pool, target.instrument_id, metadata
                    )
                    updated += 1
                except Exception:  # pylint: disable=broad-except
                    failed += 1
                    logger.exception(
                        "Failed to persist metadata for instrument %s (%s)",
                        target.instrument_id,
                        target.ticker,
                    )
            else:
                failed += 1

            sleep_seconds = getattr(self.settings, "sleep_seconds", 0.0)
            if sleep_seconds:
                time.sleep(sleep_seconds)

        logger.info(
            "Instrument metadata refresh completed (%d updated, %d skipped, total=%d)",
            updated,
            failed,
            len(targets),
        )

    def _fetch_from_yfinance(
        self, target: InstrumentMetadataTarget
    ) -> Optional[Dict[str, object]]:
        try:
            ticker = yf.Ticker(target.ticker)
            info = ticker.get_info()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "yfinance metadata fetch failed for %s: %s", target.ticker, exc
            )
            return None

        if not isinstance(info, dict) or not info:
            logger.warning("yfinance returned empty metadata for %s", target.ticker)
            return None

        metadata: Dict[str, object] = {}

        def clean(value: object) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                trimmed = value.strip()
                if not trimmed or trimmed.upper() in {"NA", "N/A", "NONE", "NULL"}:
                    return None
                return trimmed
            return str(value)

        name = clean(info.get("longName") or info.get("shortName"))
        if name:
            metadata["name"] = name

        currency = clean(
            info.get("currency")
            or info.get("financialCurrency")
            or info.get("quoteCurrency")
        )
        if not currency:
            try:
                fast_info = ticker.fast_info  # type: ignore[attr-defined]
                if hasattr(fast_info, "get"):
                    currency = clean(fast_info.get("currency"))
                else:
                    currency = clean(getattr(fast_info, "currency", None))
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug(
                    "Ignored exception while fetching fast_info for %s: %s",
                    target.ticker,
                    exc,
                )
        if currency:
            metadata["currency"] = currency.upper()

        sector = clean(info.get("sector"))
        if sector:
            metadata["sector"] = sector

        country = clean(info.get("country"))
        if country:
            metadata["country"] = country

        region = clean(info.get("region"))
        if region:
            metadata["region"] = region

        exchange = clean(info.get("fullExchangeName") or info.get("exchange"))
        if exchange:
            metadata["primary_exchange"] = exchange

        quote_type = clean(info.get("quoteType"))
        if quote_type:
            metadata["asset_class"] = quote_type.upper()

        if not metadata:
            logger.info("No useful metadata found for %s", target.ticker)
            return None

        return metadata
