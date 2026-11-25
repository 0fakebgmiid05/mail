import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from google import genai
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==============================================================================
# DIRECT CONFIGURATION
# ==============================================================================
BOT_TOKEN = "9018573596:AAE74Vb8hbWyxNQMARkTxYTptCo9a_E11sE"
GEMINI_API_KEY = "AQ.Ab8RN6LKHe0Rp-B5loqNfPLVz32jwQgnFCorUoYe2kGX09U4PA"

BASE_URL = "https://envirofront.m2mlogger.com"
LOGIN_URL = f"{BASE_URL}/?ReturnUrl=%2Fpae%2FControlPanel%2FDevices"
DEVICES_URL = f"{BASE_URL}/pae/ControlPanel/Devices"
WS_DEVICES_URL = f"{BASE_URL}/Services/EF/WS/Devices"

IST = timezone(timedelta(hours=5, minutes=30))
DISCONNECT_THRESHOLD = timedelta(hours=5)
CHECK_INTERVAL_SECONDS = 300
ALERT_COOLDOWN = timedelta(hours=6)

USER_SESSIONS: dict[int, dict[str, Any]] = {}
ALERTED_DEVICES: dict[str, datetime] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}
AI_CHAT_MODE: set[int] = set()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EnviroFRONT.Bot")


def esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


async def animate_message(msg, frames: list[str], delay: float = 0.35) -> None:
    for frame in frames:
        try:
            await msg.edit_text(frame, parse_mode=ParseMode.HTML)
            await asyncio.sleep(delay)
        except Exception:
            break


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


def login_envirofront(username: str, password: str) -> requests.Session | None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
            ),
            "Referer": LOGIN_URL,
            "Origin": BASE_URL,
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

        has_auth_cookie = ".ASPXAUTH" in session.cookies.get_dict()
        loc = post_resp.headers.get("Location", "")

        if post_resp.status_code == 302 and (
            has_auth_cookie or "/pae/ControlPanel/Devices" in loc
        ):
            target = urljoin(BASE_URL, loc or "/pae/ControlPanel/Devices")
            verify = session.get(target, timeout=25)
            if verify.ok and "Forgot Password?" not in verify.text:
                return session
    except Exception as exc:
        logger.error("Login exception: %s", exc)

    return None


def parse_timestamp(raw: str) -> datetime | None:
    cleaned = " ".join((raw or "").strip().split())
    if not cleaned:
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
    ind_clean = (industry or "N/A").strip() or "N/A"
    site_clean = (site or "N/A").strip() or "N/A"
    type_clean = (device_type or "LOGPRO PLUS").strip() or "LOGPRO PLUS"
    fw_clean = (firmware or "N/A").strip() or "N/A"
    st_clean = (status_text or "").strip()
    ts_clean = (ts_raw or "").strip()

    is_connected = (
        "connected" in st_clean.lower()
        and "disconnected" not in st_clean.lower()
    )
    last_seen = parse_timestamp(ts_clean)
    offline_td = (now - last_seen) if last_seen else None
    is_offline_5h = bool(
        (offline_td and offline_td > DISCONNECT_THRESHOLD)
        or (not is_connected and offline_td is None)
    )

    return {
        "serial_no": s_clean,
        "industry": ind_clean,
        "site": site_clean,
        "device_type": type_clean,
        "firmware": fw_clean,
        "status": st_clean or ("Connected" if is_connected else "Disconnected"),
        "is_connected": is_connected,
        "timestamp_raw": ts_clean or "N/A",
        "last_seen": last_seen,
        "offline_duration": offline_td,
        "disconnected_5h": is_offline_5h,
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
        # Partial key match fallback
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
    session: requests.Session, now: datetime, log_sample: bool = False
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

            if log_sample and page == 1:
                logger.info("WS first item keys/sample: %s", str(raw_rows[0])[:500])

            page_added = 0
            for item in raw_rows:
                dev = parse_ws_device_item(item, now)
                dedup_key = f"{dev['serial_no']}|{dev['site']}|{dev['timestamp_raw']}"
                if dedup_key not in seen_serials:
                    seen_serials.add(dedup_key)
                    all_parsed.append(dev)
                    page_added += 1

            # Agar rows page_size se kam aayi hain to aage aur pages nahi hain
            if len(raw_rows) < page_size or page_added == 0:
                break
        except Exception as exc:
            logger.warning("WS fetch failed on page %d: %s", page, exc)
            break

    return all_parsed


def extract_devices_from_html_table(
    soup: BeautifulSoup, now: datetime, log_sample: bool = False
) -> tuple[list[dict[str, Any]], int, int]:
    all_trs = soup.select("tr")
    if log_sample:
        logger.info("Total <tr> in HTML: %d", len(all_trs))
        for idx, tr in enumerate(all_trs[:5]):
            snippet = " | ".join(tr.get_text(" ", strip=True).split())[:260]
            logger.info("Sample TR[%d]: %s", idx, snippet)

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
    session: requests.Session, log_sample: bool = False
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

    _, tr_total, unparsed_ts = extract_devices_from_html_table(
        soup, now, log_sample=log_sample
    )
    meta["tr_total"] = tr_total
    meta["unparsed_ts_count"] = unparsed_ts

    # Strategy 1: Direct WS Devices JSON endpoint (/Services/EF/WS/Devices)
    ws_devices = extract_devices_from_ws(session, now, log_sample=log_sample)
    if ws_devices:
        meta["source"] = "ws-devices-json"
        return ws_devices, meta

    # Strategy 2: HTML Table fallback
    html_devices, _, _ = extract_devices_from_html_table(soup, now, log_sample=False)
    if len(html_devices) > 1:
        meta["source"] = "html-table"
        return html_devices, meta

    meta["source"] = "0-found"
    return [], meta


def run_self_analysis(
    session: requests.Session, log_sample: bool = False
) -> dict[str, Any]:
    devices, meta = fetch_devices_multi_strategy(session, log_sample=log_sample)

    total = len(devices)
    online_list = [d for d in devices if d["is_connected"]]
    offline_5h_list = [d for d in devices if d["disconnected_5h"]]

    offline_5h_list.sort(
        key=lambda x: x["offline_duration"].total_seconds()
        if x["offline_duration"]
        else 99999999,
        reverse=True,
    )

    site_offline_counts: dict[str, int] = {}
    fw_counts: dict[str, int] = {}

    for d in devices:
        fw = d["firmware"] or "Unknown"
        fw_counts[fw] = fw_counts.get(fw, 0) + 1
        if d["disconnected_5h"]:
            site_label = (
                f"{d['site']} ({d['industry']})"
                if d["industry"] != "N/A"
                else d["site"]
            )
            site_offline_counts[site_label] = site_offline_counts.get(site_label, 0) + 1

    top_sites = sorted(site_offline_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "checked_at_ist": datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST"),
        "total": total,
        "connected": len(online_list),
        "disconnected_5h_count": len(offline_5h_list),
        "all_devices": devices,
        "online_list": online_list,
        "disconnected_5h_list": offline_5h_list,
        "top_offline_sites": top_sites,
        "firmware_breakdown": fw_counts,
        "meta": meta,
    }


def build_rich_local_summary(analysis: dict[str, Any]) -> str:
    off_lines = [
        f"- {d['serial_no']} | {d['site']} | FW {d['firmware']} | Last: {d['timestamp_raw']}"
        for d in analysis["disconnected_5h_list"][:10]
    ]
    fw_summary = ", ".join(
        f"{k}: {v}" for k, v in analysis["firmware_breakdown"].items()
    ) or "N/A"
    return (
        f"Total Devices: {analysis['total']} | Online: {analysis['connected']} | Offline (>5h): {analysis['disconnected_5h_count']}\n"
        f"Firmware: {fw_summary}\n"
        + ("Offline (>5h) Devices:\n" + "\n".join(off_lines) if off_lines else "Koi device >5h offline nahi hai.")
    )


def call_gemini_with_fallback(prompt: str, fallback_text: str) -> str:
    if not GEMINI_API_KEY:
        return fallback_text

    models_to_try = ("gemini-3.8-flash", "gemini-flash-latest", "gemini-2.5-flash")

    for m_name in models_to_try:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(model=m_name, contents=prompt)
            if response and response.text:
                return response.text.strip()
        except Exception:
            continue

    body = {"contents": [{"parts": [{"text": prompt}]}]}
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
        except Exception:
            continue

    return fallback_text


def build_project_context(analysis: dict[str, Any]) -> str:
    compact_devices = [
        {
            "serial": d["serial_no"],
            "site": d["site"],
            "industry": d["industry"],
            "type": d["device_type"],
            "fw": d["firmware"],
            "status": d["status"],
            "online": d["is_connected"],
            "offline_5h": d["disconnected_5h"],
            "offline_hours": round(d["offline_duration"].total_seconds() / 3600, 1)
            if d["offline_duration"]
            else None,
            "last_seen": d["timestamp_raw"],
        }
        for d in analysis["all_devices"][:50]
    ]
    return (
        f"Checked At: {analysis['checked_at_ist']}\n"
        f"Total Devices: {analysis['total']}\n"
        f"Online Devices: {analysis['connected']}\n"
        f"Offline (>5h) Count: {analysis['disconnected_5h_count']}\n"
        f"Top Offline Sites: {analysis['top_offline_sites']}\n"
        f"Firmware Breakdown: {analysis['firmware_breakdown']}\n"
        f"Parser/Server Meta: {analysis['meta']}\n"
        f"Devices (up to 50): {compact_devices}"
    )


def generate_ai_analysis(analysis: dict[str, Any]) -> str:
    context_str = build_project_context(analysis)
    prompt = (
        "Tum ek senior industrial telemetry & IoT monitoring engineer ho jo EnviroFRONT (M2MLogger) "
        "device monitoring project par kaam kar raha hai. Is project ke live data ke basis par "
        "Hinglish mein crisp, executive aur actionable analysis do:\n"
        "1. Overall Health & Risk Level\n"
        "2. Sabse Critical Sites / Devices (>5h offline)\n"
        "3. Firmware / Connectivity Patterns\n"
        "4. Immediate Action Checklist\n\n"
        f"{context_str}"
    )
    return call_gemini_with_fallback(prompt, build_rich_local_summary(analysis))


def answer_project_question(user_question: str, analysis: dict[str, Any]) -> str:
    context_str = build_project_context(analysis)
    prompt = (
        "Tum EnviroFRONT (M2MLogger) telemetry monitoring bot aur dashboard ke dedicated AI assistant ho. "
        "User ke message ka jawab sirf is project ke live devices, sites, serials, firmware, timestamps aur "
        "alerts ke context mein Hinglish mein do.\n\n"
        f"LIVE PROJECT DATA:\n{context_str}\n\n"
        f"USER MESSAGE:\n{user_question}"
    )
    return call_gemini_with_fallback(prompt, build_rich_local_summary(analysis))


def require_auth(chat_id: int) -> dict[str, Any] | None:
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        return None
    return sess_data


def commands_menu_html() -> str:
    return (
        "<b>Available Commands</b>\n"
        "• <code>/status</code> — Overview &amp; top offline sites\n"
        "• <code>/online</code> — Connected devices\n"
        "• <code>/offline</code> — Disconnected &gt; 5h\n"
        "• <code>/site &lt;query&gt;</code> — Filter by site/industry\n"
        "• <code>/health</code> — Firmware &amp; parser diagnostics\n"
        "• <code>/ai &lt;msg&gt;</code> — Ask AI about your project\n"
        "• <code>/ai_analyze</code> — Full AI health report\n"
        "• <code>/login</code> — Switch / re-login account\n"
        "• <code>/logout</code> — End active session"
    )


# ==============================================================================
# COMMAND HANDLERS
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    existing = require_auth(chat_id)

    if existing:
        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENVIROFRONT TELEMETRY SUITE</b>\n"
            "<i>Active Session Restored</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"▸ <b>Account:</b> <code>{esc(existing.get('username'))}</code>\n"
            "▸ <b>Session State:</b> <b>AUTHENTICATED</b>\n"
            "▸ <b>5h Auto-Alert:</b> <b>ACTIVE</b>\n\n"
            f"{commands_menu_html()}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return

    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    intro = await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>ENVIROFRONT TELEMETRY SUITE</b>\n"
        "<i>Initializing Classic Console...</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.HTML,
    )
    await animate_message(
        intro,
        [
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENVIROFRONT TELEMETRY SUITE</b>\n"
            "<i>Loading modules [■□□□□]</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>ENVIROFRONT TELEMETRY SUITE</b>\n"
            "<i>Loading modules [■■■□□]</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "• Auto-login via <code>.ASPXAUTH</code> &amp; CSRF token\n"
            "• Continuous <b>5-Hour Disconnect</b> alert engine\n"
            "• Live <b>Online / Offline / Site / Health</b> views\n"
            "• Project-aware <b>/ai</b> assistant &amp; reports\n\n"
            "Kripya apna <b>Login ID / Email</b> bhejein:",
        ],
        delay=0.35,
    )


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    AI_CHAT_MODE.discard(chat_id)
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>ENVIROFRONT LOGIN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Kripya apna <b>Login ID / Email</b> bhejein:",
        parse_mode=ParseMode.HTML,
    )


async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    AI_CHAT_MODE.discard(chat_id)
    sess_data = USER_SESSIONS.pop(chat_id, None)
    LAST_SNAPSHOT.pop(chat_id, None)

    for key in [k for k in ALERTED_DEVICES if k.startswith(f"{chat_id}:")]:
        ALERTED_DEVICES.pop(key, None)

    if sess_data and sess_data.get("session"):
        try:
            sess_data["session"].close()
        except Exception:
            pass

    status_msg = await update.message.reply_text(
        "<i>Session close ho raha hai...</i>", parse_mode=ParseMode.HTML
    )
    await animate_message(
        status_msg,
        [
            "<i>Session close ho raha hai [■■□□]</i>",
            "<i>Session close ho raha hai [■■■■]</i>",
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>LOGGED OUT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Aapka EnviroFRONT session aur auto-alert monitor band kar diya gaya hai.\n"
            "Dobara login ke liye <code>/login</code> ya <code>/start</code> bhejein.",
        ],
        delay=0.3,
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    loader = await update.message.reply_text(
        "<i>Fetching telemetry status...</i>", parse_mode=ParseMode.HTML
    )
    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis

        if analysis["meta"]["server_error_500"]:
            await loader.edit_text(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<b>SERVER ALERT — HTTP 500</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "EnviroFRONT ne <code>500 Internal Server Error</code> return kiya hai.",
                parse_mode=ParseMode.HTML,
            )
            return

        sites_lines = (
            "\n".join(
                f"  ▸ <b>{esc(s)}</b> — <code>{c}</code> offline"
                for s, c in analysis["top_offline_sites"]
            )
            if analysis["top_offline_sites"]
            else "  ▸ <i>Koi critical offline site nahi</i>"
        )

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>EXECUTIVE SYSTEM STATUS</b>\n"
            f"<i>{esc(analysis['checked_at_ist'])}</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"▸ <b>Total Devices:</b> <code>{analysis['total']}</code>\n"
            f"▸ <b>Online (Connected):</b> <code>{analysis['connected']}</code>\n"
            f"▸ <b>Offline (&gt; 5h):</b> <code>{analysis['disconnected_5h_count']}</code>\n"
            f"▸ <b>Source:</b> <code>{esc(analysis['meta'].get('source'))}</code>\n\n"
            "<b>Priority Offline Sites (&gt;5h)</b>\n"
            f"{sites_lines}\n\n"
            "<i>Commands: /online · /offline · /site · /health · /ai · /ai_analyze</i>"
        )
        await loader.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await loader.edit_text(
            f"<b>Status Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def online_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        online_list = analysis["online_list"]

        if not online_list:
            await update.message.reply_text(
                "<i>Koi connected device nahi mila.</i>", parse_mode=ParseMode.HTML
            )
            return

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"<b>ONLINE DEVICES ({len(online_list)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for d in online_list[:25]:
            lines.append(
                f"• <code>{esc(d['serial_no'])}</code> | <b>{esc(d['site'])}</b>\n"
                f"  <i>FW {esc(d['firmware'])} · Last: {esc(d['timestamp_raw'])}</i>"
            )
        if len(online_list) > 25:
            lines.append(f"\n<i>...aur {len(online_list) - 25} online devices hain.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Online List Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def offline_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        off_list = analysis["disconnected_5h_list"]

        if not off_list:
            await update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<b>OFFLINE MONITOR (&gt; 5h)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Sabhi devices 5 ghante ke andar active hain.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"<b>OFFLINE &gt; 5 HOURS ({len(off_list)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for d in off_list[:20]:
            hrs = (
                f"{round(d['offline_duration'].total_seconds() / 3600, 1)}h"
                if d["offline_duration"]
                else "N/A"
            )
            lines.append(
                f"• <code>{esc(d['serial_no'])}</code> | <b>{esc(d['site'])}</b>\n"
                f"  <b>Down: {esc(hrs)}</b> · <i>Last: {esc(d['timestamp_raw'])}</i>"
            )
        if len(off_list) > 20:
            lines.append(f"\n<i>...aur {len(off_list) - 20} offline devices hain.</i>")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Offline List Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def site_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    query = " ".join(context.args or []).strip().lower()
    if not query:
        await update.message.reply_text(
            "Usage: <code>/site &lt;site ya industry ka naam&gt;</code>\n"
            "Example: <code>/site distillery</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        matches = [
            d
            for d in analysis["all_devices"]
            if query in d["site"].lower()
            or query in d["industry"].lower()
            or query in d["serial_no"].lower()
        ]
        if not matches:
            await update.message.reply_text(
                f"<code>{esc(query)}</code> se match karne wala koi device nahi mila.",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"<b>SITE FILTER: {esc(query.upper())} ({len(matches)})</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for d in matches[:20]:
            state_tag = "ONLINE" if d["is_connected"] else "OFFLINE"
            lines.append(
                f"• <code>{esc(d['serial_no'])}</code> [{esc(state_tag)}] — <b>{esc(d['site'])}</b>\n"
                f"  <i>{esc(d['industry'])} · FW {esc(d['firmware'])} · {esc(d['timestamp_raw'])}</i>"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Site Search Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        meta = analysis["meta"]
        fw_lines = (
            "\n".join(
                f"  ▸ Firmware <code>{esc(fw)}</code>: <b>{cnt}</b> devices"
                for fw, cnt in sorted(
                    analysis["firmware_breakdown"].items(), key=lambda x: -x[1]
                )
            )
            or "  ▸ <i>No firmware data</i>"
        )

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>SYSTEM &amp; PARSER DIAGNOSTICS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"▸ <b>Server HTTP 500:</b> <code>{'YES' if meta['server_error_500'] else 'NO'}</code>\n"
            f"▸ <b>Data Source:</b> <code>{esc(meta.get('source', 'none'))}</code>\n"
            f"▸ <b>HTML Size:</b> <code>{meta['html_length']}</code> bytes\n"
            f"▸ <b>Raw &lt;tr&gt; Rows:</b> <code>{meta['tr_total']}</code>\n"
            f"▸ <b>Parsed Devices:</b> <code>{analysis['total']}</code>\n"
            f"▸ <b>Unparsed Timestamps:</b> <code>{meta['unparsed_ts_count']}</code>\n\n"
            "<b>Firmware Distribution</b>\n"
            f"{fw_lines}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Health Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def debug_rows_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        analysis = await asyncio.to_thread(
            run_self_analysis, sess_data["session"], True
        )
        meta = analysis["meta"]
        sample_dev = analysis["all_devices"][0] if analysis["all_devices"] else {}
        await update.message.reply_text(
            "Debug info Railway logs mein print kar di gayi hai.\n"
            f"Source: <code>{esc(meta.get('source'))}</code> | Parsed: <code>{analysis['total']}</code>\n"
            f"Sample: <code>{esc(sample_dev.get('serial_no'))}</code> / <code>{esc(sample_dev.get('site'))}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Debug Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    question = " ".join(context.args or []).strip()
    if not question:
        AI_CHAT_MODE.add(chat_id)
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>PROJECT AI MODE ON</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Ab aap jo bhi message likhenge, main aapke <b>EnviroFRONT project</b> ke live "
            "data ke hisaab se jawab dunga.\n"
            "Normal mode mein jaane ke liye <code>/exit_ai</code> likhein.",
            parse_mode=ParseMode.HTML,
        )
        return

    thinking = await update.message.reply_text(
        "<i>Project data padh kar jawab ban raha hai...</i>",
        parse_mode=ParseMode.HTML,
    )
    await animate_message(
        thinking,
        [
            "<i>Reading EnviroFRONT snapshot [■□□]</i>",
            "<i>Generating project answer [■■■]</i>",
        ],
        delay=0.3,
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
            "<b>PROJECT AI ASSISTANT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{esc(reply_txt)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await thinking.edit_text(
            f"<b>AI Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def exit_ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    AI_CHAT_MODE.discard(chat_id)
    await update.message.reply_text(
        "<i>Project AI chat mode band kar diya gaya hai.</i>",
        parse_mode=ParseMode.HTML,
    )


async def ai_analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = require_auth(chat_id)
    if not sess_data:
        await update.message.reply_text(
            "Pehle <code>/login</code> ya <code>/start</code> se login karein.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_box = await update.message.reply_text(
        "<i>Self-analysis shuru ho rahi hai...</i>", parse_mode=ParseMode.HTML
    )
    await animate_message(
        status_box,
        [
            "<i>Scanning devices &amp; sites [■□□]</i>",
            "<i>Running AI diagnostics [■■□]</i>",
            "<i>Compiling classic report [■■■]</i>",
        ],
        delay=0.35,
    )

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        ai_text = await asyncio.to_thread(generate_ai_analysis, analysis)

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>GEMINI AI SELF-ANALYSIS</b>\n"
            f"<i>{esc(analysis['checked_at_ist'])}</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{esc(ai_text)}"
        )
        await status_box.edit_text(msg, parse_mode=ParseMode.HTML)
    except Exception as exc:
        await status_box.edit_text(
            f"<b>AI Analysis Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    state = USER_SESSIONS.get(chat_id)

    if state and state.get("step") == "awaiting_username":
        state["username"] = text
        state["step"] = "awaiting_password"
        await update.message.reply_text(
            "Ab apna <b>Password</b> bhejein:", parse_mode=ParseMode.HTML
        )
        return

    if state and state.get("step") == "awaiting_password":
        username = state["username"]
        password = text

        try:
            await update.message.delete()
        except Exception:
            pass

        auth_box = await update.message.reply_text(
            "<i>EnviroFRONT session verify ho raha hai...</i>",
            parse_mode=ParseMode.HTML,
        )
        await animate_message(
            auth_box,
            [
                "<i>Verifying .ASPXAUTH &amp; CSRF [■□□]</i>",
                "<i>Handshaking with EnviroFRONT [■■□]</i>",
            ],
            delay=0.35,
        )

        session = await asyncio.to_thread(login_envirofront, username, password)

        if session:
            USER_SESSIONS[chat_id] = {
                "step": "authenticated",
                "username": username,
                "session": session,
            }
            await auth_box.edit_text(
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<b>SESSION AUTHENTICATED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Account: <code>{esc(username)}</code>\n"
                "5-Hour Disconnect Auto-Alert: <b>ACTIVE</b>\n\n"
                f"{commands_menu_html()}",
                parse_mode=ParseMode.HTML,
            )
        else:
            USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
            await auth_box.edit_text(
                "<b>Login fail ho gaya.</b> Kripya dobara apna <b>Login ID / Email</b> bhejein:",
                parse_mode=ParseMode.HTML,
            )
        return

    if chat_id in AI_CHAT_MODE:
        sess_data = require_auth(chat_id)
        if not sess_data:
            AI_CHAT_MODE.discard(chat_id)
            await update.message.reply_text(
                "Pehle <code>/login</code> se login karein.", parse_mode=ParseMode.HTML
            )
            return

        ans_msg = await update.message.reply_text(
            "<i>Project data ke hisaab se soch raha hoon...</i>",
            parse_mode=ParseMode.HTML,
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
                "<b>PROJECT AI ASSISTANT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{esc(reply_txt)}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            await ans_msg.edit_text(
                f"<b>AI Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
            )
        return

    if not state:
        await update.message.reply_text(
            "Login ke liye <code>/login</code> ya <code>/start</code> bhejein.",
            parse_mode=ParseMode.HTML,
        )


# ==============================================================================
# BACKGROUND MONITOR (5-HOUR DISCONNECT ALERTS)
# ==============================================================================
async def background_monitor(app) -> None:
    while True:
        now = datetime.now(timezone.utc)
        for chat_id, sess_data in list(USER_SESSIONS.items()):
            if sess_data.get("step") != "authenticated":
                continue

            try:
                analysis = await asyncio.to_thread(
                    run_self_analysis, sess_data["session"]
                )
                LAST_SNAPSHOT[chat_id] = analysis

                for dev in analysis["disconnected_5h_list"]:
                    key = f"{chat_id}:{dev['serial_no']}"
                    last_alert = ALERTED_DEVICES.get(key)
                    if not last_alert or (now - last_alert) > ALERT_COOLDOWN:
                        ALERTED_DEVICES[key] = now
                        hrs = (
                            f"{round(dev['offline_duration'].total_seconds() / 3600, 1)} hours"
                            if dev["offline_duration"]
                            else "Unknown"
                        )
                        alert_msg = (
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            "<b>CRITICAL DISCONNECT (&gt; 5H)</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"▸ <b>Serial:</b> <code>{esc(dev['serial_no'])}</code>\n"
                            f"▸ <b>Site:</b> <b>{esc(dev['site'])}</b>\n"
                            f"▸ <b>Industry:</b> {esc(dev['industry'])}\n"
                            f"▸ <b>Type / FW:</b> <code>{esc(dev['device_type'])}</code> · <code>{esc(dev['firmware'])}</code>\n"
                            f"▸ <b>Offline For:</b> <b>{esc(hrs)}</b>\n"
                            f"▸ <b>Last Seen:</b> <code>{esc(dev['timestamp_raw'])}</code>"
                        )
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=alert_msg,
                            parse_mode=ParseMode.HTML,
                        )

                active_serials = {
                    d["serial_no"] for d in analysis["disconnected_5h_list"]
                }
                for k in list(ALERTED_DEVICES.keys()):
                    if k.startswith(f"{chat_id}:"):
                        serial = k.split(":", 1)[1]
                        if serial not in active_serials:
                            ALERTED_DEVICES.pop(k, None)

            except Exception as exc:
                logger.warning("Background monitor error for %s: %s", chat_id, exc)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def post_init(app) -> None:
    app.create_task(background_monitor(app))


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing hai.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("online", online_cmd))
    app.add_handler(CommandHandler("offline", offline_cmd))
    app.add_handler(CommandHandler("site", site_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
    app.add_handler(CommandHandler("debug_rows", debug_rows_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("exit_ai", exit_ai_cmd))
    app.add_handler(CommandHandler("ai_analyze", ai_analyze_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("EnviroFRONT Classic Premium Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
