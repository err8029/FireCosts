"""Fetch and stitch OpenStreetMap raster tiles into a basemap image for the
static PNG report.

Per OSM's tile usage policy (https://operations.osmfoundation.org/policies/tiles/)
this is meant for light, occasional use -- a manual "export report" click
pulling at most a few dozen tiles, with a real User-Agent and attribution
rendered on the output. Not for high-traffic/production embedding.
"""
import io
import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from PIL import Image

_HEADERS = {"User-Agent": "Hephaestus/1.0 (personal project; contact: n/a)"}
_TILE_SIZE = 256
_MAX_TILES = 36

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

    def _fetch_one(txy):
        tx, ty = txy
        try:
            resp = session.get(tile_url.format(z=zoom, x=tx, y=ty), timeout=timeout)
            resp.raise_for_status()
            # PIL sniffs the actual format from the file signature, unlike
            # matplotlib's imread which -- given a BytesIO with no
            # filename to infer an extension from -- assumes PNG and
            # chokes on JPEG tiles (e.g. Esri's satellite imagery).
            img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            return tx, ty, np.asarray(img, dtype=np.float32) / 255.0
        except Exception:
            return tx, ty, None

    tile_coords = [(tx, ty) for ty in range(y_min, y_max + 1) for tx in range(x_min, x_max + 1)]
    fetched_any = False
    # A handful of concurrent requests (not one per tile) keeps this within
    # OSM/Esri's "occasional manual use" tile policy while still cutting
    # wall-clock time substantially versus fetching up to _MAX_TILES tiles
    # one at a time.
    with ThreadPoolExecutor(max_workers=6) as pool:
        for tx, ty, tile in pool.map(_fetch_one, tile_coords):
            if tile is None:
                continue
            row_off = (ty - y_min) * _TILE_SIZE
            col_off = (tx - x_min) * _TILE_SIZE
            mosaic[row_off:row_off + _TILE_SIZE, col_off:col_off + _TILE_SIZE] = tile
            fetched_any = True

    if not fetched_any:
        return None

    north_deg, west_deg = _num2deg(x_min, y_min, zoom)
    south_deg, east_deg = _num2deg(x_max + 1, y_max + 1, zoom)
    return mosaic, (west_deg, east_deg, south_deg, north_deg)
