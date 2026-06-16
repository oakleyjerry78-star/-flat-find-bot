from __future__ import annotations

# providers/olx/olx_apartments.py
import re
import unicodedata
import random
from typing import Any, Dict, List, Optional,Callable

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_utils import safe_scroll as _safe_scroll

# ===== Офісна категорія OLX =====

PATH_CATEGORY = "nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir"
SALE_PATH_CATEGORY = "nedvizhimost/kvartiry/prodazha-kvartir"

# ===== Міста та райони (OLX) =====
CITY_ALIASES = {
    "kiev": "Київ",
    "odessa": "Одеса",
    "lvov": "Львів",
    "dnepr": "Дніпро",
    "ivano-frankovsk": "Івано-Франківськ",
    "lutsk": "Луцьк",
}
PRIMARY_CITY_SLUG = {
    "Київ": "kiev",
    "Одеса": "odessa",
    "Львів": "lvov",
    "Дніпро": "dnepr",
    "Івано-Франківськ": "ivano-frankovsk",
    "Луцьк": "lutsk",
}




DISTRICT_IDS = {
    "Київ": {"Голосіївський": 1, "Дарницький": 3, "Деснянський": 5, "Дніпровський": 7, "Оболонський": 9,
             "Печерський": 11, "Подільський": 13, "Святошинський": 15, "Солом'янський": 17, "Шевченківський": 19},
    "Одеса": {"Київський": 85, "Пересипський": 91, "Приморський": 89, "Хаджибейський": 87},
    "Львів": {"Галицький": 127, "Залізничний": 129, "Личаківський": 131, "Сихівський": 133,
              "Франківський": 135, "Шевченківський": 137},
    "Дніпро": {"Амур-Нижньодніпровський": 111, "Індустріальний": 117, "Новокодацький": 123, "Самарський": 125,
               "Соборний": 115, "Центральний": 119, "Чечелівський": 121, "Шевченківський": 113},
    "Івано-Франківськ": {},
    "Луцьк": {},
}

from app_config import OLX_CITY_ALIASES as CITY_ALIASES
from app_config import CITY_DISTRICTS
from app_config import OLX_DISTRICT_IDS as DISTRICT_IDS
from app_config import OLX_PRIMARY_CITY_SLUG as PRIMARY_CITY_SLUG
SORT_MAP = {"newest": "created_at:desc", "cheapest": "price:asc", "expensive": "price:desc"}
MAX_PAGES_HARD = 50

ROOMS_ENUM = {
    1: "odnokomnatnye",
    2: "dvuhkomnatnye",
    3: "trehkomnatnye",
    4: "chetirehkomnatnye",
    5: "piatikomnatnye",
}

PETS_MAP = {
    "cat": "yes_cat",
    "small_dog": "yes_small_dog",
    "medium_dog": "yes_medium_dog",
    "big_dog": "yes_big_dog",
    "other": "yes_other",
}

DEFAULT_PET_TYPES = ["yes_cat", "yes_small_dog", "yes_medium_dog", "yes_big_dog", "yes_other"]

# базові класи (підстрой під свій шлях)
try:
    from providers.base import Provider, Listing
except Exception:
    from ..base import Provider, Listing


class OlxProviderapartments(Provider):
    """
    Провайдер для OLX → ОФІСИ.
    ЖОДНИХ кімнат/тварин у URL і логіці.
    Підтримує: місто (slug/укр), райони, бюджет, площу (from/to), поверх (from/to), сортування, max_pages.
    """
    source = "olx"

    def __init__(
        self,
        user_agent: str | None = None,
        proxy: str | None = None,
        always_no_fee: bool = True,
        debug_sink: Optional[Callable[[bytes, str], None]] = None,  # 👈
    ):
        self.user_agent = user_agent
        self.proxy = proxy
        self.always_no_fee = always_no_fee
        self.debug_sink = debug_sink  # 👈

    def _emit_image(self, page, name: str):
        """Віддає PNG-байти у зовнішній колбек, якщо він заданий."""
        if not self.debug_sink:
            return
        try:
            png_bytes = page.screenshot(full_page=True)
            self.debug_sink(png_bytes, name)
        except Exception as e:
            print("[DEBUG_SINK] screenshot error:", e)

    # ------------ helpers ------------
    def _norm(self, s: Optional[str]) -> str:
        if not s:
            return ""
        s = unicodedata.normalize("NFKD", s).lower()
        return s.replace("’", "'").replace("ʼ", "'").replace("`", "'")

    def _parse_int(self, v) -> Optional[int]:
        if v is None:
            return None
        try:
            if isinstance(v, (int, float)):
                return int(v)
            digits = re.sub(r"[^\d]", "", str(v))
            return int(digits) if digits else None
        except Exception:
            return None

    def _best_city_slug(self, city_or_slug: Optional[str]) -> str:
        if not city_or_slug:
            return ""
        s = city_or_slug.strip().lower()
        if s in CITY_ALIASES:
            canonical = CITY_ALIASES[s]
            return PRIMARY_CITY_SLUG.get(canonical, s)
        for alias, canonical in CITY_ALIASES.items():
            if canonical.strip().lower() == s:
                return PRIMARY_CITY_SLUG.get(canonical, alias)
        return s

    def _district_params(self, city_slug: Optional[str], districts_or_ids) -> str:
        if not districts_or_ids:
            return ""

        def _as_indexed_params(ids: list[int]) -> str:
            # прибираємо дублікати, зберігаємо порядок
            uniq, seen = [], set()
            for i in ids:
                if i not in seen:
                    seen.add(i)
                    uniq.append(i)
            return "".join(f"&search[district_id][{idx}]={i}" for idx, i in enumerate(uniq))

        # 1) якщо вже прийшли id-значення
        try:
            ids = [int(d) for d in districts_or_ids if str(d).strip()]
            return _as_indexed_params(ids)
        except Exception:
            pass

        # 2) інакше — мапимо назви районів -> id
        city_key = CITY_ALIASES.get((city_slug or "").strip().lower()) or city_slug
        mapping = DISTRICT_IDS.get(city_key, {})
        if not mapping:
            return ""

        norm_map = {self._norm(k): v for k, v in mapping.items()}
        ids = []
        for d in districts_or_ids:
            nd = self._norm(str(d))
            if nd in ("всі райони", "всі райони 👀", "всі"):
                return ""  # «усі» → без параметра
            if nd in norm_map:
                ids.append(norm_map[nd])

        return _as_indexed_params(ids)

    def _budget_params(self, price_from, price_to) -> str:
        pf = self._parse_int(price_from)
        pt = self._parse_int(price_to)
        parts = []
        if pf is not None:
            parts.append(f"&search[filter_float_price:from]={pf}")
        if pt is not None:
            if pf is not None and pt < pf:
                pt = pf
            parts.append(f"&search[filter_float_price:to]={pt}")
        return "".join(parts)

    def _area_params_from_query(self, q: Dict[str, Any]) -> str:
        if isinstance(q.get("area"), dict):
            a_from = self._parse_int(q["area"].get("from"))
            a_to = self._parse_int(q["area"].get("to"))
        else:
            a_from = self._parse_int(q.get("area_from"))
            a_to = self._parse_int(q.get("area_to"))
        parts = []
        if a_from is not None:
            parts.append(f"&search[filter_float_total_area:from]={a_from}")
        if a_to is not None:
            parts.append(f"&search[filter_float_total_area:to]={a_to}")
        return "".join(parts)

    def _rooms_params(self, rooms_labels_or_nums) -> str:
        if not rooms_labels_or_nums:
            return ""
        vals = []

        def _push(n: int):
            slug = ROOMS_ENUM.get(n)
            if slug and slug not in vals:
                vals.append(slug)

        # приймаємо і список міток "1 кімната", і просто числа
        if isinstance(rooms_labels_or_nums, (list, tuple, set)):
            for r in rooms_labels_or_nums:
                if isinstance(r, int):
                    _push(r)
                else:
                    m = re.search(r"(\d+)", str(r))
                    if m:
                        _push(int(m.group(1)))
        else:
            m = re.search(r"(\d+)", str(rooms_labels_or_nums))
            if m:
                _push(int(m.group(1)))

        return "".join(
            f"&search[filter_enum_number_of_rooms_string][{i}]={v}"
            for i, v in enumerate(vals)
        )

    def _floor_params(self, floor: Optional[Dict[str, Any]]) -> str:
        if not floor or not isinstance(floor, dict):
            return ""
        s = []
        if floor.get("from") is not None:
            s.append(f"&search[filter_float_floor:from]={int(self._parse_int(floor['from']))}")
        if floor.get("to") is not None:
            s.append(f"&search[filter_float_floor:to]={int(self._parse_int(floor['to']))}")
        return "".join(s)

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

    def _coerce_pets(self, v) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        s = str(v).strip().lower()
        truthy = {"true", "1", "yes", "y", "так", "т", "маю", "есть", "є", "з тваринами", "маю тварин", "allow", "allowed"}
        falsy = {"false", "0", "no", "n", "ні", "без тварин", "без", "deny", "denied", "none", "null", ""}
        if s in truthy:
            return True
        if s in falsy:
            return False
        return False

    def _sort_param(self, sort: Optional[str]) -> str:
        if not sort:
            return ""
        s = SORT_MAP.get(sort)
        return f"&search[order]={s}" if s else ""

    # ------------ URL builder ------------
    def build_url(self, query: Dict[str, Any], page: int = 1) -> str:
        base = "https://www.olx.ua/uk"
        category = str(query.get("category") or "apartment").lower()
        path_category = SALE_PATH_CATEGORY if category in {"apartment_buy", "buy_apartment"} else PATH_CATEGORY

        city_input = (query.get("city") or "").strip()
        city_slug = (query.get("city_slug") or "").strip().lower() or self._best_city_slug(city_input)
        path_city = f"/{city_slug}" if city_slug else ""

        q = ""
        # 1) район
        q += self._district_params(city_slug, query.get("districts") or query.get("district_ids"))
        # 2) поверх
        q += self._floor_params(query.get("floor"))
        # 3) кімнати
        q += self._rooms_params(
            query.get("rooms") or query.get("rooms_label") or query.get("rooms_labels")
        )
        # 4) площа
        q += self._area_params_from_query(query)
        # 5) бюджет
        q += self._budget_params(query.get("price_from"), query.get("price_to"))
        # 6) без комісії (як і було)
        q += "&search[filter_enum_commission][0]=1"
        # 7) тварини
        pets_filter = query.get("pets_filter")
        if pets_filter in ("no",):
            q += f"&search[filter_enum_pets][0]={pets_filter}"
        # сортування наприкінці
        q += self._sort_param(query.get("sort"))

        q = f"?currency=UAH{q}"
        if page and page > 1:
            q += f"&page={page}"

        return f"{base}/{path_category}{path_city}/{q}"

    # ------------ scraping ------------
    def _hq_image_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        u = re.sub(r";s=\d+x\d+", "", url)
        u = re.sub(r"image-size=\d+x\d+;", "", u)
        u = re.sub(r"quality=\d+", "quality=100", u)
        return u

    def _extract_location_text(self, card) -> str:
        selectors = [
            "[data-testid='location-date']",
            "[data-cy='ad-card-location']",
            "p[data-testid='location-date']",
            "p[data-cy='location-date']",
        ]
        for sel in selectors:
            try:
                el = card.query_selector(sel)
                if not el:
                    continue
                text = " ".join((el.inner_text() or "").split()).strip()
                if text:
                    return text
            except Exception:
                continue
        return ""

    def _parse_city_district(self, location_text: str) -> tuple[str | None, str | None, str]:
        raw = " ".join((location_text or "").split()).strip()
        if not raw:
            return None, None, ""
        core = re.split(r"\s+[—-]\s+", raw, maxsplit=1)[0].strip()
        normalized = self._norm(core)

        city = None
        for city_name in CITY_DISTRICTS.keys():
            if self._norm(city_name) in normalized:
                city = city_name
                break

        district = None
        if city:
            for district_name in CITY_DISTRICTS.get(city, []):
                if self._norm(district_name) in normalized:
                    district = district_name
                    break

        return city, district, core

    def _extract_listings_from_page(self, page) -> List[Listing]:
        out: List[Listing] = []
        grid = page.query_selector("[data-testid='listing-grid'], [data-cy='listing-grid']")
        scope = grid or page
        cards = scope.query_selector_all(
            "div[data-cy='l-card'], article[data-testid='ad-card'], article[data-cy='l-card'], div[data-testid='l-card']"
        )
        for c in cards:
            try:
                a = (c.query_selector("a[data-cy='ad-card-title'], a[data-testid='ad-card-title']")
                     or c.query_selector("a[href*='/d/uk/obyavlenie/'], a[href*='/d/obyavlenie/']"))
                if not a:
                    continue
                url = a.get_attribute("href") or ""
                if url.startswith("/"):
                    url = "https://www.olx.ua" + url

                title_el = (c.query_selector(
                    "[data-cy='ad-card-title'] h4, [data-cy='ad-card-title'] h6, "
                    "[data-testid='ad-card-title'] h4, [data-testid='ad-card-title'] h6, h6"
                ) or a)
                title = title_el.inner_text().strip() if title_el else ""

                price_el = (c.query_selector("[data-testid='ad-price'], [data-cy='ad-card-price']")
                            or c.query_selector("p:has(span[data-testid='ad-price'])"))
                price_text = (price_el.inner_text().strip() if price_el else "").replace("\xa0", " ")
                m = re.search(r"([\d\s]+)", price_text)
                price_uah = int(m.group(1).replace(" ", "")) if m else None

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

                m = re.search(r"ID([A-Za-z0-9]+)\.html", url)
                external_id = m.group(1) if m else url
                location_text = self._extract_location_text(c)
                detected_city, detected_district, detected_address = self._parse_city_district(location_text)

                out.append(Listing(
                    id=f"olx:{external_id}",
                    source=self.source,
                    url=url,
                    title=title,
                    price_uah=price_uah,
                    city=detected_city,
                    district=detected_district,
                    address=detected_address or None,
                    photos=photos,
                    extra={"location_text": location_text} if location_text else {}
                ))
            except Exception:
                continue
        return out



    # --- точний підрахунок через Playwright за 1-2 с ---


    def search(self, query: Dict[str, Any]) -> List[Listing]:
        q = dict(query)
        debug = bool(q.get("debug"))

        # floor → очікуємо dict {"from":..,"to":..}
        floor = q.get("floor")
        if isinstance(floor, dict):
            clean = {}
            if floor.get("from") is not None:
                clean["from"] = int(self._parse_int(floor["from"]))
            if floor.get("to") is not None:
                clean["to"] = int(self._parse_int(floor["to"]))
            q["floor"] = clean if clean else None
        else:
            q["floor"] = None

        results: List[Listing] = []

        launch_args = {"headless": not debug, "args": ["--disable-blink-features=AutomationControlled"]}
        context_args = {
            "locale": "uk-UA",
            "viewport": {"width": 1366, "height": 900},
            "user_agent": (self.user_agent or
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        }
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}

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

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_args)
            try:
                context = browser.new_context(**context_args)
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

                page = context.new_page()
                page.set_default_timeout(15000)
                page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})
                if debug:
                    page.set_viewport_size({"width": 1366, "height": 900})

                # ===== 1) сторінка 1
                url = self.build_url(q, page=1)
                try:
                    page.goto(url, timeout=15000, wait_until="domcontentloaded")
                    accept_cookies(page)

                    # 👇 кадр після завантаження
                    self._emit_image(page, f"OLX старт: {url}")

                    # легкий lazy‑scroll
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

                    # 👇 кадр після прокрутки
                    self._emit_image(page, "Після прокрутки (сторінка 1)")

                    # визначаємо кількість сторінок
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

                    batch = self._extract_listings_from_page(page)
                    results.extend(batch)

                    # ===== 2) стор. 2..N
                    for pg in range(2, total_pages + 1):
                        url = self.build_url(q, page=pg)
                        try:
                            page.goto(url, timeout=15000, wait_until="domcontentloaded")
                            accept_cookies(page)

                            # 👇 кадр після відкриття сторінки N
                            self._emit_image(page, f"Сторінка {pg}: {url}")

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
                            batch = self._extract_listings_from_page(page)
                            results.extend(batch)
                        except PWTimeout:
                            page.wait_for_timeout(2500 + random.randint(0, 1000))
                            continue
                except PWTimeout:
                    page.wait_for_timeout(2500 + random.randint(0, 1000))
            finally:
                browser.close()

        # дедуп
        uniq, seen = [], set()
        for it in results:
            key = it.id or it.url
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)

        # fallback: без площі
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
                    empty_streak = 0
                    for pg in range(1, int(relaxed.get("max_pages", 2)) + 1):
                        url = self.build_url(relaxed, page=pg)
                        try:
                            page.goto(url, timeout=15000, wait_until="domcontentloaded")
                            page.wait_for_timeout(random.randint(300, 600))
                            batch = self._extract_listings_from_page(page)
                            if len(batch) == 0:
                                empty_streak += 1
                                if empty_streak >= 3:
                                    break
                            else:
                                empty_streak = 0
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

        return uniq


