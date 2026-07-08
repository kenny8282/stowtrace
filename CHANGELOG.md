# StowTrace Changelog

All notable changes to StowTrace (and its Label Forge module) are documented here.
Versions follow the app's `APP_VERSION` string.

## 2.9.14 — Fix inaccessible items + printer panel disappearing

- **All items are reachable again.** Items whose category pointed at a category
  that no longer exists (orphaned by the earlier restore/dedup bug) are now
  re-filed into "Uncategorized" automatically on startup, so nothing is stranded
  and unbrowsable. Combined with the duplicate-category healing, every item is
  reachable through the category tree. Verified against the full 4,056-item catalog.
- Added a manual repair action (POST /api/categories/repair) that runs the same
  dedup + orphan fix on demand and reports what it fixed.
- **Printer selection panel no longer flashes then disappears.** The printer
  status refresh now waits for the printer list/options panel to populate before
  rendering status, fixing the race that made the selector vanish after load.

## 2.9.13 — Seamless in-place updates (no manual refresh)

Changes now reflect immediately without needing a manual page refresh, and without
a jarring full-screen reload.

- Inventory: after any item change, the tab you're currently viewing (Categories,
  Locations) re-renders in place with the fresh data — item counts and lists update
  live while you browse, keeping your drill-down position.
- Label Forge: changing the Zebra label size updates existing rows and the preview
  instantly. Dropdowns and checkboxes now update the preview immediately instead of
  after a short delay; text typing still debounces so it doesn't render on every
  keystroke.
- The Admin panel won't rebuild while you're typing in it (preserves focus/input).

## 2.9.12 — Restore merges instead of duplicating + forge tape-width panel

**Restore fixes (important):**
- Restoring a backup no longer creates duplicate categories. Categories are now
  merged by their name-path rather than their random internal id, so a category
  that already exists is reused instead of copied. Restored items are remapped to
  the existing category.
- Items merge instead of overwriting: an item already present is kept (current
  wins), and missing fields like category are backfilled from the backup. Items
  not present are added. Re-running a restore is now idempotent — it adds nothing
  the second time and never duplicates.
- New healing step on startup de-duplicates any categories that were already
  doubled by the earlier restore bug, repointing items to the surviving category.
  Runs automatically; no-op once the tree is clean.

**Label Forge:**
- The Brother tape-width control moved into a "Brother Tape" panel below the
  header (above Label Format), matching where the Zebra label-size selector sits —
  no more cramped controls next to the printer name.
- The selected printer persists across page changes and reloads (saved per box).

## 2.9.11 — Label Forge adapts to the selected printer

Label Forge now reshapes itself to match whichever printer is selected, fixing the
issues where a Zebra still showed Brother tape controls.

- The Brother "Preview as" tape-width strip is hidden when a die-cut printer
  (Zebra) is selected — it only applies to tape.
- A **label-size selector** appears for the Zebra; picking a size sets the label's
  fixed dimensions and updates the row defaults automatically.
- The **Label Format** section adapts: for the Zebra, Width/Height show the chosen
  die-cut label size (read-only, since it's fixed) and the Tape Color field is
  hidden (direct thermal has no tape color). For the Brother it's unchanged —
  editable width (tape) with length from content.
- New rows added in Zebra mode default to the selected label size.
- The preview and the print bar are now mode-aware: no more "none match the 24mm
  tape" message when a Zebra is selected — it matches against the die-cut label
  size instead, and the print button reads "Print Selected Labels."

Still pending on-hardware verification with the actual ZD411 + label stock.

## 2.9.10 — Universal printer system + Zebra ZD411 support (in progress)

Adds a driver-based printer architecture so StowTrace can print to more than just
the Brother P-Touch. Each printer is a driver with a common interface (detect,
capabilities, status, print), registered centrally.

- **Printer picker** in Label Forge: a dropdown lists every known printer with a
  connected indicator (green/red), even when unplugged, so you can design and
  preview without printing. Selecting one persists per box.
- **Capabilities-driven UI**: the selected printer declares which options are
  relevant, so Brother shows tape width/color while Zebra shows a die-cut label
  size preset — no more irrelevant tape options when a Zebra is selected.
- **Zebra ZD411 driver**: prints via CUPS (renders label to PNG, sends with `lp`).
  Detects the Zebra on USB and whether its CUPS queue is ready. Standard
  2.25"-wide direct-thermal presets included (2.25×1.25 POS tag default), 203/300
  DPI aware.
- **install.sh** installs CUPS and auto-creates a `zebra_zd411` queue when a Zebra
  is detected on USB. The Brother path is unchanged.
- The 8.5×11 Printer sheet page is untouched.

Note: the Zebra path needs on-hardware testing with the actual printer and label
stock to finalize (queue/PPD specifics, print alignment). The Brother path and all
existing behavior are backward-compatible and fully working.

## 2.9.9 — Backup & restore available in Home Mode

Backup and restore (Back up now, Restore from File, Restore from USB, Download
Backup) are now available in the Admin tab while in Home Mode, without having to
enable Business Mode. The backend already allowed these in Home Mode — the UI just
wasn't showing them. In Business Mode they remain owner-only, as before.

## 2.9.8 — Allow large backup restores

Raised the nginx upload limit from 50 MB to 2 GB (generous headroom). Full backups that include item
photos run ~150 MB, so restoring one previously failed at the web-server layer
(413 Request Too Large) before reaching the app — the restore appeared to "fail"
right after reading the file. update.sh re-applies the nginx config, so this takes
effect on the next deploy.

## 2.9.7 — Self-healing category/location rebuild + Back button

Two fixes.

**Category tree wasn't rebuilding on the appliance.** The rebuild was gated purely
on a "done" marker, so if it had ever been marked complete without actually
building the tree (e.g. fired before the catalog was present), it would never run
again — leaving the box with almost no categories and no "Uncategorized" bucket.
The category and Unlocated-location migrations are now self-healing: they re-run
whenever the expected structure (the grouped tree with "Uncategorized", or the
"Unlocated" location) isn't actually present, regardless of the marker. Verified
against the full catalog: rebuilds to 62 nodes, all 4,056 items reachable,
"Uncategorized" holds the 27 stragglers.

**Back button on the item detail.** Opening an item from the category tree, the
location tree, or a search now shows a "← Back" button that returns to exactly
where you were — the same tree position, or the search results with your query
intact — instead of leaving the browser back button to switch the whole page.

## 2.9.6 — Show items filed directly in a parent category

Fixes items appearing "missing" from the Categories tab. Previously a category
only showed its items when it had no sub-categories (a leaf). Items filed directly
in a parent category — most notably ~500 items in "Wheels & Tires" — were hidden
behind its sub-category list. Now a parent category shows its sub-categories AND
any items filed directly in it, so every item is reachable by browsing. All 4,056
items are now accounted for across the tree (including the "Uncategorized" bucket).

## 2.9.5 — Scan-to-file, Uncategorized bucket, Unlocated location

- **Move/File now opens the QR scanner first** for fast location filing: scan a
  location's QR to file an item instantly, with a manual "type location ID" field
  and a "Browse locations…" button for the full hierarchy below the scanner.
- **Uncategorized** — the catch-all category is now named "Uncategorized" (was
  "Miscellaneous"), so anything not properly categorized has a clear home to sort
  from later.
- **Unlocated location** — a new top-level location holds every item until it's
  physically placed. The store root ("AZ Turn and Burn") is now an empty container
  to build real sub-locations under; items move out of "Unlocated" into their real
  spots as the store is organized. All items and photos preserved.

## 2.9.4 — Grouped category tree with smart routing

Replaces the flat category rebuild (2.9.3) with a curated GROUPED tree, closer to
the original organization but far more complete. Similar items are grouped under
logical parents, and the miscellaneous pile is drained by reading item
descriptions. Verified against the full 4,056-item catalog.

- Top-level groups: Vehicles, Electronics, Bodies/Paint & Accessories, Wheels &
  Tires, Parts & Hop-Ups, Hardware & Fasteners, Tools & Equipment, Oils/Grease &
  Fluids, Toys & Collectables, Apparel & Merch — each with detailed sub-categories.
- Smart routing places items by their catalog category, then falls back to
  description keywords for anything that was "Unclassified" — cutting the
  miscellaneous pile from ~800 items to 27.
- All items re-mapped, all photos preserved. Categories remain fully editable for
  further hand-tuning.

## 2.9.3 — Category tree rebuilt from real catalog data

The seeded catalog had two disagreeing category systems: detailed strings scraped
from the shop (e.g. "Toys & Collectables - HotWheels - Matchbox") and a coarse
15-node tree that force-fit items into wrong buckets. This rebuilds a clean, flat
category tree from the real strings and re-maps every item. Verified against the
full 4,056-item catalog — all items re-mapped, all photos preserved.

- ~43 natural top-level categories (Parts, Wheels & Tires, Electronics, Batteries
  & Chargers, Motors & ESCs, Servos, Toys & Collectables, RTRs & Kits, and so on)
  with clean sub-categories where the data supported them.
- Merged obvious corruption/duplicate variants (e.g. "RTRs&"/"RTRsKits" → "RTRs &
  Kits"; the three "RC Bodies…" variants into one) and dropped scale-fragment
  artifacts from the scrape.
- Item detail now shows a single Category (the tree path), not two conflicting
  rows.

The tree is a clean starting point for the shop to reorganize further — categories
remain fully editable. Migration runs automatically once on upgrade and is
idempotent.

## 2.9.2 — Single category input on item edit

- Removed the redundant free-text Category box on the item edit form. Items now
  use only the category picker, which links to the real category tree (with
  attribute filters); the category name is derived automatically from your pick.
  Previously both a loose text box and the picker existed, which was confusing and
  could disagree with each other.

## 2.9.1 — Menu styling cleanup

- Removed the stray blue box that appeared over the item count and chevron on
  category (and location) rows — it came from a leaf-category edge indicator that
  rendered as a solid block. Tree-menu rows now have cleaner spacing, rounded
  corners, and a subtle hover. Applies to both the Categories and Locations tabs.

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
