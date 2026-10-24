"""Doppio ROMS velocity at the nearest rho point (Req 4.1, 3.3).

Source: Rutgers / NOAA, Doppio ROMS real-time PSAS forecast system, FMRC
``History_Best`` aggregation on THREDDS OPeNDAP (``endpoints.DOPPIO``). Hourly
depth-averaged ``ubar`` / ``vbar`` (m/s) on the ROMS staggered C-grid, ~7 km.

Per point:

* nearest wet (``mask_rho == 1``) interior rho point within :data:`MAX_KM`;
* ``ubar`` / ``vbar`` averaged from the two adjacent u / v faces onto that rho
  point (standard ROMS u2rho / v2rho), speed = sqrt(u² + v²). Speed is
  rotation-invariant, so the grid ``angle`` is not needed;
* no wet rho point in range, or any NaN in the series -> ``"no coverage"``.

Two OPeNDAP reads through the fetch layer: ``grid`` (``lon_rho, lat_rho,
mask_rho``; static) and ``series`` (``ubar, vbar, time_run`` for the smallest
index box holding every selected rho point, over the last :data:`WINDOW_DAYS`
days of the Best time axis). A 15-day window spans one spring-neap cycle.

Model run date: the latest ``time_run`` in the window (FMRC Best mixes runs;
the series ``note`` records the earliest-latest run range).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import MODELED_CURRENT, Citation
from siting.sources.endpoints import DOPPIO as DOPPIO_EP
from siting.sources.velocity import (
    DOPPIO,
    FACTOR,
    Point,
    VelocitySeries,
    all_finite_along,
    broadcast,
    cacheable,
    gap,
    iso_utc,
    median_step_s,
    nearest_index,
    speeds_from_uv,
    today_utc,
)

SOURCE = DOPPIO
MAX_KM = 10.0  # ~7 km grid: nearest wet rho point within ~1.5 cells
WINDOW_DAYS = 15.0
GRID_VARS: tuple[str, ...] = ("lon_rho", "lat_rho", "mask_rho")
BOX_PAD = 2  # rho cells of margin around the selected points

Box = tuple[int, int, int, int]  # (ja, jb, ia, ib): rho rows [ja, jb), cols [ia, ib)


# ------------------------------------------------------------------ pure parse


def select_rho(
    points: Sequence[Point],
    lon_rho: Any,
    lat_rho: Any,
    mask_rho: Any,
    *,
    max_km: float = MAX_KM,
) -> list[tuple[int, int, float] | None]:
    """Nearest wet interior rho point ``(j, i, distance_km)`` per point, or None.

    Edge rows/cols are excluded because u2rho / v2rho need both neighbors.
    """
    lon = np.asarray(lon_rho, dtype=float)
    lat = np.asarray(lat_rho, dtype=float)
    mask = np.asarray(mask_rho, dtype=float)
    ny, nx = lon.shape
    valid = (mask == 1) & np.isfinite(lon) & np.isfinite(lat)
    valid[0, :] = valid[-1, :] = False
    valid[:, 0] = valid[:, -1] = False
    out: list[tuple[int, int, float] | None] = []
    for plat, plon in points:
        hit = nearest_index(plat, plon, lat, lon, max_km=max_km, valid=valid)
        if hit is None:
            out.append(None)
        else:
            j, i = divmod(hit[0], nx)
            out.append((j, i, hit[1]))
    return out


def box_for(
    selection: Sequence[tuple[int, int, float] | None],
    shape: tuple[int, int],
    pad: int = BOX_PAD,
) -> Box | None:
    """Rho index box ``[ja, jb) x [ia, ib)`` around every selection, padded.

    The pad leaves room to step to a neighbor whose series is finite. The box
    stays inside the interior (rows/cols 1 .. n-2) so u2rho / v2rho apply.
    """
    hits = [s for s in selection if s is not None]
    if not hits:
        return None
    ny, nx = shape
    js = [s[0] for s in hits]
    is_ = [s[1] for s in hits]
    return (
        max(1, min(js) - pad),
        min(ny - 1, max(js) + 1 + pad),
        max(1, min(is_) - pad),
        min(nx - 1, max(is_) + 1 + pad),
    )


def uv_to_rho(ubar: Any, vbar: Any) -> tuple[np.ndarray, np.ndarray]:
    """Average staggered velocities onto rho points.

    For a rho box of ``ny x nx``: ``ubar`` covers rows ``[ja, jb)`` and u-cols
    ``[ia-1, ib)`` -> shape ``(t, ny, nx+1)``; ``vbar`` covers v-rows
    ``[ja-1, jb)`` and cols ``[ia, ib)`` -> ``(t, ny+1, nx)``. Returns two
    ``(t, ny, nx)`` arrays.
    """
    u = np.asarray(ubar, dtype=float)
    v = np.asarray(vbar, dtype=float)
    return 0.5 * (u[:, :, :-1] + u[:, :, 1:]), 0.5 * (v[:, :-1, :] + v[:, 1:, :])


def build_series(
    points: Sequence[Point],
    box: Box,
    u_rho: Any,
    v_rho: Any,
    times: Sequence[Any],
    lon_rho: Any,
    lat_rho: Any,
    mask_rho: Any,
    citation: Citation,
    *,
    note: str | None = None,
    max_km: float = MAX_KM,
) -> list[VelocitySeries | DataGap]:
    """Per-point series from rho-point velocities shaped ``(t, ny, nx)`` on ``box``.

    Each point takes the nearest rho point in ``box`` that is wet and has a
    finite series over the whole window (wet-masked cells can still be NaN in
    the Best aggregation nearshore). None within ``max_km`` -> ``"no coverage"``.
    """
    ja, jb, ia, ib = box
    speed = speeds_from_uv(u_rho, v_rho)
    lon = np.asarray(lon_rho, dtype=float)[ja:jb, ia:ib]
    lat = np.asarray(lat_rho, dtype=float)[ja:jb, ia:ib]
    wet = np.asarray(mask_rho, dtype=float)[ja:jb, ia:ib] == 1
    if len(times) == 0 or speed.size == 0:
        return [gap(SOURCE, "empty response", "no time steps")] * len(points)
    valid = wet & all_finite_along(speed, axis=0)
    t_start, t_end = iso_utc(times[0]), iso_utc(times[-1])
    dt_s = median_step_s(times)
    nx = ib - ia

    out: list[VelocitySeries | DataGap] = []
    for plat, plon in points:
        hit = nearest_index(plat, plon, lat, lon, max_km=max_km, valid=valid)
        if hit is None:
            out.append(
                gap(SOURCE, "no coverage", f"no wet rho point within {max_km} km")
            )
            continue
        jj, ii = divmod(hit[0], nx)
        j, i = ja + jj, ia + ii
        out.append(
            VelocitySeries(
                source=SOURCE,
                t_start=t_start,
                t_end=t_end,
                speeds_ms=tuple(float(x) for x in speed[:, jj, ii]),
                model_run_date=citation.date,
                citation=citation,
                label=MODELED_CURRENT,
                dt_s=dt_s,
                point_lat=float(lat[jj, ii]),
                point_lon=float(lon[jj, ii]),
                distance_km=round(hit[1], 3),
                note="; ".join(x for x in (f"rho point ({j},{i})", note) if x),
            )
        )
    return out


# ------------------------------------------------------------------ fetch


def _select_grid(ds: Any) -> Any:
    # lon_rho / lat_rho are coordinates on the live dataset: keep them as data.
    sub = ds[list(GRID_VARS)].reset_coords()[list(GRID_VARS)]
    sub = cacheable(sub)
    return sub


def _select_series(box: Box, days: float):
    ja, jb, ia, ib = box

    def select(ds: Any) -> Any:
        t = ds["time"].values
        t0 = t[-1] - np.timedelta64(int(days * 86400), "s")
        first = int(np.searchsorted(t, t0, side="right"))
        sub = ds[["ubar", "vbar", "time_run"]].reset_coords()
        drop = [v for v in sub.data_vars if v not in ("ubar", "vbar", "time_run")]
        sub = sub.drop_vars(drop)
        return cacheable(sub).isel(
            time=slice(first, None),
            eta_u=slice(ja, jb),
            xi_u=slice(ia - 1, ib),
            eta_v=slice(ja - 1, jb),
            xi_v=slice(ia, ib),
        )

    return select


def fetch(
    client: FetchClient,
    points: Sequence[Point],
    *,
    max_km: float = MAX_KM,
    days: float = WINDOW_DAYS,
    today: str | None = None,
) -> list[VelocitySeries | DataGap]:
    """Doppio series per point (nearest wet rho point)."""
    n = len(points)
    if n == 0:
        return []
    ep = DOPPIO_EP
    grid = client.get_opendap(
        SOURCE,
        ep.url,
        {"step": "grid", "vars": list(GRID_VARS)},
        _select_grid,
        factor=FACTOR,
    )
    if isinstance(grid, DataGap):
        return broadcast(grid, n, SOURCE)
    lon_rho, lat_rho = grid["lon_rho"].values, grid["lat_rho"].values
    selection = select_rho(
        points, lon_rho, lat_rho, grid["mask_rho"].values, max_km=max_km
    )
    box = box_for(selection, lon_rho.shape)
    if box is None:
        return [gap(SOURCE, "no coverage", f"no wet rho point within {max_km} km")] * n

    data = client.get_opendap(
        SOURCE,
        ep.url,
        {
            "step": "series",
            "box": list(box),
            "days": days,
            "date": today or today_utc(),
        },
        _select_series(box, days),
        factor=FACTOR,
    )
    if isinstance(data, DataGap):
        return broadcast(data, n, SOURCE)

    times = list(data["time"].values)
    runs = data["time_run"].values if "time_run" in data else np.array([])
    runs = runs[~np.isnat(runs)] if runs.size else runs
    if runs.size == 0:
        # No run time recorded: cannot cite a model run date -> gap, not a guess.
        return [gap(SOURCE, "empty response", "no time_run in subset")] * n
    run_date = iso_utc(runs.max())[:10]
    first_run = iso_utc(runs.min())[:10]
    note = None if first_run == run_date else f"FMRC Best runs {first_run}..{run_date}"

    citation = Citation(
        id=SOURCE,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=ep.url,
        date=run_date,
        date_kind="model_run",
        note=MODELED_CURRENT,
    )
    u_rho, v_rho = uv_to_rho(
        data["ubar"].transpose("time", "eta_u", "xi_u").values,
        data["vbar"].transpose("time", "eta_v", "xi_v").values,
    )
    return build_series(
        points,
        box,
        u_rho,
        v_rho,
        times,
        lon_rho,
        lat_rho,
        grid["mask_rho"].values,
        citation,
        note=note,
        max_km=max_km,
    )
