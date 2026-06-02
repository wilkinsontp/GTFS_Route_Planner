"""Step 5 — In-memory RTStore and GTFS-RT background poller.

The RTStore holds the three real-time feeds from Translink in memory only —
data is never persisted.  A background thread polls every RT_POLL_INTERVAL_SECONDS
and replaces the store atomically so readers never see a partial update.

Graceful degradation: if any feed is unreachable the store retains stale data
and the rt_age_seconds field in journey responses tells the client how old it is.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests
from google.transit import gtfs_realtime_pb2

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types stored in the RTStore
# ---------------------------------------------------------------------------

# trip_updates  — keyed by trip_id
#   {
#     "delay":        int,              # latest overall delay in seconds
#     "stop_delays":  {stop_sequence: delay_seconds},
#     "schedule_relationship": int,     # 0=SCHEDULED, 1=ADDED, 3=CANCELED
#   }

# vehicle_positions — keyed by trip_id
#   {
#     "vehicle_id": str,
#     "lat":        float,
#     "lon":        float,
#     "bearing":    float | None,
#     "speed":      float | None,       # m/s
#     "timestamp":  int,                # unix epoch
#     "stop_id":    str | None,         # current/next stop
#   }

# alerts — list of dicts
#   {
#     "id":               str,
#     "header":           str,
#     "description":      str,
#     "severity":         int,
#     "informed_routes":  [route_id, ...],
#     "informed_stops":   [stop_id, ...],
#     "informed_trips":   [trip_id, ...],
#   }


# ---------------------------------------------------------------------------
# RTStore
# ---------------------------------------------------------------------------

@dataclass
class RTStore:
    trip_updates:       Dict[str, dict] = field(default_factory=dict)
    alerts:             List[dict]       = field(default_factory=list)
    vehicle_positions:  Dict[str, dict]  = field(default_factory=dict)
    last_updated:       float            = 0.0
    _lock:              threading.RLock  = field(default_factory=threading.RLock)

    def update(
        self,
        trip_updates:      Dict[str, dict],
        alerts:            List[dict],
        vehicle_positions: Dict[str, dict],
    ) -> None:
        """Atomic swap — readers see either the old or the new state, never both."""
        with self._lock:
            self.trip_updates      = trip_updates
            self.alerts            = alerts
            self.vehicle_positions = vehicle_positions
            self.last_updated      = time.time()

    def get_delay(self, trip_id: str, stop_sequence: int) -> int:
        """Return delay in seconds (0 if no RT data for this trip/stop)."""
        with self._lock:
            tu = self.trip_updates.get(trip_id)
            if not tu:
                return 0
            return tu["stop_delays"].get(stop_sequence, tu["delay"])

    def is_cancelled(self, trip_id: str) -> bool:
        with self._lock:
            tu = self.trip_updates.get(trip_id)
            return bool(tu and tu.get("schedule_relationship") == 3)

    def age_seconds(self) -> float:
        return time.time() - self.last_updated if self.last_updated else float("inf")

    def snapshot(self) -> dict:
        """Return a shallow copy of all three feeds under the lock."""
        with self._lock:
            return {
                "trip_updates":      dict(self.trip_updates),
                "alerts":            list(self.alerts),
                "vehicle_positions": dict(self.vehicle_positions),
                "last_updated":      self.last_updated,
            }


# ---------------------------------------------------------------------------
# Feed parsing helpers
# ---------------------------------------------------------------------------

def _fetch_feed(url: str, timeout: int = 15) -> Optional[gtfs_realtime_pb2.FeedMessage]:
    """Download and parse one GTFS-RT protobuf feed.  Returns None on failure."""
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(r.content)
        return msg
    except Exception as exc:
        log.warning("RT fetch failed for %s: %s", url, exc)
        return None


def _parse_trip_updates(feed: Optional[gtfs_realtime_pb2.FeedMessage]) -> Dict[str, dict]:
    if not feed:
        return {}
    result: Dict[str, dict] = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        if not trip_id:
            continue

        stop_delays: Dict[int, int] = {}
        latest_delay = 0
        for stu in tu.stop_time_update:
            delay = 0
            if stu.HasField("departure") and stu.departure.HasField("delay"):
                delay = stu.departure.delay
            elif stu.HasField("arrival") and stu.arrival.HasField("delay"):
                delay = stu.arrival.delay
            stop_delays[stu.stop_sequence] = delay
            latest_delay = delay  # last entry is the most current

        result[trip_id] = {
            "delay":                  latest_delay,
            "stop_delays":            stop_delays,
            "schedule_relationship":  tu.trip.schedule_relationship,
        }
    return result


def _parse_vehicle_positions(feed: Optional[gtfs_realtime_pb2.FeedMessage]) -> Dict[str, dict]:
    if not feed:
        return {}
    result: Dict[str, dict] = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle
        trip_id = vp.trip.trip_id if vp.HasField("trip") else ""
        if not trip_id:
            continue
        pos = vp.position if vp.HasField("position") else None
        result[trip_id] = {
            "vehicle_id": vp.vehicle.id if vp.HasField("vehicle") else "",
            "lat":        pos.latitude  if pos else None,
            "lon":        pos.longitude if pos else None,
            "bearing":    pos.bearing   if (pos and pos.HasField("bearing")) else None,
            "speed":      pos.speed     if (pos and pos.HasField("speed"))   else None,
            "timestamp":  vp.timestamp  if vp.timestamp else int(time.time()),
            "stop_id":    vp.stop_id    if vp.stop_id else None,
        }
    return result


def _parse_alerts(feed: Optional[gtfs_realtime_pb2.FeedMessage]) -> List[dict]:
    if not feed:
        return []
    result: List[dict] = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        a = entity.alert

        def _txt(translated_string) -> str:
            if translated_string.translation:
                return translated_string.translation[0].text
            return ""

        informed_routes, informed_stops, informed_trips = [], [], []
        for ie in a.informed_entity:
            if ie.route_id:
                informed_routes.append(ie.route_id)
            if ie.stop_id:
                informed_stops.append(ie.stop_id)
            if ie.HasField("trip") and ie.trip.trip_id:
                informed_trips.append(ie.trip.trip_id)

        result.append({
            "id":              entity.id,
            "header":          _txt(a.header_text),
            "description":     _txt(a.description_text),
            "severity":        a.severity_level,
            "informed_routes": informed_routes,
            "informed_stops":  informed_stops,
            "informed_trips":  informed_trips,
        })
    return result


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------

def _poll_once(store: RTStore) -> None:
    """Fetch all three feeds and atomically update the store."""
    base = config.GTFS_RT_BASE_URL
    tu_feed  = _fetch_feed(f"{base}/TripUpdates")
    vp_feed  = _fetch_feed(f"{base}/VehiclePositions")
    al_feed  = _fetch_feed(f"{base}/alerts")

    trip_updates      = _parse_trip_updates(tu_feed)
    vehicle_positions = _parse_vehicle_positions(vp_feed)
    alerts            = _parse_alerts(al_feed)

    store.update(trip_updates, alerts, vehicle_positions)
    log.info(
        "RT updated: %d trip_updates, %d vehicles, %d alerts",
        len(trip_updates), len(vehicle_positions), len(alerts),
    )


def _poller_loop(store: RTStore, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _poll_once(store)
        except Exception as exc:
            log.error("RT poll error: %s", exc)
        stop_event.wait(config.RT_POLL_INTERVAL_SECONDS)


def start_rt_poller(store: RTStore) -> tuple[threading.Thread, threading.Event]:
    """Start the background RT poller.  Returns (thread, stop_event).

    Call stop_event.set() to request a graceful shutdown.
    The first poll runs immediately in the background thread.
    """
    stop_event = threading.Event()
    t = threading.Thread(
        target=_poller_loop,
        args=(store, stop_event),
        daemon=True,
        name="rt-poller",
    )
    t.start()
    log.info("RT poller started (interval=%ds)", config.RT_POLL_INTERVAL_SECONDS)
    return t, stop_event


# ---------------------------------------------------------------------------
# Module-level singleton used by the FastAPI app
# ---------------------------------------------------------------------------

store = RTStore()
