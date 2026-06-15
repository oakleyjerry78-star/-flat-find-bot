from __future__ import annotations

from typing import Any
from telebot import types
import threading
from io import BytesIO
import requests
from PIL import Image
import re
from providers.olx_provider import get_olx_provider
from listing_cache import query_cards_for_query, upsert_listings
from city_menu import build_city_markup, city_caption
from app_config import BRAND_NAME
from background_indexer import enqueue_index_job
from gsheets import get_sub_info
from media_utils import edit_step_photo, send_step_photo
from playwright_utils import safe_scroll as _safe_scroll

user_selected_districts = {}  # {chat_id: [district1, district2, ...]}
user_selected_floors = {}     # {chat_id: [поверхи]}
user_selected_city = {}  # {chat_id: "Київ"}
user_selected_area = {}  # {chat_id: "від 50 м2" або "Будь-яка площа 👀"}

user_budget_min = {}
user_budget_max = {}

user_loading_status: dict[int, bool] = {}
user_listings: dict[int, list] = {}
user_page: dict[int, int] = {}
current_category: dict[int, str] = {}
FREE_PREVIEW_LIMIT = 3


def _has_active_subscription(user_id: int | str) -> bool:
    try:
        return str(get_sub_info(str(user_id)) or "").strip().upper() in {"TRUE", "1", "YES", "Y", "T"}
    except Exception as e:
        print("[subscription check error]", e)
        return False


def _subscription_gate_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔓 Відкрити повний доступ", callback_data="subscribe_month"))
    kb.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))
    return kb


def _send_subscription_gate(bot, chat_id: int):
    bot.send_message(
        chat_id,
        "🔒 Безкоштовно доступні перші 3 варіанти.\n\n"
        "Щоб переглядати всі знайдені оголошення без комісії, активуйте доступ до бази.",
        reply_markup=_subscription_gate_markup(),
    )



# якщо у тебе вже є справжня мапа в іншому модулі — ІМПОРТНИ її звідти!
city_url_slug_map = {
    "Київ": "kiev",           # або "kyiv", якщо так у твоєму провайдері
    "Львів": "lvov",
    "Одеса": "odessa",
    "Дніпро": "dnepr",
    "Івано-Франківськ": "ivano-frankovsk",
    "Луцьк": "lutsk",
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

def build_district_markup(districts_list, selected):
    markup = types.InlineKeyboardMarkup(row_width=2)
    for i in range(0, len(districts_list), 2):
        row = districts_list[i:i + 2]
        buttons = []
        for d in row:
            check = "✅" if d.strip() in [s.strip() for s in selected] else ""
            buttons.append(types.InlineKeyboardButton(f"{check} {d}".strip(), callback_data=f"office_district_{d}"))
        markup.add(*buttons)

    all_selected = all(d.strip() in [s.strip() for s in selected] for d in districts_list)
    check_all = "✅ " if all_selected else ""
    markup.add(
        types.InlineKeyboardButton(f"{check_all}Всі райони", callback_data="office_districts_select_all"),
        types.InlineKeyboardButton("Далі 👉", callback_data="office_district_next")
    )
    markup.add(types.InlineKeyboardButton("🔁 Назад", callback_data="office_back_to_city"))
    return markup

def build_floor_markup(selected_floors):
    markup = types.InlineKeyboardMarkup(row_width=3)
    floor_options = [f"до {i}" for i in range(3, 27)]

    # Основні поверхи по 3 в ряд
    for i in range(0, len(floor_options), 3):
        row = []
        for option in floor_options[i:i + 3]:
            check = "✅" if option in selected_floors else ""
            row.append(types.InlineKeyboardButton(f"{check} {option}".strip(), callback_data=f"office_floor_{option}"))
        markup.add(*row)

    # Додаткові опції — 2 в ряд
    extra_row = []
    for label in ["Без 1 поверху", "Без 2 поверху"]:
        check = "✅" if label in selected_floors else ""
        extra_row.append(types.InlineKeyboardButton(f"{check} {label}".strip(), callback_data=f"office_floor_{label}"))
    markup.add(*extra_row)

    # Останній поверх — окремо
    last_label = "Без останнього поверху"
    check = "✅" if last_label in selected_floors else ""
    markup.add(types.InlineKeyboardButton(f"{check} {last_label}", callback_data=f"office_floor_{last_label}"))

    # Будь-який поверх — окремо
    any_label = "Будь-який поверх🥲"
    check = "✅" if any_label in selected_floors else ""
    markup.add(types.InlineKeyboardButton(f"{check} {any_label}", callback_data=f"office_floor_{any_label}"))

    # Кнопки управління
    markup.add(
        types.InlineKeyboardButton("🔁 Назад", callback_data="office_back_to_districts"),
        types.InlineKeyboardButton("Далі 👉", callback_data="office_proceed_to_area")  # було: floor_next
    )

    return markup




def register_office_handlers(bot):
    def show_districts(call, city, districts_list):
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        selected = user_selected_districts.get(chat_id, [])

        if districts_list:
            text = (
                f"🌆 {city},Обери район\n\n"
                "Відміть галочкою ✅ район або райони, в яких ти шукаєш офіс.\n\n"
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
                types.InlineKeyboardButton("Далі 👉", callback_data="office_district_next"),
                types.InlineKeyboardButton("🔁 Назад", callback_data="office_back_to_city")
            )

        try:
            edit_step_photo(bot, chat_id, message_id, "district.png", text, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            print("⚠️ edit_step_photo error:", e)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("office_city_"))
    def handle_city(call):
        bot.answer_callback_query(call.id)
        city = call.data.replace("office_city_", "")
        chat_id = call.message.chat.id
        user_selected_city[chat_id] = city  # ✅ зберігаємо місто
        show_districts(call, city, city_districts_map[city])

    @bot.callback_query_handler(func=lambda call: call.data.startswith("office_district_") and not call.data.startswith(("office_district_next", "office_districts_select_all")))
    def toggle_district(call):
        # Фільтрація службових кнопок
        if call.data in ["office_district_next", "office_districts_select_all"]:
            return

        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        district = call.data.replace("office_district_", "").strip()
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

    @bot.callback_query_handler(func=lambda call: call.data == "office_districts_select_all")
    def select_all_districts(call):
        chat_id = call.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return
        all_districts = city_districts_map.get(city, [])
        current = user_selected_districts.get(chat_id, [])
        user_selected_districts[chat_id] = [] if set(current) == set(all_districts) else all_districts.copy()
        show_districts(call, city, all_districts)

    @bot.callback_query_handler(func=lambda call: call.data == "office_district_next")
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

    @bot.callback_query_handler(func=lambda call: call.data.startswith("office_floor_"))
    def toggle_floor(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        option_raw = call.data.replace("office_floor_", "")

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

    @bot.callback_query_handler(func=lambda call: call.data == "office_back_to_districts")
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

    @bot.callback_query_handler(func=lambda call: call.data == "office_back_to_city")
    def back_to_city(call):
        bot.answer_callback_query(call.id)
        send_step_photo(
            bot,
            call.message.chat.id,
            "city.png",
            city_caption("office"),
            reply_markup=build_city_markup("office"),
            parse_mode="Markdown",
        )


    #Обераєм площу
    def build_area_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        area_options = [f"від {i} м2" for i in range(20, 140, 10)]

        for i in range(0, len(area_options), 3):
            row = [types.InlineKeyboardButton(option, callback_data=f"office_area_{option}") for option in
                   area_options[i:i + 3]]
            markup.add(*row)

        # Додаткові кнопки
        markup.add(types.InlineKeyboardButton("Будь-яка площа 👀", callback_data="office_area_any"))
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="office_back_to_floors"))
        return markup

    @bot.callback_query_handler(func=lambda call: call.data == "office_proceed_to_area")
    def handle_floor_next(call):
        print("[FLOOR NEXT] перехід до площі")
        bot.answer_callback_query(call.id)
        show_area_selection(call)

    def show_area_selection(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "🏡 *Обери площу*\n\n_Обери ВІД якої площі ти розглядаєш офіс 🏬_"
        markup = build_area_markup()

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(bot, chat_id, "area.png", text, reply_markup=markup, parse_mode="Markdown")






    # Обробка вибору площі — ПЕРЕХІД ДО КІМНАТ
    @bot.callback_query_handler(func=lambda call: call.data.startswith("office_area_"))
    def handle_area_selection(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        selected = "Будь-яка площа 👀" if call.data == "office_area_any" else call.data.replace("office_area_", "")
        user_selected_area[chat_id] = selected

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        # ✅ Переходимо до вибору кімнат
        show_budget_from_step(call)

    def build_budget_from_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        options = [f"від {i} тис." for i in [0, 5, 7, 10, 12, 15, 17, 20, 25, 30]]
        buttons = [types.InlineKeyboardButton(text=o, callback_data=f"office_budget_from_{o}") for o in options]
        markup.add(*buttons)
        # 🔙 назад тепер до площі
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="office_back_to_area"))
        return markup

    @bot.callback_query_handler(func=lambda call: call.data == "office_back_to_area")
    def back_to_area_from_budget(call):
        show_area_selection(call)

    def show_budget_from_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "💰 *Обери бюджет*\n\n_Обери ВІД якої вартості в тис. грн. ти розглядаєш офіси 🏬_\n\n*чим більше вартість — тим менше старих ремонтів*"

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

    @bot.callback_query_handler(func=lambda call: call.data.startswith("office_budget_from_"))
    def handle_budget_from(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        selected = call.data.replace("office_budget_from_", "")
        user_budget_min[chat_id] = selected

        show_budget_to_step(call)

    def build_budget_to_markup():
        markup = types.InlineKeyboardMarkup(row_width=3)
        options = [f"до {i} тис." for i in [10, 15, 20, 25, 30, 35, 40, 45, 50, 70, 100]]
        buttons = [types.InlineKeyboardButton(text=o, callback_data=f"office_budget_to_{o}") for o in options]
        markup.add(*buttons)
        markup.add(types.InlineKeyboardButton("Не маю обмежень по бюджету", callback_data="office_budget_to_any"))
        markup.add(types.InlineKeyboardButton("🔙 Повернутися назад", callback_data="office_back_to_budget_from"))
        return markup

    def show_budget_to_step(call):
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        text = "💰 *Обери бюджет*\n\n_Обери ДО якої вартості в тис. грн. ти розглядаєш офіси 🏬_\n\n*чим більше вартість — тим менше старих ремонтів*"

        try:
            bot.delete_message(chat_id, message_id)
        except:
            pass

        send_step_photo(bot, chat_id, "budget.png", text, reply_markup=build_budget_to_markup(), parse_mode="Markdown")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("office_budget_to_"))
    def handle_budget_to(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id

        selected = "Не обмежено" if call.data == "office_budget_to_any" else call.data.replace("office_budget_to_", "")
        user_budget_max[chat_id] = selected

        # ✅ фіксуємо категорію офісів
        current_category[chat_id] = "office"

        # видаляємо повідомлення з кнопками
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass

        # показуємо "рахуємо..."
        loading = bot.send_message(chat_id, "⏳ Рахуємо варіанти… Будь ласка, зачекайте.")
        loading_msg_id = loading.message_id if loading else None

        # запускаємо підрахунок у фоні
        _start_quick_count_and_show_summary_offices(bot, call, loading_msg_id)

    def quick_count_playwright(provider, query: dict, timeout_ms: int = 12000,
                               screenshot_path: str | None = None) -> int | None:
        """
        Повертає точне число з банера “Ми знайшли …”.
        Якщо вибрано кілька районів — рахує кожен район ОКРЕМО і підсумовує.
        """
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        import re, random, requests

        def _norm(s: str) -> str:
            return (s or "").replace("\xa0", " ").replace("\u202f", " ").strip()

        def _parse_count(text: str) -> int | None:
            text = _norm(text)
            m = re.search(r"Ми\s+знайшли\s+([\d\s]+)", text)
            if not m:
                m = re.search(r"(\d[\d\s]{2,})", text[:3000])
            if m:
                try:
                    return int(_norm(m.group(1)).replace(" ", ""))
                except:
                    return None
            return None

        def _count_url(p, page, url, tmo):
            try:
                page.goto(url, timeout=tmo, wait_until="domcontentloaded")
            except PWTimeout:
                return None
            # cookies
            for sel in ("[data-testid='cookies-popup-accept-all']",
                        "[data-testid='cookiesbar-accept']",
                        "button#onetrust-accept-btn-handler",
                        "button:has-text('Прийняти все')",
                        "button:has-text('Погоджуюсь')",
                        "button:has-text('Accept all')"):
                try:
                    page.locator(sel).first.click(timeout=700)
                    page.wait_for_timeout(100)
                    break
                except:
                    pass
            # трохи прокрутки щоб дорендерився банер
            for _ in range(3):
                _safe_scroll(page, 1200)
                page.wait_for_timeout(120 + random.randint(0, 80))
            try:
                page.wait_for_function(
                    "() => /Ми\\s+знайшли\\s+[\\d\\s]+/.test(document.body.innerText)",
                    timeout=max(1500, int(tmo * 0.5))
                )
            except:
                pass
            text = page.evaluate("() => document.body.innerText") or ""
            return _parse_count(text)

        # ---- якщо кілька районів — готуємо окремі запити ----
        districts = query.get("districts") or query.get("district_ids") or []
        district_sets = []
        if isinstance(districts, (list, tuple)) and len(districts) > 1:
            for d in districts:
                q_one = dict(query)
                q_one["districts"] = [d]  # ← рівно ОДИН район
                district_sets.append(q_one)
        else:
            district_sets = [query]

        total = 0
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                ctx = browser.new_context(
                    locale="uk-UA",
                    viewport={"width": 1366, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/122.0.0.0 Safari/537.36")
                )
                ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = ctx.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9"})

                for i, q in enumerate(district_sets):
                    url = provider.build_url(q, page=1)
                    print(f"🔍 URL[{i}]:", url)
                    cnt = _count_url(p, page, url, timeout_ms + 4000)
                    if cnt is None:
                        # швидкий fallback через requests
                        try:
                            r = requests.get(url, headers={
                                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
                                "Accept-Language": "uk-UA,uk;q=0.9",
                            }, timeout=8)
                            if r.ok and r.text:
                                cnt = _parse_count(r.text)
                        except:
                            pass
                    if isinstance(cnt, int):
                        total += cnt
                return total if total > 0 else None
            finally:
                browser.close()

    def _start_quick_count_and_show_summary_offices(bot, call, loading_msg_id=None):
        chat_id = call.message.chat.id

        def _runner():
            try:
                # ---- зчитуємо стан з бота ----
                city = user_selected_city.get(chat_id)
                city_slug = city_url_slug_map.get(city, "")
                districts = user_selected_districts.get(chat_id, [])
                floors = user_selected_floors.get(chat_id, [])
                area_label = user_selected_area.get(chat_id)
                price_min = user_budget_min.get(chat_id)
                price_max = user_budget_max.get(chat_id)

                print("[COUNT][OFFICE] state:", {
                    "city": city, "city_slug": city_slug, "districts": districts,
                    "floors": floors, "area_label": area_label,
                    "budget_from": price_min, "budget_to": price_max
                })

                # ===== OLX quick-count =====
                q_olx = build_query_from_state_for_olx(
                    category="office",
                    city=city,
                    city_slug=city_slug,
                    districts=districts,
                    floors=floors,
                    area_label=area_label,
                    price_min=price_min,
                    price_max=price_max,
                    sort="newest",
                    max_pages=1,
                )
                print("[COUNT][OFFICE][OLX] q_olx →", q_olx)
                enqueue_index_job("office", city)
                cached = query_cards_for_query(
                    category="office",
                    city=city,
                    districts=districts,
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
                print("[quick_count_playwright office] error:", e)
                olx_count = None

            finally:
                if loading_msg_id:
                    try:
                        bot.delete_message(chat_id, loading_msg_id)
                    except:
                        pass

            # показуємо тільки OLX
            total = olx_count if isinstance(olx_count, int) and olx_count > 0 else 0
            print("[COUNT][OFFICE] total to show:", total)
            show_final_summary(chat_id, count=total if isinstance(total, int) else None)

        threading.Thread(target=_runner, daemon=True).start()

    def show_final_summary(chat_id, count=None):
        city = user_selected_city.get(chat_id, "—")
        listings = user_listings.get(chat_id, [])

        # ✅ коректний фолбек
        if isinstance(count, int) and count > 0:
            final_count = count
        elif listings:
            final_count = len(listings)
        else:
            final_count = 0

        districts = ", ".join(user_selected_districts.get(chat_id, [])) or "—"
        floors = ", ".join(user_selected_floors.get(chat_id, [])) or "Без обмежень"
        area = user_selected_area.get(chat_id, "—")
        budget_from = user_budget_min.get(chat_id, "—")
        budget_to = user_budget_max.get(chat_id, "—")

        budget_text = (
            f"Бюджет: від {budget_from}" if budget_to == "Не обмежено"
            else f"Бюджет: від {budget_from} до {budget_to}"
        )

        count_phrase = f"*{final_count} офісних варіантів*" if final_count else "*актуальні офісні варіанти*"

        text = (
            f"🏢 *{BRAND_NAME}* знайшов {count_phrase} без комісії за заданими фільтрами.\n\n"
            "👀 *Хочеш переглянути їх або оновити пошук?*\n\n"
            "✅ *Твої параметри:*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Місто:* `{city}`\n"
            f"🏙️ *Райони:* `{districts}`\n"
            f"🏢 *Поверх:* `{floors}`\n"
            f"📐 *Площа:* `{area}`\n"
            f"💰 *{budget_text} грн.*\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton("🔍 Переглянути варіанти", callback_data="office_show_results"),
            types.InlineKeyboardButton("🔄 Оновити параметри", callback_data="office_restart_search")
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

    @bot.callback_query_handler(func=lambda call: call.data == "office_restart_search")
    def handle_restart_search(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        show_update_parameters_menu(chat_id)

    @bot.callback_query_handler(func=lambda call: call.data == "office_edit_districts")
    def office_edit_districts(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        city = user_selected_city.get(chat_id)
        if not city:
            return
        show_districts(call, city, city_districts_map.get(city, []))

    @bot.callback_query_handler(func=lambda call: call.data == "office_back_to_results")
    def office_back_to_results(call):
        bot.answer_callback_query(call.id)
        show_final_summary(call.message.chat.id)

    @bot.callback_query_handler(func=lambda call: call.data == "office_back_to_floors")
    def office_back_to_floors(call):
        bot.answer_callback_query(call.id)
        # повертаємось на вибір поверху
        go_to_floor_selection(call)

    @bot.callback_query_handler(func=lambda call: call.data == "office_back_to_budget_from")
    def office_back_to_budget_from(call):
        bot.answer_callback_query(call.id)
        # показуємо "ВІД" бюджету
        show_budget_from_step(call)


    def build_update_parameters_keyboard():
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("📍 Місто", callback_data="office_back_to_city"),
            types.InlineKeyboardButton("🏙️ Райони", callback_data="office_edit_districts"),
            types.InlineKeyboardButton("🏢 Поверх", callback_data="office_back_to_floors"),
            types.InlineKeyboardButton("📐 Площа", callback_data="office_back_to_area"),
            types.InlineKeyboardButton("💰 Бюджет", callback_data="office_back_to_budget_from"),
        )
        markup.add(
            types.InlineKeyboardButton("⬅️ Назад до результатів", callback_data="office_back_to_results"),
            types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu")
        )
        return markup



    # Кнопка десь у вашому меню:
    # types.InlineKeyboardButton("🔍 Переглянути варіанти", callback_data="office_show_results")

    @bot.callback_query_handler(func=lambda c: c.data == "office_show_results")
    def office_show_results(call):
        chat_id = call.message.chat.id
        current_category[chat_id] = "office"

        print(f"[OFFICE_BTN] Користувач {chat_id} натиснув 'Переглянути варіанти' (офіси)")

        try:
            bot.answer_callback_query(call.id, "Показую варіанти", cache_time=0, show_alert=False)
        except Exception as e:
            print(f"[OFFICE_BTN] Не вдалося відповісти на callback: {e}")

        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception as e:
            print(f"[OFFICE_BTN] Не вдалося вимкнути клавіатуру: {e}")

        if user_listings.get(chat_id):
            user_page[chat_id] = 0
            send_listing_office(chat_id)
            return

        loading_msg = None
        try:
            loading_msg = bot.send_message(chat_id, "⏳ Готую перші варіанти…")
        except Exception as e:
            print(f"[OFFICE_BTN] Не вдалося відправити повідомлення про пошук: {e}")

        threading.Thread(
            target=_do_search_and_send_office,
            args=(chat_id, loading_msg.message_id if loading_msg else None),
            daemon=True
        ).start()

    def _to_bot_card(it) -> dict:
        price_uah = getattr(it, "price_uah", None)
        price_txt = f"{price_uah:,} грн".replace(",", " ") if price_uah else "—"
        return {
            "title": getattr(it, "title", "") or "Без назви",
            "price": price_txt,
            "link": getattr(it, "url", "") or "",
            "img_urls": (getattr(it, "photos", []) or [])[:4],
            "_key": getattr(it, "id", None) or getattr(it, "url", ""),
        }

    def _do_search_and_send_office(chat_id, loading_msg_id=None):
        try:
            user_loading_status[chat_id] = True

            # 1) Збираємо стан
            city = user_selected_city.get(chat_id)
            city_slug = city_url_slug_map.get(city, "")
            districts = user_selected_districts.get(chat_id, [])
            floors = user_selected_floors.get(chat_id, [])
            area_label = user_selected_area.get(chat_id)
            price_min = user_budget_min.get(chat_id)
            price_max = user_budget_max.get(chat_id)
            category = "office"

            print("[OFFICE_SEARCH] Зібраний стан:", {
                "city": city,
                "city_slug": city_slug,
                "districts": districts,
                "floors": floors,
                "area_label": area_label,
                "price_min": price_min,
                "price_max": price_max
            })

            # 2) Формуємо запит
            q = build_query_from_state_for_olx(
                category=category,
                city=city,
                city_slug=city_slug,
                districts=districts,
                floors=floors,
                area_label=area_label,

                price_min=price_min,
                price_max=price_max,

                sort="newest",
                max_pages=1,
            )



            print("[OFFICE_SEARCH] Запит q:", q)
            enqueue_index_job(category, city)

            provider = get_olx_provider(category)
            print("[OFFICE_SEARCH] Провайдер:", provider)
            cached = query_cards_for_query(
                category=category,
                city=city,
                districts=districts,
                query=q,
                limit=100,
            )
            if cached:
                user_listings[chat_id] = cached
                user_page[chat_id] = 0
                user_loading_status[chat_id] = False
                send_listing_office(chat_id)
                return
            safe_send_message(
                chat_id,
                "⏳ База офісів для цих параметрів зараз оновлюється. Я поставив пошук у пріоритет — спробуй переглянути варіанти ще раз за хвилину.",
            )
            user_loading_status[chat_id] = False
            return

            # 3) Перша сторінка
            listings_first = provider.search({**q, "max_pages": 1})
            if listings_first:
                try:
                    upsert_listings(category, city, listings_first)
                except Exception as e:
                    print("[OFFICE cache upsert][error]", e)
            print(f"[OFFICE_SEARCH] Отримано {len(listings_first)} офісів на 1-й сторінці")

            cards_first = [_to_bot_card(it) for it in listings_first][:100]
            user_listings[chat_id] = cards_first
            user_page[chat_id] = 0

            send_listing_office(chat_id)

            # 4) Фонове довантаження
            def background_parse(q_local, chat_id_local):
                try:
                    more = provider.search({**q_local, "max_pages": 15})
                    if more:
                        try:
                            upsert_listings(category, city, more)
                        except Exception as e:
                            print("[OFFICE background cache upsert][error]", e)
                    print(f"[OFFICE_SEARCH_BG] Довантажено {len(more)} офісів")
                    more_cards = [_to_bot_card(it) for it in more]

                    base = user_listings.get(chat_id_local, []) or []
                    seen = {(b.get("_key") or b.get("link") or "") for b in base}

                    merged = base[:]
                    for card in more_cards:
                        key = card.get("_key") or card.get("link") or ""
                        if key and key not in seen:
                            seen.add(key)
                            merged.append(card)

                    user_listings[chat_id_local] = merged
                    if len(merged) > len(base):
                        kb = types.InlineKeyboardMarkup()
                        kb.add(types.InlineKeyboardButton("▶️ Далі", callback_data="next_page_office"))
                        safe_send_message(chat_id_local, "✅ Довантажено ще офіси.", reply_markup=kb)

                except Exception as e:
                    print(f"[OFFICE_SEARCH_BG][ERROR] {e}")
                finally:
                    user_loading_status[chat_id_local] = False

            threading.Thread(target=background_parse, args=(q, chat_id), daemon=True).start()

        except Exception as e:
            print(f"[OFFICE_SEARCH][ERROR] {e}")
            safe_send_message(chat_id, "❌ Пошук тимчасово не вдався. Спробуй ще раз за хвилину або обери інші параметри.")
            user_loading_status[chat_id] = False
        finally:
            if loading_msg_id:
                try:
                    bot.delete_message(chat_id, loading_msg_id)
                except Exception:
                    pass

    def send_listing_office(chat_id: int, page_size: int = 1):
        listings = user_listings.get(chat_id, [])
        page = user_page.get(chat_id, 0)
        start, end = page * page_size, page * page_size + page_size

        if start >= len(listings):
            if user_loading_status.get(chat_id, False):
                bot.send_message(chat_id, "⏳ Йде пошук офісів… Зачекайте, будь ласка.")
            else:
                bot.send_message(chat_id, "❌ Варіанти офісів закінчились. Спробуй змінити райони або бюджет.")
            return

        # 📌 Додаткові фільтри (БЕЗ кімнат і тварин)
        city = user_selected_city.get(chat_id, "")
        districts = ", ".join(user_selected_districts.get(chat_id, []))
        floors = ", ".join(user_selected_floors.get(chat_id, []))
        area = user_selected_area.get(chat_id, "")
        min_budget = user_budget_min.get(chat_id, "")
        max_budget = user_budget_max.get(chat_id, "")

        filters = []
        if city: filters.append(f"📍 <b>Місто</b>: {city}")
        if districts: filters.append(f"📌 <b>Райони</b>: {districts}")
        if floors: filters.append(f"🏢 <b>Поверх</b>: {floors}")
        if area: filters.append(f"📐 <b>Площа</b>: {area}")
        if min_budget and max_budget: filters.append(f"💰 <b>Бюджет</b>: {min_budget} – {max_budget} грн")
        filters_text = "\n".join(filters)

        for listing in listings[start:end]:
            try:
                img_urls = listing.get("img_urls", [])
                collage = None

                caption = (
                    f"<b>{listing['title']}</b>\n"
                    f"{listing['price']}\n\n"
                    f"{filters_text}"
                )

                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔗 Переглянути", url=listing['link']))

                if collage:
                    bio = BytesIO()
                    collage.save(bio, format="JPEG", quality=85)
                    bio.seek(0)
                    bot.send_photo(chat_id, bio, caption=caption, parse_mode="HTML", reply_markup=markup)
                elif img_urls:
                    bot.send_photo(chat_id, img_urls[0], caption=caption, parse_mode="HTML", reply_markup=markup)
                else:
                    bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=markup)

            except Exception as e:
                print(f"[ERROR] Не вдалось відправити картку: {e}")
                bot.send_message(chat_id,
                                 f"{listing.get('title', '')}\n{listing.get('price', '')}\n{listing.get('link', '')}")

        # 📌 Кнопки навігації
        markup = types.InlineKeyboardMarkup()
        buttons = []

        has_prev = start > 0
        has_next = end < len(listings) or user_loading_status.get(chat_id, False)
        preview_limit_reached = (end >= FREE_PREVIEW_LIMIT) and not _has_active_subscription(chat_id)

        if has_prev:
            buttons.append(types.InlineKeyboardButton("◀️ Назад", callback_data="prev_page_office"))
        if has_next and not preview_limit_reached:
            buttons.append(types.InlineKeyboardButton("▶️ Далі", callback_data="next_page_office"))
        elif preview_limit_reached:
            buttons.append(types.InlineKeyboardButton("🔓 Відкрити всі варіанти", callback_data="subscribe_month"))

        if buttons:
            markup.add(*buttons)

        markup.add(types.InlineKeyboardButton("🏠 Головне меню", callback_data="back_to_menu"))

        bot.send_message(
            chat_id,
            f"📦 Варіант {start + 1} з {len(listings)} офісів",
            reply_markup=markup
        )

    @bot.callback_query_handler(func=lambda call: call.data == "next_page_office")
    def next_listing_office(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        if user_page.get(chat_id, 0) + 1 >= FREE_PREVIEW_LIMIT and not _has_active_subscription(call.from_user.id):
            try:
                bot.delete_message(chat_id, call.message.message_id)
            except:
                pass
            _send_subscription_gate(bot, chat_id)
            return
        user_page[chat_id] += 1
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        send_listing_office(chat_id)

    @bot.callback_query_handler(func=lambda call: call.data == "prev_page_office")
    def prev_listing_office(call):
        bot.answer_callback_query(call.id)
        chat_id = call.message.chat.id
        user_page[chat_id] = max(0, user_page.get(chat_id, 0) - 1)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except:
            pass
        send_listing_office(chat_id)

    def build_query_from_state_for_olx(
            category: str,
            city: str | None,
            city_slug: str | None,
            districts: list[str] | None,
            floors: list[str] | None,
            area_label: str | None,

            price_min: str | int | None,
            price_max: str | int | None,

            sort: str = "newest",
            max_pages: int = 3,
    ) -> dict:
        import re

        def _parse_budget(v):
            if v is None:
                return None
            if isinstance(v, (int, float)):
                return int(v)
            s = str(v).strip().lower().replace("грн", "").replace("uah", "")
            if "не обмеж" in s:
                return None
            m = re.search(r"(\d+)", s)
            if not m:
                return None
            n = int(m.group(1))
            if "тис" in s or "тыс" in s:
                n *= 1000
            return n

        def _parse_area(label: str | None):
            if not label:
                return (None, None)
            s = label.lower().replace("м2", "").replace("м²", "").replace(" ", "")
            if s.startswith("від") or s.startswith("vid"):
                m = re.search(r"(\d+)", s);
                return (int(m.group(1)) if m else None, None)
            if s.startswith("до") or s.startswith("do"):
                m = re.search(r"(\d+)", s);
                return (None, int(m.group(1)) if m else None)
            if "–" in s or "-" in s:
                sep = "–" if "–" in s else "-"
                a, b = s.split(sep, 1)
                try:
                    return (int(re.search(r"\d+", a).group()), int(re.search(r"\d+", b).group()))
                except:
                    return (None, None)
            if s.endswith("+"):
                try:
                    return (int(re.search(r"\d+", s).group()), None)
                except:
                    return (None, None)
            m = re.search(r"(\d+)", s)
            return (int(m.group(1)) if m else None, None)

        def _parse_floors(labels: list[str] | None):
            if not labels:
                return None
            s = " | ".join(str(x) for x in labels).lower()
            if "будь-який" in s:
                return None
            f_from, f_to = None, None
            for lbl in labels:
                t = str(lbl).lower()
                if t.startswith("до"):
                    m = re.search(r"(\d+)", t)
                    if m:
                        f_to = int(m.group(1))
            if any("без 1" in str(x).lower() for x in labels):
                f_from = max(f_from or 2, 2)
            if any("без 2" in str(x).lower() for x in labels):
                f_from = max(f_from or 3, 3)
            if any("без останнього" in str(x).lower() for x in labels):
                MAX_FLOOR_DEFAULT = 26
                f_to = min(f_to or MAX_FLOOR_DEFAULT, MAX_FLOOR_DEFAULT)
            if f_from is None and f_to is None:
                nums = []
                for lbl in labels:
                    for m in re.findall(r"\d+", str(lbl)):
                        try:
                            nums.append(int(m))
                        except:
                            pass
                if nums:
                    f_from, f_to = min(nums), max(nums)
            return {"from": f_from, "to": f_to} if (f_from is not None or f_to is not None) else None

        area_from, area_to = _parse_area(area_label)
        floor = _parse_floors(floors)
        p_from = _parse_budget(price_min)
        p_to = _parse_budget(price_max)

        q = {
            "category": category,  # "office"
            "city": city,
            "city_slug": (city_slug or "").strip(),
            "districts": districts or [],  # назви, провайдер сам змепить у IDs
            "price_from": p_from,
            "price_to": p_to,
            "max_pages": max_pages,
            "sort": "newest" if sort == "newest" else sort,
            "no_fee": True,  # без комісії
            "area": None,
            "floor": floor,
            "rooms": None,
            "allows_pets": False,
            "pet_types": [],
        }
        if area_from is not None or area_to is not None:
            q["area"] = {"from": area_from}
            if area_to is not None:
                q["area"]["to"] = area_to

        if isinstance(q["price_from"], int) and isinstance(q["price_to"], int) and q["price_to"] < q["price_from"]:
            q["price_to"] = q["price_from"]

        print("[OFFICE_SEARCH] normalized q →", q)
        return q

    def safe_send_message(chat_id: int, text: str, **kwargs):
        try:
            bot.send_message(chat_id, text, **kwargs)
        except Exception as ex:
            print(f"[send_message error] {ex} | text={text!r}")

    def escape_markdown(text: str) -> str:
        escape_chars = r"_*[]()~>#+-=|{}.!\\"
        return ''.join(['\\' + c if c in escape_chars else c for c in text])

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
