import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = ("9018573596:AAE74Vb8hbWyxNQMARkTxYTptCo9a_E11sE")
GEMINI_API_KEY = ("AQ.Ab8RN6I_tfcLUOolcVJFPaJHlKqj3Fdv6izB2vvGx8fC4sjLow")  # Optional: AI analysis ke liye

BASE_URL = "https://envirofront.m2mlogger.com"
LOGIN_URL = f"{BASE_URL}/?ReturnUrl=%2Fpae%2FControlPanel%2FDevices"
DEVICES_URL = f"{BASE_URL}/pae/ControlPanel/Devices"

DISCONNECT_THRESHOLD = timedelta(hours=5)
CHECK_INTERVAL_SECONDS = 30  # Har 5 minute mein background analysis
ALERT_COOLDOWN = timedelta(hours=3)

USER_SESSIONS: dict[int, dict[str, Any]] = {}
ALERTED_DEVICES: dict[str, datetime] = {}
LAST_SNAPSHOT: dict[int, dict[str, Any]] = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("EnviroBot")


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

    get_resp = session.get(LOGIN_URL, timeout=25)
    get_resp.raise_for_status()
    token = extract_verification_token(get_resp.text)

    # Apne login form ke <input name="..."> ke hisab se keys verify karein
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
        verify = session.get(urljoin(BASE_URL, loc or "/pae/ControlPanel/Devices"), timeout=25)
        if verify.ok and "Forgot Password?" not in verify.text:
            return session

    return None


def parse_timestamp(raw: str) -> datetime | None:
    cleaned = " ".join(raw.strip().split())
    if not cleaned:
        return None
    # Screenshot mein '25-Nov-2026 14:32:39' format dikha hai
    formats = (
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %I:%M:%S %p",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
    )
    for fmt in formats:
        try:
            # Server time agar IST hai to +05:30 adjust karein; yahan naive -> UTC ya IST map kar sakte hain
            dt = datetime.strptime(cleaned, fmt)
            ist = timezone(timedelta(hours=5, minutes=30))
            return dt.replace(tzinfo=ist).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def parse_devices_page(html: str) -> list[dict[str, Any]]:
    """
    Devices table rows ko parse karta hai.
    Agar pagination/AJAX endpoint alag ho to yahan extend karein.
    """
    if "500 - Internal server error" in html.lower() or "internal server error" in html.lower():
        raise RuntimeError("EnviroFRONT server returned 500 Internal Server Error on Devices page.")

    soup = BeautifulSoup(html, "html.parser")
    now = datetime.now(timezone.utc)
    devices: list[dict[str, Any]] = []

    for row in soup.select("table tbody tr"):
        cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cols) < 7:
            continue

        # Typical order from screenshot:
        # [Serial No, Industry, Site, Link, Device Type, Firmware, Status, Meta, Timestamp]
        serial_no = cols[0]
        industry = cols[1]
        site = cols[2]
        device_type = cols[4] if len(cols) > 4 else "N/A"
        firmware = cols[5] if len(cols) > 5 else "N/A"
        status_text = cols[6] if len(cols) > 6 else ""
        ts_raw = cols[-1]
        last_seen = parse_timestamp(ts_raw)

        offline_td = (now - last_seen) if last_seen else None
        is_offline_5h = bool(offline_td and offline_td > DISCONNECT_THRESHOLD)

        devices.append(
            {
                "serial_no": serial_no,
                "industry": industry,
                "site": site,
                "device_type": device_type,
                "firmware": firmware,
                "status": status_text,
                "timestamp_raw": ts_raw,
                "last_seen": last_seen,
                "offline_duration": offline_td,
                "disconnected_5h": is_offline_5h,
            }
        )

    return devices


def run_self_analysis(session: requests.Session) -> dict[str, Any]:
    resp = session.get(DEVICES_URL, timeout=25)
    resp.raise_for_status()
    devices = parse_devices_page(resp.text)

    total = len(devices)
    connected = sum(1 for d in devices if "connected" in d["status"].lower() and "dis" not in d["status"].lower())
    disconnected_5h = [d for d in devices if d["disconnected_5h"]]
    disconnected_5h.sort(
        key=lambda x: x["offline_duration"].total_seconds() if x["offline_duration"] else 0,
        reverse=True,
    )

    site_offline_counts: dict[str, int] = {}
    fw_counts: dict[str, int] = {}
    for d in devices:
        fw = d["firmware"] or "Unknown"
        fw_counts[fw] = fw_counts.get(fw, 0) + 1
        if d["disconnected_5h"]:
            site = d["site"] or "Unknown Site"
            site_offline_counts[site] = site_offline_counts.get(site, 0) + 1

    top_sites = sorted(site_offline_counts.items(), key=lambda kv: kv[1], reverse=True)[:5]

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "connected": connected,
        "disconnected_5h_count": len(disconnected_5h),
        "disconnected_5h_list": disconnected_5h,
        "top_offline_sites": top_sites,
        "firmware_breakdown": fw_counts,
    }


def generate_ai_analysis(analysis: dict[str, Any]) -> str:
    if not GEMINI_API_KEY:
        return "GEMINI_API_KEY set nahi hai—sirf rule-based self-analysis chal rahi hai."

    client = genai.Client(api_key=GEMINI_API_KEY)
    compact_offline = [
        {
            "serial": d["serial_no"],
            "site": d["site"],
            "industry": d["industry"],
            "fw": d["firmware"],
            "offline_hours": round(d["offline_duration"].total_seconds() / 3600, 1)
            if d["offline_duration"]
            else None,
            "last_seen": d["timestamp_raw"],
        }
        for d in analysis["disconnected_5h_list"][:15]
    ]

    prompt = (
        "Tum ek industrial telemetry & IoT monitoring analyst ho. "
        "Neeche EnviroFRONT devices ka analysis data diya gaya hai. "
        "Hinglish mein concise, actionable root-cause analysis aur priority action items likho "
        "(jaise kaun si site/device sabse critical hai, firmware/connectivity patterns kya dikh rahe hain):\n\n"
        f"Summary: total={analysis['total']}, connected={analysis['connected']}, "
        f"disconnected_over_5h={analysis['disconnected_5h_count']}, "
        f"top_offline_sites={analysis['top_offline_sites']}, "
        f"firmware_breakdown={analysis['firmware_breakdown']}\n"
        f"Top Disconnected Devices (>5h): {compact_offline}"
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text or "AI analysis empty aaya."


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
    await update.message.reply_text(
        "EnviroFRONT Advanced Bot mein swagat hai.\n"
        "Kripya apna **Login ID / Email** bhejein:"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        await update.message.reply_text("Pehle /start se login karein.")
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        sites_str = "\n".join(f"• {s}: {c} offline (>5h)" for s, c in analysis["top_offline_sites"]) or "None"
        await update.message.reply_text(
            f"**EnviroFRONT Status**\n"
            f"Total Devices: `{analysis['total']}`\n"
            f"Connected: `{analysis['connected']}`\n"
            f"Disconnected (>5h): `{analysis['disconnected_5h_count']}`\n\n"
            f"**Top Offline Sites (>5h):**\n{sites_str}",
            parse_mode="Markdown",
        )
    except Exception as exc:
        await update.message.reply_text(f"Analysis error: {exc}")


async def offline_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        await update.message.reply_text("Pehle /start se login karein.")
        return

    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        off_list = analysis["disconnected_5h_list"]
        if not off_list:
            await update.message.reply_text("Koi bhi device 5 ghante se zyada offline nahi hai.")
            return

        lines = [f"**Disconnected > 5 Hours ({len(off_list)}):**"]
        for d in off_list[:20]:
            hrs = round(d["offline_duration"].total_seconds() / 3600, 1)
            lines.append(
                f"• `{d['serial_no']}` | {d['site']} | **{hrs}h** | Last: `{d['timestamp_raw']}`"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def ai_analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    sess_data = USER_SESSIONS.get(chat_id)
    if not sess_data or sess_data.get("step") != "authenticated":
        await update.message.reply_text("Pehle /start se login karein.")
        return

    await update.message.reply_text("Data analyze karke AI report taiyar ki ja rahi hai...")
    try:
        analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
        LAST_SNAPSHOT[chat_id] = analysis
        ai_text = await asyncio.to_thread(generate_ai_analysis, analysis)
        await update.message.reply_text(ai_text)
    except Exception as exc:
        await update.message.reply_text(f"AI analysis error: {exc}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    state = USER_SESSIONS.get(chat_id)

    if not state:
        await update.message.reply_text("Login shuru karne ke liye /start bhejein.")
        return

    if state.get("step") == "awaiting_username":
        state["username"] = text
        state["step"] = "awaiting_password"
        await update.message.reply_text("Ab apna **Password** bhejein:")

    elif state.get("step") == "awaiting_password":
        username = state["username"]
        password = text

        try:
            await update.message.delete()
        except Exception:
            pass

        await update.message.reply_text("EnviroFRONT mein login verify ho raha hai...")
        session = await asyncio.to_thread(login_envirofront, username, password)

        if session:
            USER_SESSIONS[chat_id] = {
                "step": "authenticated",
                "username": username,
                "session": session,
            }
            await update.message.reply_text(
                f"Login safal ({username}).\n"
                "Auto-analysis & 5-hour disconnect alert active hai.\n"
                "Commands: /status, /offline, /ai_analyze"
            )
        else:
            USER_SESSIONS[chat_id] = {"step": "awaiting_username"}
            await update.message.reply_text(
                "Login fail ho gaya. Dobara apni **Login ID / Email** bhejein:"
            )


async def background_monitor(app) -> None:
    while True:
        now = datetime.now(timezone.utc)
        for chat_id, sess_data in list(USER_SESSIONS.items()):
            if sess_data.get("step") != "authenticated":
                continue

            try:
                analysis = await asyncio.to_thread(run_self_analysis, sess_data["session"])
                LAST_SNAPSHOT[chat_id] = analysis

                for dev in analysis["disconnected_5h_list"]:
                    key = f"{chat_id}:{dev['serial_no']}"
                    last_alert = ALERTED_DEVICES.get(key)
                    if not last_alert or (now - last_alert) > ALERT_COOLDOWN:
                        ALERTED_DEVICES[key] = now
                        hrs = round(dev["offline_duration"].total_seconds() / 3600, 1)
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                "🚨 **Device Disconnect Alert (>5 Hours)**\n"
                                f"Serial: `{dev['serial_no']}`\n"
                                f"Site: **{dev['site']}** ({dev['industry']})\n"
                                f"Type/FW: `{dev['device_type']}` | FW `{dev['firmware']}`\n"
                                f"Disconnected For: **{hrs} hours**\n"
                                f"Last Timestamp: `{dev['timestamp_raw']}`"
                            ),
                            parse_mode="Markdown",
                        )

                active_serials = {d["serial_no"] for d in analysis["disconnected_5h_list"]}
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
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("offline", offline_cmd))
    app.add_handler(CommandHandler("ai_analyze", ai_analyze_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()


if __name__ == "__main__":
    main()
