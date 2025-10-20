from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional
from zipfile import ZipFile

import requests
from dateutil import parser as date_parser
from xml.etree import ElementTree as ET

from .models import CashMovement, FxRate, Instrument, Transaction

logger = logging.getLogger(__name__)

SEND_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.SendRequest"
GET_URL = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement"


@dataclass(frozen=True)
class FlexStatement:
    content: str
    fetched_at: datetime
    reference_code: str


class FlexClient:
    def __init__(
        self,
        token: str,
        query_id: str,
        *,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ):
        self.token = token
        self.query_id = query_id
        self.timeout = timeout
        self.session = session or requests.Session()

    def fetch_statement(self) -> FlexStatement:
        logger.info("Requesting Flex statement")
        send_params = {"t": self.token, "q": self.query_id, "v": "3"}
        send_resp = self.session.get(SEND_URL, params=send_params, timeout=self.timeout)
        send_resp.raise_for_status()
        reference_code = _extract_reference_code(send_resp.text)
        logger.debug("Received Flex reference %s", reference_code)

        # IBKR recommends allowing a short pause before retrieving the statement.
        time.sleep(5)

        get_params = {"t": self.token, "q": reference_code, "v": "3"}
        get_resp = self.session.get(GET_URL, params=get_params, timeout=self.timeout)
        get_resp.raise_for_status()

        content = _extract_statement_body(get_resp)
        statement = FlexStatement(
            content=content,
            fetched_at=datetime.now(tz=timezone.utc),
            reference_code=reference_code,
        )
        logger.info("Fetched Flex statement (%d bytes)", len(content))
        return statement


def _extract_reference_code(payload: str) -> str:
    payload_stripped = payload.strip()
    if payload_stripped.startswith("<"):
        try:
            root = ET.fromstring(payload_stripped)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"Unable to parse Flex response XML: {payload[:200]}"
            ) from exc

        status = root.findtext("Status")
        if status and status.lower() != "success":
            code = root.findtext("ErrorCode") or "unknown"
            message = (
                root.findtext("ErrorText")
                or root.findtext("ErrorMsg")
                or payload_stripped
            )
            raise RuntimeError(f"Flex send request failed (code {code}): {message}")

        reference = root.findtext("ReferenceCode")
        if not reference:
            raise RuntimeError(f"Flex response missing ReferenceCode: {payload[:200]}")
        return reference.strip()

    for line in payload.splitlines():
        if line.startswith("ReferenceCode="):
            return line.split("=", 1)[1].strip()
        if line.startswith("ErrorCode="):
            parts = _parse_kv_pairs(line)
            code = parts.get("ErrorCode")
            message = parts.get("ErrorMsg") or payload.strip()
            raise RuntimeError(f"Flex send request failed (code {code}): {message}")
    raise RuntimeError(f"Unable to find ReferenceCode in response: {payload[:200]}")


def _extract_statement_body(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "")
    if "zip" in content_type or response.content.startswith(b"PK"):
        with ZipFile(io.BytesIO(response.content)) as zf:
            zip_members = zf.namelist()
            if not zip_members:
                raise RuntimeError("Flex zip archive was empty")
            member = zip_members[0]
            logger.debug("Unzipping Flex statement member %s", member)
            with zf.open(member) as fh:
                return fh.read().decode("utf-8")

    response.encoding = "utf-8"
    text = response.text
    if text.startswith("ErrorCode="):
        parts = _parse_kv_pairs(text)
        code = parts.get("ErrorCode")
        message = parts.get("ErrorMsg") or text.strip()
        raise RuntimeError(f"Flex get statement failed (code {code}): {message}")
    return text


def _parse_kv_pairs(payload: str) -> Dict[str, str]:
    payload = payload.replace(";", ",")
    result: Dict[str, str] = {}
    for token in payload.split(","):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        result[key.strip()] = value.strip()
    return result


SECTION_HINTS = {
    "trades": frozenset({"tradeid", "trademoney", "netcash"}),
    "cash_transactions": frozenset({"amount", "type", "transactionid"}),
    "cash_transactions_tax": frozenset({"taxdescription", "taxamount", "tradeid"}),
    "fx_rates": frozenset({"fromcurrency", "tocurrency", "rate"}),
    "forex_balances": frozenset({"functionalcurrency", "fxcurrency", "closeprice"}),
}


def parse_sections(csv_text: str) -> Dict[str, List[Dict[str, str]]]:
    buffer = io.StringIO(csv_text)
    reader = csv.reader(buffer)

    sections: Dict[str, List[Dict[str, str]]] = {}
    current_section: Optional[str] = None
    current_headers: List[str] = []

    for raw_row in reader:
        if not raw_row:
            continue
        row = [col.strip() for col in raw_row]
        if not any(row):
            continue

        first_cell = row[0]
        if first_cell in {"FlexStatement", "AccountInformation", "EquitySummaryInBase"}:
            continue

        if first_cell in {"ClientAccountID", "Date/Time"}:
            section = _identify_section(row)
            if not section:
                logger.debug("Skipping unrecognized Flex header row: %s", row)
                current_section = None
                current_headers = []
                continue

            current_section = section
            current_headers = row
            sections.setdefault(section, [])
            logger.debug(
                "Active Flex section set to %s with %d columns", section, len(row)
            )
            continue

        if current_section is None or not current_headers:
            logger.debug("Ignoring row outside of a recognized section: %s", row)
            continue

        if len(row) != len(current_headers):
            logger.warning(
                "Row length mismatch for section %s: expected %d, got %d",
                current_section,
                len(current_headers),
                len(row),
            )
            continue

        normalized = {
            header.lower(): value for header, value in zip(current_headers, row)
        }
        sections[current_section].append(normalized)

    # Merge tax hint into cash transactions
    if "cash_transactions_tax" in sections:
        sections.setdefault("cash_transactions", []).extend(
            sections.pop("cash_transactions_tax")
        )

    return sections


def _identify_section(headers: List[str]) -> Optional[str]:
    lowered = [h.strip().lower() for h in headers]
    header_set = set(lowered)

    if SECTION_HINTS["fx_rates"].issubset(header_set):
        return "fx_rates"
    if SECTION_HINTS["forex_balances"].issubset(header_set):
        return "forex_balances"
    if SECTION_HINTS["trades"].issubset(header_set):
        return "trades"
    if SECTION_HINTS["cash_transactions"].issubset(header_set):
        return "cash_transactions"
    if SECTION_HINTS["cash_transactions_tax"].issubset(header_set):
        return "cash_transactions_tax"
    return None


def _parse_decimal(raw: Optional[str]) -> Decimal:
    raw = (raw or "").strip()
    if raw == "":
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Bad decimal value '{raw}'") from exc


def _parse_datetime(date_str: Optional[str], time_str: Optional[str]) -> datetime:
    if not date_str:
        raise ValueError("Missing date for datetime parsing")

    date_str = date_str.strip()
    clean_time = (time_str or "").strip()

    if ";" in date_str:
        parts = date_str.split(";", 1)
        date_str = parts[0]
        if not clean_time and len(parts) > 1:
            clean_time = parts[1]

    if clean_time and ";" in clean_time:
        clean_time = clean_time.split(";", 1)[-1]

    if clean_time and len(clean_time) == 6 and clean_time.isdigit():
        clean_time = f"{clean_time[0:2]}:{clean_time[2:4]}:{clean_time[4:6]}"

    try:
        if clean_time:
            dt = date_parser.parse(f"{date_str} {clean_time}")
        else:
            if len(date_str) == 8 and date_str.isdigit():
                dt = datetime.strptime(date_str, "%Y%m%d")
            else:
                dt = date_parser.parse(date_str)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError(
            f"Failed to parse datetime from {date_str} {clean_time}"
        ) from exc

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _first(row: Dict[str, str], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        if key in row and row[key] != "":
            return row[key]
    return None


def extract_instruments(trade_rows: Iterable[Dict[str, str]]) -> List[Instrument]:
    instruments: Dict[int, Instrument] = {}
    for row in trade_rows:
        conid_raw = _first(row, ("conid", "conidsecurityid", "conidex"))
        if not conid_raw:
            logger.debug("Skipping trade row without conid: %s", row)
            continue
        try:
            instrument_id = int(conid_raw)
        except ValueError:
            logger.warning("Unexpected non-numeric conid %s; skipping", conid_raw)
            continue

        symbol = _first(row, ("symbol", "localsymbol")) or f"conid:{instrument_id}"
        name = row.get("description")
        currency = row.get("currencyprimary") or row.get("symbolcurrency") or "USD"
        asset_class = row.get("assetclass")
        primary_exchange = row.get("listingexchange") or row.get("primaryexchange")

        instruments[instrument_id] = Instrument(
            instrument_id=instrument_id,
            symbol=symbol,
            yfinance_symbol=row.get("symbol") or None,
            name=name,
            currency=currency,
            asset_class=asset_class,
            primary_exchange=primary_exchange,
            sector=None,
            industry=None,
            country=row.get("issuercountrycode") or row.get("countrycode"),
            region=None,
        )
    return list(instruments.values())


def extract_transactions(trade_rows: Iterable[Dict[str, str]]) -> List[Transaction]:
    transactions: List[Transaction] = []
    for row in trade_rows:
        trade_id = _first(row, ("tradeid", "ibexecid"))
        if not trade_id:
            logger.debug("Skipping trade row without trade ID: %s", row)
            continue
        conid_raw = _first(row, ("conid", "conidsecurityid", "conidex"))
        if not conid_raw:
            logger.debug("Skipping trade %s without conid", trade_id)
            continue
        try:
            instrument_id = int(conid_raw)
        except ValueError:
            logger.warning(
                "Skipping trade %s due to invalid conid %s", trade_id, conid_raw
            )
            continue

        trade_date = _first(
            row, ("datetime", "date/time", "tradedatetime", "tradedate")
        )
        trade_time = _first(row, ("tradetime",))
        try:
            trade_dt = _parse_datetime(trade_date, trade_time)
        except ValueError as exc:
            logger.warning(
                "Skipping trade %s due to datetime parse error: %s", trade_id, exc
            )
            continue

        qty = _parse_decimal(_first(row, ("quantity", "qty")))
        price = _parse_decimal(_first(row, ("tradeprice", "price")))
        fees = _parse_decimal(row.get("ibcommission")) + _parse_decimal(
            row.get("taxes")
        )
        net_amount = _parse_decimal(_first(row, ("netcash", "netamount")))
        account_id = (
            _first(row, ("clientaccountid", "accountid", "accountidsummary"))
            or "UNKNOWN"
        )
        buy_sell = _first(row, ("buy/sell", "buysell", "buysellcode")) or "UNKNOWN"
        raw_flex_id = _first(
            row, ("orderid", "origorderid", "origtransactionid", "iborderid")
        )

        transactions.append(
            Transaction(
                trade_id=trade_id,
                account_id=account_id,
                date_time_utc=trade_dt,
                type=buy_sell,
                instrument_id=instrument_id,
                qty=qty,
                price=price,
                currency=row.get("currencyprimary") or "USD",
                fees=fees,
                net_amount=net_amount,
                source="ibkr_flex",
                raw_flex_id=raw_flex_id,
            )
        )
    return transactions


def extract_cash_movements(cash_rows: Iterable[Dict[str, str]]) -> List[CashMovement]:
    movements: List[CashMovement] = []
    for row in cash_rows:
        movement_id = _first(
            row,
            (
                "transactionid",
                "cashtransactionid",
                "externaltransactionid",
                "tradeid",
                "orderid",
                "actionid",
            ),
        )
        if not movement_id:
            logger.debug("Skipping cash row without transaction ID: %s", row)
            continue
        account_id = (
            _first(row, ("clientaccountid", "accountid", "accountidsummary"))
            or "UNKNOWN"
        )
        currency = _first(row, ("currencyprimary", "currency")) or "USD"
        movement_type = _first(row, ("type", "taxdescription", "code", "levelofdetail"))
        description = row.get("description")
        if not movement_type and description:
            movement_type = description

        date_str = _first(
            row, ("date/time", "datetime", "date", "reportdate", "settledate")
        )
        time_str = _first(row, ("time", "tradetime"))
        try:
            dt_utc = _parse_datetime(date_str, time_str)
        except ValueError as exc:
            logger.warning(
                "Skipping cash movement %s due to datetime parse error: %s",
                movement_id,
                exc,
            )
            continue

        amount = _parse_decimal(
            _first(row, ("amount", "settleamount", "taxamount", "netcash"))
        )

        movements.append(
            CashMovement(
                movement_id=movement_id,
                account_id=account_id,
                date_time_utc=dt_utc,
                currency=currency,
                amount=amount,
                movement_type=movement_type,
                description=description,
                source="ibkr_flex",
            )
        )
    return movements


def extract_fx_rates(fx_rows: Iterable[Dict[str, str]]) -> List[FxRate]:
    rates: List[FxRate] = []
    for row in fx_rows:
        date_val = _first(row, ("date/time", "date"))
        if not date_val:
            logger.debug("Skipping FX row without date: %s", row)
            continue
        try:
            dt = _parse_datetime(date_val, None)
        except ValueError as exc:
            logger.warning("Skipping FX row due to datetime parse error: %s", exc)
            continue
        rate_raw = row.get("rate")
        if rate_raw is None:
            logger.debug("Skipping FX row without rate: %s", row)
            continue
        try:
            rate = _parse_decimal(rate_raw)
        except ValueError as exc:
            logger.warning("Skipping FX row due to rate parse error: %s", exc)
            continue
        from_ccy = _first(row, ("fromcurrency",))
        to_ccy = _first(row, ("tocurrency",))
        if not from_ccy or not to_ccy:
            logger.debug("Skipping FX row due to missing currencies: %s", row)
            continue

        rates.append(
            FxRate(
                date_utc=dt.date(),
                from_ccy=from_ccy,
                to_ccy=to_ccy,
                rate=rate,
                source="ibkr_flex",
            )
        )
    return rates


def extract_fx_from_forex_balances(rows: Iterable[Dict[str, str]]) -> List[FxRate]:
    rates: List[FxRate] = []
    for row in rows:
        date_val = row.get("reportdate")
        if not date_val:
            logger.debug("Skipping forex balance row without report date: %s", row)
            continue
        try:
            dt = _parse_datetime(date_val, None)
        except ValueError as exc:
            logger.warning(
                "Skipping forex balance row due to datetime parse error: %s", exc
            )
            continue

        rate_raw = row.get("closeprice")
        if not rate_raw:
            logger.debug("Skipping forex balance row without close price: %s", row)
            continue
        try:
            rate = _parse_decimal(rate_raw)
        except ValueError as exc:
            logger.warning(
                "Skipping forex balance row due to rate parse error: %s", exc
            )
            continue

        from_ccy = row.get("fxcurrency")
        to_ccy = row.get("functionalcurrency")
        if not from_ccy or not to_ccy:
            logger.debug("Skipping forex balance row with missing currencies: %s", row)
            continue

        rates.append(
            FxRate(
                date_utc=dt.date(),
                from_ccy=from_ccy,
                to_ccy=to_ccy,
                rate=rate,
                source="ibkr_flex_forex_balance",
            )
        )
    return rates


def extract_entities(
    sections: Dict[str, List[Dict[str, str]]],
) -> tuple[List[Instrument], List[Transaction], List[CashMovement], List[FxRate]]:
    trade_rows = sections.get("trades", [])
    cash_rows = sections.get("cash_transactions", [])
    fx_rows = sections.get("fx_rates", [])
    forex_balance_rows = sections.get("forex_balances", [])

    instruments = extract_instruments(trade_rows)
    transactions = extract_transactions(trade_rows)
    cash_movements = extract_cash_movements(cash_rows)
    fx_rates = extract_fx_rates(fx_rows)
    fx_rates.extend(extract_fx_from_forex_balances(forex_balance_rows))
    return instruments, transactions, cash_movements, fx_rates
