"""FastAPI application — all steps wired together.

Startup: open SQLite → load connections + footpaths → start RT poller →
         spawn background ingester check → serve requests.
"""

import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

import requests as _requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import ingester
from calendar_resolver import cache as cal_cache
from routing import (
    Connection, Footpaths, Journey, Leg,
    compute_footpaths, load_connections, plan_journey, stops_near,
)
import analytics as analytics_mod
import diff as diff_mod
from rt_merge import merge_rt
from rt_store import start_rt_poller, store as rt_store

# ---------------------------------------------------------------------------
# App state (module-level singletons)
# ---------------------------------------------------------------------------

_db_conn: Optional[sqlite3.Connection] = None
_connections: list[Connection] = []
_connections_date: Optional[date] = None
_footpaths: Footpaths = {}
_conn_lock = threading.Lock()
_last_journey_req: Optional["JourneyRequest"] = None


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

    # Warm analytics cache in background (avoids 20 s delay on first /analytics request)
    analytics_mod.warm_cache(_db_conn)

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
        description="ISO-8601 datetime, e.g. '2026-06-02T08:00'. "
                    "Mutually exclusive with arrive_before. Defaults to now.",
    )
    arrive_before: Optional[str] = Field(
        None,
        description="ISO-8601 datetime for arrive-by queries. "
                    "Mutually exclusive with depart_after.",
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
    global _last_journey_req
    _last_journey_req = req
    db = _get_db()

    # Resolve date and time constraint
    arrive_sec: Optional[int] = None
    depart_sec: Optional[int] = None

    if req.arrive_before:
        dt = datetime.fromisoformat(req.arrive_before)
        journey_date = dt.date()
        arrive_sec = dt.hour * 3600 + dt.minute * 60 + dt.second
    elif req.depart_after:
        dt = datetime.fromisoformat(req.depart_after)
        journey_date = dt.date()
        depart_sec = dt.hour * 3600 + dt.minute * 60 + dt.second
    else:
        now = datetime.now()
        journey_date = now.date()
        depart_sec = now.hour * 3600 + now.minute * 60 + now.second

    conns, footpaths = _ensure_connections(journey_date)

    origin_stops = stops_near(req.start_lat, req.start_lon, db, req.stop_search_radius_m)
    dest_stops   = stops_near(req.end_lat,   req.end_lon,   db, req.stop_search_radius_m)

    if not origin_stops:
        raise HTTPException(404, "No stops found near origin within search radius")
    if not dest_stops:
        raise HTTPException(404, "No stops found near destination within search radius")

    # Try all origin/dest combos; pick the best result.
    # For depart_after: minimise total_time (earliest arrival).
    # For arrive_before: maximise departure time (leave as late as possible),
    #   represented as minimise (arrive_before - board_time).
    journey_result: Optional[Journey] = None
    chosen_origin = origin_stops[0]
    chosen_dest   = dest_stops[0]

    for orig in origin_stops:
        for dst in dest_stops:
            j = plan_journey(
                orig[0], dst[0], conns, footpaths, db,
                depart_after=depart_sec,
                arrive_before=arrive_sec,
            )
            if j is None:
                continue
            if journey_result is None:
                journey_result, chosen_origin, chosen_dest = j, orig, dst
            elif arrive_sec is not None:
                # Latest departure wins
                j_dep  = j.legs[0].board_time  if j.legs  else 0
                best_dep = journey_result.legs[0].board_time if journey_result.legs else 0
                if j_dep > best_dep:
                    journey_result, chosen_origin, chosen_dest = j, orig, dst
            else:
                # Earliest arrival wins
                if j.total_time < journey_result.total_time:
                    journey_result, chosen_origin, chosen_dest = j, orig, dst

    if journey_result is None:
        constraint = (f"arriving before {_fmt_time(arrive_sec)}"
                      if arrive_sec else f"departing after {_fmt_time(depart_sec)}")
        raise HTTPException(
            404,
            f"No journey found for {journey_date} {constraint}. "
            "Try adjusting the time or widening the stop search radius.",
        )

    # Apply RT delays and collect warnings
    journey_result, rt_warnings = merge_rt(journey_result, rt_store, db)

    # Add any static schedule-change warnings from the last feed diff
    trip_ids = [l.trip_id for l in journey_result.legs if l.route_type != -1]
    diff_warnings = diff_mod.check_warnings(trip_ids)

    return JourneyOut(
        legs=[_leg_to_out(leg) for leg in journey_result.legs],
        total_minutes=journey_result.total_time // 60,
        transfers=journey_result.transfers,
        origin_stop=chosen_origin[0],
        origin_stop_name=chosen_origin[1],
        destination_stop=chosen_dest[0],
        destination_stop_name=chosen_dest[1],
        rt_age_seconds=round(rt_store.age_seconds(), 1),
        warnings=rt_warnings + diff_warnings,
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


@app.get("/alerts")
def alerts():
    snap = rt_store.snapshot()
    return {
        "alerts": snap["alerts"],
        "count": len(snap["alerts"]),
        "rt_age_seconds": round(rt_store.age_seconds(), 1),
    }


@app.get("/vehicles")
def vehicles():
    snap = rt_store.snapshot()
    vp   = snap["vehicle_positions"]

    enriched = []
    if vp:
        db = _get_db()
        trip_ids = list(vp.keys())
        ph = ",".join("?" * len(trip_ids))
        rows = db.execute(
            f"SELECT t.trip_id, r.route_short_name, r.route_type, "
            f"       t.direction_id, t.trip_headsign "
            f"FROM trips t JOIN routes r ON t.route_id = r.route_id "
            f"WHERE t.trip_id IN ({ph})",
            trip_ids,
        ).fetchall()
        trip_info = {r[0]: r for r in rows}

        for trip_id, v in vp.items():
            info = trip_info.get(trip_id)
            if info:
                _, route, route_type, direction_id, headsign = info
                direction = (("Inbound" if direction_id == 1 else "Outbound")
                             if direction_id is not None else None)
            else:
                route = route_type = direction = headsign = None
            enriched.append({
                **v,
                "trip_id":    trip_id,
                "route":      route,
                "route_type": route_type,
                "direction":  direction,
                "headsign":   headsign,
            })

    return {
        "vehicles": enriched,
        "count": len(enriched),
        "rt_age_seconds": round(rt_store.age_seconds(), 1),
    }


@app.post("/refresh")
def refresh():
    """Immediately re-poll all RT feeds, then re-run the last journey query."""
    from rt_store import _poll_once
    _poll_once(rt_store)
    if _last_journey_req is not None:
        return journey(_last_journey_req)
    return {"status": "ok", "rt_age_seconds": round(rt_store.age_seconds(), 1)}


@app.get("/geocode")
def geocode(q: str = Query(..., min_length=3)):
    """Proxy to Nominatim — returns up to 5 candidate locations."""
    try:
        r = _requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "countrycodes": "au", "limit": 5,
                    "addressdetails": 0},
            headers={"User-Agent": "Translink-Journey-Planner/1.0 (local)"},
            timeout=8,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        raise HTTPException(502, f"Geocoding service unavailable: {exc}")


@app.get("/analytics/density")
def analytics_density(
    cell_deg: float = Query(0.01, ge=0.005, le=0.05,
                            description="Grid cell size in degrees (~0.005°=550m … 0.05°=5.5km)"),
):
    db = _get_db()
    return analytics_mod.compute_density(db, cell_deg=cell_deg)


@app.get("/analytics/gaps")
def analytics_gaps(
    cell_deg: float   = Query(0.01,  ge=0.005, le=0.05),
    gap_radius_m: float = Query(800.0, ge=200.0, le=3000.0,
                                description="Cells with no stop within this radius are flagged"),
):
    db = _get_db()
    return analytics_mod.compute_gaps(db, gap_radius_m=gap_radius_m, cell_deg=cell_deg)


# ---------------------------------------------------------------------------
# Static frontend files
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
