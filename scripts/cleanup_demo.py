#!/usr/bin/env python3
"""Demo cleanup: keep ONLY the 'AZ T&B — Imported Catalog' bin and the items
inside it that have photos. Everything else (fake-shop seed bins/containers/
items, photo-less imports) is deleted. Saves a full backup to /tmp first so
this is reversible via the app's Restore feature.

Run ON the VM:  /opt/stowtrace/venv/bin/python3 cleanup_demo.py
"""
import json
import urllib.request

BASE = "http://localhost/api"
KEEP_BIN_NAME = "AZ T&B — Imported Catalog"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    # ---- Safety net: full backup to /tmp before touching anything ----
    print("Saving pre-cleanup backup...")
    req = urllib.request.Request(BASE + "/system/backup")
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = r.read()
    with open("/tmp/pre-cleanup-backup.json", "wb") as f:
        f.write(payload)
    print(f"  /tmp/pre-cleanup-backup.json ({len(payload)//1024} KB) — restore via Options tab if needed")

    # ---- Inventory census ----
    items = api("GET", "/items")["items"]
    keep_bin = next((i for i in items
                     if i.get("type") == "bin"
                     and i.get("description") == KEEP_BIN_NAME), None)
    if not keep_bin:
        print(f"ERROR: bin '{KEEP_BIN_NAME}' not found — nothing deleted.")
        return

    keep_ids = {keep_bin["id"]}
    kept_items = 0
    to_delete = []
    for it in items:
        if it["id"] == keep_bin["id"]:
            continue
        if (it.get("type") == "item"
                and it.get("bin_id") == keep_bin["id"]
                and it.get("has_photo")):
            keep_ids.add(it["id"])
            kept_items += 1
        else:
            to_delete.append(it)

    print(f"Keeping: 1 bin + {kept_items} photo items")
    print(f"Deleting: {len(to_delete)} records "
          f"({sum(1 for x in to_delete if x.get('type')=='bin')} bins, "
          f"{sum(1 for x in to_delete if x.get('type')=='container')} containers, "
          f"{sum(1 for x in to_delete if x.get('type')=='item')} items, "
          f"{sum(1 for x in to_delete if x.get('type') not in ('bin','container','item'))} other)")

    for it in to_delete:
        try:
            api("DELETE", f"/items/{it['id']}")
        except Exception as e:
            print(f"  delete failed for {it['id']}: {e}")
    print("Done.")
    stats = api("GET", "/stats")
    print("Final counts:", json.dumps(stats.get("counts")))


if __name__ == "__main__":
    main()
