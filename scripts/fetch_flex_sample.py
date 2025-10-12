#!/usr/bin/env python3
"""
Fetch a single Flex statement using the existing ETL Flex client.

Usage:

    python scripts/fetch_flex_sample.py [--output path]

Reads secrets from ./secrets/ and writes the raw CSV to the given path (or
prints to stdout if no path is supplied). Intended for ad-hoc inspection of
column names and formats.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure we can import the ETL package
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "etl"))

from app.config import AppConfig, DatabaseConfig, FlexConfig  # type: ignore  # pylint: disable=import-error
from app.flex import FlexClient  # type: ignore  # pylint: disable=import-error

SECRETS_DIR = ROOT / "secrets"


def read_secret(name: str) -> str:
    path = SECRETS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Secret {name} not found at {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value or value == "CHANGE_ME":
        raise ValueError(f"Secret {name} is unset or still the placeholder")
    return value


def make_config() -> AppConfig:
    return AppConfig(
        log_level="INFO",
        run_mode="flex-import",
        db=DatabaseConfig(
            host="postgres",
            port=5432,
            name="portfolio",
            user="",
            password="",
        ),
        flex=FlexConfig(
            token=read_secret("ibkr_flex_token"),
            query_id=read_secret("ibkr_flex_query_id"),
            archive_dir=ROOT / "etl_data",
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch a sample Flex statement")
    parser.add_argument(
        "--output",
        type=Path,
        help="Path to write the raw CSV (defaults to stdout)",
    )
    args = parser.parse_args()

    config = make_config()
    client = FlexClient(token=config.flex.token, query_id=config.flex.query_id)
    statement = client.fetch_statement()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(statement.content, encoding="utf-8")
        print(f"Wrote Flex CSV ({len(statement.content)} bytes) to {args.output}")
    else:
        print(statement.content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
