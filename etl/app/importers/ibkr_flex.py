from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from psycopg_pool import ConnectionPool

from .. import db
from ..config import FlexConfig
from ..flex import FlexClient, extract_entities, parse_sections

logger = logging.getLogger(__name__)


class FlexImporter:
    """Importer that ingests IBKR Flex statements (remote or local)."""

    def __init__(self, config: FlexConfig):
        self.config = config
        self.client = FlexClient(token=config.token, query_id=config.query_id)

    def run(
        self,
        pool: ConnectionPool,
        *,
        statement_content: str | None = None,
        reference_code: Optional[str] = None,
    ) -> None:
        if statement_content is None:
            logger.info("Fetching Flex statement via Web Service")
            statement = self.client.fetch_statement()
            content = statement.content
            reference = statement.reference_code
            self._archive(content, reference)
        else:
            logger.info("Importing Flex statement from local content")
            content = statement_content
            reference = reference_code or f"manual_{int(time.time())}"

        instruments, transactions, cash_movements, fx_rates = self._parse(content)
        logger.info(
            "Parsed Flex statement -> %d instruments, %d transactions, %d cash movements, %d fx rates",
            len(instruments),
            len(transactions),
            len(cash_movements),
            len(fx_rates),
        )

        db.ensure_schema(pool)
        if instruments:
            db.upsert_instruments(pool, instruments)
        if transactions:
            db.upsert_transactions(pool, transactions)
        if cash_movements:
            db.upsert_cash_movements(pool, cash_movements)
        if fx_rates:
            db.upsert_fx_rates(pool, fx_rates)

        logger.info("Flex import completed successfully (reference %s)", reference)

    def _parse(self, content: str):
        sections = parse_sections(content)
        return extract_entities(sections)

    def _archive(self, content: str, reference_code: str) -> None:
        archive_dir = self.config.archive_dir
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / f"flex_{reference_code}.csv"
        path.write_text(content, encoding="utf-8")
        logger.info("Archived Flex statement at %s", path)


def read_statement_file(file_path: Path) -> str:
    """Read a Flex statement from disk (supports zipped CSV)."""
    data = file_path.read_bytes()
    if data.startswith(b"PK"):
        import zipfile
        from io import BytesIO

        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = zf.namelist()
            if not names:
                raise ValueError("Zip archive was empty")
            with zf.open(names[0]) as fh:
                return fh.read().decode("utf-8")
    return data.decode("utf-8")
