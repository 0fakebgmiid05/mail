import asyncio
import logging
import os
import re
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
# CONFIGURATION
# ==============================================================================
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or "9018573596:AAE74Vb8hbWyxNQMARkTxYTptCo9a_E11sE"
).strip()

GEMINI_API_KEY = "AQ.Ab8RN6I9cos8rT2ACgxwY70oDOEiigJFEL7O1CBnpwQ0l9VQgA"
BASE_URL = "https://envirofront.m2mlogger.com"
LOGIN_URL = f"{BASE_URL}/?ReturnUrl=%2Fpae%2FControlPanel%2FDevices"
DEVICES_URL = f"{BASE_URL}/pae/ControlPanel/Devices"

IST = timezone(timedelta(hours=5, minutes=30))
DISCONNECT_THRESHOLD = timedelta(hours=5)
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
ALERT_COOLDOWN = timedelta(hours=int(os.getenv("ALERT_COOLDOWN_HOURS", "6")))

USER_SESSIONS: dict[int, dict[str, Any]] = {}
ALERTED_DEVICES: dict[str, datetime] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}

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


async def animate_message(msg, frames: list[str], delay: float = 0.45) -> None:
    """Classic text animation via message edits."""
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
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
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


def parse_devices_page(
    html: str, log_sample: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "server_error_500": False,
        "html_length": len(html or ""),
        "tr_total": 0,
        "unparsed_ts_count": 0,
        "source": "html",
    }

    if (
        not html
        or "500 - internal server error" in html.lower()
        or "internal server error" in html.lower()
    ):
        meta["server_error_500"] = True
        return [], meta

    soup = BeautifulSoup(html, "html.parser")
    all_trs = soup.select("tr")
    meta["tr_total"] = len(all_trs)

    if log_sample:
        logger.info("HTML length=%d | Total <tr>=%d", len(html), len(all_trs))
        for idx, tr in enumerate(all_trs[:5]):
            snippet = " | ".join(tr.get_text(" ", strip=True).split())[:260]
            logger.info("Sample TR[%d]: %s", idx, snippet)

    now = datetime.now(timezone.utc)
    devices: list[dict[str, Any]] = []

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
        is_connected = (
            "connected" in status_text.lower()
            and "disconnected" not in status_text.lower()
        )

        ts_raw = ts_match.group(0) if ts_match else cols[-1]
        last_seen = parse_timestamp(ts_raw)
        if not last_seen:
            meta["unparsed_ts_count"] += 1

        offline_td = (now - last_seen) if last_seen else None
        is_offline_5h = bool(
            (offline_td and offline_td > DISCONNECT_THRESHOLD)
            or (not is_connected and offline_td is None)
        )

        devices.append(
            {
                "serial_no": serial_no,
                "industry": industry,
                "site": site,
                "device_type": device_type,
                "firmware": firmware,
                "status": status_text,
                "is_connected": is_connected,
                "timestamp_raw": ts_raw,
                "last_seen": last_seen,
                "offline_duration": offline_td,
                "disconnected_5h": is_offline_5h,
            }
        )

    return devices, meta


def discover_and_fetch_ajax_devices(
    session: requests.Session, html: str
) -> list[dict[str, Any]]:
    candidates = set(
        re.findall(
            r"""["'](/pae/[^"']*(?:Device|Grid|Data|List|Get|Load|Table)[^"']*)["']""",
            html,
            re.IGNORECASE,
        )
    )
    candidates.update(
        {
            "/pae/ControlPanel/GetDevices",
            "/pae/ControlPanel/DevicesList",
            "/pae/ControlPanel/LoadDevices",
            "/pae/ControlPanel/GetAllDevices",
        }
    )

    now = datetime.now(timezone.utc)
    found: list[dict[str, Any]] = []

    for path in list(candidates)[:12]:
        url = urljoin(BASE_URL, path)
        for method in ("GET", "POST"):
            try:
                if method == "GET":
                    r = session.get(url, timeout=15)
                else:
                    r = session.post(
                        url,
                        data={"draw": "1", "start": "0", "length": "500"},
                        timeout=15,
                    )

                if (
                    not r.ok
                    or "application/json" not in r.headers.get("Content-Type", "").lower()
                ):
                    continue

                payload = r.json()
                rows = (
                    payload.get("data")
                    or payload.get("aaData")
                    or payload.get("rows")
                    or payload.get("items")
                    or (payload if isinstance(payload, list) else [])
                )
                if not isinstance(rows, list) or not rows:
                    continue

                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    blob = " ".join(str(v) for v in item.values() if v is not None)
                    if not (
                        "connected" in blob.lower()
                        or "disconnected" in blob.lower()
                        or "logpro" in blob.lower()
                    ):
                        continue

                    serial_no = str(
                        item.get("SerialNo")
                        or item.get("Serial_No")
                        or item.get("serialNo")
                        or item.get("Id")
                        or "N/A"
                    )
                    site = str(
                        item.get("SiteName")
                        or item.get("Site")
                        or item.get("site")
                        or "N/A"
                    )
                    industry = str(
                        item.get("IndustryName")
                        or item.get("Industry")
                        or item.get("industry")
                        or "N/A"
                    )
                    device_type = str(
                        item.get("DeviceType")
                        or item.get("deviceType")
                        or "LOGPRO PLUS"
                    )
                    firmware = str(
                        item.get("Firmware") or item.get("firmware") or "N/A"
                    )
                    status_text = str(
                        item.get("Status") or item.get("status") or ""
                    )
                    ts_raw = str(
                        item.get("Timestamp")
                        or item.get("LastSeen")
                        or item.get("timestamp")
                        or ""
                    )

                    is_connected = (
                        "connected" in status_text.lower()
                        and "disconnected" not in status_text.lower()
                    )
                    last_seen = parse_timestamp(ts_raw)
                    offline_td = (now - last_seen) if last_seen else None
                    is_offline_5h = bool(
                        (offline_td and offline_td > DISCONNECT_THRESHOLD)
                        or (not is_connected and offline_td is None)
                    )

                    found.append(
                        {
                            "serial_no": serial_no,
                            "industry": industry,
                            "site": site,
                            "device_type": device_type,
                            "firmware": firmware,
                            "status": status_text
                            or ("Connected" if is_connected else "Disconnected"),
                            "is_connected": is_connected,
                            "timestamp_raw": ts_raw,
                            "last_seen": last_seen,
                            "offline_duration": offline_td,
                            "disconnected_5h": is_offline_5h,
                        }
                    )

                if found:
                    logger.info(
                        "AJAX endpoint matched: %s (%s) -> %d devices",
                        method,
                        path,
                        len(found),
                    )
                    return found
            except Exception:
                continue

    return []


def run_self_analysis(
    session: requests.Session, log_sample: bool = False
) -> dict[str, Any]:
    resp = session.get(DEVICES_URL, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    devices, meta = parse_devices_page(resp.text, log_sample=log_sample)

    if len(devices) == 0 and not meta["server_error_500"]:
        ajax_devices = discover_and_fetch_ajax_devices(session, resp.text)
        if ajax_devices:
            devices = ajax_devices
            meta["source"] = "ajax/json"

    if len(devices) == 0 and not meta["server_error_500"]:
        scripts_text = "\n".join(
            s.get_text(" ", strip=True) for s in soup.find_all("script")
        )
        urls_found = set(re.findall(r"""["'](/[^"']{3,100})["']""", resp.text))
        interesting_urls = [
            u
            for u in sorted(urls_found)
            if any(
                k in u.lower()
                for k in (
                    "device",
                    "grid",
                    "data",
                    "list",
                    "get",
                    "load",
                    "pae",
                    "control",
                )
            )
        ]
        logger.info("Candidate URLs in HTML: %s", interesting_urls[:20])
        logger.info("Script snippet: %s", " ".join(scripts_text.split())[:600])

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


def generate_ai_analysis(analysis: dict[str, Any]) -> str:
    if not GEMINI_API_KEY:
        return (
            "<i>Gemini API key set nahi hai.</i>\n"
            "Rule-based self-analysis ke mutabik "
            f"<b>{analysis['disconnected_5h_count']}</b> devices 5+ hours se offline hain."
        )

    client = genai.Client(api_key=GEMINI_API_KEY)
    compact_offline = [
        {
            "serial": d["serial_no"],
            "site": d["site"],
            "industry": d["industry"],
            "fw": d["firmware"],
            "offline_hours": round(d["offline_duration"].total_seconds() / 3600, 1)
            if d["offline_duration"]
            else "Unknown",
            "last_seen": d["timestamp_raw"],
        }
        for d in analysis["disconnected_5h_list"][:15]
    ]

    prompt = (
        "Tum ek senior industrial telemetry & IoT monitoring engineer ho. "
        "Neeche EnviroFRONT (M2MLogger) devices ka self-analysis snapshot diya gaya hai. "
        "Hinglish mein crisp, executive aur actionable analysis do:\n"
        "1. Overall Health & Risk Level\n"
        "2. Sabse Critical Sites / Devices (>5h offline)\n"
        "3. Firmware / Server / Connectivity Patterns\n"
        "4. Immediate Action Checklist (short bullets)\n"
        "Plain text / simple lines rakho (complex markdown symbols mat lagao).\n\n"
        f"Snapshot: total={analysis['total']}, connected={analysis['connected']}, "
        f"disconnected_over_5h={analysis['disconnected_5h_count']}, "
        f"top_offline_sites={analysis['top_offline_sites']}, "
        f"firmware_breakdown={analysis['firmware_breakdown']}, "
        f"meta={analysis['meta']}\n"
        f"Top Offline Devices (>5h): {compact_offline}"
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text or "AI analysis se koi response nahi aaya."


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
        "• <code>/ai_analyze</code> — Gemini AI report\n"
        "• <code>/login</code> — Switch / re-login account\n"
        "• <code>/logout</code> — End active session"
    )


# ==============================================================================
# COMMAND HANDLERS
# ==============================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    existing = require_auth(chat_id)

    # Agar pehle se login hai, to dobara ID/Password nahi maangega
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
            "• <b>Gemini AI</b> self-analysis &amp; root-cause summary\n\n"
            "Kripya apna <b>Login ID / Email</b> bhejein:",
        ],
        delay=0.35,
    )


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
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
            f"▸ <b>Offline (&gt; 5h):</b> <code>{analysis['disconnected_5h_count']}</code>\n\n"
            "<b>Priority Offline Sites (&gt;5h)</b>\n"
            f"{sites_lines}\n\n"
            "<i>Commands: /online · /offline · /site · /health · /ai_analyze · /logout</i>"
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
            f"▸ <b>Data Source:</b> <code>{esc(meta.get('source', 'html'))}</code>\n"
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
        await update.message.reply_text(
            "Debug rows Railway logs mein print kar diye gaye hain.\n"
            f"TR count: <code>{meta['tr_total']}</code> | Parsed: <code>{analysis['total']}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await update.message.reply_text(
            f"<b>Debug Error:</b> <code>{esc(exc)}</code>", parse_mode=ParseMode.HTML
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
            "<i>Running Gemini AI diagnostics [■■□]</i>",
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

    if not state:
        await update.message.reply_text(
            "Login ke liye <code>/login</code> ya <code>/start</code> bhejein.",
            parse_mode=ParseMode.HTML,
        )
        return

    if state.get("step") == "awaiting_username":
        state["username"] = text
        state["step"] = "awaiting_password"
        await update.message.reply_text(
            "Ab apna <b>Password</b> bhejein:", parse_mode=ParseMode.HTML
        )

    elif state.get("step") == "awaiting_password":
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
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
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
    app.add_handler(CommandHandler("ai_analyze", ai_analyze_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("EnviroFRONT Classic Premium Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
