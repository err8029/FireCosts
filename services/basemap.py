"""Fetch and stitch OpenStreetMap raster tiles into a basemap image for the
static PNG report.

Per OSM's tile usage policy (https://operations.osmfoundation.org/policies/tiles/)
this is meant for light, occasional use -- a manual "export report" click
pulling at most a few dozen tiles, with a real User-Agent and attribution
rendered on the output. Not for high-traffic/production embedding.
"""
import io
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait

import numpy as np
import requests
from PIL import Image

_HEADERS = {"User-Agent": "Hephaestus/1.0 (personal project; contact: n/a)"}
_TILE_SIZE = 256
# Raised alongside report.py's target_tiles_across=6 (was 4) -- a 6x6-ish
# grid needs headroom above 36 for non-square analysis boxes (e.g. a wide
# box still fitting 6 tiles across can need more than 6 down). Tiles are
# cached (see _tile_cache below) and the fetch itself is deadline-bounded,
# so allowing more of them no longer risks a slow/hanging report the way
# it would have before those existed.
_MAX_TILES = 64

# Report generation is commonly re-run for the same or an overlapping area
# (tweaking a price, retrying after a failure, exporting again a minute
# later) -- tiles for a given layer/zoom/tile-coordinate are stable for
# far longer than one session, so re-downloading them from OSM/Esri every
# single export is pure waste (and the dominant cost in report
# generation once the locator/municipality lookups are no longer the
# bottleneck -- see services/municipalities.py's bundled region data).
# Same in-memory cache pattern as services/buildings.py and friends.
_TILE_CACHE_TTL_SECONDS = 60 * 60
_tile_cache = {}
_tile_cache_lock = threading.Lock()

# Bounds the total wall-clock time this can take regardless of how many
# tiles are slow or unresponsive -- previously `with ThreadPoolExecutor()
# as pool: pool.map(...)` had no deadline at all, so a degraded tile
# server could stall the whole report for as long as _MAX_TILES/6 * each
# tile's own 10s timeout (up to a minute). A handful of missing tiles just
# render as blank map background, which is a fine degradation; hanging
# the whole export is not.
_TILE_FETCH_DEADLINE_S = 20

# Esri World Imagery is a free, no-key-required satellite/aerial tile
# service (same light-use expectations as OSM's tile policy: manual,
# occasional requests with attribution, not high-traffic embedding). Its
# REST tile path is ordered {z}/{y}/{x}, not the usual {z}/{x}/{y}.
_LAYERS = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
    },
    "satellite": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    },
}


def get_attribution(layer):
    return _LAYERS.get(layer, _LAYERS["osm"])["attribution"]


def _deg2num(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _num2deg(x, y, zoom):
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    return math.degrees(lat_rad), lon


def _pick_zoom(west, south, east, north, target_tiles_across):
    for zoom in range(18, 2, -1):
        x_min, _ = _deg2num(north, west, zoom)
        x_max, _ = _deg2num(south, east, zoom)
        if x_max - x_min + 1 <= target_tiles_across:
            return zoom
    return 3


def fetch_basemap(bbox, layer="osm", target_tiles_across=4, timeout=10):
    """Return (mosaic_rgba_array, (west, east, south, north)) for a stitched
    basemap covering bbox, or None if no tiles could be fetched.

    layer: "osm" or "satellite" -- see _LAYERS above.
    """
    tile_url = _LAYERS.get(layer, _LAYERS["osm"])["url"]
    west, south, east, north = bbox
    zoom = _pick_zoom(west, south, east, north, target_tiles_across)

    x_min, y_min = _deg2num(north, west, zoom)
    x_max, y_max = _deg2num(south, east, zoom)
    x_min, x_max = sorted((x_min, x_max))
    y_min, y_max = sorted((y_min, y_max))

    cols, rows = x_max - x_min + 1, y_max - y_min + 1
    if cols * rows > _MAX_TILES:
        return None

    mosaic = np.ones((rows * _TILE_SIZE, cols * _TILE_SIZE, 4), dtype=np.float32)
    session = requests.Session()
    session.headers.update(_HEADERS)

    def _cache_key(tx, ty):
        return (layer, zoom, tx, ty)

    def _fetch_one(txy):
        tx, ty = txy
        key = _cache_key(tx, ty)
        with _tile_cache_lock:
            cached = _tile_cache.get(key)
        if cached is not None:
            cached_at, tile = cached
            if time.time() - cached_at <= _TILE_CACHE_TTL_SECONDS:
                return tx, ty, tile

        try:
            resp = session.get(tile_url.format(z=zoom, x=tx, y=ty), timeout=timeout)
            resp.raise_for_status()
            # PIL sniffs the actual format from the file signature, unlike
            # matplotlib's imread which -- given a BytesIO with no
            # filename to infer an extension from -- assumes PNG and
            # chokes on JPEG tiles (e.g. Esri's satellite imagery).
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            tile = np.asarray(img, dtype=np.float32) / 255.0
        except Exception:
            return tx, ty, None

        with _tile_cache_lock:
            _tile_cache[key] = (time.time(), tile)
        return tx, ty, tile

    tile_coords = [(tx, ty) for ty in range(y_min, y_max + 1) for tx in range(x_min, x_max + 1)]
    fetched_any = False
    # A handful of concurrent requests (not one per tile) keeps this within
    # OSM/Esri's "occasional manual use" tile policy while still cutting
    # wall-clock time substantially versus fetching up to _MAX_TILES tiles
    # one at a time. Deliberately not using the executor as a context
    # manager, since `with` would block on shutdown() waiting for tiles
    # we've already decided to give up on -- see _TILE_FETCH_DEADLINE_S.
    pool = ThreadPoolExecutor(max_workers=6)
    futures = {pool.submit(_fetch_one, txy): txy for txy in tile_coords}
    futures_wait(list(futures.keys()), timeout=_TILE_FETCH_DEADLINE_S)
    for fut, (tx, ty) in futures.items():
        if not fut.done():
            continue
        _, _, tile = fut.result()
        if tile is None:
            continue
        row_off = (ty - y_min) * _TILE_SIZE
        col_off = (tx - x_min) * _TILE_SIZE
        mosaic[row_off:row_off + _TILE_SIZE, col_off:col_off + _TILE_SIZE] = tile
        fetched_any = True
    pool.shutdown(wait=False)

    if not fetched_any:
        return None

    north_deg, west_deg = _num2deg(x_min, y_min, zoom)
    south_deg, east_deg = _num2deg(x_max + 1, y_max + 1, zoom)
    return mosaic, (west_deg, east_deg, south_deg, north_deg)
