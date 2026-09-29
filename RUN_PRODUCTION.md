# Running Wings Theory in Production

This is the exact, current procedure for starting the live Wings Theory system. It assumes the project folder already contains the production database (`backend/wings_theory.db`) and signing secret (`backend/.secret`) — this guide does not create, seed, or reset either.

## 1. Prerequisites

- Windows PC with **Python 3.12** installed and on PATH (or the existing virtual environment at `backend\venv`).
- The Wings Theory project folder present at `C:\Users\PLPASIG\Desktop\wings-theory` — this is the only checkout that should ever be run. (`/health` reports `project_root`; if you have more than one copy of this repo on the machine, always confirm you're launching from this one.)
- `backend\wings_theory.db` and `backend\.secret` already exist in this folder (they are the live production database and signing key — never delete or regenerate them casually).
- Python dependencies installed into the venv from `backend\requirements.txt` (already done if `backend\venv` is present and untouched).
- All devices that need access (tablets, other PCs) are on the **same Wi-Fi/hotspot** as this PC.

## 2. Start Production

Open **PowerShell** and run:

```powershell
cd C:\Users\PLPASIG\Desktop\wings-theory\backend
```
Moves into the backend folder — every command below assumes this location.

```powershell
git log -1 --oneline
```
Shows the currently checked-out commit, so you can confirm this is the version you intend to run.

```powershell
.\venv\Scripts\Activate.ps1
```
Activates the project's Python virtual environment (so `python` resolves to the venv's interpreter with the correct dependency versions installed). If PowerShell blocks the script with an execution-policy error, run PowerShell as Administrator once and execute `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then retry.

```powershell
.\start_server.ps1
```
This is the production launcher. It:
- Sets `WT_ENV=production` and `WT_DB` to the full path of `backend\wings_theory.db` (the real production database — no dev/demo database is ever used by this script).
- Loads the persistent signing secret from `backend\.secret` (generating it only if it doesn't already exist).
- Starts the backend with `python -m uvicorn wt.api:app --host 0.0.0.0 --port 8000` — bound to all network interfaces on **port 8000**, intentionally, so tablets on the shop network can reach it.
- Serves the frontend (POS/KDS/console) directly from `backend\static\` — there is no separate frontend build or server to start.
- Prints the console/POS/kitchen-display URLs using the PC's current LAN IP, and keeps watching for the IP changing (e.g. hotspot reassignment), reprinting the new address if it does.

Leave this PowerShell window open — the server runs in it and stops if it's closed.

## 3. Verify It Is Actually Running

On the server PC (or any device on the same network), open:

```
http://localhost:8000/health
```

This is an unauthenticated status endpoint. Confirm in the JSON response:

- `"environment": "production"` — confirms it's not running as `development`/`test`.
- `"database_path"` ends in `wings_theory.db` — confirms it's the production database, not `dev\wings_theory_dev.db`.
- `"schema_up_to_date": true` — confirms all migrations are applied.
- `"integrity_check": "ok"` and `"foreign_key_violations": 0` — confirms the database itself is healthy.
- Row counts under `ingredient`, `customer_order`, `stock_ledger`, `fefo_batch` are non-zero and look like real data (not empty/demo-sized).
- `"git_commit"` matches the commit you confirmed in Step 2.

Then confirm the frontend and login:

- Open `http://localhost:8000/console` (or `http://<lan-ip>:8000/console`) — the Wings Theory owner/admin console should load.
- Log in with a valid owner/admin account. A successful login plus the console dashboard rendering (not an error page) confirms the API and database are both working end-to-end.
- If login instead returns `password_change_required`, that account still has its seeded default password and must be changed via the console before proceeding — this is expected security behavior, not a bug.

## 4. Test the Critical System Flow

Using the URLs printed by `start_server.ps1`:

1. **Login** — log into `/pos` (or `/console`) with a cashier/owner account.
2. **POS** — open `/pos`, start a new order, add an item.
3. **Order** — submit the order.
4. **Kitchen** — open `/kds` (kitchen display) on the same or another device and confirm the order appears.
5. **Inventory deduction** — back in `/console`, check the affected ingredient's stock level and confirm it decreased by the expected recipe amount.
6. **Database** — re-check `/health` and confirm the `stock_ledger` row count increased by at least one (a ledger entry was written for the deduction).

Also open `/console` as an owner/admin account specifically and confirm the full owner/admin dashboard (not a restricted cashier/kitchen view) loads correctly.

## 5. LAN Access

On the server PC, find its LAN IPv4 address (also printed automatically by `start_server.ps1`):

```powershell
ipconfig
```
Look for the `IPv4 Address` under the network adapter currently connected to the shop Wi-Fi/hotspot (e.g. `192.168.1.23`).

From any other device on the **same network**, open a browser to:

```
http://<lan-ip>:8000/pos
http://<lan-ip>:8000/kds
http://<lan-ip>:8000/console
```

If another device can't connect, allow the port through Windows Firewall on the server PC (run PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "Wings Theory (8000)" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```
Creates a one-time inbound rule permitting TCP traffic on port 8000 from the local network.

## 6. Stop / Restart

**Stop**: Click into the PowerShell window running `start_server.ps1` and press `Ctrl+C`, or simply close the window (the script terminates the `uvicorn` process on exit).

**Restart**: Close the running window if still open, then repeat Step 2 (`cd` into `backend`, activate the venv, run `.\start_server.ps1` again). The same database and secret are reused automatically — no data is lost, and all existing logins remain valid.

## 7. If It Does Not Start

- **"python" not recognized / wrong version** — confirm the venv is activated: `.\venv\Scripts\Activate.ps1`, then check `python --version` (should be 3.12.x).
- **Refuses to start citing `WT_ENV`/`WT_DB`/`WT_SECRET`** — don't set these manually; always launch via `start_server.ps1`, which sets them correctly. If you see this error, something else on the machine (another terminal, a stray `$env:` from a previous session) may have overridden them — open a fresh PowerShell window and retry.
- **Refuses to start citing database integrity or a default secret** — do not attempt to fix this by regenerating `backend\.secret` or replacing `backend\wings_theory.db`; that is a real problem with the production data/secret and should be investigated (check `backend\docs\BACKUP_RESTORE.md`) before proceeding, not worked around.
- **Port 8000 already in use** — another instance may already be running (possibly a second checkout — check `/health`'s `project_root` and `pid` fields). Find and stop it:
  ```powershell
  Get-NetTCPConnection -LocalPort 8000 | Select-Object OwningProcess
  Stop-Process -Id <pid> -Force
  ```
- **Other devices can't reach it, but `/health` works on the server itself** — check Windows Firewall (Step 5) and confirm all devices are on the same Wi-Fi/hotspot network.
- **LAN address stopped working after a while** — `start_server.ps1` reprints the new address in yellow when the network IP changes (common with phone hotspots); check the still-open PowerShell window for the updated URL.
