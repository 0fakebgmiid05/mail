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
from urllib.parse import urljoin

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

# Authentic M2M Logger / EnviroFRONT URLs
BASE_URL: str = (
    os.getenv("ENVIRO_BASE_URL", "https://envirofront.m2mlogger.com")
    .strip()
    .rstrip("/")
)
LOGIN_URL: str = os.getenv("ENVIRO_LOGIN_URL", f"{BASE_URL}/").strip()
DEVICES_URL: str = (
    os.getenv("ENVIRO_DEVICES_URL", f"{BASE_URL}/pae/ControlPanel/Devices").strip()
)
WS_DEVICES_URL: str = (
    os.getenv("ENVIRO_WS_URL", f"{BASE_URL}/ws/Devices.ashx").strip()
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
    # Escape <, >, & and quotes so Telegram HTML never hits an invalid tag
    return html.escape(str(val or ""), quote=True)


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
    sid = f" [ID:{esc(dev['site_id'])}]" if dev.get("site_id") else ""

    if NOTIF_STYLE == "compact":
        return (
            f"▸ {prefix}<b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>{sid}\n"
            f"  Serial: <b>{esc(dev.get('serial_no', 'N/A'))}</b> | {dot} <b>{conn_word}</b> | "
            f"<code>{esc(dev.get('display_time', 'N/A'))}</code> / <code>{esc(off_info)}</code>"
        )

    return (
        f"╔══════════════════════╗\n"
        f"║  {dot} {prefix}<b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>{sid}\n"
        f"╚══════════════════════╝\n"
        f"  ▸ Serial : <b>{esc(dev.get('serial_no', 'N/A'))}</b>\n"
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
# PORTAL LOGIN, PARSING & LIVE DASHBOARD / INSTRUMENT SCRAPER
# ==============================================================================
def login_envirofront(
    username: str, password: str
) -> tuple[requests.Session | None, str]:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
        r_chk = sess.get(DEVICES_URL, timeout=20, allow_redirects=True)
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

    if any(k in st_low for k in ("online", "connected", "active", "live")):
        return True, "CONNECTED", None, None
    return False, "DISCONNECTED", None, None


def extract_site_id_map_from_html(
    soup: BeautifulSoup, raw_html: str
) -> dict[str, str]:
    mapping: dict[str, str] = {}

    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        m = re.search(r"/pae/Site/(\d+)/Dashboard", href, re.IGNORECASE)
        if m:
            sid = m.group(1)
            txt = " ".join(a.get_text(" ", strip=True).split())
            if txt:
                mapping[txt.lower()] = sid
            parent_tr = a.find_parent("tr")
            if parent_tr:
                row_txt = parent_tr.get_text(" ", strip=True)
                sm = re.search(r"\b(\d{6,16})\b", row_txt)
                if sm:
                    mapping[sm.group(1)] = sid

    for m in re.finditer(
        r'["\']?(?:SiteId|siteId|Site_Id|site_id)["\']?\s*[:=]\s*["\']?(\d{1,8})["\']?',
        raw_html,
    ):
        sid = m.group(1)
        win = raw_html[
            max(0, m.start() - 220) : min(len(raw_html), m.end() + 220)
        ]
        ser_m = re.search(r"\b(\d{6,16})\b", win)
        if ser_m:
            mapping.setdefault(ser_m.group(1), sid)

    return mapping


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
    dash_url = (
        f"{BASE_URL}/pae/Site/{sid_clean}/Dashboard"
        if sid_clean.isdigit()
        else ""
    )

    return {
        "serial_no": ser,
        "site_id": sid_clean,
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


def extract_devices_from_html_table(
    soup: BeautifulSoup, now_utc: datetime
) -> tuple[list[dict[str, Any]], int, int]:
    devices: list[dict[str, Any]] = []
    tr_total = 0
    unparsed_ts = 0

    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        tr_total += 1
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        if len(cells) < 2:
            continue

        row_sid = ""
        for a in tr.find_all("a", href=True):
            m = re.search(
                r"/pae/Site/(\d+)/Dashboard", str(a["href"]), re.IGNORECASE
            )
            if m:
                row_sid = m.group(1)
                break

        serial = ""
        for c in cells:
            if re.fullmatch(r"[A-Za-z0-9\-_]{6,20}", c) and re.search(
                r"\d", c
            ):
                if not re.fullmatch(r"\d{1,3}", c):
                    serial = c
                    break
        if not serial:
            serial = cells[1] if len(cells) > 1 else cells[0]

        industry = cells[2] if len(cells) > 2 else "N/A"
        site_type = cells[3] if len(cells) > 3 else "N/A"
        dtype = cells[4] if len(cells) > 4 else "N/A"
        fw = cells[5] if len(cells) > 5 else "N/A"

        raw_ts = ""
        for c in reversed(cells):
            if re.search(
                r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|/Date\(",
                c,
            ):
                raw_ts = c
                break
        if not raw_ts and len(cells) >= 2:
            raw_ts = cells[-1]

        raw_status = " ".join(cells)
        rec = normalize_device_record(
            serial_no=serial,
            industry=industry,
            site_type=site_type,
            device_type=dtype,
            firmware=fw,
            raw_status=raw_status,
            raw_last_seen=raw_ts,
            now_utc=now_utc,
            site_id=row_sid,
        )
        if rec["last_seen"] is None:
            unparsed_ts += 1
        devices.append(rec)

    return devices, tr_total, unparsed_ts


def _extract_list_from_json_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in (
            "data",
            "rows",
            "devices",
            "Devices",
            "items",
            "Items",
            "result",
            "Result",
        ):
            val = data.get(key)
            if isinstance(val, list) and val:
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, str) and val.strip().startswith(("[", "{")):
                try:
                    parsed = json.loads(val)
                    sub = _extract_list_from_json_payload(parsed)
                    if sub:
                        return sub
                except Exception:
                    pass
    return []


def extract_devices_from_ws(
    session: requests.Session, now_utc: datetime
) -> list[dict[str, Any]]:
    try:
        r = session.get(
            WS_DEVICES_URL,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": DEVICES_URL,
            },
            timeout=20,
        )
        if not r.ok:
            return []
        txt = (r.text or "").strip()
        if not txt or txt.startswith("<"):
            return []
        data = json.loads(txt)
        rows = _extract_list_from_json_payload(data)
        if not rows:
            return []

        out: list[dict[str, Any]] = []
        for item in rows:
            serial = str(
                item.get("SerialNo")
                or item.get("serial_no")
                or item.get("Serial")
                or item.get("DeviceSerial")
                or item.get("IMEI")
                or item.get("LoggerId")
                or "N/A"
            )
            sid = str(
                item.get("SiteId")
                or item.get("siteId")
                or item.get("Site_Id")
                or item.get("site_id")
                or ""
            )
            ind = str(
                item.get("IndustryName")
                or item.get("Industry")
                or item.get("ClientName")
                or item.get("CompanyName")
                or "N/A"
            )
            stype = str(
                item.get("SiteType")
                or item.get("SiteName")
                or item.get("Site")
                or "N/A"
            )
            dtype = str(
                item.get("DeviceType")
                or item.get("Model")
                or item.get("Type")
                or "N/A"
            )
            fw = str(
                item.get("Firmware")
                or item.get("FirmwareVersion")
                or item.get("FW")
                or "N/A"
            )
            raw_ts = str(
                item.get("LastSeen")
                or item.get("LastDataTime")
                or item.get("LastCommunication")
                or item.get("UpdatedOn")
                or item.get("LastUpdate")
                or ""
            )
            raw_st = str(
                item.get("Status")
                or item.get("ConnectionStatus")
                or item.get("IsConnected")
                or item.get("Online")
                or ""
            )
            out.append(
                normalize_device_record(
                    serial_no=serial,
                    industry=ind,
                    site_type=stype,
                    device_type=dtype,
                    firmware=fw,
                    raw_status=raw_st,
                    raw_last_seen=raw_ts,
                    now_utc=now_utc,
                    site_id=sid,
                )
            )
        return out
    except Exception:
        return []


def extract_devices_from_embedded_scripts(
    raw_html: str, now_utc: datetime, site_id_map: dict[str, str]
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen_serials: set[str] = set()
    for m in re.finditer(
        r'\{[^{}]{20,900}?(?:SerialNo|serial_no|Serial|DeviceSerial|IndustryName|LastSeen|LastDataTime)[^{}]{20,900}?\}',
        raw_html,
        re.IGNORECASE,
    ):
        try:
            obj = json.loads(m.group(0))
            if not isinstance(obj, dict):
                continue
            serial = str(
                obj.get("SerialNo")
                or obj.get("serial_no")
                or obj.get("Serial")
                or obj.get("DeviceSerial")
                or ""
            ).strip()
            if not serial or serial in seen_serials:
                continue
            seen_serials.add(serial)
            sid = str(
                obj.get("SiteId")
                or obj.get("siteId")
                or site_id_map.get(serial)
                or ""
            )
            ind = str(
                obj.get("IndustryName")
                or obj.get("Industry")
                or obj.get("ClientName")
                or "N/A"
            )
            stype = str(
                obj.get("SiteType")
                or obj.get("SiteName")
                or obj.get("Site")
                or "N/A"
            )
            dtype = str(
                obj.get("DeviceType")
                or obj.get("Model")
                or obj.get("Type")
                or "N/A"
            )
            fw = str(
                obj.get("Firmware")
                or obj.get("FirmwareVersion")
                or obj.get("FW")
                or "N/A"
            )
            raw_ts = str(
                obj.get("LastSeen")
                or obj.get("LastDataTime")
                or obj.get("LastCommunication")
                or obj.get("UpdatedOn")
                or ""
            )
            raw_st = str(
                obj.get("Status")
                or obj.get("ConnectionStatus")
                or obj.get("IsConnected")
                or ""
            )
            found.append(
                normalize_device_record(
                    serial_no=serial,
                    industry=ind,
                    site_type=stype,
                    device_type=dtype,
                    firmware=fw,
                    raw_status=raw_st,
                    raw_last_seen=raw_ts,
                    now_utc=now_utc,
                    site_id=sid,
                )
            )
        except Exception:
            continue
    return found


def scrape_site_dashboard_details(
    session: requests.Session, dev: dict[str, Any]
) -> dict[str, Any]:
    site_id = str(dev.get("site_id") or "").strip()
    dash_url = str(dev.get("dashboard_url") or "").strip()

    if not site_id.isdigit():
        try:
            r_dev = session.get(DEVICES_URL, timeout=20)
            if r_dev.ok:
                soup_d = BeautifulSoup(r_dev.text, "html.parser")
                smap = extract_site_id_map_from_html(soup_d, r_dev.text)
                site_id = (
                    smap.get(dev.get("serial_no", ""))
                    or smap.get(dev.get("industry", "").lower())
                    or smap.get(dev.get("site", "").lower())
                    or ""
                )
                if site_id.isdigit():
                    dash_url = f"{BASE_URL}/pae/Site/{site_id}/Dashboard"
        except Exception:
            pass

    if not site_id.isdigit():
        return {
            "ok": False,
            "site_id": "",
            "url": "",
            "error": "Is device ke liye Site ID nahi mila.",
            "instruments": [],
        }

    if not dash_url:
        dash_url = f"{BASE_URL}/pae/Site/{site_id}/Dashboard"

    cache_key = f"{site_id}:{dash_url}"
    cached = DASHBOARD_CACHE.get(cache_key)
    if cached and (time.time() - cached.get("_ts", 0)) < 45:
        return cached

    try:
        resp = session.get(dash_url, timeout=22, allow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        return {
            "ok": False,
            "site_id": site_id,
            "url": dash_url,
            "error": f"Dashboard load fail: {exc}",
            "instruments": [],
        }

    if "account/login" in resp.url.lower():
        return {
            "ok": False,
            "site_id": site_id,
            "url": dash_url,
            "error": "Portal session expire ho gaya — /login karein.",
            "instruments": [],
        }

    html_txt = resp.text or ""
    soup = BeautifulSoup(html_txt, "html.parser")
    instruments: list[dict[str, str]] = []
    seen_pairs: set[str] = set()

    def add_instrument(
        name: str, value: str, unit: str = "", extra: str = ""
    ) -> None:
        n = " ".join((name or "").split()).strip(" :-|")
        v = " ".join((value or "").split()).strip()
        u = " ".join((unit or "").split()).strip()
        if not v:
            return
        if not n:
            n = "Instrument / Tank"
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

    for script in soup.find_all("script"):
        s_txt = script.string or script.get_text() or ""
        if not s_txt:
            continue
        for m in re.finditer(
            r'\{[^{}]{10,450}?(?:Parameter|Channel|Tank|Tag|Sensor|Name|Level|Volume)[^{}]{10,450}?\}',
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
                        or obj.get("Volume")
                        or ""
                    )
                    unit = str(obj.get("Unit") or obj.get("Units") or "")
                    if nm and val:
                        add_instrument(nm, val, unit)
            except Exception:
                continue

    for tr in soup.select("table tr"):
        cols = [
            " ".join(c.get_text(" ", strip=True).split())
            for c in tr.find_all(["td", "th"])
        ]
        cols = [c for c in cols if c]
        if len(cols) >= 2:
            joined = " ".join(cols)
            if re.search(
                r"\b(\d+(?:\.\d+)?)\s*(mm|cm|m|mtr|meter|litr|liter|litre|ltr|l|m3|m³|kl|kld|mld|mg/l|ppm|ph|bar|psi|°c|temp|flow|level)\b",
                joined,
                re.IGNORECASE,
            ):
                if len(cols) >= 3:
                    add_instrument(
                        cols[0],
                        cols[1],
                        cols[2],
                        cols[3] if len(cols) > 3 else "",
                    )
                else:
                    add_instrument(cols[0], cols[1])

    for card in soup.select(
        ".card, .panel, .widget, .box, .info-box, .small-box, [class*='tank'], [class*='gauge'], [class*='sensor']"
    ):
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

    if not instruments:
        body_txt = " ".join(soup.get_text(" \n ", strip=True).split())
        for m in re.finditer(
            r"([A-Za-z][A-Za-z0-9_\-\(\)\s]{1,35}?)\s*[:\-]?\s*([\-+]?\d+(?:\.\d+)?)\s*(mm|cm|ltr|litr|liter|litre|m3|m³|kl|kld|mg/l|ppm|ph)\b",
            body_txt,
            re.IGNORECASE,
        ):
            lbl = m.group(1).strip()
            if lbl.lower() not in (
                "http",
                "width",
                "height",
                "margin",
                "padding",
                "font",
            ):
                add_instrument(lbl, m.group(2), m.group(3))
            if len(instruments) >= 25:
                break

    res = {
        "ok": True,
        "site_id": site_id,
        "url": dash_url,
        "instruments": instruments[:25],
        "error": "",
        "_ts": time.time(),
    }
    DASHBOARD_CACHE[cache_key] = res
    return res


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
                    d["dashboard_url"] = (
                        f"{BASE_URL}/pae/Site/{d['site_id']}/Dashboard"
                    )
        meta["unparsed_ts_count"] = sum(
            1 for d in ws_devices if d["last_seen"] is None
        )
        meta["source"] = "ws-devices-json"
        return ws_devices, meta

    html_devices, _, unparsed_ts = extract_devices_from_html_table(soup, now)
    if html_devices:
        for d in html_devices:
            if not d.get("site_id"):
                d["site_id"] = site_id_map.get(d["serial_no"], "")
                if d["site_id"]:
                    d["dashboard_url"] = (
                        f"{BASE_URL}/pae/Site/{d['site_id']}/Dashboard"
                    )
        meta["unparsed_ts_count"] = unparsed_ts
        meta["source"] = "html-table"
        return html_devices, meta

    script_devices = extract_devices_from_embedded_scripts(
        raw_html, now, site_id_map
    )
    if script_devices:
        for d in script_devices:
            if d.get("site_id"):
                d["dashboard_url"] = (
                    f"{BASE_URL}/pae/Site/{d['site_id']}/Dashboard"
                )
        meta["unparsed_ts_count"] = sum(
            1 for d in script_devices if d["last_seen"] is None
        )
        meta["source"] = "embedded-json"
        return script_devices, meta

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
# GROQ AI ENGINE WITH LIVE SCRAPED DASHBOARD (MM, LITR, TANKS) INJECTION
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
            ):
                candidates.append(d)

    for d in analysis.get("assigned_devices", []):
        if d not in candidates:
            candidates.append(d)
        if len(candidates) >= 8:
            break

    for dev in candidates[:8]:
        try:
            dash = scrape_site_dashboard_details(session, dev)
            live_readings[dev.get("serial_no", "N/A")] = {
                "industry": dev.get("industry"),
                "site_type": dev.get("site"),
                "site_id": dash.get("site_id") or dev.get("site_id"),
                "instruments": dash.get("instruments", []),
                "error": dash.get("error", ""),
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
        f"Live Scraped Dashboard Instruments (mm, litr, tanks): {json.dumps(live_readings or {}, ensure_ascii=False)}\n"
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
        "Agar Live Scraped Dashboard Instruments mein mm/litr/tank readings hain to unhe zaroor dikhao."
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
        "Markdown table ya ** asterisks mat lagao; clean lines/bullets use karo. "
        "Live Scraped Dashboard Instruments mein jo mm, litr, tank name aur readings hain unhe directly batao."
    )
    usr_p = f"LIVE DATA:\n{ctx}\n\nQUESTION:\n{user_question}"
    return call_groq_ai(sys_p, usr_p, analysis)


def require_auth(chat_id: int) -> dict[str, Any] | None:
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        return None
    return sess_data


# ==============================================================================
# ACCESS & MAINTENANCE GUARD
# ==============================================================================
async def check_access(chat_id: int, user_id: int | None, bot) -> bool:
    if is_admin(user_id):
        return True
    if not BOT_ENABLED:
        await bot.send_message(
            chat_id=chat_id,
            text="╔══════════════════════╗\n║  • <b>BOT CURRENTLY OFF</b>   ║\n╚══════════════════════╝\n▸ Admin ne bot ko temporary OFF kiya hua hai.",
            parse_mode=ParseMode.HTML,
        )
        return False
    if MAINTENANCE_MODE:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "╔══════════════════════╗\n"
                f"║  ◈ <b>{sc('MAINTENANCE MODE')}</b>  ║\n"
                "╚══════════════════════╝\n"
                f"▸ {esc(MAINTENANCE_MSG)}"
            ),
            parse_mode=ParseMode.HTML,
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
            "  ▸ Dashboard ke liye: <code>/dash &lt;no ya serial ya naam&gt;</code>"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            reply_markup=bottom_reply_keyboard(),
        )
        return

    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{esc(CLIENT_NAME)}</b>\n"
            f"║  <i>{sc('TELEMETRY COMMAND')}</i>\n"
            "╚══════════════════════╝\n\n"
            "  ▸ Kripya apna <b>Login ID / Email</b> bhejein:"
        ),
        parse_mode=ParseMode.HTML,
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
        "<b>▸ Telemetry, Sites &amp; Instruments</b>\n"
        "  • <code>/status</code> — Live summary &amp; aging\n"
        "  • <code>/allsite [page]</code> — Assigned sites list\n"
        "  • <code>/dash &lt;no/serial/name&gt;</code> — Site dashboard\n"
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
    await bot.send_message(
        chat_id=chat_id,
        text=help_text,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


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
    await bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode=ParseMode.HTML,
        reply_markup=bottom_reply_keyboard(),
    )


async def send_login_prompt(chat_id: int, bot) -> None:
    AI_CHAT_MODE.discard(chat_id)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{sc('PORTAL LOGIN')}</b>\n"
            "╚══════════════════════╝\n"
            "  ▸ Kripya apna <b>Login ID / Email</b> bhejein:"
        ),
        parse_mode=ParseMode.HTML,
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

    await bot.send_message(
        chat_id=chat_id,
        text=(
            "╔══════════════════════╗\n"
            f"║  ◈ <b>{sc('LOGGED OUT')}</b>\n"
            "╚══════════════════════╝\n"
            "  ▸ Session aur alerts band kar diye gaye hain."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=guest_reply_keyboard(),
    )


async def send_status_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="<i>◈ Fetching live telemetry status...</i>",
        parse_mode=ParseMode.HTML,
    )
    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"]
        )
        LAST_SNAPSHOT[chat_id] = analysis
        save_snapshot_db(chat_id, analysis)

        if analysis["meta"]["server_error_500"]:
            await loader.edit_text(
                "╔══════════════════════╗\n"
                "║  • <b>SERVER HTTP 500</b>   ║\n"
                "╚══════════════════════╝\n"
                "  ▸ Portal ne <code>500 Internal Server Error</code> diya hai.",
                parse_mode=ParseMode.HTML,
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
        await loader.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await loader.edit_text(
            f"  • <b>Status Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_aging_menu(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)


async def send_aging_bucket_page(
    chat_id: int, bucket_key: str, page: int, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "╔══════════════════════╗\n"
                    f"║  ◈ <b>{esc(title)}</b>\n"
                    "╚══════════════════════╝\n"
                    "  ▸ <code>Is range mein koi disconnected site nahi hai.</code>"
                ),
                parse_mode=ParseMode.HTML,
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
        await bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • <b>Bucket Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_allsite_page(chat_id: int, page: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
            await bot.send_message(
                chat_id=chat_id,
                text="  ▸ <code>Koi assigned site nahi mili.</code>",
                parse_mode=ParseMode.HTML,
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
        await bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • <b>All Sites Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_site_dashboard_by_target(
    chat_id: int, target: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Session expire ho gaya hai, dobara login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return

    analysis = LAST_SNAPSHOT.get(chat_id) or await asyncio.to_thread(
        run_self_analysis, sess_data["session"]
    )
    LAST_SNAPSHOT[chat_id] = analysis
    assigned_devs = analysis["assigned_devices"]

    t = (target or "").strip()
    dev: dict[str, Any] | None = None

    if t.isdigit():
        num = int(t)
        if 1 <= num <= len(assigned_devs):
            dev = assigned_devs[num - 1]
        else:
            for d in assigned_devs:
                if d.get("serial_no") == t or d.get("site_id") == t:
                    dev = d
                    break
    elif t:
        t_low = t.lower()
        for d in assigned_devs:
            if (
                t_low in str(d.get("industry", "")).lower()
                or t_low in str(d.get("site", "")).lower()
                or t_low in str(d.get("serial_no", "")).lower()
            ):
                dev = d
                break

    if not dev:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"  • <b>Reason:</b> Site '<code>{esc(t or 'empty')}</code>' nahi mili. "
                "Pehle <code>/allsite</code> check karein ya <code>/dash &lt;number/serial/name&gt;</code> bhejein."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text=f"<i>◈ Opening dashboard: {esc(dev['industry'])}...</i>",
        parse_mode=ParseMode.HTML,
    )

    try:
        dash = await asyncio.to_thread(
            scrape_site_dashboard_details, sess_data["session"], dev
        )
    except Exception as exc:
        dash = {
            "ok": False,
            "site_id": dev.get("site_id", ""),
            "url": dev.get("dashboard_url", ""),
            "error": f"Exception: {exc}",
            "instruments": [],
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

    header = (
        "╔══════════════════════╗\n"
        f"║  ◈ <b>{esc(dev['industry'])}</b>\n"
        "╚══════════════════════╝\n"
        f"  ▸ Site Type : <code>{esc(dev['site'])}</code>\n"
        f"  ▸ Serial No : <b>{esc(dev['serial_no'])}</b>\n"
        f"  ▸ Site ID   : <code>{esc(dash.get('site_id') or 'N/A')}</code>\n"
        f"  ▸ Status    : {dot} <b>{conn_word}</b> (<code>{esc(dev['display_time'])}</code>)/(<code>{esc(off_info)}</code>)\n\n"
    )

    if dash.get("instruments"):
        inst_lines = [f"<b>◈ {sc('LIVE TANK & INSTRUMENT READINGS')}</b>"]
        for i, inst in enumerate(dash["instruments"], start=1):
            u = f" {inst['unit']}" if inst.get("unit") else ""
            ex = f" ({inst['extra']})" if inst.get("extra") else ""
            inst_lines.append(
                f"  {i}. ▸ <b>{esc(inst['name'])}</b> : <code>{esc(inst['value'])}{esc(u)}</code>{esc(ex)}"
            )
        body = "\n".join(inst_lines)
    else:
        reason = (
            dash.get("error")
            or "Dashboard HTML mein static/JS sensor readings nahi mili."
        )
        url_line = (
            f"\n  ▸ URL       : <code>{esc(dash.get('url'))}</code>"
            if dash.get("url")
            else ""
        )
        body = (
            f"<b>◈ {sc('DASHBOARD STATUS')}</b>\n"
            f"  • Reason    : <code>{esc(reason)}</code>\n"
            f"  ▸ Device    : <code>{esc(dev['device_type'])}</code>\n"
            f"  ▸ Firmware  : <code>{esc(dev['firmware'])}</code>{url_line}"
        )

    await loader.edit_text(header + body, parse_mode=ParseMode.HTML)


async def send_instruments_view(
    chat_id: int, bot, page: int = 0, query: str = ""
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
        ]

    if not devs:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  ▸ <code>'{esc(query or 'sites')}' ke liye koi site nahi mili.</code>",
            parse_mode=ParseMode.HTML,
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
        text="<i>◈ Fetching tank & instrument readings (mm, litr)...</i>",
        parse_mode=ParseMode.HTML,
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
                f"│  • Readings: <code>{esc(dash.get('error') or 'No mm/litr tags parsed')}</code>"
            )
        blocks.append("└──────────────────────")

    blocks.append(
        f"\n  ▸ Pages: <code>/prev</code> / <code>/next</code> (Page {page + 1}/{total_pages})"
    )
    await loader.edit_text("\n".join(blocks), parse_mode=ParseMode.HTML)


async def send_online_page(chat_id: int, page: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
            await bot.send_message(
                chat_id=chat_id,
                text="  ▸ <code>Koi connected device nahi mila.</code>",
                parse_mode=ParseMode.HTML,
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
        await bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • <b>Online Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_unassigned_page(chat_id: int, page: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
            await bot.send_message(
                chat_id=chat_id,
                text="  ▸ <code>Koi N/A unassigned device nahi mila.</code>",
                parse_mode=ParseMode.HTML,
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
        await bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML
        )
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • <b>Unassigned Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def step_pagination(chat_id: int, delta: int, bot) -> None:
    st = LAST_PAGE_STATE.get(chat_id)
    if not st:
        await bot.send_message(
            chat_id=chat_id,
            text="  • <b>Reason:</b> Koi active paginated list nahi hai. Pehle <code>/allsite</code>, <code>/online</code>, <code>/unassigned</code>, ya <code>/instruments</code> chalayein.",
            parse_mode=ParseMode.HTML,
        )
        return

    next_p = int(st.get("page", 0)) + delta
    total_p = int(st.get("total_pages", 1))
    if next_p < 0:
        await bot.send_message(
            chat_id=chat_id,
            text="  • Aap pehle hi <b>Page 1</b> par hain.",
            parse_mode=ParseMode.HTML,
        )
        return
    if next_p >= total_p:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • Aap aakhri <b>Page {total_p}</b> par hain.",
            parse_mode=ParseMode.HTML,
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
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
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
            f"  ▸ Sync Rate   : <code>{REFRESH_INTERVAL_SEC}s</code>\n"
            f"  ▸ HTTP 500    : <code>{'YES' if meta['server_error_500'] else 'NO'}</code>\n"
            f"  ▸ Total / N/A : <b>{analysis['total']} / {analysis['unassigned_count']}</b>\n\n"
            f"<b>◈ {sc('Device Types')}</b>\n{type_lines}\n\n"
            f"<b>◈ {sc('Firmware')}</b>\n{fw_lines}"
        )
        await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • <b>Health Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_export_csv_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="<i>◈ Generating CSV report...</i>",
        parse_mode=ParseMode.HTML,
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
                f"<b>{esc(CLIENT_NAME)} — {sc('CSV EXPORT')}</b>\n"
                f"▸ Total: <b>{analysis['total']}</b> | "
                f"Assigned: <b>{analysis['assigned_count']}</b> | "
                f"Unassigned: <b>{analysis['unassigned_count']}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )
        await loader.delete()
    except Exception as exc:
        await loader.edit_text(
            f"  • <b>CSV Export Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_site_search_action(
    chat_id: int, query: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return

    q = (query or "").strip().lower()
    if not q:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Use: <code>/site &lt;industry / site / serial&gt;</code>",
            parse_mode=ParseMode.HTML,
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
    ]
    if not matches:
        await bot.send_message(
            chat_id=chat_id,
            text=f"  • '<code>{esc(query)}</code>' ke liye koi match nahi mila.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        "╔══════════════════════╗",
        f"║  ◈ <b>{sc('SEARCH RESULT')} ({len(matches)})</b>",
        "╚══════════════════════╝",
    ]
    for i, d in enumerate(matches[:10], start=1):
        lines.append(format_premium_device_block(d, i))
    await bot.send_message(
        chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.HTML
    )


async def send_ai_analyze_action(chat_id: int, bot) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="<i>◈ Groq AI live telemetry &amp; dashboard readings analyze kar raha hai...</i>",
        parse_mode=ParseMode.HTML,
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
        await loader.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await loader.edit_text(
            f"  • <b>AI Analyze Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def send_ai_answer_action(
    chat_id: int, question: str, bot
) -> None:
    sess_data = require_auth(chat_id)
    if not sess_data:
        await bot.send_message(
            chat_id=chat_id,
            text="  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return

    loader = await bot.send_message(
        chat_id=chat_id,
        text="<i>◈ Groq AI live data check karke jawab de raha hai...</i>",
        parse_mode=ParseMode.HTML,
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
        await loader.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await loader.edit_text(
            f"  • <b>Groq AI Error:</b> <code>{esc(exc)}</code>",
            parse_mode=ParseMode.HTML,
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
        await update.message.reply_text(
            "  ▸ Use: <code>/age 1d</code>, <code>/age 1w</code>, <code>/age 24d</code>, <code>/age 11m</code>, <code>/age 1y</code>, ya <code>/age gt1y</code>",
            parse_mode=ParseMode.HTML,
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
        await update.message.reply_text(
            "  ▸ Pehle login karein (<code>/login</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=guest_reply_keyboard(),
        )
        return
    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        AI_CHAT_MODE.add(chat_id)
        await update.message.reply_text(
            "  ▸ <b>Groq AI Mode ON</b> — Apna sawal bhejein (jaise tank level, mm, litr, offline sites). Band karne ke liye <code>/exit_ai</code> bhejein.",
            parse_mode=ParseMode.HTML,
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
    await update.message.reply_text(
        "  ▸ <b>Groq AI Mode OFF</b>.",
        parse_mode=ParseMode.HTML,
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
    await update.message.reply_text(
        "  ∙ <b>Bot ON kar diya gaya hai.</b>", parse_mode=ParseMode.HTML
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
    await update.message.reply_text(
        "  • <b>Bot OFF kar diya gaya hai.</b>", parse_mode=ParseMode.HTML
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
    await update.message.reply_text(
        f"  ◈ <b>Maintenance Mode ON:</b> <code>{esc(MAINTENANCE_MSG)}</code>",
        parse_mode=ParseMode.HTML,
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
    await update.message.reply_text(
        "  ∙ <b>Maintenance Mode OFF.</b>", parse_mode=ParseMode.HTML
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
        await update.message.reply_text(
            "  ▸ Use: <code>/set_refresh 30</code> (15 se 3600 seconds)",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        sec = int(context.args[0])
        if sec < 15 or sec > 3600:
            raise ValueError()
        REFRESH_INTERVAL_SEC = sec
        save_setting_db("refresh_sec", str(sec))
        await update.message.reply_text(
            f"  ∙ <b>Sync Rate set to {sec}s.</b>", parse_mode=ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text(
            "  • 15 se 3600 ke beech valid seconds dein.",
            parse_mode=ParseMode.HTML,
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
        await update.message.reply_text(
            "  ▸ Use: <code>/set_notif detailed | compact | alerts_only</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    NOTIF_STYLE = context.args[0].lower()
    save_setting_db("notif_style", NOTIF_STYLE)
    await update.message.reply_text(
        f"  ∙ <b>Notification Style set to:</b> <code>{esc(NOTIF_STYLE)}</code>",
        parse_mode=ParseMode.HTML,
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
        await update.message.reply_text(
            "  ▸ Ab apna <b>Password</b> bhejein:", parse_mode=ParseMode.HTML
        )
        return

    if sess_data and sess_data.get("step") == "awaiting_password":
        username = sess_data.get("username", "")
        password = raw_text
        try:
            await update.message.delete()
        except Exception:
            pass

        wait_msg = await update.message.reply_text(
            "<i>◈ EnviroFRONT portal par login ho raha hai...</i>",
            parse_mode=ParseMode.HTML,
        )
        session, err = await asyncio.to_thread(
            login_envirofront, username, password
        )
        if not session:
            USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
            await wait_msg.edit_text(
                f"  • <b>Login Failed:</b> <code>{esc(err)}</code>\n  ▸ Dobara <b>Login ID / Email</b> bhejein:",
                parse_mode=ParseMode.HTML,
            )
            return

        USER_SESSIONS[chat_id] = {
            "step": "authenticated",
            "username": username,
            "session": session,
        }
        await wait_msg.edit_text(
            "╔══════════════════════╗\n"
            f"║  ∙ <b>{esc(CLIENT_NAME)}</b>\n"
            f"║  <i>{sc('AUTHENTICATED')}</i>\n"
            "╚══════════════════════╝\n"
            f"  ▸ Account   : <code>{esc(username)}</code>\n"
            f"  ▸ Sync Rate : <code>{REFRESH_INTERVAL_SEC}s</code>\n"
            f"  ▸ Alerts    : <code>2h → 10h → 1d (1-Time)</code>",
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text(
            f"◈ <b>{esc(CLIENT_NAME)} — {sc('PANEL ACTIVE')}</b>",
            parse_mode=ParseMode.HTML,
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
            await update.message.reply_text(
                "  ▸ Pehle login karein (<code>/login</code>):",
                parse_mode=ParseMode.HTML,
                reply_markup=guest_reply_keyboard(),
            )
            return
        AI_CHAT_MODE.add(chat_id)
        await update.message.reply_text(
            "  ▸ <b>Groq AI Chat ON</b> — Apna sawal bhejein. Band karne ke liye <code>/exit_ai</code> bhejein.",
            parse_mode=ParseMode.HTML,
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

    await update.message.reply_text(
        "  ▸ Command ya button use karein. Poori list ke liye <code>/help</code> bhejein.",
        parse_mode=ParseMode.HTML,
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
                                try:
                                    await app.bot.send_message(
                                        chat_id=chat_id,
                                        text=(
                                            "╔══════════════════════╗\n"
                                            f"║  ∙ <b>{sc('SITE RECOVERED')}</b>     ║\n"
                                            "╚══════════════════════╝\n"
                                            f"  ▸ Site   : <b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>\n"
                                            f"  ▸ Serial : <b>{esc(serial)}</b>\n"
                                            f"  ▸ Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code>"
                                        ),
                                        parse_mode=ParseMode.HTML,
                                    )
                                except Exception:
                                    pass
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
                            try:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text=(
                                        "╔══════════════════════╗\n"
                                        f"║  • <b>{esc(stage_badge)} ALERT</b>  ║\n"
                                        "╚══════════════════════╝\n"
                                        f"  ▸ Site   : <b>{format_industry_site_label(dev.get('industry', 'N/A'), dev.get('site', 'N/A'))}</b>\n"
                                        f"  ▸ Serial : <b>{esc(serial)}</b>\n"
                                        f"  ▸ Status : <code>{esc(off_str)}</code>\n"
                                        f"  ▸ Seen   : <code>{esc(dev.get('display_time', 'N/A'))}</code>"
                                    ),
                                    parse_mode=ParseMode.HTML,
                                )
                            except Exception:
                                pass
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
            BotCommand("dash", "Site dashboard (<no/serial/name>)"),
            BotCommand("instruments", "Tank / mm / litr readings"),
            BotCommand("online", "Connected devices"),
            BotCommand("unassigned", "Unassigned N/A devices"),
            BotCommand("aging", "Aging buckets menu"),
            BotCommand("age", "Filter bucket (<1d|1w|24d|11m|1y|gt1y>)"),
            BotCommand("next", "Next page"),
            BotCommand("prev", "Previous page"),
            BotCommand("ai_analyze", "Groq AI telemetry & dashboard report"),
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
