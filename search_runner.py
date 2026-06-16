from __future__ import annotations

# search_runner.py

from typing import Dict, Any, Optional, Union, List
import re

from providers.aggregate import Aggregator

# ✅ імпорти фабрик провайдерів
from providers.olx_provider import create as create_olx



# ------------------------- утиліти парсингу -------------------------

def _coerce_to_str(v: Union[str, List[str], None]) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, list):
        return str(v[0]) if v else None
    return str(v)

def _parse_budget(label: Union[str, int, None]) -> Optional[int]:
    """
    Приймає: 10000, "10000", "10 000", "від 10 тис.", "до 60 тис.", "15k", "20 тис", "12 тис"
    Повертає: int у гривнях або None.
    """
    if label is None:
        return None
    if isinstance(label, int):
        return label
    s = str(label).lower().strip()

    m = re.findall(r"\d+", s)
    if not m:
        return None
    num = int("".join(m))  # "10 000" -> 10000

    if "тис" in s or "тыс" in s or "k" in s:
        num *= 1000
    return num

def _parse_area(area_label: Union[str, List[str], None]) -> Dict[str, Any] | None:
    """
    Парсить площу з рядків/списків:
      - "Будь-яка" -> None
      - "від 50 м²" -> {"from": 50}
      - "до 80 м2"  -> {"to": 80}
      - "45"        -> {"from":45, "to":45}
      - "30–60" або "30-60" або "30 — 60" -> {"from":30, "to":60}
    Повертає ТІЛЬКИ {"from": int?, "to": int?} або None.
    """
    s = _coerce_to_str(area_label).strip()
    if not s or "будь-яка" in s.lower():
        return None

    # приберемо одиниці виміру та зайві пробіли: "м²", "м2", "m2"
    s = re.sub(r"(м²|м2|m2)", "", s, flags=re.IGNORECASE).strip()

    # нормалізуємо дефіси: en-dash, em-dash, minus -> звичайний дефіс
    s = re.sub(r"[–—−]", "-", s)

    # 1) діапазон "30-60" (із можливими пробілами)
    m_range = re.search(r"^\s*(\d+)\s*-\s*(\d+)\s*$", s)
    if m_range:
        lo, hi = int(m_range.group(1)), int(m_range.group(2))
        if lo > hi:
            lo, hi = hi, lo
        return {"from": lo, "to": hi}

    # 2) "від N"
    m_from = re.search(r"від\s*(\d+)", s, flags=re.IGNORECASE)
    if m_from:
        return {"from": int(m_from.group(1))}

    # 3) "до N"
    m_to = re.search(r"до\s*(\d+)", s, flags=re.IGNORECASE)
    if m_to:
        return {"to": int(m_to.group(1))}

    # 4) поодиноке число -> точне
    m_exact = re.search(r"(\d+)", s)
    if m_exact:
        v = int(m_exact.group(1))
        return {"from": v, "to": v}

    return None

def _parse_rooms(rooms_label: Union[str, List[str], None]) -> Optional[int]:
    s = _coerce_to_str(rooms_label)
    if not s or "Будь-яка" in s:
        return None
    digits = re.findall(r"\d+", s)
    return int(digits[0]) if digits else None

def _parse_floor_to(floors: List[str] | None) -> Dict[str, Any] | None:
    if not floors:
        return None
    best_to = None
    best_from = None
    for f in floors:
        if not f:
            continue
        s = str(f).lower()
        m_to = re.search(r"до\s*(\d+)", s)
        m_from = re.search(r"від\s*(\d+)", s)
        m_num = re.search(r"(\d+)", s)
        if m_to:
            val = int(m_to.group(1))
            best_to = val if best_to is None else max(best_to, val)
        elif "від" in s and m_from:
            val = int(m_from.group(1))
            best_from = val if best_from is None else min(best_from, val)
        elif m_num:
            val = int(m_num.group(1))
            best_to = val if best_to is None else max(best_to, val)
    if best_to is None and best_from is None:
        return None
    out: Dict[str, Any] = {}
    if best_from is not None:
        out["from"] = best_from
    if best_to is not None:
        out["to"] = best_to
    return out


# ------------------------- побудова запиту зі стану бота -------------------------

def build_query_from_state(
    city: str | None,
    city_slug: str | None,
    districts: list[str],
    floors: list[str],
    area_label: str | None,
    rooms_label: str | None,
    price_min: int | None,
    price_max: int | None,
    has_pet: bool | None
) -> Dict[str, Any]:
    """
    Єдина функція для складання універсального запиту (OLX + DOM.RIA).
    Ми одразу додаємо ключі, які потрібні DOM.RIA:
      - floor_to (для кімнат/офісів)
      - area_from (для офісів)
      - price_from_uah / price_to_uah (для кімнат)
    """
    area = _parse_area(area_label)
    rooms = _parse_rooms(rooms_label)
    floor_range = _parse_floor_to(floors)

    price_from = _parse_budget(price_min)
    price_to   = _parse_budget(price_max)

    q: Dict[str, Any] = {
        "city": city,
        "city_slug": city_slug,
        "districts": districts or [],
        "floors": floors or [],
        "area": area,                         # OLX
        "rooms": rooms,                       # OLX (кількість кімнат)
        "price_from": price_from,             # OLX
        "price_to": price_to,                 # OLX
        "allows_pets": bool(has_pet) if has_pet is not None else None,
        "no_fee": True,
        "sort": "newest",
        "max_pages": 3,
        "floor": floor_range,                 # OLX

    }
    return q

# ------------------------- сумісна обгортка під OLX (та DOM.RIA) -------------------------

def build_query_from_state_for_olx(
    *,
    category: str | None,
    city: str | None,
    city_slug: str | None,
    districts: list[str],
    floors: list[str],
    area_label: str | None = None,
    rooms_label: str | list[str] | None = None,   # <— приймаємо і str, і list
    price_min: int | str | None = None,
    price_max: int | str | None = None,
    has_pet: bool | None = None,
    sort: str | None = "newest",
    max_pages: int = 3,
) -> dict:


    q = build_query_from_state(
        city=city,
        city_slug=city_slug,
        districts=districts,
        floors=floors,
        area_label=area_label,
        rooms_label=rooms_label,          # <— передаємо як список
        price_min=price_min,
        price_max=price_max,
        has_pet=has_pet,
    )
    q["category"] = (category or "apartment").lower()
    if sort is not None:
        q["sort"] = sort
    if max_pages is not None:
        q["max_pages"] = max_pages
    # Додамо дубль ключем, який читає провайдер
    if rooms_label:
        q["rooms"] = rooms_label
        q["rooms_label"] = rooms_label

    return q



# ------------------------- запуск пошуку по провайдерам -------------------------

def run_search(query: Dict[str, Any], limit: int = 30):
    """
    Тепер шукаємо одразу на OLX + DOM.RIA.
    Категорію беремо з query['category'] (fallback: apartment).
    """
    category = (query.get("category") or "apartment").lower()

    providers = [
        create_olx(category),

    ]

    # трохи діагностики
    print(f"[RUN] category={category}")
    print(f"[RUN] unified query → {query}")

    agg = Aggregator(providers)
    return agg.search(query, limit=limit)


# ------------------------- (не використовується тут напряму, залишив як було) -------------------------

def _pets_param(allows_pets: bool | None) -> str:
    if allows_pets is None:
        return ""
    if allows_pets is False:
        return "&search[filter_enum_pets][0]=no"
    vals = ["yes_cat", "yes_small_dog", "yes_medium_dog", "yes_big_dog", "yes_other"]
    return "".join(f"&search[filter_enum_pets][{i}]={v}" for i, v in enumerate(vals))

