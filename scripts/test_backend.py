#!/usr/bin/env python3
"""Phase 0 verification suite for the SQLite storage migration.
Simulates: legacy JSON data (incl. an inline base64 photo) -> startup
migration -> full API exercise -> backup -> reset -> restore round-trip.
"""
import base64
import io
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="st-test-")
os.environ["ST_DATA_DIR"] = TMP

# ---- Build a tiny real JPEG for the photo fixture ----
from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (300, 300), (200, 40, 30)).save(buf, format="JPEG")
PHOTO_URL = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

# ---- Legacy JSON fixtures (what a v1.19 installation looks like on disk) ----
legacy_registry = {
    "AAAAA": {"id": "AAAAA", "type": "bin", "description": "box 1",
              "location": "Home · Cave · Stack", "lines": ["box 1"],
              "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z"},
    "BBBBB": {"id": "BBBBB", "type": "container", "description": "Spektrum IC5 plugs",
              "bin_id": "AAAAA", "quantity": 4, "supplier": "Spektrum",
              "supplier_sku": "SPMXCA501", "sub_items": [{"name": "IC5 male", "qty": 2}],
              "photo": PHOTO_URL,
              "created": "2026-01-02T00:00:00Z", "updated": "2026-01-02T00:00:00Z"},
    "CCCCC": {"id": "CCCCC", "type": "container", "description": "M3 bolts",
              "bin_id": "AAAAA", "category_id": "cat_11112222",
              "attributes": {"thread": "M3", "length_mm": 10},
              "created": "2026-01-03T00:00:00Z", "updated": "2026-01-03T00:00:00Z"},
}
legacy_used = ["AAAAA", "BBBBB", "CCCCC", "ZZZZZ"]  # ZZZZZ = reserved, no record
legacy_categories = {
    "cat_11112222": {"id": "cat_11112222", "name": "Bolts", "parent_id": None,
                     "attributes": [{"key": "thread", "label": "Thread", "type": "select",
                                     "values": ["M3", "M4"]}]},
}
with open(f"{TMP}/st_registry.json", "w") as f:   json.dump(legacy_registry, f)
with open(f"{TMP}/st_used.json", "w") as f:       json.dump(legacy_used, f)
with open(f"{TMP}/st_categories.json", "w") as f: json.dump(legacy_categories, f)
with open(f"{TMP}/st_config.json", "w") as f:     json.dump({"qrMode": "combined"}, f)

# ---- Import the backend (triggers _db_init + migration) ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stowtrace_backend as gb
c = gb.app.test_client()

PASS, FAIL = 0, 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ok  {name}")
    else:    FAIL += 1; print(f"  FAIL {name} {extra}")

print("== migration ==")
check("legacy files parked", os.path.exists(f"{TMP}/st_registry.json.pre-sqlite")
      and not os.path.exists(f"{TMP}/st_registry.json"))
r = c.get("/api/items"); j = r.get_json()
check("3 items migrated", j["count"] == 3, j)
recs = {i["id"]: i for i in j["items"]}
check("photo extracted to blob (no base64 in list)", 
      "photo" not in recs["BBBBB"] or not str(recs["BBBBB"].get("photo","")).startswith("data:"))
check("has_photo flag set", recs["BBBBB"].get("has_photo") is True)
check("thumb URL provided in list", str(recs["BBBBB"].get("thumb","")).startswith("/api/items/BBBBB/thumb"))
check("config JSON untouched", os.path.exists(f"{TMP}/st_config.json"))

print("== photo serving ==")
r = c.get("/api/items/BBBBB/photo")
check("photo served as jpeg", r.status_code == 200 and r.data[:2] == b"\xff\xd8", r.status_code)
orig_len = len(r.data)
r = c.get("/api/items/BBBBB/thumb")
check("thumb served + smaller than original", r.status_code == 200 and 0 < len(r.data) < orig_len,
      f"thumb={len(r.data)} orig={orig_len}")
check("no photo -> 404", c.get("/api/items/CCCCC/photo").status_code == 404)

print("== single item GET ==")
j = c.get("/api/items/BBBBB").get_json()
check("container has bin info", j["record"].get("bin_location") == "Home · Cave · Stack")
check("photo URL on detail", str(j["record"].get("photo","")).startswith("/api/items/BBBBB/photo"))
j = c.get("/api/items/AAAAA").get_json()
check("bin contents populated", len(j["record"].get("contents", [])) == 2)
check("missing item 404", c.get("/api/items/XXXXX").status_code == 404)

print("== upsert / merge / photo intercept ==")
h0 = c.get("/api/registry-hash").get_json()["hash"]
r = c.put("/api/items/DDDDD", json={"type": "container", "description": "new thing",
                                    "bin_id": "AAAAA", "photo": PHOTO_URL})
j = r.get_json()
check("upsert ok + photo intercepted", j["ok"] and j["record"].get("has_photo") is True)
check("upsert returns URL not base64", str(j["record"].get("photo","")).startswith("/api/"))
check("photo blob exists", c.get("/api/items/DDDDD/photo").status_code == 200)
h1 = c.get("/api/registry-hash").get_json()["hash"]
check("change counter bumped", h0 != h1, f"{h0} vs {h1}")
r = c.put("/api/items/DDDDD", json={"quantity": 9})
check("merge keeps old fields", r.get_json()["record"]["description"] == "new thing")
check("merge keeps photo", r.get_json()["record"].get("has_photo") is True)
r = c.put("/api/items/DDDDD", json={"photo": None})
check("photo:null deletes blob", c.get("/api/items/DDDDD/photo").status_code == 404
      and r.get_json()["record"].get("has_photo") is False)
# URL echo must be a no-op
c.put("/api/items/BBBBB", json={"photo": "/api/items/BBBBB/photo?v=x"})
check("URL echo is no-op", c.get("/api/items/BBBBB/photo").status_code == 200)

print("== lookup / locations / stats / export ==")
j = c.post("/api/items/lookup", json={"ids": ["bbbbb", "XXXXX"]}).get_json()
check("lookup mixed", j["BBBBB"]["exists"] is True and j["XXXXX"]["exists"] is False)
check("locations endpoint alive", c.get("/api/locations").status_code == 200)
check("stats endpoint alive", c.get("/api/stats").status_code == 200)
j = c.get("/api/export").get_json()
check("export has 4 items", len(j.get("items", [])) == 4, j.get("items"))

print("== KV blob compatibility (forge path) ==")
r = c.get("/api/kv/st:registry"); j = r.get_json()
reg = json.loads(j["value"])
check("kv GET reconstructs registry", len(reg) == 4 and "BBBBB" in reg)
check("kv registry has no base64 photos", not str(reg["BBBBB"].get("photo","")).startswith("data:"))
reg["EEEEE"] = {"id": "EEEEE", "lines": ["forge label"], "qr": True}
r = c.put("/api/kv/st:registry", json={"value": json.dumps(reg)})
check("kv PUT full replace ok", r.get_json().get("ok") is True)
check("kv-added item visible via /api/items", c.get("/api/items/EEEEE").status_code == 200)
check("photo survives full registry replace", c.get("/api/items/BBBBB/photo").status_code == 200)
j = c.get("/api/kv/st:used").get_json()
check("used ids include reserved-only ZZZZZ", "ZZZZZ" in json.loads(j["value"]))

print("== categories ==")
j = c.get("/api/categories").get_json()
check("migrated category present", "cat_11112222" in j["categories"])
check("category counts computed", j["counts"].get("cat_11112222") == 1, j["counts"])
r = c.post("/api/categories", json={"name": "Electronics", "parent_id": None})
new_cat = r.get_json()["category"]["id"]
check("category create", r.status_code == 200)
r = c.put(f"/api/categories/{new_cat}", json={"name": "Electro"})
check("category rename", r.get_json()["category"]["name"] == "Electro")
r = c.delete(f"/api/categories/{new_cat}")
check("category delete", r.get_json()["ok"] is True)

print("== FTS index populated (Phase 1 foundation) ==")
d = gb._db()
row = d.execute("SELECT item_id FROM items_fts WHERE items_fts MATCH 'spektrum'").fetchone()
check("FTS finds by supplier", row and row["item_id"] == "BBBBB")
row = d.execute("SELECT item_id FROM items_fts WHERE items_fts MATCH 'M3'").fetchone()
check("FTS finds by attribute value", row and row["item_id"] == "CCCCC")

print("== backup -> reset -> restore round-trip ==")
r = c.get("/api/system/backup")
bk = json.loads(r.data)
check("backup schema v1 kept", bk["schema"] == "stowtrace-backup/v1")
check("backup includes items", len(bk["data"]["st:registry"]) == 5)
check("backup includes photo map", "BBBBB" in bk["data"].get("st:photos", {}))
check("backup registry has no inline base64", 
      not str(bk["data"]["st:registry"]["BBBBB"].get("photo","")).startswith("data:"))
c.post("/api/reset", json={"confirm": True})
check("reset emptied items", c.get("/api/items").get_json()["count"] == 0)
check("reset emptied photos", c.get("/api/items/BBBBB/photo").status_code == 404)
r = c.post("/api/system/restore", json=bk)
j = r.get_json()
check("restore items", j["added_items"] == 5, j)
check("restore photos", j.get("added_photos") == 1, j)
check("restored photo serves", c.get("/api/items/BBBBB/photo").status_code == 200)
check("restored categories", "cat_11112222" in c.get("/api/categories").get_json()["categories"])
check("restored thumb regenerated", c.get("/api/items/BBBBB/thumb").status_code == 200)

print("== legacy v1 backup (inline photos) restores on v2 ==")
v1_backup = {"schema": "stowtrace-backup/v1", "data": {
    "st:registry": {"FFFFF": {"id": "FFFFF", "type": "container",
                                "description": "legacy item", "photo": PHOTO_URL}},
    "st:used": ["FFFFF"]}}
j = c.post("/api/system/restore", json=v1_backup).get_json()
check("v1 item merged", j["added_items"] == 1, j)
check("v1 inline photo extracted", c.get("/api/items/FFFFF/photo").status_code == 200)
rec = c.get("/api/items/FFFFF").get_json()["record"]
check("v1 item has_photo set", rec.get("has_photo") is True)


print("== v2.1.0: single items ==")
r = c.post("/api/items/new-id"); j = r.get_json()
check("mint returns id", j.get("ok") and len(j.get("id","")) == 5, j)
mint1 = j["id"]
check("minted id reserved", mint1 in json.loads(c.get("/api/kv/st:used").get_json()["value"]))
# item directly in a bin
c.put(f"/api/items/{mint1}", json={"type": "item", "bin_id": "AAAAA",
                                   "description": "lone servo", "quantity": 1})
j = c.get("/api/items/AAAAA").get_json()
kinds = {x["id"]: x.get("type") for x in j["record"]["contents"]}
check("bin contents include item", kinds.get(mint1) == "item", kinds)
# item inside a container
mint2 = c.post("/api/items/new-id").get_json()["id"]
c.put(f"/api/items/{mint2}", json={"type": "item", "bin_id": "BBBBB",
                                   "description": "spare pinion"})
j = c.get("/api/items/BBBBB").get_json()
ids_in = [x["id"] for x in j["record"].get("contents", [])]
check("container lists child item", mint2 in ids_in, ids_in)
j = c.get(f"/api/items/{mint2}").get_json()
check("item reports parent_type container", j["record"].get("parent_type") == "container", j["record"].get("parent_type"))
row = gb._db().execute("SELECT item_id FROM items_fts WHERE items_fts MATCH 'pinion'").fetchone()
check("FTS finds new item", row and row["item_id"] == mint2)

print("== v2.2.0: duplicate detection ==")
c.put("/api/items/DUPE1", json={"type": "item", "bin_id": "AAAAA", "description": "Traxxas 2075 waterproof servo", "supplier": "Traxxas", "supplier_sku": "TRA-2075", "quantity": 3})
j = c.post("/api/items/similar", json={"sku": "tra2075", "description": ""}).get_json()
check("normalized SKU match", len(j["matches"]) == 1 and j["matches"][0]["id"] == "DUPE1" and j["matches"][0]["match"] == "sku", j)
check("match carries parent location", j["matches"][0]["parent"] and j["matches"][0]["parent"]["id"] == "AAAAA", j["matches"][0].get("parent"))
j = c.post("/api/items/similar", json={"sku": "", "description": "waterproof servo"}).get_json()
check("keyword match finds it", any(m["id"] == "DUPE1" for m in j["matches"]), j)
j = c.post("/api/items/similar", json={"sku": "NOPE999", "description": "zzzz qqqq"}).get_json()
check("no false positives", len(j["matches"]) == 0, j)

print("== v2.4.0 Phase 1: server-side search ==")
j = c.get("/api/search?q=spektrum").get_json()
check("search finds by brand", any(r["id"] == "BBBBB" for r in j["results"]), j)
check("search result has parent summary", any(r.get("parent") for r in j["results"]))
j = c.get("/api/search?q=waterproof+servo").get_json()
check("multi-token AND search", any(r["id"] == "DUPE1" for r in j["results"]))
j = c.get("/api/search?q=zz9qq8").get_json()
check("search no false hits", j["count"] == 0)
j = c.get("/api/search?q=servo&limit=1").get_json()
check("search limit respected", j["count"] <= 1)

print("== v2.4.0 Phase 2: locations tree ==")
r = c.post("/api/loctree", json={"name": "Store", "parent_id": None}).get_json()
store_id = r["location"]["id"]
r = c.post("/api/loctree", json={"name": "East Wall", "parent_id": store_id}).get_json()
wall_id = r["location"]["id"]
check("loctree create nested", r["ok"])
j = c.get("/api/loctree").get_json()
check("loctree lists both", store_id in j["locations"] and wall_id in j["locations"])
c.put("/api/items/AAAAA", json={"location_id": wall_id})
j = c.get("/api/loctree").get_json()
check("bin counts per location", j["counts"].get(wall_id) == 1, j["counts"])
r = c.delete(f"/api/loctree/{store_id}")
check("delete with children blocked", r.status_code == 409)
r = c.delete(f"/api/loctree/{store_id}?promote_children=true")
check("promote children delete", r.get_json()["ok"])
j = c.get("/api/loctree").get_json()
check("child promoted to root", j["locations"][wall_id]["parent_id"] is None)

print("== v2.4.0 Phase 3: home mode = zero enforcement ==")
j = c.get("/api/auth/me").get_json()
check("home mode reported", j["business_mode"] is False and j["user"] is None)
r = c.put("/api/items/AAAAA", json={"notes": "no auth needed"})
check("mutations open in home mode", r.status_code == 200)
r = c.put("/api/items/DUPE1", json={"quantity": 1})
check("qty decrease needs no reason in home mode", r.status_code == 200)

print("== v2.4.0 Phase 3: business mode ==")
r = c.post("/api/auth/setup", json={"username": "kenny", "pin": "1234"})
check("owner setup", r.status_code == 200 and r.get_json()["user"]["role"] == "owner")
j = c.get("/api/auth/me").get_json()
check("business mode on + session via cookie", j["business_mode"] and j["user"]["username"] == "kenny")
r = c.post("/api/auth/setup", json={"username": "evil", "pin": "9999"})
check("second setup blocked", r.status_code == 403)
# create users
r = c.post("/api/users", json={"username": "mgr", "pin": "2222", "role": "manager"})
check("owner creates manager", r.status_code == 200)
r = c.post("/api/users", json={"username": "emp", "pin": "3333", "role": "employee"})
check("owner creates employee", r.status_code == 200)
# logged-out client is blocked from mutations
c2 = gb.app.test_client()
r = c2.put("/api/items/AAAAA", json={"notes": "anon"})
check("anon mutation blocked", r.status_code == 401)
r = c2.get("/api/items/AAAAA")
check("anon read open (kiosk)", r.status_code == 200)
r = c2.post("/api/auth/login", json={"username": "emp", "pin": "3333"})
check("employee login", r.status_code == 200)
r = c2.put("/api/items/AAAAA", json={"notes": "emp edit"})
check("employee can edit", r.status_code == 200)
r = c2.delete("/api/items/DUPE1")
check("employee cannot delete", r.status_code == 403)
r = c2.post("/api/reset", json={"confirm": True})
check("employee cannot reset", r.status_code == 403)
r = c2.put("/api/items/DUPE1", json={"quantity": 0})
check("qty decrease without reason blocked", r.status_code == 400)
r = c2.put("/api/items/DUPE1", json={"quantity": 0, "_reason": "damaged in shipping"})
check("qty decrease with reason ok", r.status_code == 200)
r = c2.get("/api/audit")
check("employee cannot read audit", r.status_code == 403)
r = c.get("/api/audit").get_json()
check("owner reads audit", any(e["action"] == "qty_adjust" for e in r["entries"]))
adj = [e for e in r["entries"] if e["action"] == "qty_adjust"][0]
check("audit has user attribution", adj["user"] == "emp", adj)
check("audit captured reason", "damaged" in (adj["detail"] or ""), adj)
r = c2.post("/api/auth/login", json={"username": "emp", "pin": "0000"})
check("bad pin rejected", r.status_code == 401)
r = c.put("/api/users/emp", json={"active": False})
check("owner disables user", r.status_code == 200)
r = c2.put("/api/items/AAAAA", json={"notes": "after disable"})
check("disabled user session revoked", r.status_code == 401)

print("== v2.4.0 Phase 3.5: reports ==")
j = c.get("/api/reports/adjustments").get_json()
check("leaderboard has emp", any(row["user"] == "emp" for row in j["adjustment_leaderboard"]), j["adjustment_leaderboard"])
check("recent adjustments listed", len(j["recent_adjustments"]) >= 1)
r = c2.post("/api/auth/login", json={"username": "mgr", "pin": "2222"})
r = c2.get("/api/reports/adjustments")
check("manager cannot read reports", r.status_code == 403)

print(f"\n=== FINAL {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
