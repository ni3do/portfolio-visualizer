import logging
import os


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if os.getenv("ETL_DEBUG_SQL") == "1":
        logging.getLogger("psycopg").setLevel(logging.DEBUG)
