"""Rise/fall: CO-OPS nearest station + range scoring (Req 5.1-5.4, 5.9). No network."""

from __future__ import annotations

import json

import pytest

from siting.config import DeviceConfig
from siting.fetch.client import EmptyResponse, FetchClient, Response
from siting.fetch.gaps import DataGap
from siting.provenance import validate
from siting.score.resource import (
    BELOW_MIN_RANGE,
    RISE_FALL_FACTORS,
    energy_cycle_j,
    rise_fall_cf,
    rise_fall_subscore,
    score_rise_fall,
)
from siting.sources.coops import (
    CoopsTides,
    Station,
    haversine_km,
    nearest_station,
    parse_datums,
    parse_harcon,
    parse_stations,
)

BOSTON = {"id": "8443970", "name": "Boston", "lat": 42.3539, "lng": -71.0503}
NANTUCKET = {
    "id": "8449130",
    "name": "Nantucket Island",
    "lat": 41.2853,
    "lng": -70.0964,
}


def stations_body(*stations) -> bytes:
    return json.dumps({"count": len(stations), "stations": list(stations)}).encode()


def harcon_body(m2: float | None = 1.371, units: str = "meters") -> bytes:
    rows = (
        []
        if m2 is None
        else [
            {"name": "M2", "amplitude": m2},
            {"name": "S2", "amplitude": 0.208},
        ]
    )
    return json.dumps({"units": units, "HarmonicConstituents": rows}).encode()


def datums_body(mn: float | None = 2.893, units: str = "meters", mhw=None, mlw=None):
    rows = []
    if mn is not None:
        rows.append({"name": "MN", "value": mn})
    if mhw is not None:
        rows += [{"name": "MHW", "value": mhw}, {"name": "MLW", "value": mlw}]
    return json.dumps({"units": units, "datums": rows}).encode()


class RoutedTransport:
    """Returns a body by URL suffix; records every call."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, params):
        self.calls.append(url)
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                return Response(200, body)
        return Response(404, b"")


def tides(tmp_path, routes, max_km=25.0) -> tuple[CoopsTides, RoutedTransport]:
    t = RoutedTransport(routes)
    client = FetchClient(tmp_path, transport=t, sleep=lambda s: None)
    return CoopsTides(client, max_km), t


def score(src: CoopsTides, lat, lon, cfg: DeviceConfig | None = None):
    return score_rise_fall(
        src.tidal_range_at(lat, lon),
        cfg or DeviceConfig(),
        harcon_citation=src.harcon_citation(),
        datums_citation=src.datums_citation(),
    )


# ------------------------------------------------------------------ parsers


def test_parse_stations_skips_bad_coords_and_rejects_empty():
    bad = {"id": "x", "name": "bad", "lat": None, "lng": -70}
    assert [s.id for s in parse_stations(stations_body(BOSTON, bad))] == ["8443970"]
    with pytest.raises(EmptyResponse):
        parse_stations(stations_body())


def test_parse_harcon_m2_and_empty():
    assert parse_harcon(harcon_body(1.371)).m2_amplitude_m == pytest.approx(1.371)
    assert parse_harcon(harcon_body(1.0, "feet")).m2_amplitude_m == pytest.approx(
        0.3048
    )
    with pytest.raises(EmptyResponse):
        parse_harcon(harcon_body(None))


def test_parse_datums_mn_fallback_and_feet():
    assert parse_datums(datums_body(2.893)) == pytest.approx(2.893)
    assert parse_datums(datums_body(None, mhw=4.071, mlw=1.178)) == pytest.approx(2.893)
    assert parse_datums(datums_body(10.0, "feet")) == pytest.approx(3.048)
    with pytest.raises(EmptyResponse):
        parse_datums(datums_body(None))


# ------------------------------------------------------------ nearest station


def test_haversine_known_distance():
    # 1 degree of latitude along a meridian = pi * R / 180 (~111.2 km).
    assert haversine_km(41.0, -70.0, 42.0, -70.0) == pytest.approx(111.195, abs=0.01)
    assert haversine_km(42.0, -70.0, 42.0, -70.0) == 0.0


def test_nearest_station_picks_minimum_with_id_tiebreak():
    a = Station("b", "", 42.0, -70.0)
    b = Station("a", "", 42.0, -70.0)
    far = Station("c", "", 41.0, -70.0)
    st, d = nearest_station(42.0, -70.0, [a, far, b])
    assert st.id == "a" and d == 0.0
    assert nearest_station(42.0, -70.0, []) is None


# ---------------------------------------------------------------- scoring math


def test_energy_cycle_formula():
    # 0.5 * 1025 * 9.81 * 50 * 2^2
    assert energy_cycle_j(2.0, 1025, 50) == pytest.approx(1_005_525.0)


def test_subscore_ramp_in_range_squared():
    assert rise_fall_subscore(1.0, 1.0, 3.0) == 0.0
    assert rise_fall_subscore(0.5, 1.0, 3.0) == 0.0
    assert rise_fall_subscore(3.0, 1.0, 3.0) == 100.0
    assert rise_fall_subscore(4.0, 1.0, 3.0) == 100.0
    # R = 2: (4 - 1) / (9 - 1) * 100
    assert rise_fall_subscore(2.0, 1.0, 3.0) == pytest.approx(37.5)


def test_cf_formula_and_clamp():
    e = energy_cycle_j(3.0, 1025, 50)
    expected = (e / 3.6e6 * 0.30 * 705.8) / (100 * 8760)
    assert rise_fall_cf(e, 0.30, 705.8, 100) == pytest.approx(expected)
    assert rise_fall_cf(1e15, 1.0, 705.8, 1) == 1.0


# ------------------------------------------------------------ end to end


ROUTES = {
    "stations.json?type=harcon": stations_body(BOSTON, NANTUCKET),
    "8443970/harcon.json": harcon_body(1.371),
    "8443970/datums.json": datums_body(2.893),
}


def test_scored_cell_emits_all_factors_with_provenance(tmp_path):
    src, transport = tides(tmp_path, ROUTES)
    cfg = DeviceConfig()
    res = score(src, 42.33, -70.95, cfg)  # Boston Harbor approaches, ~9 km

    assert res.gaps == []
    assert set(res.factors) == set(RISE_FALL_FACTORS)
    f = res.factors
    assert f["tidal_range"].value == pytest.approx(2.893)
    assert f["coops_station"].value == "8443970"
    assert "km" in f["coops_station"].note and "M2 1.371 m" in f["coops_station"].note
    e = energy_cycle_j(2.893, cfg.seawater_density, cfg.pad_area_m2)
    assert f["energy_cycle_j"].value == pytest.approx(e)
    assert res.score == pytest.approx(rise_fall_subscore(2.893, 1.0, 3.0))
    cf = f["cf_rise_fall"].value
    assert 0 <= cf <= 1
    assert f["energy_rise_fall_kwh"].value == pytest.approx(100 * cf * 8760)
    for fac in f.values():
        assert fac.tags == ["QUANTIFIED DATA"]
    cell = {"cell_id": "C1", "factors": f}
    assert validate([cell]) == []
    assert f["coops_station"].citation.id == "coops-harcon"
    assert f["tidal_range"].citation.id == "coops-datums"

    # Second cell at the same station: memoized, no extra calls.
    n = len(transport.calls)
    score(src, 42.34, -70.96, cfg)
    assert len(transport.calls) == n


def test_range_at_or_below_min_gives_zero_energy_no_cf(tmp_path):
    routes = {**ROUTES, "8443970/datums.json": datums_body(0.9)}
    src, _ = tides(tmp_path, routes)
    res = score(src, 42.33, -70.95)
    assert res.score == 0.0
    assert "cf_rise_fall" not in res.factors
    assert res.factors["energy_rise_fall_kwh"].value == 0.0
    assert res.factors["energy_rise_fall_kwh"].note == BELOW_MIN_RANGE
    assert validate([{"cell_id": "C1", "factors": res.factors}]) == []


def assert_all_null_gap(res, reason: str):
    assert res.score is None
    assert set(res.factors) == set(RISE_FALL_FACTORS)
    for name, fac in res.factors.items():
        assert fac.value is None
        assert isinstance(fac.gap, DataGap)
        assert fac.gap.factor == name and fac.gap.reason == reason
        assert fac.gap.source == "NOAA_COOPS"
    assert validate([{"cell_id": "C1", "factors": res.factors}]) == []


def test_station_beyond_max_distance_is_gap(tmp_path):
    src, transport = tides(tmp_path, ROUTES)
    # Gulf of Maine, ~50 km from Boston and well over 100 km from Nantucket.
    res = score(src, 42.6, -70.5)
    assert_all_null_gap(res, "no coverage")
    assert "> 25 km" in res.factors["tidal_range"].gap.detail
    assert not any("harcon.json" in c for c in transport.calls)


def test_empty_harcon_is_gap_even_with_datums(tmp_path):
    routes = {**ROUTES, "8443970/harcon.json": harcon_body(None)}
    src, _ = tides(tmp_path, routes)
    assert_all_null_gap(score(src, 42.33, -70.95), "empty response")


def test_station_list_fetch_failure_is_gap(tmp_path):
    src, _ = tides(tmp_path, {})  # every URL 404s
    res = score(src, 42.33, -70.95)
    assert_all_null_gap(res, "fetch failure")
    assert len(res.gaps) == len(RISE_FALL_FACTORS)
