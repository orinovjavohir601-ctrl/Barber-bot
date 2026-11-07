"""
Sartaroshlar: Telegram bot + Mini App (ko'p sartarosh, vaqt band qilish)

Bitta fayl: bot, HTTP server, API va Mini App sahifasi.

Render'da kerakli o'zgaruvchilar (Environment):
  BOT_TOKEN     BotFather tokeni (raqamlar:harflar)
  DATABASE_URL  Postgres manzili (Neon / Supabase). Busiz ma'lumotlar deployda yo'qoladi!
  ADMIN_IDS     Platforma egasining Telegram ID si (ixtiyoriy; botga /myid yozib bilasiz).
                Ega sartaroshlarni yashira oladi va yangi sartarosh haqida xabar oladi.
Ixtiyoriy:
  PUBLIC_URL    Servis manzili (Render o'zi beradi: RENDER_EXTERNAL_URL)
  TZ_NAME       Vaqt mintaqasi (standart: Asia/Tashkent)
"""
import os
import re
import sys
import json
import gzip
import hmac
import html
import time
import base64
import hashlib
import asyncio
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from telegram import (
    Update, BotCommand, MenuButtonWebApp, WebAppInfo,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
for _n in ("httpx", "httpcore"):  # URL (ichida token) logga tushmasin
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("barber")

# ---------------- Sozlamalar ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip("\"'")
if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    sys.exit("BOT_TOKEN noto'g'ri. U 'raqamlar:harflar' ko'rinishida (':' bilan) bo'lishi kerak.")

ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tashkent"))
PORT = int(os.environ.get("PORT", "10000"))
APP_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
MAX_ACTIVE_PER_USER = 3      # bitta mijozda bir vaqtda nechta faol bron
MIN_NOTICE_MIN = 30          # hozirdan necha daqiqadan keyingi vaqtni band qilish mumkin
INIT_DATA_MAX_AGE = 2 * 86400
SOM = "so'm"
BOT_USERNAME = ""
NEW_BARBER = "🆕 Yangi sartarosh ro'yxatdan o'tdi"

WD = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
MON = ["", "yanvar", "fevral", "mart", "aprel", "may", "iyun",
       "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]

# Xizmatlar katalogi: kalit -> (uz, ru, en, narx, davomiylik). Sartarosh shulardan tanlaydi.
CATALOG = {
    "haircut":   ("Soch olish", "Стрижка", "Haircut", 50000, 30),
    "kids":      ("Bolalar soch olish", "Детская стрижка", "Kids haircut", 40000, 25),
    "styling":   ("Soch ukladka", "Укладка", "Styling", 40000, 30),
    "perm":      ("Biozavivka", "Биозавивка", "Perm", 250000, 90),
    "coloring":  ("Soch bo'yash", "Окрашивание волос", "Hair coloring", 150000, 90),
    "beard":     ("Soqol olish", "Бритьё бороды", "Beard shave", 30000, 20),
    "beardtrim": ("Soqol shakllantirish", "Моделирование бороды", "Beard trim", 35000, 25),
    "combo":     ("Soch + soqol (kombo)", "Комбо: стрижка + борода", "Combo: haircut + beard", 70000, 50),
}
_LI = {"uz": 0, "ru": 1, "en": 2}


def cat_name(kind, lang):
    return CATALOG[kind][_LI.get(lang, 0)]


# ---------------- Tillar (uz / ru / en) ----------------
LANGS = ("uz", "ru", "en")
WD_L = {
    "uz": WD,
    "ru": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
MON_L = {
    "uz": MON,
    "ru": ["", "января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря"],
    "en": ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
}
# Kalit: o'zbekcha matn. Topilmasa o'zbekcha qoladi.
_RU = {
    "so'm": "сум",
    "Bron tasdiqlandi": "Запись подтверждена",
    "Vaqtidan oldin eslatamiz. Bekor qilish: «Mening bronlarim» bo'limida.":
        "Мы напомним перед визитом. Отменить запись можно в разделе «Мои записи».",
    "Yangi bron": "Новая запись",
    "Mijoz bronni bekor qildi": "Клиент отменил запись",
    "Bron bekor qilindi": "Запись отменена",
    "Broningiz bekor qilindi (sartarosh tomonidan)": "Ваша запись отменена (барбером)",
    "Savollar bo'yicha: {phone}": "По вопросам: {phone}",
    "Eslatma": "Напоминание",
    "{shop}da bron:": "Запись в «{shop}»:",
    "📋 Bronlarni ochish": "📋 Открыть записи",
    "🧾 Mening bronlarim": "🧾 Мои записи",
    "📅 Qayta band qilish": "📅 Записаться снова",
    "✂️ Ochish": "✂️ Открыть",
    "🆕 Yangi sartarosh ro'yxatdan o'tdi": "🆕 Новый барбер зарегистрировался",
    "Assalomu alaykum, {who}! 👋\n\nSartaroshni tanlang va vaqt band qiling yoki o'z sartaroshxonangizni ro'yxatdan o'tkazing.\nBoshlash uchun pastdagi tugmani bosing.":
        "Здравствуйте, {who}! 👋\n\nВыберите барбера и запишитесь на удобное время — или зарегистрируйте свой барбершоп.\nНажмите кнопку ниже, чтобы начать.",
    "Mini App manzili sozlanmagan (PUBLIC_URL).": "Адрес Mini App не настроен (PUBLIC_URL).",
    "Sizning Telegram ID: <code>{id}</code>": "Ваш Telegram ID: <code>{id}</code>",
    "Boshlash": "Начать",
    "Ilovani Telegram orqali qayta oching": "Откройте приложение заново через Telegram",
    "So'rov juda katta": "Слишком большой запрос",
    "So'rov noto'g'ri": "Некорректный запрос",
    "Topilmadi": "Не найдено",
    "Server xatosi, birozdan keyin qayta urinib ko'ring": "Ошибка сервера, попробуйте чуть позже",
    "Sana noto'g'ri": "Неверная дата",
    "Vaqt noto'g'ri (SS:DD)": "Неверное время (ЧЧ:ММ)",
    "Ruxsat yo'q": "Нет доступа",
    "Xizmat topilmadi": "Услуга не найдена",
    "Ismingizni kiriting": "Введите ваше имя",
    "Telefon raqamini to'g'ri kiriting": "Введите корректный номер телефона",
    "Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.": "Это время уже занято. Выберите другое.",
    "Sizda allaqachon {n} ta faol bron bor. Avval birortasini bekor qiling yoki vaqti kelishini kuting.":
        "У вас уже {n} активных записи. Отмените одну из них или дождитесь визита.",
    "Bron topilmadi": "Запись не найдена",
    "Bu bron allaqachon bekor qilingan": "Эта запись уже отменена",
    "O'tib ketgan bronni bekor qilib bo'lmaydi": "Нельзя отменить прошедшую запись",
    "Xizmat nomini kiriting": "Введите название услуги",
    "Narx noto'g'ri": "Неверная цена",
    "Davomiylik 5 dan 600 daqiqagacha bo'lsin": "Длительность — от 5 до 600 минут",
    "Ish vaqti noto'g'ri": "Неверные часы работы",
    "{wd}: tugash vaqti boshlanishidan keyin bo'lsin": "{wd}: конец должен быть позже начала",
    "{wd}: tanaffus ish vaqti ichida bo'lsin": "{wd}: перерыв должен быть в рабочее время",
    "Telefon noto'g'ri": "Неверный телефон",
    "Vaqt qadami noto'g'ri": "Неверный шаг времени",
    "Eslatma vaqti noto'g'ri": "Неверное время напоминания",
    "Necha kun oldin noto'g'ri": "Неверный срок записи вперёд",
    "Rasm noto'g'ri yoki juda katta": "Фото неверное или слишком большое",
    "Sartarosh topilmadi": "Барбер не найден",
    "Siz allaqachon sartarosh sifatida ro'yxatdan o'tgansiz": "Вы уже зарегистрированы как барбер",
    "Avval sartarosh sifatida ro'yxatdan o'ting": "Сначала зарегистрируйтесь как барбер",
    "Salon nomini kiriting": "Введите название салона",
    "Manzilni kiriting": "Введите адрес",
    "Kamida bitta xizmat tanlang": "Выберите хотя бы одну услугу",
    "Tajriba noto'g'ri": "Неверный стаж",
    "Bu sartarosh hozir qabul qilmayapti": "Этот барбер сейчас не принимает записи",
}
_EN = {
    "so'm": "UZS",
    "Bron tasdiqlandi": "Booking confirmed",
    "Vaqtidan oldin eslatamiz. Bekor qilish: «Mening bronlarim» bo'limida.":
        "We'll remind you before your visit. You can cancel in \"My bookings\".",
    "Yangi bron": "New booking",
    "Mijoz bronni bekor qildi": "Client cancelled a booking",
    "Bron bekor qilindi": "Booking cancelled",
    "Broningiz bekor qilindi (sartarosh tomonidan)": "Your booking was cancelled (by the barber)",
    "Savollar bo'yicha: {phone}": "Questions: {phone}",
    "Eslatma": "Reminder",
    "{shop}da bron:": "Booking at {shop}:",
    "📋 Bronlarni ochish": "📋 Open bookings",
    "🧾 Mening bronlarim": "🧾 My bookings",
    "📅 Qayta band qilish": "📅 Book again",
    "✂️ Ochish": "✂️ Open",
    "🆕 Yangi sartarosh ro'yxatdan o'tdi": "🆕 A new barber registered",
    "Assalomu alaykum, {who}! 👋\n\nSartaroshni tanlang va vaqt band qiling yoki o'z sartaroshxonangizni ro'yxatdan o'tkazing.\nBoshlash uchun pastdagi tugmani bosing.":
        "Hello, {who}! 👋\n\nPick a barber and book a time, or register your own barbershop.\nTap the button below to start.",
    "Mini App manzili sozlanmagan (PUBLIC_URL).": "Mini App address is not set (PUBLIC_URL).",
    "Sizning Telegram ID: <code>{id}</code>": "Your Telegram ID: <code>{id}</code>",
    "Boshlash": "Start",
    "Ilovani Telegram orqali qayta oching": "Please reopen the app from Telegram",
    "So'rov juda katta": "Request is too large",
    "So'rov noto'g'ri": "Invalid request",
    "Topilmadi": "Not found",
    "Server xatosi, birozdan keyin qayta urinib ko'ring": "Server error, please try again shortly",
    "Sana noto'g'ri": "Invalid date",
    "Vaqt noto'g'ri (SS:DD)": "Invalid time (HH:MM)",
    "Ruxsat yo'q": "Access denied",
    "Xizmat topilmadi": "Service not found",
    "Ismingizni kiriting": "Enter your name",
    "Telefon raqamini to'g'ri kiriting": "Enter a valid phone number",
    "Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.": "This time was just taken. Please pick another.",
    "Sizda allaqachon {n} ta faol bron bor. Avval birortasini bekor qiling yoki vaqti kelishini kuting.":
        "You already have {n} active bookings. Cancel one or wait until a visit passes.",
    "Bron topilmadi": "Booking not found",
    "Bu bron allaqachon bekor qilingan": "This booking is already cancelled",
    "O'tib ketgan bronni bekor qilib bo'lmaydi": "A past booking can't be cancelled",
    "Xizmat nomini kiriting": "Enter the service name",
    "Narx noto'g'ri": "Invalid price",
    "Davomiylik 5 dan 600 daqiqagacha bo'lsin": "Duration must be 5 to 600 minutes",
    "Ish vaqti noto'g'ri": "Invalid working hours",
    "{wd}: tugash vaqti boshlanishidan keyin bo'lsin": "{wd}: closing time must be after opening",
    "{wd}: tanaffus ish vaqti ichida bo'lsin": "{wd}: the break must be within working hours",
    "Telefon noto'g'ri": "Invalid phone",
    "Vaqt qadami noto'g'ri": "Invalid time step",
    "Eslatma vaqti noto'g'ri": "Invalid reminder time",
    "Necha kun oldin noto'g'ri": "Invalid booking horizon",
    "Rasm noto'g'ri yoki juda katta": "The photo is invalid or too large",
    "Sartarosh topilmadi": "Barber not found",
    "Siz allaqachon sartarosh sifatida ro'yxatdan o'tgansiz": "You're already registered as a barber",
    "Avval sartarosh sifatida ro'yxatdan o'ting": "Please register as a barber first",
    "Salon nomini kiriting": "Enter the shop name",
    "Manzilni kiriting": "Enter the address",
    "Kamida bitta xizmat tanlang": "Pick at least one service",
    "Tajriba noto'g'ri": "Invalid experience",
    "Bu sartarosh hozir qabul qilmayapti": "This barber isn't taking bookings right now",
}
TR = {"ru": _RU, "en": _EN}


def tr(lang, key, **kw):
    out = TR.get(lang, {}).get(key, key)
    return out.format(**kw) if kw else out


def lang_from_code(code):
    c = (code or "").lower()
    if c.startswith("ru"):
        return "ru"
    if c.startswith("en"):
        return "en"
    return "uz"


# ---------------- Baza ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///barber.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=240, pool_size=5, max_overflow=5) \
    if not DATABASE_URL.startswith("sqlite") else create_engine(DATABASE_URL)
BOOK_LOCK = threading.Lock()

# ---- kichik kesh: bazaga murojaatni kamaytiradi (har yozuvda tozalanadi) ----
_CACHE = {}


def cached(key, ttl, fn):
    e = _CACHE.get(key)
    now = time.time()
    if e and e[0] > now:
        return e[1]
    v = fn()
    _CACHE[key] = (now + ttl, v)
    return v


def invalidate():
    _CACHE.clear()


def q_all(sql, params=None):
    with engine.connect() as c:
        return c.execute(text(sql), params or {}).fetchall()


def q_one(sql, params=None):
    with engine.connect() as c:
        return c.execute(text(sql), params or {}).fetchone()


def q_scalar(sql, params=None):
    with engine.connect() as c:
        return c.execute(text(sql), params or {}).scalar()


def now_local():
    return dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def to_dt(v):
    return v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))


def _create_tables():
    if DATABASE_URL.startswith("sqlite") and os.getenv("RENDER"):
        log.warning("DIQQAT: SQLite Render'da har deployda o'chadi. DATABASE_URL (Postgres) kiriting!")
    ident = "SERIAL PRIMARY KEY" if "postgresql" in DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS app_users (user_id BIGINT PRIMARY KEY, lang VARCHAR(2), "
                       "role VARCHAR(10), name VARCHAR(100), phone VARCHAR(30))"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS barbers (id {ident}, user_id BIGINT NOT NULL UNIQUE, "
            "name VARCHAR(80) NOT NULL, shop VARCHAR(80) NOT NULL, bio TEXT, exp INTEGER NOT NULL DEFAULT 0, "
            "address VARCHAR(200), phone VARCHAR(30), active INTEGER NOT NULL DEFAULT 1, "
            "step INTEGER NOT NULL DEFAULT 30, remind_hours INTEGER NOT NULL DEFAULT 2, "
            "horizon INTEGER NOT NULL DEFAULT 30, photo TEXT, thumb TEXT, photo_v VARCHAR(20) NOT NULL DEFAULT '0', "
            "created_at TIMESTAMP NOT NULL)"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS b_services (id {ident}, barber_id INTEGER NOT NULL, kind VARCHAR(20), "
            "name VARCHAR(100), price BIGINT NOT NULL, duration INTEGER NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0)"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS b_hours (barber_id INTEGER NOT NULL, weekday INTEGER NOT NULL, "
            "open_t VARCHAR(5), close_t VARCHAR(5), is_off INTEGER NOT NULL DEFAULT 0, "
            "br_start VARCHAR(5), br_end VARCHAR(5), PRIMARY KEY (barber_id, weekday))"))
        c.execute(text("CREATE TABLE IF NOT EXISTS b_daysoff (barber_id INTEGER NOT NULL, d VARCHAR(10) NOT NULL, "
                       "PRIMARY KEY (barber_id, d))"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS b_bookings (id {ident}, barber_id INTEGER NOT NULL, "
            "user_id BIGINT NOT NULL, tg_name VARCHAR(100), name VARCHAR(100) NOT NULL, "
            "phone VARCHAR(30) NOT NULL, service_id INTEGER, service_name VARCHAR(100) NOT NULL, "
            "service_kind VARCHAR(20), price BIGINT NOT NULL, duration INTEGER NOT NULL, "
            "start_at TIMESTAMP NOT NULL, end_at TIMESTAMP NOT NULL, "
            "status VARCHAR(12) NOT NULL DEFAULT 'active', cancelled_by VARCHAR(10), "
            "created_at TIMESTAMP NOT NULL, reminded INTEGER NOT NULL DEFAULT 0)"))



def init_db():
    _create_tables()
    _migrate()
    checks = {"barbers": B_COLS, "b_services": "id, barber_id, kind, name, price, duration, active, sort_order",
              "b_hours": "barber_id, weekday, open_t, close_t, is_off, br_start, br_end",
              "b_daysoff": "barber_id, d", "b_bookings": BK_COLS}
    rebuilt = False
    for table, cols in checks.items():
        try:
            with engine.begin() as c:
                c.execute(text(f"SELECT {cols} FROM {table} LIMIT 1"))
        except Exception as e:
            log.error("%s jadvali mos emas (%s). Eski nusxasi %s_old nomiga o'tkazilib, qayta yaratiladi.",
                      table, str(e).splitlines()[0][:200], table)
            with engine.begin() as c:
                c.execute(text(f"DROP TABLE IF EXISTS {table}_old"))
                c.execute(text(f"ALTER TABLE {table} RENAME TO {table}_old"))
            rebuilt = True
    if rebuilt:
        _create_tables()
        _migrate()
    for sql in ("CREATE INDEX IF NOT EXISTS ix_bs_barber ON b_services (barber_id)",
                "CREATE INDEX IF NOT EXISTS ix_bb_barber ON b_bookings (barber_id, start_at)",
                "CREATE INDEX IF NOT EXISTS ix_bb_user ON b_bookings (user_id, start_at)",
                # bir sartaroshda bir xil boshlanish vaqtiga ikkita faol bron bo'lishi mumkin emas
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_bb_active ON b_bookings (barber_id, start_at) "
                "WHERE status = 'active'"):
        try:
            with engine.begin() as c:
                c.execute(text(sql))
        except Exception as e:
            log.error("Indeks yaratilmadi: %s", str(e).splitlines()[0][:200])


def _migrate():
    """Eski versiyada yaratilgan jadvallarga yetishmayotgan ustunlarni qo'shadi."""
    ine = "IF NOT EXISTS " if "postgresql" in DATABASE_URL else ""
    cols = {
        "app_users": ["lang VARCHAR(2)", "role VARCHAR(10)", "name VARCHAR(100)", "phone VARCHAR(30)"],
        "barbers": ["name VARCHAR(80)", "shop VARCHAR(80)", "bio TEXT", "exp INTEGER DEFAULT 0",
                    "address VARCHAR(200)", "phone VARCHAR(30)", "active INTEGER DEFAULT 1",
                    "step INTEGER DEFAULT 30", "remind_hours INTEGER DEFAULT 2", "horizon INTEGER DEFAULT 30",
                    "photo TEXT", "thumb TEXT", "photo_v VARCHAR(20) DEFAULT '0'", "created_at TIMESTAMP"],
        "b_services": ["kind VARCHAR(20)", "name VARCHAR(100)", "price BIGINT DEFAULT 0", "duration INTEGER DEFAULT 30",
                       "active INTEGER DEFAULT 1", "sort_order INTEGER DEFAULT 0"],
        "b_hours": ["open_t VARCHAR(5)", "close_t VARCHAR(5)", "is_off INTEGER DEFAULT 0",
                    "br_start VARCHAR(5)", "br_end VARCHAR(5)"],
        "b_bookings": ["tg_name VARCHAR(100)", "name VARCHAR(100)", "phone VARCHAR(30)", "service_id INTEGER",
                       "service_name VARCHAR(100)", "service_kind VARCHAR(20)", "price BIGINT DEFAULT 0",
                       "duration INTEGER DEFAULT 30", "start_at TIMESTAMP", "end_at TIMESTAMP",
                       "status VARCHAR(12) DEFAULT 'active'", "cancelled_by VARCHAR(10)",
                       "created_at TIMESTAMP", "reminded INTEGER DEFAULT 0"],
    }
    for table, defs in cols.items():
        for d in defs:
            try:
                with engine.begin() as c:
                    c.execute(text(f"ALTER TABLE {table} ADD COLUMN {ine}{d}"))
            except Exception:
                pass  # ustun allaqachon bor


# ---- foydalanuvchi / til ----
def get_lang(uid):
    try:
        v = q_scalar("SELECT lang FROM app_users WHERE user_id=:u", {"u": uid})
        return v if v in LANGS else None
    except Exception:
        return None


def _upsert_user(uid, **fields):
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with engine.begin() as c:
        r = c.execute(text(f"UPDATE app_users SET {sets} WHERE user_id=:u"), {"u": uid, **fields})
        if not r.rowcount:
            cols = ["user_id"] + list(fields)
            c.execute(text(f"INSERT INTO app_users ({', '.join(cols)}) VALUES (:u, "
                           + ", ".join(f":{k}" for k in fields) + ")"), {"u": uid, **fields})


def set_lang(uid, lang):
    if lang in LANGS:
        _upsert_user(uid, lang=lang)


def get_role(uid):
    try:
        return q_scalar("SELECT role FROM app_users WHERE user_id=:u", {"u": uid}) or ""
    except Exception:
        return ""


# ---- sartaroshlar ----
B_COLS = "id, user_id, name, shop, bio, exp, address, phone, active, step, remind_hours, horizon, photo_v"


def _barber(r):
    return {"id": r[0], "user_id": int(r[1]), "name": r[2], "shop": r[3], "bio": r[4] or "",
            "exp": int(r[5] or 0), "address": r[6] or "", "phone": r[7] or "", "active": bool(r[8]),
            "step": int(r[9]), "remind_hours": int(r[10]), "horizon": int(r[11]), "photo_v": r[12] or "0"}


def get_barber(bid):
    def f():
        r = q_one(f"SELECT {B_COLS} FROM barbers WHERE id=:i", {"i": bid})
        return _barber(r) if r else None
    return cached(f"b:{bid}", 30, f)


def barber_of_user(uid):
    def f():
        r = q_one(f"SELECT {B_COLS} FROM barbers WHERE user_id=:u", {"u": uid})
        return _barber(r) if r else None
    return cached(f"bu:{uid}", 30, f)


# ---- xizmatlar ----
def _sv(r):
    return {"id": r[0], "kind": r[1] or "", "name": r[2] or "", "price": int(r[3]),
            "duration": int(r[4]), "active": bool(r[5])}


def sv_display(sv, lang):
    if sv["name"]:
        return sv["name"]
    return cat_name(sv["kind"], lang) if sv["kind"] in CATALOG else ""


def list_services(bid, only_active=True):
    def f():
        q = "SELECT id, kind, name, price, duration, active FROM b_services WHERE barber_id=:b"
        if only_active:
            q += " AND active=1"
        return [_sv(r) for r in q_all(q + " ORDER BY sort_order, id", {"b": bid})]
    return cached(f"sv:{bid}:{int(only_active)}", 30, f)


def get_service(bid, sid):
    for s in list_services(bid, False):
        if s["id"] == sid:
            return s
    return None


# ---- ish vaqti ----
def get_hours(bid):
    def f():
        rows = q_all("SELECT weekday, open_t, close_t, is_off, br_start, br_end FROM b_hours "
                     "WHERE barber_id=:b ORDER BY weekday", {"b": bid})
        out = [{"weekday": r[0], "open": r[1] or "09:00", "close": r[2] or "20:00", "off": bool(r[3]),
                "br_start": r[4] or "", "br_end": r[5] or ""} for r in rows]
        have = {h["weekday"] for h in out}
        for wd in range(7):
            if wd not in have:
                out.append({"weekday": wd, "open": "09:00", "close": "20:00", "off": wd == 6,
                            "br_start": "", "br_end": ""})
        return sorted(out, key=lambda h: h["weekday"])
    return cached(f"h:{bid}", 30, f)


def get_days_off(bid):
    today = now_local().date().isoformat()
    return cached(f"do:{bid}:{today}", 30, lambda: [
        r[0] for r in q_all("SELECT d FROM b_daysoff WHERE barber_id=:b AND d >= :t ORDER BY d",
                            {"b": bid, "t": today})])


def hm(s):
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


def fetch_busy(bid, start, end):
    rows = q_all("SELECT start_at, end_at FROM b_bookings WHERE barber_id=:b AND status='active' "
                 "AND start_at < :e AND end_at > :s", {"b": bid, "s": start, "e": end})
    return [(to_dt(r[0]), to_dt(r[1])) for r in rows]


def day_slots(d, duration, bb, hours, days_off, busy):
    """d kuni uchun bo'sh boshlanish vaqtlari ro'yxati ('HH:MM')."""
    now = now_local()
    if d < now.date() or d > now.date() + dt.timedelta(days=bb["horizon"]):
        return []
    h = hours[d.weekday()]
    if h["off"] or d.isoformat() in days_off:
        return []
    opening = dt.datetime.combine(d, hm(h["open"]))
    closing = dt.datetime.combine(d, hm(h["close"]))
    brk = None
    if h["br_start"] and h["br_end"]:
        brk = (dt.datetime.combine(d, hm(h["br_start"])), dt.datetime.combine(d, hm(h["br_end"])))
    step = dt.timedelta(minutes=bb["step"])
    dur = dt.timedelta(minutes=duration)
    earliest = now + dt.timedelta(minutes=MIN_NOTICE_MIN)
    out, t = [], opening
    while t + dur <= closing:
        e = t + dur
        ok = t >= earliest
        if ok and brk and t < brk[1] and e > brk[0]:
            ok = False
        if ok:
            for bs, be in busy:
                if t < be and e > bs:
                    ok = False
                    break
        if ok:
            out.append(t.strftime("%H:%M"))
        t += step
    return out


def first_free(bb, duration, busy, span=14):
    """Eng yaqin bo'sh kun va vaqtlar (bosh sahifa kartasi uchun)."""
    hours, off = get_hours(bb["id"]), set(get_days_off(bb["id"]))
    d = now_local().date()
    for _ in range(min(span, bb["horizon"]) + 1):
        sl = day_slots(d, duration, bb, hours, off, busy)
        if sl:
            return {"date": d.isoformat(), "time": sl[0]}
        d += dt.timedelta(days=1)
    return None


def shop_status(bb):
    """{'o':1,'t':'20:00'} ochiq / {'o':0,'t':'09:00','d':'2026-11-08'} yopiq."""
    now = now_local()
    hours, off = get_hours(bb["id"]), set(get_days_off(bb["id"]))
    h = hours[now.weekday()]
    if not h["off"] and now.date().isoformat() not in off:
        mins = now.hour * 60 + now.minute
        o, c = hm(h["open"]), hm(h["close"])
        if o.hour * 60 + o.minute <= mins < c.hour * 60 + c.minute:
            return {"o": 1, "t": h["close"]}
        if mins < o.hour * 60 + o.minute:
            return {"o": 0, "t": h["open"], "d": now.date().isoformat()}
    for i in range(1, 8):
        d = now.date() + dt.timedelta(days=i)
        hh = hours[d.weekday()]
        if not hh["off"] and d.isoformat() not in off:
            return {"o": 0, "t": hh["open"], "d": d.isoformat()}
    return {"o": 0, "t": "", "d": ""}


# ---- bronlar ----
BK_COLS = ("id, service_name, price, duration, start_at, end_at, status, name, phone, "
           "cancelled_by, user_id, tg_name, barber_id, service_kind")


def bk(r):
    s, e = to_dt(r[4]), to_dt(r[5])
    return {"id": r[0], "service": r[1], "price": int(r[2]), "duration": int(r[3]),
            "date": s.strftime("%Y-%m-%d"), "time": s.strftime("%H:%M"), "end": e.strftime("%H:%M"),
            "status": r[6], "name": r[7], "phone": r[8], "cancelled_by": r[9] or "",
            "user_id": int(r[10]), "tg_name": r[11] or "", "barber_id": r[12],
            "service_kind": r[13] or ""}


def localize(items, lang, with_shop=False):
    """Bron(lar)dagi xizmat nomini tilga moslaydi, istasa sartarosh ma'lumotini qo'shadi."""
    one = not isinstance(items, list)
    out = []
    for x in ([items] if one else items):
        x = dict(x)
        if x.get("service_kind") in CATALOG:
            x["service"] = cat_name(x["service_kind"], lang)
        if with_shop:
            bb = get_barber(x["barber_id"])
            x["shop"] = bb["shop"] if bb else ""
            x["address"] = bb["address"] if bb else ""
            x["shop_phone"] = bb["phone"] if bb else ""
        out.append(x)
    return out[0] if one else out


def get_booking(bid):
    r = q_one(f"SELECT {BK_COLS} FROM b_bookings WHERE id=:i", {"i": bid})
    return bk(r) if r else None


def set_cancelled(bid, by):
    with engine.begin() as c:
        r = c.execute(text("UPDATE b_bookings SET status='cancelled', cancelled_by=:b "
                           "WHERE id=:i AND status='active'"), {"b": by, "i": bid})
        ok = bool(r.rowcount)
    invalidate()
    return ok


def fmt_money(n):
    return f"{int(n):,}".replace(",", " ")


def fmt_when(date_str, time_str, lang="uz"):
    d = dt.date.fromisoformat(date_str)
    wd, mon = WD_L[lang][d.weekday()], MON_L[lang][d.month]
    if lang == "ru":
        return f"{d.day} {mon}, {wd.lower()}, {time_str}"
    if lang == "en":
        return f"{wd}, {mon} {d.day}, {time_str}"
    return f"{d.day}-{mon}, {wd.lower()}, {time_str}"


# ---------------- Telegram xabarlari (HTTP orqali, oqimlardan xavfsiz) ----------------
def tg_send(chat_id, text_, buttons=None):
    """'ok' | 'blocked' (foydalanuvchi botni bloklagan) | 'fail'"""
    payload = {"chat_id": chat_id, "text": text_, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return "ok"
    except urllib.error.HTTPError as e:
        log.warning("Telegram xabari yuborilmadi (HTTP %s), chat=%s", e.code, chat_id)
        return "blocked" if e.code in (400, 403) else "fail"
    except Exception as e:
        log.warning("Telegram xatosi: %s", type(e).__name__)
        return "fail"


def app_button(label, tab=""):
    if not APP_URL:
        return None
    url = APP_URL + (f"/?tab={tab}" if tab else "/")
    return [[{"text": label, "web_app": {"url": url}}]]


def booking_lines(b, lang, client_view=True):
    """client_view=True: mijozga (sartarosh ma'lumoti bilan); False: sartaroshga (mijoz ma'lumoti bilan)."""
    b = localize(b, lang, with_shop=True)
    lines = []
    if not client_view:
        lines += [f"👤 {html.escape(b['name'])}", f"📞 {html.escape(b['phone'])}"]
    lines += [f"✂️ {html.escape(b['service'])}",
              f"📅 {fmt_when(b['date'], b['time'], lang)}",
              f"💰 {fmt_money(b['price'])} {tr(lang, SOM)}"]
    if client_view:
        if b["shop"]:
            lines.append(f"💈 {html.escape(b['shop'])}")
        if b["address"]:
            lines.append(f"📍 {html.escape(b['address'])}")
        if b["shop_phone"]:
            lines.append(f"📞 {html.escape(b['shop_phone'])}")
    return "\n".join(lines)


def _barber_lang(b):
    bb = get_barber(b["barber_id"])
    return bb, (get_lang(bb["user_id"]) if bb else None) or "uz"


def client_lang(b):
    return get_lang(b["user_id"]) or "uz"


def notify_booked(b):
    lg = client_lang(b)
    tg_send(b["user_id"],
            f"✅ <b>{tr(lg, 'Bron tasdiqlandi')}</b>\n\n{booking_lines(b, lg)}\n\n"
            + tr(lg, "Vaqtidan oldin eslatamiz. Bekor qilish: «Mening bronlarim» bo'limida."),
            app_button(tr(lg, "🧾 Mening bronlarim"), "my"))
    bb, bl = _barber_lang(b)
    if bb:
        tg_send(bb["user_id"], f"🆕 <b>{tr(bl, 'Yangi bron')}</b>\n\n{booking_lines(b, bl, False)}",
                app_button(tr(bl, "📋 Bronlarni ochish"), "barber"))


def notify_cancelled(b, by):
    lg = client_lang(b)
    bb, bl = _barber_lang(b)
    if by == "client":
        if bb:
            tg_send(bb["user_id"], f"❌ <b>{tr(bl, 'Mijoz bronni bekor qildi')}</b>\n\n{booking_lines(b, bl, False)}",
                    app_button(tr(bl, "📋 Bronlarni ochish"), "barber"))
        tg_send(b["user_id"], f"❌ <b>{tr(lg, 'Bron bekor qilindi')}</b>\n\n{booking_lines(b, lg)}",
                app_button(tr(lg, "📅 Qayta band qilish")))
    else:
        tail = ("\n" + tr(lg, "Savollar bo'yicha: {phone}", phone=html.escape(bb["phone"]))) if bb and bb["phone"] else ""
        tg_send(b["user_id"],
                f"❌ <b>{tr(lg, 'Broningiz bekor qilindi (sartarosh tomonidan)')}</b>\n\n"
                f"{booking_lines(b, lg)}{tail}",
                app_button(tr(lg, "📅 Qayta band qilish")))


def notify_supers(bb):
    for aid in ADMIN_IDS:
        lg = get_lang(aid) or "uz"
        tg_send(aid, f"{tr(lg, NEW_BARBER)}\n\n"
                     f"💈 {html.escape(bb['shop'])}\n👤 {html.escape(bb['name'])}\n"
                     f"📍 {html.escape(bb['address'])}\n📞 {html.escape(bb['phone'])}")


def run_reminders():
    """Bron boshlanishidan oldin (sartarosh sozlagan soat) mijozga eslatma yuboradi."""
    now = now_local()
    rows = q_all("SELECT id FROM b_bookings WHERE status='active' AND reminded=0 "
                 "AND start_at > :n AND start_at <= :u", {"n": now, "u": now + dt.timedelta(hours=24)})
    for (bid,) in rows:
        b = get_booking(bid)
        if not b or b["status"] != "active":
            continue
        bb = get_barber(b["barber_id"])
        rh = bb["remind_hours"] if bb else 2
        if to_dt(f"{b['date']} {b['time']}") - now > dt.timedelta(hours=rh):
            continue
        lg = client_lang(b)
        shop = html.escape(bb["shop"]) if bb else ""
        res = tg_send(b["user_id"],
                      f"⏰ <b>{tr(lg, 'Eslatma')}</b>\n\n{tr(lg, '{shop}da bron:', shop=shop)}\n{booking_lines(b, lg)}",
                      app_button(tr(lg, "🧾 Mening bronlarim"), "my"))
        if res in ("ok", "blocked"):
            with engine.begin() as c:
                c.execute(text("UPDATE b_bookings SET reminded=1 WHERE id=:i"), {"i": bid})


# ---------------- Mini App: xavfsizlik ----------------
def verify_init_data(init_data):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa foydalanuvchi (dict), aks holda None."""
    try:
        data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(data.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(data["user"])
        user["id"] = int(user["id"])
        return user
    except Exception:
        return None


class ApiError(Exception):
    def __init__(self, msg, code=400, **kw):
        super().__init__(msg)
        self.msg, self.code, self.kw = msg, code, kw


def _str(v, n):
    return str(v or "").strip()[:n]


def _int(v, lo, hi, msg):
    try:
        n = int(float(str(v).replace(" ", "")))
    except Exception:
        raise ApiError(msg)
    if not lo <= n <= hi:
        raise ApiError(msg)
    return n


def _date(v):
    try:
        return dt.date.fromisoformat(str(v))
    except Exception:
        raise ApiError("Sana noto'g'ri")


def _hm(v, empty_ok=False):
    v = str(v or "").strip()
    if not v and empty_ok:
        return ""
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        raise ApiError("Vaqt noto'g'ri (SS:DD)")
    return v


def norm_phone(v):
    digits = re.sub(r"\D", "", str(v or ""))
    if len(digits) == 9:
        digits = "998" + digits
    if 10 <= len(digits) <= 15:
        return "+" + digits
    return None


def is_super(user):
    return user["id"] in ADMIN_IDS


def my_barber(user):
    bb = barber_of_user(user["id"])
    if not bb:
        raise ApiError("Avval sartarosh sifatida ro'yxatdan o'ting", 403)
    return bb


# ---------------- Mini App: umumiy API ----------------
def catalog_list(lang):
    return [{"kind": k, "name": cat_name(k, lang), "price": v[3], "dur": v[4]} for k, v in CATALOG.items()]


def barber_private(bb):
    return {k: bb[k] for k in ("id", "name", "shop", "bio", "exp", "address", "phone", "active",
                               "step", "remind_hours", "horizon", "photo_v")}


def api_init(user, b):
    bb = barber_of_user(user["id"])
    last = q_one("SELECT name, phone FROM b_bookings WHERE user_id=:u ORDER BY id DESC LIMIT 1", {"u": user["id"]})
    prof = q_one("SELECT role, name, phone FROM app_users WHERE user_id=:u", {"u": user["id"]})
    role = (prof[0] if prof else "") or ""
    nm = (prof[1] if prof and prof[1] else None) or (last[0] if last else None) or (user.get("first_name") or "")
    ph = (prof[2] if prof and prof[2] else None) or (last[1] if last else "")
    return {
        "me": {"id": user["id"], "lang": user["lang"], "is_super": is_super(user),
               "role": role if role in ("client", "barber") else ("barber" if bb else ("client" if last else "")),
               "barber": barber_private(bb) if bb else None, "name": nm, "phone": ph,
               "bot": BOT_USERNAME},
        "now": now_local().strftime("%Y-%m-%d %H:%M"),
        "catalog": catalog_list(user["lang"]),
        "barbers": cached("barbers", 20, _build_barbers),
    }


def api_lang(user, b):
    lg = b.get("lang")
    if lg in LANGS:
        set_lang(user["id"], lg)
    return {"ok": True}


def api_role(user, b):
    role = b.get("role")
    if role == "client":
        f = {"role": "client"}
        if b.get("name") is not None or b.get("phone") is not None:
            name = _str(b.get("name"), 60)
            if len(name) < 2:
                raise ApiError("Ismingizni kiriting")
            phone = norm_phone(b.get("phone"))
            if not phone:
                raise ApiError("Telefon raqamini to'g'ri kiriting")
            f.update(name=name, phone=phone)
        _upsert_user(user["id"], **f)
    elif role == "barber":
        _upsert_user(user["id"], role="barber")
    return {"ok": True}


def _build_barbers():
    now = now_local()
    until = now + dt.timedelta(days=15)
    busy_all = {}
    for bid, s, e in q_all("SELECT barber_id, start_at, end_at FROM b_bookings WHERE status='active' "
                           "AND end_at > :s AND start_at < :e", {"s": now, "e": until}):
        busy_all.setdefault(bid, []).append((to_dt(s), to_dt(e)))
    out = []
    for r in q_all(f"SELECT {B_COLS} FROM barbers WHERE active=1 ORDER BY id"):
        bb = _barber(r)
        svs = list_services(bb["id"], True)
        if not svs:
            continue
        shortest = min(s["duration"] for s in svs)
        out.append({
            "id": bb["id"], "shop": bb["shop"], "name": bb["name"], "exp": bb["exp"],
            "address": bb["address"], "bio": bb["bio"][:140], "photo_v": bb["photo_v"],
            "from_price": min(s["price"] for s in svs), "status": shop_status(bb),
            "next": first_free(bb, shortest, busy_all.get(bb["id"], [])),
        })
    return out


def api_barbers(user, b):
    return {"barbers": cached("barbers", 20, _build_barbers)}


def _public_barber(bid, user):
    bb = get_barber(bid)
    if not bb or (not bb["active"] and not is_super(user) and bb["user_id"] != user["id"]):
        raise ApiError("Sartarosh topilmadi", 404)
    return bb


def api_barber(user, b):
    bb = _public_barber(_int(b.get("id"), 1, 10**9, "Sartarosh topilmadi"), user)
    lg = user["lang"]
    return {
        "barber": {"id": bb["id"], "shop": bb["shop"], "name": bb["name"], "bio": bb["bio"], "exp": bb["exp"],
                   "address": bb["address"], "phone": bb["phone"], "photo_v": bb["photo_v"],
                   "active": bb["active"], "mine": bb["user_id"] == user["id"]},
        "services": [{"id": s["id"], "name": sv_display(s, lg), "price": s["price"], "duration": s["duration"]}
                     for s in list_services(bb["id"], True)],
        "hours": get_hours(bb["id"]),
        "days_off": get_days_off(bb["id"]),
        "status": shop_status(bb),
    }


def _booking_target(b, user):
    bb = _public_barber(_int(b.get("barber_id"), 1, 10**9, "Sartarosh topilmadi"), user)
    if not bb["active"]:
        raise ApiError("Bu sartarosh hozir qabul qilmayapti")
    try:
        sv = get_service(bb["id"], int(b.get("service_id")))
    except Exception:
        sv = None
    if not sv or not sv["active"]:
        raise ApiError("Xizmat topilmadi")
    return bb, sv


def api_days(user, b):
    bb, sv = _booking_target(b, user)
    hours, off = get_hours(bb["id"]), set(get_days_off(bb["id"]))
    today = now_local().date()
    last = today + dt.timedelta(days=bb["horizon"])
    busy = fetch_busy(bb["id"], dt.datetime.combine(today, dt.time.min), dt.datetime.combine(last, dt.time.max))
    days, d = [], today
    while d <= last:
        days.append({"date": d.isoformat(), "open": bool(day_slots(d, sv["duration"], bb, hours, off, busy))})
        d += dt.timedelta(days=1)
    return {"days": days}


def api_slots(user, b):
    bb, sv = _booking_target(b, user)
    d = _date(b.get("date"))
    busy = fetch_busy(bb["id"], dt.datetime.combine(d, dt.time.min), dt.datetime.combine(d, dt.time.max))
    return {"slots": day_slots(d, sv["duration"], bb, get_hours(bb["id"]), set(get_days_off(bb["id"])), busy)}


def api_book(user, b):
    bb, sv = _booking_target(b, user)
    d, t = _date(b.get("date")), _hm(b.get("time"))
    name = _str(b.get("name"), 60)
    if len(name) < 2:
        raise ApiError("Ismingizni kiriting")
    phone = norm_phone(b.get("phone"))
    if not phone:
        raise ApiError("Telefon raqamini to'g'ri kiriting")
    start = dt.datetime.combine(d, hm(t))
    end = start + dt.timedelta(minutes=sv["duration"])
    tg_name = _str(" ".join(x for x in (user.get("first_name"), user.get("last_name")) if x), 100)
    disp = sv_display(sv, "uz")
    kind = sv["kind"] if (sv["kind"] in CATALOG and not sv["name"]) else ""
    with BOOK_LOCK:
        busy = fetch_busy(bb["id"], dt.datetime.combine(d, dt.time.min), dt.datetime.combine(d, dt.time.max))
        if t not in day_slots(d, sv["duration"], bb, get_hours(bb["id"]), set(get_days_off(bb["id"])), busy):
            raise ApiError("Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.", 409)
        now = now_local()
        with engine.begin() as c:
            n = c.execute(text("SELECT COUNT(*) FROM b_bookings WHERE user_id=:u AND status='active' "
                               "AND start_at > :n"), {"u": user["id"], "n": now}).scalar()
            if n >= MAX_ACTIVE_PER_USER:
                raise ApiError("Sizda allaqachon {n} ta faol bron bor. "
                               "Avval birortasini bekor qiling yoki vaqti kelishini kuting.",
                               n=MAX_ACTIVE_PER_USER)
            remind_soon = 1 if start - now <= dt.timedelta(hours=bb["remind_hours"]) else 0
            try:
                c.execute(text(
                    "INSERT INTO b_bookings (barber_id, user_id, tg_name, name, phone, service_id, service_name, "
                    "service_kind, price, duration, start_at, end_at, status, created_at, reminded) VALUES "
                    "(:b, :u, :tn, :n, :p, :sid, :sn, :sk, :pr, :du, :s, :e, 'active', :c, :r)"),
                    {"b": bb["id"], "u": user["id"], "tn": tg_name, "n": name, "p": phone, "sid": sv["id"],
                     "sn": disp, "sk": kind, "pr": sv["price"], "du": sv["duration"], "s": start, "e": end,
                     "c": now, "r": remind_soon})
            except Exception as e:
                if "unique" in str(e).lower() or "constraint" in str(e).lower():
                    raise ApiError("Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.", 409)
                raise
            bid = c.execute(text("SELECT id FROM b_bookings WHERE barber_id=:b AND start_at=:s "
                                 "AND status='active' ORDER BY id DESC LIMIT 1"),
                            {"b": bb["id"], "s": start}).scalar()
    invalidate()
    booking = get_booking(bid)
    threading.Thread(target=notify_booked, args=(booking,), daemon=True).start()
    return {"booking": localize(booking, user["lang"], with_shop=True)}


def api_my(user, b):
    rows = q_all(f"SELECT {BK_COLS} FROM b_bookings WHERE user_id=:u ORDER BY start_at DESC LIMIT 60",
                 {"u": user["id"]})
    now = now_local()
    up, past = [], []
    for r in rows:
        x = bk(r)
        is_up = x["status"] == "active" and to_dt(f"{x['date']} {x['time']}") > now
        (up if is_up else past).append(x)
    up.reverse()  # yaqin vaqt birinchi
    lg = user["lang"]
    return {"upcoming": localize(up, lg, True), "past": localize(past[:20], lg, True)}


def api_cancel(user, b):
    x = get_booking(_int(b.get("id"), 1, 10**12, "Bron topilmadi"))
    if not x or x["user_id"] != user["id"]:
        raise ApiError("Bron topilmadi", 404)
    if x["status"] != "active":
        raise ApiError("Bu bron allaqachon bekor qilingan")
    if to_dt(f"{x['date']} {x['time']}") <= now_local():
        raise ApiError("O'tib ketgan bronni bekor qilib bo'lmaydi")
    if set_cancelled(x["id"], "client"):
        threading.Thread(target=notify_cancelled, args=(x, "client"), daemon=True).start()
    return {"ok": True}


# ---------------- Mini App: sartarosh paneli ----------------
def _check_photo(img, limit):
    m = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", str(img or ""))
    if not m or len(m.group(2)) > limit:
        raise ApiError("Rasm noto'g'ri yoki juda katta")
    base64.b64decode(m.group(2), validate=True)
    return img


def _profile_fields(b):
    shop = _str(b.get("shop"), 80)
    if len(shop) < 2:
        raise ApiError("Salon nomini kiriting")
    name = _str(b.get("name"), 80)
    if len(name) < 2:
        raise ApiError("Ismingizni kiriting")
    address = _str(b.get("address"), 200)
    if len(address) < 3:
        raise ApiError("Manzilni kiriting")
    phone = _str(b.get("phone"), 30)
    if not re.fullmatch(r"[+\d\s()\-]{5,30}", phone):
        raise ApiError("Telefon noto'g'ri")
    exp = _int(b.get("exp") or 0, 0, 70, "Tajriba noto'g'ri")
    return {"shop": shop, "name": name, "address": address, "phone": phone, "exp": exp,
            "bio": _str(b.get("bio"), 600)}


def api_b_register(user, b):
    if barber_of_user(user["id"]):
        raise ApiError("Siz allaqachon sartarosh sifatida ro'yxatdan o'tgansiz")
    f = _profile_fields(b)
    items = b.get("services")
    if not isinstance(items, list) or not items:
        raise ApiError("Kamida bitta xizmat tanlang")
    clean = []
    for it in items[:20]:
        kind = str(it.get("kind") or "")
        if kind not in CATALOG:
            raise ApiError("Xizmat topilmadi")
        clean.append((kind, _int(it.get("price"), 0, 100_000_000, "Narx noto'g'ri"),
                      _int(it.get("duration"), 5, 600, "Davomiylik 5 dan 600 daqiqagacha bo'lsin")))
    with engine.begin() as c:
        c.execute(text("INSERT INTO barbers (user_id, name, shop, bio, exp, address, phone, active, created_at) "
                       "VALUES (:u, :name, :shop, :bio, :exp, :address, :phone, 1, :c)"),
                  {"u": user["id"], "c": now_local(), **f})
        bid = c.execute(text("SELECT id FROM barbers WHERE user_id=:u"), {"u": user["id"]}).scalar()
        for wd in range(7):
            c.execute(text("INSERT INTO b_hours (barber_id, weekday, open_t, close_t, is_off, br_start, br_end) "
                           "VALUES (:b, :w, '09:00', '20:00', :o, '', '')"), {"b": bid, "w": wd, "o": 1 if wd == 6 else 0})
        for i, (kind, price, dur) in enumerate(clean):
            c.execute(text("INSERT INTO b_services (barber_id, kind, name, price, duration, active, sort_order) "
                           "VALUES (:b, :k, '', :p, :d, 1, :s)"), {"b": bid, "k": kind, "p": price, "d": dur, "s": i})
    _upsert_user(user["id"], role="barber")
    invalidate()
    bb = get_barber(bid)
    threading.Thread(target=notify_supers, args=(bb,), daemon=True).start()
    return {"barber": barber_private(bb)}


def api_b_profile(user, b):
    return {"barber": barber_private(my_barber(user))}


def api_b_profile_save(user, b):
    bb = my_barber(user)
    f = _profile_fields(b)
    step = _int(b.get("step"), 10, 120, "Vaqt qadami noto'g'ri")
    rem = _int(b.get("remind_hours"), 1, 24, "Eslatma vaqti noto'g'ri")
    hor = _int(b.get("horizon"), 1, 120, "Necha kun oldin noto'g'ri")
    with engine.begin() as c:
        c.execute(text("UPDATE barbers SET name=:name, shop=:shop, bio=:bio, exp=:exp, address=:address, "
                       "phone=:phone, step=:step, remind_hours=:rh, horizon=:hor WHERE id=:i"),
                  {**f, "step": step, "rh": rem, "hor": hor, "i": bb["id"]})
    invalidate()
    return {"barber": barber_private(get_barber(bb["id"]))}


def api_b_photo(user, b):
    bb = my_barber(user)
    with engine.begin() as c:
        if b.get("remove"):
            c.execute(text("UPDATE barbers SET photo=NULL, thumb=NULL, photo_v='0' WHERE id=:i"), {"i": bb["id"]})
            v = "0"
        else:
            img = _check_photo(b.get("image"), 450_000)
            th = _check_photo(b.get("thumb"), 70_000)
            v = str(int(time.time()))
            c.execute(text("UPDATE barbers SET photo=:p, thumb=:t, photo_v=:v WHERE id=:i"),
                      {"p": img, "t": th, "v": v, "i": bb["id"]})
    invalidate()
    return {"photo_v": v}


def api_b_services(user, b):
    return {"services": list_services(my_barber(user)["id"], False)}


def api_b_service_save(user, b):
    bb = my_barber(user)
    kind = str(b.get("kind") or "")
    if kind and kind not in CATALOG:
        kind = ""
    name = _str(b.get("name"), 100)
    if not kind and len(name) < 2:
        raise ApiError("Xizmat nomini kiriting")
    price = _int(b.get("price"), 0, 100_000_000, "Narx noto'g'ri")
    dur = _int(b.get("duration"), 5, 600, "Davomiylik 5 dan 600 daqiqagacha bo'lsin")
    active = 1 if b.get("active", True) else 0
    with engine.begin() as c:
        if b.get("id"):
            sid = _int(b.get("id"), 1, 10**9, "Xizmat topilmadi")
            r = c.execute(text("UPDATE b_services SET kind=:k, name=:n, price=:p, duration=:d, active=:a "
                               "WHERE id=:i AND barber_id=:b"),
                          {"k": kind, "n": name, "p": price, "d": dur, "a": active, "i": sid, "b": bb["id"]})
            if not r.rowcount:
                raise ApiError("Xizmat topilmadi", 404)
        else:
            mx = c.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM b_services WHERE barber_id=:b"),
                           {"b": bb["id"]}).scalar()
            c.execute(text("INSERT INTO b_services (barber_id, kind, name, price, duration, active, sort_order) "
                           "VALUES (:b, :k, :n, :p, :d, :a, :s)"),
                      {"b": bb["id"], "k": kind, "n": name, "p": price, "d": dur, "a": active, "s": int(mx) + 1})
    invalidate()
    return {"services": list_services(bb["id"], False)}


def api_b_service_delete(user, b):
    bb = my_barber(user)
    sid = _int(b.get("id"), 1, 10**9, "Xizmat topilmadi")
    with engine.begin() as c:  # bronlarda xizmat nomi va narxi nusxalangan, o'chirish xavfsiz
        c.execute(text("DELETE FROM b_services WHERE id=:i AND barber_id=:b"), {"i": sid, "b": bb["id"]})
    invalidate()
    return {"services": list_services(bb["id"], False)}


def api_b_hours(user, b):
    bb = my_barber(user)
    return {"hours": get_hours(bb["id"]), "days_off": get_days_off(bb["id"])}


def api_b_hours_save(user, b):
    bb = my_barber(user)
    rows = b.get("hours")
    if not isinstance(rows, list) or len(rows) != 7:
        raise ApiError("Ish vaqti noto'g'ri")
    clean = []
    for i, r in enumerate(rows):
        off = bool(r.get("off"))
        o, c_ = _hm(r.get("open")), _hm(r.get("close"))
        bs, be = _hm(r.get("br_start"), True), _hm(r.get("br_end"), True)
        if not off and o >= c_:
            raise ApiError("{wd}: tugash vaqti boshlanishidan keyin bo'lsin", wd=WD_L[user["lang"]][i])
        if bool(bs) != bool(be) or (bs and (bs >= be or bs < o or be > c_)):
            raise ApiError("{wd}: tanaffus ish vaqti ichida bo'lsin", wd=WD_L[user["lang"]][i])
        clean.append((i, o, c_, 1 if off else 0, bs, be))
    with engine.begin() as c:
        for i, o, c_, off, bs, be in clean:
            r = c.execute(text("UPDATE b_hours SET open_t=:o, close_t=:c, is_off=:f, br_start=:bs, br_end=:be "
                               "WHERE barber_id=:b AND weekday=:w"),
                          {"o": o, "c": c_, "f": off, "bs": bs, "be": be, "w": i, "b": bb["id"]})
            if not r.rowcount:
                c.execute(text("INSERT INTO b_hours (barber_id, weekday, open_t, close_t, is_off, br_start, br_end) "
                               "VALUES (:b, :w, :o, :c, :f, :bs, :be)"),
                          {"o": o, "c": c_, "f": off, "bs": bs, "be": be, "w": i, "b": bb["id"]})
    invalidate()
    return {"hours": get_hours(bb["id"])}


def api_b_dayoff(user, b):
    bb = my_barber(user)
    d = _date(b.get("date")).isoformat()
    with engine.begin() as c:
        c.execute(text("DELETE FROM b_daysoff WHERE barber_id=:b AND d=:d"), {"b": bb["id"], "d": d})
        if b.get("off", True):
            c.execute(text("INSERT INTO b_daysoff (barber_id, d) VALUES (:b, :d)"), {"b": bb["id"], "d": d})
    invalidate()
    return {"days_off": get_days_off(bb["id"])}


def api_b_bookings(user, b):
    bb = my_barber(user)
    d = _date(b.get("date"))
    rows = q_all(f"SELECT {BK_COLS} FROM b_bookings WHERE barber_id=:b AND start_at >= :s AND start_at <= :e "
                 "ORDER BY start_at", {"b": bb["id"], "s": dt.datetime.combine(d, dt.time.min),
                                       "e": dt.datetime.combine(d, dt.time.max)})
    items = localize([bk(r) for r in rows], user["lang"])
    return {"bookings": items, "active": sum(1 for x in items if x["status"] == "active")}


def api_b_overview(user, b):
    bb = my_barber(user)
    today = now_local().date()
    tom = today + dt.timedelta(days=1)
    rows = q_all("SELECT start_at, price FROM b_bookings WHERE barber_id=:b AND status='active' AND start_at >= :s",
                 {"b": bb["id"], "s": dt.datetime.combine(today, dt.time.min)})
    out = {"today": {"n": 0, "sum": 0}, "tomorrow": {"n": 0, "sum": 0}, "later": 0}
    for st, pr in rows:
        d = to_dt(st).date()
        k = "today" if d == today else "tomorrow" if d == tom else None
        if k:
            out[k]["n"] += 1
            out[k]["sum"] += int(pr)
        else:
            out["later"] += 1
    return out


def api_b_cancel(user, b):
    bb = my_barber(user)
    x = get_booking(_int(b.get("id"), 1, 10**12, "Bron topilmadi"))
    if not x or x["barber_id"] != bb["id"]:
        raise ApiError("Bron topilmadi", 404)
    if x["status"] != "active":
        raise ApiError("Bu bron allaqachon bekor qilingan")
    if set_cancelled(x["id"], "barber"):
        threading.Thread(target=notify_cancelled, args=(x, "barber"), daemon=True).start()
    return {"ok": True}


def api_s_barbers(user, b):
    if not is_super(user):
        raise ApiError("Ruxsat yo'q", 403)
    return {"barbers": [{"id": r[0], "shop": r[1], "name": r[2], "address": r[3], "active": bool(r[4])}
                        for r in q_all("SELECT id, shop, name, address, active FROM barbers ORDER BY id DESC")]}


def api_s_toggle(user, b):
    if not is_super(user):
        raise ApiError("Ruxsat yo'q", 403)
    bid = _int(b.get("id"), 1, 10**9, "Sartarosh topilmadi")
    with engine.begin() as c:
        c.execute(text("UPDATE barbers SET active=:a WHERE id=:i"), {"a": 1 if b.get("active") else 0, "i": bid})
    invalidate()
    return {"ok": True}


API = {
    "init": api_init, "lang": api_lang, "role": api_role,
    "barbers": api_barbers, "barber": api_barber,
    "days": api_days, "slots": api_slots, "book": api_book, "my": api_my, "cancel": api_cancel,
    "b/register": api_b_register, "b/profile": api_b_profile, "b/profile_save": api_b_profile_save,
    "b/photo": api_b_photo, "b/services": api_b_services, "b/service_save": api_b_service_save,
    "b/service_delete": api_b_service_delete, "b/hours": api_b_hours, "b/hours_save": api_b_hours_save,
    "b/dayoff": api_b_dayoff, "b/bookings": api_b_bookings, "b/overview": api_b_overview,
    "b/cancel": api_b_cancel, "s/toggle": api_s_toggle, "s/barbers": api_s_barbers,
}


# ---------------- HTTP server ----------------
_PAGE = {}


def page_bytes():
    if not _PAGE:
        raw = APP_HTML.encode("utf-8")
        _PAGE.update(raw=raw, gz=gzip.compress(raw, 9), etag='"' + hashlib.sha1(raw).hexdigest()[:20] + '"')
    return _PAGE


def _warm():
    """Sahifa so'ralgan zahoti bazani uyg'otib, sartaroshlar ro'yxatini keshga olamiz."""
    try:
        cached("barbers", 20, _build_barbers)
    except Exception:
        pass


class Web(BaseHTTPRequestHandler):
    server_version = "Barber"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        gz = False
        if len(body) > 1200 and ("text" in ctype or "json" in ctype) \
                and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body, gz = gzip.compress(body, 5), True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _page(self):
        p = page_bytes()
        if self.headers.get("If-None-Match") == p["etag"]:
            self.send_response(304)
            self.send_header("ETag", p["etag"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        body = p["gz"] if gz else p["raw"]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", p["etag"])
        self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(body)

    def _photo(self, bid, col):
        try:
            v = q_scalar(f"SELECT {col} FROM barbers WHERE id=:i", {"i": int(bid)})
            m = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,(.+)", v or "", re.S)
            if not m:
                raise LookupError
            self._send(200, base64.b64decode(m.group(2)), m.group(1), "public, max-age=2592000, immutable")
        except Exception:
            self._send(404, "yo'q", "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/app", "/index.html"):
            threading.Thread(target=_warm, daemon=True).start()
            self._page()
        elif re.fullmatch(r"/photo/\d+", path):
            self._photo(path.rsplit("/", 1)[1], "photo")
        elif re.fullmatch(r"/thumb/\d+", path):
            self._photo(path.rsplit("/", 1)[1], "thumb")
        elif path == "/health":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "yo'q", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if 0 < length <= 700_000 else b""
            if length > 700_000:
                self.close_connection = True
        except Exception:
            raw, length = b"", 0
        hl = self.headers.get("X-Lang", "")
        if not path.startswith("/api/") or not API.get(path[5:]):
            self._json(404, {"error": tr(hl if hl in LANGS else "uz", "Topilmadi")})
            return
        fn = API[path[5:]]
        user = verify_init_data(self.headers.get("X-Init-Data", ""))
        if not user:
            self._json(401, {"error": tr(hl if hl in LANGS else "uz", "Ilovani Telegram orqali qayta oching")})
            return
        if hl in LANGS:
            if path[5:] in ("init", "book", "b/register"):
                try:
                    if get_lang(user["id"]) != hl:
                        set_lang(user["id"], hl)
                except Exception:
                    log.exception("Til saqlanmadi")
            user["lang"] = hl
        else:
            user["lang"] = get_lang(user["id"]) or lang_from_code(user.get("language_code"))
        lg = user["lang"]
        if length > 700_000:
            self._json(413, {"error": tr(lg, "So'rov juda katta")})
            return
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            self._json(400, {"error": tr(lg, "So'rov noto'g'ri")})
            return
        try:
            self._json(200, fn(user, body))
        except ApiError as e:
            self._json(e.code, {"error": tr(lg, e.msg, **e.kw)})
        except Exception:
            log.exception("API xatosi: %s", path)
            self._json(500, {"error": tr(lg, "Server xatosi, birozdan keyin qayta urinib ko'ring")})

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Web)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("HTTP server %s portda ishga tushdi", PORT)
    return srv


# ---------------- Bot ----------------
ST = {
 "uz": {"hi": "Salom, {who}! 👋", "pitch": "✂️ Sartaroshlarni ko'ring va o'zingizga mosini tanlang\n📅 Qulay vaqtni bir necha soniyada band qiling\n⏰ Vaqtidan oldin eslatib turamiz",
        "barb": "<i>Sartaroshmisiz? Salonni ro'yxatdan o'tkazing, mijozlar o'zlari bron qilishadi.</i>",
        "next": "📌 <b>Keyingi broningiz</b>", "today": "📊 Bugun: <b>{n}</b> ta bron", "mine": "Sizning salon: <b>{shop}</b>",
        "b_pick": "💈 Sartaroshni tanlash", "b_my": "🧾 Bronlarim", "b_reg": "✂️ Men sartaroshman", "b_panel": "📋 Bugungi bronlar", "b_set": "⚙️ Xizmat va narxlar",
        "short": "💈 Sartaroshni tanlang va vaqtni band qiling", "desc": "Sartaroshlarni ko'ring, xizmat va narxlarni solishtiring, qulay vaqtni band qiling. Sartaroshlar uchun: o'z sahifangizni yarating va mijozlarni qabul qiling.\n\nBoshlash uchun START ni bosing.",
        "c_my": "Mening bronlarim", "c_panel": "Sartarosh paneli", "c_help": "Yordam", "help": "Botni ishlatish:\n/start - bosh sahifa\n/my - bronlarim\n/panel - sartarosh paneli\n/myid - Telegram ID"},
 "ru": {"hi": "Привет, {who}! 👋", "pitch": "✂️ Смотрите барберов и выбирайте подходящего\n📅 Записывайтесь на удобное время за несколько секунд\n⏰ Напомним перед визитом",
        "barb": "<i>Вы барбер? Зарегистрируйте салон — клиенты будут записываться сами.</i>",
        "next": "📌 <b>Ближайшая запись</b>", "today": "📊 Сегодня: <b>{n}</b> записей", "mine": "Ваш салон: <b>{shop}</b>",
        "b_pick": "💈 Выбрать барбера", "b_my": "🧾 Мои записи", "b_reg": "✂️ Я барбер", "b_panel": "📋 Записи на сегодня", "b_set": "⚙️ Услуги и цены",
        "short": "💈 Выберите барбера и запишитесь онлайн", "desc": "Смотрите барберов, сравнивайте услуги и цены, записывайтесь на удобное время. Для барберов: создайте свою страницу и принимайте клиентов.\n\nНажмите START, чтобы начать.",
        "c_my": "Мои записи", "c_panel": "Панель барбера", "c_help": "Помощь", "help": "Как пользоваться:\n/start - главная\n/my - мои записи\n/panel - панель барбера\n/myid - Telegram ID"},
 "en": {"hi": "Hi, {who}! 👋", "pitch": "✂️ Browse barbers and pick your favourite\n📅 Book a time in a few seconds\n⏰ We'll remind you before your visit",
        "barb": "<i>Are you a barber? Register your shop and clients will book on their own.</i>",
        "next": "📌 <b>Your next booking</b>", "today": "📊 Today: <b>{n}</b> bookings", "mine": "Your shop: <b>{shop}</b>",
        "b_pick": "💈 Find a barber", "b_my": "🧾 My bookings", "b_reg": "✂️ I'm a barber", "b_panel": "📋 Today's bookings", "b_set": "⚙️ Services & prices",
        "short": "💈 Pick a barber and book online", "desc": "Browse barbers, compare services and prices, book a time that suits you. For barbers: create your page and take bookings.\n\nPress START to begin.",
        "c_my": "My bookings", "c_panel": "Barber panel", "c_help": "Help", "help": "How to use:\n/start - home\n/my - my bookings\n/panel - barber panel\n/myid - Telegram ID"},
}


def _wa(label, qs=""):
    return {"text": label, "web_app": {"url": APP_URL + "/" + qs}}


def _start_payload(uid, first, lg, deep=""):
    """Foydalanuvchi holatiga qarab salomlashuv matni va tugmalar."""
    T = ST[lg]
    bb = barber_of_user(uid)
    now = now_local()
    nxt = q_one(f"SELECT {BK_COLS} FROM b_bookings WHERE user_id=:u AND status='active' AND start_at > :n "
                "ORDER BY start_at LIMIT 1", {"u": uid, "n": now})
    parts = [f"💈 <b>{T['hi'].format(who=html.escape(first or ''))}</b>"]
    rows = []
    if bb:
        n = q_scalar("SELECT COUNT(*) FROM b_bookings WHERE barber_id=:b AND status='active' "
                     "AND start_at >= :s AND start_at <= :e",
                     {"b": bb["id"], "s": dt.datetime.combine(now.date(), dt.time.min),
                      "e": dt.datetime.combine(now.date(), dt.time.max)}) or 0
        parts.append(T["mine"].format(shop=html.escape(bb["shop"])) + "\n" + T["today"].format(n=n))
        rows = [[_wa(T["b_panel"], "?tab=barber")], [_wa(T["b_set"], "?tab=barber"), _wa(T["b_pick"])]]
    else:
        parts.append(T["pitch"])
    if nxt:
        parts.append(T["next"] + "\n" + booking_lines(get_booking(nxt[0]), lg))
        rows = rows or [[_wa(T["b_pick"])]]
        rows.append([_wa(T["b_my"], "?tab=my")])
    elif not bb:
        rows = [[_wa(T["b_pick"], "?b=" + deep if deep else "")], [_wa(T["b_my"], "?tab=my"), _wa(T["b_reg"], "?r=barber")]]
        parts.append(T["barb"])
    return "\n\n".join(parts), rows


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    m = re.fullmatch(r"b(\d{1,9})", (ctx.args[0] if ctx.args else ""))
    deep = m.group(1) if m else ""

    def work():
        lg = get_lang(u.id) or lang_from_code(u.language_code)
        try:
            cached("barbers", 20, _build_barbers)  # bazani va ro'yxatni oldindan isitamiz
        except Exception:
            pass
        return lg, _start_payload(u.id, u.first_name, lg, deep)
    lg, (msg, rows) = await asyncio.to_thread(work)
    if not APP_URL:
        msg += "\n\n⚠️ " + tr(lg, "Mini App manzili sozlanmagan (PUBLIC_URL).")
    await update.effective_message.reply_text(
        msg, parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], web_app=WebAppInfo(url=b["web_app"]["url"]))
                                            for b in r] for r in rows]) if (rows and APP_URL) else None)


async def _open_cmd(update: Update, qs, key):
    lg = await asyncio.to_thread(_user_lang, update)
    if not APP_URL:
        return
    await update.effective_message.reply_text(
        "👇", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(ST[lg][key], web_app=WebAppInfo(url=APP_URL + "/" + qs))]]))


async def cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _open_cmd(update, "?tab=my", "b_my")


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _open_cmd(update, "?tab=barber", "b_panel")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lg = await asyncio.to_thread(_user_lang, update)
    await update.effective_message.reply_text(ST[lg]["help"])


def _user_lang(update):
    u = update.effective_user
    return get_lang(u.id) or lang_from_code(u.language_code)


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lg = await asyncio.to_thread(_user_lang, update)
    await update.effective_message.reply_text(
        tr(lg, "Sizning Telegram ID: <code>{id}</code>", id=update.effective_user.id), parse_mode="HTML")


async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(run_reminders)
    except Exception:
        log.exception("Eslatmalarda xato")


def _db_ping():
    q_scalar("SELECT 1")


async def keep_warm(ctx: ContextTypes.DEFAULT_TYPE):
    """Render ham, Neon bazasi ham uxlab qolmasligi uchun (har 4 daqiqada)."""
    if os.getenv("KEEP_DB_WARM") == "1":
        try:
            await asyncio.to_thread(_db_ping)
        except Exception as e:
            log.warning("db ping: %s", type(e).__name__)
    if APP_URL:
        try:
            await asyncio.to_thread(urllib.request.urlopen, APP_URL + "/health", None, 15)
        except Exception as e:
            log.warning("self_ping: %s", type(e).__name__)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("409 Conflict: bu bot boshqa joyda ham ishlayapti. Ortiqcha nusxani to'xtating.")
        return
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Tarmoq xatosi: %s", type(err).__name__)
        return
    log.error("Kutilmagan xato", exc_info=err)


async def post_init(app: Application):
    global BOT_USERNAME
    try:
        BOT_USERNAME = (await app.bot.get_me()).username or ""
    except Exception as e:
        log.warning("get_me: %s", type(e).__name__)
    def cmds(lc):
        T = ST[lc]
        return [BotCommand("start", tr(lc, "Boshlash")), BotCommand("my", T["c_my"]),
                BotCommand("panel", T["c_panel"]), BotCommand("help", T["c_help"])]
    await app.bot.set_my_commands(cmds("uz"))
    for lc in ("uz", "ru", "en"):
        try:
            await app.bot.set_my_commands(cmds(lc), language_code=lc)
            await app.bot.set_my_short_description(ST[lc]["short"], language_code=lc)
            await app.bot.set_my_description(ST[lc]["desc"], language_code=lc)
        except Exception as e:
            log.warning("Bot sozlamalari (%s) o'rnatilmadi: %s", lc, type(e).__name__)
    try:
        await app.bot.set_my_short_description(ST["uz"]["short"])
        await app.bot.set_my_description(ST["uz"]["desc"])
    except Exception as e:
        log.warning("Bot tavsifi o'rnatilmadi: %s", type(e).__name__)
    if APP_URL:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="💈 Ochish · Открыть", web_app=WebAppInfo(url=APP_URL + "/")))
        except Exception as e:
            log.warning("Menu tugmasini o'rnatib bo'lmadi: %s", type(e).__name__)


def main():
    init_db()
    start_server()
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .get_updates_read_timeout(30).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, cmd_start))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(reminder_job, interval=60, first=20)
        app.job_queue.run_repeating(keep_warm, interval=240, first=60)
    else:
        log.error("JobQueue yo'q: requirements.txt da python-telegram-bot[job-queue] bo'lsin!")
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


# ---------------- Mini App sahifasi ----------------
APP_HTML = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="color-scheme" content="light dark">
<title>Barber</title>
<style>
:root{
  --bg:var(--tg-theme-bg-color,#ffffff);
  --fg:var(--tg-theme-text-color,#17191c);
  --hint:var(--tg-theme-hint-color,#6f7683);
  --soft:var(--tg-theme-secondary-bg-color,#f1f2f4);
  --line:rgba(127,127,127,.28);
  --accent:#b8322a;--accent2:#d9534a;--accent-fg:#fff;
  --blue:#2b5d9b;
  --ok:#23864f;--bad:#d2383f;
  --shadow:0 1px 2px rgba(0,0,0,.06),0 6px 18px rgba(0,0,0,.07);
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Noto Serif",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --safe-b:env(safe-area-inset-bottom,0px);
  --tab-h:58px;--top-h:48px;
}
:root[data-scheme=dark]{--accent:#e5574b;--accent2:#c9443a;--blue:#7aaaee;--ok:#41c07a;--bad:#f2676d;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 18px rgba(0,0,0,.25)}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 var(--sans);-webkit-tap-highlight-color:transparent;
  padding-bottom:calc(var(--tab-h) + var(--safe-b) + 24px);min-height:100vh}
body.nobar{padding-bottom:calc(var(--safe-b) + 28px)}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0;text-align:inherit}
button:active:not(:disabled){transform:scale(.985)}
a{color:var(--blue);text-decoration:none}
h1,h2,h3{margin:0}
#view{padding:0 16px;max-width:560px;margin:0 auto}
.ic{flex:none;display:block}
#top{position:sticky;top:0;z-index:25;height:var(--top-h);display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:0 16px;background:var(--bg);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:8px;min-width:0;font:700 17px/1 var(--serif)}
.brand .mk{width:26px;height:26px;border-radius:8px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;display:grid;place-items:center;flex:none}
.brand span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lang{display:flex;background:var(--soft);border-radius:99px;padding:3px;flex:none}
.lang button{min-width:38px;height:28px;border-radius:99px;font-size:12.5px;font-weight:700;color:var(--hint);text-align:center}
.lang button.on{background:var(--bg);color:var(--fg);box-shadow:0 1px 3px rgba(0,0,0,.18)}
#tabs{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;background:var(--bg);border-top:1px solid var(--line);padding-bottom:var(--safe-b)}
#tabs:empty{display:none}
#tabs button{flex:1;height:var(--tab-h);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:11px;color:var(--hint);min-width:0;position:relative;text-align:center}
#tabs button span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
#tabs button.on{color:var(--accent)}
#tabs button.on::before{content:"";position:absolute;top:0;left:30%;right:30%;height:2px;background:var(--accent);border-radius:0 0 2px 2px}
h2.page{font:700 28px/1.15 var(--serif);margin:22px 0 4px}
h3.st{font:600 15px/1.3 var(--sans);margin:0 0 10px}
.hint{color:var(--hint);font-size:14px}
.lead{margin:10px 0 0;opacity:.9}
.row-h{margin:26px 0 6px}
.row-h h3{font:700 20px/1.2 var(--serif)}
.sp{height:14px}
.wel{padding-top:26px;text-align:center}
.wel .logo{width:68px;height:68px;border-radius:22px;margin:0 auto 14px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#fff;display:grid;place-items:center;box-shadow:var(--shadow)}
.wel h1{font:700 32px/1.1 var(--serif)}
.wel p{margin:8px auto 22px;color:var(--hint);max-width:300px}
.role{display:flex;gap:14px;align-items:center;width:100%;padding:18px 16px;border-radius:18px;background:var(--soft);margin:0 0 12px;box-shadow:var(--shadow)}
.role .bub{width:50px;height:50px;border-radius:16px;display:grid;place-items:center;flex:none;background:var(--bg);color:var(--accent)}
.role b{display:block;font:700 19px/1.2 var(--serif)}
.role small{display:block;color:var(--hint);font-size:13.5px;margin-top:3px}
.role .go{margin-left:auto;color:var(--hint);flex:none}
.pole{height:9px;background:repeating-linear-gradient(-45deg,var(--accent) 0 10px,#fff 10px 20px,var(--blue) 20px 30px,#fff 30px 40px)}
.pole.r{border-radius:99px;margin:0 auto 20px;width:96px}
.search{position:relative;margin:16px 0 0}
.search .ic{position:absolute;left:14px;top:14px;color:var(--hint)}
.search .inp{padding-left:42px;border-radius:14px;background:var(--soft);border-color:transparent}
.filters{display:flex;gap:8px;margin:12px 0 4px}
.fchip{padding:8px 14px;border-radius:99px;background:var(--soft);font-size:14px;font-weight:600}
.fchip.on{background:var(--fg);color:var(--bg)}
.bcard{display:grid;grid-template-columns:76px 1fr;gap:14px;width:100%;padding:12px;border-radius:18px;background:var(--soft);margin:0 0 12px;box-shadow:var(--shadow)}
.av{width:76px;height:76px;border-radius:16px;object-fit:cover;background:var(--line);display:block}
.av.ph,.hero-ph{display:grid;place-items:center;color:#fff;font:700 30px/1 var(--serif);background:linear-gradient(135deg,var(--blue),var(--accent))}
.bc-body{min-width:0}
.bc-name{font:700 18px/1.25 var(--serif);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bc-sub{font-size:13.5px;color:var(--hint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bc-row{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;margin-top:7px;font-size:13px}
.st-dot{display:inline-flex;align-items:center;gap:6px;font-weight:600}
.st-dot i{width:8px;height:8px;border-radius:50%;background:var(--bad);flex:none}
.st-dot.on i{background:var(--ok);box-shadow:0 0 0 3px rgba(35,134,79,.2)}
.tag{display:inline-flex;align-items:center;padding:2px 9px;border-radius:99px;background:var(--bg);font-size:12.5px;font-weight:600}
.tag{white-space:nowrap}
.tag.acc{background:var(--accent);color:var(--accent-fg)}
.price-f{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap}
.hero{margin:0 -16px;position:relative}
.hero img{display:block;width:100%;height:240px;object-fit:cover}
.hero .hero-ph{height:190px;font-size:64px;border-radius:0;width:100%}
.back-fab{position:absolute;left:12px;top:12px;width:40px;height:40px;border-radius:50%;background:rgba(0,0,0,.45);color:#fff;display:grid;place-items:center}
.shop{font:700 30px/1.12 var(--serif);margin:16px 0 0}
.owner{margin-top:6px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;color:var(--hint)}
.status{display:inline-flex;align-items:center;gap:8px;margin-top:12px;padding:6px 12px 6px 10px;border-radius:99px;background:var(--soft);font-size:13.5px;font-weight:600}
.status i{width:9px;height:9px;border-radius:50%;background:var(--bad);flex:none}
.status.on i{background:var(--ok);box-shadow:0 0 0 4px rgba(35,134,79,.22)}
.quick{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:18px 0 0}
.quick a,.quick button{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;min-height:66px;border-radius:14px;background:var(--soft);color:var(--fg);font-size:13px;font-weight:600;text-align:center}
.quick .x{opacity:.45;pointer-events:none}
.info{list-style:none;padding:0;margin:16px 0 0}
.info li{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--line)}
.info li:last-child{border-bottom:1px solid var(--line)}
.info .ic{margin-top:2px;color:var(--hint)}
.info details summary{cursor:pointer;list-style:none}
.info details summary::-webkit-details-marker{display:none}
.info details summary small{display:block;color:var(--blue);font-size:13px;margin-top:2px}
.wk{margin-top:8px;display:grid;gap:4px;font-size:14px}
.wk div{display:flex;justify-content:space-between;gap:16px;color:var(--hint)}
.wk div.today{color:var(--fg);font-weight:600}
.btn{display:flex;align-items:center;justify-content:center;gap:8px;min-height:50px;padding:0 20px;border-radius:14px;font-weight:600;background:var(--soft);text-align:center}
.btn.primary{background:linear-gradient(180deg,var(--accent2),var(--accent));color:var(--accent-fg);box-shadow:0 4px 14px rgba(184,50,42,.28)}
.btn.wide{width:100%}
.btn.small{min-height:38px;padding:0 14px;border-radius:10px;font-size:14px}
.btn.danger{color:var(--bad)}
.btn.primary.danger{color:var(--accent-fg)}
.btn[disabled]{opacity:.45;pointer-events:none}
.field{display:block;margin:0 0 14px}
.field>span{display:block;font-size:13px;color:var(--hint);margin:0 0 6px}
.inp{width:100%;min-height:48px;padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:transparent;outline:none}
.inp:focus{border-color:var(--accent)}
textarea.inp{min-height:92px;resize:vertical}
.row{display:flex;gap:10px;align-items:center}
.row>*{min-width:0}
.sw{position:relative;display:inline-block;width:46px;height:28px;flex:none}
.sw input{position:absolute;opacity:0;inset:0;width:100%;height:100%;margin:0;cursor:pointer;z-index:1}
.sw span{position:absolute;inset:0;border-radius:99px;background:var(--line);transition:background .15s}
.sw span::after{content:"";position:absolute;left:3px;top:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .15s;box-shadow:0 1px 2px rgba(0,0,0,.3)}
.sw input:checked+span{background:var(--ok)}
.sw input:checked+span::after{transform:translateX(18px)}
.sec{margin:26px 0 12px;font:700 20px/1.2 var(--serif)}
.sec small{display:block;font:400 14px/1.4 var(--sans);color:var(--hint);margin-top:4px}
.formcard{border-radius:16px;background:var(--soft);padding:16px 16px 4px;margin:0 0 6px}
.formcard .inp{background:var(--bg);border-color:transparent}
.pick{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.pick:last-child{border-bottom:0}
.pick .nm{font-weight:600}
.pick .vals{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:8px}
.pick .vals .inp{min-height:42px;padding:6px 12px}
.pick .vals label{font-size:12px;color:var(--hint)}
.photo-box{display:flex;gap:14px;align-items:center}
.photo-box .av{width:88px;height:88px;flex:none}
.board{margin:10px 0 0}
.svc{display:grid;grid-template-columns:auto 1fr auto;align-items:baseline;column-gap:8px;width:100%;padding:15px 0;border-bottom:1px solid var(--line)}
.svc-name{font:600 18px/1.25 var(--serif)}
.dots{border-bottom:2px dotted var(--line);transform:translateY(-5px);min-width:12px}
.svc-price{font:600 17px/1.25 var(--sans);font-variant-numeric:tabular-nums;white-space:nowrap}
.svc-sub{grid-column:1/-1;font-size:13px;color:var(--hint);margin-top:3px}
.svc.sel{box-shadow:inset 3px 0 0 var(--accent);padding-left:13px}
.svc.sel .svc-name{color:var(--accent)}
.svc.off .svc-name{text-decoration:line-through;opacity:.6}
.stepper{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:16px 0 0}
.stepper div{font-size:12px;color:var(--hint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stepper div::before{content:"";display:block;height:4px;border-radius:2px;background:var(--line);margin-bottom:6px}
.stepper div.done::before,.stepper div.now::before{background:var(--accent)}
.stepper div.now{color:var(--fg);font-weight:600}
.mini{display:flex;gap:12px;align-items:center;margin:14px 0 0;padding:10px 12px;border-radius:14px;background:var(--soft)}
.mini .av{width:44px;height:44px;border-radius:12px;font-size:18px}
.mini b{display:block;font:700 16px/1.2 var(--serif)}
.mini small{color:var(--hint)}
.step{margin:24px 0 0;scroll-margin-top:calc(var(--top-h) + 10px)}
.days{display:flex;gap:8px;overflow-x:auto;margin:0 -16px;padding:2px 16px 6px;scrollbar-width:none}
.days::-webkit-scrollbar{display:none}
.day{flex:none;width:54px;padding:8px 0 9px;border-radius:12px;border:1px solid var(--line);text-align:center}
.day small{display:block;font-size:12px;color:var(--hint)}
.day b{display:block;font:700 20px/1.2 var(--serif)}
.day.sel{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.day.sel small{color:inherit;opacity:.85}
.day.none{opacity:.38;border-style:dashed;pointer-events:none}
.part{font-size:13px;color:var(--hint);margin:14px 0 8px}
.part:first-child{margin-top:0}
.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.slot{min-height:46px;border-radius:12px;border:1px solid var(--line);text-align:center;display:grid;place-items:center;font-weight:600;font-variant-numeric:tabular-nums}
.slot.sel{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.empty{padding:26px 16px;border:1px dashed var(--line);border-radius:14px;text-align:center;color:var(--hint)}
.empty b{display:block;color:var(--fg);font:700 18px/1.3 var(--serif);margin-bottom:4px}
.empty .btn{margin:14px auto 0;max-width:260px}
.sum{border-radius:14px;background:var(--soft);padding:4px 16px;margin:0 0 16px}
.sum div{display:flex;justify-content:space-between;gap:16px;padding:10px 0;border-bottom:1px solid var(--line);font-size:15px}
.sum div:last-child{border-bottom:0;font-weight:700}
.sum span:first-child{color:var(--hint);font-weight:400}
.sum span:last-child{text-align:right}
.cta{position:fixed;left:0;right:0;bottom:calc(var(--tab-h) + var(--safe-b));z-index:15;padding:12px 16px;background:linear-gradient(to top,var(--bg) 70%,transparent)}
.cta .btn{max-width:528px;margin:0 auto}
.cta-sp{height:84px}
.ticket{position:relative;display:grid;grid-template-columns:88px 1fr;background:var(--soft);border-radius:8px;margin:0 0 14px}
.ticket::before,.ticket::after{content:"";position:absolute;left:81px;width:14px;height:14px;border-radius:50%;background:var(--bg)}
.ticket::before{top:-7px}.ticket::after{bottom:-7px}
.tk-l{padding:16px 6px 16px 14px;display:flex;flex-direction:column;justify-content:center}
.tk-time{font:700 25px/1 var(--serif);font-variant-numeric:lining-nums tabular-nums}
.tk-end{font-size:12px;color:var(--hint);margin-top:5px;font-variant-numeric:tabular-nums}
.tk-r{padding:14px 14px 14px 16px;border-left:2px dashed var(--line);min-width:0}
.tk-name{font:600 17px/1.3 var(--serif)}
.tk-shop{font-size:14px;margin-top:2px}
.tk-meta{font-size:14px;color:var(--hint);margin-top:2px}
.tk-price{font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
.tk-in{font-size:13px;font-weight:600;color:var(--ok);margin-top:4px}
.tk-act{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid currentColor;margin-left:6px;vertical-align:2px}
.badge.bad{color:var(--bad)}.badge.mut{color:var(--hint)}
.ticket.off{opacity:.62}
.ticket.off .tk-name{text-decoration:line-through}
.ticket.big{grid-template-columns:112px 1fr;animation:stamp .26s ease-out}
.ticket.big::before,.ticket.big::after{left:105px}
.ticket.big .tk-time{font-size:34px}
@keyframes stamp{from{transform:scale(1.1) rotate(-1.5deg);opacity:0}to{transform:none;opacity:1}}
.done-h{display:flex;gap:10px;align-items:center;margin:26px 0 16px}
.done-h .ok-dot{width:34px;height:34px;border-radius:50%;background:var(--ok);color:#fff;display:grid;place-items:center;flex:none}
.seg{display:flex;gap:8px;overflow-x:auto;margin:14px -16px 6px;padding:2px 16px;scrollbar-width:none}
.seg::-webkit-scrollbar{display:none}
.seg button{flex:none;padding:9px 16px;border-radius:99px;background:var(--soft);font-weight:600;font-size:14px}
.seg button.on{background:var(--fg);color:var(--bg)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0 4px}
.stat{border-radius:14px;background:var(--soft);padding:12px 12px 10px;text-align:left}
.stat small{display:block;font-size:12px;color:var(--hint)}
.stat b{display:block;font:700 26px/1.15 var(--serif)}
.stat span{display:block;font-size:12px;color:var(--hint);white-space:nowrap}
.stat.on{outline:2px solid var(--accent)}
.nav{display:flex;gap:8px;align-items:center;margin:14px 0 4px}
.nav .inp{min-height:40px;padding:4px 10px;flex:1;text-align:center}
.nav .arr{width:40px;height:40px;border-radius:10px;background:var(--soft);display:grid;place-items:center;flex:none;font-size:20px;line-height:1}
.hr{padding:14px 0;border-bottom:1px solid var(--line)}
.hr-top{display:flex;justify-content:space-between;align-items:center}
.hr-t{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center;margin-top:10px}
.hr-t .inp{min-height:42px;padding:6px 10px}
.hr-lb{font-size:13px;color:var(--hint);margin-top:10px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.chip{display:inline-flex;gap:6px;align-items:center;padding:6px 8px 6px 12px;border-radius:99px;background:var(--soft);font-size:14px}
.chip button{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;color:var(--hint)}
.menu{margin:18px 0 0;border-radius:16px;background:var(--soft);overflow:hidden}
.menu button{display:flex;gap:12px;align-items:center;width:100%;padding:15px 16px;border-bottom:1px solid var(--line);color:var(--fg);font-weight:600}
.menu button:last-child{border-bottom:0}
.menu small{display:block;font-weight:400;color:var(--hint);font-size:13px}
.menu .ic{color:var(--accent)}
.mrow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid var(--line)}
.sk{height:64px;border-radius:12px;background:linear-gradient(90deg,var(--soft),var(--line),var(--soft));background-size:200% 100%;margin:0 0 12px;animation:shim 1.2s linear infinite}
@keyframes shim{from{background-position:200% 0}to{background-position:-200% 0}}
.back{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:40;display:flex;align-items:flex-end;justify-content:center}
.sheet{width:100%;max-width:560px;max-height:90vh;overflow:auto;background:var(--bg);border-radius:20px 20px 0 0;padding:8px 16px calc(20px + var(--safe-b));animation:up .2s ease-out}
.sheet .grab{width:40px;height:4px;border-radius:2px;background:var(--line);margin:4px auto 14px}
@keyframes up{from{transform:translateY(40px);opacity:.4}to{transform:none;opacity:1}}
#toast{position:fixed;left:16px;right:16px;bottom:calc(var(--tab-h) + var(--safe-b) + 84px);z-index:60;display:flex;justify-content:center;pointer-events:none}
#toast div{max-width:420px;padding:11px 16px;border-radius:12px;background:var(--fg);color:var(--bg);font-size:14px;box-shadow:0 6px 24px rgba(0,0,0,.25)}
.splash{padding-top:90px;text-align:center;color:var(--hint)}
.hello{padding-top:0}
.hello .pole{margin:0 -16px;height:6px}
.hello h2.page{margin-top:18px}
.cnts{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;min-height:24px}
.tag.open{background:rgba(35,134,79,.16);color:var(--ok)}
.bcard{transition:transform .12s,box-shadow .12s;position:relative;overflow:hidden}
.bcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--line)}
.bcard:has(.st-dot.on)::before{background:var(--ok)}
.bcard .av{box-shadow:0 2px 8px rgba(0,0,0,.12)}
.role{transition:transform .12s}
.role:active{transform:scale(.98)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:340px){.slots{grid-template-columns:repeat(3,1fr)}.stats{grid-template-columns:1fr 1fr}}
</style>
</head>
<body>
<header id="top"><div class="brand"><div class="mk"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/></svg></div><span id="brand">Barber</span></div><div class="lang" id="lang"></div></header>
<main id="view"><div class="splash"><svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/></svg></div></main>
<div id="cta-root"></div>
<nav id="tabs"></nav>
<div id="sheet-root"></div>
<div id="toast"></div>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
(function(){
'use strict';
var tg=window.Telegram&&window.Telegram.WebApp;
var root=document.documentElement;

var I18N={
uz:{
 app_name:"Sartaroshlar",loading:"Yuklanmoqda...",open_in_tg:"Ilovani Telegram ichida oching",err_generic:"Xatolik yuz berdi",err_net:"Internet bilan aloqa yo'q",
 not_opened:"Ochilmadi",reopen_hint:"Telegramdagi bot orqali ilovani qayta oching.",retry:"Qayta urinish",back:"Orqaga",
 w_title:"Xush kelibsiz",w_sub:"Kim sifatida davom etasiz?",w_client:"Men mijozman",w_client_d:"Sartarosh tanlayman va vaqt band qilaman",
 w_barber:"Men sartaroshman",w_barber_d:"O'z salonimni ro'yxatdan o'tkazaman, mijozlar bron qiladi",
 reg_c_title:"Mijoz sifatida ro'yxatdan o'tish",reg_c_sub:"Bron qilishda ma'lumotlaringiz o'zi to'ldiriladi.",continue:"Davom etish",
 reg_b_title:"Sartarosh sifatida ro'yxatdan o'tish",reg_b_sub:"Bu ma'lumotlar mijozlarga ko'rinadi.",
 sec_shop:"Salon",sec_you:"Siz haqingizda",sec_services:"Xizmatlar va narxlar",svc_hint:"Qiladigan xizmatlaringizni belgilab, narx va davomiylikni kiriting.",
 f_shop:"Salon nomi",f_owner:"Ismingiz",f_addr:"Manzil",f_phone:"Telefon",f_exp:"Tajriba (yil)",f_bio:"O'zingiz haqingizda",
 f_bio_ph:"Masalan: 7 yillik tajriba, fade va klassik soch turmaklari ustasi",f_photo:"Rasm (ixtiyoriy)",
 register:"Ro'yxatdan o'tish",reg_done:"Tayyor! Profilingiz mijozlarga ko'rinadi.",pick_one:"Kamida bitta xizmat tanlang.",
 tab_barbers:"Sartaroshlar",tab_my:"Bronlarim",tab_profile:"Profil",tab_b_book:"Bronlar",tab_b_svc:"Xizmatlar",tab_b_hours:"Ish vaqti",
 list_title:"Sartaroshlar",hello:"Salom, {n}!",list_sub:"O'zingizga mos sartaroshni tanlang",cnt_all:"{n} ta sartarosh",cnt_open:"{n} ta hozir ochiq",search_ph:"Nomi yoki manzil bo'yicha qidirish",f_open:"Hozir ochiq",f_all:"Hammasi",closed:"Yopiq",
 no_barbers:"Hozircha sartaroshlar yo'q",no_barbers_t:"Birinchi bo'lib ro'yxatdan o'ting yoki keyinroq qaytib keling.",
 nothing_found:"Hech narsa topilmadi",nothing_found_t:"Boshqa so'z bilan qidirib ko'ring.",
 from_price:"{p} dan",exp_y:"{n} yil tajriba",new_b:"Yangi",next_free:"Bo'sh: {d} {t}",
 open_now:"Hozir ochiq · {t} gacha",opens_today:"Bugun {t} da ochiladi",closed_next:"Yopiq · {d} {t} da ochiladi",closed_nodata:"Hozir yopiq",
 hours_title:"Ish vaqti",week_sched:"Haftalik jadval",call:"Qo'ng'iroq",route:"Yo'l",share:"Ulashish",
 choose_service:"Xizmatni tanlang",choose_service_h:"Narxlar so'mda. Xizmatni bosing va vaqt tanlang.",no_svc_b:"Bu sartarosh hali xizmat qo'shmagan",
 share_text:"{shop} — vaqt band qiling",
 som:"so'm",min:"daqiqa",hour_s:"soat",day_s:"kun",today:"Bugun",tomorrow:"Ertaga",day_off:"Dam olish",
 book_title:"Band qilish",st_service:"Xizmat",st_date:"Sana",st_time:"Vaqt",st_info:"Ma'lumotlaringiz",st_info_s:"Ma'lumot",
 no_days:"Bo'sh kunlar yo'q",no_days_txt:"Hozircha bu xizmat uchun bo'sh vaqt topilmadi. Keyinroq qayta urinib ko'ring.",
 no_slots:"Bu kunda bo'sh vaqt yo'q",no_slots_txt:"Boshqa sanani tanlang.",
 part_morning:"Ertalab",part_day:"Kunduzi",part_eve:"Kechqurun",your_name:"Ismingiz",your_phone:"Telefon raqamingiz",
 sum_price:"Narx",sum_dur:"Davomiyligi",sum_barber:"Sartarosh",cta_fill:"Ism va telefonni kiriting",cta_book:"Tasdiqlash · {p}",sending:"Yuborilmoqda...",
 done_title:"Bron tasdiqlandi",done_txt:"Tasdiqlash xabari Telegramga yuborildi. Sartaroshga ham xabar bordi. Vaqtidan oldin eslatamiz.",ends:"tugash",book_again:"Yana band qilish",
 my_title:"Bronlarim",tab_upcoming:"Kelgusi",tab_past:"Avvalgilar",no_bookings:"Hali bron yo'q",no_bookings_txt:"Sartaroshni tanlang va birinchi vaqtingizni band qiling.",
 find_barber:"Sartarosh tanlash",no_upcoming:"Kelgusi bronlar yo'q.",no_past:"Avvalgi bronlar yo'q.",cancel:"Bekor qilish",cancelled:"Bekor qilingan",past_b:"O'tgan",
 cancel_q:"Bronni bekor qilasizmi?",cancel_q_admin:"Bronni bekor qilasizmi? Mijozga xabar yuboriladi.",cancelled_toast:"Bron bekor qilindi",
 in_d:"{d} kundan keyin",in_hm:"{h} soat {m} daqiqadan keyin",in_h:"{h} soatdan keyin",in_m:"{m} daqiqadan keyin",
 prof_title:"Profil",lang_label:"Til",become_barber:"Sartarosh sifatida ro'yxatdan o'tish",become_barber_d:"Salon oching va mijozlardan bron qabul qiling.",
 switch_barber:"Sartarosh paneliga o'tish",switch_client:"Mijoz rejimiga o'tish",moderation:"Moderatsiya",hide_b:"Yashirish",show_b:"Ko'rsatish",hidden_b:"Yashirin",
 b_title:"Sartarosh paneli",ov_later:"Keyinroq",ov_bk:"bron",active_n:"Faol bronlar: {n}",no_day_bookings:"Bu kunga bron yo'q",no_day_txt:"Yangi bron bo'lsa, Telegramda xabar keladi.",
 hidden:"yashirin",new_service:"Yangi xizmat",edit_service:"Xizmatni tahrirlash",f_kind:"Xizmat turi",f_custom:"Boshqa (o'z nomim)",f_name:"Nomi",
 f_price:"Narxi (so'm)",f_dur:"Davomiyligi (daqiqa)",f_visible:"Mijozlarga ko'rsatilsin",
 save:"Saqlash",saved:"Saqlandi",deleted:"O'chirildi",del_service:"Xizmatni o'chirish",del_service_q:"Xizmat o'chirilsin? Oldingi bronlar saqlanib qoladi.",
 save_hours:"Ish vaqtini saqlash",break_opt:"Tanaffus (ixtiyoriy)",days_off:"Dam olish kunlari",days_off_hint:"Bayram yoki boshqa sabab bilan yopiq kunlar.",
 add:"Qo'shish",remove:"O'chirish",none_yet:"Hozircha yo'q",pick_date:"Sanani tanlang",
 photo:"Rasm",choose_photo:"Rasm tanlash",remove_photo:"Olib tashlash",photo_uploading:"Rasm yuklanmoqda...",photo_saved:"Rasm saqlandi",
 photo_removed:"Rasm olib tashlandi",photo_read_err:"Rasmni o'qib bo'lmadi",photo_fmt_err:"Rasm formati qo'llab-quvvatlanmaydi",photo_big:"Rasm juda katta",
 s_step:"Bron vaqti qadami",s_remind:"Eslatma: bronga necha soat qolganda",s_horizon:"Necha kun oldindan band qilish mumkin",share_link:"Sahifamni ulashish",
 wd:["Dushanba","Seshanba","Chorshanba","Payshanba","Juma","Shanba","Yakshanba"],wds:["Du","Se","Ch","Pa","Ju","Sh","Ya"],
 mon:["yanvar","fevral","mart","aprel","may","iyun","iyul","avgust","sentabr","oktabr","noyabr","dekabr"]
},
ru:{
 app_name:"Барберы",loading:"Загрузка...",open_in_tg:"Откройте приложение в Telegram",err_generic:"Произошла ошибка",err_net:"Нет подключения к интернету",
 not_opened:"Не открылось",reopen_hint:"Откройте приложение заново через бота в Telegram.",retry:"Повторить",back:"Назад",
 w_title:"Добро пожаловать",w_sub:"Как вы хотите продолжить?",w_client:"Я клиент",w_client_d:"Выбираю барбера и записываюсь",
 w_barber:"Я барбер",w_barber_d:"Регистрирую свой салон — клиенты записываются сами",
 reg_c_title:"Регистрация клиента",reg_c_sub:"Данные подставятся при записи автоматически.",continue:"Продолжить",
 reg_b_title:"Регистрация барбера",reg_b_sub:"Эти данные увидят клиенты.",
 sec_shop:"Салон",sec_you:"О вас",sec_services:"Услуги и цены",svc_hint:"Отметьте ваши услуги и укажите цену и длительность.",
 f_shop:"Название салона",f_owner:"Ваше имя",f_addr:"Адрес",f_phone:"Телефон",f_exp:"Стаж (лет)",f_bio:"О себе",
 f_bio_ph:"Например: 7 лет опыта, мастер фейда и классических стрижек",f_photo:"Фото (необязательно)",
 register:"Зарегистрироваться",reg_done:"Готово! Ваш профиль виден клиентам.",pick_one:"Выберите хотя бы одну услугу.",
 tab_barbers:"Барберы",tab_my:"Мои записи",tab_profile:"Профиль",tab_b_book:"Записи",tab_b_svc:"Услуги",tab_b_hours:"Часы",
 list_title:"Барберы",hello:"Привет, {n}!",list_sub:"Выберите подходящего барбера",cnt_all:"Барберов: {n}",cnt_open:"Открыто сейчас: {n}",search_ph:"Поиск по названию или адресу",f_open:"Открыто сейчас",f_all:"Все",closed:"Закрыто",
 no_barbers:"Барберов пока нет",no_barbers_t:"Зарегистрируйтесь первым или загляните позже.",
 nothing_found:"Ничего не найдено",nothing_found_t:"Попробуйте другой запрос.",
 from_price:"от {p}",exp_y:"Стаж {n} лет",new_b:"Новый",next_free:"Свободно: {d} {t}",
 open_now:"Открыто · до {t}",opens_today:"Сегодня откроется в {t}",closed_next:"Закрыто · откроется {d} в {t}",closed_nodata:"Сейчас закрыто",
 hours_title:"Часы работы",week_sched:"График на неделю",call:"Позвонить",route:"Маршрут",share:"Поделиться",
 choose_service:"Выберите услугу",choose_service_h:"Цены в сумах. Выберите услугу и время.",no_svc_b:"Барбер ещё не добавил услуги",
 share_text:"{shop} — запишитесь онлайн",
 som:"сум",min:"мин",hour_s:"ч",day_s:"дн.",today:"Сегодня",tomorrow:"Завтра",day_off:"Выходной",
 book_title:"Запись",st_service:"Услуга",st_date:"Дата",st_time:"Время",st_info:"Ваши данные",st_info_s:"Данные",
 no_days:"Нет свободных дней",no_days_txt:"Пока нет свободного времени для этой услуги. Загляните позже.",
 no_slots:"В этот день нет свободного времени",no_slots_txt:"Выберите другую дату.",
 part_morning:"Утро",part_day:"День",part_eve:"Вечер",your_name:"Ваше имя",your_phone:"Ваш телефон",
 sum_price:"Цена",sum_dur:"Длительность",sum_barber:"Барбер",cta_fill:"Введите имя и телефон",cta_book:"Подтвердить · {p}",sending:"Отправка...",
 done_title:"Запись подтверждена",done_txt:"Подтверждение отправлено в Telegram, барбер тоже получил уведомление. Мы напомним перед визитом.",ends:"конец",book_again:"Записаться снова",
 my_title:"Мои записи",tab_upcoming:"Предстоящие",tab_past:"Прошлые",no_bookings:"Записей пока нет",no_bookings_txt:"Выберите барбера и запишитесь на первое время.",
 find_barber:"Выбрать барбера",no_upcoming:"Предстоящих записей нет.",no_past:"Прошлых записей нет.",cancel:"Отменить",cancelled:"Отменена",past_b:"Прошла",
 cancel_q:"Отменить запись?",cancel_q_admin:"Отменить запись? Клиент получит уведомление.",cancelled_toast:"Запись отменена",
 in_d:"через {d} дн.",in_hm:"через {h} ч {m} мин",in_h:"через {h} ч",in_m:"через {m} мин",
 prof_title:"Профиль",lang_label:"Язык",become_barber:"Зарегистрироваться как барбер",become_barber_d:"Откройте салон и принимайте записи от клиентов.",
 switch_barber:"Перейти в панель барбера",switch_client:"Перейти в режим клиента",moderation:"Модерация",hide_b:"Скрыть",show_b:"Показать",hidden_b:"Скрыт",
 b_title:"Панель барбера",ov_later:"Позже",ov_bk:"записей",active_n:"Активных записей: {n}",no_day_bookings:"На этот день записей нет",no_day_txt:"О новой записи придёт сообщение в Telegram.",
 hidden:"скрыта",new_service:"Новая услуга",edit_service:"Редактировать услугу",f_kind:"Тип услуги",f_custom:"Другое (своё название)",f_name:"Название",
 f_price:"Цена (сум)",f_dur:"Длительность (мин)",f_visible:"Показывать клиентам",
 save:"Сохранить",saved:"Сохранено",deleted:"Удалено",del_service:"Удалить услугу",del_service_q:"Удалить услугу? Прошлые записи сохранятся.",
 save_hours:"Сохранить часы работы",break_opt:"Перерыв (необязательно)",days_off:"Выходные дни",days_off_hint:"Дни, когда салон закрыт: праздники и т. д.",
 add:"Добавить",remove:"Удалить",none_yet:"Пока нет",pick_date:"Выберите дату",
 photo:"Фото",choose_photo:"Выбрать фото",remove_photo:"Убрать",photo_uploading:"Загрузка фото...",photo_saved:"Фото сохранено",
 photo_removed:"Фото удалено",photo_read_err:"Не удалось прочитать фото",photo_fmt_err:"Формат фото не поддерживается",photo_big:"Фото слишком большое",
 s_step:"Шаг времени записи",s_remind:"Напоминание: за сколько часов до записи",s_horizon:"На сколько дней вперёд можно записаться",share_link:"Поделиться моей страницей",
 wd:["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"],wds:["Пн","Вт","Ср","Чт","Пт","Сб","Вс"],
 mon:["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
},
en:{
 app_name:"Barbers",loading:"Loading...",open_in_tg:"Open the app inside Telegram",err_generic:"Something went wrong",err_net:"No internet connection",
 not_opened:"Couldn't open",reopen_hint:"Reopen the app from the bot in Telegram.",retry:"Try again",back:"Back",
 w_title:"Welcome",w_sub:"How would you like to continue?",w_client:"I'm a client",w_client_d:"I'll pick a barber and book a time",
 w_barber:"I'm a barber",w_barber_d:"I'll register my shop and get bookings",
 reg_c_title:"Client registration",reg_c_sub:"We'll prefill your details when you book.",continue:"Continue",
 reg_b_title:"Barber registration",reg_b_sub:"Clients will see these details.",
 sec_shop:"Shop",sec_you:"About you",sec_services:"Services & prices",svc_hint:"Tick your services and set price and duration.",
 f_shop:"Shop name",f_owner:"Your name",f_addr:"Address",f_phone:"Phone",f_exp:"Experience (years)",f_bio:"About you",
 f_bio_ph:"E.g. 7 years of experience, fades and classic cuts",f_photo:"Photo (optional)",
 register:"Register",reg_done:"Done! Your profile is now visible to clients.",pick_one:"Pick at least one service.",
 tab_barbers:"Barbers",tab_my:"My bookings",tab_profile:"Profile",tab_b_book:"Bookings",tab_b_svc:"Services",tab_b_hours:"Hours",
 list_title:"Barbers",hello:"Hi, {n}!",list_sub:"Pick the barber that suits you",cnt_all:"{n} barbers",cnt_open:"{n} open now",search_ph:"Search by name or address",f_open:"Open now",f_all:"All",closed:"Closed",
 no_barbers:"No barbers yet",no_barbers_t:"Register first, or check back later.",
 nothing_found:"Nothing found",nothing_found_t:"Try a different search.",
 from_price:"from {p}",exp_y:"{n} yrs experience",new_b:"New",next_free:"Free: {d} {t}",
 open_now:"Open now · until {t}",opens_today:"Opens today at {t}",closed_next:"Closed · opens {d} at {t}",closed_nodata:"Closed now",
 hours_title:"Working hours",week_sched:"Weekly hours",call:"Call",route:"Directions",share:"Share",
 choose_service:"Choose a service",choose_service_h:"Prices in UZS. Tap a service to pick a time.",no_svc_b:"This barber hasn't added services yet",
 share_text:"{shop} — book online",
 som:"UZS",min:"min",hour_s:"h",day_s:"days",today:"Today",tomorrow:"Tomorrow",day_off:"Closed",
 book_title:"Book a time",st_service:"Service",st_date:"Date",st_time:"Time",st_info:"Your details",st_info_s:"Details",
 no_days:"No free days",no_days_txt:"No free time for this service yet. Please check back later.",
 no_slots:"No free time on this day",no_slots_txt:"Pick another date.",
 part_morning:"Morning",part_day:"Afternoon",part_eve:"Evening",your_name:"Your name",your_phone:"Your phone number",
 sum_price:"Price",sum_dur:"Duration",sum_barber:"Barber",cta_fill:"Enter your name and phone",cta_book:"Confirm · {p}",sending:"Sending...",
 done_title:"Booking confirmed",done_txt:"A confirmation was sent to Telegram and the barber was notified too. We'll remind you before your visit.",ends:"ends",book_again:"Book again",
 my_title:"My bookings",tab_upcoming:"Upcoming",tab_past:"Past",no_bookings:"No bookings yet",no_bookings_txt:"Pick a barber and book your first time.",
 find_barber:"Find a barber",no_upcoming:"No upcoming bookings.",no_past:"No past bookings.",cancel:"Cancel",cancelled:"Cancelled",past_b:"Past",
 cancel_q:"Cancel this booking?",cancel_q_admin:"Cancel this booking? The client will be notified.",cancelled_toast:"Booking cancelled",
 in_d:"in {d} days",in_hm:"in {h} h {m} min",in_h:"in {h} h",in_m:"in {m} min",
 prof_title:"Profile",lang_label:"Language",become_barber:"Register as a barber",become_barber_d:"Set up your shop and take bookings from clients.",
 switch_barber:"Switch to barber panel",switch_client:"Switch to client mode",moderation:"Moderation",hide_b:"Hide",show_b:"Show",hidden_b:"Hidden",
 b_title:"Barber panel",ov_later:"Later",ov_bk:"bookings",active_n:"Active bookings: {n}",no_day_bookings:"No bookings for this day",no_day_txt:"You'll get a Telegram message when someone books.",
 hidden:"hidden",new_service:"New service",edit_service:"Edit service",f_kind:"Service type",f_custom:"Other (my own name)",f_name:"Name",
 f_price:"Price (UZS)",f_dur:"Duration (min)",f_visible:"Visible to clients",
 save:"Save",saved:"Saved",deleted:"Deleted",del_service:"Delete service",del_service_q:"Delete this service? Past bookings are kept.",
 save_hours:"Save working hours",break_opt:"Break (optional)",days_off:"Days off",days_off_hint:"Days the shop is closed: holidays and so on.",
 add:"Add",remove:"Remove",none_yet:"None yet",pick_date:"Pick a date",
 photo:"Photo",choose_photo:"Choose photo",remove_photo:"Remove",photo_uploading:"Uploading photo...",photo_saved:"Photo saved",
 photo_removed:"Photo removed",photo_read_err:"Couldn't read the photo",photo_fmt_err:"Unsupported photo format",photo_big:"Photo is too large",
 s_step:"Booking time step",s_remind:"Reminder: hours before the booking",s_horizon:"How many days ahead clients can book",share_link:"Share my page",
 wd:["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],wds:["Mon","Tue","Wed","Thu","Fri","Sat","Sun"],
 mon:["January","February","March","April","May","June","July","August","September","October","November","December"]
}};
var LANG='uz';
function detectLang(){
  try{var s=localStorage.getItem('lang');if(s&&I18N[s])return s}catch(e){}
  var c=((tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user&&tg.initDataUnsafe.user.language_code)||'').toLowerCase();
  if(c.indexOf('ru')===0)return 'ru';
  if(c.indexOf('en')===0)return 'en';
  return 'uz';
}
function t(k,v){
  var s=I18N[LANG][k];if(s===undefined)s=I18N.uz[k];if(s===undefined)return k;
  if(v)for(var x in v)s=s.split('{'+x+'}').join(v[x]);
  return s;
}
LANG=detectLang();root.lang=LANG;

function applyScheme(){
  var s=(tg&&tg.colorScheme)||((window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light');
  root.dataset.scheme=s;
}
applyScheme();
if(tg){try{
  tg.ready();tg.expand();
  if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();
  if(tg.setHeaderColor)tg.setHeaderColor('bg_color');
  if(tg.setBackgroundColor)tg.setBackgroundColor('bg_color');
  tg.onEvent('themeChanged',applyScheme);
}catch(e){}}
function hap(kind){try{if(tg&&tg.HapticFeedback){kind==='ok'?tg.HapticFeedback.notificationOccurred('success'):tg.HapticFeedback.impactOccurred('light')}}catch(e){}}

function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function money(n){return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
function parseISO(s){var p=s.split('-').map(Number);return new Date(p[0],p[1]-1,p[2])}
function iso(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function addDays(s,n){var d=parseISO(s);d.setDate(d.getDate()+n);return iso(d)}
function wdi(d){return (d.getDay()+6)%7}
function hmMin(x){var a=String(x).split(':');return (+a[0])*60+(+a[1])}
function dShort(s){
  var d=parseISO(s),m=I18N[LANG].mon[d.getMonth()];
  return LANG==='uz'?d.getDate()+'-'+m:LANG==='ru'?d.getDate()+' '+m:m+' '+d.getDate();
}
function dFull(s){
  var d=parseISO(s),w=I18N[LANG].wd[wdi(d)];
  if(LANG==='en')return w+', '+dShort(s);
  return dShort(s)+', '+w.toLowerCase();
}
function dRel(s){
  var n=iso(shopNow());
  if(s===n)return t('today');
  if(s===addDays(n,1))return t('tomorrow');
  return dFull(s);
}
function dTiny(s){
  var n=iso(shopNow());
  if(s===n)return t('today');
  if(s===addDays(n,1))return t('tomorrow');
  var d=parseISO(s);return I18N[LANG].wds[wdi(d)]+', '+d.getDate();
}
function $(s,r){return (r||document).querySelector(s)}
var ICONS={
  scissors:'<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
  ticket:'<path d="M3 9a2 2 0 0 0 0 6v3h18v-3a2 2 0 0 1 0-6V6H3z"/><path d="M14 6v12" stroke-dasharray="2 3"/>',
  user:'<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
  pin:'<path d="M12 21s7-6.2 7-11a7 7 0 0 0-14 0c0 4.8 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  phone:'<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/>',
  route:'<path d="M12 21s6-5.4 6-10a6 6 0 0 0-12 0c0 4.6 6 10 6 10z"/><circle cx="12" cy="11" r="2"/>',
  share:'<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 10.6 6.8-4M8.6 13.4l6.8 4"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  chev:'<path d="m9 6 6 6-6 6"/>',
  back:'<path d="m15 6-6 6 6 6"/>',
  check:'<path d="m5 12.5 4.5 4.5L19 7"/>',
  x:'<path d="M6 6l12 12M18 6 6 18"/>',
  swap:'<path d="M7 7h12l-3-3M17 17H5l3 3"/>',
  shield:'<path d="M12 3 20 6v6c0 5-3.4 8-8 9-4.6-1-8-4-8-9V6z"/><path d="m9 12 2 2 4-4"/>'
};
function ic(n,s){s=s||22;return '<svg class="ic" width="'+s+'" height="'+s+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+ICONS[n]+'</svg>'}
function toast(msg){
  var el=$('#toast');el.innerHTML='<div>'+esc(msg)+'</div>';
  clearTimeout(toast.t);toast.t=setTimeout(function(){el.innerHTML=''},3200);
}
function ask(msg,cb){
  if(tg&&tg.showConfirm){tg.showConfirm(msg,function(ok){if(ok)cb()})}
  else if(window.confirm(msg)){cb()}
}
function api(path,body){
  return fetch('/api/'+path,{method:'POST',
    headers:{'Content-Type':'application/json','X-Init-Data':tg?tg.initData:'','X-Lang':LANG},
    body:JSON.stringify(body||{})
  }).then(function(r){
    return r.json().catch(function(){return {}}).then(function(j){
      if(!r.ok){var e=new Error(j.error||t('err_generic'));e.status=r.status;throw e}
      return j;
    });
  },function(){var e=new Error(t('err_net'));e.status=0;throw e});
}
function fail(e){toast(e&&e.message?e.message:t('err_generic'))}

var S={init:null,me:null,mode:'client',v:'boot',stack:[],err:null,
  barbers:[],q:'',onlyOpen:false,bcache:{},cur:null,
  book:null,my:null,myTab:'up',reg:null,cp:{name:'',phone:''},bp:null,
  b:{date:null,rows:null,ov:null,services:null,hours:null,daysOff:[],offPick:''},moder:null,catalog:[]};
var view=$('#view');

function shopNow(){
  if(!S.init||!S.init.now)return new Date();
  var p=S.init.now.split(/[- :]/).map(Number);
  var d=new Date(p[0],p[1]-1,p[2],p[3],p[4]);
  return new Date(d.getTime()+(Date.now()-S.init.now_ts));
}
function stText(st){
  if(!st)return '';
  if(st.o)return t('open_now',{t:st.t});
  if(!st.d)return t('closed_nodata');
  var today=iso(shopNow());
  if(st.d===today)return t('opens_today',{t:st.t});
  return t('closed_next',{d:st.d===addDays(today,1)?t('tomorrow').toLowerCase():dFull(st.d),t:st.t});
}
function until(b){
  var p=(b.date+' '+b.time).split(/[- :]/).map(Number);
  var mins=Math.round((new Date(p[0],p[1]-1,p[2],p[3],p[4])-shopNow())/60000);
  if(mins<=0)return '';
  if(mins>=2880)return t('in_d',{d:Math.floor(mins/1440)});
  var h=Math.floor(mins/60),m=mins%60;
  if(!h)return t('in_m',{m:m});
  return m?t('in_hm',{h:h,m:m}):t('in_h',{h:h});
}
function initial(s){return esc((String(s||'?').trim()[0]||'?').toUpperCase())}
function hasPh(b){return b.photo_v&&b.photo_v!=='0'}
function avatar(b){
  if(hasPh(b))return '<img class="av" loading="lazy" decoding="async" src="/thumb/'+b.id+'?v='+esc(b.photo_v)+'" alt="">';
  return '<div class="av ph">'+initial(b.shop)+'</div>';
}
function empty(title,text,btn){return '<div class="empty"><b>'+esc(title)+'</b>'+esc(text||'')+(btn||'')+'</div>'}
function field(label,bind,val,o){
  o=o||{};
  var inp=o.area?'<textarea class="inp" data-bind="'+bind+'" maxlength="'+(o.max||600)+'" placeholder="'+esc(o.ph||'')+'">'+esc(val)+'</textarea>'
    :'<input class="inp" data-bind="'+bind+'" '+(o.type?'type="'+o.type+'" ':'')+(o.mode?'inputmode="'+o.mode+'" ':'')+(o.ac?'autocomplete="'+o.ac+'" ':'')+'maxlength="'+(o.max||100)+'" placeholder="'+esc(o.ph||'')+'" value="'+esc(val)+'">';
  return '<label class="field"><span>'+label+'</span>'+inp+'</label>';
}
function phoneOk(p){var d=String(p||'').replace(/\D/g,'');return d.length>=9&&d.length<=15}
function telHref(p){return 'tel:'+esc(String(p).replace(/[^\d+]/g,''))}
var PH='+998 90 123 45 67';

/* ---- kirish / ro'yxatdan o'tish ---- */
function viewWelcome(){
  return '<div class="wel"><div class="logo">'+ic('scissors',34)+'</div><h1>'+t('w_title')+'</h1><p>'+t('w_sub')+'</p></div>'+
    '<button class="role" data-act="pick-role" data-r="client"><span class="bub">'+ic('user',26)+'</span><span><b>'+t('w_client')+'</b><small>'+t('w_client_d')+'</small></span><span class="go">'+ic('chev',20)+'</span></button>'+
    '<button class="role" data-act="pick-role" data-r="barber"><span class="bub">'+ic('scissors',26)+'</span><span><b>'+t('w_barber')+'</b><small>'+t('w_barber_d')+'</small></span><span class="go">'+ic('chev',20)+'</span></button>';
}
function viewRegC(){
  var c=S.cp,ok=c.name.trim().length>=2&&phoneOk(c.phone);
  return '<div class="wel"><div class="pole r"></div></div><h2 class="page" style="margin-top:0">'+t('reg_c_title')+'</h2><div class="hint">'+t('reg_c_sub')+'</div><div class="sp"></div>'+
    '<div class="formcard">'+field(t('your_name'),'cp.name',c.name,{ac:'name',max:60})+field(t('your_phone'),'cp.phone',c.phone,{type:'tel',mode:'tel',ac:'tel',max:20,ph:PH})+'</div>'+
    '<div class="sp"></div><button class="btn primary wide" id="rc-go" data-act="reg-c" '+(ok?'':'disabled')+'>'+t('continue')+'</button>'+
    '<div style="margin-top:10px"><button class="btn wide" data-act="to-welcome">'+t('back')+'</button></div>';
}
function regPhotoHtml(){
  var r=S.reg;
  return '<div class="photo-box">'+(r.photo?'<img class="av" src="'+r.thumb+'" alt="">':'<div class="av ph">'+initial(r.shop||'?')+'</div>')+
    '<label class="btn small" style="cursor:pointer">'+t('choose_photo')+'<input type="file" accept="image/*" data-bind="reg-photo" hidden></label></div>';
}
function viewRegB(){
  var r=S.reg;
  var h='<div class="wel"><div class="pole r"></div></div><h2 class="page" style="margin-top:0">'+t('reg_b_title')+'</h2><div class="hint">'+t('reg_b_sub')+'</div>';
  h+='<div class="sec">'+t('sec_shop')+'</div><div class="formcard">'+field(t('f_shop'),'reg.shop',r.shop,{max:80})+field(t('f_addr'),'reg.address',r.address,{max:200,ac:'street-address'})+field(t('f_phone'),'reg.phone',r.phone,{type:'tel',mode:'tel',max:30,ph:PH})+'</div>';
  h+='<div class="sec">'+t('sec_you')+'</div><div class="formcard">'+field(t('f_owner'),'reg.name',r.name,{max:80,ac:'name'})+field(t('f_exp'),'reg.exp',r.exp,{mode:'numeric',max:2})+field(t('f_bio'),'reg.bio',r.bio,{area:1,max:600,ph:t('f_bio_ph')})+
    '<div class="field"><span>'+t('f_photo')+'</span><div id="reg-photo">'+regPhotoHtml()+'</div></div></div>';
  h+='<div class="sec">'+t('sec_services')+'<small>'+t('svc_hint')+'</small></div><div class="formcard">'+S.catalog.map(function(c){
    var x=r.svc[c.kind];
    return '<div class="pick"><label class="sw"><input type="checkbox" data-bind="regsvc-on" data-k="'+c.kind+'"'+(x.on?' checked':'')+'><span></span></label><span class="nm">'+esc(c.name)+'</span>'+
      (x.on?'<div class="vals"><div><label>'+t('f_price')+'</label><input class="inp" data-bind="regsvc-price" data-k="'+c.kind+'" inputmode="numeric" value="'+esc(x.price)+'"></div><div><label>'+t('f_dur')+'</label><input class="inp" data-bind="regsvc-dur" data-k="'+c.kind+'" inputmode="numeric" value="'+esc(x.dur)+'"></div></div>':'')+'</div>';
  }).join('')+'</div>';
  h+='<div class="sp"></div><button class="btn primary wide" data-act="reg-b" id="rb-go">'+t('register')+'</button>';
  if(!S.me.barber)h+='<div style="margin-top:10px"><button class="btn wide" data-act="to-welcome">'+t('back')+'</button></div>';
  return h;
}

/* ---- mijoz ---- */
function barbersHtml(){
  var q=S.q.trim().toLowerCase();
  var list=S.barbers.filter(function(b){
    if(S.onlyOpen&&!(b.status&&b.status.o))return false;
    return !q||(b.shop+' '+b.name+' '+b.address).toLowerCase().indexOf(q)>=0;
  });
  if(!S.barbers.length)return S.barbersAt?empty(t('no_barbers'),t('no_barbers_t')):'<div class="sk" style="height:104px"></div><div class="sk" style="height:104px"></div><div class="sk" style="height:104px"></div>';
  if(!list.length)return empty(t('nothing_found'),t('nothing_found_t'));
  return list.map(function(b){
    var open=b.status&&b.status.o;
    var nx=b.next?'<div class="bc-row"><span class="tag acc">'+esc(t('next_free',{d:dTiny(b.next.date),t:b.next.time}))+'</span></div>':'';
    return '<button class="bcard" data-act="open-b" data-id="'+b.id+'">'+avatar(b)+
      '<div class="bc-body"><div class="bc-name">'+esc(b.shop)+'</div><div class="bc-sub">'+esc(b.name)+(b.address?' · '+esc(b.address):'')+'</div>'+
      '<div class="bc-row"><span class="st-dot'+(open?' on':'')+'"><i></i>'+(open?t('f_open'):t('closed'))+'</span>'+
      '<span class="tag">'+(b.exp?t('exp_y',{n:b.exp}):t('new_b'))+'</span>'+
      '<span class="price-f">'+t('from_price',{p:money(b.from_price)})+'</span></div>'+nx+'</div></button>';
  }).join('');
}
function headHtml(){
  var n=S.barbers.length,o=S.barbers.filter(function(b){return b.status&&b.status.o}).length;
  return n?'<span class="tag">'+t('cnt_all',{n:n})+'</span> <span class="tag open">'+t('cnt_open',{n:o})+'</span>':'';
}
function viewList(){
  var nm=(S.me.name||'').split(' ')[0];
  return '<div class="hello"><div class="pole"></div><h2 class="page">'+(nm?t('hello',{n:esc(nm)}):t('list_title'))+'</h2><div class="hint">'+t('list_sub')+'</div><div class="cnts" id="bhead">'+headHtml()+'</div></div>'+
    '<div class="search">'+ic('search',20)+'<input class="inp" data-bind="q" placeholder="'+esc(t('search_ph'))+'" value="'+esc(S.q)+'" maxlength="60"></div>'+
    '<div class="filters"><button class="fchip'+(S.onlyOpen?'':' on')+'" data-act="f-open" data-o="0">'+t('f_all')+'</button><button class="fchip'+(S.onlyOpen?' on':'')+'" data-act="f-open" data-o="1">'+t('f_open')+'</button></div>'+
    '<div class="sp"></div><div id="blist">'+barbersHtml()+'</div>';
}
function svcRow(s,sel,act){
  return '<button class="svc'+(sel?' sel':'')+'" data-act="'+act+'" data-id="'+s.id+'"><span class="svc-name">'+esc(s.name)+'</span><span class="dots"></span><span class="svc-price">'+money(s.price)+'</span><span class="svc-sub">'+s.duration+' '+t('min')+'</span></button>';
}
function viewBarber(){
  var d=S.bcache[S.cur];
  var fab='<button class="back-fab" data-act="back" aria-label="'+t('back')+'">'+ic('back',22)+'</button>';
  if(!d)return '<div class="hero"><div class="av ph hero-ph"></div>'+fab+'</div><div class="sp"></div><div class="sk"></div><div class="sk"></div>';
  var b=d.barber;
  var hero=hasPh(b)?'<img src="/photo/'+b.id+'?v='+esc(b.photo_v)+'" alt="">':'<div class="av ph hero-ph">'+initial(b.shop)+'</div>';
  var h='<div class="hero">'+hero+'<div class="pole"></div>'+fab+'</div>';
  h+='<h1 class="shop">'+esc(b.shop)+'</h1><div class="owner"><span>'+esc(b.name)+'</span><span class="tag">'+(b.exp?t('exp_y',{n:b.exp}):t('new_b'))+'</span></div>';
  h+='<div class="status'+(d.status.o?' on':'')+'"><i></i>'+esc(stText(d.status))+'</div>';
  if(b.bio)h+='<p class="lead">'+esc(b.bio).replace(/\n/g,'<br>')+'</p>';
  var tel=b.phone?'href="'+telHref(b.phone)+'"':'';
  var map=b.address?'href="https://maps.google.com/?q='+encodeURIComponent(b.address)+'" target="_blank" rel="noopener"':'';
  h+='<div class="quick"><a class="'+(b.phone?'':'x')+'" '+tel+'>'+ic('phone')+t('call')+'</a><a class="'+(b.address?'':'x')+'" '+map+'>'+ic('route')+t('route')+'</a>'+
     '<button data-act="share-b" data-id="'+b.id+'">'+ic('share')+t('share')+'</button></div>';
  h+='<div class="row-h"><h3>'+t('choose_service')+'</h3></div><div class="hint">'+t('choose_service_h')+'</div>';
  h+=d.services.length?'<div class="board" style="margin-top:0">'+d.services.map(function(s){return svcRow(s,false,'svc-go')}).join('')+'</div>':'<div class="sp"></div>'+empty(t('no_svc_b'));
  var wd=wdi(shopNow()),th=d.hours[wd];
  var wk=d.hours.map(function(x,i){return '<div class="'+(i===wd?'today':'')+'"><span>'+I18N[LANG].wd[i]+'</span><span>'+(x.off?t('day_off'):x.open+' – '+x.close)+'</span></div>'}).join('');
  h+='<ul class="info">';
  if(b.address)h+='<li>'+ic('pin')+'<a href="https://maps.google.com/?q='+encodeURIComponent(b.address)+'" target="_blank" rel="noopener">'+esc(b.address)+'</a></li>';
  h+='<li>'+ic('clock')+'<details><summary>'+t('today')+': '+(th&&!th.off?th.open+' – '+th.close:t('day_off'))+'<small>'+t('week_sched')+'</small></summary><div class="wk">'+wk+'</div></details></li>';
  if(b.phone)h+='<li>'+ic('phone')+'<a href="'+telHref(b.phone)+'">'+esc(b.phone)+'</a></li>';
  return h+'</ul>';
}
function section(id,title,body){return '<section class="step" id="step-'+id+'"><h3 class="st">'+title+'</h3>'+body+'</section>'}
function daysHtml(){
  var b=S.book;
  if(!b.days)return '<div class="days">'+'<div class="sk" style="width:54px;height:64px;margin:0;flex:none"></div>'.repeat(5)+'</div>';
  if(!b.days.some(function(d){return d.open}))return empty(t('no_days'),t('no_days_txt'));
  return '<div class="days">'+b.days.map(function(d){
    var dd=parseISO(d.date);
    return '<button class="day'+(d.date===b.date?' sel':'')+(d.open?'':' none')+'" data-act="pick-day" data-d="'+d.date+'"><small>'+I18N[LANG].wds[wdi(dd)]+'</small><b>'+dd.getDate()+'</b></button>';
  }).join('')+'</div>'+(b.date?'<div class="hint" style="margin-top:8px">'+esc(dFull(b.date))+'</div>':'');
}
function slotsHtml(){
  var b=S.book;
  if(!b.slots)return '<div class="slots">'+'<div class="sk" style="height:46px;margin:0"></div>'.repeat(8)+'</div>';
  if(!b.slots.length)return empty(t('no_slots'),t('no_slots_txt'));
  var parts=[[t('part_morning'),function(x){return hmMin(x)<720}],[t('part_day'),function(x){var m=hmMin(x);return m>=720&&m<1020}],[t('part_eve'),function(x){return hmMin(x)>=1020}]];
  return parts.map(function(p){
    var xs=b.slots.filter(p[1]);if(!xs.length)return '';
    return '<div class="part">'+p[0]+'</div><div class="slots">'+xs.map(function(x){return '<button class="slot'+(x===b.time?' sel':'')+'" data-act="pick-time" data-t="'+x+'">'+x+'</button>'}).join('')+'</div>';
  }).join('');
}
function formHtml(){
  var b=S.book;
  return '<div class="sum"><div><span>'+t('sum_barber')+'</span><span>'+esc(b.barber.shop)+'</span></div><div><span>'+t('st_service')+'</span><span>'+esc(b.sv.name)+'</span></div>'+
    '<div><span>'+t('st_time')+'</span><span>'+esc(dRel(b.date))+', '+b.time+'</span></div>'+
    '<div><span>'+t('sum_dur')+'</span><span>'+b.sv.duration+' '+t('min')+'</span></div>'+
    '<div><span>'+t('sum_price')+'</span><span>'+money(b.sv.price)+' '+t('som')+'</span></div></div>'+
    field(t('your_name'),'b-name',b.name,{ac:'name',max:60})+field(t('your_phone'),'b-phone',b.phone,{type:'tel',mode:'tel',ac:'tel',max:20,ph:PH});
}
function stepper(n){
  var L=[t('st_service'),t('st_date'),t('st_time'),t('st_info_s')];
  return '<div class="stepper">'+L.map(function(x,i){return '<div class="'+(i+1<n?'done':i+1===n?'now':'')+'">'+x+'</div>'}).join('')+'</div>';
}
function viewBook(){
  var b=S.book;
  if(!b)return viewList();
  if(b.done)return viewDone();
  var n=!b.sv?1:!b.date?2:!b.time?3:4;
  var h='<h2 class="page">'+t('book_title')+'</h2><div class="mini">'+avatar(b.barber)+'<div><b>'+esc(b.barber.shop)+'</b><small>'+esc(b.barber.name)+'</small></div></div>'+stepper(n);
  h+=section('sv',t('st_service'),'<div class="board" style="margin:0">'+b.services.map(function(s){return svcRow(s,b.sv&&b.sv.id===s.id,'pick-sv')}).join('')+'</div>');
  if(b.sv)h+=section('date',t('st_date'),daysHtml());
  if(b.sv&&b.date)h+=section('time',t('st_time'),slotsHtml());
  if(b.sv&&b.date&&b.time)h+=section('form',t('st_info'),formHtml());
  return h+'<div class="cta-sp"></div>';
}
function ticket(b,opt){
  opt=opt||{};
  var cls='ticket'+(b.status!=='active'?' off':'')+(opt.big?' big':'');
  var badge=b.status==='cancelled'?'<span class="badge bad">'+t('cancelled')+'</span>':(opt.past?'<span class="badge mut">'+t('past_b')+'</span>':'');
  var h='<div class="'+cls+'"><div class="tk-l"><div class="tk-time">'+b.time+'</div><div class="tk-end">'+(opt.showDate?esc(dShort(b.date)):t('ends')+' '+b.end)+'</div></div><div class="tk-r">';
  h+='<div class="tk-name">'+esc(b.service)+badge+'</div>';
  if(b.shop)h+='<div class="tk-shop">'+esc(b.shop)+'</div>';
  h+='<div class="tk-meta">'+(opt.showDate?'':esc(dFull(b.date))+' · ')+'<span class="tk-price">'+money(b.price)+' '+t('som')+'</span></div>';
  if(b.address)h+='<div class="tk-meta">'+esc(b.address)+'</div>';
  if(opt.countdown&&b.status==='active'){var u=until(b);if(u)h+='<div class="tk-in">'+esc(u)+'</div>'}
  if(opt.admin)h+='<div class="tk-meta"><b style="color:var(--fg)">'+esc(b.name)+'</b> · <a href="'+telHref(b.phone)+'">'+esc(b.phone)+'</a></div>';
  if(opt.actions)h+='<div class="tk-act">'+opt.actions+'</div>';
  return h+'</div></div>';
}
function viewDone(){
  var d=S.book.done;
  return '<div class="done-h"><span class="ok-dot">'+ic('check',20)+'</span><h2 class="page" style="margin:0">'+t('done_title')+'</h2></div>'+ticket(d,{big:true})+
    '<p class="hint">'+t('done_txt')+'</p><div style="display:grid;gap:10px;margin-top:18px"><button class="btn primary" data-act="tab" data-tab="my">'+t('my_title')+'</button>'+
    '<button class="btn" data-act="tab" data-tab="list">'+t('find_barber')+'</button></div>';
}
function viewMy(){
  var h='<h2 class="page">'+t('my_title')+'</h2>';
  if(!S.my)return h+'<div class="sp"></div><div class="sk"></div><div class="sk"></div>';
  var up=S.my.upcoming,past=S.my.past;
  if(!up.length&&!past.length)return h+'<div class="sp"></div>'+empty(t('no_bookings'),t('no_bookings_txt'),'<button class="btn primary" data-act="tab" data-tab="list">'+t('find_barber')+'</button>');
  h+='<div class="seg"><button class="'+(S.myTab==='up'?'on':'')+'" data-act="my-tab" data-s="up">'+t('tab_upcoming')+(up.length?' · '+up.length:'')+'</button><button class="'+(S.myTab==='past'?'on':'')+'" data-act="my-tab" data-s="past">'+t('tab_past')+'</button></div><div style="height:10px"></div>';
  if(S.myTab==='up'){
    if(up.length)h+=up.map(function(x){return ticket(x,{showDate:true,countdown:true,actions:'<button class="btn small danger" data-act="cancel-my" data-id="'+x.id+'">'+t('cancel')+'</button>'})}).join('');
    else h+='<div class="hint" style="margin:8px 0 16px">'+t('no_upcoming')+'</div><button class="btn primary wide" data-act="tab" data-tab="list">'+t('find_barber')+'</button>';
  }else{
    if(past.length)h+=past.map(function(x){return ticket(x,{showDate:true,past:x.status==='active',actions:'<button class="btn small" data-act="open-b" data-id="'+x.barber_id+'">'+t('book_again')+'</button>'})}).join('');
    else h+='<div class="hint" style="margin:8px 0">'+t('no_past')+'</div>';
  }
  return h;
}
function langRow(){
  return '<div class="mrow"><b>'+t('lang_label')+'</b><div class="lang">'+['uz','ru','en'].map(function(l){return '<button class="'+(l===LANG?'on':'')+'" data-act="lang" data-l="'+l+'">'+l.toUpperCase()+'</button>'}).join('')+'</div></div>';
}
function viewProfile(){
  var c=S.cp,me=S.me;
  var h='<h2 class="page">'+t('prof_title')+'</h2><div class="sp"></div><div class="formcard">'+field(t('your_name'),'cp.name',c.name,{ac:'name',max:60})+field(t('your_phone'),'cp.phone',c.phone,{type:'tel',mode:'tel',ac:'tel',max:20,ph:PH})+'</div>';
  h+='<div style="margin-top:12px"><button class="btn primary wide" data-act="cp-save">'+t('save')+'</button></div>';
  h+=langRow();
  h+='<div class="menu">';
  if(me.barber)h+='<button data-act="enter-mode" data-m="barber">'+ic('scissors')+'<span>'+t('switch_barber')+'</span></button>';
  else h+='<button data-act="pick-role" data-r="barber">'+ic('scissors')+'<span>'+t('become_barber')+'<small>'+t('become_barber_d')+'</small></span></button>';
  if(me.is_super)h+='<button data-act="moder">'+ic('shield')+'<span>'+t('moderation')+'</span></button>';
  return h+'</div>';
}
function viewModer(){
  var h='<h2 class="page">'+t('moderation')+'</h2><div class="sp"></div>';
  if(!S.moder)return h+'<div class="sk"></div><div class="sk"></div>';
  return h+S.moder.map(function(b){
    return '<div class="mrow"><div style="min-width:0"><b>'+esc(b.shop)+'</b><div class="hint">'+esc(b.name)+' · '+esc(b.address)+(b.active?'':' · '+t('hidden_b'))+'</div></div><button class="btn small'+(b.active?' danger':'')+'" data-act="toggle-b" data-id="'+b.id+'" data-a="'+(b.active?0:1)+'">'+(b.active?t('hide_b'):t('show_b'))+'</button></div>';
  }).join('');
}

/* ---- sartarosh paneli ---- */
function viewBBook(){
  var a=S.b,today=iso(shopNow()),tom=addDays(today,1),o=a.ov;
  if(!a.date)a.date=today;
  var h='<h2 class="page">'+t('b_title')+'</h2>';
  h+='<div class="stats"><button class="stat'+(a.date===today?' on':'')+'" data-act="b-date" data-d="'+today+'"><small>'+t('today')+'</small><b>'+(o?o.today.n:'–')+'</b><span>'+(o?money(o.today.sum)+' '+t('som'):'&nbsp;')+'</span></button>'+
    '<button class="stat'+(a.date===tom?' on':'')+'" data-act="b-date" data-d="'+tom+'"><small>'+t('tomorrow')+'</small><b>'+(o?o.tomorrow.n:'–')+'</b><span>'+(o?money(o.tomorrow.sum)+' '+t('som'):'&nbsp;')+'</span></button>'+
    '<div class="stat"><small>'+t('ov_later')+'</small><b>'+(o?o.later:'–')+'</b><span>'+t('ov_bk')+'</span></div></div>';
  h+='<div class="nav"><button class="arr" data-act="b-prev">‹</button><input class="inp" type="date" data-bind="b-date" value="'+a.date+'"><button class="arr" data-act="b-next">›</button></div>';
  if(!a.rows)return h+'<div class="sp"></div><div class="sk"></div><div class="sk"></div>';
  h+='<div class="hint" style="margin:10px 0 14px">'+esc(dRel(a.date))+' · '+t('active_n',{n:a.rows.active})+'</div>';
  if(!a.rows.bookings.length)return h+empty(t('no_day_bookings'),t('no_day_txt'));
  return h+a.rows.bookings.map(function(x){
    var act=x.status==='active'?'<button class="btn small danger" data-act="b-cancel" data-id="'+x.id+'">'+t('cancel')+'</button>':'';
    return ticket(x,{admin:true,actions:act});
  }).join('');
}
function svName(s){
  if(s.name)return s.name;
  var c=S.catalog.filter(function(x){return x.kind===s.kind})[0];
  return c?c.name:'';
}
function viewBSvc(){
  var a=S.b,h='<h2 class="page">'+t('tab_b_svc')+'</h2>';
  if(!a.services)return h+'<div class="sp"></div><div class="sk"></div><div class="sk"></div>';
  return h+'<div class="board">'+a.services.map(function(s){
    return '<button class="svc'+(s.active?'':' off')+'" data-act="sv-edit" data-id="'+s.id+'"><span class="svc-name">'+esc(svName(s))+'</span><span class="dots"></span><span class="svc-price">'+money(s.price)+'</span><span class="svc-sub">'+s.duration+' '+t('min')+(s.active?'':' · '+t('hidden'))+'</span></button>';
  }).join('')+'</div><div style="margin-top:18px"><button class="btn primary wide" data-act="sv-new">'+t('new_service')+'</button></div>';
}
function viewBHours(){
  var a=S.b,h='<h2 class="page">'+t('tab_b_hours')+'</h2>';
  if(!a.hours)return h+'<div class="sp"></div><div class="sk"></div><div class="sk"></div>';
  h+=a.hours.map(function(x,i){
    var r='<div class="hr"><div class="hr-top"><b>'+I18N[LANG].wd[i]+'</b><label class="sw"><input type="checkbox" data-bind="hr-open" data-i="'+i+'"'+(x.off?'':' checked')+'><span></span></label></div>';
    if(!x.off){
      r+='<div class="hr-t"><input class="inp" type="time" data-bind="hr-o" data-i="'+i+'" value="'+x.open+'"><span class="hint">–</span><input class="inp" type="time" data-bind="hr-c" data-i="'+i+'" value="'+x.close+'"></div>';
      r+='<div class="hr-lb">'+t('break_opt')+'</div><div class="hr-t"><input class="inp" type="time" data-bind="hr-bs" data-i="'+i+'" value="'+x.br_start+'"><span class="hint">–</span><input class="inp" type="time" data-bind="hr-be" data-i="'+i+'" value="'+x.br_end+'"></div>';
    }
    return r+'</div>';
  }).join('');
  h+='<div style="margin:16px 0 0"><button class="btn primary wide" data-act="hrs-save">'+t('save_hours')+'</button></div>';
  h+='<h3 class="st" style="margin:30px 0 4px">'+t('days_off')+'</h3><div class="hint">'+t('days_off_hint')+'</div>';
  h+='<div class="row" style="margin-top:12px"><input class="inp" type="date" data-bind="off-date" value="'+(a.offPick||'')+'" min="'+iso(shopNow())+'"><button class="btn small" data-act="dayoff-add" style="flex:none">'+t('add')+'</button></div>';
  return h+'<div class="chips">'+(a.daysOff.length?a.daysOff.map(function(d){return '<span class="chip">'+esc(dShort(d))+'<button data-act="dayoff-rm" data-d="'+d+'" aria-label="'+t('remove')+'">'+ic('x',14)+'</button></span>'}).join(''):'<span class="hint">'+t('none_yet')+'</span>')+'</div>';
}
function sel(name,val,opts){
  return '<select class="inp" data-bind="'+name+'">'+opts.map(function(o){return '<option value="'+o[0]+'"'+(String(o[0])===String(val)?' selected':'')+'>'+o[1]+'</option>'}).join('')+'</select>';
}
function viewBProf(){
  var p=S.bp,m=t('min');
  var h='<h2 class="page">'+t('prof_title')+'</h2><div class="sp"></div>';
  h+='<div class="formcard"><div class="field"><span>'+t('photo')+'</span><div class="photo-box">'+avatar({id:p.id,shop:p.shop,photo_v:p.photo_v})+
    '<div style="display:grid;gap:8px"><label class="btn small" style="cursor:pointer">'+t('choose_photo')+'<input type="file" accept="image/*" data-bind="photo-file" hidden></label>'+(hasPh(p)?'<button class="btn small danger" data-act="photo-rm">'+t('remove_photo')+'</button>':'')+'</div></div></div>'+
    field(t('f_shop'),'bp.shop',p.shop,{max:80})+field(t('f_owner'),'bp.name',p.name,{max:80})+field(t('f_addr'),'bp.address',p.address,{max:200})+field(t('f_phone'),'bp.phone',p.phone,{type:'tel',mode:'tel',max:30,ph:PH})+
    field(t('f_exp'),'bp.exp',p.exp,{mode:'numeric',max:2})+field(t('f_bio'),'bp.bio',p.bio,{area:1,max:600,ph:t('f_bio_ph')})+
    '<label class="field"><span>'+t('s_step')+'</span>'+sel('bp.step',p.step,[[15,'15 '+m],[20,'20 '+m],[30,'30 '+m],[45,'45 '+m],[60,'60 '+m]])+'</label>'+
    '<label class="field"><span>'+t('s_remind')+'</span>'+sel('bp.remind_hours',p.remind_hours,[[1,'1 '+t('hour_s')],[2,'2 '+t('hour_s')],[3,'3 '+t('hour_s')]])+'</label>'+
    '<label class="field"><span>'+t('s_horizon')+'</span>'+sel('bp.horizon',p.horizon,[[7,'7 '+t('day_s')],[14,'14 '+t('day_s')],[30,'30 '+t('day_s')],[60,'60 '+t('day_s')]])+'</label></div>';
  h+='<div style="margin-top:12px"><button class="btn primary wide" data-act="bp-save">'+t('save')+'</button></div>'+langRow();
  h+='<div class="menu"><button data-act="share-b" data-id="'+p.id+'">'+ic('share')+'<span>'+t('share_link')+'</span></button>'+
     '<button data-act="enter-mode" data-m="client">'+ic('swap')+'<span>'+t('switch_client')+'</span></button>'+
     (S.me.is_super?'<button data-act="moder">'+ic('shield')+'<span>'+t('moderation')+'</span></button>':'')+'</div>';
  return h;
}

/* ---- chizish ---- */
var GATE={welcome:1,reg_c:1,reg_b:1};
var TABS={
  client:[['list','tab_barbers','scissors'],['my','tab_my','ticket'],['profile','tab_profile','user']],
  barber:[['b_book','tab_b_book','calendar'],['b_svc','tab_b_svc','scissors'],['b_hours','tab_b_hours','clock'],['b_prof','tab_profile','user']]
};
function tabOf(v){
  if(v==='barber'||v==='book'||v==='done')return 'list';
  if(v==='moder')return S.mode==='barber'?'b_prof':'profile';
  return v;
}
function renderTop(){
  $('#brand').textContent=t('app_name');
  $('#lang').innerHTML=['uz','ru','en'].map(function(l){return '<button class="'+(l===LANG?'on':'')+'" data-act="lang" data-l="'+l+'">'+l.toUpperCase()+'</button>'}).join('');
}
function renderTabs(){
  if(GATE[S.v]||!S.me){$('#tabs').innerHTML='';document.body.classList.add('nobar');return}
  document.body.classList.remove('nobar');
  $('#tabs').innerHTML=TABS[S.mode].map(function(x){return '<button class="'+(tabOf(S.v)===x[0]?'on':'')+'" data-act="tab" data-tab="'+x[0]+'">'+ic(x[2])+'<span>'+t(x[1])+'</span></button>'}).join('');
}
function renderCta(){
  var b=S.book,el=$('#cta-root');
  if(S.v!=='book'||!b||b.done||!b.sv||!b.time){el.innerHTML='';return}
  var ok=b.name.trim().length>=2&&phoneOk(b.phone);
  el.innerHTML='<div class="cta"><button class="btn primary wide" data-act="book"'+((ok&&!b.busy)?'':' disabled')+'>'+(b.busy?t('sending'):(ok?t('cta_book',{p:money(b.sv.price)+' '+t('som')}):t('cta_fill')))+'</button></div>';
}
var VIEWS={welcome:viewWelcome,reg_c:viewRegC,reg_b:viewRegB,list:viewList,barber:viewBarber,book:viewBook,done:viewBook,my:viewMy,profile:viewProfile,moder:viewModer,
  b_book:viewBBook,b_svc:viewBSvc,b_hours:viewBHours,b_prof:viewBProf};
function updateBack(){
  if(!tg||!tg.BackButton)return;
  try{S.stack.length&&!GATE[S.v]?tg.BackButton.show():tg.BackButton.hide()}catch(e){}
}
function render(keepScroll){
  renderTop();
  if(S.err){
    view.innerHTML='<h2 class="page">'+t('not_opened')+'</h2><div class="sp"></div>'+empty(S.err,t('reopen_hint'),'<button class="btn" data-act="reload">'+t('retry')+'</button>');
    $('#tabs').innerHTML='';$('#cta-root').innerHTML='';return;
  }
  if(!S.me){view.innerHTML='<div class="splash">'+ic('scissors',44)+'<div style="margin-top:10px">'+t('loading')+'</div></div>';$('#tabs').innerHTML='';return}
  var y=window.pageYOffset;
  view.innerHTML=(VIEWS[S.v]||viewList)();
  renderTabs();renderCta();updateBack();
  if(keepScroll)window.scrollTo(0,y);
}
function scrollToStep(id){
  requestAnimationFrame(function(){var el=document.getElementById('step-'+id);if(el)el.scrollIntoView({behavior:'smooth',block:'start'})});
}
function cloneBp(){return JSON.parse(JSON.stringify(S.me.barber))}

/* ---- navigatsiya ---- */
function defTab(){return S.mode==='barber'?'b_book':'list'}
function go(tab){
  S.stack=[];S.v=tab;
  if(tab==='my')loadMy();
  else if(tab==='profile')S.cp={name:S.me.name||'',phone:S.me.phone||''};
  else if(tab==='list')refreshBarbers();
  else if(tab==='b_book')loadBBook();
  else if(tab==='b_svc'){S.b.services=null;api('b/services').then(function(r){S.b.services=r.services;if(S.v==='b_svc')render()}).catch(fail)}
  else if(tab==='b_hours'){S.b.hours=null;api('b/hours').then(function(r){S.b.hours=r.hours;S.b.daysOff=r.days_off;if(S.v==='b_hours')render()}).catch(fail)}
  else if(tab==='b_prof')S.bp=cloneBp();
  render();window.scrollTo(0,0);
}
function push(v){S.stack.push(S.v);S.v=v;render();window.scrollTo(0,0)}
function back(){
  if(!S.stack.length)return;
  S.v=S.stack.pop();
  if(S.v==='book'&&S.book&&S.book.done)S.v='list';
  render();window.scrollTo(0,0);
}
if(tg&&tg.BackButton){try{tg.BackButton.onClick(back)}catch(e){}}
function enterMode(m){
  S.mode=m;S.me.role=m;
  api('role',{role:m}).catch(function(){});
  go(defTab());
}

/* ---- ma'lumot yuklash ---- */
function refreshBarbers(){
  if(Date.now()-(S.barbersAt||0)<15000)return;
  api('barbers').then(function(r){S.barbers=r.barbers;S.barbersAt=Date.now();if(S.v==='list'){var el=$('#blist');if(el)el.innerHTML=barbersHtml();var h=$('#bhead');if(h)h.innerHTML=headHtml()}}).catch(function(){});
}
function openBarber(id){
  S.cur=id;
  if(S.v!=='barber')push('barber');else render();
  api('barber',{id:id}).then(function(r){S.bcache[id]=r;if(S.v==='barber'&&S.cur===id)render(true)}).catch(function(e){fail(e);back()});
}
function startBook(svId){
  var d=S.bcache[S.cur];if(!d)return;
  var sv=d.services.filter(function(x){return x.id===svId})[0];if(!sv)return;
  S.book={barber:d.barber,services:d.services,sv:sv,days:null,date:null,slots:null,time:null,name:S.me.name||'',phone:S.me.phone||'',busy:false,done:null};
  push('book');loadDays();
}
function loadDays(){
  var b=S.book;
  b.days=null;b.date=null;b.slots=null;b.time=null;
  render(true);
  var id=b.sv.id;
  return api('days',{barber_id:b.barber.id,service_id:id}).then(function(r){
    if(S.book!==b||b.sv.id!==id)return;
    b.days=r.days;
    var first=r.days.filter(function(d){return d.open})[0];
    if(first)return pickDay(first.date,true);
    render(true);
  }).catch(function(e){fail(e);b.days=[];render(true)});
}
function pickDay(date,quiet){
  var b=S.book;b.date=date;b.slots=null;b.time=null;render(true);
  var id=b.sv.id;
  return api('slots',{barber_id:b.barber.id,service_id:id,date:date}).then(function(r){
    if(S.book!==b||b.sv.id!==id||b.date!==date)return;
    b.slots=r.slots;render(true);if(!quiet)scrollToStep('time');
  }).catch(function(e){fail(e);b.slots=[];render(true)});
}
function doBook(){
  var b=S.book;if(b.busy)return;
  b.busy=true;renderCta();
  api('book',{barber_id:b.barber.id,service_id:b.sv.id,date:b.date,time:b.time,name:b.name.trim(),phone:b.phone.trim()}).then(function(r){
    b.busy=false;b.done=r.booking;S.my=null;
    S.me.name=b.name.trim();S.me.phone=b.phone.trim();
    S.v='done';hap('ok');render();window.scrollTo(0,0);
    refreshBarbers();
  }).catch(function(e){
    b.busy=false;fail(e);
    if(e.status===409)pickDay(b.date,true);else renderCta();
  });
}
function loadMy(){
  S.my=null;
  api('my').then(function(r){S.my=r;if(S.v==='my')render()}).catch(function(e){fail(e);S.my={upcoming:[],past:[]};if(S.v==='my')render()});
}
function loadBBook(){
  var a=S.b;if(!a.date)a.date=iso(shopNow());
  a.rows=null;var d=a.date;
  api('b/overview').then(function(r){a.ov=r;if(S.v==='b_book')render(true)}).catch(function(){});
  api('b/bookings',{date:d}).then(function(r){if(a.date===d){a.rows=r;if(S.v==='b_book')render(true)}}).catch(function(e){fail(e);a.rows={bookings:[],active:0};render(true)});
}
function reloadInit(){
  return api('init').then(function(r){r.now_ts=Date.now();S.init=r;S.me=r.me;S.barbers=r.barbers;S.catalog=r.catalog});
}
function setLang(l){
  if(l===LANG||!I18N[l])return;
  LANG=l;root.lang=l;
  try{localStorage.setItem('lang',l)}catch(e){}
  hap();render(true);
  if(!S.me)return;
  S.bcache={};
  reloadInit().then(function(){
    if(S.v==='my')loadMy();
    if(S.v==='barber')openBarber(S.cur);
    if(S.v==='b_prof')S.bp=cloneBp();
    if(S.v==='b_svc')go('b_svc');
    if(S.book&&!S.book.done&&S.v==='book'){S.book=null;go('list')}
    render(true);
  }).catch(fail);
}

/* ---- ro'yxatdan o'tish holati ---- */
function initReg(){
  S.reg={shop:'',name:S.me.name||'',address:'',phone:S.me.phone||'',exp:'',bio:'',photo:null,thumb:null,svc:{}};
  S.catalog.forEach(function(c){S.reg.svc[c.kind]={on:c.kind==='haircut'||c.kind==='beard'||c.kind==='combo',price:c.price,dur:c.dur}});
}
function doRegB(){
  var r=S.reg;
  var items=S.catalog.filter(function(c){return r.svc[c.kind].on}).map(function(c){return {kind:c.kind,price:r.svc[c.kind].price,duration:r.svc[c.kind].dur}});
  if(!items.length){toast(t('pick_one'));return}
  var btn=$('#rb-go');if(btn)btn.disabled=true;
  api('b/register',{shop:r.shop,name:r.name,address:r.address,phone:r.phone,exp:r.exp||0,bio:r.bio,services:items}).then(function(res){
    S.me.barber=res.barber;S.me.role='barber';
    var p=r.photo?api('b/photo',{image:r.photo,thumb:r.thumb}).then(function(x){S.me.barber.photo_v=x.photo_v}).catch(function(){}):Promise.resolve();
    return p.then(function(){S.mode='barber';hap('ok');toast(t('reg_done'));go('b_book')});
  }).catch(function(e){if(btn)btn.disabled=false;fail(e)});
}

/* ---- sheet ---- */
function closeSheet(){$('#sheet-root').innerHTML=''}
function openSheet(html){$('#sheet-root').innerHTML='<div class="back" data-act="sheet-close"><div class="sheet"><div class="grab"></div>'+html+'</div></div>'}
function svSheet(s){
  s=s||{id:0,kind:'haircut',name:'',price:'',duration:30,active:true};
  var c0=S.catalog.filter(function(c){return c.kind===s.kind})[0];
  if(!s.id&&c0){s.price=c0.price;s.duration=c0.dur}
  var custom=!!(s.name||!s.kind);
  var opts=S.catalog.map(function(c){return '<option value="'+c.kind+'"'+(c.kind===s.kind&&!custom?' selected':'')+'>'+esc(c.name)+'</option>'}).join('')+
    '<option value=""'+(custom?' selected':'')+'>'+t('f_custom')+'</option>';
  openSheet('<h3 style="font:700 20px/1.2 var(--serif)">'+(s.id?t('edit_service'):t('new_service'))+'</h3><div class="sp"></div>'+
    '<label class="field"><span>'+t('f_kind')+'</span><select class="inp" id="sv-kind">'+opts+'</select></label>'+
    '<label class="field" id="sv-name-w" style="display:'+(custom?'block':'none')+'"><span>'+t('f_name')+'</span><input class="inp" id="sv-name" maxlength="100" value="'+esc(s.name)+'"></label>'+
    '<div class="row"><label class="field" style="flex:1"><span>'+t('f_price')+'</span><input class="inp" id="sv-price" inputmode="numeric" value="'+esc(s.price)+'"></label>'+
    '<label class="field" style="flex:1"><span>'+t('f_dur')+'</span><input class="inp" id="sv-dur" inputmode="numeric" value="'+esc(s.duration)+'"></label></div>'+
    '<div class="row" style="justify-content:space-between;margin:2px 0 18px"><span>'+t('f_visible')+'</span><label class="sw"><input type="checkbox" id="sv-active"'+(s.active?' checked':'')+'><span></span></label></div>'+
    '<button class="btn primary wide" data-act="sv-save" data-id="'+s.id+'">'+t('save')+'</button>'+
    (s.id?'<div style="height:10px"></div><button class="btn wide danger" data-act="sv-del" data-id="'+s.id+'">'+t('del_service')+'</button>':''));
  var sk=$('#sv-kind');
  sk.addEventListener('change',function(){
    var c=S.catalog.filter(function(x){return x.kind===sk.value})[0];
    $('#sv-name-w').style.display=c?'none':'block';
    if(c&&!s.id){$('#sv-price').value=c.price;$('#sv-dur').value=c.dur}
  });
}

/* ---- rasm ---- */
function shrinkImage(file){
  return new Promise(function(resolve,reject){
    var fr=new FileReader();
    fr.onerror=function(){reject(new Error(t('photo_read_err')))};
    fr.onload=function(){
      var im=new Image();
      im.onerror=function(){reject(new Error(t('photo_fmt_err')))};
      im.onload=function(){
        var max=900,q=0.8,out='',ok=false;
        for(var i=0;i<4;i++){
          var k=Math.min(1,max/Math.max(im.width,im.height));
          var c=document.createElement('canvas');c.width=Math.round(im.width*k);c.height=Math.round(im.height*k);
          c.getContext('2d').drawImage(im,0,0,c.width,c.height);
          out=c.toDataURL('image/jpeg',q);
          if(out.length<420000){ok=true;break}
          max=Math.round(max*0.75);q=Math.max(0.6,q-0.07);
        }
        if(!ok)return reject(new Error(t('photo_big')));
        var s=Math.min(im.width,im.height),tc=document.createElement('canvas');tc.width=tc.height=200;
        tc.getContext('2d').drawImage(im,(im.width-s)/2,(im.height-s)/2,s,s,0,0,200,200);
        resolve({image:out,thumb:tc.toDataURL('image/jpeg',0.72)});
      };
      im.src=fr.result;
    };
    fr.readAsDataURL(file);
  });
}
function shareBarber(id,shop){
  var bot=S.me.bot;if(!bot){toast(t('err_generic'));return}
  var url='https://t.me/'+bot+'?start=b'+id,text=t('share_text',{shop:shop||''});
  var link='https://t.me/share/url?url='+encodeURIComponent(url)+'&text='+encodeURIComponent(text);
  if(tg&&tg.openTelegramLink)tg.openTelegramLink(link);else window.open(link,'_blank');
}

/* ---- hodisalar ---- */
var ACT={
  'lang':function(el){setLang(el.dataset.l)},
  'reload':function(){location.reload()},
  'back':function(){back()},
  'tab':function(el){go(el.dataset.tab)},
  'pick-role':function(el){
    var r=el.dataset.r;
    if(r==='client'){S.cp={name:S.me.name||'',phone:S.me.phone||''};S.v='reg_c';render();window.scrollTo(0,0)}
    else{
      if(S.me.barber){enterMode('barber');return}
      api('role',{role:'barber'}).catch(function(){});
      initReg();S.v='reg_b';render();window.scrollTo(0,0);
    }
  },
  'to-welcome':function(){if(S.me.role&&(S.me.role!=='barber'||S.me.barber)){go(defTab())}else{S.v='welcome';render()}},
  'reg-c':function(){
    var c=S.cp;
    api('role',{role:'client',name:c.name.trim(),phone:c.phone.trim()}).then(function(){
      S.me.role='client';S.me.name=c.name.trim();S.me.phone=c.phone.trim();S.mode='client';hap('ok');go('list');
    }).catch(fail);
  },
  'reg-b':function(){doRegB()},
  'cp-save':function(){
    var c=S.cp;
    api('role',{role:S.mode==='barber'?'barber':'client',name:c.name.trim(),phone:c.phone.trim()}).then(function(){S.me.name=c.name.trim();S.me.phone=c.phone.trim();toast(t('saved'))}).catch(fail);
  },
  'enter-mode':function(el){enterMode(el.dataset.m)},
  'moder':function(){S.moder=null;push('moder');api('s/barbers').then(function(r){S.moder=r.barbers;if(S.v==='moder')render()}).catch(fail)},
  'toggle-b':function(el){api('s/toggle',{id:+el.dataset.id,active:el.dataset.a==='1'}).then(function(){return api('s/barbers')}).then(function(r){S.moder=r.barbers;render(true)}).catch(fail)},
  'f-open':function(el){S.onlyOpen=el.dataset.o==='1';render(true)},
  'open-b':function(el){hap();openBarber(+el.dataset.id)},
  'share-b':function(el){var id=+el.dataset.id,b=S.bcache[id];shareBarber(id,b?b.barber.shop:(S.me.barber&&S.me.barber.shop))},
  'svc-go':function(el){startBook(+el.dataset.id)},
  'pick-sv':function(el){
    var id=+el.dataset.id,b=S.book;if(b.sv&&b.sv.id===id)return;
    hap();b.sv=b.services.filter(function(x){return x.id===id})[0];loadDays();
  },
  'pick-day':function(el){hap();pickDay(el.dataset.d)},
  'pick-time':function(el){hap();S.book.time=el.dataset.t;render(true);scrollToStep('form')},
  'book':function(){doBook()},
  'my-tab':function(el){S.myTab=el.dataset.s;render(true)},
  'cancel-my':function(el){
    var id=+el.dataset.id;
    ask(t('cancel_q'),function(){api('cancel',{id:id}).then(function(){toast(t('cancelled_toast'));refreshBarbers();loadMy()}).catch(fail)});
  },
  'b-date':function(el){S.b.date=el.dataset.d;loadBBook();render(true)},
  'b-prev':function(){S.b.date=addDays(S.b.date,-1);loadBBook();render(true)},
  'b-next':function(){S.b.date=addDays(S.b.date,1);loadBBook();render(true)},
  'b-cancel':function(el){
    var id=+el.dataset.id;
    ask(t('cancel_q_admin'),function(){api('b/cancel',{id:id}).then(function(){toast(t('cancelled_toast'));loadBBook()}).catch(fail)});
  },
  'sv-new':function(){svSheet()},
  'sv-edit':function(el){var id=+el.dataset.id;svSheet(S.b.services.filter(function(x){return x.id===id})[0])},
  'sv-save':function(el){
    var kind=$('#sv-kind').value;
    var body={id:+el.dataset.id||0,kind:kind,name:kind?'':$('#sv-name').value,price:$('#sv-price').value,duration:$('#sv-dur').value,active:$('#sv-active').checked};
    api('b/service_save',body).then(function(r){S.b.services=r.services;closeSheet();toast(t('saved'));render(true)}).catch(fail);
  },
  'sv-del':function(el){
    var id=+el.dataset.id;
    ask(t('del_service_q'),function(){api('b/service_delete',{id:id}).then(function(r){S.b.services=r.services;closeSheet();toast(t('deleted'));render(true)}).catch(fail)});
  },
  'hrs-save':function(){api('b/hours_save',{hours:S.b.hours}).then(function(r){S.b.hours=r.hours;toast(t('saved'));render(true)}).catch(fail)},
  'dayoff-add':function(){
    var d=S.b.offPick;if(!d){toast(t('pick_date'));return}
    api('b/dayoff',{date:d,off:true}).then(function(r){S.b.daysOff=r.days_off;S.b.offPick='';render(true)}).catch(fail);
  },
  'dayoff-rm':function(el){api('b/dayoff',{date:el.dataset.d,off:false}).then(function(r){S.b.daysOff=r.days_off;render(true)}).catch(fail)},
  'bp-save':function(){
    api('b/profile_save',S.bp).then(function(r){S.me.barber=r.barber;S.bp=cloneBp();S.bcache={};toast(t('saved'));render(true)}).catch(fail);
  },
  'photo-rm':function(){api('b/photo',{remove:true}).then(function(){S.bp.photo_v='0';S.me.barber.photo_v='0';S.bcache={};toast(t('photo_removed'));render(true)}).catch(fail)},
  'sheet-close':function(el,e){if(e.target===el)closeSheet()}
};
document.addEventListener('click',function(e){
  var el=e.target.closest('[data-act]');if(!el)return;
  var f=ACT[el.dataset.act];if(f)f(el,e);
});
var BIND={
  'q':function(el){S.q=el.value;var l=$('#blist');if(l)l.innerHTML=barbersHtml()},
  'b-name':function(el){S.book.name=el.value;renderCta()},
  'b-phone':function(el){S.book.phone=el.value;renderCta()},
  'b-date':function(el){if(el.value&&el.value!==S.b.date){S.b.date=el.value;loadBBook();render(true)}},
  'off-date':function(el){S.b.offPick=el.value},
  'hr-open':function(el){S.b.hours[+el.dataset.i].off=!el.checked;render(true)},
  'hr-o':function(el){S.b.hours[+el.dataset.i].open=el.value},
  'hr-c':function(el){S.b.hours[+el.dataset.i].close=el.value},
  'hr-bs':function(el){S.b.hours[+el.dataset.i].br_start=el.value},
  'hr-be':function(el){S.b.hours[+el.dataset.i].br_end=el.value},
  'regsvc-on':function(el){S.reg.svc[el.dataset.k].on=el.checked;render(true)},
  'regsvc-price':function(el){S.reg.svc[el.dataset.k].price=el.value},
  'regsvc-dur':function(el){S.reg.svc[el.dataset.k].dur=el.value},
  'reg-photo':function(el){
    var f=el.files&&el.files[0];if(!f)return;
    shrinkImage(f).then(function(r){S.reg.photo=r.image;S.reg.thumb=r.thumb;var box=$('#reg-photo');if(box)box.innerHTML=regPhotoHtml()}).catch(fail);
  },
  'photo-file':function(el){
    var f=el.files&&el.files[0];if(!f)return;
    toast(t('photo_uploading'));
    shrinkImage(f).then(function(r){return api('b/photo',r)}).then(function(r){
      S.bp.photo_v=r.photo_v;S.me.barber.photo_v=r.photo_v;S.bcache={};toast(t('photo_saved'));render(true);
    }).catch(fail);
  }
};
function onField(e){
  var el=e.target,k=el.dataset&&el.dataset.bind;if(!k)return;
  if(e.type==='input'&&(el.type==='checkbox'||el.type==='file'||el.type==='time'||el.type==='date'||el.tagName==='SELECT'))return;
  if(BIND[k]){BIND[k](el);return}
  if(k.indexOf('.')>0){
    var p=k.split('.');S[p[0]][p[1]]=el.value;
    if(k==='cp.name'||k==='cp.phone'){var b=$('#rc-go');if(b&&S.v==='reg_c')b.disabled=!(S.cp.name.trim().length>=2&&phoneOk(S.cp.phone))}
  }
}
document.addEventListener('input',onField);
document.addEventListener('change',onField);

function snapSave(r){try{localStorage.setItem('snap2',JSON.stringify({me:r.me,barbers:r.barbers,catalog:r.catalog,now:r.now,now_ts:r.now_ts,uid:r.me.id}))}catch(e){}}
function snapLoad(){
  try{
    var j=JSON.parse(localStorage.getItem('snap2')||'null');
    var u=tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user;
    if(j&&j.me&&u&&j.uid===u.id)return j;
  }catch(e){}
  return null;
}
function route(r,fresh){
  var q=new URLSearchParams(location.search),qb=+q.get('b')||0,qt=q.get('tab'),qr=q.get('r');
  var role=r.me.role;
  if(!role&&qr!=='barber'){S.v='welcome';render();return}
  if((role==='barber'&&!r.me.barber)||(qr==='barber'&&!r.me.barber)){initReg();S.v='reg_b';render();return}
  S.mode=(role==='barber')?'barber':'client';
  if(qt==='barber'&&r.me.barber)S.mode='barber';
  if(qb||qt==='my')S.mode='client';
  S.barbersAt=Date.now();
  go(defTab());
  if(qt==='my')go('my');
  if(qb)openBarber(qb);
}
function start(){
  var snap=snapLoad(),shown=false;
  if(snap&&snap.me.role){
    S.init=snap;S.me=snap.me;S.barbers=snap.barbers;S.catalog=snap.catalog;shown=true;
    route(snap,false);
  }else render();
  api('init').then(function(r){
    r.now_ts=Date.now();S.init=r;S.me=r.me;S.barbers=r.barbers;S.catalog=r.catalog;snapSave(r);
    S.barbersAt=Date.now();
    if(shown){
      if(S.v==='list'){var el=$('#blist');if(el)el.innerHTML=barbersHtml();var h=$('#bhead');if(h)h.innerHTML=headHtml()}
      else if(S.v==='welcome'||S.v==='reg_c'||S.v==='reg_b'){}
      else if(S.v==='b_prof')S.bp=cloneBp();
      renderTabs();
      return;
    }
    route(r,true);
  }).catch(function(e){
    if(shown&&e.status!==401)return;
    S.err=(e.status===401||!tg||!tg.initData)?t('open_in_tg'):e.message;
    render();
  });
}
start();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
