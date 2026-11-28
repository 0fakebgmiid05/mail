import asyncio
import json
import logging
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BaagiJaatCompanionSuite")

# ==============================================================================
# ENVIRONMENT & SHARED AUTHORITY CONFIG
# ==============================================================================
COMPANION_BOT_TOKEN: str = (
    os.getenv("SECOND_BOT_TOKEN", "").strip()
    or os.getenv("COMPANION_BOT_TOKEN", "").strip()
)
MAIN_BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
ADMIN_TG_ID: int = int(
    os.getenv("ADMIN_TG_ID", "1390658041").strip() or "1390658041"
)
DEFAULT_CLIENT_NAME: str = (
    os.getenv("CLIENT_NAME", "BAAGI JAAT").strip() or "BAAGI JAAT"
)
DB_PATH: str = os.getenv("BOT_DB_PATH", "baagijaat_telemetry.db").strip()

MEDIA_ANIM_URL: str = os.getenv("COMPANION_ANIM_URL", "").strip()
MEDIA_GIF_URL: str = os.getenv("COMPANION_GIF_URL", "").strip()
MEDIA_PHOTO_URL: str = os.getenv("COMPANION_PHOTO_URL", "").strip()
MEDIA_VIDEO_URL: str = os.getenv("COMPANION_VIDEO_URL", "").strip()

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
ACTIVE_GAMES: dict[int, dict[str, Any]] = {}
ACTIVE_STORY_ID: dict[int, int] = {}
LAST_SYNC_META: dict[str, Any] = {
    "last_sync_ist": "Not synced yet",
    "snapshots_count": 0,
    "watchlist_count": 0,
    "alerts_count": 0,
    "sync_version": 1,
}

DEFAULT_CUSTOM_CONFIG: dict[str, str] = {
    "bot_title": f"{DEFAULT_CLIENT_NAME} ULTRA COMPANION",
    "welcome_banner": "2nd Bot → 1st Bot Sync • Murim Story • AI Arcade",
    "btn_games": "🎮 AI & Games Arcade",
    "btn_story": "📜 Murim Story Mode",
    "btn_sync": "🔄 Sync 1st & 2nd Bot",
    "btn_ai": "🤖 AI Advisor",
    "btn_media": "🎬 Media Showcase",
    "btn_admin_custom": "👑 Admin Customize",
    "game_max_rounds": "5",
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
            CREATE TABLE IF NOT EXISTS game_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game_name TEXT NOT NULL,
                outcome TEXT NOT NULL,
                score_delta INTEGER NOT NULL,
                played_at TEXT NOT NULL
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


def push_customization_to_bot1_db(key: str, val: str) -> None:
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


def reset_all_customizations() -> None:
    conn = get_db_conn()
    try:
        stamp = now_ist_str()
        for k, v in DEFAULT_CUSTOM_CONFIG.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO admin_custom_config(cfg_key, cfg_val, updated_at)
                VALUES(?, ?, ?)
                """,
                (k, v, stamp),
            )
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(k, v) VALUES('active_theme', ?)",
            (DEFAULT_CUSTOM_CONFIG["bot1_theme"],),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(k, v) VALUES('notif_style', ?)",
            (DEFAULT_CUSTOM_CONFIG["bot1_notif_style"],),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(k, v) VALUES('refresh_sec', ?)",
            (DEFAULT_CUSTOM_CONFIG["bot1_refresh_sec"],),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(k, v) VALUES('maint_mode', ?)",
            (DEFAULT_CUSTOM_CONFIG["bot1_maint_mode"],),
        )
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings(k, v) VALUES('bot_enabled', ?)",
            (DEFAULT_CUSTOM_CONFIG["bot1_bot_enabled"],),
        )
        conn.commit()
    finally:
        conn.close()


def record_game_score(
    user_id: int, game_name: str, outcome: str, score_delta: int
) -> None:
    try:
        conn = get_db_conn()
        try:
            conn.execute(
                """
                INSERT INTO game_scores(user_id, game_name, outcome, score_delta, played_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (user_id, game_name, outcome, score_delta, now_ist_str()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("record_game_score failed: %s", exc)


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
# RESILIENT GROQ AI ENGINE (LIVE MODEL DISCOVERY + FALLBACK)
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
    max_tokens: int = 380,
    temperature: float = 0.45,
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
                    "frequency_penalty": 0.35,
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
                if content:
                    return True, content[:1600]
                last_reason = f"{model_id} ne khali response diya."
            else:
                last_reason = f"{model_id} HTTP {r.status_code}"
        except Exception as exc:
            last_reason = f"{model_id}: {exc}"

    return False, last_reason


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

        # Try Groq AI for crazy Murim / Demonic Emperor next chapter
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
            max_tokens=360,
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
# MULTI-GAME ARCADE + GROQ AI GAME ENGINE
# ==============================================================================
TRIVIA_BANK: list[tuple[str, str, str]] = [
    ("Modbus RTU ka physical serial layer aam taur par kaunsa hota hai?", "RS-485", "485"),
    ("HART protocol analog signal ke saath kaunsa signal overlay karta hai?", "FSK Digital Signal", "fsk"),
    ("PLC Ladder Logic mein normally open contact ko kya kehte hain?", "XIC / NO Contact", "no"),
    ("Python mein immutable sequence ka common example kaunsa hai?", "Tuple", "tuple"),
    ("SQLite mein primary key unique index banata hai — True ya False?", "True", "true"),
]

SCRAMBLE_WORDS: list[tuple[str, str]] = [
    ("TELEMETRY", "Remote sensor & meter data transmission"),
    ("CALIBRATION", "Tank dip chart & sensor accuracy alignment"),
    ("AUTOMATION", "PLC & industrial control process"),
    ("DATABASE", "Structured persistent storage like SQLite"),
    ("PROTOCOL", "Communication rule set like Modbus or HART"),
]

MATH_OPS = ["+", "-", "*"]


def start_game_session(chat_id: int, game_key: str) -> str:
    cfg = get_custom_config()
    max_rounds = max(1, min(15, int(cfg.get("game_max_rounds") or "5")))

    if game_key == "ai_riddle":
        ok, riddle_txt = call_groq_resilient(
            "Tum ek smart Hinglish game master ho. Ek chhota, mazedaar tech ya logic riddle poochho aur end mein sirf 'Hint:' likh kar 1 line hint do. Jawab abhi mat batao.",
            "Ek naya riddle banao.",
            max_tokens=180,
            temperature=0.6,
        )
        if not ok:
            riddle_txt = "Main bina bole sab kuch sunta hoon aur data ko safe rakhta hoon — batao main kaun hoon? Hint: DB/Server"
        ACTIVE_GAMES[chat_id] = {"type": "ai_riddle", "riddle": riddle_txt}
        return (
            f"❖ <b>{esc_html(sc('AI RIDDLE CHALLENGE'))}</b>\n"
            f"{esc_html(riddle_txt)}\n\n"
            "▸ Apna jawab type karein:"
        )

    if game_key == "ai_scenario":
        ok, sc_txt = call_groq_resilient(
            "Tum ek Hinglish adventure & strategy game host ho. Ek short 3-line situation do jisme user ko 2 options (A ya B) milen.",
            "Ek naya adventure scenario shuru karo.",
            max_tokens=200,
            temperature=0.65,
        )
        if not ok:
            sc_txt = "Ek purane control room mein alarm baja hai. Option A: Main breaker check karein. Option B: Backup console dekhein."
        ACTIVE_GAMES[chat_id] = {"type": "ai_scenario", "scenario": sc_txt}
        return (
            f"❖ <b>{esc_html(sc('AI STRATEGY QUEST'))}</b>\n"
            f"{esc_html(sc_txt)}\n\n"
            "▸ Apna choice ya action type karein:"
        )

    if game_key == "ttt":
        ACTIVE_GAMES[chat_id] = {"type": "ttt", "board": [" "] * 9}
        return render_ttt(
            ACTIVE_GAMES[chat_id]["board"],
            "Aap 'X' hain, Bot 'O' hai. Cell 1–9 bhejein:",
        )

    if game_key == "guess":
        secret = random.randint(1, 100)
        ACTIVE_GAMES[chat_id] = {
            "type": "guess",
            "secret": secret,
            "tries": 0,
            "low": 1,
            "high": 100,
        }
        return (
            f"❖ <b>{esc_html(sc('NUMBER GUESS (1–100)'))}</b>\n"
            "▸ Maine 1 se 100 ke beech ek number socha hai. Guess number bhejein:"
        )

    if game_key == "rps":
        ACTIVE_GAMES[chat_id] = {
            "type": "rps",
            "user_pts": 0,
            "bot_pts": 0,
            "round": 0,
            "max_rounds": max_rounds,
        }
        return (
            f"❖ <b>{esc_html(sc('ROCK • PAPER • SCISSORS'))}</b> (Best of {max_rounds})\n"
            "▸ <code>Rock</code>, <code>Paper</code>, ya <code>Scissors</code> bhejein:"
        )

    if game_key == "rpsls":
        ACTIVE_GAMES[chat_id] = {
            "type": "rpsls",
            "user_pts": 0,
            "bot_pts": 0,
            "round": 0,
            "max_rounds": max_rounds,
        }
        return (
            f"❖ <b>{esc_html(sc('RPSLS ARCADE'))}</b>\n"
            "▸ <code>Rock</code>, <code>Paper</code>, <code>Scissors</code>, <code>Lizard</code>, ya <code>Spock</code>:"
        )

    if game_key == "math":
        a = random.randint(12, 99)
        b = random.randint(2, 25)
        op = random.choice(MATH_OPS)
        ans = a + b if op == "+" else (a - b if op == "-" else a * b)
        ACTIVE_GAMES[chat_id] = {
            "type": "math",
            "answer": ans,
            "streak": 0,
            "round": 1,
            "max_rounds": max_rounds,
        }
        return (
            f"❖ <b>{esc_html(sc('SPEED MATH'))}</b> (Round 1/{max_rounds})\n"
            f"▸ Solve karein: <code>{a} {op} {b} = ?</code>"
        )

    if game_key == "trivia":
        q_txt, ans_disp, ans_key = random.choice(TRIVIA_BANK)
        ACTIVE_GAMES[chat_id] = {
            "type": "trivia",
            "question": q_txt,
            "ans_disp": ans_disp,
            "ans_key": ans_key.lower(),
        }
        return (
            f"❖ <b>{esc_html(sc('TECH TRIVIA'))}</b>\n"
            f"▸ <b>{esc_html(q_txt)}</b>"
        )

    if game_key == "scramble":
        word, hint = random.choice(SCRAMBLE_WORDS)
        letters = list(word)
        random.shuffle(letters)
        scrambled = "".join(letters)
        if scrambled == word:
            scrambled = word[::-1]
        ACTIVE_GAMES[chat_id] = {
            "type": "scramble",
            "word": word,
            "hint": hint,
            "scrambled": scrambled,
        }
        return (
            f"❖ <b>{esc_html(sc('WORD SCRAMBLE'))}</b>\n"
            f"▸ Word: <code>{esc_html(scrambled)}</code>\n"
            f"▸ Hint: <i>{esc_html(hint)}</i>"
        )

    if game_key == "dice":
        u_roll = random.randint(1, 6)
        b_roll = random.randint(1, 6)
        if u_roll > b_roll:
            res, delta = "Aap jeete!", 10
        elif b_roll > u_roll:
            res, delta = "Bot jeeta!", -5
        else:
            res, delta = "Tie!", 2
        record_game_score(chat_id, "Dice Duel", res, delta)
        return (
            f"❖ <b>{esc_html(sc('DICE DUEL'))}</b>\n"
            f"▸ Aap: <b>{u_roll}</b> | Bot: <b>{b_roll}</b> → <b>{esc_html(res)}</b> ({delta:+d} pts)"
        )

    if game_key == "coin":
        u_side = random.choice(["Heads", "Tails"])
        b_side = random.choice(["Heads", "Tails"])
        won = u_side == b_side
        res = "Match!" if won else "Miss!"
        delta = 10 if won else 0
        record_game_score(chat_id, "Coin Flip", res, delta)
        return (
            f"❖ <b>{esc_html(sc('COIN FLIP'))}</b>\n"
            f"▸ Call: <b>{u_side}</b> | Coin: <b>{b_side}</b> → <b>{esc_html(res)}</b>"
        )

    if game_key == "slots":
        syms = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
        reels = [random.choice(syms) for _ in range(3)]
        if len(set(reels)) == 1:
            res, delta = "JACKPOT!", 50
        elif len(set(reels)) == 2:
            res, delta = "Pair Win!", 15
        else:
            res, delta = "Try Again", 0
        record_game_score(chat_id, "Lucky Slots", res, delta)
        return (
            f"❖ <b>{esc_html(sc('LUCKY SLOTS'))}</b>\n"
            f"▸ [ <b>{reels[0]} | {reels[1]} | {reels[2]}</b> ] → <b>{esc_html(res)}</b> ({delta:+d} pts)"
        )

    if game_key == "bomb":
        safe_wire = random.choice(["RED", "BLUE", "GREEN"])
        ACTIVE_GAMES[chat_id] = {"type": "bomb", "safe": safe_wire}
        return (
            f"❖ <b>{esc_html(sc('BOMB DEFUSAL'))}</b>\n"
            "▸ Wire chuniye: <code>RED</code>, <code>BLUE</code>, ya <code>GREEN</code>:"
        )

    if game_key == "highlow":
        cur_num = random.randint(15, 85)
        next_num = random.randint(1, 100)
        ACTIVE_GAMES[chat_id] = {
            "type": "highlow",
            "current": cur_num,
            "next": next_num,
        }
        return (
            f"❖ <b>{esc_html(sc('HIGHER OR LOWER'))}</b>\n"
            f"▸ Current: <b>{cur_num}</b> — Agla number <code>HIGH</code> ya <code>LOW</code>?"
        )

    return "Game start nahi ho saka."


def _ttt_win(b: list[str]) -> str | None:
    lines = [
        (0, 1, 2),
        (3, 4, 5),
        (6, 7, 8),
        (0, 3, 6),
        (1, 4, 7),
        (2, 5, 8),
        (0, 4, 8),
        (2, 4, 6),
    ]
    for x, y, z in lines:
        if b[x] != " " and b[x] == b[y] == b[z]:
            return b[x]
    return "DRAW" if " " not in b else None


def render_ttt(b: list[str], note: str) -> str:
    disp = [
        ("✕" if v == "X" else ("◯" if v == "O" else str(i + 1)))
        for i, v in enumerate(b)
    ]
    grid = (
        f"<code>  {disp[0]} │ {disp[1]} │ {disp[2]}  </code>\n"
        f"<code> ───┼───┼─── </code>\n"
        f"<code>  {disp[3]} │ {disp[4]} │ {disp[5]}  </code>\n"
        f"<code> ───┼───┼─── </code>\n"
        f"<code>  {disp[6]} │ {disp[7]} │ {disp[8]}  </code>"
    )
    return f"❖ <b>{esc_html(sc('TIC-TAC-TOE'))}</b>\n{grid}\n\n▸ {esc_html(note)}"


def handle_active_game_input(chat_id: int, text: str) -> str | None:
    st = ACTIVE_GAMES.get(chat_id)
    if not st:
        return None

    gtype = st.get("type")
    low = text.strip().lower()

    if gtype == "ai_riddle":
        riddle = str(st.get("riddle") or "")
        ACTIVE_GAMES.pop(chat_id, None)
        ok, judge = call_groq_resilient(
            "Tum Hinglish riddle judge ho. User ke jawab ko evaluate karo, short aur friendly response do, aur sahi hint/answer clarify karo.",
            f"Riddle: {riddle}\nUser Answer: {text}",
            max_tokens=180,
            temperature=0.3,
        )
        won = ok and any(
            w in judge.lower() for w in ("sahi", "correct", "badhai", "shabaash")
        )
        record_game_score(chat_id, "AI Riddle", "Evaluated", 15 if won else 5)
        return f"🤖 <b>AI Riddle Result</b>\n{esc_html(judge if ok else 'Jawab receive hua!')}"

    if gtype == "ai_scenario":
        sc_txt = str(st.get("scenario") or "")
        ACTIVE_GAMES.pop(chat_id, None)
        ok, outcome = call_groq_resilient(
            "Tum Hinglish adventure narrator ho. User ke move ka 3-4 line ka interesting result likho aur score/ending batao.",
            f"Scenario: {sc_txt}\nUser Move: {text}",
            max_tokens=200,
            temperature=0.5,
        )
        record_game_score(chat_id, "AI Quest", "Completed", 15)
        return f"🤖 <b>AI Quest Outcome</b>\n{esc_html(outcome if ok else 'Aapka move record ho gaya!')}"

    if gtype == "ttt":
        if text.strip() not in {"1", "2", "3", "4", "5", "6", "7", "8", "9"}:
            return render_ttt(st["board"], "1 se 9 tak cell number bhejein.")
        idx = int(text.strip()) - 1
        board: list[str] = st["board"]
        if board[idx] != " ":
            return render_ttt(board, "Yeh cell pehle se bhara hai.")
        board[idx] = "X"
        w = _ttt_win(board)
        if w == "X":
            ACTIVE_GAMES.pop(chat_id, None)
            record_game_score(chat_id, "Tic-Tac-Toe", "Win", 20)
            return render_ttt(board, "Aap jeet gaye! (+20 pts)")
        if w == "DRAW":
            ACTIVE_GAMES.pop(chat_id, None)
            record_game_score(chat_id, "Tic-Tac-Toe", "Draw", 5)
            return render_ttt(board, "Match Draw! (+5 pts)")

        empties = [i for i, v in enumerate(board) if v == " "]
        choice = 4 if 4 in empties else random.choice(empties)
        board[choice] = "O"
        w2 = _ttt_win(board)
        if w2 == "O":
            ACTIVE_GAMES.pop(chat_id, None)
            record_game_score(chat_id, "Tic-Tac-Toe", "Loss", 0)
            return render_ttt(board, "Bot jeet gaya!")
        if w2 == "DRAW":
            ACTIVE_GAMES.pop(chat_id, None)
            record_game_score(chat_id, "Tic-Tac-Toe", "Draw", 5)
            return render_ttt(board, "Match Draw! (+5 pts)")
        return render_ttt(
            board, f"Bot ne cell {choice + 1} chuna. Ab aapki baari:"
        )

    if gtype == "guess":
        try:
            g_val = int(text.strip())
        except ValueError:
            return "Kripya ek valid number (1–100) bhejein."
        st["tries"] = int(st["tries"]) + 1
        secret = int(st["secret"])
        if g_val == secret:
            tries = int(st["tries"])
            ACTIVE_GAMES.pop(chat_id, None)
            pts = max(10, 50 - tries * 4)
            record_game_score(chat_id, "Number Guess", f"Won in {tries}", pts)
            return f"🎉 Sahi number <b>{secret}</b> aapne <b>{tries}</b> tries mein guess kiya! (+{pts} pts)"
        if g_val < secret:
            st["low"] = max(int(st["low"]), g_val + 1)
            return f"📈 Thoda <b>BADA</b> number try karein (Range: <code>{st['low']}–{st['high']}</code>)"
        st["high"] = min(int(st["high"]), g_val - 1)
        return f"📉 Thoda <b>CHHOTA</b> number try karein (Range: <code>{st['low']}–{st['high']}</code>)"

    if gtype == "rps":
        if "rock" in low:
            u = "Rock"
        elif "paper" in low:
            u = "Paper"
        elif "scissor" in low:
            u = "Scissors"
        else:
            return "<code>Rock</code>, <code>Paper</code>, ya <code>Scissors</code> chuniye."
        b = random.choice(["Rock", "Paper", "Scissors"])
        st["round"] = int(st["round"]) + 1
        beats = {"Rock": "Scissors", "Paper": "Rock", "Scissors": "Paper"}
        if u == b:
            msg = "Round Draw!"
        elif beats[u] == b:
            st["user_pts"] = int(st["user_pts"]) + 1
            msg = "Aap round jeete!"
        else:
            st["bot_pts"] = int(st["bot_pts"]) + 1
            msg = "Bot round jeeta!"

        done = int(st["round"]) >= int(st["max_rounds"])
        score_str = f"Aap <b>{st['user_pts']}</b> - <b>{st['bot_pts']}</b> Bot (Round {st['round']}/{st['max_rounds']})"
        if done:
            ACTIVE_GAMES.pop(chat_id, None)
            won = int(st["user_pts"]) >= int(st["bot_pts"])
            record_game_score(
                chat_id, "RPS", "Win" if won else "Loss", 15 if won else 0
            )
            return f"Aap: <b>{u}</b> vs Bot: <b>{b}</b> → {msg}\nFinal: {score_str}"
        return f"Aap: <b>{u}</b> vs Bot: <b>{b}</b> → {msg}\n{score_str}"

    if gtype == "rpsls":
        valid = ["rock", "paper", "scissors", "lizard", "spock"]
        u = next((v.capitalize() for v in valid if v in low), None)
        if not u:
            return "<code>Rock</code>, <code>Paper</code>, <code>Scissors</code>, <code>Lizard</code>, ya <code>Spock</code> bhejein."
        b = random.choice([v.capitalize() for v in valid])
        wins_map = {
            "Rock": {"Scissors", "Lizard"},
            "Paper": {"Rock", "Spock"},
            "Scissors": {"Paper", "Lizard"},
            "Lizard": {"Spock", "Paper"},
            "Spock": {"Scissors", "Rock"},
        }
        st["round"] = int(st["round"]) + 1
        if u == b:
            res = "Draw"
        elif b in wins_map[u]:
            st["user_pts"] = int(st["user_pts"]) + 1
            res = "Aap jeete!"
        else:
            st["bot_pts"] = int(st["bot_pts"]) + 1
            res = "Bot jeeta!"
        if int(st["round"]) >= int(st["max_rounds"]):
            ACTIVE_GAMES.pop(chat_id, None)
            record_game_score(chat_id, "RPSLS", res, 15)
        return (
            f"Aap: <b>{u}</b> vs Bot: <b>{b}</b> → <b>{esc_html(res)}</b>\n"
            f"Score: Aap <b>{st['user_pts']}</b> | Bot <b>{st['bot_pts']}</b>"
        )

    if gtype == "math":
        try:
            u_ans = int(text.strip())
        except ValueError:
            return "Integer answer bhejein."
        correct = int(st["answer"])
        if u_ans == correct:
            st["streak"] = int(st["streak"]) + 1
            if int(st["round"]) >= int(st["max_rounds"]):
                pts = int(st["streak"]) * 10
                ACTIVE_GAMES.pop(chat_id, None)
                record_game_score(chat_id, "Speed Math", "Completed", pts)
                return f"🎉 Sab rounds complete! Streak: <b>{st['streak']}</b> (+{pts} pts)"
            st["round"] = int(st["round"]) + 1
            a = random.randint(12, 99)
            b = random.randint(2, 25)
            op = random.choice(MATH_OPS)
            st["answer"] = (
                a + b if op == "+" else (a - b if op == "-" else a * b)
            )
            return f"✅ Sahi! (Round {st['round']}/{st['max_rounds']})\nAgla: <code>{a} {op} {b} = ?</code>"
        ACTIVE_GAMES.pop(chat_id, None)
        return f"❌ Galat! Sahi jawab <b>{correct}</b> tha."

    if gtype == "trivia":
        ans_key = str(st.get("ans_key") or "")
        ans_disp = str(st.get("ans_disp") or "")
        ACTIVE_GAMES.pop(chat_id, None)
        if ans_key and ans_key in low:
            record_game_score(chat_id, "Trivia", "Correct", 15)
            return f"✅ Sahi jawab! <b>{esc_html(ans_disp)}</b> (+15 pts)"
        return f"ℹ Sahi answer: <b>{esc_html(ans_disp)}</b>"

    if gtype == "scramble":
        target = str(st.get("word") or "").lower()
        ACTIVE_GAMES.pop(chat_id, None)
        if low == target:
            record_game_score(chat_id, "Word Scramble", "Solved", 15)
            return f"✅ Shabaash! Word: <b>{esc_html(target.upper())}</b> (+15 pts)"
        return f"ℹ Sahi word: <b>{esc_html(target.upper())}</b>"

    if gtype == "bomb":
        safe = str(st.get("safe") or "").lower()
        ACTIVE_GAMES.pop(chat_id, None)
        if low == safe:
            record_game_score(chat_id, "Bomb Defusal", "Defused", 25)
            return f"💣✅ Bomb defuse ho gaya! Safe wire: <b>{safe.upper()}</b> (+25 pts)"
        return f"💥 Galat wire! Safe wire: <b>{safe.upper()}</b> thi."

    if gtype == "highlow":
        cur_n = int(st.get("current") or 50)
        nxt_n = int(st.get("next") or 50)
        ACTIVE_GAMES.pop(chat_id, None)
        if "high" in low:
            won = nxt_n >= cur_n
        elif "low" in low:
            won = nxt_n <= cur_n
        else:
            return "<code>HIGH</code> ya <code>LOW</code> type karein."
        record_game_score(
            chat_id, "Higher/Lower", "Win" if won else "Loss", 10 if won else 0
        )
        return (
            f"Current: <b>{cur_n}</b> → Next: <b>{nxt_n}</b>\n"
            f"Result: <b>{'Aap jeete! (+10 pts)' if won else 'Aap haar gaye!'}</b>"
        )

    return None


# ==============================================================================
# MEDIA DELIVERY ENGINE (PHOTO / ANIMATION / GIF / VIDEO WITH SAFE FALLBACK)
# ==============================================================================
async def send_media_showcase(bot, chat_id: int, kind: str, caption: str) -> None:
    target_map = {
        "anim": MEDIA_ANIM_URL,
        "gif": MEDIA_GIF_URL,
        "photo": MEDIA_PHOTO_URL,
        "video": MEDIA_VIDEO_URL,
    }
    media_val = target_map.get(kind, "")
    try:
        if media_val:
            if kind in ("anim", "gif"):
                await bot.send_animation(
                    chat_id=chat_id,
                    animation=media_val,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return
            if kind == "photo":
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=media_val,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return
            if kind == "video":
                await bot.send_video(
                    chat_id=chat_id,
                    video=media_val,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                return
    except Exception as exc:
        logger.warning("Media send (%s) fallback: %s", kind, exc)

    tag_map = {
        "anim": "ANIMATION READY (Set COMPANION_ANIM_URL)",
        "gif": "GIF READY (Set COMPANION_GIF_URL)",
        "photo": "PHOTO READY (Set COMPANION_PHOTO_URL)",
        "video": "VIDEO READY (Set COMPANION_VIDEO_URL)",
    }
    card = (
        f"{frame_box(sc(tag_map.get(kind, 'MEDIA')), caption)}\n"
        f"▸ Status : <b>Active UI Media Engine</b>"
    )
    await bot.send_message(
        chat_id=chat_id,
        text=card,
        parse_mode=ParseMode.HTML,
    )


# ==============================================================================
# KEYBOARDS (WITH BACK & MAIN MENU EVERYWHERE + STORY + ADMIN CUSTOMIZE)
# ==============================================================================
def main_menu_keyboard(user_id: int | None) -> ReplyKeyboardMarkup:
    cfg = get_custom_config()
    rows: list[list[KeyboardButton]] = [
        [
            KeyboardButton(cfg["btn_story"]),
            KeyboardButton(cfg["btn_games"]),
        ],
        [
            KeyboardButton(cfg["btn_sync"]),
            KeyboardButton(cfg["btn_ai"]),
        ],
        [
            KeyboardButton(cfg["btn_media"]),
            KeyboardButton("📊 Leaderboard"),
        ],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(cfg["btn_admin_custom"])])
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


def games_arcade_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🤖 AI Riddle"), KeyboardButton("🧭 AI Quest")],
            [KeyboardButton("🎲 Tic-Tac-Toe"), KeyboardButton("🔢 Number Guess")],
            [KeyboardButton("✂ RPS"), KeyboardButton("🖖 RPSLS")],
            [KeyboardButton("🧮 Speed Math"), KeyboardButton("🧠 Trivia")],
            [KeyboardButton("🔤 Scramble"), KeyboardButton("🎲 Dice Duel")],
            [KeyboardButton("🪙 Coin Flip"), KeyboardButton("🎰 Lucky Slots")],
            [KeyboardButton("💣 Bomb Defusal"), KeyboardButton("📈 Higher Lower")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def media_hub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🎞 Animation"), KeyboardButton("🖼 GIF Card")],
            [KeyboardButton("📷 Photo Card"), KeyboardButton("🎥 Video Card")],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_customize_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("✏ 2nd Bot Title"), KeyboardButton("📝 Welcome Banner")],
            [KeyboardButton("🎨 1st Bot Theme"), KeyboardButton("🔔 1st Bot Notif")],
            [KeyboardButton("⏱ 1st Bot Sync Sec"), KeyboardButton("🛠 1st Bot Maint")],
            [KeyboardButton("⚡ 1st Bot ON/OFF"), KeyboardButton("🔢 Game Rounds")],
            [KeyboardButton("🔄 Force Sync Now"), KeyboardButton("📋 View Custom Config")],
            [KeyboardButton("♻ Reset Defaults"), KeyboardButton("⬅ Back")],
            [KeyboardButton("🏠 Main Menu")],
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
    if state == "games":
        return games_arcade_keyboard()
    if state == "media":
        return media_hub_keyboard()
    if state == "admin_custom" and is_admin(user_id):
        return admin_customize_keyboard()
    return main_menu_keyboard(user_id)


def push_state(chat_id: int, new_state: str) -> None:
    cur = MENU_STATE.get(chat_id, "main")
    if cur != new_state:
        MENU_STACK.setdefault(chat_id, []).append(cur)
    MENU_STATE[chat_id] = new_state


async def pop_state(chat_id: int, user_id: int | None, bot) -> None:
    AWAITING_INPUT.pop(chat_id, None)
    ACTIVE_GAMES.pop(chat_id, None)
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
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    AWAITING_INPUT.pop(chat_id, None)
    ACTIVE_GAMES.pop(chat_id, None)
    MENU_STATE[chat_id] = "main"
    MENU_STACK[chat_id] = ["main"]

    cfg = get_custom_config()
    meta = await asyncio.to_thread(sync_bots_db)
    role_badge = (
        "ADMIN (1st + 2nd Bot Customizer)"
        if is_admin(user_id)
        else "OPERATOR (Story, Games & Sync)"
    )

    msg = (
        f"{frame_box(sc(cfg['bot_title']), cfg['welcome_banner'])}\n"
        f"  ❖ Role         : <b>{esc_html(role_badge)}</b>\n"
        f"  ❖ Sync Version : <code>v{meta.get('sync_version', 1)}</code> | Stories Saved: <b>{meta.get('story_saves_count', 0)}</b>\n"
        f"  ❖ 1st Bot Mode : Theme <code>{esc_html(cfg['bot1_theme'])}</code> | Notif <code>{esc_html(cfg['bot1_notif_style'])}</code>\n"
        f"  ❖ Last Sync    : <code>{esc_html(meta.get('last_sync_ist', 'N/A'))}</code>"
    )
    if MEDIA_ANIM_URL or MEDIA_GIF_URL or MEDIA_PHOTO_URL:
        await send_media_showcase(
            context.bot,
            chat_id,
            "anim" if MEDIA_ANIM_URL else ("gif" if MEDIA_GIF_URL else "photo"),
            msg,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="❖ <b>Command Menu Ready</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user_id),
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user_id),
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
        ACTIVE_GAMES.pop(chat_id, None)
        MENU_STATE[chat_id] = "main"
        MENU_STACK[chat_id] = ["main"]
        await context.bot.send_message(
            chat_id=chat_id,
            text=frame_box(sc(cfg["bot_title"]), "Main Control Menu"),
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

        if pending == "ask_ai":
            meta = await asyncio.to_thread(sync_bots_db)
            ok, ans = await asyncio.to_thread(
                call_groq_resilient,
                "Tum ek crisp, helpful Hinglish telemetry & system advisor ho. Bina kisi repetition ke clean jawab do.",
                f"Sync Context: {json.dumps(meta, default=str)}\nQuestion: {text}",
                320,
                0.25,
            )
            body = esc_html(ans) if ok else f"AI note: <code>{esc_html(ans)}</code>"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{frame_box(sc('GROQ AI ADVISOR'))}\n{body}",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        if not is_admin(user_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ Yeh customization sirf <b>Admin</b> ke liye allowed hai.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        if pending == "set_title":
            push_customization_to_bot1_db("bot_title", text[:100])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 2nd Bot Title updated: <b>{esc_html(text[:100])}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending == "set_banner":
            push_customization_to_bot1_db("welcome_banner", text[:160])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Banner updated: <b>{esc_html(text[:160])}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending == "set_theme":
            valid_t = next(
                (t for t in ("obsidian", "minimal", "cyber", "royal") if t in low),
                None,
            )
            if not valid_t:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Theme likhein: <code>obsidian</code>, <code>minimal</code>, <code>cyber</code>, ya <code>royal</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_customize_keyboard(),
                )
                return
            push_customization_to_bot1_db("bot1_theme", valid_t)
            await asyncio.to_thread(sync_bots_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 1st Bot (`main.py`) theme synced to <b>{esc_html(valid_t)}</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending == "set_notif":
            if "alert" in low:
                n_val = "alerts_only"
            elif "compact" in low:
                n_val = "compact"
            elif "digest" in low:
                n_val = "digest"
            elif "detail" in low:
                n_val = "detailed"
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="Mode likhein: <code>detailed</code>, <code>compact</code>, <code>alerts_only</code>, ya <code>digest</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=admin_customize_keyboard(),
                )
                return
            push_customization_to_bot1_db("bot1_notif_style", n_val)
            await asyncio.to_thread(sync_bots_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 1st Bot notification mode synced to <b>{esc_html(n_val)}</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending == "set_sync_sec":
            try:
                sec = int(text)
                if 15 <= sec <= 3600:
                    push_customization_to_bot1_db("bot1_refresh_sec", str(sec))
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ 1st Bot poll interval synced to <b>{sec}s</b>.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=admin_customize_keyboard(),
                    )
                    return
            except ValueError:
                pass
            await context.bot.send_message(
                chat_id=chat_id,
                text="15 se 3600 ke beech seconds bhejein.",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending == "set_maint_msg":
            push_customization_to_bot1_db("bot1_maint_msg", text[:220])
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 1st Bot maintenance message synced: <code>{esc_html(text[:220])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending == "set_rounds":
            try:
                r_num = int(text)
                if 1 <= r_num <= 15:
                    push_customization_to_bot1_db("game_max_rounds", str(r_num))
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ Game rounds set to <b>{r_num}</b>.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=admin_customize_keyboard(),
                    )
                    return
            except ValueError:
                pass
            await context.bot.send_message(
                chat_id=chat_id,
                text="1 se 15 ke beech number bhejein.",
                reply_markup=admin_customize_keyboard(),
            )
            return

    # Story Hub & A/B Choice actions
    if text == cfg["btn_story"]:
        push_state(chat_id, "story")
        hub_msg = (
            f"{frame_box(sc('MURIM & DEMONIC STORY MODE'), 'Kavana-Style Crazy Fantasy • A/B Choices')}\n"
            "  ❖ <b>🆕 New Story</b>      : Nayi crazy Murim / Demonic Emperor kahani\n"
            "  ❖ <b>▶ Continue Story</b> : Wapas aakar saved chapter yahin se continue karein\n"
            "  ❖ <b>📚 Old Stories</b>     : Purani saved stories dekhein aur load karein\n"
            "  ❖ <b>Choice A / B</b>   : Story ko agle crazy twist par le jaane ke liye dabayein"
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

    # Active game turn
    if chat_id in ACTIVE_GAMES:
        g_out = handle_active_game_input(chat_id, text)
        if g_out:
            await context.bot.send_message(
                chat_id=chat_id,
                text=g_out,
                parse_mode=ParseMode.HTML,
                reply_markup=games_arcade_keyboard(),
            )
            return

    # Arcade menu
    if text == cfg["btn_games"]:
        push_state(chat_id, "games")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{frame_box(sc('AI & MULTI-GAME ARCADE'))}\n  ▸ Koi game chuniye:",
            parse_mode=ParseMode.HTML,
            reply_markup=games_arcade_keyboard(),
        )
        return

    game_btn_map = {
        "🤖 AI Riddle": "ai_riddle",
        "🧭 AI Quest": "ai_scenario",
        "🎲 Tic-Tac-Toe": "ttt",
        "🔢 Number Guess": "guess",
        "✂ RPS": "rps",
        "🖖 RPSLS": "rpsls",
        "🧮 Speed Math": "math",
        "🧠 Trivia": "trivia",
        "🔤 Scramble": "scramble",
        "🎲 Dice Duel": "dice",
        "🪙 Coin Flip": "coin",
        "🎰 Lucky Slots": "slots",
        "💣 Bomb Defusal": "bomb",
        "📈 Higher Lower": "highlow",
    }
    if text in game_btn_map:
        push_state(chat_id, "games")
        out = await asyncio.to_thread(
            start_game_session, chat_id, game_btn_map[text]
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=out,
            parse_mode=ParseMode.HTML,
            reply_markup=games_arcade_keyboard(),
        )
        return

    # Media hub
    if text == cfg["btn_media"]:
        push_state(chat_id, "media")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{frame_box(sc('MEDIA SHOWCASE'))}\n  ▸ Animation, GIF, Photo, ya Video chuniye:",
            parse_mode=ParseMode.HTML,
            reply_markup=media_hub_keyboard(),
        )
        return

    media_btn_map = {
        "🎞 Animation": ("anim", "Companion Live Animation"),
        "🖼 GIF Card": ("gif", "Companion GIF Card"),
        "📷 Photo Card": ("photo", "Companion Photo Card"),
        "🎥 Video Card": ("video", "Companion Video Card"),
    }
    if text in media_btn_map:
        m_kind, m_cap = media_btn_map[text]
        await send_media_showcase(context.bot, chat_id, m_kind, m_cap)
        await context.bot.send_message(
            chat_id=chat_id,
            text="❖ Media Hub",
            parse_mode=ParseMode.HTML,
            reply_markup=media_hub_keyboard(),
        )
        return

    # Sync 1st & 2nd bot
    if text == cfg["btn_sync"]:
        meta = await asyncio.to_thread(sync_bots_db)
        snap_lines = []
        for s in meta.get("latest_snapshots") or []:
            snap_lines.append(
                f"  • Chat <code>{esc_html(s.get('chat_id'))}</code>: "
                f"Online <b>{s.get('online', 0)}</b> | Off <b>{s.get('offline', 0)}</b> | Stale <b>{s.get('stale', 0)}</b>"
            )
        b1s = meta.get("bot1_settings") or {}
        body = (
            f"{frame_box(sc('1ST & 2ND BOT SYNC'))}\n"
            f"  ❖ Sync Version : <code>v{meta.get('sync_version', 1)}</code>\n"
            f"  ❖ Last Sync    : <code>{esc_html(meta.get('last_sync_ist', 'N/A'))}</code>\n"
            f"  ❖ Snapshots    : <b>{meta.get('snapshots_count', 0)}</b> | "
            f"Stories: <b>{meta.get('story_saves_count', 0)}</b> | Alerts: <b>{meta.get('alerts_count', 0)}</b>\n"
            f"  ❖ 1st Bot DB   : Theme=<code>{esc_html(b1s.get('active_theme', cfg['bot1_theme']))}</code> | "
            f"Mode=<code>{esc_html(b1s.get('notif_style', cfg['bot1_notif_style']))}</code> | "
            f"Interval=<code>{esc_html(b1s.get('refresh_sec', cfg['bot1_refresh_sec']))}s</code>\n"
            + ("\n".join(snap_lines) if snap_lines else "  • Main bot snapshot pending.")
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=body,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_for_state(chat_id, user_id),
        )
        return

    if text == cfg["btn_ai"]:
        AWAITING_INPUT[chat_id] = "ask_ai"
        await context.bot.send_message(
            chat_id=chat_id,
            text="🤖 Apna sawaal type karein:",
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if text == "📊 Leaderboard":
        conn = get_db_conn()
        try:
            rows = conn.execute(
                """
                SELECT user_id, COALESCE(SUM(score_delta), 0) AS pts, COUNT(*) AS plays
                FROM game_scores
                GROUP BY user_id
                ORDER BY pts DESC
                LIMIT 10
                """
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            lb_txt = f"{frame_box(sc('LEADERBOARD'))}\n  • Abhi koi score record nahi hai."
        else:
            lines = [frame_box(sc("LEADERBOARD"))]
            for i, r in enumerate(rows, start=1):
                lines.append(
                    f"  {i}. User <code>{r['user_id']}</code> — <b>{r['pts']} pts</b> ({r['plays']} plays)"
                )
            lb_txt = "\n".join(lines)
        await context.bot.send_message(
            chat_id=chat_id,
            text=lb_txt,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_for_state(chat_id, user_id),
        )
        return

    # Admin-only customization controls
    if text == cfg["btn_admin_custom"] or MENU_STATE.get(chat_id) == "admin_custom":
        if not is_admin(user_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ Yeh customization panel <b>sirf Admin</b> ke liye locked hai.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        if text == cfg["btn_admin_custom"]:
            push_state(chat_id, "admin_custom")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{frame_box(sc('ADMIN CUSTOM ENGINE'), '1st & 2nd Bot Live Control')}",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "✏ 2nd Bot Title":
            AWAITING_INPUT[chat_id] = "set_title"
            await context.bot.send_message(
                chat_id=chat_id,
                text="2nd Bot ka naya title bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "📝 Welcome Banner":
            AWAITING_INPUT[chat_id] = "set_banner"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Naya welcome banner text bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🎨 1st Bot Theme":
            AWAITING_INPUT[chat_id] = "set_theme"
            await context.bot.send_message(
                chat_id=chat_id,
                text="1st Bot (`main.py`) theme likhein: <code>obsidian</code>, <code>minimal</code>, <code>cyber</code>, ya <code>royal</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🔔 1st Bot Notif":
            AWAITING_INPUT[chat_id] = "set_notif"
            await context.bot.send_message(
                chat_id=chat_id,
                text="1st Bot mode likhein: <code>detailed</code>, <code>compact</code>, <code>alerts_only</code>, ya <code>digest</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "⏱ 1st Bot Sync Sec":
            AWAITING_INPUT[chat_id] = "set_sync_sec"
            await context.bot.send_message(
                chat_id=chat_id,
                text="1st Bot poll interval (15–3600 seconds) bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🛠 1st Bot Maint":
            cur_m = "0" if cfg.get("bot1_maint_mode") == "1" else "1"
            push_customization_to_bot1_db("bot1_maint_mode", cur_m)
            AWAITING_INPUT[chat_id] = "set_maint_msg"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 1st Bot Maintenance: <b>{'ON' if cur_m == '1' else 'OFF'}</b>\nNaya maintenance message bhejein (ya <b>⬅ Back</b> dabayein):",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "⚡ 1st Bot ON/OFF":
            cur_en = "0" if cfg.get("bot1_bot_enabled") == "1" else "1"
            push_customization_to_bot1_db("bot1_bot_enabled", cur_en)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚡ 1st Bot Global State: <b>{'ENABLED' if cur_en == '1' else 'DISABLED'}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🔢 Game Rounds":
            AWAITING_INPUT[chat_id] = "set_rounds"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Game rounds limit (1–15) bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🔄 Force Sync Now":
            meta = await asyncio.to_thread(sync_bots_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Synced <code>v{meta.get('sync_version')}</code> at <code>{esc_html(meta.get('last_sync_ist'))}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "📋 View Custom Config":
            cur_cfg = get_custom_config()
            lines = [frame_box(sc("ACTIVE CUSTOM CONFIG"))]
            for k, v in cur_cfg.items():
                lines.append(f"  ❖ <code>{esc_html(k)}</code> = <b>{esc_html(v)}</b>")
            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "♻ Reset Defaults":
            reset_all_customizations()
            await asyncio.to_thread(sync_bots_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text="♻ Sabhi customizations default par reset ho gaye.",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
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
            BotCommand("start", "Start Companion Suite"),
            BotCommand("menu", "Open Main Menu"),
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Baagi Jaat Ultra Companion Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
