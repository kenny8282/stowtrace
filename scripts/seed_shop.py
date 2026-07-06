#!/usr/bin/env python3
"""Seed a fake RC hobby shop into a running StowTrace/Inventory instance.
Run ON the target machine (staging VM):  /opt/stowtrace/venv/bin/python3 seed_shop.py
Talks to the live API at http://localhost so it exercises the real code paths,
including the photo-upload -> thumbnail pipeline (generates placeholder JPEGs).
Refuses to run if the instance already has more than 5 items (pass --force to override).
"""
import base64
import io
import json
import random
import sys
import urllib.request

import os
BASE = os.environ.get("ST_API", "http://localhost/api")
random.seed(42)  # deterministic seed data


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def mint():
    return api("POST", "/items/new-id")["id"]


def make_photo(text, color):
    """Tiny placeholder product photo: colored square with big initials."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (600, 600), color)
    d = ImageDraw.Draw(img)
    # crude giant initials, centered-ish
    initials = "".join(w[0] for w in text.split()[:2]).upper()
    d.text((300, 300), initials, fill="white", anchor="mm", font_size=220)
    d.rectangle([20, 20, 580, 580], outline="white", width=6)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def main():
    stats = api("GET", "/stats")
    count = (stats.get("counts") or {}).get("total", 0)
    if count > 5 and "--force" not in sys.argv:
        print(f"Instance already has {count} items — refusing to seed. Use --force to override.")
        sys.exit(1)

    print("Seeding fake RC hobby shop...")

    # ---- Locations (bins) ----
    bins = {}
    for name, spot in [
        ("North Wall Pegboard", "North Wall"),
        ("East Wall Hangers", "East Wall"),
        ("Glass Display Cabinet", "Front Counter"),
        ("Shelf A — Electronics", "Center Aisle"),
        ("Shelf B — Hardware & Parts", "Center Aisle"),
        ("Parts Drawer Unit", "Back Room"),
    ]:
        bid = mint()
        api("PUT", f"/items/{bid}", {"type": "bin", "description": name,
                                     "location_property": "RC Shop",
                                     "location_room": spot})
        bins[name] = bid
        print(f"  bin  {bid}  {name}")

    # ---- Containers ----
    containers = {}
    for name, parent, subs in [
        ("Servo Boxes", "Shelf A — Electronics", [("Micro servo 9g", 6)]),
        ("ESC Bin", "Shelf A — Electronics", []),
        ("Battery Connectors Box", "Shelf A — Electronics",
         [("IC3 male", 10), ("IC5 female", 8), ("XT60 pairs", 15)]),
        ("M3 Hardware Box", "Shelf B — Hardware & Parts",
         [("M3x8 button", 40), ("M3x10 socket", 60), ("M3 nylock", 50)]),
        ("M4 Hardware Box", "Shelf B — Hardware & Parts", []),
        ("Bearing Assortment", "Shelf B — Hardware & Parts",
         [("5x11x4", 20), ("8x16x5", 12)]),
        ("Pinion Gear Drawer", "Parts Drawer Unit", []),
        ("Body Clips Jar", "Parts Drawer Unit", [("standard clips", 200)]),
    ]:
        cid = mint()
        rec = {"type": "container", "description": name, "bin_id": bins[parent]}
        if subs:
            rec["sub_items"] = [{"name": n, "qty": q} for n, q in subs]
        api("PUT", f"/items/{cid}", rec)
        containers[name] = cid
        print(f"  ctr  {cid}  {name}")

    # ---- Categories (store-flavored tree) ----
    def cat(name, parent_id=None, attrs=None):
        r = api("POST", "/categories", {"name": name, "parent_id": parent_id,
                                        "attributes": attrs or []})
        return r["category"]["id"]

    parts = cat("RC Parts")
    c_servo = cat("Servos", parts, [
        {"key": "torque", "label": "Torque (kg·cm)", "type": "number"},
        {"key": "size", "label": "Size", "type": "select",
         "values": ["micro", "standard", "large"]},
    ])
    c_esc = cat("Motors & ESCs", parts, [
        {"key": "kv", "label": "KV", "type": "number"},
        {"key": "scale", "label": "Scale", "type": "select",
         "values": ["1/16", "1/10", "1/8", "1/5"]},
    ])
    c_batt = cat("Batteries", parts, [
        {"key": "cells", "label": "Cells (S)", "type": "number"},
        {"key": "mah", "label": "Capacity (mAh)", "type": "number"},
        {"key": "plug", "label": "Plug", "type": "select",
         "values": ["IC3", "IC5", "XT60", "XT90", "Deans"]},
    ])
    c_tire = cat("Tires & Wheels", parts)
    c_body = cat("Bodies & Accessories", parts)
    print("  categories seeded")

    # ---- Items ----
    items = [
        # (description, brand, sku, qty, parent(bin name or container name), category, attrs, buy, photo_color)
        ("2075 Waterproof Digital Servo", "Traxxas", "TRA2075", 4,
         ("c", "Servo Boxes"), c_servo, {"torque": 9.0, "size": "standard"},
         "https://traxxas.com/products/parts/2075", (180, 40, 30)),
        ("S6250 High Torque Servo", "Spektrum", "SPMSS6250", 2,
         ("c", "Servo Boxes"), c_servo, {"torque": 22.0, "size": "standard"}, None, (30, 60, 160)),
        ("Firma 3660 3200Kv Motor", "Spektrum", "SPMXSM1400", 3,
         ("c", "ESC Bin"), c_esc, {"kv": 3200, "scale": "1/10"}, None, (30, 60, 160)),
        ("BLX100 Brushless ESC", "Arrma", "AR390201", 2,
         ("c", "ESC Bin"), c_esc, {"scale": "1/10"}, None, (200, 120, 20)),
        ("Max 10 SCT ESC", "Hobbywing", "HW30103202", 3,
         ("c", "ESC Bin"), c_esc, {"scale": "1/10"}, None, (90, 30, 120)),
        ("5000mAh 2S 50C Hardcase LiPo", "Spektrum", "SPMX50002S50H5", 8,
         ("b", "Shelf A — Electronics"), c_batt, {"cells": 2, "mah": 5000, "plug": "IC5"},
         "https://www.spektrumrc.com", (30, 60, 160)),
        ("5000mAh 3S 50C LiPo", "Spektrum", "SPMX50003S50H5", 6,
         ("b", "Shelf A — Electronics"), c_batt, {"cells": 3, "mah": 5000, "plug": "IC5"}, None, (30, 60, 160)),
        ("4000mAh 2S NiMH Stick Pack", "Traxxas", "TRA2950X", 5,
         ("b", "Shelf A — Electronics"), c_batt, {"cells": 2, "mah": 4000, "plug": "IC3"}, None, (180, 40, 30)),
        ("Sledgehammer Belted Tires (pr)", "Traxxas", "TRA9573", 6,
         ("b", "East Wall Hangers"), c_tire, {}, None, (180, 40, 30)),
        ("dBoots Backflip Tire Set", "Arrma", "AR550065", 4,
         ("b", "East Wall Hangers"), c_tire, {}, None, (200, 120, 20)),
        ("Trencher X 3.8 Pre-mounted", "ProLine", "PRO1194", 3,
         ("b", "East Wall Hangers"), c_tire, {}, None, (20, 130, 60)),
        ("Slash 2WD Clear Body", "Traxxas", "TRA6811", 5,
         ("b", "North Wall Pegboard"), c_body, {}, None, (180, 40, 30)),
        ("SC10 Body White", "ProLine", "PRO3355", 2,
         ("b", "North Wall Pegboard"), c_body, {}, None, (20, 130, 60)),
        ("Aluminum Servo Horn 25T", "Losi", "LOS251100", 7,
         ("c", "Servo Boxes"), c_servo, {}, None, (120, 100, 20)),
        ("48P 18T Pinion", "Losi", "LOS4118", 9,
         ("c", "Pinion Gear Drawer"), None, {}, None, (120, 100, 20)),
        ("Mod1 14T Pinion Hardened", "Tekno", "TKR4174", 4,
         ("c", "Pinion Gear Drawer"), None, {}, None, (60, 60, 70)),
        ("IC5 Device Connector 2-pack", "Spektrum", "SPMXCA504", 12,
         ("c", "Battery Connectors Box"), None, {}, None, (30, 60, 160)),
        ("Glow Plug Standard #3", "OS Engines", "OS71605300", 10,
         ("b", "Glass Display Cabinet"), None, {}, None, (150, 20, 90)),
        ("DX5 Rugged Transmitter", "Spektrum", "SPM5200", 1,
         ("b", "Glass Display Cabinet"), None, {},
         "https://www.spektrumrc.com/product/dx5-rugged-dsmr-tx-only/SPMR5200.html", (30, 60, 160)),
        ("Micro Starter Box 1/10", "Dynamite", "DYNE0200", 2,
         ("b", "Parts Drawer Unit"), None, {}, None, (90, 90, 90)),
    ]
    photo_every = 3  # give roughly a third of items a placeholder photo
    for idx, (desc, brand, sku, qty, (ptype, pname), cat_id, attrs, buy, color) in enumerate(items):
        iid = mint()
        parent = bins[pname] if ptype == "b" else containers[pname]
        rec = {"type": "item", "description": desc, "supplier": brand,
               "supplier_sku": sku, "quantity": qty, "bin_id": parent}
        if cat_id:
            rec["category_id"] = cat_id
        if attrs:
            rec["attributes"] = attrs
        if buy:
            rec["buy_url"] = buy
        if idx % photo_every == 0:
            rec["photo"] = make_photo(desc, color)
        api("PUT", f"/items/{iid}", rec)
        print(f"  item {iid}  {desc}")

    stats = api("GET", "/stats")
    print("\nDone. /api/stats says:", json.dumps(stats))
    print("Open the app and browse: bins on the Bins tab, items inside them,")
    print("categories under the Categories tab, photos on ~1/3 of items.")


if __name__ == "__main__":
    main()
