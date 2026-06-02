"""Central configuration — all feed URLs, paths, and tunable constants live here."""

from pathlib import Path

# --- Paths ---
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SQLITE_PATH = DATA_DIR / "gtfs.db"
STATIC_DIR = BASE_DIR / "static"

# --- Static GTFS ---
GTFS_STATIC_URL = (
    "https://www.data.qld.gov.au/dataset/"
    "general-transit-feed-specification-gtfs-translink/"
    "resource/e43b6b9f-fc2b-4630-a7c9-86dd5483552b/"
    "download/SEQ_GTFS.zip"
)
GTFS_MAX_AGE_HOURS = 24

# --- GTFS-RT ---
GTFS_RT_BASE_URL = "https://gtfsrt.api.translink.com.au/api/realtime/SEQ"
RT_POLL_INTERVAL_SECONDS = 30

# --- Routing ---
MIN_TRANSFER_SECONDS = 120   # minimum vehicle-change time when stops are co-located

# Footpath (inter-stop walking) model
MAX_WALK_TRANSFER_M   = 400   # maximum walking distance for an automatic transfer
WALK_SPEED_MS         = 1.2   # metres per second (average pedestrian pace)
WALK_BOARDING_BUFFER_S = 30   # extra seconds added to every walk leg (positioning/boarding)

# --- FastAPI ---
API_HOST = "127.0.0.1"
API_PORT = 8000
