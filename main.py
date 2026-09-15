# -*- coding: utf-8 -*-
"""
DANAT SHOP uslubidagi o'yin hisobini to'ldirish boti + Telegram Mini App
Bitta fayl: bot + Flask (Mini App) + SQLite + to'liq admin panel

ENV:
  BOT_TOKEN   = @BotFather tokeni
  ADMINS      = 7849637859,123456789
  WEBAPP_URL  = https://sizning-app.onrender.com
  PORT        = 10000 (Render o'zi beradi)
"""

import os, json, hmac, hashlib, sqlite3, threading, time, logging
from urllib.parse import parse_qsl
from datetime import datetime

import requests
from flask import Flask, request, jsonify, Response
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, KeyboardButton, WebAppInfo)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("danat")

BOT_TOKEN  = os.getenv("BOT_TOKEN", "").strip()
ADMINS     = [int(x) for x in os.getenv("ADMINS", "7849637859").replace(" ", "").split(",") if x]
# WEBAPP_URL ni qo'lda kiritish shart emas — Render/Replit o'z manzilini
# avtomatik environment orqali beradi, shundan foydalanamiz.
WEBAPP_URL = (
    os.getenv("WEBAPP_URL", "").strip().rstrip("/")
    or os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")   # Render avtomatik beradi
    or (f"https://{os.getenv('REPL_SLUG')}.{os.getenv('REPL_OWNER')}.repl.co"
        if os.getenv("REPL_SLUG") else "")                        # Replit avtomatik beradi
)
PORT       = int(os.getenv("PORT", "10000"))
DB_PATH    = os.getenv("DB_PATH", "shop.db")
API        = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------
LOCK = threading.Lock()


def _con():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def q(sql, args=(), one=False):
    with LOCK:
        con = _con()
        rows = con.execute(sql, args).fetchall()
        con.close()
    if one:
        return rows[0] if rows else None
    return rows


def x(sql, args=()):
    with LOCK:
        con = _con()
        cur = con.execute(sql, args)
        con.commit()
        lid = cur.lastrowid
        con.close()
    return lid


def init_db():
    with LOCK:
        con = _con()
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, username TEXT,
            balance INTEGER DEFAULT 0, lang TEXT DEFAULT 'uz',
            ref_by INTEGER DEFAULT 0, refs INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0, created TEXT);
        CREATE TABLE IF NOT EXISTS games(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, image TEXT,
            unit TEXT DEFAULT 'Olmoslar', need_server INTEGER DEFAULT 0,
            hint TEXT DEFAULT 'O''yin ID raqamingiz',
            active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS packages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, title TEXT,
            price INTEGER, old_price INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game_id INTEGER,
            pkg_id INTEGER, title TEXT, amount INTEGER, player_id TEXT,
            server_id TEXT, status TEXT DEFAULT 'pending', created TEXT);
        CREATE TABLE IF NOT EXISTS topups(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            method TEXT, status TEXT DEFAULT 'new', file_id TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS promos(
            code TEXT PRIMARY KEY, amount INTEGER, max_uses INTEGER DEFAULT 1,
            used INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER);
        CREATE TABLE IF NOT EXISTS channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, title TEXT, url TEXT);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS tx(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            note TEXT, created TEXT);
        """)
        con.commit()
        con.close()

    defaults = {
        "card_number": "5614 6831 1776 7954",
        "card_holder": "S MAMAZHONOVA",
        "card_type": "UZCARD",
        "card2_number": "9860 0101 0101 0101",
        "card2_holder": "S MAMAZHONOVA",
        "min_topup": "1000",
        "ref_bonus": "500",
        "support": "Akmaljon1100",
        "banner": "Eng yaxshi narxda — tez va ishonchli to'ldirish",
        "shop_name": "DANAT SHOP",
        "work": "24/7",
    }
    for k, v in defaults.items():
        if not q("SELECT 1 FROM settings WHERE k=?", (k,), one=True):
            x("INSERT INTO settings(k,v) VALUES(?,?)", (k, v))

    if not q("SELECT 1 FROM games LIMIT 1", one=True):
        seed()


def seed():
    # image maydoni: URL yoki oddiy emoji bo'lishi mumkin. Emoji bo'lsa
    # frontend rangli belgi sifatida chizadi (tashqi rasm serveriga bog'liq emas).
    data = [
        ("Free Fire (CIS)", "🔥", "Olmoslar", 0, "Free Fire ID", [
            ("100 + 10 Diamonds", 9990, 12000), ("310 + 31 Diamonds", 30500, 35000),
            ("520 + 52 Diamonds", 49000, 55000), ("1060 + 106 Diamonds", 99000, 110000),
            ("2180 + 218 Diamonds", 197000, 220000), ("5600 + 560 Diamonds", 499000, 600000)]),
        ("Free Fire (Lite)", "🔥", "Olmoslar", 0, "Free Fire ID", [
            ("110 Diamonds", 11500, 13000), ("341 Diamonds", 33500, 38000),
            ("572 Diamonds", 54000, 60000), ("1166 Diamonds", 108000, 120000)]),
        ("PUBGM (AUTO)", "🎯", "UC", 0, "PUBG Mobile ID", [
            ("60 UC", 11000, 13000), ("325 UC", 57900, 65000),
            ("660 UC", 112900, 120000), ("1800 UC", 284900, 300000),
            ("3850 UC", 559900, 600000), ("8100 UC", 1119000, 1200000)]),
        ("Telegram Stars", "⭐", "Stars", 0, "Telegram username (@siz)", [
            ("50 Stars", 12000, 14000), ("100 Stars", 23000, 26000),
            ("250 Stars", 56000, 62000), ("500 Stars", 110000, 125000)]),
        ("Telegram Premium", "✨", "Obuna", 0, "Telegram username (@siz)", [
            ("1 oy", 59000, 70000), ("3 oy", 149000, 175000), ("12 oy", 449000, 520000)]),
        ("Standoff 2", "🔫", "Gold", 0, "Standoff 2 ID", [
            ("100 Gold", 14000, 16000), ("500 Gold", 66000, 75000),
            ("1000 Gold", 129000, 145000)]),
        ("Mobile Legends", "💎", "Olmoslar", 1, "MLBB ID", [
            ("86 Diamonds", 22000, 25000), ("172 Diamonds", 43000, 48000),
            ("257 Diamonds", 64000, 71000), ("706 Diamonds", 170000, 190000)]),
    ]
    for i, (t, img, unit, ns, hint, pkgs) in enumerate(data):
        gid = x("INSERT INTO games(title,image,unit,need_server,hint,active,sort) VALUES(?,?,?,?,?,1,?)",
                (t, img, unit, ns, hint, i))
        for j, (pt, pr, op) in enumerate(pkgs):
            x("INSERT INTO packages(game_id,title,price,old_price,active,sort) VALUES(?,?,?,?,1,?)",
              (gid, pt, pr, op, j))


def S(k, d=""):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else d


def setS(k, v):
    x("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=?", (k, str(v), str(v)))


def get_user(uid):
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True)


def add_user(uid, name, username, ref_by=0):
    u = get_user(uid)
    if u:
        x("UPDATE users SET name=?,username=? WHERE id=?", (name, username, uid))
        return False
    x("INSERT INTO users(id,name,username,balance,lang,ref_by,created) VALUES(?,?,?,0,'uz',?,?)",
      (uid, name, username, ref_by, datetime.now().isoformat(timespec="seconds")))
    return True


def balance_add(uid, amount, note=""):
    x("UPDATE users SET balance=balance+? WHERE id=?", (int(amount), uid))
    x("INSERT INTO tx(user_id,amount,note,created) VALUES(?,?,?,?)",
      (uid, int(amount), note, datetime.now().isoformat(timespec="seconds")))


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


# ----------------------------------------------------------------------------
# Telegram HTTP helper (Flask thread uchun)
# ----------------------------------------------------------------------------
def tg(method, **kw):
    try:
        r = requests.post(f"{API}/{method}", json=kw, timeout=25)
        return r.json()
    except Exception as e:
        log.warning("tg %s: %s", method, e)
        return {}


def notify_admins(text, kb=None):
    for a in ADMINS:
        tg("sendMessage", chat_id=a, text=text, parse_mode="HTML",
           reply_markup={"inline_keyboard": kb} if kb else None)


def check_subs(uid):
    """Obuna bo'lmagan kanallar ro'yxatini qaytaradi."""
    bad = []
    for ch in q("SELECT * FROM channels"):
        try:
            r = requests.get(f"{API}/getChatMember",
                             params={"chat_id": ch["chat_id"], "user_id": uid}, timeout=12).json()
            st = r.get("result", {}).get("status")
            if st not in ("creator", "administrator", "member"):
                bad.append({"title": ch["title"], "url": ch["url"]})
        except Exception:
            pass
    return bad


# ----------------------------------------------------------------------------
# Mini App auth
# ----------------------------------------------------------------------------
def verify(init_data):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash", None)
        if not h:
            return None
        chk = "\n".join(f"{k}={d[k]}" for k in sorted(d))
        sk = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if hmac.new(sk, chk.encode(), hashlib.sha256).hexdigest() != h:
            return None
        return json.loads(d.get("user", "{}"))
    except Exception:
        return None


def who():
    data = request.get_json(silent=True) or {}
    u = verify(data.get("initData", ""))
    if not u or not u.get("id"):
        return None, data
    uid = int(u["id"])
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or "User"
    add_user(uid, name, u.get("username", ""))
    return get_user(uid), data


# ----------------------------------------------------------------------------
# Flask / Mini App
# ----------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.get("/health")
def health():
    return "ok"


@app.post("/api/init")
def api_init():
    u, _ = who()
    if not u:
        return jsonify(ok=False, error="auth"), 403
    if u["banned"]:
        return jsonify(ok=False, error="banned"), 403
    return jsonify(ok=True,
                   user=dict(id=u["id"], name=u["name"], balance=u["balance"],
                             lang=u["lang"], refs=u["refs"]),
                   subs=check_subs(u["id"]),
                   shop=dict(name=S("shop_name"), banner=S("banner"),
                             support=S("support"), min_topup=int(S("min_topup", "1000")),
                             ref_bonus=int(S("ref_bonus", "500"))),
                   games=[dict(r) for r in q(
                       "SELECT * FROM games WHERE active=1 ORDER BY sort,id")])


@app.post("/api/game")
def api_game():
    u, d = who()
    if not u:
        return jsonify(ok=False), 403
    g = q("SELECT * FROM games WHERE id=?", (d.get("id"),), one=True)
    if not g:
        return jsonify(ok=False), 404
    pk = q("SELECT * FROM packages WHERE game_id=? AND active=1 ORDER BY sort,id", (g["id"],))
    return jsonify(ok=True, game=dict(g), packages=[dict(r) for r in pk])


@app.post("/api/order")
def api_order():
    u, d = who()
    if not u:
        return jsonify(ok=False, error="auth"), 403
    if check_subs(u["id"]):
        return jsonify(ok=False, error="subs")
    p = q("SELECT * FROM packages WHERE id=? AND active=1", (d.get("pkg_id"),), one=True)
    if not p:
        return jsonify(ok=False, error="Paket topilmadi")
    g = q("SELECT * FROM games WHERE id=?", (p["game_id"],), one=True)
    pid = (d.get("player_id") or "").strip()
    sid = (d.get("server_id") or "").strip()
    if len(pid) < 3:
        return jsonify(ok=False, error="O'yin ID noto'g'ri")
    if u["balance"] < p["price"]:
        return jsonify(ok=False, error="Balans yetarli emas")

    balance_add(u["id"], -p["price"], f"Buyurtma: {g['title']} {p['title']}")
    oid = x("""INSERT INTO orders(user_id,game_id,pkg_id,title,amount,player_id,server_id,status,created)
              VALUES(?,?,?,?,?,?,?, 'pending', ?)""",
            (u["id"], g["id"], p["id"], f"{g['title']} — {p['title']}", p["price"],
             pid, sid, datetime.now().isoformat(timespec="seconds")))

    notify_admins(
        f"🧾 <b>Yangi buyurtma #{oid}</b>\n\n"
        f"👤 {u['name']} (<code>{u['id']}</code>) @{u['username'] or '-'}\n"
        f"🎮 {g['title']}\n📦 {p['title']}\n"
        f"🆔 <code>{pid}</code>{(' / ' + sid) if sid else ''}\n"
        f"💵 {fmt(p['price'])} so'm",
        [[{"text": "✅ Bajarildi", "callback_data": f"o:ok:{oid}"},
          {"text": "❌ Rad etish", "callback_data": f"o:no:{oid}"}]])
    tg("sendMessage", chat_id=u["id"],
       text=f"✅ Buyurtma #{oid} qabul qilindi.\n🎮 {g['title']} — {p['title']}\n"
            f"🆔 <code>{pid}</code>\n💵 {fmt(p['price'])} so'm\n\n⏳ Admin tasdiqlashini kuting.",
       parse_mode="HTML")
    return jsonify(ok=True, id=oid, balance=u["balance"] - p["price"])


@app.post("/api/topup")
def api_topup():
    u, d = who()
    if not u:
        return jsonify(ok=False), 403
    amount = int(d.get("amount") or 0)
    mn = int(S("min_topup", "1000"))
    if amount < mn:
        return jsonify(ok=False, error=f"Eng kam summa {fmt(mn)} so'm")
    method = d.get("method") or "UZCARD"
    tid = x("INSERT INTO topups(user_id,amount,method,status,created) VALUES(?,?,?,'new',?)",
            (u["id"], amount, method, datetime.now().isoformat(timespec="seconds")))
    card = S("card_number") if method == "UZCARD" else S("card2_number")
    holder = S("card_holder") if method == "UZCARD" else S("card2_holder")
    tg("sendMessage", chat_id=u["id"],
       text=f"💳 <b>To'lov #{tid}</b>\n\n"
            f"Summa: <b>{fmt(amount)}</b> so'm\n"
            f"Karta ({method}): <code>{card}</code>\n"
            f"Egasi: {holder}\n\n"
            f"👉 Pul o'tkazgach <b>chek skrinshotini shu yerga rasm qilib yuboring</b>.",
       parse_mode="HTML")
    return jsonify(ok=True, id=tid, card=card, holder=holder, method=method)


@app.post("/api/orders")
def api_orders():
    u, _ = who()
    if not u:
        return jsonify(ok=False), 403
    o = q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))
    t = q("SELECT * FROM topups WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],))
    return jsonify(ok=True, orders=[dict(r) for r in o], topups=[dict(r) for r in t])


@app.post("/api/promo")
def api_promo():
    u, d = who()
    if not u:
        return jsonify(ok=False), 403
    code = (d.get("code") or "").strip().upper()
    p = q("SELECT * FROM promos WHERE code=? AND active=1", (code,), one=True)
    if not p:
        return jsonify(ok=False, error="Promokod topilmadi")
    if p["used"] >= p["max_uses"]:
        return jsonify(ok=False, error="Promokod limiti tugagan")
    if q("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, u["id"]), one=True):
        return jsonify(ok=False, error="Siz bu promokodni ishlatgansiz")
    balance_add(u["id"], p["amount"], f"Promokod {code}")
    x("UPDATE promos SET used=used+1 WHERE code=?", (code,))
    x("INSERT INTO promo_uses(code,user_id) VALUES(?,?)", (code, u["id"]))
    return jsonify(ok=True, amount=p["amount"], balance=get_user(u["id"])["balance"])


@app.post("/api/lang")
def api_lang():
    u, d = who()
    if not u:
        return jsonify(ok=False), 403
    x("UPDATE users SET lang=? WHERE id=?", ((d.get("lang") or "uz")[:2], u["id"]))
    return jsonify(ok=True)


@app.post("/api/top")
def api_top():
    u, _ = who()
    if not u:
        return jsonify(ok=False), 403
    rows = q("""SELECT u.name, IFNULL(SUM(t.amount),0) s FROM users u
                JOIN tx t ON t.user_id=u.id AND t.amount>0
                GROUP BY u.id ORDER BY s DESC LIMIT 10""")
    return jsonify(ok=True, top=[dict(r) for r in rows])


# ----------------------------------------------------------------------------
# BOT — foydalanuvchi
# ----------------------------------------------------------------------------
def main_kb():
    if WEBAPP_URL:
        return ReplyKeyboardMarkup(
            [[KeyboardButton("🛍 Do'kon", web_app=WebAppInfo(url=WEBAPP_URL))]],
            resize_keyboard=True)
    return None


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ref = 0
    if ctx.args and ctx.args[0].startswith("ref"):
        try:
            ref = int(ctx.args[0][3:].replace("_", ""))
        except Exception:
            ref = 0
    new = add_user(u.id, u.full_name, u.username or "", ref if ref != u.id else 0)
    if new and ref and ref != u.id and get_user(ref):
        b = int(S("ref_bonus", "500"))
        balance_add(ref, b, f"Referal: {u.id}")
        x("UPDATE users SET refs=refs+1 WHERE id=?", (ref,))
        await ctx.bot.send_message(ref, f"🎉 Yangi do'st qo'shildi! +{fmt(b)} so'm bonus.")

    bad = check_subs(u.id)
    if bad:
        kb = [[InlineKeyboardButton("📢 " + c["title"], url=c["url"])] for c in bad]
        kb.append([InlineKeyboardButton("✅ Tekshirish", callback_data="chk")])
        await update.message.reply_text(
            "Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
            reply_markup=InlineKeyboardMarkup(kb))
        return

    txt = (f"Salom, {u.first_name}! 👋\n\n"
           f"{S('shop_name')} — o'yin hisobingizni tez va xavfsiz to'ldirish xizmati.\n\n"
           f"Boshlash uchun pastdagi tugmani bosing 👇")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🎮 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None
    await update.message.reply_text(txt, reply_markup=kb)
    if main_kb():
        await update.message.reply_text("Menyu tayyor 👇", reply_markup=main_kb())


async def cb_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    bad = check_subs(qy.from_user.id)
    if bad:
        await qy.answer("Hali obuna bo'lmagansiz ❌", show_alert=True)
        return
    await qy.answer("Rahmat! ✅")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🎮 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None
    await qy.message.reply_text("Tayyor! Ilovani oching 👇", reply_markup=kb)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Chek skrinshoti."""
    if ctx.user_data.get("aw"):
        return await on_text(update, ctx)
    uid = update.effective_user.id
    t = q("SELECT * FROM topups WHERE user_id=? AND status='new' ORDER BY id DESC LIMIT 1",
          (uid,), one=True)
    if not t:
        await update.message.reply_text("Avval ilovadan to'ldirish so'rovini yarating.")
        return
    fid = update.message.photo[-1].file_id
    x("UPDATE topups SET file_id=?, status='pending' WHERE id=?", (fid, t["id"]))
    u = get_user(uid)
    for a in ADMINS:
        await ctx.bot.send_photo(
            a, fid,
            caption=(f"💰 <b>To'ldirish #{t['id']}</b>\n\n"
                     f"👤 {u['name']} (<code>{uid}</code>) @{u['username'] or '-'}\n"
                     f"💵 {fmt(t['amount'])} so'm\n💳 {t['method']}"),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"t:ok:{t['id']}"),
                InlineKeyboardButton("❌ Rad etish", callback_data=f"t:no:{t['id']}")]]))
    await update.message.reply_text("✅ Chek yuborildi. Admin tekshirgach balans yangilanadi.")


async def cb_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    if qy.from_user.id not in ADMINS:
        return await qy.answer("Ruxsat yo'q", show_alert=True)
    _, act, oid = qy.data.split(":")
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not o or o["status"] != "pending":
        return await qy.answer("Allaqachon ishlangan")
    if act == "ok":
        x("UPDATE orders SET status='done' WHERE id=?", (oid,))
        await ctx.bot.send_message(o["user_id"],
                                   f"✅ Buyurtma #{oid} bajarildi!\n{o['title']}")
        await qy.edit_message_text(qy.message.text_html + "\n\n✅ <b>BAJARILDI</b>",
                                   parse_mode="HTML")
    else:
        x("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        balance_add(o["user_id"], o["amount"], f"Buyurtma #{oid} qaytarildi")
        await ctx.bot.send_message(o["user_id"],
                                   f"❌ Buyurtma #{oid} rad etildi.\n"
                                   f"💰 {fmt(o['amount'])} so'm balansga qaytarildi.")
        await qy.edit_message_text(qy.message.text_html + "\n\n❌ <b>RAD ETILDI</b>",
                                   parse_mode="HTML")
    await qy.answer()


async def cb_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    if qy.from_user.id not in ADMINS:
        return await qy.answer("Ruxsat yo'q", show_alert=True)
    _, act, tid = qy.data.split(":")
    t = q("SELECT * FROM topups WHERE id=?", (tid,), one=True)
    if not t or t["status"] != "pending":
        return await qy.answer("Allaqachon ishlangan")
    if act == "ok":
        x("UPDATE topups SET status='done' WHERE id=?", (tid,))
        balance_add(t["user_id"], t["amount"], f"To'ldirish #{tid}")
        nb = get_user(t["user_id"])["balance"]
        await ctx.bot.send_message(t["user_id"],
                                   f"✅ Balans to'ldirildi: +{fmt(t['amount'])} so'm\n"
                                   f"💼 Yangi balans: {fmt(nb)} so'm")
        await qy.edit_message_caption((qy.message.caption or "") + "\n\n✅ TASDIQLANDI")
    else:
        x("UPDATE topups SET status='rejected' WHERE id=?", (tid,))
        await ctx.bot.send_message(t["user_id"],
                                   f"❌ To'ldirish #{tid} rad etildi. Support: @{S('support')}")
        await qy.edit_message_caption((qy.message.caption or "") + "\n\n❌ RAD ETILDI")
    await qy.answer()


# ----------------------------------------------------------------------------
# ADMIN PANEL
# ----------------------------------------------------------------------------
def is_admin(uid):
    return uid in ADMINS


def adm_home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistika", callback_data="a:stats"),
         InlineKeyboardButton("🎮 O'yinlar", callback_data="a:games")],
        [InlineKeyboardButton("💳 Karta", callback_data="a:card"),
         InlineKeyboardButton("📢 Majburiy obuna", callback_data="a:subs")],
        [InlineKeyboardButton("👤 Balans +/-", callback_data="a:bal"),
         InlineKeyboardButton("🎟 Promokod", callback_data="a:promo")],
        [InlineKeyboardButton("🧾 Buyurtmalar", callback_data="a:orders"),
         InlineKeyboardButton("💰 To'ldirishlar", callback_data="a:tops")],
        [InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="a:users"),
         InlineKeyboardButton("📣 Reklama", callback_data="a:bcast")],
        [InlineKeyboardButton("⚙️ Sozlamalar", callback_data="a:settings")],
    ])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ctx.user_data.pop("aw", None)
    await update.message.reply_text("🛠 <b>Admin panel</b>", parse_mode="HTML",
                                    reply_markup=adm_home_kb())


async def edit(qy, text, kb):
    try:
        await qy.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception:
        await qy.message.reply_text(text, parse_mode="HTML", reply_markup=kb,
                                    disable_web_page_preview=True)


async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    qy = update.callback_query
    if not is_admin(qy.from_user.id):
        return await qy.answer("Ruxsat yo'q", show_alert=True)
    await qy.answer()
    p = qy.data.split(":")
    act = p[1]
    back = InlineKeyboardButton("⬅️ Orqaga", callback_data="a:home")

    if act == "home":
        ctx.user_data.pop("aw", None)
        return await edit(qy, "🛠 <b>Admin panel</b>", adm_home_kb())

    if act == "stats":
        us = q("SELECT COUNT(*) c FROM users", one=True)["c"]
        od = q("SELECT COUNT(*) c FROM orders WHERE status='done'", one=True)["c"]
        op = q("SELECT COUNT(*) c FROM orders WHERE status='pending'", one=True)["c"]
        sm = q("SELECT IFNULL(SUM(amount),0) s FROM orders WHERE status='done'", one=True)["s"]
        tp = q("SELECT IFNULL(SUM(amount),0) s FROM topups WHERE status='done'", one=True)["s"]
        bal = q("SELECT IFNULL(SUM(balance),0) s FROM users", one=True)["s"]
        return await edit(qy,
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchi: <b>{us}</b>\n"
            f"🧾 Bajarilgan buyurtma: <b>{od}</b>\n⏳ Kutilmoqda: <b>{op}</b>\n"
            f"💵 Savdo: <b>{fmt(sm)}</b> so'm\n💰 To'ldirilgan: <b>{fmt(tp)}</b> so'm\n"
            f"💼 Umumiy balans: <b>{fmt(bal)}</b> so'm",
            InlineKeyboardMarkup([[back]]))

    # ---------------- O'yinlar ----------------
    if act == "games":
        rows = q("SELECT * FROM games ORDER BY sort,id")
        kb = [[InlineKeyboardButton(f"{'🟢' if g['active'] else '🔴'} {g['title']}",
                                    callback_data=f"a:g:{g['id']}")] for g in rows]
        kb.append([InlineKeyboardButton("➕ O'yin qo'shish", callback_data="a:gadd")])
        kb.append([back])
        return await edit(qy, "🎮 <b>O'yinlar</b>", InlineKeyboardMarkup(kb))

    if act == "gadd":
        ctx.user_data["aw"] = ("gadd",)
        return await edit(qy, "Yangi o'yin ma'lumotini yuboring:\n\n"
                              "<code>Nomi | rasm (URL yoki emoji) | birlik | ID hint</code>\n\n"
                              "Masalan (emoji bilan, tavsiya etiladi — hech qachon buzilmaydi):\n"
                              "<code>Genshin Impact | 💠 | Genesis | UID raqam</code>\n\n"
                              "Yoki URL bilan:\n"
                              "<code>Genshin Impact | https://site.com/img.png | Genesis | UID raqam</code>",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data="a:games")]]))

    if act == "g":
        gid = int(p[2])
        g = q("SELECT * FROM games WHERE id=?", (gid,), one=True)
        n = q("SELECT COUNT(*) c FROM packages WHERE game_id=?", (gid,), one=True)["c"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"📦 Paketlar ({n})", callback_data=f"a:pkgs:{gid}")],
            [InlineKeyboardButton("✏️ Nomi", callback_data=f"a:gset:title:{gid}"),
             InlineKeyboardButton("🖼 Rasm", callback_data=f"a:gset:image:{gid}")],
            [InlineKeyboardButton("💠 Birlik", callback_data=f"a:gset:unit:{gid}"),
             InlineKeyboardButton("🆔 Hint", callback_data=f"a:gset:hint:{gid}")],
            [InlineKeyboardButton("🔴 O'chirish" if g["active"] else "🟢 Yoqish",
                                  callback_data=f"a:gtog:{gid}"),
             InlineKeyboardButton("🗑 Butunlay o'chirish", callback_data=f"a:gdel:{gid}")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data="a:games")]])
        return await edit(qy, f"🎮 <b>{g['title']}</b>\n\nBirlik: {g['unit']}\n"
                              f"Hint: {g['hint']}\nHolat: {'faol' if g['active'] else 'o‘chirilgan'}", kb)

    if act == "gtog":
        gid = int(p[2])
        x("UPDATE games SET active=1-active WHERE id=?", (gid,))
        qy.data = f"a:g:{gid}"
        return await cb_admin(update, ctx)

    if act == "gdel":
        gid = int(p[2])
        x("DELETE FROM packages WHERE game_id=?", (gid,))
        x("DELETE FROM games WHERE id=?", (gid,))
        qy.data = "a:games"
        return await cb_admin(update, ctx)

    if act == "gset":
        ctx.user_data["aw"] = ("gset", p[2], int(p[3]))
        return await edit(qy, f"Yangi qiymatni yuboring (<code>{p[2]}</code>):",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data=f"a:g:{p[3]}")]]))

    if act == "pkgs":
        gid = int(p[2])
        rows = q("SELECT * FROM packages WHERE game_id=? ORDER BY sort,id", (gid,))
        kb = [[InlineKeyboardButton(
            f"{'🟢' if r['active'] else '🔴'} {r['title']} — {fmt(r['price'])}",
            callback_data=f"a:p:{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton("➕ Paket qo'shish", callback_data=f"a:padd:{gid}")])
        kb.append([InlineKeyboardButton("⬅️ Orqaga", callback_data=f"a:g:{gid}")])
        return await edit(qy, "📦 <b>Paketlar</b>", InlineKeyboardMarkup(kb))

    if act == "padd":
        ctx.user_data["aw"] = ("padd", int(p[2]))
        return await edit(qy, "Paketni yuboring:\n\n<code>Nomi | narx | eski_narx</code>\n\n"
                              "Masalan: <code>100 + 10 Diamonds | 9990 | 12000</code>",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data=f"a:pkgs:{p[2]}")]]))

    if act == "p":
        pid = int(p[2])
        r = q("SELECT * FROM packages WHERE id=?", (pid,), one=True)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💵 Narx", callback_data=f"a:pset:price:{pid}"),
             InlineKeyboardButton("🏷 Eski narx", callback_data=f"a:pset:old_price:{pid}")],
            [InlineKeyboardButton("✏️ Nomi", callback_data=f"a:pset:title:{pid}"),
             InlineKeyboardButton("🔴/🟢 Holat", callback_data=f"a:ptog:{pid}")],
            [InlineKeyboardButton("🗑 O'chirish", callback_data=f"a:pdel:{pid}")],
            [InlineKeyboardButton("⬅️ Orqaga", callback_data=f"a:pkgs:{r['game_id']}")]])
        return await edit(qy, f"📦 <b>{r['title']}</b>\n\n💵 Narx: <b>{fmt(r['price'])}</b> so'm\n"
                              f"🏷 Eski: {fmt(r['old_price'])} so'm\n"
                              f"Holat: {'faol' if r['active'] else 'o‘chirilgan'}", kb)

    if act == "pset":
        ctx.user_data["aw"] = ("pset", p[2], int(p[3]))
        return await edit(qy, f"Yangi qiymat (<code>{p[2]}</code>):",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data=f"a:p:{p[3]}")]]))

    if act == "ptog":
        x("UPDATE packages SET active=1-active WHERE id=?", (int(p[2]),))
        qy.data = f"a:p:{p[2]}"
        return await cb_admin(update, ctx)

    if act == "pdel":
        r = q("SELECT game_id FROM packages WHERE id=?", (int(p[2]),), one=True)
        x("DELETE FROM packages WHERE id=?", (int(p[2]),))
        qy.data = f"a:pkgs:{r['game_id']}"
        return await cb_admin(update, ctx)

    # ---------------- Karta ----------------
    if act == "card":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 UZCARD raqam", callback_data="a:set:card_number"),
             InlineKeyboardButton("👤 Egasi", callback_data="a:set:card_holder")],
            [InlineKeyboardButton("💳 HUMO raqam", callback_data="a:set:card2_number"),
             InlineKeyboardButton("👤 Egasi", callback_data="a:set:card2_holder")],
            [back]])
        return await edit(qy,
            f"💳 <b>To'lov kartalari</b>\n\n"
            f"UZCARD: <code>{S('card_number')}</code>\n{S('card_holder')}\n\n"
            f"HUMO: <code>{S('card2_number')}</code>\n{S('card2_holder')}", kb)

    # ---------------- Majburiy obuna ----------------
    if act == "subs":
        rows = q("SELECT * FROM channels")
        kb = [[InlineKeyboardButton(f"🗑 {c['title']}", callback_data=f"a:subdel:{c['id']}")]
              for c in rows]
        kb.append([InlineKeyboardButton("➕ Kanal qo'shish", callback_data="a:subadd")])
        kb.append([back])
        txt = "📢 <b>Majburiy obuna</b>\n\n" + (
            "\n".join(f"• {c['title']} — <code>{c['chat_id']}</code>" for c in rows)
            or "Kanal qo'shilmagan.")
        return await edit(qy, txt, InlineKeyboardMarkup(kb))

    if act == "subadd":
        ctx.user_data["aw"] = ("subadd",)
        return await edit(qy, "Kanalni yuboring:\n\n<code>@kanal | Nomi | https://t.me/kanal</code>\n\n"
                              "⚠️ Bot kanalda admin bo'lishi shart.",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data="a:subs")]]))

    if act == "subdel":
        x("DELETE FROM channels WHERE id=?", (int(p[2]),))
        qy.data = "a:subs"
        return await cb_admin(update, ctx)

    # ---------------- Balans ----------------
    if act == "bal":
        ctx.user_data["aw"] = ("bal",)
        return await edit(qy, "Balans o'zgartirish:\n\n<code>user_id | summa</code>\n\n"
                              "Qo'shish: <code>7849637859 | 50000</code>\n"
                              "Ayirish: <code>7849637859 | -50000</code>",
                          InlineKeyboardMarkup([[back]]))

    # ---------------- Promokod ----------------
    if act == "promo":
        rows = q("SELECT * FROM promos")
        kb = [[InlineKeyboardButton(f"🗑 {r['code']} ({fmt(r['amount'])}) {r['used']}/{r['max_uses']}",
                                    callback_data=f"a:promodel:{r['code']}")] for r in rows]
        kb.append([InlineKeyboardButton("➕ Promokod qo'shish", callback_data="a:promoadd")])
        kb.append([back])
        return await edit(qy, "🎟 <b>Promokodlar</b>", InlineKeyboardMarkup(kb))

    if act == "promoadd":
        ctx.user_data["aw"] = ("promoadd",)
        return await edit(qy, "Promokod:\n\n<code>KOD | summa | nechta_marta</code>\n\n"
                              "Masalan: <code>DANAT10 | 10000 | 100</code>",
                          InlineKeyboardMarkup([[InlineKeyboardButton("⬅️", callback_data="a:promo")]]))

    if act == "promodel":
        x("DELETE FROM promos WHERE code=?", (p[2],))
        qy.data = "a:promo"
        return await cb_admin(update, ctx)

    # ---------------- Buyurtma / to'ldirish ro'yxati ----------------
    if act == "orders":
        rows = q("SELECT * FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 15")
        if not rows:
            return await edit(qy, "🧾 Kutilayotgan buyurtma yo'q.", InlineKeyboardMarkup([[back]]))
        kb = [[InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"o:ok:{r['id']}"),
               InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"o:no:{r['id']}")] for r in rows]
        kb.append([back])
        txt = "🧾 <b>Kutilayotgan buyurtmalar</b>\n\n" + "\n".join(
            f"#{r['id']} — {r['title']} | <code>{r['player_id']}</code> | {fmt(r['amount'])}"
            for r in rows)
        return await edit(qy, txt, InlineKeyboardMarkup(kb))

    if act == "tops":
        rows = q("SELECT * FROM topups WHERE status='pending' ORDER BY id DESC LIMIT 15")
        if not rows:
            return await edit(qy, "💰 Kutilayotgan to'ldirish yo'q.", InlineKeyboardMarkup([[back]]))
        kb = [[InlineKeyboardButton(f"✅ #{r['id']}", callback_data=f"t:ok:{r['id']}"),
               InlineKeyboardButton(f"❌ #{r['id']}", callback_data=f"t:no:{r['id']}")] for r in rows]
        kb.append([back])
        txt = "💰 <b>Kutilayotgan to'ldirishlar</b>\n\n" + "\n".join(
            f"#{r['id']} — <code>{r['user_id']}</code> | {fmt(r['amount'])} so'm" for r in rows)
        return await edit(qy, txt, InlineKeyboardMarkup(kb))

    # ---------------- Foydalanuvchilar ----------------
    if act == "users":
        ctx.user_data["aw"] = ("finduser",)
        n = q("SELECT COUNT(*) c FROM users", one=True)["c"]
        return await edit(qy, f"👥 Jami: <b>{n}</b>\n\nQidirish uchun user_id yoki @username yuboring.",
                          InlineKeyboardMarkup([[back]]))

    if act == "ban":
        x("UPDATE users SET banned=1-banned WHERE id=?", (int(p[2]),))
        u2 = get_user(int(p[2]))
        return await edit(qy, f"👤 {u2['name']} — {'🚫 bloklandi' if u2['banned'] else '✅ blokdan chiqarildi'}",
                          InlineKeyboardMarkup([[back]]))

    # ---------------- Reklama ----------------
    if act == "bcast":
        ctx.user_data["aw"] = ("bcast",)
        return await edit(qy, "📣 Reklama matnini yuboring (yoki rasm + izoh).",
                          InlineKeyboardMarkup([[back]]))

    # ---------------- Sozlamalar ----------------
    if act == "settings":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🏪 Do'kon nomi", callback_data="a:set:shop_name")],
            [InlineKeyboardButton("💵 Min. to'ldirish", callback_data="a:set:min_topup"),
             InlineKeyboardButton("🎁 Referal bonus", callback_data="a:set:ref_bonus")],
            [InlineKeyboardButton("🎧 Support username", callback_data="a:set:support")],
            [InlineKeyboardButton("📰 Banner matni", callback_data="a:set:banner")],
            [back]])
        return await edit(qy,
            f"⚙️ <b>Sozlamalar</b>\n\n🏪 {S('shop_name')}\n"
            f"💵 Min: {fmt(S('min_topup','1000'))} so'm\n"
            f"🎁 Referal: {fmt(S('ref_bonus','500'))} so'm\n"
            f"🎧 @{S('support')}\n📰 {S('banner')}", kb)

    if act == "set":
        ctx.user_data["aw"] = ("set", p[2])
        return await edit(qy, f"Yangi qiymat (<code>{p[2]}</code>):",
                          InlineKeyboardMarkup([[back]]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    aw = ctx.user_data.get("aw")
    if not aw or not is_admin(uid):
        return
    t = (update.message.text or update.message.caption or "").strip()
    kind = aw[0]
    ok = lambda m: update.message.reply_text(m, parse_mode="HTML", reply_markup=adm_home_kb())

    try:
        if kind == "set":
            setS(aw[1], t)
            ctx.user_data.pop("aw")
            return await ok(f"✅ <code>{aw[1]}</code> yangilandi.")

        if kind == "gset":
            x(f"UPDATE games SET {aw[1]}=? WHERE id=?", (t, aw[2]))
            ctx.user_data.pop("aw")
            return await ok("✅ O'yin yangilandi.")

        if kind == "pset":
            v = int(t) if aw[1] in ("price", "old_price") else t
            x(f"UPDATE packages SET {aw[1]}=? WHERE id=?", (v, aw[2]))
            ctx.user_data.pop("aw")
            return await ok("✅ Paket yangilandi.")

        if kind == "gadd":
            a = [s.strip() for s in t.split("|")]
            x("INSERT INTO games(title,image,unit,hint,active,sort) VALUES(?,?,?,?,1,99)",
              (a[0], a[1], a[2] if len(a) > 2 else "Olmoslar",
               a[3] if len(a) > 3 else "O'yin ID"))
            ctx.user_data.pop("aw")
            return await ok("✅ O'yin qo'shildi.")

        if kind == "padd":
            a = [s.strip() for s in t.split("|")]
            x("INSERT INTO packages(game_id,title,price,old_price,active,sort) VALUES(?,?,?,?,1,99)",
              (aw[1], a[0], int(a[1]), int(a[2]) if len(a) > 2 else 0))
            ctx.user_data.pop("aw")
            return await ok("✅ Paket qo'shildi.")

        if kind == "subadd":
            a = [s.strip() for s in t.split("|")]
            x("INSERT INTO channels(chat_id,title,url) VALUES(?,?,?)",
              (a[0], a[1] if len(a) > 1 else a[0],
               a[2] if len(a) > 2 else "https://t.me/" + a[0].lstrip("@")))
            ctx.user_data.pop("aw")
            return await ok("✅ Kanal qo'shildi.")

        if kind == "promoadd":
            a = [s.strip() for s in t.split("|")]
            x("INSERT OR REPLACE INTO promos(code,amount,max_uses,used,active) VALUES(?,?,?,0,1)",
              (a[0].upper(), int(a[1]), int(a[2]) if len(a) > 2 else 1))
            ctx.user_data.pop("aw")
            return await ok("✅ Promokod qo'shildi.")

        if kind == "bal":
            a = [s.strip() for s in t.split("|")]
            tid, amt = int(a[0]), int(a[1])
            if not get_user(tid):
                return await update.message.reply_text("❌ Foydalanuvchi topilmadi.")
            balance_add(tid, amt, "Admin")
            nb = get_user(tid)["balance"]
            await ctx.bot.send_message(tid,
                f"{'➕' if amt > 0 else '➖'} Balansingiz o'zgardi: {fmt(amt)} so'm\n"
                f"💼 Yangi balans: {fmt(nb)} so'm")
            ctx.user_data.pop("aw")
            return await ok(f"✅ Bajarildi. Yangi balans: <b>{fmt(nb)}</b> so'm")

        if kind == "finduser":
            if t.startswith("@"):
                u2 = q("SELECT * FROM users WHERE username=?", (t[1:],), one=True)
            else:
                u2 = get_user(int(t))
            if not u2:
                return await update.message.reply_text("❌ Topilmadi.")
            ctx.user_data.pop("aw")
            return await update.message.reply_text(
                f"👤 <b>{u2['name']}</b>\nID: <code>{u2['id']}</code>\n"
                f"@{u2['username'] or '-'}\n💼 Balans: <b>{fmt(u2['balance'])}</b> so'm\n"
                f"👥 Referal: {u2['refs']}\nHolat: {'🚫 bloklangan' if u2['banned'] else '✅ faol'}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚫 Ban / Unban", callback_data=f"a:ban:{u2['id']}")],
                    [InlineKeyboardButton("⬅️ Admin panel", callback_data="a:home")]]))

        if kind == "bcast":
            ctx.user_data.pop("aw")
            ids = [r["id"] for r in q("SELECT id FROM users WHERE banned=0")]
            await update.message.reply_text(f"📣 Yuborilmoqda... ({len(ids)})")
            sent = 0
            photo = update.message.photo[-1].file_id if update.message.photo else None
            for i in ids:
                try:
                    if photo:
                        await ctx.bot.send_photo(i, photo, caption=t, parse_mode="HTML")
                    else:
                        await ctx.bot.send_message(i, t, parse_mode="HTML")
                    sent += 1
                except Exception:
                    pass
                if sent % 25 == 0:
                    time.sleep(1)
            return await ok(f"✅ Yuborildi: <b>{sent}</b> / {len(ids)}")

    except Exception as e:
        return await update.message.reply_text(f"❌ Xato: {e}\nFormatni tekshiring.")


# ----------------------------------------------------------------------------
# MINI APP (HTML)
# ----------------------------------------------------------------------------
HTML = r"""<!doctype html>
<html lang="uz"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>DANAT SHOP</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#f2f5f9; --card:#fff; --ink:#0d1b2a; --mut:#6b7a90; --line:#e3e9f1;
  --brand:#1b6ef3; --brand2:#0b4fc4; --ok:#12a150; --warn:#e4a11b; --bad:#e5484d;
  --r:18px; --sh:0 6px 20px rgba(13,27,42,.07);
}
body.dark{--bg:#0e1116;--card:#161b23;--ink:#e8edf4;--mut:#8b98ab;--line:#232a35;--sh:none}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding-bottom:96px}
.wrap{max-width:560px;margin:0 auto;padding:14px}
.top{display:flex;align-items:center;gap:10px;padding:12px 14px;position:sticky;top:0;z-index:20;background:var(--bg)}
.av{width:46px;height:46px;border-radius:50%;object-fit:cover;background:var(--line)}
.hi{font-size:12px;color:var(--mut)}
.nm{font-weight:800;font-size:17px;text-transform:uppercase}
.chip{margin-left:auto;display:flex;gap:6px}
.chip button{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:8px 12px;font-size:13px;color:var(--ink);font-weight:600;cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--sh)}
.bal{display:flex;align-items:center;gap:12px;padding:16px}
.bal .ico{width:44px;height:44px;border-radius:14px;background:var(--bg);display:grid;place-items:center;font-size:20px}
.bal b{font-size:24px;display:block;color:var(--brand)}
.bal small{color:var(--mut);font-size:11px;letter-spacing:.06em}
.btn{background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;border:0;border-radius:999px;padding:12px 18px;font-weight:700;font-size:15px;cursor:pointer}
.btn.wide{width:100%;border-radius:16px;padding:16px}
.btn.gray{background:var(--bg);color:var(--ink);border:1px solid var(--line)}
.btn.green{background:var(--ok)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.row2 .card{padding:14px;text-align:center;font-weight:700;font-size:14px;cursor:pointer}
.banner{margin-top:12px;border-radius:var(--r);overflow:hidden;background:linear-gradient(120deg,#2b1055,#7b2ff7);color:#fff;padding:22px 18px}
.banner h3{margin:0 0 6px;font-size:22px;line-height:1.1;font-weight:900}
.banner p{margin:0;opacity:.8;font-size:13px}
.sec{display:flex;align-items:center;margin:20px 0 10px}
.sec h4{margin:0;font-size:18px;font-weight:800}
.sec a{margin-left:auto;color:var(--brand);font-size:14px;font-weight:600;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:400px){.grid{grid-template-columns:repeat(3,1fr)}}
.g{cursor:pointer}
.g img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:16px;background:var(--line)}
.g span{display:block;font-size:11px;margin-top:6px;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--mut)}
.pk{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.pkg{padding:12px;position:relative;cursor:pointer;border-radius:16px}
.pkg.sel{border-color:var(--brand);box-shadow:0 0 0 2px var(--brand) inset}
.pkg .ph{height:74px;border-radius:12px;background:var(--bg);display:grid;place-items:center;font-size:28px;margin-bottom:8px}
.pkg .t{font-size:13px;font-weight:700}
.pkg .p{color:var(--ok);font-weight:800;font-size:15px;margin-top:2px}
.pkg .o{color:var(--mut);font-size:11px;text-decoration:line-through}
.badge{position:absolute;top:8px;right:8px;background:var(--brand);color:#fff;font-size:11px;font-weight:700;padding:3px 8px;border-radius:999px}
.inp{width:100%;padding:14px;border-radius:14px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:15px;outline:none}
.inp:focus{border-color:var(--brand)}
.hero{position:relative;height:190px;border-radius:var(--r);overflow:hidden;margin-bottom:12px}
.hero img{width:100%;height:100%;object-fit:cover;filter:saturate(1.1)}
.hero h2{position:absolute;left:16px;bottom:14px;margin:0;color:#fff;font-size:26px;font-weight:900;text-shadow:0 2px 12px rgba(0,0,0,.6)}
.back{position:absolute;left:12px;top:12px;width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,.45);color:#fff;border:0;font-size:18px;cursor:pointer}
.steps{padding:16px}
.steps div{display:flex;gap:10px;align-items:flex-start;margin-bottom:10px;font-size:14px;color:var(--brand2)}
.steps i{width:24px;height:24px;border-radius:50%;background:var(--bg);color:var(--mut);display:grid;place-items:center;font-style:normal;font-size:12px;flex:0 0 auto}
.pay{padding:18px;text-align:center}
.pay .amt{font-size:32px;font-weight:900;color:var(--brand)}
.pay .card-no{font-size:22px;font-weight:800;letter-spacing:.06em;margin:6px 0}
.list .it{padding:14px;display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--line)}
.list .it:last-child{border:0}
.st{font-size:11px;padding:3px 9px;border-radius:999px;font-weight:700;margin-left:auto;white-space:nowrap}
.st.pending{background:#fff4d6;color:#8a6100}
.st.done{background:#dcf5e7;color:#0b6b36}
.st.rejected{background:#fde0e0;color:#a11318}
.empty{text-align:center;padding:60px 20px;color:var(--mut)}
.empty div{font-size:44px;margin-bottom:10px}
nav{position:fixed;left:0;right:0;bottom:0;display:flex;justify-content:space-around;background:var(--card);border-top:1px solid var(--line);padding:8px 6px 14px;z-index:30}
nav button{background:0;border:0;color:var(--mut);font-size:11px;display:grid;justify-items:center;gap:3px;cursor:pointer;padding:6px 10px;border-radius:14px}
nav button span{font-size:19px}
nav button.on{color:#fff;background:var(--brand)}
.mask{position:fixed;inset:0;background:rgba(6,10,16,.75);display:grid;place-items:center;z-index:99;padding:20px}
.mask .card{padding:22px;max-width:380px;width:100%}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:110px;background:#0d1b2a;color:#fff;padding:12px 18px;border-radius:14px;z-index:200;font-size:14px;opacity:0;transition:.25s}
.toast.on{opacity:1}
.menu{position:absolute;right:14px;top:60px;background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;z-index:50}
.menu button{display:block;width:100%;text-align:left;padding:12px 20px;border:0;background:0;color:var(--ink);font-size:15px;cursor:pointer}
</style></head><body>

<div class="top">
  <img id="av" class="av" src="">
  <div><div class="hi">Salom 👋</div><div class="nm" id="uname">—</div></div>
  <div class="chip">
    <button id="langBtn">🌐 UZ</button>
    <button>UZS</button>
    <button id="thBtn">🌙</button>
  </div>
</div>
<div id="langMenu" class="menu" style="display:none">
  <button data-l="uz">🇺🇿 O'zbekcha</button>
  <button data-l="ru">🇷🇺 Русский</button>
  <button data-l="en">🇬🇧 English</button>
  <button data-l="kk">🇰🇿 Қазақша</button>
  <button data-l="ky">🇰🇬 Кыргызча</button>
</div>

<div class="wrap" id="view"></div>

<nav>
  <button data-t="home" class="on"><span>🏠</span>Asosiy</button>
  <button data-t="games"><span>🎮</span>O'yinlar</button>
  <button data-t="topup"><span>💳</span>To'ldirish</button>
  <button data-t="orders"><span>🕘</span>Buyurtmalar</button>
  <button data-t="profile"><span>👤</span>Profil</button>
</nav>
<div class="toast" id="toast"></div>

<script>
const TG = window.Telegram.WebApp; TG.expand(); TG.ready();
const INIT = TG.initData || "";
let ST = {user:null, shop:null, games:[], tab:"home", game:null, pkg:null, lang:"uz",
          orders:[], topups:[], pay:null};
const V = document.getElementById('view');

const T = {
 uz:{bal:"BALANS",topup:"To'ldirish",promo:"Promokodlar",help:"Yordam",pop:"Ommabop o'yinlar",
     all:"Barchasi",allg:"Barcha o'yinlar",search:"O'yin yoki xizmat qidirish",choose:"Paketni tanlang",
     buy:"Sotib olish",idph:"O'yin ID raqamingiz",srv:"Server ID",bt:"Balansni to'ldirish",
     pm:"To'lov usuli",min:"Eng kam",cont:"Davom etish",wait:"To'lov kutilmoqda",
     copy:"Nusxa olish",orders:"Buyurtmalar",tx:"Tranzaksiyalar",noord:"Buyurtmalar yo'q",
     noord2:"Birinchi buyurtmangiz shu yerda ko'rinadi",out:"Chiqish",ref:"Do'st taklif qiling — bonus oling",
     share:"Ulashish",invited:"TAKLIF QILINGAN",top:"Top donaterlar",sum:"Summani kiriting",
     steps:["To'lov usuli va summani tanlang","Ko'rsatilgan raqamga AYNAN shu summani o'tkazing",
            "Skrinshotni botga yuboring","Admin tekshirgach balansingiz yangilanadi"]},
 ru:{bal:"БАЛАНС",topup:"Пополнить",promo:"Промокоды",help:"Помощь",pop:"Популярные игры",
     all:"Все",allg:"Все игры",search:"Поиск игры или услуги",choose:"Выберите пакет",
     buy:"Купить",idph:"Ваш игровой ID",srv:"ID сервера",bt:"Пополнение баланса",
     pm:"Способ оплаты",min:"Минимум",cont:"Продолжить",wait:"Ожидание оплаты",
     copy:"Копировать",orders:"Заказы",tx:"Транзакции",noord:"Заказов нет",
     noord2:"Ваш первый заказ появится здесь",out:"Выйти",ref:"Пригласи друга — получи бонус",
     share:"Поделиться",invited:"ПРИГЛАШЕНО",top:"Топ донатеры",sum:"Введите сумму",
     steps:["Выберите способ и сумму","Переведите ТОЧНО эту сумму","Отправьте скриншот боту",
            "После проверки баланс обновится"]},
 en:{bal:"BALANCE",topup:"Top up",promo:"Promo codes",help:"Support",pop:"Popular games",
     all:"All",allg:"All games",search:"Search a game or service",choose:"Choose a package",
     buy:"Buy",idph:"Your game ID",srv:"Server ID",bt:"Top up balance",
     pm:"Payment method",min:"Minimum",cont:"Continue",wait:"Waiting for payment",
     copy:"Copy",orders:"Orders",tx:"Transactions",noord:"No orders yet",
     noord2:"Your first order will appear here",out:"Log out",ref:"Invite a friend — get a bonus",
     share:"Share",invited:"INVITED",top:"Top donators",sum:"Enter amount",
     steps:["Pick a method and amount","Transfer the EXACT amount","Send the receipt to the bot",
            "Balance updates after review"]}
};
T.kk = T.ru; T.ky = T.ru;
const t = k => (T[ST.lang]||T.uz)[k];
const money = n => (n||0).toLocaleString('ru-RU').replace(/,/g,' ');

function toast(m){const e=document.getElementById('toast');e.textContent=m;e.classList.add('on');
  setTimeout(()=>e.classList.remove('on'),2200);}
async function api(p,b={}){
  const r = await fetch('/api/'+p,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({initData:INIT},b))});
  return r.json();
}
function copy(s){navigator.clipboard.writeText(s); toast('Nusxalandi ✅');}

async function boot(){
  const d = await api('init');
  if(!d.ok){V.innerHTML='<div class="empty"><div>🔒</div>Iltimos, botni Telegram orqali oching.</div>';return;}
  ST.user=d.user; ST.shop=d.shop; ST.games=d.games; ST.lang=d.user.lang||'uz';
  document.getElementById('uname').textContent=d.user.name;
  const pu = TG.initDataUnsafe?.user?.photo_url;
  if(pu) document.getElementById('av').src = pu;
  else document.getElementById('av').outerHTML =
    `<div id="av" class="av" style="display:grid;place-items:center;font-size:20px;background:linear-gradient(135deg,${grad(ST.user.name||'U')})">${(ST.user.name||'U')[0].toUpperCase()}</div>`;
  document.getElementById('langBtn').textContent='🌐 '+ST.lang.toUpperCase();
  if(d.subs && d.subs.length) return subsWall(d.subs);
  render();
}
function subsWall(list){
  document.body.insertAdjacentHTML('beforeend',
   `<div class="mask"><div class="card" style="padding:22px">
     <h3 style="margin:0 0 6px">📢 Obuna bo'ling</h3>
     <p style="color:var(--mut);font-size:14px;margin:0 0 14px">Davom etish uchun kanallarga a'zo bo'ling.</p>
     ${list.map(c=>`<a href="${c.url}" target="_blank" class="btn wide" style="display:block;text-align:center;text-decoration:none;margin-bottom:8px">${c.title}</a>`).join('')}
     <button class="btn wide green" onclick="location.reload()">✅ Tekshirish</button>
   </div></div>`);
}

/* ---------- pages ---------- */
function render(){
  const p = {home:home, games:games, game:gamePage, topup:topup, pay:payPage,
             orders:orders, profile:profile}[ST.tab] || home;
  V.innerHTML = p();
  document.querySelectorAll('nav button').forEach(b=>
    b.classList.toggle('on', b.dataset.t===ST.tab || (ST.tab==='game'&&b.dataset.t==='games')
      || (ST.tab==='pay'&&b.dataset.t==='topup')));
  window.scrollTo(0,0);
}
const GRADS = ['#ff5e62,#ff9966','#00c6ff,#0072ff','#f7971e,#ffd200','#8e2de2,#4a00e0',
               '#11998e,#38ef7d','#ee0979,#ff6a00','#396afc,#2948ff','#fc4a1a,#f7b733'];
function grad(seed){let h=0;for(let i=0;i<seed.length;i++)h=(h*31+seed.charCodeAt(i))>>>0;
  return GRADS[h%GRADS.length];}
function thumb(g,sz){
  sz = sz || '100%';
  if(g.image && g.image.startsWith('http'))
    return `<img src="${g.image}" style="width:${sz};aspect-ratio:1;object-fit:cover;border-radius:16px" onerror="this.outerHTML=thumbFallback('${g.title.replace(/'/g,"\\'")}','${g.image}')">`;
  return `<div style="width:${sz};aspect-ratio:1;border-radius:16px;display:grid;place-items:center;font-size:34px;background:linear-gradient(135deg,${grad(g.title)})">${g.image||'🎮'}</div>`;
}
function thumbFallback(title,seedv){
  return `<div style="width:100%;aspect-ratio:1;border-radius:16px;display:grid;place-items:center;font-size:34px;background:linear-gradient(135deg,${grad(seedv)})">🎮</div>`;
}
function gcard(g){return `<div class="g" onclick="openGame(${g.id})">
  ${thumb(g)}
  <span>${g.title}</span></div>`;}

function home(){
  return `
  <div class="card bal">
    <div class="ico">👛</div>
    <div><small>${t('bal')}</small><b>${money(ST.user.balance)} <span style="font-size:14px;color:var(--mut)">so'm</span></b></div>
    <button class="btn" style="margin-left:auto" onclick="go('topup')">+ ${t('topup')}</button>
  </div>
  <div class="row2">
    <div class="card" onclick="go('profile')">🎟 ${t('promo')}</div>
    <div class="card" onclick="TG.openTelegramLink('https://t.me/${ST.shop.support}')">🎧 ${t('help')}</div>
  </div>
  <div class="banner">
    <h3>${ST.shop.name}</h3>
    <p>${ST.shop.banner}</p>
    <div style="display:flex;gap:14px;margin-top:14px;font-size:11px;opacity:.85;flex-wrap:wrap">
      <span>⚡ Tezkor</span><span>🛡 100% xavfsiz</span><span>✨ Eng yaxshi narx</span><span>🕐 24/7</span></div>
  </div>
  <div class="sec"><h4>${t('pop')}</h4><a onclick="go('games')">${t('all')}</a></div>
  <div class="grid">${ST.games.slice(0,8).map(gcard).join('')}</div>`;
}

function games(){
  return `<div class="sec"><h4>${t('allg')}</h4></div>
  <input class="inp" id="sq" placeholder="🔍 ${t('search')}" oninput="filter()">
  <div class="grid" id="ggrid" style="margin-top:14px">${ST.games.map(gcard).join('')}</div>`;
}
function filter(){
  const v=document.getElementById('sq').value.toLowerCase();
  document.getElementById('ggrid').innerHTML =
    ST.games.filter(g=>g.title.toLowerCase().includes(v)).map(gcard).join('');
}

async function openGame(id){
  const d = await api('game',{id});
  if(!d.ok) return toast('Xato');
  ST.game=d.game; ST.packages=d.packages; ST.pkg=null; ST.tab='game'; render();
}
function gamePage(){
  const g=ST.game;
  const heroBg = (g.image && g.image.startsWith('http'))
    ? `<img src="${g.image}" style="width:100%;height:100%;object-fit:cover">`
    : `<div style="width:100%;height:100%;display:grid;place-items:center;font-size:80px;background:linear-gradient(135deg,${grad(g.title)})">${g.image||'🎮'}</div>`;
  return `
  <div class="hero"><button class="back" onclick="go('games')">‹</button>
    ${heroBg}<h2>${g.title}</h2></div>
  <div class="sec"><h4>${t('choose')}</h4></div>
  <div class="pk">${ST.packages.map(p=>{
    const d = p.old_price>p.price ? Math.round((1-p.price/p.old_price)*100) : 0;
    return `<div class="card pkg ${ST.pkg===p.id?'sel':''}" onclick="pick(${p.id})">
      ${d?`<div class="badge">-${d}%</div>`:''}
      <div class="ph">${g.unit==='UC'?'🪙':g.unit==='Stars'?'⭐':g.unit==='Gold'?'🥇':'💎'}</div>
      <div class="t">${p.title}</div><div class="p">${money(p.price)} <span style="font-size:11px;color:var(--mut)">so'm</span></div>
      ${p.old_price?`<div class="o">${money(p.old_price)} so'm</div>`:''}</div>`;}).join('')}
  </div>
  <div style="margin-top:16px">
    <input class="inp" id="pid" placeholder="${g.hint||t('idph')}">
    ${g.need_server?`<input class="inp" id="sid" placeholder="${t('srv')}" style="margin-top:10px">`:''}
    <button class="btn wide green" style="margin-top:12px" onclick="buy()">${t('buy')}</button>
    <p style="color:var(--mut);font-size:12px;text-align:center">Balans: ${money(ST.user.balance)} so'm</p>
  </div>`;
}
function pick(id){ST.pkg=id;render();}

async function buy(){
  if(!ST.pkg) return toast('Paketni tanlang');
  const pid=(document.getElementById('pid').value||'').trim();
  const sid=(document.getElementById('sid')?.value||'').trim();
  if(pid.length<3) return toast("O'yin ID kiriting");
  const d = await api('order',{pkg_id:ST.pkg,player_id:pid,server_id:sid});
  if(!d.ok) return toast(d.error==="subs"?"Kanalga obuna bo'ling":d.error);
  ST.user.balance=d.balance; TG.HapticFeedback?.notificationOccurred('success');
  toast('✅ Buyurtma #'+d.id+' qabul qilindi');
  go('orders');
}

function topup(){
  const m=ST.shop.min_topup;
  return `
  <div class="sec"><h4>${t('bt')}</h4></div>
  <div class="card bal"><div class="ico">👛</div>
    <div><small>${t('bal')}</small><b>${money(ST.user.balance)} <span style="font-size:14px;color:var(--mut)">so'm</span></b></div></div>
  <div class="sec"><h4 style="font-size:15px">${t('pm')}</h4></div>
  <div class="row2">
    <div class="card" id="mUZCARD" onclick="setM('UZCARD')" style="border-color:var(--brand)">💳 UZCARD</div>
    <div class="card" id="mHUMO" onclick="setM('HUMO')">🟠 HUMO</div>
  </div>
  <div class="sec"><h4 style="font-size:15px">${t('sum')}</h4></div>
  <input class="inp" id="amt" type="number" inputmode="numeric" placeholder="${money(m)}">
  <div class="row2" style="grid-template-columns:repeat(4,1fr);margin-top:10px">
    ${[50000,100000,200000,500000].map(v=>`<div class="card" style="padding:10px;font-size:13px" onclick="document.getElementById('amt').value=${v}">${v/1000}k</div>`).join('')}
  </div>
  <p style="color:var(--mut);font-size:12px">${t('min')}: ${money(m)} so'm</p>
  <div class="card steps" style="margin-top:8px">
    ${t('steps').map((s,i)=>`<div><i>${i+1}</i><span>${s}</span></div>`).join('')}
  </div>
  <button class="btn wide green" style="margin-top:14px" onclick="doTopup()">${t('cont')}</button>`;
}
let METHOD='UZCARD';
function setM(m){METHOD=m;
  document.getElementById('mUZCARD').style.borderColor = m==='UZCARD'?'var(--brand)':'var(--line)';
  document.getElementById('mHUMO').style.borderColor  = m==='HUMO'?'var(--brand)':'var(--line)';}

async function doTopup(){
  const a=parseInt(document.getElementById('amt').value||0);
  const d=await api('topup',{amount:a,method:METHOD});
  if(!d.ok) return toast(d.error);
  ST.pay={id:d.id,amount:a,card:d.card,holder:d.holder,method:d.method};
  ST.tab='pay'; render();
}
function payPage(){
  const p=ST.pay;
  return `
  <div class="sec"><h4>${t('wait')}</h4></div>
  <div class="card pay">
    <small style="color:var(--mut);letter-spacing:.08em">AYNAN</small>
    <div class="amt">${money(p.amount)} <span style="font-size:15px;color:var(--mut)">so'm</span></div>
    <button class="btn gray" onclick="copy('${p.amount}')">⧉ Summani nusxalash</button>
  </div>
  <div class="card pay" style="margin-top:10px">
    <small style="color:var(--mut)">Shu kartaga o'tkazing · ${p.method}</small>
    <div class="card-no">${p.card}</div>
    <div style="color:var(--mut);font-size:13px;margin-bottom:10px">${p.holder}</div>
    <button class="btn gray" onclick="copy('${p.card.replace(/\s/g,'')}')">⧉ ${t('copy')}</button>
  </div>
  <div class="card" style="padding:14px;margin-top:10px;font-size:13px;line-height:1.9">
    ✅ Summani birlikka ham o'zgartirmang<br>
    ✅ 15 daqiqa ichida to'lang<br>
    ❌ Boshqa summa yubormang<br>
    ❌ Summani bo'lib, ikki marta yubormang
  </div>
  <button class="btn wide green" style="margin-top:14px" onclick="TG.close()">📸 Chekni botga yuborish</button>
  <button class="btn wide gray" style="margin-top:8px" onclick="go('topup')">⬅️ Orqaga</button>`;
}

async function orders(){
  const d=await api('orders'); ST.orders=d.orders||[]; ST.topups=d.topups||[];
  V.innerHTML = ordersHTML(); return '';
}
function ordersHTML(){
  const badge=s=>`<span class="st ${s}">${s==='done'?'✅ Bajarildi':s==='pending'?'⏳ Kutilmoqda':s==='new'?'⏳ Yangi':'❌ Rad etildi'}</span>`;
  if(!ST.orders.length && !ST.topups.length)
    return `<div class="empty"><div>🕘</div><b>${t('noord')}</b><p>${t('noord2')}</p></div>`;
  return `<div class="sec"><h4>${t('orders')}</h4></div>
  <div class="card list">${ST.orders.map(o=>`<div class="it">
    <div>🎮</div><div><div style="font-weight:700;font-size:14px">${o.title}</div>
    <div style="color:var(--mut);font-size:12px">#${o.id} · ${o.player_id} · ${money(o.amount)} so'm</div></div>
    ${badge(o.status)}</div>`).join('') || '<div class="it">—</div>'}</div>
  <div class="sec"><h4>${t('tx')}</h4></div>
  <div class="card list">${ST.topups.map(o=>`<div class="it">
    <div>💳</div><div><div style="font-weight:700;font-size:14px">+${money(o.amount)} so'm</div>
    <div style="color:var(--mut);font-size:12px">#${o.id} · ${o.method}</div></div>
    ${badge(o.status)}</div>`).join('') || '<div class="it">—</div>'}</div>`;
}

function profile(){
  const link=`https://t.me/${(TG.initDataUnsafe?.user?.username?'':'')}`;
  const ppu = TG.initDataUnsafe?.user?.photo_url;
  const bigAv = ppu
    ? `<img class="av" style="width:96px;height:96px" src="${ppu}">`
    : `<div class="av" style="width:96px;height:96px;margin:0 auto;display:grid;place-items:center;font-size:34px;background:linear-gradient(135deg,${grad(ST.user.name||'U')})">${(ST.user.name||'U')[0].toUpperCase()}</div>`;
  return `
  <div class="card" style="padding:20px;text-align:center">
    ${bigAv}
    <h3 style="margin:10px 0 4px;text-transform:uppercase">${ST.user.name}</h3>
    <div style="color:var(--mut);font-size:12px">ID: ${ST.user.id}</div>
    <div class="card bal" style="margin-top:14px"><div class="ico">👛</div>
      <div style="text-align:left"><small>${t('bal')}</small><b>${money(ST.user.balance)} <span style="font-size:14px;color:var(--mut)">so'm</span></b></div>
      <button class="btn" style="margin-left:auto" onclick="go('topup')">+ ${t('topup')}</button></div>
  </div>
  <div class="card" style="padding:16px;margin-top:12px">
    <b>🎟 ${t('promo')}</b>
    <div style="display:flex;gap:8px;margin-top:10px">
      <input class="inp" id="pc" placeholder="PROMOKOD" style="text-transform:uppercase">
      <button class="btn" onclick="usePromo()">OK</button></div>
  </div>
  <div class="card" style="padding:16px;margin-top:12px">
    <b>👥 ${t('ref')}</b>
    <p style="color:var(--mut);font-size:13px;margin:6px 0 10px">
      Havolangiz orqali qo'shilgan har bir do'stingiz uchun ${money(ST.shop.ref_bonus)} so'm bonus.</p>
    <div class="inp" style="font-size:13px;overflow:hidden;white-space:nowrap" id="rl">t.me/?start=ref${ST.user.id}</div>
    <div class="row2">
      <button class="btn" onclick="shareRef()">🔗 ${t('share')}</button>
      <div class="card" style="padding:12px"><b style="font-size:18px">${ST.user.refs}</b>
        <div style="font-size:10px;color:var(--mut)">${t('invited')}</div></div></div>
  </div>
  <button class="btn wide gray" style="margin-top:12px" onclick="TG.close()">${t('out')}</button>`;
}
async function usePromo(){
  const c=document.getElementById('pc').value.trim();
  if(!c) return;
  const d=await api('promo',{code:c});
  if(!d.ok) return toast(d.error);
  ST.user.balance=d.balance; toast('✅ +'+money(d.amount)+" so'm"); render();
}
function shareRef(){
  const bot = 'https://t.me/' + (location.hostname.includes('.')?'':'');
  const link = 'https://t.me/share/url?url=' + encodeURIComponent('https://t.me/?start=ref'+ST.user.id);
  TG.openTelegramLink(link);
}

function go(tab){ST.tab=tab; if(tab==='orders'){orders();document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==='orders'));return;} render();}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>go(b.dataset.t));
document.getElementById('thBtn').onclick=e=>{
  document.body.classList.toggle('dark');
  e.target.textContent = document.body.classList.contains('dark')?'☀️':'🌙';};
document.getElementById('langBtn').onclick=()=>{
  const m=document.getElementById('langMenu'); m.style.display = m.style.display==='none'?'block':'none';};
document.querySelectorAll('#langMenu button').forEach(b=>b.onclick=async()=>{
  ST.lang=b.dataset.l; document.getElementById('langMenu').style.display='none';
  document.getElementById('langBtn').textContent='🌐 '+ST.lang.toUpperCase();
  await api('lang',{lang:ST.lang}); render();});
if(TG.colorScheme==='dark'){document.body.classList.add('dark');document.getElementById('thBtn').textContent='☀️';}
boot();
</script></body></html>"""


# ----------------------------------------------------------------------------
# RUN
# ----------------------------------------------------------------------------
def run_flask():
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN yo'q! Environment variable qo'ying.")
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    if WEBAPP_URL:
        log.info("Web App URL: %s", WEBAPP_URL)
    else:
        log.warning("WEBAPP_URL topilmadi — Do'kon tugmasi ko'rinmaydi. "
                    "Agar Render/Replit'dan boshqa joyda ishlatsangiz, "
                    "WEBAPP_URL environment variable'ni qo'lda qo'shing.")

    # Python 3.13+ da asyncio.get_event_loop() asosiy oqimda ham avtomatik
    # event loop yaratmay qo'ydi — buni qo'lda ochib beramiz, shunda PTB
    # (python-telegram-bot) qaysi Python versiyasida ham ishlayveradi.
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    a = Application.builder().token(BOT_TOKEN).build()
    a.add_handler(CommandHandler("start", cmd_start))
    a.add_handler(CommandHandler("admin", cmd_admin))
    a.add_handler(CallbackQueryHandler(cb_check, pattern=r"^chk$"))
    a.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^a:"))
    a.add_handler(CallbackQueryHandler(cb_order, pattern=r"^o:"))
    a.add_handler(CallbackQueryHandler(cb_topup, pattern=r"^t:"))
    a.add_handler(MessageHandler(filters.PHOTO, on_photo))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot ishga tushdi ✅")
    a.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
