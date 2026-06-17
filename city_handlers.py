from __future__ import annotations

import re
from typing import Any
from telebot import types
from providers.olx_provider import get_olx_provider
from PIL import Image
import threading
from io import BytesIO
import time, requests
from typing import Dict, List
from search_runner import build_query_from_state_for_olx
from gsheets import get_sub_info
from listing_cache import prepare_cards_for_display, query_cards_for_query, upsert_listings
from city_menu import build_city_markup, city_caption
from app_config import BRAND_NAME
from background_indexer import enqueue_index_job
from free_access import free_views_used_up, has_active_subscription as has_paid_access, register_listing_view
from favorites import remember_card
from media_utils import edit_step_photo, send_step_photo


user_selected_districts = {}  # {chat_id: [district1, district2, ...]}
user_selected_floors = {}     # {chat_id: [поверхи]}
user_selected_city = {}  # {chat_id: "Київ"}
user_selected_area = {}  # {chat_id: "від 50 м2" або "Будь-яка площа 👀"}
user_selected_rooms = {}
user_budget_min = {}
user_budget_max = {}
user_has_pet = {}
user_listings = {}  # chat_id: [list of flats]
user_page = {}      # chat_id: current index
sent_messages = {}  # {chat_id: [message_id1, message_id2, ...]}
user_last_messages = {}  # {chat_id: [msg_id1, msg_id2, ...]}
user_loading_status = {}  # {chat_id: bool}
user_total_expected = {}  # {chat_id: int}
user_waiting_results = {}  # chat_id -> bool; user pressed "show results" while prefetch is running
from state import current_category  # {chat_id: str}
user_prefs = {}  # {chat_id: {"allows_pets": bool, "pet_types": list[str]}}
page_msg_ids: Dict[int, Dict[int, List[int]]] = {}  # chat_id -> {page_index -> [message_ids]}
loading_notice_msg_id: Dict[int, int] = {}
next_prompt_msg_id: Dict[int, int] = {}   # <-- НОВЕ
user_last_queries = {}

DEFAULT_PET_TYPES = ["yes_cat","yes_small_dog","yes_medium_dog","yes_big_dog","yes_other"]
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






def apply_filters(chat_id, listings):
        filtered = []
        districts = user_selected_districts.get(chat_id, [])
        floors = user_selected_floors.get(chat_id, [])
        area = user_selected_area.get(chat_id, "")
        rooms = user_selected_rooms.get(chat_id, [])
        min_budget = user_budget_min.get(chat_id, "")
        max_budget = user_budget_max.get(chat_id, "")
        has_pet = user_has_pet.get(chat_id, "")

        for item in listings:
            title = (item.get("title", "") or "").lower()

            # ✅ надійний парсинг ціни: лишаємо тільки цифри (працює і для "грн/міс", і для "₴")
            raw_price = item.get("price", "") or ""
            digits = re.sub(r"[^\d]", "", str(raw_price))
            price = int(digits) if digits else 0

            # 💰 Фільтр бюджету
            if min_budget:
                try:
                    if price < int(min_budget.replace("від", "").replace("тис.", "").strip()) * 1000:
                        continue
                except:
                    pass

            if max_budget and max_budget != "Не обмежено":
                try:
                    if price > int(max_budget.replace("до", "").replace("тис.", "").strip()) * 1000:
                        continue
                except:
                    pass

            # 🚪 Фільтр кімнат (шукаємо цифри з вибору в заголовку, працює і з "1-кімнатна")
            if rooms:
                wanted_nums = []
                for rm in rooms:
                    m = re.search(r"\d+", rm)
                    if m:
                        wanted_nums.append(m.group(0))
                if wanted_nums and not any(num in title for num in wanted_nums):
                    continue

            # 📐 Фільтр площі
            if area and "від" in area:
                try:
                    min_area = int(area.replace("від", "").replace("м2", "").strip())
                    if f"{min_area}" not in title and f"{min_area + 1}" not in title:
                        continue
                except:
                    pass

            # 🏙️ Райони (не завжди вказані, можна не фільтрувати суворо)
            if districts:
                if not any(d.lower() in title for d in districts):
                    continue

            # 🏢 Поверхи — приблизна перевірка
            if floors:
                floor_pass = False
                for f in floors:
                    if "до" in f:
                        try:
                            max_fl = int(f.replace("до", "").strip())
                            if any(f"{i} поверх" in title for i in range(1, max_fl + 1)):
                                floor_pass = True
                        except:
                            pass
                    elif "Без" in f or "Будь-який" in f:
                        floor_pass = True  # Пропускаємо фільтр
                if not floor_pass:
                    continue

            # 🐶 Тварини — не перевіряємо, бо в оголошеннях рідко є ця інфа

            filtered.append(item)

        return filtered

city_url_slug_map = {
    "Київ": "kiev",
    "Одеса": "odessa",
    "Львів": "lvov",
    "Дніпро": "dnepr",
    "Івано-Франківськ": "ivano-frankovsk",
    "Луцьк": "lutsk"
}

# місто -> область так, як показує OLX у шапці результатів
CITY_TO_OBLAST = {
    "Київ": "Київська область",
    "Одеса": "Одеська область",
    "Львів": "Львівська область",
    "Дніпро": "Дніпропетровська область",
    "Івано-Франківськ": "Івано-Франківська область",
    "Луцьк": "Волинська область",
    # за потреби додай інші міста
}

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
from app_config import CITY_TO_OBLAST


def format_city_line(city: str | None) -> str:
    if not city:
        return "—"
    obl = CITY_TO_OBLAST.get(city)
    return city if obl is None else f"{city}, {obl}"

def format_full_location(city: str | None, district: str | None = None) -> str:
    """
    Без району: 'Київ' -> 'Київ, Київська область'
    З районом: 'Київ' + 'Голосіївський' -> 'Київ, Київ, Голосіївський'
      (це повторення 'Київ, Київ' — це стиль OLX: <місто>, <місто>, <район>)
    """
    if not city:
        return "—"
    if district:
        return f"{city}, {city}, {district}"
    return format_city_line(city)


def build_district_markup(districts_list, selected):
    markup = types.InlineKeyboardMarkup(row_width=2)
    for i in range(0, len(districts_list), 2):
        row = districts_list[i:i + 2]
        buttons = []
        for d in row:
            check = "✅" if d.strip() in [s.strip() for s in selected] else ""
            buttons.append(types.InlineKeyboardButton(f"{check} {d}".strip(), callback_data=f"district_{d}"))
        markup.add(*buttons)

    all_selected = all(d.strip() in [s.strip() for s in selected] for d in districts_list)
    check_all = "✅ " if all_selected else ""
    markup.add(
        types.InlineKeyboardButton(f"{check_all}Всі райони", callback_data="district_next"),
        types.InlineKeyboardButton("Далі 👉", callback_data="district_next")
    )
    markup.add(types.InlineKeyboardButton("🔁 Назад", callback_data="back_to_city"))
    return markup

def build_floor_markup(selected_floors):
    markup = types.InlineKeyboardMarkup(row_width=3)
    floor_options = [f"до {i}" for i in range(3, 27)]

    # Основні поверхи по 3 в ряд
    for i in range(0, len(floor_options), 3):
        row = []
        for option in floor_options[i:i + 3]:
            check = "✅" if option in selected_floors else ""
            row.append(types.InlineKeyboardButton(f"{check} {option}".strip(), callback_data=f"floor_{option}"))
        markup.add(*row)

    # Додаткові опції — 2 в ряд
    extra_row = []
    for label in ["Без 1 поверху", "Без 2 поверху"]:
        check = "✅" if label in selected_floors else ""
        extra_row.append(types.InlineKeyboardButton(f"{check} {label}".strip(), callback_data=f"floor_{label}"))
    markup.add(*extra_row)

    # Останній поверх — окремо
    last_label = "Без останнього поверху"
    check = "✅" if last_label in selected_floors else ""
    markup.add(types.InlineKeyboardButton(f"{check} {last_label}", callback_data=f"floor_{last_label}"))

    # Будь-який поверх — окремо
    any_label = "Будь-який поверх🥲"
    check = "✅" if any_label in selected_floors else ""
    markup.add(types.InlineKeyboardButton(f"{check} {any_label}", callback_data=f"floor_{any_label}"))

    # Кнопки управління
    markup.add(
        types.InlineKeyboardButton("🔁 Назад", callback_data="back_to_districts"),
        types.InlineKeyboardButton("Далі 👉", callback_data="proceed_to_area")  # було: floor_next
    )

    return markup



def register_city_handlers(bot):
    def show_districts(call, city, districts_list):
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        selected = user_selected_districts.get(chat_id, [])

        if districts_list:
            text = (
                f"🌆 {city},Обери район\n\n"
                "Відміть галочкою ✅ район або райони, в яких ти шукаєш квартиру.\n\n"
                "_*після чого натисни “далі”_"
                f"\n\n_✅ Вибрано: {len(selected)} район(ів)_"
            )
            markup = build_district_markup(districts_list, selected)
        else:
            text = (
                f"🌆 {city},Обери район\n\n"
                "В цьому місті відсутні райони, тому просто натисни «далі»"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("Далі 👉", callback_data="district_next"),
                types.InlineKeyboardButton("🔁 Назад", callback_data="back_to_city")
            )

        try:
            edit_step_photo(bot, chat_id, message_id, "district.png", text, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            print("⚠️ edit_step_photo error:", e)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("city_"))
    def handle_city(call):
        bot.answer_callback_query(call.id)
        city = call.data.replace("city_", "")
        chat_id = call.message.chat.id
        user_selected_city[chat_id] = city  # ✅ зберігаємо місто
        # Очищаємо вибрані райони при виборі нового міста
        user_selected_districts[chat_id] = []
        show_districts(call, city, city_districts_map[city])

    @bot.callback_query_handler(func=lambda call: call.data.startswith("district_") and not call.data.startswith(("district_next", "districts_select_all")))
    def toggle_district(call):
        # Фільтрація службових кнопок
        if call.data in ["district_next", "districts_select_all"]:
            return

        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        district = call.data.replace("district_", "").strip()
        city = user_selected_city.get(chat_id)
        if not city:
            return

        selected = user_selected_districts.get(chat_id, [])
        if district in selected:
            selected.remove(district)
        else:
            selected.append(district)
        user_selected_districts[chat_id] = selected

        show_districts(call, city, city_districts_map[city])

    @bot.callback_query_handler(func=lambda call: call.data == "districts_select_all")
    def select_all_districts(call):
        chat_id = call.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return
        all_districts = city_districts_map.get(city, [])
        current = user_selected_districts.get(chat_id, [])
        user_selected_districts[chat_id] = [] if set(current) == set(all_districts) else all_districts.copy()
        show_districts(call, city, all_districts)

    @bot.callback_query_handler(func=lambda call: call.data == "district_next")
    def handle_district_next(call):
        go_to_floor_selection(call)

    def go_to_floor_selection(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        # Очистити вибір поверхів
        user_selected_floors[chat_id] = []

        # Текст і кнопки
        text = (
            "🏃‍♂️ *Обери поверх*\n\n"
            "Постав ✅ ДО якого поверху ти розглядаєш і натисни «далі»\n\n"
            "_Також можеш обрати, якщо не розглядаєш 1-2 поверхи та останній_"
        )
        markup = build_floor_markup(user_selected_floors[chat_id])

        # Видалити попереднє повідомлення
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as e:
            print("⚠️ Не вдалось видалити повідомлення:", e)

        # Відправити нове повідомлення
        try:
            send_step_photo(bot, chat_id, "floor.png", text, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            print("⚠️ Помилка при відправці вибору поверхів:", e)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("floor_"))
    def toggle_floor(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        option_raw = call.data.replace("floor_", "")

        selected = user_selected_floors.get(chat_id, [])

        single_floor_options = [f"до {i}" for i in range(3, 27)]
        multi_options = ["Без 1 поверху", "Без 2 поверху", "Без останнього поверху"]
        any_option = "Будь-який поверх🥲"

        if option_raw == any_option:
            if any_option in selected:
                selected.remove(any_option)
            else:
                selected = [any_option]
        elif option_raw in single_floor_options:
            if option_raw in selected:
                selected.remove(option_raw)
            else:
                selected = [option_raw] + [s for s in selected if s in multi_options]
                if any_option in selected:
                    selected.remove(any_option)
        elif option_raw in multi_options:
            if option_raw in selected:
                selected.remove(option_raw)
            else:
                selected.append(option_raw)
                if any_option in selected:
                    selected.remove(any_option)

        user_selected_floors[chat_id] = selected
        print(f"[TOGGLE FLOOR] {chat_id}: {selected}")
        new_markup = build_floor_markup(selected)

        try:
            bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=new_markup)
        except Exception as e:
            if "message is not modified" not in str(e):
                print("⚠️ toggle_floor error:", e)

    @bot.callback_query_handler(func=lambda call: call.data == "back_to_districts")
    def back_to_districts(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        # Отримуємо місто користувача
        city = user_selected_city.get(chat_id)
        if not city:
            print("⚠️ Місто не знайдено для користувача!")
            return

        # Отримуємо райони цього міста
        districts = city_districts_map.get(city, [])

        # ✅ Повертаємось до show_districts
        show_districts(call, city, districts)

    @bot.callback_query_handler(func=lambda call: call.data == "back_to_city")
    def back_to_city(call):
        bot.answer_callback_query(call.id)
        send_step_photo(
            bot,
            call.message.chat.id,
            "city.png",
            city_caption("apartment"),
            reply_markup=build_city_markup("apartment"),
            parse_mode="Markdown",
        )


    #Обераєм площу
    def build_area_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        area_options = [f"від {i} м2" for i in range(20, 140, 10)]

        for i in range(0, len(area_options), 3):
            row = [types.InlineKeyboardButton(option, callback_data=f"area_{option}") for option in
                   area_options[i:i + 3]]
            markup.add(*row)

        # Додаткові кнопки
        markup.add(types.InlineKeyboardButton("Будь-яка площа 👀", callback_data="area_any"))
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="back_to_floors"))
        return markup

    @bot.callback_query_handler(func=lambda call: call.data == "proceed_to_area")
    def handle_floor_next(call):
        print("[FLOOR NEXT] перехід до площі")
        bot.answer_callback_query(call.id)
        show_area_selection(call)

    def show_area_selection(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "🏡 *Обери площу*\n\n_Обери ВІД якої площі ти розглядаєш квартиру 🏢_"
        markup = build_area_markup()

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(bot, chat_id, "area.png", text, reply_markup=markup, parse_mode="Markdown")




    @bot.callback_query_handler(func=lambda call: call.data == "back_to_floors")
    def back_to_floors(call):
        go_to_floor_selection(call)

    # Побудова клавіатури з кімнатами
    def build_room_keyboard(chat_id):
        selected = user_selected_rooms.get(chat_id, [])

        def btn(text):
            check = "✅ " if text in selected else ""
            return types.InlineKeyboardButton(f"{check}{text}", callback_data=f"room_{text}")

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(btn("1 кімната"), btn("2 кімнати"))
        markup.add(btn("3 кімнати"), btn("4 кімнати"))
        markup.add(btn("5 та більше кімнат"))
        markup.add(
            types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_area"),
            types.InlineKeyboardButton("Далі 👉", callback_data="to_budget_from")
        )
        return markup

    # Функція для відображення вибору кімнат
    def send_room_selection(chat_id):
        text = (
            "🚪 *Обери кімнати*\n\n"
            "_Відміть ✅ к-сть кімнат своєї майбутньої квартири "
            "(або обери всі, якщо не принципово)_\n\n"
            "*після чого натисни «Далі»*"
        )

        markup = build_room_keyboard(chat_id)

        send_step_photo(bot, chat_id, "rooms.png", text, reply_markup=markup, parse_mode="Markdown")

    # Обробка вибору площі — ПЕРЕХІД ДО КІМНАТ
    @bot.callback_query_handler(func=lambda call: call.data.startswith("area_"))
    def handle_area_selection(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        selected = "Будь-яка площа 👀" if call.data == "area_any" else call.data.replace("area_", "")
        user_selected_area[chat_id] = selected

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        # ✅ Переходимо до вибору кімнат
        send_room_selection(chat_id)

    # Обробка натискання на кнопки з кімнатами
    @bot.callback_query_handler(func=lambda call: call.data.startswith("room_"))
    def toggle_room_selection(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        room = call.data.replace("room_", "")

        selected = user_selected_rooms.get(chat_id, [])
        if room in selected:
            selected.remove(room)
        else:
            selected.append(room)

        user_selected_rooms[chat_id] = selected

        try:
            bot.edit_message_reply_markup(
                chat_id, message_id, reply_markup=build_room_keyboard(chat_id)
            )
        except:
            pass

    # Обробка "Далі 👉"
    @bot.callback_query_handler(func=lambda call: call.data == "to_budget_from")
    def proceed_to_budget_step(call):
        print("[DEBUG] Натиснута кнопка Далі 👉 (переходить до вибору бюджету)")  # ✅ print
        bot.answer_callback_query(call.id)

        chat_id = call.message.chat.id
        message_id = call.message.message_id

        selected = user_selected_rooms.get(chat_id, [])
        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass



        # Перехід до бюджету
        show_budget_from_step(call)  # ⬅️ передаємо call, бо він потрібен

    # Обробка кнопки "🔙 Назад" з кімнат — повертає до площі
    @bot.callback_query_handler(func=lambda call: call.data == "back_to_area")
    def back_to_area(call):
        bot.answer_callback_query(call.id)
        show_area_selection(call)

    def build_budget_from_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        options = [f"від {i} тис." for i in [0, 5, 7, 10, 12, 15, 17, 20]]
        buttons = [types.InlineKeyboardButton(text=o, callback_data=f"budget_from_{o}") for o in options]
        markup.add(*buttons)
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="back_to_rooms"))
        return markup

    def show_budget_from_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "💰 *Обери бюджет*\n\n_Обери ВІД якої вартості в тис. грн. ти розглядаєш квартиру 🏢_\n\n*чим більше вартість — тим менше старих ремонтів*"

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

    @bot.callback_query_handler(func=lambda call: call.data.startswith("budget_from_"))
    def handle_budget_from(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        selected = call.data.replace("budget_from_", "")
        user_budget_min[chat_id] = selected

        show_budget_to_step(call)

    def build_budget_to_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        options = [f"до {i} тис." for i in [10, 15, 20, 25, 30, 35, 40, 45, 50, 70, 100]]
        buttons = [types.InlineKeyboardButton(text=o, callback_data=f"budget_to_{o}") for o in options]
        markup.add(*buttons)
        markup.add(types.InlineKeyboardButton("Не маю обмежень по бюджету", callback_data="budget_to_any"))
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="back_to_budget_from"))
        return markup

    def show_budget_to_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "💰 *Обери бюджет*\n\n_Обери ДО якої вартості в тис. грн. ти розглядаєш квартиру 🏢_\n\n*чим більше вартість — тим менше старих ремонтів*"

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(bot, chat_id, "budget.png", text, reply_markup=build_budget_to_markup(), parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("budget_to_"))
    def handle_budget_to(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        selected = "Не обмежено" if call.data == "budget_to_any" else call.data.replace("budget_to_", "")
        user_budget_max[chat_id] = selected


        #Перехід до тварин
        show_pet_step(call)

    @bot.callback_query_handler(func=lambda call: call.data == "back_to_budget_from")
    def back_to_budget_from(call):
        show_budget_from_step(call)

    @bot.callback_query_handler(func=lambda call: call.data == "back_to_rooms")
    def back_to_rooms_from_budget(call):
        send_room_selection(call.message.chat.id)



    def ensure_user_prefs(chat_id: int):
        if chat_id not in user_prefs:
            user_prefs[chat_id] = {"allows_pets": False, "pet_types": []}

    # ---- Маєш тваринку? ----
    def build_pet_keyboard(allows_pets: bool | None = None):
        kb = types.InlineKeyboardMarkup(row_width=2)
        yes_text = ("✅ " if allows_pets is True else "") + "Маю 🐶🐱"
        no_text = ("✅ " if allows_pets is False else "") + "Не маю ❌"
        kb.add(
            types.InlineKeyboardButton(yes_text, callback_data="has_pet"),
            types.InlineKeyboardButton(no_text, callback_data="no_pet"),
        )
        kb.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="back_to_budget"))
        return kb

    def show_pet_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "🐶 *Маєш тваринку?*\n\n_Обери, чи маєш ти тваринку ⤵️_"

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        # показуємо клавіатуру з актуальною галочкою
        allows = user_prefs.get(chat_id, {}).get("allows_pets", None)
        send_step_photo(
            bot,
            chat_id,
            "pets.png",
            text,
            reply_markup=build_pet_keyboard(allows),
            parse_mode="Markdown"
        )

    @bot.callback_query_handler(func=lambda call: call.data in ["has_pet", "no_pet"])
    def handle_pet_selection(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        # 1) зберігаємо вибір
        user_has_pet[chat_id] = "Має" if call.data == "has_pet" else "Не має"
        ensure_user_prefs(chat_id)
        if call.data == "has_pet":
            user_prefs[chat_id]["allows_pets"] = True
            user_prefs[chat_id]["pet_types"] = DEFAULT_PET_TYPES.copy()
        else:
            user_prefs[chat_id]["allows_pets"] = False
            user_prefs[chat_id]["pet_types"] = []

        # 2) прибираємо клавіатуру + саме повідомлення (щоб не тицяли ще раз)
        safe_edit_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        try:
            bot.answer_callback_query(call.id, "⏳ Рахуємо варіанти…", cache_time=0, show_alert=False)
        except ApiTelegramException:
            pass
        safe_delete_message(chat_id, call.message.message_id)

        # 3) показуємо тимчасове "рахуємо…"
        loading = safe_send_message(chat_id, "⏳ Рахуємо варіанти… Будь ласка, зачекайте.")
        loading_msg_id = loading.message_id if loading else None

        # 4) запускаємо підрахунок (передаємо loading_msg_id)
        _start_quick_count_and_show_summary(bot, call, loading_msg_id)

    def quick_count_playwright(provider, query: dict, timeout_ms: int = 12000,
                               screenshot_path: str | None = None) -> int | None:
        try:
            from playwright.sync_api import sync_playwright  # noqa
        except Exception:
            return None

        import re

        def _only_digits(s: str) -> int | None:
            try:
                n = int(re.sub(r"[^\d]", "", s or ""))
                return n
            except Exception:
                return None

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(locale="uk-UA", viewport={"width": 1366, "height": 900})
                page = ctx.new_page()
                page.set_default_timeout(timeout_ms)

                url = provider.build_url(query, page=1)
                page.goto(url, wait_until="domcontentloaded")

                # прийняти cookies якщо є
                for sel in (
                        "[data-testid='cookies-popup-accept-all']",
                        "[data-testid='cookiesbar-accept']",
                        "#onetrust-accept-btn-handler",
                        "button:has-text('Прийняти все')",
                        "button:has-text('Погоджуюсь')",
                        "button:has-text('Accept all')",
                ):
                    try:
                        page.locator(sel).first.click(timeout=700)
                        page.wait_for_timeout(100)
                        break
                    except Exception:
                        pass

                host = page.url.split("/")[2]

                # === OLX: <span data-testid="total-count">Ми знайшли N оголошень</span>
                if "olx.ua" in host:
                    try:
                        el = page.locator("span[data-testid='total-count']").first
                        text = el.text_content(timeout=2500) or ""
                        n = _only_digits(text)
                        if n:
                            return n
                    except Exception:
                        pass
                    # запасний варіант: текст всього документа
                    t = page.evaluate("() => document.body.innerText") or ""
                    m = re.search(r"ми\s+знайшли\s+([\d\s]+)", t.lower())
                    if m:
                        n = _only_digits(m.group(1))
                        if n:
                            return n



                # Fallback: по пагінації (20 на сторінку)
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
                pages = max(nums) if nums else 1
                return max(1, pages) * 20
            finally:
                browser.close()


    def quick_count_and_cards_playwright(provider, query: dict, timeout_ms: int = 7000) -> tuple[int | None, list]:
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa
        except Exception:
            return None, []

        import re

        def _only_digits(s: str) -> int | None:
            try:
                return int(re.sub(r"[^\d]", "", s or ""))
            except Exception:
                return None

        def _parse_count(page) -> int | None:
            try:
                el = page.locator("span[data-testid='total-count']").first
                n = _only_digits(el.text_content(timeout=1800) or "")
                if n:
                    return n
            except Exception:
                pass
            try:
                body = (page.evaluate("() => document.body.innerText") or "").lower()
                m = re.search(r"ми\s+знайшли\s+([\d\s]+)", body)
                if m:
                    n = _only_digits(m.group(1))
                    if n:
                        return n
            except Exception:
                pass
            try:
                hrefs = page.evaluate("""
                    () => Array.from(document.querySelectorAll("a[href*='&page='], a[href*='?page=']"))
                                .map(a => a.getAttribute('href') || '')
                """)
            except Exception:
                hrefs = []
            nums = []
            for h in hrefs:
                for m in re.findall(r"[?&]page=(\d+)", h or ""):
                    try:
                        nums.append(int(m))
                    except Exception:
                        pass
            return max(1, max(nums) if nums else 1) * 20

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                ctx = browser.new_context(
                    locale="uk-UA",
                    viewport={"width": 1366, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ),
                )
                ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = ctx.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})
                url = provider.build_url(query, page=1)
                page.goto(url, timeout=timeout_ms + 2000, wait_until="domcontentloaded")

                for sel in (
                    "[data-testid='cookies-popup-accept-all']",
                    "[data-testid='cookiesbar-accept']",
                    "#onetrust-accept-btn-handler",
                    "button:has-text('Прийняти все')",
                    "button:has-text('Погоджуюсь')",
                    "button:has-text('Accept all')",
                ):
                    try:
                        page.locator(sel).first.click(timeout=700)
                        page.wait_for_timeout(100)
                        break
                    except Exception:
                        pass

                for _ in range(2):
                    try:
                        page.evaluate("() => window.scrollBy(0, 1400)")
                    except Exception:
                        pass
                    page.wait_for_timeout(180)

                count = _parse_count(page)
                extractor = getattr(provider, "_extract_listings_from_page", None)
                cards = extractor(page) if callable(extractor) else []
                return count, cards or []
            except PWTimeout:
                return None, []
            except Exception as e:
                print("[quick_count_and_cards][error]", e)
                return None, []
            finally:
                browser.close()



    @bot.callback_query_handler(func=lambda call: call.data == "back_to_budget")
    def back_to_budget(call):
        show_budget_to_step(call)


    def _start_quick_count_and_show_summary(bot, call, loading_msg_id=None):
        chat_id = call.message.chat.id

        def _runner():
            q_olx = {}
            category = current_category.get(chat_id, "apartment")
            city = user_selected_city.get(chat_id)
            districts = user_selected_districts.get(chat_id, [])
            combined_count = None
            try:
                # --- 1) Стан ---
                city_slug = city_url_slug_map.get(city, "")
                floors = user_selected_floors.get(chat_id, [])
                area_label = user_selected_area.get(chat_id)
                rooms_label = user_selected_rooms.get(chat_id, [])
                price_min = user_budget_min.get(chat_id)
                price_max = user_budget_max.get(chat_id)

                # --- 2) OLX: q + нормалізація pets ---
                q_olx = build_query_from_state_for_olx(
                    category=category,
                    city=city,
                    city_slug=city_slug,
                    districts=districts,
                    floors=floors,
                    area_label=area_label,
                    rooms_label=rooms_label,
                    price_min=price_min,
                    price_max=price_max,
                    has_pet=user_has_pet.get(chat_id),
                    sort="newest",
                    max_pages=1,
                )

                # pets: дозволено/заборонено
                prefs = user_prefs.get(chat_id, {})
                allows = prefs.get("allows_pets", None)
                # приберемо внутрішні прапорці — в URL їх не має бути
                q_olx.pop("allows_pets", None)
                q_olx.pop("pet_types", None)
                # якщо тварин НЕ можна → явний фільтр no
                if allows is False:
                    q_olx["pets_filter"] = "no"
                else:
                    q_olx.pop("pets_filter", None)

                enqueue_index_job(category, city)
                cached = _query_cached_cards_fast(category, city, districts, q_olx, limit=100)
                if cached:
                    user_listings[chat_id] = cached
                    user_page[chat_id] = 0
                    combined_count = len(cached)
                else:
                    combined_count = 0

                user_total_expected[chat_id] = combined_count

                # 🆕 зберігаємо останній запит
                user_last_queries[chat_id] = {"olx": q_olx}



            except Exception as e:
                print("[quick_count_playwright] error:", e)
                combined_count = None
            finally:
                if loading_msg_id:
                    safe_delete_message(chat_id, loading_msg_id)

            show_final_summary(chat_id, count=combined_count if isinstance(combined_count, int) else None)
            if not user_listings.get(chat_id):
                threading.Thread(
                    target=_prefetch_first_results,
                    args=(chat_id, q_olx, category, city, districts),
                    daemon=True,
                ).start()

        threading.Thread(target=_runner, daemon=True).start()

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

    def show_final_summary(chat_id, count=None):
        # Дані стану
        city = user_selected_city.get(chat_id, "—")
        listings = prepare_cards_for_display(user_listings.get(chat_id, []))
        user_listings[chat_id] = listings
        # якщо прийшов count — він пріоритетний; якщо ні — пробуємо user_total_expected; інакше – len(listings)
        expected = (count if isinstance(count, int) else user_total_expected.get(chat_id)) or len(listings)
        user_total_expected[chat_id] = expected  # оновимо з кешем
        ready_now = len(listings)

        districts_selected = user_selected_districts.get(chat_id, []) or []

        city_line = format_city_line(city)
        if len(districts_selected) == 0:
            districts_line = "—"
        elif len(districts_selected) == 1:
            districts_line = format_full_location(city, districts_selected[0])
        else:
            districts_line = ", ".join(districts_selected)

        floors = ", ".join(user_selected_floors.get(chat_id, [])) or "Без обмежень"
        rooms = ", ".join(user_selected_rooms.get(chat_id, [])) or "—"
        area = user_selected_area.get(chat_id, "—")
        budget_from = user_budget_min.get(chat_id, "—")
        budget_to = user_budget_max.get(chat_id, "—")

        try:
            pet = "Так" if user_has_pet.get(chat_id) == "Має" else "Ні"
        except NameError:
            pet = "—"

        if budget_to == "Не обмежено":
            budget_text = f"Бюджет: від {budget_from}"
        elif budget_from != "—" and budget_to != "—":
            budget_text = f"Бюджет: від {budget_from} до {budget_to}"
        elif budget_from != "—":
            budget_text = f"Бюджет: від {budget_from}"
        elif budget_to != "—":
            budget_text = f"Бюджет: до {budget_to}"
        else:
            budget_text = "Бюджет: —"

        if expected and expected > ready_now:
            count_phrase = f"*перші {ready_now} з {expected} варіантів квартир*"
        elif expected:
            count_phrase = f"*{expected} варіантів квартир*"
        else:
            count_phrase = "*актуальні варіанти квартир*"

        text = (
            f"🏠 *{BRAND_NAME}* підготував {count_phrase} без комісії за твоїми параметрами.\n\n"
            "👀 *Хочеш переглянути їх або оновити пошук?*\n\n"
            "✅ *Твої параметри:*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Локація:* `{city_line}`\n"
            f"🏙️ *Райони:* `{districts_line}`\n"
            f"🏢 *Поверх:* `{floors}`\n"
            f"🚪 *Кімнати:* `{rooms}`\n"
            f"📐 *Площа:* `{area}`\n"
            f"💰 *{budget_text} грн.*\n"
            f"🐶 *Тваринки:* `{pet}`\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔍 Переглянути варіанти", callback_data="show_results"),
            types.InlineKeyboardButton("🔄 Оновити параметри", callback_data="restart_search"),
        )

        send_step_photo(
            bot,
            chat_id,
            "results_found.jpg",
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    #Оновити параметри пошуку
    def show_update_parameters_menu(chat_id):
        text = (
            "🔄 *Оновити параметри пошуку*\n\n"
            "Обери, що хочеш змінити 🔽"
        )

        markup = build_update_parameters_keyboard()

        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda call: call.data == "restart_search")
    def handle_restart_search(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        # Показати меню з фото та параметрами
        show_update_parameters_menu(chat_id)


    @bot.callback_query_handler(func=lambda call: call.data == "edit_districts")
    def edit_districts(call):
            bot.answer_callback_query(call.id)
            chat_id = call.message.chat.id
            city = user_selected_city.get(chat_id)
            if not city:
                return
            show_districts(call, city, city_districts_map.get(city, []))


    @bot.callback_query_handler(func=lambda call: call.data == "edit_pet")
    def edit_pet(call):
            bot.answer_callback_query(call.id)
            show_pet_step(call)

    @bot.callback_query_handler(func=lambda call: call.data == "back_to_results")
    def back_to_results(call):
            bot.answer_callback_query(call.id)
            show_final_summary(call.message.chat.id)



    def build_update_parameters_keyboard():
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📍 Місто", callback_data="back_to_city"),
            types.InlineKeyboardButton("🏙️ Райони", callback_data="edit_districts"),
            types.InlineKeyboardButton("🏢 Поверх", callback_data="back_to_floors"),
            types.InlineKeyboardButton("🚪 Кімнати", callback_data="back_to_rooms"),
            types.InlineKeyboardButton("📐 Площа", callback_data="back_to_area"),
            types.InlineKeyboardButton("💰 Бюджет", callback_data="back_to_budget_from"),
            types.InlineKeyboardButton("🐶 Тваринки", callback_data="edit_pet"),
        )
        markup.add(
            types.InlineKeyboardButton("⬅️ Назад до результатів", callback_data="back_to_results"),
            types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu")
        )
        return markup

    from telebot.apihelper import ApiTelegramException

    def safe_answer_callback(call, text=None, show_alert=False, cache_time=0, retries=2):
        for i in range(retries + 1):
            try:
                return bot.answer_callback_query(
                    call.id, text=text, show_alert=show_alert, cache_time=cache_time
                )
            except requests.exceptions.RequestException as e:
                # мережеві збої: backoff і ще раз
                if i < retries:
                    time.sleep(0.5 * (2 ** i))
                    continue
                print("[safe_answer_callback][network]", e)
                return None
            except Exception as e:
                # будь‑яка інша помилка — не валимо бота
                print("[safe_answer_callback][err]", e)
                return None

    def safe_handler(fn):
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                print(f"[handler:{fn.__name__}][err]", e)
                # нічого не кидаємо назовні

        return wrapper

    def _query_cached_cards_fast(category, city, districts, q_olx, limit=100):
        return query_cards_for_query(
            category=category,
            city=city,
            districts=districts,
            query=q_olx,
            limit=limit,
            require_photos=False,
        )

    def _prefetch_first_results(chat_id: int, q_olx: dict, category: str, city: str, districts: list):
        if user_listings.get(chat_id):
            return
        user_loading_status[chat_id] = True
        try:
            cached = _query_cached_cards_fast(category, city, districts, q_olx, limit=100)
            if cached:
                user_listings[chat_id] = cached
            else:
                enqueue_index_job(category, city)
            user_page[chat_id] = 0
            if user_waiting_results.pop(chat_id, False):
                if user_listings.get(chat_id):
                    send_listing(chat_id)
                else:
                    safe_send_message(
                        chat_id,
                        "⏳ База для цих параметрів оновлюється. Я поставив пошук у пріоритет — спробуй переглянути варіанти ще раз за хвилину.",
                    )
        except Exception as e:
            print(f"[PREFETCH_RESULTS][ERROR] {e}")
            if user_waiting_results.pop(chat_id, False):
                safe_send_message(chat_id, "❌ Не вдалося швидко підготувати варіанти. Спробуй оновити параметри.")
        finally:
            user_loading_status[chat_id] = False

    @bot.callback_query_handler(func=lambda c: c.data == "show_results")
    def show_results(call):
        chat_id = call.message.chat.id

        # миттєво відповідаємо на callback (і більше не чіпаємо call.id)
        try:
            safe_answer_callback(call, "Показую варіанти", show_alert=False, cache_time=0)
        except ApiTelegramException:
            pass

        # вимикаємо клавіатуру під повідомленням, щоб не тикали повторно
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        if user_listings.get(chat_id):
            user_page[chat_id] = 0
            send_listing(chat_id)
            return

        user_waiting_results[chat_id] = True
        if user_loading_status.get(chat_id):
            safe_send_message(chat_id, "⏳ Перші варіанти вже готуються. Зараз покажу їх тут.")
            return

        loading_msg = safe_send_message(chat_id, "⏳ Готую перші варіанти…")
        threading.Thread(
            target=_do_search_and_send,
            args=(chat_id, loading_msg.message_id if loading_msg else None),
            daemon=True,
        ).start()

    def _dedupe_cards(cards: list[dict]) -> list[dict]:
        cards = prepare_cards_for_display(cards)
        seen, out = set(), []
        for c in cards:
            k = (c.get("_key") or c.get("link") or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    def _live_first_page_cards(provider, q_olx: dict, category: str, city: str) -> tuple[int | None, list[dict]]:
        count, first_items = quick_count_and_cards_playwright(provider, q_olx, timeout_ms=12000)
        if first_items:
            try:
                upsert_listings(category, city, first_items)
            except Exception as e:
                print("[LIVE_FIRST_PAGE][cache upsert][error]", e)
        cards = _dedupe_cards([_to_bot_card(it) for it in (first_items or [])])
        return count, cards

    def _interleave(a: List[dict], b: List[dict]) -> List[dict]:
        out, i, j = [], 0, 0
        while i < len(a) or j < len(b):
            if i < len(a):
                out.append(a[i]);
                i += 1
            if j < len(b):
                out.append(b[j]);
                j += 1
        return out

    def background_parse(chat_id: int, q_olx: dict):
        import math

        try:
            print("[background_parse][start]", chat_id, {"olx": q_olx})

            initial_total = len(user_listings.get(chat_id, []) or [])
            merged = (user_listings.get(chat_id, []) or [])[:]
            seen = {(c.get("_key") or c.get("link") or "") for c in merged}

            # --- OLX ---
            cat = q_olx.get("category", "apartment")
            olx = get_olx_provider(cat)
            olx_count = quick_count_playwright(olx, q_olx, timeout_ms=9000) or 0
            olx_pages = min(max(1, math.ceil(olx_count / 20)), 15)  # повільне фонове довантаження
            print(f"[OLX] found ~{olx_count}, pages={olx_pages}")

            olx_items = olx.search({**q_olx, "max_pages": olx_pages}) or []
            if olx_items:
                try:
                    upsert_listings(cat, q_olx.get("city") or user_selected_city.get(chat_id), olx_items)
                except Exception as e:
                    print("[background cache upsert][error]", e)

            for it in olx_items:
                price_uah = getattr(it, "price_uah", None)
                price_txt = f"{price_uah:,} грн".replace(",", " ") if price_uah else "—"
                card = {
                    "title": getattr(it, "title", "") or "Без назви",
                    "price": price_txt,
                    "link": getattr(it, "url", "") or "",
                    "img_urls": (getattr(it, "photos", []) or [])[:6],
                    "_key": getattr(it, "id", None) or getattr(it, "url", ""),
                }
                key = card.get("_key") or card.get("link") or ""
                if key and key not in seen:
                    seen.add(key)
                    merged.append(card)




            user_listings[chat_id] = _dedupe_cards(merged)
            print(f"[background_parse][done] {chat_id}: {len(user_listings[chat_id])} results")
            if len(user_listings[chat_id]) > initial_total:
                _prompt_next_button(chat_id)

        except Exception as e:
            print("[background_parse][error]", e)
        finally:
            user_loading_status[chat_id] = False

            # якщо висіло «⏳ Підвантажуємо…» — прибираємо
            msg_id = loading_notice_msg_id.pop(chat_id, None)
            if msg_id:
                safe_delete_message(chat_id, msg_id)
                # і підкажемо натиснути Далі

            pass



    def _prompt_next_button(chat_id: int):
        """Показує або оновлює один-єдиний нотиф з кнопкою 'Далі'."""
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("▶️ Далі", callback_data="next_page"))

        # Якщо вже показували — не дублюємо
        if next_prompt_msg_id.get(chat_id):
            return

        m = safe_send_message(
            chat_id,
            "✅ Пошук завершено — натисни 👉 Далі, щоб переглянути оновлення.",
            reply_markup=kb
        )
        # safe_send_message має повертати message або None; якщо ні — обгорни bot.send_message вручну
        try:
            if m and getattr(m, "message_id", None):
                next_prompt_msg_id[chat_id] = m.message_id
        except Exception:
            pass

    def _clear_next_prompt(chat_id: int):
        """Прибирає підказку, якщо вона була."""
        mid = next_prompt_msg_id.pop(chat_id, None)
        if mid:
            safe_delete_message(chat_id, mid)

    def _to_bot_card(item) -> dict:
        import re

        def pick(*names, default=None):
            for n in names:
                v = None
                if isinstance(item, dict):
                    v = item.get(n)
                if v is None:
                    v = getattr(item, n, None)
                if v not in (None, ""):
                    return v
            return default

        key = pick("id", "ad_id", "item_id", "url", "link")
        title = pick("title", "name", default="Без назви")
        link = pick("url", "link", "permalink", default="")

        photos = pick("photos", "images", "img_urls", default=[])
        if isinstance(photos, dict):
            photos = list(photos.values())
        photos = (photos or [])[:4]

        price_uah = pick("price_uah", "price", default=None)
        price_txt = pick("price_text", "price_str", default=None)
        if price_uah is None and price_txt:
            digits = re.sub(r"[^\d]", "", str(price_txt))
            price_uah = int(digits) if digits else None
        if price_txt is None:
            price_txt = f"{price_uah:,} грн".replace(",", " ") if isinstance(price_uah, int) else "—"

        return {
            "title": title or "Без назви",
            "price": price_txt or "—",
            "link": link or "",
            "img_urls": photos,
            "_key": (key or link or f"{title}|{price_txt}") or "",
        }

    def _do_search_and_send(chat_id, loading_msg_id=None):
        try:
            user_loading_status[chat_id] = True

            # --- 1) Стан
            city = user_selected_city.get(chat_id)
            city_slug = city_url_slug_map.get(city, "")
            districts = user_selected_districts.get(chat_id, [])
            floors = user_selected_floors.get(chat_id, [])
            area_label = user_selected_area.get(chat_id)
            rooms_label = user_selected_rooms.get(chat_id, [])  # ✅ завжди список
            price_min = user_budget_min.get(chat_id)
            price_max = user_budget_max.get(chat_id)
            has_pet = user_has_pet.get(chat_id)
            category = current_category.get(chat_id, "apartment")  # ✅ використовуємо категорію

            # --- 2) q для OLX (з нормалізацією pets)
            q_olx = build_query_from_state_for_olx(
                category=category,
                city=city, city_slug=city_slug,
                districts=districts, floors=floors,
                area_label=area_label, rooms_label=rooms_label,
                price_min=price_min, price_max=price_max,
                has_pet=has_pet, sort="newest", max_pages=1,
            )
            prefs = user_prefs.get(chat_id, {})
            allows = prefs.get("allows_pets", None)
            q_olx.pop("allows_pets", None)  # ✅ без ';'
            q_olx.pop("pet_types", None)
            if allows is False:
                q_olx["pets_filter"] = "no"
            else:
                q_olx.pop("pets_filter", None)

            enqueue_index_job(category, city)


            # збережемо для фону
            user_last_queries[chat_id] = {"olx": q_olx}

            provider = get_olx_provider(category)
            cached = _dedupe_cards(_query_cached_cards_fast(category, city, districts, q_olx, limit=120))
            live_count = None
            live_cards: list[dict] = []

            if len(cached) < 6:
                try:
                    live_count, live_cards = _live_first_page_cards(provider, q_olx, category, city)
                except Exception as e:
                    print("[LIVE_FIRST_PAGE][error]", e)

            merged_cards = _dedupe_cards(_interleave(cached, live_cards))
            if merged_cards:
                user_listings[chat_id] = merged_cards
                user_total_expected[chat_id] = max(
                    len(merged_cards),
                    live_count or user_total_expected.get(chat_id, 0) or 0,
                )
                user_page[chat_id] = 0
                send_listing(chat_id)
                threading.Thread(target=background_parse, args=(chat_id, q_olx), daemon=True).start()
                return

            safe_send_message(
                chat_id,
                "⏳ Поки готую перші варіанти саме під ці параметри. Пошук уже в пріоритеті — спробуй ще раз за хвилину.",
            )
            threading.Thread(
                target=background_parse,
                args=(chat_id, q_olx),
                daemon=True
            ).start()
            return

        except Exception as e:
            print(f"[CITY_SEARCH][ERROR] {e}")
            safe_send_message(chat_id, "❌ Пошук тимчасово не вдався. Спробуй ще раз за хвилину або обери інші параметри.")
            user_loading_status[chat_id] = False
        finally:
            if loading_msg_id:
                try:
                    bot.delete_message(chat_id, loading_msg_id)
                except Exception:
                    pass

    def escape_markdown(text: str) -> str:
        escape_chars = r"_*[]()~`>#+-=|{}.!\\"
        return ''.join(['\\' + c if c in escape_chars else c for c in text])

    def _download_image(url: str, timeout: int = 10) -> Image.Image | None:
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content)).convert("RGB")
            return img
        except Exception:
            return None



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

    def _page_window(page: int) -> tuple[int, int]:
        """Рахує діапазон для пагінації:
           - показуємо по одному оголошенню за раз
        """
        start = page
        return start, start + 1

    def send_listing(chat_id: int, show_cards: bool = True):
        listings = prepare_cards_for_display(user_listings.get(chat_id, []))
        user_listings[chat_id] = listings
        page = user_page.get(chat_id, 0)
        limit_reached_after_current = False

        start, end = _page_window(page)

        if start >= len(listings):
            if user_loading_status.get(chat_id, False):
                if not loading_notice_msg_id.get(chat_id):
                    try:
                        _clear_next_prompt(chat_id)  # гасимо “✅ … натисни Далі”, якщо висить
                        m = bot.send_message(chat_id, "⏳ Підвантажуємо нові квартири…")
                        loading_notice_msg_id[chat_id] = m.message_id
                    except Exception:
                        pass
            else:
                bot.send_message(chat_id, "❌ Більше квартир не знайдено.")
            return

        end = min(end, len(listings))

        # --- зберемо message_ids цієї сторінки
        page_bucket = []
        page_msg_ids.setdefault(chat_id, {})  # ініт структури

        # фільтри для caption ...
        city = user_selected_city.get(chat_id, "")
        districts_selected = user_selected_districts.get(chat_id, []) or []
        floors = ", ".join(user_selected_floors.get(chat_id, [])) or ""
        area = user_selected_area.get(chat_id, "") or ""
        rooms = ", ".join(user_selected_rooms.get(chat_id, [])) or ""
        min_budget = user_budget_min.get(chat_id, "") or ""
        max_budget = user_budget_max.get(chat_id, "") or ""

        filters = []
        loc_header = format_city_line(city)
        if loc_header:
            filters.append(f"📍 <b>Локація</b>: {loc_header}")
        if len(districts_selected) == 1:
            filters.append(f"🏙️ <b>Район</b>: {format_full_location(city, districts_selected[0])}")
        elif len(districts_selected) > 1:
            formatted = [format_full_location(city, d) for d in districts_selected]
            filters.append(f"🏙️ <b>Райони</b>: {'; '.join(formatted)}")
        if floors:
            filters.append(f"🏢 <b>Поверх</b>: {floors}")
        if area:
            filters.append(f"📐 <b>Площа</b>: {area}")
        if rooms:
            filters.append(f"🛏️ <b>Кімнат</b>: {rooms}")
        if min_budget and max_budget:
            filters.append(f"💰 <b>Бюджет</b>: {min_budget} – {max_budget} грн")
        elif min_budget and not max_budget:
            filters.append(f"💰 <b>Бюджет</b>: від {min_budget} грн")
        elif max_budget and not min_budget:
            filters.append(f"💰 <b>Бюджет</b>: до {max_budget} грн")
        filters_text = "\n".join(filters) if filters else ""

        # --- КАРТКИ (можемо вимкнути через show_cards=False)
        if show_cards:
            for listing in listings[start:end]:
                try:
                    view_state = register_listing_view(chat_id, listing)
                    if not view_state.get("allowed"):
                        _send_subscription_gate(bot, chat_id)
                        return

                    img_urls = listing.get("img_urls", []) or []
                    collage = None

                    caption = (
                        f"<b>{listing.get('title', 'Без назви')}</b>\n"
                        f"{listing.get('price', '—')}\n\n"
                        f"{filters_text}" if filters_text else
                        f"<b>{listing.get('title', 'Без назви')}</b>\n{listing.get('price', '—')}"
                    )

                    markup = types.InlineKeyboardMarkup()
                    if _has_active_subscription(chat_id):
                        fav_token = remember_card(listing)
                        markup.add(types.InlineKeyboardButton("⭐ В добірку", callback_data=f"fav_toggle:{fav_token}"))
                    if listing.get('link'):
                        markup.add(types.InlineKeyboardButton("🔗 Переглянути", url=listing['link']))

                    if collage:
                        bio = BytesIO()
                        collage.save(bio, format="WEBP", quality=85) # було JPEG замість WEBP
                        bio.seek(0)
                        m = bot.send_photo(chat_id, bio, caption=caption, parse_mode="HTML", reply_markup=markup)
                    elif img_urls:
                        m = bot.send_photo(chat_id, img_urls[0], caption=caption, parse_mode="HTML", reply_markup=markup)
                    else:
                        m = bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup)

                    if m and getattr(m, "message_id", None):
                        page_bucket.append(m.message_id)
                    if (not view_state.get("subscribed")) and int(view_state.get("remaining", 0) or 0) <= 0:
                        limit_reached_after_current = True

                except Exception as e:
                    print(f"[ERROR] Не вдалося відправити картку: {e}")
                    continue

        if limit_reached_after_current:
            nav = types.InlineKeyboardMarkup(row_width=1)
            if start > 0:
                nav.add(types.InlineKeyboardButton("◀️ Назад", callback_data="prev_page"))
            nav.add(types.InlineKeyboardButton("🔓 Відкрити повний доступ", callback_data="subscribe_month"))
            nav.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))
            footer_msg = bot.send_message(
                chat_id,
                "🔒 Безкоштовні 3 оголошення на цей місяць уже закінчилися.\n\n"
                "Можеш повернутися до попереднього варіанту або відкрити повний доступ.",
                reply_markup=nav,
            )
            if footer_msg and getattr(footer_msg, "message_id", None):
                page_bucket.append(footer_msg.message_id)
            page_msg_ids[chat_id][page] = page_bucket
            return

        # --- FOOTER / НАВІГАЦІЯ ---
        nav = types.InlineKeyboardMarkup()
        has_more = (end < len(listings)) or user_loading_status.get(chat_id, False)
        preview_limit_reached = free_views_used_up(chat_id)


        footer = f"🏢 Варіант {start + 1} з {len(listings)}"
        if start > 0:
            nav.add(types.InlineKeyboardButton("◀️ Назад", callback_data="prev_page"))
        if has_more and not preview_limit_reached:
            nav.add(types.InlineKeyboardButton("▶️ Далі", callback_data="next_page"))
        elif preview_limit_reached:
            nav.add(types.InlineKeyboardButton("🔓 Відкрити всі варіанти", callback_data="subscribe_month"))
        nav.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))
        footer_msg = bot.send_message(chat_id, footer, reply_markup=nav)
        if footer_msg and getattr(footer_msg, "message_id", None):
            page_bucket.append(footer_msg.message_id)

        # зберігаємо id’шки для поточної сторінки
        page_msg_ids[chat_id][page] = page_bucket

    def _delete_page_messages(chat_id: int, page: int):
        ids = page_msg_ids.get(chat_id, {}).get(page, []) or []
        for mid in ids:
            safe_delete_message(chat_id, mid)  # твоя безпечна обгортка або bot.delete_message з try/except
        # очистимо запис сторінки, щоб не плодити сміття
        if chat_id in page_msg_ids:
            page_msg_ids[chat_id].pop(page, None)

    def _start_bg_parse_from_state(chat_id: int):
        """
        Стартує background_parse з останніми параметрами пошуку,
        які ми раніше зберегли в user_last_queries[chat_id].
        """
        q = user_last_queries.get(chat_id) or {}
        q_olx = q.get("olx") or {}

        user_loading_status[chat_id] = True
        threading.Thread(
            target=background_parse,
            args=(chat_id, q_olx),
            daemon=True
        ).start()

    @bot.callback_query_handler(func=lambda call: call.data == "next_page")
    def next_listing(call):
        safe_answer_callback(call, "")
        chat_id = call.message.chat.id

        # видаляємо футер, по якому натиснули
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass

        current = user_page.get(chat_id, 0)

        if free_views_used_up(call.from_user.id):
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except:
                pass
            _send_subscription_gate(bot, chat_id)
            return

        # 🔥 при переході вперед — чистимо повідомлення попередньої сторінки
        _delete_page_messages(chat_id, current)

        user_page[chat_id] = current + 1

        start, end = _page_window(user_page[chat_id])
        listings = user_listings.get(chat_id, [])

        if start >= len(listings):
            _show_loading_once(chat_id)
            if not user_loading_status.get(chat_id, False):
                _start_bg_parse_from_state(chat_id)
            return

        send_listing(chat_id, show_cards=True)

    @bot.callback_query_handler(func=lambda call: call.data == "prev_page")
    def prev_listing(call):
        safe_answer_callback(call, "")
        chat_id = call.message.chat.id
        current = user_page.get(chat_id, 0)

        # 1) Видаляємо всі повідомлення поточної сторінки (картки + футер)
        _delete_page_messages(chat_id, current)

        # 2) Зменшуємо індекс сторінки (але не нижче 0)
        user_page[chat_id] = max(0, current - 1)
        prev_page_index = user_page[chat_id]

        # 3) Показуємо тільки футер/навігацію попередньої сторінки (без карток)
        try:
            send_listing(chat_id, show_cards=True)
        except Exception as e:
            print("[prev_listing][send_footer_only][err]", e)

    def _show_loading_once(chat_id: int):
        # гасимо підказку "✅ ... Далі", якщо висить
        _clear_next_prompt(chat_id)

        if loading_notice_msg_id.get(chat_id):
            return  # вже показали

        try:
            m = bot.send_message(chat_id, "⏳ Іде пошук квартир… Зачекайте, будь ласка.")
            loading_notice_msg_id[chat_id] = m.message_id
        except Exception as e:
            print("[_show_loading_once][err]", e)

    #DimRia
    def _parse_budget_thousands(label: str | None) -> int | None:
        """
        'від 10 тис.' -> 10000 ; 'до 25 тис.' -> 25000 ; None/'Не обмежено' -> None
        """
        if not label or "Не обмежено" in label:
            return None
        m = re.search(r"(\d+)", label)
        if not m:
            return None
        return int(m.group(1)) * 1000

    def _rooms_range_from_label(label: list[str] | None) -> tuple[int | None, int | None]:
        """
        ['1 кімната', '2 кімнати'] -> (1, 2)
        ['5 та більше кімнат'] -> (5, None)
        """
        if not label:
            return (None, None)
        nums = []
        has_5_plus = False
        for s in label:
            if "5" in s and "більше" in s:
                has_5_plus = True
                nums.append(5)
            else:
                m = re.search(r"(\d+)", s)
                if m:
                    nums.append(int(m.group(1)))
        if not nums:
            return (None, None)
        lo, hi = min(nums), (None if has_5_plus else max(nums))
        return (lo, hi)

    def _floor_to_from_labels(labels: list[str] | None) -> tuple[int | None, int | None]:
        """
        ['до 9', 'Без 1 поверху', ...] -> (None, 9)
        DOM.RIA не має прямих фільтрів 'без 1/2/останнього', їх пропускаємо.
        """
        if not labels:
            return (None, None)
        to_vals = []
        for s in labels:
            m = re.search(r"до\s+(\d+)", s)
            if m:
                to_vals.append(int(m.group(1)))
        if not to_vals:
            return (None, None)
        return (None, min(to_vals))  # floor_from=None, floor_to=min(обраних 'до N')

    def _area_from_label(label: str | None) -> int | None:
        # "від 50 м2" -> 50
        if not label or "Будь-яка" in label:
            return None
        m = re.search(r"(\d+)", label)
        return int(m.group(1)) if m else None

    def build_query_from_state_for_domria(*, city: str | None, districts: list[str],
                                          floors: list[str], area_label: str | None,
                                          rooms_label: list[str], price_min: str | None,
                                          price_max: str | None, has_pet_flag: str | None) -> dict:
        """
        Повертає q для DomriaApartmentsProvider.build_url(...)
        """
        floor_from, floor_to = _floor_to_from_labels(floors)
        area_from = _area_from_label(area_label)
        rooms_from, rooms_to = _rooms_range_from_label(rooms_label)
        p_from = _parse_budget_thousands(price_min)
        p_to = _parse_budget_thousands(price_max)

        has_pet = None
        # якщо вже зберігаєш у user_has_pet "Має"/"Не має"
        if isinstance(has_pet_flag, str):
            has_pet = (has_pet_flag.strip() == "Має")

        q = {
            "city": city or "",
            "districts": districts or [],
            # поверхи
            "floor_from": floor_from,
            "floor_to": floor_to,
            # площа
            "area_from": area_from,
            # кімнати
            "rooms_from": rooms_from,
            "rooms_to": rooms_to,
            # бюджет (грн)
            "price_from": p_from,
            "price_to": p_to,
            # тварини (True -> 1670_1670, False/None -> нічого)
            "has_pet": has_pet,
            # решта (no_fee додається провайдером автоматично через NO_FEE_DEFAULT=True)
        }
        return q




