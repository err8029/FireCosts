// Default: the whole Iberian Peninsula, not a single city -- gives the
// user immediate context for where the active-fire pointers (see
// loadActiveFiresOverview below) actually are, rather than starting
// zoomed into Madrid and having the view jump right after load. Once the
// overview fetch resolves it fits bounds to the real pointers anyway;
// this is just a sane starting point (and the fallback view if that
// fetch fails or finds nothing).
const map = L.map("map").setView([40.0, -4.0], 6);

const osmLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
});

// Esri World Imagery: free, no API key required, same light-use
// expectations as OSM's tile policy.
const satelliteLayer = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  {
    maxZoom: 19,
    attribution:
      "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
  }
);

osmLayer.addTo(map);
L.control.layers({ "OpenStreetMap": osmLayer, "Satellite": satelliteLayer }).addTo(map);

let currentBasemap = "osm";
map.on("baselayerchange", (e) => {
  currentBasemap = e.name === "Satellite" ? "satellite" : "osm";
});

const drawnItems = new L.FeatureGroup();
map.addLayer(drawnItems);

const drawControl = new L.Control.Draw({
  edit: { featureGroup: drawnItems, remove: false },
  draw: {
    polygon: false,
    polyline: false,
    circle: false,
    circlemarker: false,
    marker: false,
    rectangle: {
      shapeOptions: { color: "#2b6cb0" },
    },
  },
});

let currentBbox = null;
let fireLayer = null;
let burntAreaLayer = null;
let buildingsLayer = null;
let vegetationLayer = null;
let activeFiresOverviewLayer = null;

// Vegetation/land-cover categories from services/landcover.py, colored
// distinctly from the burnt-area/building layers so they read as a
// separate "what kind of land" layer rather than more burnt-area shading.
const VEGETATION_COLORS = {
  forest: "#2e7d32",
  scrub_heath: "#8d6e29",
  grassland: "#9ccc65",
  farmland: "#d4b106",
};
const VEGETATION_LABELS = {
  forest: "Forest / woodland",
  scrub_heath: "Scrub / heath",
  grassland: "Grassland / meadow",
  farmland: "Farmland",
};

let lastAffectedBuildings = null;
let lastAnalysis = null;
let lastValuation = null;
let lastMeta = null;

const toastEl = document.getElementById("toast");
const sidebarEl = document.getElementById("sidebar");
const sidebarBackdrop = document.getElementById("sidebar-backdrop");
const sidebarToggleBtn = document.getElementById("sidebar-toggle");
const analyzeBtn = document.getElementById("analyze-btn");
const drawBoxBtn = document.getElementById("draw-box-btn");
const backToOverviewBtn = document.getElementById("back-to-overview-btn");
const dateInput = document.getElementById("date-input");
const daysInput = document.getElementById("days-input");
const sourceInput = document.getElementById("source-input");
const resultsEl = document.getElementById("results");
const priceInput = document.getElementById("price-input");
const VEGETATION_CATEGORIES = ["forest", "scrub_heath", "grassland", "farmland"];
const vegetationPriceInputs = Object.fromEntries(
  VEGETATION_CATEGORIES.map((cat) => [cat, document.getElementById(`price-${cat}-input`)])
);
const valuationBtn = document.getElementById("valuation-btn");
const valuationResultsEl = document.getElementById("valuation-results");
const reportBtn = document.getElementById("report-btn");
const recentBoxesSelect = document.getElementById("recent-boxes-select");
const saveExampleBtn = document.getElementById("save-example-btn");

dateInput.value = new Date().toISOString().slice(0, 10);

function setSidebarOpen(open) {
  sidebarEl.classList.toggle("open", open);
  sidebarBackdrop.classList.toggle("visible", open);
  sidebarToggleBtn.setAttribute("aria-expanded", String(open));
}

// Start open on desktop-sized screens (controls visible immediately), and
// collapsed on phones/small tablets so the map gets the full viewport.
setSidebarOpen(window.innerWidth > 768);

sidebarToggleBtn.addEventListener("click", () => {
  setSidebarOpen(!sidebarEl.classList.contains("open"));
});

sidebarBackdrop.addEventListener("click", () => setSidebarOpen(false));

// Prices and Disclaimer are floating popups rather than always-visible
// sidebar content -- keeps the sidebar from accumulating every input and
// paragraph of fine print as features (vegetation, per-category pricing)
// get added over time.
function openModal(modal) {
  modal.classList.remove("hidden");
}
function closeModal(modal) {
  modal.classList.add("hidden");
}
document.querySelectorAll(".modal").forEach((modal) => {
  modal.querySelectorAll("[data-close-modal]").forEach((el) => {
    el.addEventListener("click", () => closeModal(modal));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) closeModal(modal);
  });
});
document.getElementById("prices-btn").addEventListener("click", () => {
  openModal(document.getElementById("prices-modal"));
});
document.getElementById("disclaimer-btn").addEventListener("click", () => {
  openModal(document.getElementById("disclaimer-modal"));
});

const RECENT_BOXES_KEY = "fireAnalysisRecentBoxes";
const CUSTOM_EXAMPLES_KEY = "fireAnalysisCustomExamples";
const MAX_RECENT_BOXES = 10;

// Curated starting points -- real-world areas worth exploring without having
// to know their coordinates offhand. Shipped in code (not localStorage) so
// they show up for every visitor, including on a fresh deploy. Each entry
// is either an exact {name, bbox: [west, south, east, north]} (e.g. one
// captured via "Save current box as example…") or a {name, lat, lon}
// center point, expanded to a fixed-size box below.
const EXAMPLE_BOXES = [
  {
    name: "Sierra Oeste Fire",
    bbox: [-4.963760375976563, 40.19041398364302, -4.137725830078126, 40.51745320894507],
  },
];
const EXAMPLE_BOX_HALF_SPAN_DEG = 0.15;

function exampleBboxFor(entry) {
  if (entry.bbox) return entry.bbox;
  const { lat, lon } = entry;
  return [
    lon - EXAMPLE_BOX_HALF_SPAN_DEG,
    lat - EXAMPLE_BOX_HALF_SPAN_DEG,
    lon + EXAMPLE_BOX_HALF_SPAN_DEG,
    lat + EXAMPLE_BOX_HALF_SPAN_DEG,
  ];
}

function loadRecentBoxes() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_BOXES_KEY)) || [];
  } catch {
    return [];
  }
}

function loadCustomExamples() {
  try {
    return JSON.parse(localStorage.getItem(CUSTOM_EXAMPLES_KEY)) || [];
  } catch {
    return [];
  }
}

function saveCustomExample(name, bbox) {
  const examples = loadCustomExamples();
  examples.push({ name, bbox });
  localStorage.setItem(CUSTOM_EXAMPLES_KEY, JSON.stringify(examples));
}

function bboxKey(bbox) {
  return bbox.map((v) => v.toFixed(5)).join(",");
}

function populateBoxesDropdown(selectedValue) {
  recentBoxesSelect.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "— select a box —";
  recentBoxesSelect.appendChild(placeholder);

  const customExamples = loadCustomExamples();
  if (EXAMPLE_BOXES.length || customExamples.length) {
    const exampleGroup = document.createElement("optgroup");
    exampleGroup.label = "Examples";
    EXAMPLE_BOXES.forEach((entry, i) => {
      const opt = document.createElement("option");
      opt.value = `example:${i}`;
      opt.textContent = entry.lat != null
        ? `${entry.name} (${entry.lat.toFixed(3)}, ${entry.lon.toFixed(3)})`
        : entry.name;
      exampleGroup.appendChild(opt);
    });
    customExamples.forEach((entry, i) => {
      const opt = document.createElement("option");
      opt.value = `custom:${i}`;
      opt.textContent = entry.name;
      exampleGroup.appendChild(opt);
    });
    recentBoxesSelect.appendChild(exampleGroup);
  }

  const recentBoxes = loadRecentBoxes();
  if (recentBoxes.length) {
    const recentGroup = document.createElement("optgroup");
    recentGroup.label = "Recently used";
    recentBoxes.forEach((entry) => {
      const [west, south, east, north] = entry.bbox;
      const centerLat = (south + north) / 2;
      const centerLon = (west + east) / 2;
      const opt = document.createElement("option");
      opt.value = `recent:${bboxKey(entry.bbox)}`;
      opt.textContent = `${centerLat.toFixed(3)}, ${centerLon.toFixed(3)} — used ${entry.date}`;
      recentGroup.appendChild(opt);
    });
    recentBoxesSelect.appendChild(recentGroup);
  }

  const validValues = Array.from(recentBoxesSelect.options).map((o) => o.value);
  recentBoxesSelect.value = selectedValue && validValues.includes(selectedValue) ? selectedValue : "";
}

function rememberBbox(bbox, date) {
  const key = bboxKey(bbox);
  const boxes = loadRecentBoxes().filter((entry) => bboxKey(entry.bbox) !== key);
  boxes.unshift({ bbox, date });
  localStorage.setItem(RECENT_BOXES_KEY, JSON.stringify(boxes.slice(0, MAX_RECENT_BOXES)));
  populateBoxesDropdown(`recent:${key}`);
}

function applyBbox(bbox) {
  const [west, south, east, north] = bbox;
  currentBbox = bbox;

  // The overview pointers would just clutter the map once the user has
  // an actual area of interest (and can visually overlap the real
  // per-detection fire markers Analyze renders inside this box) -- but
  // keep the layer itself around (just detached), not discarded, so
  // "Back to overview" can show it again without re-fetching/re-
  // geocoding everything from scratch.
  if (activeFiresOverviewLayer) {
    map.removeLayer(activeFiresOverviewLayer);
  }

  drawnItems.clearLayers();
  const rect = L.rectangle([[south, west], [north, east]], { color: "#2b6cb0" });
  drawnItems.addLayer(rect);
  map.fitBounds(rect.getBounds(), { padding: [20, 20] });

  analyzeBtn.disabled = false;
  setStatus("");
  if (activeFiresOverviewLayer) backToOverviewBtn.classList.remove("hidden");
}

recentBoxesSelect.addEventListener("change", () => {
  const value = recentBoxesSelect.value;
  if (!value) return;

  if (value.startsWith("example:")) {
    const entry = EXAMPLE_BOXES[Number(value.slice("example:".length))];
    if (entry) applyBbox(exampleBboxFor(entry));
    return;
  }

  if (value.startsWith("custom:")) {
    const entry = loadCustomExamples()[Number(value.slice("custom:".length))];
    if (entry) applyBbox(entry.bbox);
    return;
  }

  if (value.startsWith("recent:")) {
    const key = value.slice("recent:".length);
    const entry = loadRecentBoxes().find((e) => bboxKey(e.bbox) === key);
    if (entry) applyBbox(entry.bbox);
  }
});

saveExampleBtn.addEventListener("click", () => {
  if (!currentBbox) {
    setStatus("Draw or select a box first.", true);
    return;
  }
  const name = window.prompt("Name this example (e.g. \"Sierra Oeste fire\"):");
  if (!name || !name.trim()) return;

  saveCustomExample(name.trim(), currentBbox);
  populateBoxesDropdown(`custom:${loadCustomExamples().length - 1}`);
  setStatus(`Saved "${name.trim()}" as an example.`);
});

populateBoxesDropdown();

drawBoxBtn.addEventListener("click", () => {
  drawnItems.clearLayers();
  new L.Draw.Rectangle(map, drawControl.options.draw.rectangle).enable();
});

map.on(L.Draw.Event.CREATED, (e) => {
  if (activeFiresOverviewLayer) {
    map.removeLayer(activeFiresOverviewLayer);
  }
  drawnItems.clearLayers();
  const layer = e.layer;
  drawnItems.addLayer(layer);
  const bounds = layer.getBounds();
  currentBbox = [
    bounds.getWest(),
    bounds.getSouth(),
    bounds.getEast(),
    bounds.getNorth(),
  ];
  analyzeBtn.disabled = false;
  setStatus("");
  if (activeFiresOverviewLayer) backToOverviewBtn.classList.remove("hidden");
});

backToOverviewBtn.addEventListener("click", () => {
  drawnItems.clearLayers();
  currentBbox = null;
  analyzeBtn.disabled = true;
  reportBtn.disabled = true;
  valuationBtn.disabled = true;
  clearResultLayers();
  resultsEl.classList.add("hidden");
  valuationResultsEl.classList.add("hidden");
  lastAffectedBuildings = null;
  lastAnalysis = null;
  lastValuation = null;
  setStatus("");
  backToOverviewBtn.classList.add("hidden");

  if (activeFiresOverviewLayer) {
    activeFiresOverviewLayer.addTo(map);
    map.fitBounds(activeFiresOverviewLayer.getBounds(), { padding: [40, 40], maxZoom: 9 });
  }
});

function setStatus(msg, isError) {
  if (!msg) {
    toastEl.classList.add("hidden");
    return;
  }
  toastEl.textContent = msg;
  toastEl.classList.toggle("error", !!isError);
  toastEl.classList.remove("hidden");
}

// The backend can't report real incremental progress on a single
// synchronous request/response, so this shows an estimated percentage
// instead: fast at first, easing off and capping below 100% for as long
// as the request is still in flight, so a slow analysis/report doesn't
// just sit on a static "..." with no sense of whether it's still working.
// stepSeconds is how long it should take to visually approach the cap
// under normal conditions -- tuned per operation below, not a guarantee.
function startProgress(baseMessage, stepSeconds = 8) {
  let pct = 0;
  const cap = 92;
  const update = () => setStatus(`${baseMessage} (${Math.round(pct)}%)`);
  update();
  const stepMs = 200;
  const steps = (stepSeconds * 1000) / stepMs;
  const timer = setInterval(() => {
    pct += ((cap - pct) / steps) * 3;
    if (pct > cap) pct = cap;
    update();
  }, stepMs);
  return () => clearInterval(timer);
}

async function parseJsonResponse(resp) {
  const text = await resp.text();
  try {
    return JSON.parse(text);
  } catch {
    // Not JSON -- e.g. a gateway/proxy error page, or a server crash that
    // slipped past our own JSON error handler. Surface something readable
    // instead of the raw parse error.
    throw new Error(
      `Server returned an unexpected response (HTTP ${resp.status}). It may be temporarily unavailable -- try again in a moment.`
    );
  }
}

// Looking back this many days (including today) for the pre-draw fire
// pointers -- long enough that a multi-day fire shows a real "detected
// since ..." range instead of always just "today" (FIRMS's day_range
// counts forward from a start date, so this needs to start in the past
// to cover it), short enough to stay a "what's currently active" guide
// rather than a historical archive.
const OVERVIEW_DAY_RANGE = 6;

function isoDaysAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

function daysBetweenInclusive(startIso, endIso) {
  const start = new Date(startIso);
  const end = new Date(endIso);
  return Math.round((end - start) / 86400000) + 1;
}

// cluster.label starts undefined (not yet fetched -- see the "mouseover"
// handler below, which fetches it lazily), then becomes a string or null
// once /api/reverse-geocode resolves.
function overviewPopupHtml(cluster) {
  let dateText = "";
  if (cluster.start_date && cluster.end_date && cluster.start_date !== cluster.end_date) {
    dateText = `Detected ${cluster.start_date} → ${cluster.end_date}`;
  } else if (cluster.start_date) {
    dateText = `Detected ${cluster.start_date}`;
  }
  const nameText = cluster.label === undefined
    ? (cluster._labelLoading ? "Loading location…" : "Unnamed area")
    : (cluster.label || "Unnamed area");
  return (
    `<strong>${nameText}</strong><br>` +
    `~${cluster.count} fire detection${cluster.count === 1 ? "" : "s"}` +
    (cluster.max_frp ? `, up to ${cluster.max_frp.toFixed(1)} MW radiative power` : "") +
    (dateText ? `<br>${dateText}` : "") +
    `<br><em>Click to zoom in and suggest an analysis box here.</em>`
  );
}

// Shown on first load, before the user has drawn any box: rough pointers
// for where active fires currently are across Spain (e.g. one near
// "Sierra Oeste"), fetched once from FIRMS via /api/active-fires-overview
// so the user has somewhere to start instead of an empty map. Cleared as
// soon as they pick an actual area (see applyBbox / the CREATED handler
// above) since its only job is to guide that first choice.
async function loadActiveFiresOverview() {
  const overviewStart = isoDaysAgo(OVERVIEW_DAY_RANGE - 1);
  try {
    const resp = await fetch(
      `/api/active-fires-overview?start=${overviewStart}&days=${OVERVIEW_DAY_RANGE}`
    );
    const data = await parseJsonResponse(resp);
    if (!resp.ok || !data.clusters || !data.clusters.length) return;

    activeFiresOverviewLayer = L.layerGroup(
      data.clusters.map((cluster) => {
        const radius = Math.min(9, Math.max(6, Math.sqrt(cluster.count) * 2));
        const marker = L.circleMarker([cluster.lat, cluster.lon], {
          radius,
          color: "#ff3d00",
          weight: 2,
          fillColor: "#ff9f1c",
          fillOpacity: 0.55,
          dashArray: "3,3",
        }).bindPopup(overviewPopupHtml(cluster));

        marker
        // Shown on hover, not just on click -- clicking now takes the
        // action (zoom + suggest a box), so the user needs to see what a
        // pointer actually is *before* committing to that, not just
        // after. The place name itself is fetched lazily right here
        // (not eagerly for every cluster before any of this ever
        // showed up, see api_active_fires_overview's docstring) --
        // most users never hover every single pointer, so this spends
        // Nominatim's rate-limited quota only on the ones actually
        // looked at.
        .on("mouseover", () => {
          marker.openPopup();
          if (cluster.label === undefined && !cluster._labelLoading) {
            cluster._labelLoading = true;
            marker.setPopupContent(overviewPopupHtml(cluster));
            fetch(`/api/reverse-geocode?lat=${cluster.lat}&lon=${cluster.lon}`)
              .then((r) => r.json())
              .then((geo) => { cluster.label = geo.label || null; })
              .catch(() => { cluster.label = null; })
              .finally(() => marker.setPopupContent(overviewPopupHtml(cluster)));
          }
        })
        .on("mouseout", () => marker.closePopup())
        .on("click", () => {
          // Suggest a box sized to this fire's own detections (see
          // firms.cluster_fires' bbox padding) and zoom to it, same as
          // picking a box from the dropdown or drawing one by hand.
          applyBbox(cluster.bbox);
          // Match the date range to what this fire actually showed up
          // in, so clicking Analyze immediately re-fetches the right
          // window instead of silently defaulting to a single day that
          // might miss most of the fire's detections.
          if (cluster.start_date) {
            dateInput.value = cluster.start_date;
            daysInput.value = Math.max(
              1, Math.min(20, daysBetweenInclusive(cluster.start_date, cluster.end_date))
            );
          }
          setStatus("Box suggested from this fire's detections — adjust if needed, then Analyze.");
        });

        return marker;
      })
    ).addTo(map);

    // Pointers are only useful if the user can actually see them without
    // hunting -- the map otherwise defaults to a fixed Madrid view that
    // may not contain any of today's fires at all. Only do this on the
    // initial load (currentBbox is still unset at this point; a user who
    // has since drawn/selected a box gets to keep their own view).
    if (!currentBbox) {
      const bounds = L.latLngBounds(data.clusters.map((c) => [c.lat, c.lon]));
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 9 });
    }
  } catch {
    // Best-effort guide layer -- a failed fetch (e.g. NASA FIRMS/Overpass
    // unreachable) just means no pointers show up yet, not an error the
    // user needs to see; they can still draw a box manually as before.
  }
}

function clearResultLayers() {
  [fireLayer, burntAreaLayer, buildingsLayer, vegetationLayer].forEach((layer) => {
    if (layer) map.removeLayer(layer);
  });
  fireLayer = burntAreaLayer = buildingsLayer = vegetationLayer = null;
}

async function runAnalysis() {
  if (!currentBbox) return;

  const [west, south, east, north] = currentBbox;
  const params = new URLSearchParams({
    bbox: `${west},${south},${east},${north}`,
    start: dateInput.value,
    days: daysInput.value,
    source: sourceInput.value,
  });

  analyzeBtn.disabled = true;
  clearResultLayers();
  resultsEl.classList.add("hidden");
  valuationResultsEl.classList.add("hidden");
  lastAffectedBuildings = null;
  lastAnalysis = null;
  lastValuation = null;
  reportBtn.disabled = true;

  // Scales with the day range: fetching more days of fire detections
  // (and the buildings/vegetation near a potentially larger burnt area)
  // genuinely takes longer, so the progress estimate should too.
  const stopProgress = startProgress(
    "Fetching active fires, buildings and vegetation…",
    6 + Number(daysInput.value) * 2,
  );
  try {
    const resp = await fetch(`/api/estimate?${params.toString()}`);
    const data = await parseJsonResponse(resp);

    if (!resp.ok) {
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    lastAnalysis = data;
    lastMeta = {
      bbox: currentBbox,
      date: dateInput.value,
      days: Number(daysInput.value),
      source: sourceInput.value,
    };
    renderResults(data);
    reportBtn.disabled = false;
    rememberBbox(currentBbox, dateInput.value);
    stopProgress();
    setStatus("");
  } catch (err) {
    stopProgress();
    setStatus(err.message, true);
  } finally {
    analyzeBtn.disabled = false;
  }
}

function renderVegetationBarplot(byCategory) {
  const container = document.getElementById("vegetation-barplot");
  container.innerHTML = "";

  const entries = Object.entries(byCategory).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return;

  const maxValue = entries[0][1] || 1;
  entries.forEach(([category, km2]) => {
    const color = VEGETATION_COLORS[category] || "#777";
    const row = document.createElement("div");
    row.className = "barplot-row";

    const label = document.createElement("div");
    label.className = "barplot-label";
    label.textContent = VEGETATION_LABELS[category] || category;

    const track = document.createElement("div");
    track.className = "barplot-track";
    const fill = document.createElement("div");
    fill.className = "barplot-fill";
    fill.style.width = `${Math.max((km2 / maxValue) * 100, 2)}%`;
    fill.style.backgroundColor = color;
    track.appendChild(fill);

    const value = document.createElement("div");
    value.className = "barplot-value";
    value.textContent = `${km2} km²`;

    row.append(label, track, value);
    container.appendChild(row);
  });
}

function renderResults(data) {
  if (data.burnt_area) {
    burntAreaLayer = L.geoJSON(data.burnt_area, {
      style: { color: "#ff6b6b", weight: 1, fillOpacity: 0.25 },
    }).addTo(map);
  }

  if (data.burnt_vegetation && data.burnt_vegetation.features.length) {
    vegetationLayer = L.geoJSON(data.burnt_vegetation, {
      style: (feature) => ({
        color: VEGETATION_COLORS[feature.properties.category] || "#666",
        weight: 1,
        fillColor: VEGETATION_COLORS[feature.properties.category] || "#666",
        fillOpacity: 0.45,
      }),
      onEachFeature: (feature, layer) => {
        const label = VEGETATION_LABELS[feature.properties.category] || feature.properties.category;
        layer.bindPopup(label);
      },
    }).addTo(map);
  }

  fireLayer = L.geoJSON(data.fires, {
    pointToLayer: (feature, latlng) =>
      L.circleMarker(latlng, {
        radius: 4,
        color: "#ff9f1c",
        fillColor: "#ff9f1c",
        fillOpacity: 0.9,
        weight: 1,
      }).bindPopup(
        `Confidence: ${feature.properties.confidence ?? "n/a"}<br>` +
        `Acquired: ${feature.properties.acq_date ?? ""} ${feature.properties.acq_time ?? ""}`
      ),
  }).addTo(map);

  buildingsLayer = L.geoJSON(data.affected_buildings, {
    style: { color: "#00e5ff", weight: 3, fillColor: "#00e5ff", fillOpacity: 0.08 },
  }).addTo(map);

  document.getElementById("stat-fires").textContent = data.fires.features.length;
  document.getElementById("stat-area").textContent = data.burnt_area_km2;
  document.getElementById("stat-vegetation-km2").textContent = data.vegetation_burnt_km2 ?? 0;
  renderVegetationBarplot(data.vegetation_by_category || {});
  document.getElementById("stat-buildings-total").textContent = data.buildings_total;
  document.getElementById("stat-buildings-affected").textContent = data.buildings_affected;
  resultsEl.classList.remove("hidden");

  lastAffectedBuildings = data.affected_buildings;
  valuationBtn.disabled = !lastAffectedBuildings || lastAffectedBuildings.features.length === 0;
  valuationResultsEl.classList.add("hidden");

  const bounds = drawnItems.getBounds();
  if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
}

async function runValuation() {
  if (!lastAffectedBuildings || !lastAffectedBuildings.features.length) return;

  valuationBtn.disabled = true;
  // Catastro lookups run 6 at a time (see valuation.py), so wall-clock
  // time scales roughly with buildings/6, not the raw count.
  const buildingCount = lastAffectedBuildings.features.length;
  const stopProgress = startProgress(
    "Looking up Catastro records for affected buildings…",
    4 + (buildingCount / 6) * 1.5,
  );

  try {
    const resp = await fetch("/api/valuation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        buildings: lastAffectedBuildings,
        default_price_per_m2: Number(priceInput.value) || undefined,
      }),
    });
    const data = await parseJsonResponse(resp);

    if (!resp.ok) {
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    renderValuation(data);
    stopProgress();
    setStatus("");
  } catch (err) {
    stopProgress();
    setStatus(err.message, true);
  } finally {
    valuationBtn.disabled = false;
  }
}

function renderValuation(data) {
  lastValuation = data;
  const byId = new Map(data.buildings.map((b) => [b.osm_id, b]));

  if (buildingsLayer) {
    buildingsLayer.eachLayer((layer) => {
      const info = byId.get(layer.feature.properties.osm_id);
      if (!info) return;
      const parts = [`&euro;${info.value_eur.toLocaleString()} estimated value`];
      if (info.source === "catastro") {
        parts.push(`${info.built_area_m2} m&sup2; built (Catastro)`);
        if (info.use) parts.push(info.use);
        if (info.year_built) parts.push(`built ${info.year_built}`);
      } else if (info.source === "catastro_error") {
        parts.push(`${info.built_area_m2} m&sup2; footprint (OSM estimate &mdash; Catastro request failed, not a confirmed non-match)`);
      } else if (info.source === "catastro_rate_limited") {
        parts.push(`${info.built_area_m2} m&sup2; footprint (OSM estimate &mdash; Catastro's hourly quota was exceeded)`);
      } else {
        parts.push(`${info.built_area_m2} m&sup2; footprint (OSM estimate, no Catastro match)`);
      }
      layer.bindPopup(parts.join("<br>"));
    });
  }

  document.getElementById("stat-value-lost").textContent = data.total_value_eur.toLocaleString();
  document.getElementById("stat-value-matched").textContent = data.buildings_matched_catastro;
  document.getElementById("stat-value-priced").textContent = data.buildings_priced;

  const erroredRow = document.getElementById("stat-value-errored-row");
  if (data.buildings_errored > 0) {
    document.getElementById("stat-value-errored").textContent = data.buildings_errored;
    erroredRow.style.display = "";
  } else {
    erroredRow.style.display = "none";
  }

  const rateLimitedRow = document.getElementById("stat-value-rate-limited-row");
  if (data.buildings_rate_limited > 0) {
    document.getElementById("stat-value-rate-limited").textContent = data.buildings_rate_limited;
    rateLimitedRow.style.display = "";
  } else {
    rateLimitedRow.style.display = "none";
  }

  valuationResultsEl.classList.remove("hidden");
}

async function exportReport() {
  if (!lastAnalysis || !lastMeta) return;

  reportBtn.disabled = true;
  // Dominated by the municipality/locator Overpass lookups, which are
  // bounded by the backend's own ~12s deadline (see app.py), plus basemap
  // tiles and rendering -- 15s covers the common case without the
  // estimate racing too far ahead of a fast, cache-hit response.
  const stopProgress = startProgress("Rendering report…", 15);

  try {
    const resp = await fetch("/api/report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fires: lastAnalysis.fires,
        burnt_area: lastAnalysis.burnt_area,
        affected_buildings: lastAnalysis.affected_buildings,
        burnt_vegetation: lastAnalysis.burnt_vegetation,
        vegetation_burnt_km2: lastAnalysis.vegetation_burnt_km2,
        vegetation_by_category: lastAnalysis.vegetation_by_category,
        vegetation_price_per_hectare: Object.fromEntries(
          VEGETATION_CATEGORIES
            .map((cat) => [cat, Number(vegetationPriceInputs[cat].value)])
            .filter(([, value]) => !Number.isNaN(value))
        ),
        valuation: lastValuation,
        meta: { ...lastMeta, basemap: currentBasemap },
      }),
    });

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.error || `Request failed (${resp.status})`);
    }

    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `hephaestus_report_${lastMeta.date}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    stopProgress();
    setStatus("");
  } catch (err) {
    stopProgress();
    setStatus(err.message, true);
  } finally {
    reportBtn.disabled = false;
  }
}

analyzeBtn.addEventListener("click", runAnalysis);
valuationBtn.addEventListener("click", runValuation);
reportBtn.addEventListener("click", exportReport);

loadActiveFiresOverview();
