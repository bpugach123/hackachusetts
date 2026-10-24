"""NOAA CO-OPS tides: harcon station list, harcon, and datums (Req 5.1, 5.2).

Per cell, the rise/fall path needs the mean tidal range at the CO-OPS station
nearest the Centroid (great-circle / haversine distance):

* Station list: ``mdapi .../stations.json?type=harcon`` (stations that publish
  harmonic constituents).
* ``stations/{id}/harcon.json``: fetched as the supporting record (M2 amplitude),
  per the steering's "pull product=harcon" rule. An empty constituent list is a
  Data_Gap (Req 5.2).
* ``stations/{id}/datums.json``: mean range MN (= MHW - MLW) in meters.

If the nearest station is farther than ``max_station_distance_km`` the result is
a ``DataGap(reason="no coverage")``; no farther station is substituted and no
value is interpolated (Req 5.2, steering "never fabricate").

Network goes through :class:`~siting.fetch.client.FetchClient` (cache + retry).
Parse functions are pure so tests can feed fixtures. Per-station results are
memoized on :class:`CoopsTides` because many cells share a station.

Source: NOAA CO-OPS Metadata API (mdapi),
https://api.tidesandcurrents.noaa.gov/mdapi/prod/
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import Citation
from siting.sources.endpoints import (
    COOPS_DATUMS,
    COOPS_HARCON,
    COOPS_HARCON_STATIONS,
    Endpoint,
)

SOURCE = COOPS_HARCON_STATIONS.source  # "NOAA_COOPS"
FACTOR = "tidal_range"
EARTH_RADIUS_KM = 6371.0088  # IUGG mean Earth radius
FEET_TO_M = 0.3048
METRIC_PARAMS: dict = {"units": "metric"}

# Citation ids referenced by Factor.citation_id (match output/sample/).
HARCON_CITATION_ID = "coops-harcon"
DATUMS_CITATION_ID = "coops-datums"


# ----------------------------------------------------------------------- models


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Harcon:
    """Supporting harcon record: constituent count and M2 amplitude (m)."""

    n_constituents: int
    m2_amplitude_m: float | None


@dataclass(frozen=True)
class TidalRange:
    """Mean tidal range at the nearest station, with its supporting record."""

    station: Station
    distance_km: float
    range_m: float  # MN from datums.json
    harcon: Harcon
    harcon_citation: Citation
    datums_citation: Citation


# ---------------------------------------------------------------- geometry


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def nearest_station(
    lat: float, lon: float, stations: list[Station]
) -> tuple[Station, float] | None:
    """The station minimizing haversine distance (ties -> lowest id), or None."""
    best: tuple[float, str, Station] | None = None
    for st in stations:
        d = haversine_km(lat, lon, st.lat, st.lon)
        key = (d, st.id, st)
        if best is None or key[:2] < best[:2]:
            best = key
    return None if best is None else (best[2], best[0])


# ---------------------------------------------------------------- parsers


def _to_m(value: float, units: str | None) -> float:
    return value * FEET_TO_M if (units or "").lower().startswith("f") else value


def parse_stations(payload: bytes) -> list[Station]:
    """``stations.json?type=harcon`` -> stations with finite lat/lon."""
    data = json.loads(payload)
    out: list[Station] = []
    for s in data.get("stations") or []:
        try:
            lat, lon = float(s["lat"]), float(s["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        out.append(Station(str(s["id"]), str(s.get("name") or ""), lat, lon))
    if not out:
        raise EmptyResponse("no harcon stations")
    return out


def parse_harcon(payload: bytes) -> Harcon:
    """``harcon.json`` -> constituent count and M2 amplitude in meters.

    An empty ``HarmonicConstituents`` list raises :class:`EmptyResponse`
    (station returns no harcon data, Req 5.2).
    """
    data = json.loads(payload)
    rows = data.get("HarmonicConstituents") or []
    if not rows:
        raise EmptyResponse("no harmonic constituents")
    units = data.get("units")
    m2: float | None = None
    for row in rows:
        if str(row.get("name", "")).upper() == "M2":
            try:
                amp = float(row["amplitude"])
            except (KeyError, TypeError, ValueError):
                break
            if math.isfinite(amp):
                m2 = _to_m(amp, units)
            break
    return Harcon(n_constituents=len(rows), m2_amplitude_m=m2)


def parse_datums(payload: bytes) -> float:
    """``datums.json`` -> mean range MN in meters.

    Uses MN when published, else MHW - MLW. Neither available, or a
    non-positive range, raises :class:`EmptyResponse`.
    """
    data = json.loads(payload)
    units = data.get("units")
    vals: dict[str, float] = {}
    for row in data.get("datums") or []:
        try:
            v = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(v):
            vals[str(row.get("name", "")).upper()] = v
    if "MN" in vals:
        mn = vals["MN"]
    elif "MHW" in vals and "MLW" in vals:
        mn = vals["MHW"] - vals["MLW"]
    else:
        raise EmptyResponse("no MN / MHW-MLW datum")
    if mn <= 0:
        raise EmptyResponse(f"non-positive mean range {mn}")
    return _to_m(mn, units)


# ---------------------------------------------------------------- citations


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _citation(
    cid: str, ep: Endpoint, fetch_date: str | None, note: str | None = None
) -> Citation:
    return Citation(
        id=cid,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=ep.url,
        date=fetch_date or _today(),
        date_kind="fetch",
        note=note,
    )


# ---------------------------------------------------------------- fetching


def _station_url(ep: Endpoint, station_id: str) -> str:
    return ep.url.format(station=station_id)


class CoopsTides:
    """Nearest-station tidal range lookups with per-station memoization."""

    def __init__(self, client: FetchClient, max_station_distance_km: float):
        self.client = client
        self.max_km = max_station_distance_km
        self._stations: list[Station] | DataGap | None = None
        self._per_station: dict[str, tuple[Harcon, float] | DataGap] = {}

    # Citations are stable per run; the date comes from the cached fetch meta.
    def harcon_citation(self, station_id: str | None = None) -> Citation:
        date = None
        if station_id is not None:
            url = _station_url(COOPS_HARCON, station_id)
            date = self.client.fetch_date(SOURCE, url, METRIC_PARAMS)
        return _citation(HARCON_CITATION_ID, COOPS_HARCON, date)

    def datums_citation(self, station_id: str | None = None) -> Citation:
        date = None
        if station_id is not None:
            url = _station_url(COOPS_DATUMS, station_id)
            date = self.client.fetch_date(SOURCE, url, METRIC_PARAMS)
        return _citation(DATUMS_CITATION_ID, COOPS_DATUMS, date)

    def stations(self) -> list[Station] | DataGap:
        if self._stations is None:
            ep = COOPS_HARCON_STATIONS
            self._stations = self.client.get(
                ep.source, ep.url, None, parse_stations, factor=FACTOR
            )
        return self._stations

    def _station_records(self, st: Station) -> tuple[Harcon, float] | DataGap:
        if st.id not in self._per_station:
            self._per_station[st.id] = self._fetch_station(st)
        return self._per_station[st.id]

    def _fetch_station(self, st: Station) -> tuple[Harcon, float] | DataGap:
        # Harcon first: an empty constituent list is a gap even if datums exist.
        harcon = self.client.get(
            SOURCE,
            _station_url(COOPS_HARCON, st.id),
            METRIC_PARAMS,
            parse_harcon,
            factor=FACTOR,
        )
        if isinstance(harcon, DataGap):
            return _with_station(harcon, st, "harcon")
        mn = self.client.get(
            SOURCE,
            _station_url(COOPS_DATUMS, st.id),
            METRIC_PARAMS,
            parse_datums,
            factor=FACTOR,
        )
        if isinstance(mn, DataGap):
            return _with_station(mn, st, "datums")
        return harcon, mn

    def tidal_range_at(self, lat: float, lon: float) -> TidalRange | DataGap:
        """Mean range at the nearest harcon station, or a ``tidal_range`` gap."""
        stations = self.stations()
        if isinstance(stations, DataGap):
            return stations
        hit = nearest_station(lat, lon, stations)
        if hit is None:  # pragma: no cover - parse_stations rejects empty lists
            return DataGap(FACTOR, SOURCE, "empty response", "no harcon stations")
        st, dist = hit
        if dist > self.max_km:
            return DataGap(
                FACTOR,
                SOURCE,
                "no coverage",
                f"nearest station {st.id} {dist:.1f} km > {self.max_km:g} km",
            )
        rec = self._station_records(st)
        if isinstance(rec, DataGap):
            return rec
        harcon, mn = rec
        return TidalRange(
            station=st,
            distance_km=dist,
            range_m=mn,
            harcon=harcon,
            harcon_citation=self.harcon_citation(st.id),
            datums_citation=self.datums_citation(st.id),
        )


def _with_station(gap: DataGap, st: Station, product: str) -> DataGap:
    detail = f"station {st.id} {product}"
    if gap.detail:
        detail = f"{detail}: {gap.detail}"
    return DataGap(gap.factor, gap.source, gap.reason, detail)
