#!/usr/bin/env python3
"""
bake_maptiles.py — download OSM map tiles for Intercept Line and pack them into
per-zoom ZIP archives the game reads OFFLINE.

  >>> RUN THIS ON YOUR OWN MACHINE (open network) — NOT in the dev sandbox. <<<

------------------------------------------------------------------------------
READ BEFORE RUNNING — license / prerequisites
------------------------------------------------------------------------------
  * Tiles come from MapTiler (https://www.maptiler.com). You need a MapTiler
    account + API key. Put it in the MAPTILER_KEY env var (recommended) or edit
    the constant below.
  * CONFIRM your MapTiler plan PERMITS caching tiles for offline use in a
    distributed / commercial app. The free tier is limited — check before you
    ship. This is the same kind of rule that makes bulk-downloading from
    tile.openstreetmap.org forbidden; MapTiler is the licensed alternative.
  * Keep the in-game OSM attribution ("(c) OpenStreetMap contributors, ODbL").
    MapTiler may also require crediting MapTiler — check their terms.
  * Do NOT point TILE_URL at tile.openstreetmap.org for bulk download.

------------------------------------------------------------------------------
OUTPUT  (place these in the game so the loader can read them — see next step)
------------------------------------------------------------------------------
  out/maptiles_osm_z6.zip ... out/maptiles_osm_z11.zip
      one ZIP per zoom; each entry is named  "{x}/{y}.png"
      (the game opens the ZIP for the current zoom and reads tiles via ZIPReader)

------------------------------------------------------------------------------
USAGE
------------------------------------------------------------------------------
  export MAPTILER_KEY=your_key_here
  python3 bake_maptiles.py                 # all zooms 6..11 (~1.5 GB, takes a while)
  python3 bake_maptiles.py --zooms 6 7 8   # just a subset (test first!)

  Resumable: finished tiles live in cache/ and are skipped on re-run. If some
  tiles fail (network hiccups), just run it again. Delete cache/ when fully done.
  Tip: bake one low zoom first (e.g. --zooms 6) to confirm the URL/key/look,
  THEN run the full thing.
"""

import os
import sys
import math
import time
import zipfile
import argparse
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed

# ===== CONFIG (mirrors scripts/world.gd bounds + scripts/map_view.gd zooms) =====
MAPTILER_KEY = os.environ.get("MAPTILER_KEY", "PUT_YOUR_KEY_HERE")

# MapTiler map id. "openstreetmap" is closest to the game's current OSM look.
MAP_ID = "openstreetmap"

# IMPORTANT: this URL MUST return 256x256 tiles on the standard slippy XYZ
# scheme (same tiling the game uses). 512px tiles would 4x the download size
# AND misalign with the game. The "/256/" segment below requests 256px tiles;
# if MapTiler changes their API, verify the exact raster-tiles URL in their docs
# and that what you get back is genuinely 256x256.
TILE_URL = "https://api.maptiler.com/maps/{map}/256/{z}/{x}/{y}.png?key={key}"

# Map bounds to bake = the GAME area only: Ukraine + the in-game part of Russia.
# This is sized to the topo-layer coverage box (dem_builder.gd COVER_*: lat 43..56,
# lon 21..50) plus a small buffer, so every in-game target/PVO/radar (which span
# lat 43.6..56.5, lon 22.1..50.5) sits on a real tile. NOT the full pannable theater.
MIN_LAT, MAX_LAT = 43.0, 57.0
MIN_LON, MAX_LON = 21.5, 51.0

# Zoom range — keep in sync with map_view.gd MIN_ZOOM..MAX_ZOOM.
ZOOMS = [6, 7, 8, 9, 10, 11]

WORKERS = 6           # concurrent downloads (don't go crazy — respect the API)
RETRIES = 4
TIMEOUT = 30          # seconds per tile request
CACHE_DIR = "cache"   # loose tiles for resume; safe to delete after packing
OUT_DIR = "out"
USER_AGENT = "InterceptLine-tilebake/1.0"
# ================================================================================


def lon2x(lon: float, z: int) -> int:
    return int((lon + 180.0) / 360.0 * (2 ** z))


def lat2y(lat: float, z: int) -> int:
    r = math.radians(lat)
    return int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * (2 ** z))


def tile_range(z: int):
    x0, x1 = lon2x(MIN_LON, z), lon2x(MAX_LON, z)
    y0, y1 = lat2y(MAX_LAT, z), lat2y(MIN_LAT, z)  # y grows southward
    return min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)


def fetch(z: int, x: int, y: int):
    path = os.path.join(CACHE_DIR, str(z), str(x), f"{y}.png")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return ("skip", z, x, y)
    url = TILE_URL.format(map=MAP_ID, z=z, x=x, y=y, key=MAPTILER_KEY)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    last = "?"
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read()
            if not data:
                raise ValueError("empty response")
            with open(path, "wb") as f:
                f.write(data)
            return ("ok", z, x, y)
        except Exception as e:  # noqa
            last = str(e)
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    return ("fail", z, x, y, last)


def bake_zoom(z: int) -> int:
    x0, x1, y0, y1 = tile_range(z)
    tiles = [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    total = len(tiles)
    print(f"[z{z}] {total:,} tiles   x[{x0}..{x1}]  y[{y0}..{y1}]", flush=True)
    ok = skip = fail = 0
    fails = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(fetch, *t) for t in tiles]
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            tag = res[0]
            if tag == "ok":
                ok += 1
            elif tag == "skip":
                skip += 1
            else:
                fail += 1
                fails.append(res)
            if i % 250 == 0 or i == total:
                print(f"  z{z}: {i:,}/{total:,}  ok={ok} skip={skip} fail={fail}", flush=True)
    if fails:
        print(f"  z{z}: {len(fails)} FAILED (re-run to retry). Examples:")
        for f in fails[:5]:
            print("    ", f)

    os.makedirs(OUT_DIR, exist_ok=True)
    zip_path = os.path.join(OUT_DIR, f"maptiles_osm_z{z}.zip")
    print(f"  packing -> {zip_path}", flush=True)
    # PNGs are already compressed: ZIP_STORED keeps it fast and ZIPReader-friendly.
    packed = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for (_zz, x, y) in tiles:
            p = os.path.join(CACHE_DIR, str(z), str(x), f"{y}.png")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                zf.write(p, f"{x}/{y}.png")
                packed += 1
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"  z{z} done: {packed:,} tiles packed, ~{size_mb:.0f} MB\n", flush=True)
    return fail


def _pause():
    # Keep the console window open when the script is double-clicked on Windows.
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\nPress Enter to close...")
    except Exception:  # noqa
        pass


def main():
    global MAPTILER_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", type=str, default=None,
                    help="MapTiler API key (overrides the MAPTILER_KEY env var)")
    ap.add_argument("--zooms", type=int, nargs="*", default=ZOOMS,
                    help="zoom levels to bake (default 6 7 8 9 10 11)")
    args = ap.parse_args()
    if args.key:
        MAPTILER_KEY = args.key

    # A key is only needed when the tile URL actually has a {key} placeholder
    # (e.g. MapTiler). A local tile server URL has none, so no key is required.
    needs_key = "{key}" in TILE_URL
    if needs_key and MAPTILER_KEY in ("", "PUT_YOUR_KEY_HERE"):
        print("ERROR: this tile URL needs an API key, but none was provided.")
        print("  Easiest:   python bake_maptiles.py --key YOUR_KEY")
        print("  Or set it: set MAPTILER_KEY=YOUR_KEY        (Windows cmd)")
        print("             $env:MAPTILER_KEY=\"YOUR_KEY\"     (PowerShell)")
        print("             export MAPTILER_KEY=YOUR_KEY      (macOS/Linux)")
        _pause()
        sys.exit(1)

    print(f"bbox lat[{MIN_LAT}..{MAX_LAT}]  lon[{MIN_LON}..{MAX_LON}]   zooms {args.zooms}")
    print(f"map id: {MAP_ID}   workers: {WORKERS}\n")
    t0 = time.time()
    total_fail = 0
    for z in args.zooms:
        total_fail += bake_zoom(z)
    dt = time.time() - t0
    print(f"ALL DONE in {dt/60:.1f} min.  total failures: {total_fail}")
    if total_fail:
        print("Some tiles failed — just run again; finished tiles are skipped.")
    else:
        print("You can now delete the cache/ folder. ZIPs are in out/.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume — finished tiles are skipped.")
    except Exception:  # noqa
        import traceback
        traceback.print_exc()
        print("\nSomething went wrong (see the error above).")
    _pause()
