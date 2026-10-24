"""Velocity sources and the priority resolver (Req 4.1, 4.2, 3.3).

All tests use fake OPeNDAP openers / HTTP transports and a tmp cache dir: no
network. Synthetic grids are tiny so nearest-point choices are checkable by hand.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from siting.fetch.client import EmptyResponse, FetchClient, Response
from siting.fetch.gaps import DataGap
from siting.provenance import (
    MODELED_CURRENT,
    PREDICTED_CURRENT,
    QUANTIFIED,
    Citation,
    Factor,
    tags_for,
    validate,
)
from siting.sources import coops_currents, doppio, mom6, necofs
from siting.sources.endpoints import (
    DOPPIO as DOPPIO_EP,
)
from siting.sources.endpoints import (
    MOM6_NWA12,
    NECOFS_MASSBAY,
    NOAA_CURRENTS_DATA,
    NOAA_CURRENTS_STATIONS,
)
from siting.sources.velocity import (
    FACTOR,
    VelocitySeries,
    haversine_km,
    nearest_index,
    resolve,
    run_date_from_history,
    run_date_from_release,
)

T0 = np.datetime64("2026-10-24T00:00:00", "ns")
HOURS = T0 + np.arange(4) * np.timedelta64(1, "h")


def _no_network(url, params):
    raise AssertionError(f"unexpected HTTP call to {url}")


def _client(tmp_path, *, opener=None, transport=None) -> FetchClient:
    return FetchClient(
        tmp_path,
        transport=transport or _no_network,
        opener=opener,
        sleep=lambda s: None,
    )


def _cit(source: str = "NECOFS_FVCOM") -> Citation:
    return Citation(
        source,
        "UMass Dartmouth SMAST",
        "NECOFS FVCOM MassBay forecast",
        "http://x",
        "2026-10-24",
        "model_run",
        MODELED_CURRENT,
    )


def _series(source: str, peak: float = 1.0) -> VelocitySeries:
    return VelocitySeries(
        source=source,
        t_start="2026-10-24T00:00:00Z",
        t_end="2026-10-24T03:00:00Z",
        speeds_ms=(0.0, peak),
        model_run_date="2026-10-24",
        citation=_cit(source),
    )


# ------------------------------------------------------------- helpers


def test_haversine_one_degree_latitude():
    assert float(haversine_km(41.0, -70.0, 42.0, -70.0)) == pytest.approx(111.2, 0.01)


def test_nearest_index_skips_invalid_and_respects_max_km():
    lats = np.array([41.0, 41.0, 41.0])
    lons = np.array([-70.0, -70.01, -70.5])
    valid = np.array([False, True, True])
    idx, dist = nearest_index(41.0, -70.0, lats, lons, max_km=5, valid=valid)
    assert idx == 1 and dist == pytest.approx(0.84, abs=0.01)
    assert nearest_index(41.0, -69.0, lats, lons, max_km=5) is None


def test_run_date_parsers():
    hist = "Sat Oct 24 16:14:50 2026: ncrcat -O ...\nmodel started at: 05/10/2023"
    assert run_date_from_history(hist) == "2026-10-24"
    assert run_date_from_history("no stamp here") is None
    assert run_date_from_release("r20230520") == "2023-05-20"
    assert run_date_from_release(None) is None


# ------------------------------------------------------------- resolver


def test_resolver_takes_first_available_source_in_priority():
    calls: dict[str, list] = {}

    def provider(name, covers):
        def run(pts):
            calls.setdefault(name, []).append(list(pts))
            return [
                _series(name) if p in covers else DataGap(FACTOR, name, "no coverage")
                for p in pts
            ]

        return run

    a, b, c = (41.0, -70.0), (41.5, -70.5), (42.0, -71.0)
    providers = {
        "NECOFS_FVCOM": provider("NECOFS_FVCOM", {a}),
        "DOPPIO": provider("DOPPIO", {a, b}),
        "MOM6": provider("MOM6", {a, b, c}),
    }
    out = resolve([a, b, c], providers, ("NECOFS_FVCOM", "DOPPIO", "MOM6"))
    assert [r.source for r in out] == ["NECOFS_FVCOM", "DOPPIO", "MOM6"]
    # Lower-priority sources only see the points still unresolved.
    assert calls["DOPPIO"] == [[b, c]] and calls["MOM6"] == [[c]]

    # Reordering the priority changes the pick.
    out = resolve([a], providers, ("MOM6", "NECOFS_FVCOM"))
    assert out[0].source == "MOM6"


def test_resolver_all_fail_gives_one_velocity_gap_listing_every_attempt():
    def fail(name, reason):
        return lambda pts: [DataGap("*", name, reason, "boom")] * len(pts)

    def broken(pts):
        raise RuntimeError("bug")

    providers = {
        "NECOFS_FVCOM": fail("NECOFS_FVCOM", "fetch failure"),
        "MOM6": broken,
        "NOAA_CURRENTS": fail("NOAA_CURRENTS", "no coverage"),
    }
    priority = ("NECOFS_FVCOM", "DOPPIO", "MOM6", "NOAA_CURRENTS")
    (g,) = resolve([(41.0, -70.0)], providers, priority)
    assert isinstance(g, DataGap)
    assert (g.factor, g.source, g.reason) == (FACTOR, "NOAA_CURRENTS", "no coverage")
    for name in priority:  # missing provider and raising provider both recorded
        assert name in g.detail
    assert "RuntimeError: bug" in g.detail


def test_resolver_never_calls_later_sources_when_all_resolved():
    def boom(pts):
        raise AssertionError("should not be called")

    out = resolve(
        [(41.0, -70.0)],
        {"DOPPIO": lambda pts: [_series("DOPPIO")] * len(pts), "MOM6": boom},
        ("DOPPIO", "MOM6"),
    )
    assert out[0].source == "DOPPIO"


# ------------------------------------------------------------- NECOFS


def _fvcom(lonc, latc, lon, lat, h, ua, va, history=True) -> xr.Dataset:
    ds = xr.Dataset(
        {
            "ua": (("time", "nele"), np.asarray(ua, dtype=float)),
            "va": (("time", "nele"), np.asarray(va, dtype=float)),
            "h": ("node", np.asarray(h, dtype=float)),
        },
        coords={
            "time": HOURS,
            "lonc": ("nele", np.asarray(lonc, dtype=float)),
            "latc": ("nele", np.asarray(latc, dtype=float)),
            "lon": ("node", np.asarray(lon, dtype=float)),
            "lat": ("node", np.asarray(lat, dtype=float)),
        },
    )
    if history:
        ds.attrs["history"] = "Sat Oct 24 16:14:50 2026: ncrcat -O ..."
    return ds


def test_necofs_index_runs():
    assert necofs.index_runs([5, 1, 2, 900, 3], max_gap=10) == [(1, 5), (900, 900)]
    assert necofs.index_runs([]) == []


def test_necofs_nearest_element_depth_and_gom7_fallback(tmp_path):
    ua = np.tile([0.3, 9.0, 9.0], (4, 1)) * np.array([[1], [2], [3], [4]])
    va = np.tile([0.4, 0.0, 0.0], (4, 1)) * np.array([[1], [2], [3], [4]])
    massbay = _fvcom(
        lonc=[-70.0, -70.01, -70.5],
        latc=[41.5, 41.5, 41.8],
        lon=[-70.002, -70.5],
        lat=[41.5, 41.8],
        h=[20.0, 12.0],
        ua=ua,
        va=va,
    )
    gom7 = _fvcom(
        lonc=[-68.0],
        latc=[43.0],
        lon=[-68.0],
        lat=[43.0],
        h=[80.0],
        ua=np.full((4, 1), 0.6),
        va=np.full((4, 1), 0.8),
    )
    datasets = {NECOFS_MASSBAY.url: massbay, NECOFS_MASSBAY.fallback: gom7}
    opened: list[str] = []

    def opener(url):
        opened.append(url)
        return datasets[url].copy()

    pts = [(41.5, -70.001), (43.0, -68.001), (30.0, -60.0)]
    out = necofs.fetch(_client(tmp_path, opener=opener), pts, today="2026-10-24")

    near, far, none = out
    assert near.source == "NECOFS_FVCOM" and near.point_lon == pytest.approx(-70.0)
    assert near.speeds_ms == pytest.approx((0.5, 1.0, 1.5, 2.0))  # hypot(0.3k, 0.4k)
    assert near.depth_m == 20.0
    assert (near.model_run_date, near.t_start, near.t_end) == (
        "2026-10-24",
        "2026-10-24T00:00:00Z",
        "2026-10-24T03:00:00Z",
    )
    assert near.citation.id == "NECOFS_FVCOM_MASSBAY" and near.dt_s == 3600
    assert far.citation.id == "NECOFS_FVCOM_GOM7" and far.peak_ms == pytest.approx(1)
    assert isinstance(none, DataGap) and none.reason == "no coverage"
    assert none.factor == FACTOR and "MassBay" in none.detail and "GOM7" in none.detail

    # Second run is served entirely from the cache.
    n_open = len(opened)
    again = necofs.fetch(_client(tmp_path, opener=opener), pts, today="2026-10-24")
    assert len(opened) == n_open and again[0].speeds_ms == near.speeds_ms


def test_necofs_without_history_stamp_is_a_gap_not_a_guess(tmp_path):
    ds = _fvcom(
        [-70.0],
        [41.5],
        [-70.0],
        [41.5],
        [20.0],
        np.full((4, 1), 1.0),
        np.zeros((4, 1)),
        history=False,
    )
    out = necofs.fetch(
        _client(tmp_path, opener=lambda u: ds.copy()),
        [(41.5, -70.0)],
        today="2026-10-24",
        grids=(("MassBay", "NECOFS FVCOM MassBay", "u"),),
    )
    assert isinstance(out[0], DataGap) and out[0].reason == "empty response"


def test_necofs_fetch_failure_is_a_gap_for_every_point(tmp_path):
    def opener(url):
        raise OSError("down")

    out = necofs.fetch(_client(tmp_path, opener=opener), [(41.5, -70.0)] * 2)
    assert all(isinstance(g, DataGap) and g.reason == "fetch failure" for g in out)


# ------------------------------------------------------------- Doppio


def _doppio() -> xr.Dataset:
    ny, nx = 5, 6
    jj, ii = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    mask = np.ones((ny, nx))
    mask[2, 2] = 0.0  # land
    # ubar at u-col k = k  ->  u on rho col i = 0.5*((i-1) + i) = i - 0.5
    ubar = np.broadcast_to(np.arange(nx - 1, dtype=float), (4, ny, nx - 1)).copy()
    vbar = np.zeros((4, ny - 1, nx))
    return xr.Dataset(
        {
            "ubar": (("time", "eta_u", "xi_u"), ubar),
            "vbar": (("time", "eta_v", "xi_v"), vbar),
            "lon_rho": (("eta_rho", "xi_rho"), -71.0 + 0.1 * ii),
            "lat_rho": (("eta_rho", "xi_rho"), 41.0 + 0.1 * jj),
            "mask_rho": (("eta_rho", "xi_rho"), mask),
        },
        coords={
            "time": HOURS,
            "time_run": (
                "time",
                np.array([T0 - np.timedelta64(1, "D")] * 2 + [T0] * 2),
            ),
        },
    )


def test_doppio_uv_to_rho_averages_adjacent_faces():
    u = np.array([[[1.0, 3.0, 5.0]]])  # (t=1, ny=1, nx+1=3)
    v = np.array([[[2.0, 4.0], [6.0, 8.0]]])  # (t=1, ny+1=2, nx=2)
    ur, vr = doppio.uv_to_rho(u, v)
    assert ur.tolist() == [[[2.0, 4.0]]] and vr.tolist() == [[[4.0, 6.0]]]


def test_doppio_nearest_wet_rho_point(tmp_path):
    ds = _doppio()
    client = _client(tmp_path, opener=lambda u: ds.copy())
    # (41.2, -70.7) is rho (2, 3): speed 2.5. (41.2, -70.8) is the land rho
    # (2, 2): the nearest wet interior point is chosen instead (never land).
    a, b = doppio.fetch(
        client,
        [
            (41.2, -70.7),
            (41.2, -70.8),
        ],
        today="2026-10-24",
    )
    assert a.source == "DOPPIO" and a.speeds_ms == pytest.approx((2.5,) * 4)
    assert (a.point_lat, a.point_lon) == pytest.approx((41.2, -70.7))
    assert a.model_run_date == "2026-10-24" and "2026-10-23..2026-10-24" in a.note
    assert (b.point_lat, b.point_lon) != pytest.approx((41.2, -70.8))
    assert b.distance_km > 0 and "(2,2)" not in b.note


def test_doppio_point_off_grid_is_no_coverage(tmp_path):
    ds = _doppio()
    (g,) = doppio.fetch(_client(tmp_path, opener=lambda u: ds.copy()), [(45.0, -60.0)])
    assert isinstance(g, DataGap) and g.reason == "no coverage"


# ------------------------------------------------------------- MOM6


def _mom6(var: str, values) -> xr.Dataset:
    days = T0 + np.arange(5) * np.timedelta64(1, "D")
    ds = xr.Dataset(
        {var: (("time", "lat", "lon"), values)},
        coords={"time": days, "lat": [41.0, 41.1, 41.2], "lon": [289.0, 289.1, 289.2]},
    )
    ds.attrs["cefi_release"] = "r20230520"
    return ds


def test_mom6_nearest_ocean_point_with_0_360_longitudes(tmp_path):
    u = np.broadcast_to(np.array([1.0, 2.0, 3.0]), (5, 3, 3)).copy()
    u[:, 1, 1] = np.nan  # land
    v = np.zeros((5, 3, 3))
    datasets = {
        MOM6_NWA12.url: _mom6("ssu_rotate", u),
        MOM6_NWA12.extra["v_url"]: _mom6("ssv_rotate", v),
    }
    client = _client(tmp_path, opener=lambda url: datasets[url].copy())
    # Nearest is the land point (41.1, -70.9); next nearest ocean is (41.1, -70.8).
    (s,) = mom6.fetch(client, [(41.1, -70.895)])
    assert s.source == "MOM6" and s.speeds_ms == pytest.approx((3.0,) * 5)
    assert s.point_lon == pytest.approx(-70.8) and s.dt_s == 86400
    assert s.model_run_date == "2023-05-20" and "daily-mean" in s.note


# ------------------------------------------------------------- CO-OPS currents

STATIONS = {
    "stations": [
        # Harmonic station with 3 bins: mid-depth bin is 5 (14 m).
        {
            "id": "COD0911",
            "type": "H",
            "currbin": 1,
            "depth": 21.0,
            "name": "Woods Hole, The Strait",
            "lat": 41.52,
            "lng": -70.68,
        },
        {
            "id": "COD0911",
            "type": "H",
            "currbin": 5,
            "depth": 14.0,
            "name": "Woods Hole, The Strait",
            "lat": 41.52,
            "lng": -70.68,
        },
        {
            "id": "COD0911",
            "type": "H",
            "currbin": 11,
            "depth": 5.0,
            "name": "Woods Hole, The Strait",
            "lat": 41.52,
            "lng": -70.68,
        },
        # Subordinate station closer to the point: ignored (events only).
        {
            "id": "ACT9999",
            "type": "S",
            "currbin": 1,
            "depth": None,
            "name": "Sub",
            "lat": 41.521,
            "lng": -70.681,
        },
        # Two harmonic stations near a second point; the nearer one errors.
        {
            "id": "BAD0001",
            "type": "H",
            "currbin": 1,
            "depth": 10.0,
            "name": "Bad",
            "lat": 42.0,
            "lng": -70.0,
        },
        {
            "id": "OK00002",
            "type": "H",
            "currbin": 2,
            "depth": 12.0,
            "name": "Ok",
            "lat": 42.02,
            "lng": -70.0,
        },
    ]
}


def _cp(values_cms, *, event=False):
    rows = []
    for k, v in enumerate(values_cms):
        row = {
            "Time": f"2026-10-24 {k // 2:02d}:{30 * (k % 2):02d}",
            "Velocity_Major": v,
            "Depth": "4.3",
            "Bin": "5",
        }
        if event:
            row["Type"] = "flood"
        rows.append(row)
    return json.dumps({"current_predictions": {"cp": rows}}).encode()


class Router:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        if url == NOAA_CURRENTS_STATIONS.url:
            return Response(200, json.dumps(STATIONS).encode())
        assert url == NOAA_CURRENTS_DATA.url
        station = params["station"]
        if station == "BAD0001":
            return Response(200, b'{"error": {"message": "No data was found."}}')
        return Response(200, _cp([100.0, -150.0, 20.0]))


def test_coops_parse_keeps_harmonic_stations_and_mid_bin():
    stations = coops_currents.parse_stations(json.dumps(STATIONS).encode())
    assert [s.id for s in stations] == ["BAD0001", "COD0911", "OK00002"]
    wh = stations[1]
    assert wh.mid_bin() == (5, 14.0)


def test_coops_parse_predictions_cm_s_to_m_s_and_rejects_events():
    p = coops_currents.parse_predictions(_cp([100.0, -150.0]))
    assert p.speeds_ms == pytest.approx((1.0, 1.5)) and p.depth_m == 4.3
    with pytest.raises(EmptyResponse):
        coops_currents.parse_predictions(_cp([100.0], event=True))
    with pytest.raises(EmptyResponse):
        coops_currents.parse_predictions(b'{"error": {"message": "Wrong Station"}}')


def test_coops_nearest_station_within_5_km(tmp_path):
    router = Router()
    client = _client(tmp_path, transport=router)
    pts = [(41.53, -70.68), (42.0, -70.001), (41.0, -69.0)]
    wh, ok, none = coops_currents.fetch(client, pts, begin="2026-10-24")

    assert wh.source == "NOAA_CURRENTS" and wh.label == PREDICTED_CURRENT
    assert wh.speeds_ms == pytest.approx((1.0, 1.5, 0.2)) and wh.dt_s == 1800
    assert wh.model_run_date is None and wh.citation.date_kind == "fetch"
    assert "COD0911" in wh.note and "bin 5" in wh.note
    assert wh.distance_km == pytest.approx(1.11, abs=0.01)
    # Nearer BAD0001 returned no data -> next station within 5 km is used.
    assert "OK00002" in ok.note
    assert isinstance(none, DataGap) and none.reason == "no coverage"

    data_calls = [p for u, p in router.calls if u == NOAA_CURRENTS_DATA.url]
    assert {p["station"] for p in data_calls} == {"COD0911", "BAD0001", "OK00002"}
    assert data_calls[0]["interval"] == "30" and data_calls[0]["units"] == "metric"
    assert data_calls[0]["begin_date"] == "20261024" and data_calls[0]["range"] == "360"


def test_coops_station_list_failure_is_gap_for_every_point(tmp_path):
    client = _client(tmp_path, transport=lambda u, p: Response(503, b""))
    out = coops_currents.fetch(client, [(41.5, -70.6)] * 3)
    assert all(isinstance(g, DataGap) and g.reason == "fetch failure" for g in out)


# ------------------------------------------------------------- provenance


def test_series_citations_pass_the_provenance_validator(tmp_path):
    ds = _doppio()
    (model,) = doppio.fetch(
        _client(tmp_path / "d", opener=lambda u: ds.copy()), [(41.2, -70.7)]
    )
    (pred,) = coops_currents.fetch(
        _client(tmp_path / "c", transport=Router()), [(41.53, -70.68)]
    )
    factors = {
        f"velocity_peak_{s.source}": Factor(
            "velocity_peak",
            s.peak_ms,
            "m/s",
            tags_for(QUANTIFIED, s.source),
            s.citation,
            label=s.label,
        )
        for s in (model, pred)
    }
    assert model.citation.endpoint == DOPPIO_EP.url
    assert validate([{"cell_id": "C1", "factors": factors}]) == []
