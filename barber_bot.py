"""
Sartaroshlar platformasi: Telegram bot + Mini App (bir nechta sartarosh, mijoz bron qiladi)

Bitta fayl: bot, HTTP server, API va Mini App sahifasi.

Render'da kerakli o'zgaruvchilar (Environment):
  BOT_TOKEN     BotFather tokeni (raqamlar:harflar)
  DATABASE_URL  Postgres manzili (Neon). Busiz bronlar deployda yo'qoladi!
  PUBLIC_URL    Servis manzili (Render o'zi beradi: RENDER_EXTERNAL_URL)
Ixtiyoriy:
  TZ_NAME           Vaqt mintaqasi (standart: Asia/Tashkent)
  ESKIZ_EMAIL, ESKIZ_PASSWORD   Sartaroshga haqiqiy SMS yuborish (Eskiz.uz). Bo'lmasa faqat Telegram xabari.
"""
import os
import re
import sys
import gzip
import json
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _n in ("httpx", "httpcore"):  # URL (ichida token) logga tushmasin
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("barber")

# ---------------- Sozlamalar ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip().strip("\"'")
if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
    sys.exit("BOT_TOKEN noto'g'ri. U 'raqamlar:harflar' ko'rinishida (':' bilan) bo'lishi kerak.")

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tashkent"))
PORT = int(os.environ.get("PORT", "10000"))
APP_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
ESKIZ_EMAIL = os.getenv("ESKIZ_EMAIL", "").strip()
ESKIZ_PASSWORD = os.getenv("ESKIZ_PASSWORD", "").strip()
MAX_ACTIVE_PER_USER = 3      # mijozda bir vaqtda nechta faol bron
MIN_NOTICE_MIN = 30          # hozirdan necha daqiqadan keyingi vaqtni band qilish mumkin
INIT_DATA_MAX_AGE = 2 * 86400
LANGS = ("uz", "ru", "en")

WD_L = {
    "uz": ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"],
    "ru": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
}
MON_L = {
    "uz": ["", "yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"],
    "ru": ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
    "en": ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}

# Server matnlari: [uz, ru, en]
TX = {
    "hello": ["Assalomu alaykum, {name}! 👋\n\nSartaroshlarni ko'ring, o'zingizga mosini tanlang va vaqt band qiling. Agar o'zingiz sartarosh bo'lsangiz, ilovada o'z sahifangizni yarating.",
              "Здравствуйте, {name}! 👋\n\nВыбирайте мастера и записывайтесь онлайн. Если вы барбер — создайте свою страницу в приложении.",
              "Hello, {name}! 👋\n\nBrowse barbers, pick the one you like and book a time. If you are a barber, create your own page in the app."],
    "no_url": ["⚠️ Mini App manzili sozlanmagan (PUBLIC_URL).", "⚠️ Адрес Mini App не настроен (PUBLIC_URL).", "⚠️ Mini App URL is not configured (PUBLIC_URL)."],
    "btn_open": ["✂️ Ochish", "✂️ Открыть", "✂️ Open"],
    "btn_my": ["🧾 Bronlarim", "🧾 Мои записи", "🧾 My bookings"],
    "cmd_start": ["Boshlash", "Начать", "Start"],
    "som": ["so'm", "сум", "UZS"],
    "l_barber": ["Sartarosh", "Мастер", "Barber"], "l_service": ["Xizmat", "Услуга", "Service"],
    "l_time": ["Vaqt", "Время", "Time"], "l_price": ["Narx", "Цена", "Price"],
    "l_client": ["Mijoz", "Клиент", "Client"], "l_phone": ["Telefon", "Телефон", "Phone"],
    "l_addr": ["Manzil", "Адрес", "Address"],
    "t_booked": ["✅ <b>Bron tasdiqlandi</b>", "✅ <b>Запись подтверждена</b>", "✅ <b>Booking confirmed</b>"],
    "t_new": ["🆕 <b>Yangi bron</b>", "🆕 <b>Новая запись</b>", "🆕 <b>New booking</b>"],
    "t_cancel_client": ["❌ <b>Mijoz bronni bekor qildi</b>", "❌ <b>Клиент отменил запись</b>", "❌ <b>The client cancelled the booking</b>"],
    "t_cancel_barber": ["❌ <b>Broningiz bekor qilindi</b>\nSartarosh bronni bekor qildi. Boshqa vaqtni tanlashingiz mumkin.",
                        "❌ <b>Ваша запись отменена</b>\nМастер отменил запись. Вы можете выбрать другое время.",
                        "❌ <b>Your booking was cancelled</b>\nThe barber cancelled it. You can pick another time."],
    "t_remind": ["⏰ <b>Eslatma: tez orada bron</b>", "⏰ <b>Напоминание: скоро запись</b>", "⏰ <b>Reminder: booking soon</b>"],
    "t_sms": ["Yangi bron: {name}, {when}, {service}. Tel: {phone}", "Новая запись: {name}, {when}, {service}. Тел: {phone}",
              "New booking: {name}, {when}, {service}. Tel: {phone}"],
    # xatolar
    "e_auth": ["Ilovani Telegram orqali qayta oching", "Откройте приложение через Telegram заново", "Please reopen the app from Telegram"],
    "e_big": ["So'rov juda katta", "Запрос слишком большой", "Request too large"],
    "e_req": ["So'rov noto'g'ri", "Некорректный запрос", "Invalid request"],
    "e_nf": ["Topilmadi", "Не найдено", "Not found"],
    "e_srv": ["Server xatosi, birozdan keyin qayta urinib ko'ring", "Ошибка сервера, попробуйте чуть позже", "Server error, please try again shortly"],
    "e_forbidden": ["Ruxsat yo'q", "Нет доступа", "Not allowed"],
    "e_date": ["Sana noto'g'ri", "Неверная дата", "Invalid date"],
    "e_time": ["Vaqt noto'g'ri (SS:DD)", "Неверное время (ЧЧ:ММ)", "Invalid time (HH:MM)"],
    "e_name": ["Ismingizni kiriting", "Введите имя", "Enter your name"],
    "e_phone": ["Telefon raqamini to'g'ri kiriting", "Введите корректный номер телефона", "Enter a valid phone number"],
    "e_taken": ["Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.", "Это время уже занято. Выберите другое.", "This time was just taken. Please pick another."],
    "e_many": [f"Sizda allaqachon {MAX_ACTIVE_PER_USER} ta faol bron bor. Avval birortasini bekor qiling.",
               f"У вас уже {MAX_ACTIVE_PER_USER} активные записи. Сначала отмените одну.",
               f"You already have {MAX_ACTIVE_PER_USER} active bookings. Cancel one first."],
    "e_cancelled": ["Bu bron allaqachon bekor qilingan", "Эта запись уже отменена", "This booking is already cancelled"],
    "e_past": ["O'tib ketgan bronni bekor qilib bo'lmaydi", "Нельзя отменить прошедшую запись", "A past booking cannot be cancelled"],
    "e_barber": ["Sartarosh topilmadi", "Мастер не найден", "Barber not found"],
    "e_service": ["Xizmat topilmadi", "Услуга не найдена", "Service not found"],
    "e_sname": ["Xizmat nomini kiriting", "Введите название услуги", "Enter the service name"],
    "e_price": ["Narx noto'g'ri", "Неверная цена", "Invalid price"],
    "e_dur": ["Davomiylik 5 dan 600 daqiqagacha bo'lsin", "Длительность от 5 до 600 минут", "Duration must be 5 to 600 minutes"],
    "e_hours": ["Ish vaqti noto'g'ri: tugash vaqti boshlanishdan keyin bo'lishi kerak", "Неверные часы: конец должен быть позже начала", "Invalid hours: closing must be after opening"],
    "e_shop": ["Sartaroshxona nomini kiriting", "Введите название барбершопа", "Enter the shop name"],
    "e_bname": ["Ismingizni kiriting", "Введите ваше имя", "Enter your name"],
    "e_addr": ["Manzilni kiriting", "Введите адрес", "Enter the address"],
    "e_exp": ["Tajriba 0 dan 60 yilgacha", "Опыт от 0 до 60 лет", "Experience must be 0 to 60 years"],
    "e_opt": ["Sozlama noto'g'ri", "Неверная настройка", "Invalid setting"],
    "e_photo": ["Rasm noto'g'ri yoki juda katta", "Фото неверное или слишком большое", "Photo is invalid or too large"],
    "e_exists": ["Siz allaqachon sartaroshsiz", "Вы уже зарегистрированы как мастер", "You are already registered as a barber"],
}


def tx(key, lang="uz", **kw):
    arr = TX[key]
    s = arr[LANGS.index(lang) if lang in LANGS else 0]
    return s.format(**kw) if kw else s


def detect_lang(code):
    code = (code or "").lower()
    return "ru" if code.startswith("ru") else ("en" if code.startswith("en") else "uz")


# ---------------- Baza ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///barber.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
IS_PG = "postgresql" in DATABASE_URL
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=240)
BOOK_LOCK = threading.Lock()


def now_local():
    return dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def to_dt(v):
    return v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))


def rows(sql, **p):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings().all()]


def row(sql, **p):
    r = rows(sql, **p)
    return r[0] if r else None


def scalar(sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar()


def run(sql, **p):
    with engine.begin() as c:
        c.execute(text(sql), p)


def init_db():
    if DATABASE_URL.startswith("sqlite") and os.getenv("RENDER"):
        log.warning("DIQQAT: SQLite Render'da har deployda o'chadi. DATABASE_URL (Postgres) kiriting!")
    ident = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS app_users (id BIGINT PRIMARY KEY, name VARCHAR(100), phone VARCHAR(30), "
            "lang VARCHAR(2) NOT NULL DEFAULT 'uz', role VARCHAR(10), created_at TIMESTAMP NOT NULL)"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS barbers (user_id BIGINT PRIMARY KEY, shop_name VARCHAR(80) NOT NULL, "
            "barber_name VARCHAR(80) NOT NULL, address VARCHAR(250) NOT NULL, phone VARCHAR(30) NOT NULL, "
            "about TEXT, experience INTEGER NOT NULL DEFAULT 0, photo TEXT, photo_v INTEGER NOT NULL DEFAULT 0, "
            "step INTEGER NOT NULL DEFAULT 30, remind_hours INTEGER NOT NULL DEFAULT 2, "
            "horizon INTEGER NOT NULL DEFAULT 30, active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP NOT NULL)"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS barber_services (id {ident}, barber_id BIGINT NOT NULL, "
            "name VARCHAR(100) NOT NULL, price BIGINT NOT NULL, duration INTEGER NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_bs_barber ON barber_services (barber_id)"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS barber_hours (barber_id BIGINT NOT NULL, weekday INTEGER NOT NULL, "
            "open_t VARCHAR(5), close_t VARCHAR(5), is_off INTEGER NOT NULL DEFAULT 0, "
            "br_start VARCHAR(5), br_end VARCHAR(5), PRIMARY KEY (barber_id, weekday))"))
        c.execute(text("CREATE TABLE IF NOT EXISTS barber_days_off (barber_id BIGINT NOT NULL, d VARCHAR(10) NOT NULL, "
                       "PRIMARY KEY (barber_id, d))"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS appointments (id {ident}, barber_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
            "name VARCHAR(100) NOT NULL, phone VARCHAR(30) NOT NULL, service_id INTEGER, "
            "service_name VARCHAR(100) NOT NULL, price BIGINT NOT NULL, duration INTEGER NOT NULL, "
            "start_at TIMESTAMP NOT NULL, end_at TIMESTAMP NOT NULL, status VARCHAR(12) NOT NULL DEFAULT 'active', "
            "cancelled_by VARCHAR(10), created_at TIMESTAMP NOT NULL, reminded INTEGER NOT NULL DEFAULT 0)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_ap_barber ON appointments (barber_id, start_at)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_ap_user ON appointments (user_id, start_at)"))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_ap_active ON appointments (barber_id, start_at) "
                       "WHERE status = 'active'"))


# ---------------- Yordamchilar ----------------
def fmt_money(n):
    return f"{int(n):,}".replace(",", " ")


def fmt_when(date_str, time_str, lang="uz"):
    d = dt.date.fromisoformat(date_str)
    wd, mo = WD_L[lang][d.weekday()], MON_L[lang][d.month]
    if lang == "en":
        return f"{wd}, {mo} {d.day}, {time_str}"
    sep = "-" if lang == "uz" else " "
    return f"{d.day}{sep}{mo}, {wd.lower()}, {time_str}"


def hm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def get_lang(uid):
    r = scalar("SELECT lang FROM app_users WHERE id=:i", i=uid)
    return r if r in LANGS else "uz"


BCOLS = ("user_id, shop_name, barber_name, address, phone, about, experience, photo_v, step, remind_hours, "
         "horizon, active, CASE WHEN photo IS NULL THEN 0 ELSE 1 END AS has_photo")


def get_barber(bid):
    return row(f"SELECT {BCOLS} FROM barbers WHERE user_id=:i", i=bid)


def default_hours():
    return [{"open": "09:00", "close": "20:00", "off": 1 if wd == 6 else 0, "br_start": "", "br_end": ""} for wd in range(7)]


def get_hours(bid):
    h = default_hours()
    for r in rows("SELECT * FROM barber_hours WHERE barber_id=:b", b=bid):
        h[r["weekday"]] = {"open": r["open_t"] or "09:00", "close": r["close_t"] or "20:00", "off": int(r["is_off"]),
                           "br_start": r["br_start"] or "", "br_end": r["br_end"] or ""}
    return h


def get_days_off(bid):
    today = now_local().date().isoformat()
    return sorted(r["d"] for r in rows("SELECT d FROM barber_days_off WHERE barber_id=:b AND d>=:t", b=bid, t=today))


def sv_out(r):
    return {"id": r["id"], "name": r["name"], "price": int(r["price"]), "duration": int(r["duration"]), "active": int(r["active"])}


def list_services(bid, only_active=True):
    q = "SELECT * FROM barber_services WHERE barber_id=:b" + (" AND active=1" if only_active else "") + " ORDER BY sort_order, id"
    return [sv_out(r) for r in rows(q, b=bid)]


def fetch_busy(bid, start, end, c=None):
    sql = ("SELECT start_at, end_at FROM appointments WHERE barber_id=:b AND status='active' "
           "AND start_at < :e AND end_at > :s")
    p = {"b": bid, "s": start, "e": end}
    if c is not None:
        rs = c.execute(text(sql), p).mappings().all()
    else:
        with engine.connect() as cc:
            rs = cc.execute(text(sql), p).mappings().all()
    return [(to_dt(r["start_at"]), to_dt(r["end_at"])) for r in rs]


def day_slots(d, duration, step, h, busy, now):
    if not h or h["off"]:
        return []
    o, cl = hm(h["open"]), hm(h["close"])
    bs, be = (hm(h["br_start"]), hm(h["br_end"])) if h["br_start"] and h["br_end"] else (None, None)
    base = dt.datetime.combine(d, dt.time.min)
    earliest = now + dt.timedelta(minutes=MIN_NOTICE_MIN)
    out, t = [], o
    while t + duration <= cl:
        e = t + duration
        s_dt, e_dt = base + dt.timedelta(minutes=t), base + dt.timedelta(minutes=e)
        free = not (bs is not None and t < be and e > bs)
        if free and s_dt >= earliest and not any(s_dt < b_e and e_dt > b_s for b_s, b_e in busy):
            out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += step
    return out


def ap_out(r):
    s, e = to_dt(r["start_at"]), to_dt(r["end_at"])
    return {"id": r["id"], "barber_id": r["barber_id"], "date": s.date().isoformat(), "time": s.strftime("%H:%M"),
            "end": e.strftime("%H:%M"), "service": r["service_name"], "price": int(r["price"]),
            "duration": int(r["duration"]), "status": r["status"], "name": r["name"], "phone": r["phone"]}


def get_ap(aid):
    return row("SELECT a.*, b.shop_name, b.barber_name, b.address AS b_address, b.phone AS b_phone "
               "FROM appointments a JOIN barbers b ON b.user_id=a.barber_id WHERE a.id=:i", i=aid)


# ---------------- Telegram / SMS xabarlari ----------------
def tg_send(chat_id, text_, buttons=None):
    """'ok' | 'blocked' (foydalanuvchi botni bloklagan) | 'fail'"""
    payload = {"chat_id": chat_id, "text": text_, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
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


_sms_tok = {"v": None, "t": 0}


def sms_send(phone, msg):
    """Ixtiyoriy: Eskiz.uz orqali SMS. Sozlanmagan bo'lsa jim o'tadi."""
    if not (ESKIZ_EMAIL and ESKIZ_PASSWORD):
        return
    try:
        def post(url, fields, token=None):
            body = urllib.parse.urlencode(fields).encode()
            req = urllib.request.Request(url, data=body)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not _sms_tok["v"] or time.time() - _sms_tok["t"] > 20 * 86400:
            r = post("https://notify.eskiz.uz/api/auth/login", {"email": ESKIZ_EMAIL, "password": ESKIZ_PASSWORD})
            _sms_tok["v"], _sms_tok["t"] = r["data"]["token"], time.time()
        post("https://notify.eskiz.uz/api/message/sms/send",
             {"mobile_phone": re.sub(r"\D", "", phone), "message": msg, "from": "4546"}, _sms_tok["v"])
    except Exception as e:
        log.warning("SMS yuborilmadi: %s", type(e).__name__)


def app_button(label, tab=""):
    if not APP_URL:
        return None
    return [[{"text": label, "web_app": {"url": APP_URL + (f"/?tab={tab}" if tab else "/")}}]]


def _lines(rowd, lang, for_barber):
    a = ap_out(rowd)
    when = fmt_when(a["date"], a["time"], lang)
    L = lambda k: tx(k, lang)
    e = html.escape
    if for_barber:
        out = [f"👤 {L('l_client')}: <b>{e(a['name'])}</b>", f"📞 {L('l_phone')}: {e(a['phone'])}"]
    else:
        out = [f"💈 {L('l_barber')}: <b>{e(rowd['shop_name'])}</b> ({e(rowd['barber_name'])})"]
    out += [f"✂️ {L('l_service')}: {e(a['service'])}", f"🕒 {L('l_time')}: <b>{e(when)}</b>",
            f"💵 {L('l_price')}: {fmt_money(a['price'])} {L('som')}"]
    if not for_barber and rowd.get("b_address"):
        out.append(f"📍 {L('l_addr')}: {e(rowd['b_address'])}")
    return "\n".join(out), when


def notify_booked(aid):
    r = get_ap(aid)
    if not r:
        return
    bl, cl = get_lang(r["barber_id"]), get_lang(r["user_id"])
    body, when = _lines(r, bl, True)
    tg_send(r["barber_id"], f"{tx('t_new', bl)}\n\n{body}", app_button("📋", "bookings"))
    sms_send(r["b_phone"], tx("t_sms", bl, name=r["name"], when=when, service=r["service_name"], phone=r["phone"]))
    body, _ = _lines(r, cl, False)
    tg_send(r["user_id"], f"{tx('t_booked', cl)}\n\n{body}", app_button(tx("btn_my", cl), "my"))


def notify_cancelled(aid, by):
    r = get_ap(aid)
    if not r:
        return
    if by == "client":
        lg = get_lang(r["barber_id"])
        body, _ = _lines(r, lg, True)
        tg_send(r["barber_id"], f"{tx('t_cancel_client', lg)}\n\n{body}", app_button("📋", "bookings"))
    else:
        lg = get_lang(r["user_id"])
        body, _ = _lines(r, lg, False)
        tg_send(r["user_id"], f"{tx('t_cancel_barber', lg)}\n\n{body}", app_button(tx("btn_open", lg), ""))


def run_reminders():
    now = now_local()
    rs = rows("SELECT a.id, a.user_id, a.barber_id, a.start_at, b.remind_hours FROM appointments a "
              "JOIN barbers b ON b.user_id=a.barber_id WHERE a.status='active' AND a.reminded=0 "
              "AND a.start_at > :n AND a.start_at <= :m", n=now, m=now + dt.timedelta(hours=3))
    for r in rs:
        if to_dt(r["start_at"]) - now > dt.timedelta(hours=int(r["remind_hours"])):
            continue
        full = get_ap(r["id"])
        cl = get_lang(r["user_id"])
        body, _ = _lines(full, cl, False)
        res = tg_send(r["user_id"], f"{tx('t_remind', cl)}\n\n{body}", app_button(tx("btn_my", cl), "my"))
        if res in ("ok", "blocked"):
            run("UPDATE appointments SET reminded=1 WHERE id=:i", i=r["id"])


# ---------------- Tekshiruv ----------------
def verify_init_data(init_data):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa foydalanuvchi (dict), aks holda None."""
    try:
        data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        if not got:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(data.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
            return None
        user = json.loads(data["user"])
        user["id"] = int(user["id"])
        return user
    except Exception:
        return None


class ApiError(Exception):
    """key — TX kaliti; til so'rov bosqichida tanlanadi."""
    def __init__(self, key, code=400):
        super().__init__(key)
        self.key, self.code = key, code


def _str(v, n):
    return str(v or "").strip()[:n]


def _int(v, lo, hi, key):
    try:
        n = int(float(str(v).replace(" ", "")))
    except Exception:
        raise ApiError(key)
    if not lo <= n <= hi:
        raise ApiError(key)
    return n


def _date(v):
    try:
        return dt.date.fromisoformat(str(v))
    except Exception:
        raise ApiError("e_date")


def _hm(v, empty_ok=False):
    v = str(v or "").strip()
    if not v and empty_ok:
        return ""
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
        raise ApiError("e_time")
    return v


def norm_phone(v):
    digits = re.sub(r"\D", "", str(v or ""))
    if len(digits) == 9:
        digits = "998" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else None


def need_barber(user):
    b = get_barber(user["id"])
    if not b:
        raise ApiError("e_forbidden", 403)
    return b


# ---------------- API: umumiy ----------------
def barber_out(b, private=False):
    o = {"id": b["user_id"], "shop_name": b["shop_name"], "barber_name": b["barber_name"], "address": b["address"],
         "phone": b["phone"], "about": b["about"] or "", "experience": int(b["experience"]),
         "photo": f"/photo?b={b['user_id']}&v={b['photo_v']}" if b["has_photo"] else "", "horizon": int(b["horizon"])}
    if private:
        o.update(step=int(b["step"]), remind_hours=int(b["remind_hours"]), active=int(b["active"]))
    return o


def list_barbers():
    rs = rows("SELECT " + BCOLS + ", (SELECT MIN(price) FROM barber_services s WHERE s.barber_id=barbers.user_id AND s.active=1) AS min_price, "
              "(SELECT COUNT(*) FROM barber_services s WHERE s.barber_id=barbers.user_id AND s.active=1) AS n_sv "
              "FROM barbers WHERE active=1 ORDER BY created_at DESC")
    out = []
    for r in rs:
        if not r["n_sv"]:
            continue
        o = barber_out(r)
        o["about"] = o["about"][:140]
        o["min_price"] = int(r["min_price"] or 0)
        out.append(o)
    return out


def api_init(user, b):
    uid = user["id"]
    u = row("SELECT id, name, phone, lang, role FROM app_users WHERE id=:i", i=uid)
    if not u:
        full = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x)[:100]
        u = {"id": uid, "name": full, "phone": "", "lang": detect_lang(user.get("language_code")), "role": None}
        try:
            run("INSERT INTO app_users (id, name, phone, lang, role, created_at) VALUES (:i, :n, '', :l, NULL, :c)",
                i=uid, n=u["name"], l=u["lang"], c=now_local())
        except Exception:
            pass  # bir vaqtda ikki so'rov: ikkinchisi e'tiborsiz
    own = get_barber(uid)
    role = "barber" if own else u["role"]
    return {"user": {"id": uid, "name": u["name"] or "", "phone": u["phone"] or "", "lang": u["lang"], "role": role},
            "today": now_local().date().isoformat(), "me": barber_out(own, True) if own else None,
            "barbers": list_barbers()}


def api_set_role(user, b):
    if b.get("role") != "client":
        raise ApiError("e_forbidden", 403)
    run("UPDATE app_users SET role='client' WHERE id=:i AND role IS NULL", i=user["id"])
    return {"ok": True}


def api_lang(user, b):
    lang = b.get("lang")
    if lang not in LANGS:
        raise ApiError("e_req")
    run("UPDATE app_users SET lang=:l WHERE id=:i", l=lang, i=user["id"])
    return {"ok": True}


def _active_barber(bid):
    try:
        bid = int(bid)
    except Exception:
        raise ApiError("e_barber", 404)
    bar = get_barber(bid)
    if not bar or not bar["active"]:
        raise ApiError("e_barber", 404)
    return bar


def _service(bid, sid, only_active=True):
    try:
        sid = int(sid)
    except Exception:
        raise ApiError("e_service", 404)
    r = row("SELECT * FROM barber_services WHERE id=:i AND barber_id=:b", i=sid, b=bid)
    if not r or (only_active and not r["active"]):
        raise ApiError("e_service", 404)
    return sv_out(r)


def api_barber(user, b):
    bar = _active_barber(b.get("id"))
    return {"barber": barber_out(bar), "services": list_services(bar["user_id"]),
            "hours": get_hours(bar["user_id"]), "days_off": get_days_off(bar["user_id"])}


def api_days(user, b):
    bar = _active_barber(b.get("barber_id"))
    sv = _service(bar["user_id"], b.get("service_id"))
    now = now_local()
    today = now.date()
    hours, off = get_hours(bar["user_id"]), set(get_days_off(bar["user_id"]))
    last = today + dt.timedelta(days=int(bar["horizon"]))
    busy_all = fetch_busy(bar["user_id"], dt.datetime.combine(today, dt.time.min),
                          dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min))
    days = []
    d = today
    while d <= last:
        iso = d.isoformat()
        is_open = False
        if iso not in off:
            day_busy = [x for x in busy_all if x[0].date() == d]
            is_open = bool(day_slots(d, sv["duration"], int(bar["step"]), hours[d.weekday()], day_busy, now))
        days.append({"date": iso, "open": is_open})
        d += dt.timedelta(days=1)
    return {"days": days}


def _slots_for(bar, sv, d, now, c=None):
    if d.isoformat() in set(get_days_off(bar["user_id"])):
        return []
    mid = dt.datetime.combine(d, dt.time.min)
    busy = fetch_busy(bar["user_id"], mid, mid + dt.timedelta(days=1), c)
    return day_slots(d, sv["duration"], int(bar["step"]), get_hours(bar["user_id"])[d.weekday()], busy, now)


def api_slots(user, b):
    bar = _active_barber(b.get("barber_id"))
    sv = _service(bar["user_id"], b.get("service_id"))
    return {"slots": _slots_for(bar, sv, _date(b.get("date")), now_local())}


def api_book(user, b):
    bar = _active_barber(b.get("barber_id"))
    sv = _service(bar["user_id"], b.get("service_id"))
    d, t = _date(b.get("date")), _hm(b.get("time"))
    name = _str(b.get("name"), 60)
    if len(name) < 2:
        raise ApiError("e_name")
    phone = norm_phone(b.get("phone"))
    if not phone:
        raise ApiError("e_phone")
    now = now_local()
    if d < now.date() or d > now.date() + dt.timedelta(days=int(bar["horizon"])):
        raise ApiError("e_date")
    start = dt.datetime.combine(d, dt.time(int(t[:2]), int(t[3:])))
    end = start + dt.timedelta(minutes=sv["duration"])
    with BOOK_LOCK:
        with engine.begin() as c:
            if IS_PG:  # bir nechta server nusxasi bo'lsa ham bronlar ketma-ket tekshiriladi
                c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": bar["user_id"]})
            if t not in _slots_for(bar, sv, d, now, c):
                raise ApiError("e_taken", 409)
            n = c.execute(text("SELECT COUNT(*) FROM appointments WHERE user_id=:u AND status='active' AND start_at > :n"),
                          {"u": user["id"], "n": now}).scalar()
            if n >= MAX_ACTIVE_PER_USER:
                raise ApiError("e_many")
            soon = 1 if start - now <= dt.timedelta(hours=int(bar["remind_hours"])) else 0
            try:
                aid = c.execute(text(
                    "INSERT INTO appointments (barber_id, user_id, name, phone, service_id, service_name, price, duration, "
                    "start_at, end_at, status, created_at, reminded) VALUES (:b, :u, :n, :p, :sid, :sn, :pr, :du, :s, :e, "
                    "'active', :c, :r) RETURNING id"),
                    {"b": bar["user_id"], "u": user["id"], "n": name, "p": phone, "sid": sv["id"], "sn": sv["name"],
                     "pr": sv["price"], "du": sv["duration"], "s": start, "e": end, "c": now, "r": soon}).scalar()
            except ApiError:
                raise
            except Exception as e:
                if "unique" in str(e).lower() or "constraint" in str(e).lower():
                    raise ApiError("e_taken", 409)
                raise
            c.execute(text("UPDATE app_users SET name=:n, phone=:p WHERE id=:i"), {"n": name, "p": phone, "i": user["id"]})
    threading.Thread(target=notify_booked, args=(aid,), daemon=True).start()
    r = get_ap(aid)
    o = ap_out(r)
    o.update(shop=r["shop_name"], barber_name=r["barber_name"], address=r["b_address"])
    return {"booking": o}


def api_my(user, b):
    rs = rows("SELECT a.*, b.shop_name, b.barber_name, b.address AS b_address FROM appointments a "
              "JOIN barbers b ON b.user_id=a.barber_id WHERE a.user_id=:u ORDER BY a.start_at DESC LIMIT 60", u=user["id"])
    now = now_local()
    up, past = [], []
    for r in rs:
        o = ap_out(r)
        o.update(shop=r["shop_name"], barber_name=r["barber_name"], address=r["b_address"])
        (up if r["status"] == "active" and to_dt(r["start_at"]) >= now else past).append(o)
    up.reverse()
    return {"upcoming": up, "past": past}


def _cancel(aid, who_filter, by):
    with BOOK_LOCK:
        with engine.begin() as c:
            r = c.execute(text("SELECT status, start_at FROM appointments WHERE id=:i AND " + who_filter[0]),
                          dict(who_filter[1], i=aid)).mappings().first()
            if not r:
                raise ApiError("e_nf", 404)
            if r["status"] != "active":
                raise ApiError("e_cancelled")
            if to_dt(r["start_at"]) < now_local():
                raise ApiError("e_past")
            c.execute(text("UPDATE appointments SET status='cancelled', cancelled_by=:by WHERE id=:i"), {"by": by, "i": aid})
    threading.Thread(target=notify_cancelled, args=(aid, by), daemon=True).start()
    return {"ok": True}


def api_cancel(user, b):
    return _cancel(_int(b.get("id"), 1, 10 ** 12, "e_nf"), ("user_id=:u", {"u": user["id"]}), "client")


# ---------------- API: sartarosh ----------------
def api_register(user, b):
    if get_barber(user["id"]):
        raise ApiError("e_exists")
    f = _profile_fields(b)
    now = now_local()
    with engine.begin() as c:
        c.execute(text("INSERT INTO barbers (user_id, shop_name, barber_name, address, phone, about, experience, photo_v, "
                       "step, remind_hours, horizon, active, created_at) VALUES (:u, :shop, :bn, :ad, :ph, :ab, :ex, 0, 30, 2, 30, 1, :c)"),
                  {"u": user["id"], "shop": f["shop_name"], "bn": f["barber_name"], "ad": f["address"], "ph": f["phone"],
                   "ab": f["about"], "ex": f["experience"], "c": now})
        for wd, h in enumerate(default_hours()):
            c.execute(text("INSERT INTO barber_hours (barber_id, weekday, open_t, close_t, is_off, br_start, br_end) "
                           "VALUES (:b, :w, :o, :c, :f, '', '')"), {"b": user["id"], "w": wd, "o": h["open"], "c": h["close"], "f": h["off"]})
        # boshlang'ich xizmatlar yashirin turadi: sartarosh narxini qo'yib, yoqadi
        for i, (n, p, d) in enumerate([("Soch olish", 50000, 30), ("Soch ukladka", 40000, 30),
                                       ("Soqol olish", 30000, 20), ("Kombo (soch + soqol)", 70000, 50)]):
            c.execute(text("INSERT INTO barber_services (barber_id, name, price, duration, active, sort_order) "
                           "VALUES (:b, :n, :p, :d, 0, :s)"), {"b": user["id"], "n": n, "p": p, "d": d, "s": i})
        c.execute(text("UPDATE app_users SET role='barber', name=:n WHERE id=:i"), {"n": f["barber_name"], "i": user["id"]})
    return {"me": barber_out(get_barber(user["id"]), True)}


def _profile_fields(b):
    shop, bn, addr = _str(b.get("shop_name"), 80), _str(b.get("barber_name"), 80), _str(b.get("address"), 250)
    if len(shop) < 2:
        raise ApiError("e_shop")
    if len(bn) < 2:
        raise ApiError("e_bname")
    if len(addr) < 3:
        raise ApiError("e_addr")
    phone = norm_phone(b.get("phone"))
    if not phone:
        raise ApiError("e_phone")
    return {"shop_name": shop, "barber_name": bn, "address": addr, "phone": phone,
            "about": _str(b.get("about"), 600), "experience": _int(b.get("experience") or 0, 0, 60, "e_exp")}


def api_b_profile_save(user, b):
    need_barber(user)
    f = _profile_fields(b)
    step, rh, hz = _int(b.get("step"), 10, 120, "e_opt"), _int(b.get("remind_hours"), 1, 3, "e_opt"), _int(b.get("horizon"), 1, 90, "e_opt")
    act = 1 if b.get("active", 1) else 0
    run("UPDATE barbers SET shop_name=:shop, barber_name=:bn, address=:ad, phone=:ph, about=:ab, experience=:ex, "
        "step=:st, remind_hours=:rh, horizon=:hz, active=:ac WHERE user_id=:u",
        shop=f["shop_name"], bn=f["barber_name"], ad=f["address"], ph=f["phone"], ab=f["about"], ex=f["experience"],
        st=step, rh=rh, hz=hz, ac=act, u=user["id"])
    return {"me": barber_out(get_barber(user["id"]), True)}


def api_b_photo(user, b):
    need_barber(user)
    v = b.get("photo") or ""
    if not v:
        run("UPDATE barbers SET photo=NULL, photo_v=photo_v+1 WHERE user_id=:u", u=user["id"])
    else:
        m = re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", v)
        if not m or len(v) > 450_000:
            raise ApiError("e_photo")
        try:
            base64.b64decode(m.group(1), validate=True)
        except Exception:
            raise ApiError("e_photo")
        run("UPDATE barbers SET photo=:p, photo_v=photo_v+1 WHERE user_id=:u", p=v, u=user["id"])
    return {"me": barber_out(get_barber(user["id"]), True)}


def api_b_bookings(user, b):
    need_barber(user)
    d = _date(b.get("date"))
    s = dt.datetime.combine(d, dt.time.min)
    rs = rows("SELECT * FROM appointments WHERE barber_id=:b AND start_at >= :s AND start_at < :e ORDER BY start_at",
              b=user["id"], s=s, e=s + dt.timedelta(days=1))
    out = [ap_out(r) for r in rs]
    return {"bookings": out, "active": sum(1 for x in out if x["status"] == "active")}


def api_b_cancel(user, b):
    need_barber(user)
    return _cancel(_int(b.get("id"), 1, 10 ** 12, "e_nf"), ("barber_id=:u", {"u": user["id"]}), "barber")


def api_b_services(user, b):
    need_barber(user)
    return {"services": list_services(user["id"], only_active=False)}


def api_b_service_save(user, b):
    need_barber(user)
    name = _str(b.get("name"), 100)
    if not name:
        raise ApiError("e_sname")
    price, dur = _int(b.get("price"), 0, 100_000_000, "e_price"), _int(b.get("duration"), 5, 600, "e_dur")
    act = 1 if b.get("active", 1) else 0
    if b.get("id"):
        sv = _service(user["id"], b["id"], only_active=False)
        run("UPDATE barber_services SET name=:n, price=:p, duration=:d, active=:a WHERE id=:i AND barber_id=:b",
            n=name, p=price, d=dur, a=act, i=sv["id"], b=user["id"])
    else:
        if scalar("SELECT COUNT(*) FROM barber_services WHERE barber_id=:b", b=user["id"]) >= 40:
            raise ApiError("e_opt")
        mx = scalar("SELECT COALESCE(MAX(sort_order), 0) FROM barber_services WHERE barber_id=:b", b=user["id"])
        run("INSERT INTO barber_services (barber_id, name, price, duration, active, sort_order) VALUES (:b, :n, :p, :d, :a, :s)",
            b=user["id"], n=name, p=price, d=dur, a=act, s=int(mx) + 1)
    return {"services": list_services(user["id"], only_active=False)}


def api_b_service_delete(user, b):
    need_barber(user)
    sv = _service(user["id"], b.get("id"), only_active=False)
    run("DELETE FROM barber_services WHERE id=:i AND barber_id=:b", i=sv["id"], b=user["id"])
    return {"services": list_services(user["id"], only_active=False)}


def api_b_hours(user, b):
    need_barber(user)
    return {"hours": get_hours(user["id"]), "days_off": get_days_off(user["id"])}


def api_b_hours_save(user, b):
    need_barber(user)
    items = b.get("hours")
    if not isinstance(items, list) or len(items) != 7:
        raise ApiError("e_req")
    clean = []
    for h in items:
        off = 1 if h.get("off") else 0
        o, cl = _hm(h.get("open")), _hm(h.get("close"))
        bs, be = _hm(h.get("br_start"), True), _hm(h.get("br_end"), True)
        if not off:
            if hm(o) >= hm(cl):
                raise ApiError("e_hours")
            if bool(bs) != bool(be) or (bs and not (hm(o) <= hm(bs) < hm(be) <= hm(cl))):
                raise ApiError("e_hours")
        clean.append((o, cl, off, bs, be))
    with engine.begin() as c:
        c.execute(text("DELETE FROM barber_hours WHERE barber_id=:b"), {"b": user["id"]})
        for wd, (o, cl, off, bs, be) in enumerate(clean):
            c.execute(text("INSERT INTO barber_hours (barber_id, weekday, open_t, close_t, is_off, br_start, br_end) "
                           "VALUES (:b, :w, :o, :c, :f, :bs, :be)"), {"b": user["id"], "w": wd, "o": o, "c": cl, "f": off, "bs": bs, "be": be})
    return {"hours": get_hours(user["id"])}


def api_b_dayoff(user, b):
    need_barber(user)
    d = _date(b.get("date")).isoformat()
    conflicts = 0
    with engine.begin() as c:
        c.execute(text("DELETE FROM barber_days_off WHERE barber_id=:b AND d=:d"), {"b": user["id"], "d": d})
        if b.get("off", True):
            c.execute(text("INSERT INTO barber_days_off (barber_id, d) VALUES (:b, :d)"), {"b": user["id"], "d": d})
            day = dt.datetime.combine(dt.date.fromisoformat(d), dt.time.min)
            conflicts = c.execute(text("SELECT COUNT(*) FROM appointments WHERE barber_id=:b AND status='active' "
                                       "AND start_at >= :a AND start_at < :z"),
                                  {"b": user["id"], "a": day, "z": day + dt.timedelta(days=1)}).scalar()
    return {"days_off": get_days_off(user["id"]), "conflicts": int(conflicts or 0)}


API = {
    "init": api_init, "role": api_set_role, "lang": api_lang, "barber": api_barber,
    "days": api_days, "slots": api_slots, "book": api_book, "my": api_my, "cancel": api_cancel,
    "register": api_register,
    "b/profile_save": api_b_profile_save, "b/photo": api_b_photo, "b/bookings": api_b_bookings,
    "b/cancel": api_b_cancel, "b/services": api_b_services, "b/service_save": api_b_service_save,
    "b/service_delete": api_b_service_delete, "b/hours": api_b_hours, "b/hours_save": api_b_hours_save,
    "b/dayoff": api_b_dayoff,
}


# ---------------- HTTP server ----------------
PAGE = {"gz": b"", "etag": '""'}


def prepare_page():
    raw = APP_HTML.encode("utf-8")
    PAGE["raw"] = raw
    PAGE["gz"] = gzip.compress(raw, 9)
    PAGE["etag"] = '"' + hashlib.sha1(raw).hexdigest()[:16] + '"'


class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: har so'rovga yangi ulanish ochilmaydi (tezroq)
    server_version = "Barber"

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store", etag=None, gz=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        enc = None
        if (ctype.startswith("text/") or ctype.startswith("application/json")) and "gzip" in self.headers.get("Accept-Encoding", ""):
            if gz is not None:
                body, enc = gz, "gzip"
            elif len(body) > 900:
                body, enc = gzip.compress(body, 5), "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        if enc:
            self.send_header("Content-Encoding", enc)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path in ("/", "/app", "/index.html"):
            if self.headers.get("If-None-Match") == PAGE["etag"]:
                self.send_response(304)
                self.send_header("ETag", PAGE["etag"])
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, PAGE["raw"], "text/html; charset=utf-8", "no-cache", PAGE["etag"], PAGE["gz"])
        elif path == "/photo":
            try:
                q = dict(urllib.parse.parse_qsl(qs))
                v = scalar("SELECT photo FROM barbers WHERE user_id=:b", b=int(q.get("b", "0")))
                m = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,(.+)", v or "", re.S)
                if not m:
                    raise LookupError
                # v= har yuklashda o'zgaradi, shuning uchun brauzer uzoq saqlaydi
                self._send(200, base64.b64decode(m.group(2)), m.group(1), "public, max-age=31536000, immutable")
            except Exception:
                self._send(404, "-", "text/plain; charset=utf-8")
        elif path == "/health":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "-", "text/plain; charset=utf-8")

    def do_POST(self):
        lang = self.headers.get("X-Lang", "uz")
        lang = lang if lang in LANGS else "uz"
        err = lambda code, key: self._json(code, {"error": tx(key, lang)})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 600_000:
            self.close_connection = True
            return err(413, "e_big")
        raw = self.rfile.read(length) if length else b"{}"   # keep-alive uchun tanani har doim o'qiymiz
        path = self.path.split("?", 1)[0]
        fn = API.get(path[5:]) if path.startswith("/api/") else None
        if not fn:
            return err(404, "e_nf")
        user = verify_init_data(self.headers.get("X-Init-Data", ""))
        if not user:
            return err(401, "e_auth")
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return err(400, "e_req")
        try:
            self._json(200, fn(user, body))
        except ApiError as e:
            err(e.code, e.key)
        except Exception:
            log.exception("API xatosi: %s", path)
            err(500, "e_srv")

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Web)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("HTTP server %s portda ishga tushdi", PORT)
    return srv


# ---------------- Bot ----------------
def _kb(lang):
    if not APP_URL:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tx("btn_open", lang), web_app=WebAppInfo(url=APP_URL + "/"))],
        [InlineKeyboardButton(tx("btn_my", lang), web_app=WebAppInfo(url=APP_URL + "/?tab=my"))],
    ])


def _user_lang(u):
    r = scalar("SELECT lang FROM app_users WHERE id=:i", i=u.id)
    return r if r in LANGS else detect_lang(u.language_code)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    lang = await asyncio.to_thread(_user_lang, u)
    msg = tx("hello", lang, name=html.escape(u.first_name or ""))
    if not APP_URL:
        msg += "\n\n" + tx("no_url", lang)
    await update.effective_message.reply_text(msg, parse_mode="HTML", reply_markup=_kb(lang))


async def reminder_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(run_reminders)
    except Exception:
        log.exception("Eslatmalarda xato")


async def self_ping(ctx: ContextTypes.DEFAULT_TYPE):
    if not APP_URL:
        return
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
    await app.bot.set_my_commands([BotCommand("start", tx("cmd_start", "uz"))])
    for lg in ("ru", "en"):
        try:
            await app.bot.set_my_commands([BotCommand("start", tx("cmd_start", lg))], language_code=lg)
        except Exception as e:
            log.warning("Buyruqlar (%s) o'rnatilmadi: %s", lg, type(e).__name__)
    if APP_URL:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="✂️ Barber", web_app=WebAppInfo(url=APP_URL + "/")))
        except Exception as e:
            log.warning("Menu tugmasini o'rnatib bo'lmadi: %s", type(e).__name__)


def main():
    init_db()
    prepare_page()
    start_server()
    threading.Thread(target=lambda: scalar("SELECT 1"), daemon=True).start()   # bazani oldindan uyg'otish
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .get_updates_read_timeout(30).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, cmd_start))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(reminder_job, interval=60, first=20)
        app.job_queue.run_repeating(self_ping, interval=480, first=60)
    else:
        log.error("JobQueue yo'q: requirements.txt da python-telegram-bot[job-queue] bo'lsin!")
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


# ---------------- Mini App sahifasi ----------------
APP_HTML = r'''<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="color-scheme" content="light dark">
<title>Barber</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#14171c;--hint:#6b7280;--line:rgba(20,23,28,.08);--soft:rgba(20,23,28,.05);
  --gold:#c8923a;--gold2:#e2b25f;--gold-fg:#1c1303;--gold-soft:rgba(200,146,58,.14);--ok:#16a34a;--bad:#dc2626;
  --shadow:0 1px 2px rgba(20,23,28,.04),0 10px 28px rgba(20,23,28,.07);--safe-b:env(safe-area-inset-bottom,0px);--tab-h:62px}
:root[data-scheme=dark]{--bg:#0d0f12;--card:#171a1f;--fg:#f3f4f6;--hint:#9aa1ab;--line:rgba(255,255,255,.08);--soft:rgba(255,255,255,.07);
  --gold-soft:rgba(226,178,95,.16);--shadow:0 0 0 1px rgba(255,255,255,.04)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;padding-bottom:calc(var(--tab-h) + var(--safe-b) + 28px);overflow-x:hidden}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer;text-align:inherit}
a{color:inherit;text-decoration:none}
.wrap{max-width:560px;margin:0 auto;padding:14px 16px}
.fade{animation:fade .22s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
h1.page{font-size:26px;line-height:1.15;margin:6px 0 4px;letter-spacing:-.02em;font-weight:800}
h3.st{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--hint);margin:24px 0 10px;font-weight:700}
.hint{color:var(--hint);font-size:14px}
.ic{flex:none;display:block}
/* tugmalar */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:48px;padding:0 20px;border-radius:14px;font-weight:700;
  background:var(--soft);transition:transform .08s,opacity .15s;text-align:center}
.btn:active{transform:scale(.97)}
.btn.primary{background:linear-gradient(135deg,var(--gold2),var(--gold));color:var(--gold-fg);box-shadow:0 6px 18px rgba(200,146,58,.32)}
.btn.danger{color:var(--bad);background:rgba(220,38,38,.1)}
.btn.small{min-height:38px;padding:0 14px;border-radius:11px;font-size:14px}
.btn.wide{width:100%}
.btn[disabled]{opacity:.45;pointer-events:none;box-shadow:none}
/* kartalar */
.card{background:var(--card);border-radius:20px;box-shadow:var(--shadow);padding:16px}
.bcard{display:flex;gap:14px;align-items:center;width:100%;background:var(--card);border-radius:20px;padding:12px;margin:0 0 12px;box-shadow:var(--shadow);transition:transform .1s}
.bcard:active{transform:scale(.985)}
.av{width:74px;height:74px;border-radius:18px;object-fit:cover;display:grid;place-items:center;color:#fff;font-weight:800;font-size:26px;flex:none;background:var(--soft)}
.bc-b{min-width:0;flex:1}
.bc-t{font-weight:800;font-size:17px;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bc-s{color:var(--hint);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bc-a{display:flex;gap:5px;align-items:center;color:var(--hint);font-size:13px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
.tag{font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px;background:var(--soft)}
.tag.gold{background:var(--gold-soft);color:var(--gold)}
.chev{color:var(--hint);flex:none}
/* qidiruv */
.search{position:relative;margin:12px 0 16px}
.search svg{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--hint)}
.inp{width:100%;min-height:48px;padding:0 14px;border-radius:14px;border:1.5px solid transparent;background:var(--card);color:var(--fg);font:inherit;outline:0;box-shadow:var(--shadow)}
.search .inp{padding-left:42px}
textarea.inp{padding:12px 14px;min-height:96px;resize:vertical}
.inp:focus{border-color:var(--gold)}
.field{display:block;margin:0 0 14px}
.field>span{display:block;font-size:13px;color:var(--hint);margin:0 0 6px;font-weight:600}
/* sartarosh sahifasi */
.cover{margin:-14px -16px 0;height:210px;position:relative;background:linear-gradient(135deg,#2b2f36,#0f1114);overflow:hidden;border-radius:0 0 28px 28px}
.cover img{width:100%;height:100%;object-fit:cover;display:block}
.cover .ph{position:absolute;inset:0;display:grid;place-items:center;color:var(--gold2);opacity:.9}
.cover:after{content:"";position:absolute;inset:auto 0 0 0;height:90px;background:linear-gradient(transparent,rgba(0,0,0,.55))}
.pole{position:absolute;left:0;right:0;bottom:0;height:5px;z-index:2;background:repeating-linear-gradient(115deg,#d7263d 0 14px,#fff 14px 28px,#2563eb 28px 42px,#fff 42px 56px)}
.shop{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:18px 0 2px;line-height:1.15}
.lead{color:var(--hint);margin:8px 0 0;white-space:pre-line}
.info{list-style:none;margin:16px 0 0;padding:0;display:grid;gap:10px}
.info li{display:flex;gap:12px;align-items:flex-start;background:var(--card);border-radius:16px;padding:13px 14px;box-shadow:var(--shadow)}
.info li svg{color:var(--gold);margin-top:1px}
.info a{text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
details summary{cursor:pointer;list-style:none;font-weight:600}
details summary::-webkit-details-marker{display:none}
.wk{margin-top:8px;display:grid;gap:5px;font-size:14px}
.wk div{display:flex;justify-content:space-between;color:var(--hint)}
.wk div.today{color:var(--fg);font-weight:700}
/* xizmatlar */
.svc{display:flex;align-items:center;gap:12px;width:100%;background:var(--card);border-radius:16px;padding:14px;margin:0 0 10px;box-shadow:var(--shadow);border:1.5px solid transparent}
.svc.sel{border-color:var(--gold);background:var(--gold-soft)}
.svc.off{opacity:.55}
.svc-n{font-weight:700}
.svc-d{font-size:13px;color:var(--hint)}
.svc-p{margin-left:auto;font-weight:800;white-space:nowrap}
.svc-p small{font-weight:600;color:var(--hint)}
/* kun va vaqt */
.days{display:flex;gap:8px;overflow-x:auto;padding:2px 2px 8px;scrollbar-width:none;margin:0 -16px;padding-left:16px;padding-right:16px}
.days::-webkit-scrollbar{display:none}
.day{flex:none;width:58px;padding:9px 0;border-radius:16px;background:var(--card);text-align:center;box-shadow:var(--shadow);border:1.5px solid transparent}
.day small{display:block;font-size:12px;color:var(--hint);font-weight:600;text-transform:uppercase}
.day b{display:block;font-size:20px;margin-top:1px}
.day.sel{background:linear-gradient(135deg,var(--gold2),var(--gold));color:var(--gold-fg)}
.day.sel small{color:var(--gold-fg);opacity:.75}
.day.none{opacity:.35;pointer-events:none}
.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.slot{padding:12px 0;border-radius:13px;background:var(--card);text-align:center;font-weight:700;box-shadow:var(--shadow);border:1.5px solid transparent}
.slot.sel{background:linear-gradient(135deg,var(--gold2),var(--gold));color:var(--gold-fg)}
/* chipta */
.ticket{display:flex;background:var(--card);border-radius:18px;margin:0 0 12px;box-shadow:var(--shadow);overflow:hidden}
.ticket.off{opacity:.6}
.tk-l{width:84px;flex:none;padding:14px 8px;text-align:center;background:var(--gold-soft);border-right:2px dashed var(--line)}
.tk-time{font-size:22px;font-weight:800;color:var(--gold)}
.tk-end{font-size:12px;color:var(--hint)}
.tk-r{padding:13px 14px;min-width:0;flex:1}
.tk-name{font-weight:800;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tk-meta{font-size:14px;color:var(--hint);margin-top:3px}
.tk-act{margin-top:10px}
.badge{font-size:11px;font-weight:800;padding:2px 8px;border-radius:99px;text-transform:uppercase;letter-spacing:.03em}
.badge.bad{background:rgba(220,38,38,.12);color:var(--bad)}.badge.mut{background:var(--soft);color:var(--hint)}
.done-h{display:flex;gap:12px;align-items:center;margin:10px 0 16px}
.ok-dot{width:40px;height:40px;border-radius:50%;background:var(--ok);color:#fff;display:grid;place-items:center}
.empty{text-align:center;color:var(--hint);padding:36px 16px}
.empty b{display:block;color:var(--fg);font-size:18px;margin-bottom:6px}
.empty .btn{margin-top:16px}
/* rol tanlash */
.roles{display:grid;gap:14px;margin-top:22px}
.role{display:flex;gap:16px;align-items:center;background:var(--card);border-radius:22px;padding:20px;box-shadow:var(--shadow);transition:transform .1s}
.role:active{transform:scale(.98)}
.role .ri{width:54px;height:54px;border-radius:16px;display:grid;place-items:center;background:var(--gold-soft);color:var(--gold);flex:none}
.role b{display:block;font-size:18px}.role span{color:var(--hint);font-size:14px}
/* segment / switch */
.seg{display:flex;background:var(--soft);border-radius:14px;padding:4px;gap:4px;margin:12px 0}
.seg button{flex:1;text-align:center;padding:10px 4px;border-radius:11px;font-weight:700;font-size:14px;color:var(--hint)}
.seg button.on{background:var(--card);color:var(--fg);box-shadow:0 1px 4px rgba(0,0,0,.12)}
.sw{position:relative;width:48px;height:28px;flex:none}
.sw input{opacity:0;position:absolute;inset:0;width:100%;height:100%;margin:0}
.sw span{position:absolute;inset:0;border-radius:99px;background:var(--soft);transition:.2s;pointer-events:none}
.sw span:after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:.2s}
.sw input:checked+span{background:var(--gold)}.sw input:checked+span:after{transform:translateX(20px)}
.row{display:flex;gap:10px;align-items:center}
.hr{background:var(--card);border-radius:16px;padding:12px 14px;margin:0 0 10px;box-shadow:var(--shadow)}
.hr-top{display:flex;justify-content:space-between;align-items:center}
.hr-t{display:flex;gap:8px;align-items:center;margin-top:10px}
.hr-t .inp{min-height:42px;padding:0 10px;box-shadow:none;background:var(--soft)}
.hr-lb{font-size:13px;color:var(--hint);margin-top:10px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.chip{display:inline-flex;gap:6px;align-items:center;padding:6px 8px 6px 12px;border-radius:99px;background:var(--card);font-size:14px;box-shadow:var(--shadow)}
.photo{width:100%;height:160px;border-radius:16px;object-fit:cover;background:var(--soft);display:grid;place-items:center;color:var(--hint)}
/* pastki panel */
#tabs{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:space-around;padding:6px 6px calc(6px + var(--safe-b));
  background:color-mix(in srgb,var(--bg) 82%,transparent);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-top:1px solid var(--line)}
#tabs button{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 0;font-size:11px;font-weight:700;color:var(--hint);border-radius:12px;text-align:center}
#tabs button.on{color:var(--gold)}
.cta{position:fixed;left:0;right:0;bottom:0;z-index:35;padding:22px 16px calc(14px + var(--safe-b));background:linear-gradient(transparent,var(--bg) 38%)}
.cta .btn{max-width:528px;margin:0 auto;display:flex}
.cta-sp{height:84px}
/* sheet, toast */
.back{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:50;display:flex;align-items:flex-end;justify-content:center}
.sheet{width:100%;max-width:560px;max-height:90vh;overflow:auto;background:var(--bg);border-radius:24px 24px 0 0;padding:10px 16px calc(20px + var(--safe-b));animation:up .22s ease-out}
.sheet .grab{width:40px;height:4px;border-radius:2px;background:var(--line);margin:2px auto 14px}
@keyframes up{from{transform:translateY(60px);opacity:.3}to{transform:none;opacity:1}}
#toast{position:fixed;left:16px;right:16px;bottom:calc(var(--tab-h) + var(--safe-b) + 90px);z-index:60;display:flex;justify-content:center;pointer-events:none}
#toast div{max-width:420px;padding:12px 16px;border-radius:14px;background:var(--fg);color:var(--bg);font-size:14px;font-weight:600;box-shadow:0 8px 28px rgba(0,0,0,.3)}
.sk{border-radius:16px;background:linear-gradient(90deg,var(--soft),transparent,var(--soft));background-size:200% 100%;animation:sh 1.3s linear infinite;margin:0 0 12px}
@keyframes sh{from{background-position:200% 0}to{background-position:-200% 0}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:340px){.slots{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<main id="view" class="wrap"><div class="sk" style="height:34px;width:60%"></div><div class="sk" style="height:48px"></div><div class="sk" style="height:98px"></div><div class="sk" style="height:98px"></div><div class="sk" style="height:98px"></div></main>
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
var LI={uz:0,ru:1,en:2};
var L={
 n_list:["Sartaroshlar","Мастера","Barbers"],n_my:["Bronlarim","Мои записи","My bookings"],n_profile:["Profil","Профиль","Profile"],
 n_bookings:["Bronlar","Записи","Bookings"],n_services:["Xizmatlar","Услуги","Services"],n_hours:["Ish vaqti","График","Hours"],
 r_title:["Xush kelibsiz!","Добро пожаловать!","Welcome!"],r_sub:["Kim sifatida davom etasiz?","Как вы хотите продолжить?","How would you like to continue?"],
 r_client:["Men mijozman","Я клиент","I am a client"],r_client_d:["Sartaroshni tanlab, vaqt band qilaman","Выбираю мастера и записываюсь","Pick a barber and book a time"],
 r_barber:["Men sartaroshman","Я барбер","I am a barber"],r_barber_d:["O'z sahifamni yarataman, mijozlar yoziladi","Создаю свою страницу, клиенты записываются","Create my page and get bookings"],
 reg_title:["Sartarosh sifatida ro'yxatdan o'tish","Регистрация барбера","Barber registration"],
 reg_hint:["Bu ma'lumotlar mijozlarga ko'rinadi. Keyin o'zgartirish mumkin.","Эти данные увидят клиенты. Позже можно изменить.","Clients will see this. You can edit it later."],
 f_shop:["Sartaroshxona nomi","Название барбершопа","Shop name"],f_bname:["Ismingiz","Ваше имя","Your name"],f_addr:["Manzil","Адрес","Address"],
 f_phone:["Telefon","Телефон","Phone"],f_exp:["Tajriba (yil)","Опыт (лет)","Experience (years)"],f_about:["O'zingiz haqingizda","О себе","About you"],
 f_about_ph:["Tajriba, uslub, qaysi xizmatlarda kuchlisiz...","Опыт, стиль, в чём вы сильны...","Experience, style, what you do best..."],
 reg_submit:["Ro'yxatdan o'tish","Зарегистрироваться","Register"],
 reg_done:["Sahifangiz yaratildi! Endi xizmat va narxlarni kiriting.","Страница создана! Теперь добавьте услуги и цены.","Your page is ready! Now add services and prices."],
 s_ph:["Sartarosh yoki manzil bo'yicha qidirish","Поиск по мастеру или адресу","Search by barber or address"],
 l_empty:["Hozircha sartaroshlar yo'q","Пока нет мастеров","No barbers yet"],l_empty_d:["Tez orada paydo bo'ladi.","Скоро появятся.","They will appear soon."],
 l_none:["Hech narsa topilmadi","Ничего не найдено","Nothing found"],from:["dan","от","from"],yrs:["yil tajriba","лет опыта","yrs experience"],
 som:["so'm","сум","UZS"],mn:["daqiqa","мин","min"],hr:["soat","ч","h"],dy:["kun","дн","days"],
 b_about:["Sartarosh haqida","О мастере","About"],b_services:["Xizmatlar va narxlar","Услуги и цены","Services & prices"],
 b_book:["Band qilish","Записаться","Book now"],b_noserv:["Hozircha xizmatlar yo'q","Пока нет услуг","No services yet"],
 today:["Bugun","Сегодня","Today"],tomorrow:["Ertaga","Завтра","Tomorrow"],off:["Dam olish","Выходной","Closed"],
 today_off:["Bugun dam olish kuni","Сегодня выходной","Closed today"],
 bk_title:["Band qilish","Запись","Booking"],bk_service:["Xizmat","Услуга","Service"],bk_date:["Sana","Дата","Date"],bk_time:["Vaqt","Время","Time"],
 bk_you:["Ma'lumotlaringiz","Ваши данные","Your details"],f_name:["Ismingiz","Ваше имя","Your name"],f_phone2:["Telefon raqamingiz","Ваш телефон","Your phone"],
 bk_go:["Band qilish","Записаться","Book"],bk_fill:["Ism va telefonni kiriting","Введите имя и телефон","Enter name and phone"],sending:["Yuborilmoqda...","Отправка...","Sending..."],
 no_days:["Bo'sh kunlar yo'q","Нет свободных дней","No free days"],no_days_d:["Hozircha bu xizmat uchun bo'sh vaqt topilmadi.","Свободного времени для этой услуги пока нет.","No free time for this service yet."],
 no_slots:["Bu kunda bo'sh vaqt yo'q","На этот день нет времени","No free time this day"],no_slots_d:["Boshqa sanani tanlang.","Выберите другую дату.","Pick another date."],
 done_t:["Bron tasdiqlandi","Запись подтверждена","Booking confirmed"],done_d:["Tasdiqlash xabari Telegramga yuborildi. Vaqtidan oldin eslatamiz.","Подтверждение отправлено в Telegram. Мы напомним заранее.","Confirmation sent to Telegram. We will remind you beforehand."],
 again:["Yana band qilish","Записаться ещё","Book again"],my_t:["Bronlarim","Мои записи","My bookings"],
 my_empty:["Hali bron yo'q","Записей пока нет","No bookings yet"],my_empty_d:["Birinchi vaqtingizni band qiling.","Запишитесь в первый раз.","Book your first time."],
 my_none:["Kelgusi bronlar yo'q.","Предстоящих записей нет.","No upcoming bookings."],past:["Avvalgilar","Прошлые","Earlier"],
 cancel:["Bekor qilish","Отменить","Cancel"],cancelled:["Bekor qilingan","Отменена","Cancelled"],was:["O'tgan","Прошла","Past"],end:["tugash","конец","ends"],
 q_cancel:["Bronni bekor qilasizmi?","Отменить запись?","Cancel this booking?"],q_cancel_c:["Bronni bekor qilasizmi? Mijozga xabar yuboriladi.","Отменить запись? Клиент получит уведомление.","Cancel this booking? The client will be notified."],
 cancelled_ok:["Bron bekor qilindi","Запись отменена","Booking cancelled"],
 a_n:["ta faol bron","активных записей","active bookings"],a_empty:["Bu kunga bron yo'q","На этот день записей нет","No bookings for this day"],a_empty_d:["Yangi bron bo'lsa, Telegramda xabar keladi.","О новой записи придёт сообщение в Telegram.","You will get a Telegram message for new bookings."],
 sv_new:["Yangi xizmat","Новая услуга","New service"],sv_edit:["Xizmatni tahrirlash","Редактирование услуги","Edit service"],sv_name:["Nomi","Название","Name"],
 sv_price:["Narxi (so'm)","Цена (сум)","Price (UZS)"],sv_dur:["Davomiyligi (daqiqa)","Длительность (мин)","Duration (min)"],sv_show:["Mijozlarga ko'rsatilsin","Показывать клиентам","Show to clients"],
 sv_del:["Xizmatni o'chirish","Удалить услугу","Delete service"],sv_hidden:["yashirin","скрыта","hidden"],sv_q:["Xizmat o'chirilsin? Oldingi bronlar saqlanib qoladi.","Удалить услугу? Прошлые записи сохранятся.","Delete this service? Past bookings stay."],
 sv_hint:["Mijozlarga ko'rinish uchun kamida bitta xizmatni narxini tekshirib, yoqing.","Чтобы клиенты вас видели, проверьте цену и включите хотя бы одну услугу.","To appear to clients, check the price and switch on at least one service."],
 hr_save:["Ish vaqtini saqlash","Сохранить график","Save hours"],hr_break:["Tanaffus (ixtiyoriy)","Перерыв (необязательно)","Break (optional)"],
 do_t:["Dam olish kunlari","Выходные дни","Days off"],do_d:["Bayram yoki boshqa sabab bilan yopiq kunlar.","Праздники и другие закрытые дни.","Holidays or other closed days."],
 add:["Qo'shish","Добавить","Add"],rm:["O'chirish","Удалить","Remove"],none:["Hozircha yo'q","Пока нет","None yet"],pick_d:["Sanani tanlang","Выберите дату","Pick a date"],
 conflict:["ta bron shu kunga tushadi, ularni qo'lda bekor qiling","записей попадает на этот день, отмените их вручную","bookings fall on that day, cancel them manually"],
 saved:["Saqlandi","Сохранено","Saved"],deleted:["O'chirildi","Удалено","Deleted"],save:["Saqlash","Сохранить","Save"],
 p_lang:["Til","Язык","Language"],p_barber_mode:["Sartarosh paneliga o'tish","Перейти в панель барбера","Open barber panel"],p_client_mode:["Mijoz sifatida ko'rish","Смотреть как клиент","View as a client"],
 p_become:["Sartarosh bo'lish","Стать барбером","Become a barber"],p_become_d:["O'z sahifangizni yarating va mijozlardan bron qabul qiling.","Создайте свою страницу и принимайте записи.","Create your page and accept bookings."],
 p_page:["Sahifam","Моя страница","My page"],p_visible:["Mijozlarga ko'rinsin","Показывать клиентам","Visible to clients"],p_step:["Bron vaqti qadami","Шаг записи","Booking step"],
 p_remind:["Eslatma: bronga necha soat qolganda","Напоминание за сколько часов","Remind before (hours)"],p_hz:["Necha kun oldindan band qilish mumkin","На сколько дней вперёд можно записаться","How many days ahead clients can book"],
 p_photo:["Rasm","Фото","Photo"],p_photo_pick:["Rasm tanlash","Выбрать фото","Choose photo"],p_photo_rm:["Olib tashlash","Убрать","Remove"],p_photo_none:["Rasm yo'q","Нет фото","No photo"],
 photo_bad:["Rasmni o'qib bo'lmadi","Не удалось прочитать фото","Could not read the photo"],
 err:["Xatolik yuz berdi","Произошла ошибка","Something went wrong"],offline:["Internet bilan aloqa yo'q","Нет связи с интернетом","No internet connection"],
 open_fail:["Ochilmadi","Не открылось","Could not open"],open_fail_d:["Telegramdagi bot orqali ilovani qayta oching.","Откройте приложение через бота в Telegram.","Reopen the app from the Telegram bot."],
 retry:["Qayta urinish","Повторить","Retry"],hrs_today:["Bugun","Сегодня","Today"],week:["Haftalik jadval","График на неделю","Weekly hours"],close:["Yopish","Закрыть","Close"]
};
var LANG=(function(){var c=(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user&&tg.initDataUnsafe.user.language_code)||'';c=c.toLowerCase();return c.indexOf('ru')===0?'ru':(c.indexOf('en')===0?'en':'uz')})();
function t(k){var a=L[k];return a?(a[LI[LANG]]||a[0]):k}
var WDN={uz:["Dushanba","Seshanba","Chorshanba","Payshanba","Juma","Shanba","Yakshanba"],ru:["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"],en:["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]};
var WDS={uz:["Du","Se","Ch","Pa","Ju","Sh","Ya"],ru:["Пн","Вт","Ср","Чт","Пт","Сб","Вс"],en:["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]};
var MON={uz:["yanvar","fevral","mart","aprel","may","iyun","iyul","avgust","sentabr","oktabr","noyabr","dekabr"],ru:["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"],en:["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]};

/* ---------- yordamchilar ---------- */
function $(s,r){return (r||document).querySelector(s)}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function money(n){return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
function parseISO(s){var p=s.split('-').map(Number);return new Date(p[0],p[1]-1,p[2])}
function iso(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function addDays(s,n){var d=parseISO(s);d.setDate(d.getDate()+n);return iso(d)}
function wdi(d){return (d.getDay()+6)%7}
function dShort(s){var d=parseISO(s),m=MON[LANG][d.getMonth()];return LANG==='en'?m+' '+d.getDate():d.getDate()+(LANG==='uz'?'-':' ')+m}
function dFull(s){var d=parseISO(s);return WDN[LANG][wdi(d)]+', '+dShort(s)}
function phoneOk(p){var d=String(p||'').replace(/\D/g,'');return d.length>=9&&d.length<=15}
var ICONS={
 store:'<path d="M4 9 5.5 4h13L20 9"/><path d="M4 9a2.7 2.7 0 0 0 5.3 0 2.7 2.7 0 0 0 5.4 0A2.7 2.7 0 0 0 20 9"/><path d="M5 12v8h14v-8"/><path d="M10 20v-5h4v5"/>',
 scissors:'<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
 calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
 ticket:'<path d="M3 9a2 2 0 0 0 0 6v3h18v-3a2 2 0 0 1 0-6V6H3z"/><path d="M14 6v12" stroke-dasharray="2 3"/>',
 user:'<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/>',
 pin:'<path d="M12 21s7-6.2 7-11a7 7 0 0 0-14 0c0 4.8 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>',
 clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 phone:'<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/>',
 check:'<path d="m5 12.5 4.5 4.5L19 7"/>',x:'<path d="M6 6l12 12M18 6 6 18"/>',
 chev:'<path d="m9 6 6 6-6 6"/>',search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
 star:'<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>'
};
function ic(n,s){s=s||22;return '<svg class="ic" width="'+s+'" height="'+s+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'+ICONS[n]+'</svg>'}
function toast(msg){var el=$('#toast');el.innerHTML='<div>'+esc(msg)+'</div>';clearTimeout(toast.t);toast.t=setTimeout(function(){el.innerHTML=''},3200)}
function hap(k){try{if(tg&&tg.HapticFeedback){k==='ok'?tg.HapticFeedback.notificationOccurred('success'):tg.HapticFeedback.impactOccurred('light')}}catch(e){}}
function ask(msg,cb){if(tg&&tg.showConfirm){tg.showConfirm(msg,function(ok){if(ok)cb()})}else if(window.confirm(msg)){cb()}}
function applyScheme(){var s=(tg&&tg.colorScheme)||((window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light');root.dataset.scheme=s}
applyScheme();
if(tg){try{tg.ready();tg.expand();if(tg.disableVerticalSwipes)tg.disableVerticalSwipes();
  if(tg.setHeaderColor)tg.setHeaderColor('bg_color');if(tg.setBackgroundColor)tg.setBackgroundColor('bg_color');
  tg.onEvent('themeChanged',applyScheme);}catch(e){}}
function api(path,body){
  return fetch('/api/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Init-Data':tg?tg.initData:'','X-Lang':LANG},body:JSON.stringify(body||{})})
   .then(function(r){return r.json().catch(function(){return {}}).then(function(j){
      if(!r.ok){var e=new Error(j.error||t('err'));e.status=r.status;throw e}return j})},
    function(){var e=new Error(t('offline'));e.status=0;throw e});
}
function fail(e){toast(e&&e.message?e.message:t('err'))}
function avatar(b,size){
  var st=size?'width:'+size+'px;height:'+size+'px;':'';
  if(b.photo)return '<img class="av" style="'+st+'" src="'+esc(b.photo)+'" alt="" loading="lazy" decoding="async">';
  var n=(b.barber_name||b.shop_name||'?').trim(),h=0,i;for(i=0;i<n.length;i++)h=(h*31+n.charCodeAt(i))%360;
  var ini=n.split(/\s+/).slice(0,2).map(function(x){return x.charAt(0)}).join('').toUpperCase();
  return '<div class="av" style="'+st+'background:linear-gradient(135deg,hsl('+h+' 50% 42%),hsl('+((h+40)%360)+' 55% 30%))">'+esc(ini)+'</div>';
}
function compress(file,cb){
  var fr=new FileReader();
  fr.onerror=function(){cb(null)};
  fr.onload=function(){var img=new Image();img.onerror=function(){cb(null)};
    img.onload=function(){
      var out=null,tries=[[720,.8],[560,.7],[420,.6]],i;
      for(i=0;i<tries.length;i++){
        var m=tries[i][0],r=Math.min(1,m/Math.max(img.width,img.height)),c=document.createElement('canvas');
        c.width=Math.max(1,Math.round(img.width*r));c.height=Math.max(1,Math.round(img.height*r));
        c.getContext('2d').drawImage(img,0,0,c.width,c.height);out=c.toDataURL('image/jpeg',tries[i][1]);
        if(out.length<430000)break;
      }
      cb(out&&out.length<430000?out:null)};
    img.src=fr.result};
  fr.readAsDataURL(file);
}

/* ---------- holat ---------- */
var S={user:null,me:null,barbers:null,today:'',mode:'client',tab:'list',page:null,sheet:null,err:null,q:'',bp:{},key:'',
  bk:{},my:null,reg:{},pf:null,
  adm:{date:null,rows:null,services:null,hours:null,daysOff:null,offPick:''}};
var view=$('#view');

/* ---------- ko'rinishlar: umumiy ---------- */
function empty(title,text,btn){return '<div class="empty"><b>'+esc(title)+'</b>'+esc(text||'')+(btn||'')+'</div>'}
function skel(n){var h='',i;for(i=0;i<(n||3);i++)h+='<div class="sk" style="height:96px"></div>';return h}
function field(label,inner){return '<label class="field"><span>'+esc(label)+'</span>'+inner+'</label>'}
function sel(name,val,opts){return '<select class="inp" data-bind="'+name+'">'+opts.map(function(o){return '<option value="'+o[0]+'"'+(String(o[0])===String(val)?' selected':'')+'>'+esc(o[1])+'</option>'}).join('')+'</select>'}
function viewRole(){
  return '<h1 class="page" style="margin-top:28px">'+t('r_title')+'</h1><div class="hint">'+t('r_sub')+'</div><div class="roles">'+
   '<button class="role" data-act="role-client"><span class="ri">'+ic('user',28)+'</span><div><b>'+t('r_client')+'</b><span>'+t('r_client_d')+'</span></div></button>'+
   '<button class="role" data-act="reg-open"><span class="ri">'+ic('scissors',28)+'</span><div><b>'+t('r_barber')+'</b><span>'+t('r_barber_d')+'</span></div></button></div>'+langSeg();
}
function langSeg(){return '<div class="seg">'+['uz','ru','en'].map(function(l){return '<button class="'+(l===LANG?'on':'')+'" data-act="lang" data-l="'+l+'">'+({uz:"O'zbekcha",ru:'Русский',en:'English'})[l]+'</button>'}).join('')+'</div>'}

/* ---------- mijoz: sartaroshlar ---------- */
function bcard(b){
  var tags='';
  if(b.experience)tags+='<span class="tag">'+b.experience+' '+t('yrs')+'</span>';
  if(b.min_price)tags+='<span class="tag gold">'+t('from')+' '+money(b.min_price)+' '+t('som')+'</span>';
  return '<button class="bcard" data-act="open-b" data-id="'+b.id+'">'+avatar(b)+'<div class="bc-b"><div class="bc-t">'+esc(b.shop_name)+'</div><div class="bc-s">'+esc(b.barber_name)+'</div>'+
    '<div class="bc-a">'+ic('pin',14)+'<span>'+esc(b.address)+'</span></div><div class="tags">'+tags+'</div></div><span class="chev">'+ic('chev',20)+'</span></button>';
}
function listHtml(){
  var q=S.q.trim().toLowerCase(),a=S.barbers||[];
  if(!a.length)return empty(t('l_empty'),t('l_empty_d'));
  if(q)a=a.filter(function(b){return (b.shop_name+' '+b.barber_name+' '+b.address).toLowerCase().indexOf(q)>=0});
  if(!a.length)return empty(t('l_none'),'');
  return a.map(bcard).join('');
}
function viewList(){
  return '<h1 class="page">'+t('n_list')+'</h1><div class="search">'+ic('search',18)+'<input class="inp" data-bind="q" value="'+esc(S.q)+'" placeholder="'+esc(t('s_ph'))+'" autocomplete="off"></div>'+
    '<div id="blist">'+(S.barbers?listHtml():skel(4))+'</div>';
}
function viewBarber(){
  var id=S.page.id,d=S.bp[id];
  if(!d)return '<div class="sk" style="height:200px;margin:-14px -16px 14px"></div>'+skel(3);
  var b=d.barber,hrs=d.hours,off=d.days_off,today=S.today,wd=wdi(parseISO(today)),th=hrs[wd];
  var closed=!th||th.off||off.indexOf(today)>=0;
  var h='<div class="cover">'+(b.photo?'<img src="'+esc(b.photo)+'" alt="" decoding="async">':'<div class="ph">'+ic('scissors',64)+'</div>')+'<div class="pole"></div></div>';
  h+='<h1 class="shop">'+esc(b.shop_name)+'</h1><div class="hint">'+esc(b.barber_name)+'</div>';
  var tags='';if(b.experience)tags+='<span class="tag gold">'+b.experience+' '+t('yrs')+'</span>';
  if(tags)h+='<div class="tags">'+tags+'</div>';
  h+='<ul class="info">';
  h+='<li>'+ic('pin')+'<a href="https://maps.google.com/?q='+encodeURIComponent(b.address)+'" target="_blank" rel="noopener">'+esc(b.address)+'</a></li>';
  var wk=hrs.map(function(x,i){return '<div class="'+(i===wd?'today':'')+'"><span>'+WDN[LANG][i]+'</span><span>'+(x.off?t('off'):x.open+' – '+x.close)+'</span></div>'}).join('');
  h+='<li>'+ic('clock')+'<details style="flex:1"><summary>'+(closed?t('today_off'):t('hrs_today')+' '+th.open+' – '+th.close)+'</summary><div class="wk">'+wk+'</div></details></li>';
  if(b.phone)h+='<li>'+ic('phone')+'<a href="tel:'+esc(b.phone)+'">'+esc(b.phone)+'</a></li>';
  h+='</ul>';
  if(b.about)h+='<h3 class="st">'+t('b_about')+'</h3><div class="card lead" style="margin:0;color:var(--fg)">'+esc(b.about)+'</div>';
  h+='<h3 class="st">'+t('b_services')+'</h3>';
  h+=d.services.length?d.services.map(function(s){return svRow(s,false,'svc-go')}).join(''):'<div class="hint">'+t('b_noserv')+'</div>';
  return h+'<div class="cta-sp"></div>';
}
function svRow(s,sel,act){
  return '<button class="svc'+(sel?' sel':'')+'" data-act="'+act+'" data-id="'+s.id+'"><div><div class="svc-n">'+esc(s.name)+'</div><div class="svc-d">'+s.duration+' '+t('mn')+'</div></div><div class="svc-p">'+money(s.price)+' <small>'+t('som')+'</small></div></button>';
}
function section(id,title,body){return '<section class="step" id="step-'+id+'"><h3 class="st">'+title+'</h3>'+body+'</section>'}
function daysHtml(){
  var b=S.bk;
  if(!b.days)return '<div class="days">'+'<div class="sk" style="flex:none;width:58px;height:66px;margin:0"></div>'.repeat(5)+'</div>';
  if(!b.days.some(function(d){return d.open}))return empty(t('no_days'),t('no_days_d'));
  return '<div class="days">'+b.days.map(function(d){var dd=parseISO(d.date);
    return '<button class="day'+(d.date===b.date?' sel':'')+(d.open?'':' none')+'" data-act="pick-day" data-d="'+d.date+'"><small>'+WDS[LANG][wdi(dd)]+'</small><b>'+dd.getDate()+'</b></button>'}).join('')+'</div>'+
    (b.date?'<div class="hint" style="margin-top:4px">'+esc(dFull(b.date))+'</div>':'');
}
function slotsHtml(){
  var b=S.bk;
  if(!b.slots)return '<div class="slots">'+'<div class="sk" style="height:46px;margin:0"></div>'.repeat(8)+'</div>';
  if(!b.slots.length)return empty(t('no_slots'),t('no_slots_d'));
  return '<div class="slots">'+b.slots.map(function(x){return '<button class="slot'+(x===b.time?' sel':'')+'" data-act="pick-time" data-t="'+x+'">'+x+'</button>'}).join('')+'</div>';
}
function ticket(b,o){
  o=o||{};
  var badge=b.status==='cancelled'?'<span class="badge bad">'+t('cancelled')+'</span>':(o.past?'<span class="badge mut">'+t('was')+'</span>':'');
  var h='<div class="ticket'+(b.status!=='active'?' off':'')+'"><div class="tk-l"><div class="tk-time">'+b.time+'</div><div class="tk-end">'+(o.showDate?esc(dShort(b.date)):t('end')+' '+b.end)+'</div></div><div class="tk-r">';
  h+='<div class="tk-name">'+esc(b.service)+badge+'</div>';
  if(b.shop)h+='<div class="tk-meta"><b style="color:var(--fg)">'+esc(b.shop)+'</b>'+(b.barber_name?' · '+esc(b.barber_name):'')+'</div>';
  h+='<div class="tk-meta">'+(o.showDate?'':esc(dFull(b.date))+' · ')+'<b style="color:var(--gold)">'+money(b.price)+' '+t('som')+'</b></div>';
  if(b.address&&o.addr)h+='<div class="tk-meta">'+ic('pin',13)+' '+esc(b.address)+'</div>';
  if(o.admin)h+='<div class="tk-meta"><b style="color:var(--fg)">'+esc(b.name)+'</b> · <a href="tel:'+esc(b.phone)+'">'+esc(b.phone)+'</a></div>';
  if(o.actions)h+='<div class="tk-act">'+o.actions+'</div>';
  return h+'</div></div>';
}
function viewBook(){
  var b=S.bk;
  if(b.done)return viewDone();
  var d=S.bp[b.bid];if(!d)return skel(3);
  var h='<h1 class="page">'+t('bk_title')+'</h1><div class="hint">'+esc(d.barber.shop_name)+'</div>';
  h+=section('sv',t('bk_service'),d.services.map(function(s){return svRow(s,b.sv&&b.sv.id===s.id,'pick-sv')}).join(''));
  if(b.sv)h+=section('date',t('bk_date'),daysHtml());
  if(b.sv&&b.date)h+=section('time',t('bk_time'),slotsHtml());
  if(b.sv&&b.date&&b.time)h+=section('form',t('bk_you'),
    field(t('f_name'),'<input class="inp" data-bind="bk-name" autocomplete="name" maxlength="60" value="'+esc(b.name)+'">')+
    field(t('f_phone2'),'<input class="inp" data-bind="bk-phone" type="tel" inputmode="tel" autocomplete="tel" maxlength="20" placeholder="+998 90 123 45 67" value="'+esc(b.phone)+'">'));
  return h+'<div class="cta-sp"></div>';
}
function viewDone(){
  var d=S.bk.done;
  return '<div class="done-h"><span class="ok-dot">'+ic('check',22)+'</span><h1 class="page" style="margin:0">'+t('done_t')+'</h1></div>'+ticket(d,{addr:true})+
   '<p class="hint">'+t('done_d')+'</p><div style="display:grid;gap:10px;margin-top:18px"><button class="btn primary" data-act="tab" data-tab="my">'+t('my_t')+'</button><button class="btn" data-act="book-again">'+t('again')+'</button></div>';
}
function viewMy(){
  var h='<h1 class="page">'+t('my_t')+'</h1>';
  if(!S.my)return h+'<div style="height:14px"></div>'+skel(2);
  var up=S.my.upcoming,past=S.my.past;
  if(!up.length&&!past.length)return h+empty(t('my_empty'),t('my_empty_d'),'<button class="btn primary" data-act="tab" data-tab="list">'+t('n_list')+'</button>');
  h+='<div style="height:14px"></div>';
  h+=up.length?up.map(function(x){return ticket(x,{showDate:true,addr:true,actions:'<button class="btn small danger" data-act="cancel-my" data-id="'+x.id+'">'+t('cancel')+'</button>'})}).join(''):'<div class="hint" style="margin-bottom:14px">'+t('my_none')+'</div>';
  if(past.length)h+='<h3 class="st">'+t('past')+'</h3>'+past.map(function(x){return ticket(x,{showDate:true,past:x.status==='active'})}).join('');
  return h;
}

/* ---------- ro'yxatdan o'tish va profil ---------- */
function regForm(f,prefix){
  return field(t('f_shop'),'<input class="inp" data-bind="'+prefix+'shop_name" maxlength="80" value="'+esc(f.shop_name)+'">')+
   field(t('f_bname'),'<input class="inp" data-bind="'+prefix+'barber_name" maxlength="80" value="'+esc(f.barber_name)+'">')+
   field(t('f_addr'),'<input class="inp" data-bind="'+prefix+'address" maxlength="250" value="'+esc(f.address)+'">')+
   field(t('f_phone'),'<input class="inp" type="tel" inputmode="tel" data-bind="'+prefix+'phone" maxlength="30" placeholder="+998 90 123 45 67" value="'+esc(f.phone)+'">')+
   field(t('f_exp'),'<input class="inp" type="number" inputmode="numeric" min="0" max="60" data-bind="'+prefix+'experience" value="'+esc(f.experience)+'">')+
   field(t('f_about'),'<textarea class="inp" data-bind="'+prefix+'about" maxlength="600" placeholder="'+esc(t('f_about_ph'))+'">'+esc(f.about)+'</textarea>');
}
function viewReg(){return '<h1 class="page">'+t('reg_title')+'</h1><div class="hint" style="margin-bottom:16px">'+t('reg_hint')+'</div>'+regForm(S.reg,'reg:')+'<div class="cta-sp"></div>'}
function regOk(){var r=S.reg;return r.shop_name.trim().length>=2&&r.barber_name.trim().length>=2&&r.address.trim().length>=3&&phoneOk(r.phone)}
function viewProfile(){
  var h='<h1 class="page">'+t('n_profile')+'</h1><h3 class="st" style="margin-top:14px">'+t('p_lang')+'</h3>'+langSeg();
  if(S.me){
    h+='<div style="margin:14px 0 4px"><button class="btn wide" data-act="mode" data-m="'+(S.mode==='barber'?'client':'barber')+'">'+(S.mode==='barber'?t('p_client_mode'):t('p_barber_mode'))+'</button></div>';
  }else{
    h+='<div class="card" style="margin-top:18px"><b>'+t('p_become')+'</b><div class="hint" style="margin:4px 0 12px">'+t('p_become_d')+'</div><button class="btn primary wide" data-act="reg-open">'+t('p_become')+'</button></div>';
  }
  if(S.me&&S.mode==='barber'){
    var p=S.pf||(S.pf=pfFrom(S.me));
    h+='<h3 class="st">'+t('p_page')+'</h3>';
    h+='<div class="field"><span>'+t('p_photo')+'</span>'+(S.me.photo?'<img class="photo" src="'+esc(S.me.photo)+'" alt="">':'<div class="photo">'+t('p_photo_none')+'</div>')+
      '<div class="row" style="margin-top:10px"><label class="btn small" style="flex:1;cursor:pointer">'+t('p_photo_pick')+'<input type="file" accept="image/*" data-bind="photo-file" hidden></label>'+(S.me.photo?'<button class="btn small danger" data-act="photo-rm">'+t('p_photo_rm')+'</button>':'')+'</div></div>';
    h+=regForm(p,'pf:');
    h+=field(t('p_step'),sel('pf:step',p.step,[[15,'15 '+t('mn')],[20,'20 '+t('mn')],[30,'30 '+t('mn')],[45,'45 '+t('mn')],[60,'60 '+t('mn')]]));
    h+=field(t('p_remind'),sel('pf:remind_hours',p.remind_hours,[[1,'1 '+t('hr')],[2,'2 '+t('hr')],[3,'3 '+t('hr')]]));
    h+=field(t('p_hz'),sel('pf:horizon',p.horizon,[[7,'7 '+t('dy')],[14,'14 '+t('dy')],[30,'30 '+t('dy')],[60,'60 '+t('dy')]]));
    h+='<div class="row" style="justify-content:space-between;margin:4px 0 18px"><span>'+t('p_visible')+'</span><label class="sw"><input type="checkbox" data-bind="pf:active"'+(p.active?' checked':'')+'><span></span></label></div>';
    h+='<button class="btn primary wide" data-act="pf-save">'+t('save')+'</button>';
  }
  return h;
}
function pfFrom(m){return {shop_name:m.shop_name,barber_name:m.barber_name,address:m.address,phone:m.phone,experience:m.experience,about:m.about,step:m.step,remind_hours:m.remind_hours,horizon:m.horizon,active:m.active}}

/* ---------- sartarosh paneli ---------- */
function viewBookings(){
  var a=S.adm,today=S.today,tom=addDays(today,1);
  if(!a.date)a.date=today;
  var h='<h1 class="page">'+t('n_bookings')+'</h1><div class="row" style="margin:14px 0 4px"><button class="btn small'+(a.date===today?' primary':'')+'" data-act="adm-date" data-d="'+today+'">'+t('today')+'</button>'+
   '<button class="btn small'+(a.date===tom?' primary':'')+'" data-act="adm-date" data-d="'+tom+'">'+t('tomorrow')+'</button>'+
   '<input class="inp" type="date" style="min-height:38px;flex:1;padding:0 10px" data-bind="adm-date" value="'+a.date+'"></div>';
  if(!a.rows)return h+'<div style="height:12px"></div>'+skel(2);
  h+='<div class="hint" style="margin:12px 0 14px">'+esc(dFull(a.date))+' · '+a.rows.active+' '+t('a_n')+'</div>';
  if(!a.rows.bookings.length)return h+empty(t('a_empty'),t('a_empty_d'));
  return h+a.rows.bookings.map(function(x){
    var act=x.status==='active'?'<button class="btn small danger" data-act="adm-cancel" data-id="'+x.id+'">'+t('cancel')+'</button>':'';
    return ticket(x,{admin:true,actions:act});
  }).join('');
}
function viewServices(){
  var a=S.adm,h='<h1 class="page">'+t('n_services')+'</h1>';
  if(!a.services)return h+'<div style="height:14px"></div>'+skel(3);
  if(!a.services.some(function(s){return s.active}))h+='<div class="card" style="margin:12px 0;background:var(--gold-soft);box-shadow:none">'+esc(t('sv_hint'))+'</div>';
  h+='<div style="height:8px"></div>'+a.services.map(function(s){
    return '<button class="svc'+(s.active?'':' off')+'" data-act="sv-edit" data-id="'+s.id+'"><div><div class="svc-n">'+esc(s.name)+'</div><div class="svc-d">'+s.duration+' '+t('mn')+(s.active?'':' · '+t('sv_hidden'))+'</div></div><div class="svc-p">'+money(s.price)+' <small>'+t('som')+'</small></div></button>'}).join('');
  return h+'<div style="margin-top:14px"><button class="btn primary wide" data-act="sv-new">'+t('sv_new')+'</button></div>';
}
function viewHours(){
  var a=S.adm,h='<h1 class="page">'+t('n_hours')+'</h1><div style="height:12px"></div>';
  if(!a.hours)return h+skel(3);
  h+=a.hours.map(function(x,i){
    var r='<div class="hr"><div class="hr-top"><b>'+WDN[LANG][i]+'</b><label class="sw"><input type="checkbox" data-bind="hr-open" data-i="'+i+'"'+(x.off?'':' checked')+'><span></span></label></div>';
    if(!x.off){
      r+='<div class="hr-t"><input class="inp" type="time" data-bind="hr-open_t" data-i="'+i+'" value="'+x.open+'"><span class="hint">–</span><input class="inp" type="time" data-bind="hr-close_t" data-i="'+i+'" value="'+x.close+'"></div>';
      r+='<div class="hr-lb">'+t('hr_break')+'</div><div class="hr-t"><input class="inp" type="time" data-bind="hr-br_start" data-i="'+i+'" value="'+x.br_start+'"><span class="hint">–</span><input class="inp" type="time" data-bind="hr-br_end" data-i="'+i+'" value="'+x.br_end+'"></div>';
    }
    return r+'</div>';
  }).join('');
  h+='<button class="btn primary wide" data-act="hrs-save" style="margin-top:6px">'+t('hr_save')+'</button>';
  h+='<h3 class="st">'+t('do_t')+'</h3><div class="hint">'+t('do_d')+'</div>';
  h+='<div class="row" style="margin-top:12px"><input class="inp" type="date" data-bind="off-date" value="'+esc(a.offPick||'')+'" min="'+S.today+'"><button class="btn small" data-act="dayoff-add" style="flex:none">'+t('add')+'</button></div>';
  h+='<div class="chips">'+(a.daysOff&&a.daysOff.length?a.daysOff.map(function(d){return '<span class="chip">'+esc(dShort(d))+'<button data-act="dayoff-rm" data-d="'+d+'" aria-label="'+esc(t('rm'))+'" style="color:var(--hint)">'+ic('x',16)+'</button></span>'}).join(''):'<span class="hint">'+t('none')+'</span>')+'</div>';
  return h;
}
function sheetHtml(){
  var s=S.sheet;if(!s)return '';
  return '<div class="back" data-act="sheet-bg"><div class="sheet"><div class="grab"></div><h3 style="margin:0 0 14px;font-size:20px">'+(s.id?t('sv_edit'):t('sv_new'))+'</h3>'+
   field(t('sv_name'),'<input class="inp" data-bind="sh:name" maxlength="100" value="'+esc(s.name)+'">')+
   field(t('sv_price'),'<input class="inp" type="number" inputmode="numeric" data-bind="sh:price" value="'+esc(s.price)+'">')+
   field(t('sv_dur'),'<input class="inp" type="number" inputmode="numeric" data-bind="sh:duration" value="'+esc(s.duration)+'">')+
   '<div class="row" style="justify-content:space-between;margin:4px 0 18px"><span>'+t('sv_show')+'</span><label class="sw"><input type="checkbox" data-bind="sh:active"'+(s.active?' checked':'')+'><span></span></label></div>'+
   '<button class="btn primary wide" data-act="sv-save">'+t('save')+'</button>'+(s.id?'<button class="btn danger wide" style="margin-top:10px" data-act="sv-del">'+t('sv_del')+'</button>':'')+
   '</div></div>';
}

/* ---------- chizish ---------- */
function tabsDef(){
  if(S.mode==='barber')return [['bookings','n_bookings','calendar'],['services','n_services','scissors'],['hours','n_hours','clock'],['profile','n_profile','user']];
  return [['list','n_list','store'],['my','n_my','ticket'],['profile','n_profile','user']];
}
function renderTabs(show){
  var el=$('#tabs');
  if(!show){el.style.display='none';el.innerHTML='';return}
  el.style.display='flex';
  el.innerHTML=tabsDef().map(function(x){return '<button class="'+(S.tab===x[0]?'on':'')+'" data-act="tab" data-tab="'+x[0]+'">'+ic(x[2])+'<span>'+t(x[1])+'</span></button>'}).join('');
}
function renderCta(){
  var el=$('#cta-root'),p=S.page,h='';
  if(p&&p.t==='barber'){
    var d=S.bp[p.id];
    if(d&&d.services.length)h='<button class="btn primary wide" data-act="book-start">'+t('b_book')+'</button>';
  }else if(p&&p.t==='book'&&!S.bk.done&&S.bk.sv&&S.bk.time){
    var b=S.bk,ok=b.name.trim().length>=2&&phoneOk(b.phone);
    h='<button class="btn primary wide" data-act="book"'+((ok&&!b.busy)?'':' disabled')+'>'+(b.busy?t('sending'):(ok?t('bk_go')+' · '+money(b.sv.price)+' '+t('som'):t('bk_fill')))+'</button>';
  }else if(p&&p.t==='reg'){
    h='<button class="btn primary wide" data-act="reg-submit"'+(regOk()&&!S.busy?'':' disabled')+'>'+(S.busy?t('sending'):t('reg_submit'))+'</button>';
  }
  el.innerHTML=h?'<div class="cta">'+h+'</div>':'';
}
function renderSheet(){$('#sheet-root').innerHTML=sheetHtml()}
function syncBack(){
  if(!tg||!tg.BackButton)return;
  try{if(S.page||S.sheet)tg.BackButton.show();else tg.BackButton.hide()}catch(e){}
}
function render(){
  if(S.err&&!S.user){
    view.innerHTML=empty(t('open_fail'),S.err,'<button class="btn" data-act="reload">'+t('retry')+'</button>');
    renderTabs(false);renderCta();return;
  }
  if(!S.user){return}
  var html,tabs=false,p=S.page,key;
  if(p){
    key='p:'+p.t+(p.id||'');
    html=p.t==='barber'?viewBarber():p.t==='book'?viewBook():viewReg();
  }else if(S.user.role===null){key='role';html=viewRole()}
  else{
    tabs=true;key='t:'+S.mode+S.tab;
    html=({list:viewList,my:viewMy,profile:viewProfile,bookings:viewBookings,services:viewServices,hours:viewHours})[S.tab]();
  }
  view.className='wrap'+(key!==S.key?' fade':'');S.key=key;
  view.innerHTML=html;
  renderTabs(tabs);renderCta();renderSheet();syncBack();
}
function scrollToStep(id){requestAnimationFrame(function(){var el=document.getElementById('step-'+id);if(el)el.scrollIntoView({behavior:'smooth',block:'start'})})}

/* ---------- ma'lumot yuklash ---------- */
function cacheKey(){return 'bb2_'+(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user?tg.initDataUnsafe.user.id:'x')}
function cacheSave(){try{localStorage.setItem(cacheKey(),JSON.stringify({user:S.user,me:S.me,barbers:S.barbers,today:S.today}))}catch(e){}}
function cacheLoad(){try{return JSON.parse(localStorage.getItem(cacheKey())||'null')}catch(e){return null}}
function applyInit(r,first){
  S.user=r.user;S.me=r.me;S.barbers=r.barbers;S.today=r.today;
  if(r.user.lang&&LI[r.user.lang]!==undefined){LANG=r.user.lang;root.lang=LANG}
  if(first){
    S.mode=S.me?'barber':'client';S.tab=S.mode==='barber'?'bookings':'list';
    var want=(new URLSearchParams(location.search)).get('tab');
    if(want==='my'){S.mode='client';S.tab='my'}
    else if(want==='bookings'&&S.me){S.mode='barber';S.tab='bookings'}
  }
  if(S.mode==='barber'&&!S.me){S.mode='client';S.tab='list'}
  cacheSave();
}
function loadTab(){
  var a=S.adm;
  if(S.tab==='my')loadMy();
  else if(S.tab==='bookings')loadBookings();
  else if(S.tab==='services'){a.services=null;render();api('b/services').then(function(r){a.services=r.services;render()}).catch(fail)}
  else if(S.tab==='hours'){a.hours=null;render();api('b/hours').then(function(r){a.hours=r.hours;a.daysOff=r.days_off;render()}).catch(fail)}
  else render();
}
function loadMy(){S.my=null;render();api('my').then(function(r){S.my=r;render()}).catch(function(e){fail(e);S.my={upcoming:[],past:[]};render()})}
function loadBookings(){
  var a=S.adm;if(!a.date)a.date=S.today;a.rows=null;render();var d=a.date;
  api('b/bookings',{date:d}).then(function(r){if(a.date===d){a.rows=r;render()}}).catch(function(e){fail(e);a.rows={bookings:[],active:0};render()});
}
function go(tab){S.page=null;S.sheet=null;S.tab=tab;if(tab==='profile')S.pf=null;loadTab();window.scrollTo(0,0)}
function openBarber(id){
  S.page={t:'barber',id:id};render();window.scrollTo(0,0);
  if(!S.bp[id])api('barber',{id:id}).then(function(r){S.bp[id]=r;if(S.page&&S.page.t==='barber'&&S.page.id===id)render()}).catch(function(e){fail(e);S.page=null;render()});
}
function startBook(id,svId){
  var d=S.bp[id];if(!d)return;
  var sv=svId?d.services.filter(function(x){return x.id===svId})[0]:null;
  S.bk={bid:id,sv:sv||null,days:null,date:null,slots:null,time:null,name:S.user.name||'',phone:S.user.phone||'',busy:false,done:null};
  S.page={t:'book',id:id};render();window.scrollTo(0,0);
  if(sv)loadDays();
}
function loadDays(){
  var b=S.bk;b.days=null;b.date=null;b.slots=null;b.time=null;render();var sid=b.sv.id,bid=b.bid;
  api('days',{barber_id:bid,service_id:sid}).then(function(r){
    if(!b.sv||b.sv.id!==sid)return;b.days=r.days;
    var f=r.days.filter(function(d){return d.open})[0];
    if(f)pickDay(f.date,true);else render();
  }).catch(function(e){fail(e);b.days=[];render()});
}
function pickDay(date,quiet){
  var b=S.bk;b.date=date;b.slots=null;b.time=null;render();var sid=b.sv.id;
  api('slots',{barber_id:b.bid,service_id:sid,date:date}).then(function(r){
    if(!b.sv||b.sv.id!==sid||b.date!==date)return;b.slots=r.slots;render();if(!quiet)scrollToStep('time');
  }).catch(function(e){fail(e);b.slots=[];render()});
}
function doBook(){
  var b=S.bk;if(b.busy)return;b.busy=true;renderCta();
  api('book',{barber_id:b.bid,service_id:b.sv.id,date:b.date,time:b.time,name:b.name.trim(),phone:b.phone.trim()}).then(function(r){
    b.busy=false;b.done=r.booking;S.user.name=b.name.trim();S.user.phone=b.phone.trim();S.my=null;hap('ok');render();window.scrollTo(0,0);
  }).catch(function(e){
    b.busy=false;fail(e);
    if(e.status===409&&b.date)pickDay(b.date,true);else render();
  });
}
function back(){
  if(S.sheet){S.sheet=null;render();return}
  var p=S.page;if(!p)return;
  if(p.t==='book'&&!S.bk.done){S.page={t:'barber',id:p.id}}else S.page=null;
  render();window.scrollTo(0,0);
}
if(tg&&tg.BackButton){try{tg.BackButton.onClick(back)}catch(e){}}
function setLang(l){
  if(LI[l]===undefined||l===LANG)return;
  LANG=l;root.lang=l;if(S.user){S.user.lang=l;cacheSave();api('lang',{lang:l}).catch(function(){})}
  S.key='';render();
}
function setMode(m){
  S.mode=m;S.page=null;S.tab=m==='barber'?'bookings':'list';S.pf=null;loadTab();window.scrollTo(0,0);
}
function afterInit(){render();loadTab()}

/* ---------- hodisalar ---------- */
document.addEventListener('click',function(e){
  var el=e.target.closest?e.target.closest('[data-act]'):null;if(!el)return;
  var a=el.dataset.act,d=el.dataset;
  if(a==='sheet-bg'){if(e.target===el){S.sheet=null;render()}return}
  hap();
  switch(a){
    case 'tab':go(d.tab);break;
    case 'open-b':openBarber(+d.id);break;
    case 'svc-go':startBook(S.page.id,+d.id);break;
    case 'book-start':startBook(S.page.id,null);break;
    case 'pick-sv':S.bk.sv=S.bp[S.bk.bid].services.filter(function(x){return x.id===+d.id})[0];loadDays();scrollToStep('date');break;
    case 'pick-day':pickDay(d.d);break;
    case 'pick-time':S.bk.time=d.t;render();scrollToStep('form');break;
    case 'book':doBook();break;
    case 'book-again':S.page=null;S.tab='list';S.bk={};loadTab();window.scrollTo(0,0);break;
    case 'cancel-my':ask(t('q_cancel'),function(){api('cancel',{id:+d.id}).then(function(){toast(t('cancelled_ok'));loadMy()}).catch(function(e){fail(e);loadMy()})});break;
    case 'role-client':api('role',{role:'client'}).then(function(){S.user.role='client';cacheSave();afterInit()}).catch(fail);break;
    case 'reg-open':
      S.reg={shop_name:'',barber_name:S.user.name||'',address:'',phone:S.user.phone||'',experience:'',about:''};
      S.page={t:'reg'};render();window.scrollTo(0,0);break;
    case 'reg-submit':
      if(!regOk()||S.busy)break;S.busy=true;renderCta();
      api('register',S.reg).then(function(r){
        S.busy=false;S.me=r.me;S.user.role='barber';S.mode='barber';S.tab='services';S.page=null;S.pf=null;cacheSave();hap('ok');toast(t('reg_done'));loadTab();window.scrollTo(0,0);
      }).catch(function(er){S.busy=false;fail(er);renderCta()});break;
    case 'lang':setLang(d.l);break;
    case 'mode':setMode(d.m);break;
    case 'pf-save':
      api('b/profile_save',S.pf).then(function(r){S.me=r.me;S.pf=pfFrom(r.me);S.barbers=null;cacheSave();toast(t('saved'));hap('ok');render();
        api('init').then(function(i){S.barbers=i.barbers;cacheSave()}).catch(function(){})}).catch(fail);break;
    case 'photo-rm':api('b/photo',{photo:''}).then(function(r){S.me=r.me;render()}).catch(fail);break;
    case 'adm-date':S.adm.date=d.d;loadBookings();break;
    case 'adm-cancel':ask(t('q_cancel_c'),function(){api('b/cancel',{id:+d.id}).then(function(){toast(t('cancelled_ok'));loadBookings()}).catch(function(er){fail(er);loadBookings()})});break;
    case 'sv-new':S.sheet={id:0,name:'',price:'',duration:30,active:1};render();break;
    case 'sv-edit':var sv=S.adm.services.filter(function(x){return x.id===+d.id})[0];if(sv){S.sheet={id:sv.id,name:sv.name,price:sv.price,duration:sv.duration,active:sv.active};render()}break;
    case 'sv-save':
      api('b/service_save',{id:S.sheet.id||undefined,name:S.sheet.name,price:S.sheet.price,duration:S.sheet.duration,active:S.sheet.active?1:0})
        .then(function(r){S.adm.services=r.services;S.sheet=null;toast(t('saved'));hap('ok');render()}).catch(fail);break;
    case 'sv-del':ask(t('sv_q'),function(){api('b/service_delete',{id:S.sheet.id}).then(function(r){S.adm.services=r.services;S.sheet=null;toast(t('deleted'));render()}).catch(fail)});break;
    case 'hrs-save':api('b/hours_save',{hours:S.adm.hours}).then(function(r){S.adm.hours=r.hours;toast(t('saved'));hap('ok');render()}).catch(fail);break;
    case 'dayoff-add':
      if(!S.adm.offPick){toast(t('pick_d'));break}
      api('b/dayoff',{date:S.adm.offPick,off:true}).then(function(r){S.adm.daysOff=r.days_off;S.adm.offPick='';render();if(r.conflicts)toast(r.conflicts+' '+t('conflict'))}).catch(fail);break;
    case 'dayoff-rm':api('b/dayoff',{date:d.d,off:false}).then(function(r){S.adm.daysOff=r.days_off;render()}).catch(fail);break;
    case 'reload':location.reload();break;
  }
});
document.addEventListener('input',function(e){
  var el=e.target,b=el.dataset&&el.dataset.bind;if(!b)return;var v=el.value;
  if(b==='q'){S.q=v;var bl=$('#blist');if(bl)bl.innerHTML=listHtml()}
  else if(b==='bk-name'){S.bk.name=v;renderCta()}
  else if(b==='bk-phone'){S.bk.phone=v;renderCta()}
  else if(b.indexOf('reg:')===0){S.reg[b.slice(4)]=v;renderCta()}
  else if(b.indexOf('pf:')===0&&el.type!=='checkbox'&&el.tagName!=='SELECT'){S.pf[b.slice(3)]=v}
  else if(b.indexOf('sh:')===0&&el.type!=='checkbox'){S.sheet[b.slice(3)]=v}
});
document.addEventListener('change',function(e){
  var el=e.target,b=el.dataset&&el.dataset.bind;if(!b)return;
  if(b.indexOf('pf:')===0&&(el.type==='checkbox'||el.tagName==='SELECT')){S.pf[b.slice(3)]=el.type==='checkbox'?(el.checked?1:0):+el.value}
  else if(b==='sh:active'){S.sheet.active=el.checked?1:0}
  else if(b==='hr-open'){S.adm.hours[+el.dataset.i].off=el.checked?0:1;render()}
  else if(b.indexOf('hr-')===0){S.adm.hours[+el.dataset.i][({open_t:'open',close_t:'close',br_start:'br_start',br_end:'br_end'})[b.slice(3)]]=el.value}
  else if(b==='off-date'){S.adm.offPick=el.value}
  else if(b==='adm-date'){if(el.value){S.adm.date=el.value;loadBookings()}}
  else if(b==='photo-file'){
    var f=el.files&&el.files[0];if(!f)return;
    compress(f,function(data){
      if(!data){toast(t('photo_bad'));return}
      api('b/photo',{photo:data}).then(function(r){S.me=r.me;toast(t('saved'));render()}).catch(fail);
    });
  }
});

/* ---------- boshlash ---------- */
(function start(){
  root.lang=LANG;
  var c=cacheLoad();
  if(c&&c.user){applyInit(c,true);afterInit()}   // avvalgi ma'lumot bilan darhol ko'rsatamiz
  api('init').then(function(r){
    var first=!S.user;applyInit(r,first);
    if(first){afterInit()}
    else if(!S.page&&!S.sheet&&(S.tab==='list'||S.tab==='profile')){render()}   // ro'yxat yangilandi
    else if(S.tab==='list'){render()}
  }).catch(function(e){
    if(!S.user){S.err=e.message||t('err');render()}
  });
})();
})();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    main()
