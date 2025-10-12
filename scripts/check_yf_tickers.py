#!/usr/bin/env python3
"""
Inspect a list of Yahoo Finance tickers and report their latest price info.

Usage:
    python scripts/check_yf_tickers.py TICKER1 TICKER2 ...

If no tickers are provided on the command line, a default list is used.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Iterable, Sequence

import yfinance as yf

DEFAULT_TICKERS: Sequence[str] = ("NOVO-B.CO", "UETW.DE")


def inspect_ticker(ticker: str) -> None:
    print(f"\n=== {ticker} ===")
    yticker = yf.Ticker(ticker)

    # Try the fast_info path first.
    try:
        info = yticker.fast_info
        price = getattr(info, "last_price", None)
        currency = getattr(info, "currency", None)
        ts = getattr(info, "regular_market_time", None)
        if price is not None:
            as_of = (
                datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                if ts
                else "n/a"
            )
            print(f"fast_info: price={price} currency={currency} as_of={as_of}")
        else:
            print("fast_info: no last_price field")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"fast_info error: {exc}")

    # Fall back to a short price history.
    try:
        history = yticker.history(period="5d", interval="1d", auto_adjust=False)
        if history.empty:
            print("history: empty (no data)")
        else:
            last = history.tail(1).iloc[0]
            index = last.name
            if hasattr(index, "to_pydatetime"):
                dt = index.to_pydatetime()
                if dt.tzinfo:
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                index_str = dt.isoformat()
            else:
                index_str = str(index)
            currency = history.attrs.get("currency", "n/a")
            print(
                f"history: close={last['Close']} currency={currency} as_of={index_str}"
            )
    except Exception as exc:  # pylint: disable=broad-except
        print(f"history error: {exc}")


def main(args: Iterable[str]) -> int:
    tickers = list(args)
    if not tickers:
        tickers = list(DEFAULT_TICKERS)

    for ticker in tickers:
        inspect_ticker(ticker)
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
