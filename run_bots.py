import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def start_proc(script_name: str) -> subprocess.Popen:
    script_path = os.path.join(BASE_DIR, script_name)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen([sys.executable, script_path], env=env)


def main() -> None:
    procs = {
        "main.py": start_proc("main.py"),
        "companion_bot.py": start_proc("companion_bot.py"),
    }
    try:
        while True:
            for name, p in list(procs.items()):
                code = p.poll()
                if code is not None:
                    print(f"[RUNNER] {name} exited with {code}, restarting in 3s...")
                    time.sleep(3)
                    procs[name] = start_proc(name)
            time.sleep(5)
    except KeyboardInterrupt:
        for p in procs.values():
            p.terminate()


if __name__ == "__main__":
    main()
