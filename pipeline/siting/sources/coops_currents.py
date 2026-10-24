"""NOAA CO-OPS current predictions at the nearest station within 5 km (Req 4.1).

Source: NOAA CO-OPS, harmonic tidal current predictions. Station list from the
metadata API (``endpoints.NOAA_CURRENTS_STATIONS``), predictions from
``datagetter`` (``endpoints.NOAA_CURRENTS_DATA``). Last in the default priority.

These are harmonic predictions, not a model: series are labeled
``"predicted current (harmonic)"`` and their citation carries the fetch date
(``date_kind="fetch"``); ``model_run_date`` is ``None`` (provenance exception
in ``provenance.py``).

Choices, all checked against the live API on 2026-10-24:

* Only harmonic (``type == "H"``) stations are used. Subordinate (``S``) and
  weak/variable (``W``) stations return max/slack events only, even when a fixed
  interval is requested, so they cannot give an evenly spaced series.
* ``interval=30`` (30-minute steps) instead of the design's ``MAX_SLACK``: the
  capacity factor is a mean over evenly spaced steps (Req 4.6) and the frontend
  timeseries uses a fixed ``dt_s``. A 15-day window spans one spring-neap cycle.
* ``units=metric`` returns speed in cm/s; converted to m/s. Speed is
  ``|Velocity_Major|`` (or ``hypot(major, minor)`` when a minor axis is given).
* A station has one bin per depth. The bin at the median listed depth is used
  as the mid-water-column value, a stand-in for depth-averaged speed; the bin
  and its depth go in the series ``note``.
* Stations within :data:`MAX_KM` are tried nearest first; a station whose
  prediction request fails or is empty is skipped for the next one.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import PREDICTED_CURRENT, Citation
from siting.sources.endpoints import NOAA_CURRENTS_DATA, NOAA_CURRENTS_STATIONS
from siting.sources.velocity import (
    FACTOR,
    NOAA_CURRENTS,
    Point,
    VelocitySeries,
    broadcast,
    gap,
    haversine_km,
    iso_utc,
    median_step_s,
    today_utc,
)

SOURCE = NOAA_CURRENTS
MAX_KM = 5.0
WINDOW_DAYS = 15
INTERVAL_MIN = "30"
DATASET = "NOAA CO-OPS current predictions (harmonic)"
APPLICATION = "perseus_siting"


@dataclass(frozen=True)
class CurrentStation:
    id: str
    name: str
    lat: float
    lon: float
    bins: tuple[tuple[int, float | None], ...]  # (bin, depth_m), as listed

    def mid_bin(self) -> tuple[int, float | None]:
        """Bin at the median listed depth (lower median); first bin if no depths."""
        with_depth = sorted((d, b) for b, d in self.bins if d is not None)
        if not with_depth:
            return self.bins[0]
        d, b = with_depth[(len(with_depth) - 1) // 2]
        return b, d


@dataclass(frozen=True)
class Prediction:
    times: tuple[np.datetime64, ...]
    speeds_ms: tuple[float, ...]
    depth_m: float | None


# ------------------------------------------------------------------ pure parse


def parse_stations(payload: bytes) -> list[CurrentStation]:
    """Station list JSON -> harmonic stations with their bins (sorted by id)."""
    doc = json.loads(payload)
    rows = doc.get("stations") if isinstance(doc, dict) else None
    if not rows:
        raise EmptyResponse("no stations")
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("type") != "H":
            continue
        try:
            sid, lat, lon = str(r["id"]), float(r["lat"]), float(r["lng"])
            b = int(r["currbin"])
        except (KeyError, TypeError, ValueError):
            continue
        depth = r.get("depth")
        depth = float(depth) if isinstance(depth, (int, float)) else None
        g = grouped.setdefault(
            sid, {"name": str(r.get("name") or sid), "lat": lat, "lon": lon, "bins": {}}
        )
        g["bins"].setdefault(b, depth)
    if not grouped:
        raise EmptyResponse("no harmonic current stations")
    return [
        CurrentStation(
            id=sid,
            name=g["name"],
            lat=g["lat"],
            lon=g["lon"],
            bins=tuple(sorted(g["bins"].items())),
        )
        for sid, g in sorted(grouped.items())
    ]


def stations_within(
    lat: float, lon: float, stations: Sequence[CurrentStation], max_km: float = MAX_KM
) -> list[tuple[CurrentStation, float]]:
    """Stations within ``max_km`` of the point, nearest first (ties by id)."""
    if not stations:
        return []
    d = haversine_km(
        lat,
        lon,
        np.array([s.lat for s in stations]),
        np.array([s.lon for s in stations]),
    )
    hits = [(s, float(x)) for s, x in zip(stations, d, strict=True) if x <= max_km]
    return sorted(hits, key=lambda t: (t[1], t[0].id))


def parse_predictions(payload: bytes) -> Prediction:
    """datagetter JSON (units=metric, cm/s) -> evenly spaced speeds in m/s."""
    doc = json.loads(payload)
    if isinstance(doc, dict) and "error" in doc:
        msg = doc["error"].get("message") if isinstance(doc["error"], dict) else None
        raise EmptyResponse(str(msg or doc["error"]).strip())
    cp = (
        (doc.get("current_predictions") or {}).get("cp")
        if isinstance(doc, dict)
        else None
    )
    if not cp:
        raise EmptyResponse("no current predictions")
    if any("Type" in row for row in cp):
        raise EmptyResponse("event (max/slack) predictions only; no fixed interval")
    times, speeds = [], []
    depth = None
    for row in cp:
        major = float(row["Velocity_Major"])
        minor = float(row.get("Velocity_Minor") or 0.0)
        speeds.append(float(np.hypot(major, minor)) / 100.0)  # cm/s -> m/s
        times.append(
            np.datetime64(datetime.strptime(row["Time"], "%Y-%m-%d %H:%M"), "s")
        )
        if depth is None and row.get("Depth") not in (None, ""):
            try:
                depth = float(row["Depth"])
            except ValueError:
                depth = None
    if not all(np.isfinite(speeds)):
        raise EmptyResponse("non-finite predictions")
    return Prediction(tuple(times), tuple(speeds), depth)


def prediction_params(station: str, bin_: int, begin: str, days: int) -> dict:
    """datagetter params; ``begin`` is ``YYYY-MM-DD`` (UTC)."""
    return {
        "product": "currents_predictions",
        "station": station,
        "bin": str(bin_),
        "begin_date": begin.replace("-", ""),
        "range": str(int(days * 24)),
        "interval": INTERVAL_MIN,
        "units": "metric",
        "time_zone": "gmt",
        "format": "json",
        "application": APPLICATION,
    }


# ------------------------------------------------------------------ fetch


def fetch(
    client: FetchClient,
    points: Sequence[Point],
    *,
    max_km: float = MAX_KM,
    days: int = WINDOW_DAYS,
    begin: str | None = None,
) -> list[VelocitySeries | DataGap]:
    """CO-OPS predicted-current series per point (nearest harmonic station)."""
    n = len(points)
    if n == 0:
        return []
    st_ep, data_ep = NOAA_CURRENTS_STATIONS, NOAA_CURRENTS_DATA
    stations = client.get(SOURCE, st_ep.url, None, parse_stations, factor=FACTOR)
    if isinstance(stations, DataGap):
        return broadcast(stations, n, SOURCE)

    start = begin or today_utc()
    memo: dict[tuple[str, int], tuple[Prediction | DataGap, str]] = {}

    def predict(st: CurrentStation, b: int) -> tuple[Prediction | DataGap, str]:
        if (st.id, b) not in memo:
            params = prediction_params(st.id, b, start, days)
            r = client.get(
                SOURCE, data_ep.url, params, parse_predictions, factor=FACTOR
            )
            fdate = client.fetch_date(SOURCE, data_ep.url, params) or datetime.now(
                UTC
            ).strftime("%Y-%m-%d")
            memo[(st.id, b)] = (r, fdate)
        return memo[(st.id, b)]

    out: list[VelocitySeries | DataGap] = []
    for plat, plon in points:
        near = stations_within(plat, plon, stations, max_km)
        if not near:
            out.append(
                gap(SOURCE, "no coverage", f"no harmonic station within {max_km} km")
            )
            continue
        tried: list[str] = []
        found: VelocitySeries | None = None
        last: DataGap | None = None
        for st, dist in near:
            b, _ = st.mid_bin()
            pred, fdate = predict(st, b)
            if isinstance(pred, DataGap):
                last = pred
                tried.append(f"{st.id} bin {b}: {pred.reason}")
                continue
            citation = Citation(
                id=SOURCE,
                agency=data_ep.agency,
                dataset=DATASET,
                endpoint=data_ep.url,
                date=fdate,
                date_kind="fetch",
                note=PREDICTED_CURRENT,
            )
            depth = f" at {pred.depth_m:g} m" if pred.depth_m is not None else ""
            found = VelocitySeries(
                source=SOURCE,
                t_start=iso_utc(pred.times[0]),
                t_end=iso_utc(pred.times[-1]),
                speeds_ms=pred.speeds_ms,
                model_run_date=None,
                citation=citation,
                label=PREDICTED_CURRENT,
                dt_s=median_step_s(pred.times),
                point_lat=st.lat,
                point_lon=st.lon,
                distance_km=round(dist, 3),
                note=f"station {st.id} ({st.name}), bin {b}{depth}",
            )
            break
        if found is not None:
            out.append(found)
        else:  # every in-range station failed; `near` is non-empty so `last` is set
            reason = last.reason if last is not None else "no coverage"
            out.append(DataGap(FACTOR, SOURCE, reason, "; ".join(tried)))
    return out
