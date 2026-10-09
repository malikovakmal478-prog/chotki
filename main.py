# -*- coding: utf-8 -*-
"""
SYREXA - o'yin to'ldirish do'koni (Telegram bot + Mini App + to'liq admin panel)
Bitta fayl. Render.com uchun tayyor.

ENV (Render > Environment):
  BOT_TOKEN   - BotFather tokeni
  ADMIN_IDS   - admin Telegram ID lari, vergul bilan: 123456789,987654321
  DB_PATH     - (ixtiyoriy) masalan /data/syrexa.db  (Render Disk ulangan bo'lsa)
requirements.txt:
  python-telegram-bot==21.6
  Flask==3.0.3
  requests==2.32.3
Start command:  python main.py
"""
import os, re, json, base64, time, hmac, html, random, sqlite3, hashlib, asyncio, logging, threading, functools, urllib.parse
from datetime import datetime
import requests
from flask import Flask, request, jsonify, Response
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp)
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("syrexa")

TOKEN = os.getenv("BOT_TOKEN", "")
OWNERS = [int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))]
BASE_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "syrexa.db")
E = html.escape

# ============================ DATABASE ============================
_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

SCHEMA = """
create table if not exists users(id integer primary key,name text,username text,lang text default 'uz',balance integer default 0,banned integer default 0,joined integer);
create table if not exists settings(k text primary key,v text);
create table if not exists games(id integer primary key autoincrement,name text,cat text default 'game',img text default '',field text default 'Player ID',active integer default 1,sort integer default 0);
create table if not exists products(id integer primary key autoincrement,game_id integer,name text,price integer,active integer default 1);
create table if not exists banners(id integer primary key autoincrement,img text,link text default '');
create table if not exists cards(id integer primary key autoincrement,number text,holder text,bank text default 'UZCARD',active integer default 1);
create table if not exists topups(id integer primary key autoincrement,uid integer,amount integer,card_id integer,status text,created integer);
create table if not exists orders(id integer primary key autoincrement,uid integer,game text,product text,price integer,player text,status text,created integer);
create table if not exists promos(code text primary key,amount integer,left integer);
create table if not exists promo_uses(code text,uid integer,primary key(code,uid));
create table if not exists channels(id integer primary key autoincrement,chat_id text,title text,link text);
create table if not exists admins(id integer primary key);
create table if not exists files(id integer primary key autoincrement,mime text,data blob);
"""
DEFAULTS = {
    "bot_name": "Syrexa",
    "welcome_uz": "Xush kelibsiz, {name}! 👋\n\nSyrexa — o'yinlarni tez, ishonchli va xavfsiz to'ldirish xizmati.",
    "welcome_ru": "Добро пожаловать, {name}! 👋\n\nSyrexa — быстрое, надёжное и безопасное пополнение игр.",
    "support_link": "", "channel_link": "", "min_topup": "1000", "card_ttl": "5",
    "welcome_img": "", "maintenance": "0",
}
SEED_GAMES = [("PUBG Mobile", "game"), ("Free Fire", "game"), ("Mobile Legends", "game"), ("Honor of Kings", "game"),
              ("Standoff 2", "game"), ("Steam Top Up", "game"), ("Telegram Stars", "game"), ("Telegram Premium", "game"),
              ("Bigo Live", "game"), ("Clash of Clans", "game"), ("Brawl Stars", "game"), ("Clash Royale", "game"),
              ("Roblox Robux", "promo"), ("Discord Nitro", "promo")]

def ex(sql, args=()):
    with _lock:
        c = db.execute(sql, args); db.commit(); return c
def qa(sql, args=()):
    with _lock:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
def q1(sql, args=()):
    r = qa(sql, args); return r[0] if r else None

def init_db():
    with _lock:
        db.executescript(SCHEMA); db.commit()
    for sql in ("alter table games add column hero text default ''", "alter table games add column picon text default ''",
                "alter table games add column info text default ''", "alter table products add column img text default ''",
                "alter table products add column grp text default ''", "alter table products add column badge text default ''"):
        try: ex(sql)
        except Exception: pass
    if not q1("select 1 x from games"):
        for i, (n, c) in enumerate(SEED_GAMES):
            ex("insert into games(name,cat,sort) values(?,?,?)", (n, c, i))

def gs(k):
    r = q1("select v from settings where k=?", (k,))
    return r["v"] if r else DEFAULTS.get(k, "")
def ss(k, v):
    ex("insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v", (k, str(v)))

def all_admins():
    return list(dict.fromkeys(OWNERS + [r["id"] for r in qa("select id from admins")]))
def is_admin(uid):
    return uid in all_admins()
def money(n):
    return f"{int(n):,}".replace(",", " ")
def ts(t):
    return datetime.utcfromtimestamp(int(t) + 18000).strftime("%d.%m %H:%M")
def link_ok(u):
    return bool(u) and u.startswith(("https://", "http://", "tg://"))

def tg(method, **kw):
    try:
        return requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=kw, timeout=20).json()
    except Exception as e:
        log.warning("tg %s: %s", method, e); return {}

def notify(uid, text):
    tg("sendMessage", chat_id=uid, text=text, parse_mode="HTML")

def upsert_user(uid, name, username):
    ex("insert or ignore into users(id,name,username,lang,balance,joined) values(?,?,?,?,0,?)",
       (uid, name, username or "", "uz", int(time.time())))
    ex("update users set name=?,username=? where id=?", (name, username or "", uid))
    return q1("select * from users where id=?", (uid,))

_subcache = {}
def not_subbed(uid):
    if is_admin(uid): return []
    out = []
    for ch in qa("select * from channels"):
        key = (uid, ch["chat_id"]); c = _subcache.get(key)
        if c and time.time() - c[0] < 45: ok = c[1]
        else:
            r = tg("getChatMember", chat_id=ch["chat_id"], user_id=uid)
            st = r.get("result", {}).get("status") if r.get("ok") else "member"
            ok = st in ("creator", "administrator", "member", "restricted")
            _subcache[key] = (time.time(), ok)
        if not ok: out.append(ch)
    return out

def order_kb(oid):
    return {"inline_keyboard": [[{"text": "✅ Bajarildi", "callback_data": f"o:done:{oid}"},
                                 {"text": "❌ Bekor (qaytarish)", "callback_data": f"o:no:{oid}"}]]}
def topup_kb(tid):
    return {"inline_keyboard": [[{"text": "✅ Tasdiqlash", "callback_data": f"t:ok:{tid}"},
                                 {"text": "❌ Rad etish", "callback_data": f"t:no:{tid}"}]]}
def ulink(u):
    return f'<a href="tg://user?id={u["id"]}">{E(u["name"] or str(u["id"]))}</a> (<code>{u["id"]}</code>)'

# ============================ WEB API ============================
web = Flask(__name__)
web.config['MAX_CONTENT_LENGTH'] = 14 * 1024 * 1024

def auth():
    init = request.headers.get("X-Init", "")
    try:
        data = dict(urllib.parse.parse_qsl(init, keep_blank_values=True))
        h = data.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(data.get("auth_date", 0)) > 86400 * 7: return None
        u = json.loads(data["user"])
    except Exception:
        return None
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
    return upsert_user(int(u["id"]), name, u.get("username"))

def need_user(f):
    @functools.wraps(f)
    def w(*a, **k):
        u = auth()
        if not u: return jsonify(err="auth"), 401
        if u["banned"]: return jsonify(err="banned"), 403
        if gs("maintenance") == "1" and not is_admin(u["id"]): return jsonify(err="maintenance"), 503
        return f(u, *a, **k)
    return w

@web.route("/")
def index():
    r = Response(INDEX.replace("__BOT__", E(gs("bot_name"))), mimetype="text/html")
    r.headers["Cache-Control"] = "no-store"; return r

@web.route("/health")
def health():
    return "ok"

_imgdir = "/tmp/syrexa_img"; os.makedirs(_imgdir, exist_ok=True)
@web.route("/img/<fid>")
def img(fid):
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", fid): return "", 404
    if re.fullmatch(r"f\d+", fid):
        r = q1("select mime,data from files where id=?", (int(fid[1:]),))
        if not r: return "", 404
        return Response(r["data"], mimetype=r["mime"], headers={"Cache-Control": "public, max-age=604800, immutable"})
    p = os.path.join(_imgdir, fid)
    if not os.path.exists(p):
        r = tg("getFile", file_id=fid)
        fp = r.get("result", {}).get("file_path")
        if not fp: return "", 404
        try:
            d = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", timeout=30).content
            open(p, "wb").write(d)
        except Exception:
            return "", 404
    data = open(p, "rb").read()
    mt = "image/png" if data[:4] == b"\x89PNG" else "image/webp" if data[:4] == b"RIFF" else "image/jpeg"
    return Response(data, mimetype=mt, headers={"Cache-Control": "public, max-age=86400"})

@web.route("/api/init")
@need_user
def api_init(u):
    subs = [{"title": c["title"] or c["chat_id"], "link": c["link"]} for c in not_subbed(u["id"])]
    return jsonify(
        user={"id": u["id"], "name": u["name"], "username": u["username"], "lang": u["lang"],
              "balance": u["balance"], "admin": is_admin(u["id"])},
        banners=qa("select id,img,link from banners order by id"),
        games=qa("select id,name,cat,img from games where active=1 order by sort,id"),
        cfg={"support": gs("support_link"), "channel": gs("channel_link"), "min": int(gs("min_topup") or 1000),
             "bot": gs("bot_name")}, sub=subs)

@web.route("/api/game/<int:gid>")
@need_user
def api_game(u, gid):
    g = q1("select id,name,img,hero,picon,info,field,cat from games where id=? and active=1", (gid,))
    if not g: return jsonify(err="nf"), 404
    g["products"] = qa("select id,name,price,img,grp,badge from products where game_id=? and active=1 order by id", (gid,))
    for p in g["products"]: p["img"] = p["img"] or g["picon"]
    inf = g.get("info") or ""
    g["info"] = {"text": inf.split("|")[0].strip(), "link": inf.split("|")[1].strip() if "|" in inf else ""} if inf else None
    return jsonify(g)

@web.route("/api/order", methods=["POST"])
@need_user
def api_order(u):
    d = request.get_json(silent=True) or {}
    player = str(d.get("player", "")).strip()[:100]
    p = q1("select p.*,g.name gname from products p join games g on g.id=p.game_id where p.id=? and p.active=1 and g.active=1",
           (int(d.get("product_id", 0)),))
    if not p or len(player) < 2: return jsonify(err="bad"), 400
    with _lock:
        c = ex("update users set balance=balance-? where id=? and balance>=?", (p["price"], u["id"], p["price"]))
        if c.rowcount == 0: return jsonify(err="balance"), 400
        oid = ex("insert into orders(uid,game,product,price,player,status,created) values(?,?,?,?,?,'pending',?)",
                 (u["id"], p["gname"], p["name"], p["price"], player, int(time.time()))).lastrowid
    txt = (f"🛒 <b>Yangi buyurtma #{oid}</b>\n👤 {ulink(u)}\n🎮 {E(p['gname'])} — {E(p['name'])}\n"
           f"🆔 ID: <code>{E(player)}</code>\n💰 {money(p['price'])} so'm")
    for a in all_admins(): tg("sendMessage", chat_id=a, text=txt, parse_mode="HTML", reply_markup=order_kb(oid))
    return jsonify(ok=True, id=oid, balance=q1("select balance from users where id=?", (u["id"],))["balance"])

@web.route("/api/topup", methods=["POST"])
@need_user
def api_topup(u):
    d = request.get_json(silent=True) or {}
    try: amount = int(d.get("amount", 0))
    except Exception: amount = 0
    mn = int(gs("min_topup") or 1000)
    if amount < mn or amount > 100000000: return jsonify(err="min", min=mn), 400
    cards = qa("select * from cards where active=1")
    if not cards: return jsonify(err="nocard"), 400
    cd = random.choice(cards)
    tid = ex("insert into topups(uid,amount,card_id,status,created) values(?,?,?,'new',?)",
             (u["id"], amount, cd["id"], int(time.time()))).lastrowid
    return jsonify(id=tid, amount=amount, ttl=int(gs("card_ttl") or 5) * 60,
                   card={"number": cd["number"], "holder": cd["holder"], "bank": cd["bank"]})

@web.route("/api/topup/<int:tid>/paid", methods=["POST"])
@need_user
def api_paid(u, tid):
    c = ex("update topups set status='pending' where id=? and uid=? and status='new'", (tid, u["id"]))
    if c.rowcount == 0: return jsonify(err="state"), 400
    t = q1("select * from topups where id=?", (tid,))
    cd = q1("select * from cards where id=?", (t["card_id"],)) or {}
    txt = (f"💳 <b>To'ldirish so'rovi #{tid}</b>\n👤 {ulink(u)}\n💰 <b>{money(t['amount'])}</b> so'm\n"
           f"🏦 Karta: <code>{E(cd.get('number',''))}</code> ({E(cd.get('holder',''))})\n🕒 {ts(t['created'])}\n\n"
           f"Pul kartaga tushganini tekshiring, so'ng tasdiqlang.")
    for a in all_admins(): tg("sendMessage", chat_id=a, text=txt, parse_mode="HTML", reply_markup=topup_kb(tid))
    return jsonify(ok=True)

@web.route("/api/history")
@need_user
def api_history(u):
    return jsonify(
        orders=qa("select id,game,product,price,status,created from orders where uid=? order by id desc limit 50", (u["id"],)),
        tx=qa("select id,amount,status,created from topups where uid=? and status!='new' order by id desc limit 50", (u["id"],)))

@web.route("/api/promo", methods=["POST"])
@need_user
def api_promo(u):
    code = str((request.get_json(silent=True) or {}).get("code", "")).strip().upper()
    with _lock:
        p = q1("select * from promos where code=?", (code,))
        if not p or p["left"] <= 0: return jsonify(err="nf"), 400
        if q1("select 1 x from promo_uses where code=? and uid=?", (code, u["id"])): return jsonify(err="used"), 400
        ex("insert into promo_uses values(?,?)", (code, u["id"]))
        ex("update promos set left=left-1 where code=?", (code,))
        ex("update users set balance=balance+? where id=?", (p["amount"], u["id"]))
    return jsonify(ok=True, amount=p["amount"], balance=q1("select balance from users where id=?", (u["id"],))["balance"])

@web.route("/api/lang", methods=["POST"])
@need_user
def api_lang(u):
    l = (request.get_json(silent=True) or {}).get("lang")
    if l in ("uz", "ru"): ex("update users set lang=? where id=?", (l, u["id"]))
    return jsonify(ok=True)

# ============================ ADMIN WEB API (Mini App ichidagi panel) ============================
def _i(v):
    try: return int(float(str(v).replace(" ", "") or 0))
    except Exception: return 0

def need_admin(f):
    @functools.wraps(f)
    def w(*a, **k):
        u = auth()
        if not u or not is_admin(u["id"]): return jsonify(err="forbidden"), 403
        return f(u, *a, **k)
    return w

COLS = {
    "games": (["name", "cat", "img", "hero", "picon", "info", "field", "active", "sort"], ["active", "sort"]),
    "products": (["game_id", "name", "price", "grp", "badge", "img", "active"], ["game_id", "price", "active"]),
    "banners": (["img", "link"], []),
    "cards": (["number", "holder", "bank", "active"], ["active"]),
    "channels": (["chat_id", "title", "link"], []),
}

def decide_topup(tid, ok):
    t = q1("select * from topups where id=?", (tid,))
    if not t: return "Topilmadi"
    c = ex("update topups set status=? where id=? and status in ('new','pending')", ("approved" if ok else "rejected", tid))
    if not c.rowcount: return "Allaqachon ko'rilgan"
    if ok:
        ex("update users set balance=balance+? where id=?", (t["amount"], t["uid"]))
        notify(t["uid"], f"✅ Balansingiz <b>{money(t['amount'])}</b> so'mga to'ldirildi.")
        return "✅ Tasdiqlandi"
    notify(t["uid"], f"❌ {money(t['amount'])} so'm to'ldirish so'rovi rad etildi.")
    return "❌ Rad etildi"

def decide_order(oid, ok):
    o = q1("select * from orders where id=?", (oid,))
    if not o: return "Topilmadi"
    c = ex("update orders set status=? where id=? and status='pending'", ("done" if ok else "canceled", oid))
    if not c.rowcount: return "Allaqachon ko'rilgan"
    if ok:
        notify(o["uid"], f"✅ Buyurtma #{oid} bajarildi!\n🎮 {E(o['game'])} — {E(o['product'])}")
        return "✅ Bajarildi"
    ex("update users set balance=balance+? where id=?", (o["price"], o["uid"]))
    notify(o["uid"], f"❌ Buyurtma #{oid} bekor qilindi, <b>{money(o['price'])}</b> so'm balansga qaytarildi.")
    return "❌ Bekor qilindi, pul qaytarildi"

@web.route("/api/a/data/<name>")
@need_admin
def a_data(u, name):
    gid = request.args.get("id", type=int); s = request.args.get("q", "").strip()
    if name == "home":
        d0 = (int(time.time()) + 18000) // 86400 * 86400 - 18000
        n = q1("select count(*) c, coalesce(sum(balance),0) b from users")
        tp = q1("select coalesce(sum(amount),0) s from topups where status='approved'")["s"]
        tt = q1("select coalesce(sum(amount),0) s from topups where status='approved' and created>=?", (d0,))["s"]
        od = q1("select count(*) c, coalesce(sum(price),0) s from orders where status='done'")
        return jsonify(users=n["c"], bal=n["b"], new=q1("select count(*) c from users where joined>=?", (d0,))["c"],
                       top_sum=tp, top_today=tt, ord_cnt=od["c"], ord_sum=od["s"],
                       p_top=q1("select count(*) c from topups where status='pending'")["c"],
                       p_ord=q1("select count(*) c from orders where status='pending'")["c"])
    if name == "games":
        return jsonify(games=qa("select g.*,(select count(*) from products where game_id=g.id) pc from games g order by sort,id"))
    if name == "game":
        return jsonify(game=q1("select * from games where id=?", (gid,)),
                       products=qa("select * from products where game_id=? order by id", (gid,)))
    if name == "banners": return jsonify(items=qa("select * from banners order by id"))
    if name == "cards": return jsonify(items=qa("select * from cards order by id"))
    if name == "users":
        if s.isdigit(): rows = qa("select * from users where id=? or name like ? limit 30", (int(s), f"%{s}%"))
        elif s: rows = qa("select * from users where name like ? or username like ? limit 30", (f"%{s}%", f"%{s.lstrip('@')}%"))
        else: rows = qa("select * from users order by id desc limit 30")
        return jsonify(items=rows)
    if name == "tops":
        return jsonify(items=qa("select t.*,u.name uname from topups t left join users u on u.id=t.uid where t.status!='new' order by (t.status='pending') desc, t.id desc limit 40"))
    if name == "ords":
        return jsonify(items=qa("select o.*,u.name uname from orders o left join users u on u.id=o.uid order by (o.status='pending') desc, o.id desc limit 40"))
    if name == "promos": return jsonify(items=qa("select * from promos"))
    if name == "chs": return jsonify(items=qa("select * from channels"))
    if name == "set": return jsonify(s={k: gs(k) for k in DEFAULTS})
    if name == "adms": return jsonify(items=all_admins(), owners=OWNERS)
    if name == "bc": return jsonify(users=q1("select count(*) c from users where banned=0")["c"])
    return jsonify(err="nf"), 404

@web.route("/api/a/save/<t>", methods=["POST"])
@need_admin
def a_save(u, t):
    d = request.get_json(silent=True) or {}
    if t == "promos":
        code = str(d.get("code", "")).strip().upper()
        if not code or _i(d.get("amount")) <= 0: return jsonify(err="Kod va summani kiriting"), 400
        ex("insert or replace into promos(code,amount,left) values(?,?,?)", (code, _i(d.get("amount")), _i(d.get("left")))); return jsonify(ok=True)
    if t not in COLS: return jsonify(err="bad"), 400
    cols, ints = COLS[t]; vals = {}
    for c in cols:
        if c in d: vals[c] = _i(d[c]) if c in ints else str(d[c] if d[c] is not None else "").strip()
    if t == "games" and not vals.get("name"): return jsonify(err="Nom kiriting"), 400
    if t == "products" and (not vals.get("name") or vals.get("price", 0) <= 0): return jsonify(err="Nom va narxni kiriting"), 400
    if t == "banners" and not vals.get("img"): return jsonify(err="Rasm tanlang"), 400
    if t == "cards" and "number" in vals:
        dg = re.sub(r"\D", "", vals["number"])
        if len(dg) < 12: return jsonify(err="Karta raqami noto'g'ri"), 400
        vals["number"] = " ".join(dg[i:i+4] for i in range(0, len(dg), 4))
    if t == "channels" and not d.get("id"):
        r = tg("getChat", chat_id=vals.get("chat_id", ""))
        if not r.get("ok"): return jsonify(err="Kanal topilmadi yoki bot u yerda admin emas"), 400
        c = r["result"]; vals["chat_id"] = str(c["id"]); vals["title"] = c.get("title", "")
        vals["link"] = f"https://t.me/{c['username']}" if c.get("username") else (c.get("invite_link") or tg("exportChatInviteLink", chat_id=c["id"]).get("result", ""))
    if t == "games" and not d.get("id"): vals["sort"] = q1("select coalesce(max(sort),0)+1 m from games")["m"]
    if not vals: return jsonify(err="bo'sh"), 400
    if d.get("id"):
        rid = int(d["id"])
        ex(f"update {t} set {','.join(c + '=?' for c in vals)} where id=?", (*vals.values(), rid))
    else:
        rid = ex(f"insert into {t}({','.join(vals)}) values({','.join('?' * len(vals))})", tuple(vals.values())).lastrowid
    return jsonify(ok=True, id=rid)

@web.route("/api/a/del/<t>", methods=["POST"])
@need_admin
def a_del(u, t):
    i = (request.get_json(silent=True) or {}).get("id")
    if t == "promos": ex("delete from promos where code=?", (str(i),))
    elif t == "games": ex("delete from products where game_id=?", (int(i),)); ex("delete from games where id=?", (int(i),))
    elif t in COLS: ex(f"delete from {t} where id=?", (int(i),))
    else: return jsonify(err="bad"), 400
    return jsonify(ok=True)

@web.route("/api/a/bulk", methods=["POST"])
@need_admin
def a_bulk(u):
    d = request.get_json(silent=True) or {}; n = 0
    for line in str(d.get("text", "")).splitlines():
        pt = [x.strip() for x in line.split("|")]
        if len(pt) >= 2 and pt[0] and _i(pt[1]) > 0:
            ex("insert into products(game_id,name,price,grp,badge) values(?,?,?,?,?)",
               (int(d["game_id"]), pt[0], _i(pt[1]), pt[2] if len(pt) > 2 else "", pt[3] if len(pt) > 3 else "")); n += 1
    if not n: return jsonify(err="Format: nom | narx | guruh | belgi"), 400
    return jsonify(ok=True, n=n)

@web.route("/api/a/upload", methods=["POST"])
@need_admin
def a_upload(u):
    d = (request.get_json(silent=True) or {}).get("data", "")
    m = re.match(r"data:(image/(?:jpeg|png|webp));base64,(.+)$", d, re.S)
    if not m: return jsonify(err="Rasm formati noto'g'ri"), 400
    raw = base64.b64decode(m.group(2))
    if len(raw) > 6_000_000: return jsonify(err="Rasm juda katta"), 400
    return jsonify(ref=f"f{ex('insert into files(mime,data) values(?,?)', (m.group(1), raw)).lastrowid}")

@web.route("/api/a/import", methods=["POST"])
@need_admin
def a_import(u):
    url = str((request.get_json(silent=True) or {}).get("url", "")).strip()
    msg = "Havoladan rasm olinmadi. Rasm ustida «rasm manzilini nusxalash» qiling (.jpg/.png/.webp)"
    if not url.startswith(("http://", "https://")): return jsonify(err=msg), 400
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if r.status_code != 200 or ct not in ("image/jpeg", "image/png", "image/webp") or len(r.content) > 6_000_000:
            return jsonify(err=msg), 400
    except Exception:
        return jsonify(err=msg), 400
    return jsonify(ref=f"f{ex('insert into files(mime,data) values(?,?)', (ct, r.content)).lastrowid}")

@web.route("/api/a/set", methods=["POST"])
@need_admin
def a_set(u):
    for k, v in (request.get_json(silent=True) or {}).items():
        if k in DEFAULTS:
            ss(k, re.sub(r"\D", "", str(v)) or DEFAULTS[k] if k in ("min_topup", "card_ttl") else str(v if v is not None else "").strip())
    return jsonify(ok=True)

@web.route("/api/a/user", methods=["POST"])
@need_admin
def a_user(u):
    d = request.get_json(silent=True) or {}; uid = int(d["id"]); amt = _i(d.get("amt"))
    if amt > 0:
        ex("update users set balance=max(0,balance+?) where id=?", (amt if d.get("op") == "add" else -amt, uid))
        notify(uid, f"{'➕' if d.get('op') == 'add' else '➖'} Balansingiz o'zgardi: <b>{money(amt)}</b> so'm")
    if "banned" in d: ex("update users set banned=? where id=?", (1 if _i(d["banned"]) else 0, uid))
    return jsonify(ok=True)

@web.route("/api/a/topup", methods=["POST"])
@need_admin
def a_topup(u):
    d = request.get_json(silent=True) or {}; return jsonify(msg=decide_topup(int(d["id"]), _i(d.get("ok"))))

@web.route("/api/a/order", methods=["POST"])
@need_admin
def a_order(u):
    d = request.get_json(silent=True) or {}; return jsonify(msg=decide_order(int(d["id"]), _i(d.get("ok"))))

@web.route("/api/a/admin", methods=["POST"])
@need_admin
def a_admin(u):
    d = request.get_json(silent=True) or {}; i = _i(d.get("id"))
    if not i: return jsonify(err="ID kiriting"), 400
    if d.get("remove"):
        if i not in OWNERS: ex("delete from admins where id=?", (i,))
    else: ex("insert or ignore into admins values(?)", (i,))
    return jsonify(ok=True)

def do_broadcast(text, img):
    blob = None; fid = None
    if img and re.fullmatch(r"f\d+", img):
        r = q1("select data from files where id=?", (int(img[1:]),)); blob = r["data"] if r else None
    elif img: fid = img
    ok = bad = 0
    for r0 in qa("select id from users where banned=0"):
        uid = r0["id"]
        try:
            if blob and not fid:
                r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data={"chat_id": uid, "caption": text, "parse_mode": "HTML"},
                                  files={"photo": ("p.jpg", blob)}, timeout=40).json()
                if r.get("ok"): fid = r["result"]["photo"][-1]["file_id"]
            elif fid: r = tg("sendPhoto", chat_id=uid, photo=fid, caption=text, parse_mode="HTML")
            else: r = tg("sendMessage", chat_id=uid, text=text, parse_mode="HTML")
            if r.get("ok"): ok += 1
            else: bad += 1
        except Exception: bad += 1
        time.sleep(0.05)
    for a in all_admins(): notify(a, f"📨 Xabar yuborildi: ✅ {ok}  ❌ {bad}")

@web.route("/api/a/broadcast", methods=["POST"])
@need_admin
def a_broadcast(u):
    d = request.get_json(silent=True) or {}
    threading.Thread(target=do_broadcast, args=(str(d.get("text", "")), str(d.get("img", ""))), daemon=True).start()
    return jsonify(ok=True)

# ============================ ZAXIRA (Render bepul rejasi bazani o'chirmasligi uchun) ============================
_last_bak = {"id": None}
def backup_now():
    if not OWNERS: return
    try:
        tmp = "/tmp/syrexa_backup.db"
        if os.path.exists(tmp): os.remove(tmp)
        dst = sqlite3.connect(tmp)
        with _lock: db.backup(dst)
        dst.close()
        with open(tmp, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument", data={"chat_id": OWNERS[0], "caption": "💾 Syrexa zaxira nusxa " + ts(time.time())},
                              files={"document": ("syrexa_backup.db", f)}, timeout=120).json()
        if r.get("ok"):
            mid = r["result"]["message_id"]
            tg("pinChatMessage", chat_id=OWNERS[0], message_id=mid, disable_notification=True)
            if _last_bak["id"]: tg("deleteMessage", chat_id=OWNERS[0], message_id=_last_bak["id"])
            _last_bak["id"] = mid
    except Exception as e:
        log.warning("backup: %s", e)

def restore_from(path):
    src = sqlite3.connect(path); src.execute("select count(*) from games")
    with _lock: src.backup(db)
    src.close(); init_db()

def auto_restore():
    if not OWNERS: return
    if q1("select count(*) c from users")["c"] or q1("select count(*) c from products")["c"]: return
    pm = tg("getChat", chat_id=OWNERS[0]).get("result", {}).get("pinned_message") or {}
    fid = (pm.get("document") or {}).get("file_id")
    if not fid: return
    fp = tg("getFile", file_id=fid).get("result", {}).get("file_path")
    if not fp: return
    open("/tmp/restore.db", "wb").write(requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{fp}", timeout=120).content)
    restore_from("/tmp/restore.db"); _last_bak["id"] = pm.get("message_id")
    log.info("Baza zaxiradan tiklandi")

def backup_loop():
    last = db.total_changes; lastt = 0
    while True:
        time.sleep(120)
        if db.total_changes != last and time.time() - lastt > 240:
            last = db.total_changes; lastt = time.time(); backup_now()

async def cmd_backup(update, ctx):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("⏳ Zaxira nusxa yuborilmoqda...")
        await asyncio.to_thread(backup_now)

async def on_doc(update, ctx):
    u = update.effective_user; m = update.message
    if not u or not is_admin(u.id) or not m.document or "/restore" not in (m.caption or ""): return
    f = await m.document.get_file(); await f.download_to_drive("/tmp/restore.db")
    try:
        restore_from("/tmp/restore.db"); await m.reply_text("✅ Baza tiklandi")
    except Exception as e:
        await m.reply_text(f"❌ Xato: {e}")

# ============================ BOT: USER SIDE ============================
def webapp_url():
    return BASE_URL + "/"

async def send_welcome(update: Update, ctx):
    u = update.effective_user
    row = upsert_user(u.id, (u.first_name or "") + " " + (u.last_name or ""), u.username)
    if row["banned"]: return
    if gs("maintenance") == "1" and not is_admin(u.id):
        return await ctx.bot.send_message(u.id, "🛠 Texnik ishlar olib borilmoqda. Keyinroq urinib ko'ring.")
    subs = not_subbed(u.id)
    if subs:
        rows = [[InlineKeyboardButton(f"📢 {c['title'] or 'Kanal'}", url=c["link"])] for c in subs if link_ok(c["link"])]
        rows.append([InlineKeyboardButton("✅ Tekshirish", callback_data="chk")])
        return await ctx.bot.send_message(u.id, "Botdan foydalanish uchun kanallarga obuna bo'ling:",
                                          reply_markup=InlineKeyboardMarkup(rows))
    text = gs("welcome_ru" if row["lang"] == "ru" else "welcome_uz").replace("{name}", E(u.first_name or ""))
    rows = [[InlineKeyboardButton("📱 Ilovani ochish", web_app=WebAppInfo(url=webapp_url()))]]
    r2 = []
    if link_ok(gs("channel_link")): r2.append(InlineKeyboardButton("Bizning kanal", url=gs("channel_link")))
    if link_ok(gs("support_link")): r2.append(InlineKeyboardButton("Yordam", url=gs("support_link")))
    if r2: rows.append(r2)
    kb = InlineKeyboardMarkup(rows)
    img = gs("welcome_img")
    if re.fullmatch(r"f\d+", img or ""):
        _r = q1("select data from files where id=?", (int(img[1:]),)); img = _r["data"] if _r else None
    if img:
        try:
            return await ctx.bot.send_photo(u.id, img, caption=text, reply_markup=kb, parse_mode="HTML")
        except Exception: pass
    await ctx.bot.send_message(u.id, text, reply_markup=kb, parse_mode="HTML")

async def cmd_start(update, ctx):
    ctx.user_data.pop("st", None)
    await send_welcome(update, ctx)

async def cb_chk(update, ctx):
    q = update.callback_query
    _subcache.clear()
    if not_subbed(q.from_user.id):
        return await q.answer("❌ Hali obuna bo'lmagansiz", show_alert=True)
    await q.answer("✅")
    try: await q.message.delete()
    except Exception: pass
    await send_welcome(update, ctx)

# ============================ BOT: ADMIN ============================
def AK(rows):
    out = []
    for r in rows:
        out.append([InlineKeyboardButton(t, url=d) if d.startswith("http") else InlineKeyboardButton(t, callback_data=d) for t, d in r])
    return InlineKeyboardMarkup(out)

BACK = [("🔙 Admin menyu", "a:home")]

async def show(update, view):
    text, rows = view
    q = update.callback_query
    if q:
        try:
            return await q.edit_message_text(text, reply_markup=AK(rows), parse_mode="HTML", disable_web_page_preview=True)
        except BadRequest as e:
            if "not modified" in str(e).lower(): return
            return await q.message.reply_text(text, reply_markup=AK(rows), parse_mode="HTML", disable_web_page_preview=True)
    await update.message.reply_text(text, reply_markup=AK(rows), parse_mode="HTML", disable_web_page_preview=True)

def v_home():
    return ("🛠 <b>SYREXA — Admin panel</b>\nKerakli bo'limni tanlang:", [
        [("📊 Statistika", "a:stat"), ("👥 Foydalanuvchilar", "a:users")],
        [("🎮 O'yinlar / Narxlar", "a:games"), ("🖼 Bannerlar", "a:banners")],
        [("💳 Kartalar", "a:cards"), ("💰 To'ldirishlar", "a:tops")],
        [("📦 Buyurtmalar", "a:ords"), ("🎟 Promokodlar", "a:promos")],
        [("📢 Majburiy obuna", "a:chs"), ("📨 Xabar yuborish", "a:bc")],
        [("⚙️ Sozlamalar", "a:set"), ("👮 Adminlar", "a:adms")]])

def v_stat():
    d0 = (int(time.time()) + 18000) // 86400 * 86400 - 18000
    n = q1("select count(*) c, coalesce(sum(balance),0) b from users")
    new = q1("select count(*) c from users where joined>=?", (d0,))["c"]
    tp = q1("select count(*) c, coalesce(sum(amount),0) s from topups where status='approved'")
    tpt = q1("select coalesce(sum(amount),0) s from topups where status='approved' and created>=?", (d0,))["s"]
    od = q1("select count(*) c, coalesce(sum(price),0) s from orders where status='done'")
    odp = q1("select count(*) c from orders where status='pending'")["c"]
    tpp = q1("select count(*) c from topups where status='pending'")["c"]
    txt = (f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: <b>{n['c']}</b> (bugun +{new})\n💼 Umumiy balans: <b>{money(n['b'])}</b> so'm\n\n"
           f"💰 To'ldirilgan: <b>{money(tp['s'])}</b> so'm ({tp['c']} ta)\n📅 Bugun: <b>{money(tpt)}</b> so'm\n\n"
           f"📦 Bajarilgan buyurtmalar: <b>{od['c']}</b> ta — {money(od['s'])} so'm\n"
           f"⏳ Kutilayotgan buyurtma: <b>{odp}</b>\n⏳ Kutilayotgan to'ldirish: <b>{tpp}</b>")
    return txt, [[("🔄 Yangilash", "a:stat")], BACK]

def v_users():
    top = qa("select * from users order by id desc limit 8")
    rows = [[("🔎 Qidirish (ID / @username)", "a:find")]]
    for u in top: rows.append([(f"{'🚫 ' if u['banned'] else ''}{u['name'][:22]} · {money(u['balance'])}", f"a:u:{u['id']}")])
    rows.append(BACK)
    return "👥 <b>Foydalanuvchilar</b>\nSo'nggi qo'shilganlar:", rows

def v_user(uid):
    u = q1("select * from users where id=?", (uid,))
    if not u: return "Topilmadi", [BACK]
    oc = q1("select count(*) c from orders where uid=?", (uid,))["c"]
    txt = (f"👤 {ulink(u)}\n@{E(u['username'] or '-')}\n💰 Balans: <b>{money(u['balance'])}</b> so'm\n"
           f"📦 Buyurtmalar: {oc}\n🌐 Til: {u['lang']}\n📅 {ts(u['joined'] or 0)}\n{'🚫 BLOKLANGAN' if u['banned'] else ''}")
    return txt, [[("➕ Balans qo'shish", f"a:bal:{uid}:+"), ("➖ Balans ayirish", f"a:bal:{uid}:-")],
                 [("✅ Blokdan chiqarish" if u["banned"] else "🚫 Bloklash", f"a:ban:{uid}")],
                 [("🔙 Userlar", "a:users")]]

def v_games():
    rows = [[("➕ O'yin qo'shish", "a:gnew")]]
    for g in qa("select * from games order by sort,id"):
        rows.append([(f"{'🟢' if g['active'] else '🔴'} {'🎁' if g['cat']=='promo' else '🎮'} {g['name']}", f"a:g:{g['id']}")])
    rows.append(BACK)
    return "🎮 <b>O'yinlar</b>\nO'yinni tanlab, rasm/nom/mahsulot va narxlarni o'zgartiring:", rows

def v_game(gid):
    g = q1("select * from games where id=?", (gid,))
    if not g: return "Topilmadi", [[("🔙", "a:games")]]
    ps = qa("select * from products where game_id=? order by price,id", (gid,))
    txt = (f"🎮 <b>{E(g['name'])}</b>\nKategoriya: {'Promokodlar' if g['cat']=='promo' else 'O`yinlar'}\n"
           f"ID maydoni: <i>{E(g['field'])}</i>\nRasm: {'✅' if g['img'] else '❌'}\nHolat: {'faol' if g['active'] else 'o`chirilgan'}\n"
           f"Mahsulotlar: {len(ps)} ta")
    rows = [[("➕ Mahsulot qo'shish", f"a:padd:{gid}")]]
    for p in ps: rows.append([(f"{'🟢' if p['active'] else '🔴'} {(p['grp']+' · ') if p['grp'] else ''}{p['name']} — {money(p['price'])}", f"a:p:{p['id']}")])
    rows += [[("🖼 Banner (katta rasm)", f"a:ghero:{gid}"), ("💎 Mahsulot ikonkasi", f"a:gpicon:{gid}")],
             [("🖼 Guruhga rasm", f"a:pgimg:{gid}"), ("ℹ️ Info qator", f"a:ginfo:{gid}")],
             [("✏️ Nom", f"a:gname:{gid}"), ("🖼 Kichik ikonka", f"a:gimg:{gid}")],
             [("🔤 ID maydoni nomi", f"a:gfield:{gid}"), ("📂 Kategoriya", f"a:gcat2:{gid}")],
             [("👁 Yoqish/O'chirish", f"a:gtog:{gid}"), ("🗑 O'yinni o'chirish", f"a:gdel:{gid}")],
             [("🔙 O'yinlar", "a:games")]]
    return txt, rows

def v_prod(pid):
    p = q1("select * from products where id=?", (pid,))
    if not p: return "Topilmadi", [[("🔙", "a:games")]]
    return (f"📦 <b>{E(p['name'])}</b>\n💰 {money(p['price'])} so'm\nGuruh: {E(p['grp'] or '-')} · Belgi: {E(p['badge'] or '-')} · Rasm: {'✅' if p['img'] else '❌'}\nHolat: {'faol' if p['active'] else 'o`chirilgan'}",
            [[("✏️ Nom", f"a:pname:{pid}"), ("💰 Narx", f"a:pprice:{pid}")],
             [("🖼 Rasm", f"a:pimg:{pid}"), ("📂 Guruh", f"a:pgrp:{pid}"), ("🏷 Belgi", f"a:pbadge:{pid}")],
             [("👁 Yoqish/O'chirish", f"a:ptog:{pid}"), ("🗑 O'chirish", f"a:pdel:{pid}")],
             [("🔙 O'yin", f"a:g:{p['game_id']}")]])

def v_banners():
    bs = qa("select * from banners order by id")
    rows = [[("➕ Banner qo'shish", "a:badd")]]
    for b in bs: rows.append([(f"🗑 Banner #{b['id']} {('→ '+b['link'][:25]) if b['link'] else ''}", f"a:bdel:{b['id']}")])
    rows.append(BACK)
    return f"🖼 <b>Bannerlar</b> ({len(bs)} ta)\nIlova bosh sahifasidagi reklama bannerlari. O'chirish uchun bosing.", rows

def v_cards():
    cs = qa("select * from cards order by id")
    rows = [[("➕ Karta qo'shish", "a:cadd")]]
    for c in cs: rows.append([(f"{'🟢' if c['active'] else '🔴'} {c['bank']} {c['number']} · {c['holder']}", f"a:c:{c['id']}")])
    rows.append(BACK)
    return ("💳 <b>Kartalar</b>\nTo'ldirishda faol kartalardan biri tasodifiy beriladi.\nKartasiz to'ldirish ishlamaydi!", rows)

def v_card(cid):
    c = q1("select * from cards where id=?", (cid,))
    if not c: return "Topilmadi", [[("🔙", "a:cards")]]
    return (f"💳 <code>{E(c['number'])}</code>\n👤 {E(c['holder'])}\n🏦 {E(c['bank'])}\nHolat: {'faol' if c['active'] else 'o`chirilgan'}",
            [[("✏️ Almashtirish (raqam | ism | bank)", f"a:cedit:{cid}")],
             [("👁 Yoqish/O'chirish", f"a:ctog:{cid}"), ("🗑 O'chirish", f"a:cdel:{cid}")], [("🔙 Kartalar", "a:cards")]])

def v_tops():
    ts_ = qa("select * from topups where status='pending' order by id desc limit 15")
    rows = [[(f"#{t['id']} · {money(t['amount'])} · {ts(t['created'])}", f"a:t:{t['id']}")] for t in ts_]
    rows.append(BACK)
    return f"💰 <b>Kutilayotgan to'ldirishlar</b> ({len(ts_)})", rows

def v_top(tid):
    t = q1("select * from topups where id=?", (tid,))
    if not t: return "Topilmadi", [[("🔙", "a:tops")]]
    u = q1("select * from users where id=?", (t["uid"],)) or {"id": t["uid"], "name": ""}
    rows = [[("✅ Tasdiqlash", f"t:ok:{tid}"), ("❌ Rad etish", f"t:no:{tid}")]] if t["status"] == "pending" else []
    rows.append([("🔙", "a:tops")])
    return f"💳 To'ldirish #{tid}\n👤 {ulink(u)}\n💰 {money(t['amount'])} so'm\nHolat: <b>{t['status']}</b>", rows

def v_ords():
    os_ = qa("select * from orders where status='pending' order by id desc limit 15")
    rows = [[(f"#{o['id']} {o['game']} · {o['product']} · {money(o['price'])}", f"a:o:{o['id']}")] for o in os_]
    rows.append(BACK)
    return f"📦 <b>Kutilayotgan buyurtmalar</b> ({len(os_)})", rows

def v_ord(oid):
    o = q1("select * from orders where id=?", (oid,))
    if not o: return "Topilmadi", [[("🔙", "a:ords")]]
    u = q1("select * from users where id=?", (o["uid"],)) or {"id": o["uid"], "name": ""}
    rows = [[("✅ Bajarildi", f"o:done:{oid}"), ("❌ Bekor (qaytarish)", f"o:no:{oid}")]] if o["status"] == "pending" else []
    rows.append([("🔙", "a:ords")])
    return (f"📦 Buyurtma #{oid}\n👤 {ulink(u)}\n🎮 {E(o['game'])} — {E(o['product'])}\n🆔 <code>{E(o['player'])}</code>\n"
            f"💰 {money(o['price'])} so'm\nHolat: <b>{o['status']}</b>"), rows

def v_promos():
    ps = qa("select * from promos")
    rows = [[("➕ Promokod qo'shish", "a:pradd")]]
    for p in ps: rows.append([(f"🗑 {p['code']} · {money(p['amount'])} · qolgan {p['left']}", f"a:prdel:{p['code']}")])
    rows.append(BACK)
    return "🎟 <b>Promokodlar</b>\nBalansga pul beradigan kodlar. O'chirish uchun bosing.", rows

def v_chs():
    cs = qa("select * from channels")
    rows = [[("➕ Kanal/guruh qo'shish", "a:chadd")]]
    for c in cs: rows.append([(f"🗑 {c['title'] or c['chat_id']}", f"a:chdel:{c['id']}")])
    rows.append(BACK)
    return "📢 <b>Majburiy obuna</b>\nBot kanalda ADMIN bo'lishi shart.", rows

SET_KEYS = [("bot_name", "Bot nomi"), ("welcome_uz", "Salomlashuv (UZ)"), ("welcome_ru", "Salomlashuv (RU)"),
            ("welcome_img", "Salomlashuv rasmi"), ("support_link", "Yordam havolasi"), ("channel_link", "Kanal havolasi"),
            ("min_topup", "Minimal to'ldirish (so'm)"), ("card_ttl", "Karta amal qilish vaqti (daqiqa)")]

def v_set():
    rows = [[(f"✏️ {lbl}", f"a:s:{k}")] for k, lbl in SET_KEYS]
    m = gs("maintenance") == "1"
    rows.append([(f"🛠 Texnik ishlar: {'YOQIQ' if m else 'o`chiq'}", "a:s:maintenance")])
    rows.append(BACK)
    txt = "⚙️ <b>Sozlamalar</b>\n\n" + "\n".join(
        f"• {lbl}: <code>{E((gs(k) or '-')[:40])}</code>" for k, lbl in SET_KEYS if k != "welcome_img")
    return txt + "\n\nSalomlashuv matnida <code>{name}</code> — foydalanuvchi ismi.", rows

def v_adms():
    rows = [[("➕ Admin qo'shish", "a:amadd")]]
    for r in qa("select id from admins"): rows.append([(f"🗑 {r['id']}", f"a:amdel:{r['id']}")])
    rows.append(BACK)
    return f"👮 <b>Adminlar</b>\nAsosiy (ENV): {', '.join(map(str, OWNERS)) or '-'}", rows

async def ask(update, ctx, st, text):
    ctx.user_data["st"] = st
    await show(update, (text + "\n\n<i>Bekor qilish: /cancel</i>", [[("🔙 Bekor", "a:home")]]))

async def cmd_admin(update, ctx):
    if not is_admin(update.effective_user.id): return
    ctx.user_data.pop("st", None)
    rows = [[InlineKeyboardButton("🛠 Admin panelni ochish", web_app=WebAppInfo(url=webapp_url() + "?admin=1"))],
            [InlineKeyboardButton("💬 Chat ichidagi panel", callback_data="a:home")]]
    await update.message.reply_text("🛠 <b>SYREXA Admin</b>\nRasm, narx, karta — hammasini qulay panelda o'zgartiring:",
                                    reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML")

async def cmd_cancel(update, ctx):
    ctx.user_data.pop("st", None)
    if is_admin(update.effective_user.id): await show(update, v_home())

async def adm_cb(update, ctx):
    q = update.callback_query
    if not is_admin(q.from_user.id): return await q.answer("⛔", show_alert=True)
    await q.answer()
    d = q.data.split(":"); a = d[1]; x = d[2] if len(d) > 2 else None
    ctx.user_data.pop("st", None) if a in ("home", "stat", "users", "games", "banners", "cards", "tops", "ords", "promos", "chs", "set", "adms") else None
    S = lambda v: show(update, v)
    if a == "home": return await S(v_home())
    if a == "stat": return await S(v_stat())
    if a == "users": return await S(v_users())
    if a == "find": return await ask(update, ctx, ("find",), "🔎 Foydalanuvchi ID yoki @username yuboring:")
    if a == "u": return await S(v_user(int(x)))
    if a == "bal": return await ask(update, ctx, ("bal", int(x), d[3]), f"{'➕ Qo`shiladigan' if d[3]=='+' else '➖ Ayiriladigan'} summani yuboring (so'm):")
    if a == "ban":
        u = q1("select banned from users where id=?", (int(x),))
        ex("update users set banned=? where id=?", (0 if u["banned"] else 1, int(x)))
        return await S(v_user(int(x)))
    # games
    if a == "games": return await S(v_games())
    if a == "gnew": return await ask(update, ctx, ("gnew",), "🎮 Yangi o'yin nomini yuboring:")
    if a == "gcat":
        name = ctx.user_data.get("gname", "Yangi")
        gid = ex("insert into games(name,cat,sort) values(?,?,?)", (name, x, int(time.time()) % 100000)).lastrowid
        return await ask(update, ctx, ("gimg", gid), f"✅ «{E(name)}» qo'shildi.\n🖼 Endi o'yin rasmini yuboring (yoki /cancel):")
    if a == "g": return await S(v_game(int(x)))
    if a == "gname": return await ask(update, ctx, ("gname", int(x)), "✏️ Yangi nomni yuboring:")
    if a == "gimg": return await ask(update, ctx, ("gimg", int(x)), "🖼 Rasmni yuboring (rasm sifatida):")
    if a == "gfield": return await ask(update, ctx, ("gfield", int(x)), "🔤 Foydalanuvchi to'ldiradigan maydon nomi (masalan: Player ID, UID, Telegram username):")
    if a == "ghero": return await ask(update, ctx, ("ghero", int(x)), "🖼 O'yin sahifasi tepasidagi KATTA banner rasmini yuboring (gorizontal, rasm sifatida):")
    if a == "gpicon": return await ask(update, ctx, ("gpicon", int(x)), "💎 Mahsulotlar uchun umumiy ikonka yuboring (masalan UC rasmi). Alohida rasmi yo'q mahsulotlar shuni ishlatadi:")
    if a == "ginfo": return await ask(update, ctx, ("ginfo", int(x)), "ℹ️ Format: <code>matn | havola</code>\nMasalan: <code>MLBB News Channel | https://t.me/kanal</code>\nO'chirish: <code>-</code>")
    if a == "pgimg": return await ask(update, ctx, ("pgname", int(x)), "📂 Qaysi guruhga rasm qo'yamiz? Guruh nomini yuboring (masalan: UC). Guruhsiz mahsulotlar uchun <code>-</code>")
    if a == "pimg": return await ask(update, ctx, ("pimg", int(x)), "🖼 Mahsulot rasmini yuboring:")
    if a == "pgrp": return await ask(update, ctx, ("pgrp", int(x)), "📂 Guruh (tab) nomi, masalan: UC, Prime, Diamonds, RU. Tozalash: <code>-</code>")
    if a == "pbadge": return await ask(update, ctx, ("pbadge", int(x)), "🏷 Belgi matni, masalan: 2x, EP, HIT. Tozalash: <code>-</code>")
    if a == "gcat2":
        g = q1("select cat from games where id=?", (int(x),))
        ex("update games set cat=? where id=?", ("promo" if g["cat"] == "game" else "game", int(x)))
        return await S(v_game(int(x)))
    if a == "gtog":
        ex("update games set active=1-active where id=?", (int(x),)); return await S(v_game(int(x)))
    if a == "gdel":
        return await S(("⚠️ O'yin va uning barcha mahsulotlari o'chiriladi. Ishonchingiz komilmi?",
                        [[("✅ Ha, o'chirish", f"a:gdel2:{x}"), ("❌ Yo'q", f"a:g:{x}")]]))
    if a == "gdel2":
        ex("delete from products where game_id=?", (int(x),)); ex("delete from games where id=?", (int(x),))
        return await S(v_games())
    if a == "padd": return await ask(update, ctx, ("padd", int(x)),
        "➕ Mahsulot(lar)ni yuboring. Har qatorda: <code>nom | narx | guruh | belgi</code> (guruh va belgi ixtiyoriy)\nMasalan:\n<code>60 UC | 11700 | UC\n325 UC | 59000 | UC\nPrime | 12000 | Prime | HIT</code>")
    if a == "p": return await S(v_prod(int(x)))
    if a == "pname": return await ask(update, ctx, ("pname", int(x)), "✏️ Yangi mahsulot nomi:")
    if a == "pprice": return await ask(update, ctx, ("pprice", int(x)), "💰 Yangi narx (so'm):")
    if a == "ptog":
        ex("update products set active=1-active where id=?", (int(x),)); return await S(v_prod(int(x)))
    if a == "pdel":
        p = q1("select game_id from products where id=?", (int(x),)); ex("delete from products where id=?", (int(x),))
        return await S(v_game(p["game_id"]) if p else v_games())
    # banners
    if a == "banners": return await S(v_banners())
    if a == "badd": return await ask(update, ctx, ("badd",), "🖼 Banner rasmini yuboring. Ixtiyoriy: izohga havola (https://...) yozsangiz, bosilganda ochiladi.")
    if a == "bdel": ex("delete from banners where id=?", (int(x),)); return await S(v_banners())
    # cards
    if a == "cards": return await S(v_cards())
    if a == "cadd": return await ask(update, ctx, ("cadd",), "💳 Format: <code>karta raqami | Ism Familiya | Bank</code>\nMasalan: <code>8600123412341234 | Ali Valiyev | UZCARD</code>")
    if a == "c": return await S(v_card(int(x)))
    if a == "cedit": return await ask(update, ctx, ("cedit", int(x)), "✏️ Yangi ma'lumot: <code>karta raqami | Ism Familiya | Bank</code>")
    if a == "ctog": ex("update cards set active=1-active where id=?", (int(x),)); return await S(v_card(int(x)))
    if a == "cdel": ex("delete from cards where id=?", (int(x),)); return await S(v_cards())
    # topups / orders
    if a == "tops": return await S(v_tops())
    if a == "t": return await S(v_top(int(x)))
    if a == "ords": return await S(v_ords())
    if a == "o": return await S(v_ord(int(x)))
    # promos
    if a == "promos": return await S(v_promos())
    if a == "pradd": return await ask(update, ctx, ("pradd",), "🎟 Format: <code>KOD | summa | necha kishi ishlata oladi</code>\nMasalan: <code>FREE5000 | 5000 | 100</code>")
    if a == "prdel": ex("delete from promos where code=?", (x,)); return await S(v_promos())
    # channels
    if a == "chs": return await S(v_chs())
    if a == "chadd": return await ask(update, ctx, ("chadd",), "📢 Kanal @username yoki ID yuboring (bot u yerda admin bo'lsin).\nMasalan: <code>@mychannel</code>")
    if a == "chdel": ex("delete from channels where id=?", (int(x),)); return await S(v_chs())
    # broadcast
    if a == "bc": return await ask(update, ctx, ("bc",), "📨 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring (matn, rasm, video — istalgan):")
    if a == "bcgo":
        st = ctx.user_data.pop("bcmsg", None)
        if not st: return await S(v_home())
        await q.edit_message_text("⏳ Yuborilmoqda...")
        ok = bad = 0
        for u in qa("select id from users where banned=0"):
            try:
                await ctx.bot.copy_message(u["id"], st[0], st[1]); ok += 1
            except Exception: bad += 1
            await asyncio.sleep(0.05)
        return await q.message.reply_text(f"✅ Yuborildi: {ok}\n❌ Xato: {bad}", reply_markup=AK([BACK]))
    # settings
    if a == "set": return await S(v_set())
    if a == "s":
        if x == "maintenance":
            ss("maintenance", "0" if gs("maintenance") == "1" else "1"); return await S(v_set())
        lbl = dict(SET_KEYS).get(x, x)
        extra = "\n(rasm yuboring)" if x == "welcome_img" else ""
        return await ask(update, ctx, ("set", x), f"✏️ <b>{lbl}</b>\nHozirgi: <code>{E((gs(x) or '-')[:300])}</code>{extra}\nYangi qiymatni yuboring:")
    # admins
    if a == "adms": return await S(v_adms())
    if a == "amadd": return await ask(update, ctx, ("amadd",), "👮 Yangi admin Telegram ID sini yuboring:")
    if a == "amdel":
        if int(x) not in OWNERS: ex("delete from admins where id=?", (int(x),))
        return await S(v_adms())

async def on_msg(update, ctx):
    u = update.effective_user; m = update.message
    if not u or not m or not is_admin(u.id): return
    st = ctx.user_data.get("st")
    if not st: return
    k = st[0]; txt = (m.text or m.caption or "").strip()
    photo = m.photo[-1].file_id if m.photo else None
    done = lambda: ctx.user_data.pop("st", None)
    num = lambda s: int(re.sub(r"\D", "", s) or 0)
    try:
        if k == "find":
            s = txt.lstrip("@")
            r = q1("select id from users where id=?", (int(s),)) if s.isdigit() else q1("select id from users where lower(username)=?", (s.lower(),))
            if not r: return await m.reply_text("❌ Topilmadi. Qayta yuboring yoki /cancel")
            done(); return await show(update, v_user(r["id"]))
        if k == "bal":
            amt = num(txt)
            if amt <= 0: return await m.reply_text("Musbat son yuboring")
            _, uid, sign = st
            ex("update users set balance=max(0,balance+?) where id=?", (amt if sign == "+" else -amt, uid))
            notify(uid, f"{'➕' if sign=='+' else '➖'} Balansingiz o'zgardi: <b>{money(amt)}</b> so'm")
            done(); return await show(update, v_user(uid))
        if k == "gnew":
            if not txt: return await m.reply_text("Nom yuboring")
            ctx.user_data["gname"] = txt; done()
            return await show(update, (f"«{E(txt)}» uchun kategoriya:", [[("🎮 O'yinlar", "a:gcat:game"), ("🎁 Promokodlar bo'limi", "a:gcat:promo")]]))
        if k == "gname":
            ex("update games set name=? where id=?", (txt, st[1])); done(); return await show(update, v_game(st[1]))
        if k == "gfield":
            ex("update games set field=? where id=?", (txt, st[1])); done(); return await show(update, v_game(st[1]))
        if k == "gimg":
            if not photo: return await m.reply_text("Rasm yuboring (fayl emas, rasm sifatida)")
            ex("update games set img=? where id=?", (photo, st[1])); done(); return await show(update, v_game(st[1]))
        if k in ("ghero", "gpicon", "pimg"):
            if not photo: return await m.reply_text("Rasm yuboring (fayl emas, rasm sifatida)")
            if k == "pimg":
                ex("update products set img=? where id=?", (photo, st[1])); done(); return await show(update, v_prod(st[1]))
            ex(f"update games set {'hero' if k=='ghero' else 'picon'}=? where id=?", (photo, st[1])); done(); return await show(update, v_game(st[1]))
        if k == "ginfo":
            ex("update games set info=? where id=?", ("" if txt == "-" else txt, st[1])); done(); return await show(update, v_game(st[1]))
        if k in ("pgrp", "pbadge"):
            ex(f"update products set {'grp' if k=='pgrp' else 'badge'}=? where id=?", ("" if txt == "-" else txt, st[1])); done(); return await show(update, v_prod(st[1]))
        if k == "pgname":
            return await ask(update, ctx, ("pgimg", st[1], "" if txt == "-" else txt), f"🖼 «{E(txt)}» guruhidagi barcha mahsulotlar uchun rasm yuboring:")
        if k == "pgimg":
            if not photo: return await m.reply_text("Rasm yuboring")
            ex("update products set img=? where game_id=? and grp=?", (photo, st[1], st[2])); done(); return await show(update, v_game(st[1]))
        if k == "padd":
            n = 0
            for line in txt.splitlines():
                pt = [x.strip() for x in line.split("|")]
                if len(pt) >= 2 and pt[0] and num(pt[1]) > 0:
                    ex("insert into products(game_id,name,price,grp,badge) values(?,?,?,?,?)",
                       (st[1], pt[0], num(pt[1]), pt[2] if len(pt) > 2 else "", pt[3] if len(pt) > 3 else "")); n += 1
            if not n: return await m.reply_text("Format xato. <code>nom | narx</code>", parse_mode="HTML")
            done(); return await show(update, v_game(st[1]))
        if k == "pname":
            ex("update products set name=? where id=?", (txt, st[1])); done(); return await show(update, v_prod(st[1]))
        if k == "pprice":
            if num(txt) <= 0: return await m.reply_text("Narx noto'g'ri")
            ex("update products set price=? where id=?", (num(txt), st[1])); done(); return await show(update, v_prod(st[1]))
        if k == "badd":
            if not photo: return await m.reply_text("Rasm yuboring")
            ex("insert into banners(img,link) values(?,?)", (photo, txt if link_ok(txt) else "")); done(); return await show(update, v_banners())
        if k in ("cadd", "cedit"):
            parts = [p.strip() for p in txt.split("|")]
            if len(parts) < 2 or len(re.sub(r"\D", "", parts[0])) < 12: return await m.reply_text("Format: karta | ism | bank")
            number = re.sub(r"\D", "", parts[0]); number = " ".join(number[i:i+4] for i in range(0, len(number), 4))
            bank = parts[2].upper() if len(parts) > 2 and parts[2] else "UZCARD"
            if k == "cadd": ex("insert into cards(number,holder,bank) values(?,?,?)", (number, parts[1], bank)); done(); return await show(update, v_cards())
            ex("update cards set number=?,holder=?,bank=? where id=?", (number, parts[1], bank, st[1])); done(); return await show(update, v_card(st[1]))
        if k == "pradd":
            parts = [p.strip() for p in txt.split("|")]
            if len(parts) < 3 or num(parts[1]) <= 0: return await m.reply_text("Format: KOD | summa | limit")
            ex("insert or replace into promos(code,amount,left) values(?,?,?)", (parts[0].upper(), num(parts[1]), num(parts[2]))); done(); return await show(update, v_promos())
        if k == "chadd":
            cid = txt.strip()
            r = tg("getChat", chat_id=cid)
            if not r.get("ok"): return await m.reply_text("❌ Kanal topilmadi yoki bot u yerda yo'q.")
            c = r["result"]; link = f"https://t.me/{c['username']}" if c.get("username") else c.get("invite_link", "")
            if not link:
                link = tg("exportChatInviteLink", chat_id=c["id"]).get("result", "")
            ex("insert into channels(chat_id,title,link) values(?,?,?)", (str(c["id"]), c.get("title", ""), link)); done(); return await show(update, v_chs())
        if k == "bc":
            ctx.user_data["bcmsg"] = (m.chat_id, m.message_id); done()
            n = q1("select count(*) c from users where banned=0")["c"]
            return await show(update, (f"📨 Shu xabar {n} ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?", [[("✅ Yuborish", "a:bcgo"), ("❌ Bekor", "a:home")]]))
        if k == "set":
            key = st[1]
            if key == "welcome_img":
                if not photo: return await m.reply_text("Rasm yuboring")
                ss(key, photo)
            else:
                ss(key, re.sub(r"\D", "", txt) if key in ("min_topup", "card_ttl") else txt)
            done(); return await show(update, v_set())
        if k == "amadd":
            if not txt.isdigit(): return await m.reply_text("ID raqam yuboring")
            ex("insert or ignore into admins values(?)", (int(txt),)); done(); return await show(update, v_adms())
    except Exception as e:
        log.exception("on_msg")
        await m.reply_text(f"Xato: {e}")

# ---- topup / order decisions (button in notification or panel)
async def cb_decide(update, ctx):
    q = update.callback_query
    if not is_admin(q.from_user.id): return await q.answer("⛔", show_alert=True)
    kind, act, sid = q.data.split(":"); sid = int(sid)
    res = "Allaqachon ko'rilgan"
    if kind == "t":
        t = q1("select * from topups where id=?", (sid,))
        if t:
            st = "approved" if act == "ok" else "rejected"
            c = ex("update topups set status=? where id=? and status in ('new','pending')", (st, sid))
            if c.rowcount:
                if act == "ok":
                    ex("update users set balance=balance+? where id=?", (t["amount"], t["uid"]))
                    notify(t["uid"], f"✅ Balansingiz <b>{money(t['amount'])}</b> so'mga to'ldirildi.")
                    res = f"✅ Tasdiqlandi (@{E(q.from_user.username or str(q.from_user.id))})"
                else:
                    notify(t["uid"], f"❌ {money(t['amount'])} so'm to'ldirish so'rovi rad etildi. Yordam: support.")
                    res = "❌ Rad etildi"
    else:
        o = q1("select * from orders where id=?", (sid,))
        if o:
            st = "done" if act == "done" else "canceled"
            c = ex("update orders set status=? where id=? and status='pending'", (st, sid))
            if c.rowcount:
                if act == "done":
                    notify(o["uid"], f"✅ Buyurtma #{sid} bajarildi!\n🎮 {E(o['game'])} — {E(o['product'])}")
                    res = "✅ Bajarildi"
                else:
                    ex("update users set balance=balance+? where id=?", (o["price"], o["uid"]))
                    notify(o["uid"], f"❌ Buyurtma #{sid} bekor qilindi, <b>{money(o['price'])}</b> so'm balansga qaytarildi.")
                    res = "❌ Bekor qilindi, pul qaytarildi"
    await q.answer(res, show_alert=False)
    try:
        await q.edit_message_text((q.message.text_html or "") + f"\n\n<b>{res}</b>", parse_mode="HTML")
    except Exception: pass

async def post_init(app):
    if BASE_URL:
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=webapp_url())))
        except Exception as e: log.warning("menu button: %s", e)

def keepalive():
    while True:
        time.sleep(600)
        try: requests.get(BASE_URL + "/health", timeout=15)
        except Exception: pass

def main():
    if not TOKEN: raise SystemExit("BOT_TOKEN kiritilmagan!")
    init_db()
    try: auto_restore()
    except Exception as e: log.warning("auto_restore: %s", e)
    threading.Thread(target=backup_loop, daemon=True).start()
    threading.Thread(target=lambda: web.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True), daemon=True).start()
    if BASE_URL: threading.Thread(target=keepalive, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("backup", cmd_backup))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_doc))
    app.add_handler(CallbackQueryHandler(cb_chk, pattern="^chk$"))
    app.add_handler(CallbackQueryHandler(adm_cb, pattern="^a:"))
    app.add_handler(CallbackQueryHandler(cb_decide, pattern="^[to]:"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_msg))
    log.info("Syrexa ishga tushdi. Admins: %s", all_admins())
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(drop_pending_updates=True)

# ============================ MINI APP (frontend) ============================
INDEX = r"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>__BOT__</title><script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#eef0f7;--card:#fff;--tx:#151a30;--mut:#8a8fa8;--p:#7c5cff;--p2:#a78bfa;--bd:#e3e6f2;--ok:#16a34a;--er:#e11d48}
body.dk{--bg:#0a0c18;--card:#151932;--tx:#f1f2fb;--mut:#8b90ab;--bd:#242949}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;font-size:15px}
#app{padding:14px 14px 100px;max-width:520px;margin:auto}
.row{display:flex;align-items:center;gap:10px}.sp{flex:1}.mut{color:var(--mut)}.sm{font-size:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:14px;margin-bottom:12px}
.btn{border:0;border-radius:14px;padding:13px 18px;font-weight:700;font-size:15px;color:#fff;background:linear-gradient(135deg,var(--p),var(--p2));width:100%;cursor:pointer}
.btn.sm{width:auto;padding:10px 16px;font-size:13px}.btn.g{background:linear-gradient(135deg,#16a34a,#22c55e)}.btn.o{background:var(--card);color:var(--tx);border:1px solid var(--bd)}
.btn:disabled{opacity:.5}
.ib{width:38px;height:38px;border-radius:12px;background:var(--card);border:1px solid var(--bd);display:flex;align-items:center;justify-content:center;font-weight:700;cursor:pointer;font-size:13px}
.av{width:44px;height:44px;border-radius:50%;background:linear-gradient(135deg,var(--p),var(--p2));display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:18px}
.bal{display:flex;align-items:center;gap:12px}.bal b{font-size:26px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px 8px}
.gc{text-align:center;font-size:11px;cursor:pointer}.gi{aspect-ratio:1;border-radius:16px;overflow:hidden;background:var(--card);border:1px solid var(--bd);margin-bottom:5px}
.gi img{width:100%;height:100%;object-fit:cover}.ph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:800;color:#fff;background:linear-gradient(135deg,var(--p),var(--p2))}
.ban{display:flex;gap:10px;overflow-x:auto;scroll-snap-type:x mandatory;margin-bottom:12px;border-radius:18px}
.ban>*{flex:0 0 100%;scroll-snap-align:center;border-radius:18px;overflow:hidden;aspect-ratio:2.1;background:linear-gradient(135deg,#1b1147,#6d3df0)}
.ban img{width:100%;height:100%;object-fit:cover;display:block}
.hero{display:flex;align-items:center;justify-content:center;color:#fff;font-size:30px;font-weight:900;letter-spacing:2px}
h3{margin:6px 2px 10px;font-size:17px}.hd{display:flex;justify-content:space-between;align-items:center}.hd a{color:var(--p);font-weight:700;font-size:13px}
.seg{display:flex;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:4px;margin-bottom:12px}
.seg div{flex:1;text-align:center;padding:10px;border-radius:11px;font-weight:700;font-size:13px;cursor:pointer;color:var(--mut)}.seg .on{background:linear-gradient(135deg,var(--p),var(--p2));color:#fff}
input{width:100%;padding:14px;border-radius:14px;border:1.5px solid var(--bd);background:var(--card);color:var(--tx);font-size:16px;outline:none}input:focus{border-color:var(--p)}
.chips{display:flex;gap:8px;margin:10px 0}.chips div{flex:1;text-align:center;padding:10px 0;border-radius:12px;border:1px solid var(--bd);background:var(--card);font-weight:700;font-size:13px;cursor:pointer}.chips .on{border-color:var(--p);color:var(--p)}
.nav{position:fixed;left:50%;transform:translateX(-50%);bottom:12px;width:calc(100% - 24px);max-width:496px;background:var(--card);border:1px solid var(--bd);border-radius:26px;display:flex;padding:6px;box-shadow:0 8px 30px rgba(0,0,0,.18)}
.nav div{flex:1;text-align:center;padding:8px 0;border-radius:20px;font-size:10px;color:var(--mut);cursor:pointer}.nav i{display:block;font-style:normal;font-size:19px}.nav .on{background:linear-gradient(135deg,var(--p),var(--p2));color:#fff}
.prod{display:flex;justify-content:space-between;align-items:center;padding:14px;border-radius:14px;border:1.5px solid var(--bd);background:var(--card);margin-bottom:8px;cursor:pointer;font-weight:600}.prod.on{border-color:var(--p);background:rgba(124,92,255,.1)}
.tag{font-size:11px;padding:3px 9px;border-radius:20px;font-weight:700}.pending,.new{background:#fff3cd;color:#a16207}.done,.approved{background:#dcfce7;color:#15803d}.canceled,.rejected{background:#ffe4e6;color:#be123c}
.cn{background:linear-gradient(135deg,#10132a,#2a2170);color:#fff;border-radius:18px;padding:16px;margin-bottom:12px}.cn .n{font-size:21px;font-weight:800;letter-spacing:1px;margin:8px 0}
.warn{background:rgba(225,29,72,.08);border:1px solid rgba(225,29,72,.3);border-radius:14px;padding:12px;font-size:13px;margin-bottom:12px}
.tm{font-weight:800;color:var(--p)}.bar{height:5px;border-radius:5px;background:var(--bd);overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--p),var(--p2))}
#toast{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:#151a30;color:#fff;padding:11px 18px;border-radius:14px;font-size:14px;z-index:9;display:none;max-width:90%}
.empty{text-align:center;padding:50px 10px;color:var(--mut)}
.gv{--bg:#0a0c18;--card:#151932;--tx:#f1f2fb;--mut:#8b90ab;--bd:#242949;background:#0a0c18;color:var(--tx);margin:-14px -14px 0;padding-bottom:100px;min-height:100vh}
.hero2{height:200px;background:linear-gradient(135deg,#1b1147,#6d3df0);background-size:cover;background-position:center;position:relative;display:flex;align-items:flex-end;padding:16px}
.hero2 .hs{position:absolute;inset:0;background:linear-gradient(transparent 35%,#0a0c18)}.hero2 h2{position:relative;margin:0;font-size:24px;font-weight:800}
.info{display:flex;justify-content:space-between;align-items:center;margin:12px 14px;padding:13px 14px;border-radius:14px;background:var(--card);border:1px solid var(--bd);font-weight:700;font-size:14px}
.tabs{display:flex;gap:8px;flex-wrap:wrap;padding:4px 14px 0}.tabs span{padding:8px 14px;border-radius:20px;background:var(--card);border:1px solid var(--bd);font-size:12px;font-weight:700;cursor:pointer}
.tabs .on{background:linear-gradient(135deg,var(--p),var(--p2));border-color:transparent;color:#fff}.gt{margin:16px 14px 10px;font-weight:700}
.pg{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 14px}.pc{display:flex;align-items:center;gap:10px;padding:12px;border-radius:14px;background:var(--card);border:1px solid var(--bd);cursor:pointer;min-height:64px}
.pc img,.pi{width:44px;height:44px;border-radius:10px;object-fit:cover;flex:none}.pi{background:linear-gradient(135deg,var(--p),var(--p2));display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff}
.pt{display:flex;flex-direction:column;gap:3px;font-size:12px;min-width:0}.pt b{font-size:13px}.pt span{font-weight:800}.pt small{color:var(--mut);font-weight:400}.pt em{font-style:normal;background:#f59e0b;color:#fff;border-radius:6px;padding:1px 6px;font-size:10px}
.ov{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:8;display:flex;align-items:flex-end;justify-content:center}
.sh{width:100%;max-width:520px;background:#12152b;color:#f1f2fb;border-radius:24px 24px 0 0;padding:22px 16px 26px;text-align:center;--card:#1a1e3a;--bd:#2a2f52;--tx:#f1f2fb;--mut:#8b90ab}
.sh .ic{font-size:34px;width:64px;height:64px;border-radius:18px;background:rgba(225,29,72,.15);margin:0 auto 10px;display:flex;align-items:center;justify-content:center}
.tbl{background:#1a1e3a;border:1px solid #2a2f52;border-radius:14px;margin:14px 0;text-align:left}.tbl div{display:flex;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #2a2f52;font-size:13px}.tbl div:last-child{border:0}.tbl .er{color:#fb7185}
.tl{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}.tile{position:relative;background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:16px 12px;font-weight:700;font-size:13px;cursor:pointer}.tile i{display:block;font-style:normal;font-size:26px;margin-bottom:6px}.bd{position:absolute;top:8px;right:8px;background:var(--er);color:#fff;border-radius:10px;font-size:11px;padding:1px 7px}
.fm{width:calc(100% - 24px);max-width:480px;max-height:88vh;overflow:auto;background:var(--card);color:var(--tx);border-radius:20px;padding:16px}.fl{font-size:12px;color:var(--mut);margin:12px 0 5px;font-weight:600}
textarea,select{width:100%;padding:12px;border-radius:14px;border:1.5px solid var(--bd);background:var(--card);color:var(--tx);font-size:15px;font-family:inherit}textarea{min-height:90px}
.ip{display:flex;align-items:center;gap:12px}.ip img,.ip span{width:84px;height:62px;border-radius:12px;object-fit:cover;background:var(--bg);display:flex;align-items:center;justify-content:center;font-size:24px;flex:none}
.li{display:flex;align-items:center;gap:10px;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:10px;margin-bottom:8px;cursor:pointer}.li img,.li .pi{width:44px;height:44px;border-radius:10px;object-fit:cover;flex:none}.li .sp{min-width:0}
.hero2 .ed{position:absolute;top:12px;right:12px;background:rgba(0,0,0,.55);border-radius:10px;padding:6px 10px;font-size:13px;color:#fff}
</style></head><body><div id="toast"></div><div id="app"></div><div id="modal"></div>
<script>
const tg=window.Telegram.WebApp;tg.ready();tg.expand();
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=n=>Number(n).toLocaleString('ru-RU').replace(/\u00a0/g,' ');
const S={lang:localStorage.lang||'uz',dark:localStorage.dark==='1',tab:'home',d:null,amt:50000,seg:'game',oseg:'o',q:'',player:'',pid:null};
const T={uz:{hi:'Salom',bal:'BALANS',top:'To\'ldirish',promo:'Promokodlar',sup:'Yordam',pop:'Mashhur o\'yinlar',all:'Barchasi',home:'Asosiy',games:'O\'yinlar',orders:'Buyurtmalar',prof:'Profil',tx:'Tranzaksiyalar',search:'O\'yin yoki xizmatni qidiring',
amount:'Summani kiriting',min:'Minimum',steps:'To\'ldirish qadamlari',s1:'To\'lov summasini tanlang',s2:'Ko\'rsatilgan kartaga AYNAN shu summani o\'tkazing',s3:'"Men to\'ladim" tugmasini bosing',s4:'Admin tasdiqlagach balansingiz to\'ldiriladi',
exact:'Aynan shu summani o\'tkazing',one:'Faqat BITTA o\'tkazma',onet:'Summani bo\'lmang va yaxlitlamang.',valid:'Karta amal qilish vaqti',card:'Karta raqami',copy:'Nusxalash',copied:'Nusxalandi',paid:'Men to\'ladim',rules:'To\'lov qoidalari',r1:'Summani 1 so\'mga ham o\'zgartirmang',r2:'Vaqt ichida to\'lang',r3:'Boshqa summa yubormang',r4:'Summani ikkiga bo\'lmang',
buy:'Sotib olish',pid_:'ID kiriting',pick:'Mahsulotni tanlang',noprod:'Mahsulotlar hali qo\'shilmagan',noord:'Buyurtmalar yo\'q',notx:'Tranzaksiyalar yo\'q',nobal:'Balans yetarli emas',ok:'Muvaffaqiyatli!',
pending:'Kutilmoqda',done:'Bajarildi',canceled:'Bekor',approved:'Tasdiqlandi',rejected:'Rad etildi',new:'Yangi',pr_in:'PROMOKOD',act:'Faollashtirish',lang:'Til',sub:'Botdan foydalanish uchun kanalga obuna bo\'ling',chk:'Tekshirish',nocard:'Hozircha to\'lov usuli mavjud emas',err:'Xatolik',bad:'Kod topilmadi yoki ishlatilgan',added:'Qo\'shildi',expired:'Vaqt tugadi',maint:'Texnik ishlar',confirm:'Tasdiqlaysizmi?',adm:'Admin panel uchun botda /admin yozing',bonus:'Balans to\'ldirildi'},
ru:{hi:'Привет',bal:'БАЛАНС',top:'Пополнить',promo:'Промокоды',sup:'Поддержка',pop:'Популярные игры',all:'Все',home:'Главная',games:'Игры',orders:'Заказы',prof:'Профиль',tx:'Транзакции',search:'Поиск игры или услуги',
amount:'Введите сумму',min:'Минимум',steps:'Шаги пополнения',s1:'Выберите сумму',s2:'Переведите на указанную карту ТОЧНО эту сумму',s3:'Нажмите «Я оплатил»',s4:'После подтверждения баланс пополнится',
exact:'Переведите ровно',one:'Только ОДИН перевод',onet:'Не разбивайте и не округляйте сумму.',valid:'Карта действует',card:'Номер карты',copy:'Копировать',copied:'Скопировано',paid:'Я оплатил',rules:'Правила оплаты',r1:'Не меняйте сумму даже на 1 сум',r2:'Оплатите в течение времени',r3:'Не отправляйте другую сумму',r4:'Не разбивайте сумму на два перевода',
buy:'Купить',pid_:'Введите ID',pick:'Выберите товар',noprod:'Товары ещё не добавлены',noord:'Нет заказов',notx:'Нет транзакций',nobal:'Недостаточно средств',ok:'Успешно!',
pending:'Ожидание',done:'Выполнен',canceled:'Отменён',approved:'Подтверждён',rejected:'Отклонён',new:'Новый',pr_in:'ПРОМОКОД',act:'Активировать',lang:'Язык',sub:'Подпишитесь на канал, чтобы пользоваться ботом',chk:'Проверить',nocard:'Способ оплаты пока недоступен',err:'Ошибка',bad:'Код не найден или использован',added:'Добавлено',expired:'Время истекло',maint:'Технические работы',confirm:'Подтвердить?',adm:'Для админ-панели напишите боту /admin',bonus:'Баланс пополнен'}};
Object.assign(T.uz,{nobal2:'Bu xarid uchun balansda mablag\' yetarli emas. Avval balansni to\'ldiring.',price:'Mahsulot narxi',short:'Yetmaydi',close:'Yopish',topbal:'Balansni to\'ldirish'});
Object.assign(T.ru,{nobal2:'На балансе недостаточно средств для этой покупки. Сначала пополните баланс.',price:'Цена товара',short:'Не хватает',close:'Закрыть',topbal:'Пополнение баланса'});
const t=k=>(T[S.lang]||T.uz)[k]||k;
function toast(m){const e=$('#toast');e.textContent=m;e.style.display='block';clearTimeout(S.tt);S.tt=setTimeout(()=>e.style.display='none',2600)}
async function api(p,body){const r=await fetch(p,{method:body!==undefined?'POST':'GET',headers:{'Content-Type':'application/json','X-Init':tg.initData},body:body!==undefined?JSON.stringify(body):undefined});const j=await r.json().catch(()=>({}));if(!r.ok)throw j;return j}
function ask(m,cb){tg.showConfirm?tg.showConfirm(m,ok=>ok&&cb()):(confirm(m)&&cb())}
function gimg(g,cls){return g.img?`<img src="/img/${g.img}" loading="lazy">`:`<div class="ph">${esc(g.name[0])}</div>`}
function gcard(g){return `<div class="gc" onclick="openGame(${g.id})"><div class="gi">${gimg(g)}</div>${esc(g.name)}</div>`}
function go(tab,arg){S.tab=tab;S.arg=arg;clearInterval(S.tm);if(tab!='game')S.g=null;S.sheet=false;document.body.style.background=tab=='game'?'#0a0c18':'';
 const back=(tab=='game'||tab=='pay');back?tg.BackButton.show():tg.BackButton.hide();render();window.scrollTo(0,0)}
tg.BackButton.onClick(()=>back());
function head(){const u=S.d.user;return `<div class="row" style="margin-bottom:12px"><div class="av">${esc((u.name||'?')[0])}</div><div><div class="mut sm">${t('hi')} 👋</div><b>${esc(u.name)}</b></div><div class="sp"></div>${S.d.user.admin?`<div class="ib" onclick="admGo('adm')">🛠</div>`:''}<div class="ib" onclick="setLang()">${S.lang.toUpperCase()}</div><div class="ib" onclick="setDark()">${S.dark?'☀️':'🌙'}</div></div>`}
function balCard(){return `<div class="card bal"><div style="font-size:26px">💳</div><div class="sp"><div class="mut sm">${t('bal')}</div><b>${money(S.d.user.balance)}</b> <span class="mut sm">so'm</span></div><button class="btn sm" onclick="go('topup')">+ ${t('top')}</button></div>`}
function nav(){const a=[['home','🏠',t('home')],['games','🎮',t('games')],['topup','👛',t('top')],['orders','🕘',t('orders')],['prof','👤',t('prof')]];
 return `<div class="nav">${a.map(x=>`<div class="${S.tab==x[0]?'on':''}" onclick="go('${x[0]}')"><i>${x[1]}</i>${x[2]}</div>`).join('')}</div>`}
function render(){const A=$('#app');const d=S.d;if(!d)return;if(S.tab.startsWith('adm')){A.innerHTML=vAdm();return}
 if(d.sub&&d.sub.length){A.innerHTML=`<div class="card" style="margin-top:40px;text-align:center"><div style="font-size:42px">📢</div><p>${t('sub')}</p>${d.sub.map(c=>`<button class="btn o" style="margin-bottom:8px" onclick="tg.openTelegramLink('${esc(c.link)}')">${esc(c.title)}</button>`).join('')}<button class="btn" onclick="boot()">${t('chk')}</button></div>`;return}
 let h='';const m=S.tab;
 if(m=='home')h=vHome();else if(m=='games')h=vGames();else if(m=='game')h=vGame();else if(m=='topup')h=vTopup();else if(m=='pay')h=vPay();else if(m=='orders')h=vOrders();else h=vProf();
 A.innerHTML=h+((m=='game'||m=='pay')?'':nav())+((m=='game'&&S.sheet&&S.sel)?sheet():'');
 if(m=='pay')tick();}
function vHome(){const d=S.d;
 const bn=d.banners.length?d.banners.map(b=>`<div onclick="${b.link?`tg.openLink('${esc(b.link)}')`:''}"><img src="/img/${b.img}"></div>`).join(''):`<div class="hero">${esc(d.cfg.bot)}</div>`;
 return head()+balCard()+`<div class="row" style="margin-bottom:12px"><button class="btn o" onclick="go('prof')">🎟 ${t('promo')}</button><button class="btn o" onclick="sup()">🎧 ${t('sup')}</button></div><div class="ban">${bn}</div>
 <div class="hd"><h3>${t('pop')}</h3><a onclick="go('games')">${t('all')}</a></div><div class="grid">${d.games.filter(g=>g.cat=='game').slice(0,8).map(gcard).join('')}</div>`}
function sup(){const l=S.d.cfg.support;l?tg.openTelegramLink(l):toast(t('sup'))}
function vGames(){const L=S.d.games.filter(g=>g.cat==S.seg&&g.name.toLowerCase().includes(S.q.toLowerCase()));
 return `<h3>${t('games')}</h3><input id="sq" placeholder="🔍 ${t('search')}" value="${esc(S.q)}" oninput="S.q=this.value;gridUpd()" style="margin-bottom:12px"><div class="seg"><div class="${S.seg=='game'?'on':''}" onclick="S.seg='game';render()">${t('games')}</div><div class="${S.seg=='promo'?'on':''}" onclick="S.seg='promo';render()">${t('promo')}</div></div><div class="grid" id="gg">${L.map(gcard).join('')}</div>`}
function gridUpd(){const L=S.d.games.filter(g=>g.cat==S.seg&&g.name.toLowerCase().includes(S.q.toLowerCase()));$('#gg').innerHTML=L.map(gcard).join('')}
async function openGame(id){S.g=null;S.sel=null;S.grp=null;go('game',id);try{S.g=await api('/api/game/'+id);render()}catch(e){toast(t('err'));go('games')}}
function infoRow(i){return `<div class="info" onclick="${i.link?`tg.openLink('${esc(i.link)}')`:''}"><span>${esc(i.text)}</span><em style="font-style:normal">›</em></div>`}
function vGame(){const g=S.g;if(!g)return `<div class="empty">⏳</div>`;
 const grps=[...new Set(g.products.map(p=>p.grp||''))];if(S.grp==null||!grps.includes(S.grp))S.grp=grps[0]||'';
 const L=g.products.filter(p=>(p.grp||'')==S.grp),hi=g.hero||g.img;
 return `<div class="gv"><div class="hero2" style="${hi?`background-image:url(/img/${hi})`:''}"><div class="hs"></div><h2>${esc(g.name)}</h2></div>${g.info?infoRow(g.info):''}
 ${(grps.length>1||grps[0])?`<div class="tabs">${grps.map(x=>`<span class="${x==S.grp?'on':''}" data-g="${esc(x)}" onclick="S.grp=this.dataset.g;render()">${esc(x)}</span>`).join('')}</div>`:''}
 <div class="gt">${t('pick')}</div><div class="pg">${L.length?L.map(p=>`<div class="pc" onclick="selP(${p.id})">${p.img?`<img src="/img/${p.img}" loading="lazy">`:`<div class="pi">${esc(g.name[0])}</div>`}<div class="pt"><b>${esc(p.name)}${p.badge?` <em>${esc(p.badge)}</em>`:''}</b><span>${money(p.price)} <small>so'm</small></span></div></div>`).join(''):`<div class="empty" style="grid-column:1/3">${t('noprod')}</div>`}</div></div>`}
function selP(id){S.sel=S.g.products.find(x=>x.id==id);S.sheet=true;render()}
function closeSheet(){S.sheet=false;render()}
function sheet(){const p=S.sel,bal=S.d.user.balance,sh=p.price-bal;
 return `<div class="ov" onclick="closeSheet()"><div class="sh" onclick="event.stopPropagation()">`+(sh>0?
 `<div class="ic">👛</div><h3 style="margin:0">${t('nobal')}</h3><p class="mut sm">${t('nobal2')}</p><div class="tbl"><div><span class="mut">${t('price')}</span><b>${money(p.price)} so'm</b></div><div><span class="mut">${t('bal')}</span><b>${money(bal)} so'm</b></div><div class="er"><span>${t('short')}</span><b>${money(sh)} so'm</b></div></div><div class="row"><button class="btn o" onclick="closeSheet()">${t('close')}</button><button class="btn" onclick="S.sheet=false;go('topup')">+ ${t('topbal')}</button></div>`
 :`<h3 style="margin:0 0 4px">${esc(S.g.name)}</h3><div class="mut sm">${esc(p.name)}</div><input id="pl" placeholder="${esc(S.g.field)}" value="${esc(S.player)}" oninput="S.player=this.value" style="margin:14px 0 0"><div class="tbl"><div><span class="mut">${t('price')}</span><b>${money(p.price)} so'm</b></div><div><span class="mut">${t('bal')}</span><b>${money(bal)} so'm</b></div></div><div class="row"><button class="btn o" onclick="closeSheet()">${t('close')}</button><button class="btn" onclick="buy()">${t('buy')}</button></div>`)+`</div></div>`}
async function buy(){const g=S.g,p=S.sel;if(!p)return;if((S.player||'').trim().length<2)return toast(t('pid_'));
 ask(`${g.name} — ${p.name}\n${money(p.price)} so'm\n${g.field}: ${S.player}`,async()=>{try{const r=await api('/api/order',{product_id:p.id,player:S.player.trim()});S.d.user.balance=r.balance;S.sheet=false;toast('✅ '+t('ok'));S.oseg='o';go('orders')}catch(e){if(e.err=='balance'){toast(t('nobal'));render()}else toast(t('err'))}})}
function vTopup(){const A=[10000,50000,100000,200000,500000];
 return `<h3>${t('top')}</h3>`+balCard().replace(/<button.*<\/button>/,'')+`<div class="card"><div class="mut sm">${t('amount')}</div><input id="am" inputmode="numeric" value="${money(S.amt)}" oninput="S.amt=+this.value.replace(/\\D/g,'')||0"><div class="chips">${[50000,100000,200000,500000].map(a=>`<div class="${S.amt==a?'on':''}" onclick="S.amt=${a};render()">${a/1000}k</div>`).join('')}</div><div class="mut sm">${t('min')}: ${money(S.d.cfg.min)}</div></div>
 <div class="card"><b>${t('steps')}</b>${[1,2,3,4].map(i=>`<div class="row" style="margin-top:10px"><div class="ib" style="width:26px;height:26px;border-radius:50%;font-size:12px">${i}</div><div class="sm">${t('s'+i)}</div></div>`).join('')}</div><button class="btn" onclick="mkTop()">${t('top')}</button>`}
async function mkTop(){if(S.amt<S.d.cfg.min)return toast(t('min')+': '+money(S.d.cfg.min));try{S.pay=await api('/api/topup',{amount:S.amt});S.pay.t0=Date.now();go('pay')}catch(e){toast(e.err=='nocard'?t('nocard'):t('err'))}}
function vPay(){const p=S.pay;return `<h3>${t('top')}</h3><div class="card" style="text-align:center;background:rgba(124,92,255,.1)"><div class="mut sm">${t('exact')}</div><div style="font-size:30px;font-weight:900;margin:6px 0" onclick="cp('${p.amount}')">${money(p.amount)} so'm ⧉</div></div>
 <div class="warn"><b>⚠️ ${t('one')}</b><br>${t('onet')}</div>
 <div class="card"><div class="row"><span class="mut sm">${t('valid')}</span><div class="sp"></div><span class="tm" id="tm"></span></div><div class="bar"><i id="br" style="width:100%"></i></div></div>
 <div class="cn"><div class="sm" style="opacity:.7">${t('card')} · ${esc(p.card.bank)}</div><div class="n" onclick="cp('${p.card.number.replace(/\\s/g,'')}')">${esc(p.card.number)}</div><div class="sm">${esc(p.card.holder)}</div><button class="btn o" style="margin-top:12px;background:rgba(255,255,255,.12);color:#fff;border:0" onclick="cp('${p.card.number.replace(/\\s/g,'')}')">⧉ ${t('copy')}</button></div>
 <div class="card"><b>📋 ${t('rules')}</b>${['✅ r1','✅ r2','❌ r3','❌ r4'].map(x=>{const a=x.split(' ');return `<div class="sm" style="margin-top:8px">${a[0]} ${t(a[1])}</div>`}).join('')}</div><button class="btn g" onclick="paid()">${t('paid')}</button>`}
function tick(){const p=S.pay;const f=()=>{const left=Math.max(0,p.ttl-Math.floor((Date.now()-p.t0)/1000));const e=$('#tm');if(!e)return clearInterval(S.tm);e.textContent=left?`${Math.floor(left/60)}:${String(left%60).padStart(2,'0')}`:t('expired');$('#br').style.width=(left/p.ttl*100)+'%'};f();S.tm=setInterval(f,1000)}
function cp(x){(navigator.clipboard?navigator.clipboard.writeText(x):Promise.reject()).catch(()=>{const a=document.createElement('textarea');a.value=x;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove()});toast('✅ '+t('copied'));tg.HapticFeedback&&tg.HapticFeedback.impactOccurred('light')}
async function paid(){try{await api('/api/topup/'+S.pay.id+'/paid',{});toast('✅ '+t('ok'));S.oseg='t';go('orders')}catch(e){toast(t('err'))}}
function vOrders(){setTimeout(loadH,0);const H=S.h;
 let body=!H?`<div class="empty">⏳</div>`:S.oseg=='o'?(H.orders.length?H.orders.map(o=>`<div class="card row"><div class="sp"><b>${esc(o.game)}</b><div class="mut sm">${esc(o.product)} · #${o.id}</div><div class="mut sm">${ts(o.created)}</div></div><div style="text-align:right"><b>${money(o.price)}</b><div><span class="tag ${o.status}">${t(o.status)}</span></div></div></div>`).join(''):`<div class="empty">🕘<br>${t('noord')}</div>`)
 :(H.tx.length?H.tx.map(o=>`<div class="card row"><div class="sp"><b>${t('top')}</b><div class="mut sm">#${o.id} · ${ts(o.created)}</div></div><div style="text-align:right"><b>+${money(o.amount)}</b><div><span class="tag ${o.status}">${t(o.status)}</span></div></div></div>`).join(''):`<div class="empty">💳<br>${t('notx')}</div>`);
 return `<h3>${t('orders')}</h3><div class="seg"><div class="${S.oseg=='o'?'on':''}" onclick="S.oseg='o';render()">${t('orders')}</div><div class="${S.oseg=='t'?'on':''}" onclick="S.oseg='t';render()">${t('tx')}</div></div>`+body}
function ts(x){const d=new Date((x+18000)*1000);const p=n=>String(n).padStart(2,'0');return `${p(d.getUTCDate())}.${p(d.getUTCMonth()+1)} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`}
async function loadH(){if(S.hl)return;S.hl=1;try{S.h=await api('/api/history');const u=await api('/api/init');S.d.user=u.user;S.hl=0;if(S.tab=='orders')render()}catch(e){S.hl=0}}
function vProf(){const u=S.d.user;return `<div class="card" style="text-align:center"><div class="av" style="margin:auto;width:70px;height:70px;font-size:30px">${esc((u.name||'?')[0])}</div><h3 style="margin:10px 0 2px">${esc(u.name)}</h3><div class="mut sm">${u.username?'@'+esc(u.username)+' · ':''}ID: ${u.id}</div></div>`+balCard()+
 `<div class="card"><b>🎟 ${t('promo')}</b><input id="pc" placeholder="${t('pr_in')}" style="margin:10px 0;text-transform:uppercase"><button class="btn" onclick="actPromo()">${t('act')}</button></div>
 <div class="card"><b>🌐 ${t('lang')}</b><div class="seg" style="margin:10px 0 0"><div class="${S.lang=='uz'?'on':''}" onclick="setLang('uz')">O'zbekcha</div><div class="${S.lang=='ru'?'on':''}" onclick="setLang('ru')">Русский</div></div></div>${u.admin?`<button class="btn" onclick="admGo('adm')">🛠 Admin panel</button>`:''}`}
async function actPromo(){const c=$('#pc').value.trim();if(!c)return;try{const r=await api('/api/promo',{code:c});S.d.user.balance=r.balance;toast('✅ +'+money(r.amount));render()}catch(e){toast(t('bad'))}}
function setLang(l){S.lang=l||(S.lang=='uz'?'ru':'uz');localStorage.lang=S.lang;S.d.user.lang=S.lang;api('/api/lang',{lang:S.lang}).catch(()=>{});render()}
function setDark(){S.dark=!S.dark;localStorage.dark=S.dark?'1':'0';document.body.classList.toggle('dk',S.dark);render()}
async function boot(){try{S.d=await api('/api/init');if(!localStorage.lang)S.lang=S.d.user.lang||'uz';document.body.classList.toggle('dk',S.dark||(!localStorage.dark&&tg.colorScheme=='dark'));if(new URLSearchParams(location.search).get('admin')&&S.d.user.admin)admGo('adm');else render()}
 catch(e){$('#app').innerHTML=`<div class="empty" style="margin-top:80px">${e.err=='maintenance'?'🛠 '+t('maint'):e.err=='banned'?'🚫':'Telegram ichida oching'}</div>`}}
/* ===== ADMIN PANEL ===== */
let F=null;
const aj=(p,b)=>api('/api/a/'+p,b||{});
function back(){const m=S.tab;if(m.startsWith('adm')){if(m=='adm')go('prof');else if(m=='adm_game')admGo('adm_games');else admGo('adm')}else go(m=='pay'?'topup':'games')}
async function admGo(tab,arg){S.tab=tab;S.arg=arg;S.a=null;tg.BackButton.show();clearInterval(S.tm);document.body.style.background='';render();window.scrollTo(0,0);
 try{S.a=await api('/api/a/data/'+(tab.slice(4)||'home')+'?id='+(arg||'')+'&q='+encodeURIComponent(S.aq||''))}catch(e){toast((e&&e.err)||t('err'))}
 if(S.tab==tab)render()}
const ah=(h,b)=>`<div class="hd"><h3>${h}</h3>${b||''}</div>`;
const GF=[['name','Nomi','text'],['cat','Bo\'lim','sel',[['game','O\'yinlar'],['promo','Promokodlar bo\'limi']]],['img','Kichik ikonka (ro\'yxatdagi rasm)','img'],['hero','Katta banner (o\'yin sahifasi tepasi)','img'],['picon','Mahsulotlar uchun umumiy ikonka (UC, Diamonds rasmi)','img'],['field','Foydalanuvchi kiritadigan maydon (Player ID)','text'],['info','Info qator: matn | havola (ixtiyoriy)','text'],['active','Ko\'rinsinmi','tog']];
const PF=[['name','Nomi (masalan 60 UC)','text'],['price','Narxi (so\'m)','number'],['grp','Guruh / tab (UC, Prime, Diamonds, RU...)','text'],['badge','Belgi (2x, HIT...)','text'],['img','Rasm (bo\'sh bo\'lsa umumiy ikonka)','img'],['active','Ko\'rinsinmi','tog']];
const CF=[['number','Karta raqami','text'],['holder','Karta egasi ismi','text'],['bank','Bank (UZCARD, HUMO)','text'],['active','Faol','tog']];
const BF=[['img','Banner rasmi','img'],['link','Bosilganda ochiladigan havola (ixtiyoriy)','text']];
const SF=[['bot_name','Bot nomi','text'],['welcome_uz','Salomlashuv matni (UZ) — {name} = ism','area'],['welcome_ru','Salomlashuv matni (RU)','area'],['welcome_img','Salomlashuv rasmi','img'],['support_link','Yordam havolasi (https://t.me/...)','text'],['channel_link','Kanal havolasi','text'],['min_topup','Minimal to\'ldirish (so\'m)','number'],['card_ttl','Karta amal qilish vaqti (daqiqa)','number'],['maintenance','Texnik ishlar rejimi','tog']];
function vAdm(){const a=S.a;if(!a)return '<div class="empty">⏳</div>';
 return ({adm:aHome,adm_games:aGames,adm_game:aGame,adm_banners:aBanners,adm_cards:aCards,adm_users:aUsers,adm_tops:aTops,adm_ords:aOrds,adm_promos:aPromos,adm_chs:aChs,adm_set:aSet,adm_adms:aAdms,adm_bc:aBc}[S.tab]||aHome)(a)}
function aHome(a){const c=(i,v,l)=>`<div class="card" style="margin:0"><div style="font-size:20px">${i}</div><b style="font-size:18px">${v}</b><div class="mut sm">${l}</div></div>`;
 const M=[['games','🎮','O\'yinlar va narxlar'],['banners','🖼','Bannerlar'],['tops','💰','To\'ldirishlar',a.p_top],['ords','📦','Buyurtmalar',a.p_ord],['users','👥','Foydalanuvchilar'],['cards','💳','Kartalar'],['promos','🎟','Promokodlar'],['chs','📢','Majburiy obuna'],['bc','📨','Xabar yuborish'],['set','⚙️','Sozlamalar'],['adms','👮','Adminlar']];
 return ah('🛠 Admin panel')+`<div class="tl">${c('👥',a.users,'Foydalanuvchi · bugun +'+a.new)}${c('💼',money(a.bal),'Umumiy balans')}${c('💰',money(a.top_sum),'To\'ldirilgan · bugun '+money(a.top_today))}${c('📦',a.ord_cnt,'Bajarilgan · '+money(a.ord_sum))}</div><div class="tl">${M.map(x=>`<div class="tile" onclick="S.aq='';admGo('adm_${x[0]}')"><i>${x[1]}</i>${x[2]}${x[3]?`<b class="bd">${x[3]}</b>`:''}</div>`).join('')}</div>`}
function aGames(a){return ah('🎮 O\'yinlar',`<button class="btn sm" onclick="newGame()">+ O'yin</button>`)+`<div class="mut sm" style="margin-bottom:12px">O'yinni bosing → rasm, nom va narxlarni o'zgartiring</div><div class="grid">${a.games.map(g=>`<div class="gc" onclick="admGo('adm_game',${g.id})"><div class="gi">${gimg(g)}</div>${esc(g.name)}<div class="mut" style="font-size:10px">${g.pc} ta${g.active?'':' · 🔴'}</div></div>`).join('')}</div>`}
function aGame(a){const g=a.game;if(!g)return '<div class="empty">—</div>';const hi=g.hero||g.img;
 return ah(esc(g.name),`<button class="btn sm" onclick="editGame()">✏️ Tahrirlash</button>`)+`<div class="hero2" style="border-radius:18px;margin-bottom:12px;${hi?`background-image:url(/img/${hi})`:''}" onclick="editGame()"><div class="hs"></div><h2>${esc(g.name)}</h2><span class="ed">🖼 Rasmni o'zgartirish</span></div>
 <div class="hd"><b>Mahsulotlar (${a.products.length})</b><span><button class="btn sm" onclick="newProd()">+ Mahsulot</button> <button class="btn o sm" onclick="bulkProd()">📥</button></span></div>`+
 (a.products.map(p=>`<div class="li" onclick="editProd(${p.id})">${(p.img||g.picon)?`<img src="/img/${p.img||g.picon}">`:`<div class="pi">${esc(g.name[0])}</div>`}<div class="sp"><b>${esc(p.name)}</b>${p.badge?` <span class="tag pending">${esc(p.badge)}</span>`:''}<div class="mut sm">${esc(p.grp||'—')}${p.active?'':' · 🔴 yashirin'}</div></div><b>${money(p.price)}</b></div>`).join('')||'<div class="empty">Mahsulot yo\'q. «+ Mahsulot» yoki 📥 ni bosing</div>')}
function editGame(){const g=S.a.game;openForm('O\'yin',GF,g,async v=>{await aj('save/games',Object.assign({},v,{id:g.id}));await admGo('adm_game',g.id)},{del:()=>delRow('games',g.id,()=>admGo('adm_games'))})}
function newGame(){openForm('Yangi o\'yin',GF,{cat:'game',field:'Player ID',active:1},async v=>{const r=await aj('save/games',v);await admGo('adm_game',r.id)})}
function newProd(){const g=S.a.game;openForm('Yangi mahsulot',PF,{active:1},async v=>{await aj('save/products',Object.assign({},v,{game_id:g.id}));await admGo('adm_game',g.id)})}
function editProd(id){const g=S.a.game,p=S.a.products.find(x=>x.id==id);openForm('Mahsulot',PF,p,async v=>{await aj('save/products',Object.assign({},v,{id:id,game_id:g.id}));await admGo('adm_game',g.id)},{del:()=>delRow('products',id,()=>admGo('adm_game',g.id))})}
function bulkProd(){const g=S.a.game;openForm('Ommaviy qo\'shish',[['text','Har qatorda: nom | narx | guruh | belgi','area']],{text:'60 UC | 11700 | UC\n325 UC | 59000 | UC'},async v=>{await aj('bulk',{game_id:g.id,text:v.text});await admGo('adm_game',g.id)})}
function delRow(tb,id,after){ask('O\'chirasizmi?',async()=>{try{await aj('del/'+tb,{id:id});closeForm();after()}catch(e){toast((e&&e.err)||t('err'))}})}
function aBanners(a){return ah('🖼 Bannerlar',`<button class="btn sm" onclick="editBan()">+ Banner</button>`)+(a.items.map(b=>`<div class="card" onclick="editBan(${b.id})" style="padding:8px"><div class="ban" style="margin:0"><div><img src="/img/${b.img}"></div></div><div class="mut sm" style="margin:6px 4px 0">${esc(b.link||'havolasiz')}</div></div>`).join('')||'<div class="empty">Banner yo\'q</div>')}
function editBan(id){const b=id?S.a.items.find(x=>x.id==id):{};openForm('Banner',BF,b,async v=>{await aj('save/banners',Object.assign({},v,id?{id:id}:{}));await admGo('adm_banners')},id?{del:()=>delRow('banners',id,()=>admGo('adm_banners'))}:{})}
function aCards(a){return ah('💳 Kartalar',`<button class="btn sm" onclick="editCard()">+ Karta</button>`)+'<div class="mut sm" style="margin-bottom:10px">Faol kartalardan biri to\'ldirishda tasodifiy beriladi</div>'+(a.items.map(c=>`<div class="li" onclick="editCard(${c.id})"><div class="sp"><b>${esc(c.number)}</b><div class="mut sm">${esc(c.holder)} · ${esc(c.bank)}</div></div>${c.active?'🟢':'🔴'}</div>`).join('')||'<div class="empty">Karta yo\'q! To\'ldirish ishlamaydi</div>')}
function editCard(id){const c=id?S.a.items.find(x=>x.id==id):{active:1,bank:'UZCARD'};openForm('Karta',CF,c,async v=>{await aj('save/cards',Object.assign({},v,id?{id:id}:{}));await admGo('adm_cards')},id?{del:()=>delRow('cards',id,()=>admGo('adm_cards'))}:{})}
function aUsers(a){return ah('👥 Foydalanuvchilar')+`<div class="row" style="margin-bottom:12px"><input id="uq" placeholder="ID, ism yoki @username" value="${esc(S.aq||'')}" onkeydown="if(event.key=='Enter')uSearch()"><button class="btn sm" onclick="uSearch()">🔍</button></div>`+a.items.map(u=>`<div class="li" onclick="editUser(${u.id})"><div class="av" style="width:38px;height:38px;font-size:15px">${esc((u.name||'?')[0])}</div><div class="sp"><b>${esc(u.name)}</b>${u.banned?' 🚫':''}<div class="mut sm">${u.username?'@'+esc(u.username)+' · ':''}${u.id}</div></div><b>${money(u.balance)}</b></div>`).join('')}
function uSearch(){S.aq=$('#uq').value.trim();admGo('adm_users')}
function editUser(id){const u=S.a.items.find(x=>x.id==id);openForm(u.name+' · '+money(u.balance)+' so\'m',[['amt','Summa (so\'m)','number'],['op','Amal','sel',[['add','➕ Balansga qo\'shish'],['sub','➖ Balansdan ayirish']]],['banned','Bloklangan','tog']],{op:'add',banned:u.banned},async v=>{await aj('user',{id:id,op:v.op,amt:v.amt,banned:v.banned});await admGo('adm_users')})}
function decide(kind,id,ok){ask(ok?'Tasdiqlaysizmi?':'Rad etasizmi?',async()=>{try{const r=await aj(kind,{id:id,ok:ok});toast(r.msg);admGo(S.tab)}catch(e){toast(t('err'))}})}
const dbtn=(k,id)=>`<div class="row" style="margin-top:10px"><button class="btn g sm" style="flex:1" onclick="decide('${k}',${id},1)">✅ Tasdiqlash</button><button class="btn o sm" style="flex:1" onclick="decide('${k}',${id},0)">❌ Rad / Bekor</button></div>`;
function aTops(a){return ah('💰 To\'ldirishlar')+(a.items.map(x=>`<div class="card"><div class="row"><div class="sp"><b>+${money(x.amount)} so'm</b><div class="mut sm">${esc(x.uname||x.uid)} · #${x.id} · ${ts(x.created)}</div></div><span class="tag ${x.status}">${t(x.status)}</span></div>${x.status=='pending'?dbtn('topup',x.id):''}</div>`).join('')||'<div class="empty">Yo\'q</div>')}
function aOrds(a){return ah('📦 Buyurtmalar')+(a.items.map(x=>`<div class="card"><div class="row"><div class="sp"><b>${esc(x.game)} — ${esc(x.product)}</b><div class="mut sm">ID: <b>${esc(x.player)}</b> · ${esc(x.uname||x.uid)}</div><div class="mut sm">#${x.id} · ${ts(x.created)} · ${money(x.price)} so'm</div></div><span class="tag ${x.status}">${t(x.status)}</span></div>${x.status=='pending'?dbtn('order',x.id):''}</div>`).join('')||'<div class="empty">Yo\'q</div>')}
function aPromos(a){return ah('🎟 Promokodlar',`<button class="btn sm" onclick="editPromo()">+ Kod</button>`)+'<div class="mut sm" style="margin-bottom:10px">Kodni bossangiz — o\'chiriladi</div>'+(a.items.map(p=>`<div class="li" onclick="delRow('promos','${esc(p.code)}',()=>admGo('adm_promos'))"><div class="sp"><b>${esc(p.code)}</b><div class="mut sm">qolgan: ${p.left}</div></div><b>${money(p.amount)}</b><span>🗑</span></div>`).join('')||'<div class="empty">Yo\'q</div>')}
function editPromo(){openForm('Promokod',[['code','Kod','text'],['amount','Summa (so\'m)','number'],['left','Necha kishi ishlata oladi','number']],{left:100},async v=>{await aj('save/promos',v);await admGo('adm_promos')})}
function aChs(a){return ah('📢 Majburiy obuna',`<button class="btn sm" onclick="editCh()">+ Kanal</button>`)+'<div class="mut sm" style="margin-bottom:10px">Bot kanalda ADMIN bo\'lishi shart. Kanalni bossangiz — o\'chiriladi</div>'+(a.items.map(c=>`<div class="li" onclick="delRow('channels',${c.id},()=>admGo('adm_chs'))"><div class="sp"><b>${esc(c.title||c.chat_id)}</b><div class="mut sm">${esc(c.link)}</div></div><span>🗑</span></div>`).join('')||'<div class="empty">Yo\'q</div>')}
function editCh(){openForm('Kanal qo\'shish',[['chat_id','Kanal @username yoki ID','text']],{},async v=>{await aj('save/channels',v);await admGo('adm_chs')})}
function aSet(a){const s=a.s;return ah('⚙️ Sozlamalar',`<button class="btn sm" onclick="editSet()">✏️ Tahrirlash</button>`)+(s.welcome_img?`<div class="card" style="padding:8px"><img src="/img/${s.welcome_img}" style="width:100%;border-radius:12px"></div>`:'')+`<div class="card">${[['Bot nomi',s.bot_name],['Yordam',s.support_link],['Kanal',s.channel_link],['Minimal to\'ldirish',money(s.min_topup||0)],['Karta vaqti',s.card_ttl+' daqiqa'],['Texnik ishlar',s.maintenance=='1'?'YOQIQ':'o\'chiq']].map(x=>`<div class="row" style="padding:6px 0"><span class="mut sm sp">${x[0]}</span><b class="sm">${esc(x[1]||'—')}</b></div>`).join('')}</div>`}
function editSet(){openForm('Sozlamalar',SF,S.a.s,async v=>{await aj('set',v);await admGo('adm_set')})}
function aBc(a){return ah('📨 Xabar yuborish')+`<div class="card">Hozir <b>${a.users}</b> ta foydalanuvchiga xabar yuboriladi.<button class="btn" style="margin-top:12px" onclick="editBc()">✍️ Xabar yozish</button></div>`}
function editBc(){openForm('Xabar',[['text','Xabar matni (HTML mumkin: <b>qalin</b>)','area'],['img','Rasm (ixtiyoriy)','img']],{},async v=>{if(!(v.text||v.img))throw{err:'Matn yoki rasm kerak'};await new Promise((ok,no)=>ask('Hammaga yuborilsinmi?',async()=>{try{await aj('broadcast',v);toast('✅ Yuborilmoqda...');ok()}catch(e){no(e)}}))})}
function aAdms(a){return ah('👮 Adminlar',`<button class="btn sm" onclick="editAdm()">+ Admin</button>`)+a.items.map(i=>`<div class="li" ${a.owners.includes(i)?'':`onclick="delAdm(${i})"`}><div class="sp"><b>${i}</b><div class="mut sm">${a.owners.includes(i)?'Asosiy admin':'Bosing → olib tashlash'}</div></div></div>`).join('')}
function editAdm(){openForm('Yangi admin',[['id','Telegram ID','number']],{},async v=>{await aj('admin',{id:v.id});await admGo('adm_adms')})}
function delAdm(i){ask('Olib tashlansinmi?',async()=>{await aj('admin',{id:i,remove:1});admGo('adm_adms')})}
/* --- forma oynasi --- */
function openForm(title,fields,vals,save,extra){F={title:title,fields:fields,vals:Object.assign({},vals),save:save,extra:extra||{}};drawForm()}
function Fs(k,v){F.vals[k]=v}
function closeForm(){F=null;drawForm()}
async function fSave(){try{await F.save(F.vals);closeForm()}catch(e){toast((e&&e.err)||t('err'))}}
function fld(f){const k=f[0],l=f[1],ty=f[2],o=f[3],v=F.vals[k]==null?'':F.vals[k];let h=`<div class="fl">${esc(l)}</div>`;
 if(ty=='area')h+=`<textarea oninput="Fs('${k}',this.value)">${esc(v)}</textarea>`;
 else if(ty=='sel')h+=`<select onchange="Fs('${k}',this.value)">${o.map(x=>`<option value="${x[0]}" ${x[0]==v?'selected':''}>${esc(x[1])}</option>`).join('')}</select>`;
 else if(ty=='tog')h+=`<div class="seg" style="margin:0"><div class="${+v?'on':''}" onclick="Fs('${k}',1);drawForm()">Ha</div><div class="${+v?'':'on'}" onclick="Fs('${k}',0);drawForm()">Yo'q</div></div>`;
 else if(ty=='img')h+=`<div class="ip">${v?`<img src="/img/${esc(v)}">`:'<span>🖼</span>'}<div><label class="btn sm" style="display:inline-block">📷 Galereyadan<input type="file" accept="image/*" hidden onchange="upImg('${k}',this)"></label> ${v?`<button class="btn o sm" onclick="Fs('${k}','');drawForm()">✕</button>`:''}</div></div><div class="row" style="margin-top:8px"><input id="u_${k}" placeholder="yoki rasm havolasi https://..." style="padding:10px;font-size:13px"><button class="btn sm" onclick="impImg('${k}')">⬇️</button></div>`;
 else h+=`<input ${ty=='number'?'inputmode="numeric"':''} value="${esc(v)}" oninput="Fs('${k}',this.value)">`;
 return h}
function drawForm(){const m=$('#modal');if(!F){m.innerHTML='';return}
 m.innerHTML=`<div class="ov" style="align-items:center"><div class="fm"><div class="row"><b class="sp">${esc(F.title)}</b><div class="ib" onclick="closeForm()">✕</div></div>${F.fields.map(fld).join('')}<div class="row" style="margin-top:16px">${F.extra.del?`<button class="btn o" style="width:60px" onclick="F.extra.del()">🗑</button>`:''}<button class="btn" onclick="fSave()">💾 Saqlash</button></div></div></div>`}
function shrink(file,max){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>{const im=new Image();im.onload=()=>{const k=Math.min(1,max/Math.max(im.width,im.height)),w=Math.round(im.width*k),h=Math.round(im.height*k),c=document.createElement('canvas');c.width=w;c.height=h;const x=c.getContext('2d');x.fillStyle='#fff';x.fillRect(0,0,w,h);x.drawImage(im,0,0,w,h);res(c.toDataURL('image/jpeg',.86))};im.onerror=rej;im.src=r.result};r.onerror=rej;r.readAsDataURL(file)})}
async function upImg(k,inp){const f=inp.files[0];if(!f)return;toast('⏳ Yuklanmoqda...');try{const d=await shrink(f,1000);const r=await aj('upload',{data:d});Fs(k,r.ref);drawForm();toast('✅')}catch(e){toast((e&&e.err)||t('err'))}}
async function impImg(k){const u=$('#u_'+k).value.trim();if(!u)return;toast('⏳ Yuklanmoqda...');try{const r=await aj('import',{url:u});Fs(k,r.ref);drawForm();toast('✅')}catch(e){toast((e&&e.err)||t('err'))}}

boot();
</script></body></html>"""

if __name__ == "__main__":
    main()
