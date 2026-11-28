import ast
import asyncio
import json
import logging
import os
import random
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("CompanionCoreController")

# ==============================================================================
# ENVIRONMENT & SHARED AUTHORITY CONFIG
# ==============================================================================
COMPANION_BOT_TOKEN: str = (
    os.getenv("SECOND_BOT_TOKEN", "").strip()
    or os.getenv("COMPANION_BOT_TOKEN", "").strip()
)
MAIN_BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
THIRD_BOT_TOKEN: str = (
    os.getenv("THIRD_BOT_TOKEN", "").strip()
    or os.getenv("DYNAMIC_BOT_TOKEN", "").strip()
)
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
ADMIN_TG_ID: int = int(
    os.getenv("ADMIN_TG_ID", "1390658041").strip() or "1390658041"
)
DEFAULT_CLIENT_NAME: str = (
    os.getenv("CLIENT_NAME", "BAAGI JAAT").strip() or "BAAGI JAAT"
)
DB_PATH: str = os.getenv("BOT_DB_PATH", "baagijaat_telemetry.db").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
THIRD_BOT_FILE = os.path.join(BASE_DIR, "third_bot.py")
BACKUP_DIR = os.path.join(BASE_DIR, "bot_backups")

IST = timezone(timedelta(hours=5, minutes=30))
GROQ_CHAT_URL: str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL: str = "https://api.groq.com/openai/v1/models"

_SMALL_CAPS = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
)

# Runtime States
AWAITING_INPUT: dict[int, str] = {}
MENU_STACK: dict[int, list[str]] = {}
MENU_STATE: dict[int, str] = {}
ACTIVE_STORY_ID: dict[int, int] = {}
LAST_SYNC_META: dict[str, Any] = {
    "last_sync_ist": "Not synced yet",
    "snapshots_count": 0,
    "watchlist_count": 0,
    "alerts_count": 0,
    "story_saves_count": 0,
    "sync_version": 1,
}

DEFAULT_CUSTOM_CONFIG: dict[str, str] = {
    "bot_title": f"{DEFAULT_CLIENT_NAME} COMPANION",
    "welcome_banner": "Murim Story • 1st/2nd/3rd Sync • Live 3rd Bot AI Editor",
    "btn_story": "📜 Murim Story Mode",
    "btn_sync": "🔄 Sync Bots",
    "btn_admin": "👑 Admin Panel",
    "bot1_theme": "obsidian",
    "bot1_notif_style": "detailed",
    "bot1_refresh_sec": "60",
    "bot1_maint_mode": "0",
    "bot1_bot_enabled": "1",
    "bot1_maint_msg": "System maintenance chal raha hai. Kripya thodi der baad try karein.",
}


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and int(user_id) == int(ADMIN_TG_ID)


def sc(text: str) -> str:
    return (text or "").upper().translate(_SMALL_CAPS)


def esc_html(val: Any) -> str:
    s = str(val if val is not None else "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def frame_box(title: str, subtitle: str = "") -> str:
    lines = [
        "╔═◈══════════════════════◈═╗",
        f"║ ❖ <b>{esc_html(title)}</b>",
    ]
    if subtitle:
        lines.append(f"║ ▸ <i>{esc_html(subtitle)}</i>")
    lines.append("╚═◈══════════════════════◈═╝")
    return "\n".join(lines)


# ==============================================================================
# SHARED SQLITE DATABASE SYNC + STORY PERSISTENCE
# ==============================================================================
def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_companion_db() -> None:
    conn = get_db_conn()
    try:
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
            CREATE TABLE IF NOT EXISTS admin_custom_config (
                cfg_key TEXT PRIMARY KEY,
                cfg_val TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_heartbeats (
                service_name TEXT PRIMARY KEY,
                last_ping TEXT NOT NULL,
                sync_version INTEGER NOT NULL,
                details_json TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS story_saves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                arc_type TEXT NOT NULL,
                chapter INTEGER NOT NULL,
                realm TEXT NOT NULL,
                demonic_qi INTEGER NOT NULL,
                hp INTEGER NOT NULL,
                current_scene TEXT NOT NULL,
                opt_a TEXT NOT NULL,
                opt_b TEXT NOT NULL,
                history_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        for k, v in DEFAULT_CUSTOM_CONFIG.items():
            cur.execute(
                """
                INSERT OR IGNORE INTO admin_custom_config(cfg_key, cfg_val, updated_at)
                VALUES(?, ?, ?)
                """,
                (k, v, now_ist_str()),
            )
        conn.commit()
    finally:
        conn.close()


def get_custom_config() -> dict[str, str]:
    cfg = dict(DEFAULT_CUSTOM_CONFIG)
    try:
        conn = get_db_conn()
        try:
            for r in conn.execute(
                "SELECT cfg_key, cfg_val FROM admin_custom_config"
            ).fetchall():
                cfg[str(r["cfg_key"])] = str(r["cfg_val"])
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("get_custom_config error: %s", exc)
    return cfg


def push_customization_to_db(key: str, val: str) -> None:
    clean_val = str(val).strip()
    stamp = now_ist_str()
    conn = get_db_conn()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO admin_custom_config(cfg_key, cfg_val, updated_at)
            VALUES(?, ?, ?)
            """,
            (key, clean_val, stamp),
        )
        mirror_map = {
            "bot1_theme": "active_theme",
            "bot1_notif_style": "notif_style",
            "bot1_refresh_sec": "refresh_sec",
            "bot1_maint_mode": "maint_mode",
            "bot1_bot_enabled": "bot_enabled",
            "bot1_maint_msg": "maint_msg",
            "bot_title": "companion_title",
        }
        bot1_key = mirror_map.get(key)
        if bot1_key:
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings(k, v) VALUES(?, ?)",
                (bot1_key, clean_val),
            )
        conn.commit()
    finally:
        conn.close()


def sync_bots_db() -> dict[str, Any]:
    snap_cnt = 0
    watch_cnt = 0
    alert_cnt = 0
    story_cnt = 0
    latest_snapshots: list[dict[str, Any]] = []
    bot1_settings: dict[str, str] = {}

    conn = get_db_conn()
    try:
        cur = conn.cursor()
        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "snapshots" in tables:
            s_rows = cur.execute(
                "SELECT chat_id, checked_at, payload_json FROM snapshots"
            ).fetchall()
            snap_cnt = len(s_rows)
            for sr in s_rows[:5]:
                try:
                    payload = json.loads(sr["payload_json"] or "{}")
                except Exception:
                    payload = {}
                latest_snapshots.append(
                    {
                        "chat_id": sr["chat_id"],
                        "checked_at": sr["checked_at"],
                        "total": payload.get("total_count", 0),
                        "online": payload.get("online_count", 0),
                        "offline": payload.get("offline_count", 0),
                        "stale": payload.get("stale_count", 0),
                    }
                )

        if "user_watchlist" in tables:
            watch_cnt = int(
                cur.execute("SELECT COUNT(*) FROM user_watchlist").fetchone()[0]
            )

        if "sent_alerts" in tables:
            alert_cnt = int(
                cur.execute("SELECT COUNT(*) FROM sent_alerts").fetchone()[0]
            )

        if "story_saves" in tables:
            story_cnt = int(
                cur.execute("SELECT COUNT(*) FROM story_saves").fetchone()[0]
            )

        if "bot_settings" in tables:
            for b_row in cur.execute("SELECT k, v FROM bot_settings").fetchall():
                bot1_settings[str(b_row["k"])] = str(b_row["v"])

        new_ver = int(LAST_SYNC_META.get("sync_version", 1)) + 1
        ping_time = now_ist_str()
        meta = {
            "last_sync_ist": ping_time,
            "snapshots_count": snap_cnt,
            "watchlist_count": watch_cnt,
            "alerts_count": alert_cnt,
            "story_saves_count": story_cnt,
            "sync_version": new_ver,
            "latest_snapshots": latest_snapshots,
            "bot1_settings": bot1_settings,
        }
        cur.execute(
            """
            INSERT OR REPLACE INTO sync_heartbeats(service_name, last_ping, sync_version, details_json)
            VALUES('companion_bot', ?, ?, ?)
            """,
            (ping_time, new_ver, json.dumps(meta, default=str)),
        )
        conn.commit()
        LAST_SYNC_META.clear()
        LAST_SYNC_META.update(meta)
        return meta
    finally:
        conn.close()


# ==============================================================================
# RESILIENT GROQ AI ENGINE
# ==============================================================================
_CACHED_GROQ_MODELS: list[str] = []
_CACHED_MODELS_AT: float = 0.0


def get_groq_models() -> list[str]:
    global _CACHED_GROQ_MODELS, _CACHED_MODELS_AT
    fallback = [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "gemma2-9b-it",
        "mixtral-8x7b-32768",
    ]
    if not GROQ_API_KEY:
        return fallback
    if _CACHED_GROQ_MODELS and (time.time() - _CACHED_MODELS_AT) < 900:
        return _CACHED_GROQ_MODELS
    try:
        r = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            timeout=12,
        )
        if r.ok:
            data = r.json()
            ids = [
                str(m.get("id"))
                for m in (data.get("data") or [])
                if isinstance(m, dict) and m.get("id")
            ]
            preferred = [
                mid
                for mid in ids
                if any(
                    k in mid.lower() for k in ("llama", "gemma", "qwen", "mixtral")
                )
                and "whisper" not in mid.lower()
                and "guard" not in mid.lower()
            ]
            ordered: list[str] = []
            for m in preferred + fallback:
                if m not in ordered:
                    ordered.append(m)
            _CACHED_GROQ_MODELS = ordered[:6]
            _CACHED_MODELS_AT = time.time()
            return _CACHED_GROQ_MODELS
    except Exception as exc:
        logger.warning("Groq model list fetch note: %s", exc)
    return fallback


def call_groq_resilient(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1800,
    temperature: float = 0.2,
) -> tuple[bool, str]:
    if not GROQ_API_KEY:
        return False, "GROQ_API_KEY missing hai."

    models = get_groq_models()
    last_reason = "Groq se response nahi mila."

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
                },
                timeout=30,
            )
            if r.ok:
                data = r.json()
                content = (
                    (((data.get("choices") or [{}])[0]).get("message") or {})
                    .get("content")
                    or ""
                ).strip()
                if content:
                    return True, content
                last_reason = f"{model_id} ne khali response diya."
            else:
                last_reason = f"{model_id} HTTP {r.status_code}"
        except Exception as exc:
            last_reason = f"{model_id}: {exc}"

    return False, last_reason


# ==============================================================================
# 3RD BOT LIVE EDITOR + AI COMMAND ENGINE
# ==============================================================================
def _clean_and_normalize_python(raw: str) -> str:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Repair mangled double-underscore main guard if present
    cleaned = cleaned.replace('if name == "main":', 'if __name__ == "__main__":')
    cleaned = cleaned.replace("if name == 'main':", "if __name__ == '__main__':")
    return cleaned


def apply_code_to_third_bot(raw_code: str) -> tuple[bool, str]:
    code = _clean_and_normalize_python(raw_code)
    if not code:
        return False, "Code khali hai."

    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError (Line {exc.lineno}): {exc.msg}"

    os.makedirs(BACKUP_DIR, exist_ok=True)
    if os.path.exists(THIRD_BOT_FILE):
        stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(
            THIRD_BOT_FILE, os.path.join(BACKUP_DIR, f"third_bot_{stamp}.py")
        )

    with open(THIRD_BOT_FILE, "w", encoding="utf-8") as f:
        f.write(code + "\n")

    return (
        True,
        f"third_bot.py live update ho gaya ({len(code.splitlines())} lines). Runner 2s mein reload karega!",
    )


def ai_edit_third_bot_code(instruction: str) -> tuple[bool, str]:
    existing_code = ""
    if os.path.exists(THIRD_BOT_FILE):
        with open(THIRD_BOT_FILE, "r", encoding="utf-8") as f:
            existing_code = f.read()

    sys_prompt = (
        "You are an expert Python Telegram bot developer using python-telegram-bot v20+. "
        "Modify or build the complete, runnable third_bot.py based on the admin instruction. "
        "Use os.getenv('THIRD_BOT_TOKEN') or os.getenv('DYNAMIC_BOT_TOKEN') and drop_pending_updates=True. "
        "If asked to remove all buttons, use ReplyKeyboardRemove() or omit ReplyKeyboardMarkup/InlineKeyboardMarkup completely. "
        "If asked for notifications, include notification sending capability cleanly. "
        "Return ONLY raw valid Python code with proper __name__ == '__main__' and no markdown fences."
    )
    prompt = (
        f"CURRENT third_bot.py:\n{existing_code[:3800]}\n\n"
        f"ADMIN INSTRUCTION:\n{instruction}\n\n"
        "Output ONLY complete valid Python code for third_bot.py:"
    )

    last_err = "AI response fail"
    for _ in range(3):
        ok, generated = call_groq_resilient(
            sys_prompt, prompt, max_tokens=2200, temperature=0.15
        )
        if not ok:
            last_err = generated
            continue

        code = _clean_and_normalize_python(generated)
        try:
            ast.parse(code)
            return apply_code_to_third_bot(code)
        except SyntaxError as exc:
            last_err = f"Line {exc.lineno}: {exc.msg}"
            prompt = (
                f"Previous code had SyntaxError ({last_err}). Fix it completely and return ONLY valid Python code:\n"
                f"{code[:3800]}"
            )

    return False, f"AI 3 attempts mein valid syntax nahi bana paya ({last_err}). Manual Code use karein."


async def ai_send_direct_notification(bot, instruction: str) -> tuple[bool, str]:
    """Allows AI to parse and send an immediate Telegram notification to target chat/admin."""
    target_chat_id = ADMIN_TG_ID
    msg_text = instruction.strip()

    ok, parsed = await asyncio.to_thread(
        call_groq_resilient,
        "Extract notification target chat_id (default null) and clean notification message text from the user instruction. "
        "Return STRICT JSON only: {\"chat_id\": null_or_int, \"message\": \"...\"}.",
        instruction,
        220,
        0.1,
    )
    if ok:
        try:
            i1 = parsed.find("{")
            i2 = parsed.rfind("}")
            if i1 != -1 and i2 > i1:
                obj = json.loads(parsed[i1 : i2 + 1])
                if isinstance(obj.get("chat_id"), int):
                    target_chat_id = int(obj["chat_id"])
                elif isinstance(obj.get("chat_id"), str) and obj["chat_id"].lstrip("-").isdigit():
                    target_chat_id = int(obj["chat_id"])
                if obj.get("message"):
                    msg_text = str(obj["message"]).strip()
        except Exception:
            pass

    if not msg_text:
        return False, "Notification message khali hai."

    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=f"🔔 <b>AI NOTIFICATION</b>\n{esc_html(msg_text)}",
            parse_mode=ParseMode.HTML,
        )
        return True, f"Notification sent to <code>{target_chat_id}</code>!"
    except Exception as exc:
        return False, f"Notification send fail: {esc_html(exc)}"


def get_third_bot_preview() -> str:
    if not os.path.exists(THIRD_BOT_FILE):
        return "third_bot.py abhi disk par maujood nahi hai."
    with open(THIRD_BOT_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    lines = content.splitlines()
    snippet = "\n".join(lines[-45:])
    return (
        f"{frame_box(sc('THIRD_BOT.PY PREVIEW'), f'Total Lines: {len(lines)}')}\n"
        f"<code>{esc_html(snippet[-1400:])}</code>"
    )


def rollback_third_bot_code() -> tuple[bool, str]:
    if not os.path.isdir(BACKUP_DIR):
        return False, "Koi backup folder nahi mila."
    backups = sorted(
        f
        for f in os.listdir(BACKUP_DIR)
        if f.startswith("third_bot_") and f.endswith(".py")
    )
    if not backups:
        return False, "Restore karne ke liye koi backup nahi hai."
    latest = os.path.join(BACKUP_DIR, backups[-1])
    shutil.copy2(latest, THIRD_BOT_FILE)
    return True, f"Rollback complete from <code>{esc_html(backups[-1])}</code>!"


# ==============================================================================
# KAVANA-STYLE CRAZY FANTASY / MURIM / MURIM LOGIN / DEMONIC EMPEROR STORY ENGINE
# ==============================================================================
STORY_STARTERS: list[dict[str, str]] = [
    {
        "title": "Demonic Emperor: Blood Heaven Awakening",
        "arc": "Demonic Emperor • Crazy Fantasy",
        "realm": "Demonic Qi Condensation Lv.1",
        "scene": (
            "Aasman mein kaali bijli garajti hai aur tumhara purana sharir 'Demonic Emperor' ki aatma ke saath reborn hota hai! "
            "Samne Blood Sect ke elders aur ek ajeeb glowing 'Murim Login Gate' khul chuka hai."
        ),
        "opt_a": "A: Murim Login Gate ke andar kadam rakho aur Demonic System unlock karo",
        "opt_b": "B: Blood Sect elders ke saamne Demonic Aura chhod kar unhe ghutno par lao",
    },
    {
        "title": "Murim Login: Crazy Sword God Ascension",
        "arc": "Martial Art Cultivation • Murim Login",
        "realm": "Mortal Body → Sword Qi Initiate",
        "scene": (
            "Subah aankh khulte hi tumhare samne ek crazy neon prompt chamakta hai: [MURIM LOGIN SUCCESSFUL — 999x Cultivation Multiplier]! "
            "Bahar Heavenly Sword Pavilion ke disciples ek ancient martial tablet par lad rahe hain."
        ),
        "opt_a": "A: Sword Tablet absorb karke Crazy Demonic Sword Art seekho",
        "opt_b": "B: Pavilion Elder ke saath challenge lekar Murim ranking mein top par aao",
    },
    {
        "title": "Crazy Murim Abyss: Sovereign of Ten Thousand Demons",
        "arc": "Crazy Fantasy • Murim & Demonic Realm",
        "realm": "Abyssal Foundation Lv.1",
        "scene": (
            "Ek crazy dimensional rift se tum Murim ke sabse khatarnak 'Abyssal Demon Valley' mein girte ho. "
            "Ek talking Spirit Fox kehti hai ki yahan har pill ya sword crazy side-effects deta hai par unlimited power bhi!"
        ),
        "opt_a": "A: Spirit Fox ke saath Crazy Forbidden Pill nigal kar Realm break karo",
        "opt_b": "B: Demon Lord ke throne hall mein ghus kar direct Demonic Crown chhino",
    },
]

FALLBACK_BRANCHES: list[dict[str, Any]] = [
    {
        "scene": (
            "Tumhara decision poore Murim world mein goonj uthta hai! Aasman se Demonic Dragon ki dahad sunai deti hai "
            "aur tumhare meridians mein crazy golden-black energy daudne lagti hai. Ek masked Martial Saint tumhe parakh raha hai."
        ),
        "opt_a": "A: Masked Saint ke attack ko Demonic Palm se tod kar aage badho",
        "opt_b": "B: Illusion step se peeche jaakar Hidden Ancient Vault kholo",
        "qi_delta": 35,
        "hp_delta": -5,
    },
    {
        "scene": (
            "Murim Login System ek naya crazy quest deta hai: [Defeat the Immortal Sect Heir or Claim the Demon Core]. "
            "Charon taraf qi storms ban rahe hain aur ancient sects ke elders shock mein hain."
        ),
        "opt_a": "A: Demon Core ko absorb karke crazy Demonic Emperor aura unleash karo",
        "opt_b": "B: Ancient Formation ko reverse karke poore sect ka qi apni talwar mein bharo",
        "qi_delta": 50,
        "hp_delta": 10,
    },
    {
        "scene": (
            "Crazy fantasy twist aata hai — ek golden phoenix aur ek abyssal demon ek hi talwar ke andar merge ho jaate hain! "
            "Tum ab Demonic Martial cultivation ke ek naye crazy level par khade ho."
        ),
        "opt_a": "A: Twin-Element Strike se Mountain Peak ko do hisson mein kaato",
        "opt_b": "B: Demonic Domain khol kar sabhi rivals ko apne control mein lo",
        "qi_delta": 45,
        "hp_delta": 5,
    },
]


def _format_story_card(row: sqlite3.Row, extra_note: str = "") -> str:
    hist: list[dict[str, str]] = []
    try:
        hist = json.loads(row["history_json"] or "[]")
    except Exception:
        hist = []
    last_step = hist[-1]["choice"] if hist else "Start"

    lines = [
        frame_box(
            sc(str(row["title"])),
            f"Ch.{row['chapter']} • {row['arc_type']}",
        ),
        f"  ❖ Save ID : <code>#{row['id']}</code> | Last Choice: <b>{esc_html(last_step)}</b>",
        f"  ❖ Realm   : <b>{esc_html(row['realm'])}</b>",
        f"  ❖ Qi / HP : <b>{row['demonic_qi']} Qi</b> | <b>{row['hp']} HP</b>",
        "────────────────────────────",
    ]
    if extra_note:
        lines.append(f"  ▸ <i>{esc_html(extra_note)}</i>\n")
    lines.append(f"{esc_html(row['current_scene'])}\n")
    lines.append(f"<b>{esc_html(row['opt_a'])}</b>")
    lines.append(f"<b>{esc_html(row['opt_b'])}</b>")
    lines.append("\n▸ Niche <b>Choice A</b> ya <b>Choice B</b> dabayein:")
    return "\n".join(lines)


def start_new_story(user_id: int) -> str:
    starter = random.choice(STORY_STARTERS)
    stamp = now_ist_str()
    conn = get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO story_saves(
                user_id, title, arc_type, chapter, realm, demonic_qi, hp,
                current_scene, opt_a, opt_b, history_json, updated_at
            ) VALUES(?, ?, ?, 1, ?, 100, 100, ?, ?, ?, '[]', ?)
            """,
            (
                user_id,
                starter["title"],
                starter["arc"],
                starter["realm"],
                starter["scene"],
                starter["opt_a"],
                starter["opt_b"],
                stamp,
            ),
        )
        conn.commit()
        new_id = int(cur.lastrowid)
        row = cur.execute(
            "SELECT * FROM story_saves WHERE id = ?", (new_id,)
        ).fetchone()
    finally:
        conn.close()

    ACTIVE_STORY_ID[user_id] = new_id
    return _format_story_card(row, "Nayi Crazy Murim / Demonic Emperor story shuru hui!")


def continue_latest_story(user_id: int) -> str:
    conn = get_db_conn()
    try:
        row = conn.execute(
            "SELECT * FROM story_saves WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return start_new_story(user_id)

    ACTIVE_STORY_ID[user_id] = int(row["id"])
    return _format_story_card(
        row, f"Saved story resume hui (Updated: {row['updated_at']})"
    )


def list_old_stories_text(user_id: int) -> str:
    conn = get_db_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, title, chapter, realm, demonic_qi, updated_at
            FROM story_saves
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 8
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return (
            f"{frame_box(sc('OLD STORIES'))}\n"
            "  • Abhi koi purani story save nahi hai. <b>🆕 New Story</b> dabayein!"
        )

    lines = [frame_box(sc("SAVED / OLD STORIES"), "Load karne ke liye Save ID bhejein")]
    for r in rows:
        lines.append(
            f"  ❖ <code>#{r['id']}</code> — <b>{esc_html(r['title'])}</b> "
            f"(Ch.{r['chapter']} | {esc_html(r['realm'])} | {r['demonic_qi']} Qi)"
        )
    lines.append("\n▸ Kisi purani story ko kholne ke liye uska number (e.g. <code>1</code>) type karein:")
    AWAITING_INPUT[user_id] = "load_story_id"
    return "\n".join(lines)


def load_story_by_id(user_id: int, story_id_str: str) -> str:
    try:
        sid = int(story_id_str.strip().lstrip("#"))
    except ValueError:
        return "Kripya valid Story ID number bhejein (jaise <code>1</code>)."

    conn = get_db_conn()
    try:
        row = conn.execute(
            "SELECT * FROM story_saves WHERE id = ? AND user_id = ?",
            (sid, user_id),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return f"Story <code>#{sid}</code> nahi mili."

    ACTIVE_STORY_ID[user_id] = int(row["id"])
    return _format_story_card(row, f"Old Story #{sid} load ho gayi!")


def advance_story_choice(user_id: int, choice_letter: str) -> str:
    ch = choice_letter.strip().upper()
    if ch not in ("A", "B"):
        return "Kripya <b>Choice A</b> ya <b>Choice B</b> chuniye."

    conn = get_db_conn()
    try:
        sid = ACTIVE_STORY_ID.get(user_id)
        if sid:
            row = conn.execute(
                "SELECT * FROM story_saves WHERE id = ? AND user_id = ?",
                (sid, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM story_saves WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()

        if not row:
            return start_new_story(user_id)

        cur_chap = int(row["chapter"])
        next_chap = cur_chap + 1
        chosen_text = str(row["opt_a"] if ch == "A" else row["opt_b"])

        hist: list[dict[str, str]] = []
        try:
            hist = json.loads(row["history_json"] or "[]")
        except Exception:
            hist = []
        hist.append({"chapter": str(cur_chap), "choice": ch, "picked": chosen_text})

        ai_prompt = (
            f"Title: {row['title']}\nArc: {row['arc_type']}\nChapter: {next_chap}\n"
            f"Current Realm: {row['realm']} | Qi: {row['demonic_qi']} | HP: {row['hp']}\n"
            f"Previous Scene: {row['current_scene']}\nUser Chose: {chosen_text}\n"
            "Return STRICT JSON only with keys: scene, opt_a, opt_b, realm, qi_delta, hp_delta. "
            "Make it crazy fantasy, martial art cultivation, Murim login, demonic emperor style in Hinglish. "
            "opt_a must start with 'A: ' and opt_b must start with 'B: '."
        )
        ok, ai_raw = call_groq_resilient(
            "Tum Kavana-style interactive crazy fantasy & Murim cultivation story writer ho. Sirf valid JSON do.",
            ai_prompt,
            max_tokens=380,
            temperature=0.65,
        )

        next_scene = ""
        next_a = ""
        next_b = ""
        next_realm = str(row["realm"])
        qi_delta = 25 if ch == "A" else 30
        hp_delta = 0

        if ok:
            try:
                start_i = ai_raw.find("{")
                end_i = ai_raw.rfind("}")
                if start_i != -1 and end_i != -1 and end_i > start_i:
                    obj = json.loads(ai_raw[start_i : end_i + 1])
                    if isinstance(obj, dict):
                        next_scene = str(obj.get("scene") or "").strip()
                        next_a = str(obj.get("opt_a") or "").strip()
                        next_b = str(obj.get("opt_b") or "").strip()
                        if obj.get("realm"):
                            next_realm = str(obj.get("realm")).strip()[:70]
                        if isinstance(obj.get("qi_delta"), (int, float)):
                            qi_delta = int(obj["qi_delta"])
                        if isinstance(obj.get("hp_delta"), (int, float)):
                            hp_delta = int(obj["hp_delta"])
            except Exception:
                pass

        if not next_scene or not next_a or not next_b:
            fb = FALLBACK_BRANCHES[(next_chap - 2) % len(FALLBACK_BRANCHES)]
            next_scene = fb["scene"]
            next_a = fb["opt_a"]
            next_b = fb["opt_b"]
            qi_delta = int(fb["qi_delta"])
            hp_delta = int(fb["hp_delta"])
            if next_chap >= 3:
                next_realm = "Demonic Core / Murim Sovereign"

        if not next_a.upper().startswith("A:"):
            next_a = f"A: {next_a}"
        if not next_b.upper().startswith("B:"):
            next_b = f"B: {next_b}"

        new_qi = max(0, int(row["demonic_qi"]) + qi_delta)
        new_hp = max(10, min(150, int(row["hp"]) + hp_delta))
        stamp = now_ist_str()

        conn.execute(
            """
            UPDATE story_saves
            SET chapter = ?, realm = ?, demonic_qi = ?, hp = ?,
                current_scene = ?, opt_a = ?, opt_b = ?,
                history_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_chap,
                next_realm,
                new_qi,
                new_hp,
                next_scene[:1100],
                next_a[:180],
                next_b[:180],
                json.dumps(hist[-15:]),
                stamp,
                int(row["id"]),
            ),
        )
        conn.commit()
        updated_row = conn.execute(
            "SELECT * FROM story_saves WHERE id = ?", (int(row["id"]),)
        ).fetchone()
    finally:
        conn.close()

    ACTIVE_STORY_ID[user_id] = int(updated_row["id"])
    return _format_story_card(
        updated_row, f"Choice {ch} applied (Qi {qi_delta:+d} | HP {hp_delta:+d})"
    )


# ==============================================================================
# FOCUSED KEYBOARDS (STORY + SYNC + ADMIN ONLY)
# ==============================================================================
def main_menu_keyboard(user_id: int | None) -> ReplyKeyboardMarkup:
    cfg = get_custom_config()
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(cfg["btn_story"]), KeyboardButton(cfg["btn_sync"])],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(cfg["btn_admin"])])
    rows.append([KeyboardButton("🏠 Main Menu")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def story_hub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🆕 New Story"), KeyboardButton("▶ Continue Story")],
            [KeyboardButton("📚 Old Stories")],
            [KeyboardButton("Choice A"), KeyboardButton("Choice B")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🤖 AI Edit 3rd Bot"), KeyboardButton("📥 Manual Code 3rd Bot")],
            [KeyboardButton("📄 View 3rd Bot"), KeyboardButton("⏪ Rollback 3rd Bot")],
            [KeyboardButton("🔔 AI Notify"), KeyboardButton("🔄 Force Sync Now")],
            [KeyboardButton("⚡ 1st Bot ON/OFF"), KeyboardButton("🛠 1st Bot Maint")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def keyboard_for_state(
    chat_id: int, user_id: int | None
) -> ReplyKeyboardMarkup:
    state = MENU_STATE.get(chat_id, "main")
    if state == "story":
        return story_hub_keyboard()
    if state == "admin" and is_admin(user_id):
        return admin_panel_keyboard()
    return main_menu_keyboard(user_id)


def push_state(chat_id: int, new_state: str) -> None:
    cur = MENU_STATE.get(chat_id, "main")
    if cur != new_state:
        MENU_STACK.setdefault(chat_id, []).append(cur)
    MENU_STATE[chat_id] = new_state


async def pop_state(chat_id: int, user_id: int | None, bot) -> None:
    AWAITING_INPUT.pop(chat_id, None)
    st = MENU_STACK.get(chat_id) or []
    prev = st.pop() if st else "main"
    MENU_STATE[chat_id] = prev
    await bot.send_message(
        chat_id=chat_id,
        text=f"⬅ <b>{esc_html(sc(prev))}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_for_state(chat_id, user_id),
    )


# ==============================================================================
# HANDLERS & BACKGROUND SYNC WORKER
# ==============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    AWAITING_INPUT.pop(chat_id, None)
    MENU_STATE[chat_id] = "main"
    MENU_STACK[chat_id] = ["main"]

    cfg = get_custom_config()
    meta = await asyncio.to_thread(sync_bots_db)
    role_badge = (
        "ADMIN (Story + Sync + 3rd Bot AI/Manual Controller)"
        if is_admin(user_id)
        else "USER (Story + Sync)"
    )

    msg = (
        f"{frame_box(sc(cfg['bot_title']), cfg['welcome_banner'])}\n"
        f"  ❖ Role         : <b>{esc_html(role_badge)}</b>\n"
        f"  ❖ Sync Version : <code>v{meta.get('sync_version', 1)}</code> | Stories: <b>{meta.get('story_saves_count', 0)}</b>\n"
        f"  ❖ Last Sync    : <code>{esc_html(meta.get('last_sync_ist', 'N/A'))}</code>"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user_id),
    )


async def handle_document(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.document:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None

    if not is_admin(user_id):
        await context.bot.send_message(
            chat_id=chat_id,
            text="⛔ Sirf Admin hi 3rd bot ki file edit kar sakta hai.",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    doc = update.message.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".py"):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Kripya <code>.py</code> Python file bhejein.",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_panel_keyboard(),
        )
        return

    tg_file = await doc.get_file()
    raw_bytes = await tg_file.download_as_bytearray()
    code_str = raw_bytes.decode("utf-8", errors="replace")
    ok, status_msg = await asyncio.to_thread(apply_code_to_third_bot, code_str)
    AWAITING_INPUT.pop(chat_id, None)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{'✅' if ok else '❌'} {esc_html(status_msg)}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_panel_keyboard(),
    )


async def handle_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else chat_id
    text = update.message.text.strip()
    low = text.lower()
    cfg = get_custom_config()

    if text in ("🏠 Main Menu", "/menu"):
        AWAITING_INPUT.pop(chat_id, None)
        MENU_STATE[chat_id] = "main"
        MENU_STACK[chat_id] = ["main"]
        await context.bot.send_message(
            chat_id=chat_id,
            text=frame_box(sc(cfg["bot_title"]), "Main Menu"),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if text in ("⬅ Back", "Back"):
        await pop_state(chat_id, user_id, context.bot)
        return

    # Pending input handling
    pending = AWAITING_INPUT.get(chat_id)
    if pending:
        AWAITING_INPUT.pop(chat_id, None)

        if pending == "load_story_id":
            out = await asyncio.to_thread(load_story_by_id, user_id, text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=out,
                parse_mode=ParseMode.HTML,
                reply_markup=story_hub_keyboard(),
            )
            return

        if not is_admin(user_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ Yeh action sirf <b>Admin</b> ke liye allowed hai.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        if pending == "ai_edit_third":
            await context.bot.send_message(
                chat_id=chat_id,
                text="🤖 AI `third_bot.py` ko edit aur syntax-check kar raha hai...",
                reply_markup=admin_panel_keyboard(),
            )
            ok, res_msg = await asyncio.to_thread(ai_edit_third_bot_code, text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{'✅' if ok else '❌'} {esc_html(res_msg)}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if pending == "manual_code_third":
            ok, res_msg = await asyncio.to_thread(apply_code_to_third_bot, text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{'✅' if ok else '❌'} {esc_html(res_msg)}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if pending == "ai_notify":
            ok, n_msg = await ai_send_direct_notification(context.bot, text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{'✅' if ok else '❌'} {n_msg}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

    # Story Hub & A/B Choice actions
    if text == cfg["btn_story"]:
        push_state(chat_id, "story")
        hub_msg = (
            f"{frame_box(sc('MURIM & DEMONIC STORY MODE'), 'Kavana-Style Crazy Fantasy • A/B Choices')}\n"
            "  ❖ <b>🆕 New Story</b>      : Nayi crazy Murim / Demonic Emperor kahani\n"
            "  ❖ <b>▶ Continue Story</b> : Saved chapter yahin se continue karein\n"
            "  ❖ <b>📚 Old Stories</b>     : Purani saved stories load karein\n"
            "  ❖ <b>Choice A / B</b>   : Agla twist chunne ke liye dabayein"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=hub_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=story_hub_keyboard(),
        )
        return

    if MENU_STATE.get(chat_id) == "story":
        if text == "🆕 New Story":
            out = await asyncio.to_thread(start_new_story, user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=out,
                parse_mode=ParseMode.HTML,
                reply_markup=story_hub_keyboard(),
            )
            return

        if text == "▶ Continue Story":
            out = await asyncio.to_thread(continue_latest_story, user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=out,
                parse_mode=ParseMode.HTML,
                reply_markup=story_hub_keyboard(),
            )
            return

        if text == "📚 Old Stories":
            out = await asyncio.to_thread(list_old_stories_text, user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=out,
                parse_mode=ParseMode.HTML,
                reply_markup=story_hub_keyboard(),
            )
            return

        if text in ("Choice A", "A", "a"):
            out = await asyncio.to_thread(advance_story_choice, user_id, "A")
            await context.bot.send_message(
                chat_id=chat_id,
                text=out,
                parse_mode=ParseMode.HTML,
                reply_markup=story_hub_keyboard(),
            )
            return

        if text in ("Choice B", "B", "b"):
            out = await asyncio.to_thread(advance_story_choice, user_id, "B")
            await context.bot.send_message(
                chat_id=chat_id,
                text=out,
                parse_mode=ParseMode.HTML,
                reply_markup=story_hub_keyboard(),
            )
            return

    # Sync 1st, 2nd & 3rd bot
    if text == cfg["btn_sync"]:
        meta = await asyncio.to_thread(sync_bots_db)
        third_ready = os.path.exists(THIRD_BOT_FILE)
        body = (
            f"{frame_box(sc('1ST, 2ND & 3RD BOT SYNC'))}\n"
            f"  ❖ Sync Version : <code>v{meta.get('sync_version', 1)}</code>\n"
            f"  ❖ Last Sync    : <code>{esc_html(meta.get('last_sync_ist', 'N/A'))}</code>\n"
            f"  ❖ 3rd Bot File : <b>{'Live Ready' if third_ready else 'Not Created'}</b>\n"
            f"  ❖ Stories Saved: <b>{meta.get('story_saves_count', 0)}</b> | Snapshots: <b>{meta.get('snapshots_count', 0)}</b>"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=body,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_for_state(chat_id, user_id),
        )
        return

    # Admin Panel buttons
    if text == cfg["btn_admin"] or MENU_STATE.get(chat_id) == "admin":
        if not is_admin(user_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ Admin Panel sirf Admin ke liye hai.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        if text == cfg["btn_admin"]:
            push_state(chat_id, "admin")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{frame_box(sc('ADMIN & 3RD BOT CONTROLLER'), 'AI Edit • Manual Code • Rollback • Notify')}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "🤖 AI Edit 3rd Bot":
            AWAITING_INPUT[chat_id] = "ai_edit_third"
            await context.bot.send_message(
                chat_id=chat_id,
                text="🤖 Bataiye `third_bot.py` mein kya change/feature chahiye (e.g. 'sab button hata do' ya 'notification command add karo'):",
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "📥 Manual Code 3rd Bot":
            AWAITING_INPUT[chat_id] = "manual_code_third"
            await context.bot.send_message(
                chat_id=chat_id,
                text="📥 `third_bot.py` ka Python code paste karein ya `.py` file bhejein:",
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "📄 View 3rd Bot":
            preview = await asyncio.to_thread(get_third_bot_preview)
            await context.bot.send_message(
                chat_id=chat_id,
                text=preview,
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "⏪ Rollback 3rd Bot":
            ok, rb_msg = await asyncio.to_thread(rollback_third_bot_code)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{'✅' if ok else '❌'} {rb_msg}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "🔔 AI Notify":
            AWAITING_INPUT[chat_id] = "ai_notify"
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔔 Notification message likhein (optional chat_id bhi likh sakte hain):",
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "🔄 Force Sync Now":
            meta = await asyncio.to_thread(sync_bots_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Synced <code>v{meta.get('sync_version')}</code> at <code>{esc_html(meta.get('last_sync_ist'))}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "⚡ 1st Bot ON/OFF":
            cur_en = "0" if cfg.get("bot1_bot_enabled") == "1" else "1"
            push_customization_to_db("bot1_bot_enabled", cur_en)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚡ 1st Bot State: <b>{'ENABLED' if cur_en == '1' else 'DISABLED'}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if text == "🛠 1st Bot Maint":
            cur_m = "0" if cfg.get("bot1_maint_mode") == "1" else "1"
            push_customization_to_db("bot1_maint_mode", cur_m)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🛠 1st Bot Maintenance: <b>{'ON' if cur_m == '1' else 'OFF'}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

    # Natural-language AI Router for Admin (e.g. "3rd bot se sab button hata do", "notification bhejo", etc.)
    if is_admin(user_id):
        if any(
            k in low
            for k in (
                "button",
                "3rd bot",
                "third bot",
                "code",
                "command",
                "hata",
                "remove",
                "add",
                "badal",
                "edit",
            )
        ):
            await context.bot.send_message(
                chat_id=chat_id,
                text="🤖 AI aapki instruction ke hisaab se `third_bot.py` ko live edit kar raha hai...",
                reply_markup=admin_panel_keyboard(),
            )
            ok, res_msg = await asyncio.to_thread(ai_edit_third_bot_code, text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{'✅' if ok else '❌'} {esc_html(res_msg)}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

        if any(k in low for k in ("notify", "notification", "alert bhejo", "msg bhejo")):
            ok, n_msg = await ai_send_direct_notification(context.bot, text)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{'✅' if ok else '❌'} {n_msg}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_panel_keyboard(),
            )
            return

    await context.bot.send_message(
        chat_id=chat_id,
        text="❖ Menu button chuniye ya <b>🏠 Main Menu</b> dabayein.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_for_state(chat_id, user_id),
    )


async def background_sync_worker(application) -> None:
    await asyncio.sleep(3)
    while True:
        try:
            await asyncio.to_thread(sync_bots_db)
        except Exception as exc:
            logger.warning("Background sync worker note: %s", exc)
        await asyncio.sleep(20)


async def post_init(application) -> None:
    init_companion_db()
    await asyncio.to_thread(sync_bots_db)
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start Companion"),
            BotCommand("menu", "Open Menu"),
        ]
    )
    application.create_task(background_sync_worker(application))


def main() -> None:
    if not COMPANION_BOT_TOKEN:
        raise RuntimeError(
            "SECOND_BOT_TOKEN (ya COMPANION_BOT_TOKEN) set nahi hai."
        )
    if MAIN_BOT_TOKEN and COMPANION_BOT_TOKEN == MAIN_BOT_TOKEN:
        raise RuntimeError(
            "SECOND_BOT_TOKEN aur BOT_TOKEN alag hone chahiye."
        )

    init_companion_db()
    sync_bots_db()

    app = (
        ApplicationBuilder()
        .token(COMPANION_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Focused Companion Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
