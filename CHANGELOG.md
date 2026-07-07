# StowTrace Changelog

All notable changes to StowTrace (and its Label Forge module) are documented here.
Versions follow the app's `APP_VERSION` string.

## 2.9.0 — Location restructure: SKU items, one hierarchical location tree

A significant restructure of how items and locations work. **Your existing AZ
Turn and Burn catalog is fully preserved** — this was verified against a real
4,056-item / 3,823-photo backup before release.

**Items are identified by SKU / part number, not QR codes**
- Individual items no longer carry scannable QR codes. Item detail shows the SKU;
  the internal ID is no longer surfaced as a QR. All item data (SKU, category,
  attributes, photos, quantity, price, notes) is kept exactly as-is.

**One custom location hierarchy (bins + containers collapsed into "locations")**
- The separate Bins tab is gone. Everything physical — a shelf, a wall spot, a
  hanging peg, a display case, a bin, a section, any name — is now just a
  "location" in a single tree you build as needed.
- Locations nest to any depth: e.g. AZ Turn and Burn → East Wall → Shelf 3.
- The existing store root was renamed to "AZ Turn and Burn" (editable), and all
  imported items sit under it, ready to be organized into sub-locations.

**Only locations get QR labels**
- Creating a location queues its label into Label Forge to be printed. The QR on a
  location is what gets scanned. (Items go by SKU.)

**Filing items**
- Item detail has a Move / File button that opens a navigable location picker, so
  you can file an item anywhere in the hierarchy. The Unassigned tab uses the same
  picker.

**Migration** runs automatically and safely on first startup after upgrade, and is
idempotent. Delete actions on locations remain manager+.

## 2.8.0 — Auto-backup scheduler + delete gating

- **Scheduled auto-backup now runs.** A systemd timer polls hourly; when the
  owner's configured interval has elapsed and the USB drive is present, it writes
  a backup automatically and prunes per the retention setting. (The setting
  existed before; now there's an engine behind it.)
- **Delete actions gated to manager+** in the UI: item delete (edit modal and
  stuck-record), category delete, and location delete are hidden or blocked for
  employees, matching the permission tiers. The backend already enforced this;
  now the buttons match.

## 2.7.3 — Login wall fixes

- **The app is now properly blocked until sign-in** in Business Mode. Previously
  the inventory could be reached without logging in (as a leftover "guest"); the
  login overlay is now reasserted on every navigation and guest browsing is gone.
- **No more manual refresh after login** — signing in re-renders the current view
  immediately so you land on the app, not a stale page.

## 2.7.2 — Professional login screen + Forge cleanup

- **Redesigned sign-in screen**: branded StowTrace header, larger well-spaced
  username and PIN fields, and a clear inline error ("Incorrect username or PIN")
  instead of an easy-to-miss toast. First-sign-in PIN guidance shows inline too.
- **Backup & Restore removed from Label Forge** — it now lives solely in the
  Inventory Admin panel, so there's one clear home for it.

## 2.7.1 — Backup/restore in inventory Admin + USB restore fix

- **Full backup & restore now lives in the inventory Admin (⚙) panel**: drive
  status, retention, auto-backup interval, Back up now, Restore from File,
  Restore from USB (backup picker), and Download Backup — all in one place.
- Fixed a "Restore from USB" error on the Label Forge page (missing escape helper).

## 2.7.0 — Restore from USB drive

- **Restore from USB Drive** button (Label Forge → Backup & Restore): lists the
  timestamped backups on the drive with date/size, so you can pick the exact one
  to restore instead of hunting through the file picker. Restore stays additive
  (only missing items are added; current data is never overwritten).
- Backup-drive status now reflects the marker check and backup count; the restore
  button enables only when the drive holds at least one backup.

## 2.6.1 — Login UX + exit Business Mode

- **Typed username at login** (was a dropdown) so browsers can save and autofill
  credentials. First-login "choose a PIN" hint now appears on submit.
- **Exit Business Mode** (owner only, under Admin → Advanced): returns to Home Mode
  and removes all user accounts. Inventory data is untouched. Gives the owner a
  clean way back for setup/dev or re-provisioning.
- **Backup settings auto-save** on change (no Save button); inline ✓ feedback.

## 2.6.0 — USB backups, founder protection, login-screen backup

**USB backup system**
- Backups write to a registered USB drive that carries a `STOWTRACE_BACKUP.txt`
  marker file, into a `backups/` folder, so the device never writes to a random
  stick. Files are timestamped.
- Retention: keep the last N backups (default 10); older ones are pruned.
- Owner "Backups" panel: drive status, retention setting, auto-backup interval
  (every N hours; 0 = off), and a manual "Back up now to USB" button.

**Login-screen emergency backup**
- A small "Back up now to USB" link on the sign-in screen writes a backup with no
  credentials, so a locked-out customer can protect their data before getting help.
  It only writes to the physically-present USB drive and exposes no data over the
  network. Shows success (with filename) or a clear reason if it can't.

**Founder (original owner) protection**
- The first owner account created when Business Mode is enabled is marked as the
  protected founder. No other admin can demote, deactivate, delete, or reset it —
  only device recovery (SSH) can. Shown with a FOUNDER badge and locked controls.

**Full login wall**
- Business Mode now requires sign-in for the whole app (the read-only guest bypass
  was removed).

## 2.5.0 — Business Mode user management (Stage 1)

Adds a full role-based user system for shipped store boxes. Defaults to Home
Mode (no login) for development; Business Mode is opt-in from the admin tab.

**Permission tiers**
- **Employee:** day-to-day inventory — add/edit items, adjust quantity, move
  locations, and *add* categories/locations. Cannot delete or touch system settings.
- **Manager:** everything an employee can do, plus *delete* (items, categories,
  locations), AP/WiFi control, and backup/restore.
- **Owner:** everything a manager can do, plus user management and the audit log.

**User management (owner only)**
- Create a user with a username + role only — the user chooses their own PIN at
  first login (PINs are hashed and never viewable by anyone).
- Reset a user's PIN (they re-pick at next login).
- Delete a user.
- Change a user's role instantly from a dropdown (no save button).
- The last remaining owner cannot be demoted, deactivated, or deleted.

**First-login PIN**
- Owner-created accounts have no PIN until the user's first sign-in; the PIN they
  enter then becomes their PIN. The sign-in screen prompts accordingly.

## 2.4.1 — Update-check fix + quantity stepper

- Fixed the in-app update checker to read the backend version from the new
  `app/backend/` path (was looking in the old flat layout).
- Inline quantity stepper on the item detail page: −/+ buttons and a directly
  editable number box, so staff can adjust quantity without opening edit details.
  The Search/Buy button moved to its own row. In Business Mode, reducing quantity
  prompts for a reason (audit requirement); Home Mode stays frictionless.

## 2.4.0 — AP config, captive portal, self-update

- **Kiosk access point:** the box broadcasts its own WiFi (default SSID/password
  `stowtrace`/`stowtrace`) so staff reach the app on any store network without
  needing the store's WiFi password. Joining the AP auto-opens the app
  (captive-portal redirect to the home page).
- **In-app AP config** (owner/manager): change the hotspot SSID and password from
  the WiFi page; the device reboots to apply.
- **Self-update:** the app can check for and apply updates from the GitHub repo
  without SSH — pushed commits are detected and pulled from within the app.
- Friendly hostname `st.local` for reaching the box over ethernet.

## Earlier

StowTrace began as a rebrand of the earlier "Gridfinity / Label Forge" inventory
system: a Raspberry Pi appliance running a Flask/SQLite backend (WAL + FTS5),
photo/thumbnail support, a hierarchical category tree, location tracking, Shopify
catalog import, label printing via a Brother P-touch, and an audit trail. The
rebrand moved everything to the `stowtrace` namespace and a clean
`app/deploy/scripts/docs` repo layout.
