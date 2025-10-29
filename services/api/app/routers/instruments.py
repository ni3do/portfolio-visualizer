"""Routes for instrument metadata and yfinance mappings."""

from __future__ import annotations

import logging
from typing import List

import yfinance as yf
from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import Connection

from .. import repositories
from ..database import get_db_connection
from ..models import (
    InstrumentMappingResponse,
    InstrumentMappingUpdate,
    UnmappedInstrumentItem,
    UnmappedInstrumentsResponse,
    YFinanceSearchResponse,
    YFinanceSearchResult,
)
from ..security import get_current_username

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instruments")


@router.get(
    "/unmapped",
    response_model=UnmappedInstrumentsResponse,
    summary="List instruments without yfinance mappings",
)
def list_unmapped_instruments(
    *,
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> UnmappedInstrumentsResponse:
    rows = repositories.fetch_unmapped_instruments(conn)
    instruments: List[UnmappedInstrumentItem] = []
    for row in rows:
        shares = row.get("shares")
        instruments.append(
            UnmappedInstrumentItem(
                instrument_id=row["instrument_id"],
                symbol=row.get("symbol"),
                name=row.get("name"),
                currency=row.get("currency"),
                primary_exchange=row.get("primary_exchange"),
                asset_class=row.get("asset_class"),
                sector=row.get("sector"),
                industry=row.get("industry"),
                country=row.get("country"),
                region=row.get("region"),
                shares=float(shares) if shares is not None else None,
            )
        )

    return UnmappedInstrumentsResponse(instruments=instruments)


@router.put(
    "/{instrument_id}/mapping",
    response_model=InstrumentMappingResponse,
    summary="Assign a yfinance symbol to an instrument",
)
def update_mapping(
    *,
    instrument_id: int,
    payload: InstrumentMappingUpdate,
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),
) -> InstrumentMappingResponse:
    symbol = (payload.yfinance_symbol or "").strip()
    normalized = symbol or None

    try:
        row = repositories.update_instrument_mapping(
            conn,
            instrument_id=instrument_id,
            yfinance_symbol=normalized,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instrument not found",
        ) from exc

    return InstrumentMappingResponse(
        instrument_id=row["instrument_id"],
        yfinance_symbol=row.get("yfinance_symbol"),
    )


@router.get(
    "/search",
    response_model=YFinanceSearchResponse,
    summary="Search for Yahoo Finance symbols",
)
def search_yfinance(
    *,
    query: str = Query(..., alias="q", min_length=1, max_length=50),
    limit: int = Query(10, ge=1, le=20),
    _: str = Depends(get_current_username),
    conn: Connection = Depends(get_db_connection),  # noqa: ARG001 - ensures auth/connection
) -> YFinanceSearchResponse:
    del conn  # Connection not used directly but retained for consistency

    try:
        response = yf.search(query)
    except Exception as exc:  # pragma: no cover - network errors
        logger.exception("yfinance search failed for query %s", query)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="yfinance lookup failed",
        ) from exc

    quotes = response.get("quotes") if isinstance(response, dict) else None
    results: List[YFinanceSearchResult] = []

    if isinstance(quotes, list):
        for item in quotes:
            symbol = item.get("symbol") if isinstance(item, dict) else None
            if not symbol:
                continue
            results.append(
                YFinanceSearchResult(
                    symbol=symbol,
                    short_name=item.get("shortname") if isinstance(item, dict) else None,
                    long_name=item.get("longname") if isinstance(item, dict) else None,
                    exchange=(item.get("exchDisp") or item.get("exchange"))
                    if isinstance(item, dict)
                    else None,
                    quote_type=item.get("quoteType") if isinstance(item, dict) else None,
                )
            )
            if len(results) >= limit:
                break

    return YFinanceSearchResponse(query=query, results=results)
