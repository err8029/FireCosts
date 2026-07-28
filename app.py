import datetime as dt
import logging
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
        # Nothing burning in this box -- no building can be "affected", so
        # skip the OSM building query entirely instead of fetching every
        # building in the whole drawn box just to report a count against
        # zero fires.
        affected, total_count, affected_count = [], 0, 0
    else:
        # Only buildings near the burnt area can ever end up "affected" --
        # querying just that (small) bounding box instead of the whole
        # drawn box is what keeps this fast even when the box is large but
        # the fire itself covers a tiny fraction of it. Small pad (~500m)
        # comfortably covers a building whose footprint straddles the edge
        # of the burnt-area bounds even though its centroid falls inside.
        west, south, east, north = shape(burnt_area).bounds
        pad = 0.005
        query_bbox = (west - pad, south - pad, east + pad, north + pad)
        try:
            all_buildings = buildings_service.fetch_buildings(query_bbox)
        except buildings_service.BuildingsError as exc:
            return jsonify({"error": str(exc)}), 400
        affected, total_count, affected_count = estimate_service.buildings_in_burnt_area(
            all_buildings, burnt_area
        )

    return jsonify({
        "fires": fires,
        "burnt_area": burnt_area,
        "burnt_area_km2": round(area_km2, 3),
        "buildings_total": total_count,
        "buildings_affected": affected_count,
        "affected_buildings": {"type": "FeatureCollection", "features": affected},
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
    # proceeds, just without this extra classification.
    municipalities = []
    try:
        geoms = [shape(f["geometry"]) for f in buildings["features"]]
        west, south, east, north = unary_union(geoms).bounds
        pad = 0.01
        municipalities = municipalities_service.fetch_municipality_boundaries(
            (west - pad, south - pad, east + pad, north + pad)
        )
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
    LOOKUP_DEADLINE_S = 12
    pool = ThreadPoolExecutor(max_workers=2)
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

    png_bytes = report_service.render_report_png(
        fires=fires,
        burnt_area=payload.get("burnt_area"),
        affected_buildings=affected_buildings,
        valuation=payload.get("valuation"),
        municipalities=municipalities,
        country=locator_context["country"],
        region=locator_context["region"],
        meta=meta,
    )

    return Response(
        png_bytes,
        mimetype="image/png",
        headers={"Content-Disposition": "attachment; filename=fire_report.png"},
    )


if __name__ == "__main__":
    app.run(debug=True)
