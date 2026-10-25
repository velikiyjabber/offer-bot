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

try:
    _default_secret = BOT_TOKEN.split(":")[1][:32]
except IndexError:
    _default_secret = uuid.uuid4().hex[:32]
SECRET = os.environ.get("WEBHOOK_SECRET") or _default_secret

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
BUTTON_STYLE = os.environ.get("BUTTON_STYLE", "")
RENDER_MIN_SECONDS = max(60, int(os.environ.get("RENDER_MIN_SECONDS", "300")))
SWEEP_BATCH = 20

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()

EMOJI_CROSS = "5260342697075416641"
EMOJI_CHECK = "5260416304224936047"
EMOJI_TG = "5258073068852485953"


# ============ HOMOGLYPH STYLE ============

_HOMOGLYPH = {
    "а": ["a", "α"], "с": ["c"], "е": ["e"], "о": ["o", "ο"],
    "р": ["p", "ρ"], "х": ["x"], "у": ["y"], "к": ["κ"],
    "А": ["A"], "В": ["B"], "Е": ["E"], "К": ["K"], "М": ["M"],
    "Н": ["H"], "О": ["O"], "Р": ["P"], "С": ["C"], "Т": ["T"],
    "У": ["Y"], "Х": ["X"], "І": ["I"], "Ј": ["J"], "Ѕ": ["S"],
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
    out, pos, depth = [], 0, 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_style_plain(chunk) if depth == 0 else chunk)
        out.append(m.group(0))
        depth = max(0, depth - 1) if m.group(1) == "/" else depth + 1
        pos = m.end()
    tail = text[pos:]
    out.append(_style_plain(tail) if depth == 0 else tail)
    return "".join(out)


# ============ HELPERS ============

def esc(s):
    return html.escape(str(s))


def num(x):
    return f"{x:,}".replace(",", NBSP)


def num_ton(x):
    return f"{x}"


def btn(text, data, icon_id=None):
    b = {"text": text, "callback_data": data}
    if icon_id:
        b["icon_custom_emoji_id"] = icon_id
    if BUTTON_STYLE:
        b["style"] = BUTTON_STYLE
    return b


def _task_done(task):
    tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("фоновая задача упала: %s", exc, exc_info=exc)


def background(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(_task_done)
    return task


async def api(method, **params):
    async with http.post(f"{API}/{method}", json=params) as resp:
        data = await resp.json()
    if not data.get("ok"):
        log.warning(
            "Telegram API error | method=%s | code=%s | description=%s | parameters=%s",
            method, data.get("error_code"), data.get("description"), data.get("parameters"),
        )
    return data


async def send(chat, text, kb=None, bcid=None, preview=False):
    if not chat:
        log.warning("send: chat is None, skip")
        return {"ok": False, "description": "no chat"}
    text = style(text)
    params = dict(chat_id=chat, text=text, parse_mode="HTML")
    params["link_preview_options"] = {"is_disabled": not preview}
    if kb:
        params["reply_markup"] = kb
    if bcid:
        params["business_connection_id"] = bcid
    return await api("sendMessage", **params)


async def delete_business_message(bcid, mid):
    if not bcid or not mid:
        return {"ok": False}
    return await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=[mid])


async def edit_offer(o, text, kb=None, preview=True):
    if not o.get("mid"):
        return {"ok": False, "description": "no mid"}
    text = style(text)
    params = dict(chat_id=o["chat"], message_id=o["mid"], business_connection_id=o["bcid"],
                  text=text, parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})
    params["link_preview_options"] = {"is_disabled": not preview}
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    return res


async def render_card(o, text, kb, preview=True):
    res = await edit_offer(o, text, kb, preview=preview)
    if res.get("ok"):
        o["last_render"] = time.time()
        o.pop("render_failed", None)
        return res
    log.warning("edit упал (%s), пересоздаю карточку %s", res.get("description"), o.get("id"))
    res2 = await send(o["chat"], text, kb, o["bcid"], preview=preview)
    if res2.get("ok"):
        o["mid"] = res2["result"]["message_id"]
        o["last_render"] = time.time()
        o.pop("render_failed", None)
        save()
    else:
        o["render_failed"] = True
        log.error("не удалось пересоздать карточку %s: %s", o.get("id"), res2.get("description"))
    return res2


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
TON_ADDRESS = re.compile(r"(?:EQ|UQ)[A-Za-z0-9_-]{46}")

USAGE = ("Формат:\n"
         "<code>ooo ссылка сумма</code> (звёзды)\n"
         "<code>ttt ссылка сумма</code> (TON)\n"
         "<code>ttt ссылка адрес сумма</code> (TON на кошелёк)")


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

    # вырезаем TON-адрес из rest ВСЕГДА, чтобы цифры адреса не попали в сумму
    address = None
    address_match = TON_ADDRESS.search(rest)
    if address_match:
        if currency == "TON":
            address = address_match.group(0)
        rest = rest.replace(address_match.group(0), "")

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


def maker_mention(o):
    u = o["maker"]
    if u.get("username"):
        return f"@{esc(u['username'])}"
    return f"id {u.get('id', 0)}"


def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'


def left_text(seconds):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} ч. {m} мин." if h else f"{m} мин."


def offer_text(o):
    if o.get("currency") == "TON":
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num_ton(o['amount'])} TON"
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b>{addr_text} за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    else:
        amount = f'{num(o["amount"])} звёзд'
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b> за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")


def accepted_text(o):
    mention = maker_mention(o)
    deadline = o.get("transfer_deadline")
    deadline_text = f"\n\nПередать нужно в течение {TRANSFER_MIN} мин." if deadline else ""

    if o.get("currency") == "TON":
        addr_text = f" на адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num_ton(o['amount'])} TON"
        return (
            f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} пользователю {mention}, "
            f"затем нажмите «Готово».\n\n"
            f"После проверки передачи <b>{amount}</b> будут зачислены{addr_text}.{deadline_text}"
        )
    amount = f'{num(o["amount"])} звёзд'
    return (
        f"<b>Сделка принята!</b>\n\n"
        f"Передайте подарок {gift_link(o)} пользователю {mention}, "
        f"затем нажмите «Готово».\n\n"
        f"После проверки передачи <b>{amount}</b> будут зачислены на ваш баланс.{deadline_text}"
    )


def declined_text(o):
    return "❌ Отклонено."


def expired_text(o):
    return f"⌛ Предложение за подарок {gift_link(o)} истекло."


def done_text(o):
    return f"✅ Передача подтверждена. Сделка по {gift_link(o)} закрыта."


def offer_kb(o):
    return {
        "inline_keyboard": [[
            btn("Отклонить", f"of:no:{o['id']}", EMOJI_CROSS),
            btn("Принять", f"of:ok:{o['id']}", EMOJI_CHECK),
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
                {"text": "Передать", "url": transfer_url, "icon_custom_emoji_id": EMOJI_TG},
            ],
            [btn("✅ Готово", f"of:done:{o['id']}")],
        ]
    }


async def make_offer(m, maker, bcid, currency="STARS"):
    cmd, _, arg = (m.get("text") or "").partition(" ")
    parsed, error = parse_offer(arg, currency)
    chat = (m.get("chat") or {}).get("id")
    now = time.time()

    try:
        await delete_business_message(bcid, m.get("message_id"))
    except Exception as e:
        log.warning("delete команды %s не удался: %s", m.get("message_id"), e)

    if not chat:
        log.warning("make_offer: нет chat в сообщении %s", m.get("message_id"))
        return

    if error:
        return await send(chat, f"⚠️ {esc(error)}\n\n{USAGE}", bcid=bcid, preview=False)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "currency": currency, "bcid": bcid,
         "chat": chat, "mid": None, "maker": maker, "status": "pending",
         "created": now, "expires": now + OFFER_TTL, "last_render": 0}
    offers[o["id"]] = o
    save()

    res = await send(chat, offer_text(o), offer_kb(o), bcid=bcid, preview=True)
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        o["last_render"] = time.time()
        save()
    else:
        desc = res.get("description", "")
        if "PEER_FLOOD" in desc:
            return await send(chat, "⚠️ Telegram временно ограничил отправку сообщений.", bcid=bcid)
        return await send(chat, f"⚠️ Ошибка: {esc(desc)}", bcid=bcid)


FINAL = {
    "accepted": (accepted_text, accepted_kb),
    "declined": (declined_text, None),
    "expired": (expired_text, None),
    "done": (done_text, None),
}


async def settle(o, status):
    prev_status = o.get("status")
    o["status"] = status
    if status == "accepted":
        o["transfer_deadline"] = time.time() + TRANSFER_MIN * 60
    save()

    if status == "declined":
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except Exception:
            pass
        return

    text, kb = FINAL[status]
    res = await render_card(o, text(o), kb(o) if kb else None, preview=True)
    if not res.get("ok") and status == "accepted":
        log.error("accepted не отрендерился, откатываю статус оффера %s", o["id"])
        o["status"] = prev_status or "pending"
        o.pop("transfer_deadline", None)
        save()


async def _sweep_one(o, now):
    try:
        if o["status"] == "pending":
            if o["expires"] < now:
                await settle(o, "expired")
                return
            if not o.get("mid"):
                return
            if now - o.get("last_render", 0) >= RENDER_MIN_SECONDS:
                await render_card(o, offer_text(o), offer_kb(o), preview=True)
        elif o["status"] == "accepted":
            deadline = o.get("transfer_deadline")
            if deadline and now > deadline:
                await settle(o, "expired")
                return
            if not o.get("mid"):
                return
            if now - o.get("last_render", 0) >= RENDER_MIN_SECONDS:
                await render_card(o, accepted_text(o), accepted_kb(o), preview=True)
    except Exception:
        log.exception("тик оффера %s", o.get("id"))


async def sweeper():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        active = [o for o in offers.values() if o.get("status") in ("pending", "accepted")]
        for i in range(0, len(active), SWEEP_BATCH):
            batch = active[i:i + SWEEP_BATCH]
            await asyncio.gather(*(_sweep_one(o, now) for o in batch), return_exceptions=True)


async def on_callback(q):
    parts = q.get("data", "").split(":")
    o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None
    note = None
    frm_id = (q.get("from") or {}).get("id")

    if o is None:
        note = "Этот оффер устарел"
    else:
        action = parts[1]
        maker_id = o["maker"].get("id")

        if action == "cancel":
            if frm_id != maker_id:
                note = "Отменить сделку может только автор оффера"
            else:
                try:
                    await delete_business_message(o["bcid"], o["mid"])
                except Exception:
                    pass
                o["status"] = "declined"
                save()

        elif action == "done":
            if o["status"] != "accepted":
                note = "Сначала нужно принять оффер"
            elif frm_id != maker_id:
                note = "Подтвердить передачу может только автор оффера"
            elif o.get("transfer_deadline") and time.time() > o["transfer_deadline"]:
                await settle(o, "expired")
                note = "Срок передачи истёк"
            else:
                await settle(o, "done")

        elif action in ("ok", "no"):
            if frm_id == maker_id:
                note = "Свой оффер принимать нельзя"
            elif o["status"] != "pending":
                note = "Сделка уже обработана"
            elif time.time() > o["expires"]:
                await settle(o, "expired")
                note = "Срок предложения истёк"
            else:
                await settle(o, "accepted" if action == "ok" else "declined")

    extra = {"text": note, "show_alert": True} if note else {}
    await api("answerCallbackQuery", callback_query_id=q["id"], **extra)


def conn_from(c):
    rights = c.get("rights")
    rights = rights if isinstance(rights, dict) else {}
    can_reply = bool(rights.get("can_reply", c.get("can_reply")))
    can_delete = bool(rights.get("can_delete_all_messages"))
    user = c["user"]
    chat_id = c.get("user_chat_id") or user.get("id")
    return {"id": c["id"], "user_id": user["id"], "chat": chat_id,
            "name": user.get("first_name", ""), "username": user.get("username"),
            "enabled": c.get("is_enabled", True), "can_reply": can_reply,
            "can_delete": can_delete}


async def on_connection(c):
    conn = conn_from(c)
    connections[conn["id"]] = conn
    save()
    if not conn["chat"]:
        log.info("connection %s без chat_id — пропускаю приветствие", conn["id"])
        return
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
    res = await send(conn["chat"], text)
    if not res.get("ok") and res.get("error_code") == 403:
        log.info("юзер connection %s заблокировал бота, приветствие не доставлено", conn["id"])


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
    chat = (m.get("chat") or {}).get("id")
    if not chat:
        return
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
    if not SECRET:
        log.error("webhook: SECRET пустой")
        return web.Response(status=403)
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    try:
        if not hmac.compare_digest(token.encode(), SECRET.encode()):
            return web.Response(status=403)
    except TypeError:
        log.error("webhook: SECRET не ASCII, проверка невозможна")
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
    await asyncio.sleep(1)
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

    await api("setMyCommands", commands=[{"command": "start", "description": "Подключение"}])

    background(sweeper())
    if PUBLIC_URL:
        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_post("/webhook", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        try:
            await web.TCPSite(runner, "0.0.0.0", port).start()
        except OSError as e:
            log.error("порт %s занят: %s", port, e)
            raise
        await api("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=UPDATES, drop_pending_updates=True)
        background(keepalive())
    else:
        background(poll_updates())
    log.info("оффер-бот @%s запущен", bot_name)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
