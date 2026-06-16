from __future__ import annotations

# providers/olx/olx_office.py
import re
import unicodedata
import random
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_utils import safe_scroll as _safe_scroll

# === Категорія OLX «Офіси» (оренда) ===
PATH_CATEGORY = "nedvizhimost/kommercheskaya-nedvizhimost/arenda-kommercheskoy-nedvizhimosti/ofisy"

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
from app_config import CITY_DISTRICTS
from app_config import OLX_DISTRICT_IDS as DISTRICT_IDS
from app_config import OLX_PRIMARY_CITY_SLUG as PRIMARY_CITY_SLUG

SORT_MAP = {
    "newest": "created_at:desc",
    "cheapest": "price:asc",
    "expensive": "price:desc",
}

MAX_FLOOR_DEFAULT = 26
MAX_PAGES_HARD = 50

try:
    from ..base import Provider, Listing      # якщо файл у providers/olx/*
except Exception:
    from providers.base import Provider, Listing  # якщо файл у корені пакета providers/


class OlxProviderOffice(Provider):
    source = "olx"

    def __init__(self, user_agent: str | None = None, proxy: str | None = None, always_no_fee: bool = True):
        self.user_agent = user_agent
        self.proxy = proxy
        self.always_no_fee = always_no_fee

    # ========= helpers =========
    def _norm(self, s: str | None) -> str:
        if not s:
            return ""
        s = unicodedata.normalize("NFKD", s).lower()
        return s.replace("’", "'").replace("ʼ", "'").replace("`", "'")

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
        """
        Перетворюємо список назв районів -> список ID OLX.
        """
        if not districts:
            return []
        if isinstance(districts, str):
            districts = [districts]
        else:
            try:
                districts = list(districts)
            except:
                return []

        # «всі райони» → без фільтра
        for d in districts:
            if self._norm(str(d)) in ("всі райони", "всі райони 👀", "всі"):
                return []

        # city_slug може бути «kiev» АБО вже «Київ»
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

    def _budget_params(self, price_from: int | None, price_to: int | None) -> str:
        parts = []
        try:
            if price_from is not None:
                price_from = max(0, int(re.sub(r"\D", "", str(price_from))))
                parts.append(f"&search[filter_float_price:from]={price_from}")
            if price_to is not None and str(price_to).strip().lower() not in ("", "не обмежено", "не обмежено.", "none"):
                price_to = max(0, int(re.sub(r"\D", "", str(price_to))))
                if price_from is not None and price_to < price_from:
                    price_to = price_from
                parts.append(f"&search[filter_float_price:to]={price_to}")
        except Exception as e:
            print("[WARN] _budget_params parse error:", e)
            return ""
        return "".join(parts)

    def _no_fee_param(self, no_fee: bool | None) -> str:
        # Для офісів просили фільтрувати БЕЗ комісії -> у OLX це filter_enum_commission=1
        return "&search[filter_enum_commission][0]=1"

    def _area_params(self, area: Dict[str, Any] | None) -> str:
        if not area:
            return ""
        val = area.get("from")
        if val is None:
            return ""
        try:
            n = int(re.sub(r"\D", "", str(val)))
            if n < 0:
                n = 0
        except Exception as e:
            print("[WARN] _area_params parse error:", e, "| area=", area)
            return ""
        return f"&search[filter_float_total_area:from]={n}"

    def _floor_params(self, floor: Dict[str, Any] | None) -> str:
        if not floor:
            return ""
        s = []
        try:
            if "from" in floor and floor["from"] is not None:
                s.append(f"&search[filter_float_floor:from]={int(floor['from'])}")
            if "to" in floor and floor["to"] is not None:
                s.append(f"&search[filter_float_floor:to]={int(floor['to'])}")
        except Exception as e:
            print("[WARN] _floor_params parse error:", e, "| floor=", floor)
        return "".join(s)

    def floor_preset(self, preset: str | None) -> dict | None:
        if not preset:
            return None
        p = preset.strip().lower()
        if p in ("any", "будь-який поверх", "будь-який поверх🥲"):
            return None
        if p in ("no_1st", "без 1 поверху", "без першого"):
            return {"from": 2}
        if p in ("no_2nd", "без 2 поверху", "без другого"):
            return {"from": 3}
        if p in ("no_top", "без останнього поверху"):
            return {"to": MAX_FLOOR_DEFAULT}
        if p.startswith("до "):
            # "до 7" → {"to": 7}
            try:
                num = int(re.sub(r"\D", "", p))
                return {"to": num}
            except Exception:
                return None
        if p.startswith("range_"):
            try:
                _, a, b = p.split("_")
                return {"from": int(a), "to": int(b)}
            except Exception:
                return None
        return None

    def _sort_param(self, sort: str | None) -> str:
        if not sort:
            return ""
        s = SORT_MAP.get(sort)
        return f"&search[order]={s}" if s else ""

    # -------- URL builder (БЕЗ кімнат/тварин) --------
    def build_url(self, query: Dict[str, Any], page: int = 1) -> str:
        base = "https://www.olx.ua/uk"
        path_category = PATH_CATEGORY
        city_input = (query.get("city") or "").strip()
        city_slug = (query.get("city_slug") or "").strip().lower() or self._best_city_slug(city_input)
        path_city = f"/{city_slug}" if city_slug else ""

        print("[OFFICE build_url] city_input=", city_input, "| city_slug=", city_slug)
        print("[OFFICE build_url] districts=", query.get("districts"))
        print("[OFFICE build_url] price_from/to=", query.get("price_from"), query.get("price_to"))
        print("[OFFICE build_url] area=", query.get("area"))
        print("[OFFICE build_url] floor=", query.get("floor"))
        print("[OFFICE build_url] sort=", query.get("sort"))

        q = ""
        q += self._district_params(city_slug, query.get("districts"))

        q += self._budget_params(query.get("price_from"), query.get("price_to"))
        q += self._no_fee_param(query.get("no_fee"))



        q += self._area_params(query.get("area"))
        q += self._floor_params(query.get("floor"))
        q += self._sort_param(query.get("sort"))

        q = f"?currency=UAH{q}"
        if page and page > 1:
            q += f"&page={page}"

        url = f"{base}/{path_category}{path_city}/{q}"
        print("[OFFICE build_url] =>", url)
        return url

    # -------- парсинг карток на сторінці --------
    def _hq_image_url(self, url: str | None) -> str | None:
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

        # ширший контейнер списку
        grid = page.query_selector("[data-testid='listing-grid'], [data-cy='listing-grid']")
        scope = grid or page

        # ширші селектори карток (OLX інколи міняє data-* атрибути)
        cards = scope.query_selector_all(
            "div[data-cy='l-card'], article[data-testid='ad-card'], article[data-cy='l-card'], div[data-testid='l-card']"
        )

        print(f"[OFFICE extract] found raw cards: {len(cards)}")

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
            except Exception as e:
                print("[OFFICE extract] card parse error:", e)
                continue

        print(f"[OFFICE extract] parsed listings: {len(out)}")
        return out

    # -------- основний пошук --------
    def search(self, query: Dict[str, Any]) -> List[Listing]:
        q = dict(query)
        debug = bool(q.get("debug"))

        # НОРМАЛІЗАЦІЯ state → формат провайдера
        # floor: з «до 7», «Будь-який поверх🥲», «Без 1 поверху» тощо
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
                # Спроба витягти з текстового «до 7»
                try:
                    s = str(floor or "").strip().lower()
                    if s.startswith("до "):
                        num = int(re.sub(r"\D", "", s))
                        q["floor"] = {"to": num}
                    else:
                        q["floor"] = None
                except Exception:
                    q["floor"] = None

        # area: очікуємо {"from": N} (у твоєму UI — "від 50 м2")
        area_label = q.get("area_label")
        if area_label and not q.get("area"):
            try:
                v = int(re.sub(r"\D", "", str(area_label)))
                q["area"] = {"from": v}
            except Exception:
                q["area"] = None

        # ЛОГ вхідних параметрів після нормалізації
        print("[OFFICE search] IN →", {
            "city": q.get("city"),
            "city_slug": q.get("city_slug"),
            "districts": q.get("districts"),
            "price_from": q.get("price_from"),
            "price_to": q.get("price_to"),
            "area": q.get("area"),
            "floor": q.get("floor"),
            "sort": q.get("sort"),
            "max_pages": q.get("max_pages"),
        })

        results: List[Listing] = []

        # Антибот налаштування браузера
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

                # ===== 1) Перша сторінка
                url = self.build_url(q, page=1)
                print("[OFFICE search] GOTO page 1:", url)
                try:
                    page.goto(url, timeout=15000, wait_until="domcontentloaded")
                    accept_cookies(page)
                    try:
                        page.wait_for_selector("[data-testid='listing-grid'], [data-cy='listing-grid']", timeout=5000)
                    except Exception:
                        print("[OFFICE search] listing grid not immediately visible")

                    # Прокрутка, щоб підвантажились картки
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

                    # Скільки сторінок у пагінації
                    def _detect_total_pages() -> int:
                        try:
                            hrefs = page.evaluate("""
                                () => Array.from(document.querySelectorAll("a[href*='&page='], a[href*='?page=']"))
                                             .map(a => a.getAttribute('href') || '')
                            """)
                        except Exception:
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
                    print(f"[OFFICE search] pagination: detected={detected}, using={total_pages}")

                    batch = self._extract_listings_from_page(page)
                    print(f"[OFFICE search] page 1 listings: {len(batch)}")
                    results.extend(batch)

                    # ===== 2) Інші сторінки 2..N
                    for pg in range(2, total_pages + 1):
                        url = self.build_url(q, page=pg)
                        print("[OFFICE search] GOTO page", pg, ":", url)
                        try:
                            page.goto(url, timeout=15000, wait_until="domcontentloaded")
                            accept_cookies(page)

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
                            print(f"[OFFICE search] page {pg} listings: {len(batch)}")
                            results.extend(batch)
                        except PWTimeout:
                            print(f"[OFFICE search] timeout on page {pg}, continue")
                            page.wait_for_timeout(2500 + random.randint(0, 1000))
                            continue

                except PWTimeout:
                    print("[OFFICE search] timeout on page 1 (initial)")
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
        print(f"[OFFICE search] dedup: {len(results)} → {len(uniq)}")

        # ===== 4) Alias‑retry міст (kiev/kyiv тощо)
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
                        print(f"[OFFICE search] retry with city_slug={alt_slug}")
                        cand = self.search(alt)
                        if cand:
                            return cand

        # ===== 5) Fallback (послаблюємо площу)
        if not uniq:
            print("[OFFICE search] Fallback: try without area filter")
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
                        print("[OFFICE fallback] GOTO page", pg, ":", url)
                        try:
                            page.goto(url, timeout=15000, wait_until="domcontentloaded")
                            page.wait_for_timeout(random.randint(300, 600))
                            batch = self._extract_listings_from_page(page)
                            print(f"[OFFICE fallback] page {pg}: {len(batch)}")
                            if len(batch) == 0:
                                empty_streak += 1
                                if empty_streak >= 3:
                                    print(f"[OFFICE fallback] stopping after {empty_streak} empty pages in a row")
                                    break
                            else:
                                empty_streak = 0
                            results2.extend(batch)
                        except PWTimeout:
                            print(f"[OFFICE fallback] timeout on page {pg}")
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
            print(f"[OFFICE fallback] dedup: {len(results2)} → {len(uniq2)}")
            return uniq2

        return uniq

