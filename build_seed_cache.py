from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import logging
import os
import shutil
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a bundled OLX seed cache for Flat Find.")
    parser.add_argument("--cities", default="", help="Comma-separated city list. Empty = all configured cities.")
    parser.add_argument("--categories", default="apartment,house,room,office", help="Comma-separated categories.")
    parser.add_argument("--max-pages", type=int, default=20, help="Max pages per city/category crawl.")
    parser.add_argument("--pause", type=float, default=1.0, help="Pause between city/category jobs.")
    parser.add_argument("--workers", type=int, default=1, help="Parallel OLX workers for city/category jobs.")
    parser.add_argument(
        "--output-db",
        default="seed_data/olx_cache_seed.sqlite3",
        help="Output SQLite database path, relative to project root.",
    )
    parser.add_argument(
        "--gzip-output",
        action="store_true",
        help="Also create .gz archive next to the SQLite file.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    base_dir = Path(__file__).resolve().parent
    output_db = (base_dir / args.output_db).resolve()
    output_db.parent.mkdir(parents=True, exist_ok=True)

    os.environ["OLX_CACHE_DB_PATH"] = str(output_db)

    from app_config import CITY_NAMES, CITY_SLUGS
    from listing_cache import init_db, mark_stale_inactive, purge_inactive, stats, upsert_listings
    from providers.olx_provider import get_olx_provider

    categories = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    cities = [c.strip() for c in args.cities.split(",") if c.strip()] if args.cities else list(CITY_NAMES)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if output_db.exists():
        output_db.unlink()
    init_db()

    total_saved = 0
    jobs_done = 0
    started_at = time.time()
    jobs: list[tuple[str, str, str]] = []
    for city in cities:
        city_slug = CITY_SLUGS.get(city)
        if not city_slug:
            logging.warning("[SEED] skip unknown city: %s", city)
            continue
        for category in categories:
            jobs.append((city, city_slug, category))

    def _crawl_job(job: tuple[str, str, str]) -> tuple[str, str, int, int, int, int]:
        city, city_slug, category = job
        provider = get_olx_provider(category)
        query = {
            "category": category,
            "city": city,
            "city_slug": city_slug,
            "districts": [],
            "price_from": None,
            "price_to": None,
            "no_fee": True,
            "sort": "newest",
            "max_pages": max(1, int(args.max_pages)),
        }
        logging.info("[SEED] %s / %s pages=%s", city, category, args.max_pages)
        items = provider.search(query) or []
        saved = upsert_listings(category, city, items)
        stale = mark_stale_inactive(category, city, older_than_seconds=86400 * 365)
        purged = purge_inactive(older_than_seconds=86400 * 365)
        time.sleep(max(args.pause, 0.0))
        return city, category, len(items), saved, stale, purged

    workers = max(1, int(args.workers))
    if workers > 1:
        logging.warning("[SEED] workers=%s requested, but SQLite seed build is forced to 1 writer for data safety", workers)
        workers = 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(_crawl_job, job): job for job in jobs}
        for future in concurrent.futures.as_completed(future_map):
            city, _, category = future_map[future]
            try:
                result_city, result_category, item_count, saved, stale, purged = future.result()
                total_saved += saved
                jobs_done += 1
                logging.info(
                    "[SEED] done %s / %s items=%s saved=%s stale=%s purged=%s stats=%s",
                    result_city,
                    result_category,
                    item_count,
                    saved,
                    stale,
                    purged,
                    stats(),
                )
            except Exception:
                logging.exception("[SEED] failed %s / %s", city, category)

    elapsed = round(time.time() - started_at, 1)
    logging.info("[SEED] finished jobs=%s saved=%s elapsed=%ss db=%s", jobs_done, total_saved, elapsed, output_db)

    if args.gzip_output:
        gzip_path = output_db.with_suffix(output_db.suffix + ".gz")
        with output_db.open("rb") as src, gzip.open(gzip_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        logging.info("[SEED] gzip archive written: %s", gzip_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
