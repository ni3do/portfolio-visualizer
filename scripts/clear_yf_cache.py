#!/usr/bin/env python3
"""Remove any yfinance cookie/cache state inside the ETL container."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent.parent / "etl"))

from app.utils import clear_yfinance_cache  # type: ignore  # pylint: disable=import-error


def main() -> None:
    cache_dir = clear_yfinance_cache()
    print(f"Reset yfinance cache at {cache_dir}")


if __name__ == "__main__":
    main()
