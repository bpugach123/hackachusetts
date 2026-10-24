"""Conflict component: Hard_Exclusions + graded sub-scores (Req 7.1, 7.3-7.6).

Track C -> Track A contract. Hard_Exclusions run first (:func:`exclusions`).
The graded Conflict component (:func:`score_conflict`, 0 = no detected
conflict, 100 = maximum) is computed only for cells that are not excluded.

Exclusion layer groups come from ``cfg.hard_exclusion_layers`` (validated in
``siting.config``). Each group is one or more source layers:

* ``protected_habitat``: NOAA MPA Inventory (no-take / no-access / no-impact)
  + MA NHESP Priority Habitats of Rare Species.
* ``residential_shoreline``: MassGIS Land Use 2016 residential polygons
  + MassGIS / DCR Marine Beaches.
* ``shipping_lanes``: NOAA ENC shipping fairways, lanes and zones (TSS).
* ``cable_areas``: submarine cable areas (NOAA ENC data).

North Atlantic right whale critical habitat covers almost all of Cape Cod Bay,
so it is a graded Conflict sub-score, not an exclusion. Diadromous fish
proximity is never an exclusion trigger (Req 7.5).

Rule (Req 7.1): a cell is excluded by a group when its polygon ``intersects``
(UTM 19N) any geometry of any of the group's layers. ``exclusion_layers`` lists
every triggering group, and ``exclusion_citation_ids`` names the citation of
each layer that triggered it.

Missing data (Req 7.3): a source layer that fails or comes back empty gives a
Data_Gap (``factor`` = group name) on every cell, excludes nothing, and adds the
group to every cell's ``exclusion_check_incomplete``. Other layers in the same
group still apply. Nothing is substituted for a missing layer.

Graded sub-scores (Req 7.4-7.6), each 0-100 and tagged:

* ``vessel_traffic``: AIS transit count at the centroid (NE Ocean Data Portal),
  percentile-ranked across the cells with a value. PROXY.
* ``fishing_activity``: eMOLT fishing operations within 5 km of the centroid,
  percentile-ranked across the cells. PROXY.
* ``diadromous_fish``: ``ramp(d_km, 0, fish_zero_km, 100, 0)`` on the distance
  to the nearest MA DMF diadromous fish station (factor
  ``diadromous_fish_km``). PROXY. Never an exclusion trigger (Req 7.5).
* ``whale_critical_habitat``: 100 when the cell polygon intersects NOAA
  Fisheries North Atlantic right whale critical habitat, else 0. QUANTIFIED
  DATA + EXTERNAL ADDITION. Graded, not excluded.

Percentiles use the minimum rank of ties: ``100 * (number of strictly lower
values) / (n - 1)``, so the lowest value (e.g. zero tows) maps to 0 and the
highest to 100; a single value maps to 0. They are relative to this Grid run.

Component = ``weighted_mean_available`` over the sub-scores with
``cfg.conflict_weights`` (None = equal). All null -> component null plus a
``conflict`` DataGap (Req 7.6). Excluded cells get factors but no component.

The Tier 2 regulatory proxy here is NOT legal advice.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from shapely import STRtree

from siting.config import (
    HARD_EXCLUSION_LAYER_IDS,
    DeviceConfig,
    validate_exclusion_layers,
)
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
from siting.score.grid_proximity import centroids_utm, nearest_distances_km
from siting.score.ramps import ramp, weighted_mean_available
from siting.sources import emolt, marinecadastre, massgis, neodp
from siting.sources.arcgis import (
    Envelope,
    Layer,
    cell_polygons_utm,
    citation_for,
    fetch_layer,
    grid_envelope,
    query_tiles,
)
from siting.sources.emolt import FishingPoints
from siting.sources.endpoints import (
    EMOLT,
    MARINECADASTRE_CABLES,
    MARINECADASTRE_MPA,
    MARINECADASTRE_SHIPPING,
    MASSGIS_BEACHES,
    MASSGIS_LANDUSE,
    MASSGIS_NHESP,
    NEODP_DIADROMOUS,
    NEODP_VESSEL_TRAFFIC,
    NOAA_FISHERIES_RIGHT_WHALE,
)
from siting.sources.neodp import VesselSamples

PROTECTED_HABITAT = "protected_habitat"
RESIDENTIAL_SHORELINE = "residential_shoreline"
SHIPPING_LANES = "shipping_lanes"
CABLE_AREAS = "cable_areas"

# CellRecord fields written from FactorResult.extra (docs/schema.md).
HARD_EXCLUSION = "hard_exclusion"
EXCLUSION_LAYERS = "exclusion_layers"
EXCLUSION_CHECK_INCOMPLETE = "exclusion_check_incomplete"
EXCLUSION_CITATION_IDS = "exclusion_citation_ids"  # {group: [citation id]}

# Only intersections matter, so the envelope barely exceeds the Grid.
EXCLUSION_MARGIN_KM = 1.0


# ------------------------------------------------------------------- sources


@dataclass(frozen=True)
class QueryArea:
    """Spatial filters for the exclusion queries, computed once per run."""

    envelope: Envelope  # Grid bbox + EXCLUSION_MARGIN_KM (sparse marine layers)
    tiles: list[dict]  # cell-union query polygons (dense MassGIS land layers)


Fetcher = Callable[[FetchClient, QueryArea], Layer | DataGap]


@dataclass(frozen=True)
class ExclusionSource:
    source: str  # endpoint source id (provenance / gap source)
    fetch: Fetcher


EXCLUSION_GROUPS: dict[str, tuple[ExclusionSource, ...]] = {
    PROTECTED_HABITAT: (
        ExclusionSource(
            MARINECADASTRE_MPA.source,
            lambda c, a: marinecadastre.fetch_mpa_no_take(c, a.envelope),
        ),
        ExclusionSource(
            MASSGIS_NHESP.source,
            lambda c, a: massgis.fetch_nhesp_priority_habitat(c, a.tiles),
        ),
    ),
    RESIDENTIAL_SHORELINE: (
        ExclusionSource(
            MASSGIS_LANDUSE.source,
            lambda c, a: massgis.fetch_residential_landuse(c, a.tiles),
        ),
        ExclusionSource(
            MASSGIS_BEACHES.source,
            lambda c, a: massgis.fetch_public_beaches(c, a.envelope),
        ),
    ),
    SHIPPING_LANES: (
        ExclusionSource(
            MARINECADASTRE_SHIPPING.source,
            lambda c, a: marinecadastre.fetch_shipping_lanes(c, a.envelope),
        ),
    ),
    CABLE_AREAS: (
        ExclusionSource(
            MARINECADASTRE_CABLES.source,
            lambda c, a: marinecadastre.fetch_cable_areas(c, a.envelope),
        ),
    ),
}
assert tuple(EXCLUSION_GROUPS) == HARD_EXCLUSION_LAYER_IDS

# {group: [Layer or run-wide DataGap, one per source layer]}
ExclusionLayers = Mapping[str, Sequence[Layer | DataGap]]


def query_area(cells: Any) -> QueryArea:
    return QueryArea(
        envelope=grid_envelope(cells, EXCLUSION_MARGIN_KM), tiles=query_tiles(cells)
    )


def fetch_exclusion_layers(
    client: FetchClient, cells: Any, cfg: DeviceConfig
) -> dict[str, list[Layer | DataGap]]:
    """Fetch every source layer of each configured group (Req 7.1, 7.3)."""
    groups = validate_exclusion_layers(cfg.hard_exclusion_layers)
    if not groups:
        return {}
    area = query_area(cells)
    return {g: [s.fetch(client, area) for s in EXCLUSION_GROUPS[g]] for g in groups}


def exclusion_citations(layers: ExclusionLayers) -> list[Citation]:
    """Citations of every fetched exclusion layer, deduplicated by id."""
    out: dict[str, Citation] = {}
    for parts in layers.values():
        for part in parts:
            if isinstance(part, Layer):
                out.setdefault(part.citation.id, part.citation)
    return list(out.values())


# ------------------------------------------------------------------ geometry


def intersecting_cells(cell_polys: Any, geoms_utm: Sequence[Any]) -> set[int]:
    """Indices of cells whose polygon intersects at least one geometry."""
    geoms = np.asarray(list(geoms_utm), dtype=object)
    polys = np.asarray(cell_polys, dtype=object)
    if geoms.size == 0 or polys.size == 0:
        return set()
    bad = ~shapely.is_valid(geoms)
    if bad.any():  # server polygons can self-intersect; repair before testing
        geoms[bad] = shapely.make_valid(geoms[bad])
    idx_cells, _ = STRtree(geoms).query(polys, predicate="intersects")
    return set(idx_cells.tolist())


# ---------------------------------------------------------------- exclusions


def _layer_gap(group: str, part: Layer | DataGap) -> DataGap | None:
    if isinstance(part, DataGap):
        detail = f"exclusion layer {group}" + (
            f": {part.detail}" if part.detail else ""
        )
        return DataGap(group, part.source, part.reason, detail)
    if len(part) == 0:
        return DataGap(group, part.source, "empty response", "no geometries")
    return None


def exclusions(cells: Any, layers: ExclusionLayers) -> dict[str, FactorResult]:
    """Hard_Exclusion fields per ``cell_id`` (Req 7.1, 7.3).

    ``layers`` is :func:`fetch_exclusion_layers` output (or the same shape built
    by hand). Each result has ``score=None`` and ``extra`` with
    ``hard_exclusion``, ``exclusion_layers``, ``exclusion_check_incomplete`` and
    ``exclusion_citation_ids``; ``gaps`` holds one DataGap per missing layer.
    """
    ids = [str(c) for c in cells["cell_id"]]
    if not ids:
        return {}
    polys = cell_polygons_utm(cells)

    hits: list[dict[str, list[str]]] = [{} for _ in ids]
    incomplete: list[str] = []
    gaps: list[DataGap] = []
    for group, parts in layers.items():
        for part in parts:
            gap = _layer_gap(group, part)
            if gap is not None:
                gaps.append(gap)
                if group not in incomplete:
                    incomplete.append(group)
                continue
            assert isinstance(part, Layer)
            for k in intersecting_cells(polys, part.geoms_utm):
                cids = hits[k].setdefault(group, [])
                if part.citation.id not in cids:
                    cids.append(part.citation.id)

    out: dict[str, FactorResult] = {}
    for k, cid in enumerate(ids):
        res = FactorResult(score=None, gaps=list(gaps))
        res.extra[HARD_EXCLUSION] = bool(hits[k])
        res.extra[EXCLUSION_LAYERS] = list(hits[k])
        res.extra[EXCLUSION_CHECK_INCOMPLETE] = list(incomplete)
        res.extra[EXCLUSION_CITATION_IDS] = {g: list(c) for g, c in hits[k].items()}
        out[cid] = res
    return out


# ============================================================ graded conflict

CONFLICT_FACTOR = "conflict"
VESSEL_TRAFFIC = "vessel_traffic"
FISHING_ACTIVITY = "fishing_activity"
DIADROMOUS_FISH = "diadromous_fish"  # sub-score key
DIADROMOUS_FISH_KM = "diadromous_fish_km"  # factor key
WHALE_CRITICAL_HABITAT = "whale_critical_habitat"
CONFLICT_SUBSCORES: tuple[str, ...] = (
    VESSEL_TRAFFIC,
    FISHING_ACTIVITY,
    DIADROMOUS_FISH,
    WHALE_CRITICAL_HABITAT,
)
CONFLICT_SUBSCORES_EXTRA = "conflict_subscores"  # {sub-score: value | None}

FISHING_RADIUS_KM = 5.0
EMOLT_MARGIN_KM = FISHING_RADIUS_KM + 1.0  # tows just outside the Grid still count
FISH_STATION_MARGIN_KM = 50.0  # nearest station may sit up a river, off-Grid
WHALE_CITATION_ID = "nmfs-narw-ch"
WHALE_NOTE = "graded conflict, not a hard exclusion"
PERCENTILE = "percentile"

_TO_WGS84 = Transformer.from_crs("EPSG:26919", "EPSG:4326", always_xy=True)


@dataclass
class ConflictInputs:
    """Fetched conflict data (or run-wide gaps) for one Grid.

    ``vessel`` holds one sample per cell, in the order of ``cells``.
    """

    vessel: VesselSamples | DataGap
    fishing: FishingPoints | DataGap
    fish_stations: Layer | DataGap
    whale: Layer | DataGap


def centroids_lonlat(cells: Any) -> list[tuple[float, float]]:
    """Cell centroids as ``(lon, lat)`` (WGS84)."""
    cols = getattr(cells, "columns", ())
    if "centroid_lon" in cols and "centroid_lat" in cols:
        lon = np.asarray(cells["centroid_lon"], float)
        lat = np.asarray(cells["centroid_lat"], float)
    else:
        pts = centroids_utm(cells)
        lon, lat = _TO_WGS84.transform(shapely.get_x(pts), shapely.get_y(pts))
    return list(zip(np.asarray(lon).tolist(), np.asarray(lat).tolist(), strict=True))


def fetch_right_whale_habitat(
    client: FetchClient, envelope: Envelope
) -> Layer | DataGap:
    """North Atlantic right whale critical habitat polygons inside ``envelope``."""
    return fetch_layer(
        client, NOAA_FISHERIES_RIGHT_WHALE, envelope, citation_id=WHALE_CITATION_ID
    )


def fetch_conflict_inputs(client: FetchClient, cells: Any) -> ConflictInputs:
    """Fetch every graded conflict source for ``cells`` (Req 7.4)."""
    return ConflictInputs(
        vessel=neodp.sample_vessel_traffic(client, centroids_lonlat(cells)),
        fishing=emolt.fetch_fishing_points(
            client, grid_envelope(cells, EMOLT_MARGIN_KM)
        ),
        fish_stations=neodp.fetch_diadromous_stations(
            client, grid_envelope(cells, FISH_STATION_MARGIN_KM)
        ),
        whale=fetch_right_whale_habitat(
            client, grid_envelope(cells, EXCLUSION_MARGIN_KM)
        ),
    )


def conflict_citations(inputs: ConflictInputs) -> list[Citation]:
    """Citations of every conflict source that returned data, deduplicated by id."""
    out: dict[str, Citation] = {}
    for part in (inputs.vessel, inputs.fishing, inputs.fish_stations, inputs.whale):
        if not isinstance(part, DataGap):
            out.setdefault(part.citation.id, part.citation)
    return list(out.values())


# ------------------------------------------------------------------- helpers


def percentile_ranks(values: Sequence[float | None]) -> list[float | None]:
    """Min-rank percentile (0-100) of each value among the non-null values.

    ``100 * (count strictly lower) / (n - 1)``; ties share the lower rank and a
    single value maps to 0. ``None`` / NaN stay ``None``.
    """

    def ok(v: float | None) -> bool:
        return v is not None and not math.isnan(v)

    avail = sorted(float(v) for v in values if ok(v))  # type: ignore[arg-type]
    n = len(avail)
    out: list[float | None] = []
    for v in values:
        if not ok(v):
            out.append(None)
        elif n == 1:
            out.append(0.0)
        else:
            out.append(100.0 * bisect.bisect_left(avail, float(v)) / (n - 1))  # type: ignore[arg-type]
    return out


def fishing_counts(
    points_utm: Any, fishing: FishingPoints, radius_km: float = FISHING_RADIUS_KM
) -> np.ndarray:
    """Number of eMOLT fishing operations within ``radius_km`` of each point."""
    pts = np.asarray(points_utm, dtype=object)
    if len(fishing) == 0 or pts.size == 0:
        return np.zeros(pts.shape[0], dtype=int)
    tree = STRtree(np.asarray(fishing.points_utm, dtype=object))
    idx_in, _ = tree.query(pts, predicate="dwithin", distance=radius_km * 1000.0)
    return np.bincount(idx_in, minlength=pts.shape[0]).astype(int)


def diadromous_subscore(d_km: float, cfg: DeviceConfig) -> float:
    """100 at the station, 0 at or beyond ``cfg.fish_zero_km`` (Req 7.5)."""
    return ramp(d_km, 0.0, cfg.fish_zero_km, 100.0, 0.0)


def conflict_weights(cfg: DeviceConfig) -> dict[str, float]:
    """``cfg.conflict_weights`` over the sub-scores; None -> equal weights.

    Sub-scores missing from a configured mapping get weight 0. Unknown keys
    raise ``ValueError`` so a typo cannot silently drop a sub-score.
    """
    if cfg.conflict_weights is None:
        return {k: 1.0 / len(CONFLICT_SUBSCORES) for k in CONFLICT_SUBSCORES}
    given = dict(cfg.conflict_weights)
    bad = sorted(k for k in given if k not in CONFLICT_SUBSCORES)
    if bad:
        raise ValueError(
            f"conflict_weights: unknown sub-score(s) {bad}; "
            f"expected keys from {list(CONFLICT_SUBSCORES)}"
        )
    return {k: float(given.get(k, 0.0)) for k in CONFLICT_SUBSCORES}


def combine_conflict(
    subs: Mapping[str, float | None],
    weights: Mapping[str, float],
    gaps: Sequence[DataGap] = (),
) -> FactorResult:
    """Conflict component from the available sub-scores (Req 7.4, 7.6)."""
    wm = weighted_mean_available(subs, weights)
    out = FactorResult(score=wm.value, omitted=list(wm.omitted))
    out.extra[CONFLICT_SUBSCORES_EXTRA] = dict(subs)
    if wm.value is None:
        sources = sorted({g.source for g in gaps}) or [
            NEODP_VESSEL_TRAFFIC.source,
            EMOLT.source,
            NEODP_DIADROMOUS.source,
            NOAA_FISHERIES_RIGHT_WHALE.source,
        ]
        reasons = {g.reason for g in gaps}
        reason = reasons.pop() if len(reasons) == 1 else "fetch failure"
        out.gaps.append(
            DataGap(
                CONFLICT_FACTOR,
                ",".join(sources),
                reason,
                "every conflict sub-score is null",
            )
        )
    return out


def _as_gap(part: Any, source: str) -> DataGap | None:
    """Run-wide gap for a failed part or a hand-built Layer with no geometry."""
    if isinstance(part, DataGap):
        return part
    if isinstance(part, Layer) and len(part) == 0:
        return DataGap("*", source, "empty response", "no geometries")
    return None


def _gap_factor(
    name: str, unit: str | None, tags: list, cit: Citation, gap: DataGap
) -> Factor:
    return Factor(name, None, unit, tags, cit, gap=gap.for_factor(name))


# ------------------------------------------------------------------- scoring


def score_conflict(
    cells: Any,
    inputs: ConflictInputs,
    cfg: DeviceConfig,
    exclusion_results: Mapping[str, FactorResult] | None = None,
) -> dict[str, FactorResult]:
    """Conflict :class:`FactorResult` per ``cell_id`` (Req 7.4-7.6).

    ``exclusion_results`` is :func:`exclusions` output. When given, each cell's
    result extends it (Hard_Exclusion fields and layer gaps kept), and cells
    with ``hard_exclusion`` get factors but ``score=None`` and no conflict gap.
    Every factor is tagged and cited; its value is null iff it carries a gap.
    """
    ids = [str(c) for c in cells["cell_id"]]
    if not ids:
        return {}
    n = len(ids)
    pts = centroids_utm(cells)
    weights = conflict_weights(cfg)

    # vessel_traffic -------------------------------------------------------
    v_tags = tags_for(PROXY, NEODP_VESSEL_TRAFFIC.source)
    v_gap = _as_gap(inputs.vessel, NEODP_VESSEL_TRAFFIC.source)
    if v_gap is None:
        vs = inputs.vessel
        assert isinstance(vs, VesselSamples)
        if len(vs.values) != n:
            raise ValueError(f"vessel samples: {len(vs.values)} values for {n} cells")
        v_pct = percentile_ranks(vs.values)
        v_cit = vs.citation
    else:
        v_cit = neodp.vessel_citation()

    # fishing_activity -----------------------------------------------------
    f_tags = tags_for(PROXY, EMOLT.source)
    f_gap = _as_gap(inputs.fishing, EMOLT.source)
    if f_gap is None:
        fp = inputs.fishing
        assert isinstance(fp, FishingPoints)
        f_counts = fishing_counts(pts, fp)
        f_pct = percentile_ranks([float(c) for c in f_counts])
        f_cit = fp.citation
    else:
        f_cit = emolt.emolt_citation()

    # diadromous_fish_km ---------------------------------------------------
    d_tags = tags_for(PROXY, NEODP_DIADROMOUS.source)
    d_gap = _as_gap(inputs.fish_stations, NEODP_DIADROMOUS.source)
    if d_gap is None:
        fs = inputs.fish_stations
        assert isinstance(fs, Layer)
        d_km = nearest_distances_km(pts, fs.geoms_utm)
        d_cit = fs.citation
    else:
        d_cit = citation_for(neodp.DIADROMOUS_CITATION_ID, NEODP_DIADROMOUS)

    # whale_critical_habitat -----------------------------------------------
    w_tags = tags_for(QUANTIFIED, NOAA_FISHERIES_RIGHT_WHALE.source)
    w_gap = _as_gap(inputs.whale, NOAA_FISHERIES_RIGHT_WHALE.source)
    if w_gap is None:
        wl = inputs.whale
        assert isinstance(wl, Layer)
        w_hits = intersecting_cells(cell_polygons_utm(cells), wl.geoms_utm)
        w_cit = wl.citation
    else:
        w_cit = citation_for(WHALE_CITATION_ID, NOAA_FISHERIES_RIGHT_WHALE)

    out: dict[str, FactorResult] = {}
    for k, cid in enumerate(ids):
        factors: list[Factor] = []
        subs: dict[str, float | None] = dict.fromkeys(CONFLICT_SUBSCORES)

        if v_gap is not None:
            factors.append(
                _gap_factor(VESSEL_TRAFFIC, PERCENTILE, v_tags, v_cit, v_gap)
            )
        elif v_pct[k] is None:
            g = vs.gaps[k] or DataGap(
                "*", NEODP_VESSEL_TRAFFIC.source, "no coverage", "no pixel value"
            )
            factors.append(_gap_factor(VESSEL_TRAFFIC, PERCENTILE, v_tags, v_cit, g))
        else:
            subs[VESSEL_TRAFFIC] = v_pct[k]
            factors.append(
                Factor(
                    VESSEL_TRAFFIC,
                    v_pct[k],
                    PERCENTILE,
                    v_tags,
                    v_cit,
                    note=f"AIS transit raster value {vs.values[k]:g} at centroid",
                )
            )

        if f_gap is not None:
            factors.append(
                _gap_factor(FISHING_ACTIVITY, PERCENTILE, f_tags, f_cit, f_gap)
            )
        else:
            subs[FISHING_ACTIVITY] = f_pct[k]
            factors.append(
                Factor(
                    FISHING_ACTIVITY,
                    f_pct[k],
                    PERCENTILE,
                    f_tags,
                    f_cit,
                    note=f"{int(f_counts[k])} eMOLT fishing operations "
                    f"within {FISHING_RADIUS_KM:g} km",
                )
            )

        if d_gap is not None:
            factors.append(_gap_factor(DIADROMOUS_FISH_KM, "km", d_tags, d_cit, d_gap))
        else:
            dk = float(d_km[k])
            subs[DIADROMOUS_FISH] = diadromous_subscore(dk, cfg)
            factors.append(
                Factor(
                    DIADROMOUS_FISH_KM,
                    dk,
                    "km",
                    d_tags,
                    d_cit,
                    note="proximity only, never a hard exclusion",
                )
            )

        if w_gap is not None:
            factors.append(
                _gap_factor(WHALE_CRITICAL_HABITAT, None, w_tags, w_cit, w_gap)
            )
        else:
            w = 100.0 if k in w_hits else 0.0
            subs[WHALE_CRITICAL_HABITAT] = w
            factors.append(
                Factor(WHALE_CRITICAL_HABITAT, w, None, w_tags, w_cit, note=WHALE_NOTE)
            )

        ex = (exclusion_results or {}).get(cid)
        excluded = bool(ex is not None and ex.extra.get(HARD_EXCLUSION))
        if excluded:
            res = FactorResult(score=None)
            res.extra[CONFLICT_SUBSCORES_EXTRA] = dict(subs)
        else:
            gaps = [f.gap for f in factors if f.gap is not None]
            res = combine_conflict(subs, weights, gaps)
        if ex is not None:
            res.factors = {**ex.factors, **res.factors}
            res.gaps = [*ex.gaps, *res.gaps]
            res.omitted = [*ex.omitted, *res.omitted]
            res.extra = {**ex.extra, **res.extra}
        for f in factors:
            res.add(f)
        out[cid] = res
    return out
