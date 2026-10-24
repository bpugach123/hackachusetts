"""Resource score (Req 4, 5).

Sections, kept separate so each path can be built and tested on its own:

* Shared physics helpers (annual energy from CF).
* Tidal stream (Req 4.2-4.7): this module, below.
* Rise/fall (Req 5.1-5.4, 5.9): this module, below.
* Wave context (Req 5.6) and the Resource combiner (Req 5.7-5.8): this module,
  at the end.

Formulas (design: Resource section):

* ``P = 0.5 · rho · v_peak³`` (W/m²). Thresholds P > P_min, depth > D_min,
  qualifying group area > A_min; then sub-score =
  ``ramp(P, P_min, P_bench, baseline_credit, 100)``, ``P_bench = 0.5·rho·v_bench³``.
* ``CF_stream = mean_t(min(0.5·rho·A·Cp·v_t³, P_rated) / P_rated)``, clamped,
  ``A = π (D/2)²``.
* ``E_cycle_J = 0.5 · rho · g · pad_area · R²`` with g = 9.81 m/s².
* Rise/fall sub-score = ``ramp(R², R_min², R_max², 0, 100)``.
* ``CF = (E_cycle_J / 3.6e6 · eta · cycles_per_year) / (rated_kw · 8760)``,
  clamped to [0, 1]. ``energy_kwh = rated_kw · CF · 8760``.
* Wave power ``P = rho · g² / (64π) · H² · T / 1000`` (kW/m), context only.
* Resource = max of the non-null stream / rise-fall sub-scores.

Values are not rounded here; rounding happens at write time.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from siting.config import DeviceConfig
from siting.fetch.gaps import DataGap
from siting.provenance import (
    MODELED_CURRENT,
    QUANTIFIED,
    VELOCITY_PREFIX,
    Citation,
    Factor,
    FactorResult,
    is_known_source,
    tags_for,
)
from siting.score.ramps import ramp
from siting.sources.coops import SOURCE as COOPS_SOURCE
from siting.sources.coops import TidalRange
from siting.sources.ndbc import SOURCE as NDBC_SOURCE
from siting.sources.ndbc import WaveRecord
from siting.sources.velocity import NECOFS_FVCOM, VelocitySeries

G = 9.81  # m/s², Req 5.3
J_PER_KWH = 3.6e6
HOURS_PER_YEAR = 8760.0

# ------------------------------------------------------------- shared helpers


def clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


def annual_energy_kwh(rated_kw: float, cf: float) -> float:
    """Annual energy in kWh/yr = rated_kw · CF · 8760 (Req 4.6, 5.9)."""
    return rated_kw * cf * HOURS_PER_YEAR


# --------------------------------------------------------------- tidal stream
#
# Flow (Req 4.2-4.7). The area threshold needs every cell at once, so scoring
# runs in three steps:
#
# 1. :func:`stream_inputs` per cell: peak speed, power density, depth, and
#    whether the power-density and depth thresholds pass.
# 2. :func:`qualifying_areas` over the whole grid: edge-adjacent (i±1 / j±1)
#    flood fill over cells passing both thresholds; each cell gets its group's
#    total area in km² (0 for cells outside any group).
# 3. :func:`score_tidal_stream` per cell: thresholds, sub-score, CF, energy.
#
# :func:`score_tidal_stream_grid` chains all three.
#
# Depth handling (no fabrication, Req 2.7). Depth comes from the velocity
# source when it carries one (NECOFS ``h`` at the nearest node, the design's
# primary depth) or else from an externally supplied ``depth`` (the CRM
# fallback, task 8.1). With neither, the ``depth`` factor is a DataGap and the
# depth threshold cannot be evaluated:
#
# * if power density already fails, the sub-score is still 0 (a known failure);
# * otherwise the sub-score, CF and energy are null with the depth gap, rather
#   than assuming the cell passes. The cell is not counted as qualifying in the
#   flood fill, so neighbors' group areas are conservative.
#
# Cells that fail power density or depth are in no qualifying group, so their
# qualifying area is 0 km², which is also at or below the area minimum; "area"
# is then listed alongside the other failures (Req 4.4: every failed threshold).

STREAM_FACTORS: tuple[str, ...] = (
    "velocity_peak",
    "velocity_source_period",
    "power_density",
    "depth",
    "qualifying_area",
    "cf_stream",
    "energy_stream_kwh",
)
BELOW_EPRI = "below EPRI thresholds"
THRESHOLD_ORDER: tuple[str, ...] = ("power_density", "depth", "area")

_STREAM_UNITS: dict[str, str | None] = {
    "velocity_peak": "m/s",
    "velocity_source_period": None,
    "power_density": "W/m²",
    "depth": "m",
    "qualifying_area": "km²",
    "cf_stream": None,
    "energy_stream_kwh": "kWh/yr",
}


def power_density_wm2(v_ms: float, seawater_density: float) -> float:
    """Tidal-stream power density in W/m²: 0.5·rho·v³ (Req 4.3). Cubic in v."""
    return 0.5 * seawater_density * v_ms**3


def rotor_swept_area_m2(rotor_diameter_m: float) -> float:
    """π·(D/2)² in m²."""
    return math.pi * (rotor_diameter_m / 2.0) ** 2


def tidal_stream_subscore(
    p_wm2: float,
    min_power_density_wm2: float,
    benchmark_velocity_ms: float,
    seawater_density: float,
    baseline_credit: float,
) -> float:
    """``ramp(P, P_min, P_bench, baseline_credit, 100)`` for a passing cell (Req 4.5).

    Callers apply the thresholds first; this only maps power density to score.
    """
    p_bench = power_density_wm2(benchmark_velocity_ms, seawater_density)
    return ramp(p_wm2, min_power_density_wm2, p_bench, baseline_credit, 100.0)


def tidal_stream_cf(
    speeds_ms: Sequence[float],
    seawater_density: float,
    rotor_diameter_m: float,
    power_coefficient: float,
    rated_power_kw: float,
) -> float:
    """Mean over time steps of min(0.5·rho·A·Cp·v³, P_rated)/P_rated, clamped (4.6)."""
    if not speeds_ms:
        raise ValueError("tidal_stream_cf needs at least one speed")
    area = rotor_swept_area_m2(rotor_diameter_m)
    p_rated_w = rated_power_kw * 1000.0
    k = 0.5 * seawater_density * area * power_coefficient
    total = sum(min(k * v**3, p_rated_w) / p_rated_w for v in speeds_ms)
    return clamp01(total / len(speeds_ms))


@dataclass(frozen=True)
class DepthInput:
    """An externally supplied centroid depth in m (positive down), e.g. CRM."""

    depth_m: float
    source: str
    citation: Citation
    note: str | None = None


@dataclass(frozen=True)
class FloodCell:
    """Flood-fill input: grid indices, cell area, and the two per-cell tests.

    ``passes_depth`` may be None (depth unknown); such a cell does not qualify.
    """

    i: int
    j: int
    area_km2: float
    passes_power: bool
    passes_depth: bool | None


@dataclass(frozen=True)
class StreamInputs:
    """Per-cell tidal-stream inputs before the area threshold (step 1)."""

    series: VelocitySeries | DataGap
    peak_ms: float | None
    power_density_wm2: float | None
    depth: Factor
    passes_power: bool | None  # None on a velocity gap
    passes_depth: bool | None  # None when depth is a gap

    @property
    def qualifies(self) -> bool:
        return bool(self.passes_power) and self.passes_depth is True


def qualifying_areas(cells: Sequence[FloodCell]) -> list[float]:
    """Total km² of each cell's edge-adjacent qualifying group (Req 4.4).

    A cell qualifies when it passes both the power-density and depth thresholds.
    Groups are 4-connected over grid (i, j). Returns one value per input cell,
    in order; a non-qualifying cell gets 0.0. Duplicate (i, j) raise.
    """
    index: dict[tuple[int, int], int] = {}
    for n, c in enumerate(cells):
        key = (c.i, c.j)
        if key in index:
            raise ValueError(f"duplicate grid index {key}")
        index[key] = n

    def ok(n: int) -> bool:
        c = cells[n]
        return c.passes_power and c.passes_depth is True

    out = [0.0] * len(cells)
    seen = [False] * len(cells)
    for start in range(len(cells)):
        if seen[start] or not ok(start):
            continue
        group: list[int] = []
        stack = [start]
        seen[start] = True
        while stack:
            n = stack.pop()
            group.append(n)
            c = cells[n]
            for key in ((c.i + 1, c.j), (c.i - 1, c.j), (c.i, c.j + 1), (c.i, c.j - 1)):
                m = index.get(key)
                if m is not None and not seen[m] and ok(m):
                    seen[m] = True
                    stack.append(m)
        total = sum(cells[n].area_km2 for n in group)
        for n in group:
            out[n] = total
    return out


def _known_source(source: str, fallback: str) -> str:
    return source if is_known_source(source) else fallback


def _stream_factor(
    name: str,
    value: float | str | None,
    *,
    source: str,
    citation: Citation,
    label: str | None,
    gap: DataGap | None = None,
    note: str | None = None,
) -> Factor:
    return Factor(
        name=name,
        value=value,
        unit=_STREAM_UNITS[name],
        tags=tags_for(QUANTIFIED, source),
        citation=citation,
        gap=gap,
        label=label,
        note=note,
    )


def _depth_factor(
    series: VelocitySeries | DataGap,
    depth: DepthInput | DataGap | None,
    gap_citation: Citation,
) -> Factor:
    """Velocity-source depth first (NECOFS h), then the external depth, else a gap."""
    if isinstance(series, VelocitySeries) and series.depth_m is not None:
        return _stream_factor(
            "depth",
            float(series.depth_m),
            source=series.source,
            citation=series.citation,
            label=None,
            note=f"{series.source} h at nearest node",
        )
    if isinstance(depth, DepthInput):
        return _stream_factor(
            "depth",
            float(depth.depth_m),
            source=depth.source,
            citation=depth.citation,
            label=None,
            note=depth.note,
        )
    if isinstance(depth, DataGap):
        g = depth.for_factor("depth")
        return _stream_factor(
            "depth",
            None,
            source=_known_source(g.source, NECOFS_FVCOM),
            citation=gap_citation,
            label=None,
            gap=g,
        )
    src = series.source if isinstance(series, VelocitySeries) else NECOFS_FVCOM
    cit = series.citation if isinstance(series, VelocitySeries) else gap_citation
    g = DataGap(
        "depth",
        _known_source(src, NECOFS_FVCOM),
        "no coverage",
        "velocity source gives no depth and no fallback depth was supplied",
    )
    return _stream_factor(
        "depth", None, source=g.source, citation=cit, label=None, gap=g
    )


def stream_inputs(
    series: VelocitySeries | DataGap,
    cfg: DeviceConfig,
    *,
    gap_citation: Citation,
    depth: DepthInput | DataGap | None = None,
) -> StreamInputs:
    """Step 1: peak, power density, depth and the per-cell threshold tests."""
    dfac = _depth_factor(series, depth, gap_citation)
    d = dfac.value if isinstance(dfac.value, float) else None
    passes_depth = None if d is None else d > cfg.min_depth_m
    if isinstance(series, DataGap):
        return StreamInputs(series, None, None, dfac, None, passes_depth)
    peak = float(series.peak_ms)
    p = power_density_wm2(peak, cfg.seawater_density)
    return StreamInputs(
        series, peak, p, dfac, p > cfg.min_power_density_wm2, passes_depth
    )


def _stream_gap(
    inputs: StreamInputs, gap: DataGap, gap_citation: Citation
) -> FactorResult:
    """Velocity gap: every velocity-derived value null with gaps (Req 4.2)."""
    out = FactorResult(score=None)
    src = _known_source(gap.source, NECOFS_FVCOM)
    for name in STREAM_FACTORS:
        if name == "depth":
            out.add(inputs.depth)
            continue
        label = MODELED_CURRENT if name.startswith(VELOCITY_PREFIX) else None
        out.add(
            _stream_factor(
                name,
                None,
                source=src,
                citation=gap_citation,
                label=label,
                gap=gap.for_factor(name),
            )
        )
    out.extra["stream_threshold_failed"] = []
    out.extra["energy_reason"] = None
    return out


def score_tidal_stream(
    inputs: StreamInputs,
    qualifying_area_km2: float,
    cfg: DeviceConfig,
    *,
    gap_citation: Citation,
) -> FactorResult:
    """Step 3: tidal-stream sub-score and factors for one cell (Req 4.2-4.7).

    ``qualifying_area_km2`` is this cell's entry from :func:`qualifying_areas`.
    ``extra`` carries ``stream_threshold_failed`` (subset of "power_density",
    "depth", "area"; empty unless the sub-score is 0 by threshold) and
    ``energy_reason`` ("below EPRI thresholds" or None).
    """
    series = inputs.series
    if isinstance(series, DataGap):
        return _stream_gap(inputs, series, gap_citation)

    assert inputs.peak_ms is not None and inputs.power_density_wm2 is not None
    src, cit, label = series.source, series.citation, series.label
    period = f"{series.source} {series.t_start}..{series.t_end}"

    failed: list[str] = []
    if not inputs.passes_power:
        failed.append("power_density")
    if inputs.passes_depth is False:
        failed.append("depth")
    if qualifying_area_km2 <= cfg.min_qualifying_area_km2 and (
        inputs.passes_depth is not None or failed
    ):
        failed.append("area")

    depth_gap = inputs.depth.gap if inputs.passes_depth is None else None
    if failed:
        sub: float | None = 0.0
    elif depth_gap is not None:
        sub = None
    else:
        sub = tidal_stream_subscore(
            inputs.power_density_wm2,
            cfg.min_power_density_wm2,
            cfg.benchmark_velocity_ms,
            cfg.seawater_density,
            cfg.baseline_credit,
        )

    out = FactorResult(score=sub)
    kw = {"source": src, "citation": cit}
    out.add(_stream_factor("velocity_peak", inputs.peak_ms, label=label, **kw))
    out.add(_stream_factor("velocity_source_period", period, label=label, **kw))
    out.add(
        _stream_factor("power_density", inputs.power_density_wm2, label=label, **kw)
    )
    out.add(inputs.depth)
    if depth_gap is not None and not failed:
        # Area cannot be judged without depth; leave it null rather than guess.
        out.add(
            _stream_factor(
                "qualifying_area",
                None,
                label=None,
                gap=depth_gap.for_factor("qualifying_area"),
                source=_known_source(depth_gap.source, src),
                citation=cit,
            )
        )
    else:
        out.add(
            _stream_factor(
                "qualifying_area", float(qualifying_area_km2), label=None, **kw
            )
        )

    energy_reason: str | None = None
    if sub is None:
        g = depth_gap
        assert g is not None
        for name in ("cf_stream", "energy_stream_kwh"):
            out.add(
                _stream_factor(
                    name,
                    None,
                    label=label,
                    gap=g.for_factor(name),
                    source=_known_source(g.source, src),
                    citation=cit,
                )
            )
    elif sub > 0:
        cf = tidal_stream_cf(
            series.speeds_ms,
            cfg.seawater_density,
            cfg.rotor_diameter_m,
            cfg.power_coefficient,
            cfg.rated_power_kw,
        )
        area = rotor_swept_area_m2(cfg.rotor_diameter_m)
        cf_note = (
            f"Cp {cfg.power_coefficient:g}, A {area:.3f} m², "
            f"rho {cfg.seawater_density:g} kg/m³, {series.t_start}..{series.t_end}"
        )
        out.add(_stream_factor("cf_stream", cf, label=label, note=cf_note, **kw))
        energy = annual_energy_kwh(cfg.rated_power_kw, cf)
        out.add(_stream_factor("energy_stream_kwh", energy, label=label, **kw))
    else:
        # Sub-score 0 by threshold: no CF (Req 4.6 applies above 0), energy 0.
        energy_reason = BELOW_EPRI
        out.add(
            _stream_factor("energy_stream_kwh", 0.0, label=label, note=BELOW_EPRI, **kw)
        )

    out.extra["stream_threshold_failed"] = failed
    out.extra["energy_reason"] = energy_reason
    return out


def score_tidal_stream_grid(
    cells: Sequence[tuple[int, int, float]],
    series: Sequence[VelocitySeries | DataGap],
    cfg: DeviceConfig,
    *,
    gap_citation: Citation,
    depths: Sequence[DepthInput | DataGap | None] | None = None,
) -> list[FactorResult]:
    """All three steps over a grid. ``cells`` are ``(i, j, area_km2)``.

    ``series`` (from :func:`siting.sources.velocity.velocity_series`) and
    ``depths`` align with ``cells``. ``gap_citation`` is cited by gap factors.
    """
    n = len(cells)
    if len(series) != n or (depths is not None and len(depths) != n):
        raise ValueError("cells, series and depths must have the same length")
    ins = [
        stream_inputs(
            s,
            cfg,
            gap_citation=gap_citation,
            depth=None if depths is None else depths[k],
        )
        for k, s in enumerate(series)
    ]
    flood = [
        FloodCell(i, j, area, bool(x.passes_power), x.passes_depth)
        for (i, j, area), x in zip(cells, ins, strict=True)
    ]
    areas = qualifying_areas(flood)
    return [
        score_tidal_stream(x, a, cfg, gap_citation=gap_citation)
        for x, a in zip(ins, areas, strict=True)
    ]


# ------------------------------------------------------------------ rise/fall

RISE_FALL_FACTORS: tuple[str, ...] = (
    "tidal_range",
    "coops_station",
    "energy_cycle_j",
    "cf_rise_fall",
    "energy_rise_fall_kwh",
)
BELOW_MIN_RANGE = "tidal range at or below minimum"


def energy_cycle_j(
    range_m: float, seawater_density: float, pad_area_m2: float
) -> float:
    """Potential energy per tidal cycle in J: 0.5·rho·g·A·R² (Req 5.3)."""
    return 0.5 * seawater_density * G * pad_area_m2 * range_m**2


def rise_fall_subscore(range_m: float, min_range_m: float, max_range_m: float) -> float:
    """0 at R <= R_min, linear in R² to 100 at R_max, capped above (Req 5.4)."""
    return ramp(range_m**2, min_range_m**2, max_range_m**2, 0.0, 100.0)


def rise_fall_cf(
    e_cycle_j: float,
    conversion_efficiency: float,
    cycles_per_year: float,
    rated_kw: float,
) -> float:
    """Rise/fall Derived_Capacity_Factor, clamped to [0, 1] (Req 5.9)."""
    annual_kwh = e_cycle_j / J_PER_KWH * conversion_efficiency * cycles_per_year
    return clamp01(annual_kwh / (rated_kw * HOURS_PER_YEAR))


def _factor(
    name: str,
    value: float | str | None,
    unit: str | None,
    citation: Citation,
    gap: DataGap | None = None,
    note: str | None = None,
) -> Factor:
    return Factor(
        name=name,
        value=value,
        unit=unit,
        tags=tags_for(QUANTIFIED, COOPS_SOURCE),
        citation=citation,
        gap=gap,
        note=note,
    )


_UNITS: dict[str, str | None] = {
    "tidal_range": "m",
    "coops_station": None,
    "energy_cycle_j": "J",
    "cf_rise_fall": None,
    "energy_rise_fall_kwh": "kWh/yr",
}


def _rise_fall_gap(
    gap: DataGap, harcon_citation: Citation, datums_citation: Citation
) -> FactorResult:
    """Every rise/fall value null with the gap attached; nothing fabricated (5.2)."""
    out = FactorResult(score=None)
    for name in RISE_FALL_FACTORS:
        cit = harcon_citation if name == "coops_station" else datums_citation
        out.add(_factor(name, None, _UNITS[name], cit, gap=gap.for_factor(name)))
    return out


def score_rise_fall(
    tides: TidalRange | DataGap,
    cfg: DeviceConfig,
    *,
    harcon_citation: Citation,
    datums_citation: Citation,
) -> FactorResult:
    """Rise/fall sub-score and factors for one cell (Req 5.1-5.4, 5.9).

    ``tides`` comes from :meth:`siting.sources.coops.CoopsTides.tidal_range_at`.
    The citations are used for gap factors (a gap still needs a citation); a
    successful lookup carries its own.

    ``score`` is the rise/fall sub-score. When it is 0, energy is 0 kWh/yr with
    the reason "tidal range at or below minimum" and no CF is stored, since
    Req 5.9 defines the CF only for a sub-score above 0.
    """
    if isinstance(tides, DataGap):
        return _rise_fall_gap(tides, harcon_citation, datums_citation)

    st = tides.station
    r = tides.range_m
    e_cycle = energy_cycle_j(r, cfg.seawater_density, cfg.pad_area_m2)
    sub = rise_fall_subscore(r, cfg.min_range_m, cfg.max_range_m)

    m2 = tides.harcon.m2_amplitude_m
    m2_note = "M2 n/a" if m2 is None else f"M2 {m2:.3f} m"
    station_note = f"{st.name or st.id}, {tides.distance_km:.1f} km; {m2_note}"

    out = FactorResult(score=sub)
    dcit = tides.datums_citation
    out.add(_factor("tidal_range", r, "m", dcit, note=f"MN at {st.id}"))
    out.add(
        _factor("coops_station", st.id, None, tides.harcon_citation, note=station_note)
    )
    out.add(_factor("energy_cycle_j", e_cycle, "J", dcit))

    if sub > 0:
        cf = rise_fall_cf(
            e_cycle,
            cfg.conversion_efficiency,
            cfg.tidal_cycles_per_year,
            cfg.rated_power_kw,
        )
        cf_note = (
            f"E_cycle {e_cycle:.0f} J, eta {cfg.conversion_efficiency:g}, "
            f"{cfg.tidal_cycles_per_year:g} cycles/yr"
        )
        out.add(_factor("cf_rise_fall", cf, None, dcit, note=cf_note))
        energy = annual_energy_kwh(cfg.rated_power_kw, cf)
        out.add(_factor("energy_rise_fall_kwh", energy, "kWh/yr", dcit))
    else:
        out.add(
            _factor("energy_rise_fall_kwh", 0.0, "kWh/yr", dcit, note=BELOW_MIN_RANGE)
        )
    return out


# --------------------------------------------------------------- wave context
#
# Req 5.6: context only. Wave power is never blended into Resource; both factors
# carry "not blended". A gap (no buoy in range, fetch failure, all MM) gives
# null factors with the DataGap attached; nothing is estimated.

WAVE_FACTORS: tuple[str, ...] = ("wave_power", "wave_hs_mean")
NOT_BLENDED = "not blended"
_WAVE_UNITS: dict[str, str] = {"wave_power": "kW/m", "wave_hs_mean": "m"}


def wave_power_kwm(hs_m: float, period_s: float, seawater_density: float) -> float:
    """Deep-water wave energy flux in kW/m: rho·g²/(64π)·H²·T / 1000 (Req 5.6)."""
    return seawater_density * G**2 / (64.0 * math.pi) * hs_m**2 * period_s / 1000.0


def _wave_factor(
    name: str,
    value: float | None,
    citation: Citation,
    note: str,
    gap: DataGap | None = None,
) -> Factor:
    return Factor(
        name=name,
        value=value,
        unit=_WAVE_UNITS[name],
        tags=tags_for(QUANTIFIED, NDBC_SOURCE),
        citation=citation,
        gap=gap,
        note=note,
    )


def score_wave_context(
    waves: WaveRecord | DataGap,
    cfg: DeviceConfig,
    *,
    gap_citation: Citation,
) -> FactorResult:
    """``wave_power`` and ``wave_hs_mean`` factors for one cell (Req 5.6).

    ``waves`` comes from :meth:`siting.sources.ndbc.NdbcWaves.wave_at`.
    ``score`` is always None: wave power is excluded from Resource (Req 5.7).
    ``extra`` carries ``wave_buoy`` (id or None) and, on success, the means and
    record dates for reuse by Feasibility.
    """
    out = FactorResult(score=None)
    if isinstance(waves, DataGap):
        for name in WAVE_FACTORS:
            g = waves.for_factor(name)
            out.add(_wave_factor(name, None, gap_citation, NOT_BLENDED, gap=g))
        out.extra["wave_buoy"] = None
        return out

    s = waves.stats
    p = wave_power_kwm(s.hs_mean_m, s.dpd_mean_s, cfg.seawater_density)
    note = (
        f"{NOT_BLENDED}; buoy {waves.station.id} ({waves.distance_km:.1f} km), "
        f"{s.start}..{s.end}"
    )
    out.add(_wave_factor("wave_power", p, waves.citation, note))
    out.add(_wave_factor("wave_hs_mean", s.hs_mean_m, waves.citation, note))
    out.extra.update(
        wave_buoy=waves.station.id,
        wave_dpd_mean_s=s.dpd_mean_s,
        wave_record_start=s.start,
        wave_record_end=s.end,
    )
    return out


# ------------------------------------------------------------ Resource combiner

TIDAL_STREAM = "tidal_stream"
RISE_FALL = "rise_fall"
RESOURCE_FACTOR = "resource"


def _sub(x: float | FactorResult | None) -> tuple[float | None, list[DataGap]]:
    if isinstance(x, FactorResult):
        return x.score, list(x.gaps)
    return x, []


def combine_resource(
    stream: float | FactorResult | None,
    rise_fall: float | FactorResult | None,
) -> FactorResult:
    """Resource = max of the non-null sub-scores (Req 5.7, 5.8).

    Accepts the sub-score or the path's :class:`FactorResult`. Ties go to
    tidal_stream. ``extra["resource_path"]`` names the path (None when both are
    null, in which case ``score`` is None and a ``resource`` DataGap is added,
    built from the paths' own gaps when they are available).
    Wave power is not an input.
    """
    s, s_gaps = _sub(stream)
    r, r_gaps = _sub(rise_fall)
    out = FactorResult()
    if s is None and r is None:
        under = s_gaps + r_gaps
        sources = sorted({g.source for g in under}) or ["NECOFS_FVCOM", "NOAA_COOPS"]
        reasons = {g.reason for g in under}
        reason = reasons.pop() if len(reasons) == 1 else "no coverage"
        out.gaps.append(
            DataGap(
                RESOURCE_FACTOR,
                ",".join(sources),
                reason,
                "tidal-stream and rise/fall sub-scores both null",
            )
        )
        out.extra["resource_path"] = None
        return out
    if r is None or (s is not None and s >= r):
        out.score, path = s, TIDAL_STREAM
    else:
        out.score, path = r, RISE_FALL
    out.extra["resource_path"] = path
    return out
