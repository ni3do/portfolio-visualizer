from __future__ import annotations

import argparse
import logging
import signal
import sys
from contextlib import suppress
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import load_config
from .db import create_pool, ensure_schema
from .importers.ibkr_flex import FlexImporter, read_statement_file
from .logger import configure_logging
from .prices import PriceUpdater
from .fx import FxUpdater
from .snapshots import SnapshotRecalculator
from .backfill import BackfillService
from .importers.swissquote import SwissquoteImporter
from .utils import clear_yfinance_cache


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portfolio ETL service")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("scheduler", help="Run scheduled jobs (default)")

    flex_parser = sub.add_parser(
        "flex-import", help="Run a single Flex import immediately"
    )
    flex_parser.add_argument(
        "--skip-schema",
        action="store_true",
        help="Do not run schema migrations before import",
    )
    flex_parser.add_argument("--backfill-days", type=int, default=365)
    flex_parser.add_argument("--no-backfill", action="store_true")

    import_ibkr_parser = sub.add_parser(
        "import-ibkr", help="Import a local IBKR Flex statement file"
    )
    import_ibkr_parser.add_argument("--file", required=True)
    import_ibkr_parser.add_argument("--backfill-days", type=int, default=365)
    import_ibkr_parser.add_argument("--no-backfill", action="store_true")

    import_swissquote_parser = sub.add_parser(
        "import-swissquote", help="Import a Swissquote transaction CSV"
    )
    import_swissquote_parser.add_argument("--file", required=True)
    import_swissquote_parser.add_argument("--delimiter", default=";")
    import_swissquote_parser.add_argument("--timezone", default="Europe/Zurich")
    import_swissquote_parser.add_argument("--backfill-days", type=int, default=365)
    import_swissquote_parser.add_argument("--no-backfill", action="store_true")

    sub.add_parser(
        "price-update", help="Run a single price update for all held instruments"
    )

    sub.add_parser(
        "fx-update", help="Run a single FX rate update for held currencies"
    )

    sub.add_parser(
        "snapshot-recompute",
        help="Rebuild hourly positions/portfolio snapshots",
    )

    sub.add_parser(
        "position-tickers",
        help="Show the latest position snapshot mapped to yfinance tickers",
    )

    backfill_parser = sub.add_parser(
        "backfill", help="Backfill historical prices and FX rates"
    )
    backfill_parser.add_argument("--days", type=int, default=365)
    backfill_parser.add_argument("--prices-only", action="store_true")
    backfill_parser.add_argument("--fx-only", action="store_true")
    backfill_parser.add_argument("--snapshots", action="store_true")

    sub.add_parser("clear-cache", help="Clear yfinance cache files")

    return parser


def run_scheduler(config, pool) -> None:
    flex_importer = FlexImporter(config.flex)
    price_updater = PriceUpdater(config.price, pool)
    fx_updater = FxUpdater(config.fx, pool)
    snapshotper = SnapshotRecalculator(config.snapshot, pool)

    scheduler = BlockingScheduler(timezone="Europe/Amsterdam")
    scheduler.add_job(
        lambda: flex_importer.run(pool),
        trigger="cron",
        hour=18,
        minute=0,
        id="flex_import",
    )
    scheduler.add_job(
        lambda: flex_importer.run(pool),
        trigger="cron",
        hour=18,
        minute=30,
        id="flex_import_retry",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        price_updater.run,
        trigger="cron",
        minute="0,15,30,45",
        id="price_updater",
        misfire_grace_time=900,
        max_instances=1,
    )
    scheduler.add_job(
        fx_updater.run,
        trigger="cron",
        minute="10,25,40,55",
        id="fx_updater",
        misfire_grace_time=900,
        max_instances=1,
    )
    scheduler.add_job(
        snapshotper.run,
        trigger="cron",
        minute="5,20,35,50",
        id="snapshot_recompute",
        misfire_grace_time=900,
        max_instances=1,
    )

    def _handle_signal(signum, _frame):
        logging.getLogger(__name__).info(
            "Received signal %s, shutting down scheduler", signum
        )
        scheduler.shutdown(wait=False)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    logging.getLogger(__name__).info(
        "Scheduler started (prices every 15 min, FX every 15 min offset, snapshots after each cycle, Flex import daily 18:00 Europe/Amsterdam)"
    )
    scheduler.start()


def _run_backfill_after_import(pool, config, days: int) -> None:
    logging.getLogger(__name__).info(
        "Running backfill for the last %s days after import", days
    )
    backfill = BackfillService(config.snapshot, pool)
    backfill.run(
        days=days,
        include_prices=True,
        include_fx=True,
        include_snapshots=True,
    )



def main(argv: list[str] | None = None) -> int:
    config = load_config()
    configure_logging(config.log_level)

    parser = build_parser()
    arg_list = argv if argv is not None else sys.argv[1:]
    if not arg_list:
        arg_list = [config.run_mode]
    args = parser.parse_args(arg_list)

    pool = create_pool(config.db)

    try:
        if args.command == "flex-import":
            importer = FlexImporter(config.flex)
            if not args.skip_schema:
                ensure_schema(pool)
            importer.run(pool)
            if not args.no_backfill:
                _run_backfill_after_import(pool, config, args.backfill_days)
            return 0

        if args.command == "import-ibkr":
            ensure_schema(pool)
            content = read_statement_file(Path(args.file))
            importer = FlexImporter(config.flex)
            importer.run(
                pool,
                statement_content=content,
                reference_code=Path(args.file).stem,
            )
            if not args.no_backfill:
                _run_backfill_after_import(pool, config, args.backfill_days)
            return 0

        if args.command == "import-swissquote":
            ensure_schema(pool)
            importer = SwissquoteImporter(
                Path(args.file),
                delimiter=args.delimiter,
                timezone=args.timezone,
            )
            importer.run(pool)
            if not args.no_backfill:
                _run_backfill_after_import(pool, config, args.backfill_days)
            return 0

        if args.command == "price-update":
            ensure_schema(pool)
            PriceUpdater(config.price, pool).run()
            return 0

        if args.command == "fx-update":
            ensure_schema(pool)
            FxUpdater(config.fx, pool).run()
            return 0

        if args.command == "snapshot-recompute":
            ensure_schema(pool)
            SnapshotRecalculator(config.snapshot, pool).run(None)
            return 0

        if args.command == "position-tickers":
            ensure_schema(pool)
            tickers = db.get_latest_position_tickers(pool)
            if not tickers:
                logging.getLogger(__name__).info(
                    "No position snapshots with ticker mappings were found"
                )
                return 0

            print("account_id,instrument_id,ticker,shares,currency")
            for entry in tickers:
                print(
                    f"{entry.account_id},{entry.instrument_id},{entry.ticker},"
                    f"{entry.shares},{entry.currency}"
                )
            return 0

        if args.command == "backfill":
            ensure_schema(pool)
            BackfillService(config.snapshot, pool).run(
                days=args.days,
                include_prices=not args.fx_only,
                include_fx=not args.prices_only,
                include_snapshots=args.snapshots,
            )
            return 0

        if args.command == "clear-cache":
            path = clear_yfinance_cache()
            logging.getLogger(__name__).info("Cleared yfinance cache at %s", path)
            return 0

        if args.command in {None, "scheduler"}:
            ensure_schema(pool)
            run_scheduler(config, pool)
            return 0

        parser.print_help()
        return 1
    finally:
        with suppress(Exception):
            pool.close()


if __name__ == "__main__":
    sys.exit(main())
