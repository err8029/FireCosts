"""Estimate monetary value at risk for a set of affected buildings.

Spain's public Catastro service does not expose official assessed value
(`valor catastral`) to unauthenticated callers -- see services/catastro.py.
As a substitute, this pulls the real official built area, land-use
classification and construction year per building from Catastro, then
multiplies built area by a configurable price-per-m2 (by use type) to
produce a market-value *estimate*, not an official appraisal. Buildings
with no Spanish cadastral match (outside Spain, unmapped parcel, etc.) fall
back to their OSM footprint area at a default price.
"""
import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from shapely.geometry import shape

from services import catastro

logger = logging.getLogger(__name__)

# Kept modest so a large affected-buildings set doesn't burst Catastro's free
# service hard enough to trigger throttling/connection resets (catastro.py
# retries transient failures, but fewer concurrent requests avoids
# provoking them in the first place).
MAX_WORKERS = 6

DEFAULT_PRICE_PER_M2_EUR = 2700  # rough market-average fallback; override per request

# Rough market price/m2 by Catastro land-use label (EUR). These are broad
# placeholder assumptions, not appraisals -- callers should tune them for
# their region.
_PRICE_PER_M2_EUR_BY_USE = {
    "residencial": 3200,
    "comercio": 2500,
    "oficinas": 2800,
    "hotelero": 4200,
    "ocio y hosteleria": 3000,
    "industrial": 900,
    "almacen": 900,
    "cultural": 2000,
    "deportivo": 1500,
    "religioso": 1500,
    "sanidad": 2200,
    "sanidad y beneficencia": 2200,
    "espectaculos": 2200,
    "singular": 2500,
    "suelo sin edificar": 800,
    "obras de urbanizacion y jardineria": 500,
}

_M_PER_DEG_LAT = 111_320.0


def _price_for_use(use, default_price):
    if not use:
        return default_price
    key = use.strip().lower()
    for name, price in _PRICE_PER_M2_EUR_BY_USE.items():
        if name in key or key in name:
            return price
    return default_price


def _footprint_area_m2(geom, levels=1.0):
    lat = geom.centroid.y
    m_per_deg_lon = _M_PER_DEG_LAT * math.cos(math.radians(lat))
    return geom.area * _M_PER_DEG_LAT * m_per_deg_lon * levels


def _parse_levels(value):
    """OSM 'building:levels' -> floor count, defaulting to 1 when missing
    or unparseable. Used to turn a ground-floor footprint into a rough
    total-built-area estimate, closer to what Catastro's own
    total_built_area_m2 represents (summed across all floors/units)."""
    try:
        levels = float(value)
        return levels if levels > 0 else 1.0
    except (TypeError, ValueError):
        return 1.0


def municipality_for_point(point, municipalities):
    """Spatial fallback for municipality name: which fetched admin boundary
    (see services/municipalities.py) contains this point, if any. More
    reliable than OSM's addr:city tag, which most buildings simply don't
    have set."""
    if not municipalities:
        return None
    for muni in municipalities:
        try:
            if shape(muni["geometry"]).contains(point):
                return muni["name"]
        except Exception:
            continue
    return None


def _fallback_result(osm_id, geom, rc, address, municipality, default_price, levels=1.0, source="osm_footprint_estimate"):
    area_m2 = _footprint_area_m2(geom, levels)
    return {
        "osm_id": osm_id,
        "rc": rc,
        "address": address,
        "municipality": municipality or "Others",
        "year_built": None,
        "built_area_m2": round(area_m2, 1),
        "use": None,
        "value_eur": round(area_m2 * default_price),
        # "catastro_rate_limited": Catastro's hourly per-IP quota was hit.
        # "catastro_error": some other request failure (timeout/5xx/network).
        # Neither is a real "no data here" result, just unknown.
        # "osm_footprint_estimate": Catastro cleanly reported no cadastral
        # parcel at this point (e.g. outside Spain).
        "source": source,
    }


def _lookup_building(feature, default_price, rate_limited_flag, municipalities=None):
    geom = shape(feature["geometry"])
    props = feature.get("properties", {})
    osm_id = props.get("osm_id")
    centroid = geom.centroid
    fallback_municipality = municipality_for_point(centroid, municipalities) or props.get("addr_city")
    levels = _parse_levels(props.get("building_levels"))

    # Once any request in this batch has hit Catastro's hourly quota, every
    # remaining request would fail the same way -- skip straight to the
    # fallback instead of continuing to hammer an already-blocked service.
    if rate_limited_flag.is_set():
        return _fallback_result(osm_id, geom, None, None, fallback_municipality, default_price, levels, source="catastro_rate_limited")

    try:
        rc, address = catastro.lookup_reference(centroid.x, centroid.y)
    except catastro.CatastroRateLimitError:
        rate_limited_flag.set()
        return _fallback_result(osm_id, geom, None, None, fallback_municipality, default_price, levels, source="catastro_rate_limited")
    except Exception:
        logger.warning("Catastro lookup_reference failed for osm_id=%s", osm_id, exc_info=True)
        return _fallback_result(osm_id, geom, None, None, fallback_municipality, default_price, levels, source="catastro_error")

    if not rc:
        return _fallback_result(osm_id, geom, None, None, fallback_municipality, default_price, levels)

    try:
        details = catastro.lookup_details(rc)
    except catastro.CatastroRateLimitError:
        rate_limited_flag.set()
        return _fallback_result(osm_id, geom, rc, address, fallback_municipality, default_price, levels, source="catastro_rate_limited")
    except Exception:
        logger.warning("Catastro lookup_details failed for osm_id=%s rc=%s", osm_id, rc, exc_info=True)
        return _fallback_result(osm_id, geom, rc, address, fallback_municipality, default_price, levels, source="catastro_error")

    if not details or not details.get("total_built_area_m2"):
        return _fallback_result(osm_id, geom, rc, address, fallback_municipality, default_price, levels)

    if details["constructions"]:
        value = sum(
            c["built_area_m2"] * _price_for_use(c["use"], default_price)
            for c in details["constructions"]
        )
    else:
        value = details["total_built_area_m2"] * _price_for_use(details["main_use"], default_price)

    return {
        "osm_id": osm_id,
        "rc": rc,
        "address": details.get("address") or address,
        "municipality": details.get("municipality") or fallback_municipality or "Others",
        "year_built": details.get("year_built"),
        "built_area_m2": round(details["total_built_area_m2"], 1),
        "use": details.get("main_use"),
        "value_eur": round(value),
        "source": "catastro",
    }


def group_by_municipality(results):
    totals = {}
    for r in results:
        name = r["municipality"] or "Others"
        entry = totals.setdefault(name, {"municipality": name, "buildings": 0, "value_eur": 0})
        entry["buildings"] += 1
        entry["value_eur"] += r["value_eur"]
    return sorted(totals.values(), key=lambda e: e["value_eur"], reverse=True)


def estimate_value_lost(buildings_geojson, default_price_per_m2=DEFAULT_PRICE_PER_M2_EUR, municipalities=None):
    features = buildings_geojson.get("features", [])
    rate_limited_flag = threading.Event()

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(_lookup_building, f, default_price_per_m2, rate_limited_flag, municipalities)
            for f in features
        ]
        for fut in as_completed(futures):
            results.append(fut.result())

    return {
        "buildings": results,
        "total_value_eur": round(sum(r["value_eur"] for r in results)),
        "buildings_priced": len(results),
        "buildings_matched_catastro": sum(1 for r in results if r["source"] == "catastro"),
        "buildings_errored": sum(1 for r in results if r["source"] == "catastro_error"),
        "buildings_rate_limited": sum(1 for r in results if r["source"] == "catastro_rate_limited"),
        "by_municipality": group_by_municipality(results),
    }
