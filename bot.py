import telebot, sqlite3, threading, time, os, json, re, requests, html, logging, urllib3
from telebot import types
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
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

geolocator = Yandex(api_key=YANDEX_GEO_KEY)
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log"), encoding='utf-8')
    ]
)
log = logging.getLogger(__name__)

# ══════════════ ТАРИФЫ ══════════════
# (label, year, price_rf, price_nt)
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

NT_KW      = ["лнр","днр","луганск","донецк","крым","симферополь","севастополь","херсон","запорожье","мариуполь","мелитополь"]
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

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ══════════════ КЭШ СЕССИЙ ══════════════
_cache, _clock = {}, threading.Lock()
SESSION_TTL = 600

def _cget(uid):
    with _clock:
        e = _cache.get(uid)
        if e and time.time() - e[0] < SESSION_TTL:
            return e[1]
        _cache.pop(uid, None)
    return None

def _cput(uid, r, s, d):
    with _clock:
        _cache[uid] = (time.time(), {"role": r, "step": s, "data": d})

def _cdel(uid):
    with _clock:
        _cache.pop(uid, None)

def _cache_cleaner():
    while True:
        time.sleep(300)
        with _clock:
            now = time.time()
            exp = [u for u, (t, _) in _cache.items() if now - t >= SESSION_TTL]
            for u in exp:
                del _cache[u]
        if exp:
            log.info(f"🧹 Очищено {len(exp)} сессий")

threading.Thread(target=_cache_cleaner, daemon=True).start()

# ══════════════ БД ══════════════
class DB:
    @staticmethod
    @contextmanager
    def conn():
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except:
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
                CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, role TEXT, step TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS ratings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, driver_id INTEGER,
                    passenger_id INTEGER, stars INTEGER CHECK(stars BETWEEN 1 AND 5), created_at TEXT,
                    FOREIGN KEY(order_id) REFERENCES orders(id),
                    FOREIGN KEY(driver_id) REFERENCES drivers(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_orders_passenger ON orders(passenger_id);
                CREATE INDEX IF NOT EXISTS idx_orders_driver    ON orders(driver_id);
                CREATE INDEX IF NOT EXISTS idx_orders_status    ON orders(status);
                CREATE INDEX IF NOT EXISTS idx_ratings_driver   ON ratings(driver_id);
                CREATE INDEX IF NOT EXISTS idx_ratings_order    ON ratings(order_id);
            """)
        log.info("✅ БД готова (SQLite)")

    # ── СЕССИИ ──
    @staticmethod
    def session(uid):
        if c := _cget(uid):
            return c
        with DB.conn() as c:
            r = c.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)).fetchone()
        res = ({"role": r["role"], "step": r["step"], "data": json.loads(r["data"] or "{}")}
               if r else {"role": None, "step": None, "data": {}})
        _cput(uid, res["role"], res["step"], res["data"])
        return res

    @staticmethod
    def session_upd(uid, role=None, step=None, data=None):
        cur = DB.session(uid)
        nr = role if role is not None else cur["role"]
        ns = None if step == "" else (step if step is not None else cur["step"])
        nd = data if data is not None else cur["data"]
        try:
            with DB.conn() as c:
                c.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                          (uid, nr, ns, json.dumps(nd)))
            _cput(uid, nr, ns, nd)
        except Exception as e:
            _cdel(uid); raise e

    @staticmethod
    def session_clr(uid):
        cur = DB.session(uid)
        try:
            with DB.conn() as c:
                c.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                          (uid, cur["role"], None, "{}"))
            _cput(uid, cur["role"], None, {})
        except Exception as e:
            _cdel(uid); raise e

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
            for tbl in ("drivers","subscriptions","pending_subscriptions","sessions"):
                c.execute(f"DELETE FROM {tbl} WHERE user_id=?", (uid,))
            c.execute("UPDATE orders SET status='open',driver_id=NULL,taken_at=NULL "
                      "WHERE driver_id=? AND status='taken'", (uid,))
        _cdel(uid)
        for oid, pid in active_orders:
            try:
                safe_send(pid, f"⚠️ <b>Водитель удалён из системы.</b>\nЗаказ #{oid} снова открыт.")
                update_channel_post(oid)
                _notify_drivers(oid)
            except Exception as e:
                log.error(f"Ошибка уведомления по заказу #{oid}: {e}")

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
        for k in kw:
            if k not in ALLOWED_COLS: raise ValueError(f"Недопустимая колонка: {k}")
        sets = ", ".join(f"{k}=?" for k in kw)
        with DB.conn() as c:
            c.execute(f"UPDATE orders SET {sets} WHERE id=?", list(kw.values()) + [oid])

    @staticmethod
    def order_cancel_atomic(oid, uid, role="passenger"):
        with DB.conn() as c:
            if role == "passenger":
                cur = c.execute("UPDATE orders SET status='cancelled' "
                                "WHERE id=? AND passenger_id=? AND status IN ('open','taken')", (oid, uid))
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
                "SELECT * FROM orders WHERE passenger_id=? ORDER BY created_at DESC LIMIT ?", (uid, limit)
            ).fetchall()]

    @staticmethod
    def driver_orders(uid, limit=5):
        with DB.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE driver_id=? ORDER BY taken_at DESC LIMIT ?", (uid, limit)
            ).fetchall()]

    @staticmethod
    def open_orders():
        with DB.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE status='open' ORDER BY created_at DESC"
            ).fetchall()]

    @staticmethod
    def all_orders(limit=10):
        with DB.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()]

    @staticmethod
    def stats():
        with DB.conn() as c:
            t  = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            o  = c.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0]
            d  = c.execute("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0]
            dr = c.execute("SELECT COUNT(*) FROM drivers").fetchone()[0]
            dc = c.execute("SELECT COUNT(*) FROM drivers WHERE docs_verified=1").fetchone()[0]
        sub = sum(1 for drv in DB.all_drivers() if DB.sub_info(drv["user_id"])[2])
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
        log.info(f"⭐ Рейтинг сохранён: заказ #{order_id}, водитель {driver_id}, оценка {stars}")

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

def drv_name(d):
    if not d: return "Водитель"
    n = f"{d.get('first_name') or d.get('name') or ''} {d.get('last_name') or ''}".strip()
    return html.escape(n) if n else "Водитель"

def profile_link(d):
    if not d: return "—"
    p = d.get('profile_link','')
    if p and p.startswith("https://"): return p
    u = d.get('username','')
    if u and not u.startswith("tg://"): return f"https://t.me/{u.lstrip('@')}"
    uid = d.get('user_id')
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
def fmt_price(p):   return f"{p:,}".replace(",", " ") + " ₽"
def is_valid_city(c):
    return bool(c) and len(c.strip()) >= 2 and bool(re.match(r"^[а-яА-ЯёЁa-zA-Z\s\-\.]+$", c.strip()))

def geocode(city):
    if not is_valid_city(city): return None
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
               else (False,f"🔒 Ваш класс ({TARIFFS_RF.get(dc,{}).get('label',dc)}) "
                           f"ниже требуемого ({TARIFFS_RF.get(oc,{}).get('label',oc)})")
    return False, "🔒 Неизвестный класс"

def safe_send(cid, text, **kw):
    try: return bot.send_message(cid, text, **kw)
    except Exception as e: log.error(f"Ошибка отправки {cid}: {e}")

def safe_edit_markup(cid, mid, **kw):
    try: bot.edit_message_reply_markup(cid, mid, **kw)
    except Exception as e: log.error(f"Ошибка редактирования разметки: {e}")

def notify_driver_change(tgt, label, value):
    safe_send(tgt, f"ℹ️ <b>Администратор изменил {label}:</b> {html.escape(str(value))}")

def fmt_order(o, show_price=True):
    t  = tariffs(o.get("from_city",""), o.get("to_city",""))
    cc = o.get("car_class","standard")
    cl = html.escape(t.get(cc,{}).get("label",cc))
    dist = o.get("distance_km")
    dt = (f"{dist:.0f} км" if dist
          else ("⚠️ Не рассчитано" if o.get("status") in ("open","pending") else "уточняется"))
    p  = o.get("price")
    nt = is_nt(o.get("from_city","")) or is_nt(o.get("to_city",""))
    lines = [
        f"🚕 <b>Заказ #{o['id']}</b> · {'🆕 НТ' if nt else '🇷🇺 РФ'}",
        f"📍 {html.escape(o.get('from_city','—'))} → {html.escape(o.get('to_city','—'))}",
        f"📏 {dt} | 📅 {o.get('trip_date','—')} | 🕐 {o.get('trip_time','—')}",
        f"👥 {o.get('passengers','—')} чел. | 🚘 {cl}",
    ]
    if show_price and p:
        lines.append(f"💰 {t.get(cc,{}).get('price',0)} ₽/км | 💵 <b>{fmt_price(p)}</b>")
    elif show_price:
        lines.append("💵 <i>Стоимость уточняется</i>")
    lines.append("⚠️ Платные дороги — отдельно")
    if w := o.get("wishes"):
        lines.append(f"💬 {html.escape(w)}")
    lines.append(f"📌 {STATUS_ICON.get(o.get('status','open'),'—')}")
    return "\n".join(lines)


# ══════════════ КЛАВИАТУРЫ ══════════════
def kb_main():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("🚕 Создать заказ", "🚗 Я водитель")
    m.row("📋 Мои заказы", "📊 Тарифы")
    return m

def kb_driver(uid):
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    _, _, active = DB.sub_info(uid)
    m.row("📦 Доступные заказы", "👤 Мой профиль")
    m.row("💳 Абонемент", "✅ Подписка активна" if active else "❌ Нет подписки")
    m.row("📈 Мои поездки", "🔙 Главное меню")
    return m

def kb_cancel():
    return types.ReplyKeyboardMarkup(resize_keyboard=True).add("❌ Отменить")

def kb_car_class(region="rf", passengers=1):
    t = TARIFFS_NT if region == "new" else TARIFFS_RF
    classes = ["minivan"] if passengers >= 5 else ["standard","comfort","comfort+","business","minivan"]
    m = types.InlineKeyboardMarkup(row_width=1)
    for cc in classes:
        if cc in t:
            txt = f"{t[cc]['label']} · {t[cc]['price']} ₽/км"
            if cc == "minivan" and passengers >= 5:
                txt += " ✅ (рекомендуется)"
            m.add(types.InlineKeyboardButton(txt, callback_data=f"car_{cc}"))
    m.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_order"))
    return m

def kb_pclass():
    m = types.InlineKeyboardMarkup(row_width=1)
    for k in COMFORT_H + ["minivan"]:
        v  = TARIFFS_RF[k]
        yr = f" ({v['year']})" if v.get('year') else ""
        m.add(types.InlineKeyboardButton(f"{v['label']}{yr}", callback_data=f"pclass_{k}"))
    return m

def kb_passengers():
    m = types.InlineKeyboardMarkup(row_width=4)
    m.add(*[types.InlineKeyboardButton(str(i), callback_data=f"pax_{i}") for i in range(1,9)])
    m.add(types.InlineKeyboardButton("❌ Отменить", callback_data="cancel_order"))
    return m

def kb_subs():
    m = types.InlineKeyboardMarkup(row_width=1)
    for k, p in SUBS.items():
        m.add(types.InlineKeyboardButton(f"💳 {p['label']}", callback_data=f"sub_{k}"))
    m.add(types.InlineKeyboardButton("◀️ Назад", callback_data="back_driver"))
    return m

def kb_stars(oid):
    m = types.InlineKeyboardMarkup(row_width=5)
    m.add(*[types.InlineKeyboardButton(f"{i}⭐", callback_data=f"rate_{oid}_{i}") for i in range(1,6)])
    return m


# ══════════════ КОМАНДЫ ══════════════
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid = msg.chat.id
    if DB.bl_check(uid):
        safe_send(uid, "⛔ Вы заблокированы."); return
    parts = msg.text.strip().split()
    if len(parts) > 1 and parts[1].startswith("order_"):
        try:
            oid   = int(parts[1].split("_")[1])
            order = DB.order(oid)
            if not order or order["status"] != "open":
                safe_send(uid, "⚠️ Заказ не найден или уже взят.", reply_markup=kb_main()); return
            drv = DB.driver(uid)
            if not drv:
                safe_send(uid, "❌ Сначала зарегистрируйтесь как водитель.", reply_markup=kb_main()); return
            _, _, active = DB.sub_info(uid)
            if not active:
                safe_send(uid, "🔒 Нет абонемента.", reply_markup=kb_driver(uid)); return
            if not drv.get("docs_verified"):
                safe_send(uid, "⏳ Профиль ещё не верифицирован.", reply_markup=kb_driver(uid)); return
            if order["passenger_id"] == uid:
                safe_send(uid, "❌ Нельзя взять свой заказ.", reply_markup=kb_driver(uid)); return
            ct, rsn = can_take_order(drv, order)
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton("✅ Взять заказ", callback_data=f"take_{oid}")
                  if ct else types.InlineKeyboardButton(f"🔒 {rsn}", callback_data="cant_take"))
            DB.session_upd(uid, role="driver")
            safe_send(uid, f"🔗 <b>Заказ из канала:</b>\n\n{fmt_order(order)}", reply_markup=m)
            return
        except Exception as e:
            log.error(f"Deep-link: {e}")
    DB.session_clr(uid)
    DB.session_upd(uid, role="")
    safe_send(uid,
        f"👋 <b>{html.escape(msg.from_user.first_name or 'друг')}</b>, "
        f"добро пожаловать в Межгород Трансфер Россия!\n\n"
        "🔹 Пассажирам — оформить заказ\n"
        "🔹 Водителям — получать заказы\n"
        "📢 Канал: @intercitytrans",
        reply_markup=kb_main())

@bot.message_handler(commands=["help"])
def cmd_help(msg):
    safe_send(msg.chat.id,
        "📖 <b>Справка</b>\n\n"
        "<b>Пассажир:</b> Создать заказ, Мои заказы\n"
        "<b>Водитель:</b> Я водитель → Зарегистрироваться → Абонемент\n\n"
        "По вопросам: @Olegan7979")

@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if msg.chat.id not in ADMIN_IDS: return
    m = types.InlineKeyboardMarkup(row_width=1)
    for t, d in [("📋 Заказы","adm_orders"),("👥 Водители","adm_drivers"),
                 ("⛔ ЧС","adm_bl"),("💳 Оплаты","adm_subs"),
                 ("📊 Статистика","adm_stats"),("📖 Команды","adm_help")]:
        m.add(types.InlineKeyboardButton(t, callback_data=d))
    safe_send(msg.chat.id, "🔧 <b>Админ-панель</b>", reply_markup=m)

@bot.message_handler(commands=["ban"])
def cmd_ban(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        t = int(msg.text.split()[1])
        DB.bl_add(t)
        bot.reply_to(msg, f"⛔ {t} в ЧС")
        safe_send(t, "⛔ Вы заблокированы.")
    except: bot.reply_to(msg, "⚠️ /ban ID")

@bot.message_handler(commands=["unban"])
def cmd_unban(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        t = int(msg.text.split()[1])
        DB.bl_remove(t)
        bot.reply_to(msg, f"✅ {t} разблокирован")
    except: bot.reply_to(msg, "⚠️ /unban ID")

@bot.message_handler(commands=["unsub"])
def cmd_unsub(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        t = int(msg.text.split()[1])
        DB.sub_expire(t)
        bot.reply_to(msg, f"✅ Подписка {t} аннулирована")
        safe_send(t, "⛔ Абонемент аннулирован.")
    except: bot.reply_to(msg, "⚠️ /unsub ID")

@bot.message_handler(commands=["deldriver"])
def cmd_deldrv(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        t   = int(msg.text.split()[1])
        drv = DB.driver(t)
        if not drv: bot.reply_to(msg, "❌ Не найден"); return
        DB.driver_del(t)
        bot.reply_to(msg, f"✅ {drv_name(drv)} удалён")
        safe_send(t, "🗑 Ваш профиль водителя удалён.")
    except: bot.reply_to(msg, "⚠️ /deldriver ID")

@bot.message_handler(commands=["stats"])
def cmd_stats(msg):
    if msg.chat.id not in ADMIN_IDS: return
    s = DB.stats()
    safe_send(msg.chat.id,
        f"📊 Заказов: {s['total']} | Открыто: {s['open']} | Завершено: {s['done']}\n"
        f"🚗 Водителей: {s['drivers']} | Подписка: {s['subscribed']} | Проверены: {s['docs_ok']}")

@bot.message_handler(commands=["recalc"])
def cmd_recalc(msg):
    if msg.chat.id not in ADMIN_IDS: return
    try:
        _, oid, dkm, price = msg.text.split()
        oid, dkm, price = int(oid), float(dkm), int(price)
        order = DB.order(oid)
        if not order: safe_send(msg.chat.id, "❌ Заказ не найден"); return
        DB.order_upd(oid, distance_km=dkm, price=price, status="open")
        safe_send(msg.chat.id, f"✅ Заказ #{oid} пересчитан")
        if order["passenger_id"]:
            safe_send(order["passenger_id"],
                f"✅ <b>Заказ #{oid} рассчитан!</b>\n\n{fmt_order(DB.order(oid))}")
        _post_to_channel(oid)
        _notify_drivers(oid)
    except Exception as e:
        safe_send(msg.chat.id, f"❌ {e}\nФормат: /recalc order_id distance_km price")


# ══════════════ МЕНЮ ══════════════
@bot.message_handler(func=lambda m: m.text == "🔙 Главное меню")
def to_main(msg):
    DB.session_upd(msg.chat.id, role="", step="")
    safe_send(msg.chat.id, "🏠 Главное меню", reply_markup=kb_main())

@bot.message_handler(func=lambda m: m.text == "📊 Тарифы")
def show_tariffs(msg):
    lines = ["💰 <b>Тарифы · РФ</b> (₽/км)"]
    for v in TARIFFS_RF.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b>")
    lines.append("\n💰 <b>Тарифы · Новые территории</b>")
    for v in TARIFFS_NT.values():
        lines.append(f"  {v['label']} — <b>{v['price']} ₽</b>")
    lines.append("\n⚠️ Платные дороги — отдельно\n🚫 Торг запрещён")
    safe_send(msg.chat.id, "\n".join(lines))


# ══════════════ СОЗДАНИЕ ЗАКАЗА ══════════════
@bot.message_handler(func=lambda m: m.text == "🚕 Создать заказ")
def order_start(msg):
    uid = msg.chat.id
    if DB.bl_check(uid): safe_send(uid, "⛔ Заблокированы."); return
    recent = DB.passenger_orders(uid, limit=1)
    if recent and recent[0]["status"] in ("open","taken"):
        safe_send(uid, "⚠️ У вас уже есть активный заказ. Отмените его перед созданием нового.",
                  reply_markup=kb_main()); return
    s = DB.session(uid)
    if s.get("step") in ["from_city","to_city","trip_date","trip_time","passengers","car_class","wishes"]:
        safe_send(uid, "⚠️ У вас уже есть активное создание заказа. Завершите его или нажмите «❌ Отменить»",
                  reply_markup=kb_cancel()); return
    DB.session_upd(uid, step="from_city", data={})
    safe_send(uid, "🗺 <b>Шаг 1/7</b>\n📍 Введите город отправления\n<i>Например: Москва</i>",
              reply_markup=kb_cancel())

@bot.message_handler(func=lambda m: m.text == "❌ Отменить")
def order_cancel(msg):
    uid  = msg.chat.id
    step = DB.session(uid).get("step")
    DB.session_clr(uid)
    is_drv = step and step.startswith("profile_")
    safe_send(uid, "❌ Отменено.",
              reply_markup=kb_driver(uid) if (is_drv or DB.driver(uid)) else kb_main())

def step_from(msg):
    uid, city = msg.chat.id, msg.text.strip()
    if not is_valid_city(city): safe_send(uid, "❌ Некорректное название"); return
    s = DB.session(uid); s["data"]["from_city"] = city
    DB.session_upd(uid, step="to_city", data=s["data"])
    safe_send(uid, f"✅ Откуда: <b>{html.escape(city)}</b>\n\n🗺 <b>Шаг 2/7</b>\n🏁 Введите город назначения")

def step_to(msg):
    uid, city = msg.chat.id, msg.text.strip()
    if not is_valid_city(city): safe_send(uid, "❌ Некорректное название"); return
    s = DB.session(uid)
    if s["data"]["from_city"].lower() == city.lower():
        safe_send(uid, "❌ Города отправления и назначения не должны совпадать"); return
    s["data"]["to_city"] = city
    DB.session_upd(uid, step="trip_date", data=s["data"])
    safe_send(uid, f"✅ Куда: <b>{html.escape(city)}</b>\n\n🗺 <b>Шаг 3/7</b>\n📅 Дата (ДД.ММ.ГГГГ)")

def step_date(msg):
    uid = msg.chat.id
    try:
        d = datetime.strptime(msg.text.strip(), "%d.%m.%Y")
        if d.date() < now_dt().date():
            safe_send(uid, "❌ Дата не может быть в прошлом"); return
        s = DB.session(uid); s["data"]["trip_date"] = d.strftime("%d.%m.%Y")
        DB.session_upd(uid, step="trip_time", data=s["data"])
        safe_send(uid, f"✅ Дата: <b>{d.strftime('%d.%m.%Y')}</b>\n\n🗺 <b>Шаг 4/7</b>\n🕐 Время (ЧЧ:ММ)")
    except: safe_send(uid, "❌ Формат ДД.ММ.ГГГГ")

def step_time(msg):
    uid, t = msg.chat.id, msg.text.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", t):
        safe_send(uid, "❌ Формат ЧЧ:ММ"); return
    try:
        h, m = map(int, t.split(":"))
        if not (0 <= h <= 23 and 0 <= m <= 59): safe_send(uid, "❌ Некорректное время"); return
        s = DB.session(uid)
        if ds := s["data"].get("trip_date"):
            trip_dt = datetime.strptime(f"{ds} {t}", "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
            if trip_dt < now_dt(): safe_send(uid, "❌ Поездка не может быть в прошлом"); return
        s["data"]["trip_time"] = f"{h:02d}:{m:02d}"
        DB.session_upd(uid, step="passengers", data=s["data"])
        safe_send(uid, f"✅ Время: <b>{h:02d}:{m:02d}</b>\n\n🗺 <b>Шаг 5/7</b>\n👥 Пассажиры:",
                  reply_markup=kb_passengers())
    except: safe_send(uid, "❌ Некорректное время")

def show_car_class(uid):
    s = DB.session(uid); data = s["data"]
    passengers = data.get("passengers", 1)
    region = "new" if (is_nt(data.get("from_city","")) or is_nt(data.get("to_city",""))) else "rf"
    data["region"] = region
    DB.session_upd(uid, step="car_class", data=data)
    hint = "\n\n⚠️ <b>Для 5 и более пассажиров доступен только минивэн!</b>" if passengers >= 5 else ""
    safe_send(uid,
        f"🗺 <b>Шаг 6/7</b> — {'🆕 НТ' if region=='new' else '🇷🇺 РФ'}\n"
        f"👥 Пассажиров: {passengers}{hint}",
        reply_markup=kb_car_class(region, passengers))

def ask_wishes(uid):
    DB.session_upd(uid, step="wishes")
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("Нет", "❌ Отменить")
    safe_send(uid, "🗺 <b>Шаг 7/7</b>\n💬 Пожелания? Нет — нажмите кнопку", reply_markup=m)

def step_wishes(msg):
    uid, wish = msg.chat.id, msg.text.strip()
    if len(wish) > 500: safe_send(uid, "❌ Слишком длинный текст (макс. 500 символов)"); return
    s = DB.session(uid)
    s["data"]["wishes"] = "" if wish.lower() in ["нет","—","-","no"] else wish
    DB.session_upd(uid, data=s["data"])
    finalize_order(uid)

def finalize_order(uid):
    s = DB.session(uid); data = s["data"]
    DB.session_clr(uid)
    safe_send(uid, "⏳ Рассчитываю...")
    threading.Thread(target=_finalize_order_thread, args=(uid, data)).start()

def _finalize_order_thread(uid, data):
    try:
        dist = get_distance(data["from_city"], data["to_city"])
        dkm = price = None
        if dist:
            dkm   = round(dist * DIST_COEFF)
            price = calc_price(dkm, data["car_class"], data["from_city"], data["to_city"])
        oid   = DB.order_create({**data, "passenger_id": uid,
                                  "distance_km": dkm, "price": price,
                                  "status": "open" if dkm else "pending"})
        order = DB.order(oid)
        cancel_btn = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{oid}")
        )
        warn = (
            f"✅ <b>Заказ создан!</b>\n\n{fmt_order(order)}\n\n"
            + ("📲 Заказ опубликован в канале @intercitytrans\n\n" if dkm
               else "⚠️ <b>Расстояние не рассчитано</b>\n\n")
            + "⏳ <i>Пожалуйста, ожидайте — с вами свяжется водитель.</i>\n\n"
            + "🚫 <b>Не переводите предоплату!</b>\n"
            + "Оплата — только водителю после поездки.\n"
            + "<i>Если водитель просит предоплату — сообщите @Olegan7979</i>"
        )
        safe_send(uid, warn, reply_markup=cancel_btn)
        safe_send(uid, "✅ Заказ создан! Возвращаемся в меню.", reply_markup=kb_main())
        if dkm:
            _post_to_channel(oid)
            _notify_drivers(oid)
        else:
            for aid in ADMIN_IDS:
                safe_send(aid, f"⚠️ Заказ #{oid} без расстояния!\n"
                          f"{data.get('from_city')} → {data.get('to_city')}\n"
                          f"Используйте /recalc {oid} <distance_km> <price>")
    except Exception as e:
        log.error(f"Ошибка создания заказа: {e}")
        safe_send(uid, "❌ Ошибка. Попробуйте ещё раз.", reply_markup=kb_main())

def _post_to_channel(oid):
    if not GROUP_CHAT_ID: return
    order = DB.order(oid)
    if not order or order["status"] != "open": return
    m = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("✅ Взять заказ",
                                   url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}")
    )
    msg = safe_send(GROUP_CHAT_ID, fmt_order(order), reply_markup=m)
    if msg: DB.order_upd(oid, channel_msg_id=msg.message_id)

def update_channel_post(oid):
    order = DB.order(oid)
    if not order or not order.get("channel_msg_id") or not GROUP_CHAT_ID: return
    if order["status"] in ("cancelled","taken"):
        try: bot.delete_message(GROUP_CHAT_ID, order["channel_msg_id"])
        except Exception as e: log.error(f"Ошибка удаления поста: {e}")
    elif order["status"] == "open":
        m = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("✅ Взять заказ",
                                       url=f"https://t.me/{BOT_USERNAME}?start=order_{oid}")
        )
        try: bot.edit_message_text(fmt_order(order), GROUP_CHAT_ID, order["channel_msg_id"], reply_markup=m)
        except Exception as e: log.error(f"Ошибка обновления поста: {e}")

_NOTIFIED, _NLOCK = {}, threading.Lock()

def _notify_drivers(oid, exclude_uid=None):
    order = DB.order(oid)
    if not order or order["status"] != "open": return
    with _NLOCK:
        notified = _NOTIFIED.get(oid, set())
    drivers = [d for d in DB.active_drivers()
               if not (exclude_uid and d["user_id"] == exclude_uid)
               and d["user_id"] != order["passenger_id"]
               and d["user_id"] not in notified]
    sent = 0
    for drv in drivers[:NOTIFY_LIMIT]:
        ct, _ = can_take_order(drv, order)
        if not ct: continue
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("✅ Взять",       callback_data=f"take_{oid}"))
        m.add(types.InlineKeyboardButton("➡️ Пропустить", callback_data=f"skip_{oid}"))
        safe_send(drv["user_id"], f"🔔 <b>Подходит вам!</b>\n\n{fmt_order(order)}", reply_markup=m)
        with _NLOCK:
            _NOTIFIED.setdefault(oid, set()).add(drv["user_id"])
        sent += 1
        if sent % NOTIFY_BATCH == 0:
            time.sleep(NOTIFY_DELAY)

def _clear_notified(oid):
    with _NLOCK:
        _NOTIFIED.pop(oid, None)

def _notified_cleaner():
    while True:
        time.sleep(600)
        with _NLOCK:
            dead = [oid for oid in list(_NOTIFIED)
                    if not (o := DB.order(oid)) or o["status"] != "open"]
            for oid in dead: del _NOTIFIED[oid]
        if dead: log.info(f"🧹 Кэш уведомлений: {len(dead)} заказов")

threading.Thread(target=_notified_cleaner, daemon=True).start()


# ══════════════ МОИ ЗАКАЗЫ ══════════════
@bot.message_handler(func=lambda m: m.text == "📋 Мои заказы")
def my_orders(msg):
    uid = msg.chat.id
    if DB.driver(uid) and DB.session(uid).get("role") == "driver":
        drv_orders = [o for o in DB.driver_orders(uid) if o["status"] in ("taken","completed")]
        if drv_orders:
            safe_send(uid, f"🚗 <b>Поездки как водитель</b> ({len(drv_orders)}):")
            for o in drv_orders:
                m = types.InlineKeyboardMarkup()
                if o["status"] == "taken":
                    m.add(types.InlineKeyboardButton("❌ Отказаться",
                          callback_data=f"driver_cancel_{o['id']}"))
                safe_send(uid, fmt_order(o), reply_markup=m)
    result = DB.passenger_orders(uid)
    if not result: safe_send(uid, "📋 Нет пассажирских заказов."); return
    safe_send(uid, f"📋 <b>Ваши заказы как пассажир</b> ({len(result)}):")
    for o in result:
        if o["status"] in ("open","taken"):
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{o['id']}"))
            safe_send(uid, fmt_order(o), reply_markup=m)
        elif o["status"] == "completed" and o.get("driver_id") and not DB.has_rating(o["id"], uid):
            safe_send(uid, fmt_order(o) + "\n\n⭐ <b>Оцените поездку:</b>", reply_markup=kb_stars(o["id"]))
        else:
            safe_send(uid, fmt_order(o))


# ══════════════ ВОДИТЕЛЬ ══════════════
@bot.message_handler(func=lambda m: m.text == "🚗 Я водитель")
def driver_enter(msg):
    uid = msg.chat.id
    if DB.bl_check(uid): safe_send(uid, "⛔ Вы заблокированы."); return
    DB.session_upd(uid, role="driver")
    drv = DB.driver(uid)
    if not drv:
        safe_send(uid,
            "🚗 <b>Режим водителя</b>\n\nДля регистрации нажмите кнопку ниже.\n\n"
            "<b>Что потребуется:</b>\n1️⃣ Поделиться контактом\n2️⃣ Марка и модель авто\n"
            "3️⃣ Год выпуска\n4️⃣ Гос. номер\n5️⃣ Класс авто\n\n"
            "После регистрации администратор верифицирует профиль.",
            reply_markup=(types.ReplyKeyboardMarkup(resize_keyboard=True)
                          .row("👤 Зарегистрироваться").row("🔙 Главное меню")))
        return
    exp, dl, active = DB.sub_info(uid)
    avg, cnt = DB.avg_rating(uid)
    safe_send(uid,
        f"🚗 <b>{drv_name(drv)}</b>\n"
        f"🚘 {html.escape(drv.get('car_model','—'))} {drv.get('car_year','—')} г.\n"
        f"🔢 {html.escape(drv.get('car_number','—'))}\n"
        f"🏷 {html.escape(drv.get('car_class_label','—'))}\n"
        f"📞 {html.escape(drv.get('phone','—'))}\n"
        f"📄 {'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает верификации'}\n"
        f"💳 {'✅ Подписка до '+exp+' ('+str(dl)+' дн.)' if active else '❌ Нет подписки'}\n"
        f"{'⭐ Рейтинг: '+str(avg)+' ('+str(cnt)+' оценок)' if cnt else '⭐ Нет оценок'}",
        reply_markup=kb_driver(uid))

@bot.message_handler(func=lambda m: m.text == "📦 Доступные заказы")
def avail_orders(msg):
    uid = msg.chat.id
    drv = DB.driver(uid)
    if not drv: safe_send(uid, "❌ Сначала зарегистрируйтесь."); return
    _, _, active = DB.sub_info(uid)
    if not active: safe_send(uid, "🔒 Нет абонемента."); return
    if not drv.get("docs_verified"): safe_send(uid, "⏳ Профиль ещё не верифицирован администратором."); return
    all_open = [o for o in DB.open_orders() if o["passenger_id"] != uid]
    if not all_open: safe_send(uid, "📭 Нет доступных заказов."); return
    can = sum(1 for o in all_open if can_take_order(drv, o)[0])
    safe_send(uid, f"📦 <b>Открыто: {len(all_open)}</b> | ✅ Доступно: <b>{can}</b>\n"
              f"🏷 {html.escape(drv.get('car_class_label',''))}")
    for o in all_open[:10]:
        ct, rsn = can_take_order(drv, o)
        m = types.InlineKeyboardMarkup()
        m.add(types.InlineKeyboardButton("✅ Взять" if ct else f"🔒 {rsn}",
              callback_data=f"take_{o['id']}" if ct else "cant_take"))
        safe_send(uid, fmt_order(o), reply_markup=m)

@bot.message_handler(func=lambda m: m.text == "📈 Мои поездки")
def driver_trips(msg):
    uid    = msg.chat.id
    result = [o for o in DB.driver_orders(uid) if o["status"] in ("taken","completed")]
    if not result: safe_send(uid, "📈 Нет поездок."); return
    safe_send(uid, f"📈 <b>Поездки</b> ({len(result)}):")
    for o in result:
        m = types.InlineKeyboardMarkup()
        if o["status"] == "taken":
            m.add(types.InlineKeyboardButton("❌ Отказаться",
                  callback_data=f"driver_cancel_{o['id']}"))
        safe_send(uid, fmt_order(o), reply_markup=m)

@bot.message_handler(func=lambda m: m.text in ["✅ Подписка активна","❌ Нет подписки"])
def sub_btn(msg): subscription_menu(msg)


# ══════════════ ПРОФИЛЬ ВОДИТЕЛЯ ══════════════
@bot.message_handler(func=lambda m: m.text == "👤 Зарегистрироваться")
def register_driver(msg):
    uid = msg.chat.id
    if DB.driver(uid): safe_send(uid, "ℹ️ Вы уже зарегистрированы.", reply_markup=kb_driver(uid)); return
    _start_profile(uid)

@bot.message_handler(func=lambda m: m.text == "👤 Мой профиль")
def profile_menu(msg):
    uid = msg.chat.id
    drv = DB.driver(uid)
    if not drv: safe_send(uid, "❌ Профиль не найден.", reply_markup=kb_main()); return
    avg, cnt  = DB.avg_rating(uid)
    pl        = profile_link(drv)
    ph        = f'<a href="{pl}">Открыть</a>' if pl.startswith("http") else pl
    exp, dl, active = DB.sub_info(uid)
    safe_send(uid,
        f"👤 <b>{drv_name(drv)}</b>\n"
        f"🚘 {html.escape(drv.get('car_model','—'))} ({drv.get('car_year','—')})\n"
        f"🔢 {html.escape(drv.get('car_number','—'))}\n"
        f"🏷 {html.escape(drv.get('car_class_label','—'))}\n"
        f"📞 {html.escape(drv.get('phone','—'))}\n"
        f"💬 {ph}\n"
        f"📄 Статус: <b>{'✅ Верифицирован' if drv.get('docs_verified') else '⏳ Ожидает'}</b>\n"
        f"💳 {'✅ До '+exp+' ('+str(dl)+' дн.)' if active else '❌ Нет подписки'}\n"
        f"{'⭐ Рейтинг: '+str(avg)+'/5 ('+str(cnt)+' оценок)' if cnt else '⭐ Нет оценок'}\n\n"
        f"<i>Для изменения данных обратитесь к администратору @Olegan7979</i>")

def _start_profile(uid):
    DB.session_upd(uid, step="profile_share_contact", data={})
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    m.add(types.KeyboardButton("📱 Поделиться контактом", request_contact=True))
    m.add(types.KeyboardButton("❌ Отменить"))
    safe_send(uid,
        "📝 <b>Регистрация — Шаг 1/5</b>\n\n"
        "📱 Нажмите кнопку ниже, чтобы поделиться контактом.\n"
        "<i>Имя и номер телефона будут взяты из вашего профиля Telegram.</i>",
        reply_markup=m)

@bot.message_handler(content_types=["contact"])
def p_contact(msg):
    uid  = msg.chat.id
    if DB.session(uid).get("step") != "profile_share_contact": return
    if msg.contact.user_id != uid:
        safe_send(uid, "❌ Отправьте свой контакт"); return
    try:
        ui    = bot.get_chat(uid)
        fn, ln = ui.first_name or "", ui.last_name or ""
        un    = f"@{ui.username}" if ui.username else ""
        hp    = bot.get_user_profile_photos(uid, limit=1).total_count > 0
        phone = msg.contact.phone_number or ""
    except Exception as e:
        log.error(f"Профиль {uid}: {e}")
        safe_send(uid, "❌ Ошибка получения профиля Telegram."); return
    s = DB.session(uid)
    s["data"].update(first_name=fn, last_name=ln, name=fn, username=un, has_photo=hp, phone=phone,
                     profile_link=(f"https://t.me/{un.lstrip('@')}" if un else f"tg://user?id={uid}"))
    DB.session_upd(uid, step="profile_car_model", data=s["data"])
    safe_send(uid,
        f"✅ Контакт получен!\n👤 {html.escape(fn)} {html.escape(ln)}\n📞 {html.escape(phone)}\n\n"
        f"<b>Шаг 2/5 — Марка и модель авто:</b>\n<i>Например: Toyota Camry</i>",
        reply_markup=kb_cancel())

def p_model(msg):
    uid, model = msg.chat.id, msg.text.strip()
    if not (2 <= len(model) <= 100): safe_send(uid, "❌ Введите марку и модель (2–100 символов)"); return
    s = DB.session(uid); s["data"]["car_model"] = model
    DB.session_upd(uid, step="profile_car_year", data=s["data"])
    safe_send(uid, f"✅ <b>{html.escape(model)}</b>\n\n<b>Шаг 3/5 — Год выпуска:</b>")

def p_year(msg):
    uid = msg.chat.id
    try:
        year = int(msg.text.strip())
        if not (1990 <= year <= now_dt().year + 1): raise ValueError
        if year < 2008: safe_send(uid, "❌ Авто старше 2008 г. не допускаются."); return
        s = DB.session(uid); s["data"]["car_year"] = year
        DB.session_upd(uid, step="profile_car_number", data=s["data"])
        safe_send(uid, f"✅ <b>{year}</b>\n\n<b>Шаг 4/5 — Гос. номер:</b>")
    except: safe_send(uid, "❌ Некорректный год")

def p_number(msg):
    uid, number = msg.chat.id, msg.text.strip().upper().replace(" ","")
    if not number: safe_send(uid, "❌ Введите номер автомобиля"); return
    s = DB.session(uid); s["data"]["car_number"] = number
    DB.session_upd(uid, step="profile_car_class", data=s["data"])
    safe_send(uid, f"✅ <b>{html.escape(number)}</b>", reply_markup=types.ReplyKeyboardRemove())
    lines = ["<b>Шаг 5/5 — Класс авто:</b>"]
    for k in COMFORT_H + ["minivan"]:
        yr = f" ({TARIFFS_RF[k]['year']})" if TARIFFS_RF[k].get('year') else ""
        lines.append(f"{TARIFFS_RF[k]['label']}{yr} — <i>{CLASS_DESC.get(k,'')}</i>")
    safe_send(uid, "\n".join(lines), reply_markup=kb_pclass())

@bot.callback_query_handler(func=lambda c: c.data.startswith("pclass_"))
def pclass_cb(call):
    uid = call.from_user.id
    s   = DB.session(uid)
    if s.get("step") != "profile_car_class":
        bot.answer_callback_query(call.id, "❌ Неверный шаг"); return
    cc = call.data.split("_", 1)[1]
    if err := check_brand(s["data"].get("car_model",""), cc):
        bot.answer_callback_query(call.id, err, show_alert=True); return
    s["data"]["car_class"] = cc
    s["data"]["car_class_label"] = TARIFFS_RF[cc]['label']
    DB.session_upd(uid, data=s["data"])
    safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    _finalize_profile(uid)
    bot.answer_callback_query(call.id)

def _finalize_profile(uid):
    s    = DB.session(uid)
    data = s["data"]
    data.setdefault("first_name", data.get("name",""))
    data.setdefault("last_name", "")
    data["user_id"] = uid
    existing = DB.driver(uid)
    data["docs_verified"] = existing.get("docs_verified", False) if existing else False
    data["registered_at"] = existing.get("registered_at") if existing else now_iso()
    DB.driver_save(uid, data)
    DB.session_clr(uid)
    pl = profile_link(data)
    ph = f'<a href="{pl}">Открыть</a>' if pl.startswith("http") else pl
    safe_send(uid,
        f"🎉 <b>Регистрация завершена!</b>\n\n"
        f"👤 {drv_name(data)}\n📞 {html.escape(data.get('phone','—'))}\n"
        f"🚘 {html.escape(data.get('car_model',''))} ({data.get('car_year','')})\n"
        f"🔢 {html.escape(data.get('car_number','—'))}\n"
        f"🏷 {html.escape(data.get('car_class_label','—'))}\n💬 {ph}\n\n"
        f"⏳ <b>Ожидайте верификации профиля администратором.</b>",
        reply_markup=kb_driver(uid))
    if not DB.sub_info(uid)[2]:
        safe_send(uid, "💳 <b>Пока ждёте верификации — оформите абонемент:</b>", reply_markup=kb_subs())
    for aid in ADMIN_IDS:
        m = types.InlineKeyboardMarkup(row_width=2)
        m.add(types.InlineKeyboardButton("✅ Верифицировать", callback_data=f"doc_ok_{uid}"),
              types.InlineKeyboardButton("❌ Отказать",       callback_data=f"doc_rej_{uid}"))
        safe_send(aid,
            f"📋 <b>Новый водитель — нужна верификация</b>\n\n"
            f"👤 {drv_name(data)}\n📞 {html.escape(data.get('phone','—'))}\n"
            f"🚘 {html.escape(data.get('car_model','—'))} ({data.get('car_year','—')})\n"
            f"🔢 {html.escape(data.get('car_number','—'))}\n"
            f"🏷 {html.escape(data.get('car_class_label','—'))}\n"
            f"💬 {pl}\nID: <code>{uid}</code>",
            reply_markup=m)


# ══════════════ АБОНЕМЕНТ ══════════════
@bot.message_handler(func=lambda m: m.text == "💳 Абонемент")
def subscription_menu(msg):
    uid = msg.chat.id
    if not DB.driver(uid): safe_send(uid, "❌ Заполните профиль."); return
    exp, dl, active = DB.sub_info(uid)
    txt = (f"✅ <b>Абонемент активен</b>\nДо: <b>{exp}</b>\nОсталось: <b>{dl} дн.</b>\n\nПродлить?"
           if active else "💳 <b>Абонемент</b>\n\n❌ Нет подписки\nВыберите тариф:")
    safe_send(uid, txt, reply_markup=kb_subs())


# ══════════════ CALLBACK-ОБРАБОТЧИКИ ══════════════
def _cb_take(call, uid):
    oid = int(call.data.split("_")[1])
    drv = DB.driver(uid)
    if not drv or not drv.get("docs_verified"):
        bot.answer_callback_query(call.id, "❌ Профиль не верифицирован", show_alert=True); return
    _, _, active = DB.sub_info(uid)
    if not active:
        bot.answer_callback_query(call.id, "❌ Нет абонемента", show_alert=True); return
    od = DB.order(oid)
    if not od:
        bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
    if od["passenger_id"] == uid:
        bot.answer_callback_query(call.id, "❌ Нельзя взять свой заказ", show_alert=True); return
    ct, rsn = can_take_order(drv, od)
    if not ct:
        bot.answer_callback_query(call.id, rsn, show_alert=True); return
    if not DB.order_take_atomic(oid, uid):
        bot.answer_callback_query(call.id, "⚠️ Заказ уже недоступен")
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None); return
    update_channel_post(oid)
    od  = DB.order(oid)
    pid = od["passenger_id"]
    try:
        pc = bot.get_chat(pid)
        pn, pu = html.escape(pc.first_name or "Пассажир"), pc.username
    except:
        pn, pu = "Пассажир", None
    purl     = f"https://t.me/{pu}" if pu else f"tg://user?id={pid}"
    avg, cnt = DB.avg_rating(uid)
    r_text   = f"\n⭐ Рейтинг: <b>{avg}/5</b> ({cnt} оценок)" if cnt else "\n⭐ Новый водитель"
    safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(types.InlineKeyboardButton("💬 Написать пассажиру", url=purl),
          types.InlineKeyboardButton("❌ Отказаться", callback_data=f"driver_cancel_{oid}"))
    safe_send(uid,
        f"✅ <b>Заказ #{oid} принят!</b>\n👤 <a href='{purl}'>{pn}</a>\n"
        f"📍 {html.escape(od.get('from_city'))} → {html.escape(od.get('to_city'))}\n"
        f"📅 {od.get('trip_date')} 🕐 {od.get('trip_time')}", reply_markup=m)
    du   = (drv.get("username","") or "").lstrip("@")
    durl = f"https://t.me/{du}" if du and not du.startswith("id") else f"tg://user?id={uid}"
    mp   = types.InlineKeyboardMarkup(row_width=1)
    mp.add(types.InlineKeyboardButton("📞 Написать водителю", url=durl),
           types.InlineKeyboardButton("✅ Завершить поездку", callback_data=f"done_{oid}"),
           types.InlineKeyboardButton("❌ Отменить",          callback_data=f"cancel_{oid}"))
    car_note = (f"\n⚠️ <i>Водитель приедет на {html.escape(drv.get('car_class_label',drv.get('car_class','')))}</i>"
                if drv.get("car_class") != od.get("car_class") else "")
    safe_send(pid,
        f"🎉 <b>Водитель найден!</b>\n"
        f"👤 {drv_name(drv)}{r_text}\n"
        f"🚘 {html.escape(drv.get('car_model','—'))} ({drv.get('car_year','—')})\n"
        f"🔢 {html.escape(drv.get('car_number','—'))}\n"
        f"📞 {html.escape(drv.get('phone','—'))}{car_note}\n\n"
        f"⏳ <i>Ожидайте — с вами свяжется водитель.</i>\n\n"
        f"🚫 <b>Не переводите предоплату!</b>\n"
        f"Оплата — только водителю после поездки.\n"
        f"<i>Если водитель просит предоплату — сообщите @Olegan7979</i>",
        reply_markup=mp)
    bot.answer_callback_query(call.id, "✅ Заказ принят!")

def _cb_cancel(call, uid):
    oid = int(call.data.split("_")[1])
    if DB.order_cancel_atomic(oid, uid, "passenger"):
        _clear_notified(oid)
        update_channel_post(oid)
        order = DB.order(oid)
        if order and order.get("driver_id"):
            safe_send(order["driver_id"], f"❌ Пассажир отменил заказ #{oid}")
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, f"✅ Заказ #{oid} отменён.", reply_markup=kb_main())
        bot.answer_callback_query(call.id, "✅ Отменено")
    else:
        bot.answer_callback_query(call.id, "❌ Заказ уже нельзя отменить", show_alert=True)

def _cb_driver_cancel(call, uid):
    parts = call.data.split("_")
    if len(parts) < 3: bot.answer_callback_query(call.id, "❌"); return
    oid = int(parts[2])
    if DB.order_cancel_atomic(oid, uid, "driver"):
        update_channel_post(oid)
        order = DB.order(oid)
        if order and order.get("passenger_id"):
            safe_send(order["passenger_id"], f"❌ Водитель отказался от заказа #{oid}")
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, f"✅ Вы отказались от заказа #{oid}")
        _notify_drivers(oid, exclude_uid=uid)
        bot.answer_callback_query(call.id, "✅")
    else:
        bot.answer_callback_query(call.id, "❌ Заказ уже нельзя отменить", show_alert=True)

def _cb_done(call, uid):
    oid = int(call.data.split("_")[1])
    o   = DB.order(oid)
    if not o: bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
    if o["passenger_id"] != uid:
        bot.answer_callback_query(call.id, "❌ Не ваш заказ", show_alert=True); return
    if o["status"] != "taken":
        bot.answer_callback_query(call.id, "❌ Заказ не в работе", show_alert=True); return
    DB.order_upd(oid, status="completed", completed_at=now_iso())
    _clear_notified(oid)
    safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    safe_send(uid, "✅ <b>Поездка завершена!</b>\n⭐ Пожалуйста, оцените водителя:",
              reply_markup=kb_stars(oid))
    if o.get("driver_id"):
        safe_send(o["driver_id"], f"✅ Поездка #{oid} завершена пассажиром!")
    bot.answer_callback_query(call.id, "✅ Завершено")

def _cb_sub(call, uid):
    if not DB.driver(uid):
        bot.answer_callback_query(call.id, "❌ Заполните профиль", show_alert=True); return
    pk   = call.data.split("_")[1]
    plan = SUBS.get(pk)
    if not plan: bot.answer_callback_query(call.id, "❌"); return
    DB.pending_set(uid, pk)
    safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    m = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("📨 Я оплатил", callback_data=f"paid_{pk}")
    )
    safe_send(uid, f"💳 <b>{plan['label']}</b>\nСумма: <b>{plan['price']} ₽</b>\n\n{PAYMENT_DETAILS}",
              reply_markup=m)
    drv = DB.driver(uid)
    for aid in ADMIN_IDS:
        safe_send(aid, f"💳 <b>Запрос на абонемент</b>\n👤 {drv_name(drv)}\n"
                  f"🚘 {html.escape(drv.get('car_model','—'))}\nТариф: {plan['label']}\nID: <code>{uid}</code>")
    bot.answer_callback_query(call.id)


# ══════════════ ГЛАВНЫЙ CALLBACK-РОУТЕР ══════════════
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid, data = call.from_user.id, call.data

    # ── Выбор пассажиров ──
    if data.startswith("pax_"):
        s = DB.session(uid)
        if s.get("step") != "passengers": bot.answer_callback_query(call.id, "❌"); return
        s["data"]["passengers"] = int(data.split("_")[1])
        DB.session_upd(uid, data=s["data"])
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, f"✅ Пассажиров: <b>{s['data']['passengers']}</b>")
        show_car_class(uid)
        bot.answer_callback_query(call.id); return

    # ── Выбор класса авто (заказ) ──
    if data.startswith("car_"):
        s = DB.session(uid)
        if s.get("step") != "car_class": bot.answer_callback_query(call.id, "❌"); return
        cc  = data.split("_", 1)[1]
        t   = TARIFFS_NT if s["data"].get("region") == "new" else TARIFFS_RF
        s["data"]["car_class"]       = cc
        s["data"]["car_class_label"] = t[cc]['label']
        DB.session_upd(uid, data=s["data"])
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, f"✅ Класс: <b>{t[cc]['label']}</b>")
        ask_wishes(uid)
        bot.answer_callback_query(call.id); return

    if data == "cancel_order":
        DB.session_clr(uid)
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, "❌ Отменено.", reply_markup=kb_driver(uid) if DB.driver(uid) else kb_main())
        bot.answer_callback_query(call.id); return

    if data == "cant_take":
        bot.answer_callback_query(call.id, "❌ Недоступен", show_alert=True); return

    if data.startswith("skip_"):
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "Пропущено"); return

    # ── Оценка водителя ──
    if data.startswith("rate_"):
        parts = data.split("_")
        if len(parts) != 3: bot.answer_callback_query(call.id, "❌"); return
        oid, stars = int(parts[1]), int(parts[2])
        order = DB.order(oid)
        if not order: bot.answer_callback_query(call.id, "❌ Заказ не найден"); return
        if order["passenger_id"] != uid:
            bot.answer_callback_query(call.id, "❌ Не ваш заказ", show_alert=True); return
        if order["status"] != "completed":
            bot.answer_callback_query(call.id, "❌ Заказ не завершён"); return
        drv_id = order.get("driver_id")
        if not drv_id: bot.answer_callback_query(call.id, "❌ Нет водителя"); return
        if DB.has_rating(oid, uid):
            bot.answer_callback_query(call.id, "Вы уже оценили эту поездку")
            safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None); return
        DB.add_rating(oid, drv_id, uid, stars)
        avg, cnt = DB.avg_rating(drv_id)
        try:
            bot.edit_message_text(f"✅ <b>Поездка #{oid} завершена!</b>\n⭐ Ваша оценка: <b>{stars}</b>\nСпасибо!",
                                  call.message.chat.id, call.message.message_id, parse_mode="HTML")
        except:
            safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            safe_send(uid, f"⭐ Оценка {stars} сохранена!")
        safe_send(drv_id, f"⭐ <b>Новая оценка за поездку #{oid}!</b>\n"
                  f"Пассажир поставил: <b>{stars}⭐</b>\nВаш средний рейтинг: <b>{avg}/5</b> ({cnt} оценок)")
        bot.answer_callback_query(call.id, "✅ Спасибо за оценку!"); return

    # ── Подтверждение оплаты ──
    if data.startswith("paid_"):
        pk = data.split("_")[1]
        plan = SUBS.get(pk, {})
        safe_send(uid, "⏳ Заявка отправлена. Активация в течение 1–2 ч.")
        drv = DB.driver(uid)
        for aid in ADMIN_IDS:
            safe_send(aid, f"🔔 <b>Водитель сообщил об оплате!</b>\n"
                      f"👤 {drv_name(drv or {})} (ID: {uid})\nТариф: {plan.get('label','—')}")
        bot.answer_callback_query(call.id, "✅"); return

    # ── Активация/отклонение подписки (админ) ──
    if data.startswith("conf_sub_"):
        if uid not in ADMIN_IDS: bot.answer_callback_query(call.id, "❌"); return
        _, _, tgt, pk = data.split("_", 3)
        tgt  = int(tgt)
        plan = SUBS.get(pk)
        if not plan: bot.answer_callback_query(call.id, "❌"); return
        exp_str, _, _ = DB.sub_info(tgt)
        base    = (max(datetime.strptime(exp_str,"%Y-%m-%d").date(), now_dt().date())
                   if exp_str else now_dt().date())
        new_exp = (base + timedelta(days=plan["days"])).strftime("%Y-%m-%d")
        DB.sub_set(tgt, new_exp, uid, pk)
        DB.pending_del(tgt, uid, pk)
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, f"✅ Подписка активирована до {new_exp}")
        safe_send(tgt, f"🎉 <b>Абонемент активирован!</b>\n{plan['label']}\n"
                  f"До: {datetime.strptime(new_exp,'%Y-%m-%d').strftime('%d.%m.%Y')}",
                  reply_markup=kb_driver(tgt))
        bot.answer_callback_query(call.id, "✅"); return

    if data.startswith("rej_sub_"):
        if uid not in ADMIN_IDS: bot.answer_callback_query(call.id, "❌"); return
        tgt = int(data.split("_")[2])
        DB.pending_del(tgt, uid, DB.pending_get(tgt))
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(tgt, "❌ Запрос на абонемент отклонён.")
        bot.answer_callback_query(call.id, "❌"); return

    # ── Верификация профиля (админ) ──
    if data.startswith("doc_ok_") or data.startswith("doc_rej_"):
        if uid not in ADMIN_IDS: bot.answer_callback_query(call.id, "❌"); return
        ok  = data.startswith("doc_ok_")
        tgt = int(data.split("_")[2])
        DB.driver_verify(tgt, ok)
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        if ok:
            safe_send(uid, f"✅ Профиль водителя {tgt} верифицирован")
            safe_send(tgt, "✅ <b>Ваш профиль верифицирован!</b>\nТеперь вы можете принимать заказы.",
                      reply_markup=kb_driver(tgt))
        else:
            safe_send(uid, f"❌ Профиль {tgt} отклонён")
            safe_send(tgt, "❌ <b>Верификация отклонена.</b>\nОбратитесь к администратору @Olegan7979.")
        bot.answer_callback_query(call.id, "✅" if ok else "❌"); return

    if data == "back_driver":
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, "🚗 Меню водителя", reply_markup=kb_driver(uid))
        bot.answer_callback_query(call.id); return

    # ══ АДМИН-ПАНЕЛЬ ══
    if data == "adm_orders":
        if uid not in ADMIN_IDS: return
        orders = DB.all_orders(10)
        for o in orders: safe_send(uid, fmt_order(o))
        if not orders: safe_send(uid, "Нет заказов")
        bot.answer_callback_query(call.id); return

    if data == "adm_drivers":
        if uid not in ADMIN_IDS: return
        drivers = DB.all_drivers()
        if not drivers:
            safe_send(uid, "Нет водителей"); bot.answer_callback_query(call.id); return
        for d in drivers:
            exp_str, dl, active = DB.sub_info(d["user_id"])
            avg, cnt = DB.avg_rating(d["user_id"])
            info = (
                f"👤 <b>{drv_name(d)}</b>\n"
                f"├ {'✅ Верифицирован' if d.get('docs_verified') else '⏳ Ожидает'} | "
                f"Подписка: {'✅' if active else '❌'}\n"
                f"├ Рейтинг: {'⭐ '+str(avg)+' ('+str(cnt)+')' if cnt else '—'}\n"
                f"├ Авто: {html.escape(d.get('car_model','—'))} ({d.get('car_year','—')})\n"
                f"├ Номер: {html.escape(d.get('car_number','—'))}\n"
                f"├ Класс: {html.escape(d.get('car_class_label','—'))}\n"
                f"├ Тел: {html.escape(d.get('phone','—'))}\n"
                f"├ ID: <code>{d['user_id']}</code>\n"
                f"└ Рег.: {str(d.get('registered_at','—'))[:10]}"
            )
            kb = types.InlineKeyboardMarkup(row_width=2)
            un, pl_url = d.get('username',''), d.get('profile_link','')
            url = (f"https://t.me/{un.lstrip('@')}" if un and not un.startswith('tg://')
                   else pl_url if pl_url.startswith('https://') else None)
            if url: kb.add(types.InlineKeyboardButton("💬 Написать", url=url))
            kb.row(types.InlineKeyboardButton("✅ Верифицировать", callback_data=f"doc_ok_{d['user_id']}"),
                   types.InlineKeyboardButton("❌ Снять вериф.",   callback_data=f"doc_rej_{d['user_id']}"))
            kb.add(types.InlineKeyboardButton("✏️ Редактировать", callback_data=f"adm_edit_{d['user_id']}"))
            kb.add(types.InlineKeyboardButton("🗑 Удалить",        callback_data=f"adm_del_{d['user_id']}"))
            safe_send(uid, info, reply_markup=kb)
        bot.answer_callback_query(call.id); return

    # ── Редактирование водителя ──
    if data.startswith("adm_edit_"):
        if uid not in ADMIN_IDS: return
        tgt = int(data.split("_")[2])
        drv = DB.driver(tgt)
        if not drv: bot.answer_callback_query(call.id, "❌ Не найден"); return
        m = types.InlineKeyboardMarkup(row_width=1)
        m.add(types.InlineKeyboardButton("🚘 Марка/модель", callback_data=f"adm_ef_{tgt}_car_model"),
              types.InlineKeyboardButton("📅 Год выпуска",  callback_data=f"adm_ef_{tgt}_car_year"),
              types.InlineKeyboardButton("🔢 Гос. номер",   callback_data=f"adm_ef_{tgt}_car_number"),
              types.InlineKeyboardButton("🏷 Класс авто",   callback_data=f"adm_ef_{tgt}_car_class"))
        safe_send(uid, f"✏️ <b>Редактировать: {drv_name(drv)}</b>\nЧто изменить?", reply_markup=m)
        bot.answer_callback_query(call.id); return

    if data.startswith("adm_ef_"):
        if uid not in ADMIN_IDS: return
        parts = data.split("_"); tgt = int(parts[2]); field = "_".join(parts[3:])
        drv = DB.driver(tgt)
        if not drv: bot.answer_callback_query(call.id, "❌ Не найден"); return
        if field == "car_class":
            DB.session_upd(uid, step=f"adm_sc_{tgt}", data={"tgt": tgt})
            m = types.InlineKeyboardMarkup(row_width=1)
            for k in COMFORT_H + ["minivan"]:
                yr = f" ({TARIFFS_RF[k]['year']})" if TARIFFS_RF[k].get('year') else ""
                m.add(types.InlineKeyboardButton(f"{TARIFFS_RF[k]['label']}{yr}",
                      callback_data=f"adm_sc_{tgt}_{k}"))
            safe_send(uid, "🏷 Выберите новый класс:", reply_markup=m)
        else:
            prompts = {"car_model":"🚘 Введите новую марку и модель:",
                       "car_year": "📅 Введите новый год (от 2008):",
                       "car_number":"🔢 Введите новый гос. номер:"}
            DB.session_upd(uid, step=f"adm_ef_{tgt}_{field}", data={"tgt": tgt, "field": field})
            safe_send(uid, prompts.get(field,"Введите значение:"), reply_markup=kb_cancel())
        bot.answer_callback_query(call.id); return

    if data.startswith("adm_sc_"):
        if uid not in ADMIN_IDS: return
        parts = data.split("_"); tgt = int(parts[2]); cc = "_".join(parts[3:])
        drv = DB.driver(tgt)
        if not drv: bot.answer_callback_query(call.id, "❌ Не найден"); return
        if err := check_brand(drv.get("car_model",""), cc):
            bot.answer_callback_query(call.id, err, show_alert=True); return
        lbl = TARIFFS_RF.get(cc,{}).get("label", cc)
        DB.driver_update_fields(tgt, car_class=cc, car_class_label=lbl)
        DB.session_clr(uid)
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, f"✅ Класс обновлён: {lbl}")
        notify_driver_change(tgt, "класс авто", lbl)
        bot.answer_callback_query(call.id, "✅"); return

    if data.startswith("adm_del_"):
        if uid not in ADMIN_IDS: return
        tgt = int(data.split("_")[2])
        drv = DB.driver(tgt)
        if not drv: bot.answer_callback_query(call.id, "❌ Не найден"); return
        m = types.InlineKeyboardMarkup(row_width=2)
        m.add(types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"adm_delok_{tgt}"),
              types.InlineKeyboardButton("❌ Нет",         callback_data="adm_delno"))
        safe_send(uid, f"⚠️ Удалить <b>{drv_name(drv)}</b> (ID: {tgt})?", reply_markup=m)
        bot.answer_callback_query(call.id); return

    if data.startswith("adm_delok_"):
        if uid not in ADMIN_IDS: return
        tgt = int(data.split("_")[2])
        drv = DB.driver(tgt)
        if drv:
            DB.driver_del(tgt)
            safe_send(uid, f"✅ {drv_name(drv)} удалён")
            safe_send(tgt, "🗑 Ваш профиль водителя удалён администратором.")
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.answer_callback_query(call.id, "✅ Удалён"); return

    if data == "adm_delno":
        if uid not in ADMIN_IDS: return
        safe_edit_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_send(uid, "❌ Удаление отменено")
        bot.answer_callback_query(call.id); return

    if data == "adm_bl":
        if uid not in ADMIN_IDS: return
        bl = DB.bl_all()
        safe_send(uid, "⛔ <b>Чёрный список:</b>\n" + "\n".join(str(u) for u in bl)
                  if bl else "✅ Чёрный список пуст")
        bot.answer_callback_query(call.id); return

    if data == "adm_subs":
        if uid not in ADMIN_IDS: return
        pending = DB.pending_all()
        if not pending: safe_send(uid, "✅ Нет ожидающих подтверждения")
        for p in pending:
            plan, drv = SUBS.get(p["plan_key"],{}), DB.driver(p["user_id"]) or {}
            m = types.InlineKeyboardMarkup(row_width=2)
            m.add(types.InlineKeyboardButton("✅ Активировать",
                                             callback_data=f"conf_sub_{p['user_id']}_{p['plan_key']}"),
                  types.InlineKeyboardButton("❌ Отклонить",
                                             callback_data=f"rej_sub_{p['user_id']}"))
            safe_send(uid, f"💳 <b>{drv_name(drv)}</b>\n💬 {profile_link(drv)}\n"
                      f"ID: <code>{p['user_id']}</code>\nТариф: {plan.get('label','—')}", reply_markup=m)
        bot.answer_callback_query(call.id); return

    if data == "adm_stats":
        if uid not in ADMIN_IDS: return
        s = DB.stats()
        safe_send(uid,
            f"📊 <b>Статистика</b>\n\nЗаказов всего: {s['total']}\nОткрыто: {s['open']}\n"
            f"Завершено: {s['done']}\n\nВодителей: {s['drivers']}\n"
            f"Верифицировано: {s['docs_ok']}\nС подпиской: {s['subscribed']}")
        bot.answer_callback_query(call.id); return

    if data == "adm_help":
        if uid not in ADMIN_IDS: return
        safe_send(uid,
            "📖 <b>Команды администратора:</b>\n\n"
            "/ban ID — заблокировать\n/unban ID — разблокировать\n"
            "/unsub ID — аннулировать подписку\n/deldriver ID — удалить водителя\n"
            "/recalc order_id distance_km price — пересчитать заказ")
        bot.answer_callback_query(call.id); return

    # ── Именованные prefix-обработчики ──
    for prefix, fn in [("take_",_cb_take),("cancel_",_cb_cancel),
                        ("driver_cancel_",_cb_driver_cancel),("done_",_cb_done),("sub_",_cb_sub)]:
        if data.startswith(prefix):
            fn(call, uid); return

    bot.answer_callback_query(call.id, "⚠️ Неизвестная команда")


# ══════════════ ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ ══════════════
STEP_HANDLERS = {
    "from_city":          step_from,
    "to_city":            step_to,
    "trip_date":          step_date,
    "trip_time":          step_time,
    "wishes":             step_wishes,
    "profile_car_model":  p_model,
    "profile_car_year":   p_year,
    "profile_car_number": p_number,
}

@bot.message_handler(func=lambda m: True)
def universal_text(msg):
    uid  = msg.chat.id
    text = (msg.text or "").strip()

    if text == "❌ Отменить":
        order_cancel(msg); return

    step = DB.session(uid).get("step")

    # ── Ввод для редактирования полей водителя (только для админа) ──
    if step and step.startswith("adm_ef_") and uid in ADMIN_IDS:
        parts = step.split("_"); tgt = int(parts[2]); field = "_".join(parts[3:])
        drv = DB.driver(tgt)
        if not drv:
            safe_send(uid, "❌ Водитель не найден"); DB.session_clr(uid); return

        if field == "car_model":
            if not (2 <= len(text) <= 100):
                safe_send(uid, "❌ Введите марку и модель (2–100 символов)"); return
            DB.driver_update_fields(tgt, car_model=text)
            DB.session_clr(uid)
            safe_send(uid, f"✅ Марка/модель обновлены: {html.escape(text)}", reply_markup=kb_main())
            notify_driver_change(tgt, "марку/модель авто", text)

        elif field == "car_year":
            try:
                year = int(text)
                if not (2008 <= year <= now_dt().year + 1): raise ValueError
                DB.driver_update_fields(tgt, car_year=year)
                DB.session_clr(uid)
                safe_send(uid, f"✅ Год выпуска обновлён: {year}", reply_markup=kb_main())
                notify_driver_change(tgt, "год выпуска авто", year)
            except: safe_send(uid, "❌ Некорректный год (минимум 2008)")

        elif field == "car_number":
            number = text.upper().replace(" ","")
            if not number: safe_send(uid, "❌ Введите номер"); return
            DB.driver_update_fields(tgt, car_number=number)
            DB.session_clr(uid)
            safe_send(uid, f"✅ Гос. номер обновлён: {html.escape(number)}", reply_markup=kb_main())
            notify_driver_change(tgt, "гос. номер", number)
        return

    if step in STEP_HANDLERS:
        STEP_HANDLERS[step](msg)
    elif step:
        safe_send(uid, "⚠️ Введите данные или нажмите «❌ Отменить»")
    else:
        safe_send(uid, "👇 Используйте кнопки меню", reply_markup=kb_main())


# ══════════════ ЗАПУСК ══════════════
if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  🚕 МЕЖГОРОД ТРАНСФЕР v13.1 (SQLite)")
    log.info("=" * 50)
    DB.init()
    errors = 0
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
            errors = 0
        except KeyboardInterrupt:
            log.info("\n🛑 Остановлено"); break
        except (telebot.apihelper.ApiException, requests.exceptions.ConnectionError) as e:
            errors += 1
            wait = min(errors * 5, 60)
            log.error(f"❌ Ошибка #{errors}: {e}. Жду {wait}с...")
            time.sleep(wait)
        except Exception as e:
            log.exception(e); time.sleep(10)
