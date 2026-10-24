"""NOAA NDBC wave record at the nearest buoy (Req 5.6; reused by Feasibility, 9.x).

Per cell, wave context needs the mean significant wave height (``WVHT``, m) and
mean dominant wave period (``DPD``, s) at the nearest NDBC buoy:

* Station list: ``activestations.xml``. Only ``type="buoy"`` stations with
  ``met="y"`` are candidates (fixed C-MAN stations rarely report waves).
* Nearest by great-circle (haversine) distance; farther than
  ``max_buoy_distance_km`` is a ``DataGap(reason="no coverage")``. No farther
  buoy is substituted.
* ``realtime2/{STN}.txt`` (45-day standard meteorological record): whitespace
  table with two ``#`` header lines. ``MM`` values are dropped; WVHT and DPD are
  averaged independently over their non-missing values. A record with no WVHT
  or no DPD values raises :class:`EmptyResponse` -> "empty response" gap.

Network goes through :class:`~siting.fetch.client.FetchClient` (cache + retry).
Parsers are pure. :class:`NdbcWaves` memoizes per station, so Feasibility (task
8.1) can call :meth:`NdbcWaves.wave_at` again with no extra fetch and reuse
``WaveRecord.hs_mean_m`` and ``WaveRecord.station.id``.

Source: NOAA National Data Buoy Center, https://www.ndbc.noaa.gov/
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import Citation
from siting.sources.coops import haversine_km
from siting.sources.endpoints import NDBC_ACTIVE, NDBC_REALTIME, Endpoint

SOURCE = NDBC_REALTIME.source  # "NDBC"
FACTOR = "wave_power"
MISSING = "MM"

# Citation ids referenced by Factor.citation_id (match output/sample/).
REALTIME_CITATION_ID = "ndbc-realtime2"
ACTIVE_CITATION_ID = "ndbc-active"


# ----------------------------------------------------------------------- models


@dataclass(frozen=True)
class Buoy:
    id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class WaveStats:
    """Means over the realtime2 record and the record's first/last dates."""

    hs_mean_m: float
    dpd_mean_s: float
    n_hs: int
    n_dpd: int
    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD


@dataclass(frozen=True)
class WaveRecord:
    """Wave stats at the nearest buoy within range, with its citation."""

    station: Buoy
    distance_km: float
    stats: WaveStats
    citation: Citation

    @property
    def hs_mean_m(self) -> float:
        return self.stats.hs_mean_m

    @property
    def dpd_mean_s(self) -> float:
        return self.stats.dpd_mean_s


# ---------------------------------------------------------------- geometry


def nearest_buoy(
    lat: float, lon: float, buoys: list[Buoy]
) -> tuple[Buoy, float] | None:
    """The buoy minimizing haversine distance (ties -> lowest id), or None."""
    best: tuple[float, str, Buoy] | None = None
    for b in buoys:
        d = haversine_km(lat, lon, b.lat, b.lon)
        if best is None or (d, b.id) < best[:2]:
            best = (d, b.id, b)
    return None if best is None else (best[2], best[0])


# ---------------------------------------------------------------- parsers


def parse_active_stations(payload: bytes) -> list[Buoy]:
    """``activestations.xml`` -> met-reporting buoys with finite lat/lon."""
    root = ET.fromstring(payload)
    out: list[Buoy] = []
    for el in root.iter("station"):
        a = el.attrib
        if a.get("type", "").lower() != "buoy" or a.get("met", "").lower() != "y":
            continue
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)) or not a.get("id"):
            continue
        out.append(Buoy(a["id"].upper(), a.get("name", ""), lat, lon))
    if not out:
        raise EmptyResponse("no active met buoys")
    return out


def _value(tok: str) -> float | None:
    if tok == MISSING:
        return None
    try:
        v = float(tok)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_realtime2(payload: bytes) -> WaveStats:
    """``realtime2/{STN}.txt`` -> mean WVHT (m), mean DPD (s), record dates.

    Columns are located from the first ``#`` header line. Rows are dated from
    ``#YY MM DD``. ``MM`` (missing) is dropped per column. The record start/end
    are the first/last dates with any wave value. No WVHT or no DPD values
    raises :class:`EmptyResponse`.
    """
    lines = payload.decode("utf-8", errors="replace").splitlines()
    header = next((ln for ln in lines if ln.startswith("#")), None)
    if header is None:
        raise ValueError("realtime2: no header line")
    cols = header.lstrip("#").split()
    try:
        i_yy, i_mm, i_dd = cols.index("YY"), cols.index("MM"), cols.index("DD")
        i_h, i_t = cols.index("WVHT"), cols.index("DPD")
    except ValueError as exc:
        raise ValueError(f"realtime2: missing column ({exc})") from exc

    hs: list[float] = []
    tp: list[float] = []
    dates: list[str] = []
    for ln in lines:
        if not ln.strip() or ln.startswith("#"):
            continue
        tok = ln.split()
        if len(tok) <= max(i_h, i_t, i_dd):
            continue
        h, t = _value(tok[i_h]), _value(tok[i_t])
        if h is None and t is None:
            continue
        try:
            d = f"{int(tok[i_yy]):04d}-{int(tok[i_mm]):02d}-{int(tok[i_dd]):02d}"
        except ValueError:
            continue
        if h is not None:
            hs.append(h)
        if t is not None:
            tp.append(t)
        dates.append(d)
    if not hs or not tp:
        raise EmptyResponse("no WVHT/DPD values (all MM)")
    return WaveStats(
        hs_mean_m=sum(hs) / len(hs),
        dpd_mean_s=sum(tp) / len(tp),
        n_hs=len(hs),
        n_dpd=len(tp),
        start=min(dates),
        end=max(dates),
    )


# ---------------------------------------------------------------- citations


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _citation(cid: str, ep: Endpoint, fetch_date: str | None) -> Citation:
    return Citation(
        id=cid,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=ep.url,
        date=fetch_date or _today(),
        date_kind="fetch",
    )


def _station_url(station_id: str) -> str:
    return NDBC_REALTIME.url.format(station=station_id)


# ---------------------------------------------------------------- fetching


class NdbcWaves:
    """Nearest-buoy wave lookups with per-station memoization."""

    def __init__(self, client: FetchClient, max_buoy_distance_km: float):
        self.client = client
        self.max_km = max_buoy_distance_km
        self._buoys: list[Buoy] | DataGap | None = None
        self._per_station: dict[str, WaveStats | DataGap] = {}

    def realtime_citation(self, station_id: str | None = None) -> Citation:
        date = None
        if station_id is not None:
            date = self.client.fetch_date(SOURCE, _station_url(station_id), None)
        return _citation(REALTIME_CITATION_ID, NDBC_REALTIME, date)

    def buoys(self) -> list[Buoy] | DataGap:
        if self._buoys is None:
            ep = NDBC_ACTIVE
            self._buoys = self.client.get(
                ep.source, ep.url, None, parse_active_stations, factor=FACTOR
            )
        return self._buoys

    def _stats(self, b: Buoy) -> WaveStats | DataGap:
        if b.id not in self._per_station:
            res = self.client.get(
                SOURCE, _station_url(b.id), None, parse_realtime2, factor=FACTOR
            )
            if isinstance(res, DataGap):
                detail = f"buoy {b.id} realtime2"
                if res.detail:
                    detail = f"{detail}: {res.detail}"
                res = DataGap(res.factor, res.source, res.reason, detail)
            self._per_station[b.id] = res
        return self._per_station[b.id]

    def wave_at(self, lat: float, lon: float) -> WaveRecord | DataGap:
        """Wave stats at the nearest buoy within range, or a ``wave_power`` gap."""
        buoys = self.buoys()
        if isinstance(buoys, DataGap):
            return buoys
        hit = nearest_buoy(lat, lon, buoys)
        if hit is None:  # pragma: no cover - parse rejects empty lists
            return DataGap(FACTOR, SOURCE, "empty response", "no active buoys")
        b, dist = hit
        if dist > self.max_km:
            return DataGap(
                FACTOR,
                SOURCE,
                "no coverage",
                f"nearest buoy {b.id} {dist:.1f} km > {self.max_km:g} km",
            )
        stats = self._stats(b)
        if isinstance(stats, DataGap):
            return stats
        return WaveRecord(b, dist, stats, self.realtime_citation(b.id))
