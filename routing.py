"""Step 3 — CSA (Connection Scan Algorithm) routing engine.

Works against the SQLite database and a pre-resolved frozenset of active
service_ids from calendar_resolver.  Returns journey legs (one leg = one
vehicle trip) from origin stop to destination stop.

Footpath model
--------------
At startup, compute_footpaths() builds a table of walking edges between every
pair of stops within MAX_WALK_TRANSFER_M of each other.  Walking time is
derived from the Haversine distance divided by WALK_SPEED_MS, plus a boarding
buffer.  These edges let the CSA cross inter-platform gaps (e.g. UQ Lakes
stop A to stop D) without requiring a fixed minimum transfer time.

Transfer penalty logic
----------------------
- Continuing on an already-boarded trip: no penalty
- Boarding at origin or a stop reached only by walking: no extra penalty
  (the walk time is already baked into earliest[stop])
- Changing vehicles at a stop reached by transit: MIN_TRANSFER_SECONDS applied
"""

import bisect
import sqlite3
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from typing import Optional

import config

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

WALK_TRIP_ID = "__WALK__"   # sentinel used for footpath "connections"


@dataclass(frozen=True, order=True)
class Connection:
    dep_time: int       # seconds since midnight
    arr_time: int
    dep_stop: str
    arr_stop: str
    trip_id: str        # WALK_TRIP_ID for footpaths


@dataclass
class Leg:
    trip_id: str
    route_id: str
    route_short_name: str
    route_long_name: str
    route_type: int             # -1 for walk legs
    board_stop: str
    board_stop_name: str
    board_time: int             # scheduled, seconds since midnight
    alight_stop: str
    alight_stop_name: str
    alight_time: int
    walk_distance_m: Optional[float] = None   # set for walk legs only
    rt_board_time: Optional[int] = None
    rt_alight_time: Optional[int] = None


@dataclass
class Journey:
    legs: list[Leg]
    total_time: int     # alight_time of last leg - board_time of first leg
    transfers: int      # number of vehicle changes (walk legs excluded)


# Footpaths type: {stop_id: [(nearby_stop_id, walk_seconds, distance_m), ...]}
Footpaths = dict[str, list[tuple[str, int, float]]]


# ---------------------------------------------------------------------------
# Connection loading
# ---------------------------------------------------------------------------

def load_connections(
    conn: sqlite3.Connection,
    active_services: frozenset[str],
) -> list[Connection]:
    """Load and sort all transit connections for the active service day.

    A connection is a pair of consecutive stop_times on the same trip.
    """
    if not active_services:
        return []

    placeholders = ",".join("?" * len(active_services))
    rows = conn.execute(
        f"""
        SELECT st.trip_id, st.stop_id, st.stop_sequence,
               st.departure_time, st.arrival_time
        FROM stop_times st
        JOIN trips t ON st.trip_id = t.trip_id
        WHERE t.service_id IN ({placeholders})
          AND st.departure_time IS NOT NULL
        ORDER BY st.trip_id, st.stop_sequence
        """,
        list(active_services),
    ).fetchall()

    connections: list[Connection] = []
    prev: Optional[tuple] = None
    for trip_id, stop_id, seq, dep_time, arr_time in rows:
        if prev is not None and prev[0] == trip_id:
            connections.append(
                Connection(
                    dep_time=prev[3],
                    arr_time=arr_time,
                    dep_stop=prev[1],
                    arr_stop=stop_id,
                    trip_id=trip_id,
                )
            )
        prev = (trip_id, stop_id, seq, dep_time, arr_time)

    connections.sort()
    return connections


# ---------------------------------------------------------------------------
# Footpath computation
# ---------------------------------------------------------------------------

_EARTH_R = 6_371_000  # metres


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * _EARTH_R * atan2(sqrt(a), sqrt(1 - a))


def compute_footpaths(
    conn: sqlite3.Connection,
    max_walk_m: float = config.MAX_WALK_TRANSFER_M,
    walk_speed_ms: float = config.WALK_SPEED_MS,
    boarding_buffer_s: int = config.WALK_BOARDING_BUFFER_S,
) -> Footpaths:
    """Pre-compute walking edges between every pair of stops within max_walk_m.

    Uses a grid-based spatial index so the O(n²) distance matrix is never
    evaluated — only cells that could contain a neighbour are checked.

    walk_seconds = ceil(distance / walk_speed) + boarding_buffer
    """
    stops = conn.execute(
        "SELECT stop_id, stop_lat, stop_lon FROM stops"
    ).fetchall()

    # Grid cell size slightly larger than max search radius
    cell_deg = (max_walk_m / 111_320) * 1.5
    grid: dict[tuple[int, int], list] = {}
    for stop_id, lat, lon in stops:
        key = (int(lat / cell_deg), int(lon / cell_deg))
        grid.setdefault(key, []).append((stop_id, lat, lon))

    footpaths: Footpaths = {}
    for stop_id, lat, lon in stops:
        ci, cj = int(lat / cell_deg), int(lon / cell_deg)
        nearby: list[tuple[str, int, float]] = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for s_id, s_lat, s_lon in grid.get((ci + di, cj + dj), []):
                    if s_id == stop_id:
                        continue
                    d = _haversine(lat, lon, s_lat, s_lon)
                    if d <= max_walk_m:
                        walk_s = int(d / walk_speed_ms) + boarding_buffer_s
                        nearby.append((s_id, walk_s, d))
        if nearby:
            nearby.sort(key=lambda x: x[1])   # closest first
            footpaths[stop_id] = nearby

    return footpaths


# ---------------------------------------------------------------------------
# CSA with footpath propagation
# ---------------------------------------------------------------------------

_INF = 10**9


def _csa_raw(
    connections: list[Connection],
    footpaths: Footpaths,
    origin: str,
    destination: str,
    depart_after: int,
    min_transfer_s: int = config.MIN_TRANSFER_SECONDS,
) -> dict[str, Connection]:
    """Core CSA pass with footpath (inter-stop walking) support.

    Transfer penalty rules:
    - Same trip continuation          → no penalty
    - Boarding at a walked-to stop    → no extra penalty (walk IS the transfer)
    - Vehicle change at transit stop  → MIN_TRANSFER_SECONDS enforced
    """
    earliest: dict[str, int] = {origin: depart_after}
    in_connection: dict[str, Connection] = {}  # transit connections only
    walked_to: set[str] = set()               # stops reached (only) via walking
    boarded_trip: set[str] = set()

    # Propagate initial footpaths from origin so nearby stops are reachable
    # from the start without boarding any vehicle first.
    for nearby_stop, walk_s, dist_m in footpaths.get(origin, []):
        walk_arr = depart_after + walk_s
        if walk_arr < earliest.get(nearby_stop, _INF):
            earliest[nearby_stop] = walk_arr
            in_connection[nearby_stop] = Connection(
                dep_time=depart_after, arr_time=walk_arr,
                dep_stop=origin, arr_stop=nearby_stop,
                trip_id=WALK_TRIP_ID,
            )
            walked_to.add(nearby_stop)

    start_idx = bisect.bisect_left(
        connections, Connection(depart_after, 0, "", "", "")
    )

    for c in connections[start_idx:]:
        # --- Can we board this connection? ---
        if c.trip_id in boarded_trip:
            can_board = True
        else:
            arrived_by_transit = (
                c.dep_stop in in_connection
                and in_connection[c.dep_stop].trip_id != WALK_TRIP_ID
            )
            if arrived_by_transit:
                # Vehicle change: enforce minimum transfer time
                can_board = earliest.get(c.dep_stop, _INF) + min_transfer_s <= c.dep_time
            else:
                # Origin or walked-to stop: board whenever the bus is there
                can_board = earliest.get(c.dep_stop, _INF) <= c.dep_time

        if not can_board:
            continue

        if c.arr_time >= earliest.get(c.arr_stop, _INF):
            continue

        # --- Update arrival and propagate footpaths from the new stop ---
        earliest[c.arr_stop] = c.arr_time
        in_connection[c.arr_stop] = c
        boarded_trip.add(c.trip_id)
        walked_to.discard(c.arr_stop)  # now reached by transit, not just walking

        for nearby_stop, walk_s, dist_m in footpaths.get(c.arr_stop, []):
            walk_arr = c.arr_time + walk_s
            if walk_arr < earliest.get(nearby_stop, _INF):
                earliest[nearby_stop] = walk_arr
                in_connection[nearby_stop] = Connection(
                    dep_time=c.arr_time, arr_time=walk_arr,
                    dep_stop=c.arr_stop, arr_stop=nearby_stop,
                    trip_id=WALK_TRIP_ID,
                )
                walked_to.add(nearby_stop)

    return in_connection


# ---------------------------------------------------------------------------
# Journey reconstruction
# ---------------------------------------------------------------------------

def _backtrack(
    in_connection: dict[str, Connection],
    origin: str,
    destination: str,
) -> Optional[list[Connection]]:
    """Trace in_connection back from destination to origin."""
    if destination not in in_connection:
        return None
    path: list[Connection] = []
    stop = destination
    seen: set[str] = set()
    while stop != origin:
        if stop in seen:
            return None
        seen.add(stop)
        if stop not in in_connection:
            return None
        c = in_connection[stop]
        path.append(c)
        stop = c.dep_stop
    path.reverse()
    return path


def _connections_to_legs(
    path: list[Connection],
    conn: sqlite3.Connection,
    footpaths: Footpaths,
) -> list[Leg]:
    """Group consecutive same-trip connections into transit and walk legs."""
    if not path:
        return []

    transit_trip_ids = [c.trip_id for c in path if c.trip_id != WALK_TRIP_ID]
    all_stop_ids = list({s for c in path for s in (c.dep_stop, c.arr_stop)})

    t_ph = ",".join("?" * len(transit_trip_ids)) if transit_trip_ids else "NULL"
    s_ph = ",".join("?" * len(all_stop_ids))

    trip_meta: dict[str, tuple] = {}
    if transit_trip_ids:
        trip_meta = {
            row[0]: row
            for row in conn.execute(
                f"""SELECT t.trip_id, t.route_id, r.route_short_name,
                           r.route_long_name, r.route_type
                    FROM trips t JOIN routes r ON t.route_id = r.route_id
                    WHERE t.trip_id IN ({t_ph})""",
                transit_trip_ids,
            ).fetchall()
        }

    stop_meta: dict[str, tuple] = {
        row[0]: row
        for row in conn.execute(
            f"SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops WHERE stop_id IN ({s_ph})",
            all_stop_ids,
        ).fetchall()
    }

    # Build a distance lookup from the footpaths table for walk legs
    def _walk_dist(from_stop: str, to_stop: str) -> Optional[float]:
        for s_id, walk_s, dist_m in footpaths.get(from_stop, []):
            if s_id == to_stop:
                return dist_m
        return None

    legs: list[Leg] = []
    current_transit: list[Connection] = []

    def flush_transit():
        if not current_transit:
            return
        first, last = current_transit[0], current_transit[-1]
        meta = trip_meta.get(first.trip_id, (first.trip_id, "", "", "", 3))
        sname = lambda sid: stop_meta.get(sid, (sid, sid, 0.0, 0.0))[1]
        legs.append(Leg(
            trip_id=first.trip_id,
            route_id=meta[1],
            route_short_name=meta[2],
            route_long_name=meta[3],
            route_type=meta[4],
            board_stop=first.dep_stop,
            board_stop_name=sname(first.dep_stop),
            board_time=first.dep_time,
            alight_stop=last.arr_stop,
            alight_stop_name=sname(last.arr_stop),
            alight_time=last.arr_time,
        ))
        current_transit.clear()

    for c in path:
        if c.trip_id == WALK_TRIP_ID:
            flush_transit()
            dist_m = _walk_dist(c.dep_stop, c.arr_stop)
            sname = lambda sid: stop_meta.get(sid, (sid, sid, 0.0, 0.0))[1]
            legs.append(Leg(
                trip_id=WALK_TRIP_ID,
                route_id="",
                route_short_name="Walk",
                route_long_name="Walk",
                route_type=-1,
                board_stop=c.dep_stop,
                board_stop_name=sname(c.dep_stop),
                board_time=c.dep_time,
                alight_stop=c.arr_stop,
                alight_stop_name=sname(c.arr_stop),
                alight_time=c.arr_time,
                walk_distance_m=dist_m,
            ))
        else:
            if current_transit and c.trip_id != current_transit[-1].trip_id:
                flush_transit()
            current_transit.append(c)

    flush_transit()
    return legs


# ---------------------------------------------------------------------------
# Stop proximity (used by API to find candidate stops near a lat/lon)
# ---------------------------------------------------------------------------

def stops_near(
    lat: float,
    lon: float,
    conn: sqlite3.Connection,
    radius_m: float = 500.0,
    limit: int = 5,
) -> list[tuple[str, str, float]]:
    """Return up to `limit` stops within radius_m, sorted by distance."""
    deg_lat = radius_m / 111_320
    deg_lon = radius_m / (111_320 * cos(radians(lat)))

    rows = conn.execute(
        """SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops
           WHERE stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?""",
        (lat - deg_lat, lat + deg_lat, lon - deg_lon, lon + deg_lon),
    ).fetchall()

    candidates = [
        (stop_id, stop_name, _haversine(lat, lon, slat, slon))
        for stop_id, stop_name, slat, slon in rows
    ]
    candidates = [(s, n, d) for s, n, d in candidates if d <= radius_m]
    candidates.sort(key=lambda x: x[2])
    return candidates[:limit]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_journey(
    origin_stop: str,
    dest_stop: str,
    depart_after: int,
    connections: list[Connection],
    footpaths: Footpaths,
    conn: sqlite3.Connection,
) -> Optional[Journey]:
    """Find the earliest-arrival journey from origin_stop to dest_stop.

    `connections` is the pre-loaded sorted list for today.
    `footpaths` is the pre-computed inter-stop walking table.
    """
    if origin_stop == dest_stop:
        return Journey(legs=[], total_time=0, transfers=0)

    in_conn = _csa_raw(connections, footpaths, origin_stop, dest_stop, depart_after)
    path = _backtrack(in_conn, origin_stop, dest_stop)
    if path is None:
        return None

    legs = _connections_to_legs(path, conn, footpaths)
    transit_legs = [l for l in legs if l.route_type != -1]
    total = legs[-1].alight_time - legs[0].board_time if legs else 0
    return Journey(legs=legs, total_time=total, transfers=max(0, len(transit_legs) - 1))
