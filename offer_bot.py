import asyncio
import hmac
import html
import json
import logging
import math
import os
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
MAX_TON = 1_000_000
UPDATES = ["message", "callback_query", "business_connection", "business_message"]
NBSP = "\u00a0"

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()
locks = {}
save_lock = asyncio.Lock()

EMOJI_CROSS = "5260342697075416641"
EMOJI_CHECK = "5260416304224936047"
EMOJI_TG = "5258073068852485953"

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

    def on_done(t):
        tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error(
                "Фоновая задача упала",
                exc_info=(type(exc), exc, exc.__traceback__)
            )

    task.add_done_callback(on_done)
    return task

async def api(method, **params):
    for attempt in range(3):
        try:
            async with http.post(f"{API}/{method}", json=params) as resp:
                data = await resp.json()
        except Exception as e:
            log.warning("Сетевая ошибка: %s", e)
            await asyncio.sleep(2 ** attempt)
            continue
        
        if data.get("ok"):
            return data
            
        if data.get("error_code") == 429:
            retry_after = data.get("parameters", {}).get("retry_after", 5)
            log.warning(f"FLOOD_WAIT: {retry_after} сек. Попытка {attempt+1}/3")
            await asyncio.sleep(retry_after)
            continue
            
        log.warning(
            "Telegram API error | method=%s | code=%s | description=%s",
            method, data.get("error_code"), data.get("description")
        )
        return data
    return {"ok": False, "description": "Max retries reached"}

async def send(chat, text, kb=None, bcid=None, preview=False):
    params = dict(chat_id=chat, text=text, parse_mode="HTML")
    params["link_preview_options"] = {"is_disabled": not preview}
    if kb:
        params["reply_markup"] = kb
    if bcid:
        params["business_connection_id"] = bcid
    return await api("sendMessage", **params)

async def delete_business_message(bcid, mid):
    if not mid:
        return {"ok": False, "description": "No message id"}
    res = await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=[mid])
    if not res.get("ok"):
        log.warning(f"Не удалось удалить сообщение {mid}: {res.get('description')}")
    return res

async def edit_offer(o, text, kb=None, preview=True):
    params = dict(chat_id=o["chat"], message_id=o["mid"], business_connection_id=o["bcid"],
                  text=text, parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})
    params["link_preview_options"] = {"is_disabled": not preview}
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    return res

TERMINAL_STATUSES = {"declined", "cancelled", "expired"}

def prune_offers():
    terminal = sorted(
        (o for o in offers.values() if o.get("status") in TERMINAL_STATUSES),
        key=lambda o: o.get("created", 0)
    )
    for o in terminal[:-500]:
        offers.pop(o["id"], None)
        locks.pop(o["id"], None)

async def save():
    async with save_lock:
        prune_offers()
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
    
    for oid in offers:
        locks[oid] = asyncio.Lock()

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

def parse_offer(arg, currency="STARS"):
    link = NFT_LINK.search(arg)
    if not link:
        return None, "Не нашёл ссылку на NFT вида https://t.me/nft/DurovsCap-1234"
    slug = link.group(1)
    name = gift_name(slug)
    if not name:
        return None, "Это не похоже на ссылку на NFT: в конце должен быть номер."
    
    rest = f"{arg[:link.start()]} {arg[link.end():]}".strip()
    
    address = None
    if currency == "TON":
        address_match = TON_ADDRESS.search(rest)
        address = address_match.group(0) if address_match else None
        if address:
            rest = rest.replace(address, "")
        
    amounts = re.findall(r"\d+", rest)
    if not amounts:
        return None, "Не нашёл сумму."
    if len(amounts) > 1:
        return None, "Нашёл несколько чисел. Оставь только цену."
        
    val = int(amounts[0])
    
    max_val = MAX_TON if currency == "TON" else MAX_STARS
    if not 1 <= val <= max_val:
        return None, f"Сумма должна быть от 1 до {num(max_val)}."
        
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

# ============ ТЕКСТЫ ============

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

def offer_text(o):
    if o.get("currency") == "TON":
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b>{addr_text} за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    else:
        amount = f'{num(o["amount"])} звёзд'
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b> за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")

def accepted_text(o):
    u = o["maker"]
    username = (
        f"@{esc(u['username'])}"
        if u.get("username")
        else esc(u.get("name") or "пользователю")
    )
    
    if o.get("currency") == "TON":
        addr_text = f" адреса <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        return (
            f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} "
            f"пользователю {username} для успешного закрытия сделки, "
            f"после чего <b>{amount}</b> автоматически отправятся на ваш баланс{addr_text}."
        )
    else:
        amount = f'{num(o["amount"])} звёзд'
        return (
            f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} "
            f"пользователю {username} для успешного закрытия сделки, "
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
    # ДОБАВЛЯЕМ ПРОБЕЛ В CALLBACK_DATA, чтобы Telegram видел изменение структуры
    return {
        "inline_keyboard": [[
            btn("Отклонить", f"of:no:{o['id']} ", EMOJI_CROSS),
            btn("Принять", f"of:ok:{o['id']} ", EMOJI_CHECK)
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
    parsed, error = parse_offer(arg, currency)
    chat = m["chat"]["id"] 
    now = time.time()

    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        pass

    if error:
        return await send(chat, f"⚠️ {esc(error)}\n\n{USAGE}", bcid=bcid, preview=False)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "currency": currency, "bcid": bcid, "chat": chat, "mid": None, 
         "maker": maker, "status": "pending", "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    locks[o["id"]] = asyncio.Lock()
    await save()

    res = await send(chat, ".", bcid=bcid, preview=False)
    
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        await save()
        await asyncio.sleep(1.5)
        await edit_offer(o, preoffer_text(o), preoffer_kb(o), preview=True)
    else:
        desc = res.get("description", "")
        if "PEER_FLOOD" not in desc:
            log.error(f"Ошибка при создании оффера: {desc}")

async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] not in ("pending", "offer_shown", "accepted"):
                continue
            try:
                if o["expires"] < time.time():
                    async with locks.get(o["id"], asyncio.Lock()):
                        if o["mid"]:
                            await delete_business_message(o["bcid"], o["mid"])
                        o["status"] = "expired"
                        await save()
            except Exception:
                log.exception("Ошибка обработки истёкшего оффера %s", o.get("id"))

async def on_callback(q):
    parts = q.get("data", "").split(":")
    # УДАЛЯЕМ ПРОБЕЛ, который добавили для кнопок
    o_id = parts[2].strip() if len(parts) == 3 else None
    o = offers.get(o_id) if o_id else None
    
    if o is None:
        return await api("answerCallbackQuery", callback_query_id=q["id"])
        
    action = parts[1]
    lock = locks.get(o["id"])
    if not lock: return await api("answerCallbackQuery", callback_query_id=q["id"])

    async with lock:
        if time.time() > o["expires"] and o["status"] != "expired":
            if o["status"] in ("pending", "offer_shown", "accepted") and o["mid"]:
                await delete_business_message(o["bcid"], o["mid"])
            o["status"] = "expired"
            await save()
            return await api("answerCallbackQuery", callback_query_id=q["id"], text="Срок предложения истёк", show_alert=True)

        if action == "cancel":
            if o["status"] != "accepted":
                return await api("answerCallbackQuery", callback_query_id=q["id"], text="Сделку уже нельзя отменить", show_alert=True)
            try:
                await delete_business_message(o["bcid"], o["mid"])
            except Exception:
                pass
            o["status"] = "cancelled"
            await save()
            return await api("answerCallbackQuery", callback_query_id=q["id"])
        
        if action == "pre_no":
            if o["status"] != "pending":
                return await api("answerCallbackQuery", callback_query_id=q["id"])
            try:
                await delete_business_message(o["bcid"], o["mid"])
            except Exception:
                pass
            o["status"] = "declined"
            await save()
            return await api("answerCallbackQuery", callback_query_id=q["id"])
            
        if action == "pre_ok":
            if o["status"] != "pending":
                return await api("answerCallbackQuery", callback_query_id=q["id"])
            
            o["status"] = "offer_shown"
            await save()
            await edit_offer(o, offer_text(o), offer_kb(o), preview=True)
            return await api("answerCallbackQuery", callback_query_id=q["id"])

        if action in ("ok", "no"):
            if o["status"] != "offer_shown":
                return await api("answerCallbackQuery", callback_query_id=q["id"], text="Сделка уже обработана", show_alert=True)
                
            if action == "ok":
                o["status"] = "accepted"
                await save()
                await edit_offer(o, accepted_text(o), accepted_kb(o), preview=True)
                await api("answerCallbackQuery", callback_query_id=q["id"])
            else:
                try:
                    await delete_business_message(o["bcid"], o["mid"])
                except Exception:
                    pass
                o["status"] = "declined"
                await save()
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
    await save()
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
            await save()
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
