from __future__ import annotations

import re
import unicodedata
import random
from urllib.parse import quote
from typing import Any, Dict, List
from collections.abc import Iterable
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_utils import safe_scroll as _safe_scroll

# В САМІЙ ГОРІ ФАЙЛУ (після імпортів):
PATH_CATEGORY = "nedvizhimost/doma/arenda-domov"
SALE_PATH_CATEGORY = "nedvizhimost/doma/prodazha-domov"

# ===== Міста та райони (OLX) =====

CITY_ALIASES = {
    # Київ
    "kiev": "Київ",
    # Одеса
    "odessa": "Одеса",
    # Львів
    "lvov": "Львів",
    # Дніпро
    "dnepr": "Дніпро",
    # Івано-Франківськ
    "ivano-frankovsk": "Івано-Франківськ",
    # Луцьк
    "lutsk": "Луцьк",
}


# Який slug вважати основним для канонічної назви
PRIMARY_CITY_SLUG = {
    "Київ": "kiev",
    "Одеса": "odessa",
    "Львів": "lvov",
    "Дніпро": "dnepr",
    "Івано-Франківськ": "ivano-frankovsk",
    "Луцьк": "lutsk",
}



# ID районів з OLX (за надісланими посиланнями)
DISTRICT_IDS = {
    "Київ": {
        "Голосіївський": 1,
        "Дарницький": 3,
        "Деснянський": 5,
        "Дніпровський": 7,
        "Оболонський": 9,
        "Печерський": 11,
        "Подільський": 13,
        "Святошинський": 15,
        "Солом'янський": 17,
        "Шевченківський": 19,
    },
    "Одеса": {
        "Київський": 85,
        "Пересипський": 91,
        "Приморський": 89,
        "Хаджибейський": 87,
    },
    "Львів": {
        "Галицький": 127,
        "Залізничний": 129,
        "Личаківський": 131,
        "Сихівський": 133,
        "Франківський": 135,
        "Шевченківський": 137,
    },
    "Дніпро": {
        "Амур-Нижньодніпровський": 111,
        "Індустріальний": 117,
        "Новокодацький": 123,
        "Самарський": 125,
        "Соборний": 115,
        "Центральний": 119,
        "Чечелівський": 121,
        "Шевченківський": 113,
    },
    "Івано-Франківськ": {},
    "Луцьк": {},
}

from app_config import OLX_CITY_ALIASES as CITY_ALIASES
from app_config import OLX_DISTRICT_IDS as DISTRICT_IDS
from app_config import OLX_PRIMARY_CITY_SLUG as PRIMARY_CITY_SLUG

ROOMS_STRING_MAP = {
    1: "odnokomnatnye",
    2: "dvuhkomnatnye",
    3: "trehkomnatnye",
    4: "chetyrehkomnatnye",
    5: "pyatikomnatnye",
}
SORT_MAP = {
    "newest": "created_at:desc",
    "cheapest": "price:asc",
    "expensive": "price:desc",
}

CATEGORY_PATHS = {
    "apartment": "nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir",
    "house":     "nedvizhimost/doma/arenda-domov",
    "apartment_buy": "nedvizhimost/kvartiry/prodazha-kvartir",
    "house_buy": "nedvizhimost/doma/prodazha-domov",
    "room":      "nedvizhimost/komnaty/arenda-komnat",
    "office":    "nedvizhimost/kommercheskaya-nedvizhimost/arenda/ofisov",
}

# === Типи будинків (людські -> enum або keyword) ===
# те, що має ЧІТКИЙ enum на OLX:
HOUSE_TYPE_TO_OLX = {
    "котедж":    "cottage",
    "дуплекс":   "duplex",
    "таунхаус":  "townhouse",
    "садиба":    "homestead",     # інколи зустрічається 'estate' — за потреби підправиш
    "модульний": "modular_house",
    "маєток":    "mansion",       # або 'estate' — перевіриш і скоригуєш
}

# Якщо для якогось типу немає enum'а на OLX — підставимо ключове слово у шлях (/q-...).
HOUSE_TYPE_KEYWORD = {
    "садиба": "садиба",
    "маєток": "маєток",
    "вілла":  "вілла",
    "villa":  "villa",
}

DEFAULT_CATEGORY = "apartment"

MAX_FLOOR_DEFAULT = 26
MAX_PAGES_HARD = 50

PETS_MAP = {
    "cat": "yes_cat",
    "small_dog": "yes_small_dog",
    "medium_dog": "yes_medium_dog",
    "big_dog": "yes_big_dog",
    "other": "yes_other",
}

DEFAULT_PET_TYPES = ["yes_cat", "yes_small_dog", "yes_medium_dog", "yes_big_dog", "yes_other"]

try:
    from ..base import Provider, Listing      # якщо файл у providers/olx/apartments.py
except Exception:
    from providers.base import Provider, Listing  # якщо файл у корені пакета providers/


def _house_type_slug(v: str | None) -> str | None:
    if not v:
        return None
    return HOUSE_TYPE_TO_OLX.get(str(v).strip().lower())


def _normalize_house_type(v: str | None) -> str:
    if not v:
        return ""
    return str(v).strip().lower()

def _house_type_parts(house_type: str | Iterable[str] | None) -> tuple[str, str]:
    """
    Повертає (path_suffix, query_suffix) для типу(ів) будинку.
    - Якщо є enum-и OLX → будуємо &search[filter_enum_property_type_houses][i]=...
    - Якщо enum-ів нема і рівно 1 keyword → додаємо /q-<keyword> у шлях
    - Якщо keyword-ів кілька → шлях не чіпаємо (уникнемо зайвого звуження)
    """
    if not house_type:
        return "", ""

    if isinstance(house_type, str):
        types = [house_type]
    else:
        try:
            types = list(house_type)
        except Exception:
            types = [str(house_type)]

    slugs: list[str] = []
    keywords: list[str] = []

    for t in types:
        ht = _normalize_house_type(t)
        if not ht:
            continue
        s = HOUSE_TYPE_TO_OLX.get(ht)
        if s:
            slugs.append(s)
        else:
            w = HOUSE_TYPE_KEYWORD.get(ht)
            if w:
                keywords.append(w)

    # Будуємо enum-масив у query
    query = ""
    for i, s in enumerate(slugs):
        query += f"&search[filter_enum_property_type_houses][{i}]={s}"

    # Ключове слово в шлях — тільки якщо немає enum-ів і рівно 1 keyword
    path = f"/q-{quote(keywords[0])}" if (not slugs and len(keywords) == 1) else ""

    return path, query



class OlxProviderHouses(Provider):
    source = "olx"

    def __init__(self, user_agent: str | None = None, proxy: str | None = None, always_no_fee: bool = True):
        self.user_agent = user_agent
        self.proxy = proxy
        self.always_no_fee = always_no_fee

    # ===== helpers =====
    def _norm(self, s: str | None) -> str:
        if not s:
            return ""
        s = unicodedata.normalize("NFKD", s).lower()
        return s.replace("’", "'").replace("ʼ", "'").replace("`", "'")

    def _filter_supported_districts(self, city_slug: str | None, districts: list[str] | None) -> list[str]:
        if not districts:
            return []
        city_key = CITY_ALIASES.get(city_slug.strip().lower()) if city_slug else None
        if not city_key:
            city_key = city_slug
        allowed = DISTRICT_IDS.get(city_key, [])
        if not allowed:
            return []
        allowed_set = {self._norm(a) for a in allowed}
        return [d for d in districts if self._norm(d) in allowed_set]

    def _city_slug(self, query: dict) -> str:
        city_input = (query.get("city") or "").strip().lower()
        if not city_input:
            return ""
        if city_input in CITY_ALIASES:
            canonical = CITY_ALIASES[city_input]
        else:
            canonical = next((canon for alias, canon in CITY_ALIASES.items()
                              if canon.lower() == city_input), None)
            if not canonical:
                return city_input
        group = [a for a, c in CITY_ALIASES.items() if c == canonical]
        return PRIMARY_CITY_SLUG.get(canonical, (group[0] if group else city_input))

    def _city_slug_group(self, canonical_uk: str) -> list[str]:
        if not canonical_uk:
            return []
        return [a for a, c in CITY_ALIASES.items() if c == canonical_uk]

    def _best_city_slug(self, city_slug_or_cyrillic: str | None) -> str:
        if not city_slug_or_cyrillic:
            return ""
        s = city_slug_or_cyrillic.strip().lower()
        if s in CITY_ALIASES:
            canonical = CITY_ALIASES[s]
            group = self._city_slug_group(canonical)
            return PRIMARY_CITY_SLUG.get(canonical, (group[0] if group else s))
        for alias, canonical in CITY_ALIASES.items():
            if canonical.strip().lower() == s:
                group = self._city_slug_group(canonical)
                return PRIMARY_CITY_SLUG.get(canonical, (group[0] if group else alias))
        return s

    def _district_ids(self, city_slug: str | None, districts) -> list[int]:
        if not districts:
            return []
        if isinstance(districts, str):
            districts = [districts]
        else:
            try:
                districts = list(districts)
            except:
                return []
        for d in districts:
            if self._norm(str(d)) in ("всі райони", "всі райони 👀", "всі"):
                return []
        city_key = CITY_ALIASES.get((city_slug or "").strip().lower()) or city_slug
        mapping = DISTRICT_IDS.get(city_key, {})
        if not mapping:
            return []
        norm_map = {self._norm(k): v for k, v in mapping.items()}
        out = []
        for d in districts:
            nd = self._norm(str(d))
            if nd in norm_map:
                out.append(norm_map[nd])
        return out

    def _district_params(self, city_slug: str | None, districts: list[str] | None) -> str:
        ids = self._district_ids(city_slug, districts)
        if not ids:
            return ""
        return "".join(f"&search[district_id]={i}" for i in ids)

    def _rooms_param(self, rooms) -> str:
        if not rooms:
            return ""
        if isinstance(rooms, int):
            rooms = [rooms]
        out, idx = [], 0
        for r in rooms:
            try:
                slug = ROOMS_STRING_MAP.get(int(r))
                if slug:
                    out.append(f"&search[filter_enum_number_of_rooms_string][{idx}]={slug}")
                    idx += 1
            except:
                continue
        return "".join(out)

    def _budget_params(self, price_from: int | None, price_to: int | None) -> str:
        parts = []
        try:
            if price_from is not None:
                price_from = max(0, int(price_from))
                parts.append(f"&search[filter_float_price:from]={price_from}")
            if price_to is not None:
                price_to = max(0, int(price_to))
                if price_from is not None and price_to < price_from:
                    price_to = price_from
                parts.append(f"&search[filter_float_price:to]={price_to}")
        except:
            return ""
        return "".join(parts)

    def _no_fee_param(self, no_fee: bool | None) -> str:
        return "&search[filter_enum_commission][0]=1"

    def _pets_param(self, pets_allowed: bool | None, pets_types: list[str] | None = None) -> str:
        # ✅ якщо тварини НЕ допускаються — додаємо фільтр 'no'
        if pets_allowed is False:
            return "&search[filter_enum_pets][0]=no"

        # якщо не True і не False (None/невідомо) — нічого не додаємо
        if pets_allowed is not True:
            return ""

        # нижче як і було: коли тварини допускаються — додаємо дозволені типи
        ALL = DEFAULT_PET_TYPES
        TYPE_MAP = {
            "cat": "yes_cat",
            "кіт": "yes_cat", "кішка": "yes_cat",
            "small_dog": "yes_small_dog", "small-dog": "yes_small_dog",
            "medium_dog": "yes_medium_dog", "medium-dog": "yes_medium_dog",
            "big_dog": "yes_big_dog", "big-dog": "yes_big_dog", "large_dog": "yes_big_dog",
            "dog": "yes_medium_dog",
            "other": "yes_other", "інше": "yes_other",
        }
        if not pets_types:
            vals = ALL
        else:
            vals = []
            for t in pets_types:
                if not t:
                    continue
                key = str(t).strip().lower()
                slug = TYPE_MAP.get(key, key if key in ALL else None)
                if slug and slug not in vals:
                    vals.append(slug)
            if not vals:
                vals = ALL
        return "".join(f"&search[filter_enum_pets][{i}]={v}" for i, v in enumerate(vals))

    def _coerce_pets(self, v) -> bool | None:
        if isinstance(v, bool):
            return v
        if v is None:
            return None
        s = str(v).strip().lower()
        truthy = {"true", "1", "yes", "y", "так", "т", "маю", "есть", "є", "з тваринами", "маю тварин", "allow",
                  "allowed"}
        falsy = {"false", "0", "no", "n", "ні", "без тварин", "без", "deny", "denied", "none", "null", ""}
        if s in truthy:
            return True
        if s in falsy:
            return False
        return None

    def _area_params(self, area: Dict[str, Any] | None) -> str:
        if not area:
            return ""
        val = area.get("from")
        if val is None:
            return ""
        try:
            n = int(val)
            if n < 0:
                n = 0
        except:
            return ""
        return f"&search[filter_float_total_area:from]={n}"

    def _floor_params(self, floor: Dict[str, Any] | None) -> str:
        if not floor:
            return ""
        s = []
        if "from" in floor and floor["from"] is not None:
            s.append(f"&search[filter_float_floor:from]={int(floor['from'])}")
        if "to" in floor and floor["to"] is not None:
            s.append(f"&search[filter_float_floor:to]={int(floor['to'])}")
        return "".join(s)

    def floor_preset(self, preset: str | None) -> dict | None:
        if not preset:
            return None
        p = preset.strip().lower()
        if p in ("any", "будь-який поверх"):
            return None
        if p in ("no_1st", "без 1 поверху", "без першого"):
            return {"from": 2}
        if p in ("no_2nd", "без 2 поверху", "без другого"):
            return {"from": 3}
        if p in ("no_top", "без останнього поверху"):
            return {"to": MAX_FLOOR_DEFAULT}
        if p.startswith("range_"):
            try:
                _, a, b = p.split("_")
                return {"from": int(a), "to": int(b)}
            except:
                return None
        return None

    def _sort_param(self, sort: str | None) -> str:
        if not sort:
            return ""
        s = SORT_MAP.get(sort)
        return f"&search[order]={s}" if s else ""

    def build_url(self, query: Dict[str, Any], page: int = 1) -> str:
        print("[BUILD_URL_IN]", {"allows_pets": query.get("allows_pets"), "pet_types": query.get("pet_types")})

        base = "https://www.olx.ua/uk"
        category = str(query.get("category") or "house").lower()
        path_category = SALE_PATH_CATEGORY if category in {"house_buy", "buy_house"} else PATH_CATEGORY

        city_input = (query.get("city") or "").strip()
        city_slug = (query.get("city_slug") or "").strip().lower() or self._best_city_slug(city_input)
        path_city = f"/{city_slug}" if city_slug else ""

        # ⬇️ ДОДАЛИ: розрахунок частини шляху/квері для типу будинку
        type_path_suffix, type_query_suffix = _house_type_parts(query.get("house_type"))

        q = ""
        q += self._district_params(city_slug, query.get("districts"))
        q += self._rooms_param(query.get("rooms"))
        q += self._budget_params(query.get("price_from"), query.get("price_to"))
        q += self._no_fee_param(query.get("no_fee"))

        print("[DEBUG] allows_pets(raw) =", query.get("allows_pets"))
        pets_allowed = query.get("allows_pets")  # залишаємо True / False / None
        pet_types = query.get("pet_types")
        q += self._pets_param(pets_allowed, pet_types)

        q += self._area_params(query.get("area"))
        q += self._floor_params(query.get("floor"))
        q += self._sort_param(query.get("sort"))

        # ⬇️ ДОДАЛИ: enum‑частина для типу (якщо була)
        q += type_query_suffix

        # ... решта q += ...
        dist_km = int(query.get("dist_km") or 0)
        if dist_km > 0:
            q += f"&search[dist]={dist_km}"

        q = f"?currency=UAH{q}"
        if page and page > 1:
            q += f"&page={page}"

        # ⬇️ ДОДАЛИ: keyword‑частина в шляху (якщо була)
        return f"{base}/{path_category}{path_city}{type_path_suffix}/{q}"

    def _hq_image_url(self, url: str | None) -> str | None:
        if not url:
            return None
        u = re.sub(r";s=\d+x\d+", "", url)
        u = re.sub(r"image-size=\d+x\d+;", "", u)
        u = re.sub(r"quality=\d+", "quality=100", u)
        return u

    def _extract_listings_from_page(self, page) -> List[Listing]:
        out: List[Listing] = []

        # ширший контейнер списку
        grid = page.query_selector("[data-testid='listing-grid'], [data-cy='listing-grid']")
        scope = grid or page

        # ширші селектори карток (OLX інколи міняє data-* атрибути)
        cards = scope.query_selector_all(
            "div[data-cy='l-card'], article[data-testid='ad-card'], article[data-cy='l-card'], div[data-testid='l-card']"
        )

        for c in cards:
            try:
                # посилання/заголовок (кілька варіантів)
                a = (c.query_selector("a[data-cy='ad-card-title'], a[data-testid='ad-card-title']")
                     or c.query_selector("a[href*='/d/uk/obyavlenie/'], a[href*='/d/obyavlenie/']"))
                if not a:
                    continue

                url = a.get_attribute("href") or ""
                if url.startswith("/"):
                    url = "https://www.olx.ua" + url

                # заголовок (h4/h6 або текст самого посилання)
                title_el = (c.query_selector(
                    "[data-cy='ad-card-title'] h4, [data-cy='ad-card-title'] h6, "
                    "[data-testid='ad-card-title'] h4, [data-testid='ad-card-title'] h6, h6"
                ) or a)
                title = title_el.inner_text().strip() if title_el else ""

                # ціна (кілька варіантів, інколи «Договірна»)
                price_el = (c.query_selector("[data-testid='ad-price'], [data-cy='ad-card-price']")
                            or c.query_selector("p:has(span[data-testid='ad-price'])"))
                price_text = (price_el.inner_text().strip() if price_el else "").replace("\xa0", " ")
                m = re.search(r"([\d\s]+)", price_text)
                price_uah = int(m.group(1).replace(" ", "")) if m else None

                # фото (srcset / data-srcset / src / data-src)
                photos = []
                img_el = c.query_selector("img")
                if img_el:
                    best = None
                    srcset = (img_el.get_attribute("srcset")
                              or img_el.get_attribute("srcSet")
                              or img_el.get_attribute("data-srcset")
                              or img_el.get_attribute("data-srcSet"))
                    if srcset:
                        try:
                            cand = []
                            for part in srcset.split(","):
                                part = part.strip()
                                if not part:
                                    continue
                                bits = part.split()
                                url_c = bits[0]
                                w = 0
                                if len(bits) > 1 and bits[1].endswith("w"):
                                    w = int(re.sub(r"\D", "", bits[1]))
                                cand.append((w, url_c))
                            cand.sort(key=lambda t: t[0])
                            best = cand[-1][1] if cand else None
                        except Exception:
                            best = None
                    if not best:
                        best = (img_el.get_attribute("src")
                                or img_el.get_attribute("data-src")
                                or img_el.get_attribute("data-original"))
                    best = self._hq_image_url(best)
                    if best and isinstance(best, str) and best.startswith("http"):
                        photos.append(best)

                # ID оголошення
                m = re.search(r"ID([A-Za-z0-9]+)\.html", url)
                external_id = m.group(1) if m else url

                out.append(Listing(
                    id=f"olx:{external_id}",
                    source=self.source,
                    url=url,
                    title=title,
                    price_uah=price_uah,
                    photos=photos
                ))
            except Exception:
                continue

        return out

    def search(self, query: Dict[str, Any]) -> List[Listing]:
        q = dict(query)
        debug = bool(q.get("debug"))

        # 🔎 що прийшло на вхід (до будь-яких змін)
        print("[SEARCH_IN_RAW]", {"allows_pets": q.get("allows_pets"), "pet_types": q.get("pet_types")})

        # floor
        preset = q.get("floor_preset")
        floor = q.get("floor")
        fp = self.floor_preset(preset) if hasattr(self, "floor_preset") else None
        if fp is not None:
            q["floor"] = fp
        else:
            if isinstance(floor, dict):
                clean = {}
                if floor.get("from") is not None:
                    clean["from"] = int(floor["from"])
                if floor.get("to") is not None:
                    clean["to"] = int(floor["to"])
                q["floor"] = clean if clean else None
            else:
                q["floor"] = None

        # 🐾 тварини
        raw_pets = q.get("allows_pets")
        pets_flag = self._coerce_pets(raw_pets)  # -> True / False / None
        q["allows_pets"] = pets_flag
        if pets_flag is True:
            q["pet_types"] = q.get("pet_types") or DEFAULT_PET_TYPES
        elif pets_flag is False:
            q["pet_types"] = []
        else:  # None → не чіпаємо фільтр і типи
            q.pop("pet_types", None)

        # 🔎 після нормалізації
        print("[SEARCH_NORM]", {"allows_pets": q.get("allows_pets"), "pet_types": q.get("pet_types")})

        results: List[Listing] = []

        # >>> PATCH: антибот-параметри для браузера/контексту
        launch_args = {
            "headless": not debug,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        context_args = {
            "locale": "uk-UA",
            "viewport": {"width": 1366, "height": 900},
        }
        if self.user_agent:
            context_args["user_agent"] = self.user_agent
        else:
            context_args["user_agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/122.0.0.0 Safari/537.36")
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}
        # <<< PATCH

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            try:
                context = browser.new_context(**context_args)
                # >>> PATCH: прибрати webdriver-флаг
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                # <<< PATCH

                page = context.new_page()
                page.set_default_timeout(15000)
                page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})
                if debug:
                    page.set_viewport_size({"width": 1366, "height": 900})

                # >>> PATCH: helper для cookies усередині search(...)
                def accept_cookies(pg):
                    for sel in [
                        "[data-testid='cookies-popup-accept-all']",
                        "[data-testid='cookiesbar-accept']",
                        "button#onetrust-accept-btn-handler",
                        "button:has-text('Прийняти все')",
                        "button:has-text('Погоджуюсь')",
                        "button:has-text('Accept all')",
                    ]:
                        try:
                            pg.locator(sel).first.click(timeout=1200)
                            pg.wait_for_timeout(300)
                            return
                        except Exception:
                            pass

                # <<< PATCH

                # ===== 1) Перша сторінка
                url = self.build_url(q, page=1)
                print("[HOUSE][GOTO] about to open:", url)
                try:
                    page.goto(url, timeout=15000, wait_until="domcontentloaded")
                    print("[HOUSE][GOTO] ok domcontentloaded")

                    # >>> PATCH: cookies + чек контейнера + лінива догрузка
                    accept_cookies(page)
                    try:
                        page.wait_for_selector("[data-testid='listing-grid'], [data-cy='listing-grid']", timeout=5000)
                    except:
                        pass

                    try:
                        page.screenshot(path="olx_after_cookies.png", full_page=True)
                        print("[OLX] saved screenshot: olx_after_cookies.png")
                    except Exception:
                        pass

                    prev = -1
                    for _ in range(4):
                        _safe_scroll(page, 2000)
                        page.wait_for_timeout(random.randint(150, 300))
                        cur = page.locator(
                            "div[data-cy='l-card'], [data-testid='ad-card'], article[data-testid='l-card']"
                        ).count()
                        if cur == prev:
                            break
                        prev = cur

                    try:
                        page.screenshot(path="olx_after_scroll.png", full_page=True)
                        print("[OLX] saved screenshot: olx_after_scroll.png")
                    except Exception:
                        pass

                    # <<< PATCH

                    # визначаємо total_pages
                    def _detect_total_pages() -> int:
                        try:
                            hrefs = page.evaluate("""
                                () => Array.from(document.querySelectorAll("a[href*='&page='], a[href*='?page=']"))
                                             .map(a => a.getAttribute('href') || '')
                            """)
                        except:
                            hrefs = []
                        nums = []
                        for h in hrefs:
                            for m in re.findall(r"[?&]page=(\d+)", h):
                                try:
                                    nums.append(int(m))
                                except:
                                    pass
                        total = max(nums) if nums else 1
                        return max(1, min(total, MAX_PAGES_HARD))

                    detected = _detect_total_pages()
                    user_cap = int(q.get("max_pages") or 0)
                    total_pages = min(detected, user_cap) if user_cap > 0 else detected
                    print(f"[OLX] pagination: detected {detected}, using {total_pages} pages  |  {url}")

                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(random.randint(250, 450))
                    batch = self._extract_listings_from_page(page)
                    page.wait_for_timeout(random.randint(400, 800))
                    batch = self._extract_listings_from_page(page)

                    print(f"[OLX] page 1: {len(batch)} cards  |  {url}")
                    if len(batch) == 0:
                        try:
                            page.screenshot(path="olx_page_1.png", full_page=True)
                            print("[OLX] saved screenshot: olx_page_1.png")
                        except Exception as e:
                            print("[OLX] screenshot error:", e)

                    results.extend(batch)

                    # ===== 2) Інші сторінки 2..N
                    for pg in range(2, total_pages + 1):
                        url = self.build_url(q, page=pg)
                        print(f"[HOUSE][GOTO] page {pg} ->", url)
                        try:
                            page.goto(url, timeout=15000, wait_until="domcontentloaded")
                            print(f"[HOUSE][GOTO] page {pg} ok")

                            # >>> PATCH: cookies + чек + скрол для наступних сторінок
                            accept_cookies(page)
                            try:
                                page.wait_for_selector("[data-testid='listing-grid'], [data-cy='listing-grid']",
                                                       timeout=5000)
                            except:
                                pass

                            prev = -1
                            for _ in range(3):
                                _safe_scroll(page, 2000)
                                page.wait_for_timeout(random.randint(150, 300))
                                cur = page.locator(
                                    "div[data-cy='l-card'], [data-testid='ad-card'], article[data-testid='l-card']"
                                ).count()
                                if cur == prev:
                                    break
                                prev = cur
                            # <<< PATCH

                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(random.randint(250, 450))
                            batch = self._extract_listings_from_page(page)
                            page.wait_for_timeout(random.randint(400, 800))
                            batch = self._extract_listings_from_page(page)

                            print(f"[OLX] page {pg}: {len(batch)} cards  |  {url}")
                            if len(batch) == 0:
                                try:
                                    page.screenshot(path=f"olx_page_{pg}.png", full_page=True)
                                    print(f"[OLX] saved screenshot: olx_page_{pg}.png")
                                except Exception as e:
                                    print("[OLX] screenshot error:", e)

                            results.extend(batch)

                        except PWTimeout as e:
                            print(f"[HOUSE][TIMEOUT] page {pg}:", e)
                            page.wait_for_timeout(2500 + random.randint(0, 1000))
                            continue


                except PWTimeout as e:
                    print("[HOUSE][TIMEOUT] first page:", e)
                    page.wait_for_timeout(2500 + random.randint(0, 1000))
            finally:
                browser.close()

        # ===== 3) Дедуп
        uniq, seen = [], set()
        for it in results:
            key = it.id or it.url
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)

        # ===== 4) Alias‑retry міст
        if not uniq:
            city_slug = (q.get("city_slug") or q.get("city") or "").strip().lower()
            alias_depth = int(q.get("_alias_depth", 0))
            if city_slug and alias_depth < 2:
                canonical = CITY_ALIASES.get(city_slug)
                if canonical:
                    group = [a for a, c in CITY_ALIASES.items() if c == canonical]
                    for alt_slug in group:
                        if alt_slug == city_slug:
                            continue
                        alt = dict(q)
                        alt["city_slug"] = alt_slug
                        alt["_alias_depth"] = alias_depth + 1
                        print(f"[OLX] retry with city_slug={alt_slug}")
                        cand = self.search(alt)
                        if cand:
                            return cand

        # ===== 5) Fallback
        if not uniq:
            relaxed = dict(q)
            relaxed["area"] = None

            results2: List[Listing] = []
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_default_timeout(15000)
                    page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})
                    for pg in range(1, int(relaxed.get("max_pages", 2)) + 1):
                        url = self.build_url(relaxed, page=pg)
                        try:
                            page.goto(url, timeout=15000, wait_until="domcontentloaded")
                            page.wait_for_timeout(random.randint(300, 600))
                            batch = self._extract_listings_from_page(page)
                            print(f"[OLX][fallback] page {pg}: {len(batch)} cards  |  {url}")
                            if len(batch) == 0:
                                try:
                                    page.screenshot(path=f"olx_fallback_page_{pg}.png", full_page=True)
                                    print(f"[OLX] saved screenshot: olx_fallback_page_{pg}.png")
                                except Exception as e:
                                    print("[OLX] screenshot error:", e)
                            results2.extend(batch)
                        except PWTimeout:
                            continue
                finally:
                    browser.close()

            seen = set()
            uniq2 = []
            for it in results2:
                key = it.id or it.url
                if key in seen:
                    continue
                seen.add(key)
                uniq2.append(it)
            return uniq2

        # ===== Fallback для міст з нульовим радіусом
        if not uniq and int(q.get("dist_km") or 0) == 0:
            print("[HOUSE][FALLBACK] 0 результатів у місті → пробуємо радіус 30 км")
            q2 = dict(q)
            q2["dist_km"] = 30
            try:
                return self.search(q2)
            except Exception as e:
                print("[HOUSE][FALLBACK] error:", e)

        def _has_enum(ts):
            return any(HOUSE_TYPE_TO_OLX.get(_normalize_house_type(x)) for x in ts)

        def _kw_list_for(ts):
            kws = []
            if any(_normalize_house_type(x) == "садиба" for x in ts):
                kws += ["садиб", "усадьб", "homestead"]
            if any(_normalize_house_type(x) == "маєток" for x in ts):
                kws += ["маєток", "особняк", "mansion", "вілла", "villa"]
            return kws

        # <<< ВИПРАВЛЕННЯ: беремо тип будинку з нормалізованого запиту q
        house_type_req = q.get("house_type")

        if isinstance(house_type_req, str) and house_type_req.strip():
            ht = _normalize_house_type(house_type_req)
            if ht in ("садиба", "маєток"):
                KW = {
                    "садиба": ["садиб", "усадьб", "homestead"],
                    "маєток": ["маєток", "особняк", "mansion", "вілла", "villa"],
                }
                keys = KW[ht]
                before = len(uniq)
                uniq = [it for it in uniq if any(k in (getattr(it, "title", "").lower()) for k in keys)]
                print(f"[HOUSE][POSTFILTER] {ht} {before}->{len(uniq)}")

        elif isinstance(house_type_req, (list, tuple)) and house_type_req:
            # Якщо жоден з вибраних типів не має OLX-enum'а — фільтруємо по ключових словах
            if not _has_enum(house_type_req):
                keys = _kw_list_for(house_type_req)
                if keys:
                    before = len(uniq)
                    uniq = [it for it in uniq if any(k in (getattr(it, "title", "").lower()) for k in keys)]
                    print(f"[HOUSE][POSTFILTER] keywords {before}->{len(uniq)}")

        return uniq
