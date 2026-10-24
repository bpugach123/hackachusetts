"""Land-masked grid over MA coastal waters (Req 1.1, 1.4, 1.6, 1.7).

``build(cfg, shoreline)`` returns one row per water Cell:

    cell_id, i, j, geometry_utm (EPSG:26919), centroid_lat, centroid_lon,
    geometry (EPSG:4326, active geometry)

Rules (design: Grid):

* All geometry work runs in EPSG:26919 (NAD83 / UTM 19N, meters).
* Fixed origin ``(x0, y0) = (230000, 4540000)``. Cell ``(i, j)`` has its
  lower-left corner at ``(x0 + i*s, y0 + j*s)`` with
  ``s = cell_size_mi * 1609.344``. ``cell_id = f"C{s:.0f}-{i:04d}-{j:04d}"``, so
  the same config always yields the same ids and centroids (Req 1.7).
* Extent = MA land buffered by ``outer_extent_nm * 1852`` m, minus all land
  (MA, NH, RI), clipped at the seaward extensions of the MA/NH and MA/RI land
  boundaries. Islands (Nantucket, Martha's Vineyard, Elizabeth Islands) are part
  of the MA polygon, so the waters around them are inside the buffer.
* A cell is kept iff its centroid lies strictly inside the water extent. A
  centroid on land, or on the shoreline itself, is dropped (Req 1.4).

Lateral clips: the MA/RI coastal junction (Westport / Little Compton) faces
south, so its boundary is extended seaward along the meridian and water west of
it is dropped. The MA/NH coastal junction (Salisbury / Seabrook) faces east, so
a meridian through it would not separate MA and NH waters; it is extended
seaward along the parallel and water north of it is dropped. Junctions are found
from the shared land border in the shoreline data, so nothing is hard-coded. A
neighbor missing from ``shoreline`` skips that clip.

The centroid test is vectorized (``shapely.contains_xy`` on a prepared polygon)
and chunked by row, so 0.25 mi cells over a 200 nm extent stay tractable.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

UTM_CRS = "EPSG:26919"
WGS84 = "EPSG:4326"
X0, Y0 = 230_000.0, 4_540_000.0
M_PER_MI = 1609.344
M_PER_NM = 1852.0
LATLON_DP = 6

MA, NH, RI = "25", "33", "44"
COLUMNS = ["cell_id", "i", "j", "geometry_utm", "centroid_lat", "centroid_lon"]

_JUNCTION_TOL_M = 1.0  # shared-border tolerance for slightly mismatched vertices
_ROW_CHUNK_POINTS = 1_000_000  # centroids tested per contains_xy call
_CLIP_DENSIFY_DEG = 0.01  # segment length before projecting the clip box

_TO_WGS84 = Transformer.from_crs(UTM_CRS, WGS84, always_xy=True)
_TO_UTM = Transformer.from_crs(WGS84, UTM_CRS, always_xy=True)


def cell_size_m(cfg: Any) -> float:
    return float(cfg.cell_size_mi) * M_PER_MI


def cell_id(s_m: float, i: int, j: int) -> str:
    return f"C{s_m:.0f}-{i:04d}-{j:04d}"


# ------------------------------------------------------------------ extent


def _state_geom(utm: gpd.GeoDataFrame, statefp: str) -> BaseGeometry | None:
    rows = utm[utm["STATEFP"] == statefp]
    if rows.empty:
        return None
    geom = shapely.union_all(rows.geometry.values)
    return None if geom.is_empty else geom


def _junction(
    ma: BaseGeometry, neighbor: BaseGeometry, pick: str
) -> tuple[float, float] | None:
    """Coastal end of the MA/neighbor land border, in UTM.

    ``pick="east"`` takes the easternmost shared point (MA/NH), ``"south"`` the
    southernmost (MA/RI).
    """
    shared = ma.boundary.intersection(neighbor.buffer(_JUNCTION_TOL_M))
    coords = shapely.get_coordinates(shared)
    if coords.size == 0:
        return None
    k = int(np.argmax(coords[:, 0])) if pick == "east" else int(np.argmin(coords[:, 1]))
    return float(coords[k, 0]), float(coords[k, 1])


def _lateral_clip(
    ma: BaseGeometry,
    nh: BaseGeometry | None,
    ri: BaseGeometry | None,
    bounds_utm: tuple[float, float, float, float],
) -> BaseGeometry | None:
    """Box in lon/lat bounded west by the RI meridian and north by the NH parallel."""
    nh_pt = _junction(ma, nh, "east") if nh is not None else None
    ri_pt = _junction(ma, ri, "south") if ri is not None else None
    if nh_pt is None and ri_pt is None:
        return None

    minx, miny, maxx, maxy = bounds_utm
    xs = np.array([minx, maxx, minx, maxx])
    ys = np.array([miny, miny, maxy, maxy])
    lons, lats = _TO_WGS84.transform(xs, ys)
    pad = 1.0  # degrees, keeps the box well outside the extent
    west, east = float(np.min(lons)) - pad, float(np.max(lons)) + pad
    south, north = float(np.min(lats)) - pad, float(np.max(lats)) + pad
    if ri_pt is not None:
        west = float(_TO_WGS84.transform(*ri_pt)[0])
    if nh_pt is not None:
        north = float(_TO_WGS84.transform(*nh_pt)[1])

    clip_ll = shapely.segmentize(box(west, south, east, north), _CLIP_DENSIFY_DEG)
    return shapely.transform(
        clip_ll, lambda c: np.column_stack(_TO_UTM.transform(c[:, 0], c[:, 1]))
    )


def water_extent(cfg: Any, shoreline: gpd.GeoDataFrame) -> BaseGeometry:
    """MA land buffered by the outer extent, minus land, clipped at NH/RI."""
    if shoreline.crs is None:
        raise ValueError("shoreline has no CRS")
    utm = shoreline.to_crs(UTM_CRS)
    utm = utm.assign(STATEFP=utm["STATEFP"].astype(str).str.zfill(2))
    ma = _state_geom(utm, MA)
    if ma is None:
        raise ValueError("shoreline has no Massachusetts (STATEFP 25) polygon")
    land = shapely.union_all(utm.geometry.values)

    extent = ma.buffer(float(cfg.outer_extent_nm) * M_PER_NM)
    clip = _lateral_clip(ma, _state_geom(utm, NH), _state_geom(utm, RI), extent.bounds)
    if clip is not None:
        extent = extent.intersection(clip)
    return extent.difference(land)


# -------------------------------------------------------------------- build


def _index_range(lo: float, hi: float, origin: float, s: float) -> np.ndarray:
    """Cell indices whose centroid could fall in ``[lo, hi]``."""
    first = int(np.floor((lo - origin) / s - 0.5))
    last = int(np.ceil((hi - origin) / s - 0.5))
    return np.arange(first, last + 1, dtype=np.int64)


def _water_cells(water: BaseGeometry, s: float) -> tuple[np.ndarray, np.ndarray]:
    """``(i, j)`` of every cell whose centroid is strictly inside ``water``."""
    if water.is_empty:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    minx, miny, maxx, maxy = water.bounds
    ii = _index_range(minx, maxx, X0, s)
    jj = _index_range(miny, maxy, Y0, s)
    cx = X0 + (ii + 0.5) * s
    shapely.prepare(water)

    rows_per_chunk = max(1, _ROW_CHUNK_POINTS // max(1, ii.size))
    keep_i: list[np.ndarray] = []
    keep_j: list[np.ndarray] = []
    for start in range(0, jj.size, rows_per_chunk):
        jc = jj[start : start + rows_per_chunk]
        gx, gy = np.meshgrid(cx, Y0 + (jc + 0.5) * s)
        mask = shapely.contains_xy(water, gx, gy)
        if mask.any():
            r, c = np.nonzero(mask)
            keep_i.append(ii[c])
            keep_j.append(jc[r])
    if not keep_i:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    return np.concatenate(keep_i), np.concatenate(keep_j)


def build(cfg: Any, shoreline: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Grid of water Cells for ``cfg.cell_size_mi`` / ``cfg.outer_extent_nm``.

    ``shoreline`` is a GeoDataFrame with a ``STATEFP`` column (MA "25" required;
    NH "33" and RI "44" used for the land mask and lateral clips) in any CRS.
    Rows are sorted by ``(i, j)``.
    """
    s = cell_size_m(cfg)
    water = water_extent(cfg, shoreline)
    i, j = _water_cells(water, s)
    order = np.lexsort((j, i))
    i, j = i[order], j[order]

    x_lo = X0 + i * s
    y_lo = Y0 + j * s
    polys = shapely.box(x_lo, y_lo, x_lo + s, y_lo + s)
    lon, lat = _TO_WGS84.transform(x_lo + 0.5 * s, y_lo + 0.5 * s)

    geometry_utm = gpd.GeoSeries(polys, crs=UTM_CRS)
    df = pd.DataFrame(
        {
            "cell_id": [
                cell_id(s, a, b) for a, b in zip(i.tolist(), j.tolist(), strict=True)
            ],
            "i": i,
            "j": j,
            "centroid_lat": np.round(np.asarray(lat, dtype=float), LATLON_DP),
            "centroid_lon": np.round(np.asarray(lon, dtype=float), LATLON_DP),
        }
    )
    gdf = gpd.GeoDataFrame(df, geometry=geometry_utm.to_crs(WGS84).values, crs=WGS84)
    gdf["geometry_utm"] = geometry_utm.values
    return gdf[[*COLUMNS, "geometry"]]
