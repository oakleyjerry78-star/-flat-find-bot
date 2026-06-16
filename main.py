from telebot import TeleBot, types
from telebot.types import ReplyKeyboardRemove
from city_handlers import register_city_handlers
from house_handlers import register_house_handlers
from rooms_handlers import register_rooms_handlers
from office_handlers import register_office_handlers
from state import begin_category_session
from resilience import safe_handler
from helpers import safe_send
from media_utils import send_step_photo
from city_menu import build_city_markup, city_caption, parse_city_page_callback
from telebot import apihelper
import hashlib
import hmac
import html
import json
import threading
import time, logging, requests
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter, Retry
from telebot import types
import urllib.request
import os
import dotenv
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

load_dotenv()  # ← Обов'язково перед os.getenv
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
from app_config import (
    BOT_USERNAME,
    BRAND_NAME,
    REFERRAL_PAYOUT_RATE,
    REFERRAL_PAYOUT_UAH,
    SUPPORT_USERNAME,
    SUBSCRIPTION_PERIOD_TEXT,
    SUBSCRIPTION_PRICE_UAH,
    TAGLINE,
)
from background_indexer import get_indexer_status, start_background_indexer
from listing_cache import stats as cache_stats
from gsheets import (
    cancel_subscription_renewal, ensure_user, set_ref_from, set_subscription,
    upsert_ref_stats, get_ref_count, get_paid_count, get_sub_info, get_ref_summary,
    payment_id_already_processed,
    expire_old_subscriptions,
    log_payout, mark_payout_paid,  # якщо плануєш логати виплати
)

# === Основні словники ===
user_sub = {}          # user_id -> bool
pending_orders = {}    # user_id -> orderReference
payment_orders = {}    # orderReference -> user_id

web_app = Flask(__name__)

# ретраї для HTTP-сесії, яку використовує pyTelegramBotAPI
sess = apihelper._get_req_session()
retries = Retry(
    total=5, connect=5, read=5,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    raise_on_status=False,
)
adapter = HTTPAdapter(max_retries=retries, pool_connections=20, pool_maxsize=50)
sess.mount("https://", adapter)
sess.mount("http://", adapter)


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не заданий у .env або змінних середовища")

bot = TeleBot(BOT_TOKEN)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _price_text() -> str:
    return f"{float(_env('WAYFORPAY_PRODUCT_PRICE', str(SUBSCRIPTION_PRICE_UAH))):.2f}"


def _price_value() -> float:
    return float(_price_text())


def _public_base_url() -> str:
    configured = _env("WAYFORPAY_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    service_url = _env("WAYFORPAY_SERVICE_URL")
    if service_url:
        parsed = urlparse(service_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    railway_domain = _env("RAILWAY_PUBLIC_DOMAIN") or _env("RAILWAY_STATIC_URL")
    if railway_domain:
        if not railway_domain.startswith(("http://", "https://")):
            railway_domain = "https://" + railway_domain
        return railway_domain.rstrip("/")
    return "https://flat-find-bot-production.up.railway.app"


def _wayforpay_service_url() -> str:
    return _env("WAYFORPAY_SERVICE_URL", f"{_public_base_url()}/wayforpay/callback")


def _wayforpay_domain_name() -> str:
    configured = _env("WAYFORPAY_MERCHANT_DOMAIN")
    if configured:
        return configured
    parsed = urlparse(_public_base_url())
    return parsed.netloc or "flat-find-bot-production.up.railway.app"


def _hmac_md5(parts: list[str]) -> str:
    secret = _env("WAYFORPAY_SECRET_KEY")
    data = ";".join(str(part) for part in parts)
    return hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.md5).hexdigest()


def _build_order_reference(user_id: int | str) -> str:
    return f"FF-{user_id}-{int(time.time())}"


def _user_id_from_order_reference(order_reference: str) -> str | None:
    parts = (order_reference or "").split("-")
    if len(parts) >= 3 and parts[0] == "FF" and parts[1].isdigit():
        return parts[1]
    return None


def _payment_ready() -> bool:
    return bool(_env("WAYFORPAY_MERCHANT_ACCOUNT") and _env("WAYFORPAY_SECRET_KEY"))


def _bot_username_for_url() -> str:
    return BOT_USERNAME.lstrip("@")


def _payment_form(order_reference: str) -> str:
    merchant_account = _env("WAYFORPAY_MERCHANT_ACCOUNT")
    merchant_domain = _wayforpay_domain_name()
    order_date = str(int(time.time()))
    amount = _price_text()
    currency = _env("WAYFORPAY_CURRENCY", "UAH")
    product_name = _env("WAYFORPAY_PRODUCT_NAME", f"Підписка {BRAND_NAME} на 1 місяць")
    product_count = "1"
    product_price = amount
    return_url = _env("WAYFORPAY_RETURN_URL", f"https://t.me/{_bot_username_for_url()}")
    service_url = _wayforpay_service_url()

    signature = _hmac_md5([
        merchant_account,
        merchant_domain,
        order_reference,
        order_date,
        amount,
        currency,
        product_name,
        product_count,
        product_price,
    ])

    fields = {
        "merchantAccount": merchant_account,
        "merchantAuthType": "SimpleSignature",
        "merchantDomainName": merchant_domain,
        "merchantSignature": signature,
        "orderReference": order_reference,
        "orderDate": order_date,
        "amount": amount,
        "currency": currency,
        "productName[]": product_name,
        "productPrice[]": product_price,
        "productCount[]": product_count,
        "serviceUrl": service_url,
        "returnUrl": return_url,
        "language": "UA",
    }
    if _env("WAYFORPAY_RECURRING_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}:
        next_payment_ts = int(time.time()) + 30 * 24 * 60 * 60
        fields.update({
            "regularOn": "1",
            "regularMode": _env("WAYFORPAY_REGULAR_MODE", "monthly"),
            "dateNext": time.strftime("%d.%m.%Y", time.localtime(next_payment_ts)),
        })
        regular_count = _env("WAYFORPAY_REGULAR_COUNT")
        if regular_count:
            fields["regularCount"] = regular_count
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(key)}" value="{html.escape(str(value))}">'
        for key, value in fields.items()
    )
    return f"""<!doctype html>
<html lang="uk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Оплата {html.escape(BRAND_NAME)}</title>
  <style>
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#111d29; color:#fff; font-family:Arial,sans-serif; }}
    button {{ padding:14px 22px; border:0; border-radius:10px; font-size:18px; font-weight:700; color:#fff; background:#7c3aed; }}
  </style>
</head>
<body>
  <form id="pay" method="post" action="https://secure.wayforpay.com/pay">
    {inputs}
    <button type="submit">Перейти до оплати {html.escape(amount)} грн</button>
  </form>
  <script>document.getElementById("pay").submit();</script>
</body>
</html>"""


def _flatten_payload(data: dict) -> dict:
    return {key: value[0] if isinstance(value, list) and value else value for key, value in data.items()}


def _verify_wayforpay_callback(payload: dict) -> bool:
    signature = payload.get("merchantSignature") or payload.get("signature")
    if not signature:
        return False
    expected = _hmac_md5([
        payload.get("merchantAccount", ""),
        payload.get("orderReference", ""),
        payload.get("amount", ""),
        payload.get("currency", ""),
        payload.get("authCode", ""),
        payload.get("cardPan", ""),
        payload.get("transactionStatus", ""),
        payload.get("reasonCode", ""),
    ])
    return hmac.compare_digest(str(signature), expected)


def _wayforpay_accept_response(order_reference: str) -> dict:
    response_time = str(int(time.time()))
    status = "accept"
    return {
        "orderReference": order_reference,
        "status": status,
        "time": response_time,
        "signature": _hmac_md5([order_reference, status, response_time]),
    }


def _activate_paid_subscription(user_id: str, order_reference: str, payment_id: str | None = None) -> None:
    payment_id = (payment_id or order_reference or "").strip()
    if payment_id_already_processed(payment_id):
        logging.info("Payment %s already processed; skipping duplicate activation", payment_id)
        return

    user_sub[int(user_id)] = True
    set_subscription(
        str(user_id),
        active=True,
        price_uah=_price_value(),
        period_text=SUBSCRIPTION_PERIOD_TEXT,
        payment_id=payment_id,
    )
    upsert_ref_stats(str(user_id), payout_rate=REFERRAL_PAYOUT_RATE, default_price=_price_value())
    try:
        send_step_photo(
            bot,
            int(user_id),
            "success.jpg",
            "🎉 Преміум-доступ активовано.\n\nТепер можна переглядати всі знайдені варіанти без обмежень.",
            parse_mode="HTML",
        )
    except Exception:
        logging.exception("Не вдалося надіслати повідомлення про успішну оплату")


@web_app.get("/")
def healthcheck():
    return "Flat Find bot is running", 200


@web_app.get("/pay/<order_reference>")
def pay_page(order_reference: str):
    if not _payment_ready():
        return Response("WayForPay не налаштований", status=503)
    return Response(_payment_form(order_reference), mimetype="text/html")


@web_app.route("/wayforpay/callback", methods=["POST"])
def wayforpay_callback():
    payload = request.get_json(silent=True)
    if not payload:
        payload = _flatten_payload(request.form.to_dict(flat=False))
    logging.info("[WayForPay callback] %s", json.dumps(payload, ensure_ascii=False))

    order_reference = str(payload.get("orderReference", ""))
    if not order_reference:
        return jsonify({"status": "missing orderReference"}), 400

    if not _verify_wayforpay_callback(payload):
        logging.warning("[WayForPay callback] bad signature for %s", order_reference)
        return jsonify({"status": "bad signature"}), 403

    if payload.get("transactionStatus") == "Approved":
        user_id = _user_id_from_order_reference(order_reference)
        if user_id:
            payment_id = str(payload.get("transactionId") or payload.get("invoiceId") or order_reference)
            _activate_paid_subscription(user_id, order_reference, payment_id)

    return jsonify(_wayforpay_accept_response(order_reference))

try:
    bot.set_my_description(
        "Flat Find допомагає знайти квартиру, будинок або кімнату без комісії.\n\n"
        "Обери місто, район, бюджет і параметри — бот покаже актуальні варіанти. "
        "Без підписки доступні 3 оголошення на місяць."
    )
    bot.set_my_short_description("Пошук житла без комісії")
    bot.set_my_commands([
        types.BotCommand("start", "Головне меню"),
        types.BotCommand("menu", "Показати меню"),
        types.BotCommand("rent", "Пошук оренди"),
        types.BotCommand("buy", "Купівля - в розробці"),
        types.BotCommand("subscribe", "Підписка"),
        types.BotCommand("support", "Зв'язок"),
        types.BotCommand("about", "Про сервіс"),
        types.BotCommand("cache", "Статус бази оголошень"),
        types.BotCommand("stats", "Статистика кешу"),
        types.BotCommand("help", "Допомога"),
    ])
except Exception as e:
    logging.warning("Не вдалося оновити меню команд Telegram: %s", e)


# Реєстрація обробників
register_city_handlers(bot)
register_house_handlers(bot)
register_rooms_handlers(bot)
register_office_handlers(bot)





def get_main_menu():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)

    row1 = [
        types.KeyboardButton("Оренда без комісії 🏢"),
        types.KeyboardButton("Купівля - в розробці 🏗")
    ]
    row2 = [
        types.KeyboardButton("Підписка 🔒"),
        types.KeyboardButton("Про сервіс ℹ️")
    ]
    row3 = [
        types.KeyboardButton("Мої добірки 🔒"),
        types.KeyboardButton(f"Партнерство з {BRAND_NAME} 💰")
    ]
    row4 = [
        types.KeyboardButton("Зв'язок 🛠"),
        types.KeyboardButton("Договори оренди 📄")
    ]

    keyboard.add(*row1)
    keyboard.add(*row2)
    keyboard.add(*row3)
    keyboard.add(*row4)
    return keyboard

def get_rent_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        types.KeyboardButton("Оренда квартири 🏢"),
        types.KeyboardButton("Оренда будинку 🏡")
    )
    kb.add(
        types.KeyboardButton("Оренда офісу 🏬"),
        types.KeyboardButton("Оренда кімнати 🛏")
    )
    kb.add(types.KeyboardButton("🔙 Назад"))
    return kb




@bot.message_handler(commands=['start'])
@safe_handler
def start_handler(message):
    args = message.text.split()
    user_id = str(message.from_user.id)
    username = message.from_user.username or ""

    # гарантуємо наявність користувача в БД (users sheet)
    ensure_user(user_id, username)

    # якщо прийшов за реф.посиланням
    if len(args) > 1:
        referrer_id = args[1]
        if referrer_id != user_id:
            set_ref_from(user_id, referrer_id)
            upsert_ref_stats(referrer_id, payout_rate=REFERRAL_PAYOUT_RATE, default_price=_price_value())

    text = (
        "🏠 *Flat Find — знайди житло без комісії*\n\n"
        "Підберемо актуальні квартири, будинки, кімнати або офіси за твоїми фільтрами.\n"
        "Без підписки можна переглянути 3 оголошення на місяць."
    )
    bot.send_message(message.chat.id, text, reply_markup=get_main_menu(), parse_mode="Markdown")


@bot.message_handler(commands=["menu"])
@safe_handler
def menu_command(message):
    bot.send_message(message.chat.id, "Головне меню", reply_markup=get_main_menu())


@bot.message_handler(commands=["rent"])
@safe_handler
def rent_command(message):
    bot.send_message(message.chat.id, "Що будемо шукати в оренду?", reply_markup=get_rent_menu())


@bot.message_handler(commands=["buy"])
@safe_handler
def buy_command(message):
    send_buy_in_development(message.chat.id)


@bot.message_handler(commands=["subscribe"])
@safe_handler
def subscribe_command(message):
    subscription_handler(message)


@bot.message_handler(commands=["support"])
@safe_handler
def support_command(message):
    support_handler(message)


@bot.message_handler(commands=["about"])
@safe_handler
def about_command(message):
    handle_about_us(message)


@bot.message_handler(commands=["cache"])
@safe_handler
def cache_command(message):
    data = cache_stats()
    indexer = get_indexer_status()
    parts = [
        f"Активних оголошень: {data.get('active_total', 0)}",
        f"Індексатор: {'працює' if indexer.get('running') else 'очікує'}",
    ]
    if indexer.get("category") or indexer.get("city"):
        source = indexer.get("source") or "-"
        mode = indexer.get("mode") or "-"
        parts.append(f"Зараз: {source} / {indexer.get('category') or '-'} / {indexer.get('city') or '-'} ({mode})")
    parts.append(f"Черга індексатора: {indexer.get('queue_size', 0)}")
    parts.append(f"Активних задач: {indexer.get('active_jobs', 0)}")
    parts.append(f"Гарячих пошуків: {indexer.get('hot_jobs', 0)}")
    parts.append(
        f"Worker-и: {indexer.get('workers', 1)} | сторінок фон/пріоритет: "
        f"{indexer.get('max_pages', '-')} / {indexer.get('hot_max_pages', '-')}"
    )
    parts.append(f"Останнє збереження: {indexer.get('last_saved', 0)}")
    if indexer.get("last_stale") or indexer.get("last_purged"):
        parts.append(f"Прибрано старих: {indexer.get('last_stale', 0)} / очищено: {indexer.get('last_purged', 0)}")
    if data.get("by_source"):
        parts.append("Джерела: " + ", ".join(f"{source}: {count}" for source, count in data["by_source"].items()))
    for category, count in (data.get("by_category") or {}).items():
        photo_count = (data.get("with_photo") or {}).get(category, 0)
        parts.append(f"{category}: {count} (з фото: {photo_count})")
    if indexer.get("last_error"):
        parts.append(f"Остання помилка: {indexer.get('last_error')}")
    bot.send_message(message.chat.id, "\n".join(parts))


@bot.message_handler(commands=["stats"])
@safe_handler
def stats_command(message):
    cache_command(message)


@bot.message_handler(commands=["help"])
@safe_handler
def help_command(message):
    bot.send_message(
        message.chat.id,
        "Команди:\n"
        "/start - головне меню\n"
        "/rent - пошук оренди\n"
        "/buy - купівля в розробці\n"
        "/subscribe - підписка\n"
        "/support - зв'язок\n"
        "/about - про сервіс\n"
        "/cache або /stats - стан бази оголошень"
    )


@bot.message_handler(func=lambda m: m.text == "Оренда без комісії 🏢")
@safe_handler
def open_rent_menu(message):
    bot.send_message(
        message.chat.id,
        "Обери формат житла або приміщення:",
        reply_markup=get_rent_menu()
    )



@bot.message_handler(func=lambda msg: msg.text == "Оренда квартири 🏢")
@safe_handler
def find_flat_handler(message):
    begin_category_session(bot, message.chat.id, "apartment")  # ✅
    send_step_photo(
        bot,
        message.chat.id,
        "city.png",
        city_caption("apartment"),
        reply_markup=build_city_markup("apartment"),
        parse_mode="Markdown"
    )

    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )



@bot.message_handler(func=lambda msg: msg.text == "Оренда будинку 🏡")
@safe_handler
def find_house_handler(message):
    begin_category_session(bot, message.chat.id, "house")      # ✅
    send_step_photo(
        bot,
        message.chat.id,
        "city.png",
        city_caption("house"),
        reply_markup=build_city_markup("house"),
        parse_mode="Markdown"
    )

    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )



@bot.message_handler(func=lambda msg: msg.text == "Оренда кімнати 🛏")
@safe_handler
def find_room_handler(message):
    begin_category_session(bot, message.chat.id, "room")        # ✅
    send_step_photo(
        bot,
        message.chat.id,
        "city.png",
        city_caption("room"),
        reply_markup=build_city_markup("room"),
        parse_mode="Markdown"
    )

    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )



@bot.message_handler(func=lambda msg: msg.text == "Оренда офісу 🏬")
@safe_handler
def find_office_handler(message):
    begin_category_session(bot, message.chat.id, "office")      # ✅
    send_step_photo(
        bot,
        message.chat.id,
        "city.png",
        city_caption("office"),
        reply_markup=build_city_markup("office"),
        parse_mode="Markdown"
    )

    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )



@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("cities_page:"))
@safe_handler
def city_page_handler(call):
    parsed = parse_city_page_callback(call.data)
    if not parsed:
        bot.answer_callback_query(call.id)
        return
    category, page = parsed
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_caption(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            caption=city_caption(category, page),
            reply_markup=build_city_markup(category, page),
            parse_mode="Markdown",
        )
    except Exception:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_city_markup(category, page),
        )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_menu")
@safe_handler
def back_to_main_menu(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Головне меню", reply_markup=get_main_menu())


# === КНОПКА В МЕНЮ ===
@bot.message_handler(func=lambda m: m.text == "Підписка 🔒")
def subscription_handler(message):
    price_text = _price_text()
    caption = (
        f"🔒 <b>Підписка {BRAND_NAME}</b>\n\n"
        f"Вартість: <b>{price_text} грн/місяць</b>\n"
        "Оберіть дію:"
    )
    send_step_photo(
        bot,
        message.chat.id,
        "subscription_buy.png",
        caption,
        parse_mode="HTML",
        reply_markup=get_subscription_actions_keyboard()
    )


# === КЛАВІАТУРА ДІЙ З ПІДПИСКОЮ ===
def get_subscription_actions_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("💳 Оформити підписку", callback_data="subscribe_month"),
        types.InlineKeyboardButton("❌ Скасувати підписку", callback_data="cancel_subscription")
    )
    return kb


@bot.callback_query_handler(func=lambda c: c.data == "subscribe_month")
def subscribe_month(call):
    bot.answer_callback_query(call.id)
    uid = call.from_user.id

    # Тимчасово зберігаємо активне замовлення
    pending_orders[uid] = {"type": "wfp_sub", "ts": int(time.time())}
    order_reference = _build_order_reference(uid)
    payment_orders[order_reference] = uid
    
    # Приховуємо нижнє меню
    bot.send_message(
        call.message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )

    kb = types.InlineKeyboardMarkup(row_width=1)
    price_text = _price_text()
    payment_url = f"{_public_base_url()}/pay/{order_reference}"
    kb.add(
        types.InlineKeyboardButton(f"💳 Оплата {price_text} грн", url=payment_url),
        types.InlineKeyboardButton("🔁 Головне меню", callback_data="back_to_menu")
    )

    bot.send_message(
        call.message.chat.id,
        f"💼 <b>Підписка {BRAND_NAME} — {price_text} грн/місяць</b>\n\n"
        "🔹 <b>У доступі:</b>\n"
        "✅ Оренда квартир, будинків, кімнат та офісів\n"
        "🏗 Купівля квартир і будинків - в розробці\n"
        "✅ Місто, райони, бюджет, площа й додаткові параметри\n"
        "✅ Нові оголошення у зручному форматі карток\n\n"
        "Натисніть кнопку нижче, щоб перейти до оплати. Після успішної оплати доступ активується автоматично.",
        parse_mode="HTML",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data == "cancel_subscription")
def cancel_subscription(call):
    uid = str(call.from_user.id)
    cancel_subscription_renewal(uid)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔁 Головне меню", callback_data="back_to_menu")
    )


    bot.answer_callback_query(call.id)
    send_step_photo(
        bot,
        call.message.chat.id,
        "subscription_cancel.png",
        "❌ <b>Підписку скасовано</b>\n\n"
        "🔓 Доступ до преміум-функцій збережеться до завершення поточного оплаченого періоду.\n"
        "Після завершення місяця бот автоматично забере платний доступ.\n\n"
        "Будемо раді бачити вас знову 💖",
        parse_mode="HTML",
        reply_markup=kb
    )



@bot.callback_query_handler(func=lambda c: c.data in {"offer_txt", "privacy_txt", "offer_docx"})
def send_offer_docs(call):
    bot.answer_callback_query(call.id)
    try:
        if call.data == "offer_txt":
            with open("docs/offer.txt", "rb") as f:
                safe_send(
                    bot, "send_document",
                    call.message.chat.id, f,
                    caption="📄 Договір оферти",
                    visible_file_name="Договір оферти.txt"
                )
        elif call.data == "privacy_txt":
            with open("docs/privacy_policy.txt", "rb") as f:
                safe_send(
                    bot, "send_document",
                    call.message.chat.id, f,
                    caption="📄 Політика конфіденційності",
                    visible_file_name="Політика конфіденційності.txt"
                )
        elif call.data == "offer_docx":
            with open("docs/Договір оферти.docx", "rb") as f:
                safe_send(
                    bot, "send_document",
                    call.message.chat.id, f,
                    caption="📄 Договір оферти",
                    visible_file_name="Договір оферти.docx"
                )
    except FileNotFoundError:
        bot.send_message(call.message.chat.id, "⚠️ Не знайдено файл договору. Перевір, що він є у папці <code>docs/</code>.", parse_mode="HTML")





@bot.message_handler(func=lambda msg: msg.text == "Мої добірки 🔒")
@safe_handler
def saved_flats_handler(message):
    bot.send_message(message.chat.id, "💾 Персональні добірки будуть доступні після активації доступу.")


@bot.message_handler(func=lambda msg: msg.text == f"Партнерство з {BRAND_NAME} 💰")
@safe_handler
def referral_handler(message):
    user_id = str(message.from_user.id)
    referral_link = f"https://t.me/{_bot_username_for_url()}?start={user_id}"

    total_invited = get_ref_count(user_id)
    total_paid = get_paid_count(user_id)
    balance_uah = total_paid * REFERRAL_PAYOUT_UAH

    text = (
        f"<b>💼 Партнерська програма {BRAND_NAME}</b>\n"
        "Діліться сервісом з тими, хто шукає житло, і отримуйте винагороду за оплачені доступи.\n\n"
        f"🔗 Твоє реферальне посилання:\n"
        f"<a href=\"{referral_link}\">{referral_link}</a>\n\n"
        f"👥 Рефералів: {total_invited}\n"
        f"💳 Купили підписку: {total_paid}\n\n"
        f"💰 Баланс: {balance_uah} грн\n"
        f"📊 Зароблено всього: {balance_uah} грн"
    )

    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Як працює реф. програма ❓", callback_data="how_referral_works"))
    markup.add(types.InlineKeyboardButton("Вивести гроші 💰", callback_data="withdraw_money"))
    markup.add(types.InlineKeyboardButton("🔁 Повернутись назад", callback_data="back_to_menu"))

    safe_send(bot, "send_message",
        message.chat.id,
        text,
        reply_markup=markup,
        parse_mode="HTML"
    )


@bot.callback_query_handler(func=lambda call: call.data == "how_referral_works")
def how_referral_works(call):
    bot.answer_callback_query(call.id)

    text = (
        "📌 *Як це працює:*\n"
        "Ви отримуєте індивідуальне реферальне посилання.\n"
        "Ділитеся ним із клієнтами, знайомими чи у власних каналах комунікації.\n"
        f"За кожного користувача, який оформить підписку та скористається сервісом {BRAND_NAME}, "
        f"ви отримуєте орієнтовну винагороду *{REFERRAL_PAYOUT_UAH} грн*.\n\n"
        "📊 Розрахунки проводяться щомісячно.\n"
        "🔎 Відстеження здійснюється автоматично через вашу реферальну лінку.\n\n"
        "Таким чином, ви отримуєте:\n"
        "• прозорий механізм винагороди,\n"
        "• додатковий фінансовий потік,\n"
        f"• довгострокову співпрацю з сервісом {BRAND_NAME}.\n\n"
        "👉 Почніть співпрацю вже сьогодні та заробляйте разом з нами!"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Повернутися назад 🔁", callback_data="referral_back"))

    safe_send(bot, "send_message",
        call.message.chat.id,
        text,
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "withdraw_money")
def withdraw_money(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.from_user.id)

    # баланс рахуємо за платними підписками рефералів
    paid = get_paid_count(user_id)
    balance_uah = paid * REFERRAL_PAYOUT_UAH

    if balance_uah < REFERRAL_PAYOUT_UAH:
        text = f"На жаль, твій баланс — {balance_uah} грн."
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Повернутися назад 🔁", callback_data="referral_back"))
        safe_send(bot, "send_message", chat_id=call.message.chat.id, text=text, reply_markup=markup)
    else:
        bot.send_message(
            call.message.chat.id,
            f"💸 Виведення доступне.\nТвій баланс: {balance_uah} грн.\nНапиши {SUPPORT_USERNAME} для деталей."
        )


@bot.callback_query_handler(func=lambda call: call.data == "referral_back")
def referral_back(call):
    bot.answer_callback_query(call.id)
    user_id = str(call.from_user.id)
    referral_link = f"https://t.me/{_bot_username_for_url()}?start={user_id}"

    invited = get_ref_count(user_id)
    paid = get_paid_count(user_id)
    balance_uah = paid * REFERRAL_PAYOUT_UAH

    text = (
        f"Твоє реферальне посилання:\n{referral_link}\n\n"
        f"Рефералів: {invited}\n"
        f"Купили підписку: {paid}\n\n"
        f"Баланс: {balance_uah} грн\n"
        f"Зароблено всього: {balance_uah} грн"
    )

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Як працює реф. програма ❓", callback_data="how_referral_works"))
    markup.add(types.InlineKeyboardButton("Вивести гроші 💰", callback_data="withdraw_money"))
    markup.add(types.InlineKeyboardButton("🔁 Повернутись назад", callback_data="back_to_menu"))

    safe_send(bot, "send_message", chat_id=call.message.chat.id, text=text, reply_markup=markup)




@bot.message_handler(func=lambda msg: msg.text == "Зв'язок 🛠")
@safe_handler
def support_handler(message):
    bot.send_message(
        message.chat.id,
        f"Команда {BRAND_NAME} на зв'язку: {SUPPORT_USERNAME}"
    )

def get_about_us_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📄 Договір оферти", callback_data="offer_txt"),
        types.InlineKeyboardButton("Політика конфіденційності 📄", callback_data="privacy_txt"),
        types.InlineKeyboardButton("FAQ ❓", url="https://telegra.ph/FAQ-Flat-Find"),
        types.InlineKeyboardButton("🔁 Повернутись назад", callback_data="back_to_menu")
    )
    return kb

@bot.message_handler(func=lambda message: message.text == "Про сервіс ℹ️")
@safe_handler
def handle_about_us(message):
    text = (
        f"🏙️ *{BRAND_NAME}*\n"
        "Це бот для швидкого пошуку нерухомості без комісії та зайвих посередницьких кіл.\n\n"
        "📍 *Що є всередині:*\n"
        "🏡 Оренда квартир, будинків, кімнат та офісів\n"
        "🏗 Купівля квартир і будинків - в розробці\n"
        "🧭 Фільтри за містом, районом, бюджетом і параметрами\n"
        "📌 Добірки актуальних оголошень у зрозумілому форматі\n\n"
        "🔒 *Підписка відкриває:*\n"
        "✅ Повний доступ до добірок після 3 безкоштовних оголошень на місяць\n"
        "✅ Оренду, фільтри та актуальні картки\n"
        "✅ Нові оголошення у зручному форматі без зайвих дзвінків\n\n"
        "Мета проста: менше ручного перегляду, більше релевантних варіантів.\n\n"
        f"📩 Підтримка: {SUPPORT_USERNAME}"
    )
    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )

    safe_send(bot, "send_message",
        message.chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=get_about_us_menu()
    )



def get_buy_realty_menu():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(types.KeyboardButton("🔙 Назад"))
    return keyboard


# ===== Меню "Купівля нерухомості" =====

def get_buy_realty_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("Купівля - в розробці 🏗"))
    kb.add(types.KeyboardButton("🔙 Назад"))
    return kb


def send_buy_in_development(chat_id):
    bot.send_message(
        chat_id,
        "🏗 Купівля житла зараз в розробці.\n\n"
        "Ми готуємо зручний пошук квартир і будинків для купівлі без комісії.\n\n"
        "Поки що можеш скористатися орендою без комісії.",
        reply_markup=get_main_menu()
    )


@bot.message_handler(func=lambda message: message.text in {"Купівля без комісії 🏠", "Купівля - в розробці 🏗"})
@safe_handler
def handle_buy_realty(message):
    send_buy_in_development(message.chat.id)


# ---- купівля квартир та будинків через той самий пошуковий сценарій ----
@bot.message_handler(func=lambda message: message.text == "Купити квартиру 🔑")
@safe_handler
def handle_buy_flat(message):
    send_buy_in_development(message.chat.id)

@bot.message_handler(func=lambda message: message.text == "Купити будинок 🏡")
@safe_handler
def handle_buy_house(message):
    send_buy_in_development(message.chat.id)

@bot.message_handler(func=lambda message: message.text == "Комерційна нерухомість 🏬")
@safe_handler
def handle_buy_commercial(message):
    send_buy_in_development(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "Договори оренди 📄")
def rent_contract_handler(message):
    
    # Приховуємо нижнє меню
    bot.send_message(
        message.chat.id,
        TAGLINE,
        reply_markup=ReplyKeyboardRemove()
    )
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📄 Договір оренди квартири", callback_data="rent_flat_doc"),
        types.InlineKeyboardButton("📄 Договір оренди нежитолового приміщення", callback_data="rent_commercial_doc"),
        types.InlineKeyboardButton("🔁 Назад", callback_data="back_to_menu")
    )
    
    caption ="Ви можете обрати необхідний шаблон договору оренди 📄"
    bot.send_message(
        message.chat.id,
        caption,
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data == "rent_flat_doc")
def rent_flat_doc(call):
    bot.answer_callback_query(call.id)
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔁 Назад", callback_data="back_to_menu")
    )

    try:
        with open("docs/Договір_оренди.docx", "rb") as f:   # 👈 файл у папці docs
            safe_send(
                bot,
                "send_document",
                call.message.chat.id,
                f,
                caption="📄 Договір оренди квартири",
                visible_file_name="Договір оренди квартири.docx",
                reply_markup=kb                
            )
    except FileNotFoundError:
        bot.send_message(call.message.chat.id, "⚠️ Файл договору не знайдено. Переконайся, що він є у папці *docs/*.", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "rent_commercial_doc")
def rent_commercial_doc(call):
    bot.answer_callback_query(call.id)
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔁 Назад", callback_data="back_to_menu")
    )

    try:
        with open("docs/Договір_оренди_приміщення_офісу.docx", "rb") as f:   # 👈 файл у папці docs
            safe_send(
                bot,
                "send_document",
                call.message.chat.id,
                f,
                caption="📄 Договір оренди нежитлового приміщення",
                visible_file_name="Договір оренди нежитлового приміщення.docx",
                reply_markup=kb                
            )
    except FileNotFoundError:
        bot.send_message(call.message.chat.id, "⚠️ Файл договору не знайдено. Переконайся, що він є у папці *docs/*.", parse_mode="Markdown")


@bot.message_handler(func=lambda message: message.text == "🔙 Назад")
@safe_handler
def handle_back(message):
    bot.send_message(
        message.chat.id,
        "Головне меню",
        reply_markup=get_main_menu()
    )



def run_bot():
    start_background_indexer()
    threading.Thread(target=_subscription_expiry_loop, name="subscription-expiry", daemon=True).start()

    # стабільний long-polling у циклі
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=20,                 # connect timeout
                long_polling_timeout=25,    # сервер тримає з’єднання ~25с
                allowed_updates=["message","callback_query"]
            )
        except (requests.exceptions.ReadTimeout,) as e:
            logging.exception("Telegram ReadTimeout — retry через 3с")
            time.sleep(3)
        except (requests.exceptions.ConnectionError,) as e:
            logging.exception("Telegram ConnectionError/DNS — retry через 5с")
            time.sleep(5)
        except KeyboardInterrupt:
            logging.info("Bot stopped by user")
            break
        except Exception as e:
            logging.exception("Unknown polling error — retry через 5с")
            time.sleep(5)


def _subscription_expiry_loop():
    while True:
        try:
            expired = expire_old_subscriptions()
            if expired:
                logging.info("Expired subscriptions disabled: %s", expired)
        except Exception:
            logging.exception("Subscription expiry check failed")
        time.sleep(3600)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    web_app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        threaded=True,
        use_reloader=False,
    )
