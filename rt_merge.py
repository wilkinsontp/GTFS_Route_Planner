"""Step 6 — RT merge layer.

Patches real-time delay offsets from RTStore onto static journey legs at
query time.  Never modifies SQLite — all merging happens in memory per
request.

Design
------
Each transit leg carries a trip_id and board/alight stop_ids.  We look up
the stop_sequence for those stops in the RTStore's stop_delays dict, then
add the delay to the scheduled time.  If stop-level data is absent we fall
back to the trip-level delay.  Walk legs are skipped (no RT data).

Cancellation
------------
If a leg's trip is marked CANCELLED (schedule_relationship == 3) the leg is
flagged and the caller should surface a warning to the user.
"""

import sqlite3
from typing import Optional

from routing import Leg, Journey, WALK_TRIP_ID
from rt_store import RTStore


# ---------------------------------------------------------------------------
# Stop-sequence lookup (cached per request via the passed-in dict)
# ---------------------------------------------------------------------------

def _stop_sequences(
    trip_id: str,
    stop_ids: list[str],
    conn: sqlite3.Connection,
    cache: dict,
) -> dict[str, int]:
    """Return {stop_id: stop_sequence} for the given trip and stop list."""
    key = trip_id
    if key not in cache:
        ph = ",".join("?" * len(stop_ids))
        rows = conn.execute(
            f"SELECT stop_id, stop_sequence FROM stop_times "
            f"WHERE trip_id=? AND stop_id IN ({ph})",
            [trip_id] + stop_ids,
        ).fetchall()
        cache[key] = {r[0]: r[1] for r in rows}
    return cache[key]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def merge_rt(
    journey: Journey,
    rt: RTStore,
    conn: sqlite3.Connection,
) -> tuple[Journey, list[str]]:
    """Apply RT delays to all transit legs in a Journey.

    Returns (updated_journey, warnings) where warnings is a list of plain-
    text strings describing cancellations or significant delays (>= 5 min).

    The journey object is mutated in-place (rt_board_time / rt_alight_time
    fields on each Leg are filled).  The original scheduled times are kept.
    """
    warnings: list[str] = []
    seq_cache: dict[str, dict] = {}

    snapshot = rt.snapshot()
    trip_updates = snapshot["trip_updates"]

    for leg in journey.legs:
        if leg.route_type == -1 or leg.trip_id == WALK_TRIP_ID:
            continue  # walk legs have no RT data

        tu = trip_updates.get(leg.trip_id)
        if not tu:
            # No RT data for this trip — leave rt times as None (on-schedule)
            continue

        # Cancelled trip
        if tu.get("schedule_relationship") == 3:
            warnings.append(
                f"Trip {leg.route_short_name} "
                f"({leg.board_stop_name} → {leg.alight_stop_name}) "
                f"has been CANCELLED."
            )
            continue

        # Look up stop sequences for board and alight stops
        seqs = _stop_sequences(
            leg.trip_id,
            [leg.board_stop, leg.alight_stop],
            conn,
            seq_cache,
        )

        board_seq  = seqs.get(leg.board_stop)
        alight_seq = seqs.get(leg.alight_stop)

        trip_delay = tu["delay"]
        stop_delays = tu["stop_delays"]

        board_delay  = stop_delays.get(board_seq,  trip_delay) if board_seq  else trip_delay
        alight_delay = stop_delays.get(alight_seq, trip_delay) if alight_seq else trip_delay

        leg.rt_board_time  = leg.board_time  + board_delay
        leg.rt_alight_time = leg.alight_time + alight_delay

        # Warn on significant delays (>= 5 minutes on either end of the leg)
        max_delay = max(board_delay, alight_delay)
        if max_delay >= 300:
            mins = max_delay // 60
            warnings.append(
                f"Route {leg.route_short_name} is running approximately "
                f"{mins} minute{'s' if mins != 1 else ''} late "
                f"({leg.board_stop_name} → {leg.alight_stop_name})."
            )

    return journey, warnings
