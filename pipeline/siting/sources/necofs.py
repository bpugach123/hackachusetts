"""NECOFS FVCOM velocity: MassBay forecast, then the Gulf of Maine grid (Req 4.1, 3.3).

Source: UMass Dartmouth SMAST, Northeast Coastal Ocean Forecast System (NECOFS),
FVCOM unstructured-grid forecasts on THREDDS OPeNDAP (URLs in ``endpoints.py``).
The design's GOM3 fallback is retired (404); the live Gulf of Maine grid in the
same catalog is GOM7, which ``endpoints.NECOFS_MASSBAY.fallback`` points to.

Per point:

* velocity = depth-averaged ``ua``, ``va`` (m/s) at the element whose centroid
  (``lonc``, ``latc``) is nearest the cell centroid, speed = sqrt(ua² + va²);
* depth = ``h`` (m, positive down) at the nearest node (``lon``, ``lat``);
* no element within :data:`MAX_KM` -> ``"no coverage"`` on that grid, and the
  point is retried on the fallback grid.

Two OPeNDAP reads per grid, both through the fetch layer (cache + retry):

1. ``mesh``: ``lonc, latc, lon, lat, h`` (static; cached without a date).
2. ``series``: ``ua, va`` for only the needed elements over the whole forecast
   window (~6 days hourly). Keyed by the UTC date so each day's forecast run is
   fetched once.

Model run date: the first ``ctime`` stamp in the served file's ``history`` attr
(e.g. ``"Sat Oct 24 16:14:50 2026: ncrcat ..."`` -> ``2026-10-24``). With no
stamp the grid returns an ``"empty response"`` gap rather than a guessed date.

Known limitation (surfaced by the caller in metadata): the forecast window is a
few days, so a run during neaps understates the peak.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import MODELED_CURRENT, Citation
from siting.sources.endpoints import NECOFS_MASSBAY
from siting.sources.velocity import (
    FACTOR,
    NECOFS_FVCOM,
    Point,
    VelocitySeries,
    broadcast,
    cacheable,
    finite_or_none,
    gap,
    iso_utc,
    median_step_s,
    nearest_index,
    run_date_from_history,
    speeds_from_uv,
    today_utc,
)

SOURCE = NECOFS_FVCOM
# Nearest element / node must lie within this distance of the centroid, or the
# point is outside the mesh. MassBay elements are tens of m nearshore and ~1-2 km
# offshore; GOM7 is coarser offshore.
MAX_KM = 3.0
MESH_VARS: tuple[str, ...] = ("lonc", "latc", "lon", "lat", "h")
SERIES_VARS: tuple[str, ...] = ("ua", "va")
# Merge needed element indices into contiguous OPeNDAP slices when they are at
# most this far apart (fewer requests, small over-read).
RUN_GAP = 256

GRIDS: tuple[tuple[str, str, str], ...] = (
    # (grid name, dataset name, url)
    ("MassBay", NECOFS_MASSBAY.dataset, NECOFS_MASSBAY.url),
    ("GOM7", "NECOFS FVCOM GOM7 forecast", NECOFS_MASSBAY.fallback or ""),
)


# ------------------------------------------------------------------ pure parse


def select_points(
    points: Sequence[Point],
    lonc: Any,
    latc: Any,
    lon: Any,
    lat: Any,
    h: Any,
    *,
    max_km: float = MAX_KM,
) -> list[tuple[int, float, float | None] | None]:
    """Nearest element (index, distance km) and nearest-node depth per point.

    Returns ``None`` for a point with no element within ``max_km``. Depth is
    ``None`` when no node with a finite ``h`` lies within ``max_km``.
    """
    h_arr = np.asarray(h, dtype=float)
    out: list[tuple[int, float, float | None] | None] = []
    for plat, plon in points:
        el = nearest_index(plat, plon, latc, lonc, max_km=max_km)
        if el is None:
            out.append(None)
            continue
        nd = nearest_index(
            plat, plon, lat, lon, max_km=max_km, valid=np.isfinite(h_arr)
        )
        depth = None if nd is None else finite_or_none(h_arr.ravel()[nd[0]])
        out.append((el[0], el[1], depth))
    return out


def index_runs(indices: Sequence[int], max_gap: int = RUN_GAP) -> list[tuple[int, int]]:
    """Sorted unique indices -> inclusive ``(start, stop)`` runs for slicing."""
    idx = sorted(set(int(i) for i in indices))
    if not idx:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = idx[0]
    for i in idx[1:]:
        if i - prev > max_gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def build_series(
    points: Sequence[Point],
    selection: Sequence[tuple[int, float, float | None] | None],
    elem_index: Any,
    ua: Any,
    va: Any,
    times: Sequence[Any],
    lonc: Any,
    latc: Any,
    citation: Citation,
    *,
    grid: str,
    max_km: float = MAX_KM,
) -> list[VelocitySeries | DataGap]:
    """Per-point series from ``ua``/``va`` shaped ``(time, k)``.

    ``elem_index[k]`` is the original mesh element index of column ``k``. A
    point whose element series has any non-finite value is a ``"no coverage"``
    gap (no substitution from another element).
    """
    ua = np.asarray(ua, dtype=float)
    va = np.asarray(va, dtype=float)
    col = {int(e): k for k, e in enumerate(np.asarray(elem_index).ravel())}
    lonc_a = np.asarray(lonc, dtype=float).ravel()
    latc_a = np.asarray(latc, dtype=float).ravel()
    if len(times) == 0:
        return [gap(SOURCE, "empty response", f"{grid}: no time steps")] * len(points)
    t_start, t_end = iso_utc(times[0]), iso_utc(times[-1])
    dt_s = median_step_s(times)

    out: list[VelocitySeries | DataGap] = []
    for sel in selection:
        if sel is None:
            out.append(
                gap(SOURCE, "no coverage", f"{grid}: no element within {max_km} km")
            )
            continue
        e, dist, depth = sel
        k = col.get(int(e))
        if k is None:
            out.append(gap(SOURCE, "no coverage", f"{grid}: element {e} not fetched"))
            continue
        speeds = speeds_from_uv(ua[:, k], va[:, k])
        if speeds.size == 0 or not np.isfinite(speeds).all():
            out.append(gap(SOURCE, "no coverage", f"{grid}: element {e} has NaN"))
            continue
        out.append(
            VelocitySeries(
                source=SOURCE,
                t_start=t_start,
                t_end=t_end,
                speeds_ms=tuple(float(s) for s in speeds),
                model_run_date=citation.date,
                citation=citation,
                label=MODELED_CURRENT,
                dt_s=dt_s,
                point_lat=float(latc_a[e]),
                point_lon=float(lonc_a[e]),
                distance_km=round(dist, 3),
                depth_m=depth,
                note=f"NECOFS {grid}, element {e}",
            )
        )
    return out


# ------------------------------------------------------------------ fetch


def _select_mesh(ds: Any) -> Any:
    sub = ds[list(MESH_VARS)].reset_coords()[list(MESH_VARS)]
    sub = cacheable(sub)
    return sub


def _select_series(runs: Sequence[tuple[int, int]], needed: Sequence[int]):
    def select(ds: Any) -> Any:
        import xarray as xr

        attrs = dict(ds.attrs)
        parts = [
            ds[list(SERIES_VARS)].reset_coords(drop=True).isel(nele=slice(a, b + 1))
            for a, b in runs
        ]
        sub = xr.concat(parts, dim="nele") if len(parts) > 1 else parts[0]
        fetched = np.concatenate([np.arange(a, b + 1) for a, b in runs])
        pos = np.searchsorted(fetched, np.asarray(needed))
        sub = sub.isel(nele=pos).assign_coords(elem_index=("nele", np.asarray(needed)))
        sub.attrs = attrs
        sub = cacheable(sub)
        return sub

    return select


def _fetch_grid(
    client: FetchClient,
    points: Sequence[Point],
    grid: str,
    dataset: str,
    url: str,
    *,
    max_km: float,
    today: str,
) -> list[VelocitySeries | DataGap]:
    n = len(points)
    if not url:
        return [gap(SOURCE, "fetch failure", f"{grid}: no endpoint")] * n

    mesh = client.get_opendap(
        SOURCE,
        url,
        {"step": "mesh", "vars": list(MESH_VARS)},
        _select_mesh,
        factor=FACTOR,
    )
    if isinstance(mesh, DataGap):
        return broadcast(_tag(mesh, grid), n, SOURCE)

    selection = select_points(
        points,
        mesh["lonc"].values,
        mesh["latc"].values,
        mesh["lon"].values,
        mesh["lat"].values,
        mesh["h"].values,
        max_km=max_km,
    )
    needed = sorted({s[0] for s in selection if s is not None})
    if not needed:
        return [
            gap(SOURCE, "no coverage", f"{grid}: no element within {max_km} km")
        ] * n

    runs = index_runs(needed)
    data = client.get_opendap(
        SOURCE,
        url,
        {
            "step": "series",
            "vars": list(SERIES_VARS),
            "elements": needed,
            "date": today,
        },
        _select_series(runs, needed),
        factor=FACTOR,
    )
    if isinstance(data, DataGap):
        return broadcast(_tag(data, grid), n, SOURCE)

    times = list(data["time"].values)
    run_date = run_date_from_history(data.attrs.get("history"))
    if run_date is None:
        # No run stamp: a model velocity cannot be cited without its run date
        # (Req 3.3), and guessing one would fabricate provenance -> gap.
        return [
            gap(SOURCE, "empty response", f"{grid}: no model run date in history")
        ] * n

    citation = Citation(
        id=f"{SOURCE}_{grid.upper()}",
        agency=NECOFS_MASSBAY.agency,
        dataset=dataset,
        endpoint=url,
        date=run_date,
        date_kind="model_run",
        note=MODELED_CURRENT,
    )
    return build_series(
        points,
        selection,
        data["elem_index"].values,
        data["ua"].transpose("time", "nele").values,
        data["va"].transpose("time", "nele").values,
        times,
        mesh["lonc"].values,
        mesh["latc"].values,
        citation,
        grid=grid,
        max_km=max_km,
    )


def _tag(g: DataGap, grid: str) -> DataGap:
    detail = f"{grid}: {g.detail}" if g.detail else grid
    return DataGap(g.factor, g.source, g.reason, detail)


def fetch(
    client: FetchClient,
    points: Sequence[Point],
    *,
    max_km: float = MAX_KM,
    today: str | None = None,
    grids: Sequence[tuple[str, str, str]] = GRIDS,
) -> list[VelocitySeries | DataGap]:
    """NECOFS series per point: MassBay first, GOM7 for points MassBay misses."""
    n = len(points)
    if n == 0:
        return []
    day = today or today_utc()
    results: list[VelocitySeries | DataGap | None] = [None] * n
    misses: list[list[DataGap]] = [[] for _ in range(n)]
    pending = list(range(n))
    for grid, dataset, url in grids:
        if not pending:
            break
        sub = [points[i] for i in pending]
        out = _fetch_grid(client, sub, grid, dataset, url, max_km=max_km, today=day)
        still = []
        for i, r in zip(pending, out, strict=True):
            if isinstance(r, VelocitySeries):
                results[i] = r
            else:
                misses[i].append(r)
                still.append(i)
        pending = still
    for i in pending:
        last = misses[i][-1]
        detail = "; ".join(f"{g.reason} ({g.detail})" for g in misses[i])
        results[i] = DataGap(FACTOR, SOURCE, last.reason, detail)
    return results  # type: ignore[return-value]
