"""Shared velocity-source types, nearest-point helpers, and the priority resolver.

Req 4.1, 4.2, 3.3. Every velocity source module (``necofs``, ``doppio``, ``mom6``,
``coops_currents``) exposes

    fetch(client, points, **opts) -> list[VelocitySeries | DataGap]

with one entry per input point (``(lat, lon)`` in WGS84). A whole-source failure
(fetch failure / empty response) becomes the same gap for every point; a point
the source does not reach becomes a per-point ``"no coverage"`` gap. Values are
never interpolated from neighbors or defaulted (Req 2.6, 2.7).

:func:`resolve` walks ``DeviceConfig.velocity_source_priority``. Each source is
asked only for the points still unresolved, so lower-priority sources are not
fetched when a higher one already covers every cell. If no source covers a
point, the point gets one ``velocity_peak`` gap whose ``detail`` lists every
source tried and why it failed (Req 4.2).

Speed is always ``sqrt(u**2 + v**2)`` in m/s (depth-averaged where the source
provides it; see each module's notes).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from siting.fetch.gaps import DataGap
from siting.provenance import MODELED_CURRENT, Citation

Point = tuple[float, float]  # (lat, lon), WGS84 decimal degrees

FACTOR = "velocity_peak"
EARTH_RADIUS_KM = 6371.0088

# Source ids, matching endpoints.py / provenance.KIT_EQUIVALENT_SOURCES and the
# DeviceConfig.velocity_source_priority default.
NECOFS_FVCOM = "NECOFS_FVCOM"
DOPPIO = "DOPPIO"
MOM6 = "MOM6"
NOAA_CURRENTS = "NOAA_CURRENTS"


@dataclass(frozen=True)
class VelocitySeries:
    """A speed time series at one cell centroid from one source.

    ``t_start`` / ``t_end`` are ISO 8601 UTC (``YYYY-MM-DDTHH:MM:SSZ``).
    ``model_run_date`` is ``YYYY-MM-DD`` for model sources and ``None`` for NOAA
    harmonic predictions (no model run; their citation carries the fetch date).
    ``point_lat`` / ``point_lon`` / ``distance_km`` describe the model point
    (element centroid, rho point, grid point, or station) actually used.
    """

    source: str
    t_start: str
    t_end: str
    speeds_ms: tuple[float, ...]
    model_run_date: str | None
    citation: Citation
    label: str = MODELED_CURRENT
    dt_s: float | None = None
    point_lat: float | None = None
    point_lon: float | None = None
    distance_km: float | None = None
    depth_m: float | None = None  # NECOFS h at the nearest node; else None
    note: str | None = None

    @property
    def peak_ms(self) -> float:
        return max(self.speeds_ms)

    def as_tuple(self) -> tuple[str, str, str, tuple[float, ...], str | None]:
        """``(source, t_start, t_end, speeds_ms, model_run_date)`` per tasks.md 6.1."""
        return (
            self.source,
            self.t_start,
            self.t_end,
            self.speeds_ms,
            self.model_run_date,
        )


# ----------------------------------------------------------------- geometry


def haversine_km(lat1: Any, lon1: Any, lat2: Any, lon2: Any) -> np.ndarray:
    """Great-circle distance in km; broadcasts over numpy arrays."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def wrap_lon(lon: Any) -> np.ndarray:
    """Longitudes to [-180, 180) (MOM6 regrid uses 0-360)."""
    return (np.asarray(lon, dtype=float) + 180.0) % 360.0 - 180.0


def nearest_index(
    lat: float,
    lon: float,
    lats: Any,
    lons: Any,
    *,
    max_km: float,
    valid: Any = None,
) -> tuple[int, float] | None:
    """Flat index and distance of the nearest valid point within ``max_km``.

    ``lats`` / ``lons`` may be any shape (flattened). ``valid`` is an optional
    boolean mask of the same shape; invalid points (land, NaN series) are never
    chosen. Returns ``None`` when no valid point lies within ``max_km``.
    """
    la = np.asarray(lats, dtype=float).ravel()
    lo = wrap_lon(lons).ravel()
    d = haversine_km(lat, lon, la, lo)
    ok = np.isfinite(d)
    if valid is not None:
        ok &= np.asarray(valid, dtype=bool).ravel()
    if not ok.any():
        return None
    d = np.where(ok, d, np.inf)
    idx = int(np.argmin(d))
    dist = float(d[idx])
    if dist > max_km:
        return None
    return idx, dist


def speeds_from_uv(u: Any, v: Any) -> np.ndarray:
    """``sqrt(u**2 + v**2)`` elementwise (m/s in, m/s out)."""
    return np.hypot(np.asarray(u, dtype=float), np.asarray(v, dtype=float))


def all_finite_along(values: np.ndarray, axis: int = 0) -> np.ndarray:
    """Mask of points whose series has no NaN/inf along ``axis`` (time)."""
    return np.isfinite(values).all(axis=axis)


def bbox_of(points: Sequence[Point], margin_deg: float = 0.0) -> tuple[float, ...]:
    """``(lat_min, lat_max, lon_min, lon_max)`` around ``points``."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (
        min(lats) - margin_deg,
        max(lats) + margin_deg,
        min(lons) - margin_deg,
        max(lons) + margin_deg,
    )


def cacheable(ds: Any) -> Any:
    """Make an OPeNDAP subset writable as netCDF for the cache.

    Drops inherited encodings (e.g. an unlimited ``time`` dim not in the subset)
    and reserved ``_``-prefixed global attrs like ``_NCProperties``, which
    netCDF4 refuses to write.
    """
    ds.encoding = {}
    ds.attrs = {k: v for k, v in ds.attrs.items() if not str(k).startswith("_")}
    for var in ds.variables.values():
        var.attrs = {k: v for k, v in var.attrs.items() if not str(k).startswith("_")}
        var.encoding = {k: v for k, v in var.encoding.items() if k in _KEEP_ENCODING}
    return ds


# Per-variable encodings that still describe a subset correctly; chunking and
# compression settings from the remote file may not fit the subset shape.
_KEEP_ENCODING = frozenset({"dtype", "_FillValue", "units", "calendar"})


# --------------------------------------------------------------------- time


def iso_utc(t: Any) -> str:
    """numpy datetime64 / datetime -> ``YYYY-MM-DDTHH:MM:SSZ``."""
    if isinstance(t, datetime):
        t = t.astimezone(UTC).replace(tzinfo=None) if t.tzinfo else t
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(np.datetime_as_string(np.datetime64(t, "s"), unit="s")) + "Z"


def median_step_s(times: Sequence[Any]) -> float | None:
    """Median time step in seconds, or None for fewer than two steps."""
    if len(times) < 2:
        return None
    t = np.asarray(times, dtype="datetime64[s]").astype("int64")
    return float(np.median(np.diff(t)))


_HISTORY_CTIME = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +(\d{1,2}) "
    r"\d{2}:\d{2}:\d{2} (\d{4})"
)
_RELEASE = re.compile(r"r(\d{4})(\d{2})(\d{2})")


def run_date_from_history(history: str | None) -> str | None:
    """First ``ctime`` stamp in a netCDF ``history`` attr -> ``YYYY-MM-DD``.

    THREDDS/NCO prepend the newest operation, so the first stamp is the date the
    served file was produced (the model run). Returns None when absent.
    """
    if not history:
        return None
    m = _HISTORY_CTIME.search(history)
    if not m:
        return None
    dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
    return dt.strftime("%Y-%m-%d")


def run_date_from_release(tag: str | None) -> str | None:
    """CEFI release tag ``r20230520`` -> ``2023-05-20``."""
    if not tag:
        return None
    m = _RELEASE.search(tag)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ----------------------------------------------------------------- results


def gap(source: str, reason: str, detail: str | None = None) -> DataGap:
    return DataGap(FACTOR, source, reason, detail)  # type: ignore[arg-type]


def broadcast(result: DataGap, n: int, source: str) -> list[DataGap]:
    """A run-wide fetch gap, relabeled for velocity, once per point."""
    g = DataGap(FACTOR, source, result.reason, result.detail)
    return [g] * n


# ----------------------------------------------------------------- resolver

Provider = Callable[[Sequence[Point]], "list[VelocitySeries | DataGap]"]


def _describe(g: DataGap) -> str:
    return f"{g.source}: {g.reason}" + (f" ({g.detail})" if g.detail else "")


def resolve(
    points: Sequence[Point],
    providers: Mapping[str, Provider],
    priority: Sequence[str],
) -> list[VelocitySeries | DataGap]:
    """First source in ``priority`` that returns a series for each point.

    ``providers`` maps source id -> callable taking the still-unresolved points.
    A source missing from ``providers`` or raising is recorded as a gap for those
    points and the next source is tried. Points no source covers get one
    ``velocity_peak`` gap: ``source`` is the last source tried, ``reason`` is its
    reason, and ``detail`` lists every attempt in priority order.
    """
    n = len(points)
    results: list[VelocitySeries | DataGap | None] = [None] * n
    attempts: list[list[DataGap]] = [[] for _ in range(n)]
    pending = list(range(n))

    for source in priority:
        if not pending:
            break
        provider = providers.get(source)
        if provider is None:
            out: list[VelocitySeries | DataGap] = [
                gap(source, "fetch failure", "no provider configured")
            ] * len(pending)
        else:
            sub = [points[i] for i in pending]
            try:
                out = list(provider(sub))
                if len(out) != len(sub):
                    raise ValueError(
                        f"provider returned {len(out)} results for {len(sub)} points"
                    )
            except Exception as exc:  # a source bug must not abort the run (Req 2.6)
                out = [gap(source, "fetch failure", f"{type(exc).__name__}: {exc}")]
                out *= len(sub)
        still: list[int] = []
        for i, r in zip(pending, out, strict=True):
            if isinstance(r, VelocitySeries):
                results[i] = r
            else:
                attempts[i].append(r)
                still.append(i)
        pending = still

    for i in pending:
        tried = attempts[i]
        if tried:
            last = tried[-1]
            results[i] = DataGap(
                FACTOR, last.source, last.reason, "; ".join(map(_describe, tried))
            )
        else:  # empty priority list
            results[i] = gap("velocity", "no coverage", "no velocity source configured")
    return results  # type: ignore[return-value]


def default_providers(client: Any) -> dict[str, Provider]:
    """Live providers bound to ``client`` for every known velocity source."""
    from siting.sources import coops_currents, doppio, mom6, necofs

    return {
        NECOFS_FVCOM: lambda pts: necofs.fetch(client, pts),
        DOPPIO: lambda pts: doppio.fetch(client, pts),
        MOM6: lambda pts: mom6.fetch(client, pts),
        NOAA_CURRENTS: lambda pts: coops_currents.fetch(client, pts),
    }


def velocity_series(
    points: Sequence[Point],
    priority: Sequence[str],
    client: Any = None,
    *,
    providers: Mapping[str, Provider] | None = None,
) -> list[VelocitySeries | DataGap]:
    """Resolve velocity for ``points`` using live providers (or injected ones)."""
    if providers is None:
        if client is None:
            raise ValueError("velocity_series needs a FetchClient or providers")
        providers = default_providers(client)
    return resolve(points, providers, priority)


def finite_or_none(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
