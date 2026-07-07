# StowTrace Changelog

All notable changes to StowTrace (and its Label Forge module) are documented here.
Versions follow the app's `APP_VERSION` string.

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
