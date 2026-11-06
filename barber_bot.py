"""
Sartaroshxona: Telegram bot + Mini App (vaqt band qilish)

Bitta fayl: bot, HTTP server, API va Mini App sahifasi.

Render'da kerakli o'zgaruvchilar (Environment):
  BOT_TOKEN     BotFather tokeni (raqamlar:harflar)
  DATABASE_URL  Postgres manzili (Neon / Supabase). Busiz bronlar deployda yo'qoladi!
  ADMIN_IDS     Sartarosh(lar)ning Telegram ID si, vergul bilan (ID ni botga /myid yozib bilasiz)
Ixtiyoriy:
  PUBLIC_URL    Servis manzili (Render o'zi beradi: RENDER_EXTERNAL_URL)
  TZ_NAME       Vaqt mintaqasi (standart: Asia/Tashkent)
"""
import os
import re
import sys
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

WD = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
MON = ["", "yanvar", "fevral", "mart", "aprel", "may", "iyun",
       "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]

# ---------------- Baza ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///barber.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
BOOK_LOCK = threading.Lock()

DEFAULTS = {
    "name": "Sartaroshxona",
    "description": "Professional soch va soqol xizmatlari. Oldindan yoziling, navbatda kutmang.",
    "address": "", "phone": "",
    "step": "30", "remind_hours": "2", "horizon": "30", "photo_v": "0",
}


def now_local():
    return dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def to_dt(v):
    return v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))


def init_db():
    if DATABASE_URL.startswith("sqlite") and os.getenv("RENDER"):
        log.warning("DIQQAT: SQLite Render'da har deployda o'chadi, bronlar yo'qoladi. "
                    "DATABASE_URL (Postgres) kiriting!")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS bo'sh: admin bo'limi hech kimga ochilmaydi.")
    ident = "SERIAL PRIMARY KEY" if "postgresql" in DATABASE_URL else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS settings (k VARCHAR(40) PRIMARY KEY, v TEXT)"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS services (id {ident}, name VARCHAR(100) NOT NULL, "
            "price BIGINT NOT NULL, duration INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, "
            "sort_order INTEGER NOT NULL DEFAULT 0)"))
        c.execute(text(
            "CREATE TABLE IF NOT EXISTS work_hours (weekday INTEGER PRIMARY KEY, open_t VARCHAR(5), "
            "close_t VARCHAR(5), is_off INTEGER NOT NULL DEFAULT 0, br_start VARCHAR(5), br_end VARCHAR(5))"))
        c.execute(text("CREATE TABLE IF NOT EXISTS days_off (d VARCHAR(10) PRIMARY KEY)"))
        c.execute(text(
            f"CREATE TABLE IF NOT EXISTS bookings (id {ident}, user_id BIGINT NOT NULL, "
            "tg_name VARCHAR(100), name VARCHAR(100) NOT NULL, phone VARCHAR(30) NOT NULL, "
            "service_id INTEGER, service_name VARCHAR(100) NOT NULL, price BIGINT NOT NULL, "
            "duration INTEGER NOT NULL, start_at TIMESTAMP NOT NULL, end_at TIMESTAMP NOT NULL, "
            "status VARCHAR(12) NOT NULL DEFAULT 'active', cancelled_by VARCHAR(10), "
            "created_at TIMESTAMP NOT NULL, reminded INTEGER NOT NULL DEFAULT 0)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_b_start ON bookings (start_at)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_b_user ON bookings (user_id, start_at)"))
        # bir xil boshlanish vaqtiga ikkita faol bron bo'lishi mumkin emas (qo'shimcha himoya)
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_b_active_start "
                       "ON bookings (start_at) WHERE status = 'active'"))
        if not c.execute(text("SELECT COUNT(*) FROM work_hours")).scalar():
            for wd in range(7):
                c.execute(text(
                    "INSERT INTO work_hours (weekday, open_t, close_t, is_off, br_start, br_end) "
                    "VALUES (:w, '09:00', '20:00', :o, '', '')"), {"w": wd, "o": 1 if wd == 6 else 0})
        if not c.execute(text("SELECT COUNT(*) FROM services")).scalar():
            for i, (n, p, d) in enumerate([("Soch olish", 50000, 30), ("Soqol", 30000, 20),
                                           ("Kombo (soch + soqol)", 70000, 50)]):
                c.execute(text("INSERT INTO services (name, price, duration, active, sort_order) "
                               "VALUES (:n, :p, :d, 1, :s)"), {"n": n, "p": p, "d": d, "s": i})


# ---- sozlamalar ----
def get_settings():
    s = dict(DEFAULTS)
    with engine.connect() as c:
        for k, v in c.execute(text("SELECT k, v FROM settings WHERE k <> 'photo'")).fetchall():
            s[k] = v if v is not None else ""
    for k, lo, hi, dflt in (("step", 10, 120, 30), ("remind_hours", 1, 24, 2), ("horizon", 1, 120, 30)):
        try:
            s[k] = min(max(int(s[k]), lo), hi)
        except Exception:
            s[k] = dflt
    return s


def set_setting(c, k, v):
    r = c.execute(text("UPDATE settings SET v=:v WHERE k=:k"), {"k": k, "v": str(v)})
    if not r.rowcount:
        c.execute(text("INSERT INTO settings (k, v) VALUES (:k, :v)"), {"k": k, "v": str(v)})


# ---- xizmatlar ----
def _sv(r):
    return {"id": r[0], "name": r[1], "price": int(r[2]), "duration": int(r[3]), "active": bool(r[4])}


def list_services(only_active=True):
    q = "SELECT id, name, price, duration, active FROM services"
    if only_active:
        q += " WHERE active=1"
    q += " ORDER BY sort_order, id"
    with engine.connect() as c:
        return [_sv(r) for r in c.execute(text(q)).fetchall()]


def get_service(sid):
    with engine.connect() as c:
        r = c.execute(text("SELECT id, name, price, duration, active FROM services WHERE id=:i"),
                      {"i": sid}).fetchone()
    return _sv(r) if r else None


# ---- ish vaqti ----
def get_hours():
    with engine.connect() as c:
        rows = c.execute(text("SELECT weekday, open_t, close_t, is_off, br_start, br_end "
                              "FROM work_hours ORDER BY weekday")).fetchall()
    return [{"weekday": r[0], "open": r[1] or "09:00", "close": r[2] or "20:00", "off": bool(r[3]),
             "br_start": r[4] or "", "br_end": r[5] or ""} for r in rows]


def get_days_off():
    today = now_local().date().isoformat()
    with engine.connect() as c:
        return [r[0] for r in c.execute(text("SELECT d FROM days_off WHERE d >= :t ORDER BY d"),
                                        {"t": today}).fetchall()]


def hm(s):
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


def fetch_busy(start, end):
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT start_at, end_at FROM bookings WHERE status='active' AND start_at < :e AND end_at > :s"),
            {"s": start, "e": end}).fetchall()
    return [(to_dt(r[0]), to_dt(r[1])) for r in rows]


def day_slots(d, duration, s, hours, days_off, busy):
    """d kuni uchun bo'sh boshlanish vaqtlari ro'yxati ('HH:MM')."""
    now = now_local()
    if d < now.date() or d > now.date() + dt.timedelta(days=s["horizon"]):
        return []
    h = hours[d.weekday()]
    if h["off"] or d.isoformat() in days_off:
        return []
    opening = dt.datetime.combine(d, hm(h["open"]))
    closing = dt.datetime.combine(d, hm(h["close"]))
    brk = None
    if h["br_start"] and h["br_end"]:
        brk = (dt.datetime.combine(d, hm(h["br_start"])), dt.datetime.combine(d, hm(h["br_end"])))
    step = dt.timedelta(minutes=s["step"])
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


# ---- bronlar ----
BK_COLS = ("id, service_name, price, duration, start_at, end_at, status, name, phone, "
           "cancelled_by, user_id, tg_name")


def bk(r):
    s, e = to_dt(r[4]), to_dt(r[5])
    return {"id": r[0], "service": r[1], "price": int(r[2]), "duration": int(r[3]),
            "date": s.strftime("%Y-%m-%d"), "time": s.strftime("%H:%M"), "end": e.strftime("%H:%M"),
            "status": r[6], "name": r[7], "phone": r[8], "cancelled_by": r[9] or "",
            "user_id": int(r[10]), "tg_name": r[11] or ""}


def get_booking(bid):
    with engine.connect() as c:
        r = c.execute(text(f"SELECT {BK_COLS} FROM bookings WHERE id=:i"), {"i": bid}).fetchone()
    return bk(r) if r else None


def set_cancelled(bid, by):
    with engine.begin() as c:
        r = c.execute(text("UPDATE bookings SET status='cancelled', cancelled_by=:b "
                           "WHERE id=:i AND status='active'"), {"b": by, "i": bid})
        return bool(r.rowcount)


def fmt_money(n):
    return f"{int(n):,}".replace(",", " ")


def fmt_when(date_str, time_str):
    d = dt.date.fromisoformat(date_str)
    return f"{d.day}-{MON[d.month]}, {WD[d.weekday()].lower()}, {time_str}"


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


def notify_admins(text_, tab="admin"):
    for aid in ADMIN_IDS:
        tg_send(aid, text_, app_button("📋 Bronlarni ochish", tab))


def booking_lines(b, shop=None):
    lines = [f"✂️ {html.escape(b['service'])}",
             f"📅 {fmt_when(b['date'], b['time'])}",
             f"💰 {fmt_money(b['price'])} so'm"]
    if shop:
        if shop.get("address"):
            lines.append(f"📍 {html.escape(shop['address'])}")
        if shop.get("phone"):
            lines.append(f"📞 {html.escape(shop['phone'])}")
    return "\n".join(lines)


def notify_booked(b):
    s = get_settings()
    tg_send(b["user_id"],
            f"✅ <b>Bron tasdiqlandi</b>\n\n{booking_lines(b, s)}\n\n"
            "Vaqtidan oldin eslatamiz. Bekor qilish: «Mening bronlarim» bo'limida.",
            app_button("🧾 Mening bronlarim", "my"))
    who = f"👤 {html.escape(b['name'])}\n📞 {html.escape(b['phone'])}"
    notify_admins(f"🆕 <b>Yangi bron</b>\n\n{who}\n{booking_lines(b)}")


def notify_cancelled(b, by):
    s = get_settings()
    if by == "client":
        notify_admins(f"❌ <b>Mijoz bronni bekor qildi</b>\n\n👤 {html.escape(b['name'])}\n"
                      f"📞 {html.escape(b['phone'])}\n{booking_lines(b)}")
        tg_send(b["user_id"], f"❌ <b>Bron bekor qilindi</b>\n\n{booking_lines(b)}",
                app_button("📅 Qayta band qilish"))
    else:
        tail = f"\nSavollar bo'yicha: {html.escape(s['phone'])}" if s.get("phone") else ""
        tg_send(b["user_id"],
                f"❌ <b>Broningiz bekor qilindi</b> (sartarosh tomonidan)\n\n{booking_lines(b)}{tail}",
                app_button("📅 Qayta band qilish"))


def run_reminders():
    """Bron boshlanishidan oldin (sozlangan soat) mijozga eslatma yuboradi."""
    s = get_settings()
    now = now_local()
    until = now + dt.timedelta(hours=s["remind_hours"])
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT id FROM bookings WHERE status='active' AND reminded=0 "
            "AND start_at > :n AND start_at <= :u"), {"n": now, "u": until}).fetchall()
    for (bid,) in rows:
        b = get_booking(bid)
        if not b or b["status"] != "active":
            continue
        res = tg_send(b["user_id"],
                      f"⏰ <b>Eslatma</b>\n\n{html.escape(s['name'])}da bron:\n{booking_lines(b, s)}",
                      app_button("🧾 Mening bronlarim", "my"))
        if res in ("ok", "blocked"):
            with engine.begin() as c:
                c.execute(text("UPDATE bookings SET reminded=1 WHERE id=:i"), {"i": bid})


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
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


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


def require_admin(user):
    if user["id"] not in ADMIN_IDS:
        raise ApiError("Ruxsat yo'q", 403)


# ---------------- Mini App: mijoz API ----------------
def api_init(user, b):
    s = get_settings()
    with engine.connect() as c:
        last = c.execute(text("SELECT name, phone FROM bookings WHERE user_id=:u "
                              "ORDER BY id DESC LIMIT 1"), {"u": user["id"]}).fetchone()
    return {
        "shop": {"name": s["name"], "description": s["description"], "address": s["address"],
                 "phone": s["phone"], "photo_v": s["photo_v"]},
        "services": list_services(True),
        "hours": get_hours(),
        "days_off": get_days_off(),
        "is_admin": user["id"] in ADMIN_IDS,
        "today": now_local().date().isoformat(),
        "horizon": s["horizon"],
        "profile": {"name": last[0] if last else (user.get("first_name") or ""),
                    "phone": last[1] if last else ""},
    }


def _service_or_err(b):
    try:
        sv = get_service(int(b.get("service_id")))
    except Exception:
        sv = None
    if not sv or not sv["active"]:
        raise ApiError("Xizmat topilmadi")
    return sv


def api_days(user, b):
    sv = _service_or_err(b)
    s, hours, off = get_settings(), get_hours(), set(get_days_off())
    today = now_local().date()
    last = today + dt.timedelta(days=s["horizon"])
    busy = fetch_busy(dt.datetime.combine(today, dt.time.min), dt.datetime.combine(last, dt.time.max))
    days = []
    d = today
    while d <= last:
        days.append({"date": d.isoformat(),
                     "open": bool(day_slots(d, sv["duration"], s, hours, off, busy))})
        d += dt.timedelta(days=1)
    return {"days": days}


def api_slots(user, b):
    sv = _service_or_err(b)
    d = _date(b.get("date"))
    s = get_settings()
    busy = fetch_busy(dt.datetime.combine(d, dt.time.min), dt.datetime.combine(d, dt.time.max))
    return {"slots": day_slots(d, sv["duration"], s, get_hours(), set(get_days_off()), busy)}


def api_book(user, b):
    sv = _service_or_err(b)
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
    with BOOK_LOCK:
        s = get_settings()
        busy = fetch_busy(dt.datetime.combine(d, dt.time.min), dt.datetime.combine(d, dt.time.max))
        if t not in day_slots(d, sv["duration"], s, get_hours(), set(get_days_off()), busy):
            raise ApiError("Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.", 409)
        now = now_local()
        with engine.begin() as c:
            n = c.execute(text("SELECT COUNT(*) FROM bookings WHERE user_id=:u AND status='active' "
                               "AND start_at > :n"), {"u": user["id"], "n": now}).scalar()
            if n >= MAX_ACTIVE_PER_USER:
                raise ApiError(f"Sizda allaqachon {MAX_ACTIVE_PER_USER} ta faol bron bor. "
                               "Avval birortasini bekor qiling yoki vaqti kelishini kuting.")
            remind_soon = 1 if start - now <= dt.timedelta(hours=s["remind_hours"]) else 0
            try:
                c.execute(text(
                    "INSERT INTO bookings (user_id, tg_name, name, phone, service_id, service_name, price, "
                    "duration, start_at, end_at, status, created_at, reminded) VALUES "
                    "(:u, :tn, :n, :p, :sid, :sn, :pr, :du, :s, :e, 'active', :c, :r)"),
                    {"u": user["id"], "tn": tg_name, "n": name, "p": phone, "sid": sv["id"],
                     "sn": sv["name"], "pr": sv["price"], "du": sv["duration"], "s": start, "e": end,
                     "c": now, "r": remind_soon})
            except Exception as e:
                if "unique" in str(e).lower() or "constraint" in str(e).lower():
                    raise ApiError("Bu vaqt band bo'lib qoldi. Boshqa vaqtni tanlang.", 409)
                raise
            bid = c.execute(text("SELECT id FROM bookings WHERE user_id=:u AND start_at=:s "
                                 "AND status='active' ORDER BY id DESC LIMIT 1"),
                            {"u": user["id"], "s": start}).scalar()
    booking = get_booking(bid)
    threading.Thread(target=notify_booked, args=(booking,), daemon=True).start()
    return {"booking": booking}


def api_my(user, b):
    with engine.connect() as c:
        rows = c.execute(text(f"SELECT {BK_COLS} FROM bookings WHERE user_id=:u "
                              "ORDER BY start_at DESC LIMIT 60"), {"u": user["id"]}).fetchall()
    now = now_local()
    up, past = [], []
    for r in rows:
        x = bk(r)
        is_up = x["status"] == "active" and to_dt(f"{x['date']} {x['time']}") > now
        (up if is_up else past).append(x)
    up.reverse()  # yaqin vaqt birinchi
    return {"upcoming": up, "past": past[:20]}


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


# ---------------- Mini App: admin API ----------------
def api_admin_bookings(user, b):
    require_admin(user)
    d1 = _date(b.get("date"))
    d2 = _date(b.get("date_to") or b.get("date"))
    start = dt.datetime.combine(d1, dt.time.min)
    end = dt.datetime.combine(d2, dt.time.max)
    with engine.connect() as c:
        rows = c.execute(text(f"SELECT {BK_COLS} FROM bookings WHERE start_at >= :s AND start_at <= :e "
                              "ORDER BY start_at"), {"s": start, "e": end}).fetchall()
    items = [bk(r) for r in rows]
    return {"bookings": items, "active": sum(1 for x in items if x["status"] == "active")}


def api_admin_cancel(user, b):
    require_admin(user)
    x = get_booking(_int(b.get("id"), 1, 10**12, "Bron topilmadi"))
    if not x:
        raise ApiError("Bron topilmadi", 404)
    if x["status"] != "active":
        raise ApiError("Bu bron allaqachon bekor qilingan")
    if set_cancelled(x["id"], "admin"):
        threading.Thread(target=notify_cancelled, args=(x, "admin"), daemon=True).start()
    return {"ok": True}


def api_admin_services(user, b):
    require_admin(user)
    return {"services": list_services(False)}


def api_admin_service_save(user, b):
    require_admin(user)
    name = _str(b.get("name"), 100)
    if len(name) < 2:
        raise ApiError("Xizmat nomini kiriting")
    price = _int(b.get("price"), 0, 100_000_000, "Narx noto'g'ri")
    dur = _int(b.get("duration"), 5, 600, "Davomiylik 5 dan 600 daqiqagacha bo'lsin")
    active = 1 if b.get("active", True) else 0
    with engine.begin() as c:
        if b.get("id"):
            sid = _int(b.get("id"), 1, 10**9, "Xizmat topilmadi")
            r = c.execute(text("UPDATE services SET name=:n, price=:p, duration=:d, active=:a WHERE id=:i"),
                          {"n": name, "p": price, "d": dur, "a": active, "i": sid})
            if not r.rowcount:
                raise ApiError("Xizmat topilmadi", 404)
        else:
            mx = c.execute(text("SELECT COALESCE(MAX(sort_order), 0) FROM services")).scalar()
            c.execute(text("INSERT INTO services (name, price, duration, active, sort_order) "
                           "VALUES (:n, :p, :d, :a, :s)"),
                      {"n": name, "p": price, "d": dur, "a": active, "s": int(mx) + 1})
    return {"services": list_services(False)}


def api_admin_service_delete(user, b):
    require_admin(user)
    sid = _int(b.get("id"), 1, 10**9, "Xizmat topilmadi")
    with engine.begin() as c:  # bronlarda xizmat nomi va narxi nusxalangan, o'chirish xavfsiz
        c.execute(text("DELETE FROM services WHERE id=:i"), {"i": sid})
    return {"services": list_services(False)}


def api_admin_hours(user, b):
    require_admin(user)
    return {"hours": get_hours(), "days_off": get_days_off()}


def api_admin_hours_save(user, b):
    require_admin(user)
    rows = b.get("hours")
    if not isinstance(rows, list) or len(rows) != 7:
        raise ApiError("Ish vaqti noto'g'ri")
    clean = []
    for i, r in enumerate(rows):
        off = bool(r.get("off"))
        o, c_ = _hm(r.get("open")), _hm(r.get("close"))
        bs, be = _hm(r.get("br_start"), True), _hm(r.get("br_end"), True)
        if not off and o >= c_:
            raise ApiError(f"{WD[i]}: tugash vaqti boshlanishidan keyin bo'lsin")
        if bool(bs) != bool(be) or (bs and (bs >= be or bs < o or be > c_)):
            raise ApiError(f"{WD[i]}: tanaffus ish vaqti ichida bo'lsin")
        clean.append((i, o, c_, 1 if off else 0, bs, be))
    with engine.begin() as c:
        for i, o, c_, off, bs, be in clean:
            c.execute(text("UPDATE work_hours SET open_t=:o, close_t=:c, is_off=:f, br_start=:bs, br_end=:be "
                           "WHERE weekday=:w"), {"o": o, "c": c_, "f": off, "bs": bs, "be": be, "w": i})
    return {"hours": get_hours()}


def api_admin_dayoff(user, b):
    require_admin(user)
    d = _date(b.get("date")).isoformat()
    with engine.begin() as c:
        c.execute(text("DELETE FROM days_off WHERE d=:d"), {"d": d})
        if b.get("off", True):
            c.execute(text("INSERT INTO days_off (d) VALUES (:d)"), {"d": d})
    return {"days_off": get_days_off()}


def api_admin_settings(user, b):
    require_admin(user)
    s = get_settings()
    return {"settings": {k: s[k] for k in ("name", "description", "address", "phone",
                                           "step", "remind_hours", "horizon")}}


def api_admin_settings_save(user, b):
    require_admin(user)
    name = _str(b.get("name"), 60)
    if not name:
        raise ApiError("Nomini kiriting")
    phone = _str(b.get("phone"), 30)
    if phone and not re.fullmatch(r"[+\d\s()\-]{5,30}", phone):
        raise ApiError("Telefon noto'g'ri")
    step = _int(b.get("step"), 10, 120, "Vaqt qadami noto'g'ri")
    rem = _int(b.get("remind_hours"), 1, 24, "Eslatma vaqti noto'g'ri")
    hor = _int(b.get("horizon"), 1, 120, "Necha kun oldin noto'g'ri")
    with engine.begin() as c:
        for k, v in (("name", name), ("description", _str(b.get("description"), 400)),
                     ("address", _str(b.get("address"), 200)), ("phone", phone),
                     ("step", step), ("remind_hours", rem), ("horizon", hor)):
            set_setting(c, k, v)
    return api_admin_settings(user, b)


def api_admin_photo(user, b):
    require_admin(user)
    with engine.begin() as c:
        if b.get("remove"):
            c.execute(text("DELETE FROM settings WHERE k='photo'"))
            set_setting(c, "photo_v", "0")
            return {"photo_v": "0"}
        img = str(b.get("image") or "")
        m = re.fullmatch(r"data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)", img)
        if not m or len(m.group(2)) > 450_000:
            raise ApiError("Rasm noto'g'ri yoki juda katta")
        base64.b64decode(m.group(2), validate=True)
        v = str(int(time.time()))
        set_setting(c, "photo", img)
        set_setting(c, "photo_v", v)
    return {"photo_v": v}


API = {
    "init": api_init, "days": api_days, "slots": api_slots, "book": api_book,
    "my": api_my, "cancel": api_cancel,
    "admin/bookings": api_admin_bookings, "admin/cancel": api_admin_cancel,
    "admin/services": api_admin_services, "admin/service_save": api_admin_service_save,
    "admin/service_delete": api_admin_service_delete,
    "admin/hours": api_admin_hours, "admin/hours_save": api_admin_hours_save,
    "admin/dayoff": api_admin_dayoff,
    "admin/settings": api_admin_settings, "admin/settings_save": api_admin_settings_save,
    "admin/photo": api_admin_photo,
}


# ---------------- HTTP server ----------------
class Web(BaseHTTPRequestHandler):
    server_version = "Barber"

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/app", "/index.html"):
            self._send(200, APP_HTML, "text/html; charset=utf-8")
        elif path == "/photo":
            try:
                with engine.connect() as c:
                    v = c.execute(text("SELECT v FROM settings WHERE k='photo'")).scalar()
                m = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,(.+)", v or "", re.S)
                if not m:
                    raise LookupError
                self._send(200, base64.b64decode(m.group(2)), m.group(1), "public, max-age=86400")
            except Exception:
                self._send(404, "yo'q", "text/plain; charset=utf-8")
        elif path == "/health":
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "yo'q", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            self._json(404, {"error": "Topilmadi"})
            return
        fn = API.get(path[5:])
        if not fn:
            self._json(404, {"error": "Topilmadi"})
            return
        user = verify_init_data(self.headers.get("X-Init-Data", ""))
        if not user:
            self._json(401, {"error": "Ilovani Telegram orqali qayta oching"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 600_000:
                self._json(413, {"error": "So'rov juda katta"})
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            self._json(400, {"error": "So'rov noto'g'ri"})
            return
        try:
            self._json(200, fn(user, body))
        except ApiError as e:
            self._json(e.code, {"error": str(e)})
        except Exception:
            log.exception("API xatosi: %s", path)
            self._json(500, {"error": "Server xatosi, birozdan keyin qayta urinib ko'ring"})

    def log_message(self, *a):
        pass


def start_server():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Web)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("HTTP server %s portda ishga tushdi", PORT)
    return srv


# ---------------- Bot ----------------
def _kb():
    if not APP_URL:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✂️ Band qilish", web_app=WebAppInfo(url=APP_URL + "/"))],
        [InlineKeyboardButton("🧾 Mening bronlarim", web_app=WebAppInfo(url=APP_URL + "/?tab=my"))],
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await asyncio.to_thread(get_settings)
    who = html.escape(update.effective_user.first_name or "")
    msg = (f"Assalomu alaykum, {who}! 👋\n\n<b>{html.escape(s['name'])}</b>ga xush kelibsiz.\n"
           "Xizmatlar bilan tanishish va vaqt band qilish uchun pastdagi tugmani bosing.")
    if not APP_URL:
        msg += "\n\n⚠️ Mini App manzili sozlanmagan (PUBLIC_URL)."
    await update.effective_message.reply_text(msg, parse_mode="HTML", reply_markup=_kb())


async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"Sizning Telegram ID: <code>{update.effective_user.id}</code>", parse_mode="HTML")


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
    await app.bot.set_my_commands([BotCommand("start", "Boshlash")])
    if APP_URL:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="✂️ Band qilish", web_app=WebAppInfo(url=APP_URL + "/")))
        except Exception as e:
            log.warning("Menu tugmasini o'rnatib bo'lmadi: %s", type(e).__name__)


def main():
    init_db()
    start_server()
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .get_updates_read_timeout(30).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, cmd_start))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(reminder_job, interval=60, first=20)
        app.job_queue.run_repeating(self_ping, interval=480, first=60)
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
<title>Sartaroshxona</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:var(--tg-theme-bg-color,#ffffff);
  --fg:var(--tg-theme-text-color,#17191c);
  --hint:var(--tg-theme-hint-color,#6f7683);
  --soft:var(--tg-theme-secondary-bg-color,#f1f2f4);
  --line:rgba(127,127,127,.30);
  --accent:#b8322a;--accent-fg:#fff;
  --blue:#2b5d9b;
  --ok:#23864f;--bad:#d2383f;--warn:#a8680c;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Noto Serif",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --safe-b:env(safe-area-inset-bottom,0px);
  --tab-h:58px;
}
:root[data-scheme=dark]{--accent:#e5574b;--blue:#7aaaee;--ok:#41c07a;--bad:#f2676d;--warn:#e3ac4c}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 var(--sans);-webkit-tap-highlight-color:transparent;
  padding-bottom:calc(var(--tab-h) + var(--safe-b) + 24px);min-height:100vh}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0;text-align:inherit}
a{color:var(--blue);text-decoration:none}
h1,h2,h3{margin:0}
#view{padding:0 16px;max-width:560px;margin:0 auto}
.ic{flex:none;display:block}

/* ---------- tab bar ---------- */
#tabs{position:fixed;left:0;right:0;bottom:0;z-index:20;display:flex;background:var(--bg);
  border-top:1px solid var(--line);padding-bottom:var(--safe-b)}
#tabs button{flex:1;height:var(--tab-h);display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:3px;font-size:11px;color:var(--hint);min-width:0}
#tabs button span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
#tabs button.on{color:var(--accent)}

/* ---------- typography ---------- */
h2.page{font:700 28px/1.15 var(--serif);margin:22px 0 4px}
h3.st{font:600 15px/1.3 var(--sans);margin:0 0 10px}
.hint{color:var(--hint);font-size:14px}
.lead{margin:6px 0 0;color:var(--fg);opacity:.86}
.shop{font:700 30px/1.12 var(--serif);margin:18px 0 0;letter-spacing:-.2px}

/* ---------- hero ---------- */
.hero{margin:0 -16px}
.hero img{display:block;width:100%;height:230px;object-fit:cover}
.hero-ph{height:170px;display:grid;place-items:center;background:var(--blue);color:#fff}
.pole{height:9px;background:repeating-linear-gradient(-45deg,var(--accent) 0 10px,#fff 10px 20px,var(--blue) 20px 30px,#fff 30px 40px)}
.info{list-style:none;padding:0;margin:18px 0 20px}
.info li{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--line)}
.info li:last-child{border-bottom:1px solid var(--line)}
.info .ic{margin-top:2px;color:var(--hint)}
.info details summary{cursor:pointer;list-style:none}
.info details summary::-webkit-details-marker{display:none}
.info details summary::after{content:"Haftalik jadval";display:block;color:var(--blue);font-size:13px;margin-top:2px}
.info details[open] summary::after{content:"Yopish"}
.wk{margin-top:8px;display:grid;gap:4px;font-size:14px}
.wk div{display:flex;justify-content:space-between;gap:16px;color:var(--hint)}
.wk div.today{color:var(--fg);font-weight:600}

/* ---------- buttons / fields ---------- */
.btn{display:flex;align-items:center;justify-content:center;gap:8px;min-height:50px;padding:0 20px;border-radius:14px;
  font-weight:600;background:var(--soft);text-align:center}
.btn.primary{background:var(--accent);color:var(--accent-fg)}
.btn.wide{width:100%}
.btn.small{min-height:38px;padding:0 14px;border-radius:10px;font-size:14px}
.btn.danger{color:var(--bad)}
.btn[disabled]{opacity:.45;pointer-events:none}
.field{display:block;margin:0 0 14px}
.field>span{display:block;font-size:13px;color:var(--hint);margin:0 0 6px}
.inp{width:100%;min-height:48px;padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:transparent;outline:none}
.inp:focus{border-color:var(--accent)}
textarea.inp{min-height:84px;resize:vertical}
.row{display:flex;gap:10px;align-items:center}
.row>*{min-width:0}
.sw{position:relative;display:inline-block;width:46px;height:28px;flex:none}
.sw input{position:absolute;opacity:0;inset:0;width:100%;height:100%;margin:0;cursor:pointer;z-index:1}
.sw span{position:absolute;inset:0;border-radius:99px;background:var(--line);transition:background .15s}
.sw span::after{content:"";position:absolute;left:3px;top:3px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .15s;box-shadow:0 1px 2px rgba(0,0,0,.3)}
.sw input:checked+span{background:var(--ok)}
.sw input:checked+span::after{transform:translateX(18px)}

/* ---------- price board (xizmatlar) ---------- */
.board{margin:10px 0 0}
.svc{display:grid;grid-template-columns:auto 1fr auto;align-items:baseline;column-gap:8px;width:100%;padding:15px 0;
  border-bottom:1px solid var(--line)}
.svc-name{font:600 18px/1.25 var(--serif)}
.dots{border-bottom:2px dotted var(--line);transform:translateY(-5px);min-width:12px}
.svc-price{font:600 17px/1.25 var(--sans);font-variant-numeric:tabular-nums;white-space:nowrap}
.svc-sub{grid-column:1/-1;font-size:13px;color:var(--hint);margin-top:3px}
.svc.sel{box-shadow:inset 3px 0 0 var(--accent);padding-left:13px}
.svc.sel .svc-name{color:var(--accent)}
.svc.off .svc-name{text-decoration:line-through;opacity:.6}

/* ---------- booking steps ---------- */
.step{margin:24px 0 0;scroll-margin-top:12px}
.days{display:flex;gap:8px;overflow-x:auto;margin:0 -16px;padding:2px 16px 6px;scrollbar-width:none;scroll-snap-type:x proximity}
.days::-webkit-scrollbar{display:none}
.day{flex:none;width:54px;padding:8px 0 9px;border-radius:12px;border:1px solid var(--line);text-align:center;scroll-snap-align:start}
.day small{display:block;font-size:12px;color:var(--hint)}
.day b{display:block;font:700 20px/1.2 var(--serif);font-variant-numeric:lining-nums}
.day.sel{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.day.sel small{color:inherit;opacity:.85}
.day.none{opacity:.38;border-style:dashed;pointer-events:none}
.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.slot{min-height:46px;border-radius:12px;border:1px solid var(--line);text-align:center;display:grid;place-items:center;
  font-weight:600;font-variant-numeric:tabular-nums}
.slot.sel{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.empty{padding:26px 16px;border:1px dashed var(--line);border-radius:12px;text-align:center;color:var(--hint)}
.empty b{display:block;color:var(--fg);font:700 18px/1.3 var(--serif);margin-bottom:4px}
.empty .btn{margin:14px auto 0;max-width:260px}
.cta{position:fixed;left:0;right:0;bottom:calc(var(--tab-h) + var(--safe-b));z-index:15;padding:12px 16px;
  background:linear-gradient(to top,var(--bg) 70%,transparent)}
.cta .btn{max-width:528px;margin:0 auto}
.cta-sp{height:84px}

/* ---------- ticket (bron) ---------- */
.ticket{position:relative;display:grid;grid-template-columns:88px 1fr;background:var(--soft);border-radius:8px;margin:0 0 14px}
.ticket::before,.ticket::after{content:"";position:absolute;left:81px;width:14px;height:14px;border-radius:50%;background:var(--bg)}
.ticket::before{top:-7px}.ticket::after{bottom:-7px}
.tk-l{padding:16px 6px 16px 14px;display:flex;flex-direction:column;justify-content:center}
.tk-time{font:700 25px/1 var(--serif);font-variant-numeric:lining-nums tabular-nums}
.tk-end{font-size:12px;color:var(--hint);margin-top:5px;font-variant-numeric:tabular-nums}
.tk-r{padding:14px 14px 14px 16px;border-left:2px dashed var(--line);min-width:0}
.tk-name{font:600 17px/1.3 var(--serif)}
.tk-meta{font-size:14px;color:var(--hint);margin-top:2px}
.tk-price{font-weight:600;font-variant-numeric:tabular-nums}
.tk-act{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid currentColor;margin-left:6px;vertical-align:2px}
.badge.ok{color:var(--ok)}.badge.bad{color:var(--bad)}.badge.mut{color:var(--hint)}
.ticket.off{opacity:.62}
.ticket.off .tk-name{text-decoration:line-through}
.ticket.big{grid-template-columns:112px 1fr;animation:stamp .26s ease-out}
.ticket.big::before,.ticket.big::after{left:105px}
.ticket.big .tk-time{font-size:34px}
@keyframes stamp{from{transform:scale(1.1) rotate(-1.5deg);opacity:0}to{transform:none;opacity:1}}
.done-h{display:flex;gap:10px;align-items:center;margin:26px 0 16px}
.done-h .ok-dot{width:34px;height:34px;border-radius:50%;background:var(--ok);color:#fff;display:grid;place-items:center}

/* ---------- admin ---------- */
.seg{display:flex;gap:8px;overflow-x:auto;margin:14px -16px 6px;padding:2px 16px;scrollbar-width:none}
.seg::-webkit-scrollbar{display:none}
.seg button{flex:none;padding:9px 16px;border-radius:99px;background:var(--soft);font-weight:600;font-size:14px}
.seg button.on{background:var(--fg);color:var(--bg)}
.hr{padding:14px 0;border-bottom:1px solid var(--line)}
.hr-top{display:flex;justify-content:space-between;align-items:center}
.hr-t{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center;margin-top:10px}
.hr-t.br{margin-top:8px}
.hr-t .inp{min-height:42px;padding:6px 10px}
.hr-lb{font-size:13px;color:var(--hint);margin-top:10px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.chip{display:inline-flex;gap:6px;align-items:center;padding:6px 8px 6px 12px;border-radius:99px;background:var(--soft);font-size:14px}
.chip button{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;color:var(--hint)}
.photo{width:100%;height:150px;border-radius:12px;object-fit:cover;background:var(--soft);display:block}
.sk{height:64px;border-radius:10px;background:var(--soft);margin:0 0 12px;animation:pulse 1.2s ease-in-out infinite alternate}
@keyframes pulse{from{opacity:.55}to{opacity:1}}

/* ---------- sheet / toast ---------- */
.back{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:40;display:flex;align-items:flex-end;justify-content:center}
.sheet{width:100%;max-width:560px;max-height:88vh;overflow:auto;background:var(--bg);border-radius:20px 20px 0 0;
  padding:8px 16px calc(20px + var(--safe-b));animation:up .2s ease-out}
.sheet .grab{width:40px;height:4px;border-radius:2px;background:var(--line);margin:4px auto 14px}
@keyframes up{from{transform:translateY(40px);opacity:.4}to{transform:none;opacity:1}}
#toast{position:fixed;left:16px;right:16px;bottom:calc(var(--tab-h) + var(--safe-b) + 84px);z-index:60;display:flex;justify-content:center;pointer-events:none}
#toast div{max-width:420px;padding:11px 16px;border-radius:12px;background:var(--fg);color:var(--bg);font-size:14px;box-shadow:0 6px 24px rgba(0,0,0,.25)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:340px){.slots{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<main id="view"></main>
<div id="cta-root"></div>
<nav id="tabs"></nav>
<div id="sheet-root"></div>
<div id="toast"></div>
<script>
(function(){
'use strict';
var tg=window.Telegram&&window.Telegram.WebApp;
var root=document.documentElement;
var RU={};var LANG='uz';
function t(s){return (LANG==='ru'&&RU[s])||s}
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

/* ---------- yordamchilar ---------- */
var WD=['Dushanba','Seshanba','Chorshanba','Payshanba','Juma','Shanba','Yakshanba'];
var WDS=['Du','Se','Ch','Pa','Ju','Sh','Ya'];
var MON=['yanvar','fevral','mart','aprel','may','iyun','iyul','avgust','sentabr','oktabr','noyabr','dekabr'];
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function money(n){return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
function parseISO(s){var p=s.split('-').map(Number);return new Date(p[0],p[1]-1,p[2])}
function iso(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function addDays(s,n){var d=parseISO(s);d.setDate(d.getDate()+n);return iso(d)}
function wdi(d){return (d.getDay()+6)%7}
function dShort(s){var d=parseISO(s);return d.getDate()+'-'+MON[d.getMonth()]}
function dFull(s){var d=parseISO(s);return d.getDate()+'-'+MON[d.getMonth()]+', '+WD[wdi(d)].toLowerCase()}
function $(s,r){return (r||document).querySelector(s)}
var ICONS={
  home:'<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10"/>',
  scissors:'<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4 8.1 15.9M14.5 14.5 20 20M8.1 8.1 12 12"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
  ticket:'<path d="M3 9a2 2 0 0 0 0 6v3h18v-3a2 2 0 0 1 0-6V6H3z"/><path d="M14 6v12" stroke-dasharray="2 3"/>',
  shield:'<path d="M12 3 20 6v6c0 5-3.4 8-8 9-4.6-1-8-4-8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  pin:'<path d="M12 21s7-6.2 7-11a7 7 0 0 0-14 0c0 4.8 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  phone:'<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/>',
  check:'<path d="m5 12.5 4.5 4.5L19 7"/>',
  x:'<path d="M6 6l12 12M18 6 6 18"/>'
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
    headers:{'Content-Type':'application/json','X-Init-Data':tg?tg.initData:''},
    body:JSON.stringify(body||{})
  }).then(function(r){
    return r.json().catch(function(){return {}}).then(function(j){
      if(!r.ok){var e=new Error(j.error||t("Xatolik yuz berdi"));e.status=r.status;throw e}
      return j;
    });
  },function(){var e=new Error(t("Internet bilan aloqa yo'q"));e.status=0;throw e});
}
function fail(e){toast(e&&e.message?e.message:t("Xatolik yuz berdi"))}

/* ---------- holat ---------- */
var S={init:null,tab:'home',err:null,
  book:{sv:null,days:null,date:null,slots:null,time:null,name:'',phone:'',busy:false,done:null},
  my:null,
  adm:{sub:'bookings',date:null,rows:null,services:null,hours:null,daysOff:null,settings:null}};
var view=$('#view');

/* ---------- ko'rinishlar: mijoz ---------- */
function empty(title,text,btn){
  return '<div class="empty"><b>'+esc(title)+'</b>'+esc(text||'')+(btn||'')+'</div>';
}
function viewHome(){
  var s=S.init.shop,hrs=S.init.hours,off=S.init.days_off,today=S.init.today;
  var wd=wdi(parseISO(today)),th=hrs[wd];
  var closed=!th||th.off||off.indexOf(today)>=0;
  var h='<div class="hero">'+((s.photo_v&&s.photo_v!=='0')?'<img src="/photo?v='+esc(s.photo_v)+'" alt="">':'<div class="hero-ph">'+ic('scissors',56)+'</div>')+'<div class="pole"></div></div>';
  h+='<h1 class="shop">'+esc(s.name)+'</h1>';
  if(s.description)h+='<p class="lead">'+esc(s.description)+'</p>';
  h+='<ul class="info">';
  if(s.address)h+='<li>'+ic('pin')+'<a href="https://maps.google.com/?q='+encodeURIComponent(s.address)+'" target="_blank" rel="noopener">'+esc(s.address)+'</a></li>';
  var wk=hrs.map(function(x,i){
    return '<div class="'+(i===wd?'today':'')+'"><span>'+WD[i]+'</span><span>'+(x.off?t("Dam olish"):x.open+' – '+x.close)+'</span></div>';
  }).join('');
  h+='<li>'+ic('clock')+'<details><summary>'+(closed?t("Bugun dam olish kuni"):t("Bugun")+' '+th.open+' – '+th.close)+'</summary><div class="wk">'+wk+'</div></details></li>';
  if(s.phone)h+='<li>'+ic('phone')+'<a href="tel:'+esc(s.phone.replace(/[^\d+]/g,''))+'">'+esc(s.phone)+'</a></li>';
  h+='</ul>';
  h+='<button class="btn primary wide" data-act="tab" data-tab="book">'+t("Vaqt band qilish")+'</button>';
  return h;
}
function svcRow(s,sel,act){
  return '<button class="svc'+(sel?' sel':'')+'" data-act="'+act+'" data-id="'+s.id+'"><span class="svc-name">'+esc(s.name)+'</span><span class="dots"></span><span class="svc-price">'+money(s.price)+'</span><span class="svc-sub">'+s.duration+' '+t("daqiqa")+'</span></button>';
}
function viewServices(){
  var sv=S.init.services;
  var h='<h2 class="page">'+t("Xizmatlar")+'</h2><div class="hint">'+t("Narxlar so'mda. Xizmatni bosing va vaqt tanlang.")+'</div>';
  if(!sv.length)return h+'<div style="height:16px"></div>'+empty(t("Hozircha xizmatlar yo'q"),t("Sartarosh xizmatlarni qo'shgach, shu yerda ko'rinadi."));
  return h+'<div class="board">'+sv.map(function(s){return svcRow(s,false,'svc-go')}).join('')+'</div>';
}
function section(id,title,body){return '<section class="step" id="step-'+id+'"><h3 class="st">'+title+'</h3>'+body+'</section>'}
function daysHtml(){
  var b=S.book;
  if(!b.days)return '<div class="days"><div class="sk" style="width:54px;height:64px;margin:0"></div><div class="sk" style="width:54px;height:64px;margin:0"></div><div class="sk" style="width:54px;height:64px;margin:0"></div></div>';
  if(!b.days.some(function(d){return d.open}))return empty(t("Bo'sh kunlar yo'q"),t("Hozircha bu xizmat uchun bo'sh vaqt topilmadi. Keyinroq qayta urinib ko'ring."));
  return '<div class="days">'+b.days.map(function(d){
    var dd=parseISO(d.date);
    return '<button class="day'+(d.date===b.date?' sel':'')+(d.open?'':' none')+'" data-act="pick-day" data-d="'+d.date+'"><small>'+WDS[wdi(dd)]+'</small><b>'+dd.getDate()+'</b></button>';
  }).join('')+'</div>'+(b.date?'<div class="hint" style="margin-top:8px">'+dFull(b.date)+'</div>':'');
}
function slotsHtml(){
  var b=S.book;
  if(!b.slots)return '<div class="slots">'+'<div class="sk" style="height:46px;margin:0"></div>'.repeat(8)+'</div>';
  if(!b.slots.length)return empty(t("Bu kunda bo'sh vaqt yo'q"),t("Boshqa sanani tanlang."));
  return '<div class="slots">'+b.slots.map(function(x){return '<button class="slot'+(x===b.time?' sel':'')+'" data-act="pick-time" data-t="'+x+'">'+x+'</button>'}).join('')+'</div>';
}
function formHtml(){
  var b=S.book;
  return '<label class="field"><span>'+t("Ismingiz")+'</span><input class="inp" data-bind="b-name" autocomplete="name" maxlength="60" value="'+esc(b.name)+'"></label>'+
    '<label class="field"><span>'+t("Telefon raqamingiz")+'</span><input class="inp" data-bind="b-phone" type="tel" inputmode="tel" autocomplete="tel" maxlength="20" placeholder="+998 90 123 45 67" value="'+esc(b.phone)+'"></label>';
}
function phoneOk(p){var d=String(p||'').replace(/\D/g,'');return d.length>=9&&d.length<=15}
function ticket(b,opt){
  opt=opt||{};
  var cls='ticket'+(b.status!=='active'?' off':'')+(opt.big?' big':'');
  var badge=b.status==='cancelled'?'<span class="badge bad">'+t("Bekor qilingan")+'</span>':(opt.past?'<span class="badge mut">'+t("O'tgan")+'</span>':'');
  var h='<div class="'+cls+'"><div class="tk-l"><div class="tk-time">'+b.time+'</div><div class="tk-end">'+(opt.showDate?dShort(b.date):t("tugash")+' '+b.end)+'</div></div><div class="tk-r">';
  h+='<div class="tk-name">'+esc(b.service)+badge+'</div>';
  h+='<div class="tk-meta">'+(opt.showDate?'':dFull(b.date)+' · ')+'<span class="tk-price">'+money(b.price)+' '+t("so'm")+'</span></div>';
  if(opt.admin)h+='<div class="tk-meta"><b style="color:var(--fg)">'+esc(b.name)+'</b> · <a href="tel:'+esc(b.phone)+'">'+esc(b.phone)+'</a></div>';
  if(opt.actions)h+='<div class="tk-act">'+opt.actions+'</div>';
  return h+'</div></div>';
}
function viewBook(){
  var b=S.book;
  if(b.done)return viewDone();
  var sv=S.init.services;
  if(!sv.length)return '<h2 class="page">'+t("Band qilish")+'</h2><div style="height:16px"></div>'+empty(t("Hozircha xizmatlar yo'q"),t("Sartarosh xizmatlarni qo'shgach, shu yerda band qilish mumkin bo'ladi."));
  var h='<h2 class="page">'+t("Band qilish")+'</h2>';
  h+=section('sv',t("Xizmat"),'<div class="board" style="margin:0">'+sv.map(function(s){return svcRow(s,b.sv&&b.sv.id===s.id,'pick-sv')}).join('')+'</div>');
  if(b.sv)h+=section('date',t("Sana"),daysHtml());
  if(b.sv&&b.date)h+=section('time',t("Vaqt"),slotsHtml());
  if(b.sv&&b.date&&b.time)h+=section('form',t("Ma'lumotlaringiz"),formHtml());
  return h+'<div class="cta-sp"></div>';
}
function viewDone(){
  var d=S.book.done;
  return '<div class="done-h"><span class="ok-dot">'+ic('check',20)+'</span><h2 class="page" style="margin:0">'+t("Bron tasdiqlandi")+'</h2></div>'+
    ticket(d,{big:true})+
    '<p class="hint">'+t("Tasdiqlash xabari Telegramga yuborildi. Vaqtidan oldin eslatamiz.")+'</p>'+
    '<div style="display:grid;gap:10px;margin-top:18px"><button class="btn primary" data-act="tab" data-tab="my">'+t("Mening bronlarim")+'</button>'+
    '<button class="btn" data-act="book-again">'+t("Yana band qilish")+'</button></div>';
}
function viewMy(){
  var h='<h2 class="page">'+t("Bronlarim")+'</h2>';
  if(!S.my)return h+'<div style="height:14px"></div><div class="sk"></div><div class="sk"></div>';
  var up=S.my.upcoming,past=S.my.past;
  if(!up.length&&!past.length)return h+'<div style="height:14px"></div>'+empty(t("Hali bron yo'q"),t("Birinchi vaqtingizni band qiling."),'<button class="btn primary" data-act="tab" data-tab="book">'+t("Band qilish")+'</button>');
  h+='<div style="height:16px"></div>';
  if(up.length)h+=up.map(function(x){return ticket(x,{showDate:true,actions:'<button class="btn small danger" data-act="cancel-my" data-id="'+x.id+'">'+t("Bekor qilish")+'</button>'})}).join('');
  else h+='<div class="hint" style="margin-bottom:16px">'+t("Kelgusi bronlar yo'q.")+'</div>';
  if(past.length)h+='<h3 class="st" style="margin:22px 0 12px">'+t("Avvalgilar")+'</h3>'+past.map(function(x){return ticket(x,{showDate:true,past:x.status==='active'})}).join('');
  return h;
}

/* ---------- ko'rinishlar: admin ---------- */
function viewAdmin(){
  var a=S.adm;
  var subs=[['bookings',"Bronlar"],['services',"Xizmatlar"],['hours',"Ish vaqti"],['settings',"Sozlamalar"]];
  var h='<h2 class="page">'+t("Admin")+'</h2><div class="seg">'+subs.map(function(x){return '<button class="'+(a.sub===x[0]?'on':'')+'" data-act="adm-sub" data-s="'+x[0]+'">'+t(x[1])+'</button>'}).join('')+'</div>';
  if(a.sub==='bookings')return h+admBookings();
  if(a.sub==='services')return h+admServices();
  if(a.sub==='hours')return h+admHours();
  return h+admSettings();
}
function admBookings(){
  var a=S.adm,today=S.init.today,tom=addDays(today,1);
  var h='<div class="row" style="margin:14px 0 4px"><button class="btn small'+(a.date===today?' primary':'')+'" data-act="adm-date" data-d="'+today+'">'+t("Bugun")+'</button>'+
    '<button class="btn small'+(a.date===tom?' primary':'')+'" data-act="adm-date" data-d="'+tom+'">'+t("Ertaga")+'</button>'+
    '<input class="inp" type="date" style="min-height:38px;padding:4px 10px;flex:1" data-bind="adm-date" value="'+a.date+'"></div>';
  if(!a.rows)return h+'<div style="height:12px"></div><div class="sk"></div><div class="sk"></div>';
  h+='<div class="hint" style="margin:10px 0 14px">'+dFull(a.date)+' · '+a.rows.active+' '+t("ta faol bron")+'</div>';
  if(!a.rows.bookings.length)return h+empty(t("Bu kunga bron yo'q"),t("Yangi bron bo'lsa, Telegramda xabar keladi."));
  return h+a.rows.bookings.map(function(x){
    var act=x.status==='active'?'<button class="btn small danger" data-act="adm-cancel" data-id="'+x.id+'">'+t("Bekor qilish")+'</button>':'';
    return ticket(x,{admin:true,actions:act});
  }).join('');
}
function admServices(){
  var a=S.adm;
  if(!a.services)return '<div style="height:12px"></div><div class="sk"></div><div class="sk"></div>';
  return '<div style="height:6px"></div><div class="board">'+a.services.map(function(s){
    return '<button class="svc'+(s.active?'':' off')+'" data-act="sv-edit" data-id="'+s.id+'"><span class="svc-name">'+esc(s.name)+'</span><span class="dots"></span><span class="svc-price">'+money(s.price)+'</span><span class="svc-sub">'+s.duration+' '+t("daqiqa")+(s.active?'':' · '+t("yashirin"))+'</span></button>';
  }).join('')+'</div><div style="margin-top:18px"><button class="btn primary wide" data-act="sv-new">'+t("Yangi xizmat")+'</button></div>';
}
function admHours(){
  var a=S.adm;
  if(!a.hours)return '<div style="height:12px"></div><div class="sk"></div><div class="sk"></div>';
  var h='<div style="height:6px"></div>';
  h+=a.hours.map(function(x,i){
    var r='<div class="hr"><div class="hr-top"><b>'+WD[i]+'</b><label class="sw"><input type="checkbox" data-bind="hr-open" data-i="'+i+'"'+(x.off?'':' checked')+'><span></span></label></div>';
    if(!x.off){
      r+='<div class="hr-t"><input class="inp" type="time" data-bind="hr-o" data-i="'+i+'" value="'+x.open+'"><span class="hint">–</span><input class="inp" type="time" data-bind="hr-c" data-i="'+i+'" value="'+x.close+'"></div>';
      r+='<div class="hr-lb">'+t("Tanaffus (ixtiyoriy)")+'</div><div class="hr-t br"><input class="inp" type="time" data-bind="hr-bs" data-i="'+i+'" value="'+x.br_start+'"><span class="hint">–</span><input class="inp" type="time" data-bind="hr-be" data-i="'+i+'" value="'+x.br_end+'"></div>';
    }
    return r+'</div>';
  }).join('');
  h+='<div style="margin:16px 0 0"><button class="btn primary wide" data-act="hrs-save">'+t("Ish vaqtini saqlash")+'</button></div>';
  h+='<h3 class="st" style="margin:30px 0 4px">'+t("Dam olish kunlari")+'</h3><div class="hint">'+t("Bayram yoki boshqa sabab bilan yopiq kunlar.")+'</div>';
  h+='<div class="row" style="margin-top:12px"><input class="inp" type="date" data-bind="off-date" value="'+(a.offPick||'')+'" min="'+S.init.today+'"><button class="btn small" data-act="dayoff-add" style="flex:none">'+t("Qo'shish")+'</button></div>';
  h+='<div class="chips">'+(a.daysOff.length?a.daysOff.map(function(d){return '<span class="chip">'+dShort(d)+'<button data-act="dayoff-rm" data-d="'+d+'" aria-label="'+t("O'chirish")+'">'+ic('x',14)+'</button></span>'}).join(''):'<span class="hint">'+t("Hozircha yo'q")+'</span>')+'</div>';
  return h;
}
function sel(name,val,opts){
  return '<select class="inp" data-bind="'+name+'">'+opts.map(function(o){return '<option value="'+o[0]+'"'+(String(o[0])===String(val)?' selected':'')+'>'+o[1]+'</option>'}).join('')+'</select>';
}
function admSettings(){
  var a=S.adm,s=a.settings;
  if(!s)return '<div style="height:12px"></div><div class="sk"></div><div class="sk"></div>';
  var hasPhoto=S.init.shop.photo_v&&S.init.shop.photo_v!=='0';
  var h='<div style="height:10px"></div>';
  h+='<div class="field"><span>'+t("Rasm")+'</span>'+(hasPhoto?'<img class="photo" src="/photo?v='+esc(S.init.shop.photo_v)+'" alt="">':'<div class="photo" style="display:grid;place-items:center;color:var(--hint)">'+t("Rasm yo'q")+'</div>')+
    '<div class="row" style="margin-top:10px"><label class="btn small" style="flex:1;cursor:pointer">'+t("Rasm tanlash")+'<input type="file" accept="image/*" data-bind="photo-file" hidden></label>'+(hasPhoto?'<button class="btn small danger" data-act="photo-rm">'+t("Olib tashlash")+'</button>':'')+'</div></div>';
  h+='<label class="field"><span>'+t("Sartaroshxona nomi")+'</span><input class="inp" data-bind="s-name" maxlength="60" value="'+esc(s.name)+'"></label>';
  h+='<label class="field"><span>'+t("Qisqa tavsif")+'</span><textarea class="inp" data-bind="s-description" maxlength="400">'+esc(s.description)+'</textarea></label>';
  h+='<label class="field"><span>'+t("Manzil")+'</span><input class="inp" data-bind="s-address" maxlength="200" value="'+esc(s.address)+'"></label>';
  h+='<label class="field"><span>'+t("Telefon")+'</span><input class="inp" type="tel" data-bind="s-phone" maxlength="30" placeholder="+998 90 123 45 67" value="'+esc(s.phone)+'"></label>';
  h+='<label class="field"><span>'+t("Bron vaqti qadami")+'</span>'+sel('s-step',s.step,[[15,'15 '+t("daqiqa")],[20,'20 '+t("daqiqa")],[30,'30 '+t("daqiqa")],[45,'45 '+t("daqiqa")],[60,'60 '+t("daqiqa")]])+'</label>';
  h+='<label class="field"><span>'+t("Eslatma: bronga necha soat qolganda")+'</span>'+sel('s-remind_hours',s.remind_hours,[[1,'1 '+t("soat")],[2,'2 '+t("soat")],[3,'3 '+t("soat")]])+'</label>';
  h+='<label class="field"><span>'+t("Necha kun oldindan band qilish mumkin")+'</span>'+sel('s-horizon',s.horizon,[[7,'7 '+t("kun")],[14,'14 '+t("kun")],[30,'30 '+t("kun")],[60,'60 '+t("kun")]])+'</label>';
  h+='<button class="btn primary wide" data-act="set-save">'+t("Saqlash")+'</button>';
  return h;
}

/* ---------- chizish ---------- */
function renderTabs(){
  var tabs=[['home','Asosiy','home'],['services','Xizmatlar','scissors'],['book','Band qilish','calendar'],['my','Bronlarim','ticket']];
  if(S.init&&S.init.is_admin)tabs.push(['admin','Admin','shield']);
  $('#tabs').innerHTML=tabs.map(function(x){return '<button class="'+(S.tab===x[0]?'on':'')+'" data-act="tab" data-tab="'+x[0]+'">'+ic(x[2])+'<span>'+t(x[1])+'</span></button>'}).join('');
}
function renderCta(){
  var b=S.book,el=$('#cta-root');
  if(S.tab!=='book'||b.done||!b.sv||!b.time){el.innerHTML='';return}
  var ok=b.name.trim().length>=2&&phoneOk(b.phone);
  el.innerHTML='<div class="cta"><button class="btn primary wide" data-act="book"'+((ok&&!b.busy)?'':' disabled')+'>'+(b.busy?t("Yuborilmoqda..."):(ok?t("Band qilish")+' · '+money(b.sv.price)+' '+t("so'm"):t("Ism va telefonni kiriting")))+'</button></div>';
}
function render(){
  if(S.err){
    view.innerHTML='<h2 class="page">'+t("Ochilmadi")+'</h2><div style="height:12px"></div>'+empty(esc(S.err),t("Telegramdagi bot orqali ilovani qayta oching."),'<button class="btn" data-act="reload">'+t("Qayta urinish")+'</button>');
    $('#tabs').innerHTML='';$('#cta-root').innerHTML='';return;
  }
  if(!S.init){view.innerHTML='<div style="padding-top:90px;text-align:center;color:var(--hint)">'+ic('scissors',44)+'<div style="margin-top:10px">'+t("Yuklanmoqda...")+'</div></div>';return}
  var m={home:viewHome,services:viewServices,book:viewBook,my:viewMy,admin:viewAdmin};
  view.innerHTML=(m[S.tab]||viewHome)();
  renderTabs();renderCta();
}
function scrollToStep(id){
  requestAnimationFrame(function(){var el=document.getElementById('step-'+id);if(el)el.scrollIntoView({behavior:'smooth',block:'start'})});
}

/* ---------- ma'lumot yuklash ---------- */
function loadDays(){
  var b=S.book;b.days=null;b.date=null;b.slots=null;b.time=null;render();
  var id=b.sv.id;
  api('days',{service_id:id}).then(function(r){
    if(!b.sv||b.sv.id!==id)return;
    b.days=r.days;
    var first=r.days.filter(function(d){return d.open})[0];
    if(first){pickDay(first.date,true)}else{render()}
  }).catch(function(e){fail(e);b.days=[];render()});
}
function pickDay(date,quiet){
  var b=S.book;b.date=date;b.slots=null;b.time=null;render();
  var id=b.sv.id;
  api('slots',{service_id:id,date:date}).then(function(r){
    if(!b.sv||b.sv.id!==id||b.date!==date)return;
    b.slots=r.slots;render();if(!quiet)scrollToStep('time');
  }).catch(function(e){fail(e);b.slots=[];render()});
}
function loadMy(){
  S.my=null;render();
  api('my').then(function(r){S.my=r;render()}).catch(function(e){fail(e);S.my={upcoming:[],past:[]};render()});
}
function loadAdm(){
  var a=S.adm;
  if(a.sub==='bookings'){
    if(!a.date)a.date=S.init.today;
    a.rows=null;render();
    var d=a.date;
    api('admin/bookings',{date:d}).then(function(r){if(a.date===d){a.rows=r;render()}}).catch(function(e){fail(e);a.rows={bookings:[],active:0};render()});
  }else if(a.sub==='services'){
    a.services=null;render();
    api('admin/services').then(function(r){a.services=r.services;render()}).catch(fail);
  }else if(a.sub==='hours'){
    a.hours=null;render();
    api('admin/hours').then(function(r){a.hours=r.hours;a.daysOff=r.days_off;render()}).catch(fail);
  }else{
    a.settings=null;render();
    api('admin/settings').then(function(r){a.settings=r.settings;render()}).catch(fail);
  }
}
function go(tab){
  S.tab=tab;
  if(tab==='my')loadMy();
  else if(tab==='admin')loadAdm();
  else render();
  window.scrollTo(0,0);
}
function resetBook(){S.book={sv:null,days:null,date:null,slots:null,time:null,name:S.init.profile.name||'',phone:S.init.profile.phone||'',busy:false,done:null}}
function pickService(id){
  var sv=S.init.services.filter(function(x){return x.id===id})[0];if(!sv)return;
  var b=S.book;b.sv=sv;loadDays();
}
function doBook(){
  var b=S.book;if(b.busy)return;
  b.busy=true;renderCta();
  api('book',{service_id:b.sv.id,date:b.date,time:b.time,name:b.name.trim(),phone:b.phone.trim()}).then(function(r){
    b.busy=false;b.done=r.booking;S.my=null;
    S.init.profile={name:b.name.trim(),phone:b.phone.trim()};
    hap('ok');render();window.scrollTo(0,0);
  }).catch(function(e){
    b.busy=false;fail(e);
    if(e.status===409){b.time=null;pickDay(b.date,true)}else{renderCta()}
  });
}

/* ---------- sheet (xizmat tahrirlash) ---------- */
function closeSheet(){$('#sheet-root').innerHTML=''}
function openSheet(html){
  $('#sheet-root').innerHTML='<div class="back" data-act="sheet-close"><div class="sheet" data-stop="1"><div class="grab"></div>'+html+'</div></div>';
}
function svSheet(s){
  s=s||{id:0,name:'',price:'',duration:30,active:true};
  openSheet('<h3 class="st" style="font:700 20px/1.2 var(--serif)">'+(s.id?t("Xizmatni tahrirlash"):t("Yangi xizmat"))+'</h3><div style="height:14px"></div>'+
    '<label class="field"><span>'+t("Nomi")+'</span><input class="inp" id="sv-name" maxlength="100" value="'+esc(s.name)+'"></label>'+
    '<div class="row"><label class="field" style="flex:1"><span>'+t("Narxi (so'm)")+'</span><input class="inp" id="sv-price" inputmode="numeric" value="'+esc(s.price)+'"></label>'+
    '<label class="field" style="flex:1"><span>'+t("Davomiyligi (daqiqa)")+'</span><input class="inp" id="sv-dur" inputmode="numeric" value="'+esc(s.duration)+'"></label></div>'+
    '<div class="row" style="justify-content:space-between;margin:2px 0 18px"><span>'+t("Mijozlarga ko'rsatilsin")+'</span><label class="sw"><input type="checkbox" id="sv-active"'+(s.active?' checked':'')+'><span></span></label></div>'+
    '<button class="btn primary wide" data-act="sv-save" data-id="'+s.id+'">'+t("Saqlash")+'</button>'+
    (s.id?'<div style="height:10px"></div><button class="btn wide danger" data-act="sv-del" data-id="'+s.id+'">'+t("Xizmatni o'chirish")+'</button>':''));
}

/* ---------- rasmni kichraytirish ---------- */
function shrinkImage(file){
  return new Promise(function(resolve,reject){
    var fr=new FileReader();
    fr.onerror=function(){reject(new Error(t("Rasmni o'qib bo'lmadi")))};
    fr.onload=function(){
      var im=new Image();
      im.onerror=function(){reject(new Error(t("Rasm formati qo'llab-quvvatlanmaydi")))};
      im.onload=function(){
        var max=900,q=0.82,out='';
        for(var i=0;i<4;i++){
          var k=Math.min(1,max/Math.max(im.width,im.height));
          var c=document.createElement('canvas');c.width=Math.round(im.width*k);c.height=Math.round(im.height*k);
          c.getContext('2d').drawImage(im,0,0,c.width,c.height);
          out=c.toDataURL('image/jpeg',q);
          if(out.length<420000)return resolve(out);
          max=Math.round(max*0.75);q=Math.max(0.6,q-0.07);
        }
        reject(new Error(t("Rasm juda katta")));
      };
      im.src=fr.result;
    };
    fr.readAsDataURL(file);
  });
}

/* ---------- hodisalar ---------- */
var ACT={
  'tab':function(el){if(S.tab==='book'&&S.book.done&&el.dataset.tab!=='book')resetBook();go(el.dataset.tab)},
  'reload':function(){location.reload()},
  'svc-go':function(el){resetBook();S.tab='book';pickService(+el.dataset.id);window.scrollTo(0,0)},
  'pick-sv':function(el){var id=+el.dataset.id;if(S.book.sv&&S.book.sv.id===id)return;hap();pickService(id)},
  'pick-day':function(el){hap();pickDay(el.dataset.d)},
  'pick-time':function(el){hap();S.book.time=el.dataset.t;render();scrollToStep('form')},
  'book':function(){doBook()},
  'book-again':function(){resetBook();go('book')},
  'cancel-my':function(el){
    var id=+el.dataset.id;
    ask(t("Bronni bekor qilasizmi?"),function(){
      api('cancel',{id:id}).then(function(){toast(t("Bron bekor qilindi"));loadMy()}).catch(fail);
    });
  },
  'adm-sub':function(el){S.adm.sub=el.dataset.s;loadAdm()},
  'adm-date':function(el){S.adm.date=el.dataset.d;loadAdm()},
  'adm-cancel':function(el){
    var id=+el.dataset.id;
    ask(t("Bronni bekor qilasizmi? Mijozga xabar yuboriladi."),function(){
      api('admin/cancel',{id:id}).then(function(){toast(t("Bron bekor qilindi"));loadAdm()}).catch(fail);
    });
  },
  'sv-new':function(){svSheet()},
  'sv-edit':function(el){var id=+el.dataset.id;svSheet(S.adm.services.filter(function(x){return x.id===id})[0])},
  'sv-save':function(el){
    var body={id:+el.dataset.id||0,name:$('#sv-name').value,price:$('#sv-price').value,duration:$('#sv-dur').value,active:$('#sv-active').checked};
    api('admin/service_save',body).then(function(r){
      S.adm.services=r.services;closeSheet();refreshServices();toast(t("Saqlandi"));render();
    }).catch(fail);
  },
  'sv-del':function(el){
    var id=+el.dataset.id;
    ask(t("Xizmat o'chirilsin? Oldingi bronlar saqlanib qoladi."),function(){
      api('admin/service_delete',{id:id}).then(function(r){S.adm.services=r.services;closeSheet();refreshServices();toast(t("O'chirildi"));render()}).catch(fail);
    });
  },
  'hrs-save':function(){
    api('admin/hours_save',{hours:S.adm.hours}).then(function(r){S.adm.hours=r.hours;S.init.hours=r.hours;toast(t("Saqlandi"));render()}).catch(fail);
  },
  'dayoff-add':function(){
    var d=S.adm.offPick;if(!d){toast(t("Sanani tanlang"));return}
    api('admin/dayoff',{date:d,off:true}).then(function(r){S.adm.daysOff=r.days_off;S.init.days_off=r.days_off;S.adm.offPick='';render()}).catch(fail);
  },
  'dayoff-rm':function(el){
    api('admin/dayoff',{date:el.dataset.d,off:false}).then(function(r){S.adm.daysOff=r.days_off;S.init.days_off=r.days_off;render()}).catch(fail);
  },
  'set-save':function(){
    api('admin/settings_save',S.adm.settings).then(function(r){
      S.adm.settings=r.settings;
      var sh=S.init.shop;sh.name=r.settings.name;sh.description=r.settings.description;sh.address=r.settings.address;sh.phone=r.settings.phone;
      S.init.horizon=r.settings.horizon;toast(t("Saqlandi"));render();
    }).catch(fail);
  },
  'photo-rm':function(){
    api('admin/photo',{remove:true}).then(function(){S.init.shop.photo_v='0';toast(t("Rasm olib tashlandi"));render()}).catch(fail);
  },
  'sheet-close':function(el,e){if(e.target===el)closeSheet()}
};
function refreshServices(){
  api('init').then(function(r){S.init.services=r.services;render()}).catch(function(){});
}
document.addEventListener('click',function(e){
  var el=e.target.closest('[data-act]');if(!el)return;
  var f=ACT[el.dataset.act];if(f)f(el,e);
});
var BIND={
  'b-name':function(el){S.book.name=el.value;renderCta()},
  'b-phone':function(el){S.book.phone=el.value;renderCta()},
  'adm-date':function(el){if(el.value&&el.value!==S.adm.date){S.adm.date=el.value;loadAdm()}},
  'off-date':function(el){S.adm.offPick=el.value},
  'hr-open':function(el){S.adm.hours[+el.dataset.i].off=!el.checked;render()},
  'hr-o':function(el){S.adm.hours[+el.dataset.i].open=el.value},
  'hr-c':function(el){S.adm.hours[+el.dataset.i].close=el.value},
  'hr-bs':function(el){S.adm.hours[+el.dataset.i].br_start=el.value},
  'hr-be':function(el){S.adm.hours[+el.dataset.i].br_end=el.value},
  'photo-file':function(el){
    var f=el.files&&el.files[0];if(!f)return;
    toast(t("Rasm yuklanmoqda..."));
    shrinkImage(f).then(function(data){return api('admin/photo',{image:data})}).then(function(r){
      S.init.shop.photo_v=r.photo_v;toast(t("Rasm saqlandi"));render();
    }).catch(fail);
  }
};
function onField(e){
  var el=e.target,k=el.dataset&&el.dataset.bind;if(!k)return;
  if(k.indexOf('s-')===0){S.adm.settings[k.slice(2)]=el.value;return}
  if(BIND[k])BIND[k](el);
}
document.addEventListener('input',onField);
document.addEventListener('change',onField);

/* ---------- ishga tushirish ---------- */
function start(){
  render();
  api('init').then(function(r){
    S.init=r;
    resetBook();
    var q=new URLSearchParams(location.search).get('tab');
    if(q==='admin'&&!r.is_admin)q='home';
    if(['my','book','admin','services'].indexOf(q)>=0)go(q);else render();
  }).catch(function(e){
    S.err=(e.status===401||!tg||!tg.initData)?t("Ilovani Telegram ichida oching"):e.message;
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
