"""
StowTrace Label Forge — Shared Backend
========================================
Tiny Flask app that stores label data on the Pi's disk so all devices on
the network see the same registry, plus prints labels to a Brother
P-Touch tape printer via ptouch-print.

API endpoints:
  GET    /api/health          → { ok: true, version: "..." }
  GET    /api/kv/<key>        → { value: "..." }   or 404
  PUT    /api/kv/<key>        → body { value: "..." }
  GET    /api/registry-hash   → { hash: "abc123..." }  (for sync polling)
  GET    /api/export          → full v1 inventory JSON
  POST   /api/import          → merge a v1 inventory JSON (atomic)
  POST   /api/reset           → wipe everything (use with care)
  GET    /api/printer/status  → { connected, model, tape_width_mm, error }
  POST   /api/print           → render+print a label (body: see below)

Data is stored as JSON files in DATA_DIR. Atomic writes via tempfile+rename.
A file lock (fcntl) prevents concurrent writes from corrupting state.
"""

import json
import os
import subprocess
import hashlib
import tempfile
import fcntl
import io
import base64
from pathlib import Path
from flask import Flask, request, jsonify, abort

APP_VERSION = "2.4.1"  # Phases 1-3.5: server search, locations tree, auth+roles+audit (home mode default), owner reports

# Where data lives. Change with env var if you want a different path.
DATA_DIR = Path(os.environ.get("ST_DATA_DIR", "/var/lib/stowtrace"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Keys the frontend uses. Any other key would be rejected.
ALLOWED_KEYS = {
    "st:used",
    "st:registry",
    "st:rows",            # legacy (pre-v1.12) — kept readable for the one-time migration archive
    "st:rows_archive",    # v1.12 archive of pre-restructure rows (so users can recover if needed)
    "st:maker_rows",      # v1.12+: Label Maker tab rows
    "st:printer_rows",    # v1.12+: Printer tab rows
    "st:config",
    "st:tape_presets",    # user-saved {id,name,desc,w,h} for Label Maker tab
    "st:sheet_presets",   # user-saved {id,name,desc,w,h} for Printer tab
    "st:categories",      # v1.19+: hierarchical category tree (Slice 8)
}

app = Flask(__name__)

# ------------------------------------------------------------------
# File helpers — atomic write + lock
# ------------------------------------------------------------------

def _safe_path(key: str) -> Path:
    # Replace : with _ for filesystem safety
    safe = key.replace(":", "_").replace("/", "_")
    return DATA_DIR / f"{safe}.json"

def _read_file(key: str):
    p = _safe_path(key)
    if not p.exists():
        return None
    try:
        with open(p, "r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return f.read()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        app.logger.warning(f"read failed for {key}: {e}")
        return None

def _write_file(key: str, value: str):
    p = _safe_path(key)
    # Atomic write: write to tempfile, then rename
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        dir=str(DATA_DIR),
        prefix=p.stem + ".",
        suffix=".tmp",
    )
    try:
        fcntl.flock(tmp.fileno(), fcntl.LOCK_EX)
        tmp.write(value)
        tmp.flush()
        os.fsync(tmp.fileno())
        fcntl.flock(tmp.fileno(), fcntl.LOCK_UN)
        tmp.close()
        os.replace(tmp.name, p)
    except Exception:
        try:
            tmp.close()
            os.unlink(tmp.name)
        except Exception:
            pass
        raise

# ==================================================================
# Phase 0 (v2.0.0): SQLite storage engine
# ==================================================================
# The three keys that grow with inventory size (registry, used, categories)
# now live in SQLite instead of monolithic JSON files. Everything else
# (config, presets, working rows) stays as small JSON files. The legacy
# _read_file/_write_file API is preserved via dispatchers below, so every
# endpoint that speaks "whole-blob JSON" (forge KV, backup, export/import,
# categories) keeps working unchanged — it just reconstructs from / replaces
# into SQLite under the hood. Hot item endpoints use direct SQL.
#
# Photos move out of item records into an item_photos table (original +
# generated thumbnail as JPEG blobs). Item records carry has_photo; the API
# hands out /api/items/<id>/photo and /thumb URLs instead of base64.
#
# An FTS5 index (items_fts) is maintained on every write — unused by the UI
# yet, but it is the foundation for the Phase 1 keyword search.

import sqlite3
import threading

DB_PATH = DATA_DIR / "inventory.db"
_SQLITE_KEYS = {"st:registry", "st:used", "st:categories"}
_db_local = threading.local()

THUMB_SIZE = 96          # square thumbnail edge (px)
THUMB_QUALITY = 70       # JPEG quality for thumbnails


def _db():
    conn = getattr(_db_local, "conn", None)
    if conn is None:
        # isolation_level=None → autocommit mode; we manage transactions
        # explicitly with BEGIN/COMMIT where multi-statement atomicity matters.
        conn = sqlite3.connect(str(DB_PATH), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _db_local.conn = conn
    return conn


def _db_init():
    d = _db()
    d.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id          TEXT PRIMARY KEY,
            type        TEXT,
            bin_id      TEXT,
            category_id TEXT,
            created     TEXT,
            updated     TEXT,
            data        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_items_bin  ON items(bin_id);
        CREATE INDEX IF NOT EXISTS idx_items_cat  ON items(category_id);
        CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);

        CREATE TABLE IF NOT EXISTS item_photos (
            id      TEXT PRIMARY KEY,
            photo   BLOB NOT NULL,
            thumb   BLOB,
            updated TEXT
        );

        CREATE TABLE IF NOT EXISTS used_ids (
            id TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS categories (
            id   TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
            item_id UNINDEXED,
            body
        );
    """)
    d.commit()


def _bump_change(cur=None):
    """Monotonic change counter — cheap change detection for registry-hash."""
    d = _db()
    d.execute("""INSERT INTO meta(key, value) VALUES('change_counter', '1')
                 ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1""")


def _change_counter():
    row = _db().execute("SELECT value FROM meta WHERE key='change_counter'").fetchone()
    return row["value"] if row else "0"


def _fts_text(rec):
    """Flatten every searchable field of a record into one indexable string.
    This is the foundation of Phase 1 keyword search — any value a user
    typed anywhere on the item becomes findable."""
    parts = [str(rec.get("id", ""))]
    for k in ("description", "supplier", "supplier_sku", "brand", "model",
              "notes", "category", "location", "location_property",
              "location_room", "location_spot", "buy_url"):
        v = rec.get(k)
        if v:
            parts.append(str(v))
    for ln in rec.get("lines") or []:
        if ln:
            parts.append(str(ln))
    for si in rec.get("sub_items") or []:
        if isinstance(si, dict) and si.get("name"):
            parts.append(str(si["name"]))
    attrs = rec.get("attributes")
    if isinstance(attrs, dict):
        parts.extend(str(v) for v in attrs.values() if v not in (None, ""))
    return " ".join(parts)


def _item_write(rec):
    """Upsert one full record (photo already stripped) + refresh its FTS row.
    Caller is responsible for commit/_bump_change."""
    d = _db()
    d.execute(
        """INSERT INTO items(id, type, bin_id, category_id, created, updated, data)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             type=excluded.type, bin_id=excluded.bin_id,
             category_id=excluded.category_id, created=excluded.created,
             updated=excluded.updated, data=excluded.data""",
        (rec["id"], rec.get("type"), rec.get("bin_id"), rec.get("category_id"),
         rec.get("created"), rec.get("updated"), json.dumps(rec)))
    d.execute("DELETE FROM items_fts WHERE item_id=?", (rec["id"],))
    d.execute("INSERT INTO items_fts(item_id, body) VALUES(?,?)",
              (rec["id"], _fts_text(rec)))


def _item_get(item_id):
    row = _db().execute("SELECT data FROM items WHERE id=?", (item_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def _photo_store(item_id, data_url):
    """Decode a data: URL, generate a thumbnail, store both as JPEG blobs.
    Returns True on success. Degrades gracefully if Pillow is unavailable."""
    try:
        header, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        return False
    thumb_bytes = None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY)
        thumb_bytes = buf.getvalue()
    except Exception as e:
        app.logger.warning(f"thumbnail generation failed for {item_id}: {e}")
        thumb_bytes = raw  # fall back: thumb = original
    _db().execute(
        """INSERT INTO item_photos(id, photo, thumb, updated) VALUES(?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET photo=excluded.photo,
             thumb=excluded.thumb, updated=excluded.updated""",
        (item_id, raw, thumb_bytes, _now_iso()))
    return True


def _photo_delete(item_id):
    _db().execute("DELETE FROM item_photos WHERE id=?", (item_id,))


def _apply_photo_field(item_id, rec, photo_val):
    """Interprets the legacy 'photo' field on an incoming record:
       data: URL  -> store as blob, mark has_photo
       None / '' -> delete blob, clear has_photo
       any other string (e.g. a /api/... URL echoed back) -> no-op."""
    if photo_val is None or photo_val == "":
        _photo_delete(item_id)
        rec["has_photo"] = False
    elif isinstance(photo_val, str) and photo_val.startswith("data:"):
        if _photo_store(item_id, photo_val):
            rec["has_photo"] = True
    # else: URL echo or junk — leave photo state untouched
    return rec


def _decorate_photo_urls(rec):
    """Add photo/thumb URLs to an outgoing record when a photo exists."""
    if rec.get("has_photo"):
        v = (rec.get("updated") or "").replace(":", "").replace("+", "")
        rec = dict(rec)
        rec["photo"] = f"/api/items/{rec['id']}/photo?v={v}"
        rec["thumb"] = f"/api/items/{rec['id']}/thumb?v={v}"
    return rec


def _registry_dict():
    """Reconstruct the whole registry as {id: record} (photos excluded —
    records carry has_photo instead). Used by the KV blob API, backup,
    export, locations, and stats."""
    out = {}
    for row in _db().execute("SELECT id, data FROM items"):
        out[row["id"]] = json.loads(row["data"])
    return out


def _registry_replace(new_reg):
    """Full-registry replace (KV PUT / import / restore paths). Inline
    data: photos on incoming records are extracted into blob storage.
    Photos whose ids vanish are pruned; surviving ids keep their photos."""
    d = _db()
    d.execute("BEGIN")
    try:
        d.execute("DELETE FROM items")
        d.execute("DELETE FROM items_fts")
        for rid, rec in (new_reg or {}).items():
            if not isinstance(rec, dict):
                continue
            rec = dict(rec)
            rec["id"] = rid
            photo_val = rec.pop("photo", None)
            if isinstance(photo_val, str) and photo_val.startswith("data:"):
                if _photo_store(rid, photo_val):
                    rec["has_photo"] = True
            _item_write(rec)
            d.execute("INSERT OR IGNORE INTO used_ids(id) VALUES(?)", (rid,))
        d.execute("DELETE FROM item_photos WHERE id NOT IN (SELECT id FROM items)")
        _bump_change()
        d.execute("COMMIT")
    except Exception:
        d.execute("ROLLBACK")
        raise


# ---- Dispatchers: legacy blob API over SQLite for the heavy keys ----
_read_file_fs = _read_file
_write_file_fs = _write_file


def _read_file(key: str):
    if key == "st:registry":
        return json.dumps(_registry_dict())
    if key == "st:used":
        rows = _db().execute("SELECT id FROM used_ids ORDER BY id").fetchall()
        return json.dumps([r["id"] for r in rows])
    if key == "st:categories":
        out = {}
        for row in _db().execute("SELECT id, data FROM categories"):
            out[row["id"]] = json.loads(row["data"])
        return json.dumps(out)
    return _read_file_fs(key)


def _write_file(key: str, value: str):
    if key == "st:registry":
        _registry_replace(json.loads(value))
        return
    if key == "st:used":
        ids = json.loads(value)
        d = _db()
        d.execute("BEGIN")
        try:
            d.execute("DELETE FROM used_ids")
            d.executemany("INSERT OR IGNORE INTO used_ids(id) VALUES(?)",
                          [(i,) for i in ids if isinstance(i, str)])
            d.execute("COMMIT")
        except Exception:
            d.execute("ROLLBACK")
            raise
        return
    if key == "st:categories":
        tree = json.loads(value)
        d = _db()
        d.execute("BEGIN")
        try:
            d.execute("DELETE FROM categories")
            d.executemany("INSERT INTO categories(id, data) VALUES(?,?)",
                          [(cid, json.dumps(node)) for cid, node in (tree or {}).items()])
            d.execute("COMMIT")
        except Exception:
            d.execute("ROLLBACK")
            raise
        return
    _write_file_fs(key, value)


def _migrate_from_json():
    """One-time startup migration: if the DB has no items but a legacy
    st_registry.json exists, import everything (extracting inline base64
    photos into blobs), then park the legacy files as *.pre-sqlite so
    nothing is lost and the migration never re-fires."""
    d = _db()
    have = d.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    legacy_reg = _safe_path("st:registry")
    if have > 0 or not legacy_reg.exists():
        return
    app.logger.warning("SQLite migration: importing legacy JSON data...")
    stats = {"items": 0, "photos": 0, "used": 0, "categories": 0}
    try:
        reg = json.loads(legacy_reg.read_text() or "{}")
    except Exception:
        reg = {}
    photos_before = sum(1 for r in reg.values()
                        if isinstance(r, dict) and str(r.get("photo", "")).startswith("data:"))
    _registry_replace(reg)
    stats["items"] = len(reg)
    stats["photos"] = photos_before
    # used ids beyond those in the registry
    legacy_used = _safe_path("st:used")
    if legacy_used.exists():
        try:
            ids = json.loads(legacy_used.read_text() or "[]")
            d.executemany("INSERT OR IGNORE INTO used_ids(id) VALUES(?)",
                          [(i,) for i in ids if isinstance(i, str)])
            d.commit()
            stats["used"] = len(ids)
        except Exception as e:
            app.logger.warning(f"used-id migration failed: {e}")
    legacy_cat = _safe_path("st:categories")
    if legacy_cat.exists():
        try:
            _write_file("st:categories", legacy_cat.read_text() or "{}")
            stats["categories"] = len(json.loads(legacy_cat.read_text() or "{}"))
        except Exception as e:
            app.logger.warning(f"categories migration failed: {e}")
    for key in ("st:registry", "st:used", "st:categories"):
        p = _safe_path(key)
        if p.exists():
            try:
                p.rename(p.with_suffix(p.suffix + ".pre-sqlite"))
            except Exception:
                pass
    app.logger.warning(f"SQLite migration complete: {stats}")


# NOTE: _db_init() and _migrate_from_json() are invoked at the END of this
# module (after all helpers like _now_iso exist), not here.


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "schema": "stowtrace-inventory/v1",
        "data_dir": str(DATA_DIR),
    })

# ==================================================================
# Phase 3 (v2.4.0): Accounts, roles, sessions, audit log
# ==================================================================
# Design principle: HOME MODE (default) = no auth, identical behavior to all
# prior versions. BUSINESS MODE is enabled by creating the first owner
# account via /api/auth/setup; from then on, mutating endpoints require a
# session with sufficient role. The audit log is append-only: no endpoint
# can modify or delete entries; only owners can read it.

ROLE_LEVELS = {"employee": 1, "manager": 2, "owner": 3}
SESSION_TTL_S = 12 * 3600     # 12h shifts


def _auth_init():
    d = _db()
    d.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pin_hash TEXT NOT NULL,
            role     TEXT NOT NULL,
            active   INTEGER NOT NULL DEFAULT 1,
            created  TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token   TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created TEXT,
            expires REAL
        );
        CREATE TABLE IF NOT EXISTS audit (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            user    TEXT,
            role    TEXT,
            action  TEXT NOT NULL,
            item_id TEXT,
            detail  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit(ts);
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit(user);
        CREATE TABLE IF NOT EXISTS locations_tree (
            id   TEXT PRIMARY KEY,
            data TEXT NOT NULL
        );
    """)
    d.commit()


def _pin_hash(pin):
    import hashlib as _h
    return _h.sha256(("st-pin::" + str(pin)).encode()).hexdigest()


def _business_mode():
    row = _db().execute("SELECT COUNT(*) c FROM users WHERE active=1").fetchone()
    return row["c"] > 0


def _current_user():
    tok = request.cookies.get("st_session") or request.headers.get("X-Session")
    if not tok:
        return None
    import time as _t
    row = _db().execute(
        """SELECT u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id
           WHERE s.token=? AND s.expires > ? AND u.active=1""",
        (tok, _t.time())).fetchone()
    return {"username": row["username"], "role": row["role"]} if row else None


def _audit(action, item_id=None, detail=None, user=None):
    """Append-only audit entry. Never raises — auditing must not break ops."""
    try:
        u = user or _current_user() or {}
        _db().execute(
            "INSERT INTO audit(ts, user, role, action, item_id, detail) VALUES(?,?,?,?,?,?)",
            (_now_iso(), u.get("username"), u.get("role"), action, item_id,
             json.dumps(detail) if isinstance(detail, (dict, list)) else detail))
    except Exception as e:
        app.logger.warning(f"audit write failed: {e}")


# Central role gate: (METHOD, path-prefix) -> minimum role in business mode.
# Reads stay open (guest kiosk browses freely); mutations need a session.
_GATE_RULES = [
    ("POST",   "/api/auth/",        None),        # auth endpoints handle themselves
    ("GET",    "/api/",             None),        # all reads open (kiosk)
    ("DELETE", "/api/items/",       "manager"),
    ("DELETE", "/api/categories/",  "manager"),
    ("DELETE", "/api/loctree/",     "manager"),
    ("POST",   "/api/system/restore", "owner"),
    ("POST",   "/api/ap/config",    "owner"),   # change hotspot SSID/pw = owner only
    ("POST",   "/api/reset",        "owner"),
    ("POST",   "/api/categories",   "manager"),
    ("PUT",    "/api/categories/",  "manager"),
    ("POST",   "/api/loctree",      "manager"),
    ("PUT",    "/api/loctree/",     "manager"),
    ("PUT",    "/api/users/",       "owner"),
    ("POST",   "/api/users",        "owner"),
    ("PUT",    "/api/",             "employee"),  # item edits etc.
    ("POST",   "/api/",             "employee"),  # everything else mutating
]


@app.before_request
def _enforce_roles():
    if not request.path.startswith("/api/"):
        return None
    if not _business_mode():
        return None                    # HOME MODE: zero enforcement
    for method, prefix, min_role in _GATE_RULES:
        if request.method == method and request.path.startswith(prefix):
            if min_role is None:
                return None
            u = _current_user()
            if not u:
                return jsonify({"error": "login required"}), 401
            if ROLE_LEVELS.get(u["role"], 0) < ROLE_LEVELS[min_role]:
                return jsonify({"error": f"requires {min_role}"}), 403
            return None
    return None


@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    return jsonify({"business_mode": _business_mode(),
                    "user": _current_user()})


@app.route("/api/auth/setup", methods=["POST"])
def auth_setup():
    """Create the FIRST owner account — enables Business Mode. Only works
    when no users exist yet, so it can't be abused after go-live."""
    if _business_mode():
        abort(403, description="already set up — owner can add users")
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    pin = str(body.get("pin") or "").strip()
    if not username or len(pin) < 4:
        abort(400, description="username and a PIN of 4+ digits required")
    _db().execute(
        "INSERT INTO users(username, pin_hash, role, active, created) VALUES(?,?,?,1,?)",
        (username, _pin_hash(pin), "owner", _now_iso()))
    _audit("business_mode_enabled", detail=f"first owner: {username}",
           user={"username": username, "role": "owner"})
    return _login_response(username)


def _login_response(username):
    import time as _t
    import secrets as _s
    row = _db().execute("SELECT id, username, role FROM users WHERE username=? AND active=1",
                        (username,)).fetchone()
    tok = _s.token_urlsafe(32)
    _db().execute("INSERT INTO sessions(token, user_id, created, expires) VALUES(?,?,?,?)",
                  (tok, row["id"], _now_iso(), _t.time() + SESSION_TTL_S))
    resp = jsonify({"ok": True, "user": {"username": row["username"], "role": row["role"]}})
    resp.set_cookie("st_session", tok, max_age=SESSION_TTL_S,
                    httponly=True, samesite="Lax")
    return resp


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    pin = str(body.get("pin") or "").strip()
    row = _db().execute("SELECT pin_hash FROM users WHERE username=? AND active=1",
                        (username,)).fetchone()
    if not row or row["pin_hash"] != _pin_hash(pin):
        _audit("login_failed", detail=username,
               user={"username": username, "role": None})
        abort(401, description="bad login")
    _audit("login", user={"username": username, "role": None})
    return _login_response(username)


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    tok = request.cookies.get("st_session")
    if tok:
        _db().execute("DELETE FROM sessions WHERE token=?", (tok,))
    resp = jsonify({"ok": True})
    resp.set_cookie("st_session", "", max_age=0)
    return resp


@app.route("/api/users", methods=["GET"])
def users_list():
    if _business_mode():
        u = _current_user()
        if not u or u["role"] != "owner":
            # Non-owners get names only (login picker needs them)
            rows = _db().execute("SELECT username FROM users WHERE active=1 ORDER BY username").fetchall()
            return jsonify({"users": [{"username": r["username"]} for r in rows]})
    rows = _db().execute("SELECT id, username, role, active, created FROM users ORDER BY username").fetchall()
    return jsonify({"users": [dict(r) for r in rows]})


@app.route("/api/users", methods=["POST"])
def users_create():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    pin = str(body.get("pin") or "").strip()
    role = body.get("role")
    if not username or len(pin) < 4 or role not in ROLE_LEVELS:
        abort(400, description="username, 4+ digit PIN, and valid role required")
    try:
        _db().execute(
            "INSERT INTO users(username, pin_hash, role, active, created) VALUES(?,?,?,1,?)",
            (username, _pin_hash(pin), role, _now_iso()))
    except sqlite3.IntegrityError:
        abort(400, description="username already exists")
    _audit("user_created", detail={"username": username, "role": role})
    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["PUT"])
def users_update(username):
    body = request.get_json(silent=True) or {}
    row = _db().execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        abort(404)
    if "role" in body:
        if body["role"] not in ROLE_LEVELS:
            abort(400, description="invalid role")
        _db().execute("UPDATE users SET role=? WHERE id=?", (body["role"], row["id"]))
    if "active" in body:
        _db().execute("UPDATE users SET active=? WHERE id=?",
                      (1 if body["active"] else 0, row["id"]))
        if not body["active"]:
            _db().execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    if "pin" in body:
        pin = str(body["pin"]).strip()
        if len(pin) < 4:
            abort(400, description="PIN too short")
        _db().execute("UPDATE users SET pin_hash=? WHERE id=?", (_pin_hash(pin), row["id"]))
    _audit("user_updated", detail={"username": username,
                                   "fields": [k for k in body.keys() if k != "pin"] + (["pin"] if "pin" in body else [])})
    return jsonify({"ok": True})


@app.route("/api/audit", methods=["GET"])
def audit_list():
    if _business_mode():
        u = _current_user()
        if not u or u["role"] != "owner":
            abort(403, description="owner only")
    limit = min(int(request.args.get("limit", 200)), 1000)
    rows = _db().execute(
        "SELECT ts, user, role, action, item_id, detail FROM audit ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    return jsonify({"entries": [dict(r) for r in rows]})


@app.route("/api/reports/adjustments", methods=["GET"])
def report_adjustments():
    """Phase 3.5: owner oversight. Aggregates quantity-adjustment audit
    entries per user plus the biggest recent decreases."""
    if _business_mode():
        u = _current_user()
        if not u or u["role"] != "owner":
            abort(403, description="owner only")
    days = int(request.args.get("days", 30))
    import datetime as _dt
    since = (_dt.datetime.utcnow() - _dt.timedelta(days=days)).isoformat()
    rows = _db().execute(
        """SELECT user, COUNT(*) n FROM audit
           WHERE action='qty_adjust' AND ts >= ? GROUP BY user ORDER BY n DESC""",
        (since,)).fetchall()
    leaderboard = [dict(r) for r in rows]
    recent = _db().execute(
        """SELECT ts, user, item_id, detail FROM audit
           WHERE action='qty_adjust' AND ts >= ? ORDER BY id DESC LIMIT 100""",
        (since,)).fetchall()
    sensitive = _db().execute(
        """SELECT ts, user, action, item_id, detail FROM audit
           WHERE action IN ('item_deleted','restore','reset','user_created','user_updated')
             AND ts >= ? ORDER BY id DESC LIMIT 50""",
        (since,)).fetchall()
    return jsonify({"days": days,
                    "adjustment_leaderboard": leaderboard,
                    "recent_adjustments": [dict(r) for r in recent],
                    "sensitive_events": [dict(r) for r in sensitive]})


# ==================================================================
# Phase 1 (v2.4.0): server-side keyword search on the FTS index
# ==================================================================

@app.route("/api/search", methods=["GET"])
def search_items():
    """Ranked keyword search. Every token must match (AND). Returns hydrated
    rows with thumb URLs and a resolved parent/location summary — everything
    the dropdown and full-results UIs need in one call."""
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 50)), 200)
    if not q:
        return jsonify({"query": q, "count": 0, "results": []})
    tokens = ["".join(ch for ch in t if ch.isalnum()) for t in q.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return jsonify({"query": q, "count": 0, "results": []})
    fts_q = " AND ".join(f'"{t}"*' for t in tokens)   # prefix match per token
    d = _db()
    results = []
    try:
        rows = d.execute(
            """SELECT f.item_id FROM items_fts f
               JOIN items i ON i.id = f.item_id
               WHERE items_fts MATCH ? AND i.type IN ('container','item')
               ORDER BY rank LIMIT ?""", (fts_q, limit)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        rec = _item_get(r["item_id"])
        if not rec:
            continue
        rec = _decorate_photo_urls(rec)
        results.append({
            "id": rec["id"], "type": rec.get("type"),
            "description": rec.get("description"),
            "supplier": rec.get("supplier"),
            "supplier_sku": rec.get("supplier_sku"),
            "quantity": rec.get("quantity"),
            "thumb": rec.get("thumb"),
            "has_photo": bool(rec.get("has_photo")),
            "parent": _parent_summary(rec.get("bin_id")),
        })
    return jsonify({"query": q, "count": len(results), "results": results})


# ==================================================================
# Phase 2 (v2.4.0): Locations tree (Store → wall → cabinet → ...)
# ==================================================================
# Mirrors the categories machinery: flat table of nodes with parent_id.
# Bins reference a tree node via location_id. Legacy free-text location
# fields keep working; the tree is the forward path.

def _loc_load():
    out = {}
    for row in _db().execute("SELECT id, data FROM locations_tree"):
        out[row["id"]] = json.loads(row["data"])
    return out


def _loc_save_node(node):
    _db().execute(
        "INSERT INTO locations_tree(id, data) VALUES(?,?) "
        "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (node["id"], json.dumps(node)))


@app.route("/api/loctree", methods=["GET"])
def loctree_list():
    tree = _loc_load()
    counts = {}
    for row in _db().execute(
            "SELECT data FROM items WHERE type='bin'"):
        rec = json.loads(row["data"])
        lid = rec.get("location_id")
        if lid:
            counts[lid] = counts.get(lid, 0) + 1
    return jsonify({"locations": tree, "counts": counts})


@app.route("/api/loctree", methods=["POST"])
def loctree_create():
    import secrets as _s
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="name required")
    parent_id = body.get("parent_id")
    tree = _loc_load()
    if parent_id is not None and parent_id not in tree:
        abort(400, description="parent not found")
    lid = "loc_" + _s.token_hex(4)
    node = {"id": lid, "name": name, "parent_id": parent_id}
    _loc_save_node(node)
    _audit("location_created", detail={"id": lid, "name": name})
    return jsonify({"ok": True, "location": node})


@app.route("/api/loctree/<loc_id>", methods=["PUT"])
def loctree_update(loc_id):
    body = request.get_json(silent=True) or {}
    tree = _loc_load()
    if loc_id not in tree:
        abort(404)
    node = tree[loc_id]
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            abort(400, description="name cannot be empty")
        node["name"] = name
    if "parent_id" in body:
        np = body["parent_id"]
        if np is not None:
            if np not in tree or np == loc_id:
                abort(400, description="bad parent")
            cur = np
            while cur:
                if cur == loc_id:
                    abort(400, description="cycle")
                cur = tree.get(cur, {}).get("parent_id")
        node["parent_id"] = np
    _loc_save_node(node)
    _audit("location_updated", detail={"id": loc_id})
    return jsonify({"ok": True, "location": node})


@app.route("/api/loctree/<loc_id>", methods=["DELETE"])
def loctree_delete(loc_id):
    tree = _loc_load()
    if loc_id not in tree:
        abort(404)
    children = [k for k, v in tree.items() if v.get("parent_id") == loc_id]
    promote = request.args.get("promote_children") in ("1", "true", "yes")
    cascade = request.args.get("cascade") in ("1", "true", "yes")
    if children and not promote and not cascade:
        return jsonify({"ok": False, "error": "has_children",
                        "child_count": len(children)}), 409
    d = _db()
    if cascade:
        doomed = [loc_id]
        stack = [loc_id]
        while stack:
            cur = stack.pop()
            for k, v in tree.items():
                if v.get("parent_id") == cur and k not in doomed:
                    doomed.append(k)
                    stack.append(k)
        for k in doomed:
            d.execute("DELETE FROM locations_tree WHERE id=?", (k,))
    else:
        parent = tree[loc_id].get("parent_id")
        for c in children:
            tree[c]["parent_id"] = parent
            _loc_save_node(tree[c])
        d.execute("DELETE FROM locations_tree WHERE id=?", (loc_id,))
    _audit("location_deleted", detail={"id": loc_id})
    return jsonify({"ok": True})


@app.route("/api/kv/<key>", methods=["GET"])
def kv_get(key):
    if key not in ALLOWED_KEYS:
        abort(400, description=f"unknown key: {key}")
    val = _read_file(key)
    if val is None:
        abort(404)
    return jsonify({"key": key, "value": val})

@app.route("/api/kv/<key>", methods=["PUT"])
def kv_put(key):
    if key not in ALLOWED_KEYS:
        abort(400, description=f"unknown key: {key}")
    body = request.get_json(silent=True)
    if body is None or "value" not in body:
        abort(400, description="body must be JSON with 'value' field")
    value = body["value"]
    if not isinstance(value, str):
        abort(400, description="'value' must be a string")
    if len(value) > 50 * 1024 * 1024:  # 50MB cap per key
        abort(413, description="value too large")
    try:
        _write_file(key, value)
    except Exception as e:
        app.logger.exception("write failed")
        abort(500, description=str(e))
    return jsonify({"ok": True, "key": key})

@app.route("/api/registry-hash", methods=["GET"])
def registry_hash():
    """Returns a change token so clients can poll for changes cheaply.
    v2.0.0: a monotonic counter bumped on every write — O(1) instead of
    hashing the whole registry."""
    return jsonify({"hash": "c" + str(_change_counter())})

@app.route("/api/export", methods=["GET"])
def export_inventory():
    """Returns the full registry as a v1 inventory file."""
    raw = _read_file("st:registry") or "{}"
    try:
        registry = json.loads(raw)
    except Exception:
        registry = {}
    from datetime import datetime, timezone
    items = sorted(
        registry.values(),
        key=lambda r: r.get("created", "")
    )
    return jsonify({
        "schema": "stowtrace-inventory/v1",
        "exported": datetime.now(timezone.utc).isoformat(),
        "items": items,
    })

@app.route("/api/import", methods=["POST"])
def import_inventory():
    """Merge a v1 inventory file. Skips items whose IDs already exist."""
    data = request.get_json(silent=True)
    if not data or data.get("schema", "").split("/")[0] != "stowtrace-inventory":
        abort(400, description="not a stowtrace-inventory file")
    incoming = data.get("items") or []
    if not isinstance(incoming, list):
        abort(400, description="items must be a list")

    # Load existing
    raw_reg = _read_file("st:registry") or "{}"
    raw_used = _read_file("st:used") or "[]"
    try:
        registry = json.loads(raw_reg)
        used = set(json.loads(raw_used))
    except Exception:
        registry = {}
        used = set()

    added = 0
    skipped = 0
    for it in incoming:
        if not isinstance(it, dict):
            continue
        rid = it.get("id")
        if not rid:
            continue
        if rid in registry:
            skipped += 1
            continue
        registry[rid] = it
        used.add(rid)
        added += 1

    # Write atomically
    _write_file("st:registry", json.dumps(registry))
    _write_file("st:used", json.dumps(sorted(used)))

    return jsonify({"ok": True, "added": added, "skipped": skipped})

@app.route("/api/reset", methods=["POST"])
def reset_all():
    """Wipe all stored data. Requires confirm=true in body."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        abort(400, description="set confirm=true to wipe")
    for key in ALLOWED_KEYS:
        p = _safe_path(key)
        if p.exists():
            p.unlink()
    # v2.0.0: also wipe the SQLite-backed tables
    d = _db()
    d.execute("BEGIN")
    try:
        for tbl in ("items", "items_fts", "item_photos", "used_ids", "categories"):
            d.execute(f"DELETE FROM {tbl}")
        _bump_change()
        d.execute("COMMIT")
    except Exception:
        d.execute("ROLLBACK")
        raise
    _audit("reset")
    return jsonify({"ok": True, "reset": True})


# ------------------------------------------------------------------
# Slice 8: Categories — hierarchical tree with attribute schemas at leaves
# ------------------------------------------------------------------
# Categories are stored as a flat dict keyed by id, each with a parent_id
# pointing at its parent (null for top-level). A node can optionally declare
# an `attributes` schema — a list of {key, label, type, values?} entries that
# define the facets containers in that leaf can have. Containers reference
# their category by id and store filled-in attribute values in a flat dict.
#
# All operations are dict-merge style — no schema enforcement — so a category
# never breaks containers if its schema changes. Removed attribute keys leave
# orphan values on existing containers (intentionally — no data loss).

import secrets


def _cat_load():
    raw = _read_file("st:categories")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _cat_save(tree):
    _write_file("st:categories", json.dumps(tree))


def _cat_new_id():
    """Short random ID for a category (separate namespace from container IDs)."""
    # cat_ prefix keeps it visually distinct from container IDs in URLs/logs
    return "cat_" + secrets.token_hex(4)


def _cat_has_descendant(tree, parent_id, candidate_id):
    """Cycle check: would making candidate_id a child of parent_id create a loop?"""
    cur = parent_id
    while cur is not None:
        if cur == candidate_id:
            return True
        node = tree.get(cur)
        if not node:
            break
        cur = node.get("parent_id")
    return False


def _cat_descendants(tree, root_id):
    """Returns all descendant ids of root_id (not including root_id itself)."""
    out = []
    stack = [root_id]
    while stack:
        cur = stack.pop()
        for cid, node in tree.items():
            if node.get("parent_id") == cur:
                out.append(cid)
                stack.append(cid)
    return out


@app.route("/api/categories", methods=["GET"])
def categories_list():
    """Return the full category tree as a flat dict {id: node}.
    The client builds the tree structure. Also returns container counts per
    category so the UI can show '(N)' badges."""
    tree = _cat_load()
    reg = _load_registry()
    # Count containers per category (only direct membership; the UI walks the
    # tree to compute roll-up totals client-side).
    counts = {}
    for rec in reg.values():
        cid = rec.get("category_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    return jsonify({"categories": tree, "counts": counts})


@app.route("/api/categories", methods=["POST"])
def categories_create():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        abort(400, description="name required")
    parent_id = body.get("parent_id")
    tree = _cat_load()
    if parent_id is not None and parent_id not in tree:
        abort(400, description=f"parent_id {parent_id} not found")
    cid = _cat_new_id()
    while cid in tree:
        cid = _cat_new_id()
    tree[cid] = {
        "id": cid,
        "name": name,
        "parent_id": parent_id,
        "attributes": body.get("attributes") or [],
    }
    _cat_save(tree)
    return jsonify({"ok": True, "category": tree[cid]})


@app.route("/api/categories/<cat_id>", methods=["PUT"])
def categories_update(cat_id):
    body = request.get_json(silent=True) or {}
    tree = _cat_load()
    if cat_id not in tree:
        abort(404, description="category not found")
    node = tree[cat_id]
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            abort(400, description="name cannot be empty")
        node["name"] = name
    if "parent_id" in body:
        new_parent = body["parent_id"]
        if new_parent is not None:
            if new_parent not in tree:
                abort(400, description=f"parent_id {new_parent} not found")
            if new_parent == cat_id:
                abort(400, description="cannot parent to self")
            if _cat_has_descendant(tree, new_parent, cat_id):
                abort(400, description="cycle: new parent is a descendant of this node")
        node["parent_id"] = new_parent
    if "attributes" in body:
        attrs = body["attributes"] or []
        if not isinstance(attrs, list):
            abort(400, description="attributes must be a list")
        # Light validation — each must have key + label + type
        for a in attrs:
            if not isinstance(a, dict) or not a.get("key") or not a.get("label") or not a.get("type"):
                abort(400, description="each attribute needs key, label, type")
        node["attributes"] = attrs
    _cat_save(tree)
    return jsonify({"ok": True, "category": node})


@app.route("/api/categories/<cat_id>", methods=["DELETE"])
def categories_delete(cat_id):
    """Delete a category. Behavior depends on query params:
       ?cascade=true        — delete this node AND all descendants
       ?promote_children=true — move children up one level (set their parent_id
                              to this node's parent_id) before deleting.
       Default (no flag) — refuse if children exist.
       Containers referencing the deleted category(ies) keep their stored
       category_id but it dangles; the UI shows them as 'Uncategorized'."""
    tree = _cat_load()
    if cat_id not in tree:
        abort(404, description="category not found")
    cascade = request.args.get("cascade") in ("1", "true", "yes")
    promote = request.args.get("promote_children") in ("1", "true", "yes")
    children = [k for k, v in tree.items() if v.get("parent_id") == cat_id]

    if children and not cascade and not promote:
        return jsonify({"ok": False, "error": "has_children",
                        "child_count": len(children)}), 409

    if cascade:
        to_delete = [cat_id] + _cat_descendants(tree, cat_id)
        for tid in to_delete:
            tree.pop(tid, None)
    else:
        # promote_children or no children
        parent_of_deleted = tree[cat_id].get("parent_id")
        for c in children:
            tree[c]["parent_id"] = parent_of_deleted
        tree.pop(cat_id, None)

    _cat_save(tree)
    return jsonify({"ok": True, "deleted": cat_id})


@app.route("/api/categories/seed", methods=["POST"])
def categories_seed():
    """One-shot seeding endpoint: populates the tree with Hardware and
    Electronics examples if empty. Idempotent — refuses if any category
    exists. Frontend calls this on first run when the user hits the empty
    Categories tab."""
    tree = _cat_load()
    if tree:
        return jsonify({"ok": False, "error": "not_empty",
                        "existing_count": len(tree)}), 409

    def add(name, parent_id, attrs=None):
        cid = _cat_new_id()
        while cid in tree:
            cid = _cat_new_id()
        tree[cid] = {
            "id": cid, "name": name, "parent_id": parent_id,
            "attributes": attrs or [],
        }
        return cid

    # --- Hardware tree ---
    hw = add("Hardware", None)
    fast = add("Fasteners", hw)

    # Bolts and screws split by thread size at the next level so attributes
    # can be schema-clean per-size.
    bolts = add("Bolts", fast, attrs=[
        {"key": "thread",   "label": "Thread",    "type": "select",
         "values": ["M2", "M3", "M4", "M5", "M6", "M8", "M10",
                    "#4-40", "#6-32", "#8-32", "#10-24", "#10-32",
                    "1/4-20", "5/16-18", "3/8-16"]},
        {"key": "length_mm", "label": "Length (mm)", "type": "number"},
        {"key": "head",      "label": "Head type", "type": "select",
         "values": ["socket cap", "button", "flat / countersunk",
                    "pan", "hex", "set screw"]},
        {"key": "material",  "label": "Material", "type": "select",
         "values": ["steel", "stainless", "brass", "aluminum",
                    "nylon", "titanium"]},
        {"key": "coating",   "label": "Coating",  "type": "select",
         "values": ["none", "zinc", "black oxide", "galvanized"]},
    ])
    screws = add("Wood / Sheet Screws", fast, attrs=[
        {"key": "gauge",     "label": "Gauge",       "type": "text"},
        {"key": "length_mm", "label": "Length (mm)", "type": "number"},
        {"key": "head",      "label": "Head type",   "type": "select",
         "values": ["flat", "pan", "round", "truss", "wafer"]},
        {"key": "drive",     "label": "Drive",       "type": "select",
         "values": ["phillips", "torx", "robertson", "slot", "hex"]},
    ])
    nuts = add("Nuts", fast, attrs=[
        {"key": "thread",   "label": "Thread", "type": "select",
         "values": ["M2", "M3", "M4", "M5", "M6", "M8", "M10",
                    "#4-40", "#6-32", "#8-32", "#10-24", "#10-32",
                    "1/4-20", "5/16-18", "3/8-16"]},
        {"key": "style",    "label": "Style",  "type": "select",
         "values": ["hex", "nylock", "wing", "cap (acorn)", "T-nut",
                    "square", "knurled"]},
        {"key": "material", "label": "Material", "type": "select",
         "values": ["steel", "stainless", "brass", "nylon"]},
    ])
    washers = add("Washers", fast, attrs=[
        {"key": "thread",   "label": "Fits", "type": "select",
         "values": ["M2", "M3", "M4", "M5", "M6", "M8", "M10",
                    "#4", "#6", "#8", "#10", "1/4", "5/16", "3/8"]},
        {"key": "style",    "label": "Style", "type": "select",
         "values": ["flat", "lock (split)", "star (internal)",
                    "star (external)", "fender"]},
        {"key": "material", "label": "Material", "type": "select",
         "values": ["steel", "stainless", "brass", "nylon", "rubber"]},
    ])

    # --- Electronics tree ---
    el_root = add("Electronics", None)
    passives = add("Passive Components", el_root)
    add("Resistors", passives, attrs=[
        {"key": "value",       "label": "Value (Ω)",    "type": "text"},
        {"key": "tolerance",   "label": "Tolerance",    "type": "select",
         "values": ["1%", "5%", "10%"]},
        {"key": "power_w",     "label": "Power (W)",    "type": "select",
         "values": ["1/8", "1/4", "1/2", "1", "2", "5"]},
        {"key": "package",     "label": "Package",      "type": "select",
         "values": ["through-hole", "0402", "0603", "0805", "1206",
                    "1210", "2010", "2512"]},
    ])
    add("Capacitors", passives, attrs=[
        {"key": "value",     "label": "Value",      "type": "text"},
        {"key": "voltage_v", "label": "Voltage (V)", "type": "number"},
        {"key": "type",      "label": "Type",       "type": "select",
         "values": ["ceramic", "electrolytic", "tantalum",
                    "film", "supercapacitor"]},
        {"key": "package",   "label": "Package",    "type": "select",
         "values": ["through-hole", "0603", "0805", "1206", "radial",
                    "axial", "SMD bulk"]},
    ])
    add("Inductors", passives, attrs=[
        {"key": "value",     "label": "Value (H)",  "type": "text"},
        {"key": "current_a", "label": "Current (A)", "type": "number"},
        {"key": "package",   "label": "Package",    "type": "select",
         "values": ["through-hole", "shielded SMD", "unshielded SMD"]},
    ])
    actives = add("Active Components", el_root)
    add("ICs / Microcontrollers", actives, attrs=[
        {"key": "function", "label": "Function", "type": "text"},
        {"key": "package", "label": "Package", "type": "select",
         "values": ["DIP", "SOIC", "TSSOP", "QFP", "QFN", "BGA"]},
    ])
    add("Transistors", actives, attrs=[
        {"key": "type",     "label": "Type",    "type": "select",
         "values": ["NPN BJT", "PNP BJT", "N-MOSFET", "P-MOSFET",
                    "JFET", "IGBT"]},
        {"key": "package",  "label": "Package", "type": "select",
         "values": ["TO-92", "TO-220", "TO-247", "SOT-23",
                    "SOT-223", "D-PAK", "DPAK"]},
    ])
    add("Diodes", actives, attrs=[
        {"key": "type",    "label": "Type",    "type": "select",
         "values": ["standard", "schottky", "zener", "TVS", "rectifier"]},
        {"key": "package", "label": "Package", "type": "select",
         "values": ["DO-35", "DO-41", "DO-214", "SMA", "SMB",
                    "SOD-123", "SOT-23"]},
    ])
    iface = add("Interface & I/O", el_root)
    add("Switches", iface, attrs=[
        {"key": "style", "label": "Style", "type": "select",
         "values": ["tactile push-button", "toggle", "rocker", "slide",
                    "rotary", "DIP", "limit"]},
        {"key": "poles", "label": "Poles", "type": "select",
         "values": ["SPST", "SPDT", "DPST", "DPDT"]},
    ])
    add("LEDs", iface, attrs=[
        {"key": "color",   "label": "Color",   "type": "select",
         "values": ["red", "green", "yellow", "blue", "white",
                    "RGB", "infrared", "UV"]},
        {"key": "package", "label": "Package", "type": "select",
         "values": ["3mm THT", "5mm THT", "0603", "0805", "1206",
                    "PLCC-2", "WS2812 strip"]},
    ])
    add("Connectors / Headers", iface)
    add("Displays", iface, attrs=[
        {"key": "type",     "label": "Type", "type": "select",
         "values": ["7-segment", "character LCD", "OLED", "TFT",
                    "e-paper"]},
        {"key": "size",     "label": "Size", "type": "text"},
        {"key": "controller", "label": "Controller IC", "type": "text"},
    ])
    boards = add("Boards & Modules", el_root)
    add("Perf / Proto Boards", boards, attrs=[
        {"key": "size",  "label": "Size",  "type": "text"},
        {"key": "style", "label": "Style", "type": "select",
         "values": ["solderable", "breadboard", "Arduino shield",
                    "Raspberry Pi HAT"]},
    ])
    add("Development Boards", boards)
    add("Modules / Breakouts", boards)

    _cat_save(tree)
    return jsonify({"ok": True, "category_count": len(tree)})


# ------------------------------------------------------------------
# Slice 6: Backup & Restore — full-state portable snapshot
# ------------------------------------------------------------------
# The /api/system/backup endpoint bundles everything in DATA_DIR into a single
# JSON file. /api/system/restore accepts an uploaded backup and MERGES it
# (only adding IDs/items/presets not currently present — current state wins).
# An optional /api/system/backup-to-drive writes the same payload to a USB
# drive mounted at /mnt/backup, and /api/system/backup-drive-status tells the
# UI whether that destination is available.

BACKUP_DRIVE_PATH = "/mnt/backup"
BACKUP_SCHEMA = "stowtrace-backup/v1"

# Keys that participate in backup/restore. Order matters only for readability.
_BACKUP_KEYS = [
    "st:registry",       # the inventory items keyed by ID — the big one
    "st:used",           # set of issued IDs
    "st:tape_presets",   # user-saved label format presets (Label Maker)
    "st:sheet_presets",  # user-saved label format presets (Printer)
    "st:config",         # QR mode, URL prefix, format defaults, etc.
    "st:maker_rows",     # working queue rows (Label Maker tab)
    "st:printer_rows",   # working queue rows (Printer tab)
    "st:categories",     # Slice 8: category tree + attribute schemas
]


def _build_backup_payload():
    """Returns a dict suitable for json.dumps — the full backup."""
    from datetime import datetime, timezone
    data = {}
    for key in _BACKUP_KEYS:
        raw = _read_file(key)
        if raw is None:
            continue
        try:
            data[key] = json.loads(raw)
        except Exception:
            # If a file is corrupt, include the raw text so the user can still
            # recover something rather than silently dropping it.
            data[key] = {"__raw__": raw}
    # v2.0.0: photos live in blob storage now — include them as a base64 map
    # so a single backup file still restores everything. (At true retail
    # scale, a future backup phase adds a separate photo-archive path.)
    photos = {}
    for row in _db().execute("SELECT id, photo FROM item_photos"):
        photos[row["id"]] = "data:image/jpeg;base64," + base64.b64encode(row["photo"]).decode("ascii")
    if photos:
        data["st:photos"] = photos
    return {
        "schema": BACKUP_SCHEMA,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "app_version": APP_VERSION,
        "data": data,
    }


def _drive_status():
    """Returns whether /mnt/backup is mounted and how much free space it has."""
    p = Path(BACKUP_DRIVE_PATH)
    try:
        # mountpoint check: the parent's st_dev differs from the directory's st_dev
        # when the directory IS a mountpoint. Falls back gracefully if /mnt/backup
        # doesn't exist at all.
        if not p.exists():
            return {"mounted": False, "reason": "not_present"}
        parent_dev = p.parent.stat().st_dev
        own_dev = p.stat().st_dev
        if parent_dev == own_dev:
            return {"mounted": False, "reason": "not_mounted"}
        # Mounted — get free space
        st = os.statvfs(p)
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
        return {
            "mounted": True,
            "path": str(p),
            "free_bytes": free,
            "total_bytes": total,
        }
    except Exception as e:
        return {"mounted": False, "reason": f"error: {e}"}


@app.route("/api/system/backup", methods=["GET"])
def system_backup():
    """Return a full-state backup as a JSON download."""
    from datetime import datetime
    payload = _build_backup_payload()
    body = json.dumps(payload, indent=2, sort_keys=False)
    # Filename includes hostname and date for easy disambiguation when the user
    # has multiple Pis or accumulates several backups in one folder.
    host = payload.get("hostname", "inv")
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"{host}-backup-{ts}.json"
    resp = app.response_class(body, mimetype="application/json")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/api/system/backup-drive-status", methods=["GET"])
def system_backup_drive_status():
    """Tells the UI whether a USB backup drive is available."""
    return jsonify(_drive_status())


@app.route("/api/system/backup-to-drive", methods=["POST"])
def system_backup_to_drive():
    """Write a full-state backup file to the USB drive at /mnt/backup."""
    from datetime import datetime
    status = _drive_status()
    if not status.get("mounted"):
        return jsonify({"ok": False, "error": "no_drive",
                        "detail": f"USB backup drive not mounted at {BACKUP_DRIVE_PATH}",
                        "drive": status}), 400

    payload = _build_backup_payload()
    body = json.dumps(payload, indent=2, sort_keys=False)
    host = payload.get("hostname", "inv")
    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    filename = f"{host}-backup-{ts}.json"
    out = Path(BACKUP_DRIVE_PATH) / filename

    # Write atomically — temp file beside the target then rename.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", delete=False,
        dir=str(out.parent),
        prefix=filename + ".",
        suffix=".tmp",
    )
    try:
        tmp.write(body)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, out)
    except Exception as e:
        try:
            tmp.close(); os.unlink(tmp.name)
        except Exception:
            pass
        return jsonify({"ok": False, "error": "write_failed", "detail": str(e)}), 500

    return jsonify({"ok": True, "path": str(out), "size_bytes": len(body), "filename": filename})


@app.route("/api/system/restore", methods=["POST"])
def system_restore():
    """Merge a backup file's contents into current state. Current state wins
    on any conflict (per Slice 6 spec: 'only restore IDs/items not currently
    present'). Accepts both:
      - stowtrace-backup/v1   (full backup; restores registry+used+presets)
      - stowtrace-inventory/v1 (inventory-only legacy export; restores registry+used)
    """
    data = request.get_json(silent=True)
    if not data:
        abort(400, description="no JSON body")

    schema = data.get("schema", "")
    summary = {"added_items": 0, "skipped_items": 0,
               "added_presets": 0, "skipped_presets": 0,
               "added_categories": 0, "skipped_categories": 0,
               "schema": schema}

    # ---- Load current state ----
    try:
        registry = json.loads(_read_file("st:registry") or "{}")
    except Exception:
        registry = {}
    try:
        used = set(json.loads(_read_file("st:used") or "[]"))
    except Exception:
        used = set()
    try:
        tape_presets = json.loads(_read_file("st:tape_presets") or "[]")
    except Exception:
        tape_presets = []
    try:
        sheet_presets = json.loads(_read_file("st:sheet_presets") or "[]")
    except Exception:
        sheet_presets = []
    try:
        categories = json.loads(_read_file("st:categories") or "{}")
    except Exception:
        categories = {}

    # ---- Determine what items/presets the backup brings ----
    incoming_items = []
    incoming_used = []
    incoming_tape_presets = []
    incoming_sheet_presets = []
    incoming_categories = {}

    if schema.startswith("stowtrace-backup/"):
        # Full backup format: data nested under data.<key>
        d = data.get("data") or {}
        reg_in = d.get("st:registry") or {}
        if isinstance(reg_in, dict):
            incoming_items = list(reg_in.values())
        used_in = d.get("st:used") or []
        if isinstance(used_in, list):
            incoming_used = used_in
        tp_in = d.get("st:tape_presets") or []
        if isinstance(tp_in, list):
            incoming_tape_presets = tp_in
        sp_in = d.get("st:sheet_presets") or []
        if isinstance(sp_in, list):
            incoming_sheet_presets = sp_in
        cat_in = d.get("st:categories") or {}
        if isinstance(cat_in, dict):
            incoming_categories = cat_in
    elif schema.startswith("stowtrace-inventory/"):
        # Legacy inventory-export format: items[] at the top level
        items = data.get("items") or []
        if not isinstance(items, list):
            abort(400, description="items must be a list")
        incoming_items = items
    else:
        abort(400, description="unsupported backup schema; expected stowtrace-backup/* or stowtrace-inventory/*")

    # ---- Merge items (registry + used) ----
    for it in incoming_items:
        if not isinstance(it, dict):
            continue
        rid = it.get("id")
        if not rid:
            continue
        if rid in registry:
            summary["skipped_items"] += 1
            continue
        registry[rid] = it
        used.add(rid)
        summary["added_items"] += 1

    # Also seed used IDs from the backup that aren't already present.
    # (Catches edge cases where a backup has IDs in used[] but missing from the
    # registry — preserve the reservation so we don't recycle the ID.)
    for rid in incoming_used:
        if isinstance(rid, str) and rid not in used:
            used.add(rid)

    # ---- Merge presets (match by name; current wins) ----
    def merge_presets(current, incoming):
        existing_names = {p.get("name") for p in current if isinstance(p, dict)}
        added = 0
        skipped = 0
        for p in incoming:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if not name:
                continue
            if name in existing_names:
                skipped += 1
                continue
            current.append(p)
            existing_names.add(name)
            added += 1
        return added, skipped

    a, s = merge_presets(tape_presets, incoming_tape_presets)
    summary["added_presets"] += a
    summary["skipped_presets"] += s
    a, s = merge_presets(sheet_presets, incoming_sheet_presets)
    summary["added_presets"] += a
    summary["skipped_presets"] += s

    # ---- Merge categories (by id; current wins) ----
    for cid, cnode in incoming_categories.items():
        if not isinstance(cnode, dict) or not cnode.get("id"):
            continue
        if cid in categories:
            summary["skipped_categories"] += 1
            continue
        categories[cid] = cnode
        summary["added_categories"] += 1

    # ---- Write everything back atomically ----
    # Note: the registry write routes through _registry_replace, which also
    # extracts any inline data: photos carried by v1-era backup items.
    _write_file("st:registry", json.dumps(registry))
    _write_file("st:used", json.dumps(sorted(used)))
    _write_file("st:tape_presets", json.dumps(tape_presets))
    _write_file("st:sheet_presets", json.dumps(sheet_presets))
    _write_file("st:categories", json.dumps(categories))

    # ---- v2.0.0: import the photo map (current photos win) ----
    incoming_photos = {}
    if schema.startswith("stowtrace-backup/"):
        p_in = (data.get("data") or {}).get("st:photos") or {}
        if isinstance(p_in, dict):
            incoming_photos = p_in
    summary["added_photos"] = 0
    d = _db()
    for pid, purl in incoming_photos.items():
        if not (isinstance(purl, str) and purl.startswith("data:")):
            continue
        rec = _item_get(pid)
        if rec is None:
            continue  # photo for an item that wasn't restored
        row = d.execute("SELECT 1 FROM item_photos WHERE id=?", (pid,)).fetchone()
        if row:
            continue  # current photo wins
        if _photo_store(pid, purl):
            rec["has_photo"] = True
            _item_write(rec)
            summary["added_photos"] += 1
    if summary["added_photos"]:
        _bump_change()
    d.commit()

    _audit("restore", detail=summary)
    return jsonify({"ok": True, **summary})


# ------------------------------------------------------------------
# Printer integration (Brother P-Touch via ptouch-print)
# ------------------------------------------------------------------

PTOUCH_BIN = os.environ.get("PTOUCH_BIN", "/usr/local/bin/ptouch-print")


def _ptouch_info():
    """Query the printer for its current state. Returns dict or None if no printer."""
    if not os.path.exists(PTOUCH_BIN):
        return {"error": "ptouch-print not installed", "connected": False}
    try:
        result = subprocess.run(
            [PTOUCH_BIN, "--info"],
            capture_output=True, text=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        return {"error": "printer query timed out", "connected": False}
    except Exception as e:
        return {"error": f"ptouch-print failed: {e}", "connected": False}

    out = (result.stdout or "") + "\n" + (result.stderr or "")
    info = {"connected": False, "raw": out.strip()}

    # Detect "no printer" states across ptouch-print versions:
    #   v1.5 - "No printers found"
    #   v1.8 - "No P-Touch printer found on USB (remember to put switch to position E)"
    no_printer_phrases = [
        "no p-touch printer found",
        "no printers found",
        "no printer found",
    ]
    out_lower = out.lower()
    is_missing = any(p in out_lower for p in no_printer_phrases)

    if is_missing:
        info["connected"] = False
        # Friendly hint for the most common cause
        if "switch to position" in out_lower or "plite" in out_lower:
            info["hint"] = "Hold the PLite button on the printer for ~2 seconds — the green LED should turn OFF"
        return info

    # Connected detection — accept any of these markers:
    #   v1.5: "PT-XYZ found on USB bus 1, device 4"
    #   v1.8: "printer has 180 dpi, maximum printing width is 128 px"
    #   v1.8 also: "maximum printing width for this tape is 76px"
    connected_markers = [
        "found on usb",
        "maximum printing width",
        "printer has",
        "media width",
    ]
    if any(m in out_lower for m in connected_markers):
        info["connected"] = True

    # Parse details from the output
    for line in out.splitlines():
        line = line.strip()
        # v1.5 model line: "PT-P700 found on USB bus 1, device 8"
        if "found on USB" in line:
            parts = line.split(" found on USB")
            if parts and parts[0].strip():
                info["model"] = parts[0].strip()
        # Tape width — appears in both versions
        if line.startswith("media width"):
            try:
                mm = int(line.split("=")[1].strip().split()[0])
                info["tape_width_mm"] = mm
            except Exception:
                pass
        # Tape printable width in pixels — v1.8 phrasing
        if "maximum printing width for this tape" in line.lower():
            # e.g. "maximum printing width for this tape is 76px"
            try:
                import re
                m = re.search(r"(\d+)\s*px", line)
                if m:
                    info["max_print_px"] = int(m.group(1))
            except Exception:
                pass
        # Or older style: "max width = 70 px"
        elif line.startswith("max width"):
            try:
                px = int(line.split("=")[1].strip().split()[0])
                info["max_print_px"] = px
            except Exception:
                pass
        if line.startswith("tape color"):
            info["tape_color"] = line.split("=")[1].strip()
        if line.startswith("text color"):
            info["text_color"] = line.split("=")[1].strip()
        # Error code — flag only if non-zero
        if line.lower().startswith("error"):
            err = line.split("=")[1].strip() if "=" in line else ""
            # Strip 0x prefix and accept either "0000" or "0x0000" as OK
            err_clean = err.lower().replace("0x", "").strip()
            if err_clean and err_clean != "0000" and err_clean != "0":
                info["printer_error"] = err
                info["connected"] = False

    # If we still don't have a model name, try to extract one from the
    # output. Fall back to a generic label.
    if info.get("connected") and not info.get("model"):
        # Look for typical Brother model identifiers
        import re
        m = re.search(r"\bPT[-_]?[A-Z0-9]+\b", out)
        if m:
            info["model"] = m.group(0).replace("_", "-")
        else:
            info["model"] = "P-Touch printer"

    if result.returncode != 0 and not info["connected"]:
        info["error"] = (result.stderr or "ptouch-print returned an error").strip()

    return info


@app.route("/api/printer/status", methods=["GET"])
def printer_status():
    """Returns the current state of the connected tape printer."""
    info = _ptouch_info() or {"connected": False}
    return jsonify(info)


@app.route("/api/print", methods=["POST"])
def print_label():
    """Print one or more labels to the tape printer.

    Body (use one of these shapes):

      Single label:
        { "png_base64": "<data>", "copies": 1 }
        -> ptouch-print --image label.png  (repeated `copies` times)

      Batch (multiple different labels in one job):
        { "pngs_base64": ["<data1>", "<data2>", ...], "copies": 1 }
        -> ptouch-print --image l1.png --image l2.png --image l3.png
        -> One front leader for the whole batch, auto-cut between each label.
    """
    body = request.get_json(silent=True) or {}

    copies = int(body.get("copies", 1) or 1)
    if copies < 1 or copies > 50:
        abort(400, description="copies must be 1-50")

    # Accept either single PNG or list of PNGs
    pngs_b64 = body.get("pngs_base64")
    single_b64 = body.get("png_base64")

    if pngs_b64 is None and single_b64 is None:
        abort(400, description="png_base64 or pngs_base64 is required")

    if pngs_b64 is not None:
        if not isinstance(pngs_b64, list) or not pngs_b64:
            abort(400, description="pngs_base64 must be a non-empty list")
        png_list = pngs_b64
    else:
        png_list = [single_b64]

    if len(png_list) > 100:
        abort(400, description="too many labels in batch (max 100)")

    # Decode each PNG, write to its own temp file
    tmp_paths = []
    try:
        for idx, b64 in enumerate(png_list):
            if "," in b64 and b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            try:
                png_bytes = base64.b64decode(b64)
            except Exception:
                abort(400, description=f"label #{idx+1}: invalid base64")
            if not png_bytes or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
                abort(400, description=f"label #{idx+1}: not a PNG")
            t = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            t.write(png_bytes)
            t.flush()
            t.close()
            tmp_paths.append(t.name)

        # Pre-flight: confirm printer is connected
        info = _ptouch_info()
        if not info.get("connected"):
            return jsonify({
                "ok": False,
                "error": "printer not connected",
                "detail": info,
            }), 503

        # Cut strategy for the farixembedded ptouch-print fork:
        #
        # This fork doesn't have --precut. Cuts work like this:
        #   - Every ptouch-print invocation feeds + auto-cuts at the END.
        #   - --chain skips the final feed/cut so the next label can chain on.
        #   - --cutmark prints a small mark where the user should manually cut.
        #
        # Best UX: print each label as its own invocation so each one gets a
        # clean auto-cut at the end. The printer's mandatory 25mm leader only
        # appears on the very first label of a print session (the printer
        # remembers where it is on the tape). Multi-label batches can group
        # multiple --image flags in one invocation for tighter packing, but
        # that requires manual cutting between them via --cutmark.
        #
        # Single label:           ptouch-print --image L.png
        #   -> prints L, auto-cuts at end
        # Multiple discrete labels (each fully cut):
        #   -> separate ptouch-print invocations per label

        all_results = []
        n = len(tmp_paths)
        for c in range(copies):
            for i, p in enumerate(tmp_paths):
                cmd = [PTOUCH_BIN, "--image", p]
                r = subprocess.run(
                    cmd,
                    capture_output=True, text=True,
                    timeout=60,  # per-label; ptouch-print can take 5-10s
                )
                all_results.append({
                    "copy": c + 1,
                    "label": i + 1,
                    "cmd": " ".join(cmd),
                    "returncode": r.returncode,
                    "stdout": (r.stdout or "").strip(),
                    "stderr": (r.stderr or "").strip(),
                })
                if r.returncode != 0:
                    return jsonify({
                        "ok": False,
                        "error": "ptouch-print failed",
                        "copy": c + 1,
                        "label": i + 1,
                        "results": all_results,
                    }), 500

        return jsonify({
            "ok": True,
            "labels": len(png_list),
            "copies": copies,
            "printer": info.get("model", "unknown"),
            "tape_width_mm": info.get("tape_width_mm"),
        })
    finally:
        for p in tmp_paths:
            try:
                os.unlink(p)
            except Exception:
                pass


# ------------------------------------------------------------------
# Inventory app endpoints
# ------------------------------------------------------------------
# Item types: "container" (default) | "bin" | "unknown" (just printed, no scan yet)
# A container has: description, bin_id (where it lives), quantity, notes, etc.
# A bin has: location (free text), nothing else required.
# The registry holds both kinds in the same JSON. The "type" field
# distinguishes them. Old label-only records have no type → treated as
# "unknown" until first scan.

import time

def _load_registry():
    raw = _read_file("st:registry") or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {}

def _save_registry(reg):
    _write_file("st:registry", json.dumps(reg))

def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@app.route("/api/items/<item_id>", methods=["GET"])
def get_item(item_id):
    """Look up a single item by ID. Returns 404 if not in registry yet.
    v2.0.0: single-row SQL fetch; bin contents via indexed bin_id query.
    v2.1.0: bins list containers AND items; containers list child items;
    items report their parent's friendly info."""
    rec = _item_get(item_id)
    if not rec:
        return jsonify({"id": item_id, "exists": False}), 404
    rec = _decorate_photo_urls(rec)
    # If it's a bin, include everything stored in it (containers + items)
    if rec.get("type") == "bin":
        contents = []
        for row in _db().execute(
                "SELECT data FROM items WHERE bin_id=? AND type IN ('container','item')",
                (item_id,)):
            contents.append(_decorate_photo_urls(json.loads(row["data"])))
        rec = dict(rec)
        rec["contents"] = contents
    # If it's a container, include child items + the bin's friendly info
    elif rec.get("type") == "container":
        rec = dict(rec)
        contents = []
        for row in _db().execute(
                "SELECT data FROM items WHERE bin_id=? AND type='item'",
                (item_id,)):
            contents.append(_decorate_photo_urls(json.loads(row["data"])))
        rec["contents"] = contents
        if rec.get("bin_id"):
            bin_rec = _item_get(rec["bin_id"])
            if bin_rec:
                rec["bin_location"] = bin_rec.get("location", "")
                rec["bin_description"] = bin_rec.get("description")
                rec["bin_lines"] = bin_rec.get("lines")
                rec["bin_location_property"] = bin_rec.get("location_property")
                rec["bin_location_room"] = bin_rec.get("location_room")
                rec["bin_location_spot"] = bin_rec.get("location_spot")
    # If it's a single item, its parent may be a bin OR a container
    elif rec.get("type") == "item" and rec.get("bin_id"):
        parent = _item_get(rec["bin_id"])
        if parent:
            rec = dict(rec)
            rec["bin_location"] = parent.get("location", "")
            rec["bin_description"] = parent.get("description")
            rec["bin_lines"] = parent.get("lines")
            rec["parent_type"] = parent.get("type")
    return jsonify({"id": item_id, "exists": True, "record": rec})


def _parent_summary(parent_id):
    """Resolve a parent (bin or container) to friendly name + location path,
    following one more level up when the parent is a container in a bin."""
    if not parent_id:
        return None
    p = _item_get(parent_id)
    if not p:
        return None
    def name_of(r):
        if r.get("description") and str(r["description"]).strip():
            return str(r["description"]).strip()
        lines = r.get("lines") or []
        if lines and str(lines[0]).strip():
            return str(lines[0]).strip()
        return "(unnamed)"
    def loc_of(r):
        parts = [r.get("location_property"), r.get("location_room"), r.get("location_spot")]
        parts = [str(x).strip() for x in parts if x and str(x).strip()]
        return " · ".join(parts) if parts else (r.get("location") or "")
    out = {"id": parent_id, "type": p.get("type"), "name": name_of(p), "location": loc_of(p)}
    if p.get("type") == "container" and p.get("bin_id"):
        g = _item_get(p["bin_id"])
        if g:
            out["bin_name"] = name_of(g)
            if not out["location"]:
                out["location"] = loc_of(g)
    return out


@app.route("/api/items/similar", methods=["POST"])
def similar_items():
    """Duplicate detection for the quick-add flow. Body: {sku, description}.
    Returns likely-existing matches: exact normalized-SKU hits first (strong
    signal), then FTS keyword matches on the description. Each result carries
    quantity + resolved parent name/location so the UI can say 'already in
    stock — add it to Shelf A'."""
    body = request.get_json(silent=True) or {}
    sku = (body.get("sku") or "").strip()
    desc = (body.get("description") or "").strip()
    d = _db()
    results, seen = [], set()

    def add_row(rec, why):
        if rec["id"] in seen:
            return
        seen.add(rec["id"])
        results.append({
            "id": rec["id"], "type": rec.get("type"),
            "description": rec.get("description"),
            "supplier": rec.get("supplier"),
            "supplier_sku": rec.get("supplier_sku"),
            "quantity": rec.get("quantity"),
            "has_photo": bool(rec.get("has_photo")),
            "thumb": f"/api/items/{rec['id']}/thumb" if rec.get("has_photo") else None,
            "parent": _parent_summary(rec.get("bin_id")),
            "match": why,
        })

    # 1) Exact SKU match (normalized: uppercase, strip spaces/dashes)
    if sku:
        norm = "".join(ch for ch in sku.upper() if ch.isalnum())
        if norm:
            for row in d.execute(
                    "SELECT data FROM items WHERE type IN ('container','item')"):
                rec = json.loads(row["data"])
                rsku = "".join(ch for ch in str(rec.get("supplier_sku") or "").upper()
                               if ch.isalnum())
                if rsku and rsku == norm:
                    add_row(rec, "sku")
    # 2) FTS keyword match on the description
    if desc and len(results) < 5:
        # Build a forgiving OR query from alphanumeric tokens
        tokens = ["".join(ch for ch in t if ch.isalnum())
                  for t in desc.split()]
        tokens = [t for t in tokens if len(t) >= 2][:6]
        if tokens:
            q = " OR ".join(f'"{t}"' for t in tokens)
            try:
                for row in d.execute(
                        """SELECT f.item_id FROM items_fts f
                           JOIN items i ON i.id = f.item_id
                           WHERE items_fts MATCH ? AND i.type IN ('container','item')
                           ORDER BY rank LIMIT 8""", (q,)):
                    rec = _item_get(row["item_id"])
                    if rec:
                        add_row(rec, "keywords")
                    if len(results) >= 5:
                        break
            except sqlite3.OperationalError:
                pass  # malformed FTS query from odd input — no keyword results
    return jsonify({"matches": results[:5]})


@app.route("/api/items/new-id", methods=["POST"])
def mint_item_id():
    """Mint a fresh unused ID (forge alphabet) and reserve it. Used by the
    inventory app's 'Add Item' flow — a part gets a first-class record
    without a printed label. The ID stays printable later from the Forge."""
    import secrets as _secrets
    alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
    d = _db()
    for _ in range(50):
        cand = "".join(_secrets.choice(alphabet) for _ in range(5))
        if not d.execute("SELECT 1 FROM used_ids WHERE id=?", (cand,)).fetchone():
            d.execute("INSERT OR IGNORE INTO used_ids(id) VALUES(?)", (cand,))
            return jsonify({"ok": True, "id": cand})
    abort(500, description="could not mint an unused ID")


@app.route("/api/items/<item_id>/photo", methods=["GET"])
def item_photo(item_id):
    row = _db().execute("SELECT photo FROM item_photos WHERE id=?",
                        (item_id,)).fetchone()
    if not row:
        abort(404)
    resp = app.response_class(row["photo"], mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/items/<item_id>/thumb", methods=["GET"])
def item_thumb(item_id):
    row = _db().execute("SELECT thumb, photo FROM item_photos WHERE id=?",
                        (item_id,)).fetchone()
    if not row:
        abort(404)
    resp = app.response_class(row["thumb"] or row["photo"], mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/items/<item_id>", methods=["PUT", "POST"])
def upsert_item(item_id):
    """Create or update an item. Body is a partial record — fields are
    merged into the existing record. Pass type to set bin vs container."""
    body = request.get_json(silent=True) or {}
    existing = _item_get(item_id) or {}
    # Photo is handled out-of-band (blob table), never merged into the record
    photo_present = "photo" in body
    photo_val = body.pop("photo", None)
    # Merge fields, but never let the client overwrite the id
    merged = {**existing, **body, "id": item_id}
    merged["updated"] = _now_iso()
    if "created" not in merged:
        merged["created"] = _now_iso()
    if photo_present:
        merged = _apply_photo_field(item_id, merged, photo_val)
    # Phase 3: quantity-decrease auditing + mandatory reason in business mode
    reason = body.pop("_reason", None)
    old_qty = existing.get("quantity")
    new_qty = merged.get("quantity")
    qty_changed = ("quantity" in body and old_qty != new_qty)
    if qty_changed and _business_mode():
        decreased = (old_qty is not None and new_qty is not None
                     and isinstance(old_qty, (int, float))
                     and isinstance(new_qty, (int, float))
                     and new_qty < old_qty)
        if decreased and not (reason or "").strip():
            abort(400, description="a reason is required when reducing quantity")
    d = _db()
    d.execute("BEGIN")
    try:
        _item_write(merged)
        d.execute("INSERT OR IGNORE INTO used_ids(id) VALUES(?)", (item_id,))
        _bump_change()
        d.execute("COMMIT")
    except Exception:
        d.execute("ROLLBACK")
        raise
    if qty_changed:
        _audit("qty_adjust", item_id=item_id,
               detail={"from": old_qty, "to": new_qty, "reason": reason})
    elif not existing:
        _audit("item_created", item_id=item_id,
               detail={"type": merged.get("type"), "desc": (merged.get("description") or "")[:60]})
    else:
        _audit("item_edited", item_id=item_id,
               detail={"fields": [k for k in body.keys()][:10]})
    return jsonify({"ok": True, "record": _decorate_photo_urls(merged)})


@app.route("/api/items/<item_id>", methods=["DELETE"])
def delete_item(item_id):
    """Soft-delete: removes the record (and photo) but keeps the ID reserved."""
    d = _db()
    d.execute("BEGIN")
    try:
        d.execute("DELETE FROM items WHERE id=?", (item_id,))
        d.execute("DELETE FROM items_fts WHERE item_id=?", (item_id,))
        d.execute("DELETE FROM item_photos WHERE id=?", (item_id,))
        _bump_change()
        d.execute("COMMIT")
    except Exception:
        d.execute("ROLLBACK")
        raise
    _audit("item_deleted", item_id=item_id)
    return jsonify({"ok": True})


@app.route("/api/items", methods=["GET"])
def list_items():
    """List all items, optionally filtered by ?type=bin|container.
    By default, strips heavyweight fields (photo base64) to keep the
    list response small. Pass ?with_photos=1 to include them."""
    type_filter = request.args.get("type")
    if type_filter:
        rows = _db().execute(
            "SELECT data FROM items WHERE type=? ORDER BY created", (type_filter,))
    else:
        rows = _db().execute("SELECT data FROM items ORDER BY created")
    # v2.0.0: photos are blobs, never inline. Records carry has_photo; we add
    # a thumb URL so list UIs can render thumbnails cheaply.
    items = [_decorate_photo_urls(json.loads(r["data"])) for r in rows]
    return jsonify({"items": items, "count": len(items)})


@app.route("/api/items/lookup", methods=["POST"])
def lookup_items():
    """Bulk lookup. Body: {"ids": ["JN6EE", "FVVMA", ...]}.
    Returns: {"JN6EE": {"exists": true, "type": "container", ...},
              "FVVMA": {"exists": false}, ...}.
    Used by the inventory app to color-code multiple QRs on screen."""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        abort(400, description="ids must be a list")
    if len(ids) > 100:
        abort(400, description="too many ids (max 100)")
    clean = [i.upper().strip() for i in ids if isinstance(i, str)]
    found = {}
    if clean:
        q = ",".join("?" * len(clean))
        for row in _db().execute(f"SELECT id, data FROM items WHERE id IN ({q})", clean):
            found[row["id"]] = json.loads(row["data"])
    out = {}
    for rid in clean:
        rec = found.get(rid)
        if rec:
            out[rid] = {
                "exists": True,
                "type": rec.get("type", "unknown"),
                "description": rec.get("description"),
                "location": rec.get("location"),
                "location_property": rec.get("location_property"),
                "location_room": rec.get("location_room"),
                "location_spot": rec.get("location_spot"),
                "bin_id": rec.get("bin_id"),
                "lines": rec.get("lines"),  # original label lines from Label Forge
            }
        else:
            out[rid] = {"exists": False}
    return jsonify(out)


@app.route("/api/locations", methods=["GET"])
def list_locations():
    """Return autocomplete suggestions for each hierarchical location level.
    Optionally filters by parent field (e.g. rooms_in?property=Home returns
    just the rooms used at the Home property).

    Returns: {
      "properties": [{"name": "Home", "uses": 24}, ...],
      "rooms":      [{"name": "Garage", "uses": 12}, ...],
      "spots":      [{"name": "Shelf 2", "uses": 4}, ...],
      "legacy":     ["Old single-field location 1", ...]  # for migration
    }
    """
    reg = _load_registry()
    props, rooms, spots, legacy_set = {}, {}, {}, {}
    # Optional parent filtering (e.g. ?property=Home returns only rooms at Home)
    filter_prop = (request.args.get("property") or "").strip().lower()
    filter_room = (request.args.get("room") or "").strip().lower()
    for rec in reg.values():
        if rec.get("type") != "bin":
            continue
        p = (rec.get("location_property") or "").strip()
        r = (rec.get("location_room") or "").strip()
        s = (rec.get("location_spot") or "").strip()
        legacy = (rec.get("location") or "").strip()
        if legacy and not (p or r or s):
            legacy_set[legacy] = legacy_set.get(legacy, 0) + 1
        if p:
            props[p] = props.get(p, 0) + 1
        if r and (not filter_prop or p.lower() == filter_prop):
            rooms[r] = rooms.get(r, 0) + 1
        if s and (not filter_prop or p.lower() == filter_prop) \
              and (not filter_room or r.lower() == filter_room):
            spots[s] = spots.get(s, 0) + 1

    def sorted_list(d):
        return [{"name": k, "uses": v}
                for k, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0].lower()))]

    return jsonify({
        "properties": sorted_list(props),
        "rooms":      sorted_list(rooms),
        "spots":      sorted_list(spots),
        "legacy":     sorted_list(legacy_set),
    })


@app.route("/api/stats", methods=["GET"])
def stats():
    """Quick counts for the home page."""
    reg = _load_registry()
    counts = {"total": len(reg), "bin": 0, "container": 0, "unknown": 0}
    homeless = 0  # containers with no bin assigned
    for rec in reg.values():
        t = rec.get("type") or "unknown"
        counts[t] = counts.get(t, 0) + 1
        if t == "container" and not rec.get("bin_id"):
            homeless += 1
    return jsonify({
        "counts": counts,
        "homeless_containers": homeless,
    })


# ------------------------------------------------------------------
# Error handlers (JSON output)
# ------------------------------------------------------------------

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(500)
def _json_error(e):
    return jsonify({
        "error": getattr(e, "name", "error"),
        "message": getattr(e, "description", str(e)),
    }), getattr(e, "code", 500)


# ============================================================================
# WIFI MANAGEMENT (NetworkManager via nmcli)
# ============================================================================
# Lets the user configure WiFi from the web UI so the Pi can run wireless after
# initial ethernet bootstrap. Uses nmcli, which is the default on Pi OS Bookworm.
# Older systems using dhcpcd will see "not supported" responses.
#
# Security:
#   - nmcli is invoked via sudo with a narrow sudoers stub (/etc/sudoers.d/...)
#   - passwords are NEVER logged or echoed back; on error we return generic msgs
#   - enterprise (802.1X) networks are filtered out of scan results

import re
import shlex


def _nmcli(*args, timeout=15, capture_password=False):
    """Run an nmcli subcommand via sudo. Returns CompletedProcess.
    If capture_password is True, the args list contains a password that must
    NEVER be logged or echoed; we redact it from any error output."""
    cmd = ["sudo", "-n", "nmcli"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        return None
    # Redact the password from stderr if it appears
    if capture_password and r.stderr:
        for arg in args:
            if "password" in arg.lower() or len(arg) > 7:
                r.stderr = r.stderr.replace(arg, "***")
    return r


_WIFI_CACHE = {"value": None, "ts": 0, "reason": ""}


def _wifi_available():
    """Check whether nmcli + a wifi device are present.
    Returns (bool, reason_string). The reason explains why if unavailable."""
    import time
    now = time.time()
    if _WIFI_CACHE["value"] is True and now - _WIFI_CACHE["ts"] < 30:
        return True, "ok"
    if _WIFI_CACHE["value"] is False and now - _WIFI_CACHE["ts"] < 5:
        return False, _WIFI_CACHE["reason"]
    r = _nmcli("-t", "-f", "TYPE", "device")
    if r is None:
        reason = "nmcli not installed or subprocess failed (NoNewPrivileges blocks sudo?)"
        _WIFI_CACHE.update(value=False, ts=now, reason=reason)
        return False, reason
    if r.returncode != 0:
        err = (r.stderr or "").strip()[:160]
        if "sudo: a password is required" in err.lower() or "no tty" in err.lower():
            reason = "passwordless sudo for nmcli not configured"
        elif "no new privileges" in err.lower() or "operation not permitted" in err.lower():
            reason = "NoNewPrivileges=true in systemd unit blocks sudo"
        else:
            reason = f"nmcli exit {r.returncode}: {err or 'no error message'}"
        _WIFI_CACHE.update(value=False, ts=now, reason=reason)
        return False, reason
    has_wifi = any(line.strip() == "wifi" for line in r.stdout.splitlines())
    reason = "ok" if has_wifi else "no wifi device in nmcli output"
    _WIFI_CACHE.update(value=has_wifi, ts=now, reason=reason)
    return has_wifi, reason


@app.route("/api/wifi/status", methods=["GET"])
def wifi_status():
    """Current connection state: which network are we on, signal, IP, mode.
    Also detects whether comitup is currently in HOTSPOT (AP) mode, which
    affects how the home page renders."""
    ok, _reason = _wifi_available()
    if not ok:
        return jsonify({"available": False, "reason": _reason})

    # AP-mode detection: comitup running in HOTSPOT state.
    # We try the comitup-cli command first; if it's not installed, fall back
    # to checking for the AP IP on wlan0 (10.41.0.1 is comitup's default).
    ap_mode = False
    ap_ssid = None
    try:
        cr = subprocess.run(
            ["comitup-cli", "i"],
            capture_output=True, text=True, timeout=4,
        )
        if cr.returncode == 0:
            # Output includes lines like "state=HOTSPOT" and "connection=StowTrace-A2F1"
            for line in cr.stdout.splitlines():
                s = line.strip()
                if s.startswith("state=") and "HOTSPOT" in s.upper():
                    ap_mode = True
                if s.startswith("connection=") and ap_mode:
                    ap_ssid = s.split("=", 1)[1].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # comitup-cli not installed (this Pi doesn't have AP bootstrap),
        # or it hung — fall through to address-based detection.
        pass

    if not ap_mode:
        # Address-based fallback: comitup uses 10.41.0.0/24 for its AP
        try:
            ar = subprocess.run(
                ["ip", "-4", "-o", "addr", "show", "dev", "wlan0"],
                capture_output=True, text=True, timeout=3,
            )
            if ar.returncode == 0 and "10.41.0." in ar.stdout:
                ap_mode = True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Active connections (the NAME field is the connection profile name,
    # which on netplan-managed systems looks like "netplan-wlan0-<SSID>" —
    # not the user-facing SSID. We'll resolve the real SSID below.)
    r = _nmcli("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
    if r is None or r.returncode != 0:
        return jsonify({"available": True, "connected": False, "error": "nmcli failed"})

    wifi_profile = None  # the connection profile name (may be netplan-wlan0-XXX)
    ethernet_active = None
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3: continue
        name, ctype, device = parts[0], parts[1], parts[2]
        if ctype == "802-11-wireless":
            wifi_profile = {"profile": name, "device": device}
        elif ctype == "802-3-ethernet":
            ethernet_active = {"name": name, "device": device}

    # Pull current IP for whatever's primary
    ip_addr = None
    try:
        rip = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        ip_addr = (rip.stdout or "").strip().split()[0] if rip.stdout.strip() else None
    except Exception:
        pass

    # Resolve the real SSID and signal % from the active wifi (if any).
    # We use 'device wifi list' which returns the actual broadcast SSID,
    # then filter to the row marked as in-use (*).
    wifi_active = None
    if wifi_profile:
        ssid = None
        signal = None
        rs = _nmcli("-t", "-f", "IN-USE,SIGNAL,SSID", "device", "wifi", "list")
        if rs and rs.returncode == 0:
            for line in rs.stdout.splitlines():
                # Format: IN-USE:SIGNAL:SSID. SSID may have colons (escaped with \)
                fields = re.split(r"(?<!\\):", line, maxsplit=2)
                if len(fields) >= 3 and fields[0].strip() == "*":
                    try:
                        signal = int(fields[1])
                    except ValueError:
                        pass
                    ssid = fields[2].replace("\\:", ":").strip()
                    break

        # Fallback: ask nmcli for the SSID stored in the connection profile
        if not ssid:
            rp = _nmcli("-t", "-f", "802-11-wireless.ssid", "connection", "show", wifi_profile["profile"])
            if rp and rp.returncode == 0:
                out = (rp.stdout or "").strip()
                if ":" in out:
                    ssid = out.split(":", 1)[1].strip()

        # Last resort: strip the netplan- prefix from the profile name
        if not ssid:
            prof = wifi_profile["profile"]
            if prof.startswith("netplan-"):
                # Format is "netplan-<device>-<ssid>", but device name may have hyphens
                # Trim "netplan-" then trim the leading device name
                tail = prof[len("netplan-"):]
                if tail.startswith(wifi_profile["device"] + "-"):
                    ssid = tail[len(wifi_profile["device"]) + 1:]
                else:
                    ssid = tail
            else:
                ssid = prof

        wifi_active = {"ssid": ssid, "device": wifi_profile["device"], "signal": signal}

    return jsonify({
        "available": True,
        "connected": bool(wifi_active or ethernet_active),
        "wifi": wifi_active,
        "ethernet": ethernet_active,
        "ip": ip_addr,
        "ap_mode": ap_mode,
        "ap_ssid": ap_ssid,
    })


@app.route("/api/wifi/scan", methods=["GET"])
def wifi_scan():
    """Scan for nearby networks. Filters out enterprise (802.1X)."""
    ok, _reason = _wifi_available()
    if not ok:
        return jsonify({"available": False, "networks": []})

    # Force a rescan then list
    _nmcli("device", "wifi", "rescan", timeout=8)
    r = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list")
    if r is None or r.returncode != 0:
        return jsonify({"available": True, "networks": [], "error": "scan failed"})

    seen = {}  # dedupe by SSID, keep strongest signal
    for line in r.stdout.splitlines():
        # Fields: SSID:SIGNAL:SECURITY:IN-USE
        # nmcli escapes colons in SSID with backslash; split with negative lookbehind
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 4: continue
        ssid = parts[0].replace("\\:", ":").strip()
        if not ssid or ssid == "--":
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = (parts[2] or "").strip()
        in_use = parts[3].strip() == "*"
        # Skip enterprise/802.1X networks - they need extra setup
        if "802.1X" in security or "WPA-EAP" in security or "EAP" in security:
            continue
        # Dedupe
        if ssid in seen and seen[ssid]["signal"] >= signal:
            continue
        seen[ssid] = {
            "ssid": ssid,
            "signal": signal,
            "security": security or "Open",
            "secured": bool(security and security != "--"),
            "in_use": in_use,
        }

    networks = sorted(seen.values(), key=lambda n: -n["signal"])
    return jsonify({"available": True, "networks": networks})


def _resolve_ssid_for_profile(profile_name, device_name=None):
    """Given a NetworkManager connection profile name, return the actual SSID.
    On netplan-managed systems the profile name is something like
    'netplan-wlan0-MyNetwork' instead of just 'MyNetwork'.

    Strategy: ask nmcli for the profile's 802-11-wireless.ssid setting.
    Fall back to stripping the 'netplan-<device>-' prefix if that fails."""
    # Primary: query nmcli for the stored SSID
    r = _nmcli("-t", "-f", "802-11-wireless.ssid", "connection", "show", profile_name)
    if r and r.returncode == 0:
        # Output is "802-11-wireless.ssid:MyNetwork"
        line = (r.stdout or "").strip()
        if ":" in line:
            ssid = line.split(":", 1)[1].strip()
            if ssid:
                return ssid

    # Fallback: strip netplan prefix
    if profile_name.startswith("netplan-"):
        tail = profile_name[len("netplan-"):]
        if device_name and tail.startswith(device_name + "-"):
            return tail[len(device_name) + 1:]
        # Strip any 'wlanN-' prefix as a generic fallback
        import re as _re
        m = _re.match(r"^wlan\d+-(.+)$", tail)
        if m:
            return m.group(1)
        return tail

    return profile_name


@app.route("/api/wifi/saved", methods=["GET"])
def wifi_saved():
    """List saved (autoconnect) WiFi profiles. Display name is the actual SSID,
    not the netplan-mangled profile name.

    Filters out comitup's internal AP profiles (e.g. 'comitup-203') and any
    hotspot/AP profiles — these are infrastructure, not user-chosen networks,
    and showing them confuses the user."""
    ok, _reason = _wifi_available()
    if not ok:
        return jsonify({"available": False, "networks": []})

    r = _nmcli("-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show")
    if r is None or r.returncode != 0:
        return jsonify({"available": True, "networks": [], "error": "list failed"})

    out = []
    for line in r.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3: continue
        profile_name, ctype, autoconnect = parts[0], parts[1], parts[2]
        if ctype != "802-11-wireless": continue

        # Skip comitup's internal AP profiles
        if profile_name.startswith("comitup-"):
            continue

        # Skip any profile in AP/hotspot mode (we manage those, not the user)
        mr = _nmcli("-t", "-f", "802-11-wireless.mode", "connection", "show", profile_name)
        if mr and mr.returncode == 0:
            mode = (mr.stdout or "").strip().split(":", 1)[-1].lower()
            if mode in ("ap", "adhoc"):
                continue

        ssid = _resolve_ssid_for_profile(profile_name)
        out.append({
            "name": ssid,                # what we DISPLAY to the user
            "profile": profile_name,     # what we PASS BACK for forget/edit ops
            "autoconnect": autoconnect.lower() == "yes",
        })
    return jsonify({"available": True, "networks": out})


@app.route("/api/ap/config", methods=["GET"])
def ap_config_get():
    """Return the current AP (hotspot) SSID. Never returns the password."""
    ok, _reason = _wifi_available()
    if not ok:
        return jsonify({"available": False})
    ssid = None
    r = _nmcli("-t", "-f", "802-11-wireless.ssid", "connection", "show", "store-ap")
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            if ":" in line:
                ssid = line.split(":", 1)[1].strip()
                break
    return jsonify({"available": True, "ssid": ssid or "stowtrace"})


@app.route("/api/ap/config", methods=["POST"])
def ap_config_set():
    """Change the AP hotspot SSID and/or password, then reboot to apply.

    Safety: we VALIDATE everything first and only modify the connection after
    all checks pass, so a bad input can never leave the AP half-configured.
    The change is applied to the 'store-ap' connection created by
    store-ap-setup.sh. A delayed reboot (via systemd-run) lets us return a
    response before the box goes down.
    """
    ok, _reason = _wifi_available()
    if not ok:
        abort(400, description="WiFi not available on this device")

    body = request.get_json(silent=True) or {}
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""

    # --- validate BEFORE touching anything ---
    if not ssid:
        abort(400, description="ssid required")
    if len(ssid) > 32:
        abort(400, description="ssid too long (max 32 chars for WiFi)")
    if len(password) < 8 or len(password) > 63:
        abort(400, description="AP password must be 8-63 characters (WPA2 requirement)")

    # Confirm the store-ap connection exists before we try to modify it.
    check = _nmcli("-t", "-f", "connection.id", "connection", "show", "store-ap")
    if not check or check.returncode != 0:
        return jsonify({"ok": False, "error": "AP is not set up yet (run store-ap-setup.sh first)"}), 400

    # --- apply: modify the existing store-ap connection ---
    r = _nmcli(
        "connection", "modify", "store-ap",
        "802-11-wireless.ssid", ssid,
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", password,
        timeout=15, capture_password=True,
    )
    if r is None or r.returncode != 0:
        err = (r.stderr if r else "nmcli unavailable") or "AP update failed"
        return jsonify({"ok": False, "error": err.strip()}), 500

    # --- schedule reboot so the new AP settings take effect cleanly ---
    try:
        rb = subprocess.run(
            ["sudo", "-n", "systemd-run",
             "--unit=st-reboot-runner", "--collect", "--no-block",
             "--on-active=5", "/sbin/reboot"],
            capture_output=True, text=True, timeout=10,
        )
        if rb.returncode != 0:
            subprocess.Popen(
                ["sudo", "-n", "/sbin/reboot"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "ssid": ssid,
        "rebooting": True,
        "message": "AP updated. The device will reboot in 5 seconds. Reconnect to the new WiFi name.",
    })


@app.route("/api/wifi/connect", methods=["POST"])
def wifi_connect():
    """Save WiFi credentials and reboot. We don't try to live-switch from
    AP mode -> client mode because that race-conditions with comitup's own
    state machine and frequently leaves the Pi stuck mid-transition.

    Instead:
      1. Save (or create) the connection profile for this SSID + password
      2. Mark it autoconnect=yes so comitup picks it up on next boot
      3. Schedule a reboot in 5 seconds (via systemd-run so we can return
         a response to the captive portal before going down)
      4. On reboot, comitup sees the saved profile and connects to it.
         AP mode goes away naturally.

    The password is sent over HTTPS only. We never log or echo it.
    """
    ok, _reason = _wifi_available()
    if not ok:
        abort(400, description="WiFi not available on this device")

    body = request.get_json(silent=True) or {}
    ssid = (body.get("ssid") or "").strip()
    password = body.get("password") or ""
    if not ssid:
        abort(400, description="ssid required")
    if len(ssid) > 64:
        abort(400, description="ssid too long")
    if password and (len(password) < 8 or len(password) > 128):
        abort(400, description="password length must be 8-128 chars (WPA requirement)")

    # Step 1: Clean up any existing profile for this SSID so we build fresh.
    # This avoids nmcli's "key-mgmt property is missing" error which happens
    # when there's a partial profile from a previous attempt.
    list_r = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    if list_r and list_r.returncode == 0:
        for line in list_r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 2: continue
            profile_name, ctype = parts[0], parts[1]
            if ctype != "802-11-wireless": continue
            # Don't touch comitup's own AP profiles
            if profile_name.startswith("comitup-"): continue
            existing_ssid = _resolve_ssid_for_profile(profile_name)
            if existing_ssid == ssid:
                _nmcli("connection", "delete", profile_name, timeout=10)

    # Step 2: Add a new connection profile. This stores the SSID and password
    # but doesn't activate it. We choose security type WPA-PSK if a password
    # was provided, none otherwise.
    add_args = [
        "connection", "add",
        "type", "wifi",
        "ifname", "wlan0",
        "con-name", ssid,
        "ssid", ssid,
        "connection.autoconnect", "yes",
        "connection.autoconnect-priority", "10",
    ]
    if password:
        add_args += [
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", password,
        ]
    r = _nmcli(*add_args, timeout=15, capture_password=True)
    if r is None or r.returncode != 0:
        err = (r.stderr if r else "nmcli unavailable") or "profile add failed"
        return jsonify({"ok": False, "error": err.strip()}), 500

    # Step 3: Schedule a reboot in 5 seconds. systemd-run makes the reboot
    # command outlive the gunicorn worker so we can return our JSON response
    # to the captive portal browser first.
    try:
        rb = subprocess.run(
            ["sudo", "-n", "systemd-run",
             "--unit=st-reboot-runner",
             "--collect",
             "--no-block",
             "--on-active=5",   # delay 5 seconds before firing
             "/sbin/reboot"],
            capture_output=True, text=True, timeout=10,
        )
        if rb.returncode != 0:
            # Fallback: try direct reboot without delay if systemd-run failed
            subprocess.Popen(
                ["sudo", "-n", "/sbin/reboot"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "ssid": ssid,
        "rebooting": True,
        "message": "Saved. The Pi will reboot in 5 seconds and connect to your WiFi.",
    })


@app.route("/api/wifi/saved/<path:ssid>", methods=["DELETE"])
def wifi_forget(ssid):
    """Forget a saved WiFi network. The URL param is treated as either the
    nmcli profile name OR the SSID — we try the literal value first, and if
    that doesn't match anything, we look up the profile by SSID."""
    ok, _reason = _wifi_available()
    if not ok:
        abort(400, description="WiFi not available")
    target = ssid.strip()
    if not target:
        abort(400, description="ssid required")

    # Try the literal target first (might be the profile name already)
    r = _nmcli("connection", "delete", target, timeout=10)
    if r and r.returncode == 0:
        return jsonify({"ok": True})

    # That failed - the target might be the SSID, so resolve to a profile name
    list_r = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    if list_r and list_r.returncode == 0:
        for line in list_r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 2: continue
            profile_name, ctype = parts[0], parts[1]
            if ctype != "802-11-wireless": continue
            if _resolve_ssid_for_profile(profile_name) == target:
                r2 = _nmcli("connection", "delete", profile_name, timeout=10)
                if r2 and r2.returncode == 0:
                    return jsonify({"ok": True})

    err = (r.stderr if r else "not found") or "delete failed"
    return jsonify({"ok": False, "error": err.strip()}), 400


# ============================================================================
# UPDATE SYSTEM
# ============================================================================
# The Pi keeps a persistent git clone at /opt/stowtrace/src/. We can check
# whether origin/main has commits we don't have, and apply them via update.sh.
#
# Update state is cached at /var/lib/stowtrace/update-cache.json so the home
# page doesn't have to hit GitHub on every load. A daily cron refreshes it,
# but users can force a check via /api/system/update-check?refresh=1.

SRC_DIR = "/opt/stowtrace/src"
UPDATE_CACHE = DATA_DIR / "update-cache.json"
UPDATE_SCRIPT = "/opt/stowtrace/update.sh"


def _git(*args, cwd=SRC_DIR, timeout=15):
    """Run a git subcommand in the src dir. Returns CompletedProcess or None."""
    try:
        return subprocess.run(
            ["git"] + list(args),
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _git_short(commit):
    return (commit or "")[:7]


def _read_update_cache():
    """Read the cached update state. Returns dict or None."""
    try:
        with open(UPDATE_CACHE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_update_cache(data):
    """Persist update state to disk."""
    try:
        with open(UPDATE_CACHE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _check_for_updates(do_fetch=True):
    """Probe the local git clone for available updates.
    Returns a dict with the current state, suitable for caching and serving
    to the home page."""
    import time

    if not Path(SRC_DIR).exists():
        return {
            "available": False,
            "supported": False,
            "reason": "No git source directory at /opt/stowtrace/src — install via newer install.sh",
            "checked_at": int(time.time()),
        }

    if do_fetch:
        # Try to fetch the latest refs from origin. If offline, we'll fall
        # back to whatever the last fetch saw.
        _git("fetch", "--quiet", "origin", timeout=20)

    r_local = _git("rev-parse", "HEAD")
    r_remote = _git("rev-parse", "origin/main")
    if not r_local or not r_remote or r_local.returncode != 0 or r_remote.returncode != 0:
        return {
            "available": False,
            "supported": True,
            "reason": "Could not read git refs (no network?)",
            "checked_at": int(time.time()),
        }

    local = r_local.stdout.strip()
    remote = r_remote.stdout.strip()

    # How many commits is local behind?
    r_count = _git("rev-list", "--count", f"{local}..{remote}")
    behind = 0
    if r_count and r_count.returncode == 0:
        try:
            behind = int(r_count.stdout.strip())
        except ValueError:
            behind = 0

    # Try to pull a remote version string from backend file if it differs.
    # We grep for the APP_VERSION assignment in the remote file via git show.
    remote_version = None
    r_show = _git("show", f"origin/main:app/backend/stowtrace_backend.py")
    if r_show and r_show.returncode == 0:
        import re as _re
        m = _re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', r_show.stdout, _re.MULTILINE)
        if m:
            remote_version = m.group(1)

    return {
        "available": behind > 0,
        "supported": True,
        "current_hash": _git_short(local),
        "latest_hash": _git_short(remote),
        "commits_behind": behind,
        "current_version": APP_VERSION,
        "latest_version": remote_version or APP_VERSION,
        "checked_at": int(time.time()),
    }


@app.route("/api/system/update-check", methods=["GET"])
def system_update_check():
    """Return cached update status, or refresh if ?refresh=1."""
    refresh = request.args.get("refresh", "").lower() in ("1", "true", "yes")
    if refresh:
        data = _check_for_updates(do_fetch=True)
        _write_update_cache(data)
        return jsonify(data)
    # Serve from cache if recent (less than 24h), else refresh
    cache = _read_update_cache()
    import time
    if cache and (int(time.time()) - cache.get("checked_at", 0)) < 86400:
        cache["from_cache"] = True
        return jsonify(cache)
    data = _check_for_updates(do_fetch=True)
    _write_update_cache(data)
    return jsonify(data)


@app.route("/api/system/update", methods=["POST"])
def system_update():
    """Run update.sh. The service will restart mid-call so the response
    may never reach the client — that's expected. The client should poll
    /api/health afterwards to detect when it comes back up.

    We use systemd-run to launch update.sh as a transient unit so it
    survives the systemctl restart that update.sh itself triggers. If we
    spawned with subprocess.Popen, update.sh would be a child of gunicorn,
    and gunicorn dies when the service restarts — killing update.sh
    half-way through (silent failure).
    """
    if not Path(UPDATE_SCRIPT).exists():
        return jsonify({"ok": False, "error": "update.sh not present — run a fresh install"}), 500

    # systemd-run --unit=... creates a transient service that's owned by
    # systemd (PID 1), so it can outlive the gunicorn worker that requested it.
    # --collect cleans up the unit after it finishes.
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemd-run",
             "--unit=st-update-runner",
             "--collect",
             "--no-block",
             "/bin/bash", UPDATE_SCRIPT],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": f"systemd-run failed: {r.stderr.strip()}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Clear the cache so next check picks up the new version
    try:
        UPDATE_CACHE.unlink()
    except FileNotFoundError:
        pass

    return jsonify({"ok": True, "message": "Update started — service will restart in ~10s"})


# ---- Storage engine startup (must run after all helpers are defined) ----
_db_init()
_auth_init()
_migrate_from_json()


if __name__ == "__main__":
    # For development only. In production, use gunicorn (see systemd unit).
    app.run(host="127.0.0.1", port=8765, debug=False)
