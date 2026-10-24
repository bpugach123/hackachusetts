"""Feasibility component score: depth, wave exposure, port distance (Req 9.1-9.6).

Sub-scores (names match ``feasibility_omitted`` in the schema):

* ``depth``: the cell's ``depth`` factor (NECOFS ``h`` at the nearest node,
  else the NCEI CRM fallback from ``sources/bathy.py``; the tidal-stream path
  already emits that factor, so it is read here, not re-emitted). Scored
  against the band of the device matching ``resource_path`` (tidal_stream ->
  ``seabed``, rise_fall -> ``buoy``; ``cfg.depth_bands``): 100 inside
  ``[lo, hi]``, else ``ramp(distance_outside, 0, 10, 100, 0)`` (Req 9.1). Both
  device scores are kept in ``extra``. With no ``resource_path`` there is no
  device to score against, so ``depth`` is omitted.
* ``slope``: STRETCH (task 15). Not computed yet, so always omitted.
* ``wave_exposure``: ``ramp(H_mean, 1, 3, 100, 0)`` on the NDBC mean WVHT from
  the same :class:`~siting.sources.ndbc.WaveRecord` used for wave context
  (``wave_hs_mean`` factor, already emitted by Resource). The buoy id goes in
  ``extra["feasibility_wave_buoy"]`` and in the ``wave_hs_mean`` note (Req 9.3).
* ``port_distance``: ``ramp(d, 10, 50, 100, 0)`` on the straight-line km from
  the centroid to the nearest USACE/BTS NTAD principal port; emitted here as
  the ``port_km`` factor, PROXY + EXTERNAL ADDITION (Req 9.4).

Thresholds come from ``cfg.feasibility`` (``wave_hs_m``, ``port_km``).
Component = equal-weight :func:`weighted_mean_available`; the left-out
sub-scores go in ``feasibility_omitted`` (Req 9.5). Every sub-score null ->
component null plus a ``feasibility`` DataGap (Req 9.6). Scores are not rounded
here (1 dp at write time).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from shapely import STRtree

from siting.config import DeviceConfig
from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import PROXY, Citation, Factor, FactorResult, tags_for
from siting.score.grid_proximity import centroids_utm
from siting.score.ramps import ramp, weighted_mean_available
from siting.sources import ports
from siting.sources.arcgis import Layer, citation_for
from siting.sources.endpoints import NTAD_PORTS
from siting.sources.ndbc import WaveRecord

FEASIBILITY_FACTOR = "feasibility"
DEPTH = "depth"
SLOPE = "slope"
WAVE = "wave_exposure"
PORT = "port_distance"
SUBSCORES: tuple[str, ...] = (DEPTH, SLOPE, WAVE, PORT)
EQUAL_WEIGHTS: dict[str, float] = {name: 1.0 for name in SUBSCORES}

PORT_KM = "port_km"
DEPTH_RAMP_M = 10.0  # Req 9.1: 0 at 10 m beyond the nearest band edge
PATH_DEVICE: dict[str, str] = {"tidal_stream": "seabed", "rise_fall": "buoy"}
DEFAULT_GAP_SOURCES: tuple[str, ...] = (
    "NECOFS_FVCOM",
    "NCEI_CRM",
    "NDBC",
    "NTAD_PORTS",
)


# ------------------------------------------------------------- sub-scores


def depth_score(depth_m: float, band: Sequence[float]) -> float:
    """100 inside ``[lo, hi]``; linear to 0 at 10 m beyond the nearest edge."""
    lo, hi = float(band[0]), float(band[1])
    if lo <= depth_m <= hi:
        return 100.0
    outside = lo - depth_m if depth_m < lo else depth_m - hi
    return ramp(outside, 0.0, DEPTH_RAMP_M, 100.0, 0.0)


def device_for_path(resource_path: str | None) -> str | None:
    """Depth-band device for a Resource path, or None when there is no path."""
    return PATH_DEVICE.get(resource_path) if resource_path else None


def wave_score(hs_mean_m: float, cfg: DeviceConfig) -> float:
    full, zero = cfg.feasibility["wave_hs_m"]
    return ramp(hs_mean_m, full, zero, 100.0, 0.0)


def port_score(d_km: float, cfg: DeviceConfig) -> float:
    full, zero = cfg.feasibility["port_km"]
    return ramp(d_km, full, zero, 100.0, 0.0)


# ---------------------------------------------------------------- port factor


@dataclass(frozen=True)
class PortMatch:
    name: str | None
    distance_km: float


def nearest_ports(points_utm: Any, layer: Layer) -> list[PortMatch]:
    """Nearest port per point (km rounded to 0.01; 0 inside a port polygon).

    Ties go to the lowest feature index so the result is deterministic.
    """
    geoms = np.asarray(list(layer.geoms_utm), dtype=object)
    pts = np.asarray(points_utm, dtype=object)
    tree = STRtree(geoms)
    (idx_in, idx_tree), dist = tree.query_nearest(
        pts, return_distance=True, all_matches=True
    )
    best: dict[int, tuple[int, float]] = {}
    for p, t, d in zip(idx_in.tolist(), idx_tree.tolist(), dist.tolist(), strict=True):
        if p not in best or t < best[p][0]:
            best[p] = (t, d)
    out: list[PortMatch] = []
    for p in range(pts.shape[0]):
        t, d = best[p]
        out.append(
            PortMatch(ports.port_name(layer.properties[t]), round(d / 1000.0, 2))
        )
    return out


def port_factor(
    layer: Layer | DataGap,
    match: PortMatch | None,
    gap_citation: Citation,
    *,
    margin_km: float = ports.PORT_MARGIN_KM,
) -> Factor:
    """``port_km`` factor: PROXY + EXTERNAL ADDITION, cites the port dataset."""
    tags = tags_for(PROXY, ports.SOURCE)
    if isinstance(layer, DataGap):
        return Factor(
            PORT_KM, None, "km", tags, gap_citation, gap=layer.for_factor(PORT_KM)
        )
    if match is None or match.distance_km > margin_km:
        # A port outside the query envelope could be nearer: do not report it.
        detail = f"no principal port within the {margin_km:g} km query margin"
        gap = DataGap(PORT_KM, ports.SOURCE, "no coverage", detail)
        return Factor(PORT_KM, None, "km", tags, layer.citation, gap=gap)
    name = match.name or "unnamed"
    return Factor(
        PORT_KM,
        float(match.distance_km),
        "km",
        tags,
        layer.citation,
        note=f"nearest principal port: {name}",
    )


def port_gap_citation() -> Citation:
    return citation_for(ports.CITATION_ID, NTAD_PORTS)


# --------------------------------------------------------------- combiner


def _depth_inputs(
    depth: Factor | None, resource_path: str | None, cfg: DeviceConfig
) -> tuple[float | None, dict[str, float | None], str | None, DataGap | None]:
    """(path sub-score, per-device scores, device, depth gap)."""
    per_device: dict[str, float | None] = {k: None for k in cfg.depth_bands}
    gap: DataGap | None = None
    d = depth.value if depth is not None else None
    if isinstance(d, (int, float)):
        for dev, band in cfg.depth_bands.items():
            per_device[dev] = depth_score(float(d), band)
    elif depth is not None and depth.gap is not None:
        gap = depth.gap
    device = device_for_path(resource_path)
    sub = per_device.get(device) if device is not None else None
    return sub, per_device, device, gap


def score_feasibility_cell(
    cfg: DeviceConfig,
    *,
    resource_path: str | None,
    depth: Factor | None,
    waves: WaveRecord | DataGap | None,
    port: Factor | None,
) -> FactorResult:
    """Feasibility for one cell (Req 9.1, 9.3-9.6).

    ``depth`` is the cell's ``depth`` factor (from the tidal-stream path) and
    ``waves`` the same NDBC record used for wave context; neither is re-added.
    ``port`` is the ``port_km`` factor, which is added to the result.
    """
    d_sub, per_device, device, d_gap = _depth_inputs(depth, resource_path, cfg)

    w_sub: float | None = None
    w_gap: DataGap | None = None
    buoy: str | None = None
    if isinstance(waves, WaveRecord):
        w_sub = wave_score(waves.hs_mean_m, cfg)
        buoy = waves.station.id
    elif isinstance(waves, DataGap):
        w_gap = waves.for_factor("wave_hs_mean")

    p_sub: float | None = None
    if port is not None and isinstance(port.value, (int, float)):
        p_sub = port_score(float(port.value), cfg)

    subs: dict[str, float | None] = {
        DEPTH: d_sub,
        SLOPE: None,  # STRETCH (task 15)
        WAVE: w_sub,
        PORT: p_sub,
    }
    wm = weighted_mean_available(subs, EQUAL_WEIGHTS)
    out = FactorResult(score=wm.value, omitted=list(wm.omitted))
    if port is not None:
        out.add(port)
    out.extra.update(
        feasibility_omitted=list(wm.omitted),
        feasibility_subscores=dict(subs),
        feasibility_device=device,
        depth_sub_by_device=per_device,
        feasibility_wave_buoy=buoy,
    )
    if wm.value is None:
        under = [g for g in (d_gap, w_gap, port.gap if port else None) if g]
        sources = sorted({g.source for g in under}) or list(DEFAULT_GAP_SOURCES)
        reasons = {g.reason for g in under}
        reason = reasons.pop() if len(reasons) == 1 else "no coverage"
        out.gaps.append(
            DataGap(
                FEASIBILITY_FACTOR,
                ",".join(sources),
                reason,
                "every feasibility sub-score is null",
            )
        )
    return out


# ------------------------------------------------------------------ batch


def fetch_ports(client: FetchClient, cells: Any) -> Layer | DataGap:
    """Principal ports over the Grid bbox + the port query margin."""
    return ports.fetch_for_cells(client, cells)


def port_factors(cells: Any, layer: Layer | DataGap) -> dict[str, Factor]:
    """``port_km`` factor per ``cell_id``."""
    ids = [str(c) for c in cells["cell_id"]]
    if not ids:
        return {}
    gap_cit = port_gap_citation()
    if isinstance(layer, DataGap):
        return {cid: port_factor(layer, None, gap_cit) for cid in ids}
    matches = nearest_ports(centroids_utm(cells), layer)
    return {
        cid: port_factor(layer, m, gap_cit) for cid, m in zip(ids, matches, strict=True)
    }


def score_feasibility(
    cells: Any,
    cfg: DeviceConfig,
    *,
    resource_paths: Mapping[str, str | None],
    depths: Mapping[str, Factor | None],
    waves: Mapping[str, WaveRecord | DataGap | None],
    port_layer: Layer | DataGap,
) -> dict[str, FactorResult]:
    """Feasibility :class:`FactorResult` per ``cell_id``.

    ``resource_paths``, ``depths`` and ``waves`` are keyed by ``cell_id`` and
    come from the Resource pass (``extra["resource_path"]``, the ``depth``
    factor, the :meth:`NdbcWaves.wave_at` record). Missing keys count as gaps.
    """
    pf = port_factors(cells, port_layer)
    out: dict[str, FactorResult] = {}
    for cid in pf:
        out[cid] = score_feasibility_cell(
            cfg,
            resource_path=resource_paths.get(cid),
            depth=depths.get(cid),
            waves=waves.get(cid),
            port=pf[cid],
        )
    return out
