# Data sources

Everything geographic in DIVERT comes from real, cited sources. Nothing is invented: if a
source cannot be reached, the tooling fails loudly and writes nothing.

## 1. OpenStreetMap (signals and stations)

**Licence:** ODbL - (c) OpenStreetMap contributors. Attribution is shown on the map and
recorded in every snapshot (`meta.licence`). Derived data in this repository
(`backend/src/divert/cities/data/*.json`) is a Derivative Database under the ODbL and is
shared under the same licence.

**How it is fetched** - `backend/scripts/fetch_osm_data.py` (same code as `divert
fetch-data`, implemented in `divert/cities/osm.py`):

- Overpass API, endpoints tried in rotation with retries and exponential backoff:
  `overpass-api.de`, `lz4.overpass-api.de`, `overpass.kumi.systems`,
  `overpass.private.coffee`. A response is accepted only if it is valid JSON with an
  `elements` array and no runtime-error remark.
- Each city bbox is split into tiles of at most 0.25 degrees (Delhi 6, Bengaluru 4,
  Kolkata 1) so every query stays well below public-instance gateway timeouts. Per tile:

  ```
  [out:json][timeout:240];
  node["highway"="traffic_signals"](S,W,N,E);
  out body;
  (
    nwr["amenity"="hospital"](S,W,N,E);
    nwr["amenity"="fire_station"](S,W,N,E);
    nwr["amenity"="police"](S,W,N,E);
  );
  out center;
  ```

  Tile results are merged by OSM `(type, id)` before any processing, so tile borders
  neither duplicate nor split anything. Elements outside the city bbox are dropped.
- `--cache-dir` keeps raw responses keyed by query hash, so an interrupted fetch resumes
  without re-querying (the Makefile uses `.work/osm-cache`, gitignored).
- All cities are fetched before any file is written (all-or-nothing per run).

| City | bbox (S, W, N, E) | Tiles |
|---|---|---|
| Delhi | 28.40, 76.84, 28.88, 77.35 | 6 |
| Kolkata | 22.45, 88.25, 22.65, 88.50 | 1 |
| Bengaluru | 12.83, 77.46, 13.14, 77.78 | 4 |

**Processing:**

- *Signals -> junctions.* Large junctions are mapped as one `traffic_signals` node per
  approach. Nodes are clustered by single linkage within **40 m** (haversine); a junction's
  id is `J<smallest member node id>`, its position is the member centroid, its name is the
  most common member name (rarely present), and all member node ids are kept
  (`signals.osm_node_ids`) for traceability.
- *Stations.* `amenity=hospital` -> AMBULANCE base (**named hospitals only**; an unnamed
  hospital polygon is too often a campus building), `amenity=fire_station` -> FIRE_ENGINE,
  `amenity=police` -> POLICE. Ways/relations use their Overpass centre. The same named
  facility mapped twice (node + area within 150 m) is kept once. Ids encode the OSM
  element: `H|F|P` + `n|w|r` + OSM id (e.g. `Pw123456` = police station mapped as way 123456).
- *Units.* OSM has no fleet data. Every active station gets `DIVERT_UNITS_PER_STATION`
  (default 2) vehicles. This is a modelling assumption - in particular, not every
  hospital operates ambulances. Replace with the real fleet register when available
  (the `units` table is the only thing to load).

### Snapshot committed on 2026-11-17

Fetched 2026-11-16 20:34-20:41 UTC; OSM base timestamps 2026-11-16 20:33-20:39 UTC
(exact values per city in `meta.fetched_at` / `meta.osm_base`).

| City | `traffic_signals` nodes | Junctions (40 m clusters) | Multi-node junctions | AMBULANCE bases | FIRE_ENGINE | POLICE | Units (x2) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Delhi | 1276 | 500 | 366 | 847 | 42 | 242 | 2262 |
| Kolkata | 67 | 28 | 18 | 294 | 7 | 44 | 690 |
| Bengaluru | 1385 | 576 | 410 | 1086 | 22 | 134 | 2484 |

**Coverage caveat - Kolkata.** OpenStreetMap currently tags only **67** traffic-signal
nodes in the whole Kolkata bbox (verified independently with an Overpass `out count`
query on 2026-11-17). Kolkata corridors will therefore contain few or no pre-emptable
signals; this reflects OSM coverage, not the city's real signal inventory. Fix it at the
source (map the signals in OSM, or import the traffic police's signal register) rather
than by synthesising junctions. Delhi and Bengaluru coverage is far denser but also
incomplete; names are almost never tagged on signal nodes.

**Refreshing:** `make fetch-data` (or `uv run divert fetch-data --cache-dir ../.work/osm-cache`),
review the count changes in the diff, commit the JSON, then `divert seed` (idempotent:
upserts by OSM id, deactivates vanished stations/signals, never deletes history).

## 2. City profiles (NOT from OSM - approximate)

Each snapshot carries a `traffic` block used by the speed/ETA model:

- `hourly_congestion` - 24 factors (index = local hour, `Asia/Kolkata`) giving the share of
  OSRM free-flow speed traffic allows in that hour; AM peak ~09-11, PM peak ~17-20.
- `avg_junction_delay_s` - mean delay a vehicle suffers at a signalised junction that is
  *not* pre-empted (Delhi 35 s, Kolkata 40 s, Bengaluru 40 s). DIVERT's "estimated time
  saved" is computed from it and is always labelled an estimate.

These values are **hand-set approximations of the typical diurnal shape**, not
measurements. Calibrate before trusting absolute ETAs or time-saved figures, e.g. with:

- TomTom Traffic Index city pages (hourly congestion levels per city),
- traffic-police junction delay / cycle-time counts,
- DIVERT's own history: `dispatches.predicted_eta_s` vs `actual_travel_s` once real
  telemetry flows (the schema stores both for exactly this purpose).

Centres and zoom levels are presentation defaults (well-known city-centre coordinates).

## 3. OSRM (routing)

`DIVERT_OSRM_URL`, default `https://router.project-osrm.org` - the public OSRM demo server
(OpenStreetMap data, car profile). It is for light, fair use only: no SLA, rate limited.
DIVERT caches routes in Redis and never falls back to straight lines (a dispatch fails
with 503 instead). Production self-hosts OSRM from a Geofabrik India extract clipped to
the three cities: see `infra/osrm/README.md` and the compose profile `osrm`.
