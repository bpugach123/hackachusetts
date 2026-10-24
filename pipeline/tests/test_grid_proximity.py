"""Grid Proximity: HIFLD/MassGIS fetch + scoring (Req 6.1-6.6). No network."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, box, mapping

from siting.config import DeviceConfig
from siting.fetch.client import EmptyResponse, FetchClient, Response
from siting.fetch.gaps import DataGap
from siting.provenance import validate
from siting.score.grid_proximity import (
    DIST_SUBSTATION,
    DIST_TRANSMISSION,
    UTILITY,
    UTILITY_LABEL,
    GridInputs,
    combine_grid,
    distance_subscore,
    fetch_inputs,
    nearest_distances_km,
    score_grid_proximity,
    utility_matches,
)
from siting.sources import hifld, massgis
from siting.sources.arcgis import (
    Layer,
    envelope_with_margin,
    fetch_layer,
    grid_envelope,
    parse_page,
)
from siting.sources.endpoints import HIFLD_TRANSMISSION, MASSGIS_UTILITY

TO_LL = Transformer.from_crs("EPSG:26919", "EPSG:4326", always_xy=True)
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:26919", always_xy=True)
ENV = (-71.5, 41.0, -69.5, 43.0)

# Origin of the synthetic scene, in UTM 19N meters (near Boston Harbor).
X0, Y0 = 340000.0, 4690000.0


def ll(geom):
    """UTM -> lon/lat geometry."""
    return shapely.transform(
        geom, lambda c: np.column_stack(TO_LL.transform(c[:, 0], c[:, 1]))
    )


def fc(geoms_utm, props=None, exceeded=False, in_properties=False) -> bytes:
    feats = [
        {
            "type": "Feature",
            "geometry": mapping(ll(g)),
            "properties": (props or [{}] * len(geoms_utm))[i],
        }
        for i, g in enumerate(geoms_utm)
    ]
    doc: dict = {"type": "FeatureCollection", "features": feats}
    if exceeded:
        if in_properties:
            doc["properties"] = {"exceededTransferLimit": True}
        else:
            doc["exceededTransferLimit"] = True
    return json.dumps(doc).encode()


class Transport:
    """Routes ``(layer url prefix, resultOffset)`` -> body or status."""

    def __init__(self, routes: dict[tuple[str, int], bytes | int]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params or {})))
        off = int((params or {}).get("resultOffset", 0))
        for (prefix, o), body in self.routes.items():
            if url.startswith(prefix) and o == off:
                if isinstance(body, int):
                    return Response(body, b"")
                return Response(200, body)
        return Response(404, b"")


def client(tmp_path, transport) -> FetchClient:
    return FetchClient(tmp_path, transport=transport, sleep=lambda s: None)


def cells_df(points_utm: list[tuple[float, float]]) -> pd.DataFrame:
    lon, lat = TO_LL.transform([p[0] for p in points_utm], [p[1] for p in points_utm])
    return pd.DataFrame(
        {
            "cell_id": [f"C-{k}" for k in range(len(points_utm))],
            "centroid_lat": np.round(lat, 6),
            "centroid_lon": np.round(lon, 6),
        }
    )


def layer(source, geoms_utm, props=None, cid="x") -> Layer:
    from siting.sources.arcgis import citation_for

    ep = HIFLD_TRANSMISSION if source.startswith("HIFLD") else MASSGIS_UTILITY
    return Layer(
        source,
        [ll(g) for g in geoms_utm],
        list(geoms_utm),
        props or [{} for _ in geoms_utm],
        citation_for(cid, ep, date="2026-10-24"),
        ep.url,
    )


# --------------------------------------------------------------- parse / fetch


def test_parse_page_flags_and_errors():
    assert parse_page(fc([Point(X0, Y0)], exceeded=True), first=True).exceeded
    assert parse_page(
        fc([Point(X0, Y0)], exceeded=True, in_properties=True), first=True
    ).exceeded
    assert not parse_page(fc([Point(X0, Y0)]), first=True).exceeded
    with pytest.raises(EmptyResponse):
        parse_page(fc([]), first=True)
    assert parse_page(fc([]), first=False).features == []
    with pytest.raises(ValueError, match="ArcGIS error"):
        parse_page(b'{"error": {"code": 400, "message": "Invalid URL"}}', first=True)


def test_fetch_layer_paginates_until_limit_clears(tmp_path):
    url = HIFLD_TRANSMISSION.url
    lines = [
        LineString([(X0 + k * 100, Y0), (X0 + k * 100, Y0 + 50)]) for k in range(5)
    ]
    t = Transport(
        {
            (url, 0): fc(lines[:2], exceeded=True),
            (url, 2): fc(lines[2:4], exceeded=True, in_properties=True),
            (url, 4): fc(lines[4:]),
        }
    )
    got = hifld.fetch_transmission(client(tmp_path, t), ENV)
    assert isinstance(got, Layer)
    assert len(got) == 5 and got.pages == 3
    assert [int(p["resultOffset"]) for _, p in t.calls] == [0, 2, 4]
    _, p0 = t.calls[0]
    assert p0["returnGeometry"] == "true"
    assert p0["geometryType"] == "esriGeometryEnvelope"
    assert p0["geometry"] == "-71.5,41.0,-69.5,43.0"
    assert p0["inSR"] == p0["outSR"] == "4326"
    assert got.citation.id == "hifld-transmission"
    assert got.citation.endpoint == url + "/query"
    # UTM geometry round-trips to the source coordinates (sub-meter).
    assert got.geoms_utm[0].distance(lines[0]) < 0.01


def test_fetch_layer_uses_mirror_when_primary_fails(tmp_path):
    ep = HIFLD_TRANSMISSION
    t = Transport(
        {(ep.url, 0): 500, (ep.fallback, 0): fc([LineString([(X0, Y0), (X0, Y0 + 1)])])}
    )
    got = fetch_layer(client(tmp_path, t), ep, ENV, citation_id="hifld-transmission")
    assert isinstance(got, Layer)
    assert got.url == ep.fallback
    assert got.citation.endpoint == ep.fallback + "/query"
    assert got.citation.note == "fallback mirror"


def test_fetch_layer_empty_everywhere_is_gap(tmp_path):
    ep = HIFLD_TRANSMISSION
    t = Transport({(ep.url, 0): fc([]), (ep.fallback, 0): fc([])})
    got = fetch_layer(client(tmp_path, t), ep, ENV, citation_id="c")
    assert isinstance(got, DataGap)
    assert got.reason == "empty response" and got.source == "HIFLD_TRANSMISSION"


def test_failed_second_page_drops_the_layer(tmp_path):
    ep = HIFLD_TRANSMISSION
    pt = LineString([(X0, Y0), (X0, Y0 + 1)])
    t = Transport({(ep.url, 0): fc([pt], exceeded=True), (ep.url, 1): 503})
    t2 = Transport({**t.routes, (ep.fallback, 0): 503})
    got = fetch_layer(client(tmp_path, t2), ep, ENV, citation_id="c")
    assert isinstance(got, DataGap) and got.reason == "fetch failure"


def test_unresolved_substations_gap_without_network(tmp_path):
    t = Transport({})
    got = hifld.fetch_substations(client(tmp_path, t), ENV)
    assert isinstance(got, DataGap)
    assert got.source == "HIFLD_SUBSTATIONS" and got.reason == "fetch failure"
    assert t.calls == []


def test_utility_territory_fetch(tmp_path):
    town = box(X0, Y0, X0 + 1000, Y0 + 1000)
    body = fc(
        [town],
        [
            {
                "TOWN": "HULL",
                "ELEC": "Eversource Energy (NSTAR Electric)",
                "ELEC_LABEL": "NSTAR Electric d/b/a Eversource Energy",
            }
        ],
    )
    t = Transport({(MASSGIS_UTILITY.url, 0): body})
    got = massgis.fetch_utility_territory(client(tmp_path, t), ENV)
    assert isinstance(got, Layer)
    assert (
        massgis.utility_name(got.properties[0])
        == "NSTAR Electric d/b/a Eversource Energy"
    )
    assert t.calls[0][1]["outFields"] == "TOWN,ELEC,ELEC_LABEL"


def test_grid_envelope_covers_bbox_plus_margin():
    cells = gpd.GeoDataFrame(
        {"cell_id": ["a"], "geometry_utm": [box(X0, Y0, X0 + 1609, Y0 + 1609)]},
        geometry=[Point(0, 0)],
        crs="EPSG:4326",
    )
    env = grid_envelope(cells, 50.0)
    assert env == envelope_with_margin((X0, Y0, X0 + 1609, Y0 + 1609), 50.0)
    lo = box(*env)
    for x, y in [
        (X0 - 50000, Y0 - 50000),
        (X0 + 51609, Y0 + 51609),
        (X0 - 50000, Y0 + 51609),
        (X0 + 51609, Y0 - 50000),
    ]:
        assert lo.covers(Point(*TO_LL.transform(x, y)))


# -------------------------------------------------------------------- geometry


def test_nearest_distances_km_exact_and_rounded():
    geoms = [LineString([(0, 0), (0, 10000)]), Point(20000, 0)]
    pts = shapely.points([(1234.5, 5000), (19000, 0), (0, 3000)])
    d = nearest_distances_km(pts, geoms)
    assert d.tolist() == [1.23, 1.0, 0.0]


def test_utility_contains_else_nearest():
    a = box(0, 0, 1000, 1000)
    b = box(1000, 0, 2000, 1000)
    lay = layer(
        "MASSGIS_UTILITY",
        [a, b],
        [{"TOWN": "A", "ELEC_LABEL": "Util A"}, {"TOWN": "B", "ELEC": "Util B"}],
    )
    pts = shapely.points([(500, 500), (1000, 500), (2500, 500), (5000, 5000)])
    m = utility_matches(pts, lay)
    assert (m[0].name, m[0].contains) == ("Util A", True)
    assert m[1].name == "Util A" and m[1].contains  # shared edge -> lowest index
    assert (m[2].name, m[2].contains, m[2].distance_km) == ("Util B", False, 0.5)
    assert m[3].name == "Util B" and not m[3].contains


# --------------------------------------------------------------------- scoring


def test_distance_subscore_ramp():
    cfg = DeviceConfig()
    assert distance_subscore(0.5, cfg) == 100.0
    assert distance_subscore(1.0, cfg) == 100.0
    assert distance_subscore(30.0, cfg) == 0.0
    assert distance_subscore(15.5, cfg) == pytest.approx(50.0)


def test_score_full_path():
    cfg = DeviceConfig()
    line = LineString([(X0, Y0 - 50000), (X0, Y0 + 50000)])
    sub = Point(X0 + 20000, Y0)
    town = box(X0 - 3000, Y0 - 3000, X0 - 1000, Y0 + 3000)
    inputs = GridInputs(
        transmission=layer("HIFLD_TRANSMISSION", [line], cid="hifld-transmission"),
        substations=layer("HIFLD_SUBSTATIONS", [sub], cid="hifld-substations"),
        utility=layer(
            "MASSGIS_UTILITY",
            [town],
            [{"TOWN": "HULL", "ELEC_LABEL": "NSTAR Electric d/b/a Eversource Energy"}],
            cid="massgis-utility",
        ),
    )
    cells = cells_df([(X0 + 5000, Y0), (X0 - 2000, Y0)])
    res = score_grid_proximity(cells, inputs, cfg)
    r0 = res["C-0"]
    t_km = r0.factors[DIST_TRANSMISSION].value
    s_km = r0.factors[DIST_SUBSTATION].value
    assert t_km == pytest.approx(5.0, abs=0.01)
    assert s_km == pytest.approx(15.0, abs=0.01)
    assert t_km == round(t_km, 2) and s_km == round(s_km, 2)
    expect = 0.5 * distance_subscore(t_km, cfg) + 0.5 * distance_subscore(s_km, cfg)
    assert r0.score == pytest.approx(expect)
    assert r0.omitted == [] and r0.gaps == []
    u = r0.factors[UTILITY]
    assert u.value == "NSTAR Electric d/b/a Eversource Energy"
    assert u.label == UTILITY_LABEL and u.tags == ["PROXY"]
    assert "nearest" in u.note
    assert r0.factors[DIST_TRANSMISSION].tags == ["QUANTIFIED DATA"]
    assert "contains centroid" in res["C-1"].factors[UTILITY].note
    assert (
        validate([{"cell_id": k, "factors": v.factors} for k, v in res.items()]) == []
    )


def test_substation_gap_scores_from_transmission_alone():
    cfg = DeviceConfig()
    gap = DataGap("*", "HIFLD_SUBSTATIONS", "empty response")
    inputs = GridInputs(
        transmission=layer(
            "HIFLD_TRANSMISSION", [LineString([(X0, Y0), (X0, Y0 + 9000)])]
        ),
        substations=gap,
        utility=DataGap("*", "MASSGIS_UTILITY", "fetch failure"),
    )
    res = score_grid_proximity(cells_df([(X0 + 10000, Y0)]), inputs, cfg)["C-0"]
    f = res.factors[DIST_SUBSTATION]
    assert f.value is None and f.gap.factor == DIST_SUBSTATION
    assert f.gap.reason == "empty response"
    assert res.score == pytest.approx(distance_subscore(10.0, cfg))
    assert res.omitted == ["substation"]
    assert res.factors[UTILITY].value is None
    assert validate([{"cell_id": "C-0", "factors": res.factors}]) == []


def test_both_layers_gap_gives_null_component():
    cfg = DeviceConfig()
    inputs = GridInputs(
        transmission=DataGap("*", "HIFLD_TRANSMISSION", "fetch failure"),
        substations=DataGap("*", "HIFLD_SUBSTATIONS", "fetch failure"),
        utility=DataGap("*", "MASSGIS_UTILITY", "fetch failure"),
    )
    res = score_grid_proximity(cells_df([(X0, Y0)]), inputs, cfg)["C-0"]
    assert res.score is None
    grid_gaps = [g for g in res.gaps if g.factor == "grid"]
    assert len(grid_gaps) == 1
    assert grid_gaps[0].source == "HIFLD_SUBSTATIONS,HIFLD_TRANSMISSION"
    assert validate([{"cell_id": "C-0", "factors": res.factors}]) == []


def test_combine_grid_uses_config_weights():
    r = combine_grid(80.0, 20.0, {"transmission": 0.75, "substation": 0.25})
    assert r.score == pytest.approx(65.0)


def test_fetch_inputs_end_to_end(tmp_path):
    line = LineString([(X0, Y0 - 1000), (X0, Y0 + 1000)])
    town = box(X0 - 3000, Y0 - 3000, X0 - 1000, Y0 + 3000)
    t = Transport(
        {
            (HIFLD_TRANSMISSION.url, 0): fc([line]),
            (MASSGIS_UTILITY.url, 0): fc([town], [{"TOWN": "HULL", "ELEC_LABEL": "U"}]),
        }
    )
    cells = cells_df([(X0 + 2000, Y0)])
    inputs = fetch_inputs(client(tmp_path, t), cells)
    assert isinstance(inputs.substations, DataGap)
    res = score_grid_proximity(cells, inputs, DeviceConfig())["C-0"]
    assert res.factors[DIST_TRANSMISSION].value == pytest.approx(2.0, abs=0.01)
    assert res.factors[UTILITY].value == "U"
    q = parse_qs(f"g={t.calls[0][1]['geometry']}")
    xmin, ymin, xmax, ymax = (float(v) for v in q["g"][0].split(","))
    lon, lat = cells["centroid_lon"][0], cells["centroid_lat"][0]
    assert (
        xmin < lon - 0.5 and xmax > lon + 0.5 and ymin < lat - 0.4 and ymax > lat + 0.4
    )
