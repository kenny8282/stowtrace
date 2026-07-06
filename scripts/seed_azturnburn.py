#!/usr/bin/env python3
"""AZ Turn & Burn full-catalog import with auto-categorization.

Run ON the VM:  /opt/stowtrace/venv/bin/python3 seed_azturnburn.py
Options:
  --pages N          feed pages to scan, 250 products each (default 20)
  --no-photos        skip image downloads entirely
  --with-photoless   also import products that have no image on the site

What it does:
  1. Ensures a category tree exists (Vehicles/Electronics/Wheels/Bodies/Parts
     with leaves like RTR Cars & Trucks, Motors, Batteries...) — reused by
     name on re-runs, never duplicated.
  2. Scans the whole Shopify catalog, bucketing each product into a leaf.
  3. Extracts filterable attributes from titles: scale (1/10), motor KV,
     battery cells (3S) and capacity (5000mAh).
  4. Imports every bucketed product with photo, brand, SKU, price.
     Products whose SKU already exists are UPDATED in place with
     category + attributes (photos untouched) — safe, idempotent re-runs.
"""
import base64
import html
import io
import json
import re
import socket
import sys
import time
import urllib.request

# Force IPv4 — the VM's IPv6 route black-holes some CDNs (connect() hangs).
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only

BASE = "http://localhost/api"
SHOP = "https://www.azturnandburn.com"
UA = {"User-Agent": "Mozilla/5.0 (inventory-demo-import)"}

PAGES = 20
WITH_PHOTOS = True
INCLUDE_PHOTOLESS = False
for i, a in enumerate(sys.argv):
    if a == "--pages" and i + 1 < len(sys.argv):
        PAGES = int(sys.argv[i + 1])
    if a == "--no-photos":
        WITH_PHOTOS = False
    if a == "--with-photoless":
        INCLUDE_PHOTOLESS = True

# ---- Category tree + bucketing rules ---------------------------------------
SCALE_ATTR = {"key": "scale", "label": "Scale", "type": "select",
              "values": ["1/5", "1/8", "1/10", "1/12", "1/16", "1/18", "1/24"]}
TREE = {
    "Vehicles": {
        "RTR Cars & Trucks": [SCALE_ATTR],
        "Kits & Rollers": [SCALE_ATTR],
        "Boats": [],
        "Air": [],
    },
    "Electronics": {
        "Motors": [{"key": "kv", "label": "KV", "type": "number"}, SCALE_ATTR],
        "ESCs": [SCALE_ATTR],
        "Servos": [],
        "Radios & Receivers": [],
        "Batteries": [{"key": "cells", "label": "Cells (S)", "type": "number"},
                      {"key": "mah", "label": "Capacity (mAh)", "type": "number"}],
        "Chargers": [],
    },
    "Wheels & Tires": {"__leaf__": [SCALE_ATTR]},
    "Bodies": {"__leaf__": [SCALE_ATTR]},
    "Parts & Hop-Ups": {"__leaf__": [SCALE_ATTR]},
}

# First matching rule wins — vehicles outrank their component keywords.
RULES = [
    (("rtr",), ("Vehicles", "RTR Cars & Trucks")),
    (("boat", "pro boat", "proboat"), ("Vehicles", "Boats")),
    (("plane", "e-flite", "heli", "airplane", "edf"), ("Vehicles", "Air")),
    (("build kit", "race kit", "roller", "builders kit"), ("Vehicles", "Kits & Rollers")),
    (("charger",), ("Electronics", "Chargers")),
    (("lipo", "battery", "nimh", "mah"), ("Electronics", "Batteries")),
    (("brushless motor", "brushed motor", " motor"), ("Electronics", "Motors")),
    (("esc", "speed control"), ("Electronics", "ESCs")),
    (("servo",), ("Electronics", "Servos")),
    (("transmitter", "receiver", "radio system", "gyro"), ("Electronics", "Radios & Receivers")),
    (("tire", "tyre", "wheel"), ("Wheels & Tires",)),
    (("body", "bodies"), ("Bodies",)),
    (("shock", "gear", "diff", "axle", "arm", "chassis", "bearing", "pinion",
      "spur", "driveshaft", "bumper", "link", "hinge", "turnbuckle", "mount",
      "screw", "spring", "tool"), ("Parts & Hop-Ups",)),
]


def extract_attrs(title):
    """Pull filterable specs out of RC product titles."""
    t = title.lower()
    out = {}
    m = re.search(r"\b1/(\d{1,2})\b", t)
    if m:
        out["scale"] = f"1/{m.group(1)}"
    m = re.search(r"\b(\d{3,4})\s*kv\b", t)
    if m:
        out["kv"] = int(m.group(1))
    m = re.search(r"\b([1-8])s\b", t)
    if m:
        out["cells"] = int(m.group(1))
    m = re.search(r"\b(\d{3,5})\s*mah\b", t)
    if m:
        out["mah"] = int(m.group(1))
    return out


def bucket_for(p):
    text = ((p.get("title") or "") + " " + (p.get("product_type") or "")).lower()
    for keys, path in RULES:
        for k in keys:
            if k in text:
                return path
    return None


# ---- Plumbing ---------------------------------------------------------------
def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_photo_data_url(url):
    """Download product image, PAD to square (white), resize, return data URL.
    Padding (not cropping) preserves the full frame so the in-app Adjust Crop
    tool can reposition freely. Returns None on failure."""
    try:
        from PIL import Image
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        side = max(w, h)
        canvas = Image.new("RGB", (side, side), (255, 255, 255))
        canvas.paste(img, ((side - w) // 2, (side - h) // 2))
        img = canvas.resize((600, 600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"      (photo failed: {e})")
        return None


def find_or_make_bin():
    listing = api("GET", "/items?type=bin")
    for b in listing.get("items", []):
        if b.get("description") == "AZ T&B — Imported Catalog":
            return b["id"]
    bid = api("POST", "/items/new-id")["id"]
    api("PUT", f"/items/{bid}", {
        "type": "bin",
        "description": "AZ T&B — Imported Catalog",
        "location_property": "AZ Turn & Burn RC",
        "location_room": "Online Catalog Import",
    })
    print(f"  created bin {bid}: AZ T&B — Imported Catalog")
    return bid


def ensure_category_tree():
    """Create the TREE via the categories API, reusing nodes by (name,parent).
    Returns {(path tuple): leaf_category_id}."""
    existing = api("GET", "/categories")["categories"]

    def find(name, parent_id):
        for cid, node in existing.items():
            if node.get("name") == name and node.get("parent_id") == parent_id:
                return cid
        return None

    def make(name, parent_id, attrs):
        cid = find(name, parent_id)
        if cid:
            return cid
        r = api("POST", "/categories",
                {"name": name, "parent_id": parent_id, "attributes": attrs})
        cid = r["category"]["id"]
        existing[cid] = r["category"]
        return cid

    leaf_ids = {}
    for root_name, children in TREE.items():
        if "__leaf__" in children:
            cid = make(root_name, None, children["__leaf__"])
            leaf_ids[(root_name,)] = cid
        else:
            root_id = make(root_name, None, [])
            for child_name, attrs in children.items():
                cid = make(child_name, root_id, attrs)
                leaf_ids[(root_name, child_name)] = cid
    print(f"  category tree ready ({len(leaf_ids)} leaves)")
    return leaf_ids


def sku_lookup_existing(sku):
    """Returns existing item id for an exact SKU match, else None."""
    if not sku:
        return None
    try:
        m = api("POST", "/items/similar", {"sku": sku, "description": ""})
        for x in m.get("matches", []):
            if x.get("match") == "sku":
                return x["id"]
    except Exception:
        pass
    return None


def main():
    h = api("GET", "/health")
    print(f"Target instance: v{h.get('version')} at {BASE}")
    bin_id = find_or_make_bin()
    leaf_ids = ensure_category_tree()

    totals = {"imported": 0, "updated": 0, "no_bucket": 0,
              "no_photo_skipped": 0, "failed": 0}
    scanned = 0
    for page in range(1, PAGES + 1):
        try:
            feed = fetch_json(f"{SHOP}/products.json?limit=250&page={page}")
        except Exception as e:
            print(f"feed page {page} failed: {e}")
            break
        products = feed.get("products", [])
        if not products:
            break
        print(f"— page {page}: {len(products)} products —")
        for p in products:
            scanned += 1
            title = html.unescape((p.get("title") or "").strip())
            if not title:
                continue
            path = bucket_for(p)
            if not path:
                totals["no_bucket"] += 1
                continue
            images = p.get("images") or []
            if not images and not INCLUDE_PHOTOLESS:
                totals["no_photo_skipped"] += 1
                continue

            vendor = (p.get("vendor") or "").strip() or None
            ptype = (p.get("product_type") or "").strip() or None
            variants = p.get("variants") or []
            v0 = variants[0] if variants else {}
            sku = (v0.get("sku") or "").strip() or None
            price = v0.get("price")
            available = any(v.get("available") for v in variants)
            attrs = extract_attrs(title)
            cat_id = leaf_ids.get(path)

            existing_id = sku_lookup_existing(sku)
            if existing_id:
                # Upgrade in place: categorize + attributes, leave photo alone
                patch = {"category_id": cat_id}
                if attrs:
                    patch["attributes"] = attrs
                if ptype:
                    patch["category"] = ptype
                try:
                    api("PUT", f"/items/{existing_id}", patch)
                    totals["updated"] += 1
                except Exception as e:
                    totals["failed"] += 1
                    print(f"  update failed {existing_id}: {e}")
                continue

            rec = {
                "type": "item",
                "description": title[:200],
                "supplier": vendor,
                "supplier_sku": sku,
                "quantity": 1 if available else 0,
                "bin_id": bin_id,
                "category": ptype,
                "category_id": cat_id,
            }
            if attrs:
                rec["attributes"] = attrs
            if price:
                try:
                    rec["price"] = float(price)
                    rec["notes"] = f"MSRP ${price}"
                except Exception:
                    pass
            if WITH_PHOTOS and images:
                photo = fetch_photo_data_url(images[0]["src"])
                if photo:
                    rec["photo"] = photo
                time.sleep(0.05)
            try:
                iid = api("POST", "/items/new-id")["id"]
                api("PUT", f"/items/{iid}", rec)
                totals["imported"] += 1
                if totals["imported"] % 25 == 0:
                    print(f"  ... {totals['imported']} imported so far")
            except Exception as e:
                totals["failed"] += 1
                print(f"  FAILED: {title[:50]} — {e}")

    print(f"\nDone. scanned={scanned}")
    print(json.dumps(totals, indent=2))
    print("Browse the Categories tab — the whole catalog is now in the tree.")


if __name__ == "__main__":
    main()
