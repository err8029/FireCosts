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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from shapely.geometry import shape
from shapely.prepared import prep

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


# Rough assumed footprint when a building has no real polygon to measure
# -- happens when services.buildings.fetch_building_centers was used
# (a burnt area too large for full OSM footprints, see app.py's
# ESTIMATE_MAX_QUERY_BBOX_DEG2), so this only ever applies to the
# no-Catastro-match fallback path for a building we only know the center
# point of. A single-family Spanish home footprint is roughly in this
# range; deliberately not trying to be more precise than that, since a
# building this route can't resolve via Catastro at all is already an
# edge case.
_DEFAULT_FOOTPRINT_M2 = 150.0


def _footprint_area_m2(geom, levels=1.0):
    if geom.area <= 0:
        return _DEFAULT_FOOTPRINT_M2 * levels
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


def municipality_for_point(point, prepared_municipalities):
    """Spatial fallback for municipality name: which fetched admin boundary
    (see services/municipalities.py, pre-shaped by _prepare_municipalities)
    contains this point, if any. More reliable than OSM's addr:city tag,
    which most buildings simply don't have set."""
    if not prepared_municipalities:
        return None
    for name, prepared_geom in prepared_municipalities:
        try:
            if prepared_geom.contains(point):
                return name
        except Exception:
            continue
    return None


def _prepare_municipalities(municipalities):
    """Pre-shape and prepare each municipality polygon once. Without this,
    checking N buildings against M municipalities re-parses and re-indexes
    the same GeoJSON geometry up to N times per municipality -- shapely's
    prepared geometries exist specifically for this "one geometry, many
    contains() queries" pattern (the same technique estimate.py already
    uses for the burnt-area/building intersection check)."""
    prepared = []
    for muni in municipalities or []:
        try:
            prepared.append((muni["name"], prep(shape(muni["geometry"]))))
        except Exception:
            continue
    return prepared


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


def _lookup_building(feature, default_price, rate_limited_flag, prepared_municipalities=None):
    geom = shape(feature["geometry"])
    props = feature.get("properties", {})
    osm_id = props.get("osm_id")
    centroid = geom.centroid
    levels = _parse_levels(props.get("building_levels"))

    # The spatial point-in-polygon scan is only needed as a fallback, and
    # Catastro almost always includes a municipality whenever it has any
    # match at all -- so this stays a plain function (evaluated only at
    # whichever single return statement below actually fires) instead of
    # running unconditionally for every building up front.
    def fallback_municipality():
        return municipality_for_point(centroid, prepared_municipalities) or props.get("addr_city")

    # Once any request in this batch has hit Catastro's hourly quota, every
    # remaining request would fail the same way -- skip straight to the
    # fallback instead of continuing to hammer an already-blocked service.
    if rate_limited_flag.is_set():
        return _fallback_result(osm_id, geom, None, None, fallback_municipality(), default_price, levels, source="catastro_rate_limited")

    try:
        rc, address = catastro.lookup_reference(centroid.x, centroid.y)
    except catastro.CatastroRateLimitError:
        rate_limited_flag.set()
        return _fallback_result(osm_id, geom, None, None, fallback_municipality(), default_price, levels, source="catastro_rate_limited")
    except Exception:
        logger.warning("Catastro lookup_reference failed for osm_id=%s", osm_id, exc_info=True)
        return _fallback_result(osm_id, geom, None, None, fallback_municipality(), default_price, levels, source="catastro_error")

    if not rc:
        return _fallback_result(osm_id, geom, None, None, fallback_municipality(), default_price, levels)

    try:
        details = catastro.lookup_details(rc)
    except catastro.CatastroRateLimitError:
        rate_limited_flag.set()
        return _fallback_result(osm_id, geom, rc, address, fallback_municipality(), default_price, levels, source="catastro_rate_limited")
    except Exception:
        logger.warning("Catastro lookup_details failed for osm_id=%s rc=%s", osm_id, rc, exc_info=True)
        return _fallback_result(osm_id, geom, rc, address, fallback_municipality(), default_price, levels, source="catastro_error")

    if not details or not details.get("total_built_area_m2"):
        return _fallback_result(osm_id, geom, rc, address, fallback_municipality(), default_price, levels)

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
        "municipality": details.get("municipality") or fallback_municipality() or "Others",
        "year_built": details.get("year_built"),
        "built_area_m2": round(details["total_built_area_m2"], 1),
        "use": details.get("main_use"),
        "value_eur": round(value),
        "source": "catastro",
    }


def normalize_municipality_name(name):
    """Case/accent-insensitive key for merging the same place name that
    shows up differently-cased across sources -- Catastro's municipio
    field comes back upper-case (e.g. "MADRID"), while OpenStreetMap
    administrative boundary names use normal case (e.g. "Madrid"), which
    would otherwise split one city into two rows in the value-lost table."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.strip().lower()


def group_by_municipality(results):
    totals = {}
    for r in results:
        name = r["municipality"] or "Others"
        key = normalize_municipality_name(name)
        entry = totals.get(key)
        if entry is None:
            totals[key] = {"municipality": name, "buildings": 1, "value_eur": r["value_eur"]}
            continue
        # Prefer a properly-cased display name (e.g. "Madrid") over an
        # ALL-CAPS one (e.g. "MADRID") if both show up for the same place.
        if entry["municipality"].isupper() and not name.isupper():
            entry["municipality"] = name
        entry["buildings"] += 1
        entry["value_eur"] += r["value_eur"]
    return sorted(totals.values(), key=lambda e: e["value_eur"], reverse=True)


def estimate_value_lost(buildings_geojson, default_price_per_m2=DEFAULT_PRICE_PER_M2_EUR, municipalities=None):
    features = buildings_geojson.get("features", [])
    rate_limited_flag = threading.Event()
    # Prepared once up front rather than per building -- see
    # _prepare_municipalities -- since every building's lookup would
    # otherwise re-shape and re-index the same set of municipality
    # polygons independently (and concurrently, across worker threads).
    prepared_municipalities = _prepare_municipalities(municipalities)

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(_lookup_building, f, default_price_per_m2, rate_limited_flag, prepared_municipalities)
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
