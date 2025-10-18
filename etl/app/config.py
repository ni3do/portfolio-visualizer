import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _read_file(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Secret file {path} is empty")
    return value


def _resolve(name: str, required: bool = True) -> Optional[str]:
    direct = os.getenv(name)
    if direct:
        return direct.strip()
    file_key = f"{name}_FILE"
    file_path = os.getenv(file_key)
    if file_path:
        return _read_file(file_path)
    if required:
        raise RuntimeError(f"Missing configuration for {name} (or {file_key})")
    return None


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class FlexConfig:
    token: str
    query_id: str
    archive_dir: Path


@dataclass(frozen=True)
class PriceSettings:
    batch_size: int
    history_period: str
    history_interval: str
    source: str


@dataclass(frozen=True)
class FxSettings:
    base_currency: str
    history_period: str
    history_interval: str
    source: str


@dataclass(frozen=True)
class SnapshotSettings:
    base_currency: str
    timezone: str


@dataclass(frozen=True)
class InstrumentMetadataSettings:
    source: str
    sleep_seconds: float


@dataclass(frozen=True)
class AppConfig:
    log_level: str
    run_mode: str
    db: DatabaseConfig
    flex: FlexConfig
    price: PriceSettings
    fx: FxSettings
    snapshot: SnapshotSettings
    instrument: InstrumentMetadataSettings


def load_config() -> AppConfig:
    db = DatabaseConfig(
        host=os.getenv("PORTFOLIO_DB_HOST", "postgres"),
        port=int(os.getenv("PORTFOLIO_DB_PORT", "5432")),
        name=os.getenv("PORTFOLIO_DB_NAME", "portfolio"),
        user=_resolve("PORTFOLIO_DB_USER"),
        password=_resolve("PORTFOLIO_DB_PASSWORD"),
    )

    archive_dir = Path(os.getenv("FLEX_ARCHIVE_DIR", "/data/flex_archive"))
    archive_dir.mkdir(parents=True, exist_ok=True)

    flex = FlexConfig(
        token=_resolve("IBKR_FLEX_TOKEN"),
        query_id=_resolve("IBKR_FLEX_QUERY_ID"),
        archive_dir=archive_dir,
    )

    price = PriceSettings(
        batch_size=int(os.getenv("PRICE_BATCH_SIZE", "16")),
        history_period=os.getenv("PRICE_HISTORY_PERIOD", "5d"),
        history_interval=os.getenv("PRICE_HISTORY_INTERVAL", "15m"),
        source=os.getenv("PRICE_SOURCE", "yfinance"),
    )

    snapshot_base = os.getenv("SNAPSHOT_BASE_CCY", "EUR")
    snapshot = SnapshotSettings(
        base_currency=snapshot_base,
        timezone=os.getenv("SNAPSHOT_TIMEZONE", "Europe/Amsterdam"),
    )

    fx = FxSettings(
        base_currency=os.getenv("FX_BASE_CCY", snapshot_base),
        history_period=os.getenv("FX_HISTORY_PERIOD", "5d"),
        history_interval=os.getenv("FX_HISTORY_INTERVAL", "1d"),
        source=os.getenv("FX_SOURCE", "yfinance"),
    )

    instrument = InstrumentMetadataSettings(
        source=os.getenv("INSTRUMENT_SOURCE", "yfinance"),
        sleep_seconds=float(os.getenv("INSTRUMENT_SLEEP_SECONDS", "1.0")),
    )

    log_level = os.getenv("ETL_LOG_LEVEL", "INFO").upper()
    run_mode = os.getenv("ETL_RUN_MODE", "scheduler")

    return AppConfig(
        log_level=log_level,
        run_mode=run_mode,
        db=db,
        flex=flex,
        price=price,
        fx=fx,
        snapshot=snapshot,
        instrument=instrument,
    )
