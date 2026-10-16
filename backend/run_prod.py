"""Wings Theory — production server launcher (Python version of start_server.ps1).

Works on Windows (shop PC) and Linux (DigitalOcean).

Differences from the .ps1, on purpose:
  * If WT_SECRET / PORT are ALREADY set by the platform (e.g. DigitalOcean
    App Platform env vars), they are used instead of the local file/default.
  * The LAN-IP watcher uses Python's socket module instead of Get-NetRoute,
    so it also works on Linux. On a cloud server it is harmless (just prints).

Run:  python run_prod.py
"""

import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

# wt/db.py requires WT_ENV and WT_DB to be set and to agree.
os.environ["WT_ENV"] = "production"
os.environ.setdefault("WT_DB", str(BASE_DIR / "wings_theory.db"))

# Signing secret: platform env var wins; otherwise generate once and persist.
if not os.environ.get("WT_SECRET"):
    secret_path = BASE_DIR / ".secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        print(f"Generated a new signing secret at {secret_path} "
              "-- back this up; losing it logs everyone out.")
    os.environ["WT_SECRET"] = secret_path.read_text(encoding="utf-8").strip()

PORT = int(os.environ.get("PORT", "8000"))  # App Platform injects PORT


def get_lan_ip() -> str:
    """Finds the IP of the network card used for the default route.
    Connecting a UDP socket sends no packets; it just asks the OS which
    local address it would use (same idea as Get-NetRoute 0.0.0.0/0)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def show_urls(ip: str) -> None:
    print(f"Console (owner):   http://{ip}:{PORT}/console")
    print(f"POS terminal:      http://{ip}:{PORT}/pos")
    print(f"Kitchen display:   http://{ip}:{PORT}/kds")


def watch_ip(initial_ip: str) -> None:
    """Background thread: every 10s, reprint URLs if the address changed
    (phone hotspots often hand out a new IP on reconnect)."""
    current = initial_ip
    while True:
        time.sleep(10)
        new_ip = get_lan_ip()
        if new_ip != current:
            current = new_ip
            print()
            print("Network address changed. If a tablet stops connecting, "
                  "use the new address below:")
            show_urls(current)
            print()


if __name__ == "__main__":
    lan_ip = get_lan_ip()
    print("Wings Theory -- starting backend (production)...")
    print(f"Database: {os.environ['WT_DB']}")
    show_urls(lan_ip)
    print("(tablets must be on the same Wi-Fi / hotspot as this PC)")
    print()

    # daemon=True: thread dies automatically when the server stops.
    threading.Thread(target=watch_ip, args=(lan_ip,), daemon=True).start()

    try:
        uvicorn.run("wt.api:app", host="0.0.0.0", port=PORT, proxy_headers=True)
    except Exception as exc:  # keep the error visible, like the .ps1 did
        print()
        print("Server stopped with an error:")
        print(exc)
        if sys.stdin.isatty():  # only pause when a human is at a terminal
            input("Press Enter to close this window...")
        raise
