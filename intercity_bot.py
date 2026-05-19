import telebot
from telebot import types
import math
import sqlite3
import threading
import time
import os
import json
from datetime import datetime, timedelta, timezone
import requests
import re

# ═══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ — все ключи берутся из переменных окружения
# ═══════════════════════════════════════════════════════════════

BOT_TOKEN           = os.getenv("BOT_TOKEN")
YANDEX_GEOCODER_KEY = os.getenv("YANDEX_GEOCODER_KEY")
GROUP_CHAT_ID       = int(os.getenv("GROUP_CHAT_ID", "0"))
ADMIN_IDS           = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
BOT_USERNAME        = os.getenv("BOT_USERNAME", "intercitytrans_bot")
PAYMENT_DETAILS     = os.getenv("PAYMENT_DETAILS", "Для оплаты абонемента свяжитесь с администратором @Olegan7979")
DB_PATH             = os.getenv("DB_PATH", "intercity_bot.db")

# Проверка, что критически важные переменные заданы
if not BOT_TOKEN:
    raise ValueError("❌ Не задана переменная окружения BOT_TOKEN!")
if not YANDEX_GEOCODER_KEY:
    print("⚠️ YANDEX_GEOCODER_KEY не задан — расчёт расстояний не будет работать")

# ═══════════════════════════════════════════════════════════════
#  ЧАСОВОЙ ПОЯС
# ═══════════════════════════════════════════════════════════════
GEO_TZ = timezone(timedelta(hours=4))

# ═══════════════════════════════════════════════════════════════
#  ТАРИФЫ  ₽/км
# ═══════════════════════════════════════════════════════════════
TARIFFS_RF = {
    "econom":    {"label": "⚡️ Эконом",               "price": 25},
    "standard":  {"label": "🚗 Стандарт",              "price": 27},
    "comfort":   {"label": "🚙 Комфорт",               "price": 31},
    "comfort+":  {"label": "✨ Комфорт+",              "price": 35},
    "universal": {"label": "🚐 Универсал / Компактвэн", "price": 40},
    "minivan":   {"label": "🚌 Минивэн",               "price": 45},
    "business":  {"label": "💼 Бизнес",                "price": 60},
}
TARIFFS_NT = {
    "econom":    {"label": "⚡️ Эконом",    "price": 70},
    "standard":  {"label": "🚗 Стандарт",  "price": 72},
    "comfort":   {"label": "🚙 Комфорт",   "price": 80},
    "comfort+":  {"label": "✨ Комфорт+",  "price": 90},
    "universal": {"label": "🚐 Компактвэн","price": 95},
    "minivan":   {"label": "🚌 Минивэн",   "price": 100},
    "business":  {"label": "💼 Бизнес",    "price": 120},
}
NT_KEYWORDS = ["лнр","днр","луганск","донецк","крым","симферополь",
               "севастополь","херсон","запорожье","мариуполь","мелитополь"]

SUBSCRIPTION_PLANS = {
    "60":  {"days": 60,  "price": 650,  "label": "60 дней — 650 ₽"},
    "120": {"days": 120, "price": 1100, "label": "120 дней — 1 100 ₽"},
    "240": {"days": 240, "price": 2000, "label": "240 дней — 2 000 ₽"},
    "365": {"days": 365, "price": 3500, "label": "1 год — 3 500 ₽"},
}

CLASS_DESCRIPTIONS = {
    "econom":    "авто 2008–2015 г., без кондиц. требований",
    "standard":  "авто от 2015 г., базовый комфорт",
    "comfort":   "авто от 2017 г., хорошее состояние, кондиционер",
    "comfort+":  "авто от 2019 г., отличное состояние, кожа/климат",
    "universal": "универсал или компактвэн, увеличенный багажник",
    "minivan":   "7–8 мест, большой багаж, группы",
    "business":  "премиум авто, представительский класс",
}

ALLOWED_ORDER_COLUMNS = {
    "status", "driver_id", "taken_at", "completed_at",
    "distance_km", "price"
}

print("🚕 Запуск Межгород Трансфер Россия v3.3 (SQLite FSM) ...")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            user_id         INTEGER PRIMARY KEY,
            name            TEXT,
            car_model       TEXT,
            car_year        INTEGER,
            car_number      TEXT,
            car_class       TEXT,
            car_class_label TEXT,
            phone           TEXT,
            username        TEXT,
            doc_lic_front   TEXT,
            doc_lic_back    TEXT,
            doc_sts_front   TEXT,
            doc_sts_back    TEXT,
            doc_car         TEXT,
            docs_verified   INTEGER DEFAULT 0,
            registered_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id      INTEGER PRIMARY KEY,
            expires_date TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            passenger_id INTEGER NOT NULL,
            from_city    TEXT,
            to_city      TEXT,
            trip_date    TEXT,
            trip_time    TEXT,
            passengers   INTEGER,
            car_class    TEXT,
            wishes       TEXT,
            distance_km  REAL,
            price        INTEGER,
            status       TEXT DEFAULT 'pending',
            created_at   TEXT,
            driver_id    INTEGER,
            taken_at     TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            user_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS pending_subscriptions (
            user_id  INTEGER PRIMARY KEY,
            plan_key TEXT
        );

        CREATE TABLE IF NOT EXISTS subscription_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            plan_key   TEXT,
            admin_id   INTEGER,
            action     TEXT,
            created_at TEXT
        );
        
        CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY,
            role    TEXT,
            step    TEXT,
            data    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_sub_expires ON subscriptions(expires_date);
        CREATE INDEX IF NOT EXISTS idx_orders_passenger ON orders(passenger_id);
        CREATE INDEX IF NOT EXISTS idx_orders_driver    ON orders(driver_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);
    """)
    conn.commit()
    conn.close()
    print("✅ База данных готова")


# ═══════════════════════════════════════════════════════════════
#  СЛОЙ ДАННЫХ — СЕССИИ (FSM)
# ═══════════════════════════════════════════════════════════════

def db_get_session(uid: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    if row:
        return {"role": row["role"], "step": row["step"], "data": json.loads(row["data"] or "{}")}
    return {"role": None, "step": None, "data": {}}

def db_update_session(uid: int, role=None, step=None, data=None):
    curr = db_get_session(uid)
    n_role = role if role is not None else curr["role"]
    if step == "":
        n_step = None
    else:
        n_step = step if step is not None else curr["step"]
    n_data = data if data is not None else curr["data"]
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO sessions (user_id, role, step, data) VALUES (?,?,?,?)",
                 (uid, n_role, n_step, json.dumps(n_data)))
    conn.commit()
    conn.close()

def db_clear_session(uid: int):
    curr = db_get_session(uid)
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO sessions (user_id, role, step, data) VALUES (?,?,?,?)",
                 (uid, curr["role"], None, "{}"))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════
#  СЛОЙ ДАННЫХ — ВОДИТЕЛИ, ПОДПИСКИ, ЗАКАЗЫ, ПРОЧЕЕ
# ═══════════════════════════════════════════════════════════════

def db_get_driver(uid: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM drivers WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_save_driver(uid: int, data: dict):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO drivers
        (user_id, name, car_model, car_year, car_number, car_class, car_class_label,
         phone, username, doc_lic_front, doc_lic_back, doc_sts_front, doc_sts_back,
         doc_car, docs_verified, registered_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        uid, data.get("name"), data.get("car_model"), data.get("car_year"),
        data.get("car_number"), data.get("car_class"), data.get("car_class_label"),
        data.get("phone"), data.get("username"),
        data.get("doc_lic_front"), data.get("doc_lic_back"),
        data.get("doc_sts_front"), data.get("doc_sts_back"),
        data.get("doc_car"),
        1 if data.get("docs_verified") else 0,
        data.get("registered_at", datetime.now(GEO_TZ).isoformat())
    ))
    conn.commit()
    conn.close()

def db_verify_driver(uid: int, verified: bool):
    conn = get_db()
    conn.execute("UPDATE drivers SET docs_verified=? WHERE user_id=?",
                 (1 if verified else 0, uid))
    conn.commit()
    conn.close()

def db_all_drivers() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM drivers").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_subscription(uid: int) -> str | None:
    conn = get_db()
    row = conn.execute("SELECT expires_date FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row["expires_date"] if row else None

def db_set_subscription(uid: int, expires: str, admin_id: int = None, plan_key: str = None):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO subscriptions (user_id, expires_date) VALUES (?,?)",
                 (uid, expires))
    if admin_id and plan_key:
        conn.execute(
            "INSERT INTO subscription_log (user_id, plan_key, admin_id, action, created_at) VALUES (?,?,?,?,?)",
            (uid, plan_key, admin_id, "activate", datetime.now(GEO_TZ).isoformat())
        )
    conn.commit()
    conn.close()

def is_subscribed(uid: int) -> bool:
    exp = db_get_subscription(uid)
    if not exp:
        return False
    try:
        return datetime.strptime(exp, "%Y-%m-%d").date() >= datetime.now(GEO_TZ).date()
    except Exception:
        return False

def days_left(uid: int) -> int:
    exp = db_get_subscription(uid)
    if not exp:
        return 0
    try:
        return max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.now(GEO_TZ).date()).days)
    except Exception:
        return 0

def db_create_order(order: dict) -> int:
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO orders
        (passenger_id, from_city, to_city, trip_date, trip_time, passengers,
         car_class, wishes, distance_km, price, status, created_at, driver_id, taken_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        order["passenger_id"], order["from_city"], order["to_city"],
        order["trip_date"], order["trip_time"], order["passengers"],
        order["car_class"], order.get("wishes",""),
        order.get("distance_km"), order.get("price"),
        "pending", datetime.now(GEO_TZ).isoformat(), None, None
    ))
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    return oid

def db_get_order(oid: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_update_order(oid: int, **kwargs):
    if not kwargs:
        return
    for key in kwargs:
        if key not in ALLOWED_ORDER_COLUMNS:
            raise ValueError(f"❌ Попытка обновить недопустимую колонку: {key}")
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [oid]
    conn = get_db()
    conn.execute(f"UPDATE orders SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()

def db_passenger_orders(uid: int, limit: int = 5) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE passenger_id=? ORDER BY created_at DESC LIMIT ?",
        (uid, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_driver_orders(uid: int, limit: int = 5) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE driver_id=? ORDER BY taken_at DESC LIMIT ?",
        (uid, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_open_orders_for_class(car_class: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status='open' AND car_class=? ORDER BY created_at DESC",
        (car_class,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_all_orders(limit: int = 10) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_stats() -> dict:
    conn = get_db()
    total  = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    open_c = conn.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]
    done_c = conn.execute("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0]
    drv_c  = conn.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]
    docs_c = conn.execute("SELECT COUNT(*) FROM drivers WHERE docs_verified=1").fetchone()[0]
    conn.close()
    sub_c = sum(1 for d in db_all_drivers() if is_subscribed(d["user_id"]))
    return {"total": total, "open": open_c, "done": done_c,
            "drivers": drv_c, "docs_ok": docs_c, "subscribed": sub_c}

def db_in_blacklist(uid: int) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM blacklist WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row is not None

def db_add_blacklist(uid: int):
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO blacklist (user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()

def db_remove_blacklist(uid: int):
    conn = get_db()
    conn.execute("DELETE FROM blacklist WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def db_all_blacklist() -> list[int]:
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM blacklist").fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_set_pending(uid: int, plan_key: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO pending_subscriptions (user_id, plan_key) VALUES (?,?)",
                 (uid, plan_key))
    conn.commit()
    conn.close()

def db_get_pending(uid: int) -> str | None:
    conn = get_db()
    row = conn.execute("SELECT plan_key FROM pending_subscriptions WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row["plan_key"] if row else None

def db_del_pending(uid: int, admin_id: int = None, plan_key: str = None):
    conn = get_db()
    conn.execute("DELETE FROM pending_subscriptions WHERE user_id=?", (uid,))
    if admin_id and plan_key:
        conn.execute(
            "INSERT INTO subscription_log (user_id, plan_key, admin_id, action, created_at) VALUES (?,?,?,?,?)",
            (uid, plan_key, admin_id, "reject", datetime.now(GEO_TZ).isoformat())
        )
    conn.commit()
    conn.close()

def db_all_pending() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM pending_subscriptions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════

def is_nt(city: str) -> bool:
    return any(kw in city.lower() for kw in NT_KEYWORDS)

def tariffs(city_from="", city_to=""):
    return TARIFFS_NT if (is_nt(city_from) or is_nt(city_to)) else TARIFFS_RF

def calc_price(dist_km, car_class, city_from, city_to):
    t = tariffs(city_from, city_to)
    rate = t.get(car_class, t["standard"])["price"]
    return round(dist_km * rate)

def is_valid_city(city: str) -> bool:
    if not city or len(city.strip()) < 2:
        return False
    return bool(re.match(r"^[а-яА-ЯёЁa-zA-Z\s\-\.]+$", city.strip()))

def geocode(city: str):
    if not YANDEX_GEOCODER_KEY:
        return None
    if not is_valid_city(city):
        return None
    try:
        r = requests.get("https://geocode-maps.yandex.ru/1.x/", params={
            "apikey": YANDEX_GEOCODER_KEY, "geocode": city,
            "format": "json", "lang": "ru_RU", "results": 1,
            "sco": "latlong"
        }, timeout=5)
        r.raise_for_status()
        fm = r.json()["response"]["GeoObjectCollection"]["featureMember"]
        if fm:
            lon, lat = map(float, fm[0]["GeoObject"]["Point"]["pos"].split())
            return lat, lon
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Ошибка сети Я.Геокодер: {e}")
    except Exception as e:
        print(f"⚠️ Геокодер ошибка парсинга: {e}")
    return None

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def get_distance(city_from, city_to):
    c1 = geocode(city_from)
    c2 = geocode(city_to)
    if c1 and c2:
        return haversine(c1[0], c1[1], c2[0], c2[1])
    return None

def old_car(year):
    return year < 2008

def fmt_price(p):
    return f"{p:,}".replace(",", " ") + " ₽"

STATUS_ICON = {
    "open":      "🟢 Открыт — ждёт водителя",
    "taken":     "🔵 Принят водителем",
    "completed": "✅ Выполнен",
    "cancelled": "🔴 Отменён",
    "pending":   "⏳ Рассчитывается...",
}

def fmt_order(order: dict, show_price=True) -> str:
    t         = tariffs(order.get("from_city",""), order.get("to_city",""))
    cc        = order.get("car_class","standard")
    car_label = t.get(cc,{}).get("label", cc)
    dist      = order.get("distance_km")
    if dist:
        dist_txt = f"{dist:.0f} км"
    elif dist is None and order.get("status") in ("open", "pending"):
        dist_txt = "⚠️ Не удалось рассчитать автоматически"
    else:
        dist_txt = "уточняется"
    price     = order.get("price")
    nt        = is_nt(order.get("from_city","")) or is_nt(order.get("to_city",""))
    rflag     = "🆕" if nt else "🇷🇺"
    rname     = "Новые территории" if nt else "Россия"

    lines = [
        f"🚕 <b>ЗАКАЗ #{order['id']} · МЕЖГОРОД</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"  {rflag} <b>{rname}</b>",
        "",
        f"  📍 <b>Откуда:</b>   {order.get('from_city','—')}",
        f"  🏁 <b>Куда:</b>     {order.get('to_city','—')}",
        f"  📏 <b>Расстояние:</b> {dist_txt}",
        "",
        f"  📅 <b>Дата:</b>    {order.get('trip_date','—')}",
        f"  🕐 <b>Время:</b>   {order.get('trip_time','—')}",
        f"  👥 <b>Пассажиры:</b> {order.get('passengers','—')} чел.",
        f"  🚘 <b>Класс:</b>   {car_label}",
    ]
    if show_price and price:
        rate = t.get(cc,{}).get("price", 0)
        lines += [
            "",
            f"  💰 <b>Тариф:</b>   {rate} ₽/км",
            f"  💵 <b>Итого:</b>   {fmt_price(price)}",
            "  ⚠️  <i>Платные дороги — оплата клиентом</i>",
        ]
    elif show_price and not price:
        lines += [
            "",
            "  💵 <b>Итого:</b>   <i>будет рассчитана позже</i>",
            "  ⚠️  <i>Платные дороги — оплата клиентом</i>",
        ]
    if order.get("wishes"):
        lines += ["", f"  💬 <b>Пожелания:</b> {order['wishes']}"]
    lines += [
        "",
        f"  📌 <b>Статус:</b>  {STATUS_ICON.get(order.get('status','open'),'—')}",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════

def kb_main():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("🚕 Создать заказ", "🚗 Я водитель")
    m.row("📋 Мои заказы",   "📊 Тарифы")
    return m

def kb_driver(uid):
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    sub = "✅ Подписка активна" if is_subscribed(uid) else "❌ Нет подписки"
    m.row("📦 Доступные заказы", "👤 Мой профиль")
    m.row("💳 Абонемент", sub)
    m.row("📈 Мои поездки", "🔙 Главное меню")
    return m

def kb_cancel_reply():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add("❌ Отменить")
    return m

def kb_car_class(region="rf"):
    t = TARIFFS_NT if region == "new" else TARIFFS_RF
    m = types.InlineKeyboardMarkup(row_width=1)
    for key, val in t.items():
        m.add(types.InlineKeyboardButton(
            f"{val['label']}  ·  {val['price']} ₽/км",
            callback_data=f"car_{key}"
        ))
    m.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_order"))
    return m

def kb_pclass():
    m = types.InlineKeyboardMarkup(row_width=1)
    for key, val in TARIFFS_RF.items():
        m.add(types.InlineKeyboardButton(val["label"], callback_data=f"pclass_{key}"))
    return m

def kb_passengers():
    m = types.InlineKeyboardMarkup(row_width=4)
    m.add(*[types.InlineKeyboardButton(str(i), callback_data=f"pax_{i}") for i in range(1, 9)])
    m.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_order"))
    return m

def kb_subscriptions():
    m = types.InlineKeyboardMarkup(row_width=1)
    for key, plan in SUBSCRIPTION_PLANS.items():
        m.add(types.InlineKeyboardButton(f"💳  {plan['label']}", callback_data=f"sub_{key}"))
    m.add(types.InlineKeyboardButton("◀️ Назад", callback_data="back_driver"))
    return m


# ═══════════════════════════════════════════════════════════════
#  /start  /help  /admin
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid = msg.chat.id
    if db_in_blacklist(uid):
        bot.send_message(uid, "⛔ Вы заблокированы в системе.")
        return
    db_clear_session(uid)
    db_update_session(uid, role="")
    name = msg.from_user.first_name or "друг"
    bot.send_message(uid,
        f"👋 Добро пожаловать, <b>{name}</b>!\n\n"
        "🚕 <b>Межгород Трансфер Россия</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Надёжный сервис междугородних поездок по всей России.\n\n"
        "🔹 <b>Пассажирам</b> — оформить заказ за 2 минуты\n"
        "🔹 <b>Водителям</b> — получать заказы по классу авто\n"
        "🔹 <b>Тарифы</b> — фиксированные, без торга\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📢 Все заказы публикуются в канале: <b>@intercitytrans</b>\n"
        "Подпишитесь, чтобы видеть актуальные заказы!\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Выберите действие 👇",
        reply_markup=kb_main()
    )

@bot.message_handler(commands=["help"])
def cmd_help(msg):
    bot.send_message(msg.chat.id,
        "📖 <b>Справка</b>\n\n"
        "<b>Пассажир:</b>\n"
        "  • «Создать заказ» — оформить поездку за пару минут\n"
        "  • «Мои заказы» — история и текущий статус\n\n"
        "<b>Водитель:</b>\n"
        "  • «Я водитель» — войти в режим водителя\n"
        "  • Заполните профиль и загрузите документы\n"
        "  • Оформите абонемент для доступа к заказам\n\n"
        "<b>Тарифы:</b> нажмите «Тарифы» в главном меню\n\n"
        "По вопросам: @Olegan7979"
    )

@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    uid = msg.chat.id
    if uid not in ADMIN_IDS:
        bot.send_message(uid, "❌ Нет доступа.")
        return
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton("📋 Все заказы",          callback_data="adm_orders"),
        types.InlineKeyboardButton("👥 Водители",            callback_data="adm_drivers"),
        types.InlineKeyboardButton("📄 Проверить документы", callback_data="adm_docs"),
        types.InlineKeyboardButton("⛔ Чёрный список",        callback_data="adm_bl"),
        types.InlineKeyboardButton("💳 Ожидают оплаты",      callback_data="adm_subs"),
        types.InlineKeyboardButton("📊 Статистика",           callback_data="adm_stats"),
    )
    bot.send_message(uid,
        "🔧 <b>Панель администратора</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=m
    )


# ═══════════════════════════════════════════════════════════════
#  ГЛАВНОЕ МЕНЮ
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "🔙 Главное меню")
def to_main(msg):
    uid = msg.chat.id
    db_update_session(uid, role="", step="")
    bot.send_message(uid, "🏠 <b>Главное меню</b>", reply_markup=kb_main())

@bot.message_handler(func=lambda m: m.text == "📊 Тарифы")
def show_tariffs(msg):
    lines = ["💰 <b>ТАРИФЫ · РОССИЯ</b>  (₽ за км)\n━━━━━━━━━━━━━━━━━━"]
    for v in TARIFFS_RF.values():
        lines.append(f"  {v['label']} — от <b>{v['price']} ₽</b>")
    lines.append("\n💰 <b>ТАРИФЫ · НОВЫЕ ТЕРРИТОРИИ</b>  (₽ за км)\n━━━━━━━━━━━━━━━━━━")
    for v in TARIFFS_NT.values():
        lines.append(f"  {v['label']} — от <b>{v['price']} ₽</b>")
    lines.append("\n⚠️ <i>Платные дороги оплачиваются клиентом отдельно</i>")
    lines.append("🚫 <i>Торг и снижение тарифа — нарушение правил</i>")
    bot.send_message(msg.chat.id, "\n".join(lines))


# ═══════════════════════════════════════════════════════════════
#  СОЗДАНИЕ ЗАКАЗА — FSM
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "🚕 Создать заказ")
def order_start(msg):
    uid = msg.chat.id
    if db_in_blacklist(uid):
        bot.send_message(uid, "⛔ Вы заблокированы.")
        return
    db_update_session(uid, step="from_city", data={})
    bot.send_message(uid,
        "🗺 <b>Новый заказ · Шаг 1 из 7</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📍 Введите <b>город отправления</b>:\n"
        "<i>Например: Москва</i>",
        reply_markup=kb_cancel_reply()
    )

@bot.message_handler(func=lambda m: m.text == "❌ Отменить")
def order_cancel(msg):
    uid = msg.chat.id
    db_clear_session(uid)
    bot.send_message(uid, "❌ Отменено.", reply_markup=kb_main())

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "from_city")
def step_from(msg):
    uid  = msg.chat.id
    city = msg.text.strip()
    if not is_valid_city(city):
        bot.send_message(uid, "❌ Некорректное название города. Используйте буквы, пробелы и дефисы.")
        return
    s = db_get_session(uid)
    s["data"]["from_city"] = city
    db_update_session(uid, step="to_city", data=s["data"])
    bot.send_message(uid,
        f"✅ Откуда: <b>{city}</b>\n\n"
        "🗺 <b>Шаг 2 из 7</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🏁 Введите <b>город назначения</b>:"
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "to_city")
def step_to(msg):
    uid  = msg.chat.id
    city = msg.text.strip()
    if not is_valid_city(city):
        bot.send_message(uid, "❌ Некорректное название города. Используйте буквы, пробелы и дефисы.")
        return
    s = db_get_session(uid)
    s["data"]["to_city"] = city
    db_update_session(uid, step="trip_date", data=s["data"])
    bot.send_message(uid,
        f"✅ Куда: <b>{city}</b>\n\n"
        "🗺 <b>Шаг 3 из 7</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📅 Введите <b>дату поездки</b> в формате <code>ДД.ММ.ГГГГ</code>\n"
        "<i>Например: 20.06.2026</i>"
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "trip_date")
def step_date(msg):
    uid = msg.chat.id
    try:
        d = datetime.strptime(msg.text.strip(), "%d.%m.%Y")
        if d.date() < datetime.now(GEO_TZ).date():
            bot.send_message(uid, "❌ Дата не может быть в прошлом. Попробуйте ещё раз:")
            return
        s = db_get_session(uid)
        s["data"]["trip_date"] = d.strftime("%d.%m.%Y")
        db_update_session(uid, step="trip_time", data=s["data"])
        bot.send_message(uid,
            f"✅ Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n"
            "🗺 <b>Шаг 4 из 7</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🕐 Введите <b>время отправления</b> в формате <code>ЧЧ:ММ</code>\n"
            "<i>Например: 08:30</i>"
        )
    except ValueError:
        bot.send_message(uid, "❌ Неверный формат. Используйте <code>ДД.ММ.ГГГГ</code>")

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "trip_time")
def step_time(msg):
    uid = msg.chat.id
    t   = msg.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", t):
        bot.send_message(uid, "❌ Формат: <code>ЧЧ:ММ</code>  (например: 09:00)")
        return
    h, mn = map(int, t.split(":"))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        bot.send_message(uid, "❌ Некорректное время")
        return
    s = db_get_session(uid)
    s["data"]["trip_time"] = f"{h:02d}:{mn:02d}"
    db_update_session(uid, step="passengers", data=s["data"])
    bot.send_message(uid,
        f"✅ Время: <b>{h:02d}:{mn:02d}</b>\n\n"
        "🗺 <b>Шаг 5 из 7</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👥 Выберите <b>количество пассажиров</b>:",
        reply_markup=kb_passengers()
    )

def show_car_class(uid):
    s = db_get_session(uid)
    data = s["data"]
    region = "new" if (is_nt(data.get("from_city","")) or is_nt(data.get("to_city",""))) else "rf"
    data["region"] = region
    db_update_session(uid, step="car_class", data=data)
    
    rname = "🆕 Новые территории" if region == "new" else "🇷🇺 Россия"
    desc  = "\n".join(
        f"  {TARIFFS_RF[k]['label']} — <i>{CLASS_DESCRIPTIONS.get(k,'')}</i>"
        for k in CLASS_DESCRIPTIONS
    )
    bot.send_message(uid,
        "🗺 <b>Шаг 6 из 7</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🌍 Регион: <b>{rname}</b>\n\n"
        "🚘 <b>Класс автомобиля</b> — что означает:\n\n"
        f"{desc}\n\n"
        "Выберите подходящий вариант 👇",
        reply_markup=kb_car_class(region)
    )

def ask_wishes(uid):
    db_update_session(uid, step="wishes")
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("Нет", "❌ Отменить")
    bot.send_message(uid,
        "🗺 <b>Шаг 7 из 7</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "💬 <b>Дополнительные пожелания</b>\n\n"
        "Детское кресло, животные, большой багаж и т.д.\n"
        "Если нет — нажмите кнопку <b>«Нет»</b>",
        reply_markup=m
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "wishes")
def step_wishes(msg):
    uid  = msg.chat.id
    wish = msg.text.strip()
    s = db_get_session(uid)
    s["data"]["wishes"] = "" if wish.lower() in ["нет","—","-","no"] else wish
    db_update_session(uid, data=s["data"])
    finalize_order(uid)


def finalize_order(uid):
    s = db_get_session(uid)
    data = s["data"]
    db_clear_session(uid)
    
    oid = db_create_order({
        "passenger_id": uid,
        "from_city":    data["from_city"],
        "to_city":      data["to_city"],
        "trip_date":    data["trip_date"],
        "trip_time":    data["trip_time"],
        "passengers":   data["passengers"],
        "car_class":    data["car_class"],
        "wishes":       data.get("wishes",""),
        "distance_km":  None,
        "price":        None,
    })
    
    bot.send_message(uid,
        "⏳ <b>Заказ создается...</b>\n"
        "<i>Рассчитываю расстояние и стоимость, это займет несколько секунд</i>",
        reply_markup=kb_main()
    )
    
    def _background_calc():
        try:
            dist  = get_distance(data["from_city"], data["to_city"])
            price = None
            if dist:
                dist  = round(dist * 1.25)
                price = calc_price(dist, data["car_class"], data["from_city"], data["to_city"])
                db_update_order(oid, distance_km=dist, price=price)
            
            db_update_order(oid, status="open")
            order = db_get_order(oid)
            
            if dist:
                warn = "📲 Ваш заказ опубликован в канале @intercitytrans.\nВодители увидят его и свяжутся с вами."
            else:
                warn = ("⚠️ <b>Расстояние не удалось рассчитать автоматически.</b>\n"
                        "Стоимость будет уточнена после принятия заказа водителем.\n"
                        "📲 Заказ опубликован в канале.")
            
            cancel_btn = types.InlineKeyboardMarkup()
            cancel_btn.add(types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{oid}"))
            bot.send_message(uid,
                "✅ <b>Заказ успешно создан!</b>\n\n" + fmt_order(order) + f"\n\n{warn}",
                reply_markup=cancel_btn
            )
            
            _post_to_channel(oid)
            _notify_drivers(oid)
            
        except Exception as e:
            print(f"❌ Ошибка в фоновом расчете заказа #{oid}: {e}")
            bot.send_message(uid,
                "❌ Произошла ошибка при расчете заказа.\n"
                "Пожалуйста, попробуйте еще раз или обратитесь к администратору @Olegan7979"
            )
    
    threading.Thread(target=_background_calc, daemon=True).start()


def _post_to_channel(oid):
    if not GROUP_CHAT_ID:
        return
    order = db_get_order(oid)
    if not order or order["status"] != "open":
        return
    m = types.InlineKeyboardMarkup()
    m.add(types.InlineKeyboardButton(
        "✅ Взять заказ через бота",
        url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}"
    ))
    try:
        bot.send_message(GROUP_CHAT_ID, fmt_order(order), reply_markup=m)
    except Exception as e:
        print(f"⚠️ Канал: {e}")

def _notify_drivers(oid):
    order = db_get_order(oid)
    if not order or order["status"] != "open":
        return
    cc   = order.get("car_class")
    dist = order.get("distance_km") or 0
    
    for drv in db_all_drivers():
        did = drv["user_id"]
        if not is_subscribed(did):
            continue
        if drv.get("car_class") != cc:
            continue
        if old_car(drv.get("car_year", 2010)) and dist > 300:
            continue
        
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("✅ Взять заказ", callback_data=f"take_{oid}"))
        m.add(types.InlineKeyboardButton("➡️ Пропустить",  callback_data=f"skip_{oid}"))
        try:
            bot.send_message(did, "🔔 <b>Новый заказ — подходит вам!</b>\n\n" + fmt_order(order),
                             reply_markup=m)
        except Exception as e:
            print(f"⚠️ Водитель {did}: {e}")
        time.sleep(0.05)


# ═══════════════════════════════════════════════════════════════
#  МОИ ЗАКАЗЫ
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📋 Мои заказы")
def my_orders(msg):
    uid = msg.chat.id
    if db_get_session(uid).get("role") == "driver":
        driver_trips(msg)
        return
    result = db_passenger_orders(uid, limit=5)
    if not result:
        bot.send_message(uid, "📋 У вас пока нет заказов.")
        return
    bot.send_message(uid, f"📋 <b>Ваши заказы</b> (последние {len(result)}):")
    for o in result:
        m = types.InlineKeyboardMarkup()
        if o["status"] in ("open", "taken"):
            m.add(types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{o['id']}"))
        bot.send_message(uid, fmt_order(o), reply_markup=m)


# ═══════════════════════════════════════════════════════════════
#  ВОДИТЕЛЬ — МЕНЮ
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "🚗 Я водитель")
def driver_enter(msg):
    uid = msg.chat.id
    db_update_session(uid, role="driver")
    drv = db_get_driver(uid)
    if not drv:
        bot.send_message(uid,
            "🚗 <b>Режим водителя</b>\n\n"
            "Для начала работы необходимо:\n"
            "1️⃣ Заполнить профиль\n"
            "2️⃣ Загрузить документы\n"
            "3️⃣ Оформить абонемент\n\n"
            "Нажмите <b>«Мой профиль»</b> 👇",
            reply_markup=kb_driver(uid)
        )
        return
    dl       = days_left(uid)
    exp      = db_get_subscription(uid) or "—"
    sub_line = f"✅ Подписка до {exp} ({dl} дн.)" if dl > 0 else "❌ Нет активной подписки"
    docs_ok  = bool(drv.get("docs_verified"))
    docs_ln  = "✅ Документы проверены" if docs_ok else "⏳ Документы на проверке"
    bot.send_message(uid,
        "🚗 <b>Меню водителя</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 <b>{drv.get('name','—')}</b>\n"
        f"🚘 {drv.get('car_model','—')}  {drv.get('car_year','—')} г.\n"
        f"🔢 {drv.get('car_number','—')}\n"
        f"🏷 {drv.get('car_class_label','—')}\n"
        f"📞 {drv.get('phone','—')}\n\n"
        f"📄 {docs_ln}\n"
        f"💳 {sub_line}",
        reply_markup=kb_driver(uid)
    )

@bot.message_handler(func=lambda m: m.text == "📦 Доступные заказы")
def avail_orders(msg):
    uid = msg.chat.id
    if not is_subscribed(uid):
        bot.send_message(uid,
            "🔒 <b>Нет активного абонемента</b>\n\n"
            "Для доступа к заказам необходимо оформить подписку.\n"
            "Нажмите «Абонемент» 👇"
        )
        return
    drv      = db_get_driver(uid)
    cc       = drv.get("car_class","standard") if drv else "standard"
    car_year = drv.get("car_year", 2010) if drv else 2010
    avail = [
        o for o in db_open_orders_for_class(cc)
        if not (old_car(car_year) and (o.get("distance_km") or 0) > 300)
    ]
    if not avail:
        bot.send_message(uid,
            "📭 <b>Заказов пока нет</b>\n\n"
            "Для вашего класса авто нет открытых заказов.\n"
            "Мы уведомим вас, как только появится подходящий."
        )
        return
    bot.send_message(uid, f"📦 <b>Доступных заказов: {len(avail)}</b>")
    for o in avail[:5]:
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("✅ Взять заказ", callback_data=f"take_{o['id']}"))
        bot.send_message(uid, fmt_order(o), reply_markup=m)

@bot.message_handler(func=lambda m: m.text == "📈 Мои поездки")
def driver_trips(msg):
    uid    = msg.chat.id
    result = db_driver_orders(uid, limit=5)
    if not result:
        bot.send_message(uid, "📈 У вас пока нет принятых поездок.")
        return
    bot.send_message(uid, f"📈 <b>Ваши поездки</b> (последние {len(result)}):")
    for o in result:
        m = types.InlineKeyboardMarkup()
        if o["status"] == "taken":
            m.add(types.InlineKeyboardButton("❌ Отказаться от заказа", callback_data=f"driver_cancel_{o['id']}"))
        bot.send_message(uid, fmt_order(o), reply_markup=m)


# ═══════════════════════════════════════════════════════════════
#  ПРОФИЛЬ ВОДИТЕЛЯ — FSM (12 шагов)
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "👤 Мой профиль")
def profile_menu(msg):
    uid = msg.chat.id
    drv = db_get_driver(uid)
    if drv:
        docs_status = "✅ Проверены" if drv.get("docs_verified") else "⏳ На проверке у администратора"
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("✏️ Изменить профиль", callback_data="edit_profile"))
        bot.send_message(uid,
            "👤 <b>Ваш профиль водителя</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Имя:          <b>{drv.get('name','—')}</b>\n"
            f"🚘 Авто:         <b>{drv.get('car_model','—')}</b>\n"
            f"📅 Год:          <b>{drv.get('car_year','—')}</b>\n"
            f"🔢 Гос. номер:   <b>{drv.get('car_number','—')}</b>\n"
            f"🏷 Класс:        <b>{drv.get('car_class_label','—')}</b>\n"
            f"📞 Телефон:      <b>{drv.get('phone','—')}</b>\n"
            f"💬 Username:     <b>{drv.get('username','—')}</b>\n\n"
            f"📄 Документы:    <b>{docs_status}</b>",
            reply_markup=m
        )
    else:
        _start_profile(uid)

def _start_profile(uid):
    db_update_session(uid, step="profile_name", data={})
    bot.send_message(uid,
        "📝 <b>Заполнение профиля водителя</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Всего 12 шагов: анкета + фото документов\n\n"
        "<b>Шаг 1 / 12 — Ваше имя:</b>",
        reply_markup=kb_cancel_reply()
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "profile_name")
def p_name(msg):
    uid  = msg.chat.id
    name = msg.text.strip()
    if len(name) < 2:
        bot.send_message(uid, "❌ Введите корректное имя"); return
    s = db_get_session(uid)
    s["data"]["name"] = name
    db_update_session(uid, step="profile_car_model", data=s["data"])
    bot.send_message(uid,
        f"✅ Имя: <b>{name}</b>\n\n"
        "<b>Шаг 2 / 12 — Марка и модель авто:</b>\n<i>Например: Toyota Camry</i>"
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "profile_car_model")
def p_model(msg):
    uid   = msg.chat.id
    model = msg.text.strip()
    s = db_get_session(uid)
    s["data"]["car_model"] = model
    db_update_session(uid, step="profile_car_year", data=s["data"])
    bot.send_message(uid,
        f"✅ Авто: <b>{model}</b>\n\n"
        "<b>Шаг 3 / 12 — Год выпуска:</b>\n<i>Например: 2020</i>"
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "profile_car_year")
def p_year(msg):
    uid = msg.chat.id
    try:
        year = int(msg.text.strip())
        if not (1990 <= year <= datetime.now(GEO_TZ).year + 1):
            raise ValueError
        s = db_get_session(uid)
        s["data"]["car_year"] = year
        db_update_session(uid, step="profile_car_number", data=s["data"])
        warn = "\n\n⚠️ <i>Авто до 2008 г. — заказы до 300 км</i>" if year < 2008 else ""
        bot.send_message(uid,
            f"✅ Год: <b>{year}</b>{warn}\n\n"
            "<b>Шаг 4 / 12 — Гос. номер:</b>\n<i>Например: А123ВС777</i>"
        )
    except ValueError:
        bot.send_message(uid, "❌ Некорректный год. Введите, например: 2019")

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "profile_car_number")
def p_number(msg):
    uid    = msg.chat.id
    number = msg.text.strip().upper()
    s = db_get_session(uid)
    s["data"]["car_number"] = number
    db_update_session(uid, step="profile_car_class", data=s["data"])
    desc = "\n".join(
        f"  {TARIFFS_RF[k]['label']} — <i>{CLASS_DESCRIPTIONS.get(k,'')}</i>"
        for k in CLASS_DESCRIPTIONS
    )
    bot.send_message(uid,
        f"✅ Гос. номер: <b>{number}</b>\n\n"
        "<b>Шаг 5 / 12 — Класс вашего автомобиля</b>\n\n"
        "Описание классов:\n\n"
        f"{desc}\n\n"
        "Выберите класс вашего авто 👇",
        reply_markup=kb_pclass()
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "profile_phone")
def p_phone(msg):
    uid   = msg.chat.id
    phone = msg.text.strip()
    if not re.match(r"^[\+\d][\d\s\-\(\)]{6,15}$", phone):
        bot.send_message(uid, "❌ Введите корректный номер, например: +79001234567"); return
    s = db_get_session(uid)
    s["data"]["phone"] = phone
    db_update_session(uid, step="profile_username", data=s["data"])
    bot.send_message(uid,
        f"✅ Телефон: <b>{phone}</b>\n\n"
        "<b>Шаг 7 / 12 — Telegram username:</b>\n"
        "<i>Например: @ivanov_driver</i>\n"
        "<i>Если нет — напишите «нет»</i>"
    )

@bot.message_handler(func=lambda m: db_get_session(m.chat.id).get("step") == "profile_username")
def p_username(msg):
    uid = msg.chat.id
    u   = msg.text.strip()
    if u.lower() == "нет":
        u = f"@id{uid}"
    elif not u.startswith("@"):
        u = "@" + u
    s = db_get_session(uid)
    s["data"]["username"] = u
    db_update_session(uid, step="profile_doc_lic_front", data=s["data"])
    bot.send_message(uid,
        f"✅ Username: <b>{u}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📄 <b>Загрузка документов</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Шаг 8 / 12 — Водительское удостоверение</b>\n\n"
        "📸 Отправьте фото <b>лицевой стороны</b> прав\n"
        "<i>Данные должны быть чётко видны</i>"
    )

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: db_get_session(m.chat.id).get("step") in [
        "profile_doc_lic_front","profile_doc_lic_back",
        "profile_doc_sts_front","profile_doc_sts_back","profile_doc_car"
    ]
)
def p_photo(msg):
    uid  = msg.chat.id
    s = db_get_session(uid)
    step = s["step"]
    fid  = msg.photo[-1].file_id

    step_map = {
        "profile_doc_lic_front": ("doc_lic_front", "profile_doc_lic_back",
            "✅ Лицевая сторона прав получена\n\n"
            "<b>Шаг 9 / 12</b> — 📸 Обратная сторона прав:"),
        "profile_doc_lic_back":  ("doc_lic_back",  "profile_doc_sts_front",
            "✅ Обратная сторона прав получена\n\n"
            "<b>Шаг 10 / 12</b> — 📸 Лицевая сторона СТС:"),
        "profile_doc_sts_front": ("doc_sts_front", "profile_doc_sts_back",
            "✅ Лицевая сторона СТС получена\n\n"
            "<b>Шаг 11 / 12</b> — 📸 Обратная сторона СТС:"),
        "profile_doc_sts_back":  ("doc_sts_back",  "profile_doc_car",
            "✅ Обратная сторона СТС получена\n\n"
            "<b>Шаг 12 / 12</b> — 📸 Фото автомобиля целиком\n"
            "<i>Гос. номер должен быть виден</i>"),
        "profile_doc_car":       ("doc_car", None, None),
    }
    field, next_step, next_msg = step_map[step]
    s["data"][field] = fid

    if next_step:
        db_update_session(uid, step=next_step, data=s["data"])
        bot.send_message(uid, next_msg)
    else:
        db_update_session(uid, data=s["data"])
        _finalize_profile(uid)

def _finalize_profile(uid):
    s = db_get_session(uid)
    data = s["data"]
    data["docs_verified"]  = False
    data["registered_at"]  = datetime.now(GEO_TZ).isoformat()
    db_save_driver(uid, data)
    db_clear_session(uid)

    bot.send_message(uid,
        "🎉 <b>Профиль успешно создан!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {data['name']}\n"
        f"🚘 {data['car_model']} ({data['car_year']} г.)\n"
        f"🔢 {data.get('car_number','—')}\n"
        f"🏷 {data.get('car_class_label','—')}\n"
        f"📞 {data['phone']}\n"
        f"💬 {data.get('username','—')}\n\n"
        "📄 Документы отправлены на проверку администратору.\n"
        "После проверки вы получите уведомление.\n\n"
        "💳 Следующий шаг — оформите <b>абонемент</b>.",
        reply_markup=kb_driver(uid)
    )

    drv = db_get_driver(uid)
    for admin_id in ADMIN_IDS:
        try:
            m = types.InlineKeyboardMarkup(row_width=2)
            m.add(
                types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"doc_ok_{uid}"),
                types.InlineKeyboardButton("❌ Отклонить",   callback_data=f"doc_rej_{uid}")
            )
            bot.send_message(admin_id,
                "📋 <b>Новый водитель — проверка документов</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 {drv.get('name','—')}\n"
                f"🚘 {drv.get('car_model','—')} ({drv.get('car_year','—')} г.)\n"
                f"🔢 {drv.get('car_number','—')}\n"
                f"🏷 {drv.get('car_class_label','—')}\n"
                f"📞 {drv.get('phone','—')}\n"
                f"💬 {drv.get('username','—')}\n"
                f"ID: <code>{uid}</code>\n\n"
                "📸 Документы — в следующих сообщениях:"
            )
            for field, caption in [
                ("doc_lic_front", "📄 Права — лицевая"),
                ("doc_lic_back",  "📄 Права — обратная"),
                ("doc_sts_front", "📄 СТС — лицевая"),
                ("doc_sts_back",  "📄 СТС — обратная"),
                ("doc_car",       "🚗 Фото авто"),
            ]:
                fid = drv.get(field)
                if fid:
                    kwargs = {"caption": caption}
                    if field == "doc_car":
                        kwargs["reply_markup"] = m
                    bot.send_photo(admin_id, fid, **kwargs)
            if not drv.get("doc_car"):
                bot.send_message(admin_id, "Подтвердить или отклонить:", reply_markup=m)
        except Exception as e:
            print(f"⚠️ Уведомление админа {admin_id}: {e}")


# ═══════════════════════════════════════════════════════════════
#  АБОНЕМЕНТ
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "💳 Абонемент")
def subscription_menu(msg):
    uid = msg.chat.id
    dl  = days_left(uid)
    if dl > 0:
        exp  = db_get_subscription(uid) or "—"
        text = (
            "✅ <b>Абонемент активен</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"Действует до: <b>{exp}</b>\n"
            f"Осталось: <b>{dl} дн.</b>\n\n"
            "Хотите продлить подписку?"
        )
    else:
        text = (
            "💳 <b>Оформление абонемента</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "❌ Активной подписки нет\n\n"
            "Без абонемента заказы недоступны.\n"
            "Выберите тариф:"
        )
    bot.send_message(uid, text, reply_markup=kb_subscriptions())

@bot.message_handler(func=lambda m: m.text and "подписка" in m.text.lower())
def sub_status_btn(msg):
    uid = msg.chat.id
    dl  = days_left(uid)
    if dl > 0:
        bot.send_message(uid, f"✅ Подписка активна, осталось {dl} дн.")
    else:
        bot.send_message(uid, "❌ Подписка не активна. Нажмите «Абонемент».")


# ═══════════════════════════════════════════════════════════════
#  CALLBACKS
# ═══════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid  = call.from_user.id
    data = call.data
    s = db_get_session(uid)

    if data.startswith("pax_"):
        if s.get("step") != "passengers":
            bot.answer_callback_query(call.id, "❌ Ошибка сессии"); return
        pax = int(data.split("_")[1])
        s["data"]["passengers"] = pax
        db_update_session(uid, data=s["data"])
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, f"✅ Пассажиров: <b>{pax}</b>")
        show_car_class(uid)
        bot.answer_callback_query(call.id)

    elif data.startswith("car_"):
        if s.get("step") != "car_class":
            bot.answer_callback_query(call.id, "❌ Ошибка сессии"); return
        cc  = data.split("_",1)[1]
        reg = s["data"].get("region","rf")
        t   = TARIFFS_NT if reg == "new" else TARIFFS_RF
        lbl = t.get(cc,{}).get("label", cc)
        s["data"]["car_class"]       = cc
        s["data"]["car_class_label"] = lbl
        db_update_session(uid, data=s["data"])
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, f"✅ Класс: <b>{lbl}</b>")
        ask_wishes(uid)
        bot.answer_callback_query(call.id)

    elif data.startswith("pclass_"):
        if s.get("step") != "profile_car_class":
            bot.answer_callback_query(call.id, "❌ Ошибка сессии"); return
        cc  = data.split("_",1)[1]
        lbl = TARIFFS_RF.get(cc,{}).get("label", cc)
        s["data"]["car_class"]       = cc
        s["data"]["car_class_label"] = lbl
        db_update_session(uid, step="profile_phone", data=s["data"])
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid,
            f"✅ Класс: <b>{lbl}</b>\n\n"
            "<b>Шаг 6 / 12 — Номер телефона</b>\n"
            "<i>Будет виден пассажирам при принятии заказа</i>\n"
            "<i>Например: +79001234567</i>"
        )
        bot.answer_callback_query(call.id)

    elif data == "cancel_order":
        db_clear_session(uid)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, "❌ Создание заказа отменено.", reply_markup=kb_main())
        bot.answer_callback_query(call.id)

    elif data.startswith("cancel_") and not data.startswith("cancel_order"):
        oid   = int(data.split("_")[1])
        order = db_get_order(oid)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
        if order.get("passenger_id") != uid:
            bot.answer_callback_query(call.id, "❌ Это не ваш заказ"); return
        if order["status"] not in ("open", "taken"):
            bot.answer_callback_query(call.id, "❌ Нельзя отменить"); return
        db_update_order(oid, status="cancelled")
        if order.get("driver_id"):
            try:
                bot.send_message(order["driver_id"],
                    f"❌ <b>Пассажир отменил заказ #{oid}</b>\n"
                    f"{order.get('from_city','—')} → {order.get('to_city','—')}\n"
                    f"📅 {order.get('trip_date','—')}  🕐 {order.get('trip_time','—')}"
                )
            except: pass
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, f"✅ Заказ <b>#{oid}</b> отменён.")
        bot.answer_callback_query(call.id)

    elif data.startswith("driver_cancel_"):
        oid   = int(data.split("_")[2])
        order = db_get_order(oid)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
        if order.get("driver_id") != uid:
            bot.answer_callback_query(call.id, "❌ Это не ваш заказ"); return
        if order["status"] != "taken":
            bot.answer_callback_query(call.id, "❌ Нельзя отменить"); return
        db_update_order(oid, status="open", driver_id=None, taken_at=None)
        passenger_id = order.get("passenger_id")
        if passenger_id:
            try:
                bot.send_message(passenger_id,
                    f"❌ <b>Водитель отказался от заказа #{oid}</b>\n"
                    f"{order.get('from_city','—')} → {order.get('to_city','—')}\n\n"
                    "Заказ снова открыт. Водители увидят его и свяжутся с вами."
                )
            except: pass
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid,
            f"✅ Вы отказались от заказа <b>#{oid}</b>\n"
            "Заказ снова открыт для других водителей."
        )
        _notify_drivers(oid)
        bot.answer_callback_query(call.id, "✅ Заказ снова открыт")

    elif data.startswith("take_"):
        oid = int(data.split("_")[1])
        
        conn = get_db()
        cursor = conn.execute(
            "UPDATE orders SET status='taken', driver_id=?, taken_at=? WHERE id=? AND status='open'",
            (uid, datetime.now(GEO_TZ).isoformat(), oid)
        )
        if cursor.rowcount == 0:
            conn.close()
            bot.answer_callback_query(call.id, "⚠️ Заказ уже взят другим водителем или недоступен")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            return
        conn.commit()
        conn.close()
        
        order = db_get_order(oid)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
        
        if not is_subscribed(uid):
            db_update_order(oid, status="open", driver_id=None, taken_at=None)
            bot.answer_callback_query(call.id, "❌ Нет активного абонемента"); return

        drv = db_get_driver(uid) or {}
        car_year = drv.get("car_year", 2010)
        dist = order.get("distance_km") or 0
        if old_car(car_year) and dist > 300:
            db_update_order(oid, status="open", driver_id=None, taken_at=None)
            bot.answer_callback_query(call.id,
                f"❌ Ваш автомобиль {car_year} г.в. — заказы только до 300 км (здесь ~{dist:.0f} км)",
                show_alert=True
            )
            return

        passenger_id = order["passenger_id"]

        try:
            pchat  = bot.get_chat(passenger_id)
            p_name = pchat.first_name or "Пассажир"
            p_user = pchat.username
        except:
            p_name = "Пассажир"
            p_user = None

        p_url  = f"https://t.me/{p_user}" if p_user else f"tg://user?id={passenger_id}"
        p_link = f'<a href="{p_url}">{p_name}</a>'

        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

        m_drv = types.InlineKeyboardMarkup(row_width=1)
        m_drv.add(types.InlineKeyboardButton("💬 Написать пассажиру", url=p_url))
        m_drv.add(types.InlineKeyboardButton("❌ Отказаться от заказа", callback_data=f"driver_cancel_{oid}"))
        bot.send_message(uid,
            f"✅ <b>Заказ #{oid} принят!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 Пассажир: {p_link}\n"
            f"📍 {order.get('from_city','—')} → {order.get('to_city','—')}\n"
            f"📅 {order.get('trip_date','—')}  🕐 {order.get('trip_time','—')}\n"
            f"👥 {order.get('passengers','—')} чел.\n\n"
            "Свяжитесь с пассажиром для подтверждения деталей 👇\n\n"
            "⚠️ <i>Если планы изменились — нажмите «Отказаться».</i>",
            reply_markup=m_drv
        )

        drv_user = (drv.get("username","") or "").lstrip("@")
        d_url    = f"https://t.me/{drv_user}" if (drv_user and not drv_user.startswith("id")) \
                   else f"tg://user?id={uid}"
        m_pass   = types.InlineKeyboardMarkup(row_width=1)
        m_pass.add(types.InlineKeyboardButton("📞 Написать водителю",  url=d_url))
        m_pass.add(types.InlineKeyboardButton("✅ Поездка завершена",  callback_data=f"done_{oid}"))
        m_pass.add(types.InlineKeyboardButton("❌ Отменить",            callback_data=f"cancel_{oid}"))
        try:
            bot.send_message(passenger_id,
                f"🎉 <b>Водитель найден! Заказ #{oid}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 <b>{drv.get('name','—')}</b>\n"
                f"🚘 {drv.get('car_model','—')}  {drv.get('car_year','—')} г.\n"
                f"🔢 Гос. номер: <b>{drv.get('car_number','—')}</b>\n"
                f"🏷 Класс: {drv.get('car_class_label','—')}\n"
                f"📞 Телефон: <b>{drv.get('phone','—')}</b>\n"
                f"💬 Telegram: <b>{drv.get('username','—')}</b>\n\n"
                "Нажмите кнопку ниже чтобы написать водителю 👇\n"
                "Хорошей поездки! 🚕",
                reply_markup=m_pass
            )
        except Exception as e:
            print(f"⚠️ Уведомление пассажира: {e}")
        bot.answer_callback_query(call.id, "✅ Заказ принят!")

    elif data.startswith("skip_"):
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Пропущено")

    elif data.startswith("done_"):
        oid   = int(data.split("_")[1])
        order = db_get_order(oid)
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
        db_update_order(oid, status="completed", completed_at=datetime.now(GEO_TZ).isoformat())
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        stars_kb = types.InlineKeyboardMarkup()
        stars_kb.add(*[types.InlineKeyboardButton("⭐"*i, callback_data=f"rate_{oid}_{i}") for i in range(1,6)])
        bot.send_message(uid,
            "✅ <b>Поездка завершена!</b>\n\n"
            "Спасибо, что воспользовались сервисом.\n"
            "Оцените качество поездки 👇",
            reply_markup=stars_kb
        )
        if order.get("driver_id"):
            try:
                bot.send_message(order["driver_id"],
                    f"✅ Пассажир подтвердил завершение поездки #{oid}. Отличная работа! 👍"
                )
            except: pass
        bot.answer_callback_query(call.id)

    elif data.startswith("rate_"):
        stars = int(data.split("_")[2])
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, f"Спасибо за оценку {'⭐'*stars}!")
        bot.answer_callback_query(call.id)

    elif data.startswith("sub_"):
        plan_key = data.split("_")[1]
        plan     = SUBSCRIPTION_PLANS.get(plan_key)
        if not plan:
            bot.answer_callback_query(call.id, "❌ Тариф не найден"); return
        db_set_pending(uid, plan_key)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("📨 Я оплатил, жду подтверждения", callback_data=f"paid_{plan_key}"))
        bot.send_message(uid,
            "💳 <b>Оплата абонемента</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"Тариф: <b>{plan['label']}</b>\n"
            f"Сумма: <b>{plan['price']} ₽</b>\n\n"
            f"💬 {PAYMENT_DETAILS}\n\n"
            "После оплаты нажмите кнопку ниже — администратор активирует подписку.",
            reply_markup=m
        )
        drv = db_get_driver(uid) or {}
        for admin_id in ADMIN_IDS:
            try:
                m_adm = types.InlineKeyboardMarkup(row_width=2)
                m_adm.add(
                    types.InlineKeyboardButton("✅ Активировать", callback_data=f"conf_sub_{uid}_{plan_key}"),
                    types.InlineKeyboardButton("❌ Отклонить",    callback_data=f"rej_sub_{uid}")
                )
                bot.send_message(admin_id,
                    "💳 <b>Запрос на абонемент</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    f"👤 {drv.get('name','—')}\n"
                    f"🚘 {drv.get('car_model','—')}\n"
                    f"🏷 {drv.get('car_class_label','—')}\n"
                    f"Тариф: <b>{plan['label']}</b>\n"
                    f"ID: <code>{uid}</code>",
                    reply_markup=m_adm
                )
            except Exception as e:
                print(f"⚠️ {e}")
        bot.answer_callback_query(call.id)

    elif data.startswith("paid_"):
        plan_key = data.split("_")[1]
        plan = SUBSCRIPTION_PLANS.get(plan_key, {})
        bot.send_message(uid,
            "⏳ <b>Заявка отправлена!</b>\n\n"
            "Администратор получил уведомление.\n"
            "Активация обычно занимает до 1–2 часов.\n\n"
            "⚠️ <i>Подписка будет активирована только после проверки оплаты.</i>"
        )
        drv = db_get_driver(uid) or {}
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id,
                    f"🔔 <b>Пользователь сообщил об оплате!</b>\n"
                    f"👤 {drv.get('name','—')} (ID: {uid})\n"
                    f"Тариф: {plan.get('label','—')}\n\n"
                    "Проверьте оплату и активируйте подписку."
                )
            except Exception as e:
                print(f"⚠️ Уведомление админа: {e}")
        bot.answer_callback_query(call.id, "✅ Ждите подтверждения")

    elif data.startswith("conf_sub_"):
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        parts    = data.split("_")
        tgt      = int(parts[2])
        plan_key = parts[3]
        plan     = SUBSCRIPTION_PLANS.get(plan_key)
        if not plan:
            bot.answer_callback_query(call.id, "❌"); return
        cur_exp  = db_get_subscription(tgt)
        base     = max(datetime.strptime(cur_exp,"%Y-%m-%d").date(), datetime.now(GEO_TZ).date()) \
                   if cur_exp else datetime.now(GEO_TZ).date()
        new_exp  = (base + timedelta(days=plan["days"])).strftime("%Y-%m-%d")
        db_set_subscription(tgt, new_exp, admin_id=uid, plan_key=plan_key)
        db_del_pending(tgt, admin_id=uid, plan_key=plan_key)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, f"✅ Подписка активирована до {new_exp}")
        try:
            bot.send_message(tgt,
                "🎉 <b>Абонемент активирован!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"Тариф: <b>{plan['label']}</b>\n"
                f"Действует до: <b>{datetime.strptime(new_exp,'%Y-%m-%d').strftime('%d.%m.%Y')}</b>\n\n"
                "Теперь вы получаете заказы. Удачных поездок! 🚕",
                reply_markup=kb_driver(tgt)
            )
        except: pass
        bot.answer_callback_query(call.id, "✅ Подтверждено")

    elif data.startswith("rej_sub_"):
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        tgt = int(data.split("_")[2])
        pending_plan = db_get_pending(tgt)
        db_del_pending(tgt, admin_id=uid, plan_key=pending_plan)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        try:
            bot.send_message(tgt, "❌ Запрос на абонемент отклонён. Свяжитесь с @Olegan7979.")
        except: pass
        bot.answer_callback_query(call.id, "❌ Отклонено")

    elif data.startswith("doc_ok_"):
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        tgt = int(data.split("_")[2])
        db_verify_driver(tgt, True)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, f"✅ Документы водителя {tgt} подтверждены.")
        try:
            bot.send_message(tgt,
                "✅ <b>Документы проверены!</b>\n\n"
                "Ваши документы подтверждены администратором.\n"
                "Оформите абонемент для начала работы 👇",
                reply_markup=kb_driver(tgt)
            )
        except: pass
        bot.answer_callback_query(call.id, "✅")

    elif data.startswith("doc_rej_"):
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        tgt = int(data.split("_")[2])
        db_verify_driver(tgt, False)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        try:
            bot.send_message(tgt,
                "❌ <b>Документы не прошли проверку</b>\n\n"
                "Пожалуйста, загрузите документы повторно.\n"
                "Нажмите «Мой профиль» → «Изменить профиль»"
            )
        except: pass
        bot.answer_callback_query(call.id, "❌ Отклонено")

    elif data == "edit_profile":
        _start_profile(uid)
        bot.answer_callback_query(call.id)

    elif data == "back_driver":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.send_message(uid, "🚗 Меню водителя", reply_markup=kb_driver(uid))
        bot.answer_callback_query(call.id)

    elif data == "adm_orders":
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        last = db_all_orders(limit=10)
        if not last:
            bot.send_message(uid, "Заказов нет")
        else:
            bot.send_message(uid, f"📋 Последние заказы ({len(last)}):")
            for o in last:
                bot.send_message(uid, fmt_order(o))
        bot.answer_callback_query(call.id)

    elif data == "adm_drivers":
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        drivers = db_all_drivers()
        if not drivers:
            bot.send_message(uid, "Водителей нет")
            bot.answer_callback_query(call.id); return
        text = "👥 <b>Водители:</b>\n━━━━━━━━━━━━━━━━━━\n\n"
        for d in drivers:
            dl   = days_left(d["user_id"])
            sub  = f"✅ {dl}д." if dl > 0 else "❌"
            docs = "✅" if d.get("docs_verified") else "⏳"
            text += (f"👤 <b>{d.get('name','—')}</b>  {sub}  {docs}\n"
                     f"   {d.get('car_model','—')} · {d.get('car_class_label','—')}\n"
                     f"   📞 {d.get('phone','—')} · ID: <code>{d['user_id']}</code>\n\n")
        bot.send_message(uid, text[:4000])
        bot.answer_callback_query(call.id)

    elif data == "adm_docs":
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        unverified = [d for d in db_all_drivers() if not d.get("docs_verified")]
        if not unverified:
            bot.send_message(uid, "✅ Все документы проверены")
            bot.answer_callback_query(call.id); return
        bot.send_message(uid, f"📄 Ожидают проверки: {len(unverified)} водителей")
        for d in unverified[:5]:
            m = types.InlineKeyboardMarkup(row_width=2)
            m.add(
                types.InlineKeyboardButton("✅ Одобрить",  callback_data=f"doc_ok_{d['user_id']}"),
                types.InlineKeyboardButton("❌ Отклонить", callback_data=f"doc_rej_{d['user_id']}")
            )
            bot.send_message(uid,
                f"👤 {d.get('name','—')} · {d.get('car_model','—')}\n"
                f"📞 {d.get('phone','—')} · ID: {d['user_id']}",
                reply_markup=m
            )
        bot.answer_callback_query(call.id)

    elif data == "adm_bl":
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        bl = db_all_blacklist()
        if bl:
            bot.send_message(uid, "⛔ <b>Чёрный список:</b>\n" + "\n".join(str(u) for u in bl))
        else:
            bot.send_message(uid, "✅ Чёрный список пуст")
        bot.answer_callback_query(call.id)

    elif data == "adm_subs":
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        pending = db_all_pending()
        if not pending:
            bot.send_message(uid, "✅ Нет ожидающих оплат")
        for p in pending:
            plan = SUBSCRIPTION_PLANS.get(p["plan_key"], {})
            drv  = db_get_driver(p["user_id"]) or {}
            m = types.InlineKeyboardMarkup(row_width=2)
            m.add(
                types.InlineKeyboardButton("✅ Активировать",
                    callback_data=f"conf_sub_{p['user_id']}_{p['plan_key']}"),
                types.InlineKeyboardButton("❌ Отклонить",
                    callback_data=f"rej_sub_{p['user_id']}")
            )
            bot.send_message(uid,
                f"💳 {drv.get('name','—')} (ID: {p['user_id']})\n"
                f"Тариф: {plan.get('label','—')}",
                reply_markup=m
            )
        bot.answer_callback_query(call.id)

    elif data == "adm_stats":
        if uid not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌"); return
        st = db_stats()
        bot.send_message(uid,
            "📊 <b>Статистика</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Всего заказов:   <b>{st['total']}</b>\n"
            f"🟢 Открытых:        <b>{st['open']}</b>\n"
            f"✅ Завершённых:     <b>{st['done']}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🚗 Водителей:       <b>{st['drivers']}</b>\n"
            f"💳 С подпиской:     <b>{st['subscribed']}</b>\n"
            f"📄 Документы ОК:    <b>{st['docs_ok']}</b>"
        )
        bot.answer_callback_query(call.id)

    else:
        bot.answer_callback_query(call.id)


# ═══════════════════════════════════════════════════════════════
#  ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(commands=["ban"])
def cmd_ban(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        tgt = int(msg.text.split()[1])
        db_add_blacklist(tgt)
        bot.send_message(msg.chat.id, f"⛔ {tgt} добавлен в ЧС")
        try: bot.send_message(tgt, "⛔ Вы заблокированы в системе.")
        except: pass
    except Exception as e:
        bot.send_message(msg.chat.id, f"Использование: /ban <user_id>\nОшибка: {e}")

@bot.message_handler(commands=["unban"])
def cmd_unban(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        tgt = int(msg.text.split()[1])
        db_remove_blacklist(tgt)
        bot.send_message(msg.chat.id, f"✅ {tgt} удалён из ЧС")
    except Exception as e:
        bot.send_message(msg.chat.id, f"Использование: /unban <user_id>\nОшибка: {e}")

@bot.message_handler(commands=["stats"])
def cmd_stats(msg):
    if msg.chat.id not in ADMIN_IDS: return
    s = db_stats()
    bot.send_message(msg.chat.id,
        f"📊 Заказов: {s['total']} | Открыто: {s['open']} | Завершено: {s['done']}\n"
        f"🚗 Водителей: {s['drivers']} | С подпиской: {s['subscribed']}"
    )


# ═══════════════════════════════════════════════════════════════
#  FALLBACK
# ═══════════════════════════════════════════════════════════════

@bot.message_handler(
    content_types=["photo"],
    func=lambda m: db_get_session(m.chat.id).get("step") not in [
        "profile_doc_lic_front","profile_doc_lic_back",
        "profile_doc_sts_front","profile_doc_sts_back","profile_doc_car"
    ]
)
def photo_unexpected(msg):
    bot.send_message(msg.chat.id,
        "📸 Сейчас не ожидается фото.\n"
        "Используйте кнопки меню 👇",
        reply_markup=kb_main()
    )

@bot.message_handler(func=lambda m: True)
def fallback(msg):
    uid = msg.chat.id
    if db_get_session(uid).get("step"):
        bot.send_message(uid, "⚠️ Введите запрошенные данные или нажмите «❌ Отменить»")
    else:
        bot.send_message(uid, "👇 Используйте кнопки меню", reply_markup=kb_main())


# ═══════════════════════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 55)
    print("  🚕  МЕЖГОРОД ТРАНСФЕР РОССИЯ  v3.3  (SQLite FSM)")
    print("=" * 55)
    init_db()
    print(f"  📢 Канал:          {GROUP_CHAT_ID}")
    print(f"  👑 Администраторы: {ADMIN_IDS}")
    print(f"  🗄  База данных:    {DB_PATH}")
    print("=" * 55)

    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except KeyboardInterrupt:
            print("\n🛑 Остановлен вручную")
            break
        except telebot.apihelper.ApiException as e:
            print(f"❌ API: {e}")
            time.sleep(5)
        except requests.exceptions.ConnectionError as e:
            print(f"❌ Сеть: {e}")
            time.sleep(10)
        except Exception as e:
            import traceback
            traceback.print_exc()
            time.sleep(5)