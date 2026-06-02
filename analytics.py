"""Step 9 — Analytics layer.

Generates two GeoJSON files written to static/data/:
  density.geojson — service frequency heatmap (departures per grid cell)
  gaps.geojson    — grid cells with no stop within GAP_RADIUS_M

Run standalone:
    py analytics.py

Or trigger via the API after starting the server (results cached as files).
Pure Python — no geopandas/shapely required.
"""

import json
import logging
import math
import sqlite3
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)
logging.basicConfig(format="%(asctime)s [analytics] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)

# ---------------------------------------------------------------------------
# SEQ bounding box and grid
# ---------------------------------------------------------------------------

LAT_MIN, LAT_MAX =  -28.2, -26.5
LON_MIN, LON_MAX =  152.5,  153.6
CELL_DEG         =  0.01          # ~1.1 km × 0.9 km cells
GAP_RADIUS_M     =  800.0         # cells with no stop within this = gap


def _grid_cells():
    """Yield (lat_min, lat_max, lon_min, lon_max) for every cell in the box."""
    lat = LAT_MIN
    while lat < LAT_MAX:
        lon = LON_MIN
        while lon < LON_MAX:
            yield (round(lat, 6), round(lat + CELL_DEG, 6),
                   round(lon, 6), round(lon + CELL_DEG, 6))
            lon += CELL_DEG
        lat += CELL_DEG


def _cell_centre(cell):
    lat0, lat1, lon0, lon1 = cell
    return (lat0 + lat1) / 2, (lon0 + lon1) / 2


def _haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl   = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _cell_polygon(cell):
    lat0, lat1, lon0, lon1 = cell
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def _feature(cell, props):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [_cell_polygon(cell)]},
        "properties": props,
    }


# ---------------------------------------------------------------------------
# Density — departures per grid cell
# ---------------------------------------------------------------------------

def compute_density(conn: sqlite3.Connection) -> dict:
    """Count stop_times departures per grid cell using stop coordinates."""
    log.info("Loading stops and departure counts…")
    rows = conn.execute("""
        SELECT s.stop_lat, s.stop_lon, COUNT(st.rowid) AS dep_count
        FROM stops s
        JOIN stop_times st ON s.stop_id = st.stop_id
        WHERE s.stop_lat BETWEEN ? AND ?
          AND s.stop_lon BETWEEN ? AND ?
          AND st.departure_time IS NOT NULL
        GROUP BY s.stop_id
    """, (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX)).fetchall()

    log.info("  %d stops with departures", len(rows))

    # Aggregate into grid
    cell_counts: dict[tuple, int] = {}
    for slat, slon, cnt in rows:
        ci = int((slat - LAT_MIN) / CELL_DEG)
        cj = int((slon - LON_MIN) / CELL_DEG)
        lat0 = round(LAT_MIN + ci * CELL_DEG, 6)
        lon0 = round(LON_MIN + cj * CELL_DEG, 6)
        key = (lat0, round(lat0 + CELL_DEG, 6), lon0, round(lon0 + CELL_DEG, 6))
        cell_counts[key] = cell_counts.get(key, 0) + cnt

    if not cell_counts:
        return {"type": "FeatureCollection", "features": []}

    max_count = max(cell_counts.values())
    features = []
    for cell, count in cell_counts.items():
        intensity = count / max_count  # 0–1
        features.append(_feature(cell, {
            "count":     count,
            "intensity": round(intensity, 4),
        }))

    log.info("  %d non-empty density cells (max departures: %d)", len(features), max_count)
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Gaps — grid cells with no stop within GAP_RADIUS_M
# ---------------------------------------------------------------------------

def compute_gaps(conn: sqlite3.Connection) -> dict:
    """Find grid cells in the SEQ box that have no stop within GAP_RADIUS_M."""
    log.info("Loading all stops for gap analysis…")
    stops = conn.execute(
        "SELECT stop_lat, stop_lon FROM stops "
        "WHERE stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?",
        (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX),
    ).fetchall()
    log.info("  %d stops loaded", len(stops))

    # Build a quick grid-cell index for stops (same technique as footpaths)
    stop_grid: dict[tuple, list] = {}
    for slat, slon in stops:
        key = (int((slat - LAT_MIN) / CELL_DEG), int((slon - LON_MIN) / CELL_DEG))
        stop_grid.setdefault(key, []).append((slat, slon))

    search_cells = max(1, int(math.ceil(GAP_RADIUS_M / (CELL_DEG * 111_320))))

    features = []
    n_cells = 0
    for cell in _grid_cells():
        n_cells += 1
        clat, clon = _cell_centre(cell)
        ci = int((clat - LAT_MIN) / CELL_DEG)
        cj = int((clon - LON_MIN) / CELL_DEG)

        nearest = float("inf")
        for di in range(-search_cells, search_cells + 1):
            for dj in range(-search_cells, search_cells + 1):
                for slat, slon in stop_grid.get((ci + di, cj + dj), []):
                    d = _haversine(clat, clon, slat, slon)
                    if d < nearest:
                        nearest = d

        if nearest > GAP_RADIUS_M:
            features.append(_feature(cell, {
                "nearest_stop_m": round(nearest, 0) if nearest < 1e8 else None,
            }))

    log.info("  %d gap cells out of %d total", len(features), n_cells)
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_all(db_path: Path = config.SQLITE_PATH,
                 out_dir: Path = config.STATIC_DIR / "data") -> None:
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
