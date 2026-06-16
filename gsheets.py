from __future__ import annotations

# gsheets.py
import json
import os, time
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv  # ⬅️ додали
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
# ✅ Безпечне підключення форматування (автоматичне форматування колонок)
try:
    from gspread_formatting import set_number_format, NumberFormat
    _fmt_available = True
except Exception:
    _fmt_available = False
from gspread.utils import rowcol_to_a1 as _a1, rowcol_to_a1

# 1) гарантовано підтягуємо .env навіть при імпорті модуля
load_dotenv()

# 2) читаємо ENV
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID") or os.getenv("GOOGLE_SHEETS_ID")
USERS_WS = os.getenv("USERS_SHEET", "users")
PAYOUTS_WS = os.getenv("PAYOUTS_SHEET", "payouts")
REF_WS = os.getenv("REF_STATS_SHEET", "ref_stats")
CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
CREDS_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("SERVICE_ACCOUNT_JSON")

# 3) перевіряємо наявність критичних значень
if not SPREADSHEET_ID:
    raise RuntimeError("SPREADSHEET_ID/GOOGLE_SHEETS_ID не заданий. Перевір .env")

if not CREDS_JSON and not os.path.exists(CREDS_PATH):
    raise RuntimeError(f"Не знайдено файл ключа сервіс-акаунта: {CREDS_PATH}")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

if CREDS_JSON:
    creds = Credentials.from_service_account_info(json.loads(CREDS_JSON), scopes=SCOPES)
else:
    creds = Credentials.from_service_account_file(CREDS_PATH, scopes=SCOPES)
gc = gspread.authorize(creds)


def _ws(name):
    return sh.worksheet(name)

def _apply_datetime_format():
    """
    Застосовує формат dd.mm.yyyy hh:mm до колонок F,H,I без gspread-formatting,
    через native batchUpdate (userEnteredFormat.numberFormat).
    """
    ws = _ws(USERS_WS)
    sheet_id = ws._properties['sheetId']

    # Діапазони F2:F, H2:H, I2:I у індексах 0-based (F=5, H=7, I=8)
    def col_idx(letter: str) -> int:
        return ord(letter.upper()) - ord('A')

    ranges = [
        (col_idx('F'), col_idx('F')+1),
        (col_idx('H'), col_idx('H')+1),
        (col_idx('I'), col_idx('I')+1),
    ]

    requests = []
    for start_col, end_col in ranges:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,            # з рядка 2 (0-based)
                    "startColumnIndex": start_col,
                    "endColumnIndex": end_col
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {
                            "type": "DATE_TIME",
                            "pattern": "dd.mm.yyyy hh:mm"
                        }
                    }
                },
                "fields": "userEnteredFormat.numberFormat"
            }
        })

    ws.spreadsheet.batch_update({"requests": requests})



# 4) пробуємо відкрити таблицю або даємо підказку як шарити доступ
try:
    sh = gc.open_by_key(SPREADSHEET_ID)
    _apply_datetime_format()
except gspread.SpreadsheetNotFound as e:
    sa_email = getattr(creds, "_service_account_email", None) or creds.service_account_email
    raise RuntimeError(
        "Google Sheet не знайдено або немає доступу (404).\n"
        f"- Перевір, що ID в .env: SPREADSHEET_ID={SPREADSHEET_ID}\n"
        f"- У Google Sheets натисни Share і додай '{sa_email}' з правами Editor."
    ) from e

def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().upper() in ("TRUE", "1", "YES", "Y", "T")


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default

LOCAL_TZ_NAME = os.getenv("TZ", "Europe/Kyiv")

def _resolve_tz():
    """
    Повертає тайзону:
    - TZ з .env (наприклад Europe/Kyiv)
    - або legacy Europe/Kiev
    - або фіксований офсет +03:00 (керується TZ_OFFSET_HOURS)
    """
    # спроба основної тайзони
    if ZoneInfo:
        try:
            return ZoneInfo(LOCAL_TZ_NAME)
        except Exception:
            pass
        # спроба legacy-аліаса
        for cand in ("Europe/Kiev",):
            try:
                return ZoneInfo(cand)
            except Exception:
                pass
    # запасний варіант — фіксований офсет
    offset_hours = int(os.getenv("TZ_OFFSET_HOURS", "3"))
    return timezone(timedelta(hours=offset_hours))

_LOCAL_TZ = _resolve_tz()

def _now_local_dt():
    # завжди aware-datetime у локальній тайзоні
    return datetime.now(_LOCAL_TZ)

def _now_serial_for_sheets() -> float:
    """
    Серіальний формат Google Sheets (дні з 1899-12-30) у ЛОКАЛЬНОМУ часі без секунд.
    """
    dt_local = _now_local_dt()          # aware
    dt_local_naive = dt_local.replace(tzinfo=None)  # робимо naive локальний
    epoch_naive = datetime(1899, 12, 30)
    return (dt_local_naive - epoch_naive).total_seconds() / 86400.0


def _subscription_days(period_text: str = "1m") -> int:
    period = str(period_text or "1m").strip().lower()
    if period in {"1m", "month", "monthly", "місяць", "1 місяць"}:
        return 30
    if period.endswith("d") and period[:-1].isdigit():
        return int(period[:-1])
    return 30

def _set_number_a1(ws, a1: str, value: float) -> None:
    """
    Жорстко ставить у клітинку число (userEnteredValue.numberValue), минаючи локалі.
    Працює стабільніше, ніж update()/update_acell із USER_ENTERED.
    """
    # обчислимо позиції (нульові індекси) з A1
    import re
    m = re.match(r"^([A-Za-z]+)(\d+)$", a1)
    if not m:
        raise ValueError(f"Bad A1: {a1}")
    col_letters, row_str = m.groups()
    row_idx = int(row_str) - 1  # 0-based
    col_idx = 0
    for ch in col_letters.upper():
        col_idx = col_idx * 26 + (ord(ch) - ord('A') + 1)
    col_idx -= 1  # 0-based

    sheet_id = ws._properties['sheetId']
    req = {
        "requests": [{
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_idx,
                    "endRowIndex": row_idx + 1,
                    "startColumnIndex": col_idx,
                    "endColumnIndex": col_idx + 1,
                },
                "rows": [{
                    "values": [{
                        "userEnteredValue": {"numberValue": float(value)}
                    }]
                }],
                "fields": "userEnteredValue"
            }
        }]
    }
    ws.spreadsheet.batch_update(req)

def _find_row_by_user_id(ws, user_id: str) -> int | None:
    """
    Повертає номер рядка (2+) для заданого user_id з колонки B.
    Якщо не знайдено — None.
    """
    col_b = ws.col_values(2)  # B1 — заголовок, далі значення
    uid = str(user_id).strip()
    for idx, val in enumerate(col_b[1:], start=2):  # починаємо з рядка 2
        if str(val).strip() == uid:
            return idx
    return None


# ================= USERS =================
# users: № | id | username | ref_code | ref_from | registered_at |
#         subscribed | subscribed_at | sub_expires_at | sub_period | sub_price | payment_id
def ensure_user(user_id: str, username: str = "") -> None:
    ws = _ws(USERS_WS)
    ids = ws.col_values(2)[1:]  # column B = id
    if user_id not in ids:
        next_row = len(ids) + 2
        now_serial = _now_serial_for_sheets()

        # 1) спершу ставимо весь ряд (RAW, щоб не чіпати локалі)
        ws.update(
            f"A{next_row}:L{next_row}",
            [[next_row - 1, user_id, username, user_id, "", now_serial, "FALSE", "", "", "", "", ""]],
            value_input_option="RAW"
        )

        # 2) поверх — жорстко кладемо саме В F (registered_at) число як numberValue
        #    (це прибере будь-які «залипання» тексту, коми тощо)
        _set_number_a1(ws, f"F{next_row}", now_serial)


    else:
        # оновимо username, якщо змінився
        data = ws.get_all_records()
        for i, row in enumerate(data, start=2):
            if str(row["id"]) == str(user_id) and row.get("username") != username:
                ws.update_acell(f"C{i}", username)
                break

def get_col_index(ws, header_name: str) -> int:
    """Повертає 1-based індекс колонки за назвою у шапці (рядок 1)."""
    headers = ws.row_values(1)
    for idx, name in enumerate(headers, start=1):
        if str(name).strip().lower() == header_name.strip().lower():
            return idx
    raise ValueError(f"Column '{header_name}' not found in header row")

def a1(row: int, col: int) -> str:
    return rowcol_to_a1(row, col)


def get_sub_info(user_id: str) -> str | None:
    """Повертає активність підписки з урахуванням дати завершення в колонці I."""
    ws = _ws(USERS_WS)
    data = ws.get_all_records()
    for i, row in enumerate(data, start=2):
        if str(row["id"]) == str(user_id):
            subscribed = row.get("subscribed", "")
            if not _to_bool(subscribed):
                return "FALSE"

            expires_at = _to_float(row.get("sub_expires_at") or row.get("unsubscribed_at") or "")
            if not expires_at:
                raw_expires = ws.get(f"I{i}")
                expires_at = _to_float(raw_expires[0][0] if raw_expires and raw_expires[0] else "")
            if expires_at and expires_at < _now_serial_for_sheets():
                ws.update_acell(f"G{i}", "FALSE")
                return "FALSE"

            return "TRUE"
    return None


def _ensure_user_column(ws, header_name: str) -> int:
    """Return 1-based column index, creating the column if it is missing."""
    headers = ws.row_values(1)
    normalized = header_name.strip().lower()
    for idx, name in enumerate(headers, start=1):
        if str(name).strip().lower() == normalized:
            return idx

    col_idx = len(headers) + 1
    ws.update_cell(1, col_idx, header_name)
    return col_idx


def _current_month_key() -> str:
    return _now_local_dt().strftime("%Y-%m")


def payment_id_already_processed(payment_id: str) -> bool:
    """Checks whether this WayForPay transaction has already activated access."""
    payment_id = str(payment_id or "").strip()
    if not payment_id:
        return False
    ws = _ws(USERS_WS)
    for value in ws.col_values(12)[1:]:  # L = payment_id
        if str(value).strip() == payment_id:
            return True
    return False


def expire_old_subscriptions() -> int:
    """Turns off subscriptions whose sub_expires_at date has already passed."""
    ws = _ws(USERS_WS)
    now_serial = _now_serial_for_sheets()
    rows = ws.get_all_records()
    expired_rows: list[int] = []
    for row_idx, row in enumerate(rows, start=2):
        if not _to_bool(row.get("subscribed", "")):
            continue
        expires_at = _to_float(row.get("sub_expires_at") or row.get("unsubscribed_at") or "")
        if not expires_at:
            raw_expires = ws.get(f"I{row_idx}")
            expires_at = _to_float(raw_expires[0][0] if raw_expires and raw_expires[0] else "")
        if expires_at and expires_at < now_serial:
            expired_rows.append(row_idx)

    if not expired_rows:
        return 0

    ws.spreadsheet.values_batch_update({
        "valueInputOption": "RAW",
        "data": [{"range": f"G{row_idx}", "values": [["FALSE"]]} for row_idx in expired_rows],
    })
    return len(expired_rows)


def set_ref_from(user_id: str, referrer_id: str) -> None:
    if user_id == referrer_id:
        return
    ws = _ws(USERS_WS)
    data = ws.get_all_records()
    for i, row in enumerate(data, start=2):
        if str(row["id"]) == str(user_id):
            if not str(row.get("ref_from", "")).strip():
                ws.update_acell(f"E{i}", referrer_id)  # col E = ref_from
            break

def set_subscription(user_id: str, active: bool, price_uah: int = 0,
                     period_text: str = "1m", payment_id: str = "") -> None:
    ws = _ws(USERS_WS)

    row_idx = _find_row_by_user_id(ws, str(user_id))
    if not row_idx:
        return  # або ensure_user(...) і повторити

    # G..L = 7..12
    cG, cH, cI, cJ, cK, cL = 7, 8, 9, 10, 11, 12
    now_serial = _now_serial_for_sheets()
    raw_expires = ws.get(f"I{row_idx}")
    current_expires = _to_float(raw_expires[0][0] if raw_expires and raw_expires[0] else "")
    starts_from = max(now_serial, current_expires)
    expires_serial = starts_from + _subscription_days(period_text)

    if active:
        rng = f"{_a1(row_idx, cG)}:{_a1(row_idx, cL)}"
        ws.spreadsheet.values_batch_update({
            "valueInputOption": "RAW",
            "data": [{
                "range": rng,
                "values": [["TRUE", now_serial, expires_serial, period_text, price_uah, payment_id]]
            }]
        })
        # гарантуємо, що H — саме numberValue
        _set_number_a1(ws, _a1(row_idx, cH), now_serial)
        _set_number_a1(ws, _a1(row_idx, cI), expires_serial)
        ws.update_acell(f"M{row_idx}", "FALSE")  # sub_cancelled
    else:
        ws.spreadsheet.values_batch_update({
            "valueInputOption": "RAW",
            "data": [
                {"range": _a1(row_idx, cG), "values": [["FALSE"]]},
                {"range": f"{_a1(row_idx, cJ)}:{_a1(row_idx, cL)}",
                 "values": [[period_text, price_uah, payment_id]]}
            ]
        })
        # гарантуємо, що I — саме numberValue
        _set_number_a1(ws, _a1(row_idx, cI), now_serial)


def cancel_subscription_renewal(user_id: str) -> None:
    """Скасовує автопродовження, але не забирає доступ до кінця оплаченого періоду."""
    ws = _ws(USERS_WS)
    row_idx = _find_row_by_user_id(ws, str(user_id))
    if not row_idx:
        return
    ws.update_acell(f"M{row_idx}", "TRUE")  # sub_cancelled


def get_free_view_usage(user_id: str, monthly_limit: int = 3) -> dict:
    """Returns monthly free listing view usage for a non-subscribed user."""
    ws = _ws(USERS_WS)
    row_idx = _find_row_by_user_id(ws, str(user_id))
    if not row_idx:
        ensure_user(str(user_id), "")
        row_idx = _find_row_by_user_id(ws, str(user_id))
    if not row_idx:
        return {"month": _current_month_key(), "used": monthly_limit, "remaining": 0, "keys": []}

    month_col = _ensure_user_column(ws, "free_views_month")
    count_col = _ensure_user_column(ws, "free_views_count")
    keys_col = _ensure_user_column(ws, "free_viewed_keys")

    month_key = _current_month_key()
    row = ws.row_values(row_idx)

    def _cell(col: int) -> str:
        return str(row[col - 1]).strip() if len(row) >= col else ""

    stored_month = _cell(month_col)
    if stored_month != month_key:
        return {"month": month_key, "used": 0, "remaining": monthly_limit, "keys": []}

    try:
        used = int(float(_cell(count_col) or 0))
    except (TypeError, ValueError):
        used = 0
    try:
        keys = json.loads(_cell(keys_col) or "[]")
        if not isinstance(keys, list):
            keys = []
    except Exception:
        keys = []

    used = max(0, min(used, monthly_limit))
    return {
        "month": month_key,
        "used": used,
        "remaining": max(monthly_limit - used, 0),
        "keys": [str(k) for k in keys if str(k).strip()],
    }


def register_free_listing_view(user_id: str, listing_key: str, monthly_limit: int = 3) -> dict:
    """Consumes one free monthly listing view for a new listing key.

    Re-opening a listing already counted in the current month stays allowed and
    does not consume an additional free view.
    """
    ws = _ws(USERS_WS)
    row_idx = _find_row_by_user_id(ws, str(user_id))
    if not row_idx:
        ensure_user(str(user_id), "")
        row_idx = _find_row_by_user_id(ws, str(user_id))
    if not row_idx:
        return {"allowed": False, "used": monthly_limit, "remaining": 0, "already_seen": False}

    month_col = _ensure_user_column(ws, "free_views_month")
    count_col = _ensure_user_column(ws, "free_views_count")
    keys_col = _ensure_user_column(ws, "free_viewed_keys")

    usage = get_free_view_usage(str(user_id), monthly_limit=monthly_limit)
    key = str(listing_key or "").strip()
    if not key:
        key = f"listing:{int(time.time() * 1000)}"

    keys = usage["keys"]
    if key in keys:
        return {
            "allowed": True,
            "used": usage["used"],
            "remaining": usage["remaining"],
            "already_seen": True,
        }

    if usage["used"] >= monthly_limit:
        return {"allowed": False, "used": usage["used"], "remaining": 0, "already_seen": False}

    keys.append(key)
    used = min(usage["used"] + 1, monthly_limit)
    ws.spreadsheet.values_batch_update({
        "valueInputOption": "RAW",
        "data": [
            {"range": _a1(row_idx, month_col), "values": [[usage["month"]]]},
            {"range": _a1(row_idx, count_col), "values": [[str(used)]]},
            {"range": _a1(row_idx, keys_col), "values": [[json.dumps(keys[:monthly_limit], ensure_ascii=False)]]},
        ],
    })
    return {
        "allowed": True,
        "used": used,
        "remaining": max(monthly_limit - used, 0),
        "already_seen": False,
    }







# ================= REF_STATS =================
# ref_stats: ref_code | paid_count | revenue_sum | payout_rate | payout_due | payout_paid | payout_left
def upsert_ref_stats(ref_code: str, payout_rate: float = 10.0, default_price: float = 50.0) -> None:
    users_ws = _ws(USERS_WS)
    ref_ws = _ws(REF_WS)

    users = users_ws.get_all_records()
    invited = [u for u in users if str(u.get("ref_from", "")).strip() == str(ref_code)]
    paid_count = sum(1 for u in invited if _to_bool(u.get("subscribed", "")))
    revenue_sum = paid_count * float(default_price)
    payout_rate = float(payout_rate)
    payout_due = round(revenue_sum * payout_rate / 100.0, 2)

    rows = ref_ws.get_all_records()
    row_num = None
    for i, r in enumerate(rows, start=2):
        if str(r.get("ref_code", "")).strip() == str(ref_code):
            row_num = i
            break

    # якщо вже є payout_paid — збережемо його й перерахуємо payout_left
    payout_paid = 0.0
    if row_num:
        cur = rows[row_num - 2]
        try:
            payout_paid = float(cur.get("payout_paid", 0) or 0)
        except ValueError:
            payout_paid = 0.0

    payout_left = max(round(payout_due - payout_paid, 2), 0.0)

    values = [ref_code, paid_count, revenue_sum, payout_rate, payout_due, payout_paid, payout_left]
    if row_num:
        ref_ws.update(f"A{row_num}:G{row_num}", [values])
    else:
        ref_ws.append_row(values)

def get_ref_count(ref_code: str) -> int:
    ws = _ws(USERS_WS)
    users = ws.get_all_records()
    return sum(1 for u in users if str(u.get("ref_from", "")).strip() == str(ref_code))

def get_paid_count(ref_code: str) -> int:
    ws = _ws(USERS_WS)
    users = ws.get_all_records()
    invited = [u for u in users if str(u.get("ref_from", "")).strip() == str(ref_code)]
    return sum(1 for u in invited if _to_bool(u.get("subscribed", "")))

def get_ref_summary(ref_code: str) -> dict:
    ws = _ws(REF_WS)
    rows = ws.get_all_records()
    for r in rows:
        if str(r.get("ref_code", "")).strip() == str(ref_code):
            def f(x, default=0.0):
                try: return float(x)
                except: return default
            return {
                "paid_count": int(r.get("paid_count", 0) or 0),
                "revenue_sum": f(r.get("revenue_sum", 0)),
                "payout_rate": f(r.get("payout_rate", 10)),
                "payout_due": f(r.get("payout_due", 0)),
                "payout_paid": f(r.get("payout_paid", 0)),
                "payout_left": f(r.get("payout_left", 0)),
            }
    return {"paid_count": 0, "revenue_sum": 0.0, "payout_rate": 10.0, "payout_due": 0.0, "payout_paid": 0.0, "payout_left": 0.0}

# ================= PAYOUTS (опційно, якщо треба лог виплат) =================
# payouts: ref_code | invited_count | amount | paid | paid_at
def log_payout(ref_code: str, invited_count: int, amount: float, paid: bool, paid_at_ts: int | None = None) -> None:
    ws = _ws(PAYOUTS_WS)
    ws.append_row([
        ref_code, invited_count, float(amount), "TRUE" if paid else "FALSE",
        paid_at_ts if paid_at_ts else ""
    ])

def mark_payout_paid(ref_code: str, amount_paid: float) -> None:
    """Збільшити payout_paid у ref_stats на amount_paid та перерахувати payout_left."""
    ref_ws = _ws(REF_WS)
    rows = ref_ws.get_all_records()
    row_num = None
    for i, r in enumerate(rows, start=2):
        if str(r.get("ref_code", "")).strip() == str(ref_code):
            row_num = i
            cur_paid = 0.0
            try: cur_paid = float(r.get("payout_paid", 0) or 0)
            except: pass
            new_paid = round(cur_paid + float(amount_paid), 2)
            try: due = float(r.get("payout_due", 0) or 0)
            except: due = 0.0
            left = max(round(due - new_paid, 2), 0.0)
            ref_ws.update(f"F{row_num}:G{row_num}", [[new_paid, left]])
            break
