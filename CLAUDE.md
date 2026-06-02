# Translink Journey Planner — Project Context

This file provides full project context for Claude Code. Read it before scaffolding,
generating, or modifying any code in this project.

---

## Project Purpose

A locally-run web application for journey planning using Queensland (Translink) public
transport data. Core goals:

- Accept start/end locations and depart-after or arrive-before parameters
- Return optimal routes with configurable minimum/maximum journey legs
- Overlay real-time delays, alerts, and vehicle positions onto static schedule data
- Visualise route analytics (e.g. route density by location, geographic network gaps)

Development is local-only for now. The web UI is served by FastAPI on localhost.

---

## Stack Decisions

| Layer | Choice | Reason |
|---|---|---|
| Backend API | FastAPI | Lightweight, auto-docs, async-friendly |
| Static data store | SQLite | Index-friendly joins, no server needed, atomic file swap |
| RT data store | In-memory (Python dataclass) | Ephemeral by nature, never persisted |
| Routing engine | CSA or RAPTOR | Standard transit algorithms; do not brute-force with pandas |
| Map / frontend | Leaflet.js (static HTML) | No build step, runs locally |
| Analytics | GeoPandas + Shapely → GeoJSON | Batch job, output fed to Leaflet |
| Geocoding | Nominatim (OpenStreetMap) | No API key required for low-volume local use |

**Do not use Django. Do not use Flask. Do not use pandas for journey planning queries.**
Pandas is acceptable only for batch analytics jobs (route density, gap analysis).

---

## Data Sources

### Static GTFS (no authentication required)

Downloaded as a ZIP of CSV files from the Queensland Government open data portal.

- **SEQ dataset URL:**
  `https://www.data.qld.gov.au/dataset/general-transit-feed-specification-gtfs-translink/resource/e43b6b9f-fc2b-4630-a7c9-86dd5483552b/download/SEQ_GTFS.zip`

Key files inside the ZIP:

| File | Purpose |
|---|---|
| `stops.txt` | Stop IDs, names, lat/lon |
| `routes.txt` | Route IDs, short/long names, mode |
| `trips.txt` | Trip IDs linked to routes and service IDs |
| `stop_times.txt` | Scheduled arrival/departure per stop per trip (large: 5–15M rows) |
| `calendar.txt` | Service validity by day of week + date range |
| `calendar_dates.txt` | Exceptions to calendar (public holidays etc.) |
| `shapes.txt` | Route geometry for map display |

### GTFS-RT (no authentication required)

Base URL: `https://gtfsrt.api.translink.com.au/api/realtime/SEQ`

| Feed | Endpoint |
|---|---|
| Trip updates (all modes) | `/TripUpdates` |
| Vehicle positions (all modes) | `/VehiclePositions` |
| Service alerts | `/alerts` |
| Trip updates by mode | `/TripUpdates/Bus`, `/TripUpdates/Rail`, `/TripUpdates/Tram`, `/TripUpdates/Ferry` |
| Vehicle positions by mode | `/VehiclePositions/Bus`, `/VehiclePositions/Rail`, etc. |

Data is encoded as Protocol Buffers. Use the `gtfs-realtime-bindings` Python package
to decode. Use Translink's own `.proto` definition (not Google's generic one):
`https://translink.com.au/sites/default/files/acquiadam-documents/gtfs-realtime.proto`

Community support / feed change announcements:
`https://groups.google.com/forum/#!forum/translink-australia-opendata`

---

## Architecture

### Data Lifecycle

```
Application start
    ├── Load existing SQLite (immediate — serves requests right away)
    ├── Check last_updated timestamp on SQLite
    │       └── If > 24h or first use today → spawn background ingester thread
    ├── Start RT background poller (every 30s, background thread)
    └── Begin serving requests

Background ingester completes
    ├── Diff old vs new GTFS (affected trip_ids and scheduled times)
    ├── Atomic swap: replace SQLite file reference
    └── Set pending_warning flag with diff summary

User submits journey query
    ├── Read RTStore (in-memory, instant — never HTTP at query time)
    ├── Run routing engine (CSA/RAPTOR) against SQLite
    ├── Merge RT delay offsets onto candidate trips
    ├── Check pending_warning — attach to response if any result trips are in diff
    └── Return results + rt_age_seconds field

User clicks Refresh button
    ├── Trigger immediate RT re-poll (all three feeds)
    ├── Re-run last query against updated RTStore
    └── Return refreshed results + updated rt_age_seconds
```

### RT Merge Logic

RT data **patches** static schedule data — it does not replace it.

- A `trip_update` entity contains stop-level delay offsets keyed by `trip_id` + `stop_sequence`
- If a trip has no RT entry, treat it as on-schedule (do not discard it)
- Apply delay offsets to scheduled times **at query time**, not at ingestion time
- RT data is held in memory only; the background poller replaces it atomically every 30s

### Static Diff / Warning Logic

When the background ingester completes a feed refresh:

1. Snapshot `trip_id` + `departure_time` for all trips in the **old** SQLite
2. Compare against the same fields in the **new** SQLite
3. Classify changes: stop removed, trip cancelled, schedule time changed (>5 min delta), new route added
4. Store the diff as a `pending_warning` dict keyed by `trip_id`
5. At query time, if any trip in the result set appears in `pending_warning`, attach the
   relevant warning message to that leg of the journey result

Do **not** diff the entire GTFS feed — only trips relevant to the current query need warning.

---

## RTStore Design

```python
import threading
from dataclasses import dataclass, field
from typing import Dict, List

@dataclass
class RTStore:
    trip_updates: Dict[str, dict] = field(default_factory=dict)
    alerts: List[dict] = field(default_factory=list)
    vehicle_positions: Dict[str, dict] = field(default_factory=dict)
    last_updated: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def update(self, trip_updates, alerts, vehicle_positions):
        """Atomic swap — never partially updated."""
        with self._lock:
            self.trip_updates = trip_updates
            self.alerts = alerts
            self.vehicle_positions = vehicle_positions
            self.last_updated = time.time()

    def get_delay(self, trip_id: str, stop_sequence: int) -> int:
        """Returns delay in seconds, 0 if no RT data for this trip/stop."""
        with self._lock:
            ...
```

The lock is critical — FastAPI serves requests across threads while the background
poller writes. Always use the atomic swap pattern (build new dicts, then replace in
one assignment under the lock).

---

## API Endpoints

```
POST  /journey          # start, end, depart_after|arrive_before, min_legs, max_legs
GET   /alerts           # current service alerts affecting stops or routes in a result
GET   /vehicles         # live vehicle positions (polled by frontend every 30s)
GET   /analytics/density  # route density GeoJSON for map overlay
GET   /analytics/gaps     # geographic gap analysis GeoJSON
POST  /refresh          # trigger immediate RT re-poll, re-run last query
```

Every journey response includes:
- `rt_age_seconds` — how old the RT data is
- `warnings` — list of any static diff warnings affecting result trips
- `legs` — list of journey legs with scheduled and actual (RT-adjusted) times

---

## SQLite Schema (key indexes)

```sql
-- Critical indexes for query performance
CREATE INDEX idx_stop_times_stop_id    ON stop_times(stop_id);
CREATE INDEX idx_stop_times_trip_id    ON stop_times(trip_id);
CREATE INDEX idx_stop_times_departure  ON stop_times(departure_time);
CREATE INDEX idx_trips_service_id      ON trips(service_id);
CREATE INDEX idx_trips_route_id        ON trips(route_id);
CREATE INDEX idx_stops_location        ON stops(stop_lat, stop_lon);
```

`stop_times` will have 5–15 million rows. Indexes are non-negotiable.

---

## Calendar Resolution

GTFS `calendar.txt` + `calendar_dates.txt` is fiddly and must be correct.
Get this right before implementing the routing engine — every schedule query
depends on resolving which `service_id` values are active for today's date.

Logic:
1. Check `calendar.txt` for services valid on today's day-of-week within date range
2. Apply `calendar_dates.txt` exceptions (add or remove service for specific dates)
3. Result: set of active `service_id` values for today

Cache this set at startup and refresh at midnight or on static feed update.

---

## Routing Engine

Implement **CSA (Connection Scan Algorithm)** first — it is simpler than RAPTOR
(~200 lines of Python) and sufficient for most journey planning queries.
RAPTOR can be added later if needed for round-based multi-leg optimisation.

CSA works on a sorted list of connections (departure_stop, arrival_stop,
departure_time, arrival_time, trip_id). Pre-sort this at ingestion time and
store as a structured array for fast scanning.

---

## Frontend

Plain HTML + Leaflet.js. No npm, no build step, no framework.
Served as static files by FastAPI.

Pages:
- **Journey planner** — location inputs (with geocoding), depart/arrive toggle,
  leg count slider, results with RT-adjusted times and warnings
- **Live map** — vehicle positions updated every 30s, service alerts overlay
- **Analytics** — route density heatmap, gap visualisation (hex-bin or grid)

The Refresh button triggers `POST /refresh` and updates the displayed results
and `rt_age_seconds` indicator in-place without a full page reload.

---

## Build Order

✅ = complete and tested against live data.

1. ✅ **GTFS ingester** (`ingester.py`) — download ZIP, parse CSVs, load into SQLite with correct indexes
2. ✅ **Calendar resolver** (`calendar_resolver.py`) — active service_id set for today's date
3. ✅ **Static journey planner** (`routing.py`) — CSA engine against SQLite, no RT
4. ✅ **FastAPI wrapper** (`main.py`) — `/journey` endpoint working against static data
5. ✅ **RTStore + background poller** (`rt_store.py`) — 30s poll, graceful degradation if feed unavailable
6. ✅ **RT merge layer** (`rt_merge.py`) — patch delays into journey results, `rt_age_seconds` field
7. **Static diff / warning system** (`diff.py`) — diff on feed update, per-trip warnings in results
8. **Frontend** (`static/`) — Leaflet map, journey UI, refresh button
9. **Analytics layer** (`analytics.py`) — GeoPandas density + gap analysis, GeoJSON output

Steps 1–4 can be built and tested with no network access to RT endpoints.
Steps 5–7 require the RT feeds to be accessible (they are open, no auth needed).

---

## Implementation Notes (decisions made during build)

### Routing engine — CSA with footpaths

The CSA implementation in `routing.py` extends the standard algorithm with
a **footpath table** pre-computed at startup from stop lat/lon coordinates:

- `compute_footpaths(conn)` uses a grid-cell spatial index to find all stop
  pairs within `MAX_WALK_TRANSFER_M` (400 m default), then converts distance
  to walk seconds via `WALK_SPEED_MS` (1.2 m/s) + `WALK_BOARDING_BUFFER_S`
  (30 s). Builds 85k edges across 12,850 stops in ~0.45 s.
- Walk edges are propagated in the CSA scan: when a stop is newly reached
  by transit, all walkable neighbours are immediately updated in `earliest[]`.
- Walk legs appear explicitly in the journey result (route_type = -1,
  `walk_distance_m` field set) so the frontend can render them correctly.

**Transfer penalty rules** (in `_csa_raw`):

| Situation | Penalty |
|---|---|
| Continuing on the same boarded trip | None |
| Boarding at origin or a walked-to stop | None (walk time IS the transfer cost) |
| Changing vehicles at a transit-arrived stop | `MIN_TRANSFER_SECONDS` (120 s) |

This correctly models Brisbane's South East Busway: same-platform rapid
interchanges get a 2-minute minimum, while cross-platform or inter-stop
transfers (e.g. UQ Lakes stop A → stop D, ~62 m / 81 s) are modelled from
the actual physical distance.

### RT merge layer

`rt_merge.merge_rt(journey, rt_store, conn)` looks up each transit leg's
board and alight `stop_sequence` from SQLite, then reads the per-stop delay
from `RTStore.trip_updates[trip_id]["stop_delays"]`, falling back to the
trip-level delay if the specific stop has no entry.  Walk legs are skipped.
Cancellations (schedule_relationship == 3) generate a warning string.
Delays ≥ 5 minutes generate a warning string.  The function returns the
mutated journey and a list of warning strings; the original scheduled times
are preserved alongside the RT-adjusted times.

### API endpoints — current state

| Endpoint | Status | Notes |
|---|---|---|
| `POST /journey` | ✅ live | RT delays merged, walk legs included, warnings returned |
| `GET /health` | ✅ live | DB age, RT age, trip/vehicle/alert counts |
| `GET /alerts` | ❌ not yet | Planned Step 7/8 |
| `GET /vehicles` | ❌ not yet | Planned Step 8 |
| `POST /refresh` | ❌ not yet | Planned Step 8 |

### Known missing features (planned)

- `arrive_before` journey planning (CSA currently only supports `depart_after`)
- `min_legs` / `max_legs` parameters
- Multi-leg optimisation (RAPTOR) for round-based transfers

---

## Python Dependencies

```
fastapi
uvicorn[standard]
gtfs-realtime-bindings
requests
geopandas
shapely
sqlite3          # stdlib
threading        # stdlib
```

For geocoding, use the `geopy` library with the Nominatim backend.
Do not use Google Maps API — no key available and not needed for local use.

---

## Conventions

- All times stored and compared as seconds-since-midnight integers (GTFS standard)
- Times past midnight (e.g. 25:30:00) are valid in GTFS — handle them correctly
- Use `pathlib.Path` throughout, not `os.path`
- Configuration (feed URLs, poll interval, SQLite path) in a single `config.py`
- Log RT poll results and ingester progress to stdout with timestamps
- The app must start and serve requests from existing SQLite even if RT feeds are down
