# SEQ Transit Planner

A locally-run journey planner for Queensland (Translink) public transport.
Plans multi-modal trips using the CSA routing algorithm with real-time delay
overlays from the Translink GTFS-RT feeds.

---

## Quick start

### 1. Install Python dependencies

```
py -m pip install fastapi "uvicorn[standard]" gtfs-realtime-bindings requests geopy
```

### 2. Download and ingest the GTFS static feed

This downloads the SEQ feed (~43 MB) and builds a 600 MB SQLite database.
Takes about 6 minutes on first run; subsequent runs skip if the DB is < 24 h old.

```
py ingester.py
```

Force a re-download at any time:

```
py ingester.py --force
```

### 3. Generate analytics GeoJSON (optional, one-off)

Produces `static/data/density.geojson` and `static/data/gaps.geojson` for the
Analytics page. Takes ~25 s.

```
py analytics.py
```

### 4. Start the server

```
py -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Or use the module entry point directly:

```
py main.py
```

The server opens the existing SQLite immediately and begins serving requests.
A background thread checks whether the DB is stale (> 24 h) and re-ingests if
needed. The GTFS-RT poller starts automatically and updates every 30 s.

### 5. Open in your browser

| Page | URL |
|------|-----|
| Journey planner | http://127.0.0.1:8000 |
| Live vehicle map | http://127.0.0.1:8000/map.html |
| Analytics overlay | http://127.0.0.1:8000/analytics.html |
| API docs | http://127.0.0.1:8000/docs |

---

## Using the journey planner

1. Type an origin address in the **From** field and select from the dropdown.
2. Type a destination in the **To** field and select from the dropdown.
3. Choose **Depart after** and set the date/time (defaults to now).
4. Adjust **Stop search radius** if no stops are found near your location
   (default 500 m; increase to 1500 m for sparse areas).
5. Click **Find journey**.
6. The result shows each leg of the journey. Walk legs (between nearby stops)
   are shown as dashed lines with distance and estimated walk time.
7. Real-time adjusted times appear in **amber** (delayed) or **green** (early)
   next to the scheduled time.
8. Service warnings appear as amber banners below the journey.
9. Click **↻ Refresh RT data** to re-poll the Translink feeds and re-run the
   same query with fresh delay information.

---

## Using the live map

- Vehicle positions are plotted and auto-refresh every **30 seconds**.
- The sidebar shows the count of active vehicles and all current service alerts.
- Click any vehicle marker for its ID, current stop, and speed.

---

## Using the analytics page

- **Service density** (default) — colour-coded heatmap showing the number of
  scheduled departures per ~1 km² grid cell across SEQ.
  Black = low frequency, red = very high frequency.
- **Coverage gaps** — red cells indicate areas with no bus/train/ferry stop
  within 800 m. Toggle between overlays using the buttons.

Re-generate the underlying GeoJSON at any time by running `py analytics.py`
while the server is running; the API serves the files directly from disk.

---

## API reference

All endpoints are documented interactively at http://127.0.0.1:8000/docs.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/journey` | Plan a journey between two lat/lon points |
| POST | `/refresh` | Re-poll RT feeds and re-run the last journey query |
| GET | `/alerts` | Current Translink service alerts |
| GET | `/vehicles` | Live vehicle positions |
| GET | `/geocode?q=...` | Address lookup via Nominatim (OpenStreetMap) |
| GET | `/analytics/density` | Route density GeoJSON |
| GET | `/analytics/gaps` | Coverage gap GeoJSON |
| GET | `/health` | DB age, RT age, vehicle/alert counts |

### POST /journey — body fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `start_lat` | float | yes | — | Origin latitude |
| `start_lon` | float | yes | — | Origin longitude |
| `end_lat` | float | yes | — | Destination latitude |
| `end_lon` | float | yes | — | Destination longitude |
| `depart_after` | string | no | now | ISO-8601 datetime, e.g. `2026-06-04T15:59` |
| `stop_search_radius_m` | float | no | 500 | Max distance (m) to search for nearby stops |

### Journey response

```json
{
  "legs": [...],
  "total_minutes": 53,
  "transfers": 3,
  "origin_stop": "11504",
  "origin_stop_name": "UQ Lakes stop D",
  "destination_stop": "5230",
  "destination_stop_name": "Sussex Rd at Harden Street, stop 58",
  "rt_age_seconds": 12.8,
  "warnings": []
}
```

Each leg:
```json
{
  "route_short_name": "139",
  "route_long_name": "UQ Lakes - City Express",
  "route_type": 3,
  "board_stop_name": "UQ Lakes stop D",
  "board_time": "16:02",
  "alight_stop_name": "Dutton Park Place",
  "alight_time": "16:03",
  "rt_board_time": null,
  "rt_alight_time": null,
  "walk_distance_m": null
}
```

`route_type` follows the GTFS spec: 0 = tram, 2 = train, 3 = bus, 4 = ferry.
Walk legs have `route_type: -1` and a non-null `walk_distance_m`.

---

## Configuration

All tunable values live in **`config.py`**:

| Setting | Default | Description |
|---------|---------|-------------|
| `SQLITE_PATH` | `data/gtfs.db` | SQLite database location |
| `GTFS_STATIC_URL` | Translink open data portal | URL for the SEQ GTFS ZIP |
| `GTFS_MAX_AGE_HOURS` | `24` | Re-download feed if older than this |
| `RT_POLL_INTERVAL_SECONDS` | `30` | How often to refresh GTFS-RT feeds |
| `MIN_TRANSFER_SECONDS` | `120` | Minimum time to change vehicles at the same stop |
| `MAX_WALK_TRANSFER_M` | `400` | Maximum walking distance for automatic inter-stop transfers |
| `WALK_SPEED_MS` | `1.2` | Walking speed in metres per second |
| `WALK_BOARDING_BUFFER_S` | `30` | Extra seconds added to every walk leg |
| `API_HOST` | `127.0.0.1` | Bind address for the FastAPI server |
| `API_PORT` | `8000` | Port for the FastAPI server |

---

## How routing works

The planner uses the **Connection Scan Algorithm (CSA)** — a standard
transit routing algorithm that finds the earliest-arrival journey between
two stops by scanning a time-sorted list of transit connections.

**Footpath model:** At startup, the engine pre-computes a walking-transfer
table between every pair of stops within `MAX_WALK_TRANSFER_M` of each other
(using Haversine distance). This lets the router bridge inter-platform gaps
such as the UQ Lakes horseshoe (stop A to stop D ≈ 62 m, ~81 s walk) without
requiring a fixed-time penalty.

**Transfer penalty:** Changing vehicles at the same stop incurs a
`MIN_TRANSFER_SECONDS` (2 min) penalty. Continuing on a boarded trip, or
boarding at a walked-to stop, incurs no extra penalty — the walk time is
already embedded.

**Real-time:** The Translink GTFS-RT feeds are polled every 30 s and held
in memory. At query time, per-stop delay offsets are applied to each leg's
scheduled times. The original scheduled times are preserved alongside.

---

## Data sources

| Source | URL | Auth |
|--------|-----|------|
| SEQ GTFS static | Queensland Government open data portal | None |
| GTFS-RT (trip updates, vehicles, alerts) | gtfsrt.api.translink.com.au | None |
| Geocoding | Nominatim / OpenStreetMap | None |

All three sources are open and require no API key.

---

## Project layout

```
config.py          Configuration
ingester.py        GTFS static feed downloader and SQLite builder
calendar_resolver.py  Active service_id resolution (day-of-week + exceptions)
routing.py         CSA engine, footpath model, stop proximity
rt_store.py        In-memory RT store and 30s background poller
rt_merge.py        Apply RT delays to journey legs at query time
diff.py            Static feed diff and per-trip schedule-change warnings
analytics.py       Route density and coverage gap GeoJSON generator
main.py            FastAPI application (all endpoints)
static/
  index.html       Journey planner UI
  map.html         Live vehicle map
  analytics.html   Density / gap overlay
  style.css        Shared dark-theme stylesheet
  data/
    density.geojson  (generated by analytics.py)
    gaps.geojson     (generated by analytics.py)
data/
  gtfs.db          SQLite database (generated by ingester.py, git-ignored)
```
