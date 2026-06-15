# Flat Find Bot

Telegram-бот для пошуку нерухомості без комісії: оренда квартир, будинків, кімнат, офісів, а також купівля квартир і будинків.

## Що змінити перед запуском

1. Скопіюй `.env.example` у `.env`.
2. Встав `BOT_TOKEN`, `BOT_USERNAME`, `SPREADSHEET_ID`, `SUPPORT_USERNAME`.
3. Поклади Google service account JSON у файл `service_account.json`.
4. Дай сервіс-акаунту доступ Editor до Google Sheets.

## Запуск

```bash
pip install -r requirements.txt
python main.py
```

## WayForPay на Railway

Для автоматичної оплати додай у Railway Variables:

```env
WAYFORPAY_MERCHANT_ACCOUNT=t_me_04f94
WAYFORPAY_SECRET_KEY=...
WAYFORPAY_MERCHANT_PASSWORD=...
WAYFORPAY_CURRENCY=UAH
WAYFORPAY_PRODUCT_NAME=Підписка Flat Find на 1 місяць
WAYFORPAY_PRODUCT_PRICE=439.99
WAYFORPAY_RETURN_URL=https://t.me/flatfind_estate_bot
WAYFORPAY_SERVICE_URL=https://flat-find-bot-production.up.railway.app/wayforpay/callback
WAYFORPAY_RECURRING_ENABLED=1
WAYFORPAY_REGULAR_MODE=monthly
```

У Railway Public Networking використовуй порт `8080`.

Після успішного платежу доступ активується на 30 днів. Повторний успішний callback від WayForPay продовжує доступ ще на 30 днів. Кнопка `Скасувати підписку` не забирає доступ одразу: бот залишає його до кінця вже оплаченого періоду.

## Стартова картка Telegram

Код автоматично виставляє опис бота через Telegram API. Після натискання `Розпочати` бот надсилає тільки текст і меню, без великого фото.

Щоб велике фото `Flat Find` було видно саме до натискання кнопки `Розпочати`, його потрібно поставити через `@BotFather`. Кодом це зробити не можна, бо до натискання `Розпочати` бот ще не може надсилати повідомлення.

Спробуй через меню BotFather:

```text
/mybots
```

Далі:

```text
Flat Find bot -> Edit Bot -> Edit Description Picture / Edit Botpic
```

Після вибору пункту завантаж файл:

```text
media/intro.png
```

Якщо в BotFather немає пункту `Edit Description Picture`, тоді Telegram для цього бота дозволяє поставити тільки аватарку через `Edit Botpic` або `/setuserpic`. Велику картинку в стартовій картці Telegram у такому випадку не дає додати з коду.

## Нові клієнтські сценарії

- `Оренда без комісії` - квартири, будинки, офіси, кімнати.
- `Купівля без комісії` - квартири й будинки через окремі категорії пошуку.
- `Підписка` - оформлення або скасування підписки, оплата створюється через WayForPay.
- `Про сервіс` - опис доступу, локальні файли оферти й політики, перевірка підпису та FAQ.
- `Партнерство` - реферальне посилання та статистика з Google Sheets.

Файли `.env` і `service_account.json` навмисно не додані в проєкт, щоб не переносити секрети.
