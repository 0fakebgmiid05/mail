import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ==============================================================================
# CONFIGURATION & PERSISTENT SQLITE AUTHORITY STATE
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BaagiJaatUltraTelemetry")

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
ADMIN_TG_ID: int = int(
    os.getenv("ADMIN_TG_ID", "1390658041").strip() or "1390658041"
)
CLIENT_NAME: str = os.getenv("CLIENT_NAME", "BAAGI JAAT").strip() or "BAAGI JAAT"
DB_PATH: str = os.getenv("BOT_DB_PATH", "baagijaat_telemetry.db").strip()

# EnviroFRONT / M2M Logger Endpoints
BASE_URL: str = (
    os.getenv("ENVIRO_BASE_URL", "https://envirofront.m2mlogger.com")
    .strip()
    .rstrip("/")
)
UAC: str = os.getenv("ENVIRO_UAC", "pae").strip() or "pae"
LOGIN_URL: str = os.getenv("ENVIRO_LOGIN_URL", f"{BASE_URL}/").strip()
DEVICES_PAGE_URL: str = (
    os.getenv("ENVIRO_DEVICES_PAGE_URL", f"{BASE_URL}/{UAC}/ControlPanel/Devices")
    .strip()
)
DEVICES_WS_URL: str = (
    os.getenv("ENVIRO_DEVICES_WS_URL", f"{BASE_URL}/Services/EF/WS/Devices")
    .strip()
)
SITE_NAME_WS_BASE: str = (
    os.getenv("ENVIRO_SITE_NAME_WS_BASE", f"{BASE_URL}/Services/EF/WS/Site/Name")
    .strip()
    .rstrip("/")
)
DASHBOARD_WS_URL: str = (
    os.getenv(
        "ENVIRO_DASHBOARD_WS_URL", f"{BASE_URL}/Services/EF/WS/Sites/Dashboard"
    )
    .strip()
)
VAT_CODES_WS_BASE: str = (
    os.getenv(
        "ENVIRO_VAT_CODES_WS_BASE", f"{BASE_URL}/Services/EF/WS/Site/VatCodes"
    )
    .strip()
    .rstrip("/")
)
EXCISE_REQ_WS_URL: str = (
    os.getenv(
        "ENVIRO_EXCISE_REQ_WS_URL", f"{BASE_URL}/Services/EF/WS/UpExcise/Request"
    )
    .strip()
)

GROQ_CHAT_URL: str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL: str = "https://api.groq.com/openai/v1/models"

IST = timezone(timedelta(hours=5, minutes=30))

# Runtime Admin & Display Authority Settings
BOT_ENABLED: bool = True
MAINTENANCE_MODE: bool = False
MAINTENANCE_MSG: str = (
    "System maintenance chal raha hai. Kripya thodi der baad try karein."
)
REFRESH_INTERVAL_SEC: int = 60
NOTIF_STYLE: str = "detailed"  # detailed | compact | alerts_only | digest
ACTIVE_THEME: str = "obsidian"  # obsidian | minimal | cyber | royal

# Session, Navigation, Watchlist, Games & Cache State
USER_SESSIONS: dict[int, dict[str, Any]] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}
LAST_PAGE_STATE: dict[int, dict[str, Any]] = {}
MENU_STACK: dict[int, list[str]] = {}
MENU_STATE: dict[int, str] = {}
AWAITING_INPUT: dict[int, dict[str, Any]] = {}
SENT_STAGE_ALERTS: set[str] = set()
DIGEST_QUEUE: dict[int, list[str]] = {}
AI_CHAT_MODE: set[int] = set()
WATCHLIST_SITES: dict[int, list[str]] = {}
GAME_SESSIONS: dict[int, dict[str, Any]] = {}

DASHBOARD_CACHE: dict[str, dict[str, Any]] = {}
SITE_NAME_CACHE: dict[str, str] = {}
VAT_CODE_CACHE: dict[str, list[str]] = {}
SITE_TAGS_CACHE: dict[str, list[str]] = {}
API_REPORT_CACHE: dict[str, dict[str, Any]] = {}


def _ts_ms() -> int:
    return int(time.time() * 1000)


def today_ist_report_date() -> str:
    return datetime.now(IST).strftime("%d-%b-%Y")


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and int(user_id) == int(ADMIN_TG_ID)


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_alerts (
            stage_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            chat_id INTEGER PRIMARY KEY,
            checked_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_watchlist (
            chat_id INTEGER NOT NULL,
            site_key TEXT NOT NULL,
            PRIMARY KEY (chat_id, site_key)
        )
        """
    )
    conn.commit()
    conn.close()


def save_setting_db(key: str, val: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(k, v) VALUES(?, ?)",
            (key, str(val)),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("DB save_setting failed: %s", exc)


def load_persisted_state() -> None:
    global BOT_ENABLED, MAINTENANCE_MODE, MAINTENANCE_MSG, REFRESH_INTERVAL_SEC, NOTIF_STYLE, ACTIVE_THEME
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        for k, v in cur.execute("SELECT k, v FROM bot_settings"):
            if k == "bot_enabled":
                BOT_ENABLED = v == "1"
            elif k == "maint_mode":
                MAINTENANCE_MODE = v == "1"
            elif k == "maint_msg" and v.strip():
                MAINTENANCE_MSG = v.strip()
            elif k == "refresh_sec":
                try:
                    sec = int(v)
                    if 15 <= sec <= 3600:
                        REFRESH_INTERVAL_SEC = sec
                except ValueError:
                    pass
            elif k == "notif_style" and v in (
                "detailed",
                "compact",
                "alerts_only",
                "digest",
            ):
                NOTIF_STYLE = v
            elif k == "active_theme" and v in (
                "obsidian",
                "minimal",
                "cyber",
                "royal",
            ):
                ACTIVE_THEME = v

        for (stage_key,) in cur.execute("SELECT stage_key FROM sent_alerts"):
            SENT_STAGE_ALERTS.add(stage_key)

        for chat_id, site_key in cur.execute(
            "SELECT chat_id, site_key FROM user_watchlist"
        ):
            WATCHLIST_SITES.setdefault(int(chat_id), []).append(str(site_key))
        conn.close()
    except Exception as exc:
        logger.warning("DB load_persisted_state failed: %s", exc)


def toggle_watchlist_site(chat_id: int, site_key: str) -> tuple[bool, list[str]]:
    key = (site_key or "").strip()
    lst = WATCHLIST_SITES.setdefault(chat_id, [])
    added = False
    try:
        conn = sqlite3.connect(DB_PATH)
        if key in lst:
            lst.remove(key)
            conn.execute(
                "DELETE FROM user_watchlist WHERE chat_id = ? AND site_key = ?",
                (chat_id, key),
            )
            added = False
        else:
            lst.append(key)
            conn.execute(
                "INSERT OR IGNORE INTO user_watchlist(chat_id, site_key) VALUES(?, ?)",
                (chat_id, key),
            )
            added = True
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Watchlist DB error: %s", exc)
    return added, lst


def mark_stage_alert_sent(stage_key: str) -> None:
    SENT_STAGE_ALERTS.add(stage_key)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO sent_alerts(stage_key, created_at) VALUES(?, ?)",
            (stage_key, datetime.now(IST).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def clear_stage_alerts_for_device(chat_id: int, serial: str) -> None:
    prefix = f"{chat_id}:{serial}:"
    to_remove = [k for k in SENT_STAGE_ALERTS if k.startswith(prefix)]
    for k in to_remove:
        SENT_STAGE_ALERTS.discard(k)
    if to_remove:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.executemany(
                "DELETE FROM sent_alerts WHERE stage_key = ?",
                [(k,) for k in to_remove],
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


def clear_all_alerts_for_chat(chat_id: int) -> None:
    prefix = f"{chat_id}:"
    to_remove = [k for k in SENT_STAGE_ALERTS if k.startswith(prefix)]
    for k in to_remove:
        SENT_STAGE_ALERTS.discard(k)
    if to_remove:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.executemany(
                "DELETE FROM sent_alerts WHERE stage_key = ?",
                [(k,) for k in to_remove],
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


def save_snapshot_db(chat_id: int, analysis: dict[str, Any]) -> None:
    try:
        serializable = {
            k: v
            for k, v in analysis.items()
            if k
            not in (
                "all_devices",
                "assigned_devices",
                "online_list",
                "offline_list",
                "unassigned_list",
                "stale_list",
            )
        }
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO snapshots(chat_id, checked_at, payload_json) VALUES(?, ?, ?)",
            (
                chat_id,
                analysis.get("checked_at_ist", ""),
                json.dumps(serializable, default=str),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ==============================================================================
# 4 UI THEMES, TYPOGRAPHY & SAFE HTML ENGINE
# ==============================================================================
_SMALL_CAPS_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
)

THEME_CONFIGS: dict[str, dict[str, str]] = {
    "obsidian": {
        "label": "Obsidian Authority",
        "top": "╔══════════════════════╗",
        "bot": "╚══════════════════════╝",
        "bar": "▓▓▒▒░░ ◈ ░░▒▒▓▓",
        "bullet": "▸",
        "prefix": "║  ◈ ",
    },
    "minimal": {
        "label": "Executive Minimal",
        "top": "────────────────────────",
        "bot": "────────────────────────",
        "bar": "── ◈ ──────────────────",
        "bullet": "•",
        "prefix": "  ◈ ",
    },
    "cyber": {
        "label": "Cyber Telemetry Grid",
        "top": "┌──────────────────────┐",
        "bot": "└──────────────────────┘",
        "bar": "░░▓▓▒▒ [GRID] ▒▒▓▓░░",
        "bullet": "▹",
        "prefix": "│ [◈] ",
    },
    "royal": {
        "label": "Royal Frame",
        "top": "╔═◈══════════════════◈═╗",
        "bot": "╚═◈══════════════════◈═╝",
        "bar": "❖───◈───❖───◈───❖",
        "bullet": "❖",
        "prefix": "║ ❖ ",
    },
}


def get_theme() -> dict[str, str]:
    return THEME_CONFIGS.get(ACTIVE_THEME, THEME_CONFIGS["obsidian"])


def themed_box(title: str, subtitle: str = "") -> str:
    th = get_theme()
    lines = [th["top"], f"{th['prefix']}<b>{title}</b>"]
    if subtitle:
        lines.append(f"{th['prefix']}<i>{subtitle}</i>")
    lines.append(th["bot"])
    return "\n".join(lines)


def sc(text: str) -> str:
    return (text or "").upper().translate(_SMALL_CAPS_MAP)


def esc(val: Any) -> str:
    s = str(val or "")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    return html.escape(s, quote=True)


def strip_html_tags(text: str) -> str:
    txt = re.sub(r"<[^>]+>", "", str(text or ""))
    return html.unescape(txt)


def _split_long_html(text: str, limit: int = 3850) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.splitlines(keepends=True):
        if cur_len + len(line) > limit and cur:
            chunks.append("".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


async def safe_send_html(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> None:
    parts = _split_long_html(text)
    for idx, part in enumerate(parts):
        kb = reply_markup if idx == len(parts) - 1 else None
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        except Exception as exc:
            logger.warning("HTML send fallback triggered: %s", exc)
            await bot.send_message(
                chat_id=chat_id,
                text=strip_html_tags(part),
                reply_markup=kb,
            )


async def safe_edit_html(msg_obj, text: str) -> None:
    parts = _split_long_html(text)
    first = parts[0]
    try:
        await msg_obj.edit_text(first, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("HTML edit fallback triggered: %s", exc)
        await msg_obj.edit_text(strip_html_tags(first))
    for extra in parts[1:]:
        await safe_send_html(msg_obj.get_bot(), msg_obj.chat_id, extra)


def clean_ai_text(text: str) -> str:
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", str(text or ""))
    s = s.replace("**", "").replace("__", "")
    return esc(s)


def small_status_dot(is_connected: bool) -> str:
    return "∙" if is_connected else "•"


def format_duration_hm(td: timedelta | None) -> str:
    if not td or td.total_seconds() <= 0:
        return "0m"
    total_mins = int(td.total_seconds() // 60)
    days, rem_mins = divmod(total_mins, 1440)
    hours, mins = divmod(rem_mins, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_off_since_text(
    last_seen_utc: datetime | None, off_td: timedelta | None
) -> str:
    if last_seen_utc is None or off_td is None or off_td.total_seconds() <= 0:
        return "Off time N/A"
    ist_dt = last_seen_utc.astimezone(IST)
    return (
        f"Off {format_duration_hm(off_td)} "
        f"(since {ist_dt.strftime('%d-%b %I:%M %p')})"
    )


def format_industry_site_label(industry: str, site: str) -> str:
    ind = (industry or "N/A").strip()
    st = (site or "N/A").strip()
    if st and st != "N/A" and st.lower() not in ind.lower():
        return f"{esc(ind)} ({esc(st)})"
    return esc(ind)


def format_premium_device_block(
    dev: dict[str, Any], index: int | None = None
) -> str:
    th = get_theme()
    b = th["bullet"]
    dot = small_status_dot(bool(dev.get("is_connected")))
    conn_word = "CONNECTED" if dev.get("is_connected") else "DISCONNECTED"
    off_info = (
        "(Active)"
        if dev.get("is_connected")
        else format_off_since_text(
            dev.get("last_seen"), dev.get("offline_duration")
        )
    )
    stale_tag = " [STALE]" if dev.get("is_stale") else ""
    prefix = f"#{index} " if index is not None else ""
    sid = f" [Site:{esc(dev['site_id'])}]" if dev.get("site_id") else ""
    scode = (
        f" | Code: <code>{esc(dev['site_code'])}</code>"
        if dev.get("site_code")
        else ""
    )
    label = format_industry_site_label(
        dev.get("industry", "N/A"), dev.get("site", "N/A")
    )

    if NOTIF_STYLE == "compact" or ACTIVE_THEME == "minimal":
        return (
            f"{b} {prefix}<b>{label}</b>{sid}{esc(stale_tag)}\n"
            f"  Serial: <b>{esc(dev.get('serial_no', 'N/A'))}</b> | {dot} <b>{conn_word}</b>{scode} | "
            f"<code>{esc(dev.get('display_time', 'N/A'))}</code> / <code>{esc(off_info)}</code>"
        )

    return (
        f"{themed_box(prefix + label + sid + stale_tag)}\n"
        f"  {b} Serial : <b>{esc(dev.get('serial_no', 'N/A'))}</b>{scode}\n"
        f"  {b} Status : {dot} <b>{conn_word}</b>\n"
        f"  {b} Device : <code>{esc(dev.get('device_type', 'N/A'))}</code> | FW: <code>{esc(dev.get('firmware', 'N/A'))}</code>\n"
        f"  {b} Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code> / <code>{esc(off_info)}</code>"
    )


# ==============================================================================
# GUEST LOUNGE MINI-GAMES (NO BOREDOM WHILE WAITING FOR LOGIN)
# ==============================================================================
def start_ttt_game(chat_id: int) -> str:
    board = [" "] * 9
    GAME_SESSIONS[chat_id] = {
        "game": "ttt",
        "board": board,
        "moves": 0,
    }
    return render_ttt_board(board, "Aap 'X' hain, Bot 'O' hai. 1 se 9 tak cell number bhejein:")


def _check_ttt_winner(board: list[str]) -> str | None:
    wins = [
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    ]
    for a, b, c in wins:
        if board[a] != " " and board[a] == board[b] == board[c]:
            return board[a]
    if " " not in board:
        return "DRAW"
    return None


def render_ttt_board(board: list[str], status_line: str) -> str:
    def cell_disp(idx: int) -> str:
        val = board[idx]
        if val == "X":
            return "✕"
        if val == "O":
            return "◯"
        return str(idx + 1)

    grid = (
        f"<code>  {cell_disp(0)} │ {cell_disp(1)} │ {cell_disp(2)}  </code>\n"
        f"<code> ───┼───┼─── </code>\n"
        f"<code>  {cell_disp(3)} │ {cell_disp(4)} │ {cell_disp(5)}  </code>\n"
        f"<code> ───┼───┼─── </code>\n"
        f"<code>  {cell_disp(6)} │ {cell_disp(7)} │ {cell_disp(8)}  </code>"
    )
    return (
        f"{themed_box(sc('TIC-TAC-TOE LOUNGE'))}\n"
        f"{get_theme()['bar']}\n"
        f"{grid}\n\n"
        f"  ▸ {esc(status_line)}"
    )


def play_ttt_turn(chat_id: int, cell_num: int) -> tuple[str, bool]:
    st = GAME_SESSIONS.get(chat_id)
    if not st or st.get("game") != "ttt":
        return start_ttt_game(chat_id), False

    board: list[str] = st["board"]
    idx = cell_num - 1
    if idx < 0 or idx > 8 or board[idx] != " ":
        return (
            render_ttt_board(
                board, "Khali cell (1–9) chuniye."
            ),
            False,
        )

    board[idx] = "X"
    win = _check_ttt_winner(board)
    if win == "X":
        GAME_SESSIONS.pop(chat_id, None)
        return render_ttt_board(board, "Badhai! Aap jeet gaye!"), True
    if win == "DRAW":
        GAME_SESSIONS.pop(chat_id, None)
        return render_ttt_board(board, "Match Draw hua!"), True

    empty_cells = [i for i, v in enumerate(board) if v == " "]
    # Smart bot: try win or block X, else random/center
    bot_move = None
    for mark in ("O", "X"):
        for c in empty_cells:
            board[c] = mark
            if _check_ttt_winner(board) == mark:
                bot_move = c
            board[c] = " "
            if bot_move is not None:
                break
        if bot_move is not None:
            break
    if bot_move is None:
        bot_move = 4 if 4 in empty_cells else random.choice(empty_cells)

    board[bot_move] = "O"
    win2 = _check_ttt_winner(board)
    if win2 == "O":
        GAME_SESSIONS.pop(chat_id, None)
        return render_ttt_board(board, "Bot ne is baar baazi maar li!"), True
    if win2 == "DRAW":
        GAME_SESSIONS.pop(chat_id, None)
        return render_ttt_board(board, "Match Draw hua!"), True

    return (
        render_ttt_board(
            board, f"Bot ne cell {bot_move + 1} khela. Ab aapki baari (1–9):"
        ),
        False,
    )


def start_guess_game(chat_id: int) -> str:
    secret = random.randint(1, 100)
    GAME_SESSIONS[chat_id] = {
        "game": "guess",
        "secret": secret,
        "tries": 0,
        "low": 1,
        "high": 100,
    }
    return (
        f"{themed_box(sc('NUMBER GUESS (1–100)'))}\n"
        f"{get_theme()['bar']}\n"
        "  ▸ Maine 1 se 100 ke beech ek number socha hai.\n"
        "  ▸ Apna guess number bhejein (e.g. <code>50</code>):"
    )


def play_guess_turn(chat_id: int, guess_num: int) -> tuple[str, bool]:
    st = GAME_SESSIONS.get(chat_id)
    if not st or st.get("game") != "guess":
        return start_guess_game(chat_id), False

    st["tries"] += 1
    secret = int(st["secret"])
    tries = int(st["tries"])

    if guess_num == secret:
        GAME_SESSIONS.pop(chat_id, None)
        return (
            f"{themed_box(sc('NUMBER FOUND!'))}\n"
            f"  ▸ Sahi number <b>{secret}</b> aapne <b>{tries}</b> tries mein guess kar liya!"
        ), True

    if guess_num < secret:
        st["low"] = max(int(st["low"]), guess_num + 1)
        hint = f"Thoda <b>BADA</b> number try karein (Range: <code>{st['low']}–{st['high']}</code>)"
    else:
        st["high"] = min(int(st["high"]), guess_num - 1)
        hint = f"Thoda <b>CHHOTA</b> number try karein (Range: <code>{st['low']}–{st['high']}</code>)"

    return (
        f"  ▸ Try #{tries} (<code>{guess_num}</code>): {hint}"
    ), False


def start_rps_game(chat_id: int) -> str:
    GAME_SESSIONS[chat_id] = {
        "game": "rps",
        "user_score": 0,
        "bot_score": 0,
        "round": 0,
        "max_rounds": 5,
    }
    return (
        f"{themed_box(sc('ROCK • PAPER • SCISSORS'))}\n"
        f"{get_theme()['bar']}\n"
        "  ▸ Best of 5 rounds! Niche <code>Rock</code>, <code>Paper</code>, ya <code>Scissors</code> dabayein:"
    )


def play_rps_turn(chat_id: int, choice_raw: str) -> tuple[str, bool]:
    st = GAME_SESSIONS.get(chat_id)
    if not st or st.get("game") != "rps":
        return start_rps_game(chat_id), False

    c_low = choice_raw.lower()
    if "rock" in c_low:
        u_choice = "Rock"
    elif "paper" in c_low:
        u_choice = "Paper"
    elif "scissor" in c_low:
        u_choice = "Scissors"
    else:
        return "  ▸ <code>Rock</code>, <code>Paper</code>, ya <code>Scissors</code> chuniye.", False

    b_choice = random.choice(["Rock", "Paper", "Scissors"])
    st["round"] += 1
    beats = {"Rock": "Scissors", "Paper": "Rock", "Scissors": "Paper"}

    if u_choice == b_choice:
        outcome = "Round Draw!"
    elif beats[u_choice] == b_choice:
        st["user_score"] += 1
        outcome = "Aap yeh round jeete!"
    else:
        st["bot_score"] += 1
        outcome = "Bot yeh round jeeta!"

    done = st["round"] >= st["max_rounds"] or st["user_score"] == 3 or st["bot_score"] == 3
    score_line = f"Score — Aap: <b>{st['user_score']}</b> | Bot: <b>{st['bot_score']}</b> (Round {st['round']}/{st['max_rounds']})"

    if done:
        GAME_SESSIONS.pop(chat_id, None)
        if st["user_score"] > st["bot_score"]:
            final_res = "Final Result: Aap match jeet gaye!"
        elif st["bot_score"] > st["user_score"]:
            final_res = "Final Result: Bot match jeet gaya!"
        else:
            final_res = "Final Result: Match Tie raha!"
        return (
            f"  ▸ Aap: <b>{u_choice}</b> vs Bot: <b>{b_choice}</b> → {outcome}\n"
            f"  ▸ {score_line}\n"
            f"  ▸ <b>{esc(final_res)}</b>"
        ), True

    return (
        f"  ▸ Aap: <b>{u_choice}</b> vs Bot: <b>{b_choice}</b> → {outcome}\n"
        f"  ▸ {score_line}"
    ), False


# ==============================================================================
# CONTEXT-AWARE DYNAMIC REPLY KEYBOARDS WITH BACK & ADMIN SUB-MENUS
# ==============================================================================
def guest_reply_keyboard() -> ReplyKeyboardMarkup:
    st = GAME_SESSIONS.get(0)  # placeholder check overridden below
    _ = st
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔐 Login"), KeyboardButton("📖 Manual")],
            [KeyboardButton("🎮 Games Lounge")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def guest_keyboard_for_chat(chat_id: int) -> ReplyKeyboardMarkup:
    g = GAME_SESSIONS.get(chat_id)
    if g and g.get("game") == "rps":
        return ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton("Rock"),
                    KeyboardButton("Paper"),
                    KeyboardButton("Scissors"),
                ],
                [KeyboardButton("🎮 Games Lounge"), KeyboardButton("🔐 Login")],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )
    if g and g.get("game") == "ttt":
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton("1"), KeyboardButton("2"), KeyboardButton("3")],
                [KeyboardButton("4"), KeyboardButton("5"), KeyboardButton("6")],
                [KeyboardButton("7"), KeyboardButton("8"), KeyboardButton("9")],
                [KeyboardButton("🎮 Games Lounge"), KeyboardButton("🔐 Login")],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )
    return guest_reply_keyboard()


def games_lounge_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🎲 Tic-Tac-Toe"),
                KeyboardButton("🔢 Number Guess"),
            ],
            [KeyboardButton("✂ Rock Paper Scissors"), KeyboardButton("🔐 Login")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_reply_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton("◈ Status"), KeyboardButton("◈ All Sites")],
        [KeyboardButton("🌐 Site Dashboard"), KeyboardButton("📡 API Report")],
        [KeyboardButton("📐 Instruments"), KeyboardButton("⏳ Aging")],
        [KeyboardButton("⭐ Watchlist"), KeyboardButton("📋 Shift Summary")],
        [KeyboardButton("🤖 AI Suite"), KeyboardButton("📖 Manual")],
        [KeyboardButton("⚙ Ops & Export"), KeyboardButton("🚪 Logout")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton("🛡 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def dashboard_sub_keyboard(chat_id: int) -> ReplyKeyboardMarkup:
    snap = LAST_SNAPSHOT.get(chat_id) or {}
    devs = snap.get("assigned_devices") or []
    rows: list[list[KeyboardButton]] = []
    quick_row: list[KeyboardButton] = []
    for idx, d in enumerate(devs[:4], start=1):
        label = f"#{idx} {str(d.get('industry') or 'Site')[:14]}"
        quick_row.append(KeyboardButton(label))
        if len(quick_row) == 2:
            rows.append(quick_row)
            quick_row = []
    if quick_row:
        rows.append(quick_row)

    rows.append(
        [KeyboardButton("🔎 Open Site by ID/Name"), KeyboardButton("⭐ Pin Site")]
    )
    rows.append([KeyboardButton("⬅ Previous"), KeyboardButton("Next ➡")])
    rows.append([KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def api_report_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📅 Today API Report"),
                KeyboardButton("🗓 Custom Date Report"),
            ],
            [
                KeyboardButton("🏭 Site API Report"),
                KeyboardButton("🌐 Site Dashboard"),
            ],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def aging_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("◦ <1D"),
                KeyboardButton("▪ <1W"),
                KeyboardButton("• <24D"),
            ],
            [
                KeyboardButton("◆ <11M"),
                KeyboardButton("◈ <1Y"),
                KeyboardButton("▸ >1Y"),
            ],
            [KeyboardButton("⬅ Previous"), KeyboardButton("Next ➡")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def ops_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🩺 Health"), KeyboardButton("📥 Export CSV")],
            [KeyboardButton("∙ Online"), KeyboardButton("📌 Unassigned")],
            [KeyboardButton("❄ Stale Sites"), KeyboardButton("⭐ Watchlist")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🎨 Themes"), KeyboardButton("🔔 Notif & Demo")],
            [KeyboardButton("⏱ Sync Interval"), KeyboardButton("🛠 Maintenance")],
            [KeyboardButton("⚡ Bot ON/OFF"), KeyboardButton("🧹 Clear Alerts")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def theme_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("Theme: Obsidian"),
                KeyboardButton("Theme: Minimal"),
            ],
            [KeyboardButton("Theme: Cyber"), KeyboardButton("Theme: Royal")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def notif_demo_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("Mode: Detailed"),
                KeyboardButton("Mode: Compact"),
            ],
            [
                KeyboardButton("Mode: Alerts Only"),
                KeyboardButton("Mode: Digest"),
            ],
            [
                KeyboardButton("Demo: 2h Down"),
                KeyboardButton("Demo: 10h Down"),
            ],
            [
                KeyboardButton("Demo: 1d Down"),
                KeyboardButton("Demo: Recovered"),
            ],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def current_keyboard_for_chat(
    chat_id: int, user_id: int | None = None
) -> ReplyKeyboardMarkup:
    if not require_auth(chat_id):
        if MENU_STATE.get(chat_id) == "games_menu":
            return games_lounge_keyboard()
        return guest_keyboard_for_chat(chat_id)

    state = MENU_STATE.get(chat_id, "main")
    if state == "dash_menu":
        return dashboard_sub_keyboard(chat_id)
    if state == "api_menu":
        return api_report_sub_keyboard()
    if state == "age_menu":
        return aging_sub_keyboard()
    if state == "ops_menu":
        return ops_sub_keyboard()
    if state == "admin_menu" and is_admin(user_id):
        return admin_sub_keyboard()
    if state == "theme_menu" and is_admin(user_id):
        return theme_sub_keyboard()
    if state == "notif_menu" and is_admin(user_id):
        return notif_demo_sub_keyboard()
    return main_reply_keyboard(user_id)


def push_menu_state(chat_id: int, new_state: str) -> None:
    cur = MENU_STATE.get(chat_id, "main")
    if cur != new_state:
        stack = MENU_STACK.setdefault(chat_id, [])
        stack.append(cur)
        if len(stack) > 12:
            stack.pop(0)
    MENU_STATE[chat_id] = new_state


async def pop_menu_state(
    chat_id: int, user_id: int | None, bot
) -> None:
    AWAITING_INPUT.pop(chat_id, None)
    GAME_SESSIONS.pop(chat_id, None)
    stack = MENU_STACK.get(chat_id) or []
    prev_state = stack.pop() if stack else "main"
    MENU_STATE[chat_id] = prev_state

    title_map = {
        "main": "MAIN MENU",
        "dash_menu": "SITE DASHBOARD HUB",
        "api_menu": "EXCISE API REPORT",
        "age_menu": "AGING BUCKETS",
        "ops_menu": "OPS & EXPORT",
        "admin_menu": "ADMIN AUTHORITY",
        "theme_menu": "UI THEMES",
        "notif_menu": "NOTIFICATIONS & DEMO",
        "games_menu": "GAMES LOUNGE",
    }
    label = title_map.get(prev_state, "MAIN MENU")
    await safe_send_html(
        bot,
        chat_id,
        f"{themed_box(sc(label))}",
        reply_markup=current_keyboard_for_chat(chat_id, user_id),
    )


# ==============================================================================
# PORTAL LOGIN & REAL ENVIROFRONT API CLIENTS
# ==============================================================================
def _ajax_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "text/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
    }


def login_envirofront(
    username: str, password: str
) -> tuple[requests.Session | None, str]:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/158.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{BASE_URL}/",
        }
    )

    try:
        r_get = sess.get(LOGIN_URL, timeout=20, allow_redirects=True)
        r_get.raise_for_status()
    except Exception as exc:
        return None, f"Login page load nahi hua: {exc}"

    if BASE_URL.lower() not in r_get.url.lower():
        return (
            None,
            f"Unexpected redirect ({r_get.url}). ENVIRO_LOGIN_URL check karein.",
        )

    soup = BeautifulSoup(r_get.text, "html.parser")
    payload: dict[str, str] = {}
    form = soup.find("form")
    action_url = r_get.url or LOGIN_URL

    if form:
        act = form.get("action")
        if act:
            action_url = urljoin(action_url, str(act))
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "").lower()
            val = inp.get("value") or ""
            if itype == "hidden":
                payload[str(name)] = str(val)
            elif itype == "checkbox" and inp.has_attr("checked"):
                payload[str(name)] = str(val) or "true"

    user_field_candidates = [
        k
        for k in (
            (inp.get("name") if form else None)
            for inp in (form.find_all("input") if form else [])
        )
        if k
        and any(
            w in str(k).lower() for w in ("user", "email", "login", "name", "id")
        )
    ]
    pass_field_candidates = [
        k
        for k in (
            (inp.get("name") if form else None)
            for inp in (form.find_all("input") if form else [])
        )
        if k and "pass" in str(k).lower()
    ]

    u_key = user_field_candidates[0] if user_field_candidates else "UserName"
    p_key = pass_field_candidates[0] if pass_field_candidates else "Password"
    payload[str(u_key)] = username
    payload[str(p_key)] = password

    try:
        r_post = sess.post(
            action_url,
            data=payload,
            headers={"Referer": r_get.url or LOGIN_URL},
            timeout=20,
            allow_redirects=True,
        )
        r_post.raise_for_status()
    except Exception as exc:
        return None, f"Login submit fail: {exc}"

    post_lower = (r_post.text or "").lower()
    if (
        "account/login" in r_post.url.lower()
        and (
            "invalid" in post_lower
            or "error" in post_lower
            or "incorrect" in post_lower
        )
    ):
        return None, "Galat username ya password."

    try:
        r_chk = sess.get(DEVICES_PAGE_URL, timeout=20, allow_redirects=True)
        if "account/login" in r_chk.url.lower():
            return None, "Session verify nahi hua (redirect to login)."
    except Exception as exc:
        return None, f"Devices page verify fail: {exc}"

    return sess, "OK"


def ensure_live_session(
    chat_id: int, sess_data: dict[str, Any]
) -> requests.Session | None:
    session: requests.Session | None = sess_data.get("session")
    if session is None:
        return None
    # Session auto-heal check on repeated failures
    if sess_data.get("fail_streak", 0) >= 3:
        logger.info("Session auto-heal triggered for chat %s", chat_id)
        sess_data["fail_streak"] = 0
    return session


def parse_timestamp_utc(
    raw_ts: str | None, now_utc: datetime
) -> datetime | None:
    if not raw_ts:
        return None
    s = " ".join(str(raw_ts).strip().split())
    if not s or s.lower() in ("n/a", "na", "-", "none", "null", "--"):
        return None

    ms_match = re.search(r"/Date\((\-?\d+)(?:[+-]\d+)?\)/", s)
    if ms_match:
        try:
            ms = int(ms_match.group(1))
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        except Exception:
            pass

    s_clean = re.sub(r"\b(IST|UTC|GMT)\b", "", s, flags=re.IGNORECASE).strip()
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%d-%b-%Y %I:%M:%S %p",
        "%d-%b-%Y %I:%M %p",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d %b %Y %H:%M:%S",
        "%d %b %Y %H:%M",
        "%b %d, %Y %I:%M:%S %p",
        "%b %d, %Y %I:%M %p",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s_clean, fmt)
            return dt.replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue

    try:
        dt_iso = datetime.fromisoformat(s_clean.replace("Z", "+00:00"))
        if dt_iso.tzinfo is None:
            return dt_iso.replace(tzinfo=IST).astimezone(timezone.utc)
        return dt_iso.astimezone(timezone.utc)
    except Exception:
        return None


def classify_connection(
    last_seen_utc: datetime | None,
    raw_status_val: Any,
    now_utc: datetime,
) -> tuple[bool, str, float | None, timedelta | None]:
    off_td: timedelta | None = None
    off_hours: float | None = None
    if last_seen_utc is not None:
        off_td = now_utc - last_seen_utc
        if off_td.total_seconds() < 0:
            off_td = timedelta(0)
        off_hours = round(off_td.total_seconds() / 3600.0, 2)

    if isinstance(raw_status_val, (int, float)) and not isinstance(
        raw_status_val, bool
    ):
        if int(raw_status_val) == 2:
            return True, "CONNECTED", off_hours, off_td
        if int(raw_status_val) == 0:
            return False, "DISCONNECTED", off_hours, off_td

    st_str = str(raw_status_val or "").strip().lower()
    if st_str in ("2", "connected", "online", "active", "live", "true"):
        return True, "CONNECTED", off_hours, off_td
    if st_str in ("0", "disconnected", "offline", "inactive", "false"):
        return False, "DISCONNECTED", off_hours, off_td

    if last_seen_utc is not None and off_hours is not None:
        if off_hours <= 2.0:
            return True, "CONNECTED", off_hours, off_td
        return False, "DISCONNECTED", off_hours, off_td

    return False, "DISCONNECTED", off_hours, off_td


def normalize_device_record(
    serial_no: str,
    industry: str,
    site_type: str,
    device_type: str,
    firmware: str,
    raw_status: Any,
    raw_last_seen: str,
    now_utc: datetime,
    site_id: str = "",
    site_code: str = "",
) -> dict[str, Any]:
    ser = (serial_no or "N/A").strip()
    ind = (industry or "N/A").strip() or "N/A"
    st = (site_type or "N/A").strip() or "N/A"
    dtype = (device_type or "N/A").strip() or "N/A"
    fw = (firmware or "N/A").strip() or "N/A"

    is_unassigned = ind.upper() in ("N/A", "NA", "-", "") and st.upper() in (
        "N/A",
        "NA",
        "-",
        "",
    )

    last_seen_dt = parse_timestamp_utc(raw_last_seen, now_utc)
    is_conn, conn_label, off_hours, off_td = classify_connection(
        last_seen_dt, raw_status, now_utc
    )

    # Connected-but-stale flag: connected yet last seen > 30 mins ago
    is_stale = bool(
        is_conn and off_hours is not None and off_hours >= 0.5
    )

    if last_seen_dt is not None:
        display_time = last_seen_dt.astimezone(IST).strftime(
            "%d-%b-%Y %I:%M:%S %p IST"
        )
    else:
        display_time = (raw_last_seen or "N/A").strip() or "N/A"

    sid_clean = (site_id or "").strip()
    scode_clean = (site_code or "").strip()
    dash_url = (
        f"{BASE_URL}/{UAC}/Site/{sid_clean}/Dashboard"
        if sid_clean.isdigit()
        else ""
    )

    return {
        "serial_no": ser,
        "site_id": sid_clean,
        "site_code": scode_clean,
        "dashboard_url": dash_url,
        "industry": ind,
        "site": st,
        "is_unassigned": is_unassigned,
        "device_type": dtype,
        "firmware": fw,
        "is_connected": is_conn,
        "is_stale": is_stale,
        "conn_label": conn_label,
        "offline_hours": off_hours,
        "offline_duration": off_td,
        "last_seen": last_seen_dt,
        "display_time": display_time,
    }


def _extract_list_from_json_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in (
            "aaData",
            "data",
            "Data",
            "rows",
            "Rows",
            "devices",
            "Devices",
            "items",
            "Items",
            "result",
            "Result",
        ):
            val = data.get(key)
            if isinstance(val, list) and val:
                sub = _extract_list_from_json_payload(val)
                if sub:
                    return sub
            if isinstance(val, str) and val.strip().startswith(("[", "{")):
                try:
                    parsed = json.loads(val)
                    sub = _extract_list_from_json_payload(parsed)
                    if sub:
                        return sub
                except Exception:
                    pass
    return []


def _dict_to_device(
    item: dict[str, Any], now_utc: datetime
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    low_map = {str(k).lower(): v for k, v in item.items()}

    def pick_raw(*keys: str) -> Any:
        for k in keys:
            v = item.get(k)
            if v is None:
                v = low_map.get(k.lower())
            if v is not None and str(v).strip() != "":
                return v
        return None

    def pick_str(*keys: str) -> str:
        val = pick_raw(*keys)
        return str(val).strip() if val is not None else ""

    serial = pick_str(
        "SerialNo",
        "serial_no",
        "Serial",
        "DeviceSerial",
        "SerialNumber",
        "IMEI",
        "LoggerId",
        "DeviceId",
    )
    sid = pick_str("SiteId", "siteId", "Site_Id", "site_id")
    scode = pick_str("SiteCode", "siteCode", "Code", "UnitCode", "SiteNo")
    ind = (
        pick_str(
            "Industry",
            "IndustryName",
            "ClientName",
            "CompanyName",
            "SiteName",
        )
        or "N/A"
    )
    stype = pick_str("Site", "SiteType", "Category", "Location") or "N/A"
    dtype = pick_str("DeviceType", "Model", "Type", "LoggerType") or "N/A"
    fw = (
        pick_str(
            "FirmwareVer",
            "Firmware",
            "FirmwareVersion",
            "FW",
            "Version",
        )
        or "N/A"
    )
    raw_ts = pick_str(
        "Timestamp",
        "LastSeen",
        "LastDataTime",
        "LastCommunication",
        "UpdatedOn",
        "LastUpdate",
        "LastPacketTime",
        "DateTime",
    )
    raw_st = pick_raw(
        "Status",
        "ConnectionStatus",
        "IsConnected",
        "Online",
        "IsOnline",
    )

    if not serial and not sid:
        return None

    return normalize_device_record(
        serial_no=serial or f"SITE-{sid}",
        industry=ind,
        site_type=stype,
        device_type=dtype,
        firmware=fw,
        raw_status=raw_st,
        raw_last_seen=raw_ts,
        now_utc=now_utc,
        site_id=sid,
        site_code=scode,
    )


def fetch_devices_from_ws_api(
    session: requests.Session, now_utc: datetime
) -> list[dict[str, Any]]:
    all_devs: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for page_num in range(1, 15):
        params = {
            "uac": UAC,
            "rp": "50",
            "page": str(page_num),
            "devices": "",
            "deviceType": "",
            "ind": "",
            "status": "all",
            "_": str(_ts_ms()),
        }
        try:
            r = session.get(
                DEVICES_WS_URL,
                params=params,
                headers=_ajax_headers(DEVICES_PAGE_URL),
                timeout=22,
            )
            if not r.ok:
                break
            txt = (r.text or "").strip()
            if not txt or txt.startswith("<"):
                break
            data = json.loads(txt)
            rows = _extract_list_from_json_payload(data)
            if not rows:
                break

            added_this_page = 0
            for item in rows:
                rec = _dict_to_device(item, now_utc)
                if not rec:
                    continue
                dedup_key = f"{rec['serial_no']}|{rec['site_id']}|{rec['industry']}"
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_devs.append(rec)
                    added_this_page += 1

            if len(rows) < 50 or added_this_page == 0:
                break
        except Exception as exc:
            logger.warning("WS Devices page %s error: %s", page_num, exc)
            break

    return all_devs


def fetch_site_name_by_code(
    session: requests.Session, site_code: str, referer: str
) -> str:
    code = (site_code or "").strip()
    if not code:
        return ""
    if code in SITE_NAME_CACHE:
        return SITE_NAME_CACHE[code]

    url = f"{SITE_NAME_WS_BASE}/{quote(code)}"
    try:
        r = session.get(
            url,
            params={"uac": UAC, "_": str(_ts_ms())},
            headers=_ajax_headers(referer),
            timeout=15,
        )
        if not r.ok:
            return ""
        txt = (r.text or "").strip().strip('"')
        if not txt:
            return ""
        if txt.startswith("{"):
            obj = json.loads(txt)
            if isinstance(obj, dict):
                txt = str(
                    obj.get("SiteName")
                    or obj.get("Name")
                    or obj.get("IndustryName")
                    or obj.get("d")
                    or ""
                ).strip()
        SITE_NAME_CACHE[code] = txt
        return txt
    except Exception:
        return ""


def fetch_vat_codes_for_site(
    session: requests.Session, site_id: str, referer: str
) -> list[str]:
    sid = (site_id or "").strip()
    if not sid.isdigit():
        return []
    if sid in VAT_CODE_CACHE:
        return VAT_CODE_CACHE[sid]

    url = f"{VAT_CODES_WS_BASE}/{sid}"
    codes: list[str] = []
    try:
        r = session.get(
            url,
            params={"uac": UAC, "_": str(_ts_ms())},
            headers=_ajax_headers(referer),
            timeout=15,
        )
        if r.ok:
            txt = (r.text or "").strip()
            if txt and not txt.startswith("<"):
                parsed = json.loads(txt)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, str) and item.strip():
                            codes.append(item.strip())
                        elif isinstance(item, dict):
                            c = str(
                                item.get("VatCode")
                                or item.get("Code")
                                or item.get("Name")
                                or item.get("Value")
                                or ""
                            ).strip()
                            if c:
                                codes.append(c)
                elif isinstance(parsed, dict):
                    for k in (
                        "data",
                        "Data",
                        "result",
                        "Result",
                        "items",
                        "vatCodes",
                    ):
                        v = parsed.get(k)
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, str) and item.strip():
                                    codes.append(item.strip())
                                elif isinstance(item, dict):
                                    c = str(
                                        item.get("VatCode")
                                        or item.get("Code")
                                        or item.get("Name")
                                        or ""
                                    ).strip()
                                    if c:
                                        codes.append(c)
    except Exception:
        pass

    VAT_CODE_CACHE[sid] = codes
    return codes


def _safe_parse_json_str(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip().startswith("{"):
        try:
            obj = json.loads(val)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


def _to_float_or_none(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return None


def fetch_site_api_report(
    session: requests.Session,
    site_id: str,
    report_date: str | None = None,
    vat_code: str = "",
) -> dict[str, Any]:
    sid = (site_id or "").strip()
    rdate = (report_date or today_ist_report_date()).strip()
    if not sid.isdigit():
        return {
            "ok": False,
            "site_id": sid,
            "report_date": rdate,
            "vat_codes": [],
            "total": 0,
            "success": 0,
            "failed": 0,
            "initial_count": 0,
            "final_count": 0,
            "pending_final_count": 0,
            "latest_ts": "",
            "latest_result": "",
            "pairs": [],
            "rows": [],
            "error": "Site ID missing",
        }

    referer_vat = (
        f"{BASE_URL}/{UAC}/Site/{sid}/Vat/{quote(vat_code)}/ApiReport"
        if vat_code
        else f"{BASE_URL}/{UAC}/Site/{sid}/Dashboard"
    )
    vat_codes = fetch_vat_codes_for_site(session, sid, referer_vat)
    if vat_code and vat_code not in vat_codes:
        vat_codes.insert(0, vat_code)

    cache_key = f"{sid}:{rdate}"
    cached = API_REPORT_CACHE.get(cache_key)
    if cached and (time.time() - cached.get("_ts", 0)) < 45:
        return cached

    params: dict[str, str] = {
        "sEcho": "1",
        "iColumns": "18",
        "sColumns": "," * 17,
        "iDisplayStart": "0",
        "iDisplayLength": "30",
        "sSearch": "",
        "bRegex": "false",
        "strReportDate": rdate,
        "strSearch": "",
        "siteId": sid,
        "_": str(_ts_ms()),
    }
    cols = [
        "Id",
        "AccountId",
        "siteId",
        "UserId",
        "ExciseApiUrl",
        "UnitCode",
        "VatCode",
        "RequestId",
        "Timestamp",
        "EncryptedRequest",
        "DecryptedRequest",
        "DecryptedResponse",
        "UserIp",
        "Status",
        "RequestType",
        "Process",
        "Result",
        "IsDeleted",
    ]
    for idx, col in enumerate(cols):
        params[f"mDataProp_{idx}"] = col
        params[f"sSearch_{idx}"] = ""
        params[f"bRegex_{idx}"] = "false"
        params[f"bSearchable_{idx}"] = "true"

    rows: list[dict[str, Any]] = []
    err_msg = ""
    try:
        r = session.get(
            EXCISE_REQ_WS_URL,
            params=params,
            headers={
                "Accept": "*/*",
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": referer_vat,
            },
            timeout=22,
        )
        if not r.ok:
            err_msg = f"HTTP {r.status_code}"
        else:
            txt = (r.text or "").strip()
            if txt.startswith('"') and txt.endswith('"'):
                try:
                    txt = json.loads(txt)
                except Exception:
                    pass
            if txt and not str(txt).startswith("<"):
                parsed = json.loads(txt) if isinstance(txt, str) else txt
                raw_rows = _extract_list_from_json_payload(parsed)
                if not raw_rows and isinstance(parsed, dict):
                    for k in ("aaData", "data", "rows", "Result", "result"):
                        if isinstance(parsed.get(k), list):
                            raw_rows = [
                                x for x in parsed[k] if isinstance(x, dict)
                            ]
                            break
                rows = raw_rows
    except Exception as exc:
        err_msg = str(exc)

    success_cnt = 0
    failed_cnt = 0
    initial_cnt = 0
    final_cnt = 0
    latest_ts = ""
    latest_result = ""

    enriched_rows: list[dict[str, Any]] = []
    grouped_by_req: dict[str, dict[str, Any]] = {}

    for item in rows:
        dec_req = _safe_parse_json_str(item.get("DecryptedRequest"))
        dec_resp = _safe_parse_json_str(item.get("DecryptedResponse"))

        proc_raw = str(
            item.get("Process") or dec_req.get("process") or ""
        ).strip().upper()
        if proc_raw in ("I", "INITIAL"):
            proc_code = "I"
            proc_label = "INITIAL"
            initial_cnt += 1
        elif proc_raw in ("F", "FINAL"):
            proc_code = "F"
            proc_label = "FINAL"
            final_cnt += 1
        else:
            proc_code = proc_raw or "?"
            proc_label = proc_raw or "UNKNOWN"

        req_id = str(
            item.get("RequestId")
            or dec_req.get("requestId")
            or (dec_resp.get("data") or {}).get("RequestId")
            or "N/A"
        ).strip()
        vat_c = str(
            item.get("VatCode") or dec_req.get("vatCode") or "-"
        ).strip()
        unit_c = str(
            item.get("UnitCode") or dec_req.get("unitCode") or "-"
        ).strip()
        req_type = str(
            item.get("RequestType") or dec_req.get("upDown") or ""
        ).strip()
        ts_val = str(item.get("Timestamp") or item.get("DateTime") or "").strip()

        bl_val = dec_req.get("bl")
        len_val = dec_req.get("length")

        resp_status = str(
            dec_resp.get("userDisplayMesg")
            or dec_resp.get("status")
            or item.get("Result")
            or item.get("Status")
            or ""
        ).strip()

        st_low = f"{item.get('Status') or ''} {dec_resp.get('status') or ''} {dec_resp.get('userDisplayMesg') or ''} {item.get('Result') or ''}".lower()
        if (
            str(dec_resp.get("errorCode") or "") == "200"
            or "success" in st_low
            or re.search(r"\bs\b", st_low)
        ) and not any(
            w in st_low
            for w in ("fail", "error", "invalid", "reject", "timeout")
        ):
            success_cnt += 1
        elif st_low.strip():
            failed_cnt += 1

        if ts_val and not latest_ts:
            latest_ts = ts_val
        if resp_status and not latest_result:
            latest_result = f"[{proc_code}] {req_id} → {resp_status}"[:90]

        entry = {
            "request_id": req_id,
            "process": proc_code,
            "process_label": proc_label,
            "vat_code": vat_c,
            "unit_code": unit_c,
            "request_type": req_type,
            "timestamp": ts_val,
            "bl": bl_val,
            "length": len_val,
            "status_text": resp_status or "N/A",
        }
        enriched_rows.append(entry)

        pair_key = f"{req_id}|{vat_c}"
        pair_obj = grouped_by_req.setdefault(
            pair_key,
            {
                "request_id": req_id,
                "vat_code": vat_c,
                "unit_code": unit_c,
                "request_type": req_type,
                "initial": None,
                "final": None,
            },
        )
        if req_type and not pair_obj.get("request_type"):
            pair_obj["request_type"] = req_type
        if proc_code == "I" and pair_obj["initial"] is None:
            pair_obj["initial"] = entry
        elif proc_code == "F" and pair_obj["final"] is None:
            pair_obj["final"] = entry

    pending_final_cnt = 0
    for p in grouped_by_req.values():
        ini = p.get("initial")
        fin = p.get("final")
        if ini and not fin:
            pending_final_cnt += 1
            p["delta_text"] = "Pending Final (F)"
        elif ini and fin:
            b1 = _to_float_or_none(ini.get("bl"))
            b2 = _to_float_or_none(fin.get("bl"))
            l1 = _to_float_or_none(ini.get("length"))
            l2 = _to_float_or_none(fin.get("length"))
            parts_d: list[str] = []
            if b1 is not None and b2 is not None:
                parts_d.append(f"ΔL: {b2 - b1:+.2f}".rstrip("0").rstrip("."))
            if l1 is not None and l2 is not None:
                parts_d.append(f"Δmm: {l2 - l1:+.2f}".rstrip("0").rstrip("."))
            p["delta_text"] = " | ".join(parts_d) if parts_d else "Paired I→F"
        else:
            p["delta_text"] = "Final Only"

    res_obj = {
        "ok": not bool(err_msg),
        "site_id": sid,
        "report_date": rdate,
        "vat_codes": vat_codes,
        "total": len(rows),
        "success": success_cnt,
        "failed": failed_cnt,
        "initial_count": initial_cnt,
        "final_count": final_cnt,
        "pending_final_count": pending_final_cnt,
        "latest_ts": latest_ts,
        "latest_result": latest_result,
        "pairs": list(grouped_by_req.values())[:10],
        "rows": enriched_rows[:16],
        "error": err_msg,
        "_ts": time.time(),
    }
    API_REPORT_CACHE[cache_key] = res_obj
    return res_obj


def _discover_dashboard_tags_and_page(
    session: requests.Session, site_id: str
) -> tuple[str, str]:
    sid = str(site_id or "").strip()
    dash_page_url = f"{BASE_URL}/{UAC}/Site/{sid}/Dashboard"
    if sid in SITE_TAGS_CACHE and SITE_TAGS_CACHE[sid]:
        return ",".join(SITE_TAGS_CACHE[sid]), dash_page_url

    discovered_tags: list[str] = []
    seen_tags: set[str] = set()

    def add_tag(t: str) -> None:
        clean = " ".join(str(t or "").split()).strip()
        if (
            clean
            and len(clean) <= 45
            and clean.lower() not in ("timestamps", "null", "undefined")
            and clean not in seen_tags
        ):
            seen_tags.add(clean)
            discovered_tags.append(clean)

    try:
        r = session.get(dash_page_url, timeout=20, allow_redirects=True)
        if r.ok and "account/login" not in r.url.lower():
            raw = r.text or ""
            for m in re.finditer(r"[?&]tags=([^&\"'\s]+)", raw, re.IGNORECASE):
                for part in m.group(1).replace("%2C", ",").split(","):
                    add_tag(part)
            for m in re.finditer(
                r"\b((?:EST|LT|CBH|CBT|CBV|CET|CMFM|FM|FL|BT|BL|SV|AS|T)[A-Za-z0-9\-_]*)\b",
                raw,
            ):
                tok = m.group(1)
                if len(tok) >= 3 and any(ch.isdigit() for ch in tok):
                    add_tag(tok)
    except Exception:
        pass

    vat_codes = fetch_vat_codes_for_site(session, sid, dash_page_url)
    for vc in vat_codes:
        add_tag(vc)
        if not vc.upper().endswith("-BL"):
            add_tag(f"{vc}-BL")

    if not discovered_tags:
        fallback_pool = (
            "EST-1,EST-1-BL,EST-2,EST-2-BL,EST-3,EST-3-BL,EST-4,EST-4-BL,EST-5,EST-5-BL,"
            "LT2023-A,LT2023-A-BL,LT2023-B,LT2023-B-BL,LT2023-C,LT2023-C-BL,"
            "LT706-A,LT706-A-BL,LT706-B,LT706-B-BL,"
            "LT708-A,LT708-A-BL,LT708-B,LT708-B-BL,LT708-C,LT708-C-BL,LT708-D,LT708-D-BL,LT708-E,LT708-E-BL,LT708-F,LT708-F-BL,"
            "CBH-1-BL,CBH-2-BL,CBH-3-BL,CBH-4-BL,CBH-5-BL,"
            "CBT-1-BL,CBT-2-BL,CBT-4-BL,CBT-6-BL,CBT-8-BL,"
            "CET-1,CET-1-BL,CET-2,CET-2-BL,CET-3,CET-3-BL,"
            "CMFM-1-TOT,CMFM-2-TOT,CMFM-3-TOT,CMFM-4-TOT"
        )
        for t in fallback_pool.split(","):
            add_tag(t)

    SITE_TAGS_CACHE[sid] = discovered_tags
    return ",".join(discovered_tags), dash_page_url


def _format_num(val: Any) -> str:
    if isinstance(val, float):
        if abs(val) >= 1000:
            return f"{val:,.2f}".rstrip("0").rstrip(".")
        return f"{val:.2f}".rstrip("0").rstrip(".")
    return str(val).strip()


def _classify_reading_category(tag_name: str, unit_str: str) -> str:
    t_up = (tag_name or "").upper()
    u_low = (unit_str or "").strip().lower()
    if u_low == "mm":
        return "level_mm"
    if u_low in ("l", "ltr", "lit", "bl") or t_up.endswith(("-BL", "-L")):
        return "volume_l"
    return "instrument"


def _parse_dashboard_stats_and_tags(
    payload: dict[str, Any],
) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []

    unit_map: dict[str, str] = {}
    raw_tags = payload.get("Tags") or payload.get("tags") or []
    if isinstance(raw_tags, list):
        for t_item in raw_tags:
            if isinstance(t_item, (list, tuple)) and len(t_item) >= 1:
                t_name = str(t_item[0] or "").strip()
                t_unit = (
                    str(t_item[1] or "").strip() if len(t_item) >= 2 else ""
                )
                if t_name:
                    unit_map[t_name] = t_unit
            elif isinstance(t_item, dict):
                t_name = str(
                    t_item.get("Tag") or t_item.get("Name") or ""
                ).strip()
                t_unit = str(t_item.get("Unit") or "").strip()
                if t_name:
                    unit_map[t_name] = t_unit

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_stats = payload.get("Stats") or payload.get("stats") or []

    if isinstance(raw_stats, list):
        for s_item in raw_stats:
            if not isinstance(s_item, (list, tuple)) or len(s_item) < 2:
                continue
            tag_name = str(s_item[0] or "").strip()
            if not tag_name or tag_name.lower() == "timestamps":
                continue

            latest_val = s_item[1] if len(s_item) > 1 else None
            avg_val = s_item[2] if len(s_item) > 2 else None

            chosen = latest_val if latest_val is not None else avg_val
            if chosen is None or str(chosen).strip() == "":
                continue

            val_str = _format_num(chosen)
            unit_str = unit_map.get(tag_name, "")
            if not unit_str and tag_name.upper().endswith("-BL"):
                unit_str = "L"

            extra_str = "avg" if (latest_val is None and avg_val is not None) else ""
            cat = _classify_reading_category(tag_name, unit_str)
            dedup = f"{tag_name.lower()}|{val_str}|{unit_str}"
            if dedup not in seen:
                seen.add(dedup)
                results.append(
                    {
                        "name": tag_name[:60],
                        "value": val_str[:40],
                        "unit": unit_str[:16],
                        "extra": extra_str,
                        "category": cat,
                    }
                )

    return results


def group_readings_by_tank(
    instruments: list[dict[str, str]],
) -> list[str]:
    """
    Groups mm + L (-BL) + instrument/totalizer readings by base tank/tag key
    so Distillery & Bottling tanks appear cleanly together.
    """
    groups: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []

    for item in instruments:
        raw_name = item.get("name", "")
        val_str = item.get("value", "")
        unit_str = item.get("unit", "")
        cat = item.get("category", "instrument")
        disp = f"{val_str} {unit_str}".strip()

        base = re.sub(r"-(BL|L|TOT|MM)$", "", raw_name, flags=re.IGNORECASE).strip()
        if not base:
            base = raw_name

        if base not in groups:
            groups[base] = {"mm": [], "l": [], "inst": []}
            order.append(base)

        if cat == "level_mm":
            groups[base]["mm"].append(f"{raw_name}: {disp}")
        elif cat == "volume_l":
            groups[base]["l"].append(f"{raw_name}: {disp}")
        else:
            groups[base]["inst"].append(f"{raw_name}: {disp}")

    lines: list[str] = []
    for base in order[:16]:
        g = groups[base]
        parts: list[str] = []
        if g["mm"]:
            parts.append(", ".join(g["mm"]))
        if g["l"]:
            parts.append(", ".join(g["l"]))
        if g["inst"]:
            parts.append(", ".join(g["inst"]))
        lines.append(
            f"  ▸ <b>{esc(base)}</b> → <code>{esc(' | '.join(parts))}</code>"
        )
    return lines


def scrape_site_dashboard_details(
    session: requests.Session, dev: dict[str, Any]
) -> dict[str, Any]:
    site_id = str(dev.get("site_id") or "").strip()
    site_code = str(dev.get("site_code") or "").strip()

    if not site_id.isdigit():
        return {
            "ok": False,
            "site_id": "",
            "site_code": site_code,
            "site_name_ws": "",
            "url": "",
            "error": "Is device ke liye Site ID nahi mila.",
            "instruments": [],
            "api_report": None,
        }

    cache_key = f"dash:{site_id}"
    cached = DASHBOARD_CACHE.get(cache_key)
    if cached and (time.time() - cached.get("_ts", 0)) < 45:
        return cached

    tags_str, dash_page_url = _discover_dashboard_tags_and_page(session, site_id)
    site_name_ws = (
        fetch_site_name_by_code(session, site_code, dash_page_url)
        if site_code
        else ""
    )

    instruments: list[dict[str, str]] = []
    err_msg = ""

    try:
        r = session.get(
            DASHBOARD_WS_URL,
            params={
                "uac": UAC,
                "site": site_id,
                "mode": "1d",
                "avg": "nul",
                "tags": tags_str,
                "tagTypes": "1",
                "tzo": "-330",
                "_": str(_ts_ms()),
            },
            headers=_ajax_headers(dash_page_url),
            timeout=22,
        )
        if not r.ok:
            err_msg = f"Dashboard WS HTTP {r.status_code}"
        else:
            txt = (r.text or "").strip()
            if txt.startswith('"') and txt.endswith('"'):
                try:
                    txt = json.loads(txt)
                except Exception:
                    pass
            if txt and not str(txt).startswith("<"):
                payload = json.loads(txt) if isinstance(txt, str) else txt
                instruments = _parse_dashboard_stats_and_tags(payload)
            else:
                err_msg = "Empty or non-JSON dashboard response"
    except Exception as exc:
        err_msg = f"Dashboard WS error: {exc}"

    api_report = fetch_site_api_report(session, site_id)

    res = {
        "ok": not bool(err_msg),
        "site_id": site_id,
        "site_code": site_code,
        "site_name_ws": site_name_ws,
        "url": dash_page_url,
        "instruments": instruments[:35],
        "api_report": api_report,
        "error": err_msg,
        "_ts": time.time(),
    }
    DASHBOARD_CACHE[cache_key] = res
    return res


def fetch_devices_multi_strategy(
    session: requests.Session,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = datetime.now(timezone.utc)
    meta: dict[str, Any] = {
        "server_error_500": False,
        "html_length": 0,
        "tr_total": 0,
        "unparsed_ts_count": 0,
        "source": "none",
    }

    ws_devs = fetch_devices_from_ws_api(session, now)
    if ws_devs:
        meta["source"] = "ws-ef-devices"
        meta["unparsed_ts_count"] = sum(
            1 for d in ws_devs if d["last_seen"] is None
        )
        return ws_devs, meta

    resp = session.get(DEVICES_PAGE_URL, timeout=25)
    resp.raise_for_status()
    raw_html = resp.text or ""
    meta["html_length"] = len(raw_html)

    if (
        not raw_html
        or "500 - internal server error" in raw_html.lower()
        or "internal server error" in raw_html.lower()
    ):
        meta["server_error_500"] = True
        return [], meta

    soup = BeautifulSoup(raw_html, "html.parser")
    devs: list[dict[str, Any]] = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        meta["tr_total"] += 1
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        if len(cells) < 2:
            continue
        row_sid = ""
        for a in tr.find_all("a", href=True):
            m = re.search(r"/Site/(\d+)/Dashboard", str(a["href"]), re.IGNORECASE)
            if m:
                row_sid = m.group(1)
                break
        rec = normalize_device_record(
            serial_no=cells[1] if len(cells) > 1 else cells[0],
            industry=cells[2] if len(cells) > 2 else "N/A",
            site_type=cells[3] if len(cells) > 3 else "N/A",
            device_type=cells[4] if len(cells) > 4 else "N/A",
            firmware=cells[5] if len(cells) > 5 else "N/A",
            raw_status=cells[-2] if len(cells) >= 2 else "",
            raw_last_seen=cells[-1],
            now_utc=now,
            site_id=row_sid,
        )
        devs.append(rec)

    if devs:
        meta["source"] = "html-table"
        meta["unparsed_ts_count"] = sum(1 for d in devs if d["last_seen"] is None)
        return devs, meta

    meta["source"] = "0-found"
    return [], meta


# ==============================================================================
# AGING RANGE FILTER & ANALYSIS
# ==============================================================================
AGING_RANGE_CONFIG: dict[str, tuple[str, timedelta | None, timedelta]] = {
    "1d": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 1 ᴅᴀʏ (≤ 24ʜ)", None, timedelta(days=1)),
    "1w": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 1 ᴡᴇᴇᴋ (≤ 7ᴅ)", None, timedelta(days=7)),
    "24d": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 24 ᴅᴀʏꜱ (≤ 24ᴅ)", None, timedelta(days=24)),
    "11m": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 11 ᴍᴏɴᴛʜꜱ (≤ 330ᴅ)", None, timedelta(days=330)),
    "1y": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 1 ʏᴇᴀʀ (≤ 365ᴅ)", None, timedelta(days=365)),
    "gt1y": (
        "ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ > 1 ʏᴇᴀʀ (365ᴅ+)",
        timedelta(days=365),
        timedelta(days=36500),
    ),
}


def filter_devices_by_aging_range(
    devices: list[dict[str, Any]], bucket_key: str
) -> list[dict[str, Any]]:
    _, min_td, max_td = AGING_RANGE_CONFIG.get(
        bucket_key, ("", None, timedelta(days=1))
    )
    matched: list[dict[str, Any]] = []
    for d in devices:
        if d.get("is_connected"):
            continue
        off_td: timedelta | None = d.get("offline_duration")
        if off_td is None or off_td <= timedelta(0):
            continue
        if min_td is not None and off_td <= min_td:
            continue
        if off_td <= max_td:
            matched.append(d)

    matched.sort(
        key=lambda x: x["offline_duration"].total_seconds()
        if x.get("offline_duration")
        else 0,
        reverse=True,
    )
    return matched


def run_self_analysis(session: requests.Session) -> dict[str, Any]:
    devices, meta = fetch_devices_multi_strategy(session)

    assigned_devices = [d for d in devices if not d["is_unassigned"]]
    unassigned_list = [d for d in devices if d["is_unassigned"]]
    online_list = [d for d in devices if d["is_connected"]]
    offline_list = [d for d in devices if not d["is_connected"]]
    stale_list = [d for d in devices if d.get("is_stale")]

    offline_list.sort(
        key=lambda x: x["offline_duration"].total_seconds()
        if x.get("offline_duration")
        else 0,
        reverse=True,
    )

    aging_counts = {
        k: len(filter_devices_by_aging_range(devices, k))
        for k in AGING_RANGE_CONFIG
    }

    site_offline_counts: dict[str, int] = {}
    fw_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}

    for d in devices:
        fw = d["firmware"] or "Unknown"
        fw_counts[fw] = fw_counts.get(fw, 0) + 1

        dtype = d["device_type"] or "Unknown"
        type_counts[dtype] = type_counts.get(dtype, 0) + 1

        if not d["is_connected"] and not d["is_unassigned"]:
            site_label = (
                f"{d['industry']}({d['site']})"
                if d["site"] != "N/A"
                else d["industry"]
            )
            site_offline_counts[site_label] = (
                site_offline_counts.get(site_label, 0) + 1
            )

    top_sites = sorted(
        site_offline_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:5]

    return {
        "checked_at_ist": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "total": len(devices),
        "assigned_count": len(assigned_devices),
        "connected": len(online_list),
        "disconnected_count": len(offline_list),
        "stale_count": len(stale_list),
        "unassigned_count": len(unassigned_list),
        "aging_counts": aging_counts,
        "all_devices": devices,
        "assigned_devices": assigned_devices,
        "online_list": online_list,
        "offline_list": offline_list,
        "stale_list": stale_list,
        "unassigned_list": unassigned_list,
        "top_offline_sites": top_sites,
        "firmware_breakdown": fw_counts,
        "device_type_breakdown": type_counts,
        "meta": meta,
    }


def build_csv_bytes(devices: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Sr.No",
            "Client",
            "Serial No",
            "Site ID",
            "Site Code",
            "Dashboard URL",
            "Industry Name",
            "Site Type",
            "Assignment",
            "Device Type",
            "Firmware",
            "Status",
            "Stale",
            "Last Seen (IST)",
        ]
    )
    for idx, d in enumerate(devices, start=1):
        writer.writerow(
            [
                idx,
                CLIENT_NAME,
                d.get("serial_no", "N/A"),
                d.get("site_id", ""),
                d.get("site_code", ""),
                d.get("dashboard_url", ""),
                d.get("industry", "N/A"),
                d.get("site", "N/A"),
                "UNASSIGNED" if d.get("is_unassigned") else "ASSIGNED",
                d.get("device_type", "N/A"),
                d.get("firmware", "N/A"),
                d.get("conn_label", "N/A"),
                "YES" if d.get("is_stale") else "NO",
                d.get("display_time", "N/A"),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


# ==============================================================================
# PROFESSIONAL COMPACT GROQ AI ENGINE (STRICT TEXT-ONLY MODELS)
# ==============================================================================
_BLOCKED_MODEL_TOKENS = (
    "whisper",
    "orpheus",
    "arabic",
    "saudi",
    "tts",
    "playai",
    "guard",
    "prompt-guard",
    "embed",
    "audio",
    "speech",
)

_PREFERRED_TEXT_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
    "qwen/qwen3-32b",
    "llama3-70b-8192",
    "llama3-8b-8192",
]


def _is_safe_text_model(model_id: str) -> bool:
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    return not any(tok in mid for tok in _BLOCKED_MODEL_TOKENS)


def get_available_groq_models() -> list[str]:
    if not GROQ_API_KEY:
        return list(_PREFERRED_TEXT_MODELS[:4])
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=10,
        )
        if r.ok:
            data = r.json().get("data", [])
            live_ids = [
                str(m.get("id")).strip()
                for m in data
                if isinstance(m, dict)
                and m.get("id")
                and _is_safe_text_model(str(m.get("id")))
            ]
            ordered = [m for m in _PREFERRED_TEXT_MODELS if m in live_ids] + [
                m for m in live_ids if m not in _PREFERRED_TEXT_MODELS
            ]
            if ordered:
                return ordered[:4]
    except Exception:
        pass
    return list(_PREFERRED_TEXT_MODELS[:4])


def enrich_context_with_live_dashboards(
    session: requests.Session | None,
    analysis: dict[str, Any],
    query: str = "",
) -> dict[str, dict[str, Any]]:
    live_readings: dict[str, dict[str, Any]] = {}
    if not session:
        return live_readings

    q = (query or "").strip().lower()
    candidates: list[dict[str, Any]] = []

    if q:
        for d in analysis.get("all_devices", []):
            if (
                q in str(d.get("industry", "")).lower()
                or q in str(d.get("site", "")).lower()
                or q in str(d.get("serial_no", "")).lower()
                or q in str(d.get("site_id", "")).lower()
                or q in str(d.get("site_code", "")).lower()
            ):
                candidates.append(d)

    for d in analysis.get("assigned_devices", []):
        if d not in candidates:
            candidates.append(d)
        if len(candidates) >= 5:
            break

    for dev in candidates[:5]:
        try:
            dash = scrape_site_dashboard_details(session, dev)
            rep = dash.get("api_report") or {}
            live_readings[dev.get("serial_no", "N/A")] = {
                "industry": dev.get("industry"),
                "site_type": dev.get("site"),
                "site_id": dash.get("site_id") or dev.get("site_id"),
                "instruments": dash.get("instruments", [])[:12],
                "api_report": {
                    "date": rep.get("report_date"),
                    "total": rep.get("total", 0),
                    "initial": rep.get("initial_count", 0),
                    "final": rep.get("final_count", 0),
                    "pending_final": rep.get("pending_final_count", 0),
                    "success": rep.get("success", 0),
                    "failed": rep.get("failed", 0),
                },
            }
        except Exception:
            continue

    return live_readings


def build_project_context(
    analysis: dict[str, Any],
    live_readings: dict[str, dict[str, Any]] | None = None,
) -> str:
    return (
        f"Client: {CLIENT_NAME} | Checked: {analysis.get('checked_at_ist')}\n"
        f"Total: {analysis.get('total')} | Assigned: {analysis.get('assigned_count')} | "
        f"Connected: {analysis.get('connected')} | Disconnected: {analysis.get('disconnected_count')} | "
        f"Stale (>30m): {analysis.get('stale_count', 0)} | Unassigned: {analysis.get('unassigned_count')}\n"
        f"Aging: {analysis.get('aging_counts')}\n"
        f"Top Offline Sites: {analysis.get('top_offline_sites')}\n"
        f"Live Sample: {json.dumps(live_readings or {}, ensure_ascii=False)}"
    )


def call_groq_ai(
    system_prompt: str, user_prompt: str, analysis: dict[str, Any]
) -> str:
    if not GROQ_API_KEY:
        return "GROQ_API_KEY set nahi hai."

    models = [m for m in get_available_groq_models() if _is_safe_text_model(m)]
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    last_err = ""

    for model in models:
        try:
            r = requests.post(
                GROQ_CHAT_URL,
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 550,
                },
                timeout=25,
            )
            if r.ok:
                data = r.json()
                txt = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
                if txt:
                    return txt
            else:
                last_err = f"{model} HTTP {r.status_code}"
        except Exception as exc:
            last_err = f"{model}: {exc}"

    ages = analysis.get("aging_counts", {})
    return (
        f"Compact Summary ({last_err or 'Text fallback'}):\n"
        f"• Total: {analysis.get('total', 0)} | Online: {analysis.get('connected', 0)} | Off: {analysis.get('disconnected_count', 0)}\n"
        f"• Aging: <1D {ages.get('1d', 0)}, <1W {ages.get('1w', 0)}, <24D {ages.get('24d', 0)}, <11M {ages.get('11m', 0)}"
    )


def generate_ai_analysis(
    session: requests.Session | None, analysis: dict[str, Any]
) -> str:
    live = enrich_context_with_live_dashboards(session, analysis)
    ctx = build_project_context(analysis, live)
    sys_p = (
        f"Aap {CLIENT_NAME} telemetry suite ke ek polite, calm, aur professional operations advisor hain. "
        "Hinglish mein sirf 4–5 crisp main points dein—zyada lamba ya complex mat likhein. "
        "Markdown tables ya ** asterisks bilkul use na karein."
    )
    usr_p = f"TELEMETRY SNAPSHOT:\n{ctx}"
    return call_groq_ai(sys_p, usr_p, analysis)


def answer_project_question(
    user_question: str,
    session: requests.Session | None,
    analysis: dict[str, Any],
) -> str:
    live = enrich_context_with_live_dashboards(
        session, analysis, query=user_question
    )
    ctx = build_project_context(analysis, live)
    sys_p = (
        f"Aap {CLIENT_NAME} telemetry bot ke professional aur helpful assistant hain. "
        "Diye gaye live data ke hisab se seedha, saaf aur compact jawab dein. "
        "Markdown table ya ** asterisks na lagayein."
    )
    usr_p = f"LIVE DATA:\n{ctx}\n\nQUESTION:\n{user_question}"
    return call_groq_ai(sys_p, usr_p, analysis)


def require_auth(chat_id: int) -> dict[str, Any] | None:
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        return None
    return sess_data


def _find_device_by_target(
    devices: list[dict[str, Any]], target: str
) -> dict[str, Any] | None:
    t = (target or "").strip()
    if not t:
        return None
    m_hash = re.match(r"^#?(\d+)\b", t)
    if m_hash and len(t) <= 4:
        num = int(m_hash.group(1))
        if 1 <= num <= len(devices):
            return devices[num - 1]
    if t.isdigit():
        num = int(t)
        if 1 <= num <= len(devices):
            return devices[num - 1]
        for d in devices:
            if (
                str(d.get("serial_no") or "") == t
                or str(d.get("site_id") or "") == t
                or str(d.get("site_code") or "") == t
            ):
                return d
        return None
    t_low = t.lower()
    for d in devices:
        if (
            t_low in str(d.get("industry", "")).lower()
            or t_low in str(d.get("site", "")).lower()
            or t_low in str(d.get("serial_no", "")).lower()
            or t_low in str(d.get("site_code", "")).lower()
        ):
            return d
    return None


# ==============================================================================
# ACCESS & MAINTENANCE GUARD
# ==============================================================================
async def check_access(chat_id: int, user_id: int | None, bot) -> bool:
    if is_admin(user_id):
        return True
    if not BOT_ENABLED:
        await safe_send_html(
            bot,
            chat_id,
            f"{themed_box(sc('BOT CURRENTLY OFF'))}\n{get_theme()['bar']}\n  ▸ Bot abhi temporary OFF hai.",
        )
        return False
    if MAINTENANCE_MODE:
        await safe_send_html(
            bot,
            chat_id,
            f"{themed_box(sc('MAINTENANCE MODE'))}\n{get_theme()['bar']}\n  ▸ {esc(MAINTENANCE_MSG)}",
        )
        return False
    return True


# ==============================================================================
# VIEWS, THEMES, NOTIFICATION DEMO & SHIFT SUMMARY
# ==============================================================================
async def send_start_view(chat_id: int, user_id: int | None, bot) -> None:
    existing = require_auth(chat_id)
    if existing:
        MENU_STATE[chat_id] = "main"
        MENU_STACK[chat_id] = []
        AWAITING_INPUT.pop(chat_id, None)
        th = get_theme()
        msg = (
            f"{themed_box(esc(CLIENT_NAME), sc('AUTHORITY TELEMETRY'))}\n"
            f"{th['bar']}\n"
            f"  {th['bullet']} Account   : <code>{esc(existing.get('username'))}</code>\n"
            f"  {th['bullet']} Theme     : <code>{esc(th['label'])}</code>\n"
            f"  {th['bullet']} Notif Mode: <code>{esc(NOTIF_STYLE)}</code>\n"
            f"  {th['bullet']} Sync Rate : <code>{REFRESH_INTERVAL_SEC}s</code>\n\n"
            "  ▸ Niche buttons se menus, sub-menus, watchlist aur shift summary access karein."
        )
        await safe_send_html(
            bot, chat_id, msg, reply_markup=main_reply_keyboard(user_id)
        )
        return

    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    th = get_theme()
    await safe_send_html(
        bot,
        chat_id,
        (
            f"{themed_box(esc(CLIENT_NAME), sc('AUTHORITY TELEMETRY'))}\n"
            f"{th['bar']}\n\n"
            "  ▸ Kripya apna <b>Login ID / Email</b> bhejein (ya <code>🎮 Games Lounge</code> try karein):"
        ),
        reply_markup=guest_keyboard_for_chat(chat_id),
    )


async def send_manual_view(
    chat_id: int, user_id: int | None, bot
) -> None:
    th = get_theme()
    b = th["bullet"]
    manual_text = (
        f"{themed_box(sc('OPERATOR MANUAL'))}\n"
        f"{th['bar']}\n\n"
        "<b>1. Login &amp; Guest Games Lounge</b>\n"
        f"  {b} <code>/login</code> ya <code>🔐 Login</code> se portal login karein.\n"
        f"  {b} Login ka wait karte waqt <code>🎮 Games Lounge</code> mein 3 mini-games khelein.\n\n"
        "<b>2. Navigation &amp; Menus</b>\n"
        f"  {b} Har sub-menu mein <code>⬅ Back</code> aur <code>🏠 Main Menu</code> milta hai.\n"
        f"  {b} <code>🌐 Site Dashboard</code> → Tanks (`mm` + `L` grouped) &amp; Excise `I`→`F` report.\n"
        f"  {b} <code>📡 API Report</code> → `RequestId` pairs, Initial/Final &amp; net delta.\n"
        f"  {b} <code>⭐ Watchlist</code> &amp; <code>📋 Shift Summary</code> → Quick ops.\n\n"
        "<b>3. Commands</b>\n"
        "  • <code>/status</code> | <code>/allsite</code> | <code>/dash &lt;site&gt;</code> | <code>/apireport &lt;site&gt; [date]</code>\n"
        "  • <code>/instruments</code> | <code>/age 1d</code> | <code>/ai &lt;question&gt;</code> | <code>/ai_analyze</code>"
    )
    await safe_send_html(
        bot,
        chat_id,
        manual_text,
        reply_markup=current_keyboard_for_chat(chat_id, user_id),
    )


async def open_games_lounge(
    chat_id: int, user_id: int | None, bot
) -> None:
    push_menu_state(chat_id, "games_menu")
    GAME_SESSIONS.pop(chat_id, None)
    msg = (
        f"{themed_box(sc('GUEST GAMES LOUNGE'))}\n"
        f"{get_theme()['bar']}\n"
        "  ▸ <code>🎲 Tic-Tac-Toe</code> — 3x3 vs Smart Bot\n"
        "  ▸ <code>🔢 Number Guess</code> — 1 se 100 secret number\n"
        "  ▸ <code>✂ Rock Paper Scissors</code> — Best of 5 rounds"
    )
    await safe_send_html(
        bot, chat_id, msg, reply_markup=games_lounge_keyboard()
    )


async def open_dashboard_menu(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    push_menu_state(chat_id, "dash_menu")

    devs = analysis.get("assigned_devices") or []
    quick_lines = []
    for i, d in enumerate(devs[:6], start=1):
        dot = small_status_dot(bool(d.get("is_connected")))
        sid = f" [ID:{d['site_id']}]" if d.get("site_id") else ""
        quick_lines.append(
            f"  {i}. {dot} <b>{esc(d.get('industry'))}</b>{sid}"
        )

    msg = (
        f"{themed_box(sc('SITE DASHBOARD HUB'))}\n"
        f"{get_theme()['bar']}\n"
        + (
            "\n".join(quick_lines)
            if quick_lines
            else "  ▸ Koi site list nahi mili"
        )
        + "\n\n"
        "  ▸ Quick button dabayein, <code>🔎 Open Site by ID/Name</code>, ya <code>⬅ Back</code>."
    )
    await safe_send_html(
        bot, chat_id, msg, reply_markup=dashboard_sub_keyboard(chat_id)
    )


async def open_api_report_menu(chat_id: int, bot) -> None:
    if not require_auth(chat_id):
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    push_menu_state(chat_id, "api_menu")
    msg = (
        f"{themed_box(sc('EXCISE API REPORT'))}\n"
        f"{get_theme()['bar']}\n"
        f"  ▸ Today Date : <code>{esc(today_ist_report_date())}</code>\n"
        "  ▸ <code>📅 Today API Report</code> — Initial (I) &amp; Final (F) pairs + ΔL/Δmm\n"
        "  ▸ <code>🗓 Custom Date Report</code> — Site aur date (<code>DD-Mon-YYYY</code>)\n"
        "  ▸ <code>🏭 Site API Report</code> — Quick site report"
    )
    await safe_send_html(
        bot, chat_id, msg, reply_markup=api_report_sub_keyboard()
    )


async def open_aging_menu(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    push_menu_state(chat_id, "age_menu")
    ages = analysis.get("aging_counts", {})

    msg = (
        f"{themed_box(sc('AGING BUCKETS'))}\n"
        f"{get_theme()['bar']}\n"
        f"  ◦ <code>&lt; 1 Day (≤24h)    : {ages.get('1d', 0)}</code>\n"
        f"  ▪ <code>&lt; 1 Week (≤7d)    : {ages.get('1w', 0)}</code>\n"
        f"  • <code>&lt; 24 Days (≤24d)  : {ages.get('24d', 0)}</code>\n"
        f"  ◆ <code>&lt; 11 Months(≤330d): {ages.get('11m', 0)}</code>\n"
        f"  ◈ <code>&lt; 1 Year (≤365d)  : {ages.get('1y', 0)}</code>\n"
        f"  ▸ <code>&gt; 1 Year (365d+)  : {ages.get('gt1y', 0)}</code>"
    )
    await safe_send_html(
        bot, chat_id, msg, reply_markup=aging_sub_keyboard()
    )


async def open_ops_menu(chat_id: int, bot) -> None:
    if not require_auth(chat_id):
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    push_menu_state(chat_id, "ops_menu")
    msg = (
        f"{themed_box(sc('OPS & EXPORT'))}\n"
        f"{get_theme()['bar']}\n"
        "  ▸ <code>🩺 Health</code> | <code>📥 Export CSV</code>\n"
        "  ▸ <code>∙ Online</code> | <code>📌 Unassigned</code> | <code>❄ Stale Sites</code>"
    )
    await safe_send_html(bot, chat_id, msg, reply_markup=ops_sub_keyboard())


async def open_admin_panel(chat_id: int, bot) -> None:
    push_menu_state(chat_id, "admin_menu")
    th = get_theme()
    msg = (
        f"{themed_box(sc('ADMIN AUTHORITY'))}\n"
        f"{th['bar']}\n"
        f"  ▸ Bot Status   : <code>{'ON' if BOT_ENABLED else 'OFF'}</code>\n"
        f"  ▸ Maintenance  : <code>{'ON' if MAINTENANCE_MODE else 'OFF'}</code>\n"
        f"  ▸ UI Theme     : <code>{esc(th['label'])}</code>\n"
        f"  ▸ Notif Style  : <code>{esc(NOTIF_STYLE)}</code>\n"
        f"  ▸ Refresh Rate : <code>{REFRESH_INTERVAL_SEC}s</code>\n"
        f"  ▸ Active Chats : <code>{len(USER_SESSIONS)}</code>"
    )
    await safe_send_html(bot, chat_id, msg, reply_markup=admin_sub_keyboard())


async def open_theme_menu(chat_id: int, bot) -> None:
    push_menu_state(chat_id, "theme_menu")
    cur_label = get_theme()["label"]
    msg = (
        f"{themed_box(sc('UI THEMES'))}\n"
        f"{get_theme()['bar']}\n"
        f"  ▸ Current Theme: <b>{esc(cur_label)}</b>\n"
        "  • <b>Obsidian</b> — Deep dark authority frames\n"
        "  • <b>Minimal</b> — Clean executive lines\n"
        "  • <b>Cyber</b> — Technical telemetry grid\n"
        "  • <b>Royal</b> — Decorative royal border"
    )
    await safe_send_html(bot, chat_id, msg, reply_markup=theme_sub_keyboard())


async def open_notif_demo_menu(chat_id: int, bot) -> None:
    push_menu_state(chat_id, "notif_menu")
    msg = (
        f"{themed_box(sc('NOTIFICATIONS & DEMO'))}\n"
        f"{get_theme()['bar']}\n"
        f"  ▸ Active Mode: <code>{esc(NOTIF_STYLE)}</code>\n"
        "  • Modes: <code>Detailed</code>, <code>Compact</code>, <code>Alerts Only</code>, <code>Digest</code>\n"
        "  • Niche <code>Demo: ...</code> buttons se live alert preview bhejein."
    )
    await safe_send_html(
        bot, chat_id, msg, reply_markup=notif_demo_sub_keyboard()
    )


async def send_notification_demo(chat_id: int, demo_kind: str, bot) -> None:
    sample_site = "Malbros / Sample Unit (Distillery)"
    sample_serial = "2210100999"
    sample_seen = datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")

    if demo_kind == "recovered":
        txt = (
            f"{themed_box('∙ ' + sc('DEMO: SITE RECOVERED'))}\n"
            f"  ▸ Site   : <b>{esc(sample_site)}</b>\n"
            f"  ▸ Serial : <b>{esc(sample_serial)}</b>\n"
            f"  ▸ Seen   : <code>{esc(sample_seen)}</code>"
        )
    else:
        badge_map = {
            "2h": "2ʜ ᴅᴏᴡɴ ALERT",
            "10h": "10ʜ ᴅᴏᴡɴ ALERT",
            "1d": "1ᴅ ᴅᴏᴡɴ ALERT",
        }
        badge = badge_map.get(demo_kind, "2ʜ ᴅᴏᴡɴ ALERT")
        if NOTIF_STYLE == "compact":
            txt = f"▸ • <b>{esc(sample_site)}</b> [<code>{esc(badge)}</code>] | Serial: <code>{esc(sample_serial)}</code>"
        else:
            txt = (
                f"{themed_box('• ' + esc('DEMO: ' + badge))}\n"
                f"  ▸ Site   : <b>{esc(sample_site)}</b>\n"
                f"  ▸ Serial : <b>{esc(sample_serial)}</b>\n"
                f"  ▸ Seen   : <code>{esc(sample_seen)}</code>"
            )

    await safe_send_html(
        bot, chat_id, txt, reply_markup=notif_demo_sub_keyboard()
    )


async def send_shift_summary(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    ages = analysis.get("aging_counts", {})
    th = get_theme()
    b = th["bullet"]

    top_lines = (
        "\n".join(
            f"  {i}. {esc(s)} — <code>{c} off</code>"
            for i, (s, c) in enumerate(analysis.get("top_offline_sites", []), 1)
        )
        or "  <code>Koi offline site nahi</code>"
    )

    msg = (
        f"{themed_box(esc(CLIENT_NAME) + ' — ' + sc('SHIFT HANDOVER'))}\n"
        f"{th['bar']}\n"
        f"  {b} Time      : <i>{esc(analysis.get('checked_at_ist'))}</i>\n"
        f"  {b} Fleet     : Total <b>{analysis.get('total', 0)}</b> | ∙ Online <b>{analysis.get('connected', 0)}</b> | • Off <b>{analysis.get('disconnected_count', 0)}</b>\n"
        f"  {b} Stale (>30m): <b>{analysis.get('stale_count', 0)}</b> | Unassigned: <b>{analysis.get('unassigned_count', 0)}</b>\n"
        f"  {b} Aging     : &lt;1D <code>{ages.get('1d', 0)}</code> | &lt;1W <code>{ages.get('1w', 0)}</code> | &lt;24D <code>{ages.get('24d', 0)}</code> | &lt;11M <code>{ages.get('11m', 0)}</code>\n\n"
        f"<b>{b} {sc('Priority Offline Sites')}</b>\n{top_lines}"
    )
    await safe_send_html(bot, chat_id, msg)


async def send_watchlist_view(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    wlist = WATCHLIST_SITES.get(chat_id) or []
    if not wlist:
        await safe_send_html(
            bot,
            chat_id,
            f"{themed_box(sc('WATCHLIST'))}\n  ▸ Watchlist khali hai. Dashboard menu se <code>⭐ Pin Site</code> use karein.",
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    devs = analysis.get("assigned_devices", [])

    lines = [
        themed_box(sc("PINNED WATCHLIST") + f" ({len(wlist)})"),
        get_theme()["bar"],
    ]
    for idx, key in enumerate(wlist[:10], start=1):
        dev = _find_device_by_target(devs, key)
        if dev:
            dot = small_status_dot(bool(dev.get("is_connected")))
            lines.append(
                f"  {idx}. {dot} <b>{esc(dev.get('industry'))}</b> "
                f"[ID:<code>{esc(dev.get('site_id') or '-')}</code>] | <code>{esc(dev.get('conn_label'))}</code>"
            )
        else:
            lines.append(f"  {idx}. <code>{esc(key)}</code>")

    await safe_send_html(bot, chat_id, "\n".join(lines))


async def send_stale_sites_view(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    stale = analysis.get("stale_list") or []
    if not stale:
        await safe_send_html(
            bot,
            chat_id,
            f"{themed_box(sc('STALE TELEMETRY'))}\n  ∙ <code>Koi connected-but-stale site (&gt;30m) nahi mili.</code>",
        )
        return

    lines = [
        themed_box(sc("CONNECTED BUT STALE") + f" ({len(stale)})"),
        get_theme()["bar"],
    ]
    for idx, d in enumerate(stale[:10], start=1):
        lines.append(format_premium_device_block(d, idx))
    await safe_send_html(bot, chat_id, "\n".join(lines))


async def send_login_prompt(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
    AWAITING_INPUT.pop(chat_id, None)
    GAME_SESSIONS.pop(chat_id, None)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await safe_send_html(
        bot,
        chat_id,
        (
            f"{themed_box(sc('PORTAL LOGIN'))}\n"
            f"{get_theme()['bar']}\n"
            "  ▸ Kripya apna <b>Login ID / Email</b> bhejein:"
        ),
        reply_markup=guest_keyboard_for_chat(chat_id),
    )


async def send_logout_action(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
    AWAITING_INPUT.pop(chat_id, None)
    GAME_SESSIONS.pop(chat_id, None)
    MENU_STATE.pop(chat_id, None)
    MENU_STACK.pop(chat_id, None)
    LAST_PAGE_STATE.pop(chat_id, None)
    sess_data = USER_SESSIONS.pop(chat_id, None)
    LAST_SNAPSHOT.pop(chat_id, None)
    clear_all_alerts_for_chat(chat_id)

    if sess_data and sess_data.get("session"):
        try:
            sess_data["session"].close()
        except Exception:
            pass

    await safe_send_html(
        bot,
        chat_id,
        (
            f"{themed_box(sc('LOGGED OUT'))}\n"
            f"{get_theme()['bar']}\n"
            "  ▸ Session aur alerts band kar diye gaye hain."
        ),
        reply_markup=guest_keyboard_for_chat(chat_id),
    )


async def send_status_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Fetching live telemetry status...",
    )
    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        sess_data["fail_streak"] = 0
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)

        if analysis["meta"]["server_error_500"]:
            await safe_edit_html(
                loader,
                f"{themed_box('• SERVER HTTP 500')}\n  ▸ Portal ne <code>500 Internal Server Error</code> diya hai.",
            )
            return

        th = get_theme()
        b = th["bullet"]
        sites_lines = (
            "\n".join(
                f"  {i}. • <b>{esc(s)}</b> — <code>{c} off</code>"
                for i, (s, c) in enumerate(
                    analysis["top_offline_sites"], start=1
                )
            )
            if analysis["top_offline_sites"]
            else "  ∙ <code>Koi critical offline site nahi</code>"
        )
        ages = analysis["aging_counts"]

        msg = (
            f"{themed_box(esc(CLIENT_NAME) + ' — ' + sc('STATUS'))}\n"
            f"{th['bar']}\n"
            f"  {b} Time         : <i>{esc(analysis['checked_at_ist'])}</i>\n"
            f"  {b} Source       : <code>{esc(analysis['meta'].get('source', 'none'))}</code>\n"
            f"  {b} Total        : <b>{analysis['total']}</b>\n"
            f"  {b} Assigned     : <b>{analysis['assigned_count']}</b> | Unassigned: <b>{analysis['unassigned_count']}</b>\n"
            f"  {b} Connected    : ∙ <b>{analysis['connected']}</b> (Stale: <b>{analysis.get('stale_count', 0)}</b>)\n"
            f"  {b} Disconnected : • <b>{analysis['disconnected_count']}</b>\n\n"
            f"<b>{b} {sc('Aging Buckets (< Range)')}</b>\n"
            f"  ◦ <code>&lt;1D: {ages['1d']}</code> | ▪ <code>&lt;1W: {ages['1w']}</code> | • <code>&lt;24D: {ages['24d']}</code>\n"
            f"  ◆ <code>&lt;11M: {ages['11m']}</code> | ◈ <code>&lt;1Y: {ages['1y']}</code> | ▸ <code>&gt;1Y: {ages['gt1y']}</code>\n\n"
            f"<b>{b} {sc('Top Disconnected Sites')}</b>\n"
            f"{sites_lines}"
        )
        await safe_edit_html(loader, msg)
    except Exception as exc:
        sess_data["fail_streak"] = sess_data.get("fail_streak", 0) + 1
        ensure_live_session(chat_id, sess_data)
        await safe_edit_html(
            loader,
            f"  • <b>Status Error:</b> <code>{esc(exc)}</code>",
        )


async def send_aging_bucket_page(
    chat_id: int, bucket_key: str, page: int, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    title = AGING_RANGE_CONFIG.get(
        bucket_key, ("DISCONNECTED", None, timedelta(days=1))
    )[0]
    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        matched = filter_devices_by_aging_range(
            analysis["all_devices"], bucket_key
        )

        if not matched:
            await safe_send_html(
                bot,
                chat_id,
                (
                    f"{themed_box(esc(title))}\n"
                    "  ▸ <code>Is range mein koi disconnected site nahi hai.</code>"
                ),
                reply_markup=aging_sub_keyboard(),
            )
            return

        per_page = 6
        total_pages = max(1, (len(matched) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        LAST_PAGE_STATE[chat_id] = {
            "view": "age",
            "bucket": bucket_key,
            "page": page,
            "total_pages": total_pages,
        }
        start = page * per_page
        subset = matched[start : start + per_page]

        lines = [
            themed_box(esc(title) + f" ({len(matched)})", f"Page {page + 1}/{total_pages}"),
            get_theme()["bar"],
        ]
        for offset, d in enumerate(subset):
            lines.append(format_premium_device_block(d, start + offset + 1))

        lines.append(
            f"\n  ▸ Navigation: <code>/prev</code> / <code>/next</code> (Page {page + 1}/{total_pages})"
        )
        await safe_send_html(
            bot, chat_id, "\n".join(lines), reply_markup=aging_sub_keyboard()
        )
    except Exception as exc:
        await safe_send_html(
            bot,
            chat_id,
            f"  • <b>Bucket Error:</b> <code>{esc(exc)}</code>",
        )


async def send_allsite_page(chat_id: int, page: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        assigned_devs = analysis["assigned_devices"]

        if not assigned_devs:
            await safe_send_html(
                bot,
                chat_id,
                "  ▸ <code>Koi assigned site nahi mili.</code>",
            )
            return

        per_page = 6
        total_pages = max(1, (len(assigned_devs) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        LAST_PAGE_STATE[chat_id] = {
            "view": "sites",
            "page": page,
            "total_pages": total_pages,
        }
        start = page * per_page
        subset = assigned_devs[start : start + per_page]

        lines = [
            themed_box(
                sc("ALL SITES") + f" ({len(assigned_devs)})",
                f"Page {page + 1}/{total_pages}",
            ),
            get_theme()["bar"],
        ]
        for offset, d in enumerate(subset):
            g_idx = start + offset
            lines.append(format_premium_device_block(d, g_idx + 1))

        lines.append(
            "\n  ▸ Dashboard: <code>/dash &lt;no/serial/name&gt;</code> | Pages: <code>/prev</code> / <code>/next</code>"
        )
        await safe_send_html(bot, chat_id, "\n".join(lines))
    except Exception as exc:
        await safe_send_html(
            bot,
            chat_id,
            f"  • <b>All Sites Error:</b> <code>{esc(exc)}</code>",
        )


def _format_excise_pairs_and_rows(rep: dict[str, Any]) -> list[str]:
    out: list[str] = [
        f"\n\n<b>◈ {sc('UP EXCISE API REPORT')} ({esc(rep.get('report_date') or today_ist_report_date())})</b>",
        f"  ▸ VAT Codes : <code>{esc(', '.join(rep.get('vat_codes') or []) or 'N/A')}</code>",
        f"  ▸ Logs      : <b>{rep.get('total', 0)}</b> (Initial I: <b>{rep.get('initial_count', 0)}</b> | Final F: <b>{rep.get('final_count', 0)}</b> | Pending F: <b>{rep.get('pending_final_count', 0)}</b>)",
        f"  ▸ Status    : Ok <b>{rep.get('success', 0)}</b> | Err <b>{rep.get('failed', 0)}</b>",
    ]
    pairs = rep.get("pairs") or []
    if pairs:
        out.append(f"  <b>▸ {sc('RequestID Pairs (Initial → Final)')}</b>")
        for p in pairs[:6]:
            rid = p.get("request_id") or "N/A"
            vc = p.get("vat_code") or "-"
            rt = f" [{p.get('request_type')}]" if p.get("request_type") else ""
            delta_t = p.get("delta_text") or ""
            ini = p.get("initial")
            fin = p.get("final")

            ini_str = (
                f"I: {ini.get('timestamp')} (mm:{ini.get('length')}, L:{ini.get('bl')})"
                if ini
                else "I: -"
            )
            fin_str = (
                f"F: {fin.get('timestamp')} (mm:{fin.get('length')}, L:{fin.get('bl')})"
                if fin
                else "F: Pending"
            )
            delta_line = (
                f"\n      ↳ Delta: <code>{esc(delta_t)}</code>" if delta_t else ""
            )
            out.append(
                f"    • <code>{esc(rid)}</code> | VAT: <code>{esc(vc)}{esc(rt)}</code>\n"
                f"      ↳ <code>{esc(ini_str)}</code>\n"
                f"      ↳ <code>{esc(fin_str)}</code>{delta_line}"
            )
    elif rep.get("rows"):
        for idx, row in enumerate(rep["rows"][:8], start=1):
            out.append(
                f"  {idx}. [<code>{esc(row.get('process'))}</code>] <code>{esc(row.get('request_id'))}</code> "
                f"({esc(row.get('vat_code'))}) → <code>{esc(row.get('status_text'))}</code>"
            )
    else:
        out.append("  ▸ <code>Is date ke liye koi API request log nahi mila.</code>")

    if rep.get("error"):
        out.append(f"  • Report Note: <code>{esc(rep.get('error'))}</code>")
    return out


async def send_site_dashboard_by_target(
    chat_id: int, target: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Session expire ho gaya hai, dobara login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    assigned_devs = analysis["assigned_devices"]

    dev = _find_device_by_target(assigned_devs, target)
    if not dev:
        await safe_send_html(
            bot,
            chat_id,
            (
                f"  • <b>Reason:</b> Site '<code>{esc(target or 'empty')}</code>' nahi mili. "
                "Pehle <code>/allsite</code> check karein ya <code>/dash &lt;number/serial/name&gt;</code> bhejein."
            ),
        )
        return

    LAST_PAGE_STATE[chat_id] = {
        "view": "last_site",
        "site_key": dev.get("site_id") or dev.get("serial_no") or dev.get("industry"),
    }

    loader = await bot.send_message(
        chat_id=chat_id,
        text=f"◈ Opening dashboard & API report: {dev['industry']}...",
    )

    try:
        dash = await asyncio.to_thread(
            scrape_site_dashboard_details, sess_data["session"], dev
        )
    except Exception as exc:
        dash = {
            "ok": False,
            "site_id": dev.get("site_id", ""),
            "site_code": dev.get("site_code", ""),
            "site_name_ws": "",
            "url": dev.get("dashboard_url", ""),
            "error": f"Exception: {exc}",
            "instruments": [],
            "api_report": None,
        }

    dot = small_status_dot(dev["is_connected"])
    conn_word = "CONNECTED" if dev["is_connected"] else "DISCONNECTED"
    off_info = (
        "(Active)"
        if dev["is_connected"]
        else format_off_since_text(
            dev.get("last_seen"), dev.get("offline_duration")
        )
    )
    ws_name_line = (
        f"\n  ▸ WS Name   : <code>{esc(dash.get('site_name_ws'))}</code>"
        if dash.get("site_name_ws")
        else ""
    )
    code_line = (
        f"\n  ▸ Site Code : <code>{esc(dash.get('site_code'))}</code>"
        if dash.get("site_code")
        else ""
    )

    header = (
        f"{themed_box(esc(dev['industry']))}\n"
        f"{get_theme()['bar']}\n"
        f"  ▸ Site Type : <code>{esc(dev['site'])}</code>\n"
        f"  ▸ Serial No : <b>{esc(dev['serial_no'])}</b>\n"
        f"  ▸ Site ID   : <code>{esc(dash.get('site_id') or 'N/A')}</code>{code_line}{ws_name_line}\n"
        f"  ▸ Status    : {dot} <b>{conn_word}</b> (<code>{esc(dev['display_time'])}</code>)/(<code>{esc(off_info)}</code>)\n\n"
    )

    if dash.get("instruments"):
        grouped_lines = group_readings_by_tank(dash["instruments"])
        inst_section = (
            f"<b>◈ {sc('GROUPED TANK & INSTRUMENT READINGS (MM / L / INST)')}</b>\n"
            + "\n".join(grouped_lines)
        )
    else:
        reason = dash.get("error") or "Dashboard WS mein koi tag value nahi mili."
        inst_section = (
            f"<b>◈ {sc('LIVE DASHBOARD READINGS')}</b>\n"
            f"  • Info      : <code>{esc(reason)}</code>"
        )

    rep = dash.get("api_report") or {}
    api_lines = _format_excise_pairs_and_rows(rep)
    await safe_edit_html(loader, header + inst_section + "".join(api_lines))


async def send_api_report_action(
    chat_id: int, target: str, report_date: str | None, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    devs = analysis.get("assigned_devices", [])
    dev = _find_device_by_target(devs, target)
    if not dev:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Use: <code>/apireport &lt;no/serial/name&gt; [DD-Mon-YYYY]</code>",
        )
        return

    sid = str(dev.get("site_id") or "").strip()
    if not sid.isdigit():
        await safe_send_html(
            bot,
            chat_id,
            f"  • <b>{esc(dev['industry'])}</b> ke liye Site ID nahi mila.",
        )
        return

    rdate = (report_date or today_ist_report_date()).strip()
    loader = await bot.send_message(
        chat_id=chat_id,
        text=f"◈ Fetching UP Excise API Report ({rdate}) for {dev['industry']}...",
    )
    rep = await asyncio.to_thread(
        fetch_site_api_report, sess_data["session"], sid, rdate
    )

    lines = [
        themed_box(sc("API REPORT")),
        get_theme()["bar"],
        f"  ▸ Site      : <b>{esc(dev['industry'])}</b> (ID: <code>{esc(sid)}</code>)",
        f"  ▸ Date      : <code>{esc(rdate)}</code>",
    ]
    lines.extend(_format_excise_pairs_and_rows(rep))
    await safe_edit_html(loader, "".join(lines))


async def send_instruments_view(
    chat_id: int, bot, page: int = 0, query: str = ""
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    devs = analysis.get("assigned_devices", [])

    q = (query or "").strip().lower()
    if q:
        devs = [
            d
            for d in devs
            if q in str(d.get("industry", "")).lower()
            or q in str(d.get("site", "")).lower()
            or q in str(d.get("serial_no", "")).lower()
            or q in str(d.get("site_id", "")).lower()
            or q in str(d.get("site_code", "")).lower()
        ]

    if not devs:
        await safe_send_html(
            bot,
            chat_id,
            f"  ▸ <code>'{esc(query or 'sites')}' ke liye koi site nahi mili.</code>",
        )
        return

    per_page = 5
    total_pages = max(1, (len(devs) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    LAST_PAGE_STATE[chat_id] = {
        "view": "inst",
        "page": page,
        "total_pages": total_pages,
        "query": query,
    }
    start = page * per_page
    subset = devs[start : start + per_page]

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Fetching Distillery & Bottling tag/instrument readings...",
    )

    blocks: list[str] = [
        themed_box(sc("INSTRUMENTS & TANKS"), f"Page {page + 1}/{total_pages}"),
        get_theme()["bar"],
    ]

    for offset, d in enumerate(subset):
        idx = start + offset + 1
        dash = await asyncio.to_thread(
            scrape_site_dashboard_details, sess_data["session"], d
        )
        dot = small_status_dot(d["is_connected"])
        conn_word = "CONNECTED" if d["is_connected"] else "DISCONNECTED"
        blocks.append(
            f"┌─ #{idx} <b>{format_industry_site_label(d['industry'], d['site'])}</b>\n"
            f"│  ▸ Serial : <b>{esc(d['serial_no'])}</b> | {dot} <b>{conn_word}</b>"
        )
        insts = dash.get("instruments") or []
        if insts:
            for gline in group_readings_by_tank(insts)[:8]:
                blocks.append(f"│  {gline.lstrip()}")
        else:
            blocks.append(
                f"│  • Readings: <code>{esc(dash.get('error') or 'No tag values returned')}</code>"
            )
        blocks.append("└──────────────────────")

    blocks.append(
        f"\n  ▸ Pages: <code>/prev</code> / <code>/next</code> (Page {page + 1}/{total_pages})"
    )
    await safe_edit_html(loader, "\n".join(blocks))


async def send_online_page(chat_id: int, page: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        online_list = analysis["online_list"]

        if not online_list:
            await safe_send_html(
                bot,
                chat_id,
                "  ▸ <code>Koi connected device nahi mila.</code>",
            )
            return

        per_page = 6
        total_pages = max(1, (len(online_list) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        LAST_PAGE_STATE[chat_id] = {
            "view": "online",
            "page": page,
            "total_pages": total_pages,
        }
        start = page * per_page
        subset = online_list[start : start + per_page]

        lines = [
            themed_box(
                sc("CONNECTED") + f" ({len(online_list)})",
                f"Page {page + 1}/{total_pages}",
            ),
            get_theme()["bar"],
        ]
        for offset, d in enumerate(subset):
            lines.append(format_premium_device_block(d, start + offset + 1))

        lines.append(
            f"\n  ▸ Pages: <code>/prev</code> / <code>/next</code> (Page {page + 1}/{total_pages})"
        )
        await safe_send_html(bot, chat_id, "\n".join(lines))
    except Exception as exc:
        await safe_send_html(
            bot,
            chat_id,
            f"  • <b>Online Error:</b> <code>{esc(exc)}</code>",
        )


async def send_unassigned_page(chat_id: int, page: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        unassigned_list = analysis["unassigned_list"]

        if not unassigned_list:
            await safe_send_html(
                bot,
                chat_id,
                "  ▸ <code>Koi N/A unassigned device nahi mila.</code>",
            )
            return

        per_page = 6
        total_pages = max(1, (len(unassigned_list) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        LAST_PAGE_STATE[chat_id] = {
            "view": "unass",
            "page": page,
            "total_pages": total_pages,
        }
        start = page * per_page
        subset = unassigned_list[start : start + per_page]

        lines = [
            themed_box(
                sc("UNASSIGNED N/A") + f" ({len(unassigned_list)})",
                f"Page {page + 1}/{total_pages}",
            ),
            get_theme()["bar"],
        ]
        for offset, d in enumerate(subset):
            lines.append(format_premium_device_block(d, start + offset + 1))

        lines.append(
            f"\n  ▸ Pages: <code>/prev</code> / <code>/next</code> (Page {page + 1}/{total_pages})"
        )
        await safe_send_html(bot, chat_id, "\n".join(lines))
    except Exception as exc:
        await safe_send_html(
            bot,
            chat_id,
            f"  • <b>Unassigned Error:</b> <code>{esc(exc)}</code>",
        )


async def step_pagination(chat_id: int, delta: int, bot) -> None:
    st = LAST_PAGE_STATE.get(chat_id)
    if not st:
        await safe_send_html(
            bot,
            chat_id,
            "  • <b>Reason:</b> Koi active paginated list nahi hai. Pehle <code>/allsite</code>, <code>/online</code>, <code>/unassigned</code>, ya <code>/instruments</code> chalayein.",
        )
        return

    next_p = int(st.get("page", 0)) + delta
    total_p = int(st.get("total_pages", 1))
    if next_p < 0:
        await safe_send_html(
            bot,
            chat_id,
            "  • Aap pehle hi <b>Page 1</b> par hain.",
        )
        return
    if next_p >= total_p:
        await safe_send_html(
            bot,
            chat_id,
            f"  • Aap aakhri <b>Page {total_p}</b> par hain.",
        )
        return

    view = st.get("view")
    if view == "sites":
        await send_allsite_page(chat_id, next_p, bot)
    elif view == "online":
        await send_online_page(chat_id, next_p, bot)
    elif view == "unass":
        await send_unassigned_page(chat_id, next_p, bot)
    elif view == "inst":
        await send_instruments_view(
            chat_id, bot, page=next_p, query=str(st.get("query") or "")
        )
    elif view == "age":
        await send_aging_bucket_page(
            chat_id, str(st.get("bucket") or "1d"), next_p, bot
        )


async def send_health_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        meta = analysis["meta"]
        th = get_theme()
        b = th["bullet"]

        type_lines = (
            "\n".join(
                f"  {b} <code>{esc(dt)} : {cnt}</code>"
                for dt, cnt in sorted(
                    analysis["device_type_breakdown"].items(),
                    key=lambda x: -x[1],
                )
            )
            or "  <code>No device type data</code>"
        )
        fw_lines = (
            "\n".join(
                f"  {b} <code>FW {esc(fw)} : {cnt}</code>"
                for fw, cnt in sorted(
                    analysis["firmware_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  <code>No firmware data</code>"
        )

        msg = (
            f"{themed_box(sc('SYSTEM HEALTH'))}\n"
            f"{th['bar']}\n"
            f"  {b} Client      : <code>{esc(CLIENT_NAME)}</code>\n"
            f"  {b} Source      : <code>{esc(meta.get('source', 'none'))}</code>\n"
            f"  {b} Theme / Sync: <code>{esc(th['label'])}</code> / <code>{REFRESH_INTERVAL_SEC}s</code>\n"
            f"  {b} HTTP 500    : <code>{'YES' if meta['server_error_500'] else 'NO'}</code>\n"
            f"  {b} Total / N/A : <b>{analysis['total']} / {analysis['unassigned_count']}</b>\n\n"
            f"<b>{b} {sc('Device Types')}</b>\n{type_lines}\n\n"
            f"<b>{b} {sc('Firmware')}</b>\n{fw_lines}"
        )
        await safe_send_html(bot, chat_id, msg)
    except Exception as exc:
        await safe_send_html(
            bot,
            chat_id,
            f"  • <b>Health Error:</b> <code>{esc(exc)}</code>",
        )


async def send_export_csv_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Generating CSV report...",
    )
    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        csv_bytes = build_csv_bytes(analysis["all_devices"])
        ts_tag = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        fname = f"{CLIENT_NAME.lower().replace(' ', '_')}_devices_{ts_tag}.csv"
        await bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(csv_bytes),
            filename=fname,
            caption=(
                f"{CLIENT_NAME} — {sc('CSV EXPORT')}\n"
                f"▸ Total: {analysis['total']} | "
                f"Assigned: {analysis['assigned_count']} | "
                f"Unassigned: {analysis['unassigned_count']}"
            ),
        )
        await loader.delete()
    except Exception as exc:
        await safe_edit_html(
            loader,
            f"  • <b>CSV Export Error:</b> <code>{esc(exc)}</code>",
        )


async def send_site_search_action(
    chat_id: int, query: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    q = (query or "").strip().lower()
    if not q:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Use: <code>/site &lt;industry / site / serial / code&gt;</code>",
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    matches = [
        d
        for d in analysis["all_devices"]
        if q in str(d.get("industry", "")).lower()
        or q in str(d.get("site", "")).lower()
        or q in str(d.get("serial_no", "")).lower()
        or q in str(d.get("site_id", "")).lower()
        or q in str(d.get("site_code", "")).lower()
    ]
    if not matches:
        await safe_send_html(
            bot,
            chat_id,
            f"  • '<code>{esc(query)}</code>' ke liye koi match nahi mila.",
        )
        return

    lines = [
        themed_box(sc("SEARCH RESULT") + f" ({len(matches)})"),
        get_theme()["bar"],
    ]
    for i, d in enumerate(matches[:10], start=1):
        lines.append(format_premium_device_block(d, i))
    await safe_send_html(bot, chat_id, "\n".join(lines))


async def send_ai_analyze_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Groq AI compact telemetry report tayar kar raha hai...",
    )
    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)

        report_txt = await asyncio.to_thread(
            generate_ai_analysis, sess_data["session"], analysis
        )
        msg = (
            f"{themed_box(esc(CLIENT_NAME) + ' — ' + sc('AI BRIEF'))}\n"
            f"{get_theme()['bar']}\n"
            f"  ▸ Time: <i>{esc(analysis['checked_at_ist'])}</i>\n\n"
            f"{clean_ai_text(report_txt)}"
        )
        await safe_edit_html(loader, msg)
    except Exception as exc:
        await safe_edit_html(
            loader,
            f"  • <b>AI Analyze Error:</b> <code>{esc(exc)}</code>",
        )


async def send_ai_answer_action(
    chat_id: int, question: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Groq AI jawab de raha hai...",
    )
    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        ans = await asyncio.to_thread(
            answer_project_question, question, sess_data["session"], analysis
        )
        msg = (
            f"{themed_box(esc(CLIENT_NAME) + ' — ' + sc('GROQ AI'))}\n"
            f"{get_theme()['bar']}\n"
            f"{clean_ai_text(ans)}"
        )
        await safe_edit_html(loader, msg)
    except Exception as exc:
        await safe_edit_html(
            loader,
            f"  • <b>Groq AI Error:</b> <code>{esc(exc)}</code>",
        )


# ==============================================================================
# TELEGRAM COMMAND HANDLERS
# ==============================================================================
async def cmd_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await check_access(chat_id, user_id, context.bot):
        return
    AI_CHAT_MODE.discard(chat_id)
    await send_start_view(chat_id, user_id, context.bot)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await check_access(chat_id, user_id, context.bot):
        return
    await send_manual_view(chat_id, user_id, context.bot)


async def cmd_login(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_login_prompt(chat_id, context.bot)


async def cmd_logout(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_logout_action(chat_id, context.bot)


async def cmd_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_status_action(chat_id, context.bot)


async def cmd_allsite(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    page = 0
    if context.args:
        try:
            page = max(0, int(context.args[0]) - 1)
        except ValueError:
            page = 0
    await send_allsite_page(chat_id, page, context.bot)


async def cmd_dash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    target = " ".join(context.args).strip() if context.args else ""
    if not target:
        await open_dashboard_menu(chat_id, context.bot)
        return
    await send_site_dashboard_by_target(chat_id, target, context.bot)


async def cmd_apireport(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    args = list(context.args or [])
    rdate: str | None = None
    if args and re.fullmatch(r"\d{2}-[A-Za-z]{3}-\d{4}", args[-1]):
        rdate = args.pop()
    target = " ".join(args).strip()
    if not target:
        await open_api_report_menu(chat_id, context.bot)
        return
    await send_api_report_action(chat_id, target, rdate, context.bot)


async def cmd_instruments(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    q = " ".join(context.args).strip() if context.args else ""
    await send_instruments_view(chat_id, context.bot, page=0, query=q)


async def cmd_online(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    page = 0
    if context.args:
        try:
            page = max(0, int(context.args[0]) - 1)
        except ValueError:
            page = 0
    await send_online_page(chat_id, page, context.bot)


async def cmd_unassigned(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    page = 0
    if context.args:
        try:
            page = max(0, int(context.args[0]) - 1)
        except ValueError:
            page = 0
    await send_unassigned_page(chat_id, page, context.bot)


async def cmd_aging(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await open_aging_menu(chat_id, context.bot)


async def cmd_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    if not context.args:
        await open_aging_menu(chat_id, context.bot)
        return
    bucket = context.args[0].strip().lower()
    if bucket not in AGING_RANGE_CONFIG:
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ Use: <code>/age 1d</code>, <code>/age 1w</code>, <code>/age 24d</code>, <code>/age 11m</code>, <code>/age 1y</code>, ya <code>/age gt1y</code>",
        )
        return
    await send_aging_bucket_page(chat_id, bucket, 0, context.bot)


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await step_pagination(chat_id, +1, context.bot)


async def cmd_prev(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await step_pagination(chat_id, -1, context.bot)


async def cmd_health(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_health_action(chat_id, context.bot)


async def cmd_export(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_export_csv_action(chat_id, context.bot)


async def cmd_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    q = " ".join(context.args).strip() if context.args else ""
    await send_site_search_action(chat_id, q, context.bot)


async def cmd_ai_analyze(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_ai_analyze_action(chat_id, context.bot)


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    if not require_auth(chat_id):
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return
    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        AI_CHAT_MODE.add(chat_id)
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ <b>Groq AI Mode ON</b> — Apna sawal bhejein. Band karne ke liye <code>/exit_ai</code> bhejein.",
        )
        return
    await send_ai_answer_action(chat_id, q, context.bot)


async def cmd_exit_ai(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    AI_CHAT_MODE.discard(chat_id)
    await safe_send_html(
        context.bot,
        chat_id,
        "  ▸ <b>Groq AI Mode OFF</b>.",
        reply_markup=current_keyboard_for_chat(chat_id, user_id),
    )


# ==============================================================================
# ADMIN COMMANDS (ONLY ADMIN_TG_ID)
# ==============================================================================
async def cmd_admin(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    await open_admin_panel(update.effective_chat.id, context.bot)


async def cmd_set_theme(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global ACTIVE_THEME
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    th_key = (context.args[0].strip().lower() if context.args else "")
    if th_key not in THEME_CONFIGS:
        await safe_send_html(
            context.bot,
            update.effective_chat.id,
            "  ▸ Use: <code>/set_theme obsidian | minimal | cyber | royal</code>",
        )
        return
    ACTIVE_THEME = th_key
    save_setting_db("active_theme", ACTIVE_THEME)
    await safe_send_html(
        context.bot,
        update.effective_chat.id,
        f"  ∙ <b>Theme set to:</b> <code>{esc(get_theme()['label'])}</code>",
    )


async def cmd_bot_on(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global BOT_ENABLED
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    BOT_ENABLED = True
    save_setting_db("bot_enabled", "1")
    await safe_send_html(
        context.bot,
        update.effective_chat.id,
        "  ∙ <b>Bot ON kar diya gaya hai.</b>",
    )


async def cmd_bot_off(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global BOT_ENABLED
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    BOT_ENABLED = False
    save_setting_db("bot_enabled", "0")
    await safe_send_html(
        context.bot,
        update.effective_chat.id,
        "  • <b>Bot OFF kar diya gaya hai.</b>",
    )


async def cmd_maint_on(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global MAINTENANCE_MODE, MAINTENANCE_MSG
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    MAINTENANCE_MODE = True
    if context.args:
        MAINTENANCE_MSG = " ".join(context.args).strip()
    save_setting_db("maint_mode", "1")
    save_setting_db("maint_msg", MAINTENANCE_MSG)
    await safe_send_html(
        context.bot,
        update.effective_chat.id,
        f"  ◈ <b>Maintenance Mode ON:</b> <code>{esc(MAINTENANCE_MSG)}</code>",
    )


async def cmd_maint_off(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global MAINTENANCE_MODE
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    MAINTENANCE_MODE = False
    save_setting_db("maint_mode", "0")
    await safe_send_html(
        context.bot,
        update.effective_chat.id,
        "  ∙ <b>Maintenance Mode OFF.</b>",
    )


async def cmd_set_refresh(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global REFRESH_INTERVAL_SEC
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    if not context.args:
        await safe_send_html(
            context.bot,
            update.effective_chat.id,
            "  ▸ Use: <code>/set_refresh 30</code> (15 se 3600 seconds)",
        )
        return
    try:
        sec = int(context.args[0])
        if sec < 15 or sec > 3600:
            raise ValueError()
        REFRESH_INTERVAL_SEC = sec
        save_setting_db("refresh_sec", str(sec))
        await safe_send_html(
            context.bot,
            update.effective_chat.id,
            f"  ∙ <b>Sync Rate set to {sec}s.</b>",
        )
    except ValueError:
        await safe_send_html(
            context.bot,
            update.effective_chat.id,
            "  • 15 se 3600 ke beech valid seconds dein.",
        )


async def cmd_set_notif(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global NOTIF_STYLE
    if not update.effective_chat or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("  • Admin access required.")
        return
    if not context.args or context.args[0].lower() not in (
        "detailed",
        "compact",
        "alerts_only",
        "digest",
    ):
        await safe_send_html(
            context.bot,
            update.effective_chat.id,
            "  ▸ Use: <code>/set_notif detailed | compact | alerts_only | digest</code>",
        )
        return
    NOTIF_STYLE = context.args[0].lower()
    save_setting_db("notif_style", NOTIF_STYLE)
    await safe_send_html(
        context.bot,
        update.effective_chat.id,
        f"  ∙ <b>Notification Style set to:</b> <code>{esc(NOTIF_STYLE)}</code>",
    )


# ==============================================================================
# TEXT ROUTER (LOGIN FLOW, GAMES, CONTEXT MENUS, ADMIN CONTROLS, AI CHAT)
# ==============================================================================
async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    global BOT_ENABLED, MAINTENANCE_MODE, REFRESH_INTERVAL_SEC, NOTIF_STYLE, ACTIVE_THEME

    if (
        not update.effective_chat
        or not update.effective_user
        or not update.message
        or not update.message.text
    ):
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    raw_text = update.message.text.strip()
    low = raw_text.lower()

    if not await check_access(chat_id, user_id, context.bot):
        return

    # 1. Login sequence
    sess_data = USER_SESSIONS.get(chat_id)
    if sess_data and sess_data.get("step") == "awaiting_username":
        if low in ("🎮 games lounge", "🎲 tic-tac-toe", "🔢 number guess", "✂ rock paper scissors", "📖 manual"):
            pass
        else:
            USER_SESSIONS[chat_id] = {
                "step": "awaiting_password",
                "username": raw_text,
            }
            await safe_send_html(
                context.bot, chat_id, "  ▸ Ab apna <b>Password</b> bhejein:"
            )
            return

    if sess_data and sess_data.get("step") == "awaiting_password":
        username = sess_data.get("username", "")
        password = raw_text
        try:
            await update.message.delete()
        except Exception:
            pass

        wait_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="◈ EnviroFRONT portal par login ho raha hai...",
        )

        session, err = await asyncio.to_thread(
            login_envirofront, username, password
        )
        if not session:
            USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
            await safe_edit_html(
                wait_msg,
                f"  • <b>Login Failed:</b> <code>{esc(err)}</code>\n  ▸ Dobara <b>Login ID / Email</b> bhejein:",
            )
            return

        USER_SESSIONS[chat_id] = {
            "step": "authenticated",
            "username": username,
            "session": session,
            "fail_streak": 0,
        }
        GAME_SESSIONS.pop(chat_id, None)
        MENU_STATE[chat_id] = "main"
        MENU_STACK[chat_id] = []
        th = get_theme()
        await safe_edit_html(
            wait_msg,
            f"{themed_box('∙ ' + esc(CLIENT_NAME), sc('AUTHENTICATED'))}\n"
            f"{th['bar']}\n"
            f"  ▸ Account   : <code>{esc(username)}</code>\n"
            f"  ▸ Theme     : <code>{esc(th['label'])}</code>\n"
            f"  ▸ Alerts    : <code>2h → 10h → 1d (1-Time)</code>",
        )
        await safe_send_html(
            context.bot,
            chat_id,
            f"◈ <b>{esc(CLIENT_NAME)} — {sc('PANEL ACTIVE')}</b>",
            reply_markup=main_reply_keyboard(user_id),
        )
        return

    # 2. Back & Main Menu navigation
    if low == "🏠 main menu":
        MENU_STATE[chat_id] = "main" if require_auth(chat_id) else "guest"
        MENU_STACK[chat_id] = []
        AWAITING_INPUT.pop(chat_id, None)
        GAME_SESSIONS.pop(chat_id, None)
        await safe_send_html(
            context.bot,
            chat_id,
            f"{themed_box(sc('MAIN MENU'))}",
            reply_markup=current_keyboard_for_chat(chat_id, user_id),
        )
        return

    if low == "⬅ back":
        await pop_menu_state(chat_id, user_id, context.bot)
        return

    # 3. Active Mini-Game turns (when not authenticated)
    active_game = GAME_SESSIONS.get(chat_id)
    if active_game and not require_auth(chat_id):
        gtype = active_game.get("game")
        if gtype == "ttt" and raw_text.isdigit():
            reply_txt, finished = play_ttt_turn(chat_id, int(raw_text))
            await safe_send_html(
                context.bot,
                chat_id,
                reply_txt,
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        if gtype == "guess" and raw_text.isdigit():
            reply_txt, finished = play_guess_turn(chat_id, int(raw_text))
            await safe_send_html(
                context.bot,
                chat_id,
                reply_txt,
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        if gtype == "rps" and low in ("rock", "paper", "scissors"):
            reply_txt, finished = play_rps_turn(chat_id, raw_text)
            await safe_send_html(
                context.bot,
                chat_id,
                reply_txt,
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return

    # 4. Pending contextual input
    pending = AWAITING_INPUT.get(chat_id)
    if pending:
        AWAITING_INPUT.pop(chat_id, None)
        kind = pending.get("kind")
        if kind == "dash_target":
            await send_site_dashboard_by_target(chat_id, raw_text, context.bot)
            return
        if kind == "api_today_target":
            await send_api_report_action(
                chat_id, raw_text, today_ist_report_date(), context.bot
            )
            return
        if kind == "api_custom_date":
            parts = re.split(
                r"\s*\|\s*|\s+(?=\d{2}-[A-Za-z]{3}-\d{4}$)",
                raw_text,
                maxsplit=1,
            )
            if len(parts) == 2:
                await send_api_report_action(
                    chat_id, parts[0].strip(), parts[1].strip(), context.bot
                )
            else:
                await send_api_report_action(
                    chat_id, raw_text, None, context.bot
                )
            return
        if kind == "set_refresh_val" and is_admin(user_id):
            try:
                sec = int(raw_text)
                if 15 <= sec <= 3600:
                    REFRESH_INTERVAL_SEC = sec
                    save_setting_db("refresh_sec", str(sec))
                    await safe_send_html(
                        context.bot,
                        chat_id,
                        f"  ∙ <b>Sync Rate set to {sec}s.</b>",
                        reply_markup=admin_sub_keyboard(),
                    )
                    return
            except ValueError:
                pass
            await safe_send_html(
                context.bot,
                chat_id,
                "  • 15 se 3600 ke beech valid seconds bhejein.",
                reply_markup=admin_sub_keyboard(),
            )
            return
        if kind == "set_maint_msg" and is_admin(user_id):
            if low == "off":
                MAINTENANCE_MODE = False
                save_setting_db("maint_mode", "0")
                await safe_send_html(
                    context.bot,
                    chat_id,
                    "  ∙ <b>Maintenance Mode OFF.</b>",
                    reply_markup=admin_sub_keyboard(),
                )
            else:
                MAINTENANCE_MODE = True
                MAINTENANCE_MSG = raw_text
                save_setting_db("maint_mode", "1")
                save_setting_db("maint_msg", MAINTENANCE_MSG)
                await safe_send_html(
                    context.bot,
                    chat_id,
                    f"  ◈ <b>Maintenance ON:</b> <code>{esc(MAINTENANCE_MSG)}</code>",
                    reply_markup=admin_sub_keyboard(),
                )
            return

    # 5. Quick numbered site buttons from dashboard sub-menu
    m_quick = re.match(r"^#(\d+)\b", raw_text)
    if m_quick and MENU_STATE.get(chat_id) == "dash_menu":
        await send_site_dashboard_by_target(
            chat_id, m_quick.group(1), context.bot
        )
        return

    # 6. Theme & Notification Demo Buttons (Admin)
    if is_admin(user_id):
        theme_btn_map = {
            "theme: obsidian": "obsidian",
            "theme: minimal": "minimal",
            "theme: cyber": "cyber",
            "theme: royal": "royal",
        }
        if low in theme_btn_map:
            ACTIVE_THEME = theme_btn_map[low]
            save_setting_db("active_theme", ACTIVE_THEME)
            await safe_send_html(
                context.bot,
                chat_id,
                f"  ∙ <b>Active Theme:</b> <code>{esc(get_theme()['label'])}</code>",
                reply_markup=theme_sub_keyboard(),
            )
            return

        notif_btn_map = {
            "mode: detailed": "detailed",
            "mode: compact": "compact",
            "mode: alerts only": "alerts_only",
            "mode: digest": "digest",
        }
        if low in notif_btn_map:
            NOTIF_STYLE = notif_btn_map[low]
            save_setting_db("notif_style", NOTIF_STYLE)
            await safe_send_html(
                context.bot,
                chat_id,
                f"  ∙ <b>Notification Mode:</b> <code>{esc(NOTIF_STYLE)}</code>",
                reply_markup=notif_demo_sub_keyboard(),
            )
            return

        demo_btn_map = {
            "demo: 2h down": "2h",
            "demo: 10h down": "10h",
            "demo: 1d down": "1d",
            "demo: recovered": "recovered",
        }
        if low in demo_btn_map:
            await send_notification_demo(
                chat_id, demo_btn_map[low], context.bot
            )
            return

    # 7. Menu & Action Buttons
    btn_map: dict[str, str] = {
        "🔐 login": "login",
        "📖 manual": "manual",
        "🎮 games lounge": "games_lounge",
        "🎲 tic-tac-toe": "game_ttt",
        "🔢 number guess": "game_guess",
        "✂ rock paper scissors": "game_rps",
        "◈ status": "status",
        "◈ all sites": "allsite",
        "🌐 site dashboard": "dash_menu",
        "🔎 open site by id/name": "ask_dash_target",
        "⭐ pin site": "pin_last_site",
        "⭐ watchlist": "watchlist",
        "📋 shift summary": "shift_summary",
        "📡 api report": "api_menu",
        "📅 today api report": "ask_api_today",
        "🗓 custom date report": "ask_api_custom",
        "🏭 site api report": "ask_api_today",
        "📐 instruments": "instruments",
        "⏳ aging": "age_menu",
        "◦ <1d": "age_1d",
        "▪ <1w": "age_1w",
        "• <24d": "age_24d",
        "◆ <11m": "age_11m",
        "◈ <1y": "age_1y",
        "▸ >1y": "age_gt1y",
        "⚙ ops & export": "ops_menu",
        "🩺 health": "health",
        "📥 export csv": "csv",
        "∙ online": "online",
        "📌 unassigned": "unassigned",
        "❄ stale sites": "stale_sites",
        "⬅ previous": "prev",
        "next ➡": "next",
        "🤖 ai suite": "aireport",
        "🚪 logout": "logout",
        "🛡 admin panel": "admin_menu",
        "🎨 themes": "theme_menu",
        "🔔 notif & demo": "notif_menu",
        "⏱ sync interval": "ask_refresh",
        "🛠 maintenance": "ask_maint",
        "⚡ bot on/off": "toggle_bot",
        "🧹 clear alerts": "clear_alerts",
    }
    action = btn_map.get(low)

    if action == "login":
        await send_login_prompt(chat_id, context.bot)
        return
    if action == "manual":
        await send_manual_view(chat_id, user_id, context.bot)
        return
    if action == "games_lounge":
        await open_games_lounge(chat_id, user_id, context.bot)
        return
    if action == "game_ttt":
        await safe_send_html(
            context.bot,
            chat_id,
            start_ttt_game(chat_id),
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return
    if action == "game_guess":
        await safe_send_html(
            context.bot,
            chat_id,
            start_guess_game(chat_id),
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return
    if action == "game_rps":
        await safe_send_html(
            context.bot,
            chat_id,
            start_rps_game(chat_id),
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return
    if action == "status":
        await send_status_action(chat_id, context.bot)
        return
    if action == "allsite":
        await send_allsite_page(chat_id, 0, context.bot)
        return
    if action == "dash_menu":
        await open_dashboard_menu(chat_id, context.bot)
        return
    if action == "ask_dash_target":
        if not require_auth(chat_id):
            await safe_send_html(
                context.bot,
                chat_id,
                "  ▸ Pehle login karein (<code>/login</code>):",
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        AWAITING_INPUT[chat_id] = {"kind": "dash_target"}
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ Site ka <b>Number</b>, <b>Serial No</b>, <b>Site ID</b>, ya <b>Naam</b> bhejein:",
        )
        return
    if action == "pin_last_site":
        st = LAST_PAGE_STATE.get(chat_id) or {}
        skey = str(st.get("site_key") or "").strip()
        if not skey:
            await safe_send_html(
                context.bot,
                chat_id,
                "  ▸ Pehle koi site dashboard kholein, phir <code>⭐ Pin Site</code> dabayein.",
            )
            return
        added, wlist = toggle_watchlist_site(chat_id, skey)
        status_w = "Pinned to Watchlist" if added else "Removed from Watchlist"
        await safe_send_html(
            context.bot,
            chat_id,
            f"  ∙ <b>{esc(status_w)}:</b> <code>{esc(skey)}</code> (Total: <b>{len(wlist)}</b>)",
        )
        return
    if action == "watchlist":
        await send_watchlist_view(chat_id, context.bot)
        return
    if action == "shift_summary":
        await send_shift_summary(chat_id, context.bot)
        return
    if action == "api_menu":
        await open_api_report_menu(chat_id, context.bot)
        return
    if action == "ask_api_today":
        if not require_auth(chat_id):
            await safe_send_html(
                bot,
                chat_id,
                "  ▸ Pehle login karein (<code>/login</code>):",
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        AWAITING_INPUT[chat_id] = {"kind": "api_today_target"}
        await safe_send_html(
            context.bot,
            chat_id,
            f"  ▸ Today (<code>{esc(today_ist_report_date())}</code>) API report ke liye site ka <b>Number / Serial / Name</b> bhejein:",
        )
        return
    if action == "ask_api_custom":
        if not require_auth(chat_id):
            await safe_send_html(
                context.bot,
                chat_id,
                "  ▸ Pehle login karein (<code>/login</code>):",
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        AWAITING_INPUT[chat_id] = {"kind": "api_custom_date"}
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ Site aur date bhejein (format: <code>Malbros 26-Nov-2026</code>):",
        )
        return
    if action == "instruments":
        await send_instruments_view(chat_id, context.bot, page=0, query="")
        return
    if action == "age_menu":
        await open_aging_menu(chat_id, context.bot)
        return
    if action and action.startswith("age_"):
        bucket = action.replace("age_", "", 1)
        await send_aging_bucket_page(chat_id, bucket, 0, context.bot)
        return
    if action == "ops_menu":
        await open_ops_menu(chat_id, context.bot)
        return
    if action == "health":
        await send_health_action(chat_id, context.bot)
        return
    if action == "csv":
        await send_export_csv_action(chat_id, context.bot)
        return
    if action == "online":
        await send_online_page(chat_id, 0, context.bot)
        return
    if action == "unassigned":
        await send_unassigned_page(chat_id, 0, context.bot)
        return
    if action == "stale_sites":
        await send_stale_sites_view(chat_id, context.bot)
        return
    if action == "prev":
        await step_pagination(chat_id, -1, context.bot)
        return
    if action == "next":
        await step_pagination(chat_id, +1, context.bot)
        return
    if action == "aireport":
        await send_ai_analyze_action(chat_id, context.bot)
        return
    if action == "logout":
        await send_logout_action(chat_id, context.bot)
        return
    if action == "admin_menu" and is_admin(user_id):
        await open_admin_panel(chat_id, context.bot)
        return
    if action == "theme_menu" and is_admin(user_id):
        await open_theme_menu(chat_id, context.bot)
        return
    if action == "notif_menu" and is_admin(user_id):
        await open_notif_demo_menu(chat_id, context.bot)
        return
    if action == "ask_refresh" and is_admin(user_id):
        AWAITING_INPUT[chat_id] = {"kind": "set_refresh_val"}
        await safe_send_html(
            context.bot,
            chat_id,
            f"  ▸ Naya sync interval seconds mein bhejein (15–3600, current: <code>{REFRESH_INTERVAL_SEC}s</code>):",
        )
        return
    if action == "ask_maint" and is_admin(user_id):
        AWAITING_INPUT[chat_id] = {"kind": "set_maint_msg"}
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ Maintenance message bhejein (ya band karne ke liye <code>OFF</code> bhejein):",
        )
        return
    if action == "toggle_bot" and is_admin(user_id):
        BOT_ENABLED = not BOT_ENABLED
        save_setting_db("bot_enabled", "1" if BOT_ENABLED else "0")
        await safe_send_html(
            context.bot,
            chat_id,
            f"  ∙ <b>Bot Status:</b> <code>{'ON' if BOT_ENABLED else 'OFF'}</code>",
            reply_markup=admin_sub_keyboard(),
        )
        return
    if action == "clear_alerts" and is_admin(user_id):
        clear_all_alerts_for_chat(chat_id)
        await safe_send_html(
            context.bot,
            chat_id,
            "  ∙ <b>Sent stage alerts cleared.</b>",
            reply_markup=admin_sub_keyboard(),
        )
        return

    # 8. AI Chat Mode or Direct AI Query
    if chat_id in AI_CHAT_MODE:
        await send_ai_answer_action(chat_id, raw_text, context.bot)
        return

    await safe_send_html(
        context.bot,
        chat_id,
        "  ▸ Menu button ya command use karein. Poori guide ke liye <code>📖 Manual</code> ya <code>/help</code> bhejein.",
        reply_markup=current_keyboard_for_chat(chat_id, user_id),
    )


# ==============================================================================
# BACKGROUND MONITOR: 2H -> 10H -> 1D (1-TIME PER STAGE) + DIGEST & RECOVERY
# ==============================================================================
ALERT_STAGES: list[tuple[str, float, str]] = [
    ("2h", 2.0, "2ʜ ᴅᴏᴡɴ"),
    ("10h", 10.0, "10ʜ ᴅᴏᴡɴ"),
    ("1d", 24.0, "1ᴅ ᴅᴏᴡɴ"),
]


async def background_monitor_loop(app) -> None:
    tick_counter = 0
    while True:
        try:
            await asyncio.sleep(5)
            tick_counter += 5
            if tick_counter < REFRESH_INTERVAL_SEC:
                continue
            tick_counter = 0

            if not BOT_ENABLED or MAINTENANCE_MODE:
                continue

            for chat_id, sess_data in list(USER_SESSIONS.items()):
                if sess_data.get("step") != "authenticated":
                    continue
                session = sess_data.get("session")
                if not session:
                    continue

                try:
                    analysis = await asyncio.to_thread(
                        run_self_analysis, session
                    )
                    sess_data["fail_streak"] = 0
                except Exception as exc:
                    sess_data["fail_streak"] = sess_data.get("fail_streak", 0) + 1
                    ensure_live_session(chat_id, sess_data)
                    logger.warning("Monitor poll error chat %s: %s", chat_id, exc)
                    continue

                if analysis["meta"]["server_error_500"]:
                    continue

                prev_analysis = LAST_SNAPSHOT.get(chat_id)
                LAST_SNAPSHOT[chat_id] = analysis
                save_snapshot_db(chat_id, analysis)

                prev_conn_map: dict[str, bool] = {}
                if prev_analysis:
                    for d in prev_analysis.get("all_devices", []):
                        prev_conn_map[d["serial_no"]] = bool(
                            d.get("is_connected")
                        )

                batch_lines: list[str] = []

                for dev in analysis.get("all_devices", []):
                    serial = dev.get("serial_no", "N/A")
                    is_conn = bool(dev.get("is_connected"))
                    off_hrs = dev.get("offline_hours")
                    label = format_industry_site_label(
                        dev.get("industry", "N/A"), dev.get("site", "N/A")
                    )

                    if is_conn:
                        if prev_conn_map.get(serial) is False:
                            clear_stage_alerts_for_device(chat_id, serial)
                            if NOTIF_STYLE not in ("alerts_only", "digest"):
                                await safe_send_html(
                                    app.bot,
                                    chat_id,
                                    (
                                        f"{themed_box('∙ ' + sc('SITE RECOVERED'))}\n"
                                        f"  ▸ Site   : <b>{label}</b>\n"
                                        f"  ▸ Serial : <b>{esc(serial)}</b>\n"
                                        f"  ▸ Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code>"
                                    ),
                                )
                        continue

                    if off_hrs is None:
                        continue

                    for stage_code, threshold_h, stage_badge in ALERT_STAGES:
                        stage_key = f"{chat_id}:{serial}:{stage_code}"
                        if (
                            off_hrs >= threshold_h
                            and stage_key not in SENT_STAGE_ALERTS
                        ):
                            mark_stage_alert_sent(stage_key)
                            off_str = format_off_since_text(
                                dev.get("last_seen"),
                                dev.get("offline_duration"),
                            )
                            if NOTIF_STYLE == "digest":
                                batch_lines.append(
                                    f"  • [{esc(stage_badge)}] <b>{label}</b> (<code>{esc(serial)}</code>) — <code>{esc(off_str)}</code>"
                                )
                            elif NOTIF_STYLE == "compact":
                                await safe_send_html(
                                    app.bot,
                                    chat_id,
                                    f"▸ • <b>{label}</b> [<code>{esc(stage_badge)}</code>] | <code>{esc(off_str)}</code>",
                                )
                            else:
                                await safe_send_html(
                                    app.bot,
                                    chat_id,
                                    (
                                        f"{themed_box('• ' + esc(stage_badge) + ' ALERT')}\n"
                                        f"  ▸ Site   : <b>{label}</b>\n"
                                        f"  ▸ Serial : <b>{esc(serial)}</b>\n"
                                        f"  ▸ Status : <code>{esc(off_str)}</code>\n"
                                        f"  ▸ Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code>"
                                    ),
                                )

                if batch_lines:
                    await safe_send_html(
                        app.bot,
                        chat_id,
                        f"{themed_box(sc('DIGEST ALERTS'))}\n" + "\n".join(batch_lines[:15]),
                    )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Monitor loop unexpected error: %s", exc)


async def post_init(app) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Authority panel & login"),
            BotCommand("help", "Operator manual & guide"),
            BotCommand("status", "Live status & aging summary"),
            BotCommand("allsite", "Assigned sites list"),
            BotCommand("dash", "Site dashboard + API report"),
            BotCommand("apireport", "UP Excise API report (<site> [date])"),
            BotCommand("instruments", "Tank / mm / L / instrument readings"),
            BotCommand("online", "Connected devices"),
            BotCommand("unassigned", "Unassigned N/A devices"),
            BotCommand("aging", "Aging buckets menu"),
            BotCommand("age", "Filter bucket (<1d|1w|24d|11m|1y|gt1y>)"),
            BotCommand("next", "Next page"),
            BotCommand("prev", "Previous page"),
            BotCommand("ai_analyze", "Compact Groq AI telemetry report"),
            BotCommand("ai", "Ask Groq AI (<question>)"),
            BotCommand("health", "System & parser health"),
            BotCommand("export", "Export devices CSV"),
            BotCommand("admin", "Admin authority settings"),
        ]
    )
    app.create_task(background_monitor_loop(app))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing hai!")

    init_db()
    load_persisted_state()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("allsite", cmd_allsite))
    app.add_handler(CommandHandler("dash", cmd_dash))
    app.add_handler(CommandHandler("apireport", cmd_apireport))
    app.add_handler(CommandHandler("instruments", cmd_instruments))
    app.add_handler(CommandHandler("online", cmd_online))
    app.add_handler(CommandHandler("unassigned", cmd_unassigned))
    app.add_handler(CommandHandler("aging", cmd_aging))
    app.add_handler(CommandHandler("age", cmd_age))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("prev", cmd_prev))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("site", cmd_site))
    app.add_handler(CommandHandler("ai_analyze", cmd_ai_analyze))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("exit_ai", cmd_exit_ai))

    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("set_theme", cmd_set_theme))
    app.add_handler(CommandHandler("bot_on", cmd_bot_on))
    app.add_handler(CommandHandler("bot_off", cmd_bot_off))
    app.add_handler(CommandHandler("maint_on", cmd_maint_on))
    app.add_handler(CommandHandler("maint_off", cmd_maint_off))
    app.add_handler(CommandHandler("set_refresh", cmd_set_refresh))
    app.add_handler(CommandHandler("set_notif", cmd_set_notif))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    logger.info("Baagi Jaat Ultra Telemetry Suite starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
