"""Grid Proximity component score (Req 6.1-6.6). Track C -> Track A contract.

Per cell centroid, in UTM 19N (EPSG:26919, meters):

* ``dist_transmission_km``: straight-line distance to the nearest HIFLD
  transmission line geometry, km rounded to 0.01 (STRtree nearest query).
* ``dist_substation_km``: same, to the nearest HIFLD substation.
* ``utility``: the MassGIS town territory containing the centroid, else the
  nearest one. Label "distribution interconnection below 5 MW", tagged PROXY.
* Sub-scores ``ramp(d, grid_full_km, grid_zero_km, 100, 0)`` on the stored
  (rounded) distance; component = ``weighted_mean_available`` with
  ``cfg.grid_weights`` (keys ``transmission``, ``substation``).

A failed or empty layer gives every cell a null factor carrying the DataGap,
a null sub-score, and the component from the remaining sub-score alone. Both
null -> component null plus a ``grid`` DataGap (Req 6.6). The utility does not
feed the score. Scores are not rounded here (1 dp at write time).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from shapely import STRtree

from siting.config import DeviceConfig
from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import (
    PROXY,
    QUANTIFIED,
    Citation,
    Factor,
    FactorResult,
    tags_for,
)
from siting.score.ramps import ramp, weighted_mean_available
from siting.sources import hifld, massgis
from siting.sources.arcgis import Layer, citation_for, grid_envelope
from siting.sources.endpoints import (
    HIFLD_SUBSTATIONS,
    HIFLD_TRANSMISSION,
    MASSGIS_UTILITY,
)

GRID_FACTOR = "grid"
TRANSMISSION = "transmission"
SUBSTATION = "substation"
DIST_TRANSMISSION = "dist_transmission_km"
DIST_SUBSTATION = "dist_substation_km"
UTILITY = "utility"
UTILITY_LABEL = "distribution interconnection below 5 MW"
GRID_MARGIN_KM = 50.0

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:26919", always_xy=True)


@dataclass
class GridInputs:
    """Fetched layers (or run-wide gaps) shared by every cell."""

    transmission: Layer | DataGap
    substations: Layer | DataGap
    utility: Layer | DataGap


def fetch_inputs(client: FetchClient, cells: Any) -> GridInputs:
    """Fetch the three layers over the Grid bbox + 50 km (Req 6.1)."""
    env = grid_envelope(cells, GRID_MARGIN_KM)
    return GridInputs(
        transmission=hifld.fetch_transmission(client, env),
        substations=hifld.fetch_substations(client, env),
        utility=massgis.fetch_utility_territory(client, env),
    )


# ------------------------------------------------------------------ geometry


def centroids_utm(cells: Any) -> np.ndarray:
    """``shapely.Point`` array of cell centroids in UTM meters.

    Uses the exact ``geometry_utm`` centroid when present (``grid.build``
    output), else projects ``centroid_lon``/``centroid_lat``.
    """
    if "geometry_utm" in getattr(cells, "columns", ()):
        return shapely.centroid(np.asarray(cells["geometry_utm"].values))
    x, y = _TO_UTM.transform(
        np.asarray(cells["centroid_lon"], float),
        np.asarray(cells["centroid_lat"], float),
    )
    return shapely.points(np.asarray(x), np.asarray(y))


def nearest_distances_km(points_utm: Any, geoms_utm: Any) -> np.ndarray:
    """Distance (km, rounded to 0.01) from each point to the nearest geometry.

    STRtree nearest query; the distance is exact shapely planar distance in
    UTM meters. ``geoms_utm`` must be non-empty.
    """
    geoms = np.asarray(list(geoms_utm), dtype=object)
    if geoms.size == 0:
        raise ValueError("nearest_distances_km needs at least one geometry")
    pts = np.asarray(points_utm, dtype=object)
    tree = STRtree(geoms)
    (idx_in, _idx_tree), dist = tree.query_nearest(
        pts, return_distance=True, all_matches=False
    )
    out = np.full(pts.shape[0], np.nan)
    out[idx_in] = dist
    return np.round(out / 1000.0, 2)


@dataclass(frozen=True)
class UtilityMatch:
    name: str
    town: str | None
    contains: bool
    distance_km: float


def utility_matches(points_utm: Any, layer: Layer) -> list[UtilityMatch | None]:
    """Territory containing each point, else the nearest (Req 6.3).

    Ties (a point on a shared town boundary, or equidistant towns) go to the
    lowest feature index so the result is deterministic. Features without a
    provider name are skipped; if none has one, every point gets ``None``.
    """
    keep = [i for i, p in enumerate(layer.properties) if massgis.utility_name(p)]
    pts = np.asarray(points_utm, dtype=object)
    if not keep:
        return [None] * pts.shape[0]
    geoms = np.asarray([layer.geoms_utm[i] for i in keep], dtype=object)
    tree = STRtree(geoms)
    (idx_in, idx_tree), dist = tree.query_nearest(
        pts, return_distance=True, all_matches=True
    )
    best: dict[int, tuple[int, float]] = {}
    for p, t, d in zip(idx_in.tolist(), idx_tree.tolist(), dist.tolist(), strict=True):
        if p not in best or t < best[p][0]:
            best[p] = (t, d)
    out: list[UtilityMatch | None] = []
    for p in range(pts.shape[0]):
        if p not in best:
            out.append(None)
            continue
        t, d = best[p]
        props = layer.properties[keep[t]]
        out.append(
            UtilityMatch(
                name=massgis.utility_name(props) or "",
                town=props.get("TOWN"),
                contains=d == 0.0,
                distance_km=round(d / 1000.0, 2),
            )
        )
    return out


# ------------------------------------------------------------------- scoring


def distance_subscore(d_km: float, cfg: DeviceConfig) -> float:
    """100 at or below ``grid_full_km``, 0 at or above ``grid_zero_km`` (Req 6.4)."""
    return ramp(d_km, cfg.grid_full_km, cfg.grid_zero_km, 100.0, 0.0)


def combine_grid(
    transmission_sub: float | None,
    substation_sub: float | None,
    weights: Mapping[str, float],
    gaps: list[DataGap] | None = None,
) -> FactorResult:
    """Grid Proximity component from the available sub-scores (Req 6.5, 6.6)."""
    subs = {TRANSMISSION: transmission_sub, SUBSTATION: substation_sub}
    wm = weighted_mean_available(subs, weights)
    out = FactorResult(score=wm.value, omitted=list(wm.omitted))
    out.extra["grid_subscores"] = dict(subs)
    if wm.value is None:
        under = list(gaps or [])
        sources = sorted({g.source for g in under}) or [
            HIFLD_TRANSMISSION.source,
            HIFLD_SUBSTATIONS.source,
        ]
        reasons = {g.reason for g in under}
        reason = reasons.pop() if len(reasons) == 1 else "fetch failure"
        out.gaps.append(
            DataGap(
                GRID_FACTOR,
                ",".join(sources),
                reason,
                "transmission and substation sub-scores both null",
            )
        )
    return out


def _distance_factor(
    name: str, layer: Layer | DataGap, d_km: float | None, gap_citation: Citation
) -> Factor:
    source = (
        HIFLD_TRANSMISSION.source
        if name == DIST_TRANSMISSION
        else (HIFLD_SUBSTATIONS.source)
    )
    tags = tags_for(QUANTIFIED, source)
    if isinstance(layer, DataGap):
        return Factor(name, None, "km", tags, gap_citation, gap=layer.for_factor(name))
    return Factor(name, float(d_km), "km", tags, layer.citation)  # type: ignore[arg-type]


def _utility_factor(
    layer: Layer | DataGap, match: UtilityMatch | None, gap_citation: Citation
) -> Factor:
    tags = tags_for(PROXY, MASSGIS_UTILITY.source)
    if isinstance(layer, DataGap):
        return Factor(
            UTILITY,
            None,
            None,
            tags,
            gap_citation,
            gap=layer.for_factor(UTILITY),
            label=UTILITY_LABEL,
        )
    if match is None:
        gap = DataGap(
            UTILITY,
            MASSGIS_UTILITY.source,
            "no coverage",
            "no town record with a provider name",
        )
        return Factor(
            UTILITY, None, None, tags, layer.citation, gap=gap, label=UTILITY_LABEL
        )
    where = (
        "contains centroid"
        if match.contains
        else f"nearest, {match.distance_km:.2f} km"
    )
    town = f"{match.town.title()}: " if match.town else ""
    return Factor(
        UTILITY,
        match.name,
        None,
        tags,
        layer.citation,
        label=UTILITY_LABEL,
        note=f"{town}{where}",
    )


def score_grid_proximity(
    cells: Any, inputs: GridInputs, cfg: DeviceConfig
) -> dict[str, FactorResult]:
    """Grid Proximity :class:`FactorResult` per ``cell_id`` (Req 6.2-6.6).

    ``cells`` is a ``grid.build`` GeoDataFrame (or any frame with ``cell_id``
    and ``geometry_utm`` or ``centroid_lat``/``centroid_lon``).
    """
    ids = [str(c) for c in cells["cell_id"]]
    if not ids:
        return {}
    pts = centroids_utm(cells)

    def dists(layer: Layer | DataGap) -> np.ndarray | None:
        return (
            None
            if isinstance(layer, DataGap)
            else nearest_distances_km(pts, layer.geoms_utm)
        )

    d_t = dists(inputs.transmission)
    d_s = dists(inputs.substations)
    utils = (
        None
        if isinstance(inputs.utility, DataGap)
        else utility_matches(pts, inputs.utility)
    )
    cit_t = citation_for(hifld.TRANSMISSION_CITATION_ID, HIFLD_TRANSMISSION)
    cit_s = citation_for(hifld.SUBSTATIONS_CITATION_ID, HIFLD_SUBSTATIONS)
    cit_u = citation_for(massgis.UTILITY_CITATION_ID, MASSGIS_UTILITY)

    out: dict[str, FactorResult] = {}
    for k, cid in enumerate(ids):
        t_km = None if d_t is None else float(d_t[k])
        s_km = None if d_s is None else float(d_s[k])
        f_t = _distance_factor(DIST_TRANSMISSION, inputs.transmission, t_km, cit_t)
        f_s = _distance_factor(DIST_SUBSTATION, inputs.substations, s_km, cit_s)
        f_u = _utility_factor(
            inputs.utility, None if utils is None else utils[k], cit_u
        )
        sub_t = None if t_km is None else distance_subscore(t_km, cfg)
        sub_s = None if s_km is None else distance_subscore(s_km, cfg)
        gaps = [f.gap for f in (f_t, f_s) if f.gap is not None]
        res = combine_grid(sub_t, sub_s, cfg.grid_weights, gaps)
        for f in (f_t, f_s, f_u):
            res.add(f)
        out[cid] = res
    return out
