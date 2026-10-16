"""Wings Theory -- single server launcher (combines run_dev.py + run_prod.py).

Run:
  python run.py            -> development (default)
  python run.py dev        -> development: dev DB, 127.0.0.1, auto-reload
  python run.py prod       -> production:  real DB, 0.0.0.0 (LAN), IP watcher
  python run.py dev --lan  -> development, but reachable from tablets on Wi-Fi

Env vars that still win if set by the platform (e.g. DigitalOcean):
  PORT, WT_SECRET (prod only), WT_DB (prod only, ignored if it points at dev/)
"""

import argparse
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

BASE_DIR = Path(__file__).resolve().parent
DEV_DIR = BASE_DIR / "dev"
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))  # so "wt.api" can be imported


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_or_create_secret(secret_path: Path) -> str:
    """Generate once, reuse forever, so logins survive restarts."""
    secret_path.parent.mkdir(exist_ok=True)
    if not secret_path.exists():
        secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        print(f"Generated a new signing secret at {secret_path} "
              "-- back this up; losing it logs everyone out.")
    return secret_path.read_text(encoding="utf-8").strip()


def get_lan_ip() -> str:
    """UDP connect sends no packets; it just asks the OS which local
    address it would use for the default route."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def port_is_free(host: str, port: int) -> bool:
    """Fails fast if another server (e.g. the OTHER mode) already owns the
    port -- otherwise you end up logging into the wrong database.

    Checked by CONNECTING, not binding: on Windows a 127.0.0.1 bind can
    succeed alongside an existing 0.0.0.0 listener, so a bind test misses
    exactly the dev-vs-prod collision this exists to catch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def show_urls(ip: str, port: int) -> None:
    print(f"Console (owner):   http://{ip}:{port}/console")
    print(f"POS terminal:      http://{ip}:{port}/pos")
    print(f"Kitchen display:   http://{ip}:{port}/kds")


def watch_ip(initial_ip: str, port: int) -> None:
    """Every 10s, reprint URLs if the LAN address changed (hotspots do this)."""
    current = initial_ip
    while True:
        time.sleep(10)
        new_ip = get_lan_ip()
        if new_ip != current:
            current = new_ip
            print("\nNetwork address changed. If a tablet stops connecting, "
                  "use the new address below:")
            show_urls(current, port)
            print()


# ---------------------------------------------------------------------------
# per-mode environment
# ---------------------------------------------------------------------------
def configure(mode: str) -> None:
    if mode == "dev":
        # Forced, never inherited: dev can never touch the production file.
        os.environ["WT_ENV"] = "development"
        os.environ["WT_DB"] = str(DEV_DIR / "wings_theory_dev.db")
        os.environ["WT_SECRET"] = load_or_create_secret(DEV_DIR / ".secret")
    else:
        os.environ["WT_ENV"] = "production"
        # Platform may supply WT_DB, but a leftover dev path from this same
        # terminal is ignored -- that mismatch is what wt/db.py refuses.
        env_db = os.environ.get("WT_DB", "")
        if not env_db or Path(env_db).resolve().is_relative_to(DEV_DIR.resolve()):
            os.environ["WT_DB"] = str(BASE_DIR / "wings_theory.db")
        if not os.environ.get("WT_SECRET"):
            os.environ["WT_SECRET"] = load_or_create_secret(BASE_DIR / ".secret")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Wings Theory server launcher")
    parser.add_argument("mode", nargs="?", choices=["dev", "prod"], default="dev")
    parser.add_argument("--lan", action="store_true",
                        help="dev only: listen on 0.0.0.0 so tablets can connect")
    args = parser.parse_args()

    configure(args.mode)

    is_dev = args.mode == "dev"
    port = int(os.environ.get("PORT", "8000"))
    host = "0.0.0.0" if (not is_dev or args.lan) else "127.0.0.1"

    if not port_is_free(host, port):
        print(f"ERROR: port {port} is already in use -- another Wings Theory "
              "server (dev or prod?) is probably still running.")
        print(f"Find it with:  Get-NetTCPConnection -LocalPort {port} -State Listen")
        sys.exit(1)

    print(f"Wings Theory -- starting backend ({'development' if is_dev else 'production'})...")
    print(f"Database: {os.environ['WT_DB']}")
    if is_dev:
        print("(separate from the production database; safe to seed, reset, or delete)")

    shown_ip = get_lan_ip() if host == "0.0.0.0" else "127.0.0.1"
    show_urls(shown_ip, port)
    if host == "0.0.0.0":
        print("(tablets must be on the same Wi-Fi / hotspot as this PC)")
        threading.Thread(target=watch_ip, args=(shown_ip, port), daemon=True).start()
    print()

    try:
        if is_dev:
            # reload=True needs the import string; env vars are inherited by
            # the reloader's child process, so configure() need not rerun.
            uvicorn.run("wt.api:app", host=host, port=port, reload=True)
        else:
            uvicorn.run("wt.api:app", host=host, port=port, proxy_headers=True)
    except Exception as exc:
        print("\nServer stopped with an error:")
        print(exc)
        if sys.stdin.isatty():
            input("Press Enter to close this window...")
        raise


if __name__ == "__main__":
    main()
