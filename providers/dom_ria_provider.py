from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from app_config import CITY_TO_OBLAST, DOM_RIA_STATE_IDS
from providers.base import Listing, Provider

BASE_URL = "https://developers.ria.com/dom"
PHOTO_PREFIX = "https://cdn.riastatic.com/photos/"
GEO_CACHE_PATH = Path(__file__).resolve().parent.with_name("dom_ria_geo_cache.json")

CATEGORY_DEFAULTS = {
    "apartment": {"category": 1, "realty_type": 2},
    "room": {"category": 1, "realty_type": 1},
    "house": {"category": 4, "realty_type": None},
    "office": {"category": None, "realty_type": None},
}


class DomRiaProvider(Provider):
    source = "dom_ria"

    def __init__(self, category: str = "apartment", api_key: str | None = None, timeout: int = 20):
        self.kind = (category or "apartment").strip().lower()
        self.api_key = api_key or os.getenv("DOM_RIA_API_KEY", "").strip()
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def search(self, query: dict[str, Any]) -> list[Listing]:
        if not self.enabled:
            return []

        max_pages = max(1, int(query.get("max_pages") or os.getenv("DOM_RIA_MAX_PAGES", "1")))
        max_details = max(1, int(os.getenv("DOM_RIA_MAX_DETAILS_PER_SEARCH", "20")))
        detail_pause = float(os.getenv("DOM_RIA_DETAIL_PAUSE_SECONDS", "0.4"))

        ids: list[int] = []
        for page in range(1, max_pages + 1):
            payload = self._search_payload(query, page=page)
            data = self._get_json(f"{BASE_URL}/search", payload)
            page_ids = data.get("items") if isinstance(data, dict) else []
            if not page_ids:
                break
            ids.extend(int(x) for x in page_ids if str(x).isdigit())
            if len(ids) >= max_details:
                break

        out: list[Listing] = []
        for realty_id in ids[:max_details]:
            info = self._get_json(f"{BASE_URL}/info/{realty_id}", {"api_key": self.api_key})
            if not isinstance(info, dict):
                continue
            item = self._to_listing(info, fallback_city=query.get("city"))
            if item and self._matches_category(info) and self._matches_query(item, query):
                out.append(item)
            time.sleep(detail_pause)
        return out

    def build_url(self, query: dict[str, Any], page: int = 1) -> str:
        return f"{BASE_URL}/search?{urlencode(self._search_payload(query, page=page), doseq=True)}"

    def _search_payload(self, query: dict[str, Any], page: int = 1) -> dict[str, Any]:
        params = {
            "api_key": self.api_key,
            "operation_type": int(os.getenv("DOM_RIA_OPERATION_RENT", "3")),
            "sort": "created_at" if query.get("sort") == "newest" else "upd_d",
            "page": page,
            "exclude_agencies": 1 if query.get("no_fee", True) else 0,
        }
        defaults = CATEGORY_DEFAULTS.get(self.kind, CATEGORY_DEFAULTS["apartment"]).copy()
        category = self._env_int(f"DOM_RIA_{self.kind.upper()}_CATEGORY", defaults.get("category"))
        realty_type = self._env_int(f"DOM_RIA_{self.kind.upper()}_REALTY_TYPE", defaults.get("realty_type"))
        if category:
            params["category"] = category
        if realty_type:
            params["realty_type"] = realty_type

        city = query.get("city")
        state_id = self._state_id(city)
        city_id = self._city_id(city, state_id)
        if state_id:
            params["state_id"] = state_id
        if city_id:
            params["city_id"] = city_id

        district_ids = self._district_ids(city_id, query.get("districts") or [])
        for district_id in district_ids:
            params.setdefault("district_id", [])
            params["district_id"].append(district_id)

        price_from = query.get("price_from")
        price_to = query.get("price_to")
        if price_from is not None:
            params["characteristic[235][from]"] = int(price_from)
        if price_to is not None:
            params["characteristic[235][to]"] = int(price_to)

        rooms = query.get("rooms")
        if rooms:
            nums = sorted(_as_int(x) for x in rooms if _as_int(x))
            if nums:
                params["characteristic[209][from]"] = min(nums)
                params["characteristic[209][to]"] = max(nums)

        area = query.get("area") or query.get("area_total")
        if isinstance(area, dict):
            if area.get("from") is not None:
                params["characteristic[214][from]"] = int(area["from"])
            if area.get("to") is not None:
                params["characteristic[214][to]"] = int(area["to"])
        return params

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        resp = self.session.get(url, params=params, timeout=self.timeout, headers={"accept": "application/json"})
        if resp.status_code == 429:
            raise RuntimeError("DOM.RIA API ліміт запитів вичерпано")
        resp.raise_for_status()
        return resp.json()

    def _state_id(self, city: str | None) -> int | None:
        oblast = CITY_TO_OBLAST.get(city or "")
        return DOM_RIA_STATE_IDS.get(oblast or "")

    def _city_id(self, city: str | None, state_id: int | None) -> int | None:
        if not city or not state_id:
            return None
        cache = _load_geo_cache()
        cached = cache.get("cities", {}).get(str(state_id), {}).get(city)
        if cached:
            return int(cached)
        rows = self._get_json(f"{BASE_URL}/cities/{state_id}", {"api_key": self.api_key, "lang_id": 4})
        for row in rows or []:
            name = row.get("name")
            city_id = row.get("cityID")
            if name and city_id:
                cache.setdefault("cities", {}).setdefault(str(state_id), {})[name] = city_id
        _save_geo_cache(cache)
        return _as_int(cache.get("cities", {}).get(str(state_id), {}).get(city))

    def _district_ids(self, city_id: int | None, districts: list[str]) -> list[int]:
        if not city_id or not districts:
            return []
        wanted = {_norm(d) for d in districts if d}
        if not wanted:
            return []
        cache = _load_geo_cache()
        city_key = str(city_id)
        if city_key not in cache.get("districts", {}):
            rows = self._get_json(f"{BASE_URL}/cities_districts/{city_id}", {"api_key": self.api_key})
            flat = _flatten(rows)
            for row in flat:
                name = row.get("name")
                value = row.get("value") or row.get("area_id")
                if name and value:
                    cache.setdefault("districts", {}).setdefault(city_key, {})[name] = value
            _save_geo_cache(cache)
        out = []
        for name, value in cache.get("districts", {}).get(city_key, {}).items():
            norm_name = _norm(name)
            if any(w in norm_name or norm_name in w for w in wanted):
                ivalue = _as_int(value)
                if ivalue:
                    out.append(ivalue)
        return out

    def _to_listing(self, info: dict[str, Any], fallback_city: str | None = None) -> Listing | None:
        realty_id = str(info.get("realty_id") or info.get("_id") or "")
        if not realty_id:
            return None
        city = _ua_city(info.get("city_name")) or fallback_city
        district = _ua_district(info.get("district_name"))
        description = info.get("description_uk") or info.get("description") or ""
        title = _title(info)
        photos = _photos(info)
        price = _as_int(info.get("price_total") or info.get("price"))
        url = _url(info, realty_id)
        return Listing(
            id=realty_id,
            source=self.source,
            url=url,
            title=title,
            price_uah=price,
            currency=info.get("currency_type") or "UAH",
            city=city,
            district=district,
            address=", ".join(x for x in [info.get("street_name"), info.get("building_number_str")] if x),
            rooms=_as_int(info.get("rooms_count")),
            area_total=_as_float(info.get("total_square_meters")),
            floor=_as_int(info.get("floor")),
            floors_total=_as_int(info.get("floors_count")),
            is_no_fee=_is_no_fee(info),
            description=description,
            photos=photos,
            author=((info.get("user") or {}).get("name") if isinstance(info.get("user"), dict) else None),
            posted_at=info.get("publishing_date") or info.get("created_at"),
            scraped_at=str(int(time.time())),
            extra={"dom_ria_raw": info},
        )

    def _matches_query(self, item: Listing, query: dict[str, Any]) -> bool:
        if query.get("no_fee", True) and item.is_no_fee is False:
            return False
        price_from = query.get("price_from")
        price_to = query.get("price_to")
        if item.price_uah is not None and price_from is not None and item.price_uah < int(price_from):
            return False
        if item.price_uah is not None and price_to is not None and item.price_uah > int(price_to):
            return False
        return True

    def _matches_category(self, info: dict[str, Any]) -> bool:
        text = " ".join(str(info.get(key) or "") for key in [
            "realty_type_name",
            "realty_type_parent_name",
            "advert_type_name",
            "beautiful_url",
        ]).lower()
        if self.kind == "apartment":
            return any(word in text for word in ("кварт", "kvart", "apartment"))
        if self.kind == "room":
            return any(word in text for word in ("кімнат", "комнат", "room"))
        if self.kind == "house":
            return any(word in text for word in ("буд", "дом", "house", "dacha", "дач"))
        if self.kind == "office":
            return any(word in text for word in ("офіс", "офис", "office", "commercial", "комерц", "коммерц"))
        return True

    @staticmethod
    def _env_int(name: str, default: int | None) -> int | None:
        value = os.getenv(name, "").strip()
        return _as_int(value) if value else default


def get_dom_ria_provider(category: str, **kwargs) -> DomRiaProvider:
    return DomRiaProvider(category=category, **kwargs)


def _load_geo_cache() -> dict[str, Any]:
    try:
        return json.loads(GEO_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"cities": {}, "districts": {}}


def _save_geo_cache(cache: dict[str, Any]) -> None:
    GEO_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _flatten(value: Any) -> list[dict[str, Any]]:
    out = []
    if isinstance(value, dict):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(_flatten(item))
    return out


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).replace(" ", "")))
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", ".").replace(" ", ""))
    except Exception:
        return None


def _norm(value: str) -> str:
    return str(value or "").strip().lower().replace("ё", "е").replace("’", "'")


def _ua_city(value: str | None) -> str | None:
    aliases = {"Киев": "Київ", "Львов": "Львів", "Одесса": "Одеса", "Днепр": "Дніпро", "Харьков": "Харків"}
    return aliases.get(value or "", value)


def _ua_district(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).replace("Голосеевский", "Голосіївський")


def _title(info: dict[str, Any]) -> str:
    raw = info.get("title") or info.get("advert_title") or info.get("realty_type_name") or "Нерухомість DOM.RIA"
    if isinstance(raw, int):
        raw = info.get("realty_type_name") or "Нерухомість DOM.RIA"
    return str(raw)


def _url(info: dict[str, Any], realty_id: str) -> str:
    pretty = info.get("beautiful_url")
    if pretty:
        return f"https://dom.ria.com/uk/{pretty}"
    return f"https://dom.ria.com/uk/realty-{realty_id}.html"


def _photos(info: dict[str, Any]) -> list[str]:
    files = []
    photos = info.get("photos")
    if isinstance(photos, dict):
        for photo in photos.values():
            if isinstance(photo, dict) and photo.get("file"):
                files.append(photo["file"])
    if info.get("main_photo"):
        files.insert(0, info["main_photo"])
    out = []
    for file_name in files:
        url = _photo_url(str(file_name))
        if url and url not in out:
            out.append(url)
    return out[:10]


def _photo_url(file_name: str) -> str:
    if file_name.startswith("http"):
        base = file_name
    else:
        base = PHOTO_PREFIX + file_name.lstrip("/")
    return re.sub(r"\.jpg($|\?)", r"xl.jpg\1", base)


def _is_no_fee(info: dict[str, Any]) -> bool | None:
    values = info.get("characteristics_values") or {}
    offer_type = str(values.get("1437") or "")
    text = " ".join(str(x or "") for x in [
        info.get("description_uk"),
        info.get("description"),
        (info.get("user") or {}).get("name") if isinstance(info.get("user"), dict) else "",
    ]).lower()
    bad_words = ("агент", "агенц", "ріелтор", "риелтор", "коміс", "комис")
    good_words = ("без коміс", "без комис", "власник", "хозяин", "хазяїн")
    if offer_type in {"1434", "1435"}:
        return False
    if any(word in text for word in good_words):
        return True
    if any(word in text for word in bad_words):
        return False
    return None
