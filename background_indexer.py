from __future__ import annotations

import logging
import os
import threading
import time

from dotenv import load_dotenv

load_dotenv()

from app_config import CITY_NAMES, CITY_SLUGS
from listing_cache import init_db, mark_stale_inactive, stats, upsert_listings
from providers.olx_provider import get_olx_provider

CATEGORIES = ("apartment", "house", "room", "office")
INDEXER_ENABLED = os.getenv("OLX_INDEXER_ENABLED", "1").strip().lower() not in ("0", "false", "no")
INDEXER_MAX_PAGES = int(os.getenv("OLX_INDEXER_MAX_PAGES", "5"))
INDEXER_INTERVAL_SECONDS = int(os.getenv("OLX_INDEXER_INTERVAL_SECONDS", "900"))
INDEXER_CITY_PAUSE_SECONDS = float(os.getenv("OLX_INDEXER_CITY_PAUSE_SECONDS", "3"))
STALE_AFTER_SECONDS = int(os.getenv("OLX_STALE_AFTER_SECONDS", "86400"))

_started = False
_lock = threading.Lock()
_status_lock = threading.Lock()
_status = {
    "enabled": INDEXER_ENABLED,
    "running": False,
    "city": "",
    "category": "",
    "source": "",
    "last_saved": 0,
    "last_error": "",
    "cycle_started_at": 0,
    "updated_at": 0,
}


def start_background_indexer() -> None:
    global _started
    if not INDEXER_ENABLED:
        logging.info("[INDEXER] disabled by OLX_INDEXER_ENABLED")
        return
    with _lock:
        if _started:
            return
        _started = True
    init_db()
    t = threading.Thread(target=_indexer_loop, name="olx-indexer", daemon=True)
    t.start()
    logging.info("[INDEXER] started")


def _indexer_loop() -> None:
    while True:
        try:
            _set_status(running=True, cycle_started_at=int(time.time()), last_error="")
            run_index_once()
            logging.info("[INDEXER] cycle done stats=%s", stats())
        except Exception:
            _set_status(last_error="cycle error")
            logging.exception("[INDEXER] cycle error")
        finally:
            _set_status(running=False)
        time.sleep(INDEXER_INTERVAL_SECONDS)


def run_index_once() -> None:
    for city in CITY_NAMES:
        for category in CATEGORIES:
            city_slug = CITY_SLUGS.get(city, "")
            query = {
                "category": category,
                "city": city,
                "city_slug": city_slug,
                "districts": [],
                "price_from": None,
                "price_to": None,
                "no_fee": True,
                "sort": "newest",
                "max_pages": INDEXER_MAX_PAGES,
            }
            provider = get_olx_provider(category)
            source = getattr(provider, "source", "olx")
            _set_status(city=city, category=category, source=source, running=True)
            try:
                items = provider.search(query) or []
                saved = upsert_listings(category, city, items)
                stale = mark_stale_inactive(category, city, STALE_AFTER_SECONDS)
                _set_status(last_saved=saved, last_error="")
                logging.info("[INDEXER] %s %s %s saved=%s stale=%s", source, category, city, saved, stale)
            except Exception:
                _set_status(last_error=f"{source} {category} {city} failed")
                logging.exception("[INDEXER] %s %s %s failed", source, category, city)
            time.sleep(INDEXER_CITY_PAUSE_SECONDS)


def get_indexer_status() -> dict:
    with _status_lock:
        return dict(_status)


def _set_status(**kwargs) -> None:
    with _status_lock:
        _status.update(kwargs)
        _status["updated_at"] = int(time.time())
