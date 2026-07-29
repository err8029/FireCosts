# Hephaestus — working notes for Claude Code

Flask + Leaflet wildfire analysis tool. See README.md for the user-facing
feature list and project layout — this file is engineering conventions
and hard-won gotchas, kept short on purpose so it doesn't eat context
budget every session. Don't duplicate README content here; update README
instead when a route/service list goes stale.

## Testing workflow (no formal test suite)

There's no `tests/` directory. Verification is done with small throwaway
scripts: mock the relevant `services.*` function with
`unittest.mock.patch.object`, hit routes via `app.test_client()`, assert
on the response. Write these to the scratchpad dir, not the repo. For
frontend/visual changes, use Playwright (`channel="msedge"`) to screenshot
and actually look at the image before calling something done — don't
assume a route returning 200 means the UI renders correctly.

Run the dev server as `./.venv/Scripts/python.exe app.py` (needs
`.env` with `FIRMS_MAP_KEY`, see `.env.example`).

## External-call conventions — read before touching services/*

- **Every** external HTTP call (Overpass, FIRMS, Catastro, Nominatim)
  must sit behind a hard wall-clock deadline. Use
  `concurrent.futures.wait([...], timeout=X)` with **one combined
  timeout across all pending futures in a batch** — never
  `.result(timeout=X)` per future sequentially, that stacks deadlines
  (N futures × X each instead of X total).
- Don't use `with ThreadPoolExecutor() as pool:` when you intend to
  abandon slow work — `__exit__` calls `shutdown(wait=True)` and blocks
  anyway. Use a plain `pool = ThreadPoolExecutor(...)` +
  `pool.shutdown(wait=False)`.
- `services/overpass.py` tries two mirrors sharing **one** time budget
  split between them (not a full timeout each), and does not retry
  read/connect timeouts (only real transient 5xx/429 get one retry) — a
  dead mirror must not be allowed to eat the whole budget before the
  fallback gets a turn.
- FIRMS's real `day_range` cap is 1–5 per request (confirmed live; not
  the commonly-cited 1–10). `firms.fetch_active_fires` transparently
  chunks longer ranges into parallel ≤5-day requests and merges them.
- Catastro's free service has an undocumented hourly per-IP quota (403,
  "Ha superado el limite de peticiones por hora"). A multi-unit parcel
  cascades into one Catastro request *per unit* — that multiplier, not
  raw building count, is usually the real bottleneck. `catastro.py`
  single-flights concurrent identical lookups (same point or same `rc`)
  so parallel building valuation never duplicates a live request.
- When a live, authoritative point-in-polygon answer is obtained (e.g.
  an Overpass `is_in()` query), cache it directly on the
  feature/result and use it as-is downstream. Re-deriving the same
  classification later via local `.contains()` against our own
  reconstructed boundary geometry (`polygonize()` on "outer" way members
  only) can disagree with the authoritative answer for large/complex
  shapes — silently reintroducing whatever bug the live lookup exists to
  fix.
- Cache-key rounding precision must match across cooperating
  caches/dedup logic. E.g. `municipalities.py`'s point cache rounds to 2
  decimals (~1km); a caller deduping at finer precision defeats cache
  sharing between nearby points and multiplies redundant network calls.

## matplotlib report gotcha (services/report.py)

Table cell position/size isn't real until `fig.canvas.draw()` has run.
Use `cell.get_window_extent(renderer)` (converted via
`ax.transData.inverted()`) to get a correct bbox — `cell.get_transform()`
looks like the right API but gives wrong positions once `colWidths` is
non-uniform.

## This dev machine

Has had recurring, severe network flakiness reaching Overpass/FIRMS/
Catastro (TCP timeouts, TLS cert errors) — confirmed external to the
app's own code (identical failures via raw curl/PowerShell, even `pip
install`). Don't assume a 502 or timeout during local testing means new
code broke something; reproduce directly against the live service first
before chasing it as a bug.

## Git

Only commit/push when the user explicitly asks.
