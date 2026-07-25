"""Fetch and stitch OpenStreetMap raster tiles into a basemap image for the
static PNG report.

Per OSM's tile usage policy (https://operations.osmfoundation.org/policies/tiles/)
this is meant for light, occasional use -- a manual "export report" click
pulling at most a few dozen tiles, with a real User-Agent and attribution
rendered on the output. Not for high-traffic/production embedding.
"""
import io
import math

import numpy as np
import requests
from matplotlib import image as mpimg

_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_HEADERS = {"User-Agent": "FireAnalysis/1.0 (personal project; contact: n/a)"}
_TILE_SIZE = 256
_MAX_TILES = 36

ATTRIBUTION = "© OpenStreetMap contributors"


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


def fetch_basemap(bbox, target_tiles_across=4, timeout=10):
    """Return (mosaic_rgba_array, (west, east, south, north)) for a stitched
    OSM basemap covering bbox, or None if no tiles could be fetched."""
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

    fetched_any = False
    for ty in range(y_min, y_max + 1):
        for tx in range(x_min, x_max + 1):
            try:
                resp = session.get(_TILE_URL.format(z=zoom, x=tx, y=ty), timeout=timeout)
                resp.raise_for_status()
                tile = mpimg.imread(io.BytesIO(resp.content))
            except Exception:
                continue
            if tile.shape[2] == 3:
                alpha = np.ones((*tile.shape[:2], 1), dtype=tile.dtype)
                tile = np.concatenate([tile, alpha], axis=2)
            row_off = (ty - y_min) * _TILE_SIZE
            col_off = (tx - x_min) * _TILE_SIZE
            mosaic[row_off:row_off + _TILE_SIZE, col_off:col_off + _TILE_SIZE] = tile
            fetched_any = True

    if not fetched_any:
        return None

    north_deg, west_deg = _num2deg(x_min, y_min, zoom)
    south_deg, east_deg = _num2deg(x_max + 1, y_max + 1, zoom)
    return mosaic, (west_deg, east_deg, south_deg, north_deg)
