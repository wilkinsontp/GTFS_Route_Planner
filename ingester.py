"""GTFS ingester — downloads the static feed ZIP, parses all CSVs, loads SQLite.

Run directly:
    python ingester.py           # skip if DB is fresh (< GTFS_MAX_AGE_HOURS)
    python ingester.py --force   # always re-download
"""

import csv
import io
import logging
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

import config
import diff as diff_mod

logging.basicConfig(
    format="%(asctime)s [ingester] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

_BATCH_SIZE = 10_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gtfs_time_to_seconds(t: str) -> Optional[int]:
    """Convert GTFS HH:MM:SS (may exceed 24 h) to seconds since midnight.

    Returns None for empty/blank values (non-timepoint stops).
    """
    t = t.strip()
    if not t:
        return None
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _download_zip(url: str) -> bytes:
    log.info("Downloading GTFS feed from %s", url)
    r = requests.get(url, stream=True, timeout=180)
    r.raise_for_status()
    chunks: list[bytes] = []
    downloaded = 0
    for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MB chunks
        chunks.append(chunk)
        downloaded += len(chunk)
        if downloaded % (10 << 20) < (1 << 20):  # log every ~10 MB
            log.info("  %.0f MB downloaded…", downloaded / 1e6)
    log.info("Download complete: %.1f MB", downloaded / 1e6)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA cache_size=-65536;

CREATE TABLE stops (
    stop_id   TEXT PRIMARY KEY,
    stop_name TEXT,
    stop_lat  REAL,
    stop_lon  REAL
);

CREATE TABLE routes (
    route_id         TEXT PRIMARY KEY,
    route_short_name TEXT,
    route_long_name  TEXT,
    route_type       INTEGER
);

CREATE TABLE trips (
    trip_id       TEXT PRIMARY KEY,
    route_id      TEXT,
    service_id    TEXT,
    shape_id      TEXT,
    direction_id  INTEGER,
    trip_headsign TEXT
);

CREATE TABLE stop_times (
    trip_id        TEXT,
    arrival_time   INTEGER,
    departure_time INTEGER,
    stop_id        TEXT,
    stop_sequence  INTEGER,
    pickup_type    INTEGER,
    drop_off_type  INTEGER
);

CREATE TABLE calendar (
    service_id TEXT PRIMARY KEY,
    monday     INTEGER,
    tuesday    INTEGER,
    wednesday  INTEGER,
    thursday   INTEGER,
    friday     INTEGER,
    saturday   INTEGER,
    sunday     INTEGER,
    start_date TEXT,
    end_date   TEXT
);

CREATE TABLE calendar_dates (
    service_id     TEXT,
    date           TEXT,
    exception_type INTEGER
);

CREATE TABLE shapes (
    shape_id          TEXT,
    shape_pt_lat      REAL,
    shape_pt_lon      REAL,
    shape_pt_sequence INTEGER
);

CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_INDEXES = """
CREATE INDEX idx_stop_times_stop_id   ON stop_times(stop_id);
CREATE INDEX idx_stop_times_trip_id   ON stop_times(trip_id);
CREATE INDEX idx_stop_times_departure ON stop_times(departure_time);
CREATE INDEX idx_trips_service_id     ON trips(service_id);
CREATE INDEX idx_trips_route_id       ON trips(route_id);
CREATE INDEX idx_stops_location       ON stops(stop_lat, stop_lon);
CREATE INDEX idx_calendar_dates       ON calendar_dates(service_id, date);
CREATE INDEX idx_shapes               ON shapes(shape_id, shape_pt_sequence);
"""


# ---------------------------------------------------------------------------
# CSV → SQLite loader
# ---------------------------------------------------------------------------

def _load_csv(
    zf: zipfile.ZipFile,
    filename: str,
    conn: sqlite3.Connection,
    table: str,
    ncols: int,
    transform: Callable[[dict], Optional[tuple]],
) -> int:
    """Stream-parse one GTFS CSV from the ZIP and bulk-insert into SQLite.

    Commits every _BATCH_SIZE rows so the WAL stays bounded even for
    stop_times (5–15 M rows).  Returns the total row count inserted.
    """
    try:
        raw = zf.read(filename)
    except KeyError:
        log.warning("  %s not found in ZIP — skipping", filename)
        return 0

    placeholders = ", ".join("?" * ncols)
    sql = f"INSERT INTO {table} VALUES ({placeholders})"

    reader = csv.DictReader(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig"))
    batch: list[tuple] = []
    total = 0

    for row in reader:
        record = transform(row)
        if record is not None:
            batch.append(record)

        if len(batch) >= _BATCH_SIZE:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch = []

    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)

    log.info("  %-24s %10d rows", filename, total)
    return total


# ---------------------------------------------------------------------------
# Per-file transforms
# ---------------------------------------------------------------------------

def _t_stops(r: dict) -> tuple:
    return (
        r["stop_id"],
        r.get("stop_name", ""),
        float(r["stop_lat"]),
        float(r["stop_lon"]),
    )


def _t_routes(r: dict) -> tuple:
    return (
        r["route_id"],
        r.get("route_short_name", ""),
        r.get("route_long_name", ""),
        int(r["route_type"]),
    )


def _t_trips(r: dict) -> tuple:
    direction = r.get("direction_id", "").strip()
    return (
        r["trip_id"],
        r["route_id"],
        r["service_id"],
        r.get("shape_id", ""),
        int(direction) if direction else None,
        r.get("trip_headsign", ""),
    )


def _t_stop_times(r: dict) -> Optional[tuple]:
    arr = _gtfs_time_to_seconds(r.get("arrival_time", ""))
    dep = _gtfs_time_to_seconds(r.get("departure_time", ""))
    # Both times absent → non-timepoint with no usable time, skip
    if arr is None and dep is None:
        return None
    # If one is absent, mirror the other (common in some feeds)
    if arr is None:
        arr = dep
    if dep is None:
        dep = arr
    pu = r.get("pickup_type", "0").strip()
    do = r.get("drop_off_type", "0").strip()
    return (
        r["trip_id"],
        arr,
        dep,
        r["stop_id"],
        int(r["stop_sequence"]),
        int(pu) if pu else 0,
        int(do) if do else 0,
    )


def _t_calendar(r: dict) -> tuple:
    return (
        r["service_id"],
        int(r["monday"]),
        int(r["tuesday"]),
        int(r["wednesday"]),
        int(r["thursday"]),
        int(r["friday"]),
        int(r["saturday"]),
        int(r["sunday"]),
        r["start_date"],
        r["end_date"],
    )


def _t_calendar_dates(r: dict) -> tuple:
    return (r["service_id"], r["date"], int(r["exception_type"]))


def _t_shapes(r: dict) -> tuple:
    return (
        r["shape_id"],
        float(r["shape_pt_lat"]),
        float(r["shape_pt_lon"]),
        int(r["shape_pt_sequence"]),
    )


# ---------------------------------------------------------------------------
# Build the database
# ---------------------------------------------------------------------------

def _build_db(zip_bytes: bytes, db_path: Path) -> None:
    """Parse the GTFS ZIP and write a fully-indexed SQLite database.

    Uses an atomic temp-file + rename so the old DB is never half-written.
    """
    with tempfile.NamedTemporaryFile(
        dir=db_path.parent, suffix=".tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        conn = sqlite3.connect(tmp_path, isolation_level=None)
        conn.executescript(_SCHEMA)

        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        log.info("ZIP contents: %s", sorted(zf.namelist()))

        _load_csv(zf, "stops.txt",          conn, "stops",          4, _t_stops)
        _load_csv(zf, "routes.txt",         conn, "routes",         4, _t_routes)
        _load_csv(zf, "trips.txt",          conn, "trips",          6, _t_trips)
        _load_csv(zf, "stop_times.txt",     conn, "stop_times",     7, _t_stop_times)
        _load_csv(zf, "calendar.txt",       conn, "calendar",       10, _t_calendar)
        _load_csv(zf, "calendar_dates.txt", conn, "calendar_dates", 3, _t_calendar_dates)
        _load_csv(zf, "shapes.txt",         conn, "shapes",         4, _t_shapes)

        conn.execute(
            "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
            ("last_updated", str(time.time())),
        )
        conn.commit()

        log.info("Building indexes…")
        conn.executescript(_INDEXES)
        log.info("Indexes done.")

        conn.close()

        # Atomic swap — never leaves the DB in a half-written state
        tmp_path.replace(db_path)
        log.info("SQLite written → %s", db_path)

    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ingest(force: bool = False) -> None:
    """Download GTFS feed and rebuild SQLite.

    Skips the download if the existing DB is younger than GTFS_MAX_AGE_HOURS
    and force=False.
    """
    db_path = config.SQLITE_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='last_updated'"
            ).fetchone()
            conn.close()
            if row:
                age_h = (time.time() - float(row[0])) / 3600
                if age_h < config.GTFS_MAX_AGE_HOURS:
                    log.info(
                        "DB is %.1f h old (limit %d h) — skipping download",
                        age_h,
                        config.GTFS_MAX_AGE_HOURS,
                    )
                    return
        except sqlite3.Error:
            pass  # Corrupt or missing metadata → fall through to fresh download

    # Snapshot old feed before refresh (for diff warnings)
    old_snap: dict = {}
    if db_path.exists():
        try:
            old_conn = sqlite3.connect(db_path)
            old_snap = diff_mod.snapshot_trips(old_conn)
            old_conn.close()
            log.info("Snapshotted %d trips from existing feed for diff.", len(old_snap))
        except Exception as exc:
            log.warning("Could not snapshot old feed: %s", exc)

    t0 = time.time()
    zip_bytes = _download_zip(config.GTFS_STATIC_URL)
    log.info("Parsing GTFS and loading SQLite…")
    _build_db(zip_bytes, db_path)
    log.info("Ingestion complete in %.1f s", time.time() - t0)

    # Diff new feed against snapshot and store per-trip warnings
    if old_snap:
        try:
            new_conn = sqlite3.connect(db_path)
            n = diff_mod.apply_diff(old_snap, new_conn)
            new_conn.close()
            log.info("Feed diff: %d trip warning(s) stored.", n)
        except Exception as exc:
            log.warning("Feed diff failed: %s", exc)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="GTFS static feed ingester")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if the database is recent",
    )
    args = ap.parse_args()
    ingest(force=args.force)
