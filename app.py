import datetime as dt
import logging

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response
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

    try:
        all_buildings = buildings_service.fetch_buildings(bbox)
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

    result = valuation_service.estimate_value_lost(buildings, default_price)
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
    municipalities = municipalities_service.fetch_municipality_boundaries(bbox)

    png_bytes = report_service.render_report_png(
        fires=fires,
        burnt_area=payload.get("burnt_area"),
        affected_buildings=affected_buildings,
        valuation=payload.get("valuation"),
        municipalities=municipalities,
        meta=meta,
    )

    return Response(
        png_bytes,
        mimetype="image/png",
        headers={"Content-Disposition": "attachment; filename=fire_report.png"},
    )


if __name__ == "__main__":
    app.run(debug=True)
