"""Feasibility core (task 8.1): depth band, wave, port, combiner, CRM fallback.

Offline: ports are a hand-built Layer and the CRM read goes through a fake
OPeNDAP opener on a tiny synthetic grid.
"""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from shapely.geometry import Point as SPoint

from siting import provenance
from siting.config import DeviceConfig
from siting.fetch.client import FetchClient, TransportError, dataset_to_netcdf_bytes
from siting.fetch.gaps import DataGap
from siting.provenance import QUANTIFIED, Citation, Factor, tags_for
from siting.score import feasibility as fz
from siting.score.resource import DepthInput
from siting.sources import bathy
from siting.sources.arcgis import Layer, to_utm
from siting.sources.ndbc import Buoy, WaveRecord, WaveStats

CFG = DeviceConfig()
CIT = Citation("c", "Agency", "Dataset", "https://x/y", "2026-10-24", "fetch")


def depth_factor(v: float | None) -> Factor:
    gap = None if v is not None else DataGap("depth", "NECOFS_FVCOM", "no coverage")
    return Factor("depth", v, "m", tags_for(QUANTIFIED, "NECOFS_FVCOM"), CIT, gap=gap)


def wave(hs: float) -> WaveRecord:
    stats = WaveStats(hs, 7.0, 10, 10, "2026-09-10", "2026-10-24")
    return WaveRecord(Buoy("44013", "Boston", 42.35, -70.65), 12.0, stats, CIT)


def port(km: float | None) -> Factor:
    tags = tags_for(provenance.PROXY, "NTAD_PORTS")
    if km is None:
        g = DataGap("port_km", "NTAD_PORTS", "fetch failure")
        return Factor("port_km", None, "km", tags, CIT, gap=g)
    return Factor("port_km", km, "km", tags, CIT)


# ------------------------------------------------------------- sub-scores


@pytest.mark.parametrize(
    ("d", "want"),
    [(15, 100), (35, 100), (25, 100), (10, 50), (5, 0), (0, 0), (40, 50), (45, 0)],
)
def test_depth_score_seabed_band_and_10m_ramp(d, want):
    assert fz.depth_score(d, CFG.depth_bands["seabed"]) == pytest.approx(want)


def test_device_follows_resource_path():
    assert fz.device_for_path("tidal_stream") == "seabed"
    assert fz.device_for_path("rise_fall") == "buoy"
    assert fz.device_for_path(None) is None


def test_wave_and_port_ramps():
    assert fz.wave_score(1.0, CFG) == 100 and fz.wave_score(3.0, CFG) == 0
    assert fz.wave_score(2.0, CFG) == pytest.approx(50)
    assert fz.port_score(10, CFG) == 100 and fz.port_score(50, CFG) == 0
    assert fz.port_score(30, CFG) == pytest.approx(50)


# --------------------------------------------------------------- combiner


def test_equal_weight_mean_with_slope_omitted():
    res = fz.score_feasibility_cell(
        CFG,
        resource_path="tidal_stream",
        depth=depth_factor(20.0),
        waves=wave(2.0),
        port=port(30.0),
    )
    assert res.score == pytest.approx((100 + 50 + 50) / 3)
    assert res.extra["feasibility_omitted"] == ["slope"]
    assert res.extra["feasibility_device"] == "seabed"
    assert res.extra["feasibility_wave_buoy"] == "44013"
    assert "port_km" in res.factors and res.gaps == []


def test_rise_fall_scores_depth_against_buoy_band():
    res = fz.score_feasibility_cell(
        CFG,
        resource_path="rise_fall",
        depth=depth_factor(20.0),
        waves=wave(1.0),
        port=port(10.0),
    )
    # 20 m is 5 m below the 5-15 m buoy band -> 50
    assert res.extra["feasibility_subscores"]["depth"] == pytest.approx(50)
    assert res.extra["depth_sub_by_device"] == {"seabed": 100.0, "buoy": 50.0}
    assert res.score == pytest.approx((50 + 100 + 100) / 3)


def test_missing_subscores_listed_and_left_out():
    res = fz.score_feasibility_cell(
        CFG,
        resource_path="tidal_stream",
        depth=depth_factor(None),
        waves=DataGap("wave_power", "NDBC", "no coverage"),
        port=port(10.0),
    )
    assert res.score == 100
    assert res.extra["feasibility_omitted"] == ["depth", "slope", "wave_exposure"]
    assert not any(g.factor == "feasibility" for g in res.gaps)


def test_all_gaps_gives_null_and_feasibility_gap():
    res = fz.score_feasibility_cell(
        CFG,
        resource_path="tidal_stream",
        depth=depth_factor(None),
        waves=DataGap("wave_power", "NDBC", "fetch failure"),
        port=port(None),
    )
    assert res.score is None
    assert res.extra["feasibility_omitted"] == list(fz.SUBSCORES)
    (g,) = [g for g in res.gaps if g.factor == "feasibility"]
    assert set(g.source.split(",")) == {"NDBC", "NECOFS_FVCOM", "NTAD_PORTS"}
    assert res.factors["port_km"].value is None


# ------------------------------------------------------------- port factor


def _ports_layer() -> Layer:
    boston = SPoint(-71.05, 42.35).buffer(0.01)
    nb = SPoint(-70.92, 41.63).buffer(0.01)
    cit = Citation(
        "bts-ports",
        "USACE WCSC / BTS NTAD",
        "Principal Ports",
        "https://geo.dot.gov/x/query",
        "2026-10-24",
        "fetch",
    )
    return Layer(
        "NTAD_PORTS",
        [boston, nb],
        [to_utm(boston), to_utm(nb)],
        [{"portname": "Boston, MA"}, {"portname": "New Bedford, MA"}],
        cit,
        "u",
    )


def test_port_factors_nearest_named_and_valid_provenance():
    cells = {
        "cell_id": ["A", "B"],
        "centroid_lat": [42.35, 41.60],
        "centroid_lon": [-70.70, -70.90],
    }
    pf = fz.port_factors(cells, _ports_layer())
    assert pf["A"].note == "nearest principal port: Boston, MA"
    assert 27 < pf["A"].value < 30
    assert pf["B"].note == "nearest principal port: New Bedford, MA"
    assert pf["A"].tags == ["PROXY", "EXTERNAL ADDITION"]
    recs = [{"cell_id": k, "factors": {"port_km": f}} for k, f in pf.items()]
    assert provenance.validate(recs) == []


def test_port_layer_gap_gives_null_factor_on_every_cell():
    cells = {"cell_id": ["A"], "centroid_lat": [42.35], "centroid_lon": [-70.7]}
    pf = fz.port_factors(cells, DataGap("*", "NTAD_PORTS", "fetch failure"))
    assert pf["A"].value is None and pf["A"].gap.factor == "port_km"
    assert provenance.validate([{"cell_id": "A", "factors": pf}]) == []


# ------------------------------------------------------------- CRM fallback


def _crm() -> xr.Dataset:
    x = np.round(np.arange(-71.0, -70.9, 0.001), 6)
    y = np.round(np.arange(42.0, 42.1, 0.001), 6)
    z = np.full((y.size, x.size), -20.0)
    z[:, x > -70.95] = 3.0  # land on the east half
    return xr.Dataset({"z": (("y", "x"), z)}, coords={"x": x, "y": y})


def _client(tmp_path, fail: bool = False) -> FetchClient:
    def opener(url, select):
        if fail:
            raise TransportError("OPeNDAP: down")
        return dataset_to_netcdf_bytes(select(_crm()).load())

    return FetchClient(tmp_path, opendap_open=opener, sleep=lambda s: None)


def test_crm_depth_is_negated_z_and_land_is_gap(tmp_path):
    sea, land = (42.05, -70.98), (42.05, -70.92)
    got = bathy.fetch_depths(_client(tmp_path), [sea, land])
    assert isinstance(got[0], DepthInput)
    assert got[0].depth_m == pytest.approx(20.0)
    assert got[0].source == "NCEI_CRM" and got[0].citation.id == "ncei-crm"
    assert isinstance(got[1], DataGap) and got[1].reason == "no coverage"


def test_crm_fetch_failure_is_gap_for_every_point(tmp_path):
    got = bathy.fetch_depths(_client(tmp_path, fail=True), [(42.05, -70.98)] * 2)
    assert all(isinstance(g, DataGap) and g.reason == "fetch failure" for g in got)


def test_fallback_only_where_necofs_has_no_depth(tmp_path):
    class S:
        def __init__(self, d):
            self.depth_m = d

    pts = [(42.05, -70.98), (42.05, -70.98)]
    got = bathy.fallback_depths(_client(tmp_path), pts, [S(12.0), S(None)])
    assert got[0] is None and isinstance(got[1], DepthInput)
