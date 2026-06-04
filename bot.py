# ══════════════════════════════════════════════════════════════
#  МЕЖГОРОД ТРАНСФЕР v14.0 — aiogram 3 + FSM
# ══════════════════════════════════════════════════════════════
import asyncio, sqlite3, json, re, html, logging, os
import urllib3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, Contact, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from geopy.geocoders import Yandex
from geopy.distance import geodesic as geo_dist

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ══════════════ НАСТРОЙКИ ══════════════
BOT_TOKEN       = os.getenv("BOT_TOKEN")
YANDEX_GEO_KEY  = os.getenv("YANDEX_GEOCODER_KEY")
GROUP_CHAT_ID   = int(os.getenv("GROUP_CHAT_ID") or "0")
ADMIN_IDS       = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
BOT_USERNAME    = os.getenv("BOT_USERNAME", "intercitytrans_bot")
PAYMENT_DETAILS = os.getenv("PAYMENT_DETAILS", "Для оплаты абонемента свяжитесь с администратором @Olegan7979")
DB_PATH         = os.getenv("DB_PATH", os.path.join(os.getenv("DATA_DIR", "/app/data"), "intercity_bot.db"))
DATA_DIR        = os.getenv("DATA_DIR", "/app/data")
DIST_COEFF      = 1.25
NOTIFY_LIMIT    = 50
NOTIFY_BATCH    = 5
NOTIFY_DELAY    = 0.1
TZ              = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "3"))))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

if not YANDEX_GEO_KEY:
    log.warning("YANDEX_GEOCODER_KEY не задан! Геокодирование работать не будет.")

geolocator = Yandex(api_key=YANDEX_GEO_KEY) if YANDEX_GEO_KEY else None

# ══════════════ ТАРИФЫ ══════════════
TARIFF_DATA = {
    "standard": ("🚗 Стандарт",            "от 2008 г.", 25, 40),
    "comfort":  ("🚙 Комфорт",              "от 2015 г.", 34, 50),
    "comfort+": ("✨ Комфорт+",             "от 2019 г.", 40, 58),
    "minivan":  ("🚐 Минивэн / Компактвэн", "",           45, 65),
    "business": ("💼 Бизнес",               "от 2018 г.", 60, 80),
}

def _tariff_dict(nt=False):
    idx = 3 if nt else 2
    return {k: {"label": v[0], "year": v[1], "price": v[idx]} for k, v in TARIFF_DATA.items()}

TARIFFS_RF = _tariff_dict(False)
TARIFFS_NT = _tariff_dict(True)

NT_KW = ["лнр","днр","луганск","донецк","крым","симферополь","севастополь","херсон","запорожье","мариуполь","мелитополь"]
SUBS = {
    "60":  {"days": 60,  "price": 650,  "label": "60 дней — 650 ₽"},
    "120": {"days": 120, "price": 1100, "label": "120 дней — 1 100 ₽"},
    "240": {"days": 240, "price": 2000, "label": "240 дней — 2 000 ₽"},
    "365": {"days": 365, "price": 3500, "label": "1 год — 3 500 ₽"},
}
COMFORT_H  = ["standard", "comfort", "comfort+", "business"]
CLASS_DESC = {
    "standard": "Практичные авто (класс B/C), кондиционер, хорошее состояние.",
    "comfort":  "Просторные авто C-класса, климат-контроль, плавный ход.",
    "comfort+": "Седаны D-класса, кожаный салон, тишина в салоне.",
    "minivan":  "7–8 мест, большой багаж. Для групп и трансферов.",
    "business": "Премиум E-класс, кожа Nappa, представительский уровень.",
}
LOW_BRANDS   = ["lada", "datsun", "ravon"]
ALLOWED_COLS = {"status","driver_id","taken_at","completed_at","distance_km","price","channel_msg_id"}
STATUS_ICON  = {
    "open":      "🟢 Открыт",
    "taken":     "🔵 Принят",
    "completed": "✅ Завершён",
    "cancelled": "🔴 Отменён",
    "pending":   "⏳ Расчёт...",
}

# ══════════════ FSM ══════════════
class OrderForm(StatesGroup):
    from_city = State()
    to_city = State()
    trip_date = State()
    trip_time = State()
    passengers = State()
    car_class = State()
    wishes = State()

class DriverRegForm(StatesGroup):
    share_contact = State()
    car_model = State()
    car_year = State()
    car_number = State()
    car_class = State()

class AdminEditForm(StatesGroup):
    waiting_input = State()

# ══════════════ БД ══════════════
class DB:
    @staticmethod
    @contextmanager
    def conn():
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def init():
        with DB.conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS drivers(
                    user_id INTEGER PRIMARY KEY, name TEXT, first_name TEXT, last_name TEXT,
                    car_model TEXT, car_year INTEGER, car_number TEXT, car_class TEXT,
                    car_class_label TEXT, phone TEXT, username TEXT, profile_link TEXT,
                    has_photo INTEGER DEFAULT 0, docs_verified INTEGER DEFAULT 0, registered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS subscriptions(user_id INTEGER PRIMARY KEY, expires_date TEXT);
                CREATE TABLE IF NOT EXISTS orders(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, passenger_id INTEGER,
                    from_city TEXT, to_city TEXT, trip_date TEXT, trip_time TEXT,
                    passengers INTEGER, car_class TEXT, wishes TEXT,
                    distance_km REAL, price INTEGER, status TEXT DEFAULT 'pending',
                    created_at TEXT, driver_id INTEGER, taken_at TEXT, completed_at TEXT, channel_msg_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS blacklist(user_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS pending_subscriptions(user_id INTEGER PRIMARY KEY, plan_key TEXT);
                CREATE TABLE IF NOT EXISTS subscription_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_key TEXT,
                    admin_id INTEGER, action TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS ratings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, driver_id INTEGER,
                    passenger_id INTEGER, stars INTEGER CHECK(stars BETWEEN 1 AND 5), created_at TEXT,
                    FOREIGN KEY(order_id) REFERENCES orders(id),
                    FOREIGN KEY(driver_id) REFERENCES drivers(user_id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_passenger ON orders(passenger_id);
                CREATE INDEX IF NOT EXISTS idx_orders_driver    ON orders(driver_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_ratings_driver   ON ratings(driver_id);
                CREATE INDEX IF NOT EXISTS idx_ratings_order    ON ratings(order_id);
            """)
        log.info("✅ БД готова (SQLite)")
        with DB.conn() as c:
            c.executescript("""
                UPDATE drivers SET car_model   = '—' WHERE car_model   IS NULL;
                UPDATE drivers SET car_number  = '—' WHERE car_number  IS NULL;
                UPDATE drivers SET phone       = '—' WHERE phone       IS NULL;
                UPDATE drivers SET car_class   = 'standard' WHERE car_class IS NULL;
                UPDATE drivers SET car_class_label = '🚗 Стандарт' WHERE car_class_label IS NULL;
            """)

    # ── ВОДИТЕЛИ ──
    @staticmethod
    def driver(uid):
        with DB.conn() as c:
            r = c.execute("SELECT * FROM drivers WHERE user_id=?", (uid,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def driver_save(uid, d):
        with DB.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO drivers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, d.get("name"), d.get("first_name"), d.get("last_name"),
                 d.get("car_model"), d.get("car_year"), d.get("car_number"),
                 d.get("car_class"), d.get("car_class_label"), d.get("phone"),
                 d.get("username"), d.get("profile_link"),
                 1 if d.get("has_photo") else 0,
                 1 if d.get("docs_verified") else 0,
                 d.get("registered_at", now_iso()))
            )

    @staticmethod
    def driver_update_fields(uid, **fields):
        allowed = {"car_model","car_year","car_number","car_class","car_class_label"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields: return
        sets = ", ".join(f"{k}=?" for k in fields)
        with DB.conn() as c:
            c.execute(f"UPDATE drivers SET {sets} WHERE user_id=?", list(fields.values()) + [uid])

    @staticmethod
    def driver_del(uid):
        with DB.conn() as c:
            rows = c.execute(
                "SELECT id, passenger_id FROM orders WHERE driver_id=? AND status='taken'", (uid,)
            ).fetchall()
            active_orders = [(r["id"], r["passenger_id"]) for r in rows]
        with DB.conn() as c:
            c.execute("UPDATE ratings SET driver_id=NULL WHERE driver_id=?", (uid,))
            for tbl in ("drivers","subscriptions","pending_subscriptions"):
                c.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
            c.execute("UPDATE orders SET status='open',driver_id=NULL,taken_at=NULL "
                      "WHERE driver_id=? AND status='taken'", (uid,))
        return active_orders

    @staticmethod
    def driver_verify(uid, v):
        with DB.conn() as c:
            c.execute("UPDATE drivers SET docs_verified=? WHERE user_id=?", (1 if v else 0, uid))

    @staticmethod
    def all_drivers():
        with DB.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM drivers").fetchall()]

    @staticmethod
    def active_drivers():
        today = now_dt().date().strftime("%Y-%m-%d")
        with DB.conn() as c:
            rows = c.execute("""
                SELECT d.* FROM drivers d
                JOIN subscriptions s ON s.user_id=d.user_id
                WHERE d.docs_verified=1 AND s.expires_date>=?
                AND d.user_id NOT IN (SELECT user_id FROM blacklist)
            """, (today,)).fetchall()
        return [dict(r) for r in rows]

    # ── ПОДПИСКИ ──
    @staticmethod
    def sub_info(uid):
        with DB.conn() as c:
            r = c.execute("SELECT expires_date FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
        if not r: return None, 0, False
        try:
            exp  = datetime.strptime(r["expires_date"], "%Y-%m-%d").date()
            days = max(0, (exp - now_dt().date()).days)
            return r["expires_date"], days, days > 0
        except:
            return r["expires_date"], 0, False

    @staticmethod
    def sub_set(uid, exp, admin_id=None, plan_key=None):
        with DB.conn() as c:
            c.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?)", (uid, exp))
            if admin_id and plan_key:
                c.execute("INSERT INTO subscription_log (user_id,plan_key,admin_id,action,created_at) "
                          "VALUES (?,?,?,?,?)", (uid, plan_key, admin_id, "activate", now_iso()))

    @staticmethod
    def sub_expire(uid):
        with DB.conn() as c:
            c.execute("DELETE FROM subscriptions WHERE user_id=?", (uid,))

    # ── ЗАКАЗЫ ──
    @staticmethod
    def order_create(data):
        with DB.conn() as c:
            cur = c.execute(
                "INSERT INTO orders (passenger_id,from_city,to_city,trip_date,trip_time,"
                "passengers,car_class,wishes,distance_km,price,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["passenger_id"], data["from_city"], data["to_city"], data["trip_date"],
                 data["trip_time"], data["passengers"], data["car_class"], data.get("wishes",""),
                 data.get("distance_km"), data.get("price"), data.get("status","pending"), now_iso())
            )
            return cur.lastrowid

    @staticmethod
    def order(oid):
        with DB.conn() as c:
            r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return dict(r) if r else None

    @staticmethod
    def order_upd(oid, **kw):
        if not kw: return
        sets = ", ".join(f"{k}=?" for k in kw)
        with DB.conn() as c:
            c.execute(f"UPDATE orders SET {sets} WHERE id=?", list(kw.values()) + [oid])

    @staticmethod
    def order_cancel_atomic(oid, uid, role="passenger"):
        with DB.conn() as c:
            if role == "passenger":
                cur = c.execute("UPDATE orders SET status='cancelled' "
                                "WHERE id=? AND passenger_id=? AND status IN ('open','taken','pending')", (oid, uid))
            else:
                cur = c.execute("UPDATE orders SET status='open',driver_id=NULL,taken_at=NULL "
                                "WHERE id=? AND driver_id=? AND status='taken'", (oid, uid))
            return cur.rowcount > 0

    @staticmethod
    def order_take_atomic(oid, uid):
        with DB.conn() as c:
            cur = c.execute("UPDATE orders SET status='taken',driver_id=?,taken_at=? "
                            "WHERE id=? AND status='open'", (uid, now_iso(), oid))
            return cur.rowcount > 0

    @staticmethod
    def passenger_orders(uid, limit=5):
        with DB.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE passenger_id=? ORDER BY created_at DESC LIMIT ?", (uid, limit)).fetchall()]

    @staticmethod
    def driver_orders(uid, limit=5):
        with DB.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE driver_id=? ORDER BY taken_at DESC LIMIT ?", (uid, limit)).fetchall()]

    @staticmethod
    def open_orders():
        with DB.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE status='open' ORDER BY created_at DESC").fetchall()]

    @staticmethod
    def all_orders(limit=10, offset=0, status=None):
        with DB.conn() as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, limit, offset)).fetchall()
                total = c.execute("SELECT COUNT(*) FROM orders WHERE status=?", (status,)).fetchone()[0]
            else:
                rows = c.execute(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
                total = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        return [dict(r) for r in rows], total

    @staticmethod
    def stats():
        today = now_dt().date().strftime("%Y-%m-%d")
        with DB.conn() as c:
            t   = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            o   = c.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]
            d   = c.execute("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0]
            dr  = c.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]
            dc  = c.execute("SELECT COUNT(*) FROM drivers WHERE docs_verified=1").fetchone()[0]
            sub = c.execute("""
                SELECT COUNT(*) FROM drivers d
                JOIN subscriptions s ON s.user_id = d.user_id
                WHERE s.expires_date >= ?""", (today,)).fetchone()[0]
        return {"total": t, "open": o, "done": d, "drivers": dr, "docs_ok": dc, "subscribed": sub}

    # ── ЧЁРНЫЙ СПИСОК ──
    @staticmethod
    def bl_check(uid):
        with DB.conn() as c:
            return c.execute("SELECT 1 FROM blacklist WHERE user_id=?", (uid,)).fetchone() is not None

    @staticmethod
    def bl_add(uid):
        with DB.conn() as c:
            c.execute("INSERT OR IGNORE INTO blacklist VALUES (?)", (uid,))

    @staticmethod
    def bl_remove(uid):
        with DB.conn() as c:
            c.execute("DELETE FROM blacklist WHERE user_id=?", (uid,))

    @staticmethod
    def bl_all():
        with DB.conn() as c:
            return [r[0] for r in c.execute("SELECT user_id FROM blacklist").fetchall()]

    # ── ОЖИДАЮЩИЕ ПОДПИСКИ ──
    @staticmethod
    def pending_set(uid, pk):
        with DB.conn() as c:
            c.execute("INSERT OR REPLACE INTO pending_subscriptions VALUES (?,?)", (uid, pk))

    @staticmethod
    def pending_get(uid):
        with DB.conn() as c:
            r = c.execute("SELECT plan_key FROM pending_subscriptions WHERE user_id=?", (uid,)).fetchone()
        return r["plan_key"] if r else None

    @staticmethod
    def pending_del(uid, admin_id=None, plan_key=None):
        with DB.conn() as c:
            c.execute("DELETE FROM pending_subscriptions WHERE user_id=?", (uid,))
            if admin_id and plan_key:
                c.execute("INSERT INTO subscription_log (user_id,plan_key,admin_id,action,created_at) "
                          "VALUES (?,?,?,?,?)", (uid, plan_key, admin_id, "reject", now_iso()))

    @staticmethod
    def pending_all():
        with DB.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pending_subscriptions").fetchall()]

    # ── РЕЙТИНГИ ──
    @staticmethod
    def add_rating(order_id, driver_id, passenger_id, stars):
        with DB.conn() as c:
            existing = c.execute("SELECT id FROM ratings WHERE order_id=? AND passenger_id=?",
                                 (order_id, passenger_id)).fetchone()
            if existing:
                c.execute("UPDATE ratings SET stars=?,driver_id=?,created_at=? "
                          "WHERE order_id=? AND passenger_id=?",
                          (stars, driver_id, now_iso(), order_id, passenger_id))
            else:
                c.execute("INSERT INTO ratings (order_id,driver_id,passenger_id,stars,created_at) "
                          "VALUES (?,?,?,?,?)", (order_id, driver_id, passenger_id, stars, now_iso()))

    @staticmethod
    def avg_rating(driver_id):
        with DB.conn() as c:
            r = c.execute("SELECT AVG(stars) as avg, COUNT(*) as cnt FROM ratings WHERE driver_id=?",
                          (driver_id,)).fetchone()
        return (round(r["avg"], 1), r["cnt"]) if r and r["cnt"] > 0 else (0.0, 0)

    @staticmethod
    def has_rating(order_id, passenger_id):
        with DB.conn() as c:
            return c.execute("SELECT 1 FROM ratings WHERE order_id=? AND passenger_id=?",
                             (order_id, passenger_id)).fetchone() is not None


# ══════════════ УТИЛИТЫ ══════════════
def now_dt():  return datetime.now(TZ)
def now_iso(): return now_dt().isoformat()

def esc(value, default="—") -> str:
    if value is None or str(value).strip() == "":
        return default
    return html.escape(str(value))

MIN_CAR_YEAR = 2008

def _validate_car_year(year: int):
    current = now_dt().year
    if not (MIN_CAR_YEAR <= year <= current + 1):
        return f"❌ Год должен быть от {MIN_CAR_YEAR} до {current + 1}"
    return None

def drv_name(d):
    if not d: return "Водитель"
    n = f"{d.get('first_name') or d.get('name') or ''} {d.get('last_name') or ''}".strip()
    return html.escape(n) if n else "Водитель"

def profile_link(d):
    if not d: return "—"
    p = d.get("profile_link", "")
    if p and p.startswith("https://"): return p
    u = d.get("username", "")
    if u and not u.startswith("tg://"): return f"https://t.me/{u.lstrip('@')}"
    uid = d.get("user_id")
    return f"tg://user?id={uid}" if uid else "—"

def check_brand(model, cl):
    if cl == "standard": return None
    for b in LOW_BRANDS:
        if b in model.lower():
            return f"❌ {model} — только Стандарт"
    return None

def is_nt(city):    return any(kw in city.lower() for kw in NT_KW)
def tariffs(cf="", ct=""): return TARIFFS_NT if (is_nt(cf) or is_nt(ct)) else TARIFFS_RF
def calc_price(dist, cc, cf, ct):
    return round(dist * tariffs(cf, ct).get(cc, tariffs(cf, ct)["standard"])["price"])
def fmt_price(p): return f"{p:,}".replace(",", " ") + " ₽"
def is_valid_city(c):
    return bool(c) and len(c.strip()) >= 2 and bool(re.match(r"^[а-яА-ЯёЁa-zA-Z\s\-\.]+$", c.strip()))

def geocode(city):
    if not geolocator or not is_valid_city(city): return None
    try:
        loc = geolocator.geocode(city, timeout=5)
        if loc: return loc.latitude, loc.longitude
    except Exception as e:
        log.error(f"Геокодирование {city}: {e}")
    return None

def get_distance(cf, ct):
    c1, c2 = geocode(cf), geocode(ct)
    return geo_dist(c1, c2).km if c1 and c2 else None

def can_take_order(drv, order):
    dc, oc = drv.get("car_class","standard"), order.get("car_class","standard")
    if oc == "minivan":
        return (True,"") if dc == "minivan" else (False,"🔒 Заказы минивэн — только для минивэнов")
    if oc == "business":
        return (True,"") if dc == "business" else (False,"🔒 Заказы бизнес-класса — только для бизнес-авто")
    if dc == "minivan":
        return (True,"") if oc in ["standard","comfort","comfort+"] else (False,f"🔒 Минивэн не может взять {oc}")
    if dc in COMFORT_H and oc in COMFORT_H:
        return (True,"") if COMFORT_H.index(dc) >= COMFORT_H.index(oc) \
               else (False,f"🔒 Ваш класс ниже требуемого")
    return False, "🔒 Неизвестный класс"

def fmt_order(o, show_price=True):
    t  = tariffs(o.get("from_city",""), o.get("to_city",""))
    cc = o.get("car_class","standard")
    cl = esc(t.get(cc,{}).get("label",cc))
    dist = o.get("distance_km")
    dt = (f"{dist:.0f} км" if dist
          else ("⚠️ Не рассчитано" if o.get("status") in ("open","pending") else "уточняется"))
    p  = o.get("price")
    nt = is_nt(o.get("from_city","")) or is_nt(o.get("to_city",""))
    lines = [
        f"🚕 <b>Заказ #{o['id']}</b> · {'🆕 НТ' if nt else '🇷🇺 РФ'}",
        f"📍 {esc(o.get('from_city'))} → {esc(o.get('to_city'))}",
        f"📏 {dt} | 📅 {o.get('trip_date','—')} | 🕐 {o.get('trip_time','—')}",
        f"👥 {o.get('passengers','—')} чел. | 🚘 {cl}",
    ]
    if show_price and p:
        lines.append(f"💰 {t.get(cc,{}).get('price',0)} ₽/км | 💵 <b>{fmt_price(p)}</b>")
    elif show_price:
        lines.append("💵 <i>Стоимость уточняется</i>")
    lines.append("⚠️ Платные дороги — отдельно")
    if w := o.get("wishes"):
        lines.append(f"💬 {esc(w)}")
    lines.append(f"📌 {STATUS_ICON.get(o.get('status','open'),'—')}")
    return "\n".join(lines)


# ══════════════ КЛАВИАТУРЫ ══════════════
def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚕 Создать заказ"), KeyboardButton(text="🚗 Я водитель")],
            [KeyboardButton(text="📋 Мои заказы"),    KeyboardButton(text="📊 Тарифы")],
        ],
        resize_keyboard=True,
    )

def kb_driver(uid):
    _, _, active = DB.sub_info(uid)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Доступные заказы"), KeyboardButton(text="👤 Мой профиль")],
            [KeyboardButton(text="💳 Абонемент"),
             KeyboardButton(text="✅ Подписка активна" if active else "❌ Нет подписки")],
            [KeyboardButton(text="📈 Мои поездки"),      KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )

def kb_cancel():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True,
    )

def kb_car_class(region="rf", passengers=1):
    t = TARIFFS_NT if region == "new" else TARIFFS_RF
    classes = ["minivan"] if passengers >= 5 else ["standard","comfort","comfort+","business","minivan"]
    rows = []
    for cc in classes:
        if cc in t:
            txt = f"{t[cc]['label']} · {t[cc]['price']} ₽/км"
            if cc == "minivan" and passengers >= 5:
                txt += " ✅ (рекомендуется)"
            rows.append([InlineKeyboardButton(text=txt, callback_data=f"car_{cc}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pclass():
    rows = []
    for k in COMFORT_H + ["minivan"]:
        v  = TARIFFS_RF[k]
        yr = f" ({v['year']})" if v.get("year") else ""
        rows.append([InlineKeyboardButton(text=f"{v['label']}{yr}", callback_data=f"pclass_{k}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_passengers():
    btns = [InlineKeyboardButton(text=str(i), callback_data=f"pax_{i}") for i in range(1,9)]
    rows = [btns[i:i+4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_subs():
    rows = [[InlineKeyboardButton(text=f"💳 {p['label']}", callback_data=f"sub_{k}")] for k, p in SUBS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_driver")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_stars(oid):
    btns = [InlineKeyboardButton(text=f"{i}⭐", callback_data=f"rate_{oid}_{i}") for i in range(1,6)]
    return InlineKeyboardMarkup(inline_keyboard=[btns])


# ══════════════ BOT / DISPATCHER ══════════════
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp  = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ══════════════ ХЕЛПЕРЫ ОТПРАВКИ ══════════════
async def safe_send(cid, text, **kw):
    for attempt in range(3):
        try:
            return await bot.send_message(cid, text, **kw)
        except TelegramRetryAfter as e:
            log.warning(f"Flood control {cid}: ждём {e.retry_after} сек.")
            await asyncio.sleep(e.retry_after + 0.5)
        except Exception as e:
            log.error(f"Ошибка отправки {cid}: {e}")
            return None
    return None

async def safe_edit_markup(cid, mid, **kw):
    try:
        await bot.edit_message_reply_markup(cid, mid, **kw)
    except Exception as e:
        log.error(f"Ошибка редактирования разметки: {e}")

async def notify_driver_change(tgt, label, value):
    await safe_send(tgt, f"ℹ️ <b>Администратор изменил {label}:</b> {html.escape(str(value))}")

# ══════════════ MIDDLEWARE ЧЁРНОГО СПИСКА ══════════════
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter
class BlacklistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid = event.from_user.id if hasattr(event, "from_user") else None
        if uid and DB.bl_check(uid):
            await safe_send(uid, "⛔ Вы заблокированы.")
            return
        return await handler(event, data)

dp.update.middleware(BlacklistMiddleware())

# ══════════════ КЭШ УВЕДОМЛЕНИЙ ══════════════
_NOTIFIED: dict = {}
_NLOCK = asyncio.Lock()

async def _notify_drivers(oid, exclude_uid=None):
    order = DB.order(oid)
    if not order or order["status"] != "open": return
    async with _NLOCK:
        notified = set(_NOTIFIED.get(oid, set()))
    drivers = [d for d in DB.active_drivers()
               if not (exclude_uid and d["user_id"] == exclude_uid)
               and d["user_id"] != order["passenger_id"]
               and d["user_id"] not in notified]
    sent = 0
    for drv in drivers[:NOTIFY_LIMIT]:
        ct, _ = can_take_order(drv, order)
        if not ct: continue
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять",       callback_data=f"take_{oid}")],
            [InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip_{oid}")],
        ])
        await safe_send(drv["user_id"], f"🔔 <b>Подходит вам!</b>\n\n{fmt_order(order)}", reply_markup=kb)
        async with _NLOCK:
            _NOTIFIED.setdefault(oid, set()).add(drv["user_id"])
        sent += 1
        if sent % NOTIFY_BATCH == 0:
            await asyncio.sleep(NOTIFY_DELAY)

async def _clear_notified(oid):
    async with _NLOCK:
        _NOTIFIED.pop(oid, None)

TRIAL_DAYS = 50  # длительность пробного периода

async def _trial_expiry_notifier():
    """Каждые 12 часов проверяет подписки и уведомляет водителей об окончании."""
    while True:
        try:
            await asyncio.sleep(12 * 3600)
            today      = now_dt().date()
            warn3      = (today + timedelta(days=3)).strftime("%Y-%m-%d")
            warn1      = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            expired    = today.strftime("%Y-%m-%d")
            drivers    = DB.all_drivers()
            for drv in drivers:
                uid = drv["user_id"]
                exp_str, dl, active = DB.sub_info(uid)
                if not exp_str:
                    continue
                if exp_str == warn3:
                    await safe_send(uid,
                        f"⚠️ <b>Подписка заканчивается через 3 дня</b> ({exp_str})!\n\n"
                        f"Продлите абонемент чтобы продолжать получать заказы.",
                        reply_markup=kb_subs())
                elif exp_str == warn1:
                    await safe_send(uid,
                        f"🔔 <b>Подписка заканчивается завтра</b> ({exp_str})!\n\n"
                        f"Продлите абонемент чтобы не потерять доступ к заказам.",
                        reply_markup=kb_subs())
                elif exp_str < expired and not active:
                    await safe_send(uid,
                        f"❌ <b>Ваша подписка истекла.</b>\n\n"
                        f"Оформите абонемент чтобы снова получать заказы.",
                        reply_markup=kb_subs())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"_trial_expiry_notifier: {e}")

async def _notified_cleaner():
    while True:
        try:
            await asyncio.sleep(600)
            async with _NLOCK:
                current_oids = list(_NOTIFIED.keys())
            if not current_oids: continue
            try:
                placeholders = ",".join("?" * len(current_oids))
                with DB.conn() as c:
                    rows = c.execute(
                        f"SELECT id, status FROM orders WHERE id IN ({placeholders})", current_oids).fetchall()
                alive = {r["id"] for r in rows if r["status"] == "open"}
                dead  = [oid for oid in current_oids if oid not in alive]
            except Exception as e:
                log.error(f"_notified_cleaner: {e}"); continue
            if dead:
                async with _NLOCK:
                    for oid in dead:
                        _NOTIFIED.pop(oid, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"_notified_cleaner упала: {e}", exc_info=True)

# ══════════════ КАНАЛ ══════════════
async def _post_to_channel(oid):
    if not GROUP_CHAT_ID: return
    order = DB.order(oid)
    if not order or order["status"] != "open": return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Взять заказ",
                             url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}")
    ]])
    msg = await safe_send(GROUP_CHAT_ID, fmt_order(order), reply_markup=kb)
    if msg: DB.order_upd(oid, channel_msg_id=msg.message_id)

async def update_channel_post(oid):
    order = DB.order(oid)
    if not order or not order.get("channel_msg_id") or not GROUP_CHAT_ID: return
    status = order["status"]
    if status == "taken":
        status_line = "🚗 <b>Водитель найден</b>"
    elif status == "completed":
        status_line = "✅ <b>Выполнен</b>"
    elif status == "cancelled":
        status_line = "❌ <b>Отменён</b>"
    else:
        status_line = None

    if status_line:
        try:
            await bot.edit_message_text(
                fmt_order(order) + f"\n\n{status_line}",
                GROUP_CHAT_ID, order["channel_msg_id"],
                reply_markup=None)  # убираем кнопку «Взять»
        except Exception as e:
            log.error(f"Ошибка обновления поста: {e}")
    elif status == "open":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Взять заказ",
                                 url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}")
        ]])
        try:
            await bot.edit_message_text(fmt_order(order), GROUP_CHAT_ID,
                                        order["channel_msg_id"], reply_markup=kb)
        except Exception as e:
            log.error(f"Ошибка обновления поста: {e}")


# ══════════════ КОМАНДЫ ══════════════
@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    parts = msg.text.strip().split()
    if len(parts) > 1 and parts[1].startswith("order_"):
        try:
            oid   = int(parts[1].split("_")[1])
            order = DB.order(oid)
            if not order or order["status"] != "open":
                await safe_send(uid, "⚠️ Заказ не найден или уже взят.", reply_markup=kb_main()); return
            drv = DB.driver(uid)
            if not drv:
                await safe_send(uid, "❌ Сначала зарегистрируйтесь как водитель.", reply_markup=kb_main()); return
            _, _, active = DB.sub_info(uid)
            if not active:
                await safe_send(uid, "🔒 Нет абонемента.", reply_markup=kb_driver(uid)); return
            if not drv.get("docs_verified"):
                await safe_send(uid, "⏳ Профиль ещё не верифицирован.", reply_markup=kb_driver(uid)); return
            if order["passenger_id"] == uid:
                await safe_send(uid, "❌ Нельзя взять свой заказ.", reply_markup=kb_driver(uid)); return
            ct, rsn = can_take_order(drv, order)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Взять заказ", callback_data=f"take_{oid}")
                if ct else InlineKeyboardButton(text=f"🔒 {rsn}", callback_data="cant_take")
            ]])
            await state.clear()
            await safe_send(uid, f"🔗 <b>Заказ из канала:</b>\n\n{fmt_order(order)}", reply_markup=kb)
            return
        except Exception as e:
            log.error(f"Deep-link: {e}")
    await state.clear()
    await safe_send(uid,
        f"👋 <b>{html.escape(msg.from_user.first_name or 'друг')}</b>, "
        f"добро пожаловать в Межгород Трансфер Россия!\n\n"
        "🔹 Пассажирам — оформить заказ\n"
        "🔹 Водителям — получать заказы\n"
        "📢 Канал: @intercitytrans",
        reply_markup=kb_main())

@router.message(Command("help"))
async def cmd_help(msg: Message):
    await safe_send(msg.chat.id,
        "📖 <b>Справка</b>\n\n"
        "<b>Пассажир:</b> Создать заказ, Мои заказы\n"
        "<b>Водитель:</b> Я водитель → Зарегистрироваться → Абонемент\n\n"
        "По вопросам: @Olegan7979")

@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d)]
        for t, d in [("📋 Заказы","adm_orders"),("👥 Водители","adm_drivers"),
                     ("⛔ ЧС","adm_bl"),("💳 Оплаты","adm_subs"),
                     ("📊 Статистика","adm_stats"),("📖 Команды","adm_help")]
    ])
    await safe_send(msg.chat.id, "🔧 <b>Админ-панель</b>", reply_markup=kb)

@router.message(Command("ban"))
async def cmd_ban(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        t = int(msg.text.split()[1])
        DB.bl_add(t)
        await msg.reply(f"⛔ {t} в ЧС")
        await safe_send(t, "⛔ Вы заблокированы.")
    except:
        await msg.reply("⚠️ /ban ID")

@router.message(Command("unban"))
async def cmd_unban(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        t = int(msg.text.split()[1]); DB.bl_remove(t)
        await msg.reply(f"✅ {t} разблокирован")
    except:
        await msg.reply("⚠️ /unban ID")

@router.message(Command("unsub"))
async def cmd_unsub(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        t = int(msg.text.split()[1]); DB.sub_expire(t)
        await msg.reply(f"✅ Подписка {t} аннулирована")
        await safe_send(t, "⛔ Абонемент аннулирован.")
    except:
        await msg.reply("⚠️ /unsub ID")

@router.message(Command("deldriver"))
async def cmd_deldrv(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        t   = int(msg.text.split()[1])
        drv = DB.driver(t)
        if not drv: await msg.reply("❌ Не найден"); return
        active_orders = DB.driver_del(t)
        await msg.reply(f"✅ {drv_name(drv)} удалён")
        await safe_send(t, "🗑 Ваш профиль водителя удалён.")
        for oid, pid in active_orders:
            if pid:
                await safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nЗаказ #{oid} снова открыт.")
            await update_channel_post(oid)
            await _notify_drivers(oid)
    except:
        await msg.reply("⚠️ /deldriver ID")

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    s = DB.stats()
    await safe_send(msg.chat.id,
        f"📊 Заказов: {s['total']} | Открыто: {s['open']} | Завершено: {s['done']}\n"
        f"🚗 Водителей: {s['drivers']} | Подписка: {s['subscribed']} | Проверены: {s['docs_ok']}")

@router.message(Command("recalc"))
async def cmd_recalc(msg: Message):
    if msg.from_user.id not in ADMIN_IDS: return
    try:
        _, oid, dkm, price = msg.text.split()
        oid, dkm, price = int(oid), float(dkm), int(price)
        order = DB.order(oid)
        if not order: await safe_send(msg.chat.id, "❌ Заказ не найден"); return
        DB.order_upd(oid, distance_km=dkm, price=price, status="open")
        await safe_send(msg.chat.id, f"✅ Заказ #{oid} пересчитан")
        if order["passenger_id"]:
            await safe_send(order["passenger_id"],
                f"✅ <b>Заказ #{oid} рассчитан!</b>\n\n{fmt_order(DB.order(oid))}")
        await _post_to_channel(oid)
        await _notify_drivers(oid)
    except Exception as e:
        await safe_send(msg.chat.id, f"❌ {e}\nФормат: /recalc order_id distance_km price")


# ══════════════ МЕНЮ (кнопки) ══════════════
@router.message(F.text == "🔙 Главное меню")
async def to_main(msg: Message, state: FSMContext):
    await state.clear()
    await safe_send(msg.chat.id, "🏠 Главное меню", reply_markup=kb_main())

@router.message(F.text == "📊 Тарифы")
async def show_tariffs(msg: Message):
    lines = ["💰 <b>Тарифы · РФ</b> (₽/км)"]
    for v in TARIFFS_RF.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b>")
    lines.append("\n💰 <b>Тарифы · Новые территории</b>")
    for v in TARIFFS_NT.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b>")
    lines.append("\n⚠️ Платные дороги — отдельно\n🚫 Торг запрещён")
    await safe_send(msg.chat.id, "\n".join(lines))


# ══════════════ СОЗДАНИЕ ЗАКАЗА (FSM) ══════════════
@router.message(F.text == "🚕 Создать заказ")
async def order_start(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    recent = DB.passenger_orders(uid, limit=1)
    if recent and recent[0]["status"] in ("open","taken","pending"):
        await safe_send(uid, "⚠️ У вас уже есть активный заказ. Отмените его перед созданием нового.",
                        reply_markup=kb_main()); return
    cur = await state.get_state()
    if cur is not None:
        await safe_send(uid, "⚠️ У вас уже есть активное создание заказа. Завершите или нажмите «❌ Отменить»",
                        reply_markup=kb_cancel()); return
    await state.set_state(OrderForm.from_city)
    await safe_send(uid, "🗺 <b>Шаг 1/7</b>\n📍 Введите город отправления\n<i>Например: Москва</i>",
                    reply_markup=kb_cancel())

@router.message(StateFilter("*"), F.text == "❌ Отменить")
async def cancel_all(msg: Message, state: FSMContext):
    await state.clear()
    await safe_send(msg.from_user.id, "❌ Отменено.", reply_markup=kb_main())

# Шаг 1
@router.message(OrderForm.from_city)
async def step_from(msg: Message, state: FSMContext):
    uid, city = msg.from_user.id, msg.text.strip()
    if not is_valid_city(city):
        await safe_send(uid, "❌ Некорректное название"); return
    await state.update_data(from_city=city)
    await state.set_state(OrderForm.to_city)
    await safe_send(uid, f"✅ Откуда: <b>{esc(city)}</b>\n\n🗺 <b>Шаг 2/7</b>\n🏁 Введите город назначения")

# Шаг 2
@router.message(OrderForm.to_city)
async def step_to(msg: Message, state: FSMContext):
    uid, city = msg.from_user.id, msg.text.strip()
    if not is_valid_city(city):
        await safe_send(uid, "❌ Некорректное название"); return
    data = await state.get_data()
    from_city = data.get("from_city")
    if not from_city:
        await state.clear()
        await safe_send(uid, "⚠️ Данные устарели. Начните заново.", reply_markup=kb_main()); return
    if from_city.lower() == city.lower():
        await safe_send(uid, "❌ Города не должны совпадать"); return
    await state.update_data(to_city=city)
    await state.set_state(OrderForm.trip_date)
    await safe_send(uid, f"✅ Куда: <b>{esc(city)}</b>\n\n🗺 <b>Шаг 3/7</b>\n📅 Дата (ДД.ММ.ГГГГ)")

# Шаг 3
@router.message(OrderForm.trip_date)
async def step_date(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    try:
        d = datetime.strptime(msg.text.strip(), "%d.%m.%Y")
        if d.date() < now_dt().date():
            await safe_send(uid, "❌ Дата не может быть в прошлом"); return
        await state.update_data(trip_date=d.strftime("%d.%m.%Y"))
        await state.set_state(OrderForm.trip_time)
        await safe_send(uid, f"✅ Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n🗺 <b>Шаг 4/7</b>\n🕐 Время (ЧЧ:ММ)")
    except:
        await safe_send(uid, "❌ Формат ДД.ММ.ГГГГ")

# Шаг 4
@router.message(OrderForm.trip_time)
async def step_time(msg: Message, state: FSMContext):
    uid, t = msg.from_user.id, msg.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", t):
        await safe_send(uid, "❌ Формат ЧЧ:ММ"); return
    try:
        h, m = map(int, t.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            await safe_send(uid, "❌ Некорректное время"); return
        data = await state.get_data()
        if ds := data.get("trip_date"):
            trip_dt = datetime.strptime(f"{ds} {t}", "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
            if trip_dt < now_dt():
                await safe_send(uid, "❌ Поездка не может быть в прошлом"); return
        await state.update_data(trip_time=f"{h:02d}:{m:02d}")
        await state.set_state(OrderForm.passengers)
        await safe_send(uid, f"✅ Время: <b>{h:02d}:{m:02d}</b>\n\n🗺 <b>Шаг 5/7</b>\n👥 Пассажиры:",
                        reply_markup=kb_passengers())
    except:
        await safe_send(uid, "❌ Некорректное время")

# Шаг 5 (колбэк)
@router.callback_query(StateFilter(OrderForm.passengers), F.data.startswith("pax_"))
async def cb_pax_fsm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    passengers = int(call.data.split("_")[1])
    await state.update_data(passengers=passengers)
    data = await state.get_data()
    region = "new" if (is_nt(data.get("from_city","")) or is_nt(data.get("to_city",""))) else "rf"
    await state.update_data(region=region)
    await state.set_state(OrderForm.car_class)
    hint = "\n\n⚠️ <b>Для 5 и более пассажиров доступен только минивэн!</b>" if passengers >= 5 else ""
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Пассажиров: <b>{passengers}</b>")
    await safe_send(uid,
        f"🗺 <b>Шаг 6/7</b> — {'🆕 НТ' if region=='new' else '🇷🇺 РФ'}\n👥 Пассажиров: {passengers}{hint}",
        reply_markup=kb_car_class(region, passengers))
    await call.answer()

# Шаг 6 (колбэк)
@router.callback_query(StateFilter(OrderForm.car_class), F.data.startswith("car_"))
async def cb_car_fsm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    cc = call.data.split("_", 1)[1]
    data = await state.get_data()
    t = TARIFFS_NT if data.get("region") == "new" else TARIFFS_RF
    await state.update_data(car_class=cc, car_class_label=t[cc]["label"])
    await state.set_state(OrderForm.wishes)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Класс: <b>{t[cc]['label']}</b>")
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Нет"), KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True,
    )
    await safe_send(uid, "🗺 <b>Шаг 7/7</b>\n💬 Пожелания? Нет — нажмите кнопку", reply_markup=kb)
    await call.answer()

# Шаг 7
@router.message(OrderForm.wishes)
async def step_wishes(msg: Message, state: FSMContext):
    uid, wish = msg.from_user.id, msg.text.strip()
    if len(wish) > 500:
        await safe_send(uid, "❌ Слишком длинный текст (макс. 500 символов)"); return
    wish = "" if wish.lower() in ["нет","—","-","no"] else wish
    data = await state.get_data()
    await state.clear()
    await safe_send(uid, "⏳ Рассчитываю...")
    asyncio.create_task(_finalize_order_task(uid, {**data, "wishes": wish}))

async def _finalize_order_task(uid, data):
    try:
        dist = await asyncio.get_running_loop().run_in_executor(
            None, get_distance, data["from_city"], data["to_city"])
        dkm = price = None
        if dist:
            dkm   = round(dist * DIST_COEFF)
            price = calc_price(dkm, data["car_class"], data["from_city"], data["to_city"])
        oid = DB.order_create({**data, "passenger_id": uid,
                               "distance_km": dkm, "price": price,
                               "status": "open" if dkm else "pending"})
        order = DB.order(oid)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"cancel_{oid}")
        ]])
        warn = (
            f"✅ <b>Заказ создан!</b>\n\n{fmt_order(order)}\n\n"
            + ("" if dkm else "⚠️ <b>Расстояние не рассчитано</b>\n\n")
            + "⏳ <i>Пожалуйста, ожидайте — с вами свяжется водитель.</i>\n\n"
            + "🚫 <b>Не переводите предоплату!</b>\n"
            + "Оплата — только водителю после поездки.\n"
            + "<i>Если водитель просит предоплату — сообщите @Olegan7979</i>"
        )
        await safe_send(uid, warn, reply_markup=cancel_kb)
        await safe_send(uid, "✅ Заказ создан! Возвращаемся в меню.", reply_markup=kb_main())
        if dkm:
            await _post_to_channel(oid)
            await _notify_drivers(oid)
        else:
            for aid in ADMIN_IDS:
                await safe_send(aid, f"⚠️ Заказ #{oid} без расстояния!\n"
                                f"{data.get('from_city')} → {data.get('to_city')}\n"
                                f"Используйте /recalc {oid} <distance_km> <price>")
    except Exception as e:
        log.error(f"Ошибка создания заказа: {e}")
        await safe_send(uid, "❌ Ошибка. Попробуйте ещё раз.", reply_markup=kb_main())


# ══════════════ МОИ ЗАКАЗЫ ══════════════
@router.message(F.text == "📋 Мои заказы")
async def my_orders(msg: Message):
    uid = msg.from_user.id
    if DB.driver(uid):
        drv_orders = [o for o in DB.driver_orders(uid) if o["status"] in ("taken","completed")]
        if drv_orders:
            await safe_send(uid, f"🚗 <b>Поездки как водитель</b> ({len(drv_orders)}):")
            for o in drv_orders:
                kb = None
                if o["status"] == "taken":
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="❌ Отказаться", callback_data=f"driver_cancel_{o['id']}")
                    ]])
                await safe_send(uid, fmt_order(o), reply_markup=kb)
    result = DB.passenger_orders(uid)
    if not result: await safe_send(uid, "📋 Нет пассажирских заказов."); return
    await safe_send(uid, f"📋 <b>Ваши заказы как пассажир</b> ({len(result)}):")
    for o in result:
        if o["status"] in ("open","taken","pending"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{o['id']}")
            ]])
            await safe_send(uid, fmt_order(o), reply_markup=kb)
        elif o["status"] == "completed" and o.get("driver_id") and not DB.has_rating(o["id"], uid):
            await safe_send(uid, fmt_order(o) + "\n\n⭐ <b>Оцените поездку:</b>",
                            reply_markup=kb_stars(o["id"]))
        else:
            await safe_send(uid, fmt_order(o))


# ══════════════ ВОДИТЕЛЬ ══════════════
@router.message(F.text == "🚗 Я водитель")
async def driver_enter(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    await state.clear()
    drv = DB.driver(uid)
    if not drv:
        reg_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="👤 Зарегистрироваться")], [KeyboardButton(text="🔙 Главное меню")]],
            resize_keyboard=True,
        )
        await safe_send(uid,
            "🚗 <b>Режим водителя</b>\n\nДля регистрации нажмите кнопку ниже.\n\n"
            "<b>Что потребуется:</b>\n1️⃣ Поделиться контактом\n2️⃣ Марка и модель авто\n"
            "3️⃣ Год выпуска\n4️⃣ Гос. номер\n5️⃣ Класс авто\n\n"
            "После регистрации администратор верифицирует профиль.",
            reply_markup=reg_kb)
        return
    exp, dl, active = DB.sub_info(uid)
    avg, cnt = DB.avg_rating(uid)
    await safe_send(uid,
        f"🚗 <b>{drv_name(drv)}</b>\n"
        f"🚘 {esc(drv.get('car_model'))} {drv.get('car_year','—')} г.\n"
        f"🔢 {esc(drv.get('car_number'))}\n"
        f"🏷 {esc(drv.get('car_class_label'))}\n"
        f"📞 {esc(drv.get('phone'))}\n"
        f"📄 {'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает верификации'}\n"
        f"💳 {'✅ Подписка до '+exp+' ('+str(dl)+' дн.)' if active else '❌ Нет подписки'}\n"
        f"{'⭐ Рейтинг: '+str(avg)+' ('+str(cnt)+' оценок)' if cnt else '⭐ Нет оценок'}",
        reply_markup=kb_driver(uid))

@router.message(F.text == "📦 Доступные заказы")
async def avail_orders(msg: Message):
    uid = msg.from_user.id
    drv = DB.driver(uid)
    if not drv: await safe_send(uid, "❌ Сначала зарегистрируйтесь."); return
    _, _, active = DB.sub_info(uid)
    if not active: await safe_send(uid, "🔒 Нет абонемента."); return
    if not drv.get("docs_verified"): await safe_send(uid, "⏳ Профиль ещё не верифицирован администратором."); return
    all_open = [o for o in DB.open_orders() if o["passenger_id"] != uid]
    if not all_open: await safe_send(uid, "📭 Нет доступных заказов."); return
    can = sum(1 for o in all_open if can_take_order(drv, o)[0])
    await safe_send(uid, f"📦 <b>Открыто: {len(all_open)}</b> | ✅ Доступно: <b>{can}</b>\n"
                    f"🏷 {esc(drv.get('car_class_label'))}")
    for o in all_open[:10]:
        ct, rsn = can_take_order(drv, o)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Взять" if ct else f"🔒 {rsn}",
                                 callback_data=f"take_{o['id']}" if ct else "cant_take")
        ]])
        await safe_send(uid, fmt_order(o), reply_markup=kb)

@router.message(F.text == "📈 Мои поездки")
async def driver_trips(msg: Message):
    uid = msg.from_user.id
    result = [o for o in DB.driver_orders(uid) if o["status"] in ("taken","completed")]
    if not result: await safe_send(uid, "📈 Нет поездок."); return
    await safe_send(uid, f"📈 <b>Поездки</b> ({len(result)}):")
    for o in result:
        kb = None
        if o["status"] == "taken":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отказаться", callback_data=f"driver_cancel_{o['id']}")
            ]])
        await safe_send(uid, fmt_order(o), reply_markup=kb)

@router.message(F.text.in_({"✅ Подписка активна","❌ Нет подписки"}))
async def sub_btn(msg: Message):
    await subscription_menu(msg)


# ══════════════ РЕГИСТРАЦИЯ ВОДИТЕЛЯ (FSM) ══════════════
@router.message(F.text == "👤 Зарегистрироваться")
async def register_driver(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if DB.driver(uid):
        await safe_send(uid, "ℹ️ Вы уже зарегистрированы.", reply_markup=kb_driver(uid)); return
    await state.set_state(DriverRegForm.share_contact)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться контактом", request_contact=True)],
            [KeyboardButton(text="❌ Отменить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await safe_send(uid,
        "📝 <b>Регистрация — Шаг 1/5</b>\n\n"
        "📱 Нажмите кнопку ниже, чтобы поделиться контактом.",
        reply_markup=kb)

@router.message(DriverRegForm.share_contact, F.contact)
async def p_contact(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    contact: Contact = msg.contact
    if contact.user_id != uid:
        await safe_send(uid, "❌ Отправьте свой контакт"); return
    try:
        chat  = await bot.get_chat(uid)
        fn, ln = chat.first_name or "", chat.last_name or ""
        un    = f"@{chat.username}" if chat.username else ""
        hp_r  = await bot.get_user_profile_photos(uid, limit=1)
        hp    = hp_r.total_count > 0
        phone = contact.phone_number or ""
    except Exception as e:
        log.error(f"Профиль {uid}: {e}")
        await safe_send(uid, "❌ Ошибка получения профиля Telegram."); return
    await state.update_data(first_name=fn, last_name=ln, name=fn, username=un,
                            has_photo=hp, phone=phone,
                            profile_link=(f"https://t.me/{un.lstrip('@')}" if un else f"tg://user?id={uid}"))
    await state.set_state(DriverRegForm.car_model)
    await safe_send(uid,
        f"✅ Контакт получен!\n👤 {esc(fn)} {esc(ln)}\n📞 {esc(phone)}\n\n"
        f"<b>Шаг 2/5 — Марка и модель авто:</b>\n<i>Например: Toyota Camry</i>",
        reply_markup=kb_cancel())

@router.message(DriverRegForm.car_model)
async def p_model(msg: Message, state: FSMContext):
    uid, model = msg.from_user.id, msg.text.strip()
    if not (2 <= len(model) <= 100):
        await safe_send(uid, "❌ Введите марку и модель (2–100 символов)"); return
    await state.update_data(car_model=model)
    await state.set_state(DriverRegForm.car_year)
    await safe_send(uid, f"✅ <b>{esc(model)}</b>\n\n<b>Шаг 3/5 — Год выпуска:</b>")

@router.message(DriverRegForm.car_year)
async def p_year(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    try:
        year = int(msg.text.strip())
    except ValueError:
        await safe_send(uid, "❌ Введите год числом, например: 2018"); return
    if err := _validate_car_year(year):
        await safe_send(uid, err); return
    await state.update_data(car_year=year)
    await state.set_state(DriverRegForm.car_number)
    await safe_send(uid, f"✅ <b>{year}</b>\n\n<b>Шаг 4/5 — Гос. номер:</b>")

@router.message(DriverRegForm.car_number)
async def p_number(msg: Message, state: FSMContext):
    uid, number = msg.from_user.id, msg.text.strip().upper().replace(" ","")
    if not number:
        await safe_send(uid, "❌ Введите номер автомобиля"); return
    await state.update_data(car_number=number)
    await state.set_state(DriverRegForm.car_class)
    await safe_send(uid, f"✅ <b>{esc(number)}</b>", reply_markup=ReplyKeyboardRemove())
    lines = ["<b>Шаг 5/5 — Класс авто:</b>"]
    for k in COMFORT_H + ["minivan"]:
        yr = f" ({TARIFFS_RF[k]['year']})" if TARIFFS_RF[k].get("year") else ""
        lines.append(f"{TARIFFS_RF[k]['label']}{yr} — <i>{CLASS_DESC.get(k,'')}</i>")
    await safe_send(uid, "\n".join(lines), reply_markup=kb_pclass())

@router.callback_query(StateFilter(DriverRegForm.car_class), F.data.startswith("pclass_"))
async def cb_pclass_fsm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    cc = call.data.split("_", 1)[1]
    data = await state.get_data()
    if err := check_brand(data.get("car_model",""), cc):
        await call.answer(err, show_alert=True); return
    data["car_class"] = cc
    data["car_class_label"] = TARIFFS_RF[cc]["label"]
    data["user_id"] = uid
    existing = DB.driver(uid)
    data["docs_verified"] = existing.get("docs_verified", False) if existing else False
    data["registered_at"] = existing.get("registered_at") if existing else now_iso()
    DB.driver_save(uid, data)
    # Выдаём пробную подписку на 50 дней при первой регистрации
    if not existing:
        trial_exp = (now_dt().date() + timedelta(days=50)).strftime("%Y-%m-%d")
        DB.sub_set(uid, trial_exp)
        log.info(f"Пробная подписка выдана водителю {uid} до {trial_exp}")
    else:
        trial_exp = None
    await state.clear()
    pl = profile_link(data)
    ph = f'<a href="{pl}">Открыть</a>' if pl.startswith("http") else pl
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    trial_txt = (
        f"\n🎁 <b>Вам начислена бесплатная пробная подписка на 50 дней!</b>\n"
        f"До: <b>{datetime.strptime(trial_exp, '%Y-%m-%d').strftime('%d.%m.%Y')}</b>\n"
        if trial_exp else ""
    )
    await safe_send(uid,
        f"🎉 <b>Регистрация завершена!</b>\n\n"
        f"👤 {drv_name(data)}\n📞 {esc(data.get('phone'))}\n"
        f"🚘 {esc(data.get('car_model'))} ({data.get('car_year','')})\n"
        f"🔢 {esc(data.get('car_number'))}\n"
        f"🏷 {esc(data.get('car_class_label'))}\n"
        + trial_txt +
        "\n⏳ <b>Ожидайте верификации профиля администратором.</b>",
        reply_markup=kb_driver(uid))
    if existing and not DB.sub_info(uid)[2]:
        await safe_send(uid, "💳 <b>Пока ждёте верификации — оформите абонемент:</b>", reply_markup=kb_subs())
    for aid in ADMIN_IDS:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Верифицировать", callback_data=f"doc_ok_{uid}"),
            InlineKeyboardButton(text="❌ Отказать",       callback_data=f"doc_rej_{uid}"),
        ]])
        await safe_send(aid,
            f"📋 <b>Новый водитель — нужна верификация</b>\n\n"
            f"👤 {drv_name(data)}\n📞 {esc(data.get('phone'))}\n"
            f"🚘 {esc(data.get('car_model'))} ({data.get('car_year','—')})\n"
            f"🔢 {esc(data.get('car_number'))}\n"
            f"🏷 {esc(data.get('car_class_label'))}\n"
            f"💬 {pl}\nID: <code>{uid}</code>",
            reply_markup=kb)
    await call.answer()

@router.message(F.text == "👤 Мой профиль")
async def profile_menu(msg: Message):
    uid = msg.from_user.id
    drv = DB.driver(uid)
    if not drv: await safe_send(uid, "❌ Профиль не найден.", reply_markup=kb_main()); return
    avg, cnt  = DB.avg_rating(uid)
    pl        = profile_link(drv)
    ph        = f'<a href="{pl}">Открыть</a>' if pl.startswith("http") else pl
    exp, dl, active = DB.sub_info(uid)
    await safe_send(uid,
        f"👤 <b>{drv_name(drv)}</b>\n"
        f"🚘 {esc(drv.get('car_model'))} ({drv.get('car_year','—')})\n"
        f"🔢 {esc(drv.get('car_number'))}\n"
        f"🏷 {esc(drv.get('car_class_label'))}\n"
        f"📞 {esc(drv.get('phone'))}\n"
        f"📄 Статус: <b>{'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает'}</b>\n"
        f"💳 {'✅ До '+exp+' ('+str(dl)+' дн.)' if active else '❌ Нет подписки'}\n"
        f"{'⭐ Рейтинг: '+str(avg)+'/5 ('+str(cnt)+' оценок)' if cnt else '⭐ Нет оценок'}\n\n"
        f"<i>Для изменения данных обратитесь к администратору @Olegan7979</i>")


# ══════════════ АБОНЕМЕНТ ══════════════
@router.message(F.text == "💳 Абонемент")
async def subscription_menu(msg: Message):
    uid = msg.from_user.id
    if not DB.driver(uid): await safe_send(uid, "❌ Заполните профиль."); return
    exp, dl, active = DB.sub_info(uid)
    txt = (f"✅ <b>Абонемент активен</b>\nДо: <b>{exp}</b>\nОсталось: <b>{dl} дн.</b>\n\nПродлить?"
           if active else "💳 <b>Абонемент</b>\n\n❌ Нет подписки\nВыберите тариф:")
    await safe_send(uid, txt, reply_markup=kb_subs())


# ══════════════ CALLBACK-ОБРАБОТЧИКИ (общие) ══════════════
@router.callback_query(F.data == "cancel_order")
async def cb_cancel_order(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "❌ Отменено.", reply_markup=kb_main())
    await call.answer()

@router.callback_query(F.data == "cant_take")
async def cb_cant_take(call: CallbackQuery):
    await call.answer("❌ Недоступен", show_alert=True)

@router.callback_query(F.data.startswith("skip_"))
async def cb_skip(call: CallbackQuery):
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await call.answer("Пропущено")

@router.callback_query(F.data.startswith("rate_"))
async def cb_rate(call: CallbackQuery):
    uid = call.from_user.id
    _, oid_str, stars_str = call.data.split("_")
    oid, stars = int(oid_str), int(stars_str)
    order = DB.order(oid)
    if not order: await call.answer("❌ Заказ не найден"); return
    if order["passenger_id"] != uid: await call.answer("❌ Не ваш заказ", show_alert=True); return
    if order["status"] != "completed": await call.answer("❌ Заказ не завершён"); return
    drv_id = order.get("driver_id")
    if not drv_id: await call.answer("❌ Нет водителя"); return
    if DB.has_rating(oid, uid):
        await call.answer("Вы уже оценили эту поездку")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None); return
    DB.add_rating(oid, drv_id, uid, stars)
    avg, cnt = DB.avg_rating(drv_id)
    try:
        await bot.edit_message_text(
            f"✅ <b>Поездка #{oid} завершена!</b>\n⭐ Ваша оценка: <b>{stars}</b>\nСпасибо!",
            call.message.chat.id, call.message.message_id)
    except:
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await safe_send(uid, f"⭐ Оценка {stars} сохранена!")
    await safe_send(drv_id,
        f"⭐ <b>Новая оценка за поездку #{oid}!</b>\n"
        f"Пассажир поставил: <b>{stars}⭐</b>\nВаш средний рейтинг: <b>{avg}/5</b> ({cnt} оценок)")
    await call.answer("✅ Спасибо за оценку!")

@router.callback_query(F.data.startswith("take_"))
async def cb_take(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])
    drv = DB.driver(uid)
    if not drv or not drv.get("docs_verified"): await call.answer("❌ Профиль не верифицирован", show_alert=True); return
    _, _, active = DB.sub_info(uid)
    if not active: await call.answer("❌ Нет абонемента", show_alert=True); return
    od = DB.order(oid)
    if not od: await call.answer("❌ Заказ не найден"); return
    if od["passenger_id"] == uid: await call.answer("❌ Нельзя взять свой заказ", show_alert=True); return
    ct, rsn = can_take_order(drv, od)
    if not ct: await call.answer(rsn, show_alert=True); return
    if not DB.order_take_atomic(oid, uid):
        await call.answer("⚠️ Заказ уже недоступен")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None); return
    await update_channel_post(oid)
    od = DB.order(oid)
    if not od: await call.answer("✅ Заказ принят!"); return
    pid = od.get("passenger_id")
    if not pid: await call.answer("✅ Заказ принят!"); return
    try:
        pc = await bot.get_chat(pid)
        pn, pu = esc(pc.first_name or "Пассажир"), pc.username
    except:
        pn, pu = "Пассажир", None
    purl = f"https://t.me/{pu}" if pu else f"tg://user?id={pid}"
    avg, cnt = DB.avg_rating(uid)
    r_text   = f"\n⭐ Рейтинг: <b>{avg}/5</b> ({cnt} оценок)" if cnt else "\n⭐ Новый водитель"
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    drv_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать пассажиру", url=purl)],
        [InlineKeyboardButton(text="❌ Отказаться", callback_data=f"driver_cancel_{oid}")],
    ])
    await safe_send(uid,
        f"✅ <b>Заказ #{oid} принят!</b>\n👤 <a href='{purl}'>{pn}</a>\n"
        f"📍 {esc(od.get('from_city'))} → {esc(od.get('to_city'))}\n"
        f"📅 {od.get('trip_date')} 🕐 {od.get('trip_time')}", reply_markup=drv_kb)
    du = (drv.get("username","") or "").lstrip("@")
    durl = f"https://t.me/{du}" if du and not du.startswith("id") else f"tg://user?id={uid}"
    pass_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Написать водителю",  url=durl)],
        [InlineKeyboardButton(text="✅ Завершить поездку",  callback_data=f"done_{oid}")],
        [InlineKeyboardButton(text="❌ Отменить",           callback_data=f"cancel_{oid}")],
    ])
    car_note = (f"\n⚠️ <i>Водитель приедет на {esc(drv.get('car_class_label',drv.get('car_class','')))}</i>"
                if drv.get("car_class") != od.get("car_class") else "")
    await safe_send(pid,
        f"🎉 <b>Водитель найден!</b>\n"
        f"👤 {drv_name(drv)}{r_text}\n"
        f"🚘 {esc(drv.get('car_model'))} ({drv.get('car_year','—')})\n"
        f"🔢 {esc(drv.get('car_number'))}\n"
        f"📞 {esc(drv.get('phone'))}{car_note}\n\n"
        f"⏳ <i>Ожидайте — с вами свяжется водитель.</i>\n\n"
        f"🚫 <b>Не переводите предоплату!</b>\n"
        f"Оплата — только водителю после поездки.\n"
        f"<i>Если водитель просит предоплату — сообщите @Olegan7979</i>",
        reply_markup=pass_kb)
    await call.answer("✅ Заказ принят!")

@router.callback_query(F.data.regexp(r"^cancel_\d+$"))
async def cb_cancel(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])
    if DB.order_cancel_atomic(oid, uid, "passenger"):
        await _clear_notified(oid)
        await update_channel_post(oid)
        order = DB.order(oid)
        if order and order.get("driver_id"):
            await safe_send(order["driver_id"], f"❌ Пассажир отменил заказ #{oid}")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await safe_send(uid, f"✅ Заказ #{oid} отменён.", reply_markup=kb_main())
        await call.answer("✅ Отменено")
    else:
        await call.answer("❌ Заказ уже нельзя отменить", show_alert=True)

@router.callback_query(F.data.startswith("driver_cancel_"))
async def cb_driver_cancel(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[2])
    if DB.order_cancel_atomic(oid, uid, "driver"):
        await update_channel_post(oid)
        order = DB.order(oid)
        if order and order.get("passenger_id"):
            await safe_send(order["passenger_id"], f"❌ Водитель отказался от заказа #{oid}")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await safe_send(uid, f"✅ Вы отказались от заказа #{oid}")
        await _clear_notified(oid)
        await _notify_drivers(oid, exclude_uid=uid)
        await call.answer("✅")
    else:
        await call.answer("❌ Заказ уже нельзя отменить", show_alert=True)

@router.callback_query(F.data.startswith("done_"))
async def cb_done(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])
    o = DB.order(oid)
    if not o: await call.answer("❌ Заказ не найден"); return
    if o["passenger_id"] != uid: await call.answer("❌ Не ваш заказ", show_alert=True); return
    if o["status"] != "taken": await call.answer("❌ Заказ не в работе", show_alert=True); return
    DB.order_upd(oid, status="completed", completed_at=now_iso())
    await _clear_notified(oid)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, "✅ <b>Поездка завершена!</b>\n⭐ Пожалуйста, оцените водителя:",
                    reply_markup=kb_stars(oid))
    if o.get("driver_id"):
        await safe_send(o["driver_id"], f"✅ Поездка #{oid} завершена пассажиром!")
    await call.answer("✅ Завершено")

@router.callback_query(F.data.startswith("sub_"))
async def cb_sub(call: CallbackQuery):
    uid = call.from_user.id
    if not DB.driver(uid): await call.answer("❌ Заполните профиль", show_alert=True); return
    pk = call.data.split("_")[1]
    plan = SUBS.get(pk)
    if not plan: await call.answer("❌"); return
    DB.pending_set(uid, pk)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    paid_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Я оплатил", callback_data=f"paid_{pk}")
    ]])
    await safe_send(uid,
        f"💳 <b>{plan['label']}</b>\nСумма: <b>{plan['price']} ₽</b>\n\n{PAYMENT_DETAILS}",
        reply_markup=paid_kb)
    drv = DB.driver(uid)
    for aid in ADMIN_IDS:
        await safe_send(aid,
            f"💳 <b>Запрос на абонемент</b>\n👤 {drv_name(drv)}\n"
            f"🚘 {esc(drv.get('car_model'))}\nТариф: {plan['label']}\nID: <code>{uid}</code>")
    await call.answer()

@router.callback_query(F.data.startswith("paid_"))
async def cb_paid(call: CallbackQuery):
    uid = call.from_user.id
    pk = call.data.split("_")[1]
    plan = SUBS.get(pk, {})
    await safe_send(uid, "⏳ Заявка отправлена. Активация в течение 1–2 ч.")
    drv = DB.driver(uid)
    for aid in ADMIN_IDS:
        await safe_send(aid,
            f"🔔 <b>Водитель сообщил об оплате!</b>\n"
            f"👤 {drv_name(drv or {})} (ID: {uid})\nТариф: {plan.get('label','—')}")
    await call.answer("✅")

@router.callback_query(F.data == "back_driver")
async def cb_back_driver(call: CallbackQuery):
    uid = call.from_user.id
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, "🚗 Меню водителя", reply_markup=kb_driver(uid))
    await call.answer()


# ══════════════ АДМИН-КОЛБЭКИ ══════════════
@router.callback_query(F.data.startswith("conf_sub_"))
async def cb_conf_sub(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: await call.answer("❌"); return
    _, _, tgt, pk = call.data.split("_", 3)
    tgt = int(tgt)
    plan = SUBS.get(pk)
    if not plan: await call.answer("❌"); return
    exp_str, _, _ = DB.sub_info(tgt)
    base = (max(datetime.strptime(exp_str,"%Y-%m-%d").date(), now_dt().date())
            if exp_str else now_dt().date())
    new_exp = (base + timedelta(days=plan["days"])).strftime("%Y-%m-%d")
    DB.sub_set(tgt, new_exp, uid, pk)
    DB.pending_del(tgt, uid, pk)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Подписка активирована до {new_exp}")
    await safe_send(tgt,
        f"🎉 <b>Абонемент активирован!</b>\n{plan['label']}\n"
        f"До: {datetime.strptime(new_exp,'%Y-%m-%d').strftime('%d.%m.%Y')}",
        reply_markup=kb_driver(tgt))
    await call.answer("✅")

@router.callback_query(F.data.startswith("rej_sub_"))
async def cb_rej_sub(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: await call.answer("❌"); return
    tgt = int(call.data.split("_")[2])
    DB.pending_del(tgt, uid, DB.pending_get(tgt))
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(tgt, "❌ Запрос на абонемент отклонён.")
    await call.answer("❌")

@router.callback_query(F.data.regexp(r"^doc_(ok|rej)_\d+$"))
async def cb_doc_verify(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: await call.answer("❌"); return
    ok = call.data.startswith("doc_ok_")
    tgt = int(call.data.split("_")[2])
    DB.driver_verify(tgt, ok)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    if ok:
        await safe_send(uid, f"✅ Профиль водителя {tgt} верифицирован")
        await safe_send(tgt, "✅ <b>Ваш профиль верифицирован!</b>\nТеперь вы можете принимать заказы.",
                        reply_markup=kb_driver(tgt))
    else:
        await safe_send(uid, f"❌ Профиль {tgt} отклонён")
        await safe_send(tgt, "❌ <b>Верификация отклонена.</b>\nОбратитесь к администратору @Olegan7979.")
    await call.answer("✅" if ok else "❌")

@router.callback_query(F.data == "adm_orders")
async def cb_adm_orders(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    await call.answer()
    asyncio.create_task(_send_orders_page(call.from_user.id, 0, "all"))

@router.callback_query(F.data.startswith("adm_ord_page_"))
async def cb_adm_ord_page(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    try:
        parts = call.data.split("_")
        page = int(parts[-1])
        filter_key = parts[-2] if parts[-2] in ORDER_FILTERS else "all"
    except: return
    await call.answer()
    asyncio.create_task(_send_orders_page(call.from_user.id, page, filter_key))

@router.callback_query(F.data.startswith("adm_ord_filter_"))
async def cb_adm_ord_filter(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    try:
        parts = call.data.split("_")
        page = int(parts[-1])
        filter_key = parts[-2] if parts[-2] in ORDER_FILTERS else "all"
    except: return
    await call.answer()
    asyncio.create_task(_send_orders_page(call.from_user.id, page, filter_key))

@router.callback_query(F.data == "adm_ord_noop")
async def cb_adm_ord_noop(call: CallbackQuery): await call.answer()

@router.callback_query(F.data.startswith("adm_cancel_order_"))
async def cb_adm_cancel_order(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    oid = int(call.data.split("_")[-1])
    order = DB.order(oid)
    if not order: await call.answer("❌ Не найден"); return
    DB.order_upd(oid, status="cancelled")
    await _clear_notified(oid)
    await update_channel_post(oid)
    if order.get("passenger_id"):
        await safe_send(order["passenger_id"], f"❌ <b>Заказ #{oid} отменён администратором.</b>")
    if order.get("driver_id"):
        await safe_send(order["driver_id"], f"❌ Заказ #{oid} отменён администратором.")
    await safe_send(uid, f"✅ Заказ #{oid} отменён")
    await call.answer("✅")

@router.callback_query(F.data.startswith("adm_complete_order_"))
async def cb_adm_complete_order(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    oid = int(call.data.split("_")[-1])
    order = DB.order(oid)
    if not order: await call.answer("❌"); return
    DB.order_upd(oid, status="completed", completed_at=now_iso())
    if order.get("passenger_id"):
        await safe_send(order["passenger_id"], f"✅ Заказ #{oid} завершён администратором.")
    if order.get("driver_id"):
        await safe_send(order["driver_id"], f"✅ Заказ #{oid} завершён администратором.")
    await safe_send(uid, f"✅ Заказ #{oid} завершён")
    await call.answer("✅")

@router.callback_query(F.data == "adm_drivers")
async def cb_adm_drivers(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    await call.answer()
    asyncio.create_task(_send_drivers_page(call.from_user.id, 0))

@router.callback_query(F.data.startswith("adm_drv_page_"))
async def cb_adm_drv_page(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    try: page = int(call.data.split("_")[-1])
    except: return
    await call.answer()
    asyncio.create_task(_send_drivers_page(call.from_user.id, page))

@router.callback_query(F.data == "adm_drv_noop")
async def cb_adm_drv_noop(call: CallbackQuery): await call.answer()

@router.callback_query(F.data.startswith("adm_edit_"))
async def cb_adm_edit(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    tgt = int(call.data.split("_")[2])
    drv = DB.driver(tgt)
    if not drv: await call.answer("❌ Не найден"); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚘 Марка/модель", callback_data=f"adm_ef_{tgt}_car_model")],
        [InlineKeyboardButton(text="📅 Год выпуска",  callback_data=f"adm_ef_{tgt}_car_year")],
        [InlineKeyboardButton(text="🔢 Гос. номер",   callback_data=f"adm_ef_{tgt}_car_number")],
        [InlineKeyboardButton(text="🏷 Класс авто",   callback_data=f"adm_ef_{tgt}_car_class")],
    ])
    await safe_send(uid, f"✏️ <b>Редактировать: {drv_name(drv)}</b>\nЧто изменить?", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_ef_"))
async def cb_adm_ef(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    parts = call.data.split("_"); tgt = int(parts[2]); field = "_".join(parts[3:])
    drv = DB.driver(tgt)
    if not drv: await call.answer("❌ Не найден"); return
    if field == "car_class":
        rows = []
        for k in COMFORT_H + ["minivan"]:
            yr = f" ({TARIFFS_RF[k]['year']})" if TARIFFS_RF[k].get("year") else ""
            rows.append([InlineKeyboardButton(text=f"{TARIFFS_RF[k]['label']}{yr}",
                                              callback_data=f"adm_sc_{tgt}_{k}")])
        await safe_send(uid, "🏷 Выберите новый класс:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        prompts = {"car_model":"🚘 Введите новую марку и модель:",
                   "car_year": "📅 Введите новый год (от 2008):",
                   "car_number":"🔢 Введите новый гос. номер:"}
        await state.set_state(AdminEditForm.waiting_input)
        await state.update_data(tgt=tgt, field=field, admin_edit=True)
        await safe_send(uid, prompts.get(field,"Введите значение:"), reply_markup=kb_cancel())
    await call.answer()

@router.message(AdminEditForm.waiting_input)
async def process_admin_edit(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if uid not in ADMIN_IDS: return
    text = (msg.text or "").strip()
    data = await state.get_data()
    tgt = data.get("tgt"); field = data.get("field")
    drv = DB.driver(tgt)
    if not drv:
        await state.clear()
        await safe_send(uid, "❌ Водитель не найден"); return

    if field == "car_model":
        if not (2 <= len(text) <= 100):
            await safe_send(uid, "❌ Введите марку и модель (2–100 символов)"); return
        DB.driver_update_fields(tgt, car_model=text)
        await notify_driver_change(tgt, "марку/модель авто", text)
    elif field == "car_year":
        try:
            year = int(text)
            if err := _validate_car_year(year):
                await safe_send(uid, err); return
            DB.driver_update_fields(tgt, car_year=year)
            await notify_driver_change(tgt, "год выпуска авто", year)
        except ValueError:
            await safe_send(uid, f"❌ Некорректный год (минимум {MIN_CAR_YEAR})"); return
    elif field == "car_number":
        number = text.upper().replace(" ","")
        if not number: await safe_send(uid, "❌ Введите номер"); return
        DB.driver_update_fields(tgt, car_number=number)
        await notify_driver_change(tgt, "гос. номер", number)
    await state.clear()
    await safe_send(uid, f"✅ {field} обновлён: {esc(text)}", reply_markup=kb_main())

@router.callback_query(F.data.startswith("adm_sc_"))
async def cb_adm_sc(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    parts = call.data.split("_"); tgt = int(parts[2]); cc = "_".join(parts[3:])
    drv = DB.driver(tgt)
    if not drv: await call.answer("❌ Не найден"); return
    if err := check_brand(drv.get("car_model",""), cc):
        await call.answer(err, show_alert=True); return
    lbl = TARIFFS_RF.get(cc,{}).get("label", cc)
    DB.driver_update_fields(tgt, car_class=cc, car_class_label=lbl)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Класс обновлён: {lbl}")
    await notify_driver_change(tgt, "класс авто", lbl)
    await call.answer("✅")

@router.callback_query(F.data.startswith("adm_del_"))
async def cb_adm_del(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    tgt = int(call.data.split("_")[2])
    drv = DB.driver(tgt)
    if not drv: await call.answer("❌ Не найден"); return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_delok_{tgt}"),
        InlineKeyboardButton(text="❌ Нет",         callback_data="adm_delno"),
    ]])
    await safe_send(uid, f"⚠️ Удалить <b>{drv_name(drv)}</b> (ID: {tgt})?", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_delok_"))
async def cb_adm_delok(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    tgt = int(call.data.split("_")[2])
    drv = DB.driver(tgt)
    if drv:
        active_orders = DB.driver_del(tgt)
        await safe_send(uid, f"✅ {drv_name(drv)} удалён")
        await safe_send(tgt, "🗑 Ваш профиль водителя удалён администратором.")
        for oid, pid in active_orders:
            if pid:
                await safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nЗаказ #{oid} снова открыт.")
            await update_channel_post(oid)
            await _notify_drivers(oid)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await call.answer("✅ Удалён")

@router.callback_query(F.data == "adm_delno")
async def cb_adm_delno(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS: return
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "❌ Удаление отменено")
    await call.answer()

@router.callback_query(F.data == "adm_bl")
async def cb_adm_bl(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    bl = DB.bl_all()
    if not bl:
        await safe_send(uid, "✅ Чёрный список пуст")
    else:
        lines = ["⛔ <b>Чёрный список:</b>"] + [str(u) for u in bl]
        chunk, size = [], 0
        for line in lines:
            if size + len(line) + 1 > 4000:
                await safe_send(uid, "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line); size += len(line) + 1
        if chunk:
            await safe_send(uid, "\n".join(chunk))
    await call.answer()

@router.callback_query(F.data == "adm_subs")
async def cb_adm_subs(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    pending = DB.pending_all()
    if not pending:
        await safe_send(uid, "✅ Нет ожидающих подтверждения")
    for p in pending:
        plan, drv = SUBS.get(p["plan_key"],{}), DB.driver(p["user_id"]) or {}
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Активировать",
                                 callback_data=f"conf_sub_{p['user_id']}_{p['plan_key']}"),
            InlineKeyboardButton(text="❌ Отклонить",
                                 callback_data=f"rej_sub_{p['user_id']}"),
        ]])
        await safe_send(uid,
            f"💳 <b>{drv_name(drv)}</b>\n💬 {profile_link(drv)}\n"
            f"ID: <code>{p['user_id']}</code>\nТариф: {plan.get('label','—')}",
            reply_markup=kb)
        await asyncio.sleep(0.05)
    await call.answer()

@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    s = DB.stats()
    await safe_send(uid,
        f"📊 <b>Статистика</b>\n\nЗаказов всего: {s['total']}\nОткрыто: {s['open']}\n"
        f"Завершено: {s['done']}\n\nВодителей: {s['drivers']}\n"
        f"Верифицировано: {s['docs_ok']}\nС подпиской: {s['subscribed']}")
    await call.answer()

@router.callback_query(F.data == "adm_help")
async def cb_adm_help(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS: return
    await safe_send(uid,
        "📖 <b>Команды администратора:</b>\n\n"
        "/ban ID — заблокировать\n/unban ID — разблокировать\n"
        "/unsub ID — аннулировать подписку\n/deldriver ID — удалить водителя\n"
        "/recalc order_id distance_km price — пересчитать заказ")
    await call.answer()

@router.message(StateFilter("*"))
async def unknown_state_after_restart(msg: Message, state: FSMContext):
    cur = await state.get_state()
    if cur is not None:
        return
    known = {
        "🚕 Создать заказ", "🚗 Я водитель", "📋 Мои заказы", "📊 Тарифы",
        "📦 Доступные заказы", "👤 Мой профиль", "💳 Абонемент",
        "✅ Подписка активна", "❌ Нет подписки", "📈 Мои поездки",
        "🔙 Главное меню", "👤 Зарегистрироваться", "❌ Отменить", "Нет",
    }
    if msg.text in known:
        return
    await safe_send(msg.from_user.id,
        "⚠️ Сессия устарела после перезапуска бота. Начните заново.",
        reply_markup=kb_main())

@router.callback_query()
async def cb_fallback(call: CallbackQuery):
    await call.answer("⚠️ Неизвестная команда")


# ══════════════ ПАГИНАЦИЯ (АДМИН) ══════════════
DRIVER_PAGE_SIZE = 8
ORDER_PAGE_SIZE  = 5
ORDER_FILTERS = {
    "all":       ("📋 Все",          None),
    "open":      ("🟢 Открытые",    "open"),
    "taken":     ("🔵 Принятые",    "taken"),
    "completed": ("✅ Завершённые", "completed"),
    "cancelled": ("🔴 Отменённые", "cancelled"),
}

async def _send_drivers_page(uid, page=0):
    drivers = DB.all_drivers()
    total   = len(drivers)
    if not drivers:
        await safe_send(uid, "👥 Нет водителей"); return
    start       = page * DRIVER_PAGE_SIZE
    chunk       = drivers[start : start + DRIVER_PAGE_SIZE]
    if not chunk:
        await safe_send(uid, "⚠️ Страница не найдена"); return
    total_pages = max(1, (total - 1) // DRIVER_PAGE_SIZE + 1)
    await safe_send(uid, f"👥 <b>Водители</b> — страница {page + 1} из {total_pages} (всего: {total})")
    for d in chunk:
        exp_str, dl, active = DB.sub_info(d["user_id"])
        avg, cnt = DB.avg_rating(d["user_id"])
        sub_txt  = f"✅ до {exp_str} ({dl} дн.)" if active else "❌ нет подписки"
        rat_txt  = f"⭐ {avg}/5 ({cnt} оц.)" if cnt else "⭐ нет оценок"
        info = (
            f"👤 <b>{drv_name(d)}</b> | ID: <code>{d['user_id']}</code>\n"
            f"🚘 {esc(d.get('car_model'))} ({esc(d.get('car_year'))})\n"
            f"🔢 {esc(d.get('car_number'))}\n"
            f"🏷 {esc(d.get('car_class_label'))}\n"
            f"📞 {esc(d.get('phone'))}\n"
            f"📄 {'✅ Верифицирован' if d.get('docs_verified') else '⏳ Ожидает'}\n"
            f"💳 {sub_txt}\n{rat_txt}\n"
            f"🗓 Рег.: {str(d.get('registered_at','—'))[:10]}"
        )
        un  = d.get("username","") or ""
        pl  = d.get("profile_link","") or ""
        url = (f"https://t.me/{un.lstrip('@')}" if un and not un.startswith("tg://")
               else pl if pl.startswith("https://") else None)
        rows = []
        if url:
            rows.append([InlineKeyboardButton(text="💬 Написать", url=url)])
        rows.append([
            InlineKeyboardButton(text="✅ Верифицировать", callback_data=f"doc_ok_{d['user_id']}"),
            InlineKeyboardButton(text="❌ Снять вериф.",   callback_data=f"doc_rej_{d['user_id']}"),
        ])
        rows.append([InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"adm_edit_{d['user_id']}")])
        rows.append([InlineKeyboardButton(text="🗑 Удалить",        callback_data=f"adm_del_{d['user_id']}")])
        await safe_send(uid, info, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_drv_page_{page - 1}"))
    nav_btns.append(InlineKeyboardButton(text=f"· {page + 1}/{total_pages} ·", callback_data="adm_drv_noop"))
    if start + DRIVER_PAGE_SIZE < total:
        nav_btns.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"adm_drv_page_{page + 1}"))
    await safe_send(uid, "📄 Навигация:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_btns]))

async def _send_orders_page(uid, page=0, filter_key="all"):
    status        = ORDER_FILTERS.get(filter_key, ("", None))[1]
    orders, total = DB.all_orders(limit=ORDER_PAGE_SIZE, offset=page * ORDER_PAGE_SIZE, status=status)
    if not orders and page == 0:
        await safe_send(uid, "📋 Заказов нет"); return
    if not orders:
        await safe_send(uid, "⚠️ Страница не найдена"); return
    total_pages  = max(1, (total - 1) // ORDER_PAGE_SIZE + 1)
    filter_label = ORDER_FILTERS.get(filter_key, ("📋 Все", None))[0]
    await safe_send(uid,
        f"📋 <b>Заказы</b> · {filter_label}\n"
        f"Страница {page + 1} из {total_pages} (всего: {total})")
    for o in orders:
        rows = []
        if o["status"] == "open":
            rows.append([InlineKeyboardButton(
                text="🔴 Отменить заказ", callback_data=f"adm_cancel_order_{o['id']}")])
        elif o["status"] == "taken" and o.get("driver_id"):
            rows.append([
                InlineKeyboardButton(text="✅ Завершить", callback_data=f"adm_complete_order_{o['id']}"),
                InlineKeyboardButton(text="🔴 Отменить", callback_data=f"adm_cancel_order_{o['id']}"),
            ])
        await safe_send(uid, fmt_order(o),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    filter_btns = [
        InlineKeyboardButton(
            text=f"·{label}·" if k == filter_key else label,
            callback_data=f"adm_ord_filter_{k}_0")
        for k, (label, _) in ORDER_FILTERS.items()
    ]
    await safe_send(uid, "🔍 Фильтр:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[filter_btns]))
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton(
            text="◀️ Назад", callback_data=f"adm_ord_page_{filter_key}_{page - 1}"))
    nav_btns.append(InlineKeyboardButton(
        text=f"· {page + 1}/{total_pages} ·", callback_data="adm_ord_noop"))
    if (page + 1) * ORDER_PAGE_SIZE < total:
        nav_btns.append(InlineKeyboardButton(
            text="Вперёд ▶️", callback_data=f"adm_ord_page_{filter_key}_{page + 1}"))
    await safe_send(uid, "📄 Навигация:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_btns]))


# ══════════════ ЗАПУСК ══════════════
async def main():
    log.info("=" * 50)
    log.info("  🚕 МЕЖГОРОД ТРАНСФЕР v14.0 (aiogram 3 + FSM)")
    log.info("=" * 50)
    DB.init()
    cleaner_task = asyncio.create_task(_notified_cleaner())
    trial_task   = asyncio.create_task(_trial_expiry_notifier())
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        cleaner_task.cancel()
        trial_task.cancel()
        for t in (cleaner_task, trial_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

if __name__ == "__main__":
    asyncio.run(main())