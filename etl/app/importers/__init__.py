"""Importer utilities for one-off data loads."""

from dataclasses import dataclass
from typing import Iterable, Sequence

from ..models import CashMovement, FxRate, Instrument, Transaction


@dataclass
class ImportResult:
    instruments: Sequence[Instrument]
    transactions: Sequence[Transaction]
    cash_movements: Sequence[CashMovement]
    fx_rates: Sequence[FxRate] | None = None


class BaseImporter:
    """Simple interface for importers that populate core tables."""

    def run(self) -> ImportResult:  # pragma: no cover - interface
        raise NotImplementedError
