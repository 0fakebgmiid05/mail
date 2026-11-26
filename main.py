import asyncio
import csv
import html
import io
import json
import logging
import os
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
# CONFIGURATION & PERSISTENT SQLITE STATE
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BaagiJaatEnviroBot")

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
ADMIN_TG_ID: int = int(
    os.getenv("ADMIN_TG_ID", "1390658041").strip() or "1390658041"
)
CLIENT_NAME: str = os.getenv("CLIENT_NAME", "BAAGI JAAT").strip() or "BAAGI JAAT"
DB_PATH: str = os.getenv("BOT_DB_PATH", "baagijaat_telemetry.db").strip()

# Authentic EnviroFRONT / M2M Logger Endpoints
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

# Runtime Admin Settings
BOT_ENABLED: bool = True
MAINTENANCE_MODE: bool = False
MAINTENANCE_MSG: str = (
    "System maintenance chal raha hai. Kripya thodi der baad try karein."
)
REFRESH_INTERVAL_SEC: int = 60
NOTIF_STYLE: str = "detailed"  # detailed | compact | alerts_only

# In-memory states
USER_SESSIONS: dict[int, dict[str, Any]] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}
LAST_PAGE_STATE: dict[int, dict[str, Any]] = {}
SENT_STAGE_ALERTS: set[str] = set()
AI_CHAT_MODE: set[int] = set()
DASHBOARD_CACHE: dict[str, dict[str, Any]] = {}
SITE_NAME_CACHE: dict[str, str] = {}
VAT_CODE_CACHE: dict[str, list[str]] = {}
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
    global BOT_ENABLED, MAINTENANCE_MODE, MAINTENANCE_MSG, REFRESH_INTERVAL_SEC, NOTIF_STYLE
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
            ):
                NOTIF_STYLE = v

        for (stage_key,) in cur.execute("SELECT stage_key FROM sent_alerts"):
            SENT_STAGE_ALERTS.add(stage_key)
        conn.close()
    except Exception as exc:
        logger.warning("DB load_persisted_state failed: %s", exc)


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
# FORMATTING HELPERS (SAFE HTML, SMALL CAPS, PREMIUM FRAMES)
# ==============================================================================
_SMALL_CAPS_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
)


def sc(text: str) -> str:
    return (text or "").upper().translate(_SMALL_CAPS_MAP)


def esc(val: Any) -> str:
    s = str(val or "")
    s = s.replace("<", "&lt;").replace(">", "&gt;")
    return html.escape(s, quote=True)


def strip_html_tags(text: str) -> str:
    txt = re.sub(r"<[^>]+>", "", str(text or ""))
    return html.unescape(txt)


async def safe_send_html(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        logger.warning("HTML send fallback triggered: %s", exc)
        await bot.send_message(
            chat_id=chat_id,
            text=strip_html_tags(text),
            reply_markup=reply_markup,
        )


async def safe_edit_html(msg_obj, text: str) -> None:
    try:
        await msg_obj.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.warning("HTML edit fallback triggered: %s", exc)
        await msg_obj.edit_text(strip_html_tags(text))


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
    dot = small_status_dot(bool(dev.get("is_connected")))
    conn_word = "CONNECTED" if dev.get("is_connected") else "DISCONNECTED"
    off_info = (
        "(Active)"
        if dev.get("is_connected")
        else format_off_since_text(
            dev.get("last_seen"), dev.get("offline_duration")
        )
    )
    prefix = f"#{index} " if index is not None else ""
    sid = f" [Site:{esc(dev['site_id'])}]" if dev.get("site_id") else ""
    scode = (
        f" | Code: <code>{esc(dev['site_code'])}</code>"
        if dev.get("site_code")
        else ""
    )

    if NOTIF_STYLE == "compact":
        return (
            f"▸ {prefix}<b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>{sid}\n"
            f"  Serial: <b>{esc(dev.get('serial_no', 'N/A'))}</b> | {dot} <b>{conn_word}</b>{scode} | "
            f"<code>{esc(dev.get('display_time', 'N/A'))}</code> / <code>{esc(off_info)}</code>"
        )

    return (
        f"╔══════════════════════╗\n"
        f"║  {dot} {prefix}<b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>{sid}\n"
        f"╚══════════════════════╝\n"
        f"  ▸ Serial : <b>{esc(dev.get('serial_no', 'N/A'))}</b>{scode}\n"
        f"  ▸ Status : {dot} <b>{conn_word}</b>\n"
        f"  ▸ Device : <code>{esc(dev.get('device_type', 'N/A'))}</code> | FW: <code>{esc(dev.get('firmware', 'N/A'))}</code>\n"
        f"  ▸ Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code> / <code>{esc(off_info)}</code>"
    )


# ==============================================================================
# REPLY KEYBOARDS ONLY (ZERO INLINE BUTTONS)
# ==============================================================================
def bottom_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("◈ Status"), KeyboardButton("◈ All Sites")],
            [KeyboardButton("∙ Online"), KeyboardButton("⏳ Aging")],
            [KeyboardButton("📐 Instruments"), KeyboardButton("📌 Unassigned")],
            [KeyboardButton("⬅ Previous"), KeyboardButton("Next ➡")],
            [KeyboardButton("🤖 AI Chat"), KeyboardButton("🧠 AI Report")],
            [KeyboardButton("🩺 Health"), KeyboardButton("📥 CSV")],
            [KeyboardButton("❓ Help"), KeyboardButton("🚪 Logout")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def guest_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔐 Login"), KeyboardButton("❓ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
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
    raw_status_text: str,
    now_utc: datetime,
) -> tuple[bool, str, float | None, timedelta | None]:
    st_low = (raw_status_text or "").strip().lower()
    if last_seen_utc is not None:
        delta = now_utc - last_seen_utc
        hours = delta.total_seconds() / 3600.0
        if hours <= 2.0:
            return True, "CONNECTED", round(hours, 2), delta
        return False, "DISCONNECTED", round(hours, 2), delta

    if any(k in st_low for k in ("online", "connected", "active", "live", "true")):
        return True, "CONNECTED", None, None
    return False, "DISCONNECTED", None, None


def normalize_device_record(
    serial_no: str,
    industry: str,
    site_type: str,
    device_type: str,
    firmware: str,
    raw_status: str,
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
            "aaData",
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

    def pick(*keys: str) -> str:
        for k in keys:
            v = item.get(k)
            if v is None:
                v = low_map.get(k.lower())
            if v is not None and str(v).strip() != "":
                return str(v).strip()
        return ""

    serial = pick(
        "SerialNo",
        "serial_no",
        "Serial",
        "DeviceSerial",
        "SerialNumber",
        "IMEI",
        "LoggerId",
        "DeviceId",
    )
    sid = pick("SiteId", "siteId", "Site_Id", "site_id", "ID")
    scode = pick(
        "SiteCode",
        "siteCode",
        "Code",
        "UnitCode",
        "SiteNo",
    )
    ind = (
        pick(
            "IndustryName",
            "Industry",
            "ClientName",
            "CompanyName",
            "SiteName",
            "Name",
        )
        or "N/A"
    )
    stype = pick("SiteType", "Site", "Category", "Location") or "N/A"
    dtype = pick("DeviceType", "Model", "Type", "LoggerType") or "N/A"
    fw = pick("Firmware", "FirmwareVersion", "FW", "Version") or "N/A"
    raw_ts = pick(
        "LastSeen",
        "LastDataTime",
        "LastCommunication",
        "UpdatedOn",
        "LastUpdate",
        "LastPacketTime",
        "DateTime",
    )
    raw_st = pick(
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

    # Fetch pages (rp=50 as captured in network logs)
    for page_num in range(1, 11):
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

            # Stop if fewer than page size returned
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
                    for k in ("data", "Data", "result", "Result", "items", "vatCodes"):
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
            "latest_ts": "",
            "latest_result": "",
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
        "iDisplayLength": "22",
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
            if txt and not txt.startswith("<"):
                parsed = json.loads(txt)
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
    latest_ts = ""
    latest_result = ""

    for item in rows:
        st_val = str(
            item.get("Status")
            or item.get("Result")
            or item.get("Process")
            or ""
        ).lower()
        if any(
            w in st_val
            for w in ("success", "ok", "200", "completed", "approved", "valid")
        ) and not any(
            w in st_val for w in ("fail", "error", "invalid", "reject", "timeout")
        ):
            success_cnt += 1
        elif st_val:
            failed_cnt += 1

        ts_val = str(item.get("Timestamp") or item.get("DateTime") or "").strip()
        res_val = str(
            item.get("Result")
            or item.get("Status")
            or item.get("DecryptedResponse")
            or ""
        ).strip()
        if ts_val and not latest_ts:
            latest_ts = ts_val
        if res_val and not latest_result:
            latest_result = res_val[:80]

    res_obj = {
        "ok": not bool(err_msg),
        "site_id": sid,
        "report_date": rdate,
        "vat_codes": vat_codes,
        "total": len(rows),
        "success": success_cnt,
        "failed": failed_cnt,
        "latest_ts": latest_ts,
        "latest_result": latest_result,
        "rows": rows[:15],
        "error": err_msg,
        "_ts": time.time(),
    }
    API_REPORT_CACHE[cache_key] = res_obj
    return res_obj


def _discover_dashboard_tags_and_page(
    session: requests.Session, site_id: str
) -> tuple[str, str]:
    dash_page_url = f"{BASE_URL}/{UAC}/Site/{site_id}/Dashboard"
    discovered_tags: list[str] = []
    seen_tags: set[str] = set()

    def add_tag(t: str) -> None:
        clean = " ".join(str(t or "").split()).strip()
        if clean and len(clean) <= 40 and clean not in seen_tags:
            seen_tags.add(clean)
            discovered_tags.append(clean)

    try:
        r = session.get(dash_page_url, timeout=20, allow_redirects=True)
        if r.ok and "account/login" not in r.url.lower():
            raw = r.text or ""
            # Look for tags= query param in scripts/URLs
            for m in re.finditer(r"[?&]tags=([^&\"'\s]+)", raw, re.IGNORECASE):
                for part in m.group(1).replace("%2C", ",").split(","):
                    add_tag(part)
            # Look for EST-* or tank/sensor tag patterns in HTML/JS
            for m in re.finditer(r"\b(EST-[A-Za-z0-9\-_]+)\b", raw):
                add_tag(m.group(1))
    except Exception:
        pass

    if not discovered_tags:
        # Default tags observed on EnviroFRONT site dashboard
        for t in (
            "EST-1,EST-1-BL,EST-2,EST-2-BL,EST-3,EST-3-BL,EST-4,EST-4-BL,EST-5,EST-5-BL"
        ).split(","):
            add_tag(t)

    return ",".join(discovered_tags), dash_page_url


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
    seen_pairs: set[str] = set()
    err_msg = ""

    def add_instrument(
        name: str, value: str, unit: str = "", extra: str = ""
    ) -> None:
        n = " ".join((name or "").split()).strip(" :-|")
        v = " ".join((value or "").split()).strip()
        u = " ".join((unit or "").split()).strip()
        if not v:
            return
        if not n:
            n = "Tag / Tank"
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
            if txt and not txt.startswith("<"):
                payload = json.loads(txt)
                # Extract readings from flexible JSON structure
                def walk_readings(obj: Any, ctx_tag: str = "") -> None:
                    if isinstance(obj, dict):
                        tag_lbl = str(
                            obj.get("Tag")
                            or obj.get("TagName")
                            or obj.get("Name")
                            or obj.get("ParameterName")
                            or obj.get("TankName")
                            or obj.get("Label")
                            or ctx_tag
                            or ""
                        ).strip()
                        unit_lbl = str(
                            obj.get("Unit")
                            or obj.get("Units")
                            or obj.get("UOM")
                            or ""
                        ).strip()
                        val_candidate = None
                        for vk in (
                            "LastValue",
                            "CurrentValue",
                            "Value",
                            "Reading",
                            "Level",
                            "Volume",
                            "y",
                        ):
                            if vk in obj and obj.get(vk) is not None and not isinstance(
                                obj.get(vk), (dict, list)
                            ):
                                val_candidate = obj.get(vk)
                                break
                        if val_candidate is not None and str(val_candidate).strip() != "":
                            add_instrument(
                                tag_lbl or "Reading",
                                str(val_candidate),
                                unit_lbl,
                            )
                        for k, v in obj.items():
                            next_ctx = (
                                str(k)
                                if not tag_lbl and isinstance(k, str)
                                else tag_lbl
                            )
                            if isinstance(v, (dict, list)):
                                walk_readings(v, next_ctx)
                    elif isinstance(obj, list):
                        for idx, elem in enumerate(obj):
                            if isinstance(elem, (dict, list)):
                                walk_readings(elem, ctx_tag)
                            elif (
                                isinstance(elem, (int, float))
                                and ctx_tag
                                and idx == len(obj) - 1
                            ):
                                add_instrument(ctx_tag, str(elem))

                walk_readings(payload)
    except Exception as exc:
        err_msg = f"Dashboard WS error: {exc}"

    api_report = fetch_site_api_report(session, site_id)

    res = {
        "ok": not bool(err_msg),
        "site_id": site_id,
        "site_code": site_code,
        "site_name_ws": site_name_ws,
        "url": dash_page_url,
        "instruments": instruments[:25],
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

    # Primary: /Services/EF/WS/Devices?uac=pae&rp=50&page=1...
    ws_devs = fetch_devices_from_ws_api(session, now)
    if ws_devs:
        meta["source"] = "ws-ef-devices"
        meta["unparsed_ts_count"] = sum(
            1 for d in ws_devs if d["last_seen"] is None
        )
        return ws_devs, meta

    # Fallback: /pae/ControlPanel/Devices HTML page
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
            raw_status=" ".join(cells),
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
            "Site Code",
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
                d.get("site_code", ""),
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
# GROQ AI ENGINE WITH LIVE DASHBOARD & API REPORT INJECTION
# ==============================================================================
def get_available_groq_models() -> list[str]:
    preferred = [
        "llama-3.3-70b-versatile",
        "gemma2-9b-it",
        "mixtral-8x7b-32768",
    ]
    if not GROQ_API_KEY:
        return preferred
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=10,
        )
        if r.ok:
            data = r.json().get("data", [])
            live_ids = [
                m.get("id")
                for m in data
                if isinstance(m, dict)
                and m.get("id")
                and "whisper" not in str(m.get("id")).lower()
                and "guard" not in str(m.get("id")).lower()
            ]
            ordered = [m for m in preferred if m in live_ids] + [
                m for m in live_ids if m not in preferred
            ]
            if ordered:
                return ordered[:4]
    except Exception:
        pass
    return preferred


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
        if len(candidates) >= 6:
            break

    for dev in candidates[:6]:
        try:
            dash = scrape_site_dashboard_details(session, dev)
            rep = dash.get("api_report") or {}
            live_readings[dev.get("serial_no", "N/A")] = {
                "industry": dev.get("industry"),
                "site_type": dev.get("site"),
                "site_id": dash.get("site_id") or dev.get("site_id"),
                "site_code": dash.get("site_code") or dev.get("site_code"),
                "instruments": dash.get("instruments", []),
                "api_report_summary": {
                    "date": rep.get("report_date"),
                    "total": rep.get("total", 0),
                    "success": rep.get("success", 0),
                    "failed": rep.get("failed", 0),
                    "latest_result": rep.get("latest_result", ""),
                },
            }
        except Exception:
            continue

    return live_readings


def build_project_context(
    analysis: dict[str, Any],
    live_readings: dict[str, dict[str, Any]] | None = None,
) -> str:
    compact_devices = [
        {
            "serial": d.get("serial_no"),
            "site_id": d.get("site_id"),
            "site_code": d.get("site_code"),
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
        f"Total: {analysis.get('total')} | Assigned: {analysis.get('assigned_count')} | Unassigned N/A: {analysis.get('unassigned_count')}\n"
        f"Connected: {analysis.get('connected')} | Disconnected: {analysis.get('disconnected_count')}\n"
        f"Aging (< Range): {analysis.get('aging_counts')}\n"
        f"Top Offline Sites: {analysis.get('top_offline_sites')}\n"
        f"Live Scraped Dashboard & UP Excise API Report: {json.dumps(live_readings or {}, ensure_ascii=False)}\n"
        f"Devices Sample: {compact_devices}"
    )


def call_groq_ai(
    system_prompt: str, user_prompt: str, analysis: dict[str, Any]
) -> str:
    if not GROQ_API_KEY:
        return "GROQ_API_KEY (.env -> API) set nahi hai."

    models = get_available_groq_models()
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
                    "temperature": 0.3,
                    "max_tokens": 950,
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
                last_err = f"{model} HTTP {r.status_code}: {r.text[:140]}"
        except Exception as exc:
            last_err = f"{model}: {exc}"

    ages = analysis.get("aging_counts", {})
    return (
        f"[Groq Fallback — {last_err}]\n"
        f"Total: {analysis.get('total', 0)} | Connected: {analysis.get('connected', 0)} | "
        f"Disconnected: {analysis.get('disconnected_count', 0)} | Unassigned N/A: {analysis.get('unassigned_count', 0)}\n"
        f"<1D: {ages.get('1d', 0)} | <1W: {ages.get('1w', 0)} | <24D: {ages.get('24d', 0)} | "
        f"<11M: {ages.get('11m', 0)} | <1Y: {ages.get('1y', 0)}"
    )


def generate_ai_analysis(
    session: requests.Session | None, analysis: dict[str, Any]
) -> str:
    live = enrich_context_with_live_dashboards(session, analysis)
    ctx = build_project_context(analysis, live)
    sys_p = (
        f"Tum {CLIENT_NAME} ke EnviroFRONT / M2M telemetry suite ke senior AI engineer ho. "
        "Hinglish mein saaf bullet points mein report do (bina markdown tables ke aur bina ** asterisks ke). "
        "Dashboard readings aur UP Excise API Report status dono ko saaf dikhao."
    )
    usr_p = f"LIVE TELEMETRY & DASHBOARD DATA:\n{ctx}"
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
        f"Tum {CLIENT_NAME} telemetry bot ke AI assistant ho. "
        "Diye gaye LIVE TELEMETRY & DASHBOARD DATA ke basis par Hinglish mein seedha, saaf jawab do. "
        "Markdown table ya ** asterisks mat lagao; clean lines/bullets use karo."
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
            "╔══════════════════════╗\n║  • <b>BOT CURRENTLY OFF</b>   ║\n╚══════════════════════╝\n▸ Admin ne bot ko temporary OFF kiya hua hai.",
        )
        return False
    if MAINTENANCE_MODE:
        await safe_send_html(
            bot,
            chat_id,
            (
                "╔══════════════════════╗\n"
                f"║  ◈ <b>{sc('MAINTENANCE MODE')}</b>  ║\n"
                "╚══════════════════════╝\n"
                f"▸ {esc(MAINTENANCE_MSG)}"
            ),
        )
        return False
    return True


# ==============================================================================
# COMMAND-DRIVEN VIEWS (ZERO INLINE BUTTONS, PREMIUM FRAMES)
# ==============================================================================
async def send_start_view(chat_id: int, bot) -> None:
    existing = require_auth(chat_id)
    if existing:
        msg = (
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{esc(CLIENT_NAME)}</b>\n"
            f"║  <i>{sc('TELEMETRY COMMAND')}</i>\n"
            "╚══════════════════════╝\n"
            f"  ▸ Account   : <code>{esc(existing.get('username'))}</code>\n"
            f"  ▸ Sync Rate : <code>{REFRESH_INTERVAL_SEC}s</code>\n"
            f"  ▸ Alert Tier: <code>2h → 10h → 1d (1-Time)</code>\n"
            f"  ▸ Notif Mode: <code>{esc(NOTIF_STYLE)}</code>\n\n"
            "  ▸ Next/Prev ke liye <code>/next</code> aur <code>/prev</code> ya buttons use karein.\n"
            "  ▸ Dashboard + API Report: <code>/dash &lt;no/serial/name&gt;</code>"
        )
        await safe_send_html(
            bot, chat_id, msg, reply_markup=bottom_reply_keyboard()
        )
        return

    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await safe_send_html(
        bot,
        chat_id,
        (
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{esc(CLIENT_NAME)}</b>\n"
            f"║  <i>{sc('TELEMETRY COMMAND')}</i>\n"
            "╚══════════════════════╝\n\n"
            "  ▸ Kripya apna <b>Login ID / Email</b> bhejein:"
        ),
        reply_markup=guest_reply_keyboard(),
    )


async def send_help_view(chat_id: int, bot) -> None:
    help_text = (
        "╔══════════════════════╗\n"
        f"║  ◈ <b>{sc('COMMANDS & GUIDE')}</b>\n"
        "╚══════════════════════╝\n\n"
        "<b>▸ Session &amp; Pagination</b>\n"
        "  • <code>/start</code> &amp; <code>/help</code>\n"
        "  • <code>/login</code> &amp; <code>/logout</code>\n"
        "  • <code>/next</code> &amp; <code>/prev</code> (list pagination)\n\n"
        "<b>▸ Telemetry, Dashboard &amp; API Report</b>\n"
        "  • <code>/status</code> — Live summary &amp; aging\n"
        "  • <code>/allsite [page]</code> — Assigned sites list\n"
        "  • <code>/dash &lt;no/serial/name&gt;</code> — Dashboard + API Report\n"
        "  • <code>/apireport &lt;no/serial/name&gt; [DD-Mon-YYYY]</code> — Excise API logs\n"
        "  • <code>/instruments [search]</code> — Tank/mm/litr readings\n"
        "  • <code>/online [page]</code> — Connected devices\n"
        "  • <code>/unassigned [page]</code> — Unassigned N/A\n"
        "  • <code>/aging</code> &amp; <code>/age &lt;1d|1w|24d|11m|1y|gt1y&gt;</code>\n"
        "  • <code>/site &lt;query&gt;</code> | <code>/health</code> | <code>/export</code>\n\n"
        "<b>▸ Groq AI</b>\n"
        "  • <code>/ai &lt;question&gt;</code> | <code>/ai_analyze</code> | <code>/exit_ai</code>\n\n"
        f"<b>▸ Admin ({ADMIN_TG_ID})</b>\n"
        "  • <code>/admin</code> | <code>/bot_on</code> | <code>/bot_off</code>\n"
        "  • <code>/maint_on [msg]</code> | <code>/maint_off</code>\n"
        "  • <code>/set_refresh &lt;sec&gt;</code>\n"
        "  • <code>/set_notif &lt;detailed|compact|alerts_only&gt;</code>"
    )
    kb = (
        bottom_reply_keyboard()
        if require_auth(chat_id)
        else guest_reply_keyboard()
    )
    await safe_send_html(bot, chat_id, help_text, reply_markup=kb)


async def send_admin_panel(chat_id: int, bot) -> None:
    msg = (
        "╔══════════════════════╗\n"
        f"║  ◈ <b>{sc('ADMIN SETTINGS')}</b>\n"
        "╚══════════════════════╝\n"
        f"  ▸ Bot Status   : <code>{'ON' if BOT_ENABLED else 'OFF'}</code>\n"
        f"  ▸ Maintenance  : <code>{'ON' if MAINTENANCE_MODE else 'OFF'}</code>\n"
        f"  ▸ Refresh Rate : <code>{REFRESH_INTERVAL_SEC}s</code>\n"
        f"  ▸ Notif Style  : <code>{esc(NOTIF_STYLE)}</code>\n"
        f"  ▸ Active Chats : <code>{len(USER_SESSIONS)}</code>\n\n"
        "<b>Commands:</b>\n"
        "  • <code>/set_refresh 30</code> (ya 60, 120, 300)\n"
        "  • <code>/set_notif detailed | compact | alerts_only</code>\n"
        "  • <code>/maint_on [msg]</code> | <code>/maint_off</code>\n"
        "  • <code>/bot_on</code> | <code>/bot_off</code>"
    )
    await safe_send_html(
        bot, chat_id, msg, reply_markup=bottom_reply_keyboard()
    )


async def send_login_prompt(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await safe_send_html(
        bot,
        chat_id,
        (
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{sc('PORTAL LOGIN')}</b>\n"
            "╚══════════════════════╝\n"
            "  ▸ Kripya apna <b>Login ID / Email</b> bhejein:"
        ),
        reply_markup=guest_reply_keyboard(),
    )


async def send_logout_action(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
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
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{sc('LOGGED OUT')}</b>\n"
            "╚══════════════════════╝\n"
            "  ▸ Session aur alerts band kar diye gaye hain."
        ),
        reply_markup=guest_reply_keyboard(),
    )


async def send_status_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_reply_keyboard(),
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
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)

        if analysis["meta"]["server_error_500"]:
            await safe_edit_html(
                loader,
                "╔══════════════════════╗\n"
                "║  • <b>SERVER HTTP 500</b>   ║\n"
                "╚══════════════════════╝\n"
                "  ▸ Portal ne <code>500 Internal Server Error</code> diya hai.",
            )
            return

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
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{esc(CLIENT_NAME)} — {sc('STATUS')}</b>\n"
            "╚══════════════════════╝\n"
            f"  ▸ Time         : <i>{esc(analysis['checked_at_ist'])}</i>\n"
            f"  ▸ Source       : <code>{esc(analysis['meta'].get('source', 'none'))}</code>\n"
            f"  ▸ Total        : <b>{analysis['total']}</b>\n"
            f"  ▸ Assigned     : <b>{analysis['assigned_count']}</b> | Unassigned: <b>{analysis['unassigned_count']}</b>\n"
            f"  ▸ Connected    : ∙ <b>{analysis['connected']}</b>\n"
            f"  ▸ Disconnected : • <b>{analysis['disconnected_count']}</b>\n\n"
            f"<b>◈ {sc('Aging Buckets (< Range)')}</b>\n"
            f"  ◦ <code>&lt;1D: {ages['1d']}</code> | ▪ <code>&lt;1W: {ages['1w']}</code> | • <code>&lt;24D: {ages['24d']}</code>\n"
            f"  ◆ <code>&lt;11M: {ages['11m']}</code> | ◈ <code>&lt;1Y: {ages['1y']}</code> | ▸ <code>&gt;1Y: {ages['gt1y']}</code>\n\n"
            f"<b>◈ {sc('Top Disconnected Sites')}</b>\n"
            f"{sites_lines}"
        )
        await safe_edit_html(loader, msg)
    except Exception as exc:
        await safe_edit_html(
            loader,
            f"  • <b>Status Error:</b> <code>{esc(exc)}</code>",
        )


async def send_aging_menu(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_reply_keyboard(),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    ages = analysis.get("aging_counts", {})

    msg = (
        "╔══════════════════════╗\n"
        f"║  ◈ <b>{sc('AGING BUCKETS')}</b>\n"
        "╚══════════════════════╝\n"
        f"  ◦ <code>&lt; 1 Day (≤24h)    : {ages.get('1d', 0)}</code> → <code>/age 1d</code>\n"
        f"  ▪ <code>&lt; 1 Week (≤7d)    : {ages.get('1w', 0)}</code> → <code>/age 1w</code>\n"
        f"  • <code>&lt; 24 Days (≤24d)  : {ages.get('24d', 0)}</code> → <code>/age 24d</code>\n"
        f"  ◆ <code>&lt; 11 Months(≤330d): {ages.get('11m', 0)}</code> → <code>/age 11m</code>\n"
        f"  ◈ <code>&lt; 1 Year (≤365d)  : {ages.get('1y', 0)}</code> → <code>/age 1y</code>\n"
        f"  ▸ <code>&gt; 1 Year (365d+)  : {ages.get('gt1y', 0)}</code> → <code>/age gt1y</code>\n\n"
        "  ▸ Bucket dekhne ke liye command bhejein (jaise <code>/age 1d</code>)."
    )
    await safe_send_html(bot, chat_id, msg)


async def send_aging_bucket_page(
    chat_id: int, bucket_key: str, page: int, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_reply_keyboard(),
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
                    "╔══════════════════════╗\n"
                    f"║  ◈ <b>{esc(title)}</b>\n"
                    "╚══════════════════════╝\n"
                    "  ▸ <code>Is range mein koi disconnected site nahi hai.</code>"
                ),
            )
            return

        per_page = 8
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
            "╔══════════════════════╗",
            f"║  ◈ <b>{esc(title)} ({len(matched)})</b>",
            f"║  <i>Page {page + 1}/{total_pages}</i>",
            "╚══════════════════════╝",
        ]
        for offset, d in enumerate(subset):
            lines.append(format_premium_device_block(d, start + offset + 1))

        lines.append(
            f"\n  ▸ Navigation: <code>/prev</code> / <code>/next</code> (Page {page + 1}/{total_pages})"
        )
        await safe_send_html(bot, chat_id, "\n".join(lines))
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
            reply_markup=guest_reply_keyboard(),
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

        per_page = 8
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
            "╔══════════════════════╗",
            f"║  ◈ <b>{sc('ALL SITES')} ({len(assigned_devs)})</b>",
            f"║  <i>Page {page + 1}/{total_pages}</i>",
            "╚══════════════════════╝",
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


async def send_site_dashboard_by_target(
    chat_id: int, target: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Session expire ho gaya hai, dobara login karein (<code>/login</code>):",
            reply_markup=guest_reply_keyboard(),
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
        "╔══════════════════════╗\n"
        f"║  ◈ <b>{esc(dev['industry'])}</b>\n"
        "╚══════════════════════╝\n"
        f"  ▸ Site Type : <code>{esc(dev['site'])}</code>\n"
        f"  ▸ Serial No : <b>{esc(dev['serial_no'])}</b>\n"
        f"  ▸ Site ID   : <code>{esc(dash.get('site_id') or 'N/A')}</code>{code_line}{ws_name_line}\n"
        f"  ▸ Status    : {dot} <b>{conn_word}</b> (<code>{esc(dev['display_time'])}</code>)/(<code>{esc(off_info)}</code>)\n\n"
    )

    if dash.get("instruments"):
        inst_lines = [f"<b>◈ {sc('LIVE DASHBOARD READINGS')}</b>"]
        for i, inst in enumerate(dash["instruments"][:15], start=1):
            u = f" {inst['unit']}" if inst.get("unit") else ""
            inst_lines.append(
                f"  {i}. ▸ <b>{esc(inst['name'])}</b> : <code>{esc(inst['value'])}{esc(u)}</code>"
            )
        inst_section = "\n".join(inst_lines)
    else:
        reason = dash.get("error") or "Dashboard WS mein koi tag value nahi mili."
        inst_section = (
            f"<b>◈ {sc('LIVE DASHBOARD READINGS')}</b>\n"
            f"  • Info      : <code>{esc(reason)}</code>"
        )

    rep = dash.get("api_report") or {}
    vat_list = rep.get("vat_codes") or []
    vat_str = ", ".join(vat_list[:5]) if vat_list else "N/A"
    api_lines = [
        f"\n\n<b>◈ {sc('UP EXCISE API REPORT')} ({esc(rep.get('report_date') or today_ist_report_date())})</b>",
        f"  ▸ VAT Codes : <code>{esc(vat_str)}</code>",
        f"  ▸ Requests  : <b>{rep.get('total', 0)}</b> | Ok: <b>{rep.get('success', 0)}</b> | Err: <b>{rep.get('failed', 0)}</b>",
    ]
    if rep.get("latest_ts"):
        api_lines.append(f"  ▸ Last Req  : <code>{esc(rep.get('latest_ts'))}</code>")
    if rep.get("latest_result"):
        api_lines.append(f"  ▸ Result    : <code>{esc(rep.get('latest_result'))}</code>")
    if rep.get("error"):
        api_lines.append(f"  • Report Note: <code>{esc(rep.get('error'))}</code>")

    await safe_edit_html(loader, header + inst_section + "\n".join(api_lines))


async def send_api_report_action(
    chat_id: int, target: str, report_date: str | None, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_reply_keyboard(),
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

    vat_str = ", ".join(rep.get("vat_codes") or []) or "N/A"
    lines = [
        "╔══════════════════════╗",
        f"║  ◈ <b>{sc('API REPORT')}</b>",
        "╚══════════════════════╝",
        f"  ▸ Site      : <b>{esc(dev['industry'])}</b> (ID: <code>{esc(sid)}</code>)",
        f"  ▸ Date      : <code>{esc(rdate)}</code>",
        f"  ▸ VAT Codes : <code>{esc(vat_str)}</code>",
        f"  ▸ Total Logs: <b>{rep.get('total', 0)}</b> | Ok: <b>{rep.get('success', 0)}</b> | Err: <b>{rep.get('failed', 0)}</b>",
    ]
    for idx, row in enumerate(rep.get("rows", [])[:8], start=1):
        ts = str(row.get("Timestamp") or row.get("DateTime") or "N/A")
        vc = str(row.get("VatCode") or row.get("UnitCode") or "-")
        st = str(row.get("Result") or row.get("Status") or "N/A")[:55]
        lines.append(f"  {idx}. <code>{esc(ts)}</code> [{esc(vc)}] → <code>{esc(st)}</code>")

    if not rep.get("rows"):
        lines.append("  ▸ <code>Is date ke liye koi API request log nahi mila.</code>")

    await safe_edit_html(loader, "\n".join(lines))


async def send_instruments_view(
    chat_id: int, bot, page: int = 0, query: str = ""
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await safe_send_html(
            bot,
            chat_id,
            "  ▸ Pehle login karein (<code>/login</code>):",
            reply_markup=guest_reply_keyboard(),
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
        text="◈ Fetching dashboard tag & instrument readings...",
    )

    blocks: list[str] = [
        "╔══════════════════════╗",
        f"║  ◈ <b>{sc('INSTRUMENTS & TANKS')}</b>",
        f"║  <i>Page {page + 1}/{total_pages}</i>",
        "╚══════════════════════╝",
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
            for item in insts[:6]:
                u = f" {item['unit']}" if item.get("unit") else ""
                blocks.append(
                    f"│  • {esc(item['name'])} : <code>{esc(item['value'])}{esc(u)}</code>"
                )
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
            reply_markup=guest_reply_keyboard(),
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

        per_page = 8
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
            "╔══════════════════════╗",
            f"║  ◈ <b>{sc('CONNECTED')} ({len(online_list)})</b>",
            f"║  <i>Page {page + 1}/{total_pages}</i>",
            "╚══════════════════════╝",
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
            reply_markup=guest_reply_keyboard(),
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

        per_page = 8
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
            "╔══════════════════════╗",
            f"║  ◈ <b>{sc('UNASSIGNED N/A')} ({len(unassigned_list)})</b>",
            f"║  <i>Page {page + 1}/{total_pages}</i>",
            "╚══════════════════════╝",
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
            reply_markup=guest_reply_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        meta = analysis["meta"]

        type_lines = (
            "\n".join(
                f"  ▸ <code>{esc(dt)} : {cnt}</code>"
                for dt, cnt in sorted(
                    analysis["device_type_breakdown"].items(),
                    key=lambda x: -x[1],
                )
            )
            or "  <code>No device type data</code>"
        )
        fw_lines = (
            "\n".join(
                f"  ▸ <code>FW {esc(fw)} : {cnt}</code>"
                for fw, cnt in sorted(
                    analysis["firmware_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  <code>No firmware data</code>"
        )

        msg = (
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{sc('SYSTEM HEALTH')}</b>\n"
            "╚══════════════════════╝\n"
            f"  ▸ Client      : <code>{esc(CLIENT_NAME)}</code>\n"
            f"  ▸ Source      : <code>{esc(meta.get('source', 'none'))}</code>\n"
            f"  ▸ UAC / Sync  : <code>{esc(UAC)}</code> / <code>{REFRESH_INTERVAL_SEC}s</code>\n"
            f"  ▸ HTTP 500    : <code>{'YES' if meta['server_error_500'] else 'NO'}</code>\n"
            f"  ▸ Total / N/A : <b>{analysis['total']} / {analysis['unassigned_count']}</b>\n\n"
            f"<b>◈ {sc('Device Types')}</b>\n{type_lines}\n\n"
            f"<b>◈ {sc('Firmware')}</b>\n{fw_lines}"
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
            reply_markup=guest_reply_keyboard(),
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
            reply_markup=guest_reply_keyboard(),
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
        "╔══════════════════════╗",
        f"║  ◈ <b>{sc('SEARCH RESULT')} ({len(matches)})</b>",
        "╚══════════════════════╝",
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
            reply_markup=guest_reply_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Groq AI live telemetry, dashboard & API report analyze kar raha hai...",
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
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{esc(CLIENT_NAME)} — {sc('AI REPORT')}</b>\n"
            "╚══════════════════════╝\n"
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
            reply_markup=guest_reply_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="◈ Groq AI live data check karke jawab de raha hai...",
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
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{esc(CLIENT_NAME)} — {sc('GROQ AI')}</b>\n"
            "╚══════════════════════╝\n"
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
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    AI_CHAT_MODE.discard(chat_id)
    await send_start_view(chat_id, context.bot)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    await send_help_view(chat_id, context.bot)


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
    await send_aging_menu(chat_id, context.bot)


async def cmd_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    if not await check_access(chat_id, update.effective_user.id, context.bot):
        return
    if not context.args:
        await send_aging_menu(chat_id, context.bot)
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
            reply_markup=guest_reply_keyboard(),
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
    AI_CHAT_MODE.discard(chat_id)
    await safe_send_html(
        context.bot,
        chat_id,
        "  ▸ <b>Groq AI Mode OFF</b>.",
        reply_markup=bottom_reply_keyboard(),
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
    await send_admin_panel(update.effective_chat.id, context.bot)


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
        context.bot, update.effective_chat.id, "  ∙ <b>Bot ON kar diya gaya hai.</b>"
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
        context.bot, update.effective_chat.id, "  • <b>Bot OFF kar diya gaya hai.</b>"
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
        context.bot, update.effective_chat.id, "  ∙ <b>Maintenance Mode OFF.</b>"
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
    ):
        await safe_send_html(
            context.bot,
            update.effective_chat.id,
            "  ▸ Use: <code>/set_notif detailed | compact | alerts_only</code>",
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
# TEXT ROUTER (LOGIN FLOW, REPLY KEYBOARD BUTTONS, AI CHAT)
# ==============================================================================
async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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

    if not await check_access(chat_id, user_id, context.bot):
        return

    sess_data = USER_SESSIONS.get(chat_id)
    if sess_data and sess_data.get("step") == "awaiting_username":
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
        }
        await safe_edit_html(
            wait_msg,
            "╔══════════════════════╗\n"
            f"║  ∙ <b>{esc(CLIENT_NAME)}</b>\n"
            f"║  <i>{sc('AUTHENTICATED')}</i>\n"
            "╚══════════════════════╝\n"
            f"  ▸ Account   : <code>{esc(username)}</code>\n"
            f"  ▸ Sync Rate : <code>{REFRESH_INTERVAL_SEC}s</code>\n"
            f"  ▸ Alerts    : <code>2h → 10h → 1d (1-Time)</code>",
        )
        await safe_send_html(
            context.bot,
            chat_id,
            f"◈ <b>{esc(CLIENT_NAME)} — {sc('PANEL ACTIVE')}</b>",
            reply_markup=bottom_reply_keyboard(),
        )
        return

    btn_map: dict[str, str] = {
        "🔐 login": "login",
        "❓ help": "help",
        "◈ status": "status",
        "◈ all sites": "allsite",
        "∙ online": "online",
        "⏳ aging": "aging",
        "📐 instruments": "instruments",
        "📌 unassigned": "unassigned",
        "⬅ previous": "prev",
        "next ➡": "next",
        "🤖 ai chat": "aichat",
        "🧠 ai report": "aireport",
        "🩺 health": "health",
        "📥 csv": "csv",
        "🚪 logout": "logout",
    }
    action = btn_map.get(raw_text.lower())
    if action == "login":
        await send_login_prompt(chat_id, context.bot)
        return
    if action == "help":
        await send_help_view(chat_id, context.bot)
        return
    if action == "status":
        await send_status_action(chat_id, context.bot)
        return
    if action == "allsite":
        await send_allsite_page(chat_id, 0, context.bot)
        return
    if action == "online":
        await send_online_page(chat_id, 0, context.bot)
        return
    if action == "aging":
        await send_aging_menu(chat_id, context.bot)
        return
    if action == "instruments":
        await send_instruments_view(chat_id, context.bot, page=0, query="")
        return
    if action == "unassigned":
        await send_unassigned_page(chat_id, 0, context.bot)
        return
    if action == "prev":
        await step_pagination(chat_id, -1, context.bot)
        return
    if action == "next":
        await step_pagination(chat_id, +1, context.bot)
        return
    if action == "aichat":
        if not require_auth(chat_id):
            await safe_send_html(
                context.bot,
                chat_id,
                "  ▸ Pehle login karein (<code>/login</code>):",
                reply_markup=guest_reply_keyboard(),
            )
            return
        AI_CHAT_MODE.add(chat_id)
        await safe_send_html(
            context.bot,
            chat_id,
            "  ▸ <b>Groq AI Chat ON</b> — Apna sawal bhejein. Band karne ke liye <code>/exit_ai</code> bhejein.",
        )
        return
    if action == "aireport":
        await send_ai_analyze_action(chat_id, context.bot)
        return
    if action == "health":
        await send_health_action(chat_id, context.bot)
        return
    if action == "csv":
        await send_export_csv_action(chat_id, context.bot)
        return
    if action == "logout":
        await send_logout_action(chat_id, context.bot)
        return

    if chat_id in AI_CHAT_MODE:
        await send_ai_answer_action(chat_id, raw_text, context.bot)
        return

    await safe_send_html(
        context.bot,
        chat_id,
        "  ▸ Command ya button use karein. Poori list ke liye <code>/help</code> bhejein.",
        reply_markup=(
            bottom_reply_keyboard()
            if require_auth(chat_id)
            else guest_reply_keyboard()
        ),
    )


# ==============================================================================
# BACKGROUND MONITOR: 2H -> 10H -> 1D (1-TIME PER STAGE) + AUTO-RECOVERY
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
                except Exception as exc:
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

                for dev in analysis.get("all_devices", []):
                    serial = dev.get("serial_no", "N/A")
                    is_conn = bool(dev.get("is_connected"))
                    off_hrs = dev.get("offline_hours")

                    if is_conn:
                        if prev_conn_map.get(serial) is False:
                            clear_stage_alerts_for_device(chat_id, serial)
                            if NOTIF_STYLE != "alerts_only":
                                await safe_send_html(
                                    app.bot,
                                    chat_id,
                                    (
                                        "╔══════════════════════╗\n"
                                        f"║  ∙ <b>{sc('SITE RECOVERED')}</b>     ║\n"
                                        "╚══════════════════════╝\n"
                                        f"  ▸ Site   : <b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>\n"
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
                            await safe_send_html(
                                app.bot,
                                chat_id,
                                (
                                    "╔══════════════════════╗\n"
                                    f"║  • <b>{esc(stage_badge)} ALERT</b>  ║\n"
                                    "╚══════════════════════╝\n"
                                    f"  ▸ Site   : <b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>\n"
                                    f"  ▸ Serial : <b>{esc(serial)}</b>\n"
                                    f"  ▸ Status : <code>{esc(off_str)}</code>\n"
                                    f"  ▸ Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code>"
                                ),
                            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Monitor loop unexpected error: %s", exc)


async def post_init(app) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start & login / panel"),
            BotCommand("help", "All commands & guide"),
            BotCommand("status", "Live status & aging summary"),
            BotCommand("allsite", "Assigned sites list"),
            BotCommand("dash", "Dashboard + API report (<no/serial/name>)"),
            BotCommand("apireport", "UP Excise API report (<site> [date])"),
            BotCommand("instruments", "Tank / mm / litr readings"),
            BotCommand("online", "Connected devices"),
            BotCommand("unassigned", "Unassigned N/A devices"),
            BotCommand("aging", "Aging buckets menu"),
            BotCommand("age", "Filter bucket (<1d|1w|24d|11m|1y|gt1y>)"),
            BotCommand("next", "Next page"),
            BotCommand("prev", "Previous page"),
            BotCommand("ai_analyze", "Groq AI telemetry & API report"),
            BotCommand("ai", "Ask Groq AI (<question>)"),
            BotCommand("health", "System & parser health"),
            BotCommand("export", "Export devices CSV"),
            BotCommand("admin", "Admin settings"),
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
    app.add_handler(CommandHandler("bot_on", cmd_bot_on))
    app.add_handler(CommandHandler("bot_off", cmd_bot_off))
    app.add_handler(CommandHandler("maint_on", cmd_maint_on))
    app.add_handler(CommandHandler("maint_off", cmd_maint_off))
    app.add_handler(CommandHandler("set_refresh", cmd_set_refresh))
    app.add_handler(CommandHandler("set_notif", cmd_set_notif))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    logger.info("Baagi Jaat EnviroFRONT Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
