import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_THIRD_BOT_CODE = '''import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

THIRD_BOT_TOKEN = (
    os.getenv("THIRD_BOT_TOKEN", "").strip()
    or os.getenv("DYNAMIC_BOT_TOKEN", "").strip()
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "3rd Dynamic Bot Online! Companion Bot se AI ya manual code bhej kar mujhe live edit karein."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    txt = update.message.text.strip()
    await update.message.reply_text(f"3rd Bot Live Reply: {txt}")


def main() -> None:
    if not THIRD_BOT_TOKEN:
        raise RuntimeError("THIRD_BOT_TOKEN set nahi hai.")

    app = ApplicationBuilder().token(THIRD_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("[3RD_BOT] Polling started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
'''


def ensure_third_bot_file() -> str:
    path = os.path.join(BASE_DIR, "third_bot.py")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_THIRD_BOT_CODE)
    return path


def has_third_bot_token() -> bool:
    return bool(
        os.getenv("THIRD_BOT_TOKEN", "").strip()
        or os.getenv("DYNAMIC_BOT_TOKEN", "").strip()
    )


def start_process(script_name: str) -> subprocess.Popen:
    script_path = os.path.join(BASE_DIR, script_name)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    print(f"[RUNNER] Starting {script_name}...")
    return subprocess.Popen(
        [sys.executable, script_path],
        cwd=BASE_DIR,
        env=env,
    )


def stop_process(proc: subprocess.Popen | None, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[RUNNER] Stopping {name} (PID {proc.pid})...")
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        print(f"[RUNNER] Force killing {name} (PID {proc.pid})...")
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass


def main() -> None:
    third_path = ensure_third_bot_file()
    last_third_mtime = os.path.getmtime(third_path)
    last_third_size = os.path.getsize(third_path)

    procs: dict[str, subprocess.Popen | None] = {
        "main.py": start_process("main.py"),
        "companion_bot.py": start_process("companion_bot.py"),
        "third_bot.py": start_process("third_bot.py")
        if has_third_bot_token()
        else None,
    }

    stopping = False

    def _handle_sig(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    try:
        while not stopping:
            # 1. Watch third_bot.py for live edits from companion_bot.py
            if os.path.exists(third_path):
                cur_mtime = os.path.getmtime(third_path)
                cur_size = os.path.getsize(third_path)
                if cur_mtime != last_third_mtime or cur_size != last_third_size:
                    last_third_mtime = cur_mtime
                    last_third_size = cur_size
                    print(
                        "[RUNNER] Detected live change in third_bot.py -> Reloading 3rd bot..."
                    )
                    stop_process(procs.get("third_bot.py"), "third_bot.py")
                    if has_third_bot_token():
                        time.sleep(1.5)
                        procs["third_bot.py"] = start_process("third_bot.py")
                    else:
                        procs["third_bot.py"] = None

            # 2. Monitor & auto-restart stopped/crashed processes
            for name in ("main.py", "companion_bot.py", "third_bot.py"):
                if name == "third_bot.py" and not has_third_bot_token():
                    continue
                p = procs.get(name)
                if p is None or p.poll() is not None:
                    exit_code = p.returncode if p is not None else "N/A"
                    print(
                        f"[RUNNER] {name} exited (code={exit_code}). Restarting in 3s..."
                    )
                    time.sleep(3)
                    if stopping:
                        break
                    procs[name] = start_process(name)

            time.sleep(2)
    finally:
        print("[RUNNER] Shutting down all bot processes...")
        for name in ("third_bot.py", "companion_bot.py", "main.py"):
            stop_process(procs.get(name), name)


if __name__ == "__main__":
    main()
