"""MOM6 NWA12 velocity at the nearest grid point (Req 4.1, 3.3). Third fallback.

Source: NOAA GFDL / CEFI regional MOM6 Northwest Atlantic 1/12 deg hindcast,
release r20230520, served by NOAA PSL THREDDS (``endpoints.MOM6_NWA12``).

The task names ``ubar`` / ``vbar``, but the only reachable NWA12 velocity
product is the regridded daily-mean surface current pair ``ssu_rotate`` /
``ssv_rotate`` (separate u and v files; see ``endpoints.py``). This module uses
those, and every series it returns carries a ``note`` saying so:

* daily means average out the semidiurnal tide, so peaks are strongly
  understated. It sits third in the default priority for that reason;
* values are surface, not depth-averaged.

Per point: nearest grid point (1-D ``lat``, ``lon`` in 0-360) within
:data:`MAX_KM` whose u and v series are finite over the window (land is NaN).
Window: the last :data:`WINDOW_DAYS` days of the hindcast (ends 2019-12-31).

Model run date: the ``cefi_release`` attribute (``r20230520`` -> 2023-05-20),
falling back to the release tag in the URL. No tag -> ``"empty response"`` gap.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import MODELED_CURRENT, Citation
from siting.sources.endpoints import MOM6_NWA12
from siting.sources.velocity import (
    FACTOR,
    MOM6,
    Point,
    VelocitySeries,
    all_finite_along,
    bbox_of,
    broadcast,
    cacheable,
    gap,
    iso_utc,
    median_step_s,
    nearest_index,
    run_date_from_release,
    speeds_from_uv,
)

SOURCE = MOM6
U_VAR, V_VAR = "ssu_rotate", "ssv_rotate"
MAX_KM = 10.0  # 1/12 deg grid (~7-9 km); coastal points are NaN (land)
WINDOW_DAYS = 30
BBOX_MARGIN_DEG = 0.25
NOTE = "daily-mean surface current (tidal signal averaged out; not depth-averaged)"


# ------------------------------------------------------------------ pure parse


def select_points(
    points: Sequence[Point],
    lat: Any,
    lon: Any,
    valid: Any,
    *,
    max_km: float = MAX_KM,
) -> list[tuple[int, int, float] | None]:
    """Nearest valid ``(j, i, distance_km)`` on a regular ``lat x lon`` grid."""
    lat1 = np.asarray(lat, dtype=float)
    lon1 = np.asarray(lon, dtype=float)
    lat2, lon2 = np.meshgrid(lat1, lon1, indexing="ij")
    nx = lon1.size
    out: list[tuple[int, int, float] | None] = []
    for plat, plon in points:
        hit = nearest_index(plat, plon, lat2, lon2, max_km=max_km, valid=valid)
        if hit is None:
            out.append(None)
        else:
            j, i = divmod(hit[0], nx)
            out.append((j, i, hit[1]))
    return out


def build_series(
    points: Sequence[Point],
    u: Any,
    v: Any,
    times: Sequence[Any],
    lat: Any,
    lon: Any,
    citation: Citation,
    *,
    max_km: float = MAX_KM,
) -> list[VelocitySeries | DataGap]:
    """Per-point series from ``u``, ``v`` shaped ``(time, lat, lon)``."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    if len(times) == 0 or u.size == 0:
        return [gap(SOURCE, "empty response", "no time steps")] * len(points)
    speed = speeds_from_uv(u, v)
    valid = all_finite_along(speed, axis=0)
    selection = select_points(points, lat, lon, valid, max_km=max_km)
    lat1 = np.asarray(lat, dtype=float)
    lon1 = np.asarray(lon, dtype=float)
    t_start, t_end = iso_utc(times[0]), iso_utc(times[-1])
    dt_s = median_step_s(times)

    out: list[VelocitySeries | DataGap] = []
    for sel in selection:
        if sel is None:
            out.append(
                gap(SOURCE, "no coverage", f"no ocean grid point within {max_km} km")
            )
            continue
        j, i, dist = sel
        out.append(
            VelocitySeries(
                source=SOURCE,
                t_start=t_start,
                t_end=t_end,
                speeds_ms=tuple(float(x) for x in speed[:, j, i]),
                model_run_date=citation.date,
                citation=citation,
                label=MODELED_CURRENT,
                dt_s=dt_s,
                point_lat=float(lat1[j]),
                point_lon=float((lon1[i] + 180.0) % 360.0 - 180.0),
                distance_km=round(dist, 3),
                note=NOTE,
            )
        )
    return out


# ------------------------------------------------------------------ fetch


def _select(var: str, bbox: tuple[float, ...], days: int):
    lat0, lat1, lon0, lon1 = bbox

    def select(ds: Any) -> Any:
        sub = ds[[var]].sel(
            lat=slice(lat0, lat1), lon=slice(lon0 % 360.0, lon1 % 360.0)
        )
        return cacheable(sub).isel(time=slice(-days, None))

    return select


def fetch(
    client: FetchClient,
    points: Sequence[Point],
    *,
    max_km: float = MAX_KM,
    days: int = WINDOW_DAYS,
) -> list[VelocitySeries | DataGap]:
    """MOM6 NWA12 daily surface-current series per point."""
    n = len(points)
    if n == 0:
        return []
    ep = MOM6_NWA12
    v_url = ep.extra.get("v_url", "")
    bbox = tuple(round(x, 4) for x in bbox_of(points, BBOX_MARGIN_DEG))
    params = {"bbox": list(bbox), "days": days}

    du = client.get_opendap(
        SOURCE,
        ep.url,
        {**params, "var": U_VAR},
        _select(U_VAR, bbox, days),
        factor=FACTOR,
    )
    if isinstance(du, DataGap):
        return broadcast(du, n, SOURCE)
    dv = client.get_opendap(
        SOURCE,
        v_url,
        {**params, "var": V_VAR},
        _select(V_VAR, bbox, days),
        factor=FACTOR,
    )
    if isinstance(dv, DataGap):
        return broadcast(dv, n, SOURCE)

    run_date = run_date_from_release(du.attrs.get("cefi_release")) or (
        run_date_from_release(ep.url)
    )
    if run_date is None:
        return [gap(SOURCE, "empty response", "no release tag for run date")] * n
    if not np.array_equal(du["time"].values, dv["time"].values):
        return [gap(SOURCE, "empty response", "u/v time axes differ")] * n

    citation = Citation(
        id=SOURCE,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=ep.url,
        date=run_date,
        date_kind="model_run",
        note=MODELED_CURRENT,
    )
    return build_series(
        points,
        du[U_VAR].transpose("time", "lat", "lon").values,
        dv[V_VAR].transpose("time", "lat", "lon").values,
        list(du["time"].values),
        du["lat"].values,
        du["lon"].values,
        citation,
        max_km=max_km,
    )
