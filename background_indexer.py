from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Deque

from dotenv import load_dotenv

load_dotenv()

from app_config import CITY_NAMES, CITY_SLUGS
from listing_cache import init_db, mark_stale_inactive, purge_inactive, stats, upsert_listings
from providers.olx_provider import get_olx_provider

CATEGORIES = ("apartment", "house", "room", "office")


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logging.warning("[INDEXER] bad %s=%r, using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logging.warning("[INDEXER] bad %s=%r, using %s", name, raw, default)
        return default


INDEXER_ENABLED = (os.getenv("OLX_INDEXER_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no")
INDEXER_MAX_PAGES = _env_int("OLX_INDEXER_MAX_PAGES", 3)
INDEXER_HOT_MAX_PAGES = _env_int("OLX_INDEXER_HOT_MAX_PAGES", 15)
INDEXER_WORKERS = max(1, min(_env_int("OLX_INDEXER_WORKERS", 2), 4))
INDEXER_JOB_PAUSE_SECONDS = _env_float("OLX_INDEXER_CITY_PAUSE_SECONDS", 1.5)
INDEXER_IDLE_SECONDS = _env_float("OLX_INDEXER_IDLE_SECONDS", 5)
INDEXER_MIN_REFRESH_SECONDS = _env_int("OLX_INDEXER_MIN_REFRESH_SECONDS", 180)
HOT_JOB_TTL_SECONDS = _env_int("OLX_INDEXER_HOT_TTL_SECONDS", 7200)
STALE_AFTER_SECONDS = _env_int("OLX_STALE_AFTER_SECONDS", 86400)
PURGE_AFTER_SECONDS = _env_int("OLX_PURGE_AFTER_SECONDS", 604800)
STARTUP_CITIES = tuple(
    city.strip()
    for city in (
        os.getenv(
            "OLX_INDEXER_STARTUP_CITIES",
            "Київ,Львів,Одеса,Дніпро,Харків,Вінниця,Івано-Франківськ,Тернопіль,Черкаси,Полтава",
        )
        or ""
    ).split(",")
    if city.strip() in CITY_SLUGS
)

Job = tuple[str, str, int]  # category, city, max_pages

_started = False
_lock = threading.RLock()
_status_lock = threading.Lock()
_priority_jobs: Deque[Job] = deque()
_queued_jobs: set[tuple[str, str]] = set()
_active_jobs: set[tuple[str, str]] = set()
_hot_jobs: dict[tuple[str, str], int] = {}
_last_indexed: dict[tuple[str, str], int] = {}
_round_robin_jobs: tuple[tuple[str, str], ...] = tuple(
    (category, city) for city in CITY_NAMES for category in CATEGORIES
)
_round_robin_pos = 0

_status = {
    "enabled": INDEXER_ENABLED,
    "running": False,
    "city": "",
    "category": "",
    "source": "",
    "last_saved": 0,
    "last_stale": 0,
    "last_purged": 0,
    "last_error": "",
    "mode": "",
    "queue_size": 0,
    "workers": INDEXER_WORKERS,
    "max_pages": INDEXER_MAX_PAGES,
    "hot_max_pages": INDEXER_HOT_MAX_PAGES,
    "cycle_started_at": 0,
    "last_indexed_at": 0,
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
    _enqueue_startup_jobs()
    for worker_id in range(INDEXER_WORKERS):
        t = threading.Thread(target=_indexer_loop, args=(worker_id,), name=f"olx-indexer-{worker_id + 1}", daemon=True)
        t.start()
    logging.info("[INDEXER] started workers=%s startup_jobs=%s", INDEXER_WORKERS, len(_priority_jobs))


def enqueue_index_job(category: str | None, city: str | None, max_pages: int | None = None) -> None:
    if not INDEXER_ENABLED:
        return
    category = (category or "apartment").strip().lower()
    city = (city or "").strip()
    if category not in CATEGORIES or not city or city not in CITY_SLUGS:
        return

    key = (category, city)
    now = int(time.time())
    with _lock:
        _hot_jobs[key] = now
        if now - _last_indexed.get(key, 0) < INDEXER_MIN_REFRESH_SECONDS:
            return
        if key in _queued_jobs:
            return
        _priority_jobs.appendleft((category, city, max_pages or INDEXER_HOT_MAX_PAGES))
        _queued_jobs.add(key)
        _set_status(queue_size=len(_priority_jobs))


def run_index_once(limit: int | None = None) -> None:
    count = 0
    for city in CITY_NAMES:
        for category in CATEGORIES:
            _index_job(category, city, INDEXER_MAX_PAGES, mode="manual")
            count += 1
            if limit and count >= limit:
                return
            time.sleep(INDEXER_JOB_PAUSE_SECONDS)


def get_indexer_status() -> dict:
    with _status_lock:
        data = dict(_status)
    with _lock:
        data["queue_size"] = len(_priority_jobs)
        data["hot_jobs"] = len(_hot_jobs)
        data["active_jobs"] = len(_active_jobs)
    return data


def _indexer_loop(worker_id: int = 0) -> None:
    while True:
        job = _take_priority_job() or _pick_hot_job() or _next_round_robin_job()
        if not job:
            time.sleep(INDEXER_IDLE_SECONDS)
            continue

        category, city, max_pages = job
        key = (category, city)
        with _lock:
            if key in _active_jobs:
                time.sleep(0.2)
                continue
            _active_jobs.add(key)
        mode = "priority" if (category, city) in _hot_jobs else "background"
        try:
            _index_job(category, city, max_pages, mode=mode)
        except Exception:
            logging.exception("[INDEXER:%s] unexpected job error", worker_id + 1)
        finally:
            with _lock:
                _active_jobs.discard(key)
        time.sleep(INDEXER_JOB_PAUSE_SECONDS)


def _enqueue_startup_jobs() -> None:
    if not STARTUP_CITIES:
        return
    now = int(time.time())
    with _lock:
        for city in reversed(STARTUP_CITIES):
            for category in reversed(CATEGORIES):
                key = (category, city)
                if key in _queued_jobs:
                    continue
                _priority_jobs.appendleft((category, city, INDEXER_HOT_MAX_PAGES))
                _queued_jobs.add(key)
                _hot_jobs[key] = now
        _set_status(queue_size=len(_priority_jobs))


def _take_priority_job() -> Job | None:
    now = int(time.time())
    with _lock:
        while _priority_jobs:
            category, city, max_pages = _priority_jobs.popleft()
            key = (category, city)
            _queued_jobs.discard(key)
            if key in _active_jobs:
                continue
            if now - _last_indexed.get(key, 0) >= INDEXER_MIN_REFRESH_SECONDS:
                _set_status(queue_size=len(_priority_jobs))
                return category, city, max_pages
        _set_status(queue_size=0)
    return None


def _pick_hot_job() -> Job | None:
    now = int(time.time())
    with _lock:
        for key, requested_at in list(_hot_jobs.items()):
            if now - requested_at > HOT_JOB_TTL_SECONDS:
                _hot_jobs.pop(key, None)

        candidates = [
            (now - _last_indexed.get(key, 0), requested_at, key)
            for key, requested_at in _hot_jobs.items()
            if key not in _active_jobs
            if now - _last_indexed.get(key, 0) >= INDEXER_MIN_REFRESH_SECONDS
        ]
        if not candidates:
            return None
        candidates.sort(reverse=True)
        category, city = candidates[0][2]
        return category, city, INDEXER_HOT_MAX_PAGES


def _next_round_robin_job() -> Job | None:
    global _round_robin_pos
    if not _round_robin_jobs:
        return None

    now = int(time.time())
    with _lock:
        for _ in range(len(_round_robin_jobs)):
            category, city = _round_robin_jobs[_round_robin_pos]
            _round_robin_pos = (_round_robin_pos + 1) % len(_round_robin_jobs)
            key = (category, city)
            if key not in _active_jobs and now - _last_indexed.get(key, 0) >= INDEXER_MIN_REFRESH_SECONDS:
                return category, city, INDEXER_MAX_PAGES
    return None


def _index_job(category: str, city: str, max_pages: int, mode: str) -> None:
    city_slug = CITY_SLUGS.get(city, "")
    if not city_slug:
        return

    query = {
        "category": category,
        "city": city,
        "city_slug": city_slug,
        "districts": [],
        "price_from": None,
        "price_to": None,
        "no_fee": True,
        "sort": "newest",
        "max_pages": max(1, int(max_pages or INDEXER_MAX_PAGES)),
    }

    provider = get_olx_provider(category)
    source = getattr(provider, "source", "olx")
    _set_status(
        running=True,
        city=city,
        category=category,
        source=source,
        mode=mode,
        cycle_started_at=int(time.time()),
        last_error="",
    )

    key = (category, city)
    try:
        items = provider.search(query) or []
        saved = upsert_listings(category, city, items)
        stale = mark_stale_inactive(category, city, STALE_AFTER_SECONDS)
        purged = purge_inactive(PURGE_AFTER_SECONDS)
        with _lock:
            _last_indexed[key] = int(time.time())
        _set_status(
            running=False,
            last_saved=saved,
            last_stale=stale,
            last_purged=purged,
            last_indexed_at=int(time.time()),
            last_error="",
        )
        logging.info(
            "[INDEXER] %s %s %s saved=%s stale=%s purged=%s mode=%s stats=%s",
            source,
            category,
            city,
            saved,
            stale,
            purged,
            mode,
            stats(),
        )
    except Exception as exc:
        with _lock:
            _last_indexed[key] = int(time.time())
        _set_status(running=False, last_error=f"{source} {category} {city} failed: {exc}")
        logging.exception("[INDEXER] %s %s %s failed", source, category, city)


def _set_status(**kwargs) -> None:
    with _status_lock:
        _status.update(kwargs)
        _status["updated_at"] = int(time.time())
