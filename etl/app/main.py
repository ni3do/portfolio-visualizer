from __future__ import annotations

import argparse
import logging
import signal
import sys
import csv
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional, Sequence, List

from apscheduler.schedulers.blocking import BlockingScheduler
from dateutil import parser as date_parser

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
from .instruments import InstrumentMetadataUpdater
from .performance import PerformanceCalculator, PositionPnlRow


DecimalZero = Decimal("0")
DecimalOne = Decimal("1")


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
    flex_parser.add_argument("--backfill-start-date")
    flex_parser.add_argument("--backfill-end-date")
    flex_parser.add_argument("--no-backfill", action="store_true")

    import_ibkr_parser = sub.add_parser(
        "import-ibkr", help="Import a local IBKR Flex statement file"
    )
    import_ibkr_parser.add_argument("--file", required=True)
    import_ibkr_parser.add_argument("--backfill-days", type=int, default=365)
    import_ibkr_parser.add_argument("--backfill-start-date")
    import_ibkr_parser.add_argument("--backfill-end-date")
    import_ibkr_parser.add_argument("--no-backfill", action="store_true")

    import_swissquote_parser = sub.add_parser(
        "import-swissquote", help="Import a Swissquote transaction CSV"
    )
    import_swissquote_parser.add_argument("--file", required=True)
    import_swissquote_parser.add_argument("--delimiter", default=";")
    import_swissquote_parser.add_argument("--timezone", default="Europe/Zurich")
    import_swissquote_parser.add_argument("--backfill-days", type=int, default=365)
    import_swissquote_parser.add_argument("--backfill-start-date")
    import_swissquote_parser.add_argument("--backfill-end-date")
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

    returns_parser = sub.add_parser(
        "returns-report",
        help="Compute portfolio returns (TWR, MWR, contributions) for an account",
    )
    returns_parser.add_argument("--account", required=True, help="Account identifier")
    returns_parser.add_argument(
        "--start",
        required=True,
        help="Start timestamp (ISO 8601)",
    )
    returns_parser.add_argument(
        "--end",
        required=True,
        help="End timestamp (ISO 8601)",
    )
    returns_parser.add_argument(
        "--csv",
        dest="csv_path",
        help="Optional path to write CSV output",
    )

    positions_pnl_parser = sub.add_parser(
        "position-pnl",
        help="Show unrealized/realized PnL and returns for positions",
    )
    positions_pnl_parser.add_argument("--account", required=True, help="Account identifier")
    positions_pnl_parser.add_argument(
        "--as-of",
        help="Snapshot timestamp to inspect (defaults to latest)",
    )
    positions_pnl_parser.add_argument(
        "--hours",
        type=int,
        default=24,
        help="Number of hours for hourly return window (default 24)",
    )
    positions_pnl_parser.add_argument(
        "--csv",
        dest="csv_path",
        help="Optional path to write CSV output",
    )

    backfill_parser = sub.add_parser(
        "backfill", help="Backfill historical prices and FX rates"
    )
    backfill_parser.add_argument("--days", type=int, default=365)
    backfill_parser.add_argument("--start-date")
    backfill_parser.add_argument("--end-date")
    backfill_parser.add_argument("--prices-only", action="store_true")
    backfill_parser.add_argument("--fx-only", action="store_true")
    backfill_parser.add_argument("--snapshots", action="store_true")

    sub.add_parser("clear-cache", help="Clear yfinance cache files")

    instrument_parser = sub.add_parser(
        "instrument-update",
        help="Refresh instrument metadata such as sector, country, and exchange",
    )
    instrument_parser.add_argument(
        "--all",
        action="store_true",
        help="Update all instruments (default updates entries missing metadata)",
    )

    return parser


def run_scheduler(config, pool) -> None:
    flex_importer = FlexImporter(config.flex)
    price_updater = PriceUpdater(config.price, pool)
    fx_updater = FxUpdater(config.fx, pool)
    snapshotper = SnapshotRecalculator(config.snapshot, pool)
    instrument_updater = InstrumentMetadataUpdater(config.instrument, pool)

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

    scheduler.add_job(
        lambda: instrument_updater.run(include_all=False),
        trigger="cron",
        hour=3,
        minute=30,
        id="instrument_metadata_refresh",
        misfire_grace_time=3600,
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
        "Scheduler started (prices every 15 min, FX every 15 min offset, snapshots after each cycle, Flex import daily 18:00 Europe/Amsterdam, instrument metadata 03:30)"
    )
    scheduler.start()


def _run_backfill_after_import(
    pool,
    config,
    days: int | None = None,
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> None:
    if start_date:
        logging.getLogger(__name__).info(
            "Running backfill from %s to %s after import",
            start_date.date(),
            (end_date.date() if end_date else "now"),
        )
    else:
        logging.getLogger(__name__).info(
            "Running backfill for the last %s days after import", days
        )
    backfill = BackfillService(config.snapshot, pool)
    backfill.run(
        days=days if not start_date else None,
        start=start_date,
        end=end_date,
        include_prices=True,
        include_fx=True,
        include_snapshots=True,
    )



def _parse_timestamp(raw: str) -> datetime:
    try:
        result = date_parser.isoparse(raw)
    except Exception:  # pylint: disable=broad-except
        result = date_parser.parse(raw)

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _parse_optional_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        result = date_parser.isoparse(raw)
    except Exception:  # pylint: disable=broad-except
        try:
            result = date_parser.parse(raw)
        except Exception as exc:  # pylint: disable=broad-except
            raise ValueError(f"Could not parse date: {raw}") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _fmt_decimal(value: Optional[Decimal], places: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{places}f}"


def _fmt_percent(value: Optional[Decimal | float], places: int = 2) -> str:
    if value is None:
        return "-"
    numeric = float(value)
    return f"{numeric * 100:.{places}f}%"


def _fmt_currency(value: Optional[Decimal], base_currency: str) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.2f} {base_currency}"


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(str(header)) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))

    header_line = " | ".join(str(header).ljust(widths[idx]) for idx, header in enumerate(headers))
    separator = "-+-".join("-" * width for width in widths)
    rendered_rows = [
        " | ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(row))
        for row in rows
    ]
    return "\n".join([header_line, separator, *rendered_rows])


def _summarize_positions(rows: Sequence[PositionPnlRow]) -> dict[str, Optional[Decimal | int]]:
    total_value = DecimalZero
    total_cost = DecimalZero
    total_unrealized = DecimalZero
    weighted_hourly_numerator = DecimalZero
    weighted_hourly_denominator = DecimalZero

    for row in rows:
        value = row.value_eur if row.value_eur is not None else DecimalZero
        total_value += value
        total_cost += row.cost_eur
        total_unrealized += row.unrealized_eur if row.unrealized_eur is not None else DecimalZero
        if row.hourly_return is not None and row.value_eur is not None:
            weighted_hourly_numerator += row.value_eur * row.hourly_return
            weighted_hourly_denominator += row.value_eur

    simple_return = None
    if total_cost != DecimalZero:
        simple_return = (total_value / total_cost) - DecimalOne

    hourly_return = None
    if weighted_hourly_denominator != DecimalZero:
        hourly_return = weighted_hourly_numerator / weighted_hourly_denominator

    return {
        "count": len(rows),
        "value": total_value,
        "cost": total_cost,
        "unrealized": total_unrealized,
        "simple_return": simple_return,
        "hourly_return": hourly_return,
    }


def _rank_positions(rows: Sequence[PositionPnlRow], *, top: bool, count: int) -> List[PositionPnlRow]:
    candidates = [row for row in rows if row.simple_return is not None and row.value_eur is not None]
    if not candidates:
        return []
    sorted_rows = sorted(candidates, key=lambda r: r.simple_return, reverse=top)
    return sorted_rows[:count]


def _print_returns_report(metrics, base_currency: str) -> None:
    summary_rows = [
        ("Account", metrics.account_id),
        ("Range", f"{metrics.start_at.isoformat()} -> {metrics.end_at.isoformat()}"),
        ("NAV start", _fmt_currency(metrics.nav_start, base_currency)),
        ("NAV end", _fmt_currency(metrics.nav_end, base_currency)),
        ("Δ NAV", _fmt_currency(metrics.absolute_change, base_currency)),
        ("Percent change", _fmt_percent(metrics.percent_change)),
        ("TWR", _fmt_percent(metrics.twr)),
        ("MWR (IRR)", _fmt_percent(metrics.mwr)),
        ("MWR annualized", _fmt_percent(metrics.mwr_annualized)),
        ("Contributions", _fmt_currency(metrics.contributions, base_currency)),
        ("Withdrawals", _fmt_currency(metrics.withdrawals, base_currency)),
        ("Net flow", _fmt_currency(metrics.net_flow, base_currency)),
        ("Realized PnL Δ", _fmt_currency(metrics.realized_pnl_change, base_currency)),
    ]

    print(_render_table(["Metric", "Value"], summary_rows))

    if metrics.sub_periods:
        sub_rows = [
            (ts.isoformat(), _fmt_percent(value)) for ts, value in metrics.sub_periods
        ]
        print()
        print("Sub-period returns:")
        print(_render_table(["Period End", "Return"], sub_rows))


def _write_returns_csv(metrics, path: Path) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("account_id", metrics.account_id),
        ("start_at", metrics.start_at.isoformat()),
        ("end_at", metrics.end_at.isoformat()),
        ("nav_start", str(metrics.nav_start)),
        ("nav_end", str(metrics.nav_end)),
        ("absolute_change", str(metrics.absolute_change)),
        ("percent_change", str(metrics.percent_change) if metrics.percent_change is not None else ""),
        ("twr", str(metrics.twr) if metrics.twr is not None else ""),
        ("mwr", str(metrics.mwr) if metrics.mwr is not None else ""),
        (
            "mwr_annualized",
            str(metrics.mwr_annualized) if metrics.mwr_annualized is not None else "",
        ),
        ("contributions", str(metrics.contributions)),
        ("withdrawals", str(metrics.withdrawals)),
        ("net_flow", str(metrics.net_flow)),
        ("realized_pnl_change", str(metrics.realized_pnl_change)),
    ]

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["metric", "value"])
        writer.writerows(rows)
        writer.writerow([])
        writer.writerow(["period_end", "sub_period_return"])
        for ts, value in metrics.sub_periods:
            writer.writerow([ts.isoformat(), str(value)])


def _print_position_report(snapshot_at: datetime, rows: Sequence[PositionPnlRow], base_currency: str) -> None:
    summary = _summarize_positions(rows)
    summary_table = [
        ("Snapshot", snapshot_at.isoformat()),
        ("Positions", str(summary["count"])),
        ("Total Value", _fmt_currency(summary["value"], base_currency)),
        ("Total Cost", _fmt_currency(summary["cost"], base_currency)),
        ("Unrealized PnL", _fmt_currency(summary["unrealized"], base_currency)),
        ("Simple Return", _fmt_percent(summary["simple_return"])),
        ("Hourly Return", _fmt_percent(summary["hourly_return"])),
    ]
    print(_render_table(["Metric", "Value"], summary_table))

    winners = _rank_positions(rows, top=True, count=5)
    losers = _rank_positions(rows, top=False, count=5)

    if winners:
        print()
        print("Top performers")
        print(
            _render_table(
                ["Symbol", "Value", "Unrealized", "Return", "1h Return"],
                [
                    [
                        row.symbol,
                        _fmt_currency(row.value_eur, base_currency),
                        _fmt_currency(row.unrealized_eur, base_currency),
                        _fmt_percent(row.simple_return),
                        _fmt_percent(row.hourly_return),
                    ]
                    for row in winners
                ],
            )
        )

    if losers:
        print()
        print("Bottom performers")
        print(
            _render_table(
                ["Symbol", "Value", "Unrealized", "Return", "1h Return"],
                [
                    [
                        row.symbol,
                        _fmt_currency(row.value_eur, base_currency),
                        _fmt_currency(row.unrealized_eur, base_currency),
                        _fmt_percent(row.simple_return),
                        _fmt_percent(row.hourly_return),
                    ]
                    for row in losers
                ],
            )
        )


def _write_position_csv(snapshot_at: datetime, rows: Sequence[PositionPnlRow], path: Path) -> None:
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        summary = _summarize_positions(rows)
        writer.writerow(["metric", "value"])
        writer.writerow(["snapshot_at", snapshot_at.isoformat()])
        writer.writerow(["positions", summary["count"]])
        writer.writerow(["total_value_eur", str(summary["value"])])
        writer.writerow(["total_cost_eur", str(summary["cost"])])
        writer.writerow(["unrealized_eur", str(summary["unrealized"])])
        writer.writerow(
            ["simple_return", str(summary["simple_return"]) if summary["simple_return"] is not None else ""]
        )
        writer.writerow(
            ["hourly_return", str(summary["hourly_return"]) if summary["hourly_return"] is not None else ""]
        )
        writer.writerow([])

        winners = _rank_positions(rows, top=True, count=5)
        losers = _rank_positions(rows, top=False, count=5)

        if winners:
            writer.writerow(["top_performers"])
            writer.writerow(["symbol", "value_eur", "unrealized_eur", "simple_return", "hourly_return"])
            for row in winners:
                writer.writerow(
                    [
                        row.symbol,
                        str(row.value_eur) if row.value_eur is not None else "",
                        str(row.unrealized_eur) if row.unrealized_eur is not None else "",
                        str(row.simple_return) if row.simple_return is not None else "",
                        str(row.hourly_return) if row.hourly_return is not None else "",
                    ]
                )
            writer.writerow([])

        if losers:
            writer.writerow(["bottom_performers"])
            writer.writerow(["symbol", "value_eur", "unrealized_eur", "simple_return", "hourly_return"])
            for row in losers:
                writer.writerow(
                    [
                        row.symbol,
                        str(row.value_eur) if row.value_eur is not None else "",
                        str(row.unrealized_eur) if row.unrealized_eur is not None else "",
                        str(row.simple_return) if row.simple_return is not None else "",
                        str(row.hourly_return) if row.hourly_return is not None else "",
                    ]
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
                try:
                    start_date = _parse_optional_date(args.backfill_start_date)
                    end_date = _parse_optional_date(args.backfill_end_date)
                except ValueError as exc:
                    logging.getLogger(__name__).error(str(exc))
                    return 1
                days = None if start_date else args.backfill_days
                _run_backfill_after_import(
                    pool,
                    config,
                    days,
                    start_date=start_date,
                    end_date=end_date,
                )
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
                try:
                    start_date = _parse_optional_date(args.backfill_start_date)
                    end_date = _parse_optional_date(args.backfill_end_date)
                except ValueError as exc:
                    logging.getLogger(__name__).error(str(exc))
                    return 1
                days = None if start_date else args.backfill_days
                _run_backfill_after_import(
                    pool,
                    config,
                    days,
                    start_date=start_date,
                    end_date=end_date,
                )
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
                try:
                    start_date = _parse_optional_date(args.backfill_start_date)
                    end_date = _parse_optional_date(args.backfill_end_date)
                except ValueError as exc:
                    logging.getLogger(__name__).error(str(exc))
                    return 1
                days = None if start_date else args.backfill_days
                _run_backfill_after_import(
                    pool,
                    config,
                    days,
                    start_date=start_date,
                    end_date=end_date,
                )
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

        if args.command == "returns-report":
            ensure_schema(pool)
            try:
                start_at = _parse_timestamp(args.start)
                end_at = _parse_timestamp(args.end)
            except ValueError as exc:
                logging.getLogger(__name__).error(str(exc))
                return 1

            calculator = PerformanceCalculator(pool, config.snapshot.base_currency)
            try:
                metrics = calculator.compute_account_returns(args.account, start_at, end_at)
            except ValueError as exc:
                logging.getLogger(__name__).error(str(exc))
                return 1

            _print_returns_report(metrics, config.snapshot.base_currency)
            if args.csv_path:
                _write_returns_csv(metrics, Path(args.csv_path))
            return 0

        if args.command == "position-pnl":
            ensure_schema(pool)
            as_of = None
            if args.as_of:
                try:
                    as_of = _parse_timestamp(args.as_of)
                except ValueError as exc:
                    logging.getLogger(__name__).error(str(exc))
                    return 1

            calculator = PerformanceCalculator(pool, config.snapshot.base_currency)
            try:
                snapshot_at, rows = calculator.build_position_report(
                    args.account,
                    as_of=as_of,
                    hourly_window=args.hours,
                )
            except ValueError as exc:
                logging.getLogger(__name__).error(str(exc))
                return 1

            _print_position_report(snapshot_at, rows, config.snapshot.base_currency)
            if args.csv_path:
                _write_position_csv(snapshot_at, rows, Path(args.csv_path))
            return 0

        if args.command == "backfill":
            ensure_schema(pool)
            try:
                start_date = _parse_optional_date(args.start_date)
                end_date = _parse_optional_date(args.end_date)
            except ValueError as exc:
                logging.getLogger(__name__).error(str(exc))
                return 1
            days = args.days if start_date is None else None
            BackfillService(config.snapshot, pool).run(
                days=days,
                start=start_date,
                end=end_date,
                include_prices=not args.fx_only,
                include_fx=not args.prices_only,
                include_snapshots=args.snapshots,
            )
            return 0

        if args.command == "instrument-update":
            ensure_schema(pool)
            InstrumentMetadataUpdater(config.instrument, pool).run(
                include_all=args.all
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
