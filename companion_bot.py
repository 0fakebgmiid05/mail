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

IST = timezone(timedelta(hours=5, minutes=30))
GROQ_CHAT_URL: str = "https://api.groq.com/openai/v1/chat/completions"

# Runtime States
AWAITING_ADMIN_INPUT: dict[int, str] = {}
MENU_STACK: dict[int, list[str]] = {}
MENU_STATE: dict[int, str] = {}
ACTIVE_GAMES: dict[int, dict[str, Any]] = {}
LAST_SYNC_META: dict[str, Any] = {
    "last_sync_ist": "Not synced yet",
    "snapshots_count": 0,
    "watchlist_count": 0,
    "alerts_count": 0,
    "sync_version": 1,
}

DEFAULT_CUSTOM_CONFIG: dict[str, str] = {
    "bot_title": f"{DEFAULT_CLIENT_NAME} COMPANION & ARCADE",
    "welcome_banner": "Admin Custom Engine + Multi-Game Lounge + DB Sync Active",
    "btn_games": "🎮 Games Arcade",
    "btn_sync": "🔄 Sync Bridge",
    "btn_ai": "🤖 AI Advisor",
    "btn_admin_custom": "👑 Admin Customize",
    "admin_only_mode": "1",
    "game_max_rounds": "5",
}


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and int(user_id) == int(ADMIN_TG_ID)


def esc_html(val: Any) -> str:
    s = str(val if val is not None else "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ==============================================================================
# SHARED SQLITE DATABASE SYNC LAYER (WITH main.py)
# ==============================================================================
def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
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
            rows = conn.execute(
                "SELECT cfg_key, cfg_val FROM admin_custom_config"
            ).fetchall()
            for r in rows:
                cfg[str(r["cfg_key"])] = str(r["cfg_val"])
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("get_custom_config error: %s", exc)
    return cfg


def set_custom_config_value(key: str, val: str) -> None:
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
        # Also mirror key settings into shared bot_settings table
        if key == "bot_title":
            conn.execute(
                "INSERT OR REPLACE INTO bot_settings(k, v) VALUES('companion_title', ?)",
                (clean_val,),
            )
        conn.commit()
    finally:
        conn.close()


def reset_custom_config_defaults() -> None:
    conn = get_db_conn()
    try:
        for k, v in DEFAULT_CUSTOM_CONFIG.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO admin_custom_config(cfg_key, cfg_val, updated_at)
                VALUES(?, ?, ?)
                """,
                (k, v, now_ist_str()),
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


def sync_with_main_db() -> dict[str, Any]:
    """Reads main.py tables safely and updates companion sync heartbeat."""
    snap_cnt = 0
    watch_cnt = 0
    alert_cnt = 0
    latest_snapshots: list[dict[str, Any]] = []
    settings_map: dict[str, str] = {}

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

        if "bot_settings" in tables:
            for b_row in cur.execute("SELECT k, v FROM bot_settings").fetchall():
                settings_map[str(b_row["k"])] = str(b_row["v"])

        prev_ver = int(LAST_SYNC_META.get("sync_version", 1))
        new_ver = prev_ver + 1
        ping_time = now_ist_str()
        meta = {
            "last_sync_ist": ping_time,
            "snapshots_count": snap_cnt,
            "watchlist_count": watch_cnt,
            "alerts_count": alert_cnt,
            "sync_version": new_ver,
            "latest_snapshots": latest_snapshots,
            "main_settings": settings_map,
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
# MULTI-GAME ARCADE ENGINE (10+ GAMES)
# ==============================================================================
TRIVIA_BANK: list[tuple[str, str, str]] = [
    (
        "Modbus RTU ka physical serial layer aam taur par kaunsa hota hai?",
        "RS-485",
        "485",
    ),
    (
        "HART protocol analog signal ke saath kaunsa signal overlay karta hai?",
        "FSK Digital Signal",
        "fsk",
    ),
    (
        "PLC Ladder Logic mein normally open contact ko kya kehte hain?",
        "XIC / NO Contact",
        "no",
    ),
    (
        "Python mein immutable sequence ka common example kaunsa hai?",
        "Tuple",
        "tuple",
    ),
    (
        "SQLite mein primary key unique index banata hai — True ya False?",
        "True",
        "true",
    ),
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
            "🔢 <b>Number Guess (1–100)</b>\n"
            "Maine 1 se 100 ke beech ek number chuna hai. Apna guess number bhejein:"
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
            f"✂ <b>Rock • Paper • Scissors</b> (Best of {max_rounds})\n"
            "<code>Rock</code>, <code>Paper</code>, ya <code>Scissors</code> bhejein:"
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
            "🖖 <b>RPSLS Arcade</b>\n"
            "Choices: <code>Rock</code>, <code>Paper</code>, <code>Scissors</code>, <code>Lizard</code>, <code>Spock</code>"
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
            f"🧮 <b>Speed Math (Round 1/{max_rounds})</b>\n"
            f"Solve karein: <code>{a} {op} {b} = ?</code>"
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
            f"🧠 <b>Tech & Telemetry Trivia</b>\n"
            f"Sawaal: <b>{esc_html(q_txt)}</b>\n"
            "Apna jawab type karein:"
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
            f"🔤 <b>Word Scramble</b>\n"
            f"Scrambled: <code>{esc_html(scrambled)}</code>\n"
            f"Hint: <i>{esc_html(hint)}</i>\n"
            "Sahi shabd bhejein:"
        )

    if game_key == "dice":
        u_roll = random.randint(1, 6)
        b_roll = random.randint(1, 6)
        if u_roll > b_roll:
            res = "Aap jeete!"
            delta = 10
        elif b_roll > u_roll:
            res = "Bot jeeta!"
            delta = -5
        else:
            res = "Tie!"
            delta = 2
        record_game_score(chat_id, "Dice Duel", res, delta)
        return (
            f"🎲 <b>Dice Duel</b>\n"
            f"Aap: <b>{u_roll}</b> | Bot: <b>{b_roll}</b> → <b>{esc_html(res)}</b> (Pts: {delta:+d})"
        )

    if game_key == "coin":
        u_side = random.choice(["Heads", "Tails"])
        b_side = random.choice(["Heads", "Tails"])
        won = u_side == b_side
        res = "Match!" if won else "Miss!"
        delta = 10 if won else 0
        record_game_score(chat_id, "Coin Flip", res, delta)
        return (
            f"🪙 <b>Coin Flip</b>\n"
            f"Aapki Call: <b>{u_side}</b> | Coin: <b>{b_side}</b> → <b>{esc_html(res)}</b>"
        )

    if game_key == "slots":
        syms = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
        reels = [random.choice(syms) for _ in range(3)]
        if len(set(reels)) == 1:
            res = "JACKPOT!"
            delta = 50
        elif len(set(reels)) == 2:
            res = "Pair Win!"
            delta = 15
        else:
            res = "Try Again"
            delta = 0
        record_game_score(chat_id, "Lucky Slots", res, delta)
        return (
            f"🎰 <b>Lucky Slots</b>\n"
            f"[ <b>{reels[0]} | {reels[1]} | {reels[2]}</b> ]\n"
            f"Result: <b>{esc_html(res)}</b> (Pts: {delta:+d})"
        )

    if game_key == "bomb":
        safe_wire = random.choice(["RED", "BLUE", "GREEN"])
        ACTIVE_GAMES[chat_id] = {"type": "bomb", "safe": safe_wire}
        return (
            "💣 <b>Bomb Defusal</b>\n"
            "Ek wire ka naam type karein: <code>RED</code>, <code>BLUE</code>, ya <code>GREEN</code>:"
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
            f"📈 <b>Higher or Lower</b>\n"
            f"Current Number: <b>{cur_num}</b>\n"
            "Agla number (1–100) <code>HIGH</code> hoga ya <code>LOW</code>?"
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
    return f"🎲 <b>Tic-Tac-Toe</b>\n{grid}\n\n▸ {esc_html(note)}"


def handle_active_game_input(chat_id: int, text: str) -> str | None:
    st = ACTIVE_GAMES.get(chat_id)
    if not st:
        return None

    gtype = st.get("type")
    low = text.strip().lower()

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
            return f"🎉 Sahi number <b>{secret}</b> aapne <b>{tries}</b> tries mein dhoondh liya! (+{pts} pts)"
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
            return f"Aap: <b>{u}</b> vs Bot: <b>{b}</b> → {msg}\nFinal Score: {score_str}"
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
                return f"🎉 Sab rounds complete! Total Streak: <b>{st['streak']}</b> (+{pts} pts)"
            st["round"] = int(st["round"]) + 1
            a = random.randint(12, 99)
            b = random.randint(2, 25)
            op = random.choice(MATH_OPS)
            st["answer"] = (
                a + b if op == "+" else (a - b if op == "-" else a * b)
            )
            return (
                f"✅ Sahi! (Round {st['round']}/{st['max_rounds']})\n"
                f"Agla sawaal: <code>{a} {op} {b} = ?</code>"
            )
        ACTIVE_GAMES.pop(chat_id, None)
        return f"❌ Galat! Sahi jawab <b>{correct}</b> tha. Game over."

    if gtype == "trivia":
        ans_key = str(st.get("ans_key") or "")
        ans_disp = str(st.get("ans_disp") or "")
        ACTIVE_GAMES.pop(chat_id, None)
        if ans_key and ans_key in low:
            record_game_score(chat_id, "Trivia", "Correct", 15)
            return f"✅ bilkul sahi! Answer: <b>{esc_html(ans_disp)}</b> (+15 pts)"
        return f"ℹ Sahi answer: <b>{esc_html(ans_disp)}</b>"

    if gtype == "scramble":
        target = str(st.get("word") or "").lower()
        ACTIVE_GAMES.pop(chat_id, None)
        if low == target:
            record_game_score(chat_id, "Word Scramble", "Solved", 15)
            return f"✅ Shabaash! Sahi word <b>{esc_html(target.upper())}</b> tha (+15 pts)"
        return f"ℹ Sahi word <b>{esc_html(target.upper())}</b> tha."

    if gtype == "bomb":
        safe = str(st.get("safe") or "").lower()
        ACTIVE_GAMES.pop(chat_id, None)
        if low == safe:
            record_game_score(chat_id, "Bomb Defusal", "Defused", 25)
            return f"💣✅ Bomb safaltapoorvak defuse ho gaya! Safe wire <b>{safe.upper()}</b> thi (+25 pts)"
        return f"💥 Galat wire! Safe wire <b>{safe.upper()}</b> thi."

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
# GROQ AI ADVISOR (OPTIONAL)
# ==============================================================================
def ask_groq_companion(prompt: str, sync_meta: dict[str, Any]) -> str:
    if not GROQ_API_KEY:
        return "GROQ_API_KEY set nahi hai. Local sync summary use karein."
    try:
        r = requests.post(
            GROQ_CHAT_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {
                        "role": "system",
                        "content": "Tum ek crisp Hinglish assistant ho. Bina kisi repetition ke short aur helpful jawab do.",
                    },
                    {
                        "role": "user",
                        "content": f"Sync Meta: {json.dumps(sync_meta)}\nQuery: {prompt}",
                    },
                ],
                "max_tokens": 280,
                "temperature": 0.25,
            },
            timeout=20,
        )
        if r.ok:
            txt = (
                (((r.json().get("choices") or [{}])[0]).get("message") or {})
                .get("content")
                or ""
            ).strip()
            if txt:
                return esc_html(txt[:1200])
    except Exception as exc:
        logger.warning("Groq companion error: %s", exc)
    return "Groq AI se abhi response nahi mila."


# ==============================================================================
# KEYBOARDS (WITH BACK & MAIN MENU EVERYWHERE + ADMIN-ONLY CUSTOMIZATION)
# ==============================================================================
def main_menu_keyboard(user_id: int | None) -> ReplyKeyboardMarkup:
    cfg = get_custom_config()
    rows: list[list[KeyboardButton]] = [
        [
            KeyboardButton(cfg["btn_games"]),
            KeyboardButton(cfg["btn_sync"]),
        ],
        [
            KeyboardButton(cfg["btn_ai"]),
            KeyboardButton("📊 Leaderboard"),
        ],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(cfg["btn_admin_custom"])])
    rows.append([KeyboardButton("🏠 Main Menu")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def games_arcade_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
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


def admin_customize_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("✏ Bot Title"),
                KeyboardButton("📝 Welcome Banner"),
            ],
            [
                KeyboardButton("🏷 Games Btn Text"),
                KeyboardButton("🏷 Sync Btn Text"),
            ],
            [
                KeyboardButton("🔢 Game Rounds"),
                KeyboardButton("🔄 Force Sync Now"),
            ],
            [
                KeyboardButton("♻ Reset Defaults"),
                KeyboardButton("📋 View Custom Config"),
            ],
            [KeyboardButton("⬅ Back"), KeyboardButton("🏠 Main Menu")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def keyboard_for_state(
    chat_id: int, user_id: int | None
) -> ReplyKeyboardMarkup:
    state = MENU_STATE.get(chat_id, "main")
    if state == "games":
        return games_arcade_keyboard()
    if state == "admin_custom" and is_admin(user_id):
        return admin_customize_keyboard()
    return main_menu_keyboard(user_id)


def push_state(chat_id: int, new_state: str) -> None:
    cur = MENU_STATE.get(chat_id, "main")
    if cur != new_state:
        st = MENU_STACK.setdefault(chat_id, [])
        st.append(cur)
    MENU_STATE[chat_id] = new_state


async def pop_state(chat_id: int, user_id: int | None, bot) -> None:
    AWAITING_ADMIN_INPUT.pop(chat_id, None)
    ACTIVE_GAMES.pop(chat_id, None)
    st = MENU_STACK.get(chat_id) or []
    prev = st.pop() if st else "main"
    MENU_STATE[chat_id] = prev
    await bot.send_message(
        chat_id=chat_id,
        text=f"⬅ Returned to <b>{esc_html(prev.upper())}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_for_state(chat_id, user_id),
    )


# ==============================================================================
# HANDLERS & BACKGROUND SYNC LOOP
# ==============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None
    AWAITING_ADMIN_INPUT.pop(chat_id, None)
    ACTIVE_GAMES.pop(chat_id, None)
    MENU_STATE[chat_id] = "main"
    MENU_STACK[chat_id] = ["main"]

    cfg = get_custom_config()
    meta = await asyncio.to_thread(sync_with_main_db)
    role_badge = "ADMIN (Full Customization)" if is_admin(user_id) else "USER (Arcade & Sync View)"

    msg = (
        f"╔════════════════════════════╗\n"
        f"║  <b>{esc_html(cfg['bot_title'])}</b>\n"
        f"╚════════════════════════════╝\n"
        f"▸ {esc_html(cfg['welcome_banner'])}\n"
        f"▸ Role         : <b>{esc_html(role_badge)}</b>\n"
        f"▸ Sync Version : <code>v{meta.get('sync_version', 1)}</code> | Snapshots: <b>{meta.get('snapshots_count', 0)}</b>\n"
        f"▸ Last Sync    : <code>{esc_html(meta.get('last_sync_ist', 'N/A'))}</code>"
    )
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
    user_id = update.effective_user.id if update.effective_user else None
    text = update.message.text.strip()
    cfg = get_custom_config()

    if text in ("🏠 Main Menu", "/menu"):
        AWAITING_ADMIN_INPUT.pop(chat_id, None)
        ACTIVE_GAMES.pop(chat_id, None)
        MENU_STATE[chat_id] = "main"
        MENU_STACK[chat_id] = ["main"]
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🏠 <b>{esc_html(cfg['bot_title'])}</b> — Main Menu",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user_id),
        )
        return

    if text in ("⬅ Back", "Back"):
        await pop_state(chat_id, user_id, context.bot)
        return

    # Admin-only pending input handlers
    pending_admin = AWAITING_ADMIN_INPUT.get(chat_id)
    if pending_admin:
        if not is_admin(user_id):
            AWAITING_ADMIN_INPUT.pop(chat_id, None)
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ Sirf Admin hi customization change kar sakta hai.",
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        AWAITING_ADMIN_INPUT.pop(chat_id, None)
        key_map = {
            "set_title": "bot_title",
            "set_banner": "welcome_banner",
            "set_btn_games": "btn_games",
            "set_btn_sync": "btn_sync",
        }
        if pending_admin in key_map:
            set_custom_config_value(key_map[pending_admin], text[:120])
            await asyncio.to_thread(sync_with_main_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Updated <code>{esc_html(key_map[pending_admin])}</code>: <b>{esc_html(text[:120])}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending_admin == "set_rounds":
            try:
                r_num = int(text)
                if 1 <= r_num <= 15:
                    set_custom_config_value("game_max_rounds", str(r_num))
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ Game max rounds set to <b>{r_num}</b>.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=admin_customize_keyboard(),
                    )
                    return
            except ValueError:
                pass
            await context.bot.send_message(
                chat_id=chat_id,
                text="1 se 15 ke beech valid number bhejein.",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if pending_admin == "ask_ai":
            meta = await asyncio.to_thread(sync_with_main_db)
            ans = await asyncio.to_thread(ask_groq_companion, text, meta)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🤖 <b>AI Advisor</b>\n{ans}",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
            return

    # Active game input
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

    # Main & sub-menu buttons
    if text == cfg["btn_games"]:
        push_state(chat_id, "games")
        await context.bot.send_message(
            chat_id=chat_id,
            text="🎮 <b>Multi-Game Arcade</b> — Koi bhi game chuniye:",
            parse_mode=ParseMode.HTML,
            reply_markup=games_arcade_keyboard(),
        )
        return

    game_btn_map = {
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
        out = start_game_session(chat_id, game_btn_map[text])
        await context.bot.send_message(
            chat_id=chat_id,
            text=out,
            parse_mode=ParseMode.HTML,
            reply_markup=games_arcade_keyboard(),
        )
        return

    if text == cfg["btn_sync"]:
        meta = await asyncio.to_thread(sync_with_main_db)
        snap_lines = []
        for s in meta.get("latest_snapshots") or []:
            snap_lines.append(
                f"  • Chat <code>{esc_html(s.get('chat_id'))}</code>: "
                f"Online <b>{s.get('online', 0)}</b> | Off <b>{s.get('offline', 0)}</b> | "
                f"Stale <b>{s.get('stale', 0)}</b>"
            )
        body = (
            f"🔄 <b>DB &amp; Main Bot Sync Bridge</b>\n"
            f"▸ Sync Version : <code>v{meta.get('sync_version', 1)}</code>\n"
            f"▸ Last Sync    : <code>{esc_html(meta.get('last_sync_ist', 'N/A'))}</code>\n"
            f"▸ Snapshots    : <b>{meta.get('snapshots_count', 0)}</b> | "
            f"Watchlist: <b>{meta.get('watchlist_count', 0)}</b> | "
            f"Alerts: <b>{meta.get('alerts_count', 0)}</b>\n"
            + ("\n".join(snap_lines) if snap_lines else "  • Abhi main bot snapshot pending hai.")
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=body,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_for_state(chat_id, user_id),
        )
        return

    if text == cfg["btn_ai"]:
        AWAITING_ADMIN_INPUT[chat_id] = "ask_ai"
        await context.bot.send_message(
            chat_id=chat_id,
            text="🤖 Apna sawaal type karein (sync &amp; operations context ke saath):",
            parse_mode=ParseMode.HTML,
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
            lb_text = "📊 <b>Arcade Leaderboard</b>\nAbhi koi score record nahi hai."
        else:
            lines = ["📊 <b>Arcade Leaderboard</b>"]
            for idx, r in enumerate(rows, start=1):
                lines.append(
                    f"  {idx}. User <code>{r['user_id']}</code> — <b>{r['pts']} pts</b> ({r['plays']} plays)"
                )
            lb_text = "\n".join(lines)
        await context.bot.send_message(
            chat_id=chat_id,
            text=lb_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard_for_state(chat_id, user_id),
        )
        return

    # Admin-only customization menu
    if text == cfg["btn_admin_custom"] or MENU_STATE.get(chat_id) == "admin_custom":
        if not is_admin(user_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text="⛔ Yeh section <b>sirf Admin</b> ke liye locked hai.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(user_id),
            )
            return

        if text == cfg["btn_admin_custom"]:
            push_state(chat_id, "admin_custom")
            await context.bot.send_message(
                chat_id=chat_id,
                text="👑 <b>Admin-Only Bot Customization Engine</b>\nHar ek customization sirf Admin ke liye active hai:",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "✏ Bot Title":
            AWAITING_ADMIN_INPUT[chat_id] = "set_title"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Naya Bot Title bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "📝 Welcome Banner":
            AWAITING_ADMIN_INPUT[chat_id] = "set_banner"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Naya Welcome Banner text bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🏷 Games Btn Text":
            AWAITING_ADMIN_INPUT[chat_id] = "set_btn_games"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Games button ka naya label bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🏷 Sync Btn Text":
            AWAITING_ADMIN_INPUT[chat_id] = "set_btn_sync"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Sync button ka naya label bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🔢 Game Rounds":
            AWAITING_ADMIN_INPUT[chat_id] = "set_rounds"
            await context.bot.send_message(
                chat_id=chat_id,
                text="Game rounds limit (1–15) bhejein:",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "🔄 Force Sync Now":
            meta = await asyncio.to_thread(sync_with_main_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Sync complete: <code>v{meta.get('sync_version')}</code> ({esc_html(meta.get('last_sync_ist'))})",
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "♻ Reset Defaults":
            reset_custom_config_defaults()
            await asyncio.to_thread(sync_with_main_db)
            await context.bot.send_message(
                chat_id=chat_id,
                text="♻ Sabhi admin customizations default par reset kar diye gaye hain.",
                reply_markup=admin_customize_keyboard(),
            )
            return

        if text == "📋 View Custom Config":
            cur_cfg = get_custom_config()
            lines = ["📋 <b>Current Admin Customization Config</b>"]
            for k, v in cur_cfg.items():
                lines.append(f"  • <code>{esc_html(k)}</code> = <b>{esc_html(v)}</b>")
            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=admin_customize_keyboard(),
            )
            return

    await context.bot.send_message(
        chat_id=chat_id,
        text="Kripya menu button chuniye ya <b>🏠 Main Menu</b> dabayein.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_for_state(chat_id, user_id),
    )


async def background_sync_worker(application) -> None:
    await asyncio.sleep(3)
    while True:
        try:
            await asyncio.to_thread(sync_with_main_db)
        except Exception as exc:
            logger.warning("Background sync worker note: %s", exc)
        await asyncio.sleep(20)


async def post_init(application) -> None:
    init_companion_db()
    await asyncio.to_thread(sync_with_main_db)
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
            "SECOND_BOT_TOKEN (ya COMPANION_BOT_TOKEN) set nahi hai. "
            "main.py ke saath alag bot token use karein taaki polling conflict na ho."
        )
    if MAIN_BOT_TOKEN and COMPANION_BOT_TOKEN == MAIN_BOT_TOKEN:
        raise RuntimeError(
            "SECOND_BOT_TOKEN aur BOT_TOKEN same nahi hone chahiye. "
            "Dusre bot ka token set karein."
        )

    init_companion_db()
    sync_with_main_db()

    app = (
        ApplicationBuilder()
        .token(COMPANION_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Baagi Jaat Companion & Arcade Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
