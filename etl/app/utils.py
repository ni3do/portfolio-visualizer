from __future__ import annotations

import os
import shutil
from pathlib import Path


def clear_yfinance_cache(default_cache: Path | None = None) -> Path:
    """
    Remove yfinance cache directories and return the active cache path.
    """
    cache_dir = Path(os.getenv("YF_CACHE_DIR", "/tmp/yfinance_cache"))
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["YF_CACHE_DIR"] = str(cache_dir)

    default_cache = default_cache or (Path.home() / ".cache" / "yfinance")
    shutil.rmtree(default_cache, ignore_errors=True)

    return cache_dir
