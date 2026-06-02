"""Step 2 — Calendar resolver.

Resolves which service_ids are active for a given date by combining
calendar.txt (day-of-week rules) with calendar_dates.txt (exceptions).
Result is cached and refreshed at midnight or on static feed update.
"""

import sqlite3
import threading
from datetime import date, datetime
from typing import Optional

_DAY_COLS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def active_service_ids(for_date: date, conn: sqlite3.Connection) -> frozenset[str]:
    """Return the set of service_ids running on for_date.

    Applies calendar.txt base rules then calendar_dates.txt exceptions
    (exception_type 1 = add service, 2 = remove service).
    """
    dow = for_date.weekday()  # 0=Monday … 6=Sunday
    day_col = _DAY_COLS[dow]
    date_str = for_date.strftime("%Y%m%d")

    # Base set from calendar.txt (day-of-week + date range)
    rows = conn.execute(
        f"SELECT service_id FROM calendar "
        f"WHERE {day_col}=1 AND start_date<=? AND end_date>=?",
        (date_str, date_str),
    ).fetchall()
    active = set(r[0] for r in rows)

    # Apply calendar_dates.txt exceptions
    exceptions = conn.execute(
        "SELECT service_id, exception_type FROM calendar_dates WHERE date=?",
        (date_str,),
    ).fetchall()
    for service_id, exception_type in exceptions:
        if exception_type == 1:
            active.add(service_id)
        elif exception_type == 2:
            active.discard(service_id)

    return frozenset(active)


class CalendarCache:
    """Thread-safe cache of active service_ids, refreshed at midnight."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._date: Optional[date] = None
        self._ids: frozenset[str] = frozenset()

    def get(self, conn: sqlite3.Connection, for_date: Optional[date] = None) -> frozenset[str]:
        today = for_date or date.today()
        with self._lock:
            if self._date != today:
                self._ids = active_service_ids(today, conn)
                self._date = today
        return self._ids

    def invalidate(self) -> None:
        with self._lock:
            self._date = None


# Module-level singleton used by the FastAPI app
cache = CalendarCache()
