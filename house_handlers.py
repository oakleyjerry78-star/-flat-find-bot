from __future__ import annotations

from typing import Any
from telebot import types
import threading
import html, re
import json
from io import BytesIO
import requests
from PIL import Image
from providers.olx_provider import get_olx_provider
from listing_cache import prepare_cards_for_display, query_cards_for_query, upsert_listings
from city_menu import build_city_markup, city_caption
from app_config import BRAND_NAME, CITY_SLUGS
from background_indexer import enqueue_index_job
from free_access import free_views_used_up, has_active_subscription as has_paid_access, register_listing_view
from favorites import remember_card
from gsheets import get_sub_info
from media_utils import edit_step_photo, send_step_photo
from playwright_utils import safe_scroll as _safe_scroll

import traceback
from telebot.apihelper import ApiTelegramException



user_selected_districts = {}
user_selected_city = {}
user_selected_house_type = {}  # {chat_id: List[str]}
user_budget_min = {}
user_budget_max = {}
user_has_pet = {}
# Глобальні сховища (якщо в іншому модулі — імпортуй звідти і прибери ці рядки)
user_loading_status = globals().get("user_loading_status", {})
user_listings = globals().get("user_listings", {})
user_page = globals().get("user_page", {})
user_prefs = globals().get("user_prefs", {})  # {'allows_pets': bool, 'pet_types': [...]}
user_selected_floors = {}
user_selected_rooms = {}
user_selected_area = {}
from state import current_category  # {chat_id: str}

city_url_slug_map = CITY_SLUGS

# місто -> область так, як показує OLX у шапці результатів
OBLAST_BY_CITY = {
    "Київ": "Київська область",
    "Одеса": "Одеська область",
    "Львів": "Львівська область",
    "Дніпро": "Дніпропетровська область",
    "Івано-Франківськ": "Івано-Франківська область",
    "Луцьк": "Волинська область",
    # за потреби додай інші міста
}

house_types = [
    "Котедж", "Дуплекс", "Таунхаус",
    "Садиба", "Модульний", "Маєток"
]

city_districts_map: dict[str, list[str] | list[Any]] = {
    "Київ": ["Дарницький", "Деснянський", "Дніпровський", "Печерський", "Голосіївський", "Шевченківський", "Солом’янський", "Подільський", "Оболонський", "Святошинський"],
    "Одеса": ["Приморський", "Київський", "Хаджибейський", "Пересипський"],
    "Львів": ["Галицький", "Залізничний", "Личаківський", "Сихівський", "Франківський", "Шевченківський"],
    "Дніпро": ["Амур-Нижньодніпровський", "Індустріальний", "Новокодацький", "Самарський", "Соборний", "Центральний", "Чечелівський", "Шевченківський"],
    "Івано-Франківськ": [],
    "Луцьк": []
}

from app_config import CITY_DISTRICTS as city_districts_map
from app_config import CITY_SLUGS as city_url_slug_map
from app_config import CITY_TO_OBLAST as OBLAST_BY_CITY

CAPTION_HOUSE_TYPES = (
    "🏡 *Оберіть тип(и) будинку*\n\n"
    "_Можна вибрати кілька варіантів._\n"
    "_Якщо натиснеш «Всі типи» — буде пошук за всіма типами домів._"
)

CAPTION_DISTRICTS = (
    "🌆 *Обери район(и)*\n\n"
    "_Можна вибрати кілька варіантів._\n"
    "_Якщо натиснеш «Всі райони» — буде пошук за всіма районами._"
)

# --- константи для "великих" вибірок
BIG_RESULTS_TOTAL_THRESHOLD = 50   # коли всього багато
BIG_RESULTS_NEW_THRESHOLD   = 20   # або якщо нових докинули багато
PAGE_SIZE = 1
FREE_PREVIEW_LIMIT = 3


def _has_active_subscription(user_id: int | str) -> bool:
    return has_paid_access(user_id)


def _subscription_gate_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔓 Відкрити повний доступ", callback_data="subscribe_month"))
    kb.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))
    return kb


def _send_subscription_gate(bot, chat_id: int):
    bot.send_message(
        chat_id,
        "🔒 Безкоштовний ліміт на цей місяць вичерпано.\n\n"
        "Без підписки доступно 3 оголошення на місяць. З підпискою можна переглядати варіанти без обмежень.",
        reply_markup=_subscription_gate_markup(),
    )


# спільні стейти (виносимо у глобал, щоби не дублікувати між модулями)
next_prompt_msg_id = globals().get("next_prompt_msg_id", {})

def _get_house_types(chat_id: int) -> list[str]:
    v = user_selected_house_type.get(chat_id)
    if isinstance(v, str):
        v = v.strip()
        return [v] if v else []
    return list(v or [])


def _debug_dump(label: str, data: dict | None):
    try:
        print(f"{label} =\n" + json.dumps(data or {}, ensure_ascii=False, indent=2))
    except Exception:
        print(f"{label} =", data)


def format_city_line(city: str | None) -> str:
    """
    Повертає рядок як на OLX у шапці: 'Київ, Київська область'
    Якщо міста немає в мапі — вертає просто city.
    """
    if not city:
        return ""
    oblast = OBLAST_BY_CITY.get(city.strip(), "")
    return f"{city}, {oblast}" if oblast else city

def format_full_location(city: str | None, district: str | None = None) -> str:
    """
    Для одного району — як у полі пошуку OLX: 'Київ, Київ, Голосіївський'
    Якщо району нема — повертає те саме, що format_city_line().
    """
    if not city:
        return ""
    if district:
        # На OLX показують 'Місто, Місто, Район'
        return f"{city}, {city}, {district}"
    return format_city_line(city)


# Якщо у тебе вже є ця функція — видали цей shim і імпортуй реальну
def build_query_from_state_for_olx(
    category: str,
    city: str | None,
    city_slug: str | None,
    districts: list[str] | None = None,
    price_min: str | int | None = None,
    price_max: str | int | None = None,
    has_pet: str | bool | None = None,
    house_type: str | None = None,
    sort: str = "newest",
    max_pages: int = 3,
) -> dict:
    """
    SHIM: мінімальний конструктор запиту.
    Якщо у тебе є свій — використовуй його.
    """
    # нормалізація бюджету
    def _to_int_thousands(v):
        if v in (None, "", "Не обмежено"):
            return None
        if isinstance(v, int):
            return v
        digits = "".join(ch for ch in str(v) if ch.isdigit())
        return int(digits) * 1000 if digits else None

    return {
        "category": category,               # "house"
        "city": city,
        "city_slug": city_slug or "",
        "districts": districts or [],
        "price_min": _to_int_thousands(price_min),
        "price_max": _to_int_thousands(price_max),
        "has_pet": (has_pet == "Має") if isinstance(has_pet, str) else bool(has_pet),
        "house_type": house_type,          # якщо твій провайдер це не підтримує — ігноруватиме
        "sort": sort,
        "max_pages": max_pages,
    }

def _to_int_clean_thousands(v):
    if v in (None, "", "Не обмежено"):
        return None
    if isinstance(v, int):
        return v
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return int(digits) * 1000 if digits else None

def _room_bounds_from_labels(labels: list[str] | None) -> tuple[int | None, int | None]:
    """'2 кімнати' -> (2,2). Якщо обрано кілька — беремо min..max."""
    if not labels:
        return None, None
    nums = []
    for s in labels:
        m = re.search(r"\d+", s or "")
        if m:
            nums.append(int(m.group(0)))
    if not nums:
        return None, None
    return min(nums), max(nums)

def _area_from_label(label: str | None) -> int | None:
    # "від 60 м2" -> 60
    if not label:
        return None
    m = re.search(r"\d+", str(label))
    return int(m.group(0)) if m else None





# Кадрові утиліти (колаж, кроп)
def _hq_url(url: str) -> str:
    if not url:
        return url
    url = re.sub(r";s=\d+x\d+", "", url)
    url = re.sub(r"image-size=\d+x\d+;", "", url)
    url = re.sub(r"quality=\d+", "quality=100", url)
    return url

def _center_crop_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    w, h = img.size
    if w == 0 or h == 0:
        return img
    src_ratio = w / h
    dst_ratio = target_w / target_h
    if src_ratio > dst_ratio:
        new_h = target_h
        new_w = int(new_h * src_ratio)
    else:
        new_w = target_w
        new_h = int(new_w / src_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))

def create_collage(urls, size=(1080, 1080), bg=(18, 24, 33)):
    if not urls:
        return None
    urls = [_hq_url(u) for u in urls if u][:4]

    W, H = size
    canvas = Image.new("RGB", size, bg)

    def _load(u):
        r = requests.get(u, timeout=10)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")

    if len(urls) == 1:
        try:
            img = _center_crop_cover(_load(urls[0]), W, H)
            canvas.paste(img, (0, 0))
            return canvas
        except Exception:
            return None

    cell_w, cell_h = W // 2, H // 2
    for i, url in enumerate(urls):
        try:
            img = _center_crop_cover(_load(url), cell_w, cell_h)
            x = (i % 2) * cell_w
            y = (i // 2) * cell_h
            canvas.paste(img, (x, y))
        except Exception:
            continue
    return canvas

def register_house_handlers(bot):
    def safe_send_message(chat_id: int, text: str, **kwargs):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except Exception as ex:
            print(f"[send_message error] {ex} | text={text!r}")

    def safe_delete_message(chat_id: int, message_id: int):
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    def safe_edit_reply_markup(chat_id: int, message_id: int, reply_markup=None):
        try:
            bot.edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
        except Exception:
            pass

    # ВИНОСИМО ОКРЕМО — не в середині callback_query_handler
    def build_district_markup(districts_list, selected):
        markup = types.InlineKeyboardMarkup(row_width=2)

        for i in range(0, len(districts_list), 2):
            row = districts_list[i:i + 2]
            buttons = []
            for d in row:
                check = "✅" if d.strip() in [s.strip() for s in selected] else ""
                buttons.append(types.InlineKeyboardButton(f"{check} {d}".strip(),
                                                          callback_data=f"house_district_{d}"))
            markup.add(*buttons)

        # позначка “Всі райони” (візуально, якщо фактично обрані всі)
        all_selected = all(d.strip() in [s.strip() for s in selected] for d in districts_list) and len(
            districts_list) > 0
        all_label = "✅ Всі райони" if all_selected else "Всі райони"

        # ⬇️ головне: натискання одразу чистить вибір і переходить далі
        markup.add(
            types.InlineKeyboardButton(all_label, callback_data="house_districts_all_next"),
            types.InlineKeyboardButton("Далі 👉", callback_data="house_next")
        )
        markup.add(types.InlineKeyboardButton("🔁 Назад", callback_data="house_to_city"))
        return markup

    @bot.callback_query_handler(func=lambda c: c.data == "house_next")
    def house_next_from_districts(c):
        show_house_type_step(c)

    def house_districts(call, city, districts_list):
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        selected = user_selected_districts.get(chat_id, [])

        if districts_list:
            text = CAPTION_DISTRICTS + f"\n\n_✅ Вибрано: {len(selected)} район(ів)_"
            markup = build_district_markup(districts_list, selected)
        else:
            text = CAPTION_DISTRICTS + "\n\n_В цьому місті відсутні райони, просто натисни «Далі 👉»_"
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("Далі 👉", callback_data="house_next"),
                types.InlineKeyboardButton("🔁 Назад", callback_data="house_to_city")
            )

        try:
            edit_step_photo(bot, chat_id, message_id, "district.png", text, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            print("⚠️ edit_step_photo error:", e)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("house_city_"))
    def handle_city(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        city = call.data.replace("house_city_", "")
        user_selected_city[chat_id] = city
        current_category[chat_id] = current_category.get(chat_id, "house")
        house_districts(call, city, city_districts_map[city])

    @bot.callback_query_handler(func=lambda call: call.data == "house_to_city")
    def back_to_city(call):
        bot.answer_callback_query(call.id)
        category = current_category.get(call.message.chat.id, "house")
        send_step_photo(
            bot,
            call.message.chat.id,
            "city.png",
            city_caption(category),
            reply_markup=build_city_markup(category),
            parse_mode="Markdown",
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("house_district_"))
    def toggle_district(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return

        district = call.data.replace("house_district_", "").strip()
        selected = user_selected_districts.get(chat_id, [])

        if district in selected:
            selected.remove(district)
        else:
            selected.append(district)

        user_selected_districts[chat_id] = selected
        house_districts(call, city, city_districts_map[city])

    @bot.callback_query_handler(func=lambda call: call.data == "house_select_all")
    def handle_select_all(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return

        all_districts = [d.strip() for d in (city_districts_map.get(city, []) or [])]
        cur = [s.strip() for s in (user_selected_districts.get(chat_id, []) or [])]

        # якщо вже були обрані всі → очистити; інакше → вибрати всі (лише для галочок у UI)
        if all_districts and set(cur) >= set(all_districts):
            user_selected_districts[chat_id] = []
        else:
            user_selected_districts[chat_id] = all_districts[:]

        house_districts(call, city, city_districts_map[city])

    @bot.callback_query_handler(func=lambda call: call.data == "house_districts_all_next")
    def handle_districts_all_next(call):
        bot.answer_callback_query(call.id, "Шукаємо у всіх районах 🗺️", show_alert=False)
        chat_id = call.message.chat.id

        # очищаємо вибір, щоб у пошук не летіли райони
        user_selected_districts[chat_id] = []

        # одразу переходимо на наступний крок (типи будинків)
        show_house_type_step(call)

    def build_house_type_markup(chat_id: int):
        markup = types.InlineKeyboardMarkup(row_width=2)
        selected = set(s.strip() for s in _get_house_types(chat_id))

        for i in range(0, len(house_types), 2):
            row = []
            for t in house_types[i:i + 2]:
                check = "✅ " if t in selected else ""
                row.append(types.InlineKeyboardButton(f"{check}{t}", callback_data=f"house_type_toggle::{t}"))
            # перевірка
            if not all(isinstance(b, types.InlineKeyboardButton) for b in row):
                raise TypeError("Non-button detected in house_type row")
            if row:
                markup.add(*row)

        all_selected = len(selected) == len(house_types)
        all_label = "✅ Всі типи" if all_selected else "Всі типи"

        markup.add(
            types.InlineKeyboardButton(all_label, callback_data="house_type_all_next"),
            types.InlineKeyboardButton("Далі 👉", callback_data="house_type_next"),
        )
        markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_districts"))
        return markup

    @bot.callback_query_handler(func=lambda c: c.data == "back_to_districts")
    def house_back_to_districts(c):
        chat_id = c.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return
        house_districts(c, city, city_districts_map.get(city, []))

    def show_house_type_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass
        bot.send_message(
            chat_id,
            CAPTION_HOUSE_TYPES,
            reply_markup=build_house_type_markup(chat_id),
            parse_mode="Markdown"
        )

    # ✅ перемикач одного типу (галочка)
    @bot.callback_query_handler(func=lambda c: c.data.startswith("house_type_toggle::"))
    def handle_house_type_toggle(c):
        bot.answer_callback_query(c.id)
        chat_id = c.message.chat.id
        t = c.data.split("::", 1)[1]
        cur = set(s.strip() for s in _get_house_types(chat_id))
        if t in cur:
            cur.remove(t)
        else:
            cur.add(t)
        user_selected_house_type[chat_id] = list(cur)
        try:
            bot.edit_message_text(
                CAPTION_HOUSE_TYPES,
                chat_id=chat_id,
                message_id=c.message.message_id,
                reply_markup=build_house_type_markup(chat_id),
                parse_mode="Markdown",
            )
        except:
            show_house_type_step(c)

    # ✅ “Всі типи” = очистити вибір (нічого не передавати в пошук)
    @bot.callback_query_handler(func=lambda c: c.data == "house_type_all")
    def handle_house_type_all(c):
        bot.answer_callback_query(c.id, "Типи очищено — фільтр не застосовуємо ✅", show_alert=False)
        chat_id = c.message.chat.id
        user_selected_house_type[chat_id] = []  # порожній = не фільтруємо
        # перерисувати, щоб зникли галочки
        try:
            bot.edit_message_text(
                "🏡 *Оберіть тип(и) будинку*\n\n"
                "_Фільтр очищено. Можеш натиснути «Далі 👉», щоб продовжити без фільтра за типом._",
                chat_id=chat_id,
                message_id=c.message.message_id,
                reply_markup=build_house_type_markup(chat_id),
                parse_mode="Markdown",
            )
        except:
            show_house_type_step(c)

    # ✅ “Далі 👉” — переходимо на бюджет, навіть якщо вибір порожній
    @bot.callback_query_handler(func=lambda c: c.data == "house_type_all_next")
    def handle_house_type_all_next(c):
        bot.answer_callback_query(c.id, "Шукаємо всі типи будинків 🏡", show_alert=False)
        chat_id = c.message.chat.id
        user_selected_house_type[chat_id] = []  # порожній = не фільтруємо
        # одразу перехід на крок бюджету
        show_budget_step(c)

    @bot.callback_query_handler(func=lambda c: c.data == "house_type_next")
    def handle_house_type_next(c):
        bot.answer_callback_query(c.id)
        show_budget_step(c)

    def build_budget_from_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        options = [f"від {i} тис." for i in [0, 5, 7, 10, 12, 15, 17, 20, 25, 30, 35, 40, 45, 50]]
        buttons = [types.InlineKeyboardButton(text=o, callback_data=f"house_budget_from_{o}") for o in options]
        markup.add(*buttons)
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="house_to_rooms"))
        return markup

    def show_budget_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "💰 *Обери бюджет*\n\n_Обери ВІД якої вартості в тис. грн. ти розглядаєш будинок 🏡_"

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(
            bot,
            chat_id,
            "budget.png",
            text,
            reply_markup=build_budget_from_markup(),
            parse_mode="Markdown"
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("house_budget_from_"))
    def handle_budget_from(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        selected = call.data.replace("house_budget_from_", "")  # ✅
        user_budget_min[chat_id] = selected
        show_budget_to_step(call)

    def show_budget_to_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "💰 *Обери бюджет*\n\n_Обери ДО якої вартості в тис. грн. ти розглядаєш будинок 🏡_\n\n*чим більше вартість — тим менше старих ремонтів*"

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(bot, chat_id, "budget.png", text, reply_markup=build_budget_to_markup(), parse_mode="Markdown")

    def build_budget_to_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        options = [f"до {i} тис." for i in [40, 45, 50, 55, 60, 65, 70, 75, 80, 90, 100]]
        buttons = [types.InlineKeyboardButton(text=o, callback_data=f"house_budget_to_{o}") for o in options]
        markup.add(*buttons)
        markup.add(types.InlineKeyboardButton("Не маю обмежень по бюджету", callback_data="house_budget_to_any"))  # ✅
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="house_to_budget_from"))
        return markup

    @bot.callback_query_handler(
        func=lambda call: call.data.startswith("house_budget_to_") or call.data == "house_budget_to_any")
    def handle_budget_to(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        selected = "Не обмежено" if call.data == "house_budget_to_any" else call.data.replace("house_budget_to_", "")
        user_budget_max[chat_id] = selected
        house_pet_step(call)



    @bot.callback_query_handler(func=lambda call: call.data == "house_to_rooms")
    def back_to_rooms_from_budget(call):
        show_house_type_step(call)

    def build_pet_keyboard():
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("Маю 🐶🐱", callback_data="house_has_pet"),
            types.InlineKeyboardButton("Не маю ❌", callback_data="house_no_pet")
        )
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="back_to_budget"))
        return markup

    @bot.callback_query_handler(func=lambda call: call.data == "back_to_budget")
    def back_to_budget(call):
        bot.answer_callback_query(call.id)
        show_budget_to_step(call)  # або show_budget_step(call), якщо хочеш повертати на "ВІД ..."

    def house_pet_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "🐶 *Маєш тваринку?*\n\n_Обери, чи маєш ти тваринку ⤵️_"

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(bot, chat_id, "pets.png", text, reply_markup=build_pet_keyboard(), parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda call: call.data in ["house_has_pet", "house_no_pet"])
    def handle_house_pet_selection(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        # зберігаємо вибір
        user_has_pet[chat_id] = "Має" if call.data == "house_has_pet" else "Не має"

        # прибираємо клавіатуру під повідомленням і саме повідомлення
        safe_edit_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        try:
            bot.answer_callback_query(call.id, "⏳ Рахуємо варіанти…", cache_time=0, show_alert=False)
        except ApiTelegramException:
            pass
        safe_delete_message(chat_id, call.message.message_id)

        # епізодичне повідомлення "рахуємо…"
        loading = safe_send_message(chat_id, "⏳ Рахуємо варіанти… Будь ласка, зачекайте.")
        loading_msg_id = loading.message_id if loading else None

        # запускаємо підрахунок (важливо: передаємо loading_msg_id!)
        _start_quick_count_and_show_summary_houses(bot, call, loading_msg_id)

    def quick_count_playwright(provider, query: dict, timeout_ms: int = 12000,
                               screenshot_path: str | None = None) -> int | None:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        import re, random

        def _only_digits(s: str) -> int | None:
            try:
                return int(re.sub(r"[^\d]", "", s or ""))
            except Exception:
                return None

        def _norm(s: str) -> str:
            return (s or "").replace("\xa0", " ").replace("\u202f", " ").strip()




        # Фолбек: оцінимо за пагінацією
        def _estimate_by_pagination(page, per_page: int) -> int | None:
            try:
                hrefs = page.evaluate("""
                    () => Array.from(document.querySelectorAll("a[href*='&page='], a[href*='?page=']"))
                                .map(a => a.getAttribute('href') || '')
                """)
            except Exception:
                hrefs = []
            pages = 1
            for h in hrefs:
                for mm in re.findall(r"[?&]page=(\d+)", h or ""):
                    try:
                        pages = max(pages, int(mm))
                    except:
                        pass
            return pages * per_page if pages > 0 else None

        # ---- якщо районів багато — рахуємо окремо й сумуємо ----
        districts = (query.get("districts") or query.get("district_ids") or [])
        multi = isinstance(districts, (list, tuple)) and len(districts) > 1

        def _strip_radius(q):
            q2 = dict(q)
            q2.pop("dist_km", None)
            return q2

        district_sets = []
        if multi:
            for d in districts:
                q_one = _strip_radius(query)
                q_one["districts"] = [d]
                district_sets.append(q_one)
        else:
            district_sets = [_strip_radius(query)]

        total = 0
        with sync_playwright() as p:
            browser = None
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
                )
                ctx = browser.new_context(
                    locale="uk-UA",
                    viewport={"width": 1366, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
                )
                ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = ctx.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})

                for i, q in enumerate(district_sets):
                    url = provider.build_url(q, page=1)
                    print(f"🔍 URL[{i}]:", url)

                    try:
                        page.goto(url, timeout=timeout_ms + 3000, wait_until="domcontentloaded")
                    except PWTimeout:
                        continue

                    # cookies
                    for sel in ("[data-testid='cookies-popup-accept-all']",
                                "[data-testid='cookiesbar-accept']",
                                "#onetrust-accept-btn-handler",
                                "button:has-text('Прийняти все')",
                                "button:has-text('Погоджуюсь')",
                                "button:has-text('Accept all')"):
                        try:
                            page.locator(sel).first.click(timeout=700)
                            page.wait_for_timeout(120)
                            break
                        except:
                            pass

                    # легкий скрол
                    for _ in range(3):
                        _safe_scroll(page, 1200)
                        page.wait_for_timeout(140 + random.randint(0, 60))

                    host = (page.url.split("/")[2] if "://" in page.url else "").lower()

                    # OLX — як було (швидко і точно)
                    if "olx.ua" in host:
                        try:
                            el = page.locator("span[data-testid='total-count']").first
                            text = el.text_content(timeout=2500) or ""
                            n = _only_digits(text)
                            if n:
                                print(f"   ↳ count[{i}] = {n} (olx header)")
                                total += n
                                continue
                        except Exception:
                            pass
                        # запасний: текст сторінки
                        t = (_norm(page.evaluate("() => document.body.innerText") or "")).lower()
                        m = re.search(r"ми\s+знайшли\s+([\d\s]+)", t)
                        if m:
                            n = _only_digits(m.group(1))
                            if n:
                                print(f"   ↳ count[{i}] = {n} (olx body)")
                                total += n
                                continue
                        # останній фолбек — пагінація × 40
                        est = _estimate_by_pagination(page, per_page=40)
                        print(f"   ↳ count[{i}] = {est} (olx paginate)")
                        total += (est or 0)
                        continue



                    # інші хости — фолбек на пагінацію
                    est = _estimate_by_pagination(page, per_page=20)
                    print(f"   ↳ count[{i}] = {est} (generic paginate)")
                    total += (est or 0)

            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass

        return total if isinstance(total, int) and total >= 0 else None

    def _start_quick_count_and_show_summary_houses(bot, call, loading_msg_id=None):
        chat_id = call.message.chat.id

        def _runner():
            try:
                city = user_selected_city.get(chat_id)
                city_slug = city_url_slug_map.get(city, "")
                districts = user_selected_districts.get(chat_id, []) or []
                price_min = user_budget_min.get(chat_id)
                price_max = user_budget_max.get(chat_id)

                category = current_category.get(chat_id, "house")

                # визначаємо, чи треба прибрати districts
                selected = [s.strip() for s in districts]
                all_d = [s.strip() for s in (city_districts_map.get(city, []) or [])]
                drop_districts = (not selected) or (all_d and set(selected) >= set(all_d))

                # ===== OLX quick-count =====
                q_olx = build_query_from_state_for_olx(
                    category=category,
                    city=city,
                    city_slug=city_slug,
                    districts=([] if drop_districts else selected),
                    price_min=price_min,
                    price_max=price_max,
                    has_pet=user_has_pet.get(chat_id),
                    house_type = user_selected_house_type.get(chat_id),
                    sort="newest",
                    max_pages=1,
                )
                q_olx["price_from"] = q_olx.pop("price_min", None)
                q_olx["price_to"] = q_olx.pop("price_max", None)

                # pets: тільки "no" або нічого
                has_pet_flag = (user_has_pet.get(chat_id) == "Має")
                q_olx["allows_pets"] = True if has_pet_flag else None
                q_olx.pop("pet_types", None)

                _debug_dump("[OLX][HOUSE] q", q_olx)
                enqueue_index_job(category, city)
                cached = query_cards_for_query(
                    category=category,
                    city=city,
                    districts=([] if drop_districts else selected),
                    query=q_olx,
                    limit=100,
                )
                if cached:
                    user_listings[chat_id] = cached
                    user_page[chat_id] = 0
                    olx_count = len(cached)
                else:
                    olx_count = None



            except Exception as e:
                print("[quick_count_playwright house] error:", e)
                olx_count = None

            finally:
                if loading_msg_id:
                    safe_delete_message(chat_id, loading_msg_id)

            # Підсумок тільки по OLX
            total = olx_count if isinstance(olx_count, int) and olx_count > 0 else 0

            print(f"[SUMMARY][HOUSE] olx={olx_count}  total={total}")

            show_final_summary(chat_id, count=total if isinstance(total, int) else None)

        threading.Thread(target=_runner, daemon=True).start()

    def show_final_summary(chat_id, count=None):
        city = user_selected_city.get(chat_id, "—")
        listings = user_listings.get(chat_id, [])

        if isinstance(count, int) and count > 0:
            final_count = count
        elif listings:
            final_count = len(listings)
        else:
            final_count = 0

        districts = user_selected_districts.get(chat_id, []) or []
        sel_types = _get_house_types(chat_id)
        types_line = ", ".join(sel_types) if sel_types else "будь-який"
        budget_from = user_budget_min.get(chat_id, "—")
        budget_to = user_budget_max.get(chat_id, "—")
        pet = "Так" if user_has_pet.get(chat_id) == "Має" else "Ні"

        loc_line = format_city_line(city)
        districts_line = ", ".join(districts) or "—"

        budget_text = (
            f"Бюджет: від {budget_from}" if budget_to == "Не обмежено"
            else f"Бюджет: від {budget_from} до {budget_to}"
        )

        count_phrase = f"*{final_count} варіантів будинків*" if final_count else "*актуальні варіанти будинків*"

        text = (
            f"🏠 *{BRAND_NAME}* підібрав {count_phrase} без комісії за твоїми параметрами.\n\n"
            "👀 *Хочеш переглянути їх або оновити пошук?*\n\n"
            "✅ *Твої параметри:*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Локація:* `{loc_line}`\n"
            f"🏙️ *Райони:* `{districts_line}`\n"
            f"🏡 *Тип:* `{types_line}`\n"
            f"💰 *{budget_text} грн.*\n"
            f"🐶 *Тваринки:* `{pet}`\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔍 Переглянути варіанти", callback_data="house_show_results"),
            types.InlineKeyboardButton("🔄 Оновити параметри", callback_data="house_search")
        )

        send_step_photo(
            bot,
            chat_id,
            "results_found.jpg",
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    # Оновити параметри пошуку
    def house_update_parameters_menu(chat_id):
        text = (
            "🔄 *Оновити параметри пошуку*\n\n"
            "Обери, що хочеш змінити 🔽"
        )

        markup = build_update_parameters_keyboard()

        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

    # 🔄 Оновити параметри пошуку (HOUSE)
    @bot.callback_query_handler(func=lambda call: call.data == "house_search")
    def handle_restart_search(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        # Показати меню з фото та параметрами
        house_update_parameters_menu(chat_id)

    # ⬅️ Назад до результатів з екрана оновлення параметрів
    @bot.callback_query_handler(func=lambda c: c.data == "back_to_results")
    def back_to_results(c):
        bot.answer_callback_query(c.id)
        show_final_summary(c.message.chat.id)

    # 🐶 Перехід у крок "Тваринки" з меню оновлення
    @bot.callback_query_handler(func=lambda c: c.data == "edit_pet")
    def edit_pet(c):
        bot.answer_callback_query(c.id)
        house_pet_step(c)

    # 💰 Перехід у крок "Бюджет від ..."
    @bot.callback_query_handler(func=lambda c: c.data == "house_to_budget_from")
    def house_back_to_budget_from(c):
        bot.answer_callback_query(c.id)
        show_budget_step(c)

    # 🏙️ Редагувати райони з меню оновлення
    @bot.callback_query_handler(func=lambda c: c.data == "house_edit_districts")
    def house_edit_districts(c):
        bot.answer_callback_query(c.id)
        chat_id = c.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return
        house_districts(c, city, city_districts_map.get(city, []))

    # 👉 "Далі" після вибору районів (переходимо до типів будинків)
    @bot.callback_query_handler(func=lambda c: c.data == "house_next")
    def house_next_from_districts(c):
        bot.answer_callback_query(c.id)
        show_house_type_step(c)

    def build_update_parameters_keyboard():
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📍 Місто", callback_data="house_to_city"),
            types.InlineKeyboardButton("🏙️ Райони", callback_data="house_edit_districts"),
            types.InlineKeyboardButton("🏡 Тип будинку", callback_data="house_to_rooms"),
            types.InlineKeyboardButton("💰 Бюджет", callback_data="house_to_budget_from"),
            types.InlineKeyboardButton("🐶 Тваринки", callback_data="edit_pet"),
        )
        markup.add(
            types.InlineKeyboardButton("⬅️ Назад до результатів", callback_data="back_to_results"),
            types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu")
        )
        return markup



    @bot.callback_query_handler(func=lambda c: c.data == "house_show_results")
    def house_show_results(call):
        chat_id = call.message.chat.id
        print(f"[DEBUG] HOUSE_SHOW_RESULTS pressed | chat_id={chat_id}")

        try:
            bot.answer_callback_query(call.id, "Показую варіанти", cache_time=0, show_alert=False)
        except Exception as e:
            print("[DEBUG] answer_callback_query error:", e)

        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception as e:
            print("[DEBUG] edit_message_reply_markup error:", e)

        if user_listings.get(chat_id):
            user_page[chat_id] = 0
            send_house_listing(chat_id)
            return

        loading_msg = None
        try:
            loading_msg = bot.send_message(chat_id, "⏳ Готую перші варіанти…")
        except Exception as e:
            print("[DEBUG] send loading msg error:", e)

        print("[DEBUG] starting _do_house_search_and_send in thread")
        threading.Thread(
            target=_do_house_search_and_send,
            args=(chat_id, loading_msg.message_id if loading_msg else None),
            daemon=True
        ).start()

    # додай біля констант
    USE_PLAYWRIGHT_IN_BG = False  # ← за замовчуванням вимкнено

    def _can_use_playwright_bg() -> bool:
        """Дозволити Playwright у фоні лише якщо явно увімкнено і ми не в asyncio-loop."""
        if not USE_PLAYWRIGHT_IN_BG:
            return False
        try:
            import asyncio
            asyncio.get_running_loop()  # якщо цикл вже біжить — не можна Sync API
            return False
        except RuntimeError:
            # немає активного loop у цьому треді
            return True
        except Exception:
            return False

    def background_parse_houses(chat_id: int, q_olx: dict):
        import math

        use_pw = _can_use_playwright_bg()
        p_ctx = browser = context = page = None  # ресурси Playwright (якщо будемо юзати)

        def accept_cookies(pg):
            for sel in (
                    "[data-testid='cookies-popup-accept-all']",
                    "[data-testid='cookiesbar-accept']",
                    "button#onetrust-accept-btn-handler",
                    "button:has-text('Прийняти все')",
                    "button:has-text('Погоджуюсь')",
                    "button:has-text('Accept all')",
            ):
                try:
                    pg.locator(sel).first.click(timeout=800)
                    pg.wait_for_timeout(150)
                    break
                except Exception:
                    pass

        def _collect_images_for(url: str, limit: int = 4) -> list[str]:
            """Збирання фото зі сторінки оголошення (лише якщо дозволено Playwright)."""
            if not (use_pw and page and url):
                return []
            try:
                page.goto(url, timeout=15000, wait_until="domcontentloaded")
                accept_cookies(page)
                for _ in range(3):
                    _safe_scroll(page, 1200)
                    page.wait_for_timeout(250)

                return _collect_olx_images(page, limit)
            except Exception:
                return []

        try:
            print("[HOUSE][BG] start", chat_id, {"olx": q_olx})

            initial_total = len(user_listings.get(chat_id, []) or [])
            merged = (user_listings.get(chat_id, []) or [])[:]
            seen = {(c.get("_key") or _norm_url(c.get("link") or "")) for c in merged}

            category = current_category.get(chat_id, "house")
            olx = get_olx_provider(category)


            MAX_OLX_PAGES = 15
            olx_pages = MAX_OLX_PAGES

            target_total = 300
            remaining_olx = target_total




            # ініціалізуємо Playwright лише якщо це безпечно й потрібно
            if use_pw:
                from playwright.sync_api import sync_playwright
                p_ctx = sync_playwright().start()
                browser = p_ctx.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                context = browser.new_context(
                    locale="uk-UA",
                    viewport={"width": 1366, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.new_page()
                page.set_default_timeout(12000)
                page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})

            # ---- OLX пачками
            if olx_pages and len(merged) < target_total:
                more_items = olx.search({**q_olx, "max_pages": olx_pages}) or []
                if more_items:
                    try:
                        upsert_listings(category, q_olx.get("city") or user_selected_city.get(chat_id), more_items)
                    except Exception as e:
                        print("[HOUSE background cache upsert][error]", e)
                for it in more_items:
                    if (isinstance(remaining_olx, int) and remaining_olx <= 0) or len(merged) >= target_total:
                        break
                    try:
                        price_uah = getattr(it, "price_uah", None)
                        price_txt = f"{price_uah:,} грн".replace(",", " ") if price_uah else "—"
                        card = {
                            "title": getattr(it, "title", "") or "Без назви",
                            "price": price_txt,
                            "link": getattr(it, "url", "") or "",
                            "img_urls": (getattr(it, "photos", []) or [])[:4],
                            "_key": getattr(it, "id", None) or getattr(it, "url", ""),
                            "src": "olx",
                        }
                        k = card.get("_key") or _norm_url(card.get("link") or "")
                        if not k or k in seen:
                            continue
                        if not card["img_urls"]:
                            card["img_urls"] = _collect_images_for(card["link"], limit=4)

                        seen.add(k)
                        merged.append(card)
                        if isinstance(remaining_olx, int):
                            remaining_olx -= 1
                    except Exception as e:
                        print("[HOUSE][BG][olx card error]", e)
                        continue



            merged = _dedupe_cards(merged)[:target_total]
            user_listings[chat_id] = merged

            new_total = len(merged)
            new_added = max(0, new_total - initial_total)
            print(f"[HOUSE][BG] done: total={new_total} (added {new_added})")

            if new_added > 0:
                _prompt_next_button(chat_id)

        except Exception as e:
            print("[HOUSE][BG][error]", e)
        finally:
            # акуратно закриваємо Playwright, якщо відкривали
            if use_pw:
                try:
                    if page: page.close()
                except Exception:
                    pass
                try:
                    if context: context.close()
                except Exception:
                    pass
                try:
                    if browser: browser.close()
                except Exception:
                    pass
                try:
                    if p_ctx: p_ctx.stop()
                except Exception:
                    pass
            user_loading_status[chat_id] = False

    def _do_house_search_and_send(chat_id: int, loading_msg_id=None):
        print(f"[HOUSE] _do_house_search_and_send START | chat_id={chat_id}")
        try:
            user_loading_status[chat_id] = True

            # ---- 1) СТАН
            city = user_selected_city.get(chat_id)
            city_slug = city_url_slug_map.get(city, "")
            districts = user_selected_districts.get(chat_id, [])
            price_min = user_budget_min.get(chat_id)
            price_max = user_budget_max.get(chat_id)
            has_pet = user_has_pet.get(chat_id)

            category = current_category.get(chat_id, "house")

            sel_types = _get_house_types(chat_id)
            house_type_param = sel_types if sel_types else None  # порожній список => None (не фільтруємо)

            # ---- 2) Q для OLX
            q_olx = build_query_from_state_for_olx(
                category=category,
                city=city,
                city_slug=city_slug,
                districts=districts,
                price_min=price_min,
                price_max=price_max,
                has_pet=has_pet,
                house_type=house_type_param,
                sort="newest",
                max_pages=1,
            )
            q_olx["price_from"] = q_olx.pop("price_min", None)
            q_olx["price_to"] = q_olx.pop("price_max", None)
            q_olx["allows_pets"] = True if user_has_pet.get(chat_id) == "Має" else None
            q_olx.pop("pet_types", None)

            enqueue_index_job(category, city)


            # Якщо обрані всі/жодного району — не передаємо districts
            selected = [s.strip() for s in (user_selected_districts.get(chat_id, []) or [])]
            all_d = [s.strip() for s in (city_districts_map.get(city, []) or [])]
            if (not selected) or (all_d and set(selected) >= set(all_d)):
                q_olx.pop("districts", None)


            #

            # ---- 4) Провайдери + перші сторінки
            olx = get_olx_provider(category)
            cached = prepare_cards_for_display(query_cards_for_query(
                category=category,
                city=city,
                districts=selected,
                query=q_olx,
                limit=100,
                require_photos=False,
            ))
            if cached:
                user_listings[chat_id] = cached
                user_page[chat_id] = 0
                send_house_listing(chat_id)
                threading.Thread(
                    target=background_parse_houses,
                    args=(chat_id, q_olx),
                    daemon=True,
                ).start()
                return
            first_olx = olx.search({**q_olx, "max_pages": 1}) or []
            if first_olx:
                try:
                    upsert_listings(category, city, first_olx)
                except Exception as e:
                    print("[HOUSE cache upsert][error]", e)


            def _to_card(it, src):
                price_uah = getattr(it, "price_uah", None)
                price_txt = f"{price_uah:,} грн".replace(",", " ") if price_uah else "—"
                return {
                    "title": getattr(it, "title", "") or "Без назви",
                    "price": price_txt,
                    "link": getattr(it, "url", "") or "",
                    "img_urls": (getattr(it, "photos", []) or [])[:4],
                    "_key": getattr(it, "id", None) or getattr(it, "url", ""),
                    "src": src,
                }

            olx_cards_all = [_to_card(it, "olx") for it in first_olx]

            merged = prepare_cards_for_display(_dedupe_cards(olx_cards_all[:100]))

            user_listings[chat_id] = merged
            user_page[chat_id] = 0
            if merged:
                send_house_listing(chat_id)
            else:
                safe_send_message(
                    chat_id,
                    "⏳ Поки не знайшов будинків у швидкому режимі. Я вже поставив цей пошук у пріоритет — спробуй ще раз за хвилину.",
                )

            # ---- 5) Фоном — добираємо усе за фактом (із капом для DOM.RIA)
            threading.Thread(
                target=background_parse_houses,
                args=(chat_id, q_olx),
                daemon=True
            ).start()

        except Exception as e:
            print("[HOUSE][FATAL] _do_house_search_and_send:", e, traceback.format_exc())
            try:
                safe_send_message(chat_id, "❌ Пошук тимчасово не вдався. Спробуй ще раз за хвилину або обери інші параметри.")
            except Exception:
                pass
            user_loading_status[chat_id] = False
        finally:
            if loading_msg_id:
                try:
                    bot.delete_message(chat_id, loading_msg_id)
                except Exception as ex:
                    print("[HOUSE] delete loading msg error:", ex)

    def _esc(s: str) -> str:
        return html.escape(str(s or ""), quote=False)

    def _one_line(s: str, maxlen: int = 160) -> str:
        return re.sub(r"\s+", " ", str(s or "")).strip()[:maxlen]




    def _norm_url(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        u = u.split("#")[0]
        # прибираємо трекінг-параметри
        u = re.sub(r"[?&](utm_[^=&]+|fbclid|gclid|yclid|utm|ref|referrer)=[^&]*", "", u, flags=re.I)
        # чистимо зайві символи у хвості
        u = re.sub(r"[?&]+$", "", u)
        return u

    def _dedupe_cards(cards):
        seen, out = set(), []
        for c in cards or []:
            k = c.get("_key") or _norm_url(c.get("link") or "")
            if k and k not in seen:
                seen.add(k)
                out.append(c)
        return out

    def _page_window(page: int):
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        return start, end

    def _clear_next_prompt(chat_id: int):
        mid = next_prompt_msg_id.pop(chat_id, None)
        if mid:
            try:
                bot.delete_message(chat_id, mid)
            except Exception:
                pass

    def _prompt_next_button(chat_id: int):
        """Показуємо підказку 'Далі' один раз, коли результатів справді багато."""
        if next_prompt_msg_id.get(chat_id):
            return
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("▶️ Далі", callback_data="house_next_page"))
        try:
            m = bot.send_message(
                chat_id,
                "✅ Пошук завершено — натисни 👉 Далі, щоб переглянути оновлення.",
                reply_markup=kb
            )
            if m and getattr(m, "message_id", None):
                next_prompt_msg_id[chat_id] = m.message_id
        except Exception:
            pass

    # --- збір фото (якщо у провайдера порожньо)
    def _collect_olx_images(page, limit=4):
        sel_candidates = [
            "img[data-testid='swiper-image']",
            "img[data-testid='swiper-image-lazy']",
            "div.swiper-zoom-container img",
            "picture img.css-1bmvjcs",
            "img[loading][src*='olxcdn.com']",
        ]
        urls = []
        for sel in sel_candidates:
            try:
                for el in page.locator(sel).all():
                    src = el.get_attribute("src") or ""
                    if not src:
                        srcset = el.get_attribute("srcset") or ""
                        if srcset:
                            parts = [p.strip() for p in srcset.split(",") if p.strip()]
                            if parts:
                                src = parts[-1].split()[0]
                    if src and src not in urls:
                        urls.append(src)
                    if len(urls) >= limit:
                        break
            except Exception:
                pass
            if len(urls) >= limit:
                break
        return urls[:limit]


    def send_house_listing(chat_id: int, show_cards: bool = True):
        listings = prepare_cards_for_display(user_listings.get(chat_id, []))
        user_listings[chat_id] = listings
        page = user_page.get(chat_id, 0)
        limit_reached_after_current = False
        start, end = _page_window(page)

        if start >= len(listings):
            if user_loading_status.get(chat_id, False):
                try:
                    bot.send_message(chat_id, "⏳ Підвантажуємо нові будинки…")
                except Exception:
                    pass
            else:
                bot.send_message(chat_id, "❌ Варіанти будинків закінчились. Спробуй змінити райони або бюджет.")
            return

        end = min(end, len(listings))
        page_bucket = []

        city = user_selected_city.get(chat_id, "")
        districts_selected = user_selected_districts.get(chat_id, []) or []
        sel_types = _get_house_types(chat_id)
        min_b = user_budget_min.get(chat_id, "") or ""
        max_b = user_budget_max.get(chat_id, "") or ""
        pet = user_has_pet.get(chat_id, "") or ""

        lines = []
        loc_header = format_city_line(city) or ""
        if loc_header:
            lines.append(f"📍 Локація: {_esc(loc_header)}")
        if len(districts_selected) == 1:
            lines.append(f"🏙️ Район: {_esc(format_full_location(city, districts_selected[0]))}")
        elif len(districts_selected) > 1:
            lines.append(f"🏙️ Райони: {_esc('; '.join(districts_selected))}")
        if sel_types:
            lines.append(f"🏡 Тип будинку: {_esc(', '.join(sel_types))}")
        else:
            lines.append("🏡 Тип будинку: будь-який")

        if min_b and max_b and max_b != "Не обмежено":
            budget_line = f"{min_b} – {max_b} грн"
        elif min_b and (max_b == "Не обмежено" or not max_b):
            budget_line = f"від {min_b} грн"
        elif max_b and not min_b:
            budget_line = f"до {max_b} грн"
        else:
            budget_line = "—"
        lines.append(f"💰 Бюджет: {_esc(budget_line)}")

        if pet:
            lines.append(f"🐶 Тварини: {_esc('Так' if pet == 'Має' else 'Ні')}")

        caption_text = "\n".join(lines)
        if len(caption_text) > 1000:
            caption_text = caption_text[:995] + "…"

        if show_cards:
            for listing in listings[start:end]:
                try:
                    view_state = register_listing_view(chat_id, listing)
                    if not view_state.get("allowed"):
                        _send_subscription_gate(bot, chat_id)
                        return

                    title = _esc(_one_line(listing.get("title", "Без назви"), 120))
                    price = _esc(listing.get("price") or "—")

                    card_caption = f"🏷 {title}\n💵 {price}\n\n{caption_text}"

                    img_urls = listing.get("img_urls", []) or []
                    collage = None

                    markup = types.InlineKeyboardMarkup()
                    if _has_active_subscription(chat_id):
                        fav_token = remember_card(listing)
                        markup.add(types.InlineKeyboardButton("⭐ В добірку", callback_data=f"fav_toggle:{fav_token}"))
                    link = listing.get("link") or ""
                    if link:
                        markup.add(types.InlineKeyboardButton("🔗 Переглянути", url=link))

                    if collage:
                        bio = BytesIO()
                        collage.save(bio, format="JPEG", quality=85)
                        bio.seek(0)
                        m = bot.send_photo(chat_id, bio, caption=card_caption, parse_mode="HTML", reply_markup=markup)
                    elif img_urls:
                        m = bot.send_photo(chat_id, img_urls[0], caption=card_caption, parse_mode="HTML", reply_markup=markup)
                    else:
                        m = bot.send_message(chat_id, card_caption, parse_mode="HTML", reply_markup=markup)

                    if m and getattr(m, "message_id", None):
                        page_bucket.append(m.message_id)
                    if (not view_state.get("subscribed")) and int(view_state.get("remaining", 0) or 0) <= 0:
                        limit_reached_after_current = True
                except Exception as e:
                        # fallback: відправимо хоча б текст
                        try:
                            m = bot.send_message(chat_id, re.sub(r"<[^>]+>", "", card_caption))
                            if m and getattr(m, "message_id", None):
                                page_bucket.append(m.message_id)
                        except Exception as _e:
                            print("[HOUSE][card error]", _e)

        if limit_reached_after_current:
            nav = types.InlineKeyboardMarkup(row_width=1)
            if start > 0:
                nav.add(types.InlineKeyboardButton("◀️ Назад", callback_data="house_prev_page"))
            nav.add(types.InlineKeyboardButton("🔓 Відкрити повний доступ", callback_data="subscribe_month"))
            nav.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))
            bot.send_message(
                chat_id,
                "🔒 Безкоштовні 3 оголошення на цей місяць уже закінчилися.\n\n"
                "Можеш повернутися до попереднього варіанту або відкрити повний доступ.",
                reply_markup=nav,
            )
            return


        nav = types.InlineKeyboardMarkup()
        has_more = (end < len(listings)) or user_loading_status.get(chat_id, False)
        preview_limit_reached = free_views_used_up(chat_id)
        if start > 0:
            nav.add(types.InlineKeyboardButton("◀️ Назад", callback_data="house_prev_page"))
        if has_more and not preview_limit_reached:
            nav.add(types.InlineKeyboardButton("▶️ Далі", callback_data="house_next_page"))
        elif preview_limit_reached:
            nav.add(types.InlineKeyboardButton("🔓 Відкрити всі варіанти", callback_data="subscribe_month"))
        nav.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))

        footer = f"🏠 Варіант {start + 1} з {len(listings)}"
        try:
            bot.send_message(chat_id, footer, reply_markup=nav)
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda call: call.data == "house_next_page")
    def handle_house_next_page(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        cur = user_page.get(chat_id, 0)
        if free_views_used_up(call.from_user.id):
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except Exception:
                pass
            _send_subscription_gate(bot, chat_id)
            return

        total = len(user_listings.get(chat_id, []))
        max_page = max((total - 1) // PAGE_SIZE, 0)

        if cur >= max_page:
            try:
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except Exception:
                pass

            if user_loading_status.get(chat_id, False):
                bot.send_message(chat_id, "⏳ Ще підвантажуємо нові будинки… Спробуй «Далі» трохи пізніше.")
            else:
                bot.send_message(chat_id, "❌ Варіанти будинків закінчились. Спробуй змінити райони або бюджет.")
            return

        user_page[chat_id] = cur + 1
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        send_house_listing(chat_id)

    @bot.callback_query_handler(func=lambda call: call.data == "house_prev_page")
    def handle_house_prev_page(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        cur = user_page.get(chat_id, 0)
        user_page[chat_id] = cur - 1 if cur > 0 else 0

        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass

        send_house_listing(chat_id)

    def _normalize_rooms(labels):
        # OLX очікує числа 1..5, а не "3 кімнати"
        import re
        if not labels: return []
        out = []
        for s in labels:
            m = re.search(r"\d+", s or "")
            if m: out.append(int(m.group(0)))
        return out

    def run_house_search_for_chat(chat_id, max_pages=3):
        category = current_category.get(chat_id, "house")
        print(f"[DEBUG] CATEGORY SET TO: {category}")
        provider = get_olx_provider(category)

        city = user_selected_city.get(chat_id)
        city_slug = city_url_slug_map.get(city, "")
        districts = user_selected_districts.get(chat_id, [])
        rooms = _normalize_rooms(user_selected_rooms.get(chat_id, []))  # якщо непотрібно для будинків — прибери
        price_min = user_budget_min.get(chat_id)
        price_max = user_budget_max.get(chat_id)
        area_label = user_selected_area.get(chat_id)
        floors = user_selected_floors.get(chat_id, [])

        pets = user_prefs.get(chat_id, {"allows_pets": None, "pet_types": []})
        allows_pets = pets.get("allows_pets", None)
        pet_types = pets.get("pet_types") or []

        q = {
            "city": city,
            "city_slug": city_slug,
            "districts": districts,
            "rooms": rooms,  # опційно
            "price_from": price_min,
            "price_to": price_max,
            "no_fee": True,
            "allows_pets": allows_pets,
            "pet_types": pet_types,
            "area": _parse_area_from(area_label),  # повертає dict або None
            "floor": _build_floor_range(floors),  # dict або None
            "sort": "newest",
            "max_pages": max_pages,
            "debug": False,
        }

        try:
            print("OLX URL (house):", provider.build_url(q, 1))
        except Exception as e:
            print("[HOUSE][WARN] build_url:", e)

        return provider.search(q)

    def _parse_area_from(area_label):
        # "від 60 м2" -> 60
        import re
        if not area_label: return None
        m = re.search(r"\d+", str(area_label))
        if not m: return None
        return {"from": int(m.group(0))}

    def _build_floor_range(floor_selections):
        # якщо юзер обрав "Будь-який поверх🥲" — повертаємо None
        if not floor_selections: return None
        if any("Будь-який" in s for s in floor_selections):
            return None
        # приклади: ["до 9", "Без 1 поверху"] -> {"to": 9} (а "без 1/2" вже обробляється у твоєму floor_preset)
        import re
        up_to = None
        for s in floor_selections:
            m = re.search(r"до\s+(\d+)", s)
            if m:
                up_to = int(m.group(1))
                break
        if up_to:
            return {"to": up_to}
