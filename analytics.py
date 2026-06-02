"""Step 9 — Analytics layer.

Generates two GeoJSON files written to static/data/ for the default resolution,
and also provides interactive (variable cell_deg / gap_radius_m) endpoints
backed by an in-memory per-stop cache.

Run standalone to regenerate default files:
    py analytics.py

Pure Python — no geopandas/shapely required.
"""

import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s [analytics] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)

# ---------------------------------------------------------------------------
# SEQ bounding box and grid defaults
# ---------------------------------------------------------------------------

LAT_MIN, LAT_MAX =  -28.2, -26.5
LON_MIN, LON_MAX =  152.5,  153.6
DEFAULT_CELL_DEG =  0.01
DEFAULT_GAP_M    =  800.0

# ---------------------------------------------------------------------------
# In-memory per-stop cache (loaded once, reused for all cell sizes)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_stop_counts: Optional[list] = None    # [(lat, lon, dep_count), ...]
_stop_positions: Optional[list] = None # [(lat, lon), ...]


def _load_stop_counts(conn: sqlite3.Connection) -> list:
    """Per-stop departure counts. Slow first call (~20 s), instant after."""
    global _stop_counts
    with _cache_lock:
        if _stop_counts is None:
            log.info("Loading per-stop departure counts (first call — may take ~20 s)…")
            t0 = time.time()
            rows = conn.execute("""
                SELECT s.stop_lat, s.stop_lon, COUNT(st.rowid) AS dep_count
                FROM stops s
                JOIN stop_times st ON s.stop_id = st.stop_id
                WHERE s.stop_lat BETWEEN ? AND ?
                  AND s.stop_lon BETWEEN ? AND ?
                  AND st.departure_time IS NOT NULL
                GROUP BY s.stop_id
            """, (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX)).fetchall()
            _stop_counts = rows
            log.info("  %d stops loaded in %.1f s", len(rows), time.time() - t0)
        return _stop_counts


def _load_stop_positions(conn: sqlite3.Connection) -> list:
    """All stop positions (fast, ~0.1 s)."""
    global _stop_positions
    with _cache_lock:
        if _stop_positions is None:
            rows = conn.execute(
                "SELECT stop_lat, stop_lon FROM stops "
                "WHERE stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?",
                (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX),
            ).fetchall()
            _stop_positions = rows
        return _stop_positions


def warm_cache(conn: sqlite3.Connection) -> None:
    """Call at startup to pre-load the slow SQL query in a background thread."""
    threading.Thread(target=_load_stop_counts, args=(conn,), daemon=True,
                     name="analytics-warm").start()

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _cell_polygon(lat0, lat1, lon0, lon1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def _feature(lat0, lat1, lon0, lon1, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon",
                     "coordinates": [_cell_polygon(lat0, lat1, lon0, lon1)]},
        "properties": props,
    }

# ---------------------------------------------------------------------------
# Density — departures per grid cell (variable cell_deg)
# ---------------------------------------------------------------------------

def compute_density(
    conn: sqlite3.Connection,
    cell_deg: float = DEFAULT_CELL_DEG,
) -> dict:
    """Service frequency heatmap at the requested grid resolution."""
    rows = _load_stop_counts(conn)

    cell_counts: dict[tuple, int] = {}
    for slat, slon, cnt in rows:
        ci = int((slat - LAT_MIN) / cell_deg)
        cj = int((slon - LON_MIN) / cell_deg)
        lat0 = round(LAT_MIN + ci * cell_deg, 7)
        lon0 = round(LON_MIN + cj * cell_deg, 7)
        key  = (lat0, lon0)
        cell_counts[key] = cell_counts.get(key, 0) + cnt

    if not cell_counts:
        return {"type": "FeatureCollection", "features": []}

    max_count = max(cell_counts.values())
    features = [
        _feature(lat0, round(lat0 + cell_deg, 7),
                 lon0, round(lon0 + cell_deg, 7),
                 {"count": cnt, "intensity": round(cnt / max_count, 4)})
        for (lat0, lon0), cnt in cell_counts.items()
    ]
    log.info("density: %d cells (cell_deg=%.4f, max=%d)", len(features), cell_deg, max_count)
    return {"type": "FeatureCollection", "features": features}

# ---------------------------------------------------------------------------
# Gaps — grid cells with no stop within gap_radius_m (variable both params)
# ---------------------------------------------------------------------------

def compute_gaps(
    conn: sqlite3.Connection,
    gap_radius_m: float = DEFAULT_GAP_M,
    cell_deg: float     = DEFAULT_CELL_DEG,
) -> dict:
    """Coverage gap analysis at the requested grid resolution and gap radius."""
    stops = _load_stop_positions(conn)

    # Build spatial grid for fast nearest-stop lookup
    sc = max(1, int(math.ceil(gap_radius_m / (cell_deg * 111_320))))
    stop_grid: dict[tuple, list] = {}
    for slat, slon in stops:
        key = (int((slat - LAT_MIN) / cell_deg), int((slon - LON_MIN) / cell_deg))
        stop_grid.setdefault(key, []).append((slat, slon))

    features = []
    lat = LAT_MIN
    while lat < LAT_MAX:
        lon = LON_MIN
        while lon < LON_MAX:
            clat = lat + cell_deg / 2
            clon = lon + cell_deg / 2
            ci   = int((clat - LAT_MIN) / cell_deg)
            cj   = int((clon - LON_MIN) / cell_deg)

            nearest = float("inf")
            for di in range(-sc, sc + 1):
                for dj in range(-sc, sc + 1):
                    for slat, slon in stop_grid.get((ci + di, cj + dj), []):
                        d = _haversine(clat, clon, slat, slon)
                        if d < nearest:
                            nearest = d

            if nearest > gap_radius_m:
                features.append(_feature(
                    round(lat, 7), round(lat + cell_deg, 7),
                    round(lon, 7), round(lon + cell_deg, 7),
                    {"nearest_stop_m": round(nearest, 0) if nearest < 1e8 else None},
                ))
            lon = round(lon + cell_deg, 7)
        lat = round(lat + cell_deg, 7)

    log.info("gaps: %d cells (cell_deg=%.4f, radius=%.0fm)", len(features), cell_deg, gap_radius_m)
    return {"type": "FeatureCollection", "features": features}

# ---------------------------------------------------------------------------
# Standalone entry point — generates default-resolution files
# ---------------------------------------------------------------------------

def generate_all(
    db_path: Path = config.SQLITE_PATH,
    out_dir: Path = config.STATIC_DIR / "data",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)

    t0 = time.time()
    log.info("=== Route density ===")
    density = compute_density(conn)
    (out_dir / "density.geojson").write_text(json.dumps(density))
    log.info("  Written density.geojson (%d features)", len(density["features"]))

    log.info("=== Gap analysis ===")
    gaps = compute_gaps(conn)
    (out_dir / "gaps.geojson").write_text(json.dumps(gaps))
    log.info("  Written gaps.geojson (%d features)", len(gaps["features"]))

    conn.close()
    log.info("Analytics complete in %.1f s", time.time() - t0)


if __name__ == "__main__":
    generate_all()
