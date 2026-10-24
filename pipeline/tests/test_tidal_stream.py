"""Tidal-stream scoring (Req 4.2-4.7). No network."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from siting.config import DeviceConfig
from siting.fetch.gaps import DataGap
from siting.provenance import (
    MODELED_CURRENT,
    PREDICTED_CURRENT,
    QUANTIFIED,
    Citation,
    validate,
)
from siting.score.resource import (
    BELOW_EPRI,
    STREAM_FACTORS,
    DepthInput,
    FloodCell,
    power_density_wm2,
    qualifying_areas,
    rotor_swept_area_m2,
    score_tidal_stream,
    score_tidal_stream_grid,
    stream_inputs,
    tidal_stream_cf,
    tidal_stream_subscore,
)
from siting.sources.velocity import NECOFS_FVCOM, NOAA_CURRENTS, VelocitySeries

CFG = DeviceConfig()

NECOFS_CIT = Citation(
    id="necofs",
    agency="UMass Dartmouth / NOAA",
    dataset="NECOFS FVCOM GOM5 forecast",
    endpoint="http://www.smast.umassd.edu:8080/thredds/dodsC/models/fvcom/NECOFS",
    date="2025-01-10",
    date_kind="model_run",
)
GAP_CIT = Citation(
    id="velocity-gap",
    agency="UMass Dartmouth / NOAA",
    dataset="NECOFS FVCOM GOM5 forecast",
    endpoint="http://www.smast.umassd.edu:8080/thredds/dodsC/models/fvcom/NECOFS",
    date="2025-01-10",
    date_kind="model_run",
)
CRM_CIT = Citation(
    id="crm",
    agency="NOAA NCEI",
    dataset="Coastal Relief Model",
    endpoint="https://www.ngdc.noaa.gov/thredds/dodsC/crm/crm_vol1.nc",
    date="2025-01-10",
    date_kind="fetch",
)
COOPS_CUR_CIT = Citation(
    id="coops-currents",
    agency="NOAA CO-OPS",
    dataset="CO-OPS current predictions",
    endpoint="https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
    date="2025-01-10",
    date_kind="fetch",
)


def series(speeds, depth_m: float | None = 20.0, **kw) -> VelocitySeries:
    base = dict(
        source=NECOFS_FVCOM,
        t_start="2025-01-10T00:00:00Z",
        t_end="2025-01-13T00:00:00Z",
        speeds_ms=tuple(speeds),
        model_run_date="2025-01-10",
        citation=NECOFS_CIT,
        label=MODELED_CURRENT,
        dt_s=3600.0,
        depth_m=depth_m,
    )
    base.update(kw)
    return VelocitySeries(**base)


VGAP = DataGap("velocity_peak", NECOFS_FVCOM, "no coverage", "outside mesh")


def score_one(s, area_km2: float = 10.0, depth=None):
    ins = stream_inputs(s, CFG, gap_citation=GAP_CIT, depth=depth)
    return score_tidal_stream(ins, area_km2, CFG, gap_citation=GAP_CIT)


def assert_valid(res) -> None:
    cell = SimpleNamespace(cell_id="C1", factors=res.factors)
    assert validate([cell]) == []


# ------------------------------------------------------------------ physics


def test_power_density_is_cubic():
    assert power_density_wm2(1.0, 1025) == pytest.approx(512.5)
    assert power_density_wm2(2.0, 1025) == pytest.approx(8 * 512.5)


def test_subscore_mapping():
    p_bench = power_density_wm2(2.5, 1025)
    args = (500.0, 2.5, 1025.0, 40.0)
    assert tidal_stream_subscore(500.0, *args) == pytest.approx(40.0)
    assert tidal_stream_subscore(p_bench, *args) == pytest.approx(100.0)
    assert tidal_stream_subscore(p_bench * 3, *args) == pytest.approx(100.0)
    mid = (500.0 + p_bench) / 2
    assert tidal_stream_subscore(mid, *args) == pytest.approx(70.0)


def test_cf_mean_of_capped_power():
    a = rotor_swept_area_m2(5.0)
    assert a == pytest.approx(math.pi * 6.25)
    k = 0.5 * 1025 * a * 0.4
    # v=0 contributes 0; v=3 m/s is far above rated (100 kW) and caps at 1.
    v1 = 1.0
    expected = (0.0 + min(k * v1**3, 1e5) / 1e5 + 1.0) / 3
    assert tidal_stream_cf([0.0, v1, 3.0], 1025, 5.0, 0.4, 100.0) == pytest.approx(
        expected
    )
    assert tidal_stream_cf([10.0] * 4, 1025, 5.0, 0.4, 100.0) == 1.0


# --------------------------------------------------------------- flood fill


def test_flood_fill_edge_adjacent_groups():
    cells = [
        FloodCell(0, 0, 0.2, True, True),
        FloodCell(0, 1, 0.2, True, True),  # edge-adjacent to (0,0)
        FloodCell(1, 1, 0.2, True, True),  # edge-adjacent to (0,1)
        FloodCell(2, 2, 0.2, True, True),  # diagonal only → separate group
        FloodCell(0, 2, 0.2, False, True),  # fails power
        FloodCell(5, 5, 0.2, True, None),  # depth unknown → not qualifying
    ]
    areas = qualifying_areas(cells)
    assert areas[:3] == pytest.approx([0.6, 0.6, 0.6])
    assert areas[3] == pytest.approx(0.2)
    assert areas[4] == 0.0 and areas[5] == 0.0


def test_flood_fill_rejects_duplicate_index():
    with pytest.raises(ValueError):
        qualifying_areas([FloodCell(0, 0, 1, True, True)] * 2)


# ---------------------------------------------------------------- scoring


def test_passing_cell_scores_and_emits_factors():
    res = score_one(series([0.5, 1.5, 2.0]))
    p = power_density_wm2(2.0, CFG.seawater_density)
    assert res.score == pytest.approx(
        tidal_stream_subscore(p, 500.0, 2.5, CFG.seawater_density, 40.0)
    )
    assert 40.0 < res.score < 100.0
    assert set(res.factors) == set(STREAM_FACTORS)
    f = res.factors
    assert f["velocity_peak"].value == 2.0
    assert f["velocity_peak"].label == MODELED_CURRENT
    assert f["velocity_peak"].tags == [QUANTIFIED]
    assert f["velocity_source_period"].value == (
        "NECOFS_FVCOM 2025-01-10T00:00:00Z..2025-01-13T00:00:00Z"
    )
    assert f["power_density"].value == pytest.approx(p)
    assert f["depth"].value == 20.0
    assert f["qualifying_area"].value == 10.0
    cf = f["cf_stream"].value
    assert 0.0 <= cf <= 1.0
    assert "Cp 0.4" in f["cf_stream"].note
    assert f["energy_stream_kwh"].value == pytest.approx(CFG.rated_power_kw * cf * 8760)
    assert res.extra == {"stream_threshold_failed": [], "energy_reason": None}
    assert res.gaps == []
    assert_valid(res)


def test_all_thresholds_fail_named():
    res = score_one(series([0.2, 0.5], depth_m=3.0), area_km2=0.0)
    assert res.score == 0.0
    assert res.extra["stream_threshold_failed"] == ["power_density", "depth", "area"]
    assert res.extra["energy_reason"] == BELOW_EPRI
    assert res.factors["energy_stream_kwh"].value == 0.0
    assert res.factors["energy_stream_kwh"].note == BELOW_EPRI
    assert "cf_stream" not in res.factors
    assert_valid(res)


def test_area_only_failure():
    res = score_one(series([2.0]), area_km2=0.5)  # at the minimum → fails
    assert res.score == 0.0
    assert res.extra["stream_threshold_failed"] == ["area"]


def test_power_density_at_minimum_fails():
    v = (2 * 500.0 / CFG.seawater_density) ** (1 / 3)  # P == P_min exactly
    res = score_one(series([v]))
    assert "power_density" in res.extra["stream_threshold_failed"]
    assert res.score == 0.0


def test_velocity_gap_nulls_everything():
    res = score_one(VGAP)
    assert res.score is None
    for name in STREAM_FACTORS:
        fac = res.factors[name]
        assert fac.value is None and fac.gap is not None
        assert fac.gap.factor == name
    assert res.extra["energy_reason"] is None
    assert_valid(res)


def test_missing_depth_is_gap_and_nulls_subscore():
    res = score_one(series([2.0], depth_m=None))
    assert res.score is None
    assert res.factors["depth"].value is None
    assert res.factors["depth"].gap.reason == "no coverage"
    assert res.factors["cf_stream"].value is None
    assert res.factors["energy_stream_kwh"].value is None
    assert res.factors["power_density"].value is not None
    assert_valid(res)


def test_missing_depth_with_power_failure_still_zero():
    res = score_one(series([0.3], depth_m=None), area_km2=0.0)
    assert res.score == 0.0
    assert res.extra["stream_threshold_failed"] == ["power_density", "area"]
    assert_valid(res)


def test_external_depth_fallback_tagged_external():
    depth = DepthInput(12.0, "NCEI_CRM", CRM_CIT, "CRM at centroid")
    res = score_one(series([2.0], depth_m=None), depth=depth)
    assert res.factors["depth"].value == 12.0
    assert "EXTERNAL ADDITION" in res.factors["depth"].tags
    assert res.score is not None and res.score > 0
    assert_valid(res)


def test_noaa_predictions_label_passes_validator():
    s = series(
        [2.0],
        depth_m=None,
        source=NOAA_CURRENTS,
        citation=COOPS_CUR_CIT,
        label=PREDICTED_CURRENT,
        model_run_date=None,
    )
    depth = DepthInput(15.0, "NCEI_CRM", CRM_CIT)
    res = score_one(s, depth=depth)
    assert res.factors["velocity_peak"].label == PREDICTED_CURRENT
    assert_valid(res)


def test_grid_wrapper_flood_fills_across_cells():
    # Two adjacent 0.3 km² passing cells → 0.6 km² group, above 0.5.
    # An isolated passing 0.3 km² cell fails on area.
    cells = [(0, 0, 0.3), (0, 1, 0.3), (4, 4, 0.3)]
    out = score_tidal_stream_grid(cells, [series([2.0])] * 3, CFG, gap_citation=GAP_CIT)
    assert out[0].factors["qualifying_area"].value == pytest.approx(0.6)
    assert out[0].score > 0 and out[1].score > 0
    assert out[2].score == 0.0
    assert out[2].extra["stream_threshold_failed"] == ["area"]
