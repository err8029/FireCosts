"""Render a static PNG report: map (fires, burnt area, affected buildings
colored by estimated value) with a title/legend, plus a value-lost-by-
municipality table.

The map and table are rendered as two independent figures and then stacked,
rather than as subplots of one figure. A map axes with equal aspect (needed
so lon/lat don't look stretched) shrinks to fit its allocated box, and
matplotlib's `bbox_inches="tight"` only trims a figure's outer margins, not
dead space *between* subplots -- so a shared-figure layout leaves a large
blank gap whenever the map's aspect-corrected box doesn't fill its cell.
Two independently-cropped figures avoids that.
"""
import io
import math
import unicodedata

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
import numpy as np
from matplotlib import image as mpimg
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.patches import Polygon as MplPolygon
from matplotlib.collections import PatchCollection
from matplotlib.lines import Line2D
from shapely.geometry import box, shape

from services import basemap as basemap_service
from services import estimate as estimate_service
from services import valuation as valuation_service

_M_PER_DEG_LAT = 111_320.0

# Orange -> red scale for per-building value markers (avoids the pale-yellow
# low end of stock colormaps like YlOrRd, per the requested "orange to red").
_VALUE_CMAP = LinearSegmentedColormap.from_list("orange_red", ["#ffa94d", "#8b0000"])


def _polygon_patches(geometry):
    """Yield matplotlib Polygon patches for a GeoJSON Polygon/MultiPolygon."""
    geom_type = geometry.get("type")
    if geom_type == "Polygon":
        rings = [geometry["coordinates"]]
    elif geom_type == "MultiPolygon":
        rings = geometry["coordinates"]
    else:
        return
    for polygon in rings:
        yield MplPolygon(polygon[0], closed=True)


def _add_basemap(ax, bbox, layer):
    """Fetch and draw a tile basemap (OSM or satellite) behind everything
    else. Returns True if a basemap was drawn, so the caller can render
    the matching attribution."""
    result = basemap_service.fetch_basemap(bbox, layer=layer)
    if not result:
        return False
    mosaic, (west, east, south, north) = result
    ax.imshow(mosaic, extent=(west, east, south, north), origin="upper",
              zorder=0, interpolation="bilinear")
    return True


def _normalize_name(name):
    """Case/accent-insensitive key for matching a municipality name from
    OSM (mixed case, e.g. "San Martín de Valdeiglesias") against the same
    place as it appears in the value-lost breakdown (Catastro's municipio
    name, which comes back upper-case, e.g. "SAN MARTIN DE VALDEIGLESIAS")."""
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return stripped.strip().lower()


def _top_municipality_names(valuation, limit=5):
    """Normalized names of the top `limit` municipalities by value lost, or
    None if there's no valuation to rank by (caller should show all)."""
    if not valuation or not valuation.get("by_municipality"):
        return None
    return {_normalize_name(m["municipality"]) for m in valuation["by_municipality"][:limit]}


def _reclassify_municipalities(valuation, affected_buildings, municipalities):
    """Give buildings stuck in the "Others" bucket a second chance at a real
    municipality name using the boundaries fetched for *this* report.

    "Estimate value lost" and "Export report" are separate user actions,
    often minutes apart, each fetching municipality boundaries from
    Overpass independently. If that fetch failed or was rate-limited during
    valuation, every building falls back to "Others" with no way to recover
    short of re-running the estimate -- even though the boundaries needed
    to classify them might fetch just fine right now, for the report. This
    re-attempts the same point-in-polygon lookup valuation.py uses, so a
    transient failure earlier doesn't have to mean "Others" forever.
    """
    if not valuation or not municipalities:
        return valuation

    geom_by_osm_id = {}
    for feat in (affected_buildings or {}).get("features", []):
        osm_id = feat.get("properties", {}).get("osm_id")
        if osm_id is not None:
            geom_by_osm_id[osm_id] = shape(feat["geometry"])

    updated_buildings = []
    changed = False
    for b in valuation["buildings"]:
        if not b.get("municipality") or b["municipality"] == "Others":
            geom = geom_by_osm_id.get(b.get("osm_id"))
            if geom is not None:
                found = valuation_service.municipality_for_point(geom.centroid, municipalities)
                if found:
                    b = {**b, "municipality": found}
                    changed = True
        updated_buildings.append(b)

    if not changed:
        return valuation

    return {
        **valuation,
        "buildings": updated_buildings,
        "by_municipality": valuation_service.group_by_municipality(updated_buildings),
    }


def _add_municipality_boundaries(ax, municipalities, bbox, top_names=None, max_count=15):
    """Draw municipality boundary outlines. If top_names is given (from
    _top_municipality_names), only municipalities in that set are drawn --
    keeps the map to the handful that actually matter instead of every
    municipality Overpass returns for the bbox, which in rural areas can be
    a lot of mostly-irrelevant slivers.

    Returns (drawn: bool, label_candidates: [(name, x, y), ...]). Label text
    is drawn separately by _draw_municipality_labels.
    """
    if not municipalities:
        return False, []

    if top_names is not None:
        municipalities = [m for m in municipalities if _normalize_name(m["name"]) in top_names]

    view_box = box(*bbox)
    drawn = False
    label_candidates = []
    for muni in municipalities[:max_count]:
        geom = shape(muni["geometry"])
        patches = list(_polygon_patches(muni["geometry"]))
        for patch in patches:
            patch.set_facecolor("none")
            patch.set_edgecolor("#6a3d9a")
            patch.set_linewidth(1.3)
            patch.set_linestyle("--")
            patch.set_zorder(2)
            ax.add_patch(patch)
            drawn = True

        # A municipality's own representative_point() can land anywhere in
        # its (possibly huge, rural) full extent -- often well outside a
        # tightly-cropped map even though the boundary itself passes
        # through the visible area. Use the point inside *both* the
        # municipality and the visible bbox instead, so a label only gets
        # skipped when the municipality truly isn't visible at all.
        visible_part = geom.intersection(view_box)
        if visible_part.is_empty:
            continue
        label_point = visible_part.representative_point()
        label_candidates.append((muni["name"], label_point.x, label_point.y))

    return drawn, label_candidates


def _draw_municipality_labels(ax, label_candidates):
    """Draw the deferred municipality name labels. (No longer needs to dodge
    the legend -- that now renders in its own box below the map instead of
    on top of it.)"""
    for name, x, y in label_candidates:
        text = ax.text(
            x, y, name, fontsize=8, fontweight="bold", color="#6a3d9a",
            ha="center", va="center", zorder=6,
        )
        text.set_path_effects([
            path_effects.Stroke(linewidth=2.5, foreground="white"),
            path_effects.Normal(),
        ])


def _mainland_bounds(geom):
    """Bounds of a geometry's largest polygon by area, not its overall
    MultiPolygon bbox -- Spain's admin boundary includes the Canary
    Islands, ~1000km southwest of the mainland, so the raw bbox would be
    dominated by that gap and make the mainland (and the region within it)
    a speck when used to set a zoom extent. Returns None for an empty
    geometry."""
    if geom.is_empty:
        return None
    if geom.geom_type == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return geom.bounds


def _add_locator_inset(ax, country, region, analysis_bbox):
    """Zoomed-out inset in the map's top-right corner: the country (faint
    background context) and the containing region (Comunidad Autonoma --
    geometrically the same boundary as its NUTS2 statistical region)
    highlighted, with the analysis box's location marked, so a viewer
    unfamiliar with the exact area can see where it sits. Skipped entirely
    if no region was found (e.g. bbox isn't in Spain, or Overpass was
    unavailable).

    Zoomed to fit the country's mainland (see _mainland_bounds) when
    available, so the country's actual outline/coastline is visible rather
    than just a flat fill color -- falling back to a wide margin around
    just the region if there's no country geometry to work with.
    """
    if not region:
        return

    inset = ax.inset_axes([0.66, 0.66, 0.32, 0.32])
    inset.set_facecolor("#f5f5f0")
    inset.set_zorder(20)

    country_bounds = None
    if country:
        for patch in _polygon_patches(country["geometry"]):
            patch.set_facecolor("#e8e4d8")
            patch.set_edgecolor("#999999")
            patch.set_linewidth(0.5)
            inset.add_patch(patch)
        country_bounds = _mainland_bounds(shape(country["geometry"]))

    region_geom = shape(region["geometry"])
    for patch in _polygon_patches(region["geometry"]):
        patch.set_facecolor("#d8b8c8")
        patch.set_edgecolor("#555555")
        patch.set_linewidth(1.0)
        inset.add_patch(patch)

    west, south, east, north = analysis_bbox
    center_x, center_y = (west + east) / 2, (south + north) / 2
    inset.scatter(
        [center_x], [center_y], marker="*", s=110, color="#e31a1c",
        edgecolors="#7a0000", linewidths=0.6, zorder=5,
    )

    if country_bounds:
        minx, miny, maxx, maxy = country_bounds
        pad_frac = 0.06
    else:
        minx, miny, maxx, maxy = region_geom.bounds
        pad_frac = 1.0  # no country outline to lean on -- zoom out further around just the region
    pad_x = (maxx - minx) * pad_frac or 0.1
    pad_y = (maxy - miny) * pad_frac or 0.1
    inset.set_xlim(minx - pad_x, maxx + pad_x)
    inset.set_ylim(miny - pad_y, maxy + pad_y)
    inset.set_aspect(1 / max(math.cos(math.radians((miny + maxy) / 2)), 0.15))
    inset.set_xticks([])
    inset.set_yticks([])
    for spine in inset.spines.values():
        spine.set_edgecolor("#555555")
        spine.set_linewidth(0.8)

    inset.set_title(region.get("name", ""), fontsize=6, pad=2)


def _add_burnt_area(ax, burnt_area_geojson):
    if not burnt_area_geojson:
        return
    patches = list(_polygon_patches(burnt_area_geojson))
    if not patches:
        return
    ax.add_collection(PatchCollection(
        patches, facecolor="#ff6b6b", edgecolor="#c0392b", alpha=0.25, linewidth=1, zorder=1,
    ))


def _sort_by_value_ascending(xs, ys, values):
    """Sort points so the highest value is plotted last -- within a single
    scatter call, later points are drawn on top of earlier ones, so this
    keeps the highest-value markers visible instead of them being randomly
    buried under lower-value ones that happen to overlap."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    return ([xs[i] for i in order], [ys[i] for i in order], [values[i] for i in order])


def _sizes_from_value(values, norm, min_size, max_size):
    """Proportional-symbol sizing: higher value -> visibly bigger marker,
    on top of the color scale, so the highest-value spots read clearly even
    in a small static image."""
    return [min_size + (max_size - min_size) * float(norm(v)) for v in values]


def _add_buildings(ax, buildings_geojson, value_by_osm_id, norm):
    """Draw affected building footprints as outlines (for shape context),
    plus -- when value data is available -- a circle at each building's
    centroid colored and sized on an orange-to-red scale by its estimated
    value, with the highest-value circles drawn on top. Returns the circle
    scatter (for the colorbar), or None if there's no per-building value data."""
    features = buildings_geojson.get("features", []) if buildings_geojson else []
    if not features:
        return None

    patches = []
    for feat in features:
        patches.extend(_polygon_patches(feat["geometry"]))
    if patches:
        ax.add_collection(PatchCollection(
            patches, facecolor="none", edgecolor="#333333", linewidth=0.8, zorder=3,
        ))

    if not value_by_osm_id:
        return None

    xs, ys, values = [], [], []
    for feat in features:
        value = value_by_osm_id.get(feat["properties"].get("osm_id"))
        if value is None:
            continue
        centroid = shape(feat["geometry"]).centroid
        xs.append(centroid.x)
        ys.append(centroid.y)
        values.append(value)

    if not xs:
        return None

    xs, ys, values = _sort_by_value_ascending(xs, ys, values)
    sizes = _sizes_from_value(values, norm, min_size=50, max_size=260)

    return ax.scatter(
        xs, ys, c=values, cmap=_VALUE_CMAP, norm=norm, s=sizes, edgecolors="#4d2600",
        linewidths=0.6, zorder=4,
    )


def _add_fires(ax, fires_geojson, fire_values, norm):
    """Draw active-fire detections. If fire_values (estimated value burnt
    within each detection's own buffer) is available, color and size them
    on the same orange-to-red scale as buildings, highest value on top;
    otherwise use a flat color. Returns the scatter mappable if colored by
    value, else None."""
    features = fires_geojson.get("features", []) if fires_geojson else []
    if not features:
        return None
    xs = [f["geometry"]["coordinates"][0] for f in features]
    ys = [f["geometry"]["coordinates"][1] for f in features]

    if fire_values is not None:
        xs, ys, fire_values = _sort_by_value_ascending(xs, ys, fire_values)
        sizes = _sizes_from_value(fire_values, norm, min_size=30, max_size=160)
        return ax.scatter(
            xs, ys, c=fire_values, cmap=_VALUE_CMAP, norm=norm, s=sizes,
            edgecolors="#4d2600", linewidths=0.7, zorder=5,
        )

    ax.scatter(xs, ys, s=18, c="#ff9f1c", edgecolors="#7a4a00", linewidths=0.5, zorder=5)
    return None


_SCALE_BAR_STEPS_M = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000]


def _add_scale_bar(ax, mean_lat):
    """Draw a ground-distance scale bar in the bottom-left corner, in place
    of lon/lat axis ticks."""
    xlim, ylim = ax.get_xlim(), ax.get_ylim()
    m_per_deg_lon = _M_PER_DEG_LAT * max(math.cos(math.radians(mean_lat)), 0.15)
    span_m = (xlim[1] - xlim[0]) * m_per_deg_lon

    target_m = span_m * 0.25
    bar_m = min(_SCALE_BAR_STEPS_M, key=lambda step: abs(step - target_m))
    bar_deg = bar_m / m_per_deg_lon

    x0 = xlim[0] + (xlim[1] - xlim[0]) * 0.05
    y0 = ylim[0] + (ylim[1] - ylim[0]) * 0.05
    tick_h = (ylim[1] - ylim[0]) * 0.012

    ax.plot([x0, x0 + bar_deg], [y0, y0], color="black", linewidth=2.5,
            solid_capstyle="butt", zorder=10)
    for tx in (x0, x0 + bar_deg):
        ax.plot([tx, tx], [y0 - tick_h, y0 + tick_h], color="black", linewidth=1.5, zorder=10)

    label = f"{bar_m} m" if bar_m < 1000 else f"{bar_m / 1000:g} km"
    text = ax.text(x0 + bar_deg / 2, y0 + tick_h * 1.8, label, ha="center", va="bottom",
                    fontsize=8, color="black", zorder=10)
    text.set_path_effects([
        path_effects.Stroke(linewidth=2.5, foreground="white"),
        path_effects.Normal(),
    ])


def _fmt_eur(value):
    return f"€{value:,.0f}"


def _fig_to_array(fig, facecolor="white", pad_inches=0.1):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight", pad_inches=pad_inches, facecolor=facecolor)
    plt.close(fig)
    buf.seek(0)
    return mpimg.imread(buf)


def _trim_vertical_whitespace(arr):
    """Trim near-white rows from the top and bottom of an image array.
    matplotlib's bbox_inches="tight" doesn't always hug an aspect-
    constrained axes exactly, leaving a thin residual margin -- this
    guarantees a hard, gap-free stack against a neighboring image
    regardless of that quirk."""
    is_content_row = (arr[:, :, :3] < 0.98).any(axis=(1, 2))
    if not is_content_row.any():
        return arr
    rows = np.nonzero(is_content_row)[0]
    return arr[rows[0]:rows[-1] + 1]


def _vstack(arrays, gaps=24):
    """Vertically stack RGBA float arrays of possibly-different widths,
    centering narrower ones on a white background.

    gaps: either a single gap size (px) applied between every pair, or a
    list of len(arrays)-1 sizes for per-transition control (e.g. 0 to butt
    two sections directly together with no visible seam).
    """
    if isinstance(gaps, int):
        gaps = [gaps] * (len(arrays) - 1)

    max_w = max(a.shape[1] for a in arrays)
    channels = arrays[0].shape[2]

    def pad(a):
        extra = max_w - a.shape[1]
        if extra <= 0:
            return a
        left = extra // 2
        right = extra - left
        pad_l = np.ones((a.shape[0], left, channels), dtype=a.dtype)
        pad_r = np.ones((a.shape[0], right, channels), dtype=a.dtype)
        return np.concatenate([pad_l, a, pad_r], axis=1)

    parts = []
    for i, arr in enumerate(arrays):
        parts.append(pad(arr))
        if i < len(arrays) - 1 and gaps[i] > 0:
            parts.append(np.ones((gaps[i], max_w, channels), dtype=arrays[0].dtype))
    return np.concatenate(parts, axis=0)


def _render_map_figure(fires, burnt_area, affected_buildings, valuation, municipalities, country, region, meta):
    west, south, east, north = meta["bbox"]
    mean_lat = (south + north) / 2
    lon_span_m = (east - west) * _M_PER_DEG_LAT * max(math.cos(math.radians(mean_lat)), 0.15)
    lat_span_m = (north - south) * _M_PER_DEG_LAT
    aspect = lon_span_m / lat_span_m if lat_span_m else 1.0

    map_width = 10.0
    map_height = min(max(map_width / aspect, 4.0), 16.0)
    fig = plt.figure(figsize=(map_width, map_height + 1.6))
    fig.subplots_adjust(top=0.86)
    ax = fig.add_subplot(111)

    has_basemap = _add_basemap(ax, meta["bbox"], meta.get("basemap", "osm"))
    _add_burnt_area(ax, burnt_area)
    top_names = _top_municipality_names(valuation)
    has_boundaries, muni_label_candidates = _add_municipality_boundaries(
        ax, municipalities, meta["bbox"], top_names=top_names,
    )

    value_by_osm_id = {b["osm_id"]: b["value_eur"] for b in valuation["buildings"]} if valuation else {}
    fire_values = None
    if valuation and value_by_osm_id:
        fire_values = estimate_service.value_per_fire(
            fires or {}, affected_buildings or {}, value_by_osm_id, source=meta.get("source", "VIIRS_SNPP_NRT"),
        )

    norm = None
    all_values = list(value_by_osm_id.values()) + (fire_values or [])
    if all_values:
        vmin, vmax = min(all_values), max(all_values)
        norm = Normalize(vmin=vmin, vmax=vmax if vmax > vmin else vmin + 1)

    buildings_mappable = _add_buildings(ax, affected_buildings, value_by_osm_id, norm)
    fires_mappable = _add_fires(ax, fires, fire_values, norm)
    collection = buildings_mappable or fires_mappable

    # Wider than a purely cosmetic margin would need: the extra room keeps
    # the locator inset (top-right corner) from sitting on top of real fire
    # detections or buildings that would otherwise extend into that corner.
    pad_x = (east - west) * 0.18 or 0.001
    pad_y = (north - south) * 0.18 or 0.001
    ax.set_xlim(west - pad_x, east + pad_x)
    ax.set_ylim(south - pad_y, north + pad_y)
    ax.set_aspect(1 / max(math.cos(math.radians(mean_lat)), 0.15))
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color("#888888")

    _add_locator_inset(ax, country, region, meta["bbox"])
    _add_scale_bar(ax, mean_lat)

    if has_basemap:
        attribution_text = basemap_service.get_attribution(meta.get("basemap", "osm"))
        attribution = ax.text(
            0.99, 0.01, attribution_text, transform=ax.transAxes, fontsize=6,
            ha="right", va="bottom", color="#333333", zorder=10,
        )
        attribution.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="none", pad=1.5))

    fires_count = len(fires.get("features", [])) if fires else 0
    buildings_count = len(affected_buildings.get("features", [])) if affected_buildings else 0
    subtitle = (
        f"{meta.get('date', '')} · {meta.get('days', 1)} day(s) · {meta.get('source', '')} · "
        f"{fires_count} fire detections · {buildings_count} buildings affected"
    )
    if valuation:
        subtitle += f" · {_fmt_eur(valuation['total_value_eur'])} estimated value lost"

    fig.suptitle("Hephaestus — Fire & Burnt Area Report", fontsize=16, fontweight="bold", x=0.02, ha="left", y=0.97)
    fig.text(0.02, 0.905, subtitle, fontsize=9, color="#555555")

    fire_label = "Active fire detection" + (" (colored by value burnt nearby)" if fires_mappable is not None else "")
    legend_handles = [
        Line2D([0], [0], marker="o", linestyle="", color="#ff9f1c", markeredgecolor="#7a4a00",
               markersize=7, label=fire_label),
        MplPolygon([[0, 0], [1, 0], [1, 1]], closed=True, facecolor="#ff6b6b", edgecolor="#c0392b",
                   alpha=0.25, label="Estimated burnt area"),
    ]
    if affected_buildings and affected_buildings.get("features"):
        legend_handles.append(
            MplPolygon([[0, 0], [1, 0], [1, 1]], closed=True, facecolor="none", edgecolor="#333333",
                       label="Affected building (outline)")
        )
    if collection is not None:
        legend_handles.append(
            Line2D([0], [0], marker="o", linestyle="", color="#b30000", markeredgecolor="#4d2600",
                   markersize=8, label="Estimated value (see color scale)")
        )
    if has_boundaries:
        legend_handles.append(
            Line2D([0], [0], color="#6a3d9a", linestyle="--", linewidth=1.3,
                   label="Municipality boundary (OSM)")
        )
    if collection is not None:
        cbar = fig.colorbar(collection, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label("Estimated value (€)", fontsize=8)

    # No legend drawn on the map itself -- it renders in its own box below
    # the map instead (see _render_legend_figure), so labels never have to
    # dodge it.
    _draw_municipality_labels(ax, muni_label_candidates)

    return fig, legend_handles


def _render_legend_figure(legend_handles):
    """Render the map's legend as its own bordered box below the map,
    instead of overlaid on top of it -- so it never covers map content
    (fires, buildings, municipality labels)."""
    ncol = min(len(legend_handles), 3)
    n_rows = math.ceil(len(legend_handles) / ncol)

    fig = plt.figure(figsize=(10, 0.55 * n_rows + 0.35))
    ax = fig.add_subplot(111)
    ax.axis("off")
    ax.legend(
        handles=legend_handles, loc="center", ncol=ncol, fontsize=9,
        frameon=True, framealpha=1, edgecolor="#888888", borderpad=1.2,
    )
    return fig


def _render_table_figure(valuation):
    fig = plt.figure(figsize=(10, 0.9))
    ax = fig.add_subplot(111)
    ax.axis("off")

    if not valuation:
        ax.text(0, 0.5, "Value-lost estimation was not run for this analysis.", fontsize=10)
        return fig
    if not valuation.get("by_municipality"):
        ax.text(0, 0.5, "No buildings could be priced.", fontsize=10)
        return fig

    rows = valuation["by_municipality"]
    top_n = 15
    shown_rows = rows[:top_n]
    remaining_rows = rows[top_n:]

    col_labels = ["Municipality / village", "Buildings affected", "Estimated value lost"]
    cell_text = [[r["municipality"], str(r["buildings"]), _fmt_eur(r["value_eur"])] for r in shown_rows]

    others_row_idx = None
    if remaining_rows:
        others_row_idx = len(cell_text)
        cell_text.append([
            f"Others ({len(remaining_rows)} more)",
            str(sum(r["buildings"] for r in remaining_rows)),
            _fmt_eur(sum(r["value_eur"] for r in remaining_rows)),
        ])

    cell_text.append(["TOTAL", str(valuation["buildings_priced"]), _fmt_eur(valuation["total_value_eur"])])

    row_h = 0.42
    fig.set_size_inches(10, 1.1 + row_h * (len(cell_text) + 1))

    title = "Value lost by municipality / village"
    if remaining_rows:
        title += f" (top {top_n})"
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left")
    tbl = ax.table(cellText=cell_text, colLabels=col_labels, loc="upper center", cellLoc="left")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.6)

    if others_row_idx is not None:
        for col in range(3):
            tbl[others_row_idx + 1, col].set_text_props(style="italic", color="#555555")

    n_rows = len(cell_text)
    for col in range(3):
        tbl[n_rows, col].set_text_props(fontweight="bold")
        tbl[n_rows, col].set_facecolor("#f0f0f0")

    return fig


def _render_footer_figure():
    footer = (
        "Burnt area is a proxy built by buffering active-fire detections, not an official burn "
        "perimeter. Value estimates combine real Catastro built areas (Spain only) or OSM "
        "footprints (x floor count, when available) with an assumed price/m² -- not an official appraisal."
    )
    fig = plt.figure(figsize=(10, 0.5))
    ax = fig.add_subplot(111)
    ax.axis("off")
    ax.text(0, 0.5, footer, fontsize=7, color="#777777", wrap=True, va="center")
    return fig


def render_report_png(fires, burnt_area, affected_buildings, valuation, municipalities, country, region, meta):
    """Return PNG bytes for the report.

    fires: FeatureCollection of active-fire points
    burnt_area: burnt-area geometry (Polygon/MultiPolygon) or None
    affected_buildings: FeatureCollection of affected building footprints
    valuation: result dict from services.valuation.estimate_value_lost, or None
    municipalities: list of {"name", "geometry"} from services.municipalities, or None
    country, region: {"name", "geometry"} for the locator inset (see
        services.municipalities.fetch_locator_context), or None to skip it
    meta: dict with title info -- bbox, date, days, source
    """
    valuation = _reclassify_municipalities(valuation, affected_buildings, municipalities)

    map_fig, legend_handles = _render_map_figure(
        fires, burnt_area, affected_buildings, valuation, municipalities, country, region, meta
    )
    # Trimmed (not just zero-padded) so the map's bottom edge and the
    # legend box's top edge sit truly flush with no visible gap.
    map_arr = _trim_vertical_whitespace(_fig_to_array(map_fig, pad_inches=0))
    legend_arr = _trim_vertical_whitespace(_fig_to_array(_render_legend_figure(legend_handles), pad_inches=0))
    table_arr = _fig_to_array(_render_table_figure(valuation))
    footer_arr = _fig_to_array(_render_footer_figure())

    combined = _vstack([map_arr, legend_arr, table_arr, footer_arr], gaps=[0, 24, 24])

    buf = io.BytesIO()
    mpimg.imsave(buf, combined, format="png")
    buf.seek(0)
    return buf.getvalue()
