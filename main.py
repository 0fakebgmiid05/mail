import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import time
import traceback
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
BACKUP_DIR: str = os.getenv("BOT_BACKUP_DIR", "backups").strip() or "backups"
SCRIPT_PATH: str = os.path.abspath(__file__)

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

# Circuit Breaker & Error / Patch State
PORTAL_FAIL_COUNT: int = 0
PORTAL_CIRCUIT_OPEN_UNTIL: float = 0.0
LAST_ERROR_LOG: dict[str, Any] = {}
PENDING_AI_PATCH: dict[str, Any] = {}
PATCH_HISTORY: list[dict[str, Any]] = []

# Session, Navigation, Watchlist, Games & Cache State
USER_SESSIONS: dict[int, dict[str, Any]] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}
PREV_SNAPSHOT: dict[int, dict[str, Any]] = {}
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


def require_auth(chat_id: int) -> dict[str, Any] | None:
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        return None
    return sess_data


# ==============================================================================
# PERSISTENT SQLITE DATABASE, BACKUP & AUDIT LOGS
# ==============================================================================
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS patch_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            backup_file TEXT NOT NULL
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


def log_patch_audit_db(action: str, summary: str, backup_file: str) -> None:
    rec = {
        "time": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "action": action,
        "summary": summary,
        "backup": backup_file,
    }
    PATCH_HISTORY.append(rec)
    if len(PATCH_HISTORY) > 25:
        PATCH_HISTORY.pop(0)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO patch_audit(created_at, action, summary, backup_file) VALUES(?, ?, ?, ?)",
            (rec["time"], action, summary, backup_file),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Patch audit DB log failed: %s", exc)


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

        for created_at, action, summary, backup_file in cur.execute(
            "SELECT created_at, action, summary, backup_file FROM patch_audit ORDER BY id DESC LIMIT 15"
        ):
            PATCH_HISTORY.append(
                {
                    "time": created_at,
                    "action": action,
                    "summary": summary,
                    "backup": backup_file,
                }
            )
        conn.close()
    except Exception as exc:
        logger.warning("DB load_persisted_state failed: %s", exc)


def create_script_backup(tag: str = "manual") -> str:
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        b_path = os.path.join(
            BACKUP_DIR, f"{os.path.basename(SCRIPT_PATH)}.{stamp}.{tag}.bak"
        )
        shutil.copy2(SCRIPT_PATH, b_path)
        return b_path
    except Exception as exc:
        logger.warning("Backup creation failed: %s", exc)
        return ""


def get_latest_backup_file() -> str | None:
    if not os.path.isdir(BACKUP_DIR):
        return None
    files = sorted(
        [
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.endswith(".bak")
        ],
        key=lambda p: os.path.getmtime(p),
        reverse=True,
    )
    return files[0] if files else None


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
# 4 UI THEMES, TYPOGRAPHY & SAFE HTML + ANTI-LOOP ENGINE
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


def sanitize_ai_loop(text: str) -> str:
    """
    Prevents AI token/phrase repetition loops (such as repeated 'samay se bahut zyada...')
    and deduplicates consecutive repeated lines/phrases.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    raw = re.sub(
        r"\b(\w+(?:\s+\w+){0,4})\b(?:\s+\1\b){2,}",
        r"\1",
        raw,
        flags=re.IGNORECASE,
    )

    cleaned_lines: list[str] = []
    seen_line_counts: dict[str, int] = {}
    for line in raw.splitlines():
        ln = line.strip()
        if not ln:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        norm_key = re.sub(r"\W+", " ", ln.lower()).strip()
        cnt = seen_line_counts.get(norm_key, 0) + 1
        seen_line_counts[norm_key] = cnt
        if cnt > 2:
            continue
        cleaned_lines.append(ln[:260])
        if len(cleaned_lines) >= 18:
            break

    out = "\n".join(cleaned_lines).strip()
    if len(out) > 1400:
        out = out[:1400].rsplit(" ", 1)[0].strip() + "..."
    return out


def clean_ai_text(text: str) -> str:
    s = sanitize_ai_loop(text)
    s = re.sub(r"\*\*(.*?)\*\*", r"\1", s)
    s = s.replace("**", "").replace("__", "")
    return esc(s)


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
# GUEST LOUNGE MINI-GAMES
# ==============================================================================
def start_ttt_game(chat_id: int) -> str:
    board = [" "] * 9
    GAME_SESSIONS[chat_id] = {
        "game": "ttt",
        "board": board,
        "moves": 0,
    }
    return render_ttt_board(
        board, "Aap 'X' hain, Bot 'O' hai. 1 se 9 tak cell number bhejein:"
    )


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
        return render_ttt_board(board, "Khali cell (1–9) chuniye."), False

    board[idx] = "X"
    win = _check_ttt_winner(board)
    if win == "X":
        GAME_SESSIONS.pop(chat_id, None)
        return render_ttt_board(board, "Badhai! Aap jeet gaye!"), True
    if win == "DRAW":
        GAME_SESSIONS.pop(chat_id, None)
        return render_ttt_board(board, "Match Draw hua!"), True

    empty_cells = [i for i, v in enumerate(board) if v == " "]
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

    return f"  ▸ Try #{tries} (<code>{guess_num}</code>): {hint}", False


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

    done = (
        st["round"] >= st["max_rounds"]
        or st["user_score"] == 3
        or st["bot_score"] == 3
    )
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
# CONTEXT-AWARE DYNAMIC REPLY KEYBOARDS (BACK, NEXT & MAIN MENU EVERYWHERE)
# ==============================================================================
def guest_reply_keyboard() -> ReplyKeyboardMarkup:
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
                [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
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
                [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
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


def paginated_list_keyboard(user_id: int | None = None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
        [KeyboardButton("🏠 Main Menu")],
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
    rows.append([KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")])
    rows.append([KeyboardButton("🏠 Main Menu")])
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
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
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
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
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
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
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
            [KeyboardButton("🤖 AI Patch & Logs"), KeyboardButton("↩ Rollback Backup")],
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def ai_patch_sub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🛠 Generate AI Fix"),
                KeyboardButton("✅ Apply AI Patch"),
            ],
            [
                KeyboardButton("📋 Error & Patch Logs"),
                KeyboardButton("↩ Rollback Backup"),
            ],
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
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
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
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
            [KeyboardButton("⬅ Back"), KeyboardButton("Next ➡")],
            [KeyboardButton("🏠 Main Menu")],
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
    if state == "paginated_list":
        return paginated_list_keyboard(user_id)
    if state == "admin_menu" and is_admin(user_id):
        return admin_sub_keyboard()
    if state == "patch_menu" and is_admin(user_id):
        return ai_patch_sub_keyboard()
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
        if len(stack) > 15:
            stack.pop(0)
    MENU_STATE[chat_id] = new_state


async def pop_menu_state(chat_id: int, user_id: int | None, bot) -> None:
    AWAITING_INPUT.pop(chat_id, None)
    GAME_SESSIONS.pop(chat_id, None)
    stack = MENU_STACK.get(chat_id) or []
    prev_state = (
        stack.pop() if stack else ("main" if require_auth(chat_id) else "guest")
    )
    MENU_STATE[chat_id] = prev_state

    title_map = {
        "guest": "GUEST PORTAL",
        "main": "MAIN MENU",
        "dash_menu": "SITE DASHBOARD HUB",
        "api_menu": "EXCISE API REPORT",
        "age_menu": "AGING BUCKETS",
        "ops_menu": "OPS & EXPORT",
        "paginated_list": "PAGINATED VIEW",
        "admin_menu": "ADMIN AUTHORITY",
        "patch_menu": "AI PATCH & LOGS",
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
# PORTAL LOGIN, AUTO RE-LOGIN, CIRCUIT BREAKER & REAL ENVIROFRONT API CLIENTS
# ==============================================================================
def _ajax_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "text/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
    }


def _record_error(source: str, exc_or_msg: Any, tb_str: str = "") -> None:
    global LAST_ERROR_LOG, PORTAL_FAIL_COUNT, PORTAL_CIRCUIT_OPEN_UNTIL
    LAST_ERROR_LOG = {
        "time": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "source": source,
        "error": str(exc_or_msg)[:350],
        "traceback": (tb_str or traceback.format_exc())[-1400:],
    }
    PORTAL_FAIL_COUNT += 1
    if PORTAL_FAIL_COUNT >= 6:
        PORTAL_CIRCUIT_OPEN_UNTIL = time.time() + 45.0


def _reset_portal_health() -> None:
    global PORTAL_FAIL_COUNT, PORTAL_CIRCUIT_OPEN_UNTIL
    PORTAL_FAIL_COUNT = 0
    PORTAL_CIRCUIT_OPEN_UNTIL = 0.0


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
        _record_error("login_get", exc)
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
        _record_error("login_post", exc)
        return None, f"Login submit fail: {exc}"

    post_lower = (r_post.text or "").lower()
    if "account/login" in r_post.url.lower() and (
        "invalid" in post_lower
        or "error" in post_lower
        or "incorrect" in post_lower
    ):
        return None, "Galat username ya password."

    try:
        r_chk = sess.get(DEVICES_PAGE_URL, timeout=20, allow_redirects=True)
        if "account/login" in r_chk.url.lower():
            return None, "Session verify nahi hua (redirect to login)."
    except Exception as exc:
        _record_error("login_verify", exc)
        return None, f"Devices page verify fail: {exc}"

    _reset_portal_health()
    return sess, "OK"


def ensure_live_session(
    chat_id: int, sess_data: dict[str, Any]
) -> requests.Session | None:
    session: requests.Session | None = sess_data.get("session")
    u = str(sess_data.get("username") or "").strip()
    p = str(sess_data.get("password") or "").strip()

    if sess_data.get("fail_streak", 0) >= 2 and u and p:
        new_sess, err = login_envirofront(u, p)
        if new_sess:
            if session:
                try:
                    session.close()
                except Exception:
                    pass
            sess_data["session"] = new_sess
            sess_data["fail_streak"] = 0
            logger.info("Auto re-login succeeded for chat %s", chat_id)
            return new_sess
        logger.warning("Auto re-login failed for chat %s: %s", chat_id, err)

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

    is_stale = bool(is_conn and off_hours is not None and off_hours >= 0.5)

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
            for m in re.finditer(
                r"[?&]tags=([^&\"'\s]+)", raw, re.IGNORECASE
            ):
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

            extra_str = (
                "avg" if (latest_val is None and avg_val is not None) else ""
            )
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
    groups: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []
    anomalies: list[str] = []

    for item in instruments:
        raw_name = item.get("name", "")
        val_str = item.get("value", "")
        unit_str = item.get("unit", "")
        cat = item.get("category", "instrument")
        disp = f"{val_str} {unit_str}".strip()

        num_val = _to_float_or_none(val_str)
        if cat == "level_mm" and num_val is not None and num_val < 0:
            anomalies.append(f"{raw_name} ({disp} NEGATIVE/DRIFT)")

        base = re.sub(
            r"-(BL|L|TOT|MM)$", "", raw_name, flags=re.IGNORECASE
        ).strip()
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

    if anomalies:
        lines.append(
            f"  • <b>Zero/Drift Flag:</b> <code>{esc(', '.join(anomalies[:5]))}</code>"
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
                "siteId": site_id,
                "tags": tags_str,
                "_": str(_ts_ms()),
            },
            headers=_ajax_headers(dash_page_url),
            timeout=22,
        )
        if not r.ok:
            err_msg = f"Dashboard WS HTTP {r.status_code}"
        else:
            txt = (r.text or "").strip()
            if not txt or txt.startswith("<"):
                err_msg = "Empty or non-JSON dashboard response"
            else:
                payload = json.loads(txt)
                if isinstance(payload, dict):
                    instruments = _parse_dashboard_stats_and_tags(payload)
                    if not instruments:
                        err_msg = "No tag values returned"
                else:
                    err_msg = "Unexpected dashboard payload format"
    except Exception as exc:
        err_msg = f"Dashboard fetch error: {exc}"

    api_rep = fetch_site_api_report(session, site_id)

    res_obj = {
        "ok": bool(instruments) and not err_msg,
        "site_id": site_id,
        "site_code": site_code,
        "site_name_ws": site_name_ws,
        "url": dash_page_url,
        "error": err_msg,
        "instruments": instruments,
        "api_report": api_rep,
        "_ts": time.time(),
    }
    DASHBOARD_CACHE[cache_key] = res_obj
    return res_obj


def parse_devices_page_and_ws(
    session: requests.Session,
) -> tuple[list[dict[str, Any]], str]:
    now_utc = datetime.now(timezone.utc)
    ws_devs = fetch_devices_from_ws_api(session, now_utc)
    if ws_devs:
        _reset_portal_health()
        return ws_devs, "OK"

    try:
        r = session.get(DEVICES_PAGE_URL, timeout=22, allow_redirects=True)
        r.raise_for_status()
    except Exception as exc:
        _record_error("devices_page", exc)
        return [], f"Devices page load nahi hua: {exc}"

    if "account/login" in r.url.lower():
        return [], "Session expire ho gaya hai. Kripya dobara /login karein."

    soup = BeautifulSoup(r.text, "html.parser")
    devices: list[dict[str, Any]] = []
    seen_serials: set[str] = set()

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        if all(c == "" for c in cells):
            continue

        serial = cells[0] if len(cells) > 0 else "N/A"
        industry = cells[1] if len(cells) > 1 else "N/A"
        site_type = cells[2] if len(cells) > 2 else "N/A"
        device_type = cells[3] if len(cells) > 3 else "N/A"
        firmware = cells[4] if len(cells) > 4 else "N/A"
        raw_status = cells[5] if len(cells) > 5 else ""
        raw_last_seen = cells[6] if len(cells) > 6 else ""

        sid = ""
        link = tr.find("a", href=True)
        if link:
            m = re.search(r"/Site/(\d+)/", str(link["href"]), re.IGNORECASE)
            if m:
                sid = m.group(1)

        rec = normalize_device_record(
            serial_no=serial,
            industry=industry,
            site_type=site_type,
            device_type=device_type,
            firmware=firmware,
            raw_status=raw_status,
            raw_last_seen=raw_last_seen,
            now_utc=now_utc,
            site_id=sid,
        )
        if rec["serial_no"] not in seen_serials:
            seen_serials.add(rec["serial_no"])
            devices.append(rec)

    if not devices:
        return [], "Portal se koi device record nahi mila."

    _reset_portal_health()
    return devices, "OK"


# ==============================================================================
# FLEET ANALYSIS, AGING BUCKETS & SHIFT HANDOVER INTELLIGENCE
# ==============================================================================
def analyze_devices(devices: list[dict[str, Any]]) -> dict[str, Any]:
    now_ist = datetime.now(IST)
    checked_at_ist = now_ist.strftime("%d-%b-%Y %I:%M:%S %p IST")

    assigned_devs = [d for d in devices if not d.get("is_unassigned")]
    unassigned_devs = [d for d in devices if d.get("is_unassigned")]

    online_devs = [d for d in assigned_devs if d.get("is_connected")]
    offline_devs = [d for d in assigned_devs if not d.get("is_connected")]
    stale_devs = [d for d in assigned_devs if d.get("is_stale")]

    b_lt_1d: list[dict[str, Any]] = []
    b_lt_1w: list[dict[str, Any]] = []
    b_lt_24d: list[dict[str, Any]] = []
    b_lt_11m: list[dict[str, Any]] = []
    b_lt_1y: list[dict[str, Any]] = []
    b_gt_1y: list[dict[str, Any]] = []

    for d in offline_devs:
        oh = d.get("offline_hours")
        if oh is None:
            b_gt_1y.append(d)
        elif oh < 24.0:
            b_lt_1d.append(d)
        elif oh < 168.0:
            b_lt_1w.append(d)
        elif oh < 576.0:
            b_lt_24d.append(d)
        elif oh < 7920.0:
            b_lt_11m.append(d)
        elif oh <= 8760.0:
            b_lt_1y.append(d)
        else:
            b_gt_1y.append(d)

    def _off_sort_key(item: dict[str, Any]) -> float:
        val = item.get("offline_hours")
        return float(val) if val is not None else -1.0

    b_lt_1d.sort(key=_off_sort_key, reverse=True)
    b_lt_1w.sort(key=_off_sort_key, reverse=True)
    b_lt_24d.sort(key=_off_sort_key, reverse=True)
    b_lt_11m.sort(key=_off_sort_key, reverse=True)
    b_lt_1y.sort(key=_off_sort_key, reverse=True)
    b_gt_1y.sort(key=_off_sort_key, reverse=True)

    return {
        "checked_at_ist": checked_at_ist,
        "total_count": len(devices),
        "assigned_count": len(assigned_devs),
        "online_count": len(online_devs),
        "offline_count": len(offline_devs),
        "unassigned_count": len(unassigned_devs),
        "stale_count": len(stale_devs),
        "all_devices": devices,
        "assigned_devices": assigned_devs,
        "online_list": online_devs,
        "offline_list": offline_devs,
        "unassigned_list": unassigned_devs,
        "stale_list": stale_devs,
        "buckets": {
            "<1D": b_lt_1d,
            "<1W": b_lt_1w,
            "<24D": b_lt_24d,
            "<11M": b_lt_11m,
            "<1Y": b_lt_1y,
            ">1Y": b_gt_1y,
        },
        "bucket_counts": {
            "<1D": len(b_lt_1d),
            "<1W": len(b_lt_1w),
            "<24D": len(b_lt_24d),
            "<11M": len(b_lt_11m),
            "<1Y": len(b_lt_1y),
            ">1Y": len(b_gt_1y),
        },
    }


def build_status_message(analysis: dict[str, Any]) -> str:
    th = get_theme()
    b = th["bullet"]
    bc = analysis["bucket_counts"]
    return (
        f"{themed_box(sc(CLIENT_NAME + ' — FLEET STATUS'))}\n"
        f"{th['bar']}\n"
        f"  {b} Time       : <code>{esc(analysis['checked_at_ist'])}</code>\n"
        f"  {b} Total      : <b>{analysis['total_count']}</b> | Assigned: <b>{analysis['assigned_count']}</b>\n"
        f"  {b} Online     : {small_status_dot(True)} <b>{analysis['online_count']}</b> | "
        f"Offline: {small_status_dot(False)} <b>{analysis['offline_count']}</b>\n"
        f"  {b} Unassigned : <b>{analysis['unassigned_count']}</b> | "
        f"Stale (&gt;30m): <b>{analysis['stale_count']}</b>\n"
        f"{th['bar']}\n"
        f"  {b} Aging : &lt;1D <b>{bc['<1D']}</b> | &lt;1W <b>{bc['<1W']}</b> | &lt;24D <b>{bc['<24D']}</b>\n"
        f"  {b}         &lt;11M <b>{bc['<11M']}</b> | &lt;1Y <b>{bc['<1Y']}</b> | &gt;1Y <b>{bc['>1Y']}</b>"
    )


def build_shift_handover_message(
    analysis: dict[str, Any], chat_id: int | None = None
) -> str:
    th = get_theme()
    b = th["bullet"]
    bc = analysis["bucket_counts"]
    off_list = analysis.get("offline_list") or []
    stale_list = analysis.get("stale_list") or []

    lines = [
        themed_box(sc(CLIENT_NAME + " — SHIFT HANDOVER")),
        th["bar"],
        f"  {b} Time       : <code>{esc(analysis['checked_at_ist'])}</code>",
        f"  {b} Fleet      : Total <b>{analysis['total_count']}</b> | "
        f"{small_status_dot(True)} Online <b>{analysis['online_count']}</b> | "
        f"{small_status_dot(False)} Off <b>{analysis['offline_count']}</b>",
        f"  {b} Stale(&gt;30m): <b>{analysis['stale_count']}</b> | Unassigned: <b>{analysis['unassigned_count']}</b>",
        f"  {b} Aging      : &lt;1D <b>{bc['<1D']}</b> | &lt;1W <b>{bc['<1W']}</b> | &lt;24D <b>{bc['<24D']}</b> | &lt;11M <b>{bc['<11M']}</b>",
    ]

    if chat_id is not None:
        w_keys = WATCHLIST_SITES.get(chat_id) or []
        if w_keys:
            w_off = 0
            for d in off_list:
                hay = f"{d.get('site_id','')} {d.get('serial_no','')} {d.get('industry','')}".lower()
                if any(wk.lower() in hay for wk in w_keys):
                    w_off += 1
            lines.append(
                f"  {b} Watchlist  : <b>{len(w_keys)}</b> pinned | Offline now: <b>{w_off}</b>"
            )

    lines.append(f"\n{th['bar']}\n{b} <b>{sc('PRIORITY OFFLINE SITES')}</b>")
    if off_list:
        for idx, d in enumerate(off_list[:5], start=1):
            lbl = format_industry_site_label(
                d.get("industry", "N/A"), d.get("site", "N/A")
            )
            dur = format_duration_hm(d.get("offline_duration"))
            lines.append(
                f"  {idx}. <b>{lbl}</b> — <code>{esc(dur)}</code>"
            )
    else:
        lines.append("  • Koi offline site nahi hai.")

    if stale_list:
        lines.append(f"\n{b} <b>{sc('STALE ACTIVE SITES')}</b>")
        for d in stale_list[:4]:
            lbl = format_industry_site_label(
                d.get("industry", "N/A"), d.get("site", "N/A")
            )
            dur = format_duration_hm(d.get("offline_duration"))
            lines.append(f"  • {lbl} — <code>{esc(dur)}</code>")

    return "\n".join(lines)


# ==============================================================================
# GROQ AI CLIENT, SAFE SCHEMA PARSER & CONTROLLED PATCH GENERATOR
# ==============================================================================
def _list_groq_models() -> list[str]:
    if not GROQ_API_KEY:
        return ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=12,
        )
        if r.ok:
            data = r.json()
            ids = [
                m.get("id")
                for m in (data.get("data") or [])
                if isinstance(m, dict) and m.get("id")
            ]
            if ids:
                return ids[:6]
    except Exception:
        pass
    return ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]


def call_groq_ai(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 420,
    temperature: float = 0.25,
) -> tuple[bool, str]:
    if not GROQ_API_KEY:
        return False, "GROQ_API_KEY set nahi hai."

    models = _list_groq_models()
    last_err = "Groq AI se jawab nahi mila."

    for model_id in models:
        try:
            r = requests.post(
                GROQ_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "frequency_penalty": 0.4,
                    "presence_penalty": 0.2,
                },
                timeout=25,
            )
            if r.ok:
                data = r.json()
                content = (
                    (((data.get("choices") or [{}])[0]).get("message") or {})
                    .get("content")
                    or ""
                ).strip()
                cleaned = sanitize_ai_loop(content)
                if cleaned:
                    return True, cleaned
                last_err = "AI ne khali ya repeated text diya."
            else:
                last_err = f"Groq HTTP {r.status_code}"
        except Exception as exc:
            last_err = str(exc)

    return False, last_err


def generate_ai_brief_text(analysis: dict[str, Any]) -> str:
    bc = analysis["bucket_counts"]
    off_names = [
        d.get("industry", "N/A") for d in (analysis.get("offline_list") or [])[:6]
    ]
    summary_payload = {
        "client": CLIENT_NAME,
        "checked_at_ist": analysis["checked_at_ist"],
        "total": analysis["total_count"],
        "online": analysis["online_count"],
        "offline": analysis["offline_count"],
        "stale_gt_30m": analysis["stale_count"],
        "unassigned": analysis["unassigned_count"],
        "aging": bc,
        "top_offline": off_names,
    }
    ok, resp = call_groq_ai(
        system_prompt=(
            "Tum ek crisp, grounded telemetry operations advisor ho. "
            "Hinglish mein 4-5 short bullet points do. "
            "Kisi shabd ya vaakyansh ko dohrana mat. Sirf diye gaye data par adharit baat karo."
        ),
        user_prompt=f"Fleet telemetry snapshot:\n{json.dumps(summary_payload)}",
        max_tokens=320,
        temperature=0.2,
    )
    if ok and resp:
        return clean_ai_text(resp)

    return (
        f"• Offline sites: <b>{analysis['offline_count']}</b> / {analysis['assigned_count']} assigned "
        f"(Top: <code>{esc(', '.join(off_names[:3]) or 'None')}</code>)\n"
        f"• Stale (&gt;30m): <b>{analysis['stale_count']}</b> | Unassigned: <b>{analysis['unassigned_count']}</b>\n"
        f"• Aging &gt;1Y: <b>{bc['>1Y']}</b> | &lt;11M: <b>{bc['<11M']}</b> | &lt;1D: <b>{bc['<1D']}</b>\n"
        f"• AI fallback note: {esc(resp)}"
    )


def answer_groq_query(query_text: str, analysis: dict[str, Any]) -> str:
    q = (query_text or "").strip()
    m_site = re.search(r"\b(\d{7,12})\b", q)
    if m_site:
        sid = m_site.group(1)
        cached = API_REPORT_CACHE.get(f"{sid}:{today_ist_report_date()}") or {}
        if cached:
            return (
                f"Site <code>{esc(sid)}</code> ({esc(cached.get('report_date',''))}): "
                f"Total <b>{cached.get('total', 0)}</b> | I: <b>{cached.get('initial_count', 0)}</b> | "
                f"F: <b>{cached.get('final_count', 0)}</b> | Pending F: <b>{cached.get('pending_final_count', 0)}</b> | "
                f"Success: <b>{cached.get('success', 0)}</b> | Failed: <b>{cached.get('failed', 0)}</b>"
            )

    ctx = {
        "checked_at_ist": analysis.get("checked_at_ist", ""),
        "total": analysis.get("total_count", 0),
        "online": analysis.get("online_count", 0),
        "offline": analysis.get("offline_count", 0),
        "stale": analysis.get("stale_count", 0),
        "unassigned": analysis.get("unassigned_count", 0),
        "aging": analysis.get("bucket_counts", {}),
    }
    ok, resp = call_groq_ai(
        system_prompt=(
            "Tum ek helpful telemetry assistant ho. Hinglish mein seedha, saaf aur bina repetition ke jawab do. "
            "Diye gaye fleet context ke bahar galat data mat banao."
        ),
        user_prompt=f"Context: {json.dumps(ctx)}\nQuestion: {q}",
        max_tokens=300,
        temperature=0.25,
    )
    if ok and resp:
        return clean_ai_text(resp)
    return f"AI query response available nahi: {esc(resp)}"


def prepare_ai_error_patch() -> tuple[bool, str]:
    global PENDING_AI_PATCH
    if not LAST_ERROR_LOG:
        return False, "Koi recent error log recorded nahi hai."

    err_info = {
        "source": LAST_ERROR_LOG.get("source", ""),
        "error": LAST_ERROR_LOG.get("error", ""),
        "traceback": LAST_ERROR_LOG.get("traceback", ""),
    }
    ok, ai_out = call_groq_ai(
        system_prompt=(
            "Tum Python telemetry bot ke liye safe patch advisor ho. "
            "JSON output do with keys: summary, target_snippet, replacement_snippet. "
            "Sirf chhota, safe bugfix suggest karo; bina zaroorat bada rewrite mat karo."
        ),
        user_prompt=f"Error details:\n{json.dumps(err_info)}",
        max_tokens=450,
        temperature=0.15,
    )
    if not ok:
        return False, f"AI patch generation fail: {ai_out}"

    summary = "AI suggested recovery check"
    old_snip = ""
    new_snip = ""
    try:
        m_json = re.search(r"\{.*\}", ai_out, flags=re.DOTALL)
        if m_json:
            obj = json.loads(m_json.group(0))
            if isinstance(obj, dict):
                summary = str(obj.get("summary") or summary).strip()[:160]
                old_snip = str(obj.get("target_snippet") or "")
                new_snip = str(obj.get("replacement_snippet") or "")
    except Exception:
        summary = ai_out[:160]

    PENDING_AI_PATCH = {
        "created_at": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "source": LAST_ERROR_LOG.get("source", ""),
        "summary": summary,
        "target_snippet": old_snip,
        "replacement_snippet": new_snip,
        "raw_ai": ai_out[:700],
    }
    return True, summary


def apply_pending_ai_patch() -> tuple[bool, str]:
    global PENDING_AI_PATCH
    if not PENDING_AI_PATCH:
        return False, "Apply karne ke liye koi pending AI patch nahi hai."

    old_snip = PENDING_AI_PATCH.get("target_snippet") or ""
    new_snip = PENDING_AI_PATCH.get("replacement_snippet") or ""
    summary = PENDING_AI_PATCH.get("summary") or "AI patch"

    backup_file = create_script_backup("pre_ai_patch")
    if not backup_file:
        return False, "Backup create nahi ho saka; patch abort kiya gaya."

    if not old_snip or not new_snip:
        log_patch_audit_db("ADVISORY_ONLY", summary, backup_file)
        PENDING_AI_PATCH = {}
        return (
            True,
            f"Backup bana ({os.path.basename(backup_file)}). Patch advisory-only tha, koi file overwrite nahi hui.",
        )

    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        if src.count(old_snip) != 1:
            return (
                False,
                "Target snippet unique match nahi hua (0 ya >1 matches). Safe abort kiya gaya.",
            )
        patched_src = src.replace(old_snip, new_snip, 1)
        compile(patched_src, SCRIPT_PATH, "exec")
        with open(SCRIPT_PATH, "w", encoding="utf-8") as f:
            f.write(patched_src)
        log_patch_audit_db("APPLIED", summary, backup_file)
        PENDING_AI_PATCH = {}
        return (
            True,
            f"Patch apply aur syntax-verify ho gaya! Backup: {os.path.basename(backup_file)}",
        )
    except SyntaxError as se:
        return False, f"SyntaxError aaya; file edit nahi ki gayi: {se}"
    except Exception as exc:
        _record_error("apply_patch", exc)
        return False, f"Patch apply fail: {exc}"


def rollback_latest_backup() -> tuple[bool, str]:
    b_file = get_latest_backup_file()
    if not b_file:
        return False, "Koi backup file nahi mili."
    try:
        with open(b_file, "r", encoding="utf-8") as f:
            b_src = f.read()
        compile(b_src, SCRIPT_PATH, "exec")
        shutil.copy2(b_file, SCRIPT_PATH)
        log_patch_audit_db("ROLLBACK", f"Restored {os.path.basename(b_file)}", b_file)
        return True, f"Rollback safaltapoorvak ho gaya: {os.path.basename(b_file)}"
    except Exception as exc:
        return False, f"Rollback fail: {exc}"


# ==============================================================================
# DISPLAY FORMATTERS (ALL SITES, INSTRUMENTS, API REPORT, AGING, HEALTH, CSV)
# ==============================================================================
def build_all_sites_page(
    analysis: dict[str, Any], page: int = 1, page_size: int = 5
) -> tuple[str, int, int]:
    devs = analysis.get("all_devices") or []
    total_items = len(devs)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    chunk = devs[start:end]

    th = get_theme()
    header = (
        f"{themed_box(sc('ALL FLEET SITES'), f'Page {page}/{total_pages} • Total {total_items}')}\n"
        f"{th['bar']}\n"
        f"  {th['bullet']} Checked: <code>{esc(analysis['checked_at_ist'])}</code>\n"
    )
    if not chunk:
        return header + "\n  • Koi site record nahi mila.", page, total_pages

    blocks = [header]
    for idx, d in enumerate(chunk, start=start + 1):
        blocks.append(format_premium_device_block(d, index=idx))

    return "\n\n".join(blocks), page, total_pages


def build_filtered_devices_page(
    devices: list[dict[str, Any]],
    title: str,
    checked_at: str,
    page: int = 1,
    page_size: int = 5,
) -> tuple[str, int, int]:
    total_items = len(devices)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    end = start + page_size
    chunk = devices[start:end]

    th = get_theme()
    header = (
        f"{themed_box(sc(title), f'Page {page}/{total_pages} • Count {total_items}')}\n"
        f"{th['bar']}\n"
        f"  {th['bullet']} Checked: <code>{esc(checked_at)}</code>\n"
    )
    if not chunk:
        return header + "\n  • Is category mein koi record nahi hai.", page, total_pages

    blocks = [header]
    for idx, d in enumerate(chunk, start=start + 1):
        blocks.append(format_premium_device_block(d, index=idx))
    return "\n\n".join(blocks), page, total_pages


def build_instruments_page(
    session: requests.Session,
    analysis: dict[str, Any],
    page: int = 1,
    page_size: int = 5,
) -> tuple[str, int, int]:
    devs = analysis.get("assigned_devices") or []
    total_items = len(devs)
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))

    start = (page - 1) * page_size
    end = start + page_size
    chunk = devs[start:end]

    th = get_theme()
    b = th["bullet"]
    header = (
        f"{themed_box(sc('INSTRUMENTS & TANKS'), f'Page {page}/{total_pages}')}\n"
        f"{th['bar']}\n"
        f"  {b} Checked: <code>{esc(analysis['checked_at_ist'])}</code>\n"
    )
    if not chunk:
        return header + "\n  • Koi assigned site nahi mili.", page, total_pages

    blocks = [header]
    for idx, d in enumerate(chunk, start=start + 1):
        lbl = format_industry_site_label(
            d.get("industry", "N/A"), d.get("site", "N/A")
        )
        dot = small_status_dot(bool(d.get("is_connected")))
        st_word = "CONNECTED" if d.get("is_connected") else "DISCONNECTED"
        sid = d.get("site_id") or "N/A"
        ser = d.get("serial_no") or "N/A"

        card_lines = [
            f"┌─ #{idx} <b>{lbl}</b>",
            f"│  {b} Serial : <code>{esc(ser)}</code> | SiteID: <code>{esc(sid)}</code> | {dot} <b>{st_word}</b>",
        ]

        if str(sid).isdigit():
            dash = scrape_site_dashboard_details(session, d)
            insts = dash.get("instruments") or []
            if insts:
                card_lines.extend(group_readings_by_tank(insts))
            else:
                reason = dash.get("error") or "No tag values returned"
                card_lines.append(f"│  • Readings: <code>{esc(reason)}</code>")
        else:
            card_lines.append("│  • Readings: <code>Site ID missing</code>")

        card_lines.append("└──────────────────────")
        blocks.append("\n".join(card_lines))

    return "\n\n".join(blocks), page, total_pages


def build_site_dashboard_card(
    session: requests.Session,
    dev: dict[str, Any],
    index: int | None = None,
    chat_id: int | None = None,
) -> str:
    th = get_theme()
    b = th["bullet"]
    lbl = format_industry_site_label(
        dev.get("industry", "N/A"), dev.get("site", "N/A")
    )
    dot = small_status_dot(bool(dev.get("is_connected")))
    st_word = "CONNECTED" if dev.get("is_connected") else "DISCONNECTED"
    off_txt = (
        "(Active)"
        if dev.get("is_connected")
        else format_off_since_text(
            dev.get("last_seen"), dev.get("offline_duration")
        )
    )
    prefix = f"#{index} " if index is not None else ""
    sid = str(dev.get("site_id") or "").strip()
    ser = str(dev.get("serial_no") or "N/A").strip()

    pinned_badge = ""
    if chat_id is not None:
        w_list = WATCHLIST_SITES.get(chat_id) or []
        if any(
            k.lower() in f"{sid} {ser} {dev.get('industry','')}".lower()
            for k in w_list
        ):
            pinned_badge = " [PINNED]"

    lines = [
        themed_box(sc(prefix + lbl + pinned_badge)),
        th["bar"],
        f"  {b} Serial : <b>{esc(ser)}</b> | SiteID: <code>{esc(sid or 'N/A')}</code>",
        f"  {b} Status : {dot} <b>{st_word}</b> | Seen: <code>{esc(dev.get('display_time', 'N/A'))}</code>",
        f"  {b} Off    : <code>{esc(off_txt)}</code>",
    ]

    if sid.isdigit():
        dash = scrape_site_dashboard_details(session, dev)
        if dash.get("url"):
            lines.append(f"  {b} URL    : <code>{esc(dash['url'])}</code>")
        insts = dash.get("instruments") or []
        if insts:
            lines.append(f"\n  {b} <b>{sc('LIVE TAGS & TANKS')}</b>")
            lines.extend(group_readings_by_tank(insts))
        else:
            lines.append(
                f"  • Readings: <code>{esc(dash.get('error') or 'No tag values')}</code>"
            )

        rep = dash.get("api_report") or {}
        if rep:
            lines.append(
                f"  {b} Excise ({esc(rep.get('report_date',''))}): "
                f"Total <b>{rep.get('total', 0)}</b> | I: <b>{rep.get('initial_count', 0)}</b> | "
                f"F: <b>{rep.get('final_count', 0)}</b> | Pending F: <b>{rep.get('pending_final_count', 0)}</b> | "
                f"OK: <b>{rep.get('success', 0)}</b> / Fail: <b>{rep.get('failed', 0)}</b>"
            )
            if rep.get("latest_result"):
                lines.append(
                    f"  {b} Latest : <code>{esc(rep['latest_result'])}</code>"
                )
    else:
        lines.append("  • Dashboard / Excise WS ke liye Site ID available nahi hai.")

    return "\n".join(lines)


def build_api_report_card(
    session: requests.Session,
    dev: dict[str, Any],
    report_date: str | None = None,
    vat_code: str = "",
) -> str:
    th = get_theme()
    b = th["bullet"]
    lbl = format_industry_site_label(
        dev.get("industry", "N/A"), dev.get("site", "N/A")
    )
    sid = str(dev.get("site_id") or "").strip()
    rdate = (report_date or today_ist_report_date()).strip()

    if not sid.isdigit():
        return (
            f"{themed_box(sc('EXCISE API REPORT'))}\n"
            f"  • <b>{lbl}</b>: Site ID nahi mila."
        )

    rep = fetch_site_api_report(session, sid, report_date=rdate, vat_code=vat_code)
    v_list = rep.get("vat_codes") or []
    v_disp = ", ".join(v_list[:8]) if v_list else "None"

    lines = [
        themed_box(sc("EXCISE API REPORT"), f"{lbl} • {rdate}"),
        th["bar"],
        f"  {b} SiteID    : <code>{esc(sid)}</code> | VATs: <code>{esc(v_disp)}</code>",
        f"  {b} Total Reqs: <b>{rep.get('total', 0)}</b> | "
        f"Initial (I): <b>{rep.get('initial_count', 0)}</b> | "
        f"Final (F): <b>{rep.get('final_count', 0)}</b> | "
        f"Pending F: <b>{rep.get('pending_final_count', 0)}</b>",
        f"  {b} Success   : <b>{rep.get('success', 0)}</b> | Failed: <b>{rep.get('failed', 0)}</b>",
    ]
    if rep.get("latest_ts"):
        lines.append(f"  {b} Latest TS : <code>{esc(rep['latest_ts'])}</code>")
    if rep.get("latest_result"):
        lines.append(f"  {b} Latest Res: <code>{esc(rep['latest_result'])}</code>")

    pairs = rep.get("pairs") or []
    if pairs:
        lines.append(f"\n  {b} <b>{sc('REQUEST PAIRS (I → F)')}</b>")
        for p in pairs[:6]:
            has_i = "I" if p.get("initial") else "-"
            has_f = "F" if p.get("final") else "-"
            lines.append(
                f"  • Req <code>{esc(p.get('request_id','N/A'))}</code> "
                f"[{esc(p.get('vat_code','-'))}] ({has_i}→{has_f}): "
                f"<code>{esc(p.get('delta_text',''))}</code>"
            )
    elif rep.get("error"):
        lines.append(f"  • Note: <code>{esc(rep['error'])}</code>")
    else:
        lines.append("  • Is date ke liye koi API request record nahi mila.")

    return "\n".join(lines)


def build_health_report(analysis: dict[str, Any]) -> str:
    th = get_theme()
    b = th["bullet"]
    cb_status = (
        f"OPEN (until {datetime.fromtimestamp(PORTAL_CIRCUIT_OPEN_UNTIL, tz=IST).strftime('%I:%M:%S %p')})"
        if time.time() < PORTAL_CIRCUIT_OPEN_UNTIL
        else "CLOSED (Healthy)"
    )
    return (
        f"{themed_box(sc('SYSTEM & PORTAL HEALTH'))}\n"
        f"{th['bar']}\n"
        f"  {b} Theme          : <b>{esc(get_theme()['label'])}</b> ({esc(ACTIVE_THEME)})\n"
        f"  {b} Notif Mode     : <b>{esc(NOTIF_STYLE)}</b>\n"
        f"  {b} Poll Interval  : <b>{REFRESH_INTERVAL_SEC}s</b>\n"
        f"  {b} Bot / Maint    : <b>{'ON' if BOT_ENABLED else 'OFF'}</b> / <b>{'ON' if MAINTENANCE_MODE else 'OFF'}</b>\n"
        f"  {b} Circuit Breaker: <code>{esc(cb_status)}</code> (Fails: {PORTAL_FAIL_COUNT})\n"
        f"  {b} Cache Counts   : Dash={len(DASHBOARD_CACHE)} | API={len(API_REPORT_CACHE)} | Tags={len(SITE_TAGS_CACHE)}\n"
        f"  {b} DB & Backups   : <code>{esc(DB_PATH)}</code> | <code>{esc(BACKUP_DIR)}</code>"
    )


def build_fleet_csv_bytes(analysis: dict[str, Any]) -> io.BytesIO:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "SerialNo",
            "SiteId",
            "SiteCode",
            "Industry",
            "SiteType",
            "Status",
            "IsStale",
            "IsUnassigned",
            "DeviceType",
            "Firmware",
            "OfflineDuration",
            "LastSeenIST",
        ]
    )
    for d in analysis.get("all_devices") or []:
        writer.writerow(
            [
                d.get("serial_no", ""),
                d.get("site_id", ""),
                d.get("site_code", ""),
                d.get("industry", ""),
                d.get("site", ""),
                d.get("conn_label", ""),
                "YES" if d.get("is_stale") else "NO",
                "YES" if d.get("is_unassigned") else "NO",
                d.get("device_type", ""),
                d.get("firmware", ""),
                format_duration_hm(d.get("offline_duration")),
                d.get("display_time", ""),
            ]
        )
    out = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    out.name = f"baagijaat_fleet_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv"
    out.seek(0)
    return out


# ==============================================================================
# DEMO ALERT SIMULATOR & BACKGROUND POLLING LOOP
# ==============================================================================
async def run_demo_alert(bot, chat_id: int, demo_kind: str) -> None:
    snap = LAST_SNAPSHOT.get(chat_id) or {}
    devs = snap.get("assigned_devices") or []
    sample = devs[0] if devs else {
        "industry": "DEMO DISTILLERY UNIT",
        "site": "Distillery",
        "serial_no": "2210100999",
        "site_id": "2210100999",
        "device_type": "M2M-PRO",
        "firmware": "v4.2",
        "is_connected": False,
        "is_stale": False,
        "display_time": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "last_seen": datetime.now(timezone.utc) - timedelta(hours=3),
        "offline_duration": timedelta(hours=3),
    }

    lbl = format_industry_site_label(
        sample.get("industry", "DEMO SITE"), sample.get("site", "Unit")
    )
    ser = esc(sample.get("serial_no", "N/A"))

    if demo_kind == "2h":
        msg = (
            f"{themed_box(sc('DEMO ALERT — 2H DOWN'))}\n"
            f"  ▸ Site   : <b>{lbl}</b> (Serial: <code>{ser}</code>)\n"
            f"  ▸ Stage  : 2h offline threshold triggered."
        )
    elif demo_kind == "10h":
        msg = (
            f"{themed_box(sc('DEMO ALERT — 10H DOWN'))}\n"
            f"  ▸ Site   : <b>{lbl}</b> (Serial: <code>{ser}</code>)\n"
            f"  ▸ Stage  : 10h prolonged outage warning."
        )
    elif demo_kind == "1d":
        msg = (
            f"{themed_box(sc('DEMO ALERT — 1D CRITICAL'))}\n"
            f"  ▸ Site   : <b>{lbl}</b> (Serial: <code>{ser}</code>)\n"
            f"  ▸ Stage  : 24h+ critical downtime escalation."
        )
    else:
        msg = (
            f"{themed_box(sc('DEMO ALERT — RECOVERED'))}\n"
            f"  ▸ Site   : <b>{lbl}</b> (Serial: <code>{ser}</code>)\n"
            f"  ▸ Status : {small_status_dot(True)} <b>CONNECTED</b> — telemetry restored."
        )

    await safe_send_html(bot, chat_id, msg)


async def _emit_alert_or_digest(bot, chat_id: int, html_msg: str) -> None:
    if NOTIF_STYLE == "digest":
        DIGEST_QUEUE.setdefault(chat_id, []).append(html_msg)
        return
    await safe_send_html(bot, chat_id, html_msg)


async def background_poll_loop(app) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            if not BOT_ENABLED or MAINTENANCE_MODE:
                await asyncio.sleep(max(15, REFRESH_INTERVAL_SEC))
                continue

            if time.time() < PORTAL_CIRCUIT_OPEN_UNTIL:
                await asyncio.sleep(15)
                continue

            for chat_id, sess_data in list(USER_SESSIONS.items()):
                if sess_data.get("step") != "authenticated":
                    continue

                session = ensure_live_session(chat_id, sess_data)
                if not session:
                    continue

                devs, err = await asyncio.to_thread(
                    parse_devices_page_and_ws, session
                )
                if not devs or err != "OK":
                    sess_data["fail_streak"] = sess_data.get("fail_streak", 0) + 1
                    continue

                sess_data["fail_streak"] = 0
                analysis = analyze_devices(devs)
                PREV_SNAPSHOT[chat_id] = LAST_SNAPSHOT.get(chat_id) or {}
                LAST_SNAPSHOT[chat_id] = analysis
                save_snapshot_db(chat_id, analysis)

                for d in analysis.get("assigned_devices") or []:
                    ser = str(d.get("serial_no") or "")
                    if not ser:
                        continue
                    lbl = format_industry_site_label(
                        d.get("industry", "N/A"), d.get("site", "N/A")
                    )
                    w_keys = WATCHLIST_SITES.get(chat_id) or []
                    is_pinned = any(
                        wk.lower()
                        in f"{d.get('site_id','')} {ser} {d.get('industry','')}".lower()
                        for wk in w_keys
                    )
                    pin_tag = " [PINNED]" if is_pinned else ""

                    if d.get("is_connected"):
                        had_down = any(
                            k.startswith(f"{chat_id}:{ser}:down_")
                            for k in SENT_STAGE_ALERTS
                        )
                        clear_stage_alerts_for_device(chat_id, ser)
                        if had_down or is_pinned:
                            if NOTIF_STYLE != "alerts_only" or is_pinned:
                                await _emit_alert_or_digest(
                                    app.bot,
                                    chat_id,
                                    f"∙ <b>RECOVERED{esc(pin_tag)}:</b> <b>{lbl}</b> (<code>{esc(ser)}</code>) is back online.",
                                )
                    else:
                        oh = d.get("offline_hours") or 0.0
                        stage = None
                        if oh >= 24.0:
                            stage = "down_1d"
                        elif oh >= 10.0:
                            stage = "down_10h"
                        elif oh >= 2.0 or is_pinned:
                            stage = "down_2h"

                        if stage:
                            st_key = f"{chat_id}:{ser}:{stage}"
                            if st_key not in SENT_STAGE_ALERTS:
                                mark_stage_alert_sent(st_key)
                                dur = format_duration_hm(
                                    d.get("offline_duration")
                                )
                                await _emit_alert_or_digest(
                                    app.bot,
                                    chat_id,
                                    f"• <b>OFFLINE{esc(pin_tag)} ({esc(stage.replace('down_',''))}):</b> "
                                    f"<b>{lbl}</b> (<code>{esc(ser)}</code>) — Off <code>{esc(dur)}</code>",
                                )

                if NOTIF_STYLE == "digest":
                    q_list = DIGEST_QUEUE.get(chat_id) or []
                    if len(q_list) >= 5:
                        DIGEST_QUEUE[chat_id] = []
                        digest_body = (
                            f"{themed_box(sc('DIGEST ALERTS'))}\n"
                            + "\n".join(q_list[:12])
                        )
                        await safe_send_html(app.bot, chat_id, digest_body)

        except Exception as exc:
            _record_error("background_poll_loop", exc)
            logger.warning("Background poll error: %s", exc)

        await asyncio.sleep(max(15, REFRESH_INTERVAL_SEC))


# ==============================================================================
# COMMAND & MESSAGE HANDLERS
# ==============================================================================
def _check_bot_gate(user_id: int | None) -> str | None:
    if is_admin(user_id):
        return None
    if not BOT_ENABLED:
        return "Bot filhal offline/disabled hai."
    if MAINTENANCE_MODE:
        return MAINTENANCE_MSG
    return None


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else None

    gate_err = _check_bot_gate(user_id)
    if gate_err:
        await safe_send_html(context.bot, chat_id, esc(gate_err))
        return

    AWAITING_INPUT.pop(chat_id, None)
    GAME_SESSIONS.pop(chat_id, None)
    AI_CHAT_MODE.discard(chat_id)

    if require_auth(chat_id):
        MENU_STATE[chat_id] = "main"
        MENU_STACK[chat_id] = ["main"]
        snap = LAST_SNAPSHOT.get(chat_id)
        if snap:
            await safe_send_html(
                context.bot,
                chat_id,
                build_status_message(snap),
                reply_markup=main_reply_keyboard(user_id),
            )
        else:
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc(CLIENT_NAME + ' — COMMAND CENTER'), 'Authenticated Session Active')}",
                reply_markup=main_reply_keyboard(user_id),
            )
        return

    MENU_STATE[chat_id] = "guest"
    MENU_STACK[chat_id] = ["guest"]
    welcome = (
        f"{themed_box(sc(CLIENT_NAME + ' — TELEMETRY SUITE'), 'EnviroFRONT & Excise Operations')}\n"
        f"{get_theme()['bar']}\n"
        "  ▸ Portal access ke liye <b>🔐 Login</b> dabayein ya <code>/login</code> bhejein.\n"
        "  ▸ Login se pehle <b>📖 Manual</b> dekhein ya <b>🎮 Games Lounge</b> mein mini-games khelein."
    )
    await safe_send_html(
        context.bot,
        chat_id,
        welcome,
        reply_markup=guest_reply_keyboard(),
    )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    gate_err = _check_bot_gate(user_id)
    if gate_err:
        await safe_send_html(context.bot, chat_id, esc(gate_err))
        return

    GAME_SESSIONS.pop(chat_id, None)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    AWAITING_INPUT[chat_id] = {"action": "login_user"}
    await safe_send_html(
        context.bot,
        chat_id,
        f"{themed_box(sc('PORTAL LOGIN'))}\n  ▸ Apna EnviroFRONT <b>Username</b> bhejein:",
        reply_markup=guest_keyboard_for_chat(chat_id),
    )


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = USER_SESSIONS.pop(chat_id, None)
    if sess_data and sess_data.get("session"):
        try:
            sess_data["session"].close()
        except Exception:
            pass
    AWAITING_INPUT.pop(chat_id, None)
    AI_CHAT_MODE.discard(chat_id)
    MENU_STATE[chat_id] = "guest"
    MENU_STACK[chat_id] = ["guest"]
    await safe_send_html(
        context.bot,
        chat_id,
        f"{themed_box(sc('LOGGED OUT'))}\n  ▸ Session band kar diya gaya hai.",
        reply_markup=guest_reply_keyboard(),
    )


async def cmd_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    manual_txt = (
        f"{themed_box(sc('OPERATOR MANUAL'))}\n"
        f"{get_theme()['bar']}\n"
        "  ▸ <b>◈ Status / ◈ All Sites</b>: Fleet overview aur paginated site list.\n"
        "  ▸ <b>🌐 Site Dashboard</b>: Live tank `mm` / `L` readings, tags aur Excise summary.\n"
        "  ▸ <b>📡 API Report</b>: Today / Custom Date / Site Excise API (`I` → `F`) report.\n"
        "  ▸ <b>📐 Instruments</b>: Sabhi sites ke instruments & tanks (with zero/drift flags).\n"
        "  ▸ <b>⏳ Aging</b>: Offline buckets (<code>&lt;1D</code> se <code>&gt;1Y</code>).\n"
        "  ▸ <b>⭐ Watchlist</b>: Priority pinned sites & alerts.\n"
        "  ▸ <b>🤖 AI Suite</b>: Grounded AI brief aur Q&amp;A (anti-loop protected).\n"
        "  ▸ <b>⬅ Back / Next ➡ / 🏠 Main Menu</b>: Har view mein direct navigation."
    )
    await safe_send_html(
        context.bot,
        chat_id,
        manual_txt,
        reply_markup=current_keyboard_for_chat(chat_id, user_id),
    )


async def _refresh_or_get_snapshot(
    chat_id: int, sess_data: dict[str, Any], bot
) -> dict[str, Any] | None:
    cached = LAST_SNAPSHOT.get(chat_id)
    if cached and cached.get("all_devices"):
        return cached

    session = ensure_live_session(chat_id, sess_data)
    if not session:
        await safe_send_html(
            bot, chat_id, "Session available nahi hai. Kripya dobara login karein."
        )
        return None

    wait_msg = await bot.send_message(
        chat_id=chat_id, text="Portal se live fleet data fetch ho raha hai..."
    )
    devs, err = await asyncio.to_thread(parse_devices_page_and_ws, session)
    if not devs or err != "OK":
        sess_data["fail_streak"] = sess_data.get("fail_streak", 0) + 1
        await safe_edit_html(wait_msg, f"Fetch error: <code>{esc(err)}</code>")
        return None

    sess_data["fail_streak"] = 0
    analysis = analyze_devices(devs)
    LAST_SNAPSHOT[chat_id] = analysis
    save_snapshot_db(chat_id, analysis)
    try:
        await wait_msg.delete()
    except Exception:
        pass
    return analysis


async def _force_fresh_snapshot(
    chat_id: int, sess_data: dict[str, Any], bot
) -> dict[str, Any] | None:
    session = ensure_live_session(chat_id, sess_data)
    if not session:
        return None
    devs, err = await asyncio.to_thread(parse_devices_page_and_ws, session)
    if not devs or err != "OK":
        return LAST_SNAPSHOT.get(chat_id)
    analysis = analyze_devices(devs)
    PREV_SNAPSHOT[chat_id] = LAST_SNAPSHOT.get(chat_id) or {}
    LAST_SNAPSHOT[chat_id] = analysis
    save_snapshot_db(chat_id, analysis)
    return analysis


async def _render_paginated_state(
    chat_id: int, user_id: int | None, sess_data: dict[str, Any], bot, delta: int = 0
) -> None:
    st = LAST_PAGE_STATE.get(chat_id) or {"kind": "all_sites", "page": 1}
    kind = st.get("kind", "all_sites")
    page = max(1, int(st.get("page", 1)) + delta)

    analysis = await _refresh_or_get_snapshot(chat_id, sess_data, bot)
    if not analysis:
        return

    session = sess_data.get("session")

    if kind == "all_sites":
        txt, page, total_p = build_all_sites_page(analysis, page=page, page_size=5)
    elif kind == "online":
        txt, page, total_p = build_filtered_devices_page(
            analysis.get("online_list") or [],
            "ONLINE SITES",
            analysis["checked_at_ist"],
            page=page,
            page_size=5,
        )
    elif kind == "unassigned":
        txt, page, total_p = build_filtered_devices_page(
            analysis.get("unassigned_list") or [],
            "UNASSIGNED DEVICES",
            analysis["checked_at_ist"],
            page=page,
            page_size=5,
        )
    elif kind == "stale":
        txt, page, total_p = build_filtered_devices_page(
            analysis.get("stale_list") or [],
            "STALE SITES (>30m)",
            analysis["checked_at_ist"],
            page=page,
            page_size=5,
        )
    elif kind == "bucket":
        b_name = st.get("bucket", "<1D")
        b_list = (analysis.get("buckets") or {}).get(b_name) or []
        txt, page, total_p = build_filtered_devices_page(
            b_list,
            f"AGING {b_name}",
            analysis["checked_at_ist"],
            page=page,
            page_size=5,
        )
    elif kind == "instruments":
        if not session:
            return
        msg_w = await bot.send_message(
            chat_id=chat_id,
            text=f"Instruments & tanks load ho rahe hain (Page {page})...",
        )
        txt, page, total_p = await asyncio.to_thread(
            build_instruments_page, session, analysis, page=page, page_size=5
        )
        try:
            await msg_w.delete()
        except Exception:
            pass
    elif kind == "dashboard":
        devs = analysis.get("assigned_devices") or []
        if not devs:
            txt = "Koi assigned site nahi mili."
            page, total_p = 1, 1
        else:
            total_p = len(devs)
            page = max(1, min(page, total_p))
            dev_obj = devs[page - 1]
            msg_w = await bot.send_message(
                chat_id=chat_id,
                text=f"Site dashboard fetch ho raha hai (#{page}/{total_p})...",
            )
            txt = await asyncio.to_thread(
                build_site_dashboard_card,
                session,
                dev_obj,
                index=page,
                chat_id=chat_id,
            )
            try:
                await msg_w.delete()
            except Exception:
                pass
    else:
        txt, page, total_p = build_all_sites_page(analysis, page=1, page_size=5)

    st["page"] = page
    st["total_pages"] = total_p
    LAST_PAGE_STATE[chat_id] = st

    kb = (
        dashboard_sub_keyboard(chat_id)
        if kind == "dashboard"
        else paginated_list_keyboard(user_id)
    )
    await safe_send_html(bot, chat_id, txt, reply_markup=kb)


async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else None
    text = update.message.text.strip()
    low = text.lower()

    gate_err = _check_bot_gate(user_id)
    if gate_err:
        await safe_send_html(context.bot, chat_id, esc(gate_err))
        return

    # Universal Main Menu & Back shortcuts
    if text in ("🏠 Main Menu", "Main Menu", "/menu"):
        AWAITING_INPUT.pop(chat_id, None)
        GAME_SESSIONS.pop(chat_id, None)
        AI_CHAT_MODE.discard(chat_id)
        if require_auth(chat_id):
            MENU_STATE[chat_id] = "main"
            MENU_STACK[chat_id] = ["main"]
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('MAIN MENU'))}",
                reply_markup=main_reply_keyboard(user_id),
            )
        else:
            MENU_STATE[chat_id] = "guest"
            MENU_STACK[chat_id] = ["guest"]
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('GUEST PORTAL'))}",
                reply_markup=guest_reply_keyboard(),
            )
        return

    if text in ("⬅ Back", "Back", "🔙 Back"):
        await pop_menu_state(chat_id, user_id, context.bot)
        return

    # Awaiting user input flow
    awaiting = AWAITING_INPUT.get(chat_id)
    if awaiting:
        action = awaiting.get("action")
        AWAITING_INPUT.pop(chat_id, None)

        if action == "login_user":
            USER_SESSIONS[chat_id] = {
                "step": "awaiting_password",
                "username": text,
            }
            AWAITING_INPUT[chat_id] = {"action": "login_pass"}
            await safe_send_html(
                context.bot,
                chat_id,
                "Ab apna EnviroFRONT <b>Password</b> bhejein:",
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return

        if action == "login_pass":
            sess_tmp = USER_SESSIONS.get(chat_id) or {}
            uname = str(sess_tmp.get("username") or "").strip()
            pword = text
            wait_m = await context.bot.send_message(
                chat_id=chat_id, text="EnviroFRONT portal mein login ho raha hai..."
            )
            sess_obj, err = await asyncio.to_thread(
                login_envirofront, uname, pword
            )
            if not sess_obj:
                USER_SESSIONS[chat_id] = {"step": "guest"}
                await safe_edit_html(
                    wait_m,
                    f"Login fail: <code>{esc(err)}</code>\nDobara <b>🔐 Login</b> dabayein.",
                )
                return

            devs, d_err = await asyncio.to_thread(
                parse_devices_page_and_ws, sess_obj
            )
            USER_SESSIONS[chat_id] = {
                "step": "authenticated",
                "username": uname,
                "password": pword,
                "session": sess_obj,
                "fail_streak": 0,
            }
            MENU_STATE[chat_id] = "main"
            MENU_STACK[chat_id] = ["main"]

            if devs and d_err == "OK":
                analysis = analyze_devices(devs)
                LAST_SNAPSHOT[chat_id] = analysis
                save_snapshot_db(chat_id, analysis)
                try:
                    await wait_m.delete()
                except Exception:
                    pass
                await safe_send_html(
                    context.bot,
                    chat_id,
                    build_status_message(analysis),
                    reply_markup=main_reply_keyboard(user_id),
                )
            else:
                await safe_edit_html(
                    wait_m,
                    f"Login ho gaya, par initial fleet fetch note: <code>{esc(d_err)}</code>",
                )
                await safe_send_html(
                    context.bot,
                    chat_id,
                    f"{themed_box(sc('MAIN MENU'))}",
                    reply_markup=main_reply_keyboard(user_id),
                )
            return

        sess_data = require_auth(chat_id)
        if not sess_data:
            await safe_send_html(
                context.bot,
                chat_id,
                "Session expired. Kripya login karein.",
                reply_markup=guest_reply_keyboard(),
            )
            return

        session = ensure_live_session(chat_id, sess_data)
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )

        if action == "open_site_query":
            devs = (analysis or {}).get("assigned_devices") or []
            matched_idx = None
            matched_dev = None
            for idx, d in enumerate(devs, start=1):
                hay = f"{d.get('site_id','')} {d.get('serial_no','')} {d.get('industry','')} {d.get('site','')}".lower()
                if text.lower() in hay:
                    matched_idx = idx
                    matched_dev = d
                    break
            if matched_dev and session:
                card = await asyncio.to_thread(
                    build_site_dashboard_card,
                    session,
                    matched_dev,
                    index=matched_idx,
                    chat_id=chat_id,
                )
                await safe_send_html(
                    context.bot,
                    chat_id,
                    card,
                    reply_markup=dashboard_sub_keyboard(chat_id),
                )
            else:
                await safe_send_html(
                    context.bot,
                    chat_id,
                    f"Koi site match nahi hui: <code>{esc(text)}</code>",
                    reply_markup=dashboard_sub_keyboard(chat_id),
                )
            return

        if action == "pin_site_query":
            added, lst = toggle_watchlist_site(chat_id, text)
            state_txt = "pinned to Watchlist" if added else "removed from Watchlist"
            await safe_send_html(
                context.bot,
                chat_id,
                f"<code>{esc(text)}</code> {state_txt}. Total pinned: <b>{len(lst)}</b>",
                reply_markup=dashboard_sub_keyboard(chat_id),
            )
            return

        if action == "custom_date_report":
            devs = (analysis or {}).get("assigned_devices") or []
            target = next(
                (d for d in devs if str(d.get("site_id") or "").strip().isdigit()),
                None,
            )
            if target and session:
                card = await asyncio.to_thread(
                    build_api_report_card,
                    session,
                    target,
                    report_date=text,
                )
                await safe_send_html(
                    context.bot,
                    chat_id,
                    card,
                    reply_markup=api_report_sub_keyboard(),
                )
            else:
                await safe_send_html(
                    context.bot,
                    chat_id,
                    "Site ID available nahi hai.",
                    reply_markup=api_report_sub_keyboard(),
                )
            return

        if action == "site_api_report":
            devs = (analysis or {}).get("assigned_devices") or []
            target = next(
                (
                    d
                    for d in devs
                    if text.lower()
                    in f"{d.get('site_id','')} {d.get('industry','')} {d.get('serial_no','')}".lower()
                ),
                None,
            )
            if target and session:
                card = await asyncio.to_thread(
                    build_api_report_card,
                    session,
                    target,
                    report_date=today_ist_report_date(),
                )
                await safe_send_html(
                    context.bot,
                    chat_id,
                    card,
                    reply_markup=api_report_sub_keyboard(),
                )
            else:
                await safe_send_html(
                    context.bot,
                    chat_id,
                    f"Site nahi mili: <code>{esc(text)}</code>",
                    reply_markup=api_report_sub_keyboard(),
                )
            return

        if action == "ai_ask":
            ans = await asyncio.to_thread(
                answer_groq_query, text, analysis or {}
            )
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc(CLIENT_NAME + ' — GROQ AI'))}\n{get_theme()['bar']}\n{ans}",
                reply_markup=main_reply_keyboard(user_id),
            )
            return

        if action == "admin_sync_sec" and is_admin(user_id):
            global REFRESH_INTERVAL_SEC
            try:
                sec = int(text)
                if 15 <= sec <= 3600:
                    REFRESH_INTERVAL_SEC = sec
                    save_setting_db("refresh_sec", str(sec))
                    await safe_send_html(
                        context.bot,
                        chat_id,
                        f"Poll interval set to <b>{sec}s</b>.",
                        reply_markup=admin_sub_keyboard(),
                    )
                    return
            except ValueError:
                pass
            await safe_send_html(
                context.bot,
                chat_id,
                "15 se 3600 ke beech valid seconds bhejein.",
                reply_markup=admin_sub_keyboard(),
            )
            return

        if action == "admin_maint_msg" and is_admin(user_id):
            global MAINTENANCE_MSG
            MAINTENANCE_MSG = text[:240]
            save_setting_db("maint_msg", MAINTENANCE_MSG)
            await safe_send_html(
                context.bot,
                chat_id,
                f"Maintenance message updated: <code>{esc(MAINTENANCE_MSG)}</code>",
                reply_markup=admin_sub_keyboard(),
            )
            return

    # Guest & Games Lounge flow
    if not require_auth(chat_id):
        g_sess = GAME_SESSIONS.get(chat_id)
        if g_sess:
            g_type = g_sess.get("game")
            if g_type == "ttt" and text in ("1", "2", "3", "4", "5", "6", "7", "8", "9"):
                out, _ = play_ttt_turn(chat_id, int(text))
                await safe_send_html(
                    context.bot,
                    chat_id,
                    out,
                    reply_markup=guest_keyboard_for_chat(chat_id),
                )
                return
            if g_type == "guess" and text.isdigit():
                out, _ = play_guess_turn(chat_id, int(text))
                await safe_send_html(
                    context.bot,
                    chat_id,
                    out,
                    reply_markup=guest_keyboard_for_chat(chat_id),
                )
                return
            if g_type == "rps" and any(
                w in low for w in ("rock", "paper", "scissor")
            ):
                out, _ = play_rps_turn(chat_id, text)
                await safe_send_html(
                    context.bot,
                    chat_id,
                    out,
                    reply_markup=guest_keyboard_for_chat(chat_id),
                )
                return

        if text == "🔐 Login":
            await cmd_login(update, context)
            return
        if text == "📖 Manual":
            await cmd_manual(update, context)
            return
        if text == "🎮 Games Lounge":
            push_menu_state(chat_id, "games_menu")
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('GAMES LOUNGE'))}\n  ▸ Koi mini-game chuniye:",
                reply_markup=games_lounge_keyboard(),
            )
            return
        if text == "🎲 Tic-Tac-Toe":
            out = start_ttt_game(chat_id)
            await safe_send_html(
                context.bot,
                chat_id,
                out,
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        if text == "🔢 Number Guess":
            out = start_guess_game(chat_id)
            await safe_send_html(
                context.bot,
                chat_id,
                out,
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return
        if text == "✂ Rock Paper Scissors":
            out = start_rps_game(chat_id)
            await safe_send_html(
                context.bot,
                chat_id,
                out,
                reply_markup=guest_keyboard_for_chat(chat_id),
            )
            return

        await safe_send_html(
            context.bot,
            chat_id,
            "Kripya <b>🔐 Login</b> dabayein ya <b>🎮 Games Lounge</b> chuniye.",
            reply_markup=guest_keyboard_for_chat(chat_id),
        )
        return

    # Authenticated flow
    sess_data = require_auth(chat_id)
    assert sess_data is not None

    if text in ("Next ➡", "Next", "/next"):
        if MENU_STATE.get(chat_id) == "paginated_list":
            await _render_paginated_state(
                chat_id, user_id, sess_data, context.bot, delta=1
            )
        else:
            push_menu_state(chat_id, "paginated_list")
            LAST_PAGE_STATE[chat_id] = {"kind": "all_sites", "page": 2}
            await _render_paginated_state(
                chat_id, user_id, sess_data, context.bot, delta=0
            )
        return

    if text == "🚪 Logout":
        await cmd_logout(update, context)
        return

    if text == "📖 Manual":
        await cmd_manual(update, context)
        return

    if text == "◈ Status":
        fresh = await _force_fresh_snapshot(chat_id, sess_data, context.bot)
        if fresh:
            await safe_send_html(
                context.bot,
                chat_id,
                build_status_message(fresh),
                reply_markup=main_reply_keyboard(user_id),
            )
        return

    if text == "◈ All Sites":
        push_menu_state(chat_id, "paginated_list")
        LAST_PAGE_STATE[chat_id] = {"kind": "all_sites", "page": 1}
        await _render_paginated_state(
            chat_id, user_id, sess_data, context.bot, delta=0
        )
        return

    if text == "🌐 Site Dashboard":
        push_menu_state(chat_id, "dash_menu")
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        devs = (analysis or {}).get("assigned_devices") or []
        if devs and sess_data.get("session"):
            card = await asyncio.to_thread(
                build_site_dashboard_card,
                sess_data["session"],
                devs[0],
                index=1,
                chat_id=chat_id,
            )
            await safe_send_html(
                context.bot,
                chat_id,
                card,
                reply_markup=dashboard_sub_keyboard(chat_id),
            )
        else:
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('SITE DASHBOARD HUB'))}",
                reply_markup=dashboard_sub_keyboard(chat_id),
            )
        return

    m_quick = re.match(r"^#(\d+)\s+", text)
    if m_quick and MENU_STATE.get(chat_id) == "dash_menu":
        idx = int(m_quick.group(1))
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        devs = (analysis or {}).get("assigned_devices") or []
        if 1 <= idx <= len(devs) and sess_data.get("session"):
            card = await asyncio.to_thread(
                build_site_dashboard_card,
                sess_data["session"],
                devs[idx - 1],
                index=idx,
                chat_id=chat_id,
            )
            await safe_send_html(
                context.bot,
                chat_id,
                card,
                reply_markup=dashboard_sub_keyboard(chat_id),
            )
        return

    if text == "🔎 Open Site by ID/Name":
        AWAITING_INPUT[chat_id] = {"action": "open_site_query"}
        await safe_send_html(
            context.bot,
            chat_id,
            "Site ID, Serial No, ya Site/Industry ka naam bhejein:",
            reply_markup=dashboard_sub_keyboard(chat_id),
        )
        return

    if text == "⭐ Pin Site":
        AWAITING_INPUT[chat_id] = {"action": "pin_site_query"}
        await safe_send_html(
            context.bot,
            chat_id,
            "Pin/Unpin karne ke liye Site ID, Serial, ya Site Name bhejein:",
            reply_markup=dashboard_sub_keyboard(chat_id),
        )
        return

    if text == "📡 API Report":
        push_menu_state(chat_id, "api_menu")
        await safe_send_html(
            context.bot,
            chat_id,
            f"{themed_box(sc('EXCISE API REPORT HUB'))}\n  ▸ Report option chuniye:",
            reply_markup=api_report_sub_keyboard(),
        )
        return

    if text == "📅 Today API Report":
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        devs = (analysis or {}).get("assigned_devices") or []
        target = next(
            (d for d in devs if str(d.get("site_id") or "").strip().isdigit()),
            None,
        )
        if target and sess_data.get("session"):
            card = await asyncio.to_thread(
                build_api_report_card,
                sess_data["session"],
                target,
                report_date=today_ist_report_date(),
            )
            await safe_send_html(
                context.bot,
                chat_id,
                card,
                reply_markup=api_report_sub_keyboard(),
            )
        return

    if text == "🗓 Custom Date Report":
        AWAITING_INPUT[chat_id] = {"action": "custom_date_report"}
        await safe_send_html(
            context.bot,
            chat_id,
            f"Date bhejein (e.g. <code>{esc(today_ist_report_date())}</code>):",
            reply_markup=api_report_sub_keyboard(),
        )
        return

    if text == "🏭 Site API Report":
        AWAITING_INPUT[chat_id] = {"action": "site_api_report"}
        await safe_send_html(
            context.bot,
            chat_id,
            "Site ID ya Site Name bhejein:",
            reply_markup=api_report_sub_keyboard(),
        )
        return

    if text == "📐 Instruments":
        push_menu_state(chat_id, "paginated_list")
        LAST_PAGE_STATE[chat_id] = {"kind": "instruments", "page": 1}
        await _render_paginated_state(
            chat_id, user_id, sess_data, context.bot, delta=0
        )
        return

    if text == "⏳ Aging":
        push_menu_state(chat_id, "age_menu")
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        bc = (analysis or {}).get("bucket_counts") or {}
        await safe_send_html(
            context.bot,
            chat_id,
            f"{themed_box(sc('AGING BUCKETS'))}\n"
            f"  ▸ &lt;1D: <b>{bc.get('<1D',0)}</b> | &lt;1W: <b>{bc.get('<1W',0)}</b> | &lt;24D: <b>{bc.get('<24D',0)}</b>\n"
            f"  ▸ &lt;11M: <b>{bc.get('<11M',0)}</b> | &lt;1Y: <b>{bc.get('<1Y',0)}</b> | &gt;1Y: <b>{bc.get('>1Y',0)}</b>",
            reply_markup=aging_sub_keyboard(),
        )
        return

    bucket_map = {
        "◦ <1D": "<1D",
        "▪ <1W": "<1W",
        "• <24D": "<24D",
        "◆ <11M": "<11M",
        "◈ <1Y": "<1Y",
        "▸ >1Y": ">1Y",
    }
    if text in bucket_map:
        push_menu_state(chat_id, "paginated_list")
        LAST_PAGE_STATE[chat_id] = {
            "kind": "bucket",
            "bucket": bucket_map[text],
            "page": 1,
        }
        await _render_paginated_state(
            chat_id, user_id, sess_data, context.bot, delta=0
        )
        return

    if text == "⭐ Watchlist":
        w_list = WATCHLIST_SITES.get(chat_id) or []
        if not w_list:
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('WATCHLIST'))}\n  • Watchlist abhi khali hai. Site Dashboard se <b>⭐ Pin Site</b> dabayein.",
                reply_markup=main_reply_keyboard(user_id),
            )
            return
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        all_d = (analysis or {}).get("all_devices") or []
        matched = [
            d
            for d in all_d
            if any(
                wk.lower()
                in f"{d.get('site_id','')} {d.get('serial_no','')} {d.get('industry','')}".lower()
                for wk in w_list
            )
        ]
        blocks = [
            f"{themed_box(sc('WATCHLIST SITES'), f'Pinned: {len(w_list)}')}"
        ]
        for idx, d in enumerate(matched[:10], start=1):
            blocks.append(format_premium_device_block(d, index=idx))
        if not matched:
            blocks.append(
                f"  • Pinned keys: <code>{esc(', '.join(w_list))}</code>"
            )
        await safe_send_html(
            context.bot,
            chat_id,
            "\n\n".join(blocks),
            reply_markup=main_reply_keyboard(user_id),
        )
        return

    if text == "📋 Shift Summary":
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        if analysis:
            await safe_send_html(
                context.bot,
                chat_id,
                build_shift_handover_message(analysis, chat_id=chat_id),
                reply_markup=main_reply_keyboard(user_id),
            )
        return

    if text == "🤖 AI Suite":
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        brief_html = await asyncio.to_thread(
            generate_ai_brief_text, analysis or {}
        )
        AWAITING_INPUT[chat_id] = {"action": "ai_ask"}
        await safe_send_html(
            context.bot,
            chat_id,
            f"{themed_box(sc(CLIENT_NAME + ' — AI BRIEF'))}\n{get_theme()['bar']}\n{brief_html}\n\n"
            "  ▸ Koi sawaal poochhna ho to abhi type karein:",
            reply_markup=main_reply_keyboard(user_id),
        )
        return

    if text == "⚙ Ops & Export":
        push_menu_state(chat_id, "ops_menu")
        await safe_send_html(
            context.bot,
            chat_id,
            f"{themed_box(sc('OPS & EXPORT'))}",
            reply_markup=ops_sub_keyboard(),
        )
        return

    if text == "🩺 Health":
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        await safe_send_html(
            context.bot,
            chat_id,
            build_health_report(analysis or {}),
            reply_markup=ops_sub_keyboard(),
        )
        return

    if text == "📥 Export CSV":
        analysis = await _refresh_or_get_snapshot(
            chat_id, sess_data, context.bot
        )
        csv_io = build_fleet_csv_bytes(analysis or {})
        await context.bot.send_document(
            chat_id=chat_id,
            document=csv_io,
            caption=f"{CLIENT_NAME} Fleet CSV Export",
            reply_markup=ops_sub_keyboard(),
        )
        return

    if text == "∙ Online":
        push_menu_state(chat_id, "paginated_list")
        LAST_PAGE_STATE[chat_id] = {"kind": "online", "page": 1}
        await _render_paginated_state(
            chat_id, user_id, sess_data, context.bot, delta=0
        )
        return

    if text == "📌 Unassigned":
        push_menu_state(chat_id, "paginated_list")
        LAST_PAGE_STATE[chat_id] = {"kind": "unassigned", "page": 1}
        await _render_paginated_state(
            chat_id, user_id, sess_data, context.bot, delta=0
        )
        return

    if text == "❄ Stale Sites":
        push_menu_state(chat_id, "paginated_list")
        LAST_PAGE_STATE[chat_id] = {"kind": "stale", "page": 1}
        await _render_paginated_state(
            chat_id, user_id, sess_data, context.bot, delta=0
        )
        return

    # Admin Panel & Sub-menus
    if is_admin(user_id):
        global BOT_ENABLED, MAINTENANCE_MODE, NOTIF_STYLE, ACTIVE_THEME

        if text == "🛡 Admin Panel":
            push_menu_state(chat_id, "admin_menu")
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('ADMIN AUTHORITY PANEL'))}",
                reply_markup=admin_sub_keyboard(),
            )
            return

        if text == "🎨 Themes":
            push_menu_state(chat_id, "theme_menu")
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('UI THEMES'), 'Current: ' + get_theme()['label'])}",
                reply_markup=theme_sub_keyboard(),
            )
            return

        theme_btn_map = {
            "Theme: Obsidian": "obsidian",
            "Theme: Minimal": "minimal",
            "Theme: Cyber": "cyber",
            "Theme: Royal": "royal",
        }
        if text in theme_btn_map:
            ACTIVE_THEME = theme_btn_map[text]
            save_setting_db("active_theme", ACTIVE_THEME)
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('THEME APPLIED'), get_theme()['label'])}",
                reply_markup=theme_sub_keyboard(),
            )
            return

        if text == "🔔 Notif & Demo":
            push_menu_state(chat_id, "notif_menu")
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('NOTIFICATIONS & DEMO'), 'Mode: ' + NOTIF_STYLE)}",
                reply_markup=notif_demo_sub_keyboard(),
            )
            return

        mode_btn_map = {
            "Mode: Detailed": "detailed",
            "Mode: Compact": "compact",
            "Mode: Alerts Only": "alerts_only",
            "Mode: Digest": "digest",
        }
        if text in mode_btn_map:
            NOTIF_STYLE = mode_btn_map[text]
            save_setting_db("notif_style", NOTIF_STYLE)
            await safe_send_html(
                context.bot,
                chat_id,
                f"Notification mode set to <b>{esc(NOTIF_STYLE)}</b>.",
                reply_markup=notif_demo_sub_keyboard(),
            )
            return

        if text == "Demo: 2h Down":
            await run_demo_alert(context.bot, chat_id, "2h")
            return
        if text == "Demo: 10h Down":
            await run_demo_alert(context.bot, chat_id, "10h")
            return
        if text == "Demo: 1d Down":
            await run_demo_alert(context.bot, chat_id, "1d")
            return
        if text == "Demo: Recovered":
            await run_demo_alert(context.bot, chat_id, "recovered")
            return

        if text == "⏱ Sync Interval":
            AWAITING_INPUT[chat_id] = {"action": "admin_sync_sec"}
            await safe_send_html(
                context.bot,
                chat_id,
                f"Current interval: <b>{REFRESH_INTERVAL_SEC}s</b>. Naya interval (15–3600s) bhejein:",
                reply_markup=admin_sub_keyboard(),
            )
            return

        if text == "🛠 Maintenance":
            MAINTENANCE_MODE = not MAINTENANCE_MODE
            save_setting_db("maint_mode", "1" if MAINTENANCE_MODE else "0")
            AWAITING_INPUT[chat_id] = {"action": "admin_maint_msg"}
            await safe_send_html(
                context.bot,
                chat_id,
                f"Maintenance mode: <b>{'ON' if MAINTENANCE_MODE else 'OFF'}</b>.\n"
                "Naya maintenance message bhejein (ya <b>⬅ Back</b> dabayein):",
                reply_markup=admin_sub_keyboard(),
            )
            return

        if text == "⚡ Bot ON/OFF":
            BOT_ENABLED = not BOT_ENABLED
            save_setting_db("bot_enabled", "1" if BOT_ENABLED else "0")
            await safe_send_html(
                context.bot,
                chat_id,
                f"Bot global state: <b>{'ENABLED (ON)' if BOT_ENABLED else 'DISABLED (OFF)'}</b>.",
                reply_markup=admin_sub_keyboard(),
            )
            return

        if text == "🧹 Clear Alerts":
            clear_all_alerts_for_chat(chat_id)
            DIGEST_QUEUE.pop(chat_id, None)
            await safe_send_html(
                context.bot,
                chat_id,
                "Sabhi sent stage alerts aur digest queue clear kar diye gaye hain.",
                reply_markup=admin_sub_keyboard(),
            )
            return

        if text == "🤖 AI Patch & Logs":
            push_menu_state(chat_id, "patch_menu")
            err_src = LAST_ERROR_LOG.get("source") or "None"
            err_msg = LAST_ERROR_LOG.get("error") or "No error logged"
            patch_sum = PENDING_AI_PATCH.get("summary") or "None"
            await safe_send_html(
                context.bot,
                chat_id,
                f"{themed_box(sc('AI PATCH & ERROR LOGS'))}\n"
                f"  ▸ Last Error : <code>{esc(err_src)}</code> — {esc(err_msg)}\n"
                f"  ▸ Pending Fix: <code>{esc(patch_sum)}</code>",
                reply_markup=ai_patch_sub_keyboard(),
            )
            return

        if text == "🛠 Generate AI Fix":
            ok, info = await asyncio.to_thread(prepare_ai_error_patch)
            await safe_send_html(
                context.bot,
                chat_id,
                f"  ▸ {'AI Fix Ready' if ok else 'Fix Note'}: <code>{esc(info)}</code>",
                reply_markup=ai_patch_sub_keyboard(),
            )
            return

        if text == "✅ Apply AI Patch":
            ok, info = await asyncio.to_thread(apply_pending_ai_patch)
            await safe_send_html(
                context.bot,
                chat_id,
                f"  ▸ {'Applied' if ok else 'Blocked'}: <code>{esc(info)}</code>",
                reply_markup=ai_patch_sub_keyboard(),
            )
            return

        if text == "📋 Error & Patch Logs":
            lines = [f"{themed_box(sc('ERROR & PATCH AUDIT'))}"]
            if LAST_ERROR_LOG:
                lines.append(
                    f"  ▸ Last Err ({esc(LAST_ERROR_LOG.get('time',''))}): "
                    f"<code>{esc(LAST_ERROR_LOG.get('source',''))}</code> — {esc(LAST_ERROR_LOG.get('error',''))}"
                )
            else:
                lines.append("  ▸ Koi error log nahi hai.")
            for rec in reversed(PATCH_HISTORY[-6:]):
                lines.append(
                    f"  • [{esc(rec.get('action',''))}] {esc(rec.get('summary',''))} "
                    f"(<code>{esc(os.path.basename(rec.get('backup','')))}</code>)"
                )
            await safe_send_html(
                context.bot,
                chat_id,
                "\n".join(lines),
                reply_markup=ai_patch_sub_keyboard(),
            )
            return

        if text == "↩ Rollback Backup":
            ok, info = await asyncio.to_thread(rollback_latest_backup)
            await safe_send_html(
                context.bot,
                chat_id,
                f"  ▸ {esc(info)}",
                reply_markup=admin_sub_keyboard(),
            )
            return

    await safe_send_html(
        context.bot,
        chat_id,
        "Kripya menu button ka use karein ya <b>🏠 Main Menu</b> dabayein.",
        reply_markup=current_keyboard_for_chat(chat_id, user_id),
    )


async def post_init(application) -> None:
    init_db()
    load_persisted_state()
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start / Main Menu"),
            BotCommand("login", "Login to EnviroFRONT"),
            BotCommand("logout", "Logout session"),
            BotCommand("manual", "Operator Manual"),
            BotCommand("menu", "Open Main Menu"),
            BotCommand("next", "Next Page"),
        ]
    )
    application.create_task(background_poll_loop(application))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable set nahi hai.")

    init_db()
    load_persisted_state()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("manual", cmd_manual))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("%s Ultra Telemetry Bot starting...", CLIENT_NAME)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
