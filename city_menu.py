from __future__ import annotations

from math import ceil

from telebot import types

from app_config import CITY_NAMES

CITY_PAGE_SIZE = 8

CATEGORY_CITY_PREFIX = {
    "apartment": "city_",
    "house": "house_city_",
    "apartment_buy": "city_",
    "house_buy": "house_city_",
    "room": "rooms_city_",
    "office": "office_city_",
}

CATEGORY_CITY_TEXT = {
    "apartment": "🌍 *Оберіть місто* для пошуку квартири в оренду",
    "house": "🌍 *Оберіть місто* для пошуку будинку в оренду",
    "apartment_buy": "🌍 *Оберіть місто* для купівлі квартири без комісії",
    "house_buy": "🌍 *Оберіть місто* для купівлі будинку без комісії",
    "room": "🌍 *Оберіть місто* для пошуку кімнати",
    "office": "🌍 *Оберіть місто* для пошуку офісу",
}


def city_caption(category: str, page: int = 0) -> str:
    page = _clamp_page(page)
    return f"{CATEGORY_CITY_TEXT.get(category, CATEGORY_CITY_TEXT['apartment'])}\n\nСторінка {page + 1}/{_total_pages()}"


def build_city_markup(category: str, page: int = 0) -> types.InlineKeyboardMarkup:
    prefix = CATEGORY_CITY_PREFIX.get(category, CATEGORY_CITY_PREFIX["apartment"])
    page = _clamp_page(page)
    start = page * CITY_PAGE_SIZE
    cities = CITY_NAMES[start:start + CITY_PAGE_SIZE]

    markup = types.InlineKeyboardMarkup(row_width=2)
    for i in range(0, len(cities), 2):
        row = cities[i:i + 2]
        markup.add(*[types.InlineKeyboardButton(city, callback_data=f"{prefix}{city}") for city in row])

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"cities_page:{category}:{page - 1}"))
    if page < _total_pages() - 1:
        nav.append(types.InlineKeyboardButton("Далі ▶️", callback_data=f"cities_page:{category}:{page + 1}"))
    if nav:
        markup.add(*nav)

    markup.add(types.InlineKeyboardButton("🔁 Повернутись назад", callback_data="back_to_menu"))
    return markup


def parse_city_page_callback(data: str) -> tuple[str, int] | None:
    if not (data or "").startswith("cities_page:"):
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return parts[1], _clamp_page(int(parts[2]))
    except ValueError:
        return None


def _total_pages() -> int:
    return max(1, ceil(len(CITY_NAMES) / CITY_PAGE_SIZE))


def _clamp_page(page: int) -> int:
    return max(0, min(page, _total_pages() - 1))
