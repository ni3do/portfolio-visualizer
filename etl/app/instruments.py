from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import yfinance as yf

from . import db
from .models import DataGap, InstrumentMetadataTarget
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
        gaps: list[DataGap] = []
        cleared_ids: set[int] = set()

        for target in targets:
            gap_timestamp = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            metadata, exposures = self._fetch_from_yfinance(target)
            metadata_saved = False
            exposures_saved = exposures is None

            if metadata:
                try:
                    db.update_instrument_metadata(
                        self.pool, target.instrument_id, metadata
                    )
                    updated += 1
                    metadata_saved = True
                except Exception:  # pylint: disable=broad-except
                    failed += 1
                    logger.exception(
                        "Failed to persist metadata for instrument %s (%s)",
                        target.instrument_id,
                        target.ticker,
                    )
                    gaps.append(
                        DataGap(
                            gap_type="instrument_metadata",
                            target_timestamp=gap_timestamp,
                            instrument_id=target.instrument_id,
                            account_id=None,
                            details={"ticker": target.ticker, "reason": "persist_failed"},
                            detected_at=gap_timestamp,
                        )
                    )
            else:
                failed += 1
                gaps.append(
                    DataGap(
                        gap_type="instrument_metadata",
                        target_timestamp=gap_timestamp,
                        instrument_id=target.instrument_id,
                        account_id=None,
                        details={"ticker": target.ticker, "reason": "missing_metadata"},
                        detected_at=gap_timestamp,
                    )
                )

            if exposures is not None:
                try:
                    db.replace_instrument_exposures(
                        self.pool, target.instrument_id, exposures
                    )
                    exposures_saved = True
                except Exception:  # pylint: disable=broad-except
                    failed += 1
                    logger.exception(
                        "Failed to persist exposure overrides for instrument %s (%s)",
                        target.instrument_id,
                        target.ticker,
                    )
                    gaps.append(
                        DataGap(
                            gap_type="instrument_metadata",
                            target_timestamp=gap_timestamp,
                            instrument_id=target.instrument_id,
                            account_id=None,
                            details={
                                "ticker": target.ticker,
                                "reason": "exposure_persist_failed",
                            },
                            detected_at=gap_timestamp,
                        )
                    )

            if metadata_saved and exposures_saved:
                cleared_ids.add(target.instrument_id)

            sleep_seconds = getattr(self.settings, "sleep_seconds", 0.0)
            if sleep_seconds:
                time.sleep(sleep_seconds)

        if gaps:
            db.upsert_data_gaps(self.pool, gaps)
        for instrument_id in cleared_ids:
            db.delete_data_gap(
                self.pool,
                "instrument_metadata",
                instrument_id=instrument_id,
            )

        logger.info(
            "Instrument metadata refresh completed (%d updated, %d skipped, total=%d)",
            updated,
            failed,
            len(targets),
        )

    def _fetch_from_yfinance(
        self, target: InstrumentMetadataTarget
    ) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, List[Tuple[str, float]]]]]:
        try:
            ticker = yf.Ticker(target.ticker)
            info = ticker.get_info()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "yfinance metadata fetch failed for %s: %s", target.ticker, exc
            )
            return None, None

        if not isinstance(info, dict) or not info:
            logger.warning("yfinance returned empty metadata for %s", target.ticker)
            return None, None

        metadata: Dict[str, object] = {}

        name = self._clean_string(info.get("longName") or info.get("shortName"))
        if name:
            metadata["name"] = name

        currency = self._clean_string(
            info.get("currency")
            or info.get("financialCurrency")
            or info.get("quoteCurrency")
        )
        if not currency:
            try:
                fast_info = ticker.fast_info  # type: ignore[attr-defined]
                if hasattr(fast_info, "get"):
                    currency = self._clean_string(fast_info.get("currency"))
                else:
                    currency = self._clean_string(getattr(fast_info, "currency", None))
            except Exception as exc:  # pylint: disable=broad-except
                logger.debug(
                    "Ignored exception while fetching fast_info for %s: %s",
                    target.ticker,
                    exc,
                )
        if currency:
            metadata["currency"] = currency.upper()

        sector = self._clean_string(info.get("sector"))
        if sector:
            metadata["sector"] = sector

        industry = self._clean_string(info.get("industry"))
        if industry:
            metadata["industry"] = industry

        country = self._clean_string(info.get("country"))
        if country:
            metadata["country"] = country

        region = self._clean_string(info.get("region"))
        if region:
            metadata["region"] = region

        exchange = self._clean_string(info.get("fullExchangeName") or info.get("exchange"))
        if exchange:
            metadata["primary_exchange"] = exchange

        quote_type = self._clean_string(info.get("quoteType"))
        if quote_type:
            metadata["asset_class"] = quote_type.upper()

        exposures: Optional[Dict[str, List[Tuple[str, float]]]] = {}
        if quote_type and quote_type.upper() == "ETF":
            exposures = self._extract_etf_exposures(ticker, info, target.ticker)
        else:
            exposures = {}

        if not metadata:
            logger.info("No useful metadata found for %s", target.ticker)
            return None, None

        return metadata, exposures

    @staticmethod
    def _clean_string(value: object) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            trimmed = value.strip()
            if not trimmed:
                return None
            if trimmed.upper() in {"NA", "N/A", "NONE", "NULL"}:
                return None
            return trimmed
        text = str(value).strip()
        return text or None

    @staticmethod
    def _safe_float(value: object) -> Optional[float]:
        if value is None:
            return None
        candidate = value
        if isinstance(candidate, str):
            candidate = candidate.replace("%", "").strip()
            if not candidate:
                return None
        try:
            number = float(candidate)
        except (TypeError, ValueError):
            return None
        if math.isnan(number) or math.isinf(number):
            return None
        return number

    def _normalize_weightings(self, raw: object) -> List[Tuple[str, float]]:
        pairs: List[Tuple[str, float]] = []
        items: List[Tuple[object, object]] = []

        if isinstance(raw, dict):
            items = list(raw.items())
        elif isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, dict):
                    items.extend(entry.items())
        elif raw is None:
            return []

        for label, weight in items:
            cleaned_label = self._clean_string(label)
            numeric_weight = self._safe_float(weight)
            if not cleaned_label or numeric_weight is None:
                continue
            if numeric_weight <= 0:
                continue
            if numeric_weight > 1.0:
                numeric_weight = numeric_weight / 100.0
            pairs.append((cleaned_label, numeric_weight))

        if not pairs:
            return []

        total = sum(weight for _, weight in pairs)
        if total > 0 and total > 1.0 + 1e-6:
            pairs = [(label, weight / total) for label, weight in pairs]
        pairs.sort(key=lambda item: item[1], reverse=True)
        return pairs

    def _collect_holdings_entries(self, data: object) -> List[Dict[str, object]]:
        entries: List[Dict[str, object]] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    entries.append(item)
            return entries

        if isinstance(data, dict):
            possible_keys = (
                "holdings",
                "equityHoldings",
                "stockHoldings",
                "bondHoldings",
                "topHoldings",
            )
            for key in possible_keys:
                value = data.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            entries.append(item)
                elif isinstance(value, dict):
                    nested = value.get("holdings")
                    if isinstance(nested, list):
                        for item in nested:
                            if isinstance(item, dict):
                                entries.append(item)
        return entries

    def _extract_country_weights(
        self, ticker: yf.Ticker, info: Dict[str, object], symbol: str
    ) -> List[Tuple[str, float]]:
        weights: Dict[str, float] = {}
        sources: List[object] = []

        try:
            holdings = ticker.get_fund_holdings()
            sources.append(holdings)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug(
                "Ignored exception while fetching fund holdings for %s: %s",
                symbol,
                exc,
            )

        sources.append(info.get("topHoldings"))

        for source in sources:
            if not source:
                continue
            for entry in self._collect_holdings_entries(source):
                country = self._clean_string(
                    entry.get("holdingCountry")
                    or entry.get("holdingCountryName")
                    or entry.get("holdingCountryCode")
                    or entry.get("country")
                )
                if not country:
                    continue
                weight = (
                    self._safe_float(entry.get("holdingPercent"))
                    or self._safe_float(entry.get("holdingPercentRaw"))
                    or self._safe_float(entry.get("weight"))
                    or self._safe_float(entry.get("percentage"))
                )
                if weight is None or weight <= 0:
                    continue
                if weight > 1.0:
                    weight = weight / 100.0
                weights[country] = weights.get(country, 0.0) + weight

        if not weights:
            return []

        total = sum(weights.values())
        if total > 0 and total > 1.0 + 1e-6:
            weights = {key: value / total for key, value in weights.items()}

        return sorted(weights.items(), key=lambda item: item[1], reverse=True)

    def _extract_etf_exposures(
        self, ticker: yf.Ticker, info: Dict[str, object], symbol: str
    ) -> Dict[str, List[Tuple[str, float]]]:
        exposures: Dict[str, List[Tuple[str, float]]] = {
            "sector": [],
            "region": [],
            "country": [],
        }

        try:
            sectors_raw = getattr(ticker, "fund_sector_weightings", None)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug(
                "Ignored exception while fetching sector weightings for %s: %s",
                symbol,
                exc,
            )
            sectors_raw = None

        try:
            regions_raw = getattr(ticker, "fund_geo_holdings", None)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug(
                "Ignored exception while fetching geo weightings for %s: %s",
                symbol,
                exc,
            )
            regions_raw = None

        sectors = self._normalize_weightings(sectors_raw)
        regions = self._normalize_weightings(regions_raw)
        countries = self._extract_country_weights(ticker, info, symbol)

        if sectors:
            exposures["sector"] = sectors
        if regions:
            exposures["region"] = regions
        if countries:
            exposures["country"] = countries

        for dimension in db.ALLOWED_EXPOSURE_DIMENSIONS:
            exposures.setdefault(dimension, [])

        return exposures
