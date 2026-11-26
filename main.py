import asyncio
import csv
import io
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import os
from dotenv import load_dotenv

load_dotenv()
GROQ_API_KEY = os.getenv("API")
import requests
from bs4 import BeautifulSoup
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==============================================================================
# CONFIGURATION — CLIENT: BAGGI JAAT
# ==============================================================================
CLIENT_NAME = "BAGGI JAAT"
BOT_TOKEN = "9018573596:AAE74Vb8hbWyxNQMARkTxYTptCo9a_E11sE"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
BASE_URL = "https://envirofront.m2mlogger.com"
LOGIN_URL = f"{BASE_URL}/?ReturnUrl=%2Fpae%2FControlPanel%2FDevices"
DEVICES_URL = f"{BASE_URL}/pae/ControlPanel/Devices"
WS_DEVICES_URL = f"{BASE_URL}/Services/EF/WS/Devices"

DB_PATH = "baggi_jaat_enterprise.db"
IST = timezone(timedelta(hours=5, minutes=30))
CHECK_INTERVAL_SECONDS = 60  # 1-minute auto-refresh

USER_SESSIONS: dict[int, dict[str, Any]] = {}
SENT_STAGE_ALERTS: set[str] = set()  # "chat_id:serial:stage" -> strictly one-time
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}
AI_CHAT_MODE: set[int] = set()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BaggiJaat.Enterprise")


# ==============================================================================
# STYLISH UNICODE TEXT & COLOR BADGE FORMATTERS
# ==============================================================================
_SMALL_CAPS_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
)


def sc(text: str) -> str:
    return text.translate(_SMALL_CAPS_MAP)


def esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def color_badge_for_hours(hours: float | None, is_connected: bool) -> str:
    if is_connected:
        return "🟢"
    if hours is None:
        return "🔴"
    if hours < 24:
        return "🟡"
    if hours < 24 * 7:
        return "🟠"
    if hours < 24 * 30:
        return "🔴"
    return "🟣"


# ==============================================================================
# SQLITE PERSISTENCE (ONE-TIME STAGE ALERTS + SNAPSHOTS)
# ==============================================================================
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            chat_id INTEGER PRIMARY KEY,
            checked_at_ist TEXT,
            payload_json TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS stage_alerts (
            stage_key TEXT PRIMARY KEY,
            created_ts REAL
        )
        """
    )
    conn.commit()
    conn.close()


def load_stage_alerts_db() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT stage_key FROM stage_alerts")
        for (k,) in cur.fetchall():
            SENT_STAGE_ALERTS.add(str(k))
        conn.close()
    except Exception as exc:
        logger.warning("Failed loading stage_alerts: %s", exc)


def mark_stage_alert_sent(stage_key: str) -> None:
    SENT_STAGE_ALERTS.add(stage_key)
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO stage_alerts (stage_key, created_ts) VALUES (?, ?)",
            (stage_key, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Failed saving stage_alert: %s", exc)


def clear_stage_alerts_for_device(chat_id: int, serial_no: str) -> None:
    prefix = f"{chat_id}:{serial_no}:"
    to_remove = [k for k in SENT_STAGE_ALERTS if k.startswith(prefix)]
    for k in to_remove:
        SENT_STAGE_ALERTS.discard(k)
    if to_remove:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("DELETE FROM stage_alerts WHERE stage_key LIKE ?", (f"{prefix}%",))
            conn.commit()
            conn.close()
        except Exception:
            pass


def clear_all_alerts_for_chat(chat_id: int) -> None:
    prefix = f"{chat_id}:"
    for k in [k for k in SENT_STAGE_ALERTS if k.startswith(prefix)]:
        SENT_STAGE_ALERTS.discard(k)
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM stage_alerts WHERE stage_key LIKE ?", (f"{prefix}%",))
        conn.commit()
        conn.close()
    except Exception:
        pass


def save_snapshot_db(chat_id: int, analysis: dict[str, Any]) -> None:
    try:
        serializable = {
            "checked_at_ist": analysis.get("checked_at_ist"),
            "total": analysis.get("total", 0),
            "assigned_count": analysis.get("assigned_count", 0),
            "unassigned_count": analysis.get("unassigned_count", 0),
            "connected": analysis.get("connected", 0),
            "disconnected_count": analysis.get("disconnected_count", 0),
            "aging_counts": analysis.get("aging_counts", {}),
            "top_offline_sites": analysis.get("top_offline_sites", []),
            "firmware_breakdown": analysis.get("firmware_breakdown", {}),
            "device_type_breakdown": analysis.get("device_type_breakdown", {}),
        }
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO snapshots (chat_id, checked_at_ist, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                checked_at_ist = excluded.checked_at_ist,
                payload_json = excluded.payload_json
            """,
            (chat_id, analysis.get("checked_at_ist", ""), json.dumps(serializable)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ==============================================================================
# KEYBOARDS (MAIN + AGING SUB-MENU + INTERACTIVE SITES LIST)
# ==============================================================================
def main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 ꜱᴛᴀᴛᴜꜱ", callback_data="act:status"),
                InlineKeyboardButton("🌐 ᴀʟʟ ꜱɪᴛᴇꜱ (ᴅᴀꜱʜ)", callback_data="sites:0"),
            ],
            [
                InlineKeyboardButton("🟢 ᴏɴʟɪɴᴇ", callback_data="act:online"),
                InlineKeyboardButton("⏳ ᴀɢɪɴɢ (< ʀᴀɴɢᴇ)", callback_data="act:aging_menu"),
            ],
            [
                InlineKeyboardButton("📌 ᴜɴᴀꜱꜱɪɢɴᴇᴅ ɴ/ᴀ", callback_data="act:unassigned"),
                InlineKeyboardButton("🩺 ʜᴇᴀʟᴛʜ", callback_data="act:health"),
            ],
            [
                InlineKeyboardButton("🤖 ɢʀᴏǫ ᴀɪ ᴄʜᴀᴛ", callback_data="act:ai_mode"),
                InlineKeyboardButton("🧠 ɢʀᴏǫ ᴀɪ ʀᴇᴘᴏʀᴛ", callback_data="act:ai_analyze"),
            ],
            [
                InlineKeyboardButton("📥 ᴇxᴘᴏʀᴛ ᴄꜱᴠ", callback_data="act:export_csv"),
                InlineKeyboardButton("🚪 ʟᴏɢᴏᴜᴛ", callback_data="act:logout"),
            ],
        ]
    )


def aging_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟡 < 1 ᴅᴀʏ (≤24ʜ)", callback_data="age:1d"),
                InlineKeyboardButton("🟠 < 1 ᴡᴇᴇᴋ (≤7ᴅ)", callback_data="age:1w"),
            ],
            [
                InlineKeyboardButton("🔴 < 24 ᴅᴀʏꜱ (≤24ᴅ)", callback_data="age:24d"),
                InlineKeyboardButton("🟣 < 11 ᴍᴏɴᴛʜꜱ (≤330ᴅ)", callback_data="age:11m"),
            ],
            [
                InlineKeyboardButton("⚫ < 1 ʏᴇᴀʀ (≤365ᴅ)", callback_data="age:1y"),
                InlineKeyboardButton("🚨 > 1 ʏᴇᴀʀ (365ᴅ+)", callback_data="age:gt1y"),
            ],
            [
                InlineKeyboardButton("⬅️ ʙᴀᴄᴋ ᴛᴏ ᴍᴀɪɴ", callback_data="act:main_menu"),
            ],
        ]
    )


def guest_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔐 ʟᴏɢɪɴ ᴇɴᴠɪʀᴏꜰʀᴏɴᴛ", callback_data="act:login")],
        ]
    )


def bottom_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 Status"), KeyboardButton("🌐 All Sites")],
            [KeyboardButton("🟢 Online"), KeyboardButton("⏳ Aging Menus")],
            [KeyboardButton("📌 Unassigned N/A"), KeyboardButton("🩺 Health")],
            [KeyboardButton("🤖 AI Chat"), KeyboardButton("🧠 AI Report")],
            [KeyboardButton("📥 Export CSV"), KeyboardButton("🚪 Logout")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def animate_message(
    msg, frames: list[str], delay: float = 0.25, final_markup=None
) -> None:
    total = len(frames)
    for idx, frame in enumerate(frames):
        is_last = idx == total - 1
        try:
            await msg.edit_text(
                frame,
                parse_mode=ParseMode.HTML,
                reply_markup=final_markup if is_last else None,
            )
            if not is_last:
                await asyncio.sleep(delay)
        except Exception:
            break


# ==============================================================================
# ROBUST LOGIN INFERENCE (MULTI-PATTERN REDIRECT & COOKIE VERIFICATION)
# ==============================================================================
def extract_verification_token(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "__RequestVerificationToken"})
    if inp and inp.get("value"):
        return str(inp["value"])
    match = re.search(
        r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)["\']',
        html,
    )
    return match.group(1) if match else None


def is_authenticated_html(html: str) -> bool:
    low = (html or "").lower()
    if not low:
        return False
    if "forgot password?" in low or 'name="password"' in low or "id=\"password\"" in low:
        return False
    return (
        "controlpanel/devices" in low
        or "logout" in low
        or "log off" in low
        or "devices" in low
    )


def login_envirofront(username: str, password: str) -> tuple[requests.Session | None, str]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
            ),
            "Referer": LOGIN_URL,
            "Origin": BASE_URL,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    try:
        get_resp = session.get(LOGIN_URL, timeout=25)
        get_resp.raise_for_status()
        token = extract_verification_token(get_resp.text)

        payload: dict[str, str] = {
            "UserName": username.strip(),
            "Password": password,
            "RememberMe": "true",
        }
        if token:
            payload["__RequestVerificationToken"] = token

        # Strategy 1: Standard POST with redirect tracking
        post_resp = session.post(
            LOGIN_URL,
            data=payload,
            timeout=25,
            allow_redirects=False,
        )

        cookies_dict = session.cookies.get_dict()
        has_auth_cookie = any(
            "ASPXAUTH" in k.upper() or "AUTH" in k.upper() or "ASP.NET" in k.upper()
            for k in cookies_dict
        )
        loc = post_resp.headers.get("Location", "")

        if post_resp.status_code in (301, 302, 303, 307):
            target = urljoin(BASE_URL, loc or "/pae/ControlPanel/Devices")
            verify = session.get(target, timeout=25, allow_redirects=True)
            if verify.ok and is_authenticated_html(verify.text):
                return session, "OK"

        # Strategy 2: Direct check on Devices URL if cookie was set on 200 OK
        if has_auth_cookie or post_resp.status_code == 200:
            verify2 = session.get(DEVICES_URL, timeout=25, allow_redirects=True)
            if verify2.ok and is_authenticated_html(verify2.text):
                return session, "OK"

        # Strategy 3: Extract validation summary from returned HTML if login failed
        soup = BeautifulSoup(post_resp.text or "", "html.parser")
        err_div = soup.find(class_=re.compile(r"validation-summary-errors|text-danger", re.I))
        if err_div:
            err_txt = " ".join(err_div.get_text(" ", strip=True).split())
            if err_txt:
                return None, err_txt[:180]

        return None, f"Invalid ID/Password ya session reject (HTTP {post_resp.status_code})."
    except requests.Timeout:
        return None, "EnviroFRONT server timeout. Dobara try karein."
    except Exception as exc:
        logger.error("Login inference exception: %s", exc)
        return None, f"Network/Login error: {exc}"


# ==============================================================================
# PARSING, SITE_ID COLLECTION & DASHBOARD SCRAPER (MULTI-STRATEGY IF/ELSE)
# ==============================================================================
def is_unassigned_value(val: str) -> bool:
    v = (val or "").strip().lower()
    return v in ("", "n/a", "na", "none", "null", "unassigned", "-")


def is_unassigned_device(industry: str, site: str) -> bool:
    return is_unassigned_value(industry) and is_unassigned_value(site)


def parse_timestamp(raw: str) -> datetime | None:
    cleaned = " ".join((raw or "").strip().split())
    if not cleaned:
        return None

    ms_match = re.search(r"/Date\((\-?\d+)(?:[+\-]\d{4})?\)/", cleaned)
    if ms_match:
        try:
            ms = int(ms_match.group(1))
            return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        except Exception:
            return None

    formats = (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %I:%M:%S %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def format_display_time(dt_utc: datetime | None, fallback_raw: str) -> str:
    if dt_utc:
        return dt_utc.astimezone(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")
    return (fallback_raw or "N/A").strip() or "N/A"


def format_human_duration(td: timedelta | None) -> str:
    if td is None:
        return "N/A"
    total_sec = max(0, int(td.total_seconds()))
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def format_industry_with_type(industry: str, site_type: str) -> str:
    ind_unassigned = is_unassigned_value(industry)
    type_unassigned = is_unassigned_value(site_type)

    if ind_unassigned and type_unassigned:
        return "<code>SITE ASSIGN NAHI HAI (N/A)</code>"
    if ind_unassigned:
        return f"<code>SITE ASSIGN NAHI HAI ({esc(site_type.strip())})</code>"
    if type_unassigned:
        return f"<code>{esc(industry.strip())}</code>"
    return f"<code>{esc(industry.strip())} ({esc(site_type.strip())})</code>"


def format_status_with_time(is_connected: bool, display_time: str, off_td: timedelta | None = None) -> str:
    if is_connected:
        return f"🟢 <code>ONLINE ({esc(display_time)})</code>"
    dur_txt = f" • {format_human_duration(off_td)}" if off_td else ""
    return f"🔴 <code>DISCONNECTED ({esc(display_time)}{esc(dur_txt)})</code>"


def build_device_record(
    serial_no: str,
    industry: str,
    site: str,
    device_type: str,
    firmware: str,
    status_text: str,
    ts_raw: str,
    now: datetime,
    site_id: str = "",
) -> dict[str, Any]:
    s_clean = (serial_no or "N/A").strip() or "N/A"
    ind_clean = "N/A" if is_unassigned_value(industry) else industry.strip()
    site_clean = "N/A" if is_unassigned_value(site) else site.strip()
    type_clean = (device_type or "LOGPRO PLUS").strip() or "LOGPRO PLUS"
    fw_clean = (firmware or "N/A").strip() or "N/A"
    st_clean = (status_text or "").strip()
    ts_clean = (ts_raw or "").strip()
    sid_clean = re.sub(r"\D", "", str(site_id or ""))

    last_seen = parse_timestamp(ts_clean)
    offline_td = (now - last_seen) if last_seen else None

    st_lower = st_clean.lower()
    explicit_disconnected = "disconnected" in st_lower or "offline" in st_lower
    explicit_connected = (
        ("connected" in st_lower and not explicit_disconnected)
        or st_lower in ("online", "true", "1", "active")
    )

    if explicit_disconnected:
        is_connected = False
    elif explicit_connected:
        is_connected = True
    elif offline_td is not None:
        is_connected = offline_td <= timedelta(hours=2)
    else:
        is_connected = False

    display_ts = format_display_time(last_seen, ts_clean)
    unassigned_flag = is_unassigned_device(ind_clean, site_clean)
    off_hours = (
        round(offline_td.total_seconds() / 3600.0, 2)
        if (offline_td is not None and not is_connected)
        else None
    )

    return {
        "serial_no": s_clean,
        "site_id": sid_clean,
        "dashboard_url": f"{BASE_URL}/pae/Site/{sid_clean}/Dashboard" if sid_clean else "",
        "industry": ind_clean,
        "site": site_clean,
        "device_type": type_clean,
        "firmware": fw_clean,
        "status": st_clean or ("Connected" if is_connected else "Disconnected"),
        "conn_label": "ONLINE" if is_connected else "DISCONNECTED",
        "is_connected": is_connected,
        "timestamp_raw": ts_clean or "N/A",
        "display_time": display_ts,
        "last_seen": last_seen,
        "offline_duration": offline_td if not is_connected else None,
        "offline_hours": off_hours,
        "is_unassigned": unassigned_flag,
    }


def extract_rows_from_any_json(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        s = payload.strip()
        if s.startswith(("{", "[")):
            try:
                payload = json.loads(s)
            except Exception:
                return []
        else:
            return []

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in (
            "Devices",
            "devices",
            "Data",
            "data",
            "Rows",
            "rows",
            "Items",
            "items",
            "Result",
            "result",
            "aaData",
            "Records",
            "records",
        ):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                inner = extract_rows_from_any_json(val)
                if inner:
                    return inner
            if isinstance(val, str) and val.strip().startswith(("{", "[")):
                inner = extract_rows_from_any_json(val)
                if inner:
                    return inner

        for val in payload.values():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                inner = extract_rows_from_any_json(val)
                if inner:
                    return inner

    return []


def flatten_dict(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}".lower() if prefix else str(k).lower()
        if isinstance(v, dict):
            out.update(flatten_dict(v, key))
        else:
            out[key] = v
    return out


def parse_ws_device_item(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    flat = flatten_dict(item)

    def pick(*candidates: str, default: str = "N/A") -> str:
        for c in candidates:
            cl = c.lower()
            if cl in flat and flat[cl] is not None:
                val = str(flat[cl]).strip()
                if val != "":
                    return val
        for c in candidates:
            cl = c.lower()
            for fk, fv in flat.items():
                if fv is not None and (fk.endswith(f".{cl}") or cl in fk):
                    val = str(fv).strip()
                    if val != "":
                        return val
        return default

    # Multi-method Site ID extraction (if/else fallback)
    raw_site_id = pick(
        "siteid",
        "site_id",
        "site.id",
        "fk_siteid",
        "locationid",
        "stationid",
        default="",
    )
    if not re.search(r"\d+", raw_site_id):
        raw_blob = json.dumps(item)
        m_url = re.search(r"/Site/(\d+)", raw_blob, re.IGNORECASE)
        if m_url:
            raw_site_id = m_url.group(1)

    serial_no = pick(
        "serialno",
        "serial_no",
        "serial",
        "deviceserial",
        "device_serial",
        "mac",
        default="N/A",
    )
    site = pick(
        "sitename",
        "site.name",
        "site",
        "locationname",
        "location",
    )
    industry = pick(
        "industryname",
        "industry.name",
        "industry",
        "sector",
    )
    device_type = pick(
        "devicetype",
        "device_type",
        "type.name",
        "type",
        "devicename",
        "device_name",
        default="LOGPRO PLUS",
    )
    firmware = pick(
        "firmware",
        "firmwareversion",
        "fw",
        "fwversion",
        "version",
    )
    status_text = pick(
        "status",
        "connectionstatus",
        "isconnected",
        "online",
        "state",
        default="",
    )
    ts_raw = pick(
        "timestamp",
        "lastseen",
        "lastcommunication",
        "lastpackettime",
        "lastlogtime",
        "datetime",
        "updatedon",
        default="",
    )

    return build_device_record(
        serial_no=serial_no,
        industry=industry,
        site=site,
        device_type=device_type,
        firmware=firmware,
        status_text=status_text,
        ts_raw=ts_raw,
        now=now,
        site_id=raw_site_id,
    )


def extract_site_id_map_from_html(soup: BeautifulSoup, raw_html: str) -> dict[str, str]:
    """
    Collects mapping of serial_no / industry / site -> 4-digit Site ID
    using multiple if/else methods (href links, onclick, data attributes, inline JS).
    """
    mapping: dict[str, str] = {}

    # Method 1: Anchor links <a href="/pae/Site/3545/Dashboard">
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        m = re.search(r"/Site/(\d+)", href, re.IGNORECASE)
        if m:
            sid = m.group(1)
            txt = " ".join(a.get_text(" ", strip=True).split()).lower()
            if txt:
                mapping[txt] = sid
            tr = a.find_parent("tr")
            if tr:
                row_txt = " ".join(tr.get_text(" ", strip=True).split())
                for tok in re.findall(r"\b\d{6,14}\b", row_txt):
                    mapping[tok] = sid

    # Method 2: Rows with onclick or data-siteid / data-id attributes
    for tag in soup.find_all(attrs={"onclick": True}):
        m = re.search(r"/Site/(\d+)|SiteId['\"]?\s*[:=]\s*['\"]?(\d+)", str(tag["onclick"]), re.I)
        if m:
            sid = m.group(1) or m.group(2)
            row_txt = " ".join(tag.get_text(" ", strip=True).split())
            for tok in re.findall(r"\b\d{6,14}\b", row_txt):
                mapping.setdefault(tok, sid)

    # Method 3: Script blocks mapping SerialNo and SiteId
    for m in re.finditer(
        r'"SerialNo"\s*:\s*"([^"]+)"[^}]{0,350}?"SiteId"\s*:\s*(\d+)',
        raw_html,
        re.IGNORECASE,
    ):
        mapping.setdefault(m.group(1).strip(), m.group(2).strip())

    for m in re.finditer(
        r'"SiteId"\s*:\s*(\d+)[^}]{0,350}?"SerialNo"\s*:\s*"([^"]+)"',
        raw_html,
        re.IGNORECASE,
    ):
        mapping.setdefault(m.group(2).strip(), m.group(1).strip())

    return mapping


def extract_devices_from_ws(
    session: requests.Session, now: datetime
) -> list[dict[str, Any]]:
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "text/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": DEVICES_URL,
    }

    all_parsed: list[dict[str, Any]] = []
    seen_serials: set[str] = set()
    page_size = 100

    for page in range(1, 25):
        params = {
            "uac": "pae",
            "rp": str(page_size),
            "page": str(page),
            "devices": "",
            "deviceType": "",
            "ind": "",
            "status": "all",
            "_": str(int(time.time() * 1000)),
        }
        try:
            r = session.get(WS_DEVICES_URL, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            raw_rows = extract_rows_from_any_json(r.text)
            if not raw_rows:
                break

            page_added = 0
            for item in raw_rows:
                dev = parse_ws_device_item(item, now)
                dedup_key = f"{dev['serial_no']}|{dev['site']}|{dev['timestamp_raw']}"
                if dedup_key not in seen_serials:
                    seen_serials.add(dedup_key)
                    all_parsed.append(dev)
                    page_added += 1

            if len(raw_rows) < page_size or page_added == 0:
                break
        except Exception as exc:
            logger.warning("WS fetch failed on page %d: %s", page, exc)
            break

    return all_parsed


def extract_devices_from_html_table(
    soup: BeautifulSoup, now: datetime
) -> tuple[list[dict[str, Any]], int, int]:
    all_trs = soup.select("tr")
    devices: list[dict[str, Any]] = []
    unparsed_ts = 0

    for tr in all_trs:
        tds = tr.find_all("td")
        cells = tds if tds else tr.find_all(["div", "span"], recursive=False)
        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in cells]
        cols = [c for c in cols if c != ""]

        if len(cols) < 4:
            continue

        row_text = " ".join(cols)
        has_status = ("connected" in row_text.lower()) or (
            "disconnected" in row_text.lower()
        )
        ts_match = re.search(
            r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{1,2}:\d{2}:\d{2}(?:\s*(?:AM|PM))?\b",
            row_text,
            re.IGNORECASE,
        )

        if not has_status and not ts_match:
            continue

        sid = ""
        for a in tr.find_all("a", href=True):
            m = re.search(r"/Site/(\d+)", str(a["href"]), re.IGNORECASE)
            if m:
                sid = m.group(1)
                break

        serial_no = cols[0]
        industry = cols[1] if len(cols) > 1 else "N/A"
        site = cols[2] if len(cols) > 2 else "N/A"
        device_type = cols[4] if len(cols) > 4 else "LOGPRO PLUS"
        firmware = cols[5] if len(cols) > 5 else "N/A"
        status_text = next(
            (
                c
                for c in cols
                if "disconnected" in c.lower() or "connected" in c.lower()
            ),
            cols[6] if len(cols) > 6 else "Unknown",
        )
        ts_raw = ts_match.group(0) if ts_match else cols[-1]
        if not parse_timestamp(ts_raw):
            unparsed_ts += 1

        devices.append(
            build_device_record(
                serial_no=serial_no,
                industry=industry,
                site=site,
                device_type=device_type,
                firmware=firmware,
                status_text=status_text,
                ts_raw=ts_raw,
                now=now,
                site_id=sid,
            )
        )

    return devices, len(all_trs), unparsed_ts


def discover_site_id_via_search(session: requests.Session, dev: dict[str, Any]) -> str:
    """
    Fallback discovery if site_id wasn't in initial Devices payload:
    tries ControlPanel/Sites or WS/Sites endpoint using industry/serial.
    """
    candidates = [
        f"{BASE_URL}/pae/ControlPanel/Sites",
        f"{BASE_URL}/Services/EF/WS/Sites",
    ]
    needle_serial = (dev.get("serial_no") or "").lower()
    needle_ind = (dev.get("industry") or "").lower()

    for url in candidates:
        try:
            r = session.get(url, timeout=15)
            if not r.ok:
                continue
            txt = r.text or ""
            # Check JSON rows first
            rows = extract_rows_from_any_json(txt)
            if rows:
                for item in rows:
                    blob = json.dumps(item).lower()
                    if (needle_serial and needle_serial in blob) or (
                        needle_ind and needle_ind != "n/a" and needle_ind in blob
                    ):
                        flat = flatten_dict(item)
                        for k in ("siteid", "site_id", "id"):
                            if k in flat and re.fullmatch(r"\d+", str(flat[k]).strip()):
                                return str(flat[k]).strip()

            # Else parse HTML links /Site/<id>/Dashboard
            soup = BeautifulSoup(txt, "html.parser")
            for a in soup.find_all("a", href=True):
                m = re.search(r"/Site/(\d+)", str(a["href"]), re.IGNORECASE)
                if m:
                    parent_txt = (
                        a.find_parent("tr").get_text(" ", strip=True).lower()
                        if a.find_parent("tr")
                        else a.get_text(" ", strip=True).lower()
                    )
                    if (needle_serial and needle_serial in parent_txt) or (
                        needle_ind and needle_ind != "n/a" and needle_ind in parent_txt
                    ):
                        return m.group(1)
        except Exception:
            continue
    return ""


def scrape_site_dashboard_details(
    session: requests.Session, dev: dict[str, Any]
) -> dict[str, Any]:
    """
    Multi-method dashboard scraper (if/else fallback chain):
    1. Resolve Site ID (from dev record or live discovery)
    2. Fetch /pae/Site/<SITE_ID>/Dashboard
    3. Extract Tank/Instrument names & readings (mm, litr, m3, KL, flow, level, etc.)
       via: (a) embedded JSON/JS variables, (b) dashboard cards/widgets,
       (c) HTML tables, (d) regex unit scanner.
    """
    site_id = dev.get("site_id") or discover_site_id_via_search(session, dev)
    if site_id:
        dev["site_id"] = site_id
        dev["dashboard_url"] = f"{BASE_URL}/pae/Site/{site_id}/Dashboard"

    if not site_id:
        return {
            "ok": False,
            "site_id": "",
            "url": "",
            "error": "Is site ka 4-digit Site ID portal response mein nahi mila.",
            "instruments": [],
        }

    dash_url = f"{BASE_URL}/pae/Site/{site_id}/Dashboard"
    try:
        resp = session.get(dash_url, timeout=25)
        resp.raise_for_status()
        html = resp.text or ""
    except Exception as exc:
        return {
            "ok": False,
            "site_id": site_id,
            "url": dash_url,
            "error": f"Dashboard load error: {exc}",
            "instruments": [],
        }

    soup = BeautifulSoup(html, "html.parser")
    instruments: list[dict[str, str]] = []
    seen_pairs: set[str] = set()

    def add_instrument(name: str, value: str, unit: str = "", extra: str = "") -> None:
        n = " ".join((name or "").split()).strip(" :-|")
        v = " ".join((value or "").split()).strip()
        u = " ".join((unit or "").split()).strip()
        if not v:
            return
        if not n:
            n = "Sensor / Channel"
        key = f"{n.lower()}|{v.lower()}|{u.lower()}"
        if key not in seen_pairs:
            seen_pairs.add(key)
            instruments.append(
                {
                    "name": n[:65],
                    "value": v[:45],
                    "unit": u[:20],
                    "extra": extra[:60],
                }
            )

    # Method 1: Check embedded JSON / JS object arrays in <script> tags
    for script in soup.find_all("script"):
        s_txt = script.string or script.get_text() or ""
        if not s_txt:
            continue
        for m in re.finditer(
            r'\{[^{}]{10,450}?(?:Parameter|Channel|Tank|Tag|Sensor|Name)[^{}]{10,450}?\}',
            s_txt,
            re.IGNORECASE,
        ):
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    nm = str(
                        obj.get("TankName")
                        or obj.get("ParameterName")
                        or obj.get("TagName")
                        or obj.get("Name")
                        or obj.get("ChannelName")
                        or ""
                    )
                    val = str(
                        obj.get("Value")
                        or obj.get("LastValue")
                        or obj.get("Reading")
                        or obj.get("Level")
                        or ""
                    )
                    unit = str(obj.get("Unit") or obj.get("Units") or "")
                    if nm and val:
                        add_instrument(nm, val, unit)
            except Exception:
                continue

    # Method 2: Dashboard HTML Tables (Parameter | Value | Unit | Timestamp)
    for tr in soup.select("table tr"):
        cols = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["td", "th"])]
        cols = [c for c in cols if c]
        if len(cols) >= 2:
            joined = " ".join(cols)
            if re.search(
                r"\b(\d+(?:\.\d+)?)\s*(mm|cm|m|mtr|meter|litr|liter|litre|ltr|l|m3|m³|kl|kld|mld|mg/l|ppm|ph|bar|psi|°c|temp|flow|level)\b",
                joined,
                re.IGNORECASE,
            ):
                if len(cols) >= 3:
                    add_instrument(cols[0], cols[1], cols[2], cols[3] if len(cols) > 3 else "")
                else:
                    add_instrument(cols[0], cols[1])

    # Method 3: Dashboard Cards / Panels / Tank Widgets
    for card in soup.select(".card, .panel, .widget, .box, .info-box, .small-box, [class*='tank'], [class*='gauge'], [class*='sensor']"):
        txt = " ".join(card.get_text(" | ", strip=True).split())
        if len(txt) > 320:
            continue
        m_unit = re.search(
            r"([A-Za-z0-9_\-\(\)\s]{2,40}?)\s*[:|\-]\s*([\-+]?\d+(?:\.\d+)?)\s*(mm|cm|m|ltr|litr|liter|litre|m3|m³|kl|kld|mg/l|ppm|ph|°c|%)",
            txt,
            re.IGNORECASE,
        )
        if m_unit:
            add_instrument(m_unit.group(1), m_unit.group(2), m_unit.group(3))

    # Method 4: Page-wide regex unit scanner fallback (mm, litr, m3, KL, etc.)
    if not instruments:
        body_txt = " ".join(soup.get_text(" \n ", strip=True).split())
        for m in re.finditer(
            r"([A-Za-z][A-Za-z0-9_\-\(\)\s]{1,35}?)\s*[:\-]?\s*([\-+]?\d+(?:\.\d+)?)\s*(mm|cm|ltr|litr|liter|litre|m3|m³|kl|kld|mg/l|ppm|ph)\b",
            body_txt,
            re.IGNORECASE,
        ):
            lbl = m.group(1).strip()
            if lbl.lower() not in ("http", "width", "height", "margin", "padding", "font"):
                add_instrument(lbl, m.group(2), m.group(3))
            if len(instruments) >= 25:
                break

    return {
        "ok": True,
        "site_id": site_id,
        "url": dash_url,
        "instruments": instruments[:25],
        "error": "",
    }


def fetch_devices_multi_strategy(
    session: requests.Session,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resp = session.get(DEVICES_URL, timeout=25)
    resp.raise_for_status()
    raw_html = resp.text or ""

    meta: dict[str, Any] = {
        "server_error_500": False,
        "html_length": len(raw_html),
        "tr_total": 0,
        "unparsed_ts_count": 0,
        "source": "none",
    }

    if (
        not raw_html
        or "500 - internal server error" in raw_html.lower()
        or "internal server error" in raw_html.lower()
    ):
        meta["server_error_500"] = True
        return [], meta

    soup = BeautifulSoup(raw_html, "html.parser")
    now = datetime.now(timezone.utc)
    site_id_map = extract_site_id_map_from_html(soup, raw_html)

    _, tr_total, _ = extract_devices_from_html_table(soup, now)
    meta["tr_total"] = tr_total

    ws_devices = extract_devices_from_ws(session, now)
    if ws_devices:
        for d in ws_devices:
            if not d.get("site_id"):
                d["site_id"] = (
                    site_id_map.get(d["serial_no"])
                    or site_id_map.get(d["industry"].lower())
                    or site_id_map.get(d["site"].lower())
                    or ""
                )
                if d["site_id"]:
                    d["dashboard_url"] = f"{BASE_URL}/pae/Site/{d['site_id']}/Dashboard"
        meta["unparsed_ts_count"] = sum(
            1 for d in ws_devices if d["last_seen"] is None
        )
        meta["source"] = "ws-devices-json"
        return ws_devices, meta

    html_devices, _, unparsed_ts = extract_devices_from_html_table(soup, now)
    if len(html_devices) > 1:
        for d in html_devices:
            if not d.get("site_id"):
                d["site_id"] = site_id_map.get(d["serial_no"], "")
                if d["site_id"]:
                    d["dashboard_url"] = f"{BASE_URL}/pae/Site/{d['site_id']}/Dashboard"
        meta["unparsed_ts_count"] = unparsed_ts
        meta["source"] = "html-table"
        return html_devices, meta

    meta["source"] = "0-found"
    return [], meta


# ==============================================================================
# AGING RANGE FILTER (< 1D, < 1W, < 24D, < 11M, < 1Y) & ANALYSIS
# ==============================================================================
AGING_RANGE_CONFIG: dict[str, tuple[str, timedelta | None, timedelta]] = {
    # key: (Display Title, min_exclusive_td, max_inclusive_td)
    "1d": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 1 ᴅᴀʏ (≤ 24ʜ)", None, timedelta(days=1)),
    "1w": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 1 ᴡᴇᴇᴋ (≤ 7ᴅ)", None, timedelta(days=7)),
    "24d": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 24 ᴅᴀʏꜱ (≤ 24ᴅ)", None, timedelta(days=24)),
    "11m": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 11 ᴍᴏɴᴛʜꜱ (≤ 330ᴅ)", None, timedelta(days=330)),
    "1y": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ < 1 ʏᴇᴀʀ (≤ 365ᴅ)", None, timedelta(days=365)),
    "gt1y": ("ᴅɪꜱᴄᴏɴɴᴇᴄᴛᴇᴅ > 1 ʏᴇᴀʀ (365ᴅ+)", timedelta(days=365), timedelta(days=36500)),
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
                f"{d['industry']} ({d['site']})"
                if d["site"] != "N/A"
                else d["industry"]
            )
            site_offline_counts[site_label] = site_offline_counts.get(site_label, 0) + 1

    top_sites = sorted(site_offline_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "checked_at_ist": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "total": len(devices),
        "assigned_count": len(assigned_devices),
        "connected": len(online_list),
        "disconnected_count": len(offline_list),
        "unassigned_count": len(unassigned_list),
        "aging_counts": aging_counts,
        "all_devices": devices,
        "assigned_devices": assigned_devices,
        "online_list": online_list,
        "offline_list": offline_list,
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
            "Dashboard URL",
            "Industry Name",
            "Site Type",
            "Assignment",
            "Device Type",
            "Firmware",
            "Status",
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
                d.get("dashboard_url", ""),
                d.get("industry", "N/A"),
                d.get("site", "N/A"),
                "UNASSIGNED" if d.get("is_unassigned") else "ASSIGNED",
                d.get("device_type", "N/A"),
                d.get("firmware", "N/A"),
                d.get("conn_label", "N/A"),
                d.get("display_time", "N/A"),
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


# ==============================================================================
# GROQ AI ENGINE (LLAMA 3.3 70B + INSTANT FALLBACK)
# ==============================================================================
def build_project_context(analysis: dict[str, Any]) -> str:
    compact_devices = [
        {
            "serial": d.get("serial_no"),
            "site_id": d.get("site_id"),
            "industry": d.get("industry"),
            "site_type": d.get("site"),
            "unassigned": d.get("is_unassigned"),
            "device_type": d.get("device_type"),
            "fw": d.get("firmware"),
            "status": d.get("conn_label"),
            "offline_hours": d.get("offline_hours"),
            "last_seen": d.get("display_time"),
        }
        for d in analysis.get("all_devices", [])[:60]
    ]
    return (
        f"Client: {CLIENT_NAME}\n"
        f"Checked At: {analysis.get('checked_at_ist')}\n"
        f"Total Devices: {analysis.get('total')} | Assigned: {analysis.get('assigned_count')} | Unassigned N/A: {analysis.get('unassigned_count')}\n"
        f"Online: {analysis.get('connected')} | Disconnected: {analysis.get('disconnected_count')}\n"
        f"Aging (< Range): {analysis.get('aging_counts')}\n"
        f"Top Offline Sites: {analysis.get('top_offline_sites')}\n"
        f"Device Types: {analysis.get('device_type_breakdown')}\n"
        f"Firmware: {analysis.get('firmware_breakdown')}\n"
        f"Devices Sample: {compact_devices}"
    )


def call_groq_ai(system_prompt: str, user_prompt: str, analysis: dict[str, Any]) -> str:
    if not GROQ_API_KEY:
        return "GROQ_API_KEY missing hai."

    models = ("llama-3.3-70b-versatile", "llama-3.1-8b-instant")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    last_err = ""

    for model in models:
        try:
            r = requests.post(
                GROQ_API_URL,
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.35,
                    "max_tokens": 900,
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
                last_err = f"{model} HTTP {r.status_code}: {r.text[:160]}"
        except Exception as exc:
            last_err = f"{model}: {exc}"

    ages = analysis.get("aging_counts", {})
    return (
        f"[Groq Fallback — {last_err}]\n"
        f"Total: {analysis.get('total', 0)} | Online: {analysis.get('connected', 0)} | "
        f"Offline: {analysis.get('disconnected_count', 0)} | Unassigned N/A: {analysis.get('unassigned_count', 0)}\n"
        f"<1D: {ages.get('1d', 0)} | <1W: {ages.get('1w', 0)} | <24D: {ages.get('24d', 0)} | "
        f"<11M: {ages.get('11m', 0)} | <1Y: {ages.get('1y', 0)}"
    )


def generate_ai_analysis(analysis: dict[str, Any]) -> str:
    ctx = build_project_context(analysis)
    sys_p = (
        f"Tum {CLIENT_NAME} ke EnviroFRONT industrial IoT telemetry suite ke senior AI engineer ho. "
        "Hinglish mein crisp, structured aur actionable executive report do."
    )
    usr_p = (
        "Live telemetry data ke basis par report banao:\n"
        "1. Overall System Health & Risk\n"
        "2. Critical Offline Sites & Aging Buckets (<1D, <1W, <24D, <11M, <1Y)\n"
        "3. Unassigned (N/A) Devices & Firmware Insights\n"
        "4. Action Checklist\n\n"
        f"{ctx}"
    )
    return call_groq_ai(sys_p, usr_p, analysis)


def answer_project_question(user_question: str, analysis: dict[str, Any]) -> str:
    ctx = build_project_context(analysis)
    sys_p = (
        f"Tum {CLIENT_NAME} EnviroFRONT telemetry bot ke AI assistant ho. "
        "Sirf diye gaye live project data ke basis par Hinglish mein accurate aur seedha jawab do."
    )
    usr_p = f"LIVE TELEMETRY DATA:\n{ctx}\n\nUSER QUESTION:\n{user_question}"
    return call_groq_ai(sys_p, usr_p, analysis)


def require_auth(chat_id: int) -> dict[str, Any] | None:
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        return None
    return sess_data


# ==============================================================================
# UI VIEWS & INTERACTIVE SITE DASHBOARD FLOW
# ==============================================================================
async def send_start_view(chat_id: int, bot) -> None:
    existing = require_auth(chat_id)
    if existing:
        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — {sc('TELEMETRY COMMAND')}</b>\n"
            f"🟣 <i>{sc('ENTERPRISE ACTIVE SESSION')}</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>{sc('Account')}:</b> <code>{esc(existing.get('username'))}</code>\n"
            f"🔄 <b>{sc('Auto-Sync')}:</b> <code>EVERY 1 MINUTE</code>\n"
            f"🔔 <b>{sc('Alerts')}:</b> <code>2H → 10H → 1D (1-TIME EACH)</code>\n\n"
            "<i>Neeche buttons se live sites, dashboard readings aur aging check karein:</i>"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
        await bot.send_message(
            chat_id=chat_id,
            text=f"🎛️ <b>{esc(CLIENT_NAME)} — {sc('CONTROL PANEL')}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=bottom_reply_keyboard(),
        )
        return

    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    intro = await bot.send_message(
        chat_id=chat_id,
        text=(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — {sc('TELEMETRY COMMAND')}</b>\n"
            "<i>Initializing Console...</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=guest_inline_keyboard(),
    )
    await animate_message(
        intro,
        [
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — {sc('TELEMETRY COMMAND')}</b>\n"
            "<i>Loading modules [■■□□□]</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — {sc('TELEMETRY COMMAND')}</b>\n"
            "<i>Loading modules [■■■■■]</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 Kripya apna <b>Login ID / Email</b> bhejein:",
        ],
        delay=0.25,
        final_markup=guest_inline_keyboard(),
    )


async def send_login_prompt(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔐 <b>{esc(CLIENT_NAME)} — {sc('PORTAL LOGIN')}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Kripya apna <b>Login ID / Email</b> bhejein:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=guest_inline_keyboard(),
    )


async def send_logout_action(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
    sess_data = USER_SESSIONS.pop(chat_id, None)
    LAST_SNAPSHOT.pop(chat_id, None)
    clear_all_alerts_for_chat(chat_id)

    if sess_data and sess_data.get("session"):
        try:
            sess_data["session"].close()
        except Exception:
            pass

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚪 <b>{sc('SESSION CLOSED')}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Session aur alerts band kar diye gaye hain.\n"
            "Dobara login ke liye button dabayein:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=guest_inline_keyboard(),
    )


async def send_status_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="<i>📡 Fetching live telemetry status...</i>",
        parse_mode=ParseMode.HTML,
    )
    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)

        if analysis["meta"]["server_error_500"]:
            await loader.edit_text(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔥 <b>SERVER ALERT — HTTP 500</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "EnviroFRONT ne <code>500 Internal Server Error</code> return kiya hai.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        sites_lines = (
            "\n".join(
                f"  {i}. <code>{esc(s)} — {c} offline</code>"
                for i, (s, c) in enumerate(analysis["top_offline_sites"], start=1)
            )
            if analysis["top_offline_sites"]
            else "  ✨ <code>Koi critical offline site nahi</code>"
        )
        ages = analysis["aging_counts"]

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — {sc('LIVE STATUS')}</b>\n"
            f"🕒 <i>{esc(analysis['checked_at_ist'])}</i> (1m sync)\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>{sc('Total Devices')}:</b> <b>{analysis['total']}</b>\n"
            f"🏭 <b>{sc('Assigned Sites')}:</b> <b>{analysis['assigned_count']}</b>\n"
            f"📌 <b>{sc('Unassigned N/A')}:</b> <b>{analysis['unassigned_count']}</b>\n"
            f"🟢 <b>{sc('Online')}:</b> <b>{analysis['connected']}</b>\n"
            f"🔴 <b>{sc('Disconnected')}:</b> <b>{analysis['disconnected_count']}</b>\n\n"
            f"<b>⏳ {sc('Aging Buckets (< Range)')}</b>\n"
            f"🟡 <code>&lt;1D: {ages['1d']}</code> | 🟠 <code>&lt;1W: {ages['1w']}</code> | 🔴 <code>&lt;24D: {ages['24d']}</code>\n"
            f"🟣 <code>&lt;11M: {ages['11m']}</code> | ⚫ <code>&lt;1Y: {ages['1y']}</code> | 🚨 <code>&gt;1Y: {ages['gt1y']}</code>\n\n"
            f"<b>🔥 {sc('Top Offline Sites')}</b>\n"
            f"{sites_lines}"
        )
        await loader.edit_text(
            msg, parse_mode=ParseMode.HTML, reply_markup=main_inline_keyboard()
        )
    except Exception as exc:
        await loader.edit_text(
            f"❌ <b>Status Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_aging_menu(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    ages = analysis.get("aging_counts", {})

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ <b>{sc('AGING SUB-MENU (< RANGE)')}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🟡 <code>&lt; 1 Day (≤24h)    : {ages.get('1d', 0)}</code>\n"
        f"🟠 <code>&lt; 1 Week (≤7d)    : {ages.get('1w', 0)}</code>\n"
        f"🔴 <code>&lt; 24 Days (≤24d)  : {ages.get('24d', 0)}</code>\n"
        f"🟣 <code>&lt; 11 Months(≤330d): {ages.get('11m', 0)}</code>\n"
        f"⚫ <code>&lt; 1 Year (≤365d)  : {ages.get('1y', 0)}</code>\n"
        f"🚨 <code>&gt; 1 Year (365d+)  : {ages.get('gt1y', 0)}</code>\n\n"
        "<i>Button dabayein—har bucket mein sirf us limit tak offline sites dikhengi:</i>"
    )
    await bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode=ParseMode.HTML,
        reply_markup=aging_submenu_keyboard(),
    )


async def send_aging_bucket_list(chat_id: int, bucket_key: str, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    title = AGING_RANGE_CONFIG.get(bucket_key, ("DISCONNECTED", None, timedelta(days=1)))[0]
    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        matched = filter_devices_by_aging_range(analysis["all_devices"], bucket_key)

        if not matched:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏳ <b>{esc(title)}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "<code>Is range mein koi offline site nahi hai.</code>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=aging_submenu_keyboard(),
            )
            return

        entries: list[str] = []
        for idx, d in enumerate(matched, start=1):
            badge = color_badge_for_hours(d.get("offline_hours"), False)
            entries.append(
                f"{idx}. {badge} {format_industry_with_type(d['industry'], d['site'])} | "
                f"<b>{esc(d['serial_no'])}</b>\n"
                f"   {format_status_with_time(False, d['display_time'], d.get('offline_duration'))}"
            )

        chunk: list[str] = []
        sent_parts = 0
        for line in entries:
            chunk.append(line)
            if len("\n".join(chunk)) > 3200:
                hdr = (
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏳ <b>{esc(title)} ({len(matched)})</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    if sent_parts == 0
                    else ""
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=hdr + "\n".join(chunk),
                    parse_mode=ParseMode.HTML,
                )
                sent_parts += 1
                chunk = []

        hdr = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>{esc(title)} ({len(matched)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            if sent_parts == 0
            else ""
        )
        await bot.send_message(
            chat_id=chat_id,
            text=hdr + "\n".join(chunk),
            parse_mode=ParseMode.HTML,
            reply_markup=aging_submenu_keyboard(),
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Bucket Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=aging_submenu_keyboard(),
        )


async def send_allsite_buttons_page(chat_id: int, page: int, bot) -> None:
    """
    Shows Assigned Sites list with bold serial numbers AND clickable inline buttons
    for every site so user can tap any site to open its live Dashboard details.
    """
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    assigned_devs = analysis["assigned_devices"]

    if not assigned_devs:
        await bot.send_message(
            chat_id=chat_id,
            text="<code>Koi assigned site nahi mili.</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
        return

    per_page = 12
    total_pages = max(1, (len(assigned_devs) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, len(assigned_devs))
    subset = assigned_devs[start:end]

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🌐 <b>{sc('ALL ASSIGNED SITES')} ({len(assigned_devs)}) — P.{page + 1}/{total_pages}</b>",
        "<i>Kisi bhi site button par tap karke live Dashboard (mm, litr, tank) dekhein:</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    btn_rows: list[list[InlineKeyboardButton]] = []

    for offset, d in enumerate(subset):
        global_idx = start + offset
        badge = color_badge_for_hours(d.get("offline_hours"), d["is_connected"])
        lines.append(
            f"{global_idx + 1}. {badge} {format_industry_with_type(d['industry'], d['site'])} — "
            f"<b>{esc(d['serial_no'])}</b>\n"
            f"   {format_status_with_time(d['is_connected'], d['display_time'], d.get('offline_duration'))}"
        )
        short_ind = d["industry"][:22]
        short_type = d["site"][:10] if d["site"] != "N/A" else ""
        label = f"{badge} {global_idx + 1}. {short_ind}" + (f" ({short_type})" if short_type else "")
        btn_rows.append(
            [InlineKeyboardButton(label[:48], callback_data=f"dash:{global_idx}")]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sites:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"sites:{page + 1}"))
    if nav_row:
        btn_rows.append(nav_row)

    btn_rows.append(
        [
            InlineKeyboardButton("🔄 Refresh List", callback_data=f"refresh_sites:{page}"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="act:main_menu"),
        ]
    )

    await bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(btn_rows),
    )


async def send_site_dashboard_view(chat_id: int, site_index: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    assigned_devs = analysis["assigned_devices"]

    if site_index < 0 or site_index >= len(assigned_devs):
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Site index purana ho gaya hai, list refresh kar rahe hain...",
            parse_mode=ParseMode.HTML,
        )
        await send_allsite_buttons_page(chat_id, 0, bot)
        return

    dev = assigned_devs[site_index]
    loader = await bot.send_message(
        chat_id=chat_id,
        text=f"<i>🎛️ Dashboard fetch ho raha hai: {esc(dev['industry'])}...</i>",
        parse_mode=ParseMode.HTML,
    )

    dash = await asyncio.to_thread(
        scrape_site_dashboard_details, sess_data["session"], dev
    )

    badge = color_badge_for_hours(dev.get("offline_hours"), dev["is_connected"])
    header = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏭 <b>{esc(dev['industry'])}</b>\n"
        f"📌 <b>{sc('Site Type')}:</b> <code>{esc(dev['site'])}</code> | "
        f"<b>{sc('Serial')}:</b> <b>{esc(dev['serial_no'])}</b>\n"
        f"{badge} {format_status_with_time(dev['is_connected'], dev['display_time'], dev.get('offline_duration'))}\n"
    )
    if dash.get("site_id"):
        header += f"🆔 <b>{sc('Site ID')}:</b> <code>{esc(dash['site_id'])}</code>\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━\n\n"

    if not dash.get("ok"):
        body = (
            f"⚠️ <code>{esc(dash.get('error', 'Dashboard unavailable'))}</code>\n"
            f"▸ <b>Device Type:</b> <code>{esc(dev['device_type'])}</code>\n"
            f"▸ <b>Firmware:</b> <code>{esc(dev['firmware'])}</code>"
        )
    elif not dash.get("instruments"):
        body = (
            f"ℹ️ <code>Dashboard ({esc(dash['url'])}) open hua lekin live sensor tags empty hain.</code>\n"
            f"▸ <b>Device Type:</b> <code>{esc(dev['device_type'])}</code>\n"
            f"▸ <b>Firmware:</b> <code>{esc(dev['firmware'])}</code>"
        )
    else:
        inst_lines = [f"<b>📊 {sc('LIVE INSTRUMENTS & TANK READINGS')}</b>"]
        for i, inst in enumerate(dash["instruments"], start=1):
            unit_str = f" {inst['unit']}" if inst.get("unit") else ""
            ext_str = f" ({inst['extra']})" if inst.get("extra") else ""
            inst_lines.append(
                f"{i}. 🔷 <b>{esc(inst['name'])}:</b> "
                f"<code>{esc(inst['value'])}{esc(unit_str)}</code>{esc(ext_str)}"
            )
        body = "\n".join(inst_lines)

    page_back = site_index // 12
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Refresh Dashboard", callback_data=f"dash:{site_index}"),
                InlineKeyboardButton("⬅️ Back to Sites", callback_data=f"sites:{page_back}"),
            ],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="act:main_menu")],
        ]
    )

    await loader.edit_text(
        header + body, parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def send_online_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        online_list = analysis["online_list"]

        if not online_list:
            await bot.send_message(
                chat_id=chat_id,
                text="🟡 <code>Koi online device nahi mila.</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🟢 <b>{sc('ONLINE DEVICES')} ({len(online_list)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for idx, d in enumerate(online_list[:25], start=1):
            lines.append(
                f"{idx}. 🟢 {format_industry_with_type(d['industry'], d['site'])} | "
                f"<b>{esc(d['serial_no'])}</b>\n"
                f"   {format_status_with_time(True, d['display_time'])}"
            )
        if len(online_list) > 25:
            lines.append(f"\n<code>...aur {len(online_list) - 25} online devices hain.</code>")

        await bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Online Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_unassigned_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        unassigned_list = analysis["unassigned_list"]

        if not unassigned_list:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{sc('UNASSIGNED SITES (N/A)')}</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "<code>Koi N/A unassigned device nahi mila.</code>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        entries: list[str] = []
        for idx, d in enumerate(unassigned_list, start=1):
            badge = color_badge_for_hours(d.get("offline_hours"), d["is_connected"])
            entries.append(
                f"{idx}. {badge} <b>{esc(d['serial_no'])}</b> | "
                f"<code>Type: {esc(d['site'])} | {esc(d['device_type'])}</code>\n"
                f"   <code>Site assign nahi hai</code> — "
                f"{format_status_with_time(d['is_connected'], d['display_time'], d.get('offline_duration'))}"
            )

        chunk: list[str] = []
        sent_parts = 0
        for line in entries:
            chunk.append(line)
            if len("\n".join(chunk)) > 3200:
                hdr = (
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>{sc('UNASSIGNED N/A — SITE ASSIGN NAHI HAI')} ({len(unassigned_list)})</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    if sent_parts == 0
                    else ""
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=hdr + "\n".join(chunk),
                    parse_mode=ParseMode.HTML,
                )
                sent_parts += 1
                chunk = []

        hdr = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>{sc('UNASSIGNED N/A — SITE ASSIGN NAHI HAI')} ({len(unassigned_list)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            if sent_parts == 0
            else ""
        )
        await bot.send_message(
            chat_id=chat_id,
            text=hdr + "\n".join(chunk),
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Unassigned Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_health_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        meta = analysis["meta"]

        type_lines = (
            "\n".join(
                f"  🔷 <code>{esc(dt)} : {cnt} devices</code>"
                for dt, cnt in sorted(
                    analysis["device_type_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  <code>No device type data</code>"
        )

        fw_lines = (
            "\n".join(
                f"  🟣 <code>FW {esc(fw)} : {cnt} devices</code>"
                for fw, cnt in sorted(
                    analysis["firmware_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  <code>No firmware data</code>"
        )

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🩺 <b>{esc(CLIENT_NAME)} — {sc('SYSTEM HEALTH')}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"▸ <b>{sc('Auto-Sync')}:</b> <code>EVERY 60s</code>\n"
            f"▸ <b>{sc('Alert Tiers')}:</b> <code>2h → 10h → 1d (One-Time)</code>\n"
            f"▸ <b>{sc('Server HTTP 500')}:</b> <code>{'YES' if meta['server_error_500'] else 'NO'}</code>\n"
            f"▸ <b>{sc('Total / Assigned / N/A')}:</b> <b>{analysis['total']} / {analysis['assigned_count']} / {analysis['unassigned_count']}</b>\n\n"
            f"<b>{sc('Device Type Distribution')}</b>\n"
            f"{type_lines}\n\n"
            f"<b>{sc('Firmware Distribution')}</b>\n"
            f"{fw_lines}"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Health Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_export_csv_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        csv_bytes = build_csv_bytes(analysis.get("all_devices", []))
        bio = io.BytesIO(csv_bytes)
        bio.name = f"BaggiJaat_EnviroFRONT_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv"
        await bot.send_document(
            chat_id=chat_id,
            document=bio,
            caption=f"📥 <b>{esc(CLIENT_NAME)} Telemetry CSV</b>\n<code>Total: {analysis.get('total', 0)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ <b>Export Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_ai_mode_prompt(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    AI_CHAT_MODE.add(chat_id)
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>{sc('GROQ AI ASSISTANT ON')}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ab apna sawaal type karein—Groq AI aapke live telemetry data ke hisaab se jawab dega.\n"
            "Bahar jaane ke liye <code>/exit_ai</code> bhejein."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=main_inline_keyboard(),
    )


async def send_ai_report_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    status_box = await bot.send_message(
        chat_id=chat_id,
        text="<i>🧠 Groq AI report taiyar kar raha hai...</i>",
        parse_mode=ParseMode.HTML,
    )
    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        ai_text = await asyncio.to_thread(generate_ai_analysis, analysis)

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 <b>{esc(CLIENT_NAME)} — {sc('GROQ AI REPORT')}</b>\n"
            f"🕒 <i>{esc(analysis['checked_at_ist'])}</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{esc(ai_text[:3400])}</code>"
        )
        await status_box.edit_text(
            msg, parse_mode=ParseMode.HTML, reply_markup=main_inline_keyboard()
        )
    except Exception as exc:
        await status_box.edit_text(
            f"❌ <b>AI Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


# ==============================================================================
# COMMAND, CALLBACK & TEXT HANDLERS (SAFE FOR ALL GROUPS & PRIVATE DM)
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_start_view(update.effective_chat.id, context.bot)


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_login_prompt(update.effective_chat.id, context.bot)


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_logout_action(update.effective_chat.id, context.bot)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_status_action(update.effective_chat.id, context.bot)


async def aging_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_aging_menu(update.effective_chat.id, context.bot)


async def online_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_online_action(update.effective_chat.id, context.bot)


async def unassigned_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_unassigned_action(update.effective_chat.id, context.bot)


async def allsite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_allsite_buttons_page(update.effective_chat.id, 0, context.bot)


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_health_action(update.effective_chat.id, context.bot)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_export_csv_action(update.effective_chat.id, context.bot)


async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    query = " ".join(context.args or []).strip().lower()
    if not query:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: <code>/site &lt;industry ya type ya serial&gt;</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    matches = [
        d
        for d in analysis["all_devices"]
        if query in d["site"].lower()
        or query in d["industry"].lower()
        or query in d["serial_no"].lower()
        or query in d["device_type"].lower()
    ]
    if not matches:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"<code>{esc(query)} se match karne wala koi device nahi mila.</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🔎 <b>{sc('FILTER')}: {esc(query.upper())} ({len(matches)})</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, d in enumerate(matches[:20], start=1):
        badge = color_badge_for_hours(d.get("offline_hours"), d["is_connected"])
        lines.append(
            f"{idx}. {badge} {format_industry_with_type(d['industry'], d['site'])} | "
            f"<b>{esc(d['serial_no'])}</b>\n"
            f"   {format_status_with_time(d['is_connected'], d['display_time'], d.get('offline_duration'))}"
        )
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=main_inline_keyboard(),
    )


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    question = " ".join(context.args or []).strip()
    if not question:
        await send_ai_mode_prompt(chat_id, context.bot)
        return

    thinking = await context.bot.send_message(
        chat_id=chat_id,
        text="<i>🤖 Groq AI jawab taiyar kar raha hai...</i>",
        parse_mode=ParseMode.HTML,
    )
    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    reply_txt = await asyncio.to_thread(answer_project_question, question, analysis)
    await thinking.edit_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>{sc('GROQ AI ASSISTANT')}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>{esc(reply_txt[:3400])}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_inline_keyboard(),
    )


async def exit_ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        AI_CHAT_MODE.discard(update.effective_chat.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="<code>AI mode band kar diya gaya hai.</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def ai_analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        await send_ai_report_action(update.effective_chat.id, context.bot)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data or ""

    if data == "act:main_menu":
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"👑 <b>{esc(CLIENT_NAME)} — {sc('MAIN MENU')}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    elif data == "act:aging_menu":
        await send_aging_menu(chat_id, context.bot)
    elif data.startswith("age:"):
        await send_aging_bucket_list(chat_id, data.split(":", 1)[1], context.bot)
    elif data.startswith("sites:"):
        await send_allsite_buttons_page(chat_id, int(data.split(":", 1)[1]), context.bot)
    elif data.startswith("refresh_sites:"):
        sess_data = require_auth(chat_id)
        if sess_data:
            LAST_SNAPSHOT[chat_id] = await asyncio.to_thread(
                run_self_analysis, sess_data["session"]
            )
        await send_allsite_buttons_page(chat_id, int(data.split(":", 1)[1]), context.bot)
    elif data.startswith("dash:"):
        await send_site_dashboard_view(chat_id, int(data.split(":", 1)[1]), context.bot)
    elif data == "act:login":
        await send_login_prompt(chat_id, context.bot)
    elif data == "act:logout":
        await send_logout_action(chat_id, context.bot)
    elif data == "act:status":
        await send_status_action(chat_id, context.bot)
    elif data == "act:online":
        await send_online_action(chat_id, context.bot)
    elif data == "act:unassigned":
        await send_unassigned_action(chat_id, context.bot)
    elif data == "act:health":
        await send_health_action(chat_id, context.bot)
    elif data == "act:export_csv":
        await send_export_csv_action(chat_id, context.bot)
    elif data == "act:ai_mode":
        await send_ai_mode_prompt(chat_id, context.bot)
    elif data == "act:ai_analyze":
        await send_ai_report_action(chat_id, context.bot)


REPLY_BUTTON_MAP: dict[str, str] = {
    "📊 status": "status",
    "🌐 all sites": "allsite",
    "🟢 online": "online",
    "⏳ aging menus": "aging_menu",
    "📌 unassigned n/a": "unassigned",
    "🩺 health": "health",
    "🤖 ai chat": "ai_mode",
    "🧠 ai report": "ai_analyze",
    "📥 export csv": "export_csv",
    "🚪 logout": "logout",
}


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    text = (update.effective_message.text or "").strip()
    state = USER_SESSIONS.get(chat_id)

    # Step 1: Username capture (works identically in Group & Private DM)
    if state and state.get("step") == "awaiting_username":
        state["username"] = text
        state["step"] = "awaiting_password"
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔑 Ab apna <b>Password</b> bhejein:",
            parse_mode=ParseMode.HTML,
        )
        return

    # Step 2: Password capture — uses bot.send_message (never replies to deleted msg!)
    if state and state.get("step") == "awaiting_password":
        username = state["username"]
        password = text
        state["step"] = "verifying"

        try:
            await update.effective_message.delete()
        except Exception:
            pass

        auth_box = await context.bot.send_message(
            chat_id=chat_id,
            text="<i>🔐 Session verify ho raha hai...</i>",
            parse_mode=ParseMode.HTML,
        )
        await animate_message(
            auth_box,
            [
                "<i>Verifying .ASPXAUTH &amp; CSRF [■■□]</i>",
                "<i>Handshaking with EnviroFRONT [■■■]</i>",
            ],
            delay=0.25,
        )

        session, reason = await asyncio.to_thread(
            login_envirofront, username, password
        )

        if session:
            USER_SESSIONS[chat_id] = {
                "step": "authenticated",
                "username": username,
                "session": session,
            }
            await auth_box.edit_text(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🟢 <b>{esc(CLIENT_NAME)} — {sc('AUTHENTICATED')}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>{sc('Account')}:</b> <code>{esc(username)}</code>\n"
                f"🔄 <b>{sc('Auto-Sync')}:</b> <code>EVERY 1 MINUTE</code>\n"
                f"🔔 <b>{sc('One-Time Alerts')}:</b> <code>2H → 10H → 1D</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🎛️ <b>{esc(CLIENT_NAME)} — {sc('PANEL BUTTONS ACTIVE')}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=bottom_reply_keyboard(),
            )
        else:
            USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
            await auth_box.edit_text(
                f"❌ <b>Login fail:</b> <code>{esc(reason)}</code>\n\n"
                "Dobara apna <b>Login ID / Email</b> bhejein:",
                parse_mode=ParseMode.HTML,
                reply_markup=guest_inline_keyboard(),
            )
        return

    mapped_action = REPLY_BUTTON_MAP.get(text.lower())
    if mapped_action:
        if mapped_action == "status":
            await send_status_action(chat_id, context.bot)
        elif mapped_action == "allsite":
            await send_allsite_buttons_page(chat_id, 0, context.bot)
        elif mapped_action == "online":
            await send_online_action(chat_id, context.bot)
        elif mapped_action == "aging_menu":
            await send_aging_menu(chat_id, context.bot)
        elif mapped_action == "unassigned":
            await send_unassigned_action(chat_id, context.bot)
        elif mapped_action == "health":
            await send_health_action(chat_id, context.bot)
        elif mapped_action == "ai_mode":
            await send_ai_mode_prompt(chat_id, context.bot)
        elif mapped_action == "ai_analyze":
            await send_ai_report_action(chat_id, context.bot)
        elif mapped_action == "export_csv":
            await send_export_csv_action(chat_id, context.bot)
        elif mapped_action == "logout":
            await send_logout_action(chat_id, context.bot)
        return

    if chat_id in AI_CHAT_MODE:
        sess_data = require_auth(chat_id)
        if not sess_data:
            AI_CHAT_MODE.discard(chat_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔒 Pehle login karein:",
                parse_mode=ParseMode.HTML,
                reply_markup=guest_inline_keyboard(),
            )
            return

        ans_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="<i>🤖 Groq AI jawab taiyar kar raha hai...</i>",
            parse_mode=ParseMode.HTML,
        )
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        reply_txt = await asyncio.to_thread(
            answer_project_question, text, analysis
        )
        await ans_msg.edit_text(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>{sc('GROQ AI ASSISTANT')}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{esc(reply_txt[:3400])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
        return

    if chat_type == "private" and not state:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔒 Login ke liye button dabayein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )


# ==============================================================================
# 1-MINUTE MONITOR: ONE-TIME TIERED NOTIFICATIONS (2H -> 10H -> 1D)
# ==============================================================================
ALERT_TIERS: list[tuple[str, str, timedelta]] = [
    ("2h", "2 HOURS SE OFFLINE", timedelta(hours=2)),
    ("10h", "10 HOURS SE OFFLINE", timedelta(hours=10)),
    ("1d", "1 DAY (24H) SE OFFLINE", timedelta(hours=24)),
]


async def background_monitor(app) -> None:
    while True:
        for chat_id, sess_data in list(USER_SESSIONS.items()):
            if sess_data.get("step") != "authenticated":
                continue

            try:
                analysis = await asyncio.to_thread(
                    run_self_analysis, sess_data["session"]
                )
                LAST_SNAPSHOT[chat_id] = analysis
                save_snapshot_db(chat_id, analysis)

                # Clear stage history if a device comes back ONLINE
                for d in analysis["online_list"]:
                    clear_stage_alerts_for_device(chat_id, d["serial_no"])

                # Check each offline device against 2h, 10h, 1d (strictly one-time per stage)
                tier_buckets: dict[str, list[dict[str, Any]]] = {
                    "2h": [],
                    "10h": [],
                    "1d": [],
                }

                for dev in analysis["offline_list"]:
                    off_td: timedelta | None = dev.get("offline_duration")
                    if off_td is None:
                        continue
                    serial = dev["serial_no"]

                    # Determine highest reached tier right now
                    reached_stage = ""
                    for stage_code, _, min_td in ALERT_TIERS:
                        if off_td >= min_td:
                            reached_stage = stage_code

                    if not reached_stage:
                        continue

                    stage_key = f"{chat_id}:{serial}:{reached_stage}"
                    if stage_key not in SENT_STAGE_ALERTS:
                        # Mark all lower stages too so lower alerts don't fire later
                        for s_code, _, s_td in ALERT_TIERS:
                            if off_td >= s_td:
                                mark_stage_alert_sent(f"{chat_id}:{serial}:{s_code}")
                        tier_buckets[reached_stage].append(dev)

                for stage_code, stage_title, _ in ALERT_TIERS:
                    devs_to_notify = tier_buckets[stage_code]
                    if not devs_to_notify:
                        continue

                    badge = "🟡" if stage_code == "2h" else ("🟠" if stage_code == "10h" else "🔴")
                    header = (
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{badge} <b>{esc(CLIENT_NAME)} — {esc(stage_title)} ({len(devs_to_notify)})</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    chunk: list[str] = []
                    for idx, dev in enumerate(devs_to_notify, start=1):
                        row = (
                            f"{idx}. {badge} {format_industry_with_type(dev['industry'], dev['site'])} | "
                            f"<b>{esc(dev['serial_no'])}</b>\n"
                            f"   {format_status_with_time(False, dev['display_time'], dev.get('offline_duration'))}"
                        )
                        if len(header + "\n".join(chunk + [row])) > 3500:
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=header + "\n".join(chunk),
                                parse_mode=ParseMode.HTML,
                                reply_markup=main_inline_keyboard(),
                            )
                            chunk = [row]
                        else:
                            chunk.append(row)

                    if chunk:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=header + "\n".join(chunk),
                            parse_mode=ParseMode.HTML,
                            reply_markup=main_inline_keyboard(),
                        )

            except Exception as exc:
                logger.warning("1m monitor error for %s: %s", chat_id, exc)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(app) -> None:
    init_db()
    load_stage_alerts_db()
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "BAGGI JAAT control panel"),
                BotCommand("login", "Login to EnviroFRONT"),
                BotCommand("status", "Live status & aging counts"),
                BotCommand("allsite", "Sites buttons & live dashboard"),
                BotCommand("aging", "<1D / <1W / <24D / <11M / <1Y"),
                BotCommand("unassigned", "Unassigned N/A sites"),
                BotCommand("online", "Online devices list"),
                BotCommand("health", "System & firmware health"),
                BotCommand("export", "Download CSV report"),
                BotCommand("site", "Search industry/type/serial"),
                BotCommand("ai", "Ask Groq AI assistant"),
                BotCommand("ai_analyze", "Full Groq AI report"),
                BotCommand("logout", "Logout session"),
            ]
        )
    except Exception as exc:
        logger.warning("Failed setting commands: %s", exc)

    app.create_task(background_monitor(app))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing hai.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("aging", aging_cmd))
    app.add_handler(CommandHandler("online", online_cmd))
    app.add_handler(CommandHandler("unassigned", unassigned_cmd))
    app.add_handler(CommandHandler("allsite", allsite_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("site", site_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("exit_ai", exit_ai_cmd))
    app.add_handler(CommandHandler("ai_analyze", ai_analyze_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("%s Enterprise Bot starting...", CLIENT_NAME)
    app.run_polling()


if __name__ == "__main__":
    main()
