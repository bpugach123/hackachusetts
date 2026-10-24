"""Depth fallback from the NOAA NCEI Coastal Relief Model (Req 9.1, 4.4).

NECOFS ``h`` at the nearest node is the primary depth (``sources/necofs.py``
puts it on ``VelocitySeries.depth_m``). This module fills the cells where NECOFS
gives no depth, from the NCEI U.S. Coastal Relief Model, Vol. 1 (Northeast
Atlantic, 3 arc-second grid), read over OPeNDAP from ``endpoints.NCEI_CRM``.
The coastwatch ERDDAP id ``usgsCeCrm1`` named in the design returned 404 on
2026-10-24, so the registry points at the NCEI THREDDS copy.

One OPeNDAP read for every point that needs a depth, through the fetch layer
(cache + retry):

* the elevation variable (``z``; also accepts ``elevation``/``topo``/``Band1``)
  over the points' bbox + :data:`PAD_DEG`, strided so the block holds at most
  :data:`MAX_VALUES` values (stride 1 for small grids);
* per point: the nearest sampled grid node. Depth = ``-z`` (m, positive down).

No fabrication (Req 2.7): a node farther than :data:`MAX_KM`, a non-finite
value, or ``z >= 0`` (land / intertidal at the node) is a ``"no coverage"``
DataGap for that point. A failed or empty read is a gap for every point.

The result is a :class:`siting.score.resource.DepthInput`, which the tidal-
stream depth factor already accepts as its fallback; Feasibility reads the
same ``depth`` factor. Tagged QUANTIFIED DATA + EXTERNAL ADDITION (source
``NCEI_CRM``). Slope from the 3x3 neighborhood is the STRETCH task 15.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

from siting.fetch.cache import cache_key
from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import Citation
from siting.score.resource import DepthInput
from siting.sources.endpoints import NCEI_CRM
from siting.sources.velocity import Point, bbox_of, cacheable, haversine_km

SOURCE = NCEI_CRM.source  # "NCEI_CRM"
FACTOR = "depth"
# Citation id (matches output/sample/metadata.json).
CITATION_ID = "ncei-crm"
PAD_DEG = 0.02  # ~2 km around the points' bbox
MAX_VALUES = 4_000_000  # cap on the strided block (~16 MB float32)
MAX_KM = 1.0  # nearest sampled node must lie within this of the centroid
BBOX_DP = 4  # round the bbox so the cache key is stable

LON_NAMES: tuple[str, ...] = ("x", "lon", "longitude")
LAT_NAMES: tuple[str, ...] = ("y", "lat", "latitude")
Z_NAMES: tuple[str, ...] = ("z", "elevation", "topo", "Band1")


# ------------------------------------------------------------------ helpers


def _pick(names: Sequence[str], available: Any, what: str) -> str:
    for n in names:
        if n in available:
            return n
    raise KeyError(f"CRM: no {what} variable (looked for {list(names)})")


def _axes(ds: Any, zname: str) -> tuple[str, str, str, str]:
    """``(y dim, x dim, lat coord, lon coord)`` of the elevation variable."""
    ydim, xdim = ds[zname].dims[-2:]
    lat = ydim if ydim in ds.variables else _pick(LAT_NAMES, ds.variables, "lat")
    lon = xdim if xdim in ds.variables else _pick(LON_NAMES, ds.variables, "lon")
    return ydim, xdim, lat, lon


def stride_for(ny: int, nx: int, max_values: int = MAX_VALUES) -> int:
    """Smallest stride s with ceil(ny/s) * ceil(nx/s) <= max_values."""
    if ny <= 0 or nx <= 0:
        return 1
    s = max(1, math.ceil(math.sqrt(ny * nx / max_values)))
    while math.ceil(ny / s) * math.ceil(nx / s) > max_values:
        s += 1
    return s


def _window(coord: np.ndarray, lo: float, hi: float) -> slice:
    """Index slice covering ``[lo, hi]`` on a monotonic 1-D coordinate."""
    inside = np.nonzero((coord >= lo) & (coord <= hi))[0]
    if inside.size == 0:
        # bbox smaller than one cell: take the single nearest index
        k = int(np.argmin(np.abs(coord - (lo + hi) / 2.0)))
        return slice(k, k + 1)
    return slice(int(inside[0]), int(inside[-1]) + 1)


def subset_params(points: Sequence[Point]) -> dict:
    lat0, lat1, lon0, lon1 = bbox_of(points, PAD_DEG)
    f = 10**BBOX_DP
    bbox = [
        math.floor(lat0 * f) / f,
        math.ceil(lat1 * f) / f,
        math.floor(lon0 * f) / f,
        math.ceil(lon1 * f) / f,
    ]
    return {"step": "depth", "bbox": bbox, "max_values": MAX_VALUES}


def select_block(params: dict):
    """``select`` for :meth:`FetchClient.get_opendap`: strided bbox block of z."""
    lat0, lat1, lon0, lon1 = params["bbox"]

    def select(ds: Any) -> Any:
        zname = _pick(Z_NAMES, ds.variables, "elevation")
        ydim, xdim, lat_name, lon_name = _axes(ds, zname)
        ys = np.asarray(ds[lat_name].values, dtype=float)
        xs = np.asarray(ds[lon_name].values, dtype=float)
        sy, sx = _window(ys, lat0, lat1), _window(xs, lon0, lon1)
        s = stride_for(sy.stop - sy.start, sx.stop - sx.start, params["max_values"])
        sub = ds[[zname]].isel(
            {ydim: slice(sy.start, sy.stop, s), xdim: slice(sx.start, sx.stop, s)}
        )
        sub = sub.rename({zname: "z"})
        sub.attrs = dict(ds.attrs)
        sub.attrs["crm_stride"] = int(s)
        return cacheable(sub)

    return select


def _coords(ds: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(lats 1-D, lons 1-D, z 2-D [lat, lon])`` from a cached block."""
    ydim, xdim, lat_name, lon_name = _axes(ds, "z")
    z = np.asarray(ds["z"].transpose(..., ydim, xdim).values, dtype=float)
    z = z.reshape(z.shape[-2:])
    lats = np.asarray(ds[lat_name].values, dtype=float)
    lons = np.asarray(ds[lon_name].values, dtype=float)
    return lats, lons, z


def depth_at(
    lat: float,
    lon: float,
    lats: np.ndarray,
    lons: np.ndarray,
    z: np.ndarray,
    *,
    max_km: float = MAX_KM,
) -> tuple[float, float] | DataGap:
    """``(depth_m, node distance km)`` at the nearest grid node, or a gap."""
    if lats.size == 0 or lons.size == 0:
        return DataGap(FACTOR, SOURCE, "no coverage", "empty CRM block")
    iy = int(np.argmin(np.abs(lats - lat)))
    ix = int(np.argmin(np.abs(lons - lon)))
    dist = float(haversine_km(lat, lon, lats[iy], lons[ix]))
    if dist > max_km:
        return DataGap(
            FACTOR,
            SOURCE,
            "no coverage",
            f"nearest CRM node {dist:.2f} km > {max_km:g} km",
        )
    val = float(z[iy, ix])
    if not math.isfinite(val):
        return DataGap(FACTOR, SOURCE, "no coverage", "CRM node has no value")
    if val >= 0.0:
        return DataGap(
            FACTOR,
            SOURCE,
            "no coverage",
            f"CRM node at or above sea level (z={val:g} m)",
        )
    return -val, dist


# ---------------------------------------------------------------- citation


def _fetch_date(client: FetchClient, url: str, params: dict) -> str:
    """Cache-meta fetch date for this read (the payload is always cached first)."""
    entry = client.cache.read(SOURCE, cache_key(SOURCE, url, params))
    date = entry.meta.get("fetch_date") if entry is not None else None
    return date or datetime.now(UTC).strftime("%Y-%m-%d")


def citation(date: str) -> Citation:
    return Citation(
        id=CITATION_ID,
        agency=NCEI_CRM.agency,
        dataset=NCEI_CRM.dataset,
        endpoint=NCEI_CRM.url,
        date=date,
        date_kind="fetch",
    )


# ------------------------------------------------------------------- fetch


def fetch_depths(
    client: FetchClient, points: Sequence[Point]
) -> list[DepthInput | DataGap]:
    """CRM depth per point: ``DepthInput`` or ``DataGap`` (one entry per point)."""
    n = len(points)
    if n == 0:
        return []
    url = NCEI_CRM.url
    params = subset_params(points)
    ds = client.get_opendap(SOURCE, url, params, select_block(params), factor=FACTOR)
    if isinstance(ds, DataGap):
        return [ds.for_factor(FACTOR)] * n
    try:
        lats, lons, z = _coords(ds)
    except (KeyError, ValueError) as exc:
        g = DataGap(FACTOR, SOURCE, "fetch failure", f"unreadable CRM block: {exc}")
        return [g] * n
    stride = int(ds.attrs.get("crm_stride", 1))
    cit = citation(_fetch_date(client, url, params))
    out: list[DepthInput | DataGap] = []
    for lat, lon in points:
        got = depth_at(lat, lon, lats, lons, z)
        if isinstance(got, DataGap):
            out.append(got)
            continue
        d, dist = got
        note = f"NECOFS h unavailable; CRM fallback (node {dist:.2f} km"
        note += f", every {stride} grid nodes)" if stride > 1 else ")"
        out.append(DepthInput(depth_m=d, source=SOURCE, citation=cit, note=note))
    return out


def fallback_depths(
    client: FetchClient, points: Sequence[Point], series: Sequence[Any]
) -> list[Any]:
    """CRM depth only where the velocity series carries no NECOFS ``h``.

    Returns one entry per point: ``None`` where the series already has a depth
    (primary source wins), else a ``DepthInput`` or ``DataGap``. Pass each entry
    as ``depth=`` to :func:`siting.score.resource.stream_inputs`.
    """
    need = [k for k, s in enumerate(series) if getattr(s, "depth_m", None) is None]
    out: list[Any] = [None] * len(points)
    got = fetch_depths(client, [points[k] for k in need])
    for k, r in zip(need, got, strict=True):
        out[k] = r
    return out
