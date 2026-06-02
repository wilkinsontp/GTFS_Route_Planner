"""Step 4 — FastAPI application.

Startup sequence:
  1. Open existing SQLite immediately (serves requests right away).
  2. Check DB age; if stale, spawn background ingester thread.
  3. Pre-load connection list for today into memory.
  4. Serve /journey and static frontend files.

RT poller and merge layer are wired in Steps 5–6.
"""

import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import ingester
from calendar_resolver import cache as cal_cache
from routing import (
    Connection, Footpaths, Journey, Leg,
    compute_footpaths, load_connections, plan_journey, stops_near,
)
from rt_store import start_rt_poller, store as rt_store

# ---------------------------------------------------------------------------
# App state (module-level singletons)
# ---------------------------------------------------------------------------

_db_conn: Optional[sqlite3.Connection] = None
_connections: list[Connection] = []
_connections_date: Optional[date] = None
_footpaths: Footpaths = {}
_conn_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    if _db_conn is None:
        raise HTTPException(503, "Database not ready")
    return _db_conn


def _ensure_connections(today: date) -> tuple[list[Connection], Footpaths]:
    """Return cached (connections, footpaths) for today, reloading if date changed."""
    global _connections, _connections_date, _footpaths
    with _conn_lock:
        if _connections_date != today:
            db = _get_db()
            active = cal_cache.get(db, today)
            _connections = load_connections(db, active)
            if not _footpaths:          # footpaths only depend on stop geometry, not the date
                print(f"{_ts()} [app] Computing footpaths…")
                _footpaths = compute_footpaths(db)
                fp_stops = len(_footpaths)
                fp_edges = sum(len(v) for v in _footpaths.values())
                print(f"{_ts()} [app] Footpaths: {fp_stops:,} stops, {fp_edges:,} walk edges")
            _connections_date = today
            print(
                f"{_ts()} [app] Loaded {len(_connections):,} connections "
                f"for {today} ({len(active)} active service_ids)"
            )
    return _connections, _footpaths


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

def _check_and_maybe_ingest() -> None:
    """Run in a background thread: ingest if the DB is stale."""
    try:
        ingester.ingest(force=False)
    except Exception as e:
        print(f"{_ts()} [ingester] ERROR: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_conn
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not config.SQLITE_PATH.exists():
        print(f"{_ts()} [app] No DB found — running initial ingest (this may take ~6 min)…")
        ingester.ingest(force=True)

    _db_conn = sqlite3.connect(str(config.SQLITE_PATH), check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    print(f"{_ts()} [app] Opened DB: {config.SQLITE_PATH}")

    today = date.today()
    _ensure_connections(today)  # also builds footpaths

    # Spawn background ingester check (non-blocking)
    t = threading.Thread(target=_check_and_maybe_ingest, daemon=True)
    t.start()

    # Start GTFS-RT background poller
    _rt_thread, _rt_stop = start_rt_poller(rt_store)

    yield

    _rt_stop.set()
    if _db_conn:
        _db_conn.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Translink Journey Planner", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class JourneyRequest(BaseModel):
    start_lat: float = Field(..., description="Origin latitude (WGS-84)")
    start_lon: float = Field(..., description="Origin longitude (WGS-84)")
    end_lat: float = Field(..., description="Destination latitude (WGS-84)")
    end_lon: float = Field(..., description="Destination longitude (WGS-84)")
    depart_after: Optional[str] = Field(
        None,
        description="ISO-8601 datetime (local), e.g. '2026-06-02T08:00'. "
                    "Defaults to now if omitted.",
    )
    stop_search_radius_m: float = Field(500.0, ge=50, le=2000)


class LegOut(BaseModel):
    trip_id: str
    route_id: str
    route_short_name: str
    route_long_name: str
    route_type: int           # -1 = walk leg
    board_stop: str
    board_stop_name: str
    board_time: str           # HH:MM
    alight_stop: str
    alight_stop_name: str
    alight_time: str          # HH:MM
    walk_distance_m: Optional[float] = None
    rt_board_time: Optional[str] = None
    rt_alight_time: Optional[str] = None


class JourneyOut(BaseModel):
    legs: list[LegOut]
    total_minutes: int
    transfers: int
    origin_stop: str
    origin_stop_name: str
    destination_stop: str
    destination_stop_name: str
    rt_age_seconds: float
    warnings: list[str]


def _fmt_time(s: Optional[int]) -> Optional[str]:
    if s is None:
        return None
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def _leg_to_out(leg: Leg) -> LegOut:
    return LegOut(
        trip_id=leg.trip_id,
        route_id=leg.route_id,
        route_short_name=leg.route_short_name,
        route_long_name=leg.route_long_name,
        route_type=leg.route_type,
        board_stop=leg.board_stop,
        board_stop_name=leg.board_stop_name,
        board_time=_fmt_time(leg.board_time),
        alight_stop=leg.alight_stop,
        alight_stop_name=leg.alight_stop_name,
        alight_time=_fmt_time(leg.alight_time),
        walk_distance_m=round(leg.walk_distance_m, 1) if leg.walk_distance_m else None,
        rt_board_time=_fmt_time(leg.rt_board_time),
        rt_alight_time=_fmt_time(leg.rt_alight_time),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/journey", response_model=JourneyOut)
def journey(req: JourneyRequest):
    db = _get_db()

    if req.depart_after:
        dt = datetime.fromisoformat(req.depart_after)
        journey_date = dt.date()
        depart_sec = dt.hour * 3600 + dt.minute * 60 + dt.second
    else:
        now = datetime.now()
        journey_date = now.date()
        depart_sec = now.hour * 3600 + now.minute * 60 + now.second

    conns, footpaths = _ensure_connections(journey_date)

    origin_stops = stops_near(req.start_lat, req.start_lon, db, req.stop_search_radius_m)
    dest_stops = stops_near(req.end_lat, req.end_lon, db, req.stop_search_radius_m)

    if not origin_stops:
        raise HTTPException(404, "No stops found near origin within search radius")
    if not dest_stops:
        raise HTTPException(404, "No stops found near destination within search radius")

    # Try origin/dest combos; pick the one with the earliest arrival
    journey_result: Optional[Journey] = None
    chosen_origin = origin_stops[0]
    chosen_dest = dest_stops[0]

    for orig in origin_stops:
        for dst in dest_stops:
            j = plan_journey(orig[0], dst[0], depart_sec, conns, footpaths, db)
            if j is not None:
                if journey_result is None or j.total_time < journey_result.total_time:
                    journey_result = j
                    chosen_origin = orig
                    chosen_dest = dst

    if journey_result is None:
        raise HTTPException(
            404,
            f"No journey found between the given coordinates for "
            f"{today} departing after {_fmt_time(depart_sec)}. "
            "Try a later departure time or wider search radius.",
        )

    return JourneyOut(
        legs=[_leg_to_out(leg) for leg in journey_result.legs],
        total_minutes=journey_result.total_time // 60,
        transfers=journey_result.transfers,
        origin_stop=chosen_origin[0],
        origin_stop_name=chosen_origin[1],
        destination_stop=chosen_dest[0],
        destination_stop_name=chosen_dest[1],
        rt_age_seconds=round(rt_store.age_seconds(), 1),
        warnings=[],  # filled in Step 7
    )


@app.get("/health")
def health():
    db = _get_db()
    row = db.execute("SELECT value FROM metadata WHERE key='last_updated'").fetchone()
    age_h = (time.time() - float(row["value"])) / 3600 if row else None
    return {
        "status": "ok",
        "db_age_hours": round(age_h, 2) if age_h else None,
        "rt_age_seconds": round(rt_store.age_seconds(), 1),
        "rt_trip_updates": len(rt_store.trip_updates),
        "rt_vehicles": len(rt_store.vehicle_positions),
        "rt_alerts": len(rt_store.alerts),
    }


# ---------------------------------------------------------------------------
# Static frontend files (Step 8 — served when present)
# ---------------------------------------------------------------------------

if config.STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.API_HOST,
        port=config.API_PORT,
        reload=False,
    )
