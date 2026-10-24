"""Wave context (NDBC) and the Resource combiner (Req 5.6-5.8). No network."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from siting.config import DeviceConfig
from siting.fetch.client import EmptyResponse, FetchClient, Response
from siting.fetch.gaps import DataGap
from siting.provenance import EXTERNAL_ADDITION, QUANTIFIED, FactorResult, validate
from siting.score.resource import (
    NOT_BLENDED,
    RISE_FALL,
    TIDAL_STREAM,
    WAVE_FACTORS,
    combine_resource,
    score_wave_context,
    wave_power_kwm,
)
from siting.sources.ndbc import (
    NdbcWaves,
    nearest_buoy,
    parse_active_stations,
    parse_realtime2,
)

ACTIVE = b"""<?xml version="1.0" encoding="UTF-8"?>
<stations created="2026-10-24T12:00:00UTC" count="4">
<station id="44013" lat="42.346" lon="-70.651" name="BOSTON 16 NM East of Boston"
 owner="NDBC" pgm="NDBC Meteorological/Ocean" type="buoy" met="y" currents="n"/>
<station id="44020" lat="41.497" lon="-70.283" name="NANTUCKET SOUND"
 owner="NDBC" pgm="NDBC Meteorological/Ocean" type="buoy" met="y" currents="n"/>
<station id="bzbm3" lat="41.524" lon="-70.671" name="Woods Hole"
 owner="NOS" pgm="NOS/CO-OPS" type="fixed" met="y" currents="n"/>
<station id="dart1" lat="42.30" lon="-70.70" name="DART" type="dart" met="n"/>
</stations>
"""

RT_HEADER = (
    "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP\n"
    "#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC\n"
)
RT_ROWS = (
    "2026 10 24 12 00 240  5.0  6.0   1.0   8.0   5.0 100 1015.0  12.0\n"
    "2026 10 01 06 00 240  5.0  6.0    MM    MM    MM  MM 1015.0  12.0\n"
    "2026 09 10 00 00 240  5.0  6.0   2.0    MM   5.0 100 1015.0  12.0\n"
    "2026 09 09 18 00 240  5.0  6.0    MM  10.0   5.0 100 1015.0  12.0\n"
)
RT_ALL_MM = "2026 10 24 12 00 240  5.0  6.0    MM    MM    MM  MM 1015.0  12.0\n"

BOSTON_HARBOR = (42.36, -70.95)  # ~25 km W of 44013
OFFSHORE_FAR = (44.5, -66.0)  # far from both fixture buoys


class RoutedTransport:
    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url, params):
        self.calls.append(url)
        for suffix, body in self.routes.items():
            if url.endswith(suffix):
                return Response(200, body)
        return Response(404, b"")


def waves(tmp_path, routes, max_km=100.0) -> tuple[NdbcWaves, RoutedTransport]:
    t = RoutedTransport(routes)
    client = FetchClient(tmp_path, transport=t, sleep=lambda s: None)
    return NdbcWaves(client, max_km), t


def context(src: NdbcWaves, lat, lon) -> FactorResult:
    return score_wave_context(
        src.wave_at(lat, lon), DeviceConfig(), gap_citation=src.realtime_citation()
    )


# ------------------------------------------------------------------ parsers


def test_parse_active_keeps_met_buoys_only():
    ids = [b.id for b in parse_active_stations(ACTIVE)]
    assert ids == ["44013", "44020"]
    with pytest.raises(EmptyResponse):
        parse_active_stations(b"<stations></stations>")


def test_parse_realtime2_drops_mm_and_records_dates():
    s = parse_realtime2((RT_HEADER + RT_ROWS).encode())
    assert s.hs_mean_m == pytest.approx(1.5)  # (1.0 + 2.0) / 2
    assert s.dpd_mean_s == pytest.approx(9.0)  # (8.0 + 10.0) / 2
    assert (s.n_hs, s.n_dpd) == (2, 2)
    assert (s.start, s.end) == ("2026-09-09", "2026-10-24")


def test_parse_realtime2_all_mm_is_empty():
    with pytest.raises(EmptyResponse):
        parse_realtime2((RT_HEADER + RT_ALL_MM).encode())


def test_nearest_buoy_picks_closest():
    buoys = parse_active_stations(ACTIVE)
    b, d = nearest_buoy(*BOSTON_HARBOR, buoys)
    assert b.id == "44013" and 20 < d < 30


# ------------------------------------------------------------------ physics


def test_wave_power_formula():
    # rho g² / 64π · H² · T, H = 1 m, T = 1 s -> ~0.4906 kW/m at rho 1025.
    assert wave_power_kwm(1.0, 1.0, 1025.0) == pytest.approx(
        1025 * 9.81**2 / (64 * math.pi) / 1000
    )
    assert wave_power_kwm(2.0, 8.0, 1025.0) == pytest.approx(
        4 * 8 * wave_power_kwm(1.0, 1.0, 1025.0)
    )


# ------------------------------------------------------------- wave context


def test_wave_context_success_factors_and_memo(tmp_path):
    src, t = waves(
        tmp_path,
        {"activestations.xml": ACTIVE, "44013.txt": (RT_HEADER + RT_ROWS).encode()},
    )
    res = context(src, *BOSTON_HARBOR)
    assert res.score is None  # never feeds Resource
    p = res.factors["wave_power"]
    assert p.value == pytest.approx(wave_power_kwm(1.5, 9.0, 1025.0))
    assert p.unit == "kW/m"
    assert res.factors["wave_hs_mean"].value == pytest.approx(1.5)
    for name in WAVE_FACTORS:
        f = res.factors[name]
        assert f.tags == [QUANTIFIED] and EXTERNAL_ADDITION not in f.tags
        assert f.note.startswith(NOT_BLENDED)
        assert "44013" in f.note and "2026-09-09..2026-10-24" in f.note
        assert f.citation.agency == "NOAA NDBC" and f.gap is None
    assert res.extra["wave_buoy"] == "44013"
    assert validate([{"cell_id": "c", "factors": res.factors}]) == []

    # Second lookup (e.g. Feasibility reuse) hits the per-station memo.
    n = len(t.calls)
    again = src.wave_at(*BOSTON_HARBOR)
    assert again.hs_mean_m == pytest.approx(1.5) and len(t.calls) == n


@pytest.mark.parametrize(
    "routes, where, reason",
    [
        ({"activestations.xml": ACTIVE}, OFFSHORE_FAR, "no coverage"),
        ({"activestations.xml": ACTIVE}, BOSTON_HARBOR, "fetch failure"),
        (
            {
                "activestations.xml": ACTIVE,
                "44013.txt": (RT_HEADER + RT_ALL_MM).encode(),
            },
            BOSTON_HARBOR,
            "empty response",
        ),
        ({}, BOSTON_HARBOR, "fetch failure"),
    ],
    ids=["out-of-range", "realtime-404", "all-mm", "station-list-404"],
)
def test_wave_context_gaps_are_null(tmp_path, routes, where, reason):
    src, _ = waves(tmp_path, routes)
    res = context(src, *where)
    for name in WAVE_FACTORS:
        f = res.factors[name]
        assert f.value is None and f.gap is not None
        assert f.gap.factor == name and f.gap.reason == reason
        assert f.gap.source == "NDBC"
    assert res.extra["wave_buoy"] is None
    assert validate([{"cell_id": "c", "factors": res.factors}]) == []


# --------------------------------------------------------- Resource combiner


@pytest.mark.parametrize(
    "s, r, score, path",
    [
        (70.0, 40.0, 70.0, TIDAL_STREAM),
        (20.0, 55.0, 55.0, RISE_FALL),
        (50.0, 50.0, 50.0, TIDAL_STREAM),
        (None, 30.0, 30.0, RISE_FALL),
        (0.0, None, 0.0, TIDAL_STREAM),
    ],
)
def test_combine_resource(s, r, score, path):
    res = combine_resource(s, r)
    assert res.score == score and res.extra["resource_path"] == path
    assert res.gaps == []


def test_combine_resource_both_null_records_gap():
    sg = FactorResult(score=None)
    sg.gaps.append(DataGap("velocity_peak", "NECOFS_FVCOM", "fetch failure"))
    rg = FactorResult(score=None)
    rg.gaps.append(DataGap("tidal_range", "NOAA_COOPS", "no coverage"))
    res = combine_resource(sg, rg)
    assert res.score is None and res.extra["resource_path"] is None
    [g] = res.gaps
    assert g.factor == "resource" and g.source == "NECOFS_FVCOM,NOAA_COOPS"
    assert combine_resource(None, None).gaps[0].factor == "resource"


sub = st.one_of(st.none(), st.floats(0, 100, allow_nan=False))


@given(sub, sub)
def test_combine_resource_property(s, r):
    """Property: Resource = max of non-null sub-scores, path names it; both null
    -> null with a Resource gap. **Validates: Requirements 5.7, 5.8**"""
    res = combine_resource(s, r)
    vals = [v for v in (s, r) if v is not None]
    if not vals:
        assert res.score is None and res.gaps[0].factor == "resource"
        return
    assert res.score == max(vals)
    expected = TIDAL_STREAM if s is not None and (r is None or s >= r) else RISE_FALL
    assert res.extra["resource_path"] == expected
