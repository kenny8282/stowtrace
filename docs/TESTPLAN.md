# TESTPLAN — run after every deploy

Two stages. Stage A is automated and gates the deploy; Stage B is a manual
click-through of whatever the release touched plus the always-run core.

## Stage A — automated suite (on the VM, before trusting a deploy)

    ssh stowtrace@10.19.27.237
    cd /tmp && PYTHONPATH=/opt/stowtrace/src/backend /opt/stowtrace/venv/bin/python3 test_backend.py | tail -3

(One-liner from PowerShell:
    ssh -t stowtrace@10.19.27.237 "cd /tmp && PYTHONPATH=/opt/stowtrace/src/backend /opt/stowtrace/venv/bin/python3 test_backend.py | tail -3"
The PYTHONPATH points the suite at the DEPLOYED backend, so Stage A tests
exactly what the service is running.)

PASS = "=== FINAL N passed, 0 failed ===". Any failure: stop, report output.
The suite runs against its own temp data dir — it NEVER touches live data.

## Stage B — core manual pass (every release, ~3 minutes)

Footer: `ui vX.Y.Z · api vX.Y.Z` and versions match (red text = stale page).

1. SEARCH: type "servo" → dropdown has thumbnails + locations.
   Enter → full results page → tap a result → detail opens → ✕ Close restores scanner.
2. ITEM: open any item from a bin → detail card complete (photo, qty, buy/search,
   category). ✎ Edit → change notes → Save → change persists after refresh.
3. PHOTO: on one item — ✂ Adjust Crop → drag/zoom → Save → thumbnail updates.
4. CATEGORIES: drill to a leaf → filter bar dropdowns filter, sort control sorts.
5. BACKUP: Options → Download Backup → file downloads, JSON opens, has st:photos.

## Stage B extensions — v2.4.0 specific

6. LOCATIONS: Locations tab → + Add Location "Store" → drill in → add "East Wall".
   Edit a bin → Store Location → Pick… → drill → Use → bin detail shows the path.
   Locations tab → East Wall → the bin is listed.
7. HOME MODE (before enabling business): no login anywhere, all edits work,
   qty decreases need no reason. ⚙ tab shows "Enable Business Mode" form.
8. BUSINESS MODE (staging only — this is one-way in the UI):
   ⚙ → owner username + PIN → Enable. Page now gates:
   a. Open an incognito window → login overlay appears → "Continue as guest"
      → browsing + search work, +Add New Item blocked with a toast.
   b. Log in as owner → create an employee user in ⚙.
   c. In incognito, sign in as the employee → edit an item's quantity DOWN
      → reason prompt appears → cancel = no save; provide reason = saves.
   d. Owner ⚙ tab → Adjustment Report lists the employee; Recent Activity
      shows qty_adjust with the reason text.
   e. Employee tries a delete (edit modal) → blocked 403 toast.
9. RESET FOR RE-TEST: business mode is enabled by the presence of users.
   To return staging to home mode: on the VM
       sqlite3 /var/lib/stowtrace/inventory.db "DELETE FROM users; DELETE FROM sessions;"
   (sqlite3 CLI: sudo apt install -y sqlite3. Audit log intentionally survives.)

## When anything fails
Note: exact step, expected vs got, and paste
    sudo journalctl -u stowtrace-backend -n 30 --no-pager
