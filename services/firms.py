"""Fetch active-fire detections from NASA FIRMS (Fire Information for Resource
Management System) area API and convert them to GeoJSON points.

Docs: https://firms.modaps.eosdis.nasa.gov/api/area/
"""
import csv
import datetime as dt
import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

FIRMS_BASE_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# FIRMS's area/csv endpoint rejects any single request's day_range outside
# 1-5 with its own HTTP 400 (confirmed against the live API). Longer
# ranges are still useful for a multi-day fire though, so fetch_active_fires
# transparently splits anything above 5 into multiple <=5-day calls (see
# _iter_chunks) run in parallel and merges the results. Capped here at 20
# (four parallel calls) to keep a single analysis from firing off a large
# burst of requests -- NRT sources generally only retain a couple of
# months of data, so this is a usability cap, not a data-availability one.
MAX_TOTAL_DAY_RANGE = 20
_MAX_CHUNK_DAY_RANGE = 5

# Numeric fields present in FIRMS CSV output, kept as floats/ints in the
# GeoJSON properties instead of strings.
_NUMERIC_FIELDS = {
    "brightness", "bright_ti4", "bright_ti5", "scan", "track",
    "confidence", "version", "frp",
}


class FirmsError(RuntimeError):
    pass


def get_map_key():
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        raise FirmsError(
            "FIRMS_MAP_KEY is not set. Get a free key at "
            "https://firms.modaps.eosdis.nasa.gov/api/map_key/ and put it "
            "in a .env file (see .env.example)."
        )
    return key


def _iter_chunks(start_date, day_range):
    """Split [start_date, start_date + day_range - 1] into consecutive,
    non-overlapping (start_date, length) chunks of at most
    _MAX_CHUNK_DAY_RANGE days each. Confirmed directly against the live
    API that day_range counts forward from start_date (date=2026-07-25,
    day_range=3 returned 07-25/07-26/07-27), so chunks are laid out
    forward the same way -- no gaps, no overlap, so merging chunk results
    is a plain concatenation with no de-duplication needed.
    """
    start = dt.date.fromisoformat(start_date)
    offset = 0
    while offset < day_range:
        chunk_len = min(_MAX_CHUNK_DAY_RANGE, day_range - offset)
        yield (start + dt.timedelta(days=offset)).isoformat(), chunk_len
        offset += chunk_len


def _fetch_chunk(bbox, start_date, day_range, source):
    """Fetch at most _MAX_CHUNK_DAY_RANGE days in a single FIRMS request.

    bbox: (west, south, east, north) in WGS84 degrees.
    start_date: 'YYYY-MM-DD', the first day of the range.
    source: FIRMS sensor/source id, e.g. VIIRS_SNPP_NRT, MODIS_NRT.
    """
    map_key = get_map_key()
    west, south, east, north = bbox
    area = f"{west},{south},{east},{north}"
    day_range = max(1, min(_MAX_CHUNK_DAY_RANGE, day_range))

    url = f"{FIRMS_BASE_URL}/{map_key}/{source}/{area}/{day_range}/{start_date}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise FirmsError(
            "NASA FIRMS did not respond in time. Try a smaller area, fewer days, or try again shortly."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        # A 4xx here means FIRMS rejected the request itself (bad key, bad
        # day range, malformed area, etc.) -- distinct from a connectivity
        # failure, so it gets its own message instead of the misleading
        # "could not reach" text below.
        detail = resp.text.strip()[:200] if resp is not None else str(exc)
        raise FirmsError(f"NASA FIRMS rejected the request: {detail}") from exc
    except requests.exceptions.RequestException as exc:
        raise FirmsError(f"Could not reach NASA FIRMS ({exc}). Try again shortly.") from exc

    text = resp.text.strip()
    if not text or text.lower().startswith("invalid"):
        raise FirmsError(f"FIRMS API returned an error: {text[:200]}")

    reader = csv.DictReader(io.StringIO(text))
    features = []
    for row in reader:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
        except (KeyError, ValueError):
            continue

        props = {}
        for key, value in row.items():
            if key in ("latitude", "longitude"):
                continue
            if key in _NUMERIC_FIELDS:
                try:
                    props[key] = float(value)
                except (TypeError, ValueError):
                    props[key] = value
            else:
                props[key] = value

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })

    return {"type": "FeatureCollection", "features": features}


def cluster_fires(fires_geojson, cell_size_deg=0.05, bbox_pad_frac=0.2, min_bbox_pad_deg=0.015):
    """Group raw hotspot detections into rough clusters, one per real
    fire rather than one per detected pixel -- a single active fire
    commonly lights up dozens of adjacent VIIRS/MODIS pixels, which would
    otherwise show as a dense scatter of individual points on a wide,
    low-zoom overview map (see app.py's /api/active-fires-overview, used
    to show the user roughly where active fires are before they've drawn
    an analysis box of their own).

    Simple grid-snap + flood-fill: bins points into cell_size_deg-sized
    cells (~5km at cell_size_deg=0.05), then merges any cells that touch
    (including diagonally) into one cluster. Deliberately coarse -- this
    is a "roughly here" guide for where to draw a box, not the burnt-area
    estimate itself (see estimate.py for that).

    Returns [{"lat", "lon", "count", "max_frp", "start_date", "end_date",
    "bbox"}, ...] sorted by point count descending (a real, sustained fire
    generally has far more detections than a stray false-positive pixel).

    start_date/end_date: earliest/latest acq_date among the cluster's own
    detections (plain 'YYYY-MM-DD' strings, which sort/compare correctly
    as text) -- lets a caller show roughly how long a fire has been
    burning, not just that it's currently active.

    bbox: (west, south, east, north) padded by bbox_pad_frac around the
    cluster's own detections (floored at min_bbox_pad_deg, ~1.5km, so a
    single-point cluster still gets a sensible starting box) -- meant as
    a ready-to-use suggested analysis box (see app.py's
    /api/active-fires-overview and map.js), not a precise fire perimeter.
    """
    cells = {}
    for feat in fires_geojson.get("features", []):
        lon, lat = feat["geometry"]["coordinates"]
        cell = (int(lat // cell_size_deg), int(lon // cell_size_deg))
        props = feat.get("properties", {})
        frp = props.get("frp") or 0.0
        acq_date = props.get("acq_date")
        cells.setdefault(cell, []).append((lat, lon, frp, acq_date))

    visited = set()
    clusters = []
    for start_cell in cells:
        if start_cell in visited:
            continue
        visited.add(start_cell)
        stack = [start_cell]
        group = []
        while stack:
            cell = stack.pop()
            group.extend(cells[cell])
            row, col = cell
            for d_row in (-1, 0, 1):
                for d_col in (-1, 0, 1):
                    neighbor = (row + d_row, col + d_col)
                    if neighbor in cells and neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)

        lats = [p[0] for p in group]
        lons = [p[1] for p in group]
        frps = [p[2] for p in group]
        dates = sorted(p[3] for p in group if p[3])

        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        pad_lat = max((max_lat - min_lat) * bbox_pad_frac, min_bbox_pad_deg)
        pad_lon = max((max_lon - min_lon) * bbox_pad_frac, min_bbox_pad_deg)

        clusters.append({
            "lat": sum(lats) / len(lats),
            "lon": sum(lons) / len(lons),
            "count": len(group),
            "max_frp": max(frps) if frps else 0.0,
            "start_date": dates[0] if dates else None,
            "end_date": dates[-1] if dates else None,
            "bbox": [min_lon - pad_lon, min_lat - pad_lat, max_lon + pad_lon, max_lat + pad_lat],
        })

    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


def fetch_active_fires(bbox, start_date, day_range=1, source="VIIRS_SNPP_NRT"):
    """Fetch active fire detections within bbox as a GeoJSON FeatureCollection,
    covering up to MAX_TOTAL_DAY_RANGE days even though FIRMS itself caps a
    single request at 5.

    bbox: (west, south, east, north) in WGS84 degrees.
    start_date: 'YYYY-MM-DD', the first day of the range.
    day_range: total number of days of data to pull (1-MAX_TOTAL_DAY_RANGE).
        Split into <=5-day chunks fetched concurrently and merged when it
        exceeds what FIRMS accepts in one call.
    source: FIRMS sensor/source id, e.g. VIIRS_SNPP_NRT, MODIS_NRT.
    """
    day_range = max(1, min(MAX_TOTAL_DAY_RANGE, day_range))
    chunks = list(_iter_chunks(start_date, day_range))

    if len(chunks) == 1:
        chunk_start, chunk_len = chunks[0]
        return _fetch_chunk(bbox, chunk_start, chunk_len, source)

    all_features = []
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [pool.submit(_fetch_chunk, bbox, chunk_start, chunk_len, source) for chunk_start, chunk_len in chunks]
        # Any chunk failing (a real FirmsError) fails the whole fetch --
        # silently dropping a few days of detections would understate the
        # burnt area without any indication to the user.
        for fut in as_completed(futures):
            all_features.extend(fut.result()["features"])

    return {"type": "FeatureCollection", "features": all_features}
