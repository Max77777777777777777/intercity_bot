# ══════════════════════════════════════════════════════════════
#  МЕЖГОРОД ТРАНСФЕР v16.2 — aiogram 3 + FSM
#  fix: автоматический сброс FSM-состояния
#  fix: синтаксическая ошибка trip_cancel
# ══════════════════════════════════════════════════════════════
import asyncio, json, re, html, logging, os
import aiohttp
import urllib3
import asyncpg
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
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

class IsAdmin(BaseFilter):
    """Пропускает событие только от пользователей из ADMIN_IDS (Message или CallbackQuery)."""
    async def __call__(self, event) -> bool:
        return event.from_user.id in ADMIN_IDS

BOT_USERNAME    = os.getenv("BOT_USERNAME", "intercitytrans_bot")
PAYMENT_DETAILS = os.getenv("PAYMENT_DETAILS", "Для оплаты абонемента свяжитесь с администратором @Olegan7979")
DISPATCHER_PHONES = ["+79033176800", "+79381584161"]
DATA_DIR        = os.getenv("DATA_DIR", "/app/data")
PG_DSN          = os.getenv("PG_DSN", "")
DIST_COEFF      = 1.25  # fallback коэффициент для РФ
NOTIFY_LIMIT    = 50
NOTIFY_BATCH    = 5
NOTIFY_DELAY    = 0.1
TZ              = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET_HOURS", "3"))))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")
if not PG_DSN:
    raise ValueError("PG_DSN не задан!")

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
# price = цена за км (вся машина), price_seat = цена за км за одно место в рейсе
TARIFFS_RF = {
    "standard": {"label": "🚗 Стандарт",            "year": "от 2008 г.", "price": 25,   "price_seat": 11},
    "comfort":  {"label": "🚙 Комфорт",              "year": "от 2015 г.", "price": 34,   "price_seat": 15},
    "comfort+": {"label": "✨ Комфорт+",             "year": "от 2019 г.", "price": 40,   "price_seat": 17.5},
    "minivan":  {"label": "🚐 Минивэн", "year": "",           "price": 45,   "price_seat": 10},
    "business": {"label": "💼 Бизнес",               "year": "от 2018 г.", "price": 60,   "price_seat": 26},
}
TARIFFS_NT = {
    "standard": {"label": "🚗 Стандарт",            "year": "от 2008 г.", "price": 40,   "price_seat": 17.5},
    "comfort":  {"label": "🚙 Комфорт",              "year": "от 2015 г.", "price": 50,   "price_seat": 21.5},
    "comfort+": {"label": "✨ Комфорт+",             "year": "от 2019 г.", "price": 58,   "price_seat": 25},
    "minivan":  {"label": "🚐 Минивэн", "year": "",           "price": 65,   "price_seat": 14},
    "business": {"label": "💼 Бизнес",               "year": "от 2018 г.", "price": 80,   "price_seat": 34.5},
}
TARIFFS_CIS = {
    "standard": {"label": "🚗 Стандарт",            "year": "от 2008 г.", "price": 32,   "price_seat": 12.5},
    "comfort":  {"label": "🚙 Комфорт",              "year": "от 2015 г.", "price": 37,   "price_seat": 15.5},
    "comfort+": {"label": "✨ Комфорт+",             "year": "от 2019 г.", "price": 45,   "price_seat": 17.5},
    "minivan":  {"label": "🚐 Минивэн", "year": "",           "price": 55,   "price_seat": 15.5},
    "business": {"label": "💼 Бизнес",               "year": "от 2018 г.", "price": 60,   "price_seat": 25},
}

NT_KW = ["лнр","днр","луганск","донецк","крым","симферополь","севастополь","херсон","запорожье","мариуполь","мелитополь"]
CIS_KW = [
    "тбилиси","батуми","кутаиси","рустави","гори","зугдиди","поти","телави","мцхета","боржоми","сигнахи","грузия",
    "ереван","гюмри","ванадзор","вагаршапат","абовян","армения",
    "баку","гянджа","сумгаит","мингячевир","нахчыван","азербайджан",
    "алматы","астана","шымкент","актобе","тараз","павлодар","казахстан",
    "минск","гомель","могилёв","витебск","гродно","брест","беларусь",
    "ташкент","самарканд","бухара","наманган","андижан","узбекистан",
    "бишкек","ош","джалал-абад","кыргызстан",
    "душанбе","худжанд","таджикистан",
    "ашхабад","туркменистан",
    "кишинёв","тирасполь","молдова",
    "стамбул","анкара","трабзон","эрзурум","карс","турция",
    "тебриз","ардебиль",
]
REGION_LABELS = {"rf": "🇷🇺 Россия", "nt": "🆕 Новые территории", "cis": "🌍 Кавказ/СНГ"}
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
ADMIN_EDIT_FIELD_LABELS = {
    "car_model":  "Марка/модель",
    "car_year":   "Год выпуска",
    "car_number": "Гос. номер",
}
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

class TripForm(StatesGroup):
    from_city      = State()
    to_city        = State()
    trip_date      = State()
    trip_time      = State()
    seats_total    = State()
    car_class      = State()
    price_per_seat = State()
    confirm        = State()

class SearchTripForm(StatesGroup):
    from_city = State()
    to_city   = State()
    trip_date = State()

# ══════════════ БД ══════════════
_pg_pool: asyncpg.Pool = None

async def _init_pool():
    global _pg_pool
    _pg_pool = await asyncpg.create_pool(
        PG_DSN,
        min_size=2,
        max_size=20,
        command_timeout=30,
    )
    log.info("✅ Пул соединений PostgreSQL создан (2–20, asyncpg)")

class DB:
    @staticmethod
    async def init():
        async with _pg_pool.acquire() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS drivers(
                    user_id BIGINT PRIMARY KEY, name TEXT, first_name TEXT, last_name TEXT,
                    car_model TEXT DEFAULT '—', car_year INTEGER, car_number TEXT DEFAULT '—',
                    car_class TEXT DEFAULT 'standard',
                    car_class_label TEXT DEFAULT '🚗 Стандарт',
                    phone TEXT DEFAULT '—', username TEXT, profile_link TEXT,
                    has_photo INTEGER DEFAULT 0, docs_verified INTEGER DEFAULT 0, registered_at TEXT
                )
            """)
            await c.execute("CREATE TABLE IF NOT EXISTS subscriptions(user_id BIGINT PRIMARY KEY, expires_date TEXT)")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS orders(
                    id SERIAL PRIMARY KEY, passenger_id BIGINT,
                    from_city TEXT, to_city TEXT, trip_date TEXT, trip_time TEXT,
                    passengers INTEGER, car_class TEXT, wishes TEXT,
                    distance_km REAL, price INTEGER, status TEXT DEFAULT 'pending',
                    created_at TEXT, driver_id BIGINT, taken_at TEXT, completed_at TEXT, channel_msg_id BIGINT
                )
            """)
            await c.execute("CREATE TABLE IF NOT EXISTS blacklist(user_id BIGINT PRIMARY KEY)")
            await c.execute("CREATE TABLE IF NOT EXISTS pending_subscriptions(user_id BIGINT PRIMARY KEY, plan_key TEXT)")
            await c.execute("""
                CREATE TABLE IF NOT EXISTS subscription_log(
                    id SERIAL PRIMARY KEY, user_id BIGINT, plan_key TEXT,
                    admin_id BIGINT, action TEXT, created_at TEXT
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS ratings(
                    id SERIAL PRIMARY KEY, order_id INTEGER, driver_id BIGINT,
                    passenger_id BIGINT, stars INTEGER CHECK(stars BETWEEN 1 AND 5), created_at TEXT
                )
            """)
            await c.execute("CREATE INDEX IF NOT EXISTS idx_orders_passenger ON orders(passenger_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_orders_driver    ON orders(driver_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_ratings_driver   ON ratings(driver_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_ratings_order    ON ratings(order_id)")
            # ── РЕЙСЫ ──
            await c.execute("""
                CREATE TABLE IF NOT EXISTS trips(
                    id SERIAL PRIMARY KEY,
                    driver_id BIGINT NOT NULL,
                    from_city TEXT NOT NULL,
                    to_city TEXT NOT NULL,
                    trip_date TEXT NOT NULL,
                    trip_time TEXT NOT NULL,
                    car_class TEXT NOT NULL,
                    car_class_label TEXT NOT NULL,
                    seats_total INTEGER NOT NULL,
                    seats_free INTEGER NOT NULL,
                    price_per_seat INTEGER NOT NULL,
                    distance_km REAL,
                    region TEXT NOT NULL DEFAULT 'rf',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT
                )
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS trip_bookings(
                    id SERIAL PRIMARY KEY,
                    trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                    passenger_id BIGINT NOT NULL,
                    seats INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT,
                    UNIQUE(trip_id, passenger_id)
                )
            """)
            await c.execute("CREATE INDEX IF NOT EXISTS idx_trips_driver  ON trips(driver_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_trips_status  ON trips(status)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_trips_route   ON trips(from_city, to_city, trip_date)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_trip ON trip_bookings(trip_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_bookings_pass ON trip_bookings(passenger_id)")
        log.info("✅ БД готова (PostgreSQL asyncpg)")

    # ── ВОДИТЕЛИ ──
    @staticmethod
    async def driver(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM drivers WHERE user_id=$1", uid)
        return dict(r) if r else None

    @staticmethod
    async def driver_save(uid, d):
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO drivers "
                "(user_id,name,first_name,last_name,car_model,car_year,car_number,"
                "car_class,car_class_label,phone,username,profile_link,has_photo,docs_verified,registered_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "name=EXCLUDED.name, first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name, "
                "car_model=EXCLUDED.car_model, car_year=EXCLUDED.car_year, car_number=EXCLUDED.car_number, "
                "car_class=EXCLUDED.car_class, car_class_label=EXCLUDED.car_class_label, "
                "phone=EXCLUDED.phone, username=EXCLUDED.username, profile_link=EXCLUDED.profile_link, "
                "has_photo=EXCLUDED.has_photo, docs_verified=EXCLUDED.docs_verified",
                uid, d.get("name"), d.get("first_name"), d.get("last_name"),
                d.get("car_model"), d.get("car_year"), d.get("car_number"),
                d.get("car_class"), d.get("car_class_label"), d.get("phone"),
                d.get("username"), d.get("profile_link"),
                1 if d.get("has_photo") else 0,
                1 if d.get("docs_verified") else 0,
                d.get("registered_at", now_iso())
            )

    @staticmethod
    async def driver_update_fields(uid, **fields):
        allowed = {"car_model","car_year","car_number","car_class","car_class_label"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields: return
        i = 1
        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k}=${i}")
            vals.append(v)
            i += 1
        vals.append(uid)
        async with _pg_pool.acquire() as c:
            await c.execute(f"UPDATE drivers SET {', '.join(sets)} WHERE user_id=${i}", *vals)

    @staticmethod
    async def driver_del(uid):
        async with _pg_pool.acquire() as c:
            async with c.transaction():
                rows = await c.fetch(
                    "SELECT id, passenger_id FROM orders WHERE driver_id=$1 AND status='taken'", uid)
                active_orders = [(r["id"], r["passenger_id"]) for r in rows]

                trip_rows = await c.fetch(
                    "SELECT id FROM trips WHERE driver_id=$1 AND status IN ('open','full')", uid)
                trip_ids = [r["id"] for r in trip_rows]
                active_trip_bookings = []
                if trip_ids:
                    booking_rows = await c.fetch(
                        "SELECT trip_id, passenger_id FROM trip_bookings "
                        "WHERE trip_id = ANY($1::int[]) AND status='confirmed'", trip_ids)
                    active_trip_bookings = [(r["trip_id"], r["passenger_id"]) for r in booking_rows]
                    await c.execute(
                        "UPDATE trip_bookings SET status='cancelled_by_driver' "
                        "WHERE trip_id = ANY($1::int[]) AND status='confirmed'", trip_ids)
                    await c.execute(
                        "UPDATE trips SET status='cancelled' WHERE driver_id=$1 AND status IN ('open','full')", uid)

                await c.execute("UPDATE ratings SET driver_id=NULL WHERE driver_id=$1", uid)
                for tbl in ("drivers","subscriptions","pending_subscriptions"):
                    await c.execute(f"DELETE FROM {tbl} WHERE user_id=$1", uid)
                await c.execute(
                    "UPDATE orders SET status='open',driver_id=NULL,taken_at=NULL WHERE driver_id=$1 AND status='taken'", uid)
        return active_orders, active_trip_bookings

    @staticmethod
    async def driver_verify(uid, v):
        try:
            async with _pg_pool.acquire() as c:
                await c.execute("UPDATE drivers SET docs_verified=$1 WHERE user_id=$2", 1 if v else 0, uid)
        except Exception as e:
            log.error(f"driver_verify uid={uid}: {e}")
            raise

    @staticmethod
    async def all_drivers():
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM drivers")
        return [dict(r) for r in rows]

    @staticmethod
    async def active_drivers():
        today = now_dt().date().strftime("%Y-%m-%d")
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("""
                SELECT d.* FROM drivers d
                JOIN subscriptions s ON s.user_id=d.user_id
                WHERE d.docs_verified=1 AND s.expires_date>=$1
                AND d.user_id NOT IN (SELECT user_id FROM blacklist)
            """, today)
        return [dict(r) for r in rows]

    # ── ПОДПИСКИ ──
    @staticmethod
    async def sub_info(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", uid)
        if not r: return None, 0, False
        try:
            exp  = datetime.strptime(r["expires_date"], "%Y-%m-%d").date()
            days = max(0, (exp - now_dt().date()).days)
            return r["expires_date"], days, days > 0
        except:
            return r["expires_date"], 0, False

    @staticmethod
    async def sub_set(uid, exp, admin_id=None, plan_key=None):
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO subscriptions VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET expires_date=EXCLUDED.expires_date",
                uid, exp)
            if admin_id and plan_key:
                await c.execute(
                    "INSERT INTO subscription_log (user_id,plan_key,admin_id,action,created_at) VALUES ($1,$2,$3,$4,$5)",
                    uid, plan_key, admin_id, "activate", now_iso())

    @staticmethod
    async def sub_expire(uid):
        async with _pg_pool.acquire() as c:
            await c.execute("DELETE FROM subscriptions WHERE user_id=$1", uid)

    @staticmethod
    async def sub_extend_all(days: int, admin_id=None, plan_key="bulk_extend"):
        """
        Продлевает подписку на `days` дней всем зарегистрированным водителям.
        Если у водителя уже есть активная подписка — продлевает от текущей
        даты окончания. Если подписки нет или она истекла — считает от сегодня.
        Возвращает список user_id, кому продлили.
        """
        today = now_dt().date()
        async with _pg_pool.acquire() as c:
            uids = [r["user_id"] for r in await c.fetch("SELECT user_id FROM drivers")]
            updated = []
            for uid in uids:
                r = await c.fetchrow("SELECT expires_date FROM subscriptions WHERE user_id=$1", uid)
                base = today
                if r and r["expires_date"]:
                    try:
                        cur_exp = datetime.strptime(r["expires_date"], "%Y-%m-%d").date()
                        if cur_exp > today:
                            base = cur_exp
                    except:
                        pass
                new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%d")
                await c.execute(
                    "INSERT INTO subscriptions VALUES ($1,$2) "
                    "ON CONFLICT (user_id) DO UPDATE SET expires_date=EXCLUDED.expires_date",
                    uid, new_exp)
                if admin_id:
                    await c.execute(
                        "INSERT INTO subscription_log (user_id,plan_key,admin_id,action,created_at) "
                        "VALUES ($1,$2,$3,$4,$5)",
                        uid, plan_key, admin_id, "bulk_extend", now_iso())
                updated.append(uid)
            return updated

    # ── ЗАКАЗЫ ──
    @staticmethod
    async def order_create(data):
        async with _pg_pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO orders (passenger_id,from_city,to_city,trip_date,trip_time,"
                "passengers,car_class,wishes,distance_km,price,status,created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id",
                data.get("passenger_id"), data.get("from_city"), data.get("to_city"),
                data.get("trip_date"), data.get("trip_time"), data.get("passengers"),
                data.get("car_class"), data.get("wishes"), data.get("distance_km"),
                data.get("price"), data.get("status","pending"), now_iso()
            )
        return row["id"]

    @staticmethod
    async def order(oid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM orders WHERE id=$1", oid)
        return dict(r) if r else None

    @staticmethod
    async def order_relink_passenger(oid, real_uid):
        """Заменяет синтетический passenger_id (с сайта) на реальный Telegram id,
        когда пассажир открывает бота по спецссылке из заявки. Не трогает заказ,
        если он уже привязан к другому реальному (положительному) id."""
        async with _pg_pool.acquire() as c:
            row = await c.fetchrow("SELECT passenger_id FROM orders WHERE id=$1", oid)
            if row and row["passenger_id"] and row["passenger_id"] < 0:
                await c.execute("UPDATE orders SET passenger_id=$1 WHERE id=$2", real_uid, oid)

    @staticmethod
    async def order_upd(oid, **kw):
        dropped = set(kw) - ALLOWED_COLS
        if dropped:
            log.warning(f"order_upd: проигнорированы недопустимые поля {dropped} для заказа {oid}")
        kw = {k: v for k, v in kw.items() if k in ALLOWED_COLS}
        if not kw: return
        i = 1
        sets = []
        vals = []
        for k, v in kw.items():
            sets.append(f"{k}=${i}")
            vals.append(v)
            i += 1
        vals.append(oid)
        async with _pg_pool.acquire() as c:
            await c.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=${i}", *vals)

    @staticmethod
    async def order_cancel_atomic(oid, uid, role="passenger"):
        async with _pg_pool.acquire() as c:
            if role == "passenger":
                result = await c.execute(
                    "UPDATE orders SET status='cancelled' WHERE id=$1 AND passenger_id=$2 AND status IN ('open','taken','pending')",
                    oid, uid)
            else:
                result = await c.execute(
                    "UPDATE orders SET status='open',driver_id=NULL,taken_at=NULL WHERE id=$1 AND driver_id=$2 AND status='taken'",
                    oid, uid)
        return int(result.split()[-1]) > 0

    @staticmethod
    async def order_take_atomic(oid, uid):
        async with _pg_pool.acquire() as c:
            result = await c.execute(
                "UPDATE orders SET status='taken',driver_id=$1,taken_at=$2 WHERE id=$3 AND status='open'",
                uid, now_iso(), oid)
        return int(result.split()[-1]) > 0

    @staticmethod
    async def passenger_orders(uid, limit=5):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM orders WHERE passenger_id=$1 ORDER BY created_at DESC LIMIT $2", uid, limit)
        return [dict(r) for r in rows]

    @staticmethod
    async def driver_orders(uid, limit=5):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM orders WHERE driver_id=$1 ORDER BY taken_at DESC LIMIT $2", uid, limit)
        return [dict(r) for r in rows]

    @staticmethod
    async def open_orders():
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM orders WHERE status='open' ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    @staticmethod
    async def all_orders(limit=10, offset=0, status=None):
        async with _pg_pool.acquire() as c:
            if status:
                rows  = await c.fetch(
                    "SELECT * FROM orders WHERE status=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                    status, limit, offset)
                total = await c.fetchval("SELECT COUNT(*) FROM orders WHERE status=$1", status)
            else:
                rows  = await c.fetch(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT $1 OFFSET $2", limit, offset)
                total = await c.fetchval("SELECT COUNT(*) FROM orders")
        return [dict(r) for r in rows], total

    @staticmethod
    async def stats():
        today = now_dt().date().strftime("%Y-%m-%d")
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM orders)                    as t,
                    (SELECT COUNT(*) FROM orders WHERE status='open') as o,
                    (SELECT COUNT(*) FROM orders WHERE status='completed') as d,
                    (SELECT COUNT(*) FROM drivers)                   as dr,
                    (SELECT COUNT(*) FROM drivers WHERE docs_verified=1) as dc,
                    (SELECT COUNT(*) FROM drivers d
                     JOIN subscriptions s ON s.user_id=d.user_id
                     WHERE s.expires_date>=$1)                       as sub
            """, today)
        return {"total": r["t"], "open": r["o"], "done": r["d"],
                "drivers": r["dr"], "docs_ok": r["dc"], "subscribed": r["sub"]}

    # ── ЧЁРНЫЙ СПИСОК ──
    @staticmethod
    async def bl_check(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT 1 FROM blacklist WHERE user_id=$1", uid)
        return r is not None

    @staticmethod
    async def bl_add(uid):
        async with _pg_pool.acquire() as c:
            await c.execute("INSERT INTO blacklist VALUES ($1) ON CONFLICT DO NOTHING", uid)

    @staticmethod
    async def bl_remove(uid):
        async with _pg_pool.acquire() as c:
            await c.execute("DELETE FROM blacklist WHERE user_id=$1", uid)

    @staticmethod
    async def bl_all():
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT user_id FROM blacklist")
        return [r["user_id"] for r in rows]

    # ── ОЖИДАЮЩИЕ ПОДПИСКИ ──
    @staticmethod
    async def pending_set(uid, pk):
        async with _pg_pool.acquire() as c:
            await c.execute(
                "INSERT INTO pending_subscriptions VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET plan_key=EXCLUDED.plan_key",
                uid, pk)

    @staticmethod
    async def pending_get(uid):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT plan_key FROM pending_subscriptions WHERE user_id=$1", uid)
        return r["plan_key"] if r else None

    @staticmethod
    async def pending_del(uid, admin_id=None, plan_key=None):
        async with _pg_pool.acquire() as c:
            await c.execute("DELETE FROM pending_subscriptions WHERE user_id=$1", uid)
            if admin_id and plan_key:
                await c.execute(
                    "INSERT INTO subscription_log (user_id,plan_key,admin_id,action,created_at) VALUES ($1,$2,$3,$4,$5)",
                    uid, plan_key, admin_id, "reject", now_iso())

    @staticmethod
    async def pending_all():
        async with _pg_pool.acquire() as c:
            rows = await c.fetch("SELECT * FROM pending_subscriptions")
        return [dict(r) for r in rows]

    # ── РЕЙТИНГИ ──
    @staticmethod
    async def add_rating(order_id, driver_id, passenger_id, stars):
        async with _pg_pool.acquire() as c:
            existing = await c.fetchrow(
                "SELECT id FROM ratings WHERE order_id=$1 AND passenger_id=$2", order_id, passenger_id)
            if existing:
                await c.execute(
                    "UPDATE ratings SET stars=$1,driver_id=$2,created_at=$3 WHERE order_id=$4 AND passenger_id=$5",
                    stars, driver_id, now_iso(), order_id, passenger_id)
            else:
                await c.execute(
                    "INSERT INTO ratings (order_id,driver_id,passenger_id,stars,created_at) VALUES ($1,$2,$3,$4,$5)",
                    order_id, driver_id, passenger_id, stars, now_iso())

    @staticmethod
    async def avg_rating(driver_id):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow(
                "SELECT AVG(stars) as avg, COUNT(*) as cnt FROM ratings WHERE driver_id=$1", driver_id)
        return (round(float(r["avg"]), 1), r["cnt"]) if r and r["cnt"] > 0 else (0.0, 0)

    @staticmethod
    async def has_rating(order_id, passenger_id):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow(
                "SELECT 1 FROM ratings WHERE order_id=$1 AND passenger_id=$2", order_id, passenger_id)
        return r is not None

    # ── РЕЙСЫ ──
    @staticmethod
    async def trip_create(data: dict) -> int:
        async with _pg_pool.acquire() as c:
            row = await c.fetchrow(
                "INSERT INTO trips "
                "(driver_id,from_city,to_city,trip_date,trip_time,car_class,car_class_label,"
                "seats_total,seats_free,price_per_seat,distance_km,region,status,created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$8,$9,$10,$11,'open',$12) RETURNING id",
                data["driver_id"], data["from_city"], data["to_city"],
                data["trip_date"], data["trip_time"], data["car_class"], data["car_class_label"],
                data["seats_total"], data["price_per_seat"],
                data.get("distance_km"), data.get("region","rf"), now_iso()
            )
        return row["id"]

    @staticmethod
    async def trip(tid: int):
        async with _pg_pool.acquire() as c:
            r = await c.fetchrow("SELECT * FROM trips WHERE id=$1", tid)
        return dict(r) if r else None

    @staticmethod
    async def trips_search(from_city: str, to_city: str, trip_date: str):
        current_date = now_dt().date().strftime("%Y-%m-%d")
        min_time = now_dt().strftime("%H:%M") if trip_date == current_date else "00:00"
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM trips "
                "WHERE LOWER(from_city)=LOWER($1) AND LOWER(to_city)=LOWER($2) "
                "AND trip_date=$3 AND trip_time >= $4 AND status='open' AND seats_free>0 "
                "ORDER BY trip_time ASC",
                from_city, to_city, trip_date, min_time
            )
        return [dict(r) for r in rows]

    @staticmethod
    async def trip_book_atomic(tid: int, passenger_id: int, seats: int = 1):
        async with _pg_pool.acquire() as c:
            async with c.transaction():
                # Явная проверка дубля внутри транзакции
                existing = await c.fetchrow(
                    "SELECT 1 FROM trip_bookings WHERE trip_id=$1 AND passenger_id=$2",
                    tid, passenger_id
                )
                if existing:
                    return False
                result = await c.execute(
                    "UPDATE trips SET seats_free=seats_free-$1 "
                    "WHERE id=$2 AND seats_free>=$1 AND status='open'",
                    seats, tid
                )
                if int(result.split()[-1]) == 0:
                    return False
                await c.execute(
                    "INSERT INTO trip_bookings(trip_id,passenger_id,seats,status,created_at) "
                    "VALUES ($1,$2,$3,'confirmed',$4)",
                    tid, passenger_id, seats, now_iso()
                )
                await c.execute(
                    "UPDATE trips SET status='full' WHERE id=$1 AND seats_free=0", tid)
        return True

    @staticmethod
    async def trip_cancel_booking(tid: int, passenger_id: int):
        async with _pg_pool.acquire() as c:
            async with c.transaction():
                b = await c.fetchrow(
                    "SELECT seats FROM trip_bookings "
                    "WHERE trip_id=$1 AND passenger_id=$2 AND status='confirmed'",
                    tid, passenger_id
                )
                if not b: return False
                await c.execute(
                    "UPDATE trip_bookings SET status='cancelled_by_passenger' "
                    "WHERE trip_id=$1 AND passenger_id=$2", tid, passenger_id)
                await c.execute(
                    "UPDATE trips SET seats_free=seats_free+$1, "
                    "status=CASE WHEN status='full' THEN 'open' ELSE status END WHERE id=$2",
                    b["seats"], tid)
        return True

    @staticmethod
    async def trip_reject_passenger(tid: int, passenger_id: int):
        async with _pg_pool.acquire() as c:
            async with c.transaction():
                b = await c.fetchrow(
                    "SELECT seats FROM trip_bookings "
                    "WHERE trip_id=$1 AND passenger_id=$2 AND status='confirmed'",
                    tid, passenger_id)
                if not b: return False
                await c.execute(
                    "UPDATE trip_bookings SET status='cancelled_by_driver' "
                    "WHERE trip_id=$1 AND passenger_id=$2", tid, passenger_id)
                await c.execute(
                    "UPDATE trips SET seats_free=seats_free+$1, "
                    "status=CASE WHEN status='full' THEN 'open' ELSE status END WHERE id=$2",
                    b["seats"], tid)
        return True

    @staticmethod
    async def trip_cancel(tid: int, driver_id: int):
        async with _pg_pool.acquire() as c:
            async with c.transaction():
                result = await c.execute(
                    "UPDATE trips SET status='cancelled' "
                    "WHERE id=$1 AND driver_id=$2 AND status IN ('open','full')",
                    tid, driver_id)
                if int(result.split()[-1]) == 0: return None
                rows = await c.fetch(
                    "SELECT passenger_id FROM trip_bookings "
                    "WHERE trip_id=$1 AND status='confirmed'", tid)
                await c.execute(
                    "UPDATE trip_bookings SET status='cancelled_by_driver' "
                    "WHERE trip_id=$1 AND status='confirmed'", tid)
        return [r["passenger_id"] for r in rows]

    @staticmethod
    async def trip_complete(tid: int, driver_id: int):
        async with _pg_pool.acquire() as c:
            async with c.transaction():
                result = await c.execute(
                    "UPDATE trips SET status='done' "
                    "WHERE id=$1 AND driver_id=$2 AND status IN ('open','full')",
                    tid, driver_id)
                if int(result.split()[-1]) == 0: return None
                rows = await c.fetch(
                    "SELECT passenger_id FROM trip_bookings "
                    "WHERE trip_id=$1 AND status='confirmed'", tid)
        return [r["passenger_id"] for r in rows]

    @staticmethod
    async def driver_trips(driver_id: int):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM trips WHERE driver_id=$1 ORDER BY trip_date DESC, trip_time DESC LIMIT 20",
                driver_id)
        return [dict(r) for r in rows]

    @staticmethod
    async def trip_bookings(tid: int):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT * FROM trip_bookings WHERE trip_id=$1 AND status='confirmed'", tid)
        return [dict(r) for r in rows]

    @staticmethod
    async def passenger_bookings(passenger_id: int):
        async with _pg_pool.acquire() as c:
            rows = await c.fetch(
                "SELECT tb.*, t.from_city, t.to_city, t.trip_date, t.trip_time, "
                "t.price_per_seat, t.car_class_label, t.driver_id, t.status as trip_status, "
                "t.distance_km "
                "FROM trip_bookings tb JOIN trips t ON t.id=tb.trip_id "
                "WHERE tb.passenger_id=$1 AND tb.status='confirmed' "
                "ORDER BY t.trip_date DESC, t.trip_time DESC LIMIT 10",
                passenger_id)
        return [dict(r) for r in rows]

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

def profile_link_html(d):
    """tg://-ссылки Telegram не делает кликабельными в обычном тексте — оборачиваем в <a href>."""
    pl = profile_link(d)
    return f'<a href="{pl}">Открыть</a>' if pl.startswith("http") else pl

def check_brand(model, cl):
    if cl == "standard": return None
    for b in LOW_BRANDS:
        if b in model.lower():
            return f"❌ {model} — только Стандарт"
    return None

def is_nt(city: str) -> bool:
    c = city.lower()
    return any(re.search(r'\b' + re.escape(kw) + r'\b', c) for kw in NT_KW)

def is_cis(city: str) -> bool:
    c = city.lower()
    return any(re.search(r'\b' + re.escape(kw) + r'\b', c) for kw in CIS_KW)

def route_region(from_city: str, to_city: str) -> str:
    """Приоритет: НТ > Кавказ/СНГ > РФ"""
    if is_nt(from_city) or is_nt(to_city):   return "nt"
    if is_cis(from_city) or is_cis(to_city): return "cis"
    return "rf"

def tariffs(cf="", ct=""):
    r = route_region(cf, ct)
    if r == "nt":  return TARIFFS_NT
    if r == "cis": return TARIFFS_CIS
    return TARIFFS_RF

def calc_price(dist, cc, cf, ct, trip_mode=False):
    """trip_mode=True — цена за место в рейсе (price_seat)."""
    t = tariffs(cf, ct)
    entry = t.get(cc, t.get("standard", {"price": 25, "price_seat": 11}))
    rate = entry.get("price_seat" if trip_mode else "price", 25)
    return round(dist * rate)

def fmt_price(p): return f"{p:,}".replace(",", " ") + " ₽"

def is_valid_city(c):
    return bool(c) and len(c.strip()) >= 2 and bool(re.match(r"^[а-яА-ЯёЁa-zA-Z\s\-\.]+$", c.strip()))

_DATE_RE = re.compile(r"^(\d{1,2})[.\-/\s]*(\d{1,2})[.\-/\s]*(\d{2,4})$")
_TIME_RE = re.compile(r"^(\d{1,2})[:.\-\s]*(\d{2})$")

def parse_ru_date(raw):
    """
    ДД.ММ.ГГГГ и щадящие варианты: без точек (30062026), с -/. /space,
    двузначный год (26 -> 2026). None при неверном формате (в т.ч. raw=None).
    """
    text = (raw or "").strip()
    m = _DATE_RE.match(text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None

def parse_ru_time(raw):
    """
    ЧЧ:ММ и щадящие варианты: без двоеточия (1430), с точкой/тире/пробелом.
    Возвращает (час, минута) БЕЗ проверки диапазона — её делает вызывающий
    код, чтобы сохранить отдельное сообщение об ошибке для 25:99 и т.п.
    None при неверном формате (в т.ч. raw=None).
    """
    text = (raw or "").strip()
    m = _TIME_RE.match(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def fmt_date_ru(iso_str):
    """'YYYY-MM-DD' -> 'ДД.ММ.ГГГГ' для показа пользователю. Пусто/None/битый формат -> как есть."""
    if not iso_str:
        return "—"
    try:
        return datetime.strptime(iso_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso_str

async def ask_city_step(uid, raw_text, err_invalid, *, duplicate_of=None, err_duplicate=None):
    """
    Общий шаг ввода города для OrderForm/TripForm/SearchTripForm:
    валидирует, при необходимости проверяет совпадение с другим городом,
    при ошибке сама шлёт сообщение и возвращает None — иначе возвращает
    очищенное название города.
    """
    city = (raw_text or "").strip()
    if not is_valid_city(city):
        await safe_send(uid, err_invalid)
        return None
    if duplicate_of and city.lower() == duplicate_of.lower():
        await safe_send(uid, err_duplicate)
        return None
    return city

def geocode(city):
    if not geolocator or not is_valid_city(city): return None
    try:
        loc = geolocator.geocode(city, timeout=5)
        if loc: return loc.latitude, loc.longitude
    except Exception as e:
        log.error(f"Геокодирование {city}: {e}")
    return None

async def _geocode_async(city: str):
    """Геокодирование без блокировки event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, geocode, city)

async def validate_city_geocode(city: str) -> tuple:
    """Возвращает (city_valid, geo_available).
    Если геокодер недоступен — не блокируем пользователя."""
    if not geolocator:
        return True, False
    try:
        result = await asyncio.wait_for(_geocode_async(city), timeout=6.0)
        return result is not None, True
    except asyncio.TimeoutError:
        log.warning(f"Геокодер таймаут для '{city}' — пропускаем валидацию")
        return True, False
    except Exception as e:
        log.warning(f"Геокодер недоступен для '{city}': {e}")
        return True, False

async def _osrm_distance(coords_from: tuple, coords_to: tuple):
    """Реальное расстояние по дорогам через публичный OSRM."""
    try:
        lat1, lon1 = coords_from
        lat2, lon2 = coords_to
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{lon1},{lat1};{lon2},{lat2}?overview=false")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == "Ok":
                        meters = data["routes"][0]["distance"]
                        return round(meters / 1000)
    except Exception as e:
        log.warning(f"OSRM недоступен: {e}")
    return None

def get_distance(cf: str, ct: str):
    """Синхронный геодезический расчёт (fallback)."""
    c1, c2 = geocode(cf), geocode(ct)
    if not c1 or not c2: return None
    region = route_region(cf, ct)
    coeff = 1.45 if region == "cis" else (1.35 if region == "nt" else 1.25)
    return round(geo_dist(c1, c2).km * coeff)

async def get_distance_async(cf: str, ct: str):
    """Асинхронный расчёт: сначала OSRM, fallback — геодезическое × коэффициент."""
    coords_from, coords_to = await asyncio.gather(
        _geocode_async(cf), _geocode_async(ct)
    )
    if not coords_from or not coords_to:
        return None
    osrm_km = await _osrm_distance(coords_from, coords_to)
    if osrm_km:
        log.info(f"OSRM: {cf}→{ct} = {osrm_km} км")
        return osrm_km
    region = route_region(cf, ct)
    coeff = 1.45 if region == "cis" else (1.35 if region == "nt" else 1.25)
    km = round(geo_dist(coords_from, coords_to).km * coeff)
    log.info(f"Геодезическое (coeff={coeff}): {cf}→{ct} = {km} км")
    return km

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
    region = route_region(o.get("from_city",""), o.get("to_city",""))
    region_label = REGION_LABELS.get(region, "🇷🇺 Россия")
    lines = [
        f"🚕 <b>Заказ #{o['id']}</b> · {region_label}",
        f"📍 {esc(o.get('from_city'))} → {esc(o.get('to_city'))}",
        f"📏 {dt} | 📅 {esc(o.get('trip_date'))} | 🕐 {esc(o.get('trip_time'))}",
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


def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🚕 Создать заказ"),        KeyboardButton(text="🚗 Я водитель")],
            [KeyboardButton(text="🚐 Поехать вместе"),       KeyboardButton(text="🎫 Мои брони")],
            [KeyboardButton(text="📋 Мои заказы"),           KeyboardButton(text="📊 Тарифы")],
        ],
        resize_keyboard=True,
    )

async def kb_driver(uid):
    _, _, active = await DB.sub_info(uid)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Доступные заказы"),  KeyboardButton(text="🚐 Создать рейс")],
            [KeyboardButton(text="🗓 Мои рейсы"),         KeyboardButton(text="👤 Мой профиль")],
            [KeyboardButton(text="💳 Абонемент"),
             KeyboardButton(text="✅ Подписка активна" if active else "❌ Нет подписки")],
            [KeyboardButton(text="📈 Мои поездки"),       KeyboardButton(text="🔙 Главное меню")],
        ],
        resize_keyboard=True,
    )

def kb_cancel():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True,
    )

def kb_car_class(region="rf", passengers=1):
    t = TARIFFS_NT if region == "nt" else (TARIFFS_CIS if region == "cis" else TARIFFS_RF)
    unit = "₽/км/место" if region == "cis" else "₽/км"
    classes = ["minivan"] if passengers >= 5 else list(t.keys())
    rows = []
    for cc in classes:
        if cc in t:
            txt = f"{t[cc]['label']} · {t[cc]['price_seat'] if region == 'cis' else t[cc]['price']} {unit}"
            if cc == "minivan" and passengers >= 5:
                txt += " ✅"
            rows.append([InlineKeyboardButton(text=txt, callback_data=f"car_{cc}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pclass():
    rows = []
    for k in COMFORT_H + ["minivan"]:
        v  = TARIFFS_RF[k]
        yr = f" ({v['year']})" if v.get("year") else ""
        rows.append([InlineKeyboardButton(text=f"{v['label']}{yr}", callback_data=f"pclass_{k}")])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="reg_cancel_class")])
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

def fmt_trip(t, drv=None, avg=None, cnt=None):
    region_label = REGION_LABELS.get(t.get("region","rf"), "🇷🇺 Россия")
    seats_total = t.get("seats_total", 0)
    seats_free  = t.get("seats_free", 0)
    seats_taken = seats_total - seats_free
    dist = t.get("distance_km")
    dist_str = f"{int(dist)} км" if dist else "—"
    status_map = {"open":"🟢 Открыт","full":"🔵 Набран","cancelled":"❌ Отменён","done":"✅ Завершён"}
    lines = [
        f"🚐 <b>Рейс #{t['id']}</b> · {region_label}",
        f"📍 {esc(t.get('from_city'))} → {esc(t.get('to_city'))}",
        f"📅 {esc(fmt_date_ru(t.get('trip_date')))} · 🕐 {esc(t.get('trip_time'))}",
        f"📏 {dist_str}",
        f"🚘 {esc(t.get('car_class_label'))}",
        f"💺 Мест: {seats_taken}/{seats_total}",
        f"💰 <b>{fmt_price(t.get('price_per_seat',0))}/место</b>",
        f"📌 {status_map.get(t.get('status','open'), t.get('status',''))}",
    ]
    if drv:
        r_str = f"⭐ {avg}/5 ({cnt} оц.)" if cnt else "⭐ Новый водитель"
        lines.append(f"👤 {drv_name(drv)} · {r_str}")
    return "\n".join(lines)

def kb_trip_car_class(region: str):
    t = TARIFFS_CIS if region == "cis" else (TARIFFS_NT if region == "nt" else TARIFFS_RF)
    rows = []
    for cc, info in t.items():
        rows.append([InlineKeyboardButton(
            text=f"{info['label']} · {info['price_seat']} ₽/км/место",
            callback_data=f"trip_class_{cc}"
        )])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="trip_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_trip_seats():
    btns = [InlineKeyboardButton(text=str(i), callback_data=f"trip_seats_{i}") for i in range(1, 9)]
    rows = [btns[i:i+2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="trip_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def yandex_maps_url(from_city: str, to_city: str) -> str:
    import urllib.parse
    f = urllib.parse.quote(from_city)
    t = urllib.parse.quote(to_city)
    return f"https://yandex.ru/maps/?rtext={f}~{t}&rtt=auto"


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
        await bot.edit_message_reply_markup(chat_id=cid, message_id=mid, **kw)
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
        if uid and uid in _BL_CACHE:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Вы заблокированы.", show_alert=True)
            else:
                await safe_send(uid, "⛔ Вы заблокированы.")
            return
        return await handler(event, data)

_blacklist_mw = BlacklistMiddleware()
dp.message.outer_middleware(_blacklist_mw)
dp.callback_query.outer_middleware(_blacklist_mw)

# ══════════════ КЭШ УВЕДОМЛЕНИЙ ══════════════
_NOTIFIED: dict = {}
_NLOCK    = asyncio.Lock()
_BL_CACHE: set = set()

async def _notify_drivers(oid, exclude_uid=None):
    order = await DB.order(oid)
    if not order or order["status"] != "open": return
    async with _NLOCK:
        notified = set(_NOTIFIED.get(oid, set()))
    drivers = [d for d in await DB.active_drivers()
               if not (exclude_uid and d["user_id"] == exclude_uid)
               and d["user_id"] != order["passenger_id"]
               and d["user_id"] not in notified]
    sent = 0
    newly_notified: set = set()
    for drv in drivers[:NOTIFY_LIMIT]:
        ct, _ = can_take_order(drv, order)
        if not ct: continue
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять",       callback_data=f"take_{oid}")],
            [InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"skip_{oid}")],
        ])
        await safe_send(drv["user_id"], f"🔔 <b>Подходит вам!</b>\n\n{fmt_order(order)}", reply_markup=kb)
        newly_notified.add(drv["user_id"])
        sent += 1
        if sent % NOTIFY_BATCH == 0:
            await asyncio.sleep(NOTIFY_DELAY)
    # Один захват лока для всего батча вместо захвата на каждого водителя
    if newly_notified:
        async with _NLOCK:
            _NOTIFIED.setdefault(oid, set()).update(newly_notified)

async def _clear_notified(oid):
    async with _NLOCK:
        _NOTIFIED.pop(oid, None)

TRIAL_DAYS = 50  # длительность пробного периода

async def _trial_expiry_notifier():
    """Раз в сутки проверяет подписки и уведомляет водителей об окончании."""
    while True:
        try:
            await asyncio.sleep(24 * 3600)
            today      = now_dt().date()
            warn3      = (today + timedelta(days=3)).strftime("%Y-%m-%d")
            warn1      = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            expired    = today.strftime("%Y-%m-%d")
            drivers    = await DB.all_drivers()
            for drv in drivers:
                uid = drv["user_id"]
                exp_str, dl, active = await DB.sub_info(uid)
                if not exp_str:
                    continue
                if exp_str == warn3:
                    await safe_send(uid,
                        f"⚠️ <b>Подписка заканчивается через 3 дня</b> ({fmt_date_ru(exp_str)})!\n\n"
                        f"Продлите абонемент чтобы продолжать получать заказы.",
                        reply_markup=kb_subs())
                elif exp_str == warn1:
                    await safe_send(uid,
                        f"🔔 <b>Подписка заканчивается завтра</b> ({fmt_date_ru(exp_str)})!\n\n"
                        f"Продлите абонемент чтобы не потерять доступ к заказам.",
                        reply_markup=kb_subs())
                elif exp_str == expired and not active:
                    # Уведомляем ровно один раз — в день истечения (не каждый день после)
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
                async with _pg_pool.acquire() as c:
                    rows = await c.fetch(
                        f"SELECT id, status FROM orders WHERE id = ANY($1::bigint[])",
                        current_oids)
                alive = {r["id"] for r in rows if r["status"] == "open"}
                dead  = [oid for oid in current_oids if oid not in alive]
            except Exception as e:
                log.error(f"_notified_cleaner (ошибка БД): {e}")
                await asyncio.sleep(5)
                continue
            if dead:
                async with _NLOCK:
                    for oid in dead:
                        _NOTIFIED.pop(oid, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"_notified_cleaner упала: {e}", exc_info=True)
            await asyncio.sleep(5)

# ══════════════ КАНАЛ ══════════════
async def _post_to_channel(oid):
    if not GROUP_CHAT_ID: return
    order = await DB.order(oid)
    if not order or order["status"] != "open": return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Взять заказ",
                             url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}")
    ]])
    msg = await safe_send(GROUP_CHAT_ID, fmt_order(order), reply_markup=kb)
    if msg: await DB.order_upd(oid, channel_msg_id=msg.message_id)

async def update_channel_post(oid):
    order = await DB.order(oid)
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
                text=fmt_order(order) + f"\n\n{status_line}",
                chat_id=GROUP_CHAT_ID,
                message_id=order["channel_msg_id"],
                reply_markup=None)
        except Exception as e:
            log.error(f"Ошибка обновления поста: {e}")
    elif status == "open":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Взять заказ",
                                 url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}")
        ]])
        try:
            await bot.edit_message_text(
                text=fmt_order(order),
                chat_id=GROUP_CHAT_ID,
                message_id=order["channel_msg_id"],
                reply_markup=kb)
        except Exception as e:
            log.error(f"Ошибка обновления поста: {e}")


# ══════════════ WEB API (форма заказа с сайта мтранс.рф) ══════════════
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import time

api = FastAPI(title="МТранс Order API")

api.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://мтранс.рф", "https://xn--80axekhc.xn--p1ai",
        "https://www.мтранс.рф", "https://www.xn--80axekhc.xn--p1ai",
    ],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

VALID_CAR_CLASSES = {"standard", "comfort", "comfort+", "minivan", "business"}

@api.get("/")
async def root():
    return {"service": "МТранс Order API", "status": "ok"}

@api.get("/health")
async def health():
    ok_db = _pg_pool is not None
    return {"status": "ok" if ok_db else "no_db", "db_pool": ok_db}

class SiteOrderRequest(BaseModel):
    from_city: str
    to_city: str
    trip_date: str            # "YYYY-MM-DD"
    trip_time: str = ""
    passengers: int = Field(1, ge=1, le=8)
    car_class: str = "standard"
    name: str = ""
    phone: str
    wishes: str = ""

@api.post("/api/order")
async def create_site_order(req: SiteOrderRequest):
    if req.car_class not in VALID_CAR_CLASSES:
        raise HTTPException(400, "Некорректный класс авто")
    if not req.phone.strip():
        raise HTTPException(400, "Укажите телефон")

    dist = await get_distance_async(req.from_city, req.to_city)
    dkm = price = None
    if dist:
        dkm   = round(dist)
        price = calc_price(dkm, req.car_class, req.from_city, req.to_city)
    region = route_region(req.from_city, req.to_city)

    # Синтетический отрицательный id — заказ пришёл с сайта, не из Telegram
    site_passenger_id = -int(time.time() * 1000) % 9_000_000_000

    contact_note = f"📞 {req.phone}" + (f", {req.name}" if req.name else "")
    full_wishes = (req.wishes + "\n" if req.wishes else "") + f"🌐 Заявка с сайта. {contact_note}"

    oid = await DB.order_create({
        "passenger_id": site_passenger_id,
        "from_city": req.from_city, "to_city": req.to_city,
        "trip_date": req.trip_date, "trip_time": req.trip_time,
        "passengers": req.passengers, "car_class": req.car_class,
        "wishes": full_wishes, "distance_km": dkm, "price": price,
        "status": "open" if dkm else "pending", "region": region,
    })

    if dkm:
        await _post_to_channel(oid)
        await _notify_drivers(oid)
    else:
        for aid in ADMIN_IDS:
            await safe_send(aid, f"⚠️ Заявка с сайта #{oid} без расстояния!\n"
                            f"{req.from_city} → {req.to_city}\n"
                            f"Используйте /recalc {oid} <distance_km> <price>")

    return {
        "order_id": oid,
        "distance_km": dkm,
        "price": price,
        "status": "open" if dkm else "pending",
        "telegram_link": f"https://t.me/{BOT_USERNAME}?start=orderlink_{oid}",
        # ⚠️ Замени VK_GROUP на короткое имя вашего VK-сообщества (см. vk.com/[имя]),
        # и убедись, что VK-бот умеет обрабатывать payload orderlink_<id> так же, как здесь.
        "vk_link": f"https://vk.me/VK_GROUP?ref=orderlink_{oid}",
    }

@api.get("/api/order/{oid}")
async def get_site_order(oid: int):
    order = await DB.order(oid)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return {
        "order_id": order["id"], "status": order["status"],
        "price": order["price"], "distance_km": order["distance_km"],
        "driver_id": order.get("driver_id"),
    }


# ══════════════ КОМАНДЫ ══════════════
@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    parts = msg.text.strip().split()

    if len(parts) > 1 and parts[1].startswith("orderlink_"):
        try:
            oid   = int(parts[1].split("_")[1])
            order = await DB.order(oid)
            if not order:
                await safe_send(uid, "⚠️ Заявка не найдена — возможно, устарела ссылка.", reply_markup=kb_main())
                return
            await DB.order_relink_passenger(oid, uid)
            await state.clear()
            status_txt = {
                "open": "🔎 Ищем свободного водителя...",
                "taken": "🚗 Водитель уже назначен, скоро свяжется с вами.",
                "done": "✅ Поездка завершена.",
                "cancelled": "❌ Заявка отменена.",
            }.get(order["status"], "")
            price_txt = f"\n💰 Цена: {fmt_price(order['price'])}" if order.get("price") else ""
            await safe_send(uid,
                f"🔗 <b>Заявка №{oid} привязана к этому чату.</b>\n"
                f"{order['from_city']} → {order['to_city']}{price_txt}\n\n"
                f"{status_txt}\n\nЯ пришлю сюда сообщение, как только водитель возьмёт заказ.",
                reply_markup=kb_main())
            return
        except Exception as e:
            log.error(f"orderlink deep-link: {e}")

    if len(parts) > 1 and parts[1].startswith("order_"):
        try:
            oid   = int(parts[1].split("_")[1])
            order = await DB.order(oid)
            if not order or order["status"] != "open":
                await safe_send(uid, "⚠️ Заказ не найден или уже взят.", reply_markup=kb_main()); return
            drv = await DB.driver(uid)
            if not drv:
                await safe_send(uid, "❌ Сначала зарегистрируйтесь как водитель.", reply_markup=kb_main()); return
            _, _, active = await DB.sub_info(uid)
            if not active:
                await safe_send(uid, "🔒 Нет абонемента.", reply_markup=await kb_driver(uid)); return
            if not drv.get("docs_verified"):
                await safe_send(uid, "⏳ Профиль ещё не верифицирован.", reply_markup=await kb_driver(uid)); return
            if order["passenger_id"] == uid:
                await safe_send(uid, "❌ Нельзя взять свой заказ.", reply_markup=await kb_driver(uid)); return
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
        f"добро пожаловать в Межгород Трансфер!\n\n"
        "🔹 Пассажирам — оформить заказ\n"
        "🔹 Водителям — получать заказы\n"
        "📢 Канал: @intercitytrans",
        reply_markup=kb_main())

@router.message(Command("help"), StateFilter("*"))
async def cmd_help(msg: Message, state: FSMContext):
    await safe_send(msg.chat.id,
        "📖 <b>Справка</b>\n\n"
        "<b>Пассажир:</b> Создать заказ, Мои заказы\n"
        "<b>Водитель:</b> Я водитель → Зарегистрироваться → Абонемент\n\n"
        "По вопросам: @Olegan7979")

@router.message(Command("admin"), StateFilter("*"), IsAdmin())
async def cmd_admin(msg: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d)]
        for t, d in [("📋 Заказы","adm_orders"),("👥 Водители","adm_drivers"),
                     ("⛔ ЧС","adm_bl"),("💳 Оплаты","adm_subs"),
                     ("📊 Статистика","adm_stats"),("📖 Команды","adm_help")]
    ])
    await safe_send(msg.chat.id, "🔧 <b>Админ-панель</b>", reply_markup=kb)

@router.message(Command("ban"), StateFilter("*"), IsAdmin())
async def cmd_ban(msg: Message, state: FSMContext):
    try:
        t = int(msg.text.split()[1])
        await DB.bl_add(t)
        _BL_CACHE.add(t)
        await msg.reply(f"⛔ {t} в ЧС")
        await safe_send(t, "⛔ Вы заблокированы.")
    except:
        await msg.reply("⚠️ /ban ID")

@router.message(Command("unban"), StateFilter("*"), IsAdmin())
async def cmd_unban(msg: Message, state: FSMContext):
    try:
        t = int(msg.text.split()[1]); await DB.bl_remove(t)
        _BL_CACHE.discard(t)
        await msg.reply(f"✅ {t} разблокирован")
    except:
        await msg.reply("⚠️ /unban ID")

@router.message(Command("unsub"), StateFilter("*"), IsAdmin())
async def cmd_unsub(msg: Message, state: FSMContext):
    try:
        t = int(msg.text.split()[1]); await DB.sub_expire(t)
        await msg.reply(f"✅ Подписка {t} аннулирована")
        await safe_send(t, "⛔ Абонемент аннулирован.")
    except:
        await msg.reply("⚠️ /unsub ID")

@router.message(Command("extend"), StateFilter("*"), IsAdmin())
async def cmd_extend_one(msg: Message, state: FSMContext):
    try:
        parts = msg.text.split()
        uid   = int(parts[1])
        days  = int(parts[2])
        if days <= 0:
            raise ValueError
    except:
        await msg.reply("⚠️ /extend ID ДНЕЙ, например: /extend 123456789 100")
        return

    drv = await DB.driver(uid)
    if not drv:
        await msg.reply("❌ Водитель не найден")
        return

    today = now_dt().date()
    exp_str, days_left, active = await DB.sub_info(uid)
    base = today
    if exp_str:
        try:
            cur_exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if cur_exp > today:
                base = cur_exp
        except:
            pass
    new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%d")
    await DB.sub_set(uid, new_exp, admin_id=msg.from_user.id, plan_key="manual_extend")

    await msg.reply(f"✅ {drv_name(drv)} ({uid}): подписка продлена до {new_exp}")
    await safe_send(uid, f"🎁 Ваш абонемент продлён на {days} дн. Действует до {new_exp}.")

@router.message(Command("extend_all"), StateFilter("*"), IsAdmin())
async def cmd_extend_all(msg: Message, state: FSMContext):
    try:
        days = int(msg.text.split()[1])
        if days <= 0:
            raise ValueError
    except:
        await msg.reply("⚠️ /extend_all КОЛ-ВО_ДНЕЙ, например: /extend_all 100")
        return

    await msg.reply(f"⏳ Продлеваю подписку на {days} дн. всем водителям...")
    updated = await DB.sub_extend_all(days, admin_id=msg.from_user.id)
    await msg.reply(f"✅ Готово: подписка продлена на {days} дн. для {len(updated)} водителей.")

    notify_text = f"🎁 Ваш бесплатный абонемент продлён ещё на {days} дн.!"
    for uid in updated:
        await safe_send(uid, notify_text)

@router.message(Command("deldriver"), StateFilter("*"), IsAdmin())
async def cmd_deldrv(msg: Message, state: FSMContext):
    try:
        t   = int(msg.text.split()[1])
        drv = await DB.driver(t)
        if not drv: await msg.reply("❌ Не найден"); return
        active_orders, active_trip_bookings = await DB.driver_del(t)
        await msg.reply(f"✅ {drv_name(drv)} удалён")
        await safe_send(t, "🗑 Ваш профиль водителя удалён.")
        for oid, pid in active_orders:
            if pid:
                await safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nЗаказ #{oid} снова открыт.")
            await update_channel_post(oid)
            await _notify_drivers(oid)
        for tid, pid in active_trip_bookings:
            await safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nРейс #{tid} отменён, бронь аннулирована.")
    except:
        await msg.reply("⚠️ /deldriver ID")

@router.message(Command("stats"), StateFilter("*"), IsAdmin())
async def cmd_stats(msg: Message, state: FSMContext):
    s = await DB.stats()
    await safe_send(msg.chat.id,
        f"📊 Заказов: {s['total']} | Открыто: {s['open']} | Завершено: {s['done']}\n"
        f"🚗 Водителей: {s['drivers']} | Подписка: {s['subscribed']} | Проверены: {s['docs_ok']}")

@router.message(Command("recalc"), StateFilter("*"), IsAdmin())
async def cmd_recalc(msg: Message, state: FSMContext):
    try:
        _, oid, dkm, price = msg.text.split()
        oid, dkm, price = int(oid), float(dkm), int(price)
        order = await DB.order(oid)
        if not order: await safe_send(msg.chat.id, "❌ Заказ не найден"); return

        was_pending = order["status"] == "pending"
        update_fields = {"distance_km": dkm, "price": price}
        if was_pending:
            # Заказ ещё не публиковался — это первый расчёт, открываем его
            update_fields["status"] = "open"
        await DB.order_upd(oid, **update_fields)

        order = await DB.order(oid)
        await safe_send(msg.chat.id, f"✅ Заказ #{oid} пересчитан (статус: {order['status']})")
        if order["passenger_id"]:
            if order["status"] == "open":
                await safe_send(order["passenger_id"],
                    f"✅ <b>Заказ #{oid} рассчитан!</b>\n\n{fmt_order(order)}")
            else:
                await safe_send(order["passenger_id"],
                    f"ℹ️ Стоимость заказа #{oid} скорректирована администратором.\n\n{fmt_order(order)}")

        if was_pending:
            await _post_to_channel(oid)
            await _notify_drivers(oid)
        elif order["status"] == "open":
            # Заказ уже был открыт — просто обновляем существующий пост, не дублируем
            await update_channel_post(oid)
            await _notify_drivers(oid)
        else:
            # Заказ уже взят/завершён/отменён — статус не трогаем, только синхронизируем текст поста
            await update_channel_post(oid)
    except Exception as e:
        await safe_send(msg.chat.id, f"❌ {e}\nФормат: /recalc order_id distance_km price")


# ══════════════ МЕНЮ (кнопки) ══════════════
@router.message(F.text == "🔙 Главное меню", StateFilter("*"))
async def to_main(msg: Message, state: FSMContext):
    await state.clear()
    await safe_send(msg.chat.id, "🏠 Главное меню", reply_markup=kb_main())

@router.message(F.text == "📊 Тарифы", StateFilter("*"))
async def show_tariffs(msg: Message, state: FSMContext):
    await state.clear()
    lines = ["💰 <b>Тарифы на поездки</b> (вся машина / место в рейсе)\n"]
    lines.append("🇷🇺 <b>Россия</b>")
    for v in TARIFFS_RF.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b> / <b>{v['price_seat']} ₽</b>")
    lines.append("\n🌍 <b>Кавказ/СНГ</b>")
    for v in TARIFFS_CIS.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b> / <b>{v['price_seat']} ₽</b>")
    lines.append("\n🆕 <b>Новые территории</b>")
    for v in TARIFFS_NT.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b> / <b>{v['price_seat']} ₽</b>")
    lines.append("\n⚠️ Платные дороги — отдельно\n🚫 Торг запрещён")
    lines.append("\n📞 <b>Заказ по телефону:</b>")
    for phone in DISPATCHER_PHONES:
        lines.append(f"  {phone}")
    await safe_send(msg.chat.id, "\n".join(lines))


# ══════════════ СОЗДАНИЕ ЗАКАЗА (FSM) ══════════════
@router.message(F.text == "🚕 Создать заказ", StateFilter("*"))
async def order_start(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    recent = await DB.passenger_orders(uid, limit=1)
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
    uid = msg.from_user.id
    cur = await state.get_state()
    await state.clear()
    kb = await kb_driver(uid) if cur and cur.startswith("TripForm:") else kb_main()
    await safe_send(uid, "❌ Отменено.", reply_markup=kb)

# Шаг 1
@router.message(OrderForm.from_city)
async def step_from(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    city = await ask_city_step(uid, msg.text, "❌ Некорректное название")
    if not city: return
    await state.update_data(from_city=city)
    await state.set_state(OrderForm.to_city)
    await safe_send(uid, f"✅ Откуда: <b>{esc(city)}</b>\n\n🗺 <b>Шаг 2/7</b>\n🏁 Введите город назначения")

# Шаг 2
@router.message(OrderForm.to_city)
async def step_to(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    city = await ask_city_step(uid, msg.text, "❌ Некорректное название")
    if not city: return
    data = await state.get_data()
    from_city = data.get("from_city")
    if not from_city:
        await state.clear()
        await safe_send(uid, "⚠️ Данные устарели. Начните заново.", reply_markup=kb_main()); return
    if from_city.lower() == city.lower():
        await safe_send(uid, "❌ Города не должны совпадать"); return
    await state.update_data(to_city=city)
    await state.set_state(OrderForm.trip_date)
    await safe_send(uid, f"✅ Куда: <b>{esc(city)}</b>\n\n🗺 <b>Шаг 3/7</b>\n📅 Дата (ДД.ММ.ГГГГ, точки можно не ставить)")

# Шаг 3
@router.message(OrderForm.trip_date)
async def step_date(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    d = parse_ru_date(msg.text)
    if d is None:
        await safe_send(uid, "❌ Формат ДД.ММ.ГГГГ (точки не обязательны, например 30062026)"); return
    if d < now_dt().date():
        await safe_send(uid, "❌ Дата не может быть в прошлом"); return
    await state.update_data(trip_date=d.strftime("%d.%m.%Y"))
    await state.set_state(OrderForm.trip_time)
    await safe_send(uid, f"✅ Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n🗺 <b>Шаг 4/7</b>\n🕐 Время (ЧЧ:ММ, например 1430)")

# Шаг 4
@router.message(OrderForm.trip_time)
async def step_time(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    parsed = parse_ru_time(msg.text)
    if parsed is None:
        await safe_send(uid, "❌ Формат ЧЧ:ММ (двоеточие не обязательно, например 1430)"); return
    h, m = parsed
    if not (0 <= h <= 23 and 0 <= m <= 59):
        await safe_send(uid, "❌ Некорректное время"); return
    nice_time = f"{h:02d}:{m:02d}"
    data = await state.get_data()
    if ds := data.get("trip_date"):
        trip_dt = datetime.strptime(f"{ds} {nice_time}", "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
        if trip_dt < now_dt():
            await safe_send(uid, "❌ Поездка не может быть в прошлом"); return
    await state.update_data(trip_time=nice_time)
    await state.set_state(OrderForm.passengers)
    await safe_send(uid, f"✅ Время: <b>{nice_time}</b>\n\n🗺 <b>Шаг 5/7</b>\n👥 Пассажиры:",
                    reply_markup=kb_passengers())

# Шаг 5 (колбэк)
@router.callback_query(StateFilter(OrderForm.passengers), F.data.startswith("pax_"))
async def cb_pax_fsm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    passengers = int(call.data.split("_")[1])
    await call.answer()
    await state.update_data(passengers=passengers)
    data = await state.get_data()
    region = route_region(data.get("from_city",""), data.get("to_city",""))
    await state.update_data(region=region)
    await state.set_state(OrderForm.car_class)
    hint = "\n\n⚠️ <b>Для 5 и более пассажиров доступен только минивэн!</b>" if passengers >= 5 else ""
    region_hint = f"\n🌍 Тариф: {REGION_LABELS.get(region,'')}" if region != "rf" else ""
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Пассажиров: <b>{passengers}</b>")
    await safe_send(uid,
        f"🗺 <b>Шаг 6/7</b>{region_hint}\n👥 Пассажиров: {passengers}{hint}",
        reply_markup=kb_car_class(region, passengers))

# Шаг 6 (колбэк)
@router.callback_query(StateFilter(OrderForm.car_class), F.data.startswith("car_"))
async def cb_car_fsm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    cc = call.data.split("_", 1)[1]
    data = await state.get_data()
    region = data.get("region", "rf")
    t = TARIFFS_NT if region == "nt" else (TARIFFS_CIS if region == "cis" else TARIFFS_RF)
    if cc not in t:
        await call.answer("❌ Неверный класс", show_alert=True); return
    await call.answer()
    await state.update_data(car_class=cc, car_class_label=t[cc]["label"])
    await state.set_state(OrderForm.wishes)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Класс: <b>{t[cc]['label']}</b>")
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Нет"), KeyboardButton(text="❌ Отменить")]],
        resize_keyboard=True,
    )
    await safe_send(uid, "🗺 <b>Шаг 7/7</b>\n💬 Пожелания? Нет — нажмите кнопку", reply_markup=kb)

# Шаг 7
@router.message(OrderForm.wishes)
async def step_wishes(msg: Message, state: FSMContext):
    uid, wish = msg.from_user.id, msg.text.strip()
    if len(wish) > 500:
        await safe_send(uid, "❌ Слишком длинный текст (макс. 500 символов)"); return
    wish = "" if wish.lower() in ["нет","—","-","no"] else wish
    data = await state.get_data()
    # Не очищаем стейт здесь — очистка будет внутри таски после успешного сохранения в БД.
    # Если таска упадёт до order_create, пользователь останется в состоянии OrderForm.wishes
    # и сможет повторить попытку без потери данных.
    await safe_send(uid, "⏳ Рассчитываю...", reply_markup=ReplyKeyboardRemove())
    asyncio.create_task(_finalize_order_task(uid, data, wish, state))

async def _finalize_order_task(uid, data, wish, state: FSMContext):
    try:
        dist = await get_distance_async(data["from_city"], data["to_city"])
        dkm = price = None
        if dist:
            dkm   = round(dist)
            price = calc_price(dkm, data["car_class"], data["from_city"], data["to_city"])
        region = route_region(data["from_city"], data["to_city"])
        oid = await DB.order_create({**data, "passenger_id": uid, "wishes": wish,
                               "distance_km": dkm, "price": price,
                               "status": "open" if dkm else "pending",
                               "region": region})
        # Только после успешного сохранения в БД чистим стейт
        await state.clear()
        order = await DB.order(oid)
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
        await safe_send(uid, "❌ Ошибка при создании заказа. Попробуйте ещё раз — данные сохранены.",
                        reply_markup=kb_main())


# ══════════════ МОИ ЗАКАЗЫ ══════════════
@router.message(F.text == "📋 Мои заказы", StateFilter("*"))
async def my_orders(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    if await DB.driver(uid):
        drv_orders = [o for o in await DB.driver_orders(uid) if o["status"] in ("taken","completed")]
        if drv_orders:
            await safe_send(uid, f"🚗 <b>Поездки как водитель</b> ({len(drv_orders)}):")
            for o in drv_orders:
                kb = None
                if o["status"] == "taken":
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="❌ Отказаться", callback_data=f"driver_cancel_{o['id']}")
                    ]])
                await safe_send(uid, fmt_order(o), reply_markup=kb)
    result = await DB.passenger_orders(uid)
    if not result: await safe_send(uid, "📋 Нет пассажирских заказов."); return
    await safe_send(uid, f"📋 <b>Ваши заказы как пассажир</b> ({len(result)}):")
    for o in result:
        if o["status"] in ("open","taken","pending"):
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{o['id']}")
            ]])
            await safe_send(uid, fmt_order(o), reply_markup=kb)
        elif o["status"] == "completed" and o.get("driver_id") and not await DB.has_rating(o["id"], uid):
            await safe_send(uid, fmt_order(o) + "\n\n⭐ <b>Оцените поездку:</b>",
                            reply_markup=kb_stars(o["id"]))
        else:
            await safe_send(uid, fmt_order(o))


# ══════════════ ВОДИТЕЛЬ ══════════════
@router.message(F.text == "🚗 Я водитель", StateFilter("*"))
async def driver_enter(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    await state.clear()
    drv = await DB.driver(uid)
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
    exp, dl, active = await DB.sub_info(uid)
    avg, cnt = await DB.avg_rating(uid)
    await safe_send(uid,
        f"🚗 <b>{drv_name(drv)}</b>\n"
        f"🚘 {esc(drv.get('car_model'))} {drv.get('car_year','—')} г.\n"
        f"🔢 {esc(drv.get('car_number'))}\n"
        f"🏷 {esc(drv.get('car_class_label'))}\n"
        f"📞 {esc(drv.get('phone'))}\n"
        f"📄 {'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает верификации'}\n"
        f"💳 {'✅ Подписка до '+fmt_date_ru(exp)+' ('+str(dl)+' дн.)' if active else '❌ Нет подписки'}\n"
        f"{'⭐ Рейтинг: '+str(avg)+' ('+str(cnt)+' оценок)' if cnt else '⭐ Нет оценок'}",
        reply_markup=await kb_driver(uid))

@router.message(F.text == "📦 Доступные заказы", StateFilter("*"))
async def avail_orders(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    drv = await DB.driver(uid)
    if not drv: await safe_send(uid, "❌ Сначала зарегистрируйтесь."); return
    _, _, active = await DB.sub_info(uid)
    if not active: await safe_send(uid, "🔒 Нет абонемента."); return
    if not drv.get("docs_verified"): await safe_send(uid, "⏳ Профиль ещё не верифицирован администратором."); return
    all_open = [o for o in await DB.open_orders() if o["passenger_id"] != uid]
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

@router.message(F.text == "📈 Мои поездки", StateFilter("*"))
async def driver_trips(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    result = [o for o in await DB.driver_orders(uid) if o["status"] in ("taken","completed")]
    if not result: await safe_send(uid, "📈 Нет поездок."); return
    await safe_send(uid, f"📈 <b>Поездки</b> ({len(result)}):")
    for o in result:
        kb = None
        if o["status"] == "taken":
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отказаться", callback_data=f"driver_cancel_{o['id']}")
            ]])
        await safe_send(uid, fmt_order(o), reply_markup=kb)

@router.message(F.text.in_({"✅ Подписка активна","❌ Нет подписки"}), StateFilter("*"))
async def sub_btn(msg: Message, state: FSMContext):
    await state.clear()
    await subscription_menu(msg)


# ══════════════ РЕГИСТРАЦИЯ ВОДИТЕЛЯ (FSM) ══════════════
@router.message(F.text == "👤 Зарегистрироваться", StateFilter("*"))
async def register_driver(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    if await DB.driver(uid):
        await safe_send(uid, "ℹ️ Вы уже зарегистрированы.", reply_markup=await kb_driver(uid)); return
    await state.clear()
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

@router.callback_query(StateFilter(DriverRegForm.car_class), F.data == "reg_cancel_class")
async def cb_pclass_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "❌ Регистрация отменена.", reply_markup=kb_main())

@router.callback_query(StateFilter(DriverRegForm.car_class), F.data.startswith("pclass_"))
async def cb_pclass_fsm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    cc = call.data.split("_", 1)[1]
    data = await state.get_data()
    if err := check_brand(data.get("car_model",""), cc):
        await call.answer(err, show_alert=True); return
    await call.answer()
    data["car_class"] = cc
    data["car_class_label"] = TARIFFS_RF[cc]["label"]
    data["user_id"] = uid
    existing = await DB.driver(uid)
    data["docs_verified"] = existing.get("docs_verified", False) if existing else False
    data["registered_at"] = existing.get("registered_at") if existing else now_iso()
    await DB.driver_save(uid, data)
    # Выдаём пробную подписку при первой регистрации (см. TRIAL_DAYS)
    if not existing:
        trial_exp = (now_dt().date() + timedelta(days=TRIAL_DAYS)).strftime("%Y-%m-%d")
        await DB.sub_set(uid, trial_exp)
        log.info(f"Пробная подписка выдана водителю {uid} до {trial_exp}")
    else:
        trial_exp = None
    await state.clear()
    ph = profile_link_html(data)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    trial_txt = (
        f"\n🎁 <b>Вам начислена бесплатная пробная подписка на {TRIAL_DAYS} дней!</b>\n"
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
        reply_markup=await kb_driver(uid))
    if existing and not (await DB.sub_info(uid))[2]:
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
            f"💬 {ph}\nID: <code>{uid}</code>",
            reply_markup=kb)

@router.message(F.text == "👤 Мой профиль", StateFilter("*"))
async def profile_menu(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    drv = await DB.driver(uid)
    if not drv: await safe_send(uid, "❌ Профиль не найден.", reply_markup=kb_main()); return
    avg, cnt  = await DB.avg_rating(uid)
    exp, dl, active = await DB.sub_info(uid)
    await safe_send(uid,
        f"👤 <b>{drv_name(drv)}</b>\n"
        f"🚘 {esc(drv.get('car_model'))} ({drv.get('car_year','—')})\n"
        f"🔢 {esc(drv.get('car_number'))}\n"
        f"🏷 {esc(drv.get('car_class_label'))}\n"
        f"📞 {esc(drv.get('phone'))}\n"
        f"📄 Статус: <b>{'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает'}</b>\n"
        f"💳 {'✅ До '+fmt_date_ru(exp)+' ('+str(dl)+' дн.)' if active else '❌ Нет подписки'}\n"
        f"{'⭐ Рейтинг: '+str(avg)+'/5 ('+str(cnt)+' оценок)' if cnt else '⭐ Нет оценок'}\n\n"
        f"<i>Для изменения данных обратитесь к администратору @Olegan7979</i>")


# ══════════════ АБОНЕМЕНТ ══════════════
@router.message(F.text == "💳 Абонемент", StateFilter("*"))
async def subscription_menu(msg: Message, state: FSMContext = None):
    if state:
        await state.clear()
    uid = msg.from_user.id
    if not await DB.driver(uid): await safe_send(uid, "❌ Заполните профиль."); return
    exp, dl, active = await DB.sub_info(uid)
    txt = (f"✅ <b>Абонемент активен</b>\nДо: <b>{fmt_date_ru(exp)}</b>\nОсталось: <b>{dl} дн.</b>\n\nПродлить?"
           if active else "💳 <b>Абонемент</b>\n\n❌ Нет подписки\nВыберите тариф:")
    await safe_send(uid, txt, reply_markup=kb_subs())


# ══════════════ CALLBACK-ОБРАБОТЧИКИ (общие) ══════════════
@router.callback_query(F.data == "cancel_order")
async def cb_cancel_order(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "❌ Отменено.", reply_markup=kb_main())

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
    order = await DB.order(oid)
    if not order:
        await call.answer("❌ Заказ не найден"); return
    if order["passenger_id"] != uid:
        await call.answer("❌ Не ваш заказ", show_alert=True); return
    if order["status"] != "completed":
        await call.answer("❌ Заказ не завершён"); return
    drv_id = order.get("driver_id")
    if not drv_id:
        await call.answer("❌ Нет водителя"); return
    if await DB.has_rating(oid, uid):
        await call.answer("Вы уже оценили эту поездку")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    await call.answer("✅ Спасибо за оценку!")
    await DB.add_rating(oid, drv_id, uid, stars)
    avg, cnt = await DB.avg_rating(drv_id)
    try:
        await bot.edit_message_text(
            text=f"✅ <b>Поездка #{oid} завершена!</b>\n⭐ Ваша оценка: <b>{stars}</b>\nСпасибо!",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id)
    except:
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await safe_send(uid, f"⭐ Оценка {stars} сохранена!")
    await safe_send(drv_id,
        f"⭐ <b>Новая оценка за поездку #{oid}!</b>\n"
        f"Пассажир поставил: <b>{stars}⭐</b>\nВаш средний рейтинг: <b>{avg}/5</b> ({cnt} оценок)")

@router.callback_query(F.data.startswith("take_"))
async def cb_take(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])
    drv = await DB.driver(uid)
    if not drv or not drv.get("docs_verified"):
        await call.answer("❌ Профиль не верифицирован", show_alert=True); return
    _, _, active = await DB.sub_info(uid)
    if not active:
        await call.answer("❌ Нет абонемента", show_alert=True); return
    od = await DB.order(oid)
    if not od:
        await call.answer("❌ Заказ не найден"); return
    if od["passenger_id"] == uid:
        await call.answer("❌ Нельзя взять свой заказ", show_alert=True); return
    ct, rsn = can_take_order(drv, od)
    if not ct:
        await call.answer(rsn, show_alert=True); return
    # Отвечаем Telegram сразу — до тяжёлых операций
    await call.answer("⏳ Принимаем заказ...")
    if not await DB.order_take_atomic(oid, uid):
        await safe_send(uid, "⚠️ Заказ уже недоступен — его взял другой водитель.")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    await update_channel_post(oid)
    od = await DB.order(oid)
    if not od: return
    pid = od.get("passenger_id")
    if not pid: return
    try:
        pc = await bot.get_chat(pid)
        pn, pu = esc(pc.first_name or "Пассажир"), pc.username
    except:
        pn, pu = "Пассажир", None
    purl = f"https://t.me/{pu}" if pu else f"tg://user?id={pid}"
    avg, cnt = await DB.avg_rating(uid)
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
    durl = f"https://t.me/{du}" if du else f"tg://user?id={uid}"
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

@router.callback_query(F.data.regexp(r"^cancel_\d+$"))
async def cb_cancel(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])
    if await DB.order_cancel_atomic(oid, uid, "passenger"):
        await call.answer("✅ Отменено")
        await _clear_notified(oid)
        await update_channel_post(oid)
        order = await DB.order(oid)
        if order and order.get("driver_id"):
            await safe_send(order["driver_id"], f"❌ Пассажир отменил заказ #{oid}")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await safe_send(uid, f"✅ Заказ #{oid} отменён.", reply_markup=kb_main())
    else:
        await call.answer("❌ Заказ уже нельзя отменить", show_alert=True)

@router.callback_query(F.data.startswith("driver_cancel_"))
async def cb_driver_cancel(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[2])
    if await DB.order_cancel_atomic(oid, uid, "driver"):
        await call.answer("✅")
        await update_channel_post(oid)
        order = await DB.order(oid)
        if order and order.get("passenger_id"):
            await safe_send(order["passenger_id"], f"❌ Водитель отказался от заказа #{oid}")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        await safe_send(uid, f"✅ Вы отказались от заказа #{oid}")
        await _clear_notified(oid)
        await _notify_drivers(oid, exclude_uid=uid)
    else:
        await call.answer("❌ Заказ уже нельзя отменить", show_alert=True)

@router.callback_query(F.data.startswith("done_"))
async def cb_done(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])
    o = await DB.order(oid)
    if not o:
        await call.answer("❌ Заказ не найден"); return
    if o["passenger_id"] != uid:
        await call.answer("❌ Не ваш заказ", show_alert=True); return
    if o["status"] != "taken":
        await call.answer("❌ Заказ не в работе", show_alert=True); return
    await call.answer("✅ Завершено")
    await DB.order_upd(oid, status="completed", completed_at=now_iso())
    await _clear_notified(oid)
    await update_channel_post(oid)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, "✅ <b>Поездка завершена!</b>\n⭐ Пожалуйста, оцените водителя:",
                    reply_markup=kb_stars(oid))
    if o.get("driver_id"):
        await safe_send(o["driver_id"], f"✅ Поездка #{oid} завершена пассажиром!")

@router.callback_query(F.data.startswith("sub_"))
async def cb_sub(call: CallbackQuery):
    uid = call.from_user.id
    if not await DB.driver(uid):
        await call.answer("❌ Заполните профиль", show_alert=True); return
    pk = call.data.split("_")[1]
    plan = SUBS.get(pk)
    if not plan:
        await call.answer("❌"); return
    await call.answer()
    await DB.pending_set(uid, pk)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    paid_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📨 Я оплатил", callback_data=f"paid_{pk}")
    ]])
    await safe_send(uid,
        f"💳 <b>{plan['label']}</b>\nСумма: <b>{plan['price']} ₽</b>\n\n{PAYMENT_DETAILS}",
        reply_markup=paid_kb)
    drv = await DB.driver(uid)
    for aid in ADMIN_IDS:
        await safe_send(aid,
            f"💳 <b>Запрос на абонемент</b>\n👤 {drv_name(drv)}\n"
            f"🚘 {esc(drv.get('car_model'))}\nТариф: {plan['label']}\nID: <code>{uid}</code>")

@router.callback_query(F.data.startswith("paid_"))
async def cb_paid(call: CallbackQuery):
    uid = call.from_user.id
    pk = call.data.split("_")[1]
    plan = SUBS.get(pk, {})
    await call.answer("✅")
    await safe_send(uid, "⏳ Заявка отправлена. Активация в течение 1–2 ч.")
    drv = await DB.driver(uid)
    for aid in ADMIN_IDS:
        await safe_send(aid,
            f"🔔 <b>Водитель сообщил об оплате!</b>\n"
            f"👤 {drv_name(drv or {})} (ID: {uid})\nТариф: {plan.get('label','—')}")

@router.callback_query(F.data == "back_driver")
async def cb_back_driver(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, "🚗 Меню водителя", reply_markup=await kb_driver(uid))


# ══════════════ АДМИН-КОЛБЭКИ ══════════════
@router.callback_query(F.data.startswith("conf_sub_"))
async def cb_conf_sub(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("❌"); return
    _, _, tgt, pk = call.data.split("_", 3)
    tgt = int(tgt)
    plan = SUBS.get(pk)
    if not plan:
        await call.answer("❌"); return
    await call.answer("✅")
    exp_str, _, _ = await DB.sub_info(tgt)
    base = (max(datetime.strptime(exp_str,"%Y-%m-%d").date(), now_dt().date())
            if exp_str else now_dt().date())
    new_exp = (base + timedelta(days=plan["days"])).strftime("%Y-%m-%d")
    await DB.sub_set(tgt, new_exp, uid, pk)
    await DB.pending_del(tgt)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Подписка активирована до {new_exp}")
    await safe_send(tgt,
        f"🎉 <b>Абонемент активирован!</b>\n{plan['label']}\n"
        f"До: {datetime.strptime(new_exp,'%Y-%m-%d').strftime('%d.%m.%Y')}",
        reply_markup=await kb_driver(tgt))

@router.callback_query(F.data.startswith("rej_sub_"))
async def cb_rej_sub(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("❌"); return
    tgt = int(call.data.split("_")[2])
    await call.answer("❌")
    await DB.pending_del(tgt, uid, await DB.pending_get(tgt))
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(tgt, "❌ Запрос на абонемент отклонён.")

@router.callback_query(F.data.regexp(r"^doc_(ok|rej)_\d+$"))
async def cb_doc_verify(call: CallbackQuery):
    uid = call.from_user.id
    if uid not in ADMIN_IDS:
        await call.answer("❌")
        return
    ok  = call.data.startswith("doc_ok_")
    tgt = int(call.data.split("_")[2])
    await call.answer("✅" if ok else "❌")
    try:
        await DB.driver_verify(tgt, ok)
    except Exception as e:
        log.error(f"cb_doc_verify: {e}")
        await safe_send(uid, f"❌ Ошибка при верификации: {e}")
        return
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    if ok:
        await safe_send(uid, f"✅ Профиль водителя {tgt} верифицирован")
        await safe_send(tgt, "✅ <b>Ваш профиль верифицирован!</b>\nТеперь вы можете принимать заказы.",
                        reply_markup=await kb_driver(tgt))
    else:
        await safe_send(uid, f"❌ Профиль {tgt} отклонён")
        await safe_send(tgt, "❌ <b>Верификация отклонена.</b>\nОбратитесь к администратору @Olegan7979.")

@router.callback_query(F.data == "adm_orders", IsAdmin())
async def cb_adm_orders(call: CallbackQuery):
    await call.answer()
    asyncio.create_task(_send_orders_page(call.from_user.id, 0, "all"))

@router.callback_query(F.data.startswith(("adm_ord_page_", "adm_ord_filter_")), IsAdmin())
async def cb_adm_ord_page(call: CallbackQuery):
    try:
        parts = call.data.split("_")
        page = int(parts[-1])
        filter_key = parts[-2] if parts[-2] in ORDER_FILTERS else "all"
    except: return
    await call.answer()
    asyncio.create_task(_send_orders_page(call.from_user.id, page, filter_key))

@router.callback_query(F.data == "adm_ord_noop")
async def cb_adm_ord_noop(call: CallbackQuery): await call.answer()

@router.callback_query(F.data.startswith("adm_cancel_order_"), IsAdmin())
async def cb_adm_cancel_order(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[-1])
    order = await DB.order(oid)
    if not order:
        await call.answer("❌ Не найден"); return
    await call.answer("✅")
    await DB.order_upd(oid, status="cancelled")
    await _clear_notified(oid)
    await update_channel_post(oid)
    if order.get("passenger_id"):
        await safe_send(order["passenger_id"], f"❌ <b>Заказ #{oid} отменён администратором.</b>")
    if order.get("driver_id"):
        await safe_send(order["driver_id"], f"❌ Заказ #{oid} отменён администратором.")
    await safe_send(uid, f"✅ Заказ #{oid} отменён")

@router.callback_query(F.data.startswith("adm_complete_order_"), IsAdmin())
async def cb_adm_complete_order(call: CallbackQuery):
    uid = call.from_user.id
    oid = int(call.data.split("_")[-1])
    order = await DB.order(oid)
    if not order:
        await call.answer("❌"); return
    await call.answer("✅")
    await DB.order_upd(oid, status="completed", completed_at=now_iso())
    await _clear_notified(oid)
    await update_channel_post(oid)
    if order.get("passenger_id"):
        await safe_send(order["passenger_id"], f"✅ Заказ #{oid} завершён администратором.")
    if order.get("driver_id"):
        await safe_send(order["driver_id"], f"✅ Заказ #{oid} завершён администратором.")
    await safe_send(uid, f"✅ Заказ #{oid} завершён")

@router.callback_query(F.data == "adm_drivers", IsAdmin())
async def cb_adm_drivers(call: CallbackQuery):
    await call.answer()
    asyncio.create_task(_send_drivers_page(call.from_user.id, 0))

@router.callback_query(F.data.startswith("adm_drv_page_"), IsAdmin())
async def cb_adm_drv_page(call: CallbackQuery):
    try: page = int(call.data.split("_")[-1])
    except: return
    await call.answer()
    asyncio.create_task(_send_drivers_page(call.from_user.id, page))

@router.callback_query(F.data == "adm_drv_noop")
async def cb_adm_drv_noop(call: CallbackQuery): await call.answer()

@router.callback_query(F.data.startswith("adm_edit_"), IsAdmin())
async def cb_adm_edit(call: CallbackQuery):
    uid = call.from_user.id
    tgt = int(call.data.split("_")[2])
    drv = await DB.driver(tgt)
    if not drv:
        await call.answer("❌ Не найден"); return
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚘 Марка/модель", callback_data=f"adm_ef_{tgt}_car_model")],
        [InlineKeyboardButton(text="📅 Год выпуска",  callback_data=f"adm_ef_{tgt}_car_year")],
        [InlineKeyboardButton(text="🔢 Гос. номер",   callback_data=f"adm_ef_{tgt}_car_number")],
        [InlineKeyboardButton(text="🏷 Класс авто",   callback_data=f"adm_ef_{tgt}_car_class")],
    ])
    await safe_send(uid, f"✏️ <b>Редактировать: {drv_name(drv)}</b>\nЧто изменить?", reply_markup=kb)

@router.callback_query(F.data.startswith("adm_ef_"), IsAdmin())
async def cb_adm_ef(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    parts = call.data.split("_"); tgt = int(parts[2]); field = "_".join(parts[3:])
    drv = await DB.driver(tgt)
    if not drv:
        await call.answer("❌ Не найден"); return
    await call.answer()
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

@router.message(AdminEditForm.waiting_input, IsAdmin())
async def process_admin_edit(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    text = (msg.text or "").strip()
    data = await state.get_data()
    tgt = data.get("tgt"); field = data.get("field")
    drv = await DB.driver(tgt)
    if not drv:
        await state.clear()
        await safe_send(uid, "❌ Водитель не найден"); return

    if field == "car_model":
        if not (2 <= len(text) <= 100):
            await safe_send(uid, "❌ Введите марку и модель (2–100 символов)"); return
        await DB.driver_update_fields(tgt, car_model=text)
        await notify_driver_change(tgt, "марку/модель авто", text)
    elif field == "car_year":
        try:
            year = int(text)
            if err := _validate_car_year(year):
                await safe_send(uid, err); return
            await DB.driver_update_fields(tgt, car_year=year)
            await notify_driver_change(tgt, "год выпуска авто", year)
        except ValueError:
            await safe_send(uid, f"❌ Некорректный год (минимум {MIN_CAR_YEAR})"); return
    elif field == "car_number":
        number = text.upper().replace(" ","")
        if not number: await safe_send(uid, "❌ Введите номер"); return
        await DB.driver_update_fields(tgt, car_number=number)
        await notify_driver_change(tgt, "гос. номер", number)
    await state.clear()
    field_label = ADMIN_EDIT_FIELD_LABELS.get(field, field)
    await safe_send(uid, f"✅ Изменено: <b>{field_label}</b>\nНовое значение: {esc(text)}")

@router.callback_query(F.data.startswith("adm_sc_"), IsAdmin())
async def cb_adm_sc(call: CallbackQuery):
    uid = call.from_user.id
    parts = call.data.split("_"); tgt = int(parts[2]); cc = "_".join(parts[3:])
    drv = await DB.driver(tgt)
    if not drv:
        await call.answer("❌ Не найден"); return
    if err := check_brand(drv.get("car_model",""), cc):
        await call.answer(err, show_alert=True); return
    await call.answer("✅")
    lbl = TARIFFS_RF.get(cc,{}).get("label", cc)
    await DB.driver_update_fields(tgt, car_class=cc, car_class_label=lbl)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Класс обновлён: {lbl}")
    await notify_driver_change(tgt, "класс авто", lbl)

@router.callback_query(F.data.startswith("adm_del_"), IsAdmin())
async def cb_adm_del(call: CallbackQuery):
    uid = call.from_user.id
    tgt = int(call.data.split("_")[2])
    drv = await DB.driver(tgt)
    if not drv:
        await call.answer("❌ Не найден"); return
    await call.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_delok_{tgt}"),
        InlineKeyboardButton(text="❌ Нет",         callback_data="adm_delno"),
    ]])
    await safe_send(uid, f"⚠️ Удалить <b>{drv_name(drv)}</b> (ID: {tgt})?", reply_markup=kb)

@router.callback_query(F.data.startswith("adm_delok_"), IsAdmin())
async def cb_adm_delok(call: CallbackQuery):
    uid = call.from_user.id
    tgt = int(call.data.split("_")[2])
    await call.answer("✅ Удалён")
    drv = await DB.driver(tgt)
    if drv:
        active_orders, active_trip_bookings = await DB.driver_del(tgt)
        await safe_send(uid, f"✅ {drv_name(drv)} удалён")
        await safe_send(tgt, "🗑 Ваш профиль водителя удалён администратором.")
        for oid, pid in active_orders:
            if pid:
                await safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nЗаказ #{oid} снова открыт.")
            await update_channel_post(oid)
            await _notify_drivers(oid)
        for tid, pid in active_trip_bookings:
            await safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nРейс #{tid} отменён, бронь аннулирована.")
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

@router.callback_query(F.data == "adm_delno", IsAdmin())
async def cb_adm_delno(call: CallbackQuery):
    await call.answer()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "❌ Удаление отменено")

@router.callback_query(F.data == "adm_bl", IsAdmin())
async def cb_adm_bl(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    bl = await DB.bl_all()
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

@router.callback_query(F.data == "adm_subs", IsAdmin())
async def cb_adm_subs(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    pending = await DB.pending_all()
    if not pending:
        await safe_send(uid, "✅ Нет ожидающих подтверждения")
    for p in pending:
        plan, drv = SUBS.get(p["plan_key"],{}), await DB.driver(p["user_id"]) or {}
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Активировать",
                                 callback_data=f"conf_sub_{p['user_id']}_{p['plan_key']}"),
            InlineKeyboardButton(text="❌ Отклонить",
                                 callback_data=f"rej_sub_{p['user_id']}"),
        ]])
        await safe_send(uid,
            f"💳 <b>{drv_name(drv)}</b>\n💬 {profile_link_html(drv)}\n"
            f"ID: <code>{p['user_id']}</code>\nТариф: {plan.get('label','—')}",
            reply_markup=kb)
        await asyncio.sleep(0.05)

@router.callback_query(F.data == "adm_stats", IsAdmin())
async def cb_adm_stats(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    s = await DB.stats()
    await safe_send(uid,
        f"📊 <b>Статистика</b>\n\nЗаказов всего: {s['total']}\nОткрыто: {s['open']}\n"
        f"Завершено: {s['done']}\n\nВодителей: {s['drivers']}\n"
        f"Верифицировано: {s['docs_ok']}\nС подпиской: {s['subscribed']}")

@router.callback_query(F.data == "adm_help", IsAdmin())
async def cb_adm_help(call: CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    await safe_send(uid,
        "📖 <b>Команды администратора:</b>\n\n"
        "/ban ID — заблокировать\n/unban ID — разблокировать\n"
        "/unsub ID — аннулировать подписку\n/deldriver ID — удалить водителя\n"
        "/recalc order_id distance_km price — пересчитать заказ")


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
    drivers = await DB.all_drivers()
    total   = len(drivers)
    if not drivers:
        await safe_send(uid, "👥 Нет водителей"); return
    start       = page * DRIVER_PAGE_SIZE
    chunk       = drivers[start : start + DRIVER_PAGE_SIZE]
    if not chunk:
        await safe_send(uid, "⚠️ Страница не найдена"); return
    total_pages = max(1, (total - 1) // DRIVER_PAGE_SIZE + 1)
    await safe_send(uid, f"👥 <b>Водители</b> — страница {page + 1} из {total_pages} (всего: {total})")
    sub_results    = await asyncio.gather(*(DB.sub_info(d["user_id"]) for d in chunk))
    rating_results = await asyncio.gather(*(DB.avg_rating(d["user_id"]) for d in chunk))
    for d, (exp_str, dl, active), (avg, cnt) in zip(chunk, sub_results, rating_results):
        sub_txt  = f"✅ до {fmt_date_ru(exp_str)} ({dl} дн.)" if active else "❌ нет подписки"
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
    orders, total = await DB.all_orders(limit=ORDER_PAGE_SIZE, offset=page * ORDER_PAGE_SIZE, status=status)
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


# ══════════════════════════════════════════════════════════════
#  РЕЙСЫ — ВОДИТЕЛЬ СОЗДАЁТ РЕЙС
# ══════════════════════════════════════════════════════════════

@router.message(F.text == "🚐 Создать рейс", StateFilter("*"))
async def trip_start(msg: Message, state: FSMContext):
    # ✅ ГАРАНТИРОВАННЫЙ СБРОС ПЕРЕД СТАРТОМ
    await state.clear()
    
    uid = msg.from_user.id
    drv = await DB.driver(uid)
    if not drv or not drv.get("docs_verified"):
        await safe_send(uid, "❌ Профиль не верифицирован.")
        return
    _, _, active = await DB.sub_info(uid)
    if not active:
        await safe_send(uid, "❌ Нет активного абонемента.", reply_markup=kb_subs())
        return
    await state.set_state(TripForm.from_city)
    await safe_send(uid, "🚐 <b>Создание рейса</b>\n\n🏙 <b>Шаг 1/6</b> — Откуда?", reply_markup=kb_cancel())

@router.message(StateFilter(TripForm.from_city))
async def trip_from(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    city = await ask_city_step(uid, msg.text, "❌ Введите корректное название города.")
    if not city: return
    await state.update_data(from_city=city)
    await state.set_state(TripForm.to_city)
    await safe_send(uid, f"✅ Откуда: <b>{esc(city)}</b>\n\n🏙 <b>Шаг 2/6</b> — Куда?")

@router.message(StateFilter(TripForm.to_city))
async def trip_to(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    data = await state.get_data()
    city = await ask_city_step(uid, msg.text, "❌ Введите корректное название города.",
                                duplicate_of=data.get("from_city",""), err_duplicate="❌ Откуда и куда совпадают.")
    if not city: return
    await state.update_data(to_city=city)
    await state.set_state(TripForm.trip_date)
    await safe_send(uid,
        f"✅ Куда: <b>{esc(city)}</b>\n\n📅 <b>Шаг 3/6</b> — Дата?\n<i>Формат: ДД.ММ.ГГГГ (точки не обязательны)</i>")

@router.message(StateFilter(TripForm.trip_date))
async def trip_date_step(msg: Message, state: FSMContext):
    uid, text = msg.from_user.id, msg.text.strip()
    d = parse_ru_date(text)
    if d is None:
        await safe_send(uid, "❌ Неверный формат. Введите ДД.ММ.ГГГГ (точки не обязательны)."); return
    if d < now_dt().date():
        await safe_send(uid, "❌ Дата уже прошла."); return
    await state.update_data(trip_date=d.strftime("%Y-%m-%d"))
    await state.set_state(TripForm.trip_time)
    await safe_send(uid, f"✅ Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n🕐 <b>Шаг 4/6</b> — Время?\n<i>Формат: ЧЧ:ММ (например 1430)</i>")

@router.message(StateFilter(TripForm.trip_time))
async def trip_time_step(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    parsed = parse_ru_time(msg.text)
    if parsed is None:
        await safe_send(uid, "❌ Неверный формат. Введите ЧЧ:ММ (например 1430)."); return
    h, m = parsed
    if not (0 <= h <= 23 and 0 <= m <= 59):
        await safe_send(uid, "❌ Неверное время."); return
    nice_time = f"{h:02d}:{m:02d}"
    await state.update_data(trip_time=nice_time)
    await state.set_state(TripForm.seats_total)
    await safe_send(uid,
        f"✅ Время: <b>{nice_time}</b>\n\n💺 <b>Шаг 5/6</b> — Сколько мест?",
        reply_markup=kb_trip_seats())

@router.callback_query(StateFilter(TripForm.seats_total), F.data.startswith("trip_seats_"))
async def trip_seats_step(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    seats = int(call.data.split("_")[2])
    await call.answer()
    await state.update_data(seats_total=seats)
    data = await state.get_data()
    region = route_region(data.get("from_city",""), data.get("to_city",""))
    await state.update_data(region=region)
    await state.set_state(TripForm.car_class)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid,
        f"✅ Мест: <b>{seats}</b>\n\n🚘 <b>Шаг 6/6</b> — Класс автомобиля:",
        reply_markup=kb_trip_car_class(region))

@router.callback_query(StateFilter(TripForm.car_class), F.data.startswith("trip_class_"))
async def trip_class_step(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    cc = call.data.split("trip_class_")[1]
    drv = await DB.driver(uid)
    if err := check_brand(drv.get("car_model","") if drv else "", cc):
        await call.answer(err, show_alert=True); return
    await call.answer()
    data = await state.get_data()
    region = data.get("region","rf")
    t = TARIFFS_CIS if region=="cis" else (TARIFFS_NT if region=="nt" else TARIFFS_RF)
    if cc not in t:
        await safe_send(uid, "❌ Неверный класс."); return
    label = t[cc]["label"]
    await state.update_data(car_class=cc, car_class_label=label)
    await state.set_state(TripForm.price_per_seat)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, "⏳ Рассчитываю расстояние...")
    from_city = data.get("from_city","")
    to_city   = data.get("to_city","")
    dist = await get_distance_async(from_city, to_city)
    if dist:
        suggested = calc_price(dist, cc, from_city, to_city, trip_mode=True)
        await state.update_data(distance_km=dist)
        price_hint = (f"💡 Предлагаемая цена: <b>{fmt_price(suggested)}/место</b>\n"
                      f"   ({dist} км × {t[cc]['price_seat']} ₽/км/место)")
    else:
        suggested = None
        await state.update_data(distance_km=None)
        price_hint = "⚠️ Расстояние не рассчитано. Введите цену вручную."
    kb_price_rows = []
    if suggested:
        kb_price_rows.append([InlineKeyboardButton(
            text=f"✅ {fmt_price(suggested)} (рекомендуется)",
            callback_data=f"trip_price_auto_{suggested}")])
    kb_price_rows.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="trip_price_manual")])
    kb_price_rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="trip_cancel")])
    await safe_send(uid,
        f"✅ Класс: <b>{label}</b>\n\n💰 <b>Цена за место:</b>\n{price_hint}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_price_rows))

@router.callback_query(StateFilter(TripForm.price_per_seat), F.data.startswith("trip_price_auto_"))
async def trip_price_auto(call: CallbackQuery, state: FSMContext):
    await call.answer()
    price = int(call.data.split("_")[-1])
    await state.update_data(price_per_seat=price)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await _trip_show_confirm(call.from_user.id, state)

@router.callback_query(StateFilter(TripForm.price_per_seat), F.data == "trip_price_manual")
async def trip_price_manual(call: CallbackQuery):
    await call.answer()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "✏️ Введите цену за одно место (₽):", reply_markup=kb_cancel())

@router.message(StateFilter(TripForm.price_per_seat))
async def trip_price_input(msg: Message, state: FSMContext):
    uid, text = msg.from_user.id, msg.text.strip()
    if not text.isdigit() or not (100 <= int(text) <= 50000):
        await safe_send(uid, "❌ Введите корректную цену (от 100 до 50 000 ₽)."); return
    await state.update_data(price_per_seat=int(text))
    await _trip_show_confirm(uid, state)

async def _trip_show_confirm(uid: int, state: FSMContext):
    data = await state.get_data()
    await state.set_state(TripForm.confirm)
    region_label = REGION_LABELS.get(data.get("region","rf"),"🇷🇺 Россия")
    price = data.get("price_per_seat",0)
    seats = data.get("seats_total",1)
    dist  = data.get("distance_km")
    dist_str = f"{int(dist)} км" if dist else "не рассчитано"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать рейс", callback_data="trip_confirm")],
        [InlineKeyboardButton(text="❌ Отменить",     callback_data="trip_cancel")],
    ])
    await safe_send(uid,
        f"📋 <b>Проверьте рейс:</b>\n\n"
        f"📍 {esc(data.get('from_city'))} → {esc(data.get('to_city'))}\n"
        f"📅 {fmt_date_ru(data.get('trip_date'))} · 🕐 {data.get('trip_time')}\n"
        f"📏 {dist_str}\n"
        f"🚘 {esc(data.get('car_class_label'))}\n"
        f"💺 Мест: {seats}\n"
        f"💰 {fmt_price(price)}/место\n"
        f"🌍 {region_label}\n\n"
        f"💡 Макс. заработок: <b>{fmt_price(price * seats)}</b>",
        reply_markup=kb)

@router.callback_query(StateFilter(TripForm.confirm), F.data == "trip_confirm")
async def trip_confirm(call: CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    await call.answer("✅ Публикуем...")
    data = await state.get_data()
    try:
        tid = await DB.trip_create({
            "driver_id":     uid,
            "from_city":     data["from_city"],
            "to_city":       data["to_city"],
            "trip_date":     data["trip_date"],
            "trip_time":     data["trip_time"],
            "car_class":     data["car_class"],
            "car_class_label": data["car_class_label"],
            "seats_total":   data["seats_total"],
            "price_per_seat": data["price_per_seat"],
            "distance_km":   data.get("distance_km"),
            "region":        data.get("region","rf"),
        })
        await state.clear()
    except Exception as e:
        log.error(f"trip_confirm uid={uid}: {e}")
        await safe_send(uid, "❌ Ошибка при создании рейса. Попробуйте ещё раз.")
        return
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid,
        f"🎉 <b>Рейс #{tid} опубликован!</b>\n"
        f"Пассажиры найдут его при поиске маршрута {esc(data['from_city'])} → {esc(data['to_city'])}.",
        reply_markup=await kb_driver(uid))

@router.callback_query(F.data == "trip_cancel")
async def trip_cancel_fsm(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(call.from_user.id, "❌ Создание рейса отменено.", reply_markup=await kb_driver(call.from_user.id))

@router.message(F.text == "🗓 Мои рейсы", StateFilter("*"))
async def my_trips(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    if not await DB.driver(uid):
        await safe_send(uid, "❌ Вы не зарегистрированы как водитель."); return
    trips = await DB.driver_trips(uid)
    if not trips:
        await safe_send(uid, "📭 У вас ещё нет рейсов."); return
    await safe_send(uid, f"🗓 <b>Ваши рейсы ({len(trips)}):</b>")
    for t in trips:
        bookings = await DB.trip_bookings(t["id"])
        maps_url = yandex_maps_url(t["from_city"], t["to_city"])
        kb_rows = []
        if t["status"] in ("open","full"):
            kb_rows.append([InlineKeyboardButton(
                text=f"👥 Пассажиры ({len(bookings)})",
                callback_data=f"trip_passengers_{t['id']}")])
            kb_rows.append([InlineKeyboardButton(text="🗺 Маршрут", url=maps_url)])
            kb_rows.append([InlineKeyboardButton(
                text="✅ Завершить рейс", callback_data=f"trip_complete_{t['id']}")])
            kb_rows.append([InlineKeyboardButton(
                text="❌ Отменить рейс", callback_data=f"trip_drv_cancel_{t['id']}")])
        await safe_send(uid, fmt_trip(t),
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None)
        await asyncio.sleep(0.05)

@router.callback_query(F.data.startswith("trip_drv_cancel_"))
async def cb_trip_drv_cancel(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    trip = await DB.trip(tid)
    if not trip or trip["driver_id"] != uid:
        await call.answer("❌ Рейс не найден"); return
    await call.answer("❌ Отменяем...")
    passengers = await DB.trip_cancel(tid, uid)
    if passengers is None:
        await safe_send(uid, "❌ Рейс уже не активен."); return
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Рейс #{tid} отменён.")
    for pid in passengers:
        await safe_send(pid,
            f"❌ <b>Рейс #{tid} отменён водителем.</b>\n"
            f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])} · {fmt_date_ru(trip['trip_date'])}")

@router.callback_query(F.data.startswith("trip_complete_"))
async def cb_trip_complete(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    trip = await DB.trip(tid)
    if not trip or trip["driver_id"] != uid:
        await call.answer("❌"); return
    if trip["status"] not in ("open","full"):
        await call.answer("❌ Рейс уже завершён"); return
    await call.answer("✅ Завершаем...")
    passengers = await DB.trip_complete(tid, uid)
    if passengers is None:
        await safe_send(uid, "❌ Ошибка."); return
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(uid, f"✅ Рейс #{tid} завершён!")
    for pid in passengers:
        kb_rate = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{i}⭐", callback_data=f"trip_rate_{tid}_{uid}_{i}")
            for i in range(1, 6)
        ]])
        await safe_send(pid,
            f"✅ <b>Рейс #{tid} завершён водителем.</b>\n"
            f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])} · {fmt_date_ru(trip['trip_date'])}\n\n"
            f"⭐ Пожалуйста, оцените водителя:",
            reply_markup=kb_rate)

@router.callback_query(F.data.startswith("trip_passengers_"))
async def cb_trip_passengers(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    trip = await DB.trip(tid)
    if not trip or trip["driver_id"] != uid:
        await call.answer("❌"); return
    await call.answer()
    bookings = await DB.trip_bookings(tid)
    if not bookings:
        await safe_send(uid, "👥 Пассажиров пока нет."); return
    await safe_send(uid, f"👥 <b>Пассажиры рейса #{tid}:</b>")
    for b in bookings:
        pid = b["passenger_id"]
        try:
            pc = await bot.get_chat(pid)
            pname = esc(pc.first_name or "Пассажир")
            pun = pc.username
        except:
            pname, pun = "Пассажир", None
        purl = f"https://t.me/{pun}" if pun else f"tg://user?id={pid}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать", url=purl)],
            [InlineKeyboardButton(text="❌ Отклонить",
                                  callback_data=f"trip_reject_{tid}_{pid}")],
        ])
        await safe_send(uid,
            f"👤 <a href='{purl}'>{pname}</a>\n💺 Мест: {b['seats']}",
            reply_markup=kb)
        await asyncio.sleep(0.05)

@router.callback_query(F.data.startswith("trip_reject_"))
async def cb_trip_reject(call: CallbackQuery):
    uid = call.from_user.id
    _, _, tid_str, pid_str = call.data.split("_", 3)
    tid, pid = int(tid_str), int(pid_str)
    trip = await DB.trip(tid)
    if not trip or trip["driver_id"] != uid:
        await call.answer("❌"); return
    await call.answer("✅")
    ok = await DB.trip_reject_passenger(tid, pid)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    if ok:
        await safe_send(uid, "✅ Пассажир отклонён. Место возвращено.")
        await safe_send(pid,
            f"❌ <b>Водитель отклонил вашу бронь</b>\n"
            f"Рейс #{tid}: {esc(trip['from_city'])} → {esc(trip['to_city'])}\n"
            "Попробуйте другой рейс: 🚐 Поехать вместе")

@router.callback_query(F.data.regexp(r"^trip_rate_\d+_\d+_\d+$"))
async def cb_trip_rate(call: CallbackQuery):
    uid = call.from_user.id
    _, _, tid_str, drv_str, stars_str = call.data.split("_")
    tid, drv_id, stars = int(tid_str), int(drv_str), int(stars_str)
    if await DB.has_rating(-tid, uid):
        await call.answer("Вы уже оценили эту поездку")
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        return
    await call.answer(f"{stars}⭐ Спасибо!")
    await DB.add_rating(-tid, drv_id, uid, stars)
    avg, cnt = await DB.avg_rating(drv_id)
    try:
        await bot.edit_message_text(
            text=f"✅ <b>Рейс #{tid} завершён!</b>\n⭐ Ваша оценка: <b>{stars}</b>\nСпасибо!",
            chat_id=call.message.chat.id, message_id=call.message.message_id)
    except:
        await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    await safe_send(drv_id,
        f"⭐ <b>Новая оценка за рейс #{tid}!</b>\n"
        f"Пассажир поставил: <b>{stars}⭐</b>\n"
        f"Ваш средний рейтинг: <b>{avg}/5</b> ({cnt} оценок)")

# ══════════════════════════════════════════════════════════════
#  РЕЙСЫ — ПАССАЖИР ИЩЕТ МЕСТО
# ══════════════════════════════════════════════════════════════

@router.message(F.text == "🚐 Поехать вместе", StateFilter("*"))
async def search_trip_start(msg: Message, state: FSMContext):
    # ✅ ГАРАНТИРОВАННЫЙ СБРОС ПЕРЕД СТАРТОМ
    await state.clear()
    
    uid = msg.from_user.id
    if await DB.bl_check(uid):
        await safe_send(uid, "⛔ Вы заблокированы.")
        return
    await state.set_state(SearchTripForm.from_city)
    await safe_send(uid,
        "🚐 <b>Поиск рейса</b>\n\n🏙 <b>Шаг 1/3</b> — Откуда едете?",
        reply_markup=kb_cancel())

@router.message(StateFilter(SearchTripForm.from_city))
async def search_from(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    city = await ask_city_step(uid, msg.text, "❌ Введите корректное название города.")
    if not city: return
    await state.update_data(from_city=city)
    await state.set_state(SearchTripForm.to_city)
    await safe_send(uid, f"✅ Откуда: <b>{esc(city)}</b>\n\n🏙 <b>Шаг 2/3</b> — Куда?")

@router.message(StateFilter(SearchTripForm.to_city))
async def search_to(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    city = await ask_city_step(uid, msg.text, "❌ Введите корректное название города.")
    if not city: return
    await state.update_data(to_city=city)
    await state.set_state(SearchTripForm.trip_date)
    await safe_send(uid,
        f"✅ Куда: <b>{esc(city)}</b>\n\n📅 <b>Шаг 3/3</b> — Дата?\n<i>Формат: ДД.ММ.ГГГГ (точки не обязательны)</i>")

@router.message(StateFilter(SearchTripForm.trip_date))
async def search_date(msg: Message, state: FSMContext):
    uid, text = msg.from_user.id, msg.text.strip()
    d = parse_ru_date(text)
    if d is None:
        await safe_send(uid, "❌ Неверный формат. Введите ДД.ММ.ГГГГ (точки не обязательны)."); return
    iso = d.strftime("%Y-%m-%d")
    nice_date = d.strftime("%d.%m.%Y")
    data = await state.get_data()
    await state.clear()
    from_city = data["from_city"]
    to_city   = data["to_city"]
    await safe_send(uid, "🔍 Ищу рейсы...", reply_markup=ReplyKeyboardRemove())
    trips = await DB.trips_search(from_city, to_city, iso)
    if not trips:
        await safe_send(uid,
            f"😔 Рейсов <b>{esc(from_city)} → {esc(to_city)}</b> на <b>{nice_date}</b> не найдено.\n\n"
            "Попробуйте другую дату или создайте обычный заказ.",
            reply_markup=kb_main())
        return
    await safe_send(uid,
        f"✅ Найдено рейсов: <b>{len(trips)}</b>\n📍 {esc(from_city)} → {esc(to_city)} · {nice_date}")
    for t in trips:
        drv = await DB.driver(t["driver_id"])
        avg, cnt = await DB.avg_rating(t["driver_id"])
        maps_url = yandex_maps_url(t["from_city"], t["to_city"])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📌 Забронировать место",
                                  callback_data=f"book_trip_{t['id']}")],
            [InlineKeyboardButton(text="🗺 Маршрут", url=maps_url)],
        ])
        await safe_send(uid, fmt_trip(t, drv=drv, avg=avg, cnt=cnt), reply_markup=kb)
        await asyncio.sleep(0.05)
    await safe_send(uid, "⬆️ Выберите рейс и нажмите «Забронировать»", reply_markup=kb_main())

@router.callback_query(F.data.startswith("book_trip_"))
async def cb_book_trip(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    trip = await DB.trip(tid)
    if not trip:
        await call.answer("❌ Рейс не найден"); return
    if trip["status"] not in ("open","full"):
        await call.answer("❌ Рейс недоступен", show_alert=True); return
    if trip["seats_free"] <= 0:
        await call.answer("❌ Мест нет", show_alert=True); return
    if trip["driver_id"] == uid:
        await call.answer("❌ Нельзя бронировать свой рейс", show_alert=True); return
    await call.answer("✅ Бронируем...")
    ok = await DB.trip_book_atomic(tid, uid)
    if not ok:
        await safe_send(uid, "❌ Не удалось забронировать — место уже занято."); return
    trip = await DB.trip(tid)
    drv  = await DB.driver(trip["driver_id"])
    maps_url = yandex_maps_url(trip["from_city"], trip["to_city"])
    du = (drv.get("username","") or "").lstrip("@") if drv else ""
    durl = f"https://t.me/{du}" if du else f"tg://user?id={trip['driver_id']}"
    is_full = trip["status"] == "full"
    kb_pass = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Написать водителю", url=durl)],
        [InlineKeyboardButton(text="🗺 Маршрут", url=maps_url)],
        [InlineKeyboardButton(text="ℹ️ Инфо о рейсе",    callback_data=f"trip_info_{tid}")],
        [InlineKeyboardButton(text="👥 Пассажиры",         callback_data=f"trip_pax_pub_{tid}")],
        [InlineKeyboardButton(text="❌ Отменить бронь",    callback_data=f"cancel_booking_{tid}")],
    ])
    await safe_send(uid,
        f"🎉 <b>Место забронировано!</b>\n\n{fmt_trip(trip, drv=drv)}\n\n"
        f"📞 {esc(drv.get('phone','—')) if drv else '—'}\n"
        f"🚗 {esc(drv.get('car_model','—')) if drv else '—'}\n\n"
        f"💰 К оплате: <b>{fmt_price(trip['price_per_seat'])}</b>\n"
        f"🚫 <b>Не переводите предоплату!</b>",
        reply_markup=kb_pass)
    # Уведомление водителю
    seats_left = trip["seats_free"]
    try:
        pc = await bot.get_chat(uid)
        pname = esc(pc.first_name or "Пассажир")
        pun = pc.username
    except:
        pname, pun = "Пассажир", None
    purl = f"https://t.me/{pun}" if pun else f"tg://user?id={uid}"
    kb_drv_rows = [
        [InlineKeyboardButton(text="💬 Написать пассажиру", url=purl)],
        [InlineKeyboardButton(text="👥 Все пассажиры",
                              callback_data=f"trip_passengers_{tid}")],
        [InlineKeyboardButton(text="🗺 Маршрут", url=maps_url)],
        [InlineKeyboardButton(text="✅ Завершить рейс",
                              callback_data=f"trip_complete_{tid}")],
        [InlineKeyboardButton(text="❌ Отклонить пассажира",
                              callback_data=f"trip_reject_{tid}_{uid}")],
    ]
    if is_full:
        await safe_send(trip["driver_id"],
            f"🔵 <b>Рейс #{tid} полностью набран!</b>\n"
            f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])} · {fmt_date_ru(trip['trip_date'])}\n"
            f"💺 Все {trip['seats_total']} мест заняты\n"
            f"💰 Итого: <b>{fmt_price(trip['price_per_seat'] * trip['seats_total'])}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_drv_rows))
    else:
        await safe_send(trip["driver_id"],
            f"🔔 <b>Новая бронь на рейс #{tid}!</b>\n"
            f"👤 <a href='{purl}'>{pname}</a>\n"
            f"💺 {trip['seats_total']-seats_left}/{trip['seats_total']} занято · {seats_left} своб.\n"
            f"💰 Оплатит: <b>{fmt_price(trip['price_per_seat'])}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_drv_rows))

@router.callback_query(F.data.startswith("cancel_booking_"))
async def cb_cancel_booking(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    trip = await DB.trip(tid)
    if not trip:
        await call.answer("❌ Рейс не найден"); return
    await call.answer("✅")
    ok = await DB.trip_cancel_booking(tid, uid)
    await safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    if ok:
        await safe_send(uid,
            f"✅ Бронь на рейс #{tid} отменена.\n"
            f"📍 {esc(trip['from_city'])} → {esc(trip['to_city'])} · {fmt_date_ru(trip['trip_date'])}",
            reply_markup=kb_main())
        await safe_send(trip["driver_id"],
            f"ℹ️ Пассажир отменил бронь на рейс #{tid}. Место освободилось.")
    else:
        await safe_send(uid, "❌ Бронь не найдена или уже отменена.")

@router.callback_query(F.data.startswith("trip_info_"))
async def cb_trip_info(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    trip = await DB.trip(tid)
    if not trip:
        await call.answer("❌"); return
    await call.answer()
    drv = await DB.driver(trip["driver_id"])
    avg, cnt = await DB.avg_rating(trip["driver_id"])
    maps_url = yandex_maps_url(trip["from_city"], trip["to_city"])
    du = (drv.get("username","") or "").lstrip("@") if drv else ""
    durl = f"https://t.me/{du}" if du else f"tg://user?id={trip['driver_id']}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 Написать водителю", url=durl)],
        [InlineKeyboardButton(text="🗺 Маршрут", url=maps_url)],
        [InlineKeyboardButton(text="👥 Пассажиры", callback_data=f"trip_pax_pub_{tid}")],
        [InlineKeyboardButton(text="❌ Отменить бронь", callback_data=f"cancel_booking_{tid}")],
    ])
    await safe_send(uid, fmt_trip(trip, drv=drv, avg=avg, cnt=cnt), reply_markup=kb)

@router.callback_query(F.data.startswith("trip_pax_pub_"))
async def cb_trip_pax_pub(call: CallbackQuery):
    uid = call.from_user.id
    tid = int(call.data.split("_")[-1])
    await call.answer()
    bookings = await DB.trip_bookings(tid)
    if not bookings:
        await safe_send(uid, "👥 Пока только вы."); return
    lines = [f"👥 Пассажиры рейса #{tid} ({len(bookings)} чел.):"]
    for b in bookings:
        pid = b["passenger_id"]
        try:
            pc = await bot.get_chat(pid)
            pname = esc(pc.first_name or "Пассажир")
            pun = pc.username
        except:
            pname, pun = "Пассажир", None
        purl = f"https://t.me/{pun}" if pun else f"tg://user?id={pid}"
        marker = " ← вы" if pid == uid else ""
        lines.append(f"  · <a href='{purl}'>{pname}</a>{marker}")
    await safe_send(uid, "\n".join(lines))

@router.message(F.text == "🎫 Мои брони", StateFilter("*"))
async def my_bookings(msg: Message, state: FSMContext):
    await state.clear()
    uid = msg.from_user.id
    bookings = await DB.passenger_bookings(uid)
    if not bookings:
        await safe_send(uid, "📭 У вас нет активных броней на рейсы.", reply_markup=kb_main()); return
    await safe_send(uid, f"🎫 <b>Ваши брони ({len(bookings)}):</b>")
    for b in bookings:
        trip_status = b.get("trip_status","open")
        status_icon = {"open":"🟢 Активен","full":"🔵 Набран","cancelled":"🔴 Отменён"}.get(trip_status,"—")
        drv = await DB.driver(b["driver_id"])
        maps_url = yandex_maps_url(b["from_city"], b["to_city"])
        du = (drv.get("username","") or "").lstrip("@") if drv else ""
        durl = f"https://t.me/{du}" if du else f"tg://user?id={b['driver_id']}"
        kb_rows = [
            [InlineKeyboardButton(text="📞 Написать водителю", url=durl)],
            [InlineKeyboardButton(text="🗺 Маршрут", url=maps_url)],
            [InlineKeyboardButton(text="ℹ️ Инфо о рейсе",
                                  callback_data=f"trip_info_{b['trip_id']}")],
            [InlineKeyboardButton(text="👥 Пассажиры",
                                  callback_data=f"trip_pax_pub_{b['trip_id']}")],
        ]
        if trip_status in ("open","full"):
            kb_rows.append([InlineKeyboardButton(
                text="❌ Отменить бронь", callback_data=f"cancel_booking_{b['trip_id']}")])
        await safe_send(uid,
            f"🎫 <b>Бронь #{b['id']}</b> · {status_icon}\n"
            f"🚐 Рейс #{b['trip_id']}\n"
            f"📍 {esc(b['from_city'])} → {esc(b['to_city'])}\n"
            f"📅 {fmt_date_ru(b['trip_date'])} · 🕐 {b['trip_time']}\n"
            f"🚘 {esc(b['car_class_label'])}\n"
            f"💰 К оплате: {fmt_price(b['price_per_seat'] * b['seats'])}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        await asyncio.sleep(0.05)


# ══════════════ ОБРАБОТЧИКИ-ЗАГЛУШКИ (FALLBACK) ══════════════
# ВАЖНО: эти хендлеры должны регистрироваться ПОСЛЕДНИМИ среди
# @router.message(...) / @router.callback_query(...).
# aiogram проверяет хендлеры в порядке регистрации и выполняет
# первый подошедший — если поставить заглушку раньше, она
# перехватит сообщение/нажатие и до нужного хендлера дело не дойдёт
# (именно так была перехвачена «🗓 Мои рейсы» и все инлайн-кнопки рейсов).
# ══════════════ ОБРАБОТЧИК ДЛЯ «БИТЫХ» СОСТОЯНИЙ ══════════════
@router.message(StateFilter("*"))
async def unknown_state_after_restart(msg: Message, state: FSMContext):
    """
    Обработчик для случаев, когда FSM-состояние сброшено (например, после перезапуска бота).
    Автоматически восстанавливает работу FSM-функций.
    """
    uid = msg.from_user.id
    cur = await state.get_state()
    
    # Случай 1: пользователь в активном FSM-шаге, но прислал текст вместо кнопки
    if cur is not None:
        await safe_send(
            uid,
            "ℹ️ Пожалуйста, используйте кнопки выше, чтобы продолжить."
        )
        return
    
    # Случай 2: неизвестный текст или левая команда — сбрасываем состояние
    # (все известные кнопки уже перехвачены своими хендлерами выше по файлу —
    #  сюда мы попадаем, только если ни один из них не подошёл)
    await state.clear()
    await safe_send(
        uid,
        "🔄 Состояние сброшено. Нажмите нужную кнопку снова.",
        reply_markup=kb_main()
    )

@router.callback_query()
async def cb_fallback(call: CallbackQuery):
    await call.answer("⚠️ Неизвестная команда")

# ══════════════ ЗАПУСК ══════════════
RUN_POLLING = os.environ.get("RUN_POLLING", "true").lower() != "false"

async def main():
    log.info("=" * 50)
    log.info("  🚕 МЕЖГОРОД ТРАНСФЕР v16.2 (aiogram 3 + FSM)")
    log.info(f"  Режим: {'ПОЛНЫЙ (бот + API)' if RUN_POLLING else 'ТОЛЬКО WEB API (домен)'}")
    log.info("=" * 50)
    await _init_pool()
    await DB.init()
    _BL_CACHE.update(await DB.bl_all())

    import uvicorn
    api_port = int(os.environ.get("PORT", 8080))

    async def _run_api_safely():
        """Веб-API не должен ронять весь процесс (включая Telegram-бота),
        если порт занят или сервер по другой причине не поднялся.
        Несколько попыток — вдруг порт освобождается с задержкой после передеплоя."""
        for attempt in range(1, 6):
            try:
                api_config = uvicorn.Config(api, host="0.0.0.0", port=api_port, log_level="warning")
                api_server = uvicorn.Server(api_config)
                await api_server.serve()
                return
            except SystemExit as e:
                log.error(f"⚠️ Попытка {attempt}/5: Web API не поднялся (SystemExit {e.code}), "
                          f"порт {api_port} занят.")
            except Exception as e:
                log.error(f"⚠️ Попытка {attempt}/5: Web API упал: {e}")
            if attempt < 5:
                await asyncio.sleep(3)
        log.error("❌ Web API так и не запустился за 5 попыток. Бот продолжает работать без API.")

    api_task = asyncio.create_task(_run_api_safely())
    log.info(f"  🌐 Web API запущен на http://0.0.0.0:{api_port} (/api/order)")

    if not RUN_POLLING:
        # Этот инстанс существует только ради веб-API с доменом.
        # Telegram-опрос (getUpdates) НЕ запускаем, чтобы не конфликтовать
        # с основным ботом intercity_bot1, который делает polling.
        try:
            await api_task
        finally:
            await bot.session.close()
            if _pg_pool:
                await _pg_pool.close()
        return

    _BL_CACHE.update(await DB.bl_all())
    cleaner_task = asyncio.create_task(_notified_cleaner())
    trial_task   = asyncio.create_task(_trial_expiry_notifier())
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        cleaner_task.cancel()
        trial_task.cancel()
        api_task.cancel()
        for t in (cleaner_task, trial_task, api_task):
            try:
                await t
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        if _pg_pool:
            await _pg_pool.close()

if __name__ == "__main__":
    asyncio.run(main())