"""Hard_Exclusions: sources, tiled queries and the intersection rule (Req 7.1, 7.3).

No network: every fetch goes through a fake transport.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon, box, mapping, shape

from siting.config import (
    HARD_EXCLUSION_LAYER_IDS,
    ConfigError,
    DeviceConfig,
    from_dict,
)
from siting.fetch.client import FetchClient, Response
from siting.fetch.gaps import DataGap
from siting.score.conflict import (
    CABLE_AREAS,
    EXCLUSION_CHECK_INCOMPLETE,
    EXCLUSION_CITATION_IDS,
    EXCLUSION_GROUPS,
    EXCLUSION_LAYERS,
    HARD_EXCLUSION,
    PROTECTED_HABITAT,
    RESIDENTIAL_SHORELINE,
    SHIPPING_LANES,
    exclusion_citations,
    exclusions,
    fetch_exclusion_layers,
)
from siting.sources import marinecadastre, massgis
from siting.sources.arcgis import (
    MAX_QUERY_VERTICES,
    Layer,
    citation_for,
    fetch_layer_tiles,
    query_tiles,
)
from siting.sources.endpoints import (
    MARINECADASTRE_CABLES,
    MARINECADASTRE_MPA,
    MARINECADASTRE_SHIPPING,
    MASSGIS_BEACHES,
    MASSGIS_LANDUSE,
    MASSGIS_NHESP,
)

TO_LL = Transformer.from_crs("EPSG:26919", "EPSG:4326", always_xy=True)
X0, Y0 = 340000.0, 4690000.0  # UTM 19N, near Boston Harbor
S = 1609.344  # 1 mi cell


def ll(geom):
    return shapely.transform(
        geom, lambda c: np.column_stack(TO_LL.transform(c[:, 0], c[:, 1]))
    )


def cells_df(n: int, row: int = 0) -> pd.DataFrame:
    """``n`` adjacent 1-mi cells in a row (UTM squares)."""
    polys = [
        box(X0 + i * S, Y0 + row * S, X0 + (i + 1) * S, Y0 + (row + 1) * S)
        for i in range(n)
    ]
    return pd.DataFrame(
        {"cell_id": [f"C-{i}" for i in range(n)], "geometry_utm": polys}
    )


def cell_center(i: int) -> tuple[float, float]:
    return X0 + (i + 0.5) * S, Y0 + 0.5 * S


def layer(source: str, geoms_utm, cid: str) -> Layer:
    ep = {
        "MARINECADASTRE_SHIPPING": MARINECADASTRE_SHIPPING,
        "MARINECADASTRE_CABLES": MARINECADASTRE_CABLES,
        "MARINECADASTRE_MPA": MARINECADASTRE_MPA,
        "MASSGIS_NHESP": MASSGIS_NHESP,
        "MASSGIS_LANDUSE": MASSGIS_LANDUSE,
        "MASSGIS_BEACHES": MASSGIS_BEACHES,
    }[source]
    return Layer(
        source,
        [ll(g) for g in geoms_utm],
        list(geoms_utm),
        [{} for _ in geoms_utm],
        citation_for(cid, ep, date="2026-10-24"),
        ep.url,
    )


def fc(geoms_utm) -> bytes:
    feats = [
        {"type": "Feature", "geometry": mapping(ll(g)), "properties": {"k": i}}
        for i, g in enumerate(geoms_utm)
    ]
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


class Transport:
    """Routes by layer URL prefix; a callable route sees ``params``."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params or {})))
        for prefix in sorted(self.routes, key=len, reverse=True):
            if url.startswith(prefix.rstrip("/") + "/query"):
                body = self.routes[prefix]
                if callable(body):
                    body = body(dict(params or {}))
                if isinstance(body, int):
                    return Response(body, b"")
                return Response(200, body)
        return Response(404, b"")

    def calls_to(self, ep_url: str) -> list[dict]:
        return [p for u, p in self.calls if u.startswith(ep_url.rstrip("/") + "/")]


def client(tmp_path, transport) -> FetchClient:
    return FetchClient(tmp_path, transport=transport, sleep=lambda s: None)


# ------------------------------------------------------------ intersection rule


def test_cell_excluded_iff_it_intersects_and_every_group_is_recorded():
    cells = cells_df(4)
    lane = LineString([cell_center(0), cell_center(1)])  # crosses cells 0 and 1
    cable = box(*cell_center(1), cell_center(1)[0] + 10, cell_center(1)[1] + 10)
    layers = {
        SHIPPING_LANES: [layer("MARINECADASTRE_SHIPPING", [lane], "fairways")],
        CABLE_AREAS: [layer("MARINECADASTRE_CABLES", [cable], "cables")],
    }
    out = exclusions(cells, layers)

    assert out["C-0"].extra[EXCLUSION_LAYERS] == [SHIPPING_LANES]
    assert out["C-1"].extra[EXCLUSION_LAYERS] == [SHIPPING_LANES, CABLE_AREAS]
    assert out["C-1"].extra[EXCLUSION_CITATION_IDS] == {
        SHIPPING_LANES: ["fairways"],
        CABLE_AREAS: ["cables"],
    }
    for cid in ("C-2", "C-3"):
        assert out[cid].extra[HARD_EXCLUSION] is False
        assert out[cid].extra[EXCLUSION_LAYERS] == []
    for res in out.values():
        assert res.extra[HARD_EXCLUSION] == bool(res.extra[EXCLUSION_LAYERS])
        assert res.extra[EXCLUSION_CHECK_INCOMPLETE] == []
        assert res.gaps == [] and res.score is None


def test_shared_edge_counts_as_intersecting():
    cells = cells_df(2)
    edge_x = X0 + S  # boundary between cells 0 and 1
    poly = box(edge_x, Y0 - 100, edge_x + 50, Y0 + 100)  # touches cell 0's edge
    out = exclusions(cells, {PROTECTED_HABITAT: [layer("MASSGIS_NHESP", [poly], "n")]})
    assert out["C-0"].extra[HARD_EXCLUSION] and out["C-1"].extra[HARD_EXCLUSION]


def test_two_layers_of_one_group_record_both_citations():
    cells = cells_df(1)
    p = Point(cell_center(0)).buffer(50)
    layers = {
        RESIDENTIAL_SHORELINE: [
            layer("MASSGIS_LANDUSE", [p], "landuse"),
            layer("MASSGIS_BEACHES", [p], "beaches"),
        ]
    }
    res = exclusions(cells, layers)["C-0"]
    assert res.extra[EXCLUSION_LAYERS] == [RESIDENTIAL_SHORELINE]
    assert res.extra[EXCLUSION_CITATION_IDS] == {
        RESIDENTIAL_SHORELINE: ["landuse", "beaches"]
    }
    assert [c.id for c in exclusion_citations(layers)] == ["landuse", "beaches"]


def test_invalid_server_polygon_is_repaired_not_dropped():
    cells = cells_df(2)
    cx, cy = cell_center(0)
    bowtie = Polygon([(cx - 50, cy - 50), (cx + 50, cy + 50), (cx + 50, cy - 50),
                      (cx - 50, cy + 50)])  # fmt: skip
    assert not bowtie.is_valid
    out = exclusions(
        cells, {CABLE_AREAS: [layer("MARINECADASTRE_CABLES", [bowtie], "c")]}
    )
    assert out["C-0"].extra[HARD_EXCLUSION] and not out["C-1"].extra[HARD_EXCLUSION]


# --------------------------------------------------------------- missing layers


def test_failed_layer_flags_every_cell_and_excludes_nothing():
    cells = cells_df(3)
    gap = DataGap("*", "MASSGIS_NHESP", "fetch failure", "HTTP 503")
    mpa = Point(cell_center(2)).buffer(10)
    layers = {
        PROTECTED_HABITAT: [layer("MARINECADASTRE_MPA", [mpa], "mpa"), gap],
        SHIPPING_LANES: [DataGap("*", "MARINECADASTRE_SHIPPING", "empty response")],
    }
    out = exclusions(cells, layers)
    for cid, res in out.items():
        assert res.extra[EXCLUSION_CHECK_INCOMPLETE] == [
            PROTECTED_HABITAT,
            SHIPPING_LANES,
        ]
        assert [(g.factor, g.source, g.reason) for g in res.gaps] == [
            (PROTECTED_HABITAT, "MASSGIS_NHESP", "fetch failure"),
            (SHIPPING_LANES, "MARINECADASTRE_SHIPPING", "empty response"),
        ]
        assert "protected_habitat" in res.gaps[0].detail
        # The working MPA layer in the same group still applies.
        assert res.extra[HARD_EXCLUSION] is (cid == "C-2")
    assert out["C-2"].extra[EXCLUSION_LAYERS] == [PROTECTED_HABITAT]


def test_layer_with_no_geometries_is_an_empty_gap():
    cells = cells_df(1)
    empty = layer("MARINECADASTRE_CABLES", [], "c")
    res = exclusions(cells, {CABLE_AREAS: [empty]})["C-0"]
    assert res.extra[EXCLUSION_CHECK_INCOMPLETE] == [CABLE_AREAS]
    assert res.gaps[0].reason == "empty response"
    assert res.extra[HARD_EXCLUSION] is False


def test_no_cells_no_results():
    assert exclusions(cells_df(0), {}) == {}


# ----------------------------------------------------------------- tiled query


def test_query_tiles_cover_every_cell_and_respect_vertex_cap():
    rows = [cells_df(12, row=r) for r in range(12)]
    cells = pd.concat(rows, ignore_index=True)
    cells["cell_id"] = [f"C-{k}" for k in range(len(cells))]
    tiles = query_tiles(cells)
    assert len(tiles) > 1
    union = shapely.union_all(
        [Polygon(r) for t in tiles for r in t["rings"]]  # lon/lat rings
    )
    for poly in cells["geometry_utm"]:
        assert union.covers(ll(poly))
    for t in tiles:
        assert sum(len(r) for r in t["rings"]) <= MAX_QUERY_VERTICES
        assert t["spatialReference"] == {"wkid": 4326}
    assert query_tiles(cells) == tiles  # deterministic -> stable cache keys


def test_query_tiles_fall_back_to_hull_when_outline_is_too_complex():
    # Checkerboard cells -> a union with many corners.
    polys = [
        box(X0 + i * S, Y0 + j * S, X0 + (i + 1) * S, Y0 + (j + 1) * S)
        for i in range(4)
        for j in range(4)
        if (i + j) % 2 == 0
    ]
    cells = pd.DataFrame({"cell_id": range(len(polys)), "geometry_utm": polys})
    tiles = query_tiles(cells, tile_km=50, max_vertices=6)
    assert len(tiles) == 1 and sum(len(r) for r in tiles[0]["rings"]) <= 6
    hull = Polygon(tiles[0]["rings"][0])
    assert all(hull.covers(ll(p)) for p in polys)


def test_fetch_layer_tiles_skips_empty_tiles_dedupes_and_notes_generalization(
    tmp_path,
):
    cells = pd.concat([cells_df(2), cells_df(2, row=20)], ignore_index=True)
    tiles = query_tiles(cells)
    assert len(tiles) == 2
    shared = Point(cell_center(0)).buffer(20)
    bodies = iter([fc([shared, Point(cell_center(1)).buffer(5)]), fc([])])

    t = Transport({MASSGIS_LANDUSE.url: lambda p: next(bodies)})
    got = massgis.fetch_residential_landuse(client(tmp_path, t), tiles)
    assert isinstance(got, Layer) and len(got) == 2
    params = t.calls_to(MASSGIS_LANDUSE.url)
    assert len(params) == 2
    assert all(p["geometryType"] == "esriGeometryPolygon" for p in params)
    assert all(p["where"] == "USEGENCODE IN (10,11,12,13)" for p in params)
    assert params[0]["maxAllowableOffset"] == "5e-05"
    assert json.loads(params[0]["geometry"])["rings"]
    assert "generalized" in got.citation.note

    # Same feature returned by two tiles is kept once.
    t2 = Transport({MASSGIS_LANDUSE.url: fc([shared])})
    got2 = fetch_layer_tiles(client(tmp_path / "b", t2), MASSGIS_LANDUSE, tiles,
                             citation_id="x")  # fmt: skip
    assert isinstance(got2, Layer) and len(got2) == 1


def test_fetch_layer_tiles_failure_or_all_empty_is_gap(tmp_path):
    tiles = query_tiles(cells_df(2))
    t = Transport({MASSGIS_NHESP.url: 503})
    got = massgis.fetch_nhesp_priority_habitat(client(tmp_path, t), tiles)
    assert isinstance(got, DataGap) and got.reason == "fetch failure"
    assert got.source == "MASSGIS_NHESP"

    t2 = Transport({MASSGIS_NHESP.url: fc([])})
    got2 = massgis.fetch_nhesp_priority_habitat(client(tmp_path / "b", t2), tiles)
    assert isinstance(got2, DataGap) and got2.reason == "empty response"


def test_fetch_layer_tiles_uses_mirror_when_a_primary_tile_fails(tmp_path):
    cells = pd.concat([cells_df(1), cells_df(1, row=20)], ignore_index=True)
    tiles = query_tiles(cells)
    calls = {"n": 0}

    def primary(params):
        calls["n"] += 1
        return fc([Point(cell_center(0)).buffer(5)]) if calls["n"] == 1 else 500

    ep = MASSGIS_BEACHES  # has a fallback URL
    t = Transport({ep.url: primary, ep.fallback: fc([Point(cell_center(0))])})
    got = fetch_layer_tiles(client(tmp_path, t), ep, tiles, citation_id="b")
    assert isinstance(got, Layer) and got.url == ep.fallback
    assert got.citation.note == "fallback mirror"


# --------------------------------------------------------------- source filters


def test_shipping_filter_on_primary_and_unfiltered_fallback(tmp_path):
    ep = MARINECADASTRE_SHIPPING
    lane = box(X0, Y0, X0 + 10, Y0 + 10)
    t = Transport({ep.url: 500, ep.fallback: fc([lane])})
    got = marinecadastre.fetch_shipping_lanes(client(tmp_path, t), (-71, 42, -70, 43))
    assert isinstance(got, Layer) and got.url == ep.fallback
    (p_primary, *_), (p_fb, *_) = t.calls_to(ep.url), t.calls_to(ep.fallback)
    assert "Area to be Avoided" not in p_primary["where"]
    assert p_primary["where"].startswith("THEMELAYER IN (")
    assert "'Traffic Separation Schemes/Traffic Lanes'" in p_primary["where"]
    assert p_fb["where"] == "1=1"


def test_mpa_filters_no_take_and_never_uses_unfilterable_fallback(tmp_path):
    t = Transport({MARINECADASTRE_MPA.url: 500, MARINECADASTRE_MPA.fallback: fc([])})
    got = marinecadastre.fetch_mpa_no_take(client(tmp_path, t), (-71, 42, -70, 43))
    assert isinstance(got, DataGap) and got.reason == "fetch failure"
    assert t.calls_to(MARINECADASTRE_MPA.fallback) == []
    where = t.calls_to(MARINECADASTRE_MPA.url)[0]["where"]
    assert where == "Prot_Lvl IN ('No Take','No Access','No Impact')"


# ----------------------------------------------------------------- end to end


def test_fetch_exclusion_layers_end_to_end(tmp_path):
    cells = cells_df(3)
    cx, cy = cell_center(0)
    routes = {
        MARINECADASTRE_MPA.url: fc([]),  # no no-take MPA here -> empty gap
        MASSGIS_NHESP.url: 503,
        MASSGIS_NHESP.fallback or "unused": 503,
        MASSGIS_LANDUSE.url: fc([box(cx, cy, cx + 30, cy + 30)]),
        MASSGIS_BEACHES.url: fc([LineString([cell_center(2), (X0 + 2.9 * S, Y0)])]),
        MARINECADASTRE_SHIPPING.url: fc([LineString([cell_center(1), cell_center(2)])]),
        MARINECADASTRE_CABLES.url: 500,
        MARINECADASTRE_CABLES.fallback: 500,
    }
    t = Transport(routes)
    layers = fetch_exclusion_layers(client(tmp_path, t), cells, DeviceConfig())
    assert list(layers) == list(HARD_EXCLUSION_LAYER_IDS)
    assert [len(v) for v in layers.values()] == [
        len(EXCLUSION_GROUPS[g]) for g in layers
    ]
    out = exclusions(cells, layers)

    assert out["C-0"].extra[EXCLUSION_LAYERS] == [RESIDENTIAL_SHORELINE]
    assert out["C-1"].extra[EXCLUSION_LAYERS] == [SHIPPING_LANES]
    assert out["C-2"].extra[EXCLUSION_LAYERS] == [RESIDENTIAL_SHORELINE, SHIPPING_LANES]
    assert out["C-2"].extra[EXCLUSION_CITATION_IDS][RESIDENTIAL_SHORELINE] == [
        "massgis-marine-beaches"
    ]
    for res in out.values():
        assert res.extra[EXCLUSION_CHECK_INCOMPLETE] == [PROTECTED_HABITAT, CABLE_AREAS]
        assert sorted(g.source for g in res.gaps) == [
            "MARINECADASTRE_CABLES",
            "MARINECADASTRE_MPA",
            "MASSGIS_NHESP",
        ]
    # Dense land layer was tiled, sparse marine layers used one envelope.
    assert t.calls_to(MASSGIS_LANDUSE.url)[0]["geometryType"] == "esriGeometryPolygon"
    assert (
        t.calls_to(MARINECADASTRE_SHIPPING.url)[0]["geometryType"]
        == "esriGeometryEnvelope"
    )


def test_config_selects_groups(tmp_path):
    cells = cells_df(1)
    t = Transport({MARINECADASTRE_CABLES.url: fc([box(X0, Y0, X0 + 5, Y0 + 5)])})
    cfg = from_dict({"hard_exclusion_layers": ["cable_areas"]})
    layers = fetch_exclusion_layers(client(tmp_path, t), cells, cfg)
    assert list(layers) == [CABLE_AREAS]
    assert {u.split("/rest/")[0] for u, _ in t.calls} == {
        MARINECADASTRE_CABLES.url.split("/rest/")[0]
    }
    res = exclusions(cells, layers)["C-0"]
    assert res.extra[EXCLUSION_LAYERS] == [CABLE_AREAS]
    assert res.extra[EXCLUSION_CHECK_INCOMPLETE] == []

    empty = from_dict({"hard_exclusion_layers": []})
    assert fetch_exclusion_layers(client(tmp_path, Transport({})), cells, empty) == {}


@pytest.mark.parametrize(
    "value",
    [["whale_critical_habitat"], ["diadromous_fish"], ["shipping_lane"], "cable_areas"],
)
def test_config_rejects_non_exclusion_layer_ids(value):
    with pytest.raises(ConfigError, match="hard_exclusion_layers"):
        from_dict({"hard_exclusion_layers": value})


def test_default_groups_and_no_graded_layers_in_exclusions():
    assert DeviceConfig().hard_exclusion_layers == [
        "protected_habitat",
        "residential_shoreline",
        "shipping_lanes",
        "cable_areas",
    ]
    sources = {s.source for parts in EXCLUSION_GROUPS.values() for s in parts}
    assert "NOAA_FISHERIES_RIGHT_WHALE" not in sources
    assert "NEODP_DIADROMOUS" not in sources


def test_layer_geometry_round_trip_is_lon_lat():
    # Guard for the helpers above: fc() writes lon/lat and Layer stores UTM.
    g = shape(json.loads(fc([Point(X0, Y0)]))["features"][0]["geometry"])
    assert -72 < g.x < -69 and 41 < g.y < 43
