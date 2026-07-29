import datetime as dt
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response
from shapely.geometry import shape
from shapely.ops import unary_union
from werkzeug.exceptions import HTTPException

from services import buildings as buildings_service
from services import estimate as estimate_service
from services import firms as firms_service
from services import geocode as geocode_service
from services import landcover as landcover_service
from services import municipalities as municipalities_service
from services import report as report_service
from services import valuation as valuation_service

load_dotenv()

app = Flask(__name__)
logger = logging.getLogger(__name__)


@app.errorhandler(HTTPException)
def handle_http_exception(exc):
    return jsonify({"error": exc.description}), exc.code


@app.errorhandler(Exception)
def handle_unexpected_exception(exc):
    # Every /api/* route is called by fetch() expecting JSON. Without this,
    # an unhandled exception (e.g. Overpass/FIRMS/Catastro timing out with
    # something other than the specific error types we catch below) falls
    # through to Flask's default HTML error page, which breaks the frontend
    # with "Unexpected token '<' ... is not valid JSON" instead of a
    # readable message.
    logger.exception("Unhandled exception in %s", request.path)
    return jsonify({"error": f"Unexpected server error: {exc}"}), 500


def _parse_bbox(args):
    raw = args.get("bbox")
    if not raw:
        raise ValueError("Missing 'bbox' query param (west,south,east,north)")
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("'bbox' must have 4 comma-separated values: west,south,east,north")
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        raise ValueError("'bbox' values must be numbers")
    if west >= east or south >= north:
        raise ValueError("'bbox' is invalid: west<east and south<north required")

    # Reject an oversized box immediately, before any slow external call --
    # otherwise a giant analysis can run long enough to trip the hosting
    # platform's own gateway timeout, which shows up to the user as an
    # opaque 502 with no useful explanation.
    area_deg2 = (east - west) * (north - south)
    if area_deg2 > buildings_service.MAX_BBOX_DEG2:
        raise ValueError(
            "Selected area is too large to analyze (roughly 50km x 50km max). "
            "Please draw a smaller box."
        )
    return (west, south, east, north)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/fires")
def api_fires():
    try:
        bbox = _parse_bbox(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    start_date = request.args.get("start", dt.date.today().isoformat())
    day_range = int(request.args.get("days", 1))
    source = request.args.get("source", "VIIRS_SNPP_NRT")

    try:
        fires = firms_service.fetch_active_fires(bbox, start_date, day_range, source)
    except firms_service.FirmsError as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(fires)


# Roughly mainland Spain + Balearics, used only for the active-fire
# "pointer" overview shown on first load (before the user has drawn any
# box of their own) -- see api_active_fires_overview below. FIRMS's
# area/csv endpoint caps a single request's box at 20 degrees in each
# direction, comfortably above this ~14 x 8 degree extent, so it's one
# request rather than needing firms.fetch_active_fires' own chunking.
SPAIN_BBOX = (-9.5, 35.9, 4.6, 43.9)
MAX_OVERVIEW_CLUSTERS = 15
GEOCODE_DEADLINE_S = 8


@app.route("/api/active-fires-overview")
def api_active_fires_overview():
    """Return a handful of rough {"lat","lon","count","max_frp","start_date",
    "end_date","bbox"} clusters for the active fires currently detected
    across Spain, so the map can show the user roughly where to draw their
    analysis box before they've picked an area -- e.g. one pointer near
    "Sierra Oeste" instead of nothing until they already know where to
    look.

    Deliberately coarse and best-effort: this is a "here's roughly where
    to look" guide, not the precise per-fire data /api/fires and
    /api/estimate provide once the user has drawn a box.

    Deliberately does *not* reverse-geocode a place name here -- that used
    to run eagerly for every cluster before this ever returned, one
    request at a time (Nominatim's usage policy caps it at 1/second), so
    the very first thing a user saw was however long that took (up to
    MAX_OVERVIEW_CLUSTERS seconds). See /api/reverse-geocode below: the
    frontend fetches a label lazily, per marker, only when the user
    actually hovers over it -- markers themselves show up as soon as
    FIRMS responds, and most users never hover every single one.
    """
    start_date = request.args.get("start", dt.date.today().isoformat())
    day_range = int(request.args.get("days", 1))
    source = request.args.get("source", "VIIRS_SNPP_NRT")

    try:
        fires = firms_service.fetch_active_fires(SPAIN_BBOX, start_date, day_range, source)
    except firms_service.FirmsError as exc:
        return jsonify({"error": str(exc)}), 502

    clusters = firms_service.cluster_fires(fires)[:MAX_OVERVIEW_CLUSTERS]
    return jsonify({"clusters": clusters})


@app.route("/api/reverse-geocode")
def api_reverse_geocode():
    """Return {"label": str|None} for a single point -- used to lazily
    label one active-fire pointer on hover (see api_active_fires_overview
    above for why this isn't done eagerly for all of them up front).
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"error": "'lat' and 'lon' query params are required numbers"}), 400

    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(geocode_service.reverse_geocode, lat, lon)
    futures_wait([future], timeout=GEOCODE_DEADLINE_S)
    label = future.result() if future.done() else None
    pool.shutdown(wait=False)

    return jsonify({"label": label})


@app.route("/api/buildings")
def api_buildings():
    try:
        bbox = _parse_bbox(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        result = buildings_service.fetch_buildings(bbox)
    except buildings_service.BuildingsError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(result)


@app.route("/api/estimate")
def api_estimate():
    try:
        bbox = _parse_bbox(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    start_date = request.args.get("start", dt.date.today().isoformat())
    day_range = int(request.args.get("days", 1))
    source = request.args.get("source", "VIIRS_SNPP_NRT")

    try:
        fires = firms_service.fetch_active_fires(bbox, start_date, day_range, source)
    except firms_service.FirmsError as exc:
        return jsonify({"error": str(exc)}), 502

    burnt_area, area_km2 = estimate_service.build_burnt_area(fires, source=source)

    if burnt_area is None:
        # Nothing burning in this box -- no building or vegetation stat can
        # be "affected", so skip both OSM queries entirely instead of
        # fetching everything in the whole drawn box just to report counts
        # against zero fires.
        affected, total_count, affected_count = [], 0, 0
        vegetation_features, vegetation_km2, vegetation_by_category = [], 0.0, {}
    else:
        # Only buildings/vegetation near the burnt area can ever end up
        # "affected" -- querying just that (small) bounding box instead of
        # the whole drawn box is what keeps this fast even when the box is
        # large but the fire itself covers a tiny fraction of it. Small pad
        # (~500m) comfortably covers a building whose footprint straddles
        # the edge of the burnt-area bounds even though its centroid falls
        # inside.
        west, south, east, north = shape(burnt_area).bounds
        pad = 0.005
        query_bbox = (west - pad, south - pad, east + pad, north + pad)

        # Buildings and vegetation are independent OSM queries -- fetch
        # them concurrently rather than back to back. A hard wall-clock
        # deadline bounds the total wait regardless of how long Overpass's
        # own internal retry/mirror-fallback ends up taking -- confirmed
        # directly that Overpass can take 20s+ to even fail a single
        # simple query on a bad day, and this call previously had no
        # deadline at all, so a bad Overpass day could hang the whole
        # Analyze step for minutes with the progress indicator stuck
        # waiting. Buildings are essential to the analysis, so a timeout
        # here is a real error; vegetation is supplementary and degrades
        # to empty instead, same reasoning as the municipality/locator
        # lookups in /api/report. Deliberately not using the executor as
        # a context manager, since `with` would block on shutdown()
        # waiting for a fetch we've already decided to give up on.
        #
        # Overridable via env var: a hosting environment's outbound route
        # to Overpass can genuinely be slower/more rate-limited than a
        # home connection (e.g. observed slower + vegetation degrading to
        # empty more often on Render's free tier than locally) -- letting
        # this be tuned per-deployment avoids needing a code change/redeploy
        # just to grant it more patience.
        FETCH_DEADLINE_S = int(os.environ.get("ESTIMATE_FETCH_DEADLINE_S", 30))
        pool = ThreadPoolExecutor(max_workers=2)
        buildings_future = pool.submit(buildings_service.fetch_buildings, query_bbox)
        vegetation_future = pool.submit(landcover_service.fetch_vegetation, query_bbox)

        futures_wait([buildings_future, vegetation_future], timeout=FETCH_DEADLINE_S)

        if not buildings_future.done():
            pool.shutdown(wait=False)
            return jsonify({
                "error": "OpenStreetMap's building data service (Overpass) did not respond "
                         "in time. Try a smaller area, or try again shortly."
            }), 502

        try:
            all_buildings = buildings_future.result()
        except buildings_service.BuildingsError as exc:
            pool.shutdown(wait=False)
            return jsonify({"error": str(exc)}), 400

        if vegetation_future.done():
            try:
                vegetation_geojson = vegetation_future.result()
            except landcover_service.LandcoverError:
                logger.warning("Vegetation lookup failed for estimate", exc_info=True)
                vegetation_geojson = {"type": "FeatureCollection", "features": []}
        else:
            logger.warning("Vegetation lookup timed out for estimate")
            vegetation_geojson = {"type": "FeatureCollection", "features": []}
        pool.shutdown(wait=False)

        affected, total_count, affected_count = estimate_service.buildings_in_burnt_area(
            all_buildings, burnt_area
        )
        vegetation_features, vegetation_km2, vegetation_by_category = estimate_service.vegetation_in_burnt_area(
            vegetation_geojson, burnt_area
        )

    return jsonify({
        "fires": fires,
        "burnt_area": burnt_area,
        "burnt_area_km2": round(area_km2, 3),
        "buildings_total": total_count,
        "buildings_affected": affected_count,
        "affected_buildings": {"type": "FeatureCollection", "features": affected},
        "vegetation_burnt_km2": round(vegetation_km2, 3),
        "vegetation_by_category": {k: round(v, 3) for k, v in vegetation_by_category.items()},
        "burnt_vegetation": {"type": "FeatureCollection", "features": vegetation_features},
    })


@app.route("/api/valuation", methods=["POST"])
def api_valuation():
    payload = request.get_json(silent=True) or {}
    buildings = payload.get("buildings")
    if not buildings or not buildings.get("features"):
        return jsonify({"error": "No buildings provided"}), 400

    try:
        default_price = float(
            payload.get("default_price_per_m2", valuation_service.DEFAULT_PRICE_PER_M2_EUR)
        )
    except (TypeError, ValueError):
        return jsonify({"error": "'default_price_per_m2' must be a number"}), 400

    # Real municipality boundaries give a much better "municipality" label
    # for buildings that fall back to the OSM-estimate path (no Catastro
    # match) than OSM's addr:city tag, which most buildings don't have set.
    # Best-effort: if Overpass is unavailable/rate-limited, valuation still
    # proceeds, just without this extra classification -- but that was only
    # true in spirit until now: this call had no actual deadline, so a slow
    # Overpass day (confirmed directly: a single query can take 100s+ once
    # its internal retries/mirror-fallback all end up timing out rather
    # than failing cleanly) could hang the whole "Estimate value lost"
    # request for minutes. Bounded the same way as the lookups in
    # /api/report.
    municipalities = []
    try:
        geoms = [shape(f["geometry"]) for f in buildings["features"]]
        west, south, east, north = unary_union(geoms).bounds
        pad = 0.01
        muni_bbox = (west - pad, south - pad, east + pad, north + pad)

        pool = ThreadPoolExecutor(max_workers=1)
        muni_future = pool.submit(municipalities_service.fetch_municipality_boundaries, muni_bbox)
        futures_wait([muni_future], timeout=12)
        if muni_future.done():
            municipalities = muni_future.result()
        else:
            logger.warning("Municipality boundary lookup timed out for valuation")
        pool.shutdown(wait=False)
    except Exception:
        logger.warning("Municipality boundary lookup failed for valuation", exc_info=True)

    result = valuation_service.estimate_value_lost(buildings, default_price, municipalities=municipalities)
    return jsonify(result)


@app.route("/api/report", methods=["POST"])
def api_report():
    payload = request.get_json(silent=True) or {}

    fires = payload.get("fires")
    affected_buildings = payload.get("affected_buildings")
    meta = payload.get("meta") or {}

    if not affected_buildings:
        return jsonify({"error": "Missing 'affected_buildings'"}), 400
    try:
        bbox = tuple(float(v) for v in meta.get("bbox", []))
        if len(bbox) != 4:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Missing/invalid 'meta.bbox'"}), 400
    meta["bbox"] = bbox

    # Scope the municipality-boundary fetch to near the burnt area rather
    # than the whole drawn box. Confirmed directly against Overpass: the
    # same admin_level=8 query over a large (~80km) drawn box hit a 504
    # from Overpass's own server (it's genuinely too expensive), while the
    # same query padded around a small burnt area resolved in ~5s. Skipped
    # entirely when there's no burnt area to anchor it to -- with no fire,
    # there's nothing for these boundaries to add.
    burnt_area_geojson = payload.get("burnt_area")
    municipality_bbox = None
    if burnt_area_geojson:
        try:
            mwest, msouth, meast, mnorth = shape(burnt_area_geojson).bounds
            pad = 0.1
            municipality_bbox = (mwest - pad, msouth - pad, meast + pad, mnorth + pad)
        except Exception:
            logger.warning("Could not derive municipality bbox from burnt area", exc_info=True)

    # These two Overpass lookups don't depend on each other -- run them
    # concurrently rather than back to back, same reasoning as the
    # FIRMS/buildings fetch in /api/estimate. Both are purely decorative
    # (boundary outlines, locator inset) and already degrade gracefully to
    # "skip it" on failure -- but Overpass itself can take 30-90+ seconds
    # to time out and retry across its two mirrors when it's having a bad
    # day (measured directly), and none of that is worth making the user
    # wait for. A single combined wait() call bounds TOTAL added latency to
    # one deadline regardless of how many lookups are pending -- calling
    # .result(timeout=...) on each future separately would instead let the
    # deadline apply per-future and stack up (12s + 12s = 24s), which is
    # exactly the bug an earlier version of this had. Deliberately not
    # using the executor as a context manager, since `with` would block on
    # shutdown() waiting for a lookup we've already decided to give up on.
    #
    # 25s, not 12s: fetch_locator_context's query is inherently heavier
    # than a municipality-boundary lookup -- it pulls full country *and*
    # region boundary geometry (out geom), and measured directly at ~18s
    # even on a healthy connection. A 12s deadline meant the locator
    # inset (country/region minimap with a star on the analysis
    # location) was being silently abandoned on essentially every report,
    # not just on a bad Overpass day. Overridable via env var for the same
    # reason as ESTIMATE_FETCH_DEADLINE_S above -- a slower hosting
    # environment may need more room than a fixed constant can give it.
    LOOKUP_DEADLINE_S = int(os.environ.get("REPORT_LOOKUP_DEADLINE_S", 25))
    pool = ThreadPoolExecutor(max_workers=4)
    municipalities_future = (
        pool.submit(municipalities_service.fetch_municipality_boundaries, municipality_bbox)
        if municipality_bbox else None
    )
    locator_future = pool.submit(municipalities_service.fetch_locator_context, bbox)

    futures_wait(
        [f for f in (municipalities_future, locator_future) if f is not None],
        timeout=LOOKUP_DEADLINE_S,
    )

    municipalities = []
    if municipalities_future is not None:
        if municipalities_future.done():
            try:
                municipalities = municipalities_future.result()
            except Exception:
                logger.warning("Municipality boundary lookup failed for report", exc_info=True)
        else:
            logger.warning("Municipality boundary lookup timed out for report")

    if locator_future.done():
        try:
            locator_context = locator_future.result()
        except Exception:
            logger.warning("Locator context lookup failed for report inset", exc_info=True)
            locator_context = {"country": None, "region": None}
    else:
        logger.warning("Locator context lookup timed out for report inset")
        locator_context = {"country": None, "region": None}
    pool.shutdown(wait=False)

    # Unlike buildings (which usually get their municipality straight from
    # Catastro), burnt vegetation has no such direct source -- it relies
    # entirely on the boundary list above, whose bbox-intersects-linework
    # query only finds a municipality when its edge happens to cross the
    # (small, burnt-area-sized) query box. A vegetation polygon sitting
    # deep inside one large rural municipality with no bordering neighbor
    # nearby would otherwise never get classified at all. Resolve any
    # vegetation centroid the boundary list doesn't already cover with a
    # direct point-in-polygon lookup instead, same deadline-bounded
    # pattern as above.
    vegetation_geojson = payload.get("burnt_vegetation") or {}
    veg_features = vegetation_geojson.get("features", [])
    if veg_features:
        prepared_municipalities = valuation_service._prepare_municipalities(municipalities)
        point_by_key = {}
        features_by_key = {}
        for feat in veg_features:
            try:
                centroid = shape(feat["geometry"]).centroid
            except Exception:
                continue
            name = valuation_service.municipality_for_point(centroid, prepared_municipalities)
            if name:
                # Cache the match directly on the feature so report.py uses
                # it as-is (see below for why re-deriving it there is the
                # actual bug being avoided).
                feat.setdefault("properties", {})["municipality"] = name
                continue
            # Rounded to the same ~1km precision as
            # municipalities._point_key, not finer -- otherwise two
            # vegetation patches a few hundred meters apart (commonly the
            # same municipality) would round to different keys here, each
            # firing its own live Overpass query even though they'd hit
            # the exact same cache entry once queried. That mismatch was
            # multiplying Overpass load for no benefit.
            key = (round(centroid.y, 2), round(centroid.x, 2))
            point_by_key[key] = centroid
            features_by_key.setdefault(key, []).append(feat)

        # Bound worst-case query complexity regardless of how many distinct
        # unmatched points a fragmented burnt area produces -- the
        # remaining ones simply stay "Others" rather than growing the
        # batched query below without limit.
        MAX_LIVE_MUNICIPALITY_LOOKUPS = 20
        if len(point_by_key) > MAX_LIVE_MUNICIPALITY_LOOKUPS:
            point_by_key = dict(list(point_by_key.items())[:MAX_LIVE_MUNICIPALITY_LOOKUPS])

        if point_by_key:
            # One batched, cached Overpass request for every remaining
            # point instead of one live request per point -- see
            # municipalities_service.fetch_municipalities_for_points for
            # why this is the actual fix for report generation being slow
            # (each separate round-trip pays the same network latency,
            # which this app has seen spike into the tens of seconds
            # during Overpass service degradation, so N points used to
            # mean N times that latency instead of 1).
            keys = list(point_by_key.keys())
            veg_pool = ThreadPoolExecutor(max_workers=1)
            veg_future = veg_pool.submit(
                municipalities_service.fetch_municipalities_for_points,
                [(point_by_key[k].y, point_by_key[k].x) for k in keys],
            )
            futures_wait([veg_future], timeout=LOOKUP_DEADLINE_S)
            found_per_point = veg_future.result() if veg_future.done() else [None] * len(keys)
            veg_pool.shutdown(wait=False)

            for key, found in zip(keys, found_per_point):
                if not found:
                    continue
                for feat in features_by_key[key]:
                    feat.setdefault("properties", {})["municipality"] = found["name"]
                municipalities.append(found)

    vegetation_price_per_hectare = {}
    for category, price in (payload.get("vegetation_price_per_hectare") or {}).items():
        try:
            vegetation_price_per_hectare[category] = float(price)
        except (TypeError, ValueError):
            continue

    png_bytes = report_service.render_report_png(
        fires=fires,
        burnt_area=payload.get("burnt_area"),
        affected_buildings=affected_buildings,
        vegetation=payload.get("burnt_vegetation"),
        vegetation_km2=payload.get("vegetation_burnt_km2") or 0,
        valuation=payload.get("valuation"),
        municipalities=municipalities,
        country=locator_context["country"],
        region=locator_context["region"],
        meta=meta,
        vegetation_price_per_hectare=vegetation_price_per_hectare,
    )

    return Response(
        png_bytes,
        mimetype="image/png",
        headers={"Content-Disposition": "attachment; filename=fire_report.png"},
    )


if __name__ == "__main__":
    app.run(debug=True)
