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
GEMINI_API_KEY = "AQ.Ab8RN6LKHe0Rp-B5loqNfPLVz32jwQgnFCorUoYe2kGX09U4PA"

BASE_URL = "https://envirofront.m2mlogger.com"
LOGIN_URL = f"{BASE_URL}/?ReturnUrl=%2Fpae%2FControlPanel%2FDevices"
DEVICES_URL = f"{BASE_URL}/pae/ControlPanel/Devices"
WS_DEVICES_URL = f"{BASE_URL}/Services/EF/WS/Devices"

DB_PATH = "baggi_jaat_telemetry.db"
IST = timezone(timedelta(hours=5, minutes=30))
DISCONNECT_THRESHOLD = timedelta(hours=5)
CHECK_INTERVAL_SECONDS = 60  # Har 1 minute mein auto-refresh
ALERT_COOLDOWN = timedelta(hours=6)

USER_SESSIONS: dict[int, dict[str, Any]] = {}
ALERTED_DEVICES: dict[str, datetime] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}
AI_CHAT_MODE: set[int] = set()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BaggiJaat.EnviroFRONT")


# ==============================================================================
# SQLITE PERSISTENCE
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
        CREATE TABLE IF NOT EXISTS alert_log (
            alert_key TEXT PRIMARY KEY,
            last_alerted_ts REAL
        )
        """
    )
    conn.commit()
    conn.close()


def save_snapshot_db(chat_id: int, analysis: dict[str, Any]) -> None:
    try:
        serializable = {
            "checked_at_ist": analysis.get("checked_at_ist"),
            "total": analysis.get("total", 0),
            "assigned_count": analysis.get("assigned_count", 0),
            "unassigned_count": analysis.get("unassigned_count", 0),
            "connected": analysis.get("connected", 0),
            "disconnected_5h_count": analysis.get("disconnected_5h_count", 0),
            "top_offline_sites": analysis.get("top_offline_sites", []),
            "firmware_breakdown": analysis.get("firmware_breakdown", {}),
            "device_type_breakdown": analysis.get("device_type_breakdown", {}),
            "meta": analysis.get("meta", {}),
            "all_devices": [
                {
                    k: v
                    for k, v in d.items()
                    if k not in ("last_seen", "offline_duration")
                }
                | {
                    "offline_hours": round(
                        d["offline_duration"].total_seconds() / 3600, 2
                    )
                    if d.get("offline_duration")
                    else None
                }
                for d in analysis.get("all_devices", [])
            ],
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
    except Exception as exc:
        logger.warning("DB snapshot save failed: %s", exc)


def save_alert_key_db(alert_key: str, dt_utc: datetime) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO alert_log (alert_key, last_alerted_ts)
            VALUES (?, ?)
            ON CONFLICT(alert_key) DO UPDATE SET last_alerted_ts = excluded.last_alerted_ts
            """,
            (alert_key, dt_utc.timestamp()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("DB alert save failed: %s", exc)


def delete_alert_key_db(alert_key: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM alert_log WHERE alert_key = ?", (alert_key,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def load_alert_keys_db() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT alert_key, last_alerted_ts FROM alert_log")
        for k, ts in cur.fetchall():
            ALERTED_DEVICES[str(k)] = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        conn.close()
    except Exception:
        pass


# ==============================================================================
# HELPERS & KEYBOARDS (INLINE SUB-MENUS + REPLY KEYBOARD)
# ==============================================================================
def esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def is_unassigned_value(val: str) -> bool:
    v = (val or "").strip().lower()
    return v in ("", "n/a", "na", "none", "null", "unassigned", "-")


def is_unassigned_device(industry: str, site: str) -> bool:
    return is_unassigned_value(industry) and is_unassigned_value(site)


def main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Status", callback_data="act:status"),
                InlineKeyboardButton("🌐 All Sites", callback_data="act:allsite"),
            ],
            [
                InlineKeyboardButton("🟢 Online", callback_data="act:online"),
                InlineKeyboardButton("⏳ Aging Menus", callback_data="act:aging_menu"),
            ],
            [
                InlineKeyboardButton("📌 Unassigned N/A", callback_data="act:unassigned"),
                InlineKeyboardButton("🩺 Health", callback_data="act:health"),
            ],
            [
                InlineKeyboardButton("🤖 AI Chat", callback_data="act:ai_mode"),
                InlineKeyboardButton("🧠 AI Report", callback_data="act:ai_analyze"),
            ],
            [
                InlineKeyboardButton("📥 Export CSV", callback_data="act:export_csv"),
                InlineKeyboardButton("🚪 Logout", callback_data="act:logout"),
            ],
        ]
    )


def aging_submenu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🗓️ > 1 Year", callback_data="age:1y"),
                InlineKeyboardButton("📆 > 11 Months", callback_data="age:11m"),
            ],
            [
                InlineKeyboardButton("📅 > 24 Days", callback_data="age:24d"),
                InlineKeyboardButton("🗓 > 1 Week", callback_data="age:1w"),
            ],
            [
                InlineKeyboardButton("⏰ > 1 Day", callback_data="age:1d"),
                InlineKeyboardButton("🔴 > 5 Hours", callback_data="age:5h"),
            ],
            [
                InlineKeyboardButton("⬅️ Back to Main", callback_data="act:main_menu"),
            ],
        ]
    )


def guest_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔐 Login EnviroFRONT", callback_data="act:login")],
        ]
    )


def bottom_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("📊 Status"),
                KeyboardButton("🌐 All Sites"),
            ],
            [
                KeyboardButton("🟢 Online"),
                KeyboardButton("⏳ Aging Menus"),
            ],
            [
                KeyboardButton("📌 Unassigned N/A"),
                KeyboardButton("🩺 Health"),
            ],
            [
                KeyboardButton("🤖 AI Chat"),
                KeyboardButton("🧠 AI Report"),
            ],
            [
                KeyboardButton("📥 Export CSV"),
                KeyboardButton("🚪 Logout"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def animate_message(
    msg, frames: list[str], delay: float = 0.3, final_markup=None
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
# ENVIROFRONT AUTH & PARSER
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
            "UserName": username,
            "Password": password,
            "RememberMe": "false",
        }
        if token:
            payload["__RequestVerificationToken"] = token

        post_resp = session.post(
            LOGIN_URL,
            data=payload,
            timeout=25,
            allow_redirects=False,
        )

        cookies_dict = session.cookies.get_dict()
        has_auth_cookie = any(
            k.upper().startswith(".ASPXAUTH") or "AUTH" in k.upper()
            for k in cookies_dict
        )
        loc = post_resp.headers.get("Location", "")

        if post_resp.status_code in (301, 302, 303) and (
            has_auth_cookie or "/pae/ControlPanel/Devices" in loc or loc
        ):
            target = urljoin(BASE_URL, loc or "/pae/ControlPanel/Devices")
            verify = session.get(target, timeout=25, allow_redirects=True)
            if (
                verify.ok
                and "Forgot Password?" not in verify.text
                and "UserName" not in verify.text
            ):
                return session, "OK"
            return None, "Login page wapas aaya — ID ya Password galat ho sakta hai."

        if post_resp.status_code == 200 and has_auth_cookie:
            verify = session.get(DEVICES_URL, timeout=25, allow_redirects=True)
            if verify.ok and "Forgot Password?" not in verify.text:
                return session, "OK"

        return None, f"Auth reject hua (HTTP {post_resp.status_code}). Credentials check karein."
    except requests.Timeout:
        return None, "EnviroFRONT server timeout ho gaya. Dobara try karein."
    except Exception as exc:
        logger.error("Login exception: %s", exc)
        return None, f"Network/Login error: {exc}"


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


def format_status_with_time(is_connected: bool, display_time: str) -> str:
    if is_connected:
        return f"🟢 <code>ONLINE ({esc(display_time)})</code>"
    return f"🔴 <code>DISCONNECTED ({esc(display_time)})</code>"


def build_device_record(
    serial_no: str,
    industry: str,
    site: str,
    device_type: str,
    firmware: str,
    status_text: str,
    ts_raw: str,
    now: datetime,
) -> dict[str, Any]:
    s_clean = (serial_no or "N/A").strip() or "N/A"
    ind_clean = "N/A" if is_unassigned_value(industry) else industry.strip()
    site_clean = "N/A" if is_unassigned_value(site) else site.strip()
    type_clean = (device_type or "LOGPRO PLUS").strip() or "LOGPRO PLUS"
    fw_clean = (firmware or "N/A").strip() or "N/A"
    st_clean = (status_text or "").strip()
    ts_clean = (ts_raw or "").strip()

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
        is_connected = offline_td <= DISCONNECT_THRESHOLD
    else:
        is_connected = False

    is_offline_5h = bool(
        (offline_td is not None and offline_td > DISCONNECT_THRESHOLD)
        or (not is_connected and offline_td is None)
    )

    display_ts = format_display_time(last_seen, ts_clean)
    unassigned_flag = is_unassigned_device(ind_clean, site_clean)

    return {
        "serial_no": s_clean,
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
        "offline_duration": offline_td,
        "disconnected_5h": is_offline_5h,
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

    serial_no = pick(
        "serialno",
        "serial_no",
        "serial",
        "deviceserial",
        "device_serial",
        "mac",
        "id",
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
    )


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
            )
        )

    return devices, len(all_trs), unparsed_ts


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

    _, tr_total, _ = extract_devices_from_html_table(soup, now)
    meta["tr_total"] = tr_total

    ws_devices = extract_devices_from_ws(session, now)
    if ws_devices:
        meta["unparsed_ts_count"] = sum(
            1 for d in ws_devices if d["last_seen"] is None
        )
        meta["source"] = "ws-devices-json"
        return ws_devices, meta

    html_devices, _, unparsed_ts = extract_devices_from_html_table(soup, now)
    if len(html_devices) > 1:
        meta["unparsed_ts_count"] = unparsed_ts
        meta["source"] = "html-table"
        return html_devices, meta

    meta["source"] = "0-found"
    return [], meta


# ==============================================================================
# DURATION BUCKETS & TELEMETRY ANALYSIS
# ==============================================================================
AGING_CONFIG: dict[str, tuple[str, timedelta]] = {
    "1y": ("DISCONNECTED > 1 YEAR (365d+)", timedelta(days=365)),
    "11m": ("DISCONNECTED > 11 MONTHS (330d+)", timedelta(days=330)),
    "24d": ("DISCONNECTED > 24 DAYS (24d+)", timedelta(days=24)),
    "1w": ("DISCONNECTED > 1 WEEK (7d+)", timedelta(days=7)),
    "1d": ("DISCONNECTED > 1 DAY (24h+)", timedelta(days=1)),
    "5h": ("DISCONNECTED > 5 HOURS (5h+)", timedelta(hours=5)),
}


def filter_disconnected_by_delta(
    devices: list[dict[str, Any]], min_delta: timedelta
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for d in devices:
        if d.get("is_connected"):
            continue
        off_td: timedelta | None = d.get("offline_duration")
        if off_td is not None and off_td >= min_delta:
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
    offline_5h_list = [d for d in devices if d["disconnected_5h"]]

    offline_5h_list.sort(
        key=lambda x: x["offline_duration"].total_seconds()
        if x["offline_duration"]
        else 99999999,
        reverse=True,
    )

    aging_counts = {
        key: len(filter_disconnected_by_delta(devices, delta))
        for key, (_, delta) in AGING_CONFIG.items()
    }

    site_offline_counts: dict[str, int] = {}
    fw_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}

    for d in devices:
        fw = d["firmware"] or "Unknown"
        fw_counts[fw] = fw_counts.get(fw, 0) + 1

        dtype = d["device_type"] or "Unknown"
        type_counts[dtype] = type_counts.get(dtype, 0) + 1

        if d["disconnected_5h"] and not d["is_unassigned"]:
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
        "disconnected_5h_count": len(offline_5h_list),
        "unassigned_count": len(unassigned_list),
        "aging_counts": aging_counts,
        "all_devices": devices,
        "assigned_devices": assigned_devices,
        "online_list": online_list,
        "disconnected_5h_list": offline_5h_list,
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
            "Industry Name",
            "Site Type",
            "Assignment Status",
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
# AI ENGINE (FIXED MODELS + DIAGNOSTIC + SMART LOCAL ANALYZER)
# ==============================================================================
def build_smart_local_report(analysis: dict[str, Any], query: str = "") -> str:
    ages = analysis.get("aging_counts", {})
    top_sites = ", ".join(f"{s} ({c})" for s, c in analysis.get("top_offline_sites", [])) or "None"
    fw_txt = ", ".join(f"{k}:{v}" for k, v in analysis.get("firmware_breakdown", {}).items()) or "N/A"
    type_txt = ", ".join(f"{k}:{v}" for k, v in analysis.get("device_type_breakdown", {}).items()) or "N/A"

    base = (
        f"[{CLIENT_NAME} Telemetry Report]\n"
        f"Total: {analysis.get('total', 0)} | Online: {analysis.get('connected', 0)} | "
        f"Offline >5h: {analysis.get('disconnected_5h_count', 0)} | Unassigned N/A: {analysis.get('unassigned_count', 0)}\n"
        f"Aging -> >1Y: {ages.get('1y', 0)} | >11M: {ages.get('11m', 0)} | "
        f">24D: {ages.get('24d', 0)} | >1W: {ages.get('1w', 0)} | >1D: {ages.get('1d', 0)}\n"
        f"Top Offline Sites: {top_sites}\n"
        f"Device Types: {type_txt}\n"
        f"Firmware: {fw_txt}"
    )

    if not query:
        return base

    q = query.lower()
    devs = analysis.get("all_devices", [])
    matches = [
        d
        for d in devs
        if q in str(d.get("industry", "")).lower()
        or q in str(d.get("site", "")).lower()
        or q in str(d.get("serial_no", "")).lower()
        or q in str(d.get("device_type", "")).lower()
        or q in str(d.get("firmware", "")).lower()
    ]
    if matches:
        m_lines = [
            f"{i+1}. {m.get('industry')} ({m.get('site')}) | {m.get('serial_no')} | {m.get('conn_label')} ({m.get('display_time')})"
            for i, m in enumerate(matches[:8])
        ]
        return f"Matched ({len(matches)}) for '{query}':\n" + "\n".join(m_lines)

    return base + f"\nNote: '{query}' ke direct matches nahi mile, overall summary upar hai."


def call_gemini_ai(prompt: str, analysis: dict[str, Any], query: str = "") -> str:
    local_fallback = build_smart_local_report(analysis, query=query)
    if not GEMINI_API_KEY:
        return local_fallback

    models_to_try = ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash")
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    last_err = ""

    for m_name in models_to_try:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent",
                headers={
                    "Content-Type": "application/json",
                    "X-goog-api-key": GEMINI_API_KEY,
                },
                json=body,
                timeout=25,
            )
            if r.ok:
                data = r.json()
                parts = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                txt = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
                if txt:
                    return txt
            else:
                try:
                    err_msg = r.json().get("error", {}).get("message", r.text[:160])
                except Exception:
                    err_msg = r.text[:160]
                last_err = f"{m_name}: HTTP {r.status_code} ({err_msg})"
        except Exception as exc:
            last_err = f"{m_name}: {exc}"

    logger.warning("Gemini fallback used. Last error: %s", last_err)
    status_note = (
        f"[Gemini API Status: {last_err}]\n\n" if last_err else ""
    )
    return status_note + local_fallback


def build_project_context(analysis: dict[str, Any]) -> str:
    compact_devices = [
        {
            "serial": d.get("serial_no"),
            "industry": d.get("industry"),
            "site_type": d.get("site"),
            "unassigned": d.get("is_unassigned"),
            "device_type": d.get("device_type"),
            "fw": d.get("firmware"),
            "status": d.get("conn_label"),
            "offline_hours": d.get("offline_hours")
            if "offline_hours" in d
            else (
                round(d["offline_duration"].total_seconds() / 3600, 1)
                if d.get("offline_duration")
                else None
            ),
            "last_seen": d.get("display_time"),
        }
        for d in analysis.get("all_devices", [])[:50]
    ]
    return (
        f"Client: {CLIENT_NAME}\n"
        f"Checked At: {analysis.get('checked_at_ist')}\n"
        f"Total Devices: {analysis.get('total')}\n"
        f"Assigned Sites: {analysis.get('assigned_count')}\n"
        f"Unassigned (N/A): {analysis.get('unassigned_count')}\n"
        f"Online: {analysis.get('connected')}\n"
        f"Offline >5h: {analysis.get('disconnected_5h_count')}\n"
        f"Aging Counts: {analysis.get('aging_counts')}\n"
        f"Top Offline Sites: {analysis.get('top_offline_sites')}\n"
        f"Device Types: {analysis.get('device_type_breakdown')}\n"
        f"Firmware: {analysis.get('firmware_breakdown')}\n"
        f"Devices (sample): {compact_devices}"
    )


def generate_ai_analysis(analysis: dict[str, Any]) -> str:
    ctx = build_project_context(analysis)
    prompt = (
        f"Tum {CLIENT_NAME} ke EnviroFRONT telemetry project ke senior AI engineer ho. "
        "Hinglish mein crisp, actionable analysis do:\n"
        "1. System Health & Risk\n"
        "2. Critical Offline Sites & Long-Disconnected Buckets (>1Y, >11M, >24D, >1W, >1D)\n"
        "3. Unassigned (N/A) Sites, Device Types & Firmware\n"
        "4. Action Checklist\n\n"
        f"{ctx}"
    )
    return call_gemini_ai(prompt, analysis)


def answer_project_question(user_question: str, analysis: dict[str, Any]) -> str:
    ctx = build_project_context(analysis)
    prompt = (
        f"Tum {CLIENT_NAME} EnviroFRONT telemetry bot ke AI assistant ho. "
        "Live telemetry data ke basis par Hinglish mein seedha aur accurate jawab do.\n\n"
        f"LIVE DATA:\n{ctx}\n\n"
        f"QUESTION:\n{user_question}"
    )
    return call_gemini_ai(prompt, analysis, query=user_question)


def require_auth(chat_id: int) -> dict[str, Any] | None:
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        return None
    return sess_data


# ==============================================================================
# ACTION RUNNERS
# ==============================================================================
async def send_start_view(chat_id: int, send_fn) -> None:
    existing = require_auth(chat_id)
    if existing:
        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — TELEMETRY SUITE</b>\n"
            "✨ <i>Enterprise Active Session</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>Account:</b> <code>{esc(existing.get('username'))}</code>\n"
            "🔄 <b>Auto-Refresh:</b> <code>EVERY 1 MINUTE</code>\n"
            "🔔 <b>Alerts:</b> <code>GROUPED + RECONNECT</code>\n\n"
            "<i>Neeche buttons ya sub-menus se chalayein:</i>"
        )
        await send_fn(
            msg, parse_mode=ParseMode.HTML, reply_markup=main_inline_keyboard()
        )
        await send_fn(
            "🎛️ <b>Control Panel Ready</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=bottom_reply_keyboard(),
        )
        return

    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    intro = await send_fn(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 <b>{esc(CLIENT_NAME)} — TELEMETRY SUITE</b>\n"
        "<i>Initializing Console...</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
        reply_markup=guest_inline_keyboard(),
    )
    await animate_message(
        intro,
        [
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — TELEMETRY SUITE</b>\n"
            "<i>Loading modules [■■□□□]</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(CLIENT_NAME)} — TELEMETRY SUITE</b>\n"
            "<i>Loading modules [■■■■■]</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔐 Kripya apna <b>Login ID / Email</b> bhejein:",
        ],
        delay=0.3,
        final_markup=guest_inline_keyboard(),
    )


async def send_login_prompt(chat_id: int, send_fn) -> None:
    AI_CHAT_MODE.discard(chat_id)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await send_fn(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔐 <b>{esc(CLIENT_NAME)} — LOGIN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Kripya apna <b>Login ID / Email</b> bhejein:",
        parse_mode=ParseMode.HTML,
        reply_markup=guest_inline_keyboard(),
    )


async def send_logout_action(chat_id: int, send_fn) -> None:
    AI_CHAT_MODE.discard(chat_id)
    sess_data = USER_SESSIONS.pop(chat_id, None)
    LAST_SNAPSHOT.pop(chat_id, None)

    for key in [k for k in ALERTED_DEVICES if k.startswith(f"{chat_id}:")]:
        ALERTED_DEVICES.pop(key, None)
        delete_alert_key_db(key)

    if sess_data and sess_data.get("session"):
        try:
            sess_data["session"].close()
        except Exception:
            pass

    status_msg = await send_fn(
        "<i>Session close ho raha hai...</i>", parse_mode=ParseMode.HTML
    )
    await animate_message(
        status_msg,
        [
            "<i>Session close ho raha hai [■■□□]</i>",
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🚪 <b>LOGGED OUT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Session aur alerts band kar diye gaye hain.\n"
            "Dobara login ke liye neeche button dabayein:",
        ],
        delay=0.3,
        final_markup=guest_inline_keyboard(),
    )


async def send_aging_menu(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        ages = analysis.get("aging_counts", {})
    except Exception:
        ages = {}

    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ <b>DISCONNECT DURATION SUB-MENU</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<code>&gt; 1 Year (365d+)  : {ages.get('1y', 0)}</code>\n"
        f"<code>&gt; 11 Months(330d+): {ages.get('11m', 0)}</code>\n"
        f"<code>&gt; 24 Days (24d+)  : {ages.get('24d', 0)}</code>\n"
        f"<code>&gt; 1 Week (7d+)    : {ages.get('1w', 0)}</code>\n"
        f"<code>&gt; 1 Day (24h+)    : {ages.get('1d', 0)}</code>\n"
        f"<code>&gt; 5 Hours (5h+)   : {ages.get('5h', 0)}</code>\n\n"
        "<i>Kisi bhi duration button par tap karke list dekhein:</i>"
    )
    await send_fn(
        msg, parse_mode=ParseMode.HTML, reply_markup=aging_submenu_keyboard()
    )


async def send_aging_bucket_list(chat_id: int, bucket_key: str, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    title, min_delta = AGING_CONFIG.get(
        bucket_key, ("DISCONNECTED > 5 HOURS (5h+)", timedelta(hours=5))
    )

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        matched = filter_disconnected_by_delta(analysis["all_devices"], min_delta)

        if not matched:
            await send_fn(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ <b>{esc(title)}</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<code>Is duration mein koi disconnected site nahi hai.</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=aging_submenu_keyboard(),
            )
            return

        entries: list[str] = []
        for idx, d in enumerate(matched, start=1):
            entries.append(
                f"{idx}. {format_industry_with_type(d['industry'], d['site'])} | "
                f"<code>{esc(d['serial_no'])}</code>\n"
                f"   {format_status_with_time(False, d['display_time'])}"
            )

        chunk: list[str] = []
        sent_parts = 0
        for line in entries:
            chunk.append(line)
            if len("\n".join(chunk)) > 3300:
                hdr = (
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⏳ <b>{esc(title)} ({len(matched)})</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    if sent_parts == 0
                    else ""
                )
                await send_fn(hdr + "\n".join(chunk), parse_mode=ParseMode.HTML)
                sent_parts += 1
                chunk = []

        hdr = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>{esc(title)} ({len(matched)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            if sent_parts == 0
            else ""
        )
        await send_fn(
            hdr + "\n".join(chunk),
            parse_mode=ParseMode.HTML,
            reply_markup=aging_submenu_keyboard(),
        )
    except Exception as exc:
        await send_fn(
            f"❌ <b>Bucket Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=aging_submenu_keyboard(),
        )


async def send_status_action(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    loader = await send_fn(
        "<i>📡 Fetching telemetry status...</i>", parse_mode=ParseMode.HTML
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
                f"  {i}. <code>{esc(s)} — {c} disconnected</code>"
                for i, (s, c) in enumerate(analysis["top_offline_sites"], start=1)
            )
            if analysis["top_offline_sites"]
            else "  ✨ <code>Koi critical offline site nahi</code>"
        )
        ages = analysis["aging_counts"]

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>{esc(CLIENT_NAME)} — STATUS</b>\n"
            f"🕒 <i>{esc(analysis['checked_at_ist'])}</i> (1m auto-sync)\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 <b>Total Devices:</b> <code>{analysis['total']}</code>\n"
            f"🏭 <b>Assigned Sites:</b> <code>{analysis['assigned_count']}</code>\n"
            f"📌 <b>Unassigned (N/A):</b> <code>{analysis['unassigned_count']}</code>\n"
            f"🟢 <b>Online:</b> <code>{analysis['connected']}</code>\n"
            f"🔴 <b>Disconnected (&gt;5h):</b> <code>{analysis['disconnected_5h_count']}</code>\n\n"
            "<b>Aging Buckets</b>\n"
            f"<code>&gt;1Y: {ages['1y']} | &gt;11M: {ages['11m']} | &gt;24D: {ages['24d']} | &gt;1W: {ages['1w']} | &gt;1D: {ages['1d']}</code>\n\n"
            "<b>Priority Offline (&gt;5h)</b>\n"
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


async def send_online_action(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        online_list = analysis["online_list"]

        if not online_list:
            await send_fn(
                "🟡 <code>Koi online device nahi mila.</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🟢 <b>ONLINE DEVICES ({len(online_list)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for idx, d in enumerate(online_list[:25], start=1):
            lines.append(
                f"{idx}. {format_industry_with_type(d['industry'], d['site'])} | "
                f"<code>{esc(d['serial_no'])}</code>\n"
                f"   {format_status_with_time(True, d['display_time'])}"
            )
        if len(online_list) > 25:
            lines.append(f"\n<code>...aur {len(online_list) - 25} online devices hain.</code>")

        await send_fn(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await send_fn(
            f"❌ <b>Online Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_unassigned_action(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        unassigned_list = analysis["unassigned_list"]

        if not unassigned_list:
            await send_fn(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📌 <b>UNASSIGNED SITES (N/A)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<code>Koi N/A unassigned device nahi mila.</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        formatted_entries: list[str] = []
        for idx, d in enumerate(unassigned_list, start=1):
            formatted_entries.append(
                f"{idx}. <code>{esc(d['serial_no'])} | Type: {esc(d['site'])} | {esc(d['device_type'])}</code>\n"
                f"   <code>In par abhi site assign nahi hai</code> — "
                f"{format_status_with_time(d['is_connected'], d['display_time'])}"
            )

        chunk_lines: list[str] = []
        sent_chunks = 0
        for line in formatted_entries:
            chunk_lines.append(line)
            if len("\n".join(chunk_lines)) > 3300:
                header = (
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>UNASSIGNED N/A — SITE ASSIGN NAHI HAI ({len(unassigned_list)})</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    if sent_chunks == 0
                    else ""
                )
                await send_fn(
                    header + "\n".join(chunk_lines),
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_inline_keyboard() if sent_chunks == 0 else None,
                )
                sent_chunks += 1
                chunk_lines = []

        header = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <b>UNASSIGNED N/A — SITE ASSIGN NAHI HAI ({len(unassigned_list)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            if sent_chunks == 0
            else ""
        )
        await send_fn(
            header + "\n".join(chunk_lines),
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await send_fn(
            f"❌ <b>Unassigned Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_allsite_action(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    loader = await send_fn(
        "<i>🌐 Assigned sites load ho rahi hain...</i>",
        parse_mode=ParseMode.HTML,
    )
    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        assigned_devs = analysis["assigned_devices"]

        if not assigned_devs:
            await loader.edit_text(
                "<code>Koi assigned site nahi mili.</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        formatted_entries: list[str] = []
        for idx, d in enumerate(assigned_devs, start=1):
            logger_name = (
                d["serial_no"]
                if d["device_type"] == "LOGPRO PLUS"
                else f"{d['device_type']} ({d['serial_no']})"
            )
            formatted_entries.append(
                f"{idx}. {format_industry_with_type(d['industry'], d['site'])} — "
                f"<code>{esc(logger_name)}</code> — "
                f"{format_status_with_time(d['is_connected'], d['display_time'])}"
            )

        chunk_lines: list[str] = []
        sent_chunks = 0

        for line in formatted_entries:
            chunk_lines.append(line)
            if len("\n".join(chunk_lines)) > 3300:
                header = (
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🌐 <b>ALL ASSIGNED SITES ({len(assigned_devs)})</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    if sent_chunks == 0
                    else ""
                )
                if sent_chunks == 0:
                    await loader.edit_text(
                        header + "\n".join(chunk_lines), parse_mode=ParseMode.HTML
                    )
                else:
                    await send_fn("\n".join(chunk_lines), parse_mode=ParseMode.HTML)
                sent_chunks += 1
                chunk_lines = []

        header = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 <b>ALL ASSIGNED SITES ({len(assigned_devs)})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            if sent_chunks == 0
            else ""
        )
        if sent_chunks == 0:
            await loader.edit_text(
                header + "\n".join(chunk_lines),
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
        else:
            await send_fn(
                "\n".join(chunk_lines),
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
    except Exception as exc:
        await loader.edit_text(
            f"❌ <b>AllSite Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_health_action(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        meta = analysis["meta"]

        type_lines = (
            "\n".join(
                f"  <code>{esc(dt)} : {cnt} devices</code>"
                for dt, cnt in sorted(
                    analysis["device_type_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  <code>No device type data</code>"
        )

        fw_lines = (
            "\n".join(
                f"  <code>FW {esc(fw)} : {cnt} devices</code>"
                for fw, cnt in sorted(
                    analysis["firmware_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  <code>No firmware data</code>"
        )

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🩺 <b>SYSTEM &amp; PARSER HEALTH</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"▸ <b>Client:</b> <code>{esc(CLIENT_NAME)}</code>\n"
            f"▸ <b>Auto-Refresh:</b> <code>EVERY 60s</code>\n"
            f"▸ <b>Server HTTP 500:</b> <code>{'YES' if meta['server_error_500'] else 'NO'}</code>\n"
            f"▸ <b>Source:</b> <code>{esc(meta.get('source', 'none'))}</code>\n"
            f"▸ <b>Total Parsed:</b> <code>{analysis['total']}</code>\n"
            f"▸ <b>Assigned / Unassigned:</b> <code>{analysis['assigned_count']} / {analysis['unassigned_count']}</code>\n"
            f"▸ <b>Unparsed TS:</b> <code>{meta['unparsed_ts_count']}</code>\n\n"
            "<b>Device Type Distribution</b>\n"
            f"{type_lines}\n\n"
            "<b>Firmware Distribution</b>\n"
            f"{fw_lines}"
        )
        await send_fn(
            msg, parse_mode=ParseMode.HTML, reply_markup=main_inline_keyboard()
        )
    except Exception as exc:
        await send_fn(
            f"❌ <b>Health Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_export_csv_action(chat_id: int, bot, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
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
            caption=f"📥 <b>{esc(CLIENT_NAME)} Telemetry CSV Export</b>\n<code>Total: {analysis.get('total', 0)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await send_fn(
            f"❌ <b>Export Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def send_ai_mode_prompt(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    AI_CHAT_MODE.add(chat_id)
    await send_fn(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>PROJECT AI MODE ON</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Ab apna sawaal type karein—AI aapke live telemetry data ke hisaab se jawab dega.\n"
        "Bahar jaane ke liye <code>/exit_ai</code> bhejein.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_inline_keyboard(),
    )


async def send_ai_report_action(chat_id: int, send_fn) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await send_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    status_box = await send_fn(
        "<i>🧠 AI report ban rahi hai...</i>", parse_mode=ParseMode.HTML
    )
    await animate_message(
        status_box,
        [
            "<i>Scanning industries &amp; buckets [■■□]</i>",
            "<i>Compiling AI report [■■■]</i>",
        ],
        delay=0.3,
    )

    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)
        ai_text = await asyncio.to_thread(generate_ai_analysis, analysis)

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 <b>{esc(CLIENT_NAME)} — AI REPORT</b>\n"
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
# COMMAND, CALLBACK & TEXT HANDLERS
# ==============================================================================
def get_reply_fn(update: Update):
    if update.effective_message:
        return update.effective_message.reply_text
    raise RuntimeError("No effective message found")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_start_view(update.effective_chat.id, get_reply_fn(update))


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_login_prompt(update.effective_chat.id, get_reply_fn(update))


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_logout_action(update.effective_chat.id, get_reply_fn(update))


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_status_action(update.effective_chat.id, get_reply_fn(update))


async def aging_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_aging_menu(update.effective_chat.id, get_reply_fn(update))


async def online_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_online_action(update.effective_chat.id, get_reply_fn(update))


async def unassigned_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_unassigned_action(update.effective_chat.id, get_reply_fn(update))


async def allsite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_allsite_action(update.effective_chat.id, get_reply_fn(update))


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_health_action(update.effective_chat.id, get_reply_fn(update))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_export_csv_action(
        update.effective_chat.id, context.bot, get_reply_fn(update)
    )


async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    reply_fn = get_reply_fn(update)
    sess_data = require_auth(chat_id)
    if not sess_data:
        await reply_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    query = " ".join(context.args or []).strip().lower()
    if not query:
        await reply_fn(
            "Usage: <code>/site &lt;industry ya type ya serial&gt;</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
        return

    try:
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
            await reply_fn(
                f"<code>{esc(query)} se match karne wala koi device nahi mila.</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            return

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🔎 <b>FILTER: {esc(query.upper())} ({len(matches)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for idx, d in enumerate(matches[:20], start=1):
            lines.append(
                f"{idx}. {format_industry_with_type(d['industry'], d['site'])} | "
                f"<code>{esc(d['serial_no'])} ({esc(d['device_type'])})</code>\n"
                f"   {format_status_with_time(d['is_connected'], d['display_time'])}"
            )
        await reply_fn(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await reply_fn(
            f"❌ <b>Site Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    reply_fn = get_reply_fn(update)
    sess_data = require_auth(chat_id)
    if not sess_data:
        await reply_fn(
            "🔒 Pehle login karein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )
        return

    question = " ".join(context.args or []).strip()
    if not question:
        await send_ai_mode_prompt(chat_id, reply_fn)
        return

    thinking = await reply_fn(
        "<i>🤖 AI jawab taiyar kar raha hai...</i>", parse_mode=ParseMode.HTML
    )
    try:
        analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        reply_txt = await asyncio.to_thread(
            answer_project_question, question, analysis
        )
        await thinking.edit_text(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>PROJECT AI ASSISTANT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>{esc(reply_txt[:3400])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    except Exception as exc:
        await thinking.edit_text(
            f"❌ <b>AI Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )


async def exit_ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    AI_CHAT_MODE.discard(update.effective_chat.id)
    await get_reply_fn(update)(
        "<code>AI mode band kar diya gaya hai.</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_inline_keyboard(),
    )


async def ai_analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_ai_report_action(update.effective_chat.id, get_reply_fn(update))


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data or ""

    async def send_via_chat(text: str, parse_mode=None, reply_markup=None):
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    if data == "act:main_menu":
        await send_via_chat(
            f"🎛️ <b>{esc(CLIENT_NAME)} — MAIN MENU</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_inline_keyboard(),
        )
    elif data == "act:aging_menu":
        await send_aging_menu(chat_id, send_via_chat)
    elif data.startswith("age:"):
        bucket = data.split(":", 1)[1]
        await send_aging_bucket_list(chat_id, bucket, send_via_chat)
    elif data == "act:login":
        await send_login_prompt(chat_id, send_via_chat)
    elif data == "act:logout":
        await send_logout_action(chat_id, send_via_chat)
    elif data == "act:status":
        await send_status_action(chat_id, send_via_chat)
    elif data == "act:online":
        await send_online_action(chat_id, send_via_chat)
    elif data == "act:unassigned":
        await send_unassigned_action(chat_id, send_via_chat)
    elif data == "act:allsite":
        await send_allsite_action(chat_id, send_via_chat)
    elif data == "act:health":
        await send_health_action(chat_id, send_via_chat)
    elif data == "act:export_csv":
        await send_export_csv_action(chat_id, context.bot, send_via_chat)
    elif data == "act:ai_mode":
        await send_ai_mode_prompt(chat_id, send_via_chat)
    elif data == "act:ai_analyze":
        await send_ai_report_action(chat_id, send_via_chat)


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
    reply_fn = update.effective_message.reply_text
    state = USER_SESSIONS.get(chat_id)

    if state and state.get("step") == "awaiting_username":
        state["username"] = text
        state["step"] = "awaiting_password"
        await reply_fn(
            "🔑 Ab apna <b>Password</b> bhejein:", parse_mode=ParseMode.HTML
        )
        return

    if state and state.get("step") == "awaiting_password":
        username = state["username"]
        password = text

        try:
            await update.effective_message.delete()
        except Exception:
            pass

        auth_box = await reply_fn(
            "<i>🔐 Session verify ho raha hai...</i>",
            parse_mode=ParseMode.HTML,
        )
        await animate_message(
            auth_box,
            [
                "<i>Verifying .ASPXAUTH &amp; CSRF [■■□]</i>",
                "<i>Handshaking with EnviroFRONT [■■■]</i>",
            ],
            delay=0.3,
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
                f"🟢 <b>{esc(CLIENT_NAME)} — AUTHENTICATED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Account:</b> <code>{esc(username)}</code>\n"
                "🔄 <b>Auto-Refresh:</b> <code>EVERY 1 MINUTE</code>\n"
                "🔔 <b>Alerts:</b> <code>GROUPED + RECONNECT</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
            await reply_fn(
                "🎛️ <b>Panel Buttons Active</b>",
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
            await send_status_action(chat_id, reply_fn)
        elif mapped_action == "allsite":
            await send_allsite_action(chat_id, reply_fn)
        elif mapped_action == "online":
            await send_online_action(chat_id, reply_fn)
        elif mapped_action == "aging_menu":
            await send_aging_menu(chat_id, reply_fn)
        elif mapped_action == "unassigned":
            await send_unassigned_action(chat_id, reply_fn)
        elif mapped_action == "health":
            await send_health_action(chat_id, reply_fn)
        elif mapped_action == "ai_mode":
            await send_ai_mode_prompt(chat_id, reply_fn)
        elif mapped_action == "ai_analyze":
            await send_ai_report_action(chat_id, reply_fn)
        elif mapped_action == "export_csv":
            await send_export_csv_action(chat_id, context.bot, reply_fn)
        elif mapped_action == "logout":
            await send_logout_action(chat_id, reply_fn)
        return

    if chat_id in AI_CHAT_MODE:
        sess_data = require_auth(chat_id)
        if not sess_data:
            AI_CHAT_MODE.discard(chat_id)
            await reply_fn(
                "🔒 Pehle login karein:",
                parse_mode=ParseMode.HTML,
                reply_markup=guest_inline_keyboard(),
            )
            return

        ans_msg = await reply_fn(
            "<i>🤖 AI jawab taiyar kar raha hai...</i>", parse_mode=ParseMode.HTML
        )
        try:
            analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
                run_self_analysis, sess_data["session"]
            )
            LAST_SNAPSHOT[chat_id] = analysis
            reply_txt = await asyncio.to_thread(
                answer_project_question, text, analysis
            )
            await ans_msg.edit_text(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🤖 <b>PROJECT AI ASSISTANT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>{esc(reply_txt[:3400])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
        except Exception as exc:
            await ans_msg.edit_text(
                f"❌ <b>AI Error:</b> <code>{esc(exc)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_inline_keyboard(),
            )
        return

    if chat_type == "private" and not state:
        await reply_fn(
            "🔒 Login ke liye button dabayein:",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_inline_keyboard(),
        )


# ==============================================================================
# 1-MINUTE BACKGROUND MONITOR (GROUPED DISCONNECT + RECONNECT ALERTS)
# ==============================================================================
async def background_monitor(app) -> None:
    while True:
        now = datetime.now(timezone.utc)
        for chat_id, sess_data in list(USER_SESSIONS.items()):
            if sess_data.get("step") != "authenticated":
                continue

            try:
                prev_snap = LAST_SNAPSHOT.get(chat_id)
                prev_offline_serials = (
                    {d["serial_no"] for d in prev_snap.get("disconnected_5h_list", [])}
                    if prev_snap
                    else set()
                )

                analysis = await asyncio.to_thread(
                    run_self_analysis, sess_data["session"]
                )
                LAST_SNAPSHOT[chat_id] = analysis
                save_snapshot_db(chat_id, analysis)

                newly_disconnected: list[dict[str, Any]] = []
                for dev in analysis["disconnected_5h_list"]:
                    key = f"{chat_id}:{dev['serial_no']}"
                    last_alert = ALERTED_DEVICES.get(key)
                    if not last_alert or (now - last_alert) > ALERT_COOLDOWN:
                        ALERTED_DEVICES[key] = now
                        save_alert_key_db(key, now)
                        newly_disconnected.append(dev)

                if newly_disconnected:
                    header = (
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🚨 <b>{esc(CLIENT_NAME)} — DISCONNECT (&gt;5H) {len(newly_disconnected)}</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━\n"
                    )
                    chunk: list[str] = []
                    for idx, dev in enumerate(newly_disconnected, start=1):
                        row = (
                            f"{idx}. {format_industry_with_type(dev['industry'], dev['site'])} | "
                            f"<code>{esc(dev['serial_no'])}</code>\n"
                            f"   {format_status_with_time(False, dev['display_time'])}"
                        )
                        if len(header + "\n".join(chunk + [row])) > 3600:
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

                current_online_map = {
                    d["serial_no"]: d for d in analysis["online_list"]
                }
                recovered = [
                    current_online_map[s]
                    for s in prev_offline_serials
                    if s in current_online_map
                ]
                if recovered:
                    rec_lines = [
                        "━━━━━━━━━━━━━━━━━━━━━━",
                        f"🟢 <b>{esc(CLIENT_NAME)} — RECONNECTED ({len(recovered)})</b>",
                        "━━━━━━━━━━━━━━━━━━━━━━",
                    ]
                    for idx, d in enumerate(recovered[:20], start=1):
                        rec_lines.append(
                            f"{idx}. {format_industry_with_type(d['industry'], d['site'])} | "
                            f"<code>{esc(d['serial_no'])}</code>\n"
                            f"   {format_status_with_time(True, d['display_time'])}"
                        )
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text="\n".join(rec_lines),
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_inline_keyboard(),
                    )

                active_offline_serials = {
                    d["serial_no"] for d in analysis["disconnected_5h_list"]
                }
                for k in list(ALERTED_DEVICES.keys()):
                    if k.startswith(f"{chat_id}:"):
                        serial = k.split(":", 1)[1]
                        if serial not in active_offline_serials:
                            ALERTED_DEVICES.pop(k, None)
                            delete_alert_key_db(k)

            except Exception as exc:
                logger.warning("1m background monitor error for %s: %s", chat_id, exc)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(app) -> None:
    init_db()
    load_alert_keys_db()
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "BAGGI JAAT control panel"),
                BotCommand("login", "Login to EnviroFRONT"),
                BotCommand("status", "Live status & aging counts"),
                BotCommand("aging", "1Y / 11M / 24D / 1W / 1D menus"),
                BotCommand("allsite", "All assigned sites"),
                BotCommand("unassigned", "Unassigned N/A sites"),
                BotCommand("online", "Online devices list"),
                BotCommand("health", "Device types & firmware health"),
                BotCommand("export", "Download CSV report"),
                BotCommand("site", "Search industry/type/serial"),
                BotCommand("ai", "Ask AI about live telemetry"),
                BotCommand("ai_analyze", "Full AI report"),
                BotCommand("logout", "Logout session"),
            ]
        )
    except Exception as exc:
        logger.warning("Failed to set bot commands: %s", exc)

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

    logger.info("%s EnviroFRONT Bot starting (1m auto-refresh)...", CLIENT_NAME)
    app.run_polling()


if __name__ == "__main__":
    main()
