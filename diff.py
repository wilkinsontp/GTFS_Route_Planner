"""Step 7 — Static GTFS diff and per-trip warning system.

When the background ingester completes a feed refresh it calls:
    1. snapshot_trips(old_conn)   before the swap
    2. apply_diff(old_snap, new_conn)  after the swap

Warnings are stored in a module-level dict keyed by trip_id and consumed
by the /journey endpoint at query time.
"""

import sqlite3
import threading
from typing import Dict, Optional, Tuple

# Warning store — trip_id -> human-readable warning string
_lock = threading.Lock()
_pending: Dict[str, str] = {}

# Snapshot type: {trip_id: (route_short_name, first_dep_sec, stop_count)}
Snapshot = Dict[str, Tuple[str, Optional[int], int]]

DELAY_THRESHOLD_S = 300   # 5 minutes


def snapshot_trips(conn: sqlite3.Connection) -> Snapshot:
    """Capture a lightweight summary of every trip in the current feed."""
    rows = conn.execute("""
        SELECT t.trip_id,
               r.route_short_name,
               MIN(st.departure_time) AS first_dep,
               COUNT(st.stop_id)      AS stop_count
        FROM trips t
        JOIN routes r      ON t.route_id  = r.route_id
        JOIN stop_times st ON t.trip_id   = st.trip_id
        GROUP BY t.trip_id, r.route_short_name
    """).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def apply_diff(old_snap: Snapshot, new_conn: sqlite3.Connection) -> int:
    """Diff old snapshot against the newly-loaded feed and store warnings.

    Returns the number of warnings generated.
    """
    if not old_snap:
        return 0

    new_snap = snapshot_trips(new_conn)
    new_warnings: Dict[str, str] = {}

    for trip_id, (route, old_dep, old_stops) in old_snap.items():
        if trip_id not in new_snap:
            new_warnings[trip_id] = (
                f"Schedule update: route {route} trip no longer appears "
                f"in the current feed — it may have been cancelled or renumbered."
            )
            continue

        _, new_dep, new_stops = new_snap[trip_id]

        if new_stops < old_stops:
            dropped = old_stops - new_stops
            new_warnings[trip_id] = (
                f"Schedule update: route {route} has had {dropped} "
                f"stop{'s' if dropped != 1 else ''} removed from this trip."
            )

        if old_dep is not None and new_dep is not None:
            delta = new_dep - old_dep
            if abs(delta) >= DELAY_THRESHOLD_S:
                direction = "earlier" if delta < 0 else "later"
                mins = abs(delta) // 60
                new_warnings[trip_id] = (
                    f"Schedule update: route {route} departure is now "
                    f"{mins} min {direction} than the previous timetable."
                )

    # New trips in fresh feed don't need warnings — they're additions.

    with _lock:
        _pending.clear()
        _pending.update(new_warnings)

    return len(new_warnings)


def check_warnings(trip_ids: list[str]) -> list[str]:
    """Return warning strings for any of the given trip_ids that have one."""
    with _lock:
        return [_pending[tid] for tid in trip_ids if tid in _pending]


def pending_count() -> int:
    with _lock:
        return len(_pending)
