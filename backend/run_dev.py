"""Wings Theory — development server launcher (Python version of start_server_dev.ps1).

Same behavior as the PowerShell launcher, but works on Windows, macOS and Linux:
  * WT_DB points at backend/dev/wings_theory_dev.db (never the production file)
  * WT_ENV=development
  * WT_SECRET is generated once and stored in backend/dev/.secret (gitignored)
  * Runs the FastAPI app with uvicorn on 127.0.0.1:8000 with auto-reload

Run:  python run_dev.py      (from the backend folder, or anywhere)
"""

import os
import secrets
import sys
from pathlib import Path

import uvicorn

# Equivalent of: Set-Location $PSScriptRoot
BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))  # so "wt.api" can be imported

# Equivalent of: New-Item dev folder if missing
DEV_DIR = BASE_DIR / "dev"
DEV_DIR.mkdir(exist_ok=True)

os.environ["WT_DB"] = str(DEV_DIR / "wings_theory_dev.db")
os.environ["WT_ENV"] = "development"

# Per-machine secret, generated once and reused so logins survive restarts.
secret_path = DEV_DIR / ".secret"
if not secret_path.exists():
    # 48 random bytes -> url-safe text (same strength as the .ps1 version)
    secret_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    print(f"Generated a new dev signing secret at {secret_path}")
os.environ["WT_SECRET"] = secret_path.read_text(encoding="utf-8").strip()

print("Wings Theory -- starting backend (development)...")
print(f"Database: {os.environ['WT_DB']}")
print("(separate from the production database; safe to seed, reset, or delete)")
print()

if __name__ == "__main__":
    # reload=True requires the app as an import string, not an object.
    uvicorn.run("wt.api:app", host="127.0.0.1", port=8000, reload=True)
