from __future__ import annotations

import gzip
import logging
import os
import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DB = BASE_DIR / "olx_cache.sqlite3"
SEED_ARCHIVE = BASE_DIR / "seed_data" / "olx_cache_seed.sqlite3.gz"
MIN_DB_SIZE_BYTES = 32 * 1024


def ensure_seed_cache() -> bool:
    """
    Restores the runtime OLX cache from a bundled seed archive if the local DB
    is missing or obviously empty. This makes fresh Railway deploys boot with
    an already usable cache instead of starting from zero.
    """
    runtime_db = Path(os.getenv("OLX_RUNTIME_DB_PATH", str(RUNTIME_DB)))
    seed_archive = Path(os.getenv("OLX_SEED_ARCHIVE_PATH", str(SEED_ARCHIVE)))

    if runtime_db.exists() and runtime_db.stat().st_size >= MIN_DB_SIZE_BYTES:
        return False
    if not seed_archive.exists():
        logging.info("[SEED CACHE] archive not found: %s", seed_archive)
        return False

    runtime_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = runtime_db.with_suffix(".sqlite3.tmp")
    try:
        with gzip.open(seed_archive, "rb") as src, tmp_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp_size = tmp_path.stat().st_size if tmp_path.exists() else 0
        if tmp_size < MIN_DB_SIZE_BYTES:
            raise RuntimeError(f"seed cache looks too small: {tmp_size} bytes")
        tmp_path.replace(runtime_db)
        logging.info("[SEED CACHE] restored runtime cache from %s", seed_archive)
        return True
    except Exception:
        logging.exception("[SEED CACHE] failed to restore archive")
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False
