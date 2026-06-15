from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).with_name("olx_cache.sqlite3")
_LOCK = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS listings (
                source TEXT NOT NULL,
                listing_id TEXT NOT NULL,
                category TEXT NOT NULL,
                city TEXT,
                district TEXT,
                title TEXT,
                price_uah INTEGER,
                url TEXT,
                photos_json TEXT,
                rooms INTEGER,
                area_total REAL,
                floor INTEGER,
                is_no_fee INTEGER,
                allows_pets INTEGER,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT,
                PRIMARY KEY (source, listing_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_lookup ON listings(category, city, active, price_uah)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_seen ON listings(category, city, last_seen)")


def _listing_id(item: Any) -> str:
    return str(getattr(item, "id", None) or getattr(item, "url", None) or "")


def upsert_listings(category: str, city: str, listings: list[Any]) -> int:
    if not listings:
        return 0
    init_db()
    now = int(time.time())
    changed = 0
    with _LOCK, _connect() as conn:
        for item in listings:
            listing_id = _listing_id(item)
            if not listing_id:
                continue
            source = str(getattr(item, "source", "olx") or "olx")
            photos = getattr(item, "photos", None) or []
            raw = item.to_dict() if hasattr(item, "to_dict") else {}
            conn.execute(
                """
                INSERT INTO listings (
                    source, listing_id, category, city, district, title, price_uah, url,
                    photos_json, rooms, area_total, floor, is_no_fee, allows_pets,
                    first_seen, last_seen, active, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(source, listing_id) DO UPDATE SET
                    category=excluded.category,
                    city=excluded.city,
                    district=excluded.district,
                    title=excluded.title,
                    price_uah=excluded.price_uah,
                    url=excluded.url,
                    photos_json=CASE
                        WHEN excluded.photos_json IS NOT NULL AND excluded.photos_json!='[]'
                        THEN excluded.photos_json
                        ELSE listings.photos_json
                    END,
                    rooms=excluded.rooms,
                    area_total=excluded.area_total,
                    floor=excluded.floor,
                    is_no_fee=excluded.is_no_fee,
                    allows_pets=excluded.allows_pets,
                    last_seen=excluded.last_seen,
                    active=1,
                    raw_json=excluded.raw_json
                """,
                (
                    source,
                    listing_id,
                    category,
                    getattr(item, "city", None) or city,
                    getattr(item, "district", None),
                    getattr(item, "title", None) or "Без назви",
                    getattr(item, "price_uah", None),
                    getattr(item, "url", None) or "",
                    json.dumps(photos, ensure_ascii=False),
                    getattr(item, "rooms", None),
                    getattr(item, "area_total", None),
                    getattr(item, "floor", None),
                    _bool_to_int(getattr(item, "is_no_fee", None)),
                    _bool_to_int(getattr(item, "allows_pets", None)),
                    now,
                    now,
                    json.dumps(raw, ensure_ascii=False, default=str),
                ),
            )
            changed += 1
    return changed


def mark_stale_inactive(category: str, city: str, older_than_seconds: int = 86400) -> int:
    init_db()
    cutoff = int(time.time()) - older_than_seconds
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "UPDATE listings SET active=0 WHERE category=? AND city=? AND last_seen<?",
            (category, city, cutoff),
        )
        return cur.rowcount or 0


def purge_inactive(older_than_seconds: int = 604800) -> int:
    init_db()
    cutoff = int(time.time()) - older_than_seconds
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM listings WHERE active=0 AND last_seen<?",
            (cutoff,),
        )
        return cur.rowcount or 0


def query_cards(
    *,
    category: str,
    city: str | None = None,
    districts: list[str] | None = None,
    price_from: int | None = None,
    price_to: int | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    init_db()
    where = ["active=1", "category=?"]
    params: list[Any] = [category]
    if city:
        where.append("(city=? OR city IS NULL OR city='')")
        params.append(city)
    if price_from is not None:
        where.append("(price_uah IS NULL OR price_uah>=?)")
        params.append(price_from)
    if price_to is not None:
        where.append("(price_uah IS NULL OR price_uah<=?)")
        params.append(price_to)

    sql = f"""
        SELECT * FROM listings
        WHERE {' AND '.join(where)}
        ORDER BY last_seen DESC
        LIMIT ?
    """
    params.append(limit * 3)

    district_norm = {_norm(d) for d in (districts or []) if d}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    with _LOCK, _connect() as conn:
        for row in conn.execute(sql, params):
            if district_norm:
                rd = _norm(row["district"] or "")
                title = _norm(row["title"] or "")
                if rd not in district_norm and not any(d in title for d in district_norm):
                    continue
            key = row["listing_id"] or row["url"]
            if not key or key in seen:
                continue
            seen.add(key)
            photos = _loads(row["photos_json"], [])
            price = row["price_uah"]
            out.append(
                {
                    "title": row["title"] or "Без назви",
                    "price": f"{int(price):,} грн".replace(",", " ") if price else "—",
                    "link": row["url"] or "",
                    "img_urls": [_normalize_photo_url(p) for p in photos if p][:6],
                    "_key": key,
                }
            )
            if len(out) >= limit:
                break
    return out


def stats() -> dict[str, Any]:
    init_db()
    with _LOCK, _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM listings WHERE active=1").fetchone()[0]
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n FROM listings WHERE active=1 GROUP BY category ORDER BY category"
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM listings WHERE active=1 GROUP BY source ORDER BY source"
        ).fetchall()
        photo_rows = conn.execute(
            """
            SELECT category, COUNT(*) AS n
            FROM listings
            WHERE active=1 AND photos_json IS NOT NULL AND photos_json!='[]'
            GROUP BY category
            ORDER BY category
            """
        ).fetchall()
    return {
        "active_total": total,
        "by_category": {r["category"]: r["n"] for r in rows},
        "by_source": {r["source"]: r["n"] for r in source_rows},
        "with_photo": {r["category"]: r["n"] for r in photo_rows},
    }


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except Exception:
        return default


def _bool_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _norm(value: str) -> str:
    return str(value or "").strip().lower().replace("’", "'")


def _normalize_photo_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    url = url.replace("https://ireland.apollo.olxcdn.com:443/", "https://ireland.apollo.olxcdn.com/")
    url = url.replace(";q=50", ";q=90").replace(";q=75", ";q=90")
    return url
