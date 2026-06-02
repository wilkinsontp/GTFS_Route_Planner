"""Step 7 — Static GTFS diff and per-trip warning system.

After each feed refresh, compares the old and new SQLite databases to detect
schedule changes (cancelled trips, time shifts >5 min, removed stops, new routes).
Stores a pending_warning dict keyed by trip_id; warnings are attached to
journey results at query time.
"""

# TODO: implement diff_feeds(old_conn, new_conn) -> dict[str, str]
# TODO: implement check_warnings(trip_ids, pending_warning) -> list[str]
