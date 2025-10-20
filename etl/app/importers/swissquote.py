from __future__ import annotations

import csv
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Optional

from dateutil import parser as date_parser
from psycopg_pool import ConnectionPool
from zoneinfo import ZoneInfo

from .. import db
from ..models import CashMovement, Instrument, Transaction

logger = logging.getLogger(__name__)


@dataclass
class SwissquoteRow:
    raw: Dict[str, str]


class SwissquoteImporter:
    """One-off importer for Swissquote CSV exports."""

    def __init__(
        self,
        csv_path: Path,
        *,
        delimiter: str = ";",
        timezone: str = "Europe/Zurich",
    ) -> None:
        self.csv_path = csv_path
        self.delimiter = delimiter
        self.tz = ZoneInfo(timezone)

    def run(self, pool: ConnectionPool) -> None:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Swissquote CSV not found: {self.csv_path}")

        instruments: Dict[str, Instrument] = {}
        transactions: list[Transaction] = []
        cash_movements: list[CashMovement] = []

        for row in self._iter_rows():
            result = self._parse_row(row, instruments)
            if result is None:
                continue
            kind, payload = result
            if kind == "transaction":
                transactions.append(payload)  # type: ignore[arg-type]
            elif kind == "cash":
                cash_movements.append(payload)  # type: ignore[arg-type]

        if not any([instruments, transactions, cash_movements]):
            logger.warning("No Swissquote rows could be mapped")
            return

        db.ensure_schema(pool)
        if instruments:
            db.upsert_instruments(pool, instruments.values())
        if transactions:
            db.upsert_transactions(pool, transactions)
        if cash_movements:
            db.upsert_cash_movements(pool, cash_movements)

        logger.info(
            "Swissquote import completed -> %d instruments, %d transactions, %d cash entries",
            len(instruments),
            len(transactions),
            len(cash_movements),
        )

    # ------------------------------------------------------------------
    def _iter_rows(self):
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=self.delimiter)
            for raw in reader:
                if not raw:
                    continue
                yield SwissquoteRow({k.strip(): v.strip() for k, v in raw.items() if k is not None})

    def _parse_row(  # noqa: C901 - keep logic together
        self,
        row: SwissquoteRow,
        instruments: Dict[str, Instrument],
    ) -> tuple[str, Transaction | CashMovement] | None:
        data = row.raw
        type_value = data.get("Type") or data.get("Transaction Type") or ""
        type_lower = type_value.lower()

        timestamp = self._parse_datetime(data)
        account_id = self._get_value(data, ["Account", "Account Number", "Portfolio"])
        currency = self._get_value(data, ["Currency", "Account Currency", "Currency (Account)"]) or ""
        symbol = self._get_value(data, ["Symbol", "Product", "Ticker"]) or ""
        isin = self._get_value(data, ["ISIN", "Isin"]) or symbol
        qty = self._parse_decimal(self._get_value(data, ["Quantity", "Qty"])) or Decimal("0")
        price = self._parse_decimal(self._get_value(data, ["Price", "Price (Currency)"])) or Decimal("0")
        fees = self._parse_decimal(self._get_value(data, ["Fees", "Charges", "Commission"])) or Decimal("0")
        net_amount = self._parse_decimal(
            self._get_value(data, ["Amount in Account currency", "Amount (Account)", "Net Amount"])
        ) or Decimal("0")
        gross_amount = self._parse_decimal(
            self._get_value(data, ["Amount in Product currency", "Gross Amount", "Amount"])
        )
        description = self._get_value(data, ["Description", "Remarks", "Comment"]) or type_value
        transaction_id = self._get_value(data, ["Transaction ID", "Order ID", "ID"]) or self._hash_row(data)

        # Instruments -----------------------------------------------------
        instrument: Optional[Instrument] = None
        inst_key: Optional[str] = None
        if symbol or isin:
            inst_key = isin or symbol
            instrument = instruments.get(inst_key)
            if instrument is None:
                instrument_id = _stable_int(inst_key)
                instrument = Instrument(
                    instrument_id=instrument_id,
                    symbol=symbol or isin,
                    yfinance_symbol=symbol or None,
                    name=self._get_value(data, ["Product Name", "Security", "Instrument"]),
                    currency=self._get_value(
                        data, ["Product Currency", "Instrument Currency", "Currency"]
                    )
                    or currency
                    or "USD",
                    asset_class=self._get_value(data, ["Asset Type", "Category"]),
                    primary_exchange=self._get_value(data, ["Exchange"]),
                    sector=None,
                    country=self._get_value(data, ["Country", "Market"]),
                    region=None,
                )
                instruments[inst_key] = instrument

        # Transactions ----------------------------------------------------
        if any(keyword in type_lower for keyword in ("buy", "sell")):
            if instrument is None:
                logger.warning("Skipping trade without instrument data: %s", data)
                return None
            signed_qty = qty if "buy" in type_lower else -qty
            net_ccy = net_amount if net_amount != 0 else (gross_amount or Decimal("0"))
            if signed_qty > 0 and net_ccy > 0:
                net_ccy *= Decimal("-1")
            if signed_qty < 0 and net_ccy < 0:
                net_ccy *= Decimal("-1")
            trade = Transaction(
                trade_id=transaction_id,
                account_id=account_id,
                date_time_utc=timestamp,
                type=type_value.upper() or ("BUY" if signed_qty >= 0 else "SELL"),
                instrument_id=instrument.instrument_id,
                qty=signed_qty,
                price=price,
                currency=instrument.currency,
                fees=fees.copy_abs(),
                net_amount=net_ccy,
                source="swissquote",
                raw_flex_id=None,
            )
            return "transaction", trade

        # Cash flows ------------------------------------------------------
        if type_lower:
            direction = Decimal("1")
            if any(
                word in type_lower
                for word in ("fee", "charge", "tax", "interest debit", "debit", "withdraw", "payment")
            ) or "fx credit comp" in type_lower:
                direction = Decimal("-1")
            amount = net_amount or gross_amount or fees
            if amount == 0:
                logger.debug("Skipping zero-amount cash movement: %s", data)
                return None
            cash_currency = currency or (instrument.currency if instrument else "EUR")
            movement = CashMovement(
                movement_id=transaction_id,
                account_id=account_id,
                date_time_utc=timestamp,
                currency=cash_currency,
                amount=amount * direction,
                movement_type=type_value,
                description=description,
                source="swissquote",
            )
            return "cash", movement

        logger.debug("Unrecognised Swissquote row: %s", data)
        return None

    # ------------------------------------------------------------------
    def _parse_datetime(self, data: Dict[str, str]) -> datetime:
        date_str = self._get_value(data, ["Date", "Trade Date", "Execution Date"]) or ""
        time_str = self._get_value(data, ["Time", "Trade Time", "Execution Time"])
        if not date_str:
            raise ValueError("Missing date column in Swissquote file")
        dt = date_parser.parse(f"{date_str} {time_str or '00:00:00'}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        else:
            dt = dt.astimezone(self.tz)
        return dt.astimezone(timezone.utc)

    def _get_value(self, data: Dict[str, str], keys) -> Optional[str]:
        for key in keys:
            if key in data and data[key]:
                return data[key]
        return None

    def _parse_decimal(self, value: Optional[str]) -> Optional[Decimal]:
        if value is None or value == "":
            return None
        normalised = value.replace("'", "").replace(" ", "").replace(",", ".")
        try:
            return Decimal(normalised)
        except InvalidOperation:
            logger.debug("Could not parse decimal '%s'", value)
            return None

    def _hash(self, value: str) -> str:
        return hashlib.sha1(value.encode("utf-8")).hexdigest()

    def _hash_row(self, data: Dict[str, str]) -> str:
        signature = "|".join(f"{k}:{v}" for k, v in sorted(data.items()))
        return self._hash(signature)


def _stable_int(key: str) -> int:
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], byteorder="big", signed=False)
    return value >> 1
