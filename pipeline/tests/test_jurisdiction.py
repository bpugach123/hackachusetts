"""Jurisdiction by centroid, straddle flag, fallback, gaps (Req 8.1-8.5).

Fake HTTP transports and a tmp cache only: no network. Geometry is synthetic
lon/lat boxes so every expected class is checkable by hand. The state/federal
line sits at lon -70.2; land is everything west of lon -70.55.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import geopandas as gpd
import jsonschema
import pytest
import shapely
from shapely.geometry import LineString, box

from siting import jurisdiction as jx
from siting.fetch.client import FetchClient, Response
from siting.fetch.gaps import GapRegistry
from siting.sources.endpoints import MARINECADASTRE_SUBMERGED_LANDS as EP

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "docs" / "schema"
SCORE_DIR = ROOT / "pipeline" / "siting" / "score"

PRIMARY_URL = EP.url + "/query"
FALLBACK_URL = EP.fallback + "/query"

LAND = box(-71.0, 41.0, -70.55, 43.0)
STATE_POLY = box(-70.55, 41.0, -70.2, 43.0)  # west edge is the shoreline
SLA_LINE = LineString([(-70.2, 40.0), (-70.2, 43.0)])


def _cell(cid: str, lon: float, lat: float, half: float = 0.01) -> dict:
    return {
        "cell_id": cid,
        "centroid_lon": lon,
        "centroid_lat": lat,
        "geometry": box(lon - half, lat - half, lon + half, lat + half),
    }


def _cells() -> gpd.GeoDataFrame:
    rows = [
        _cell("A", -70.5, 41.8),  # state, inside
        _cell("B", -70.0, 41.8),  # federal, outside
        _cell("C", -70.21, 41.8, 0.02),  # state centroid, cell crosses line
        _cell("D", -70.19, 41.8, 0.02),  # federal centroid, cell crosses line
        _cell("E", -70.2, 41.9),  # centroid on the line -> state
        # Cell edge lies on the line (touch only): federal, not straddling.
        {
            "cell_id": "F",
            "centroid_lon": -70.19,
            "centroid_lat": 42.0,
            "geometry": box(-70.2, 41.99, -70.18, 42.01),
        },
    ]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=jx.WGS84)


EXPECTED = {
    "A": ("state", "MA CZM", False),
    "B": ("federal", "BOEM", False),
    "C": ("state", "MA CZM", True),
    "D": ("federal", "BOEM", True),
    "E": ("state", "MA CZM", True),
    "F": ("federal", "BOEM", False),
}


def _fc(*geoms) -> bytes:
    feats = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": json.loads(shapely.to_geojson(g)),
        }
        for g in geoms
    ]
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


def _client(tmp_path, routes: dict[str, Response]) -> tuple[FetchClient, list[str]]:
    calls: list[str] = []

    def transport(url, params):
        calls.append(url)
        return routes.get(url, Response(500, b""))

    return FetchClient(tmp_path, transport=transport, sleep=lambda s: None), calls


def _ok(payload: bytes) -> Response:
    return Response(200, payload)


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _assert_expected(result: jx.JurisdictionResult) -> None:
    got = {
        cid: (r.jurisdiction, r.agency, r.straddles_boundary)
        for cid, r in result.cells.items()
    }
    assert got == EXPECTED
    for r in result.cells.values():
        assert r.disclaimer == "Not legal advice"
        assert r.to_dict()["disclaimer"] == "Not legal advice"
        assert r.gaps == []


# --------------------------------------------------------------------- primary


def test_primary_polygon_classifies_by_centroid(tmp_path):
    client, calls = _client(tmp_path, {PRIMARY_URL: _ok(_fc(STATE_POLY))})
    result = jx.assign(client, _cells(), land=LAND)

    _assert_expected(result)
    assert result.gap is None
    assert result.citation.id == jx.PRIMARY_CITATION_ID
    assert result.citation.endpoint == PRIMARY_URL
    assert FALLBACK_URL not in calls


def test_boundary_line_drops_shoreline_and_clip_edges(tmp_path):
    client, _ = _client(tmp_path, {PRIMARY_URL: _ok(_fc(STATE_POLY))})
    result = jx.assign(client, _cells(), land=LAND)

    line = result.boundary.line
    # Only the 3 nm side (lon -70.2) is left: no shoreline, no bbox edges.
    xs = {round(x, 6) for x, _ in shapely.get_coordinates(line)}
    assert xs == {-70.2}

    gj = result.boundary_geojson()
    jsonschema.validate(gj, _schema("boundary_3nm.schema.json"))
    props = gj["features"][0]["properties"]
    assert props["disclaimer"] == "Not legal advice"
    assert props["citation_id"] == jx.PRIMARY_CITATION_ID


def test_without_land_line_keeps_polygon_outline():
    bbox = (-70.62, 41.68, -69.89, 42.11)
    line = jx.boundary_line(STATE_POLY.intersection(box(*bbox)), bbox, None)
    xs = {round(x, 6) for x, _ in shapely.get_coordinates(line)}
    assert xs == {-70.55, -70.2}


# -------------------------------------------------------------------- fallback


def test_fallback_line_oriented_by_land(tmp_path):
    routes = {PRIMARY_URL: Response(503, b""), FALLBACK_URL: _ok(_fc(SLA_LINE))}
    client, calls = _client(tmp_path, routes)
    result = jx.assign(client, _cells(), land=LAND)

    _assert_expected(result)
    assert calls.count(PRIMARY_URL) == 3  # retried before falling back
    assert result.citation.id == jx.FALLBACK_CITATION_ID
    jsonschema.validate(result.boundary_geojson(), _schema("boundary_3nm.schema.json"))


@pytest.mark.parametrize(
    "bad",
    [
        json.dumps({"error": {"code": 400, "message": "Invalid query"}}).encode(),
        json.dumps(
            {"type": "FeatureCollection", "features": [], "exceededTransferLimit": True}
        ).encode(),
    ],
)
def test_arcgis_error_or_partial_primary_falls_back(tmp_path, bad):
    routes = {PRIMARY_URL: _ok(bad), FALLBACK_URL: _ok(_fc(SLA_LINE))}
    client, _ = _client(tmp_path, routes)
    result = jx.assign(client, _cells(), land=LAND)
    assert result.citation.id == jx.FALLBACK_CITATION_ID
    _assert_expected(result)


def test_fallback_without_land_is_a_gap(tmp_path):
    empty = json.dumps({"type": "FeatureCollection", "features": []}).encode()
    routes = {PRIMARY_URL: _ok(empty), FALLBACK_URL: _ok(_fc(SLA_LINE))}
    client, _ = _client(tmp_path, routes)
    result = jx.assign(client, _cells(), land=None)

    assert result.boundary is None
    assert result.gap.reason == "empty response"
    assert all(r.jurisdiction is None for r in result.cells.values())


def test_line_that_separates_nothing_is_rejected():
    bbox = (-70.62, 41.68, -69.89, 42.11)
    # Line runs through land only: every face touches land.
    inland = LineString([(-70.6, 40.0), (-70.6, 43.0)])
    with pytest.raises(jx.EmptyResponse):
        jx.state_waters_from_lines(inland, LAND, bbox)


# ------------------------------------------------------------------------ gaps


def test_both_sources_fail_null_and_gap_on_every_cell(tmp_path):
    client, _ = _client(tmp_path, {})
    registry = GapRegistry()
    cells = _cells()
    result = jx.assign(client, cells, land=LAND, registry=registry)

    assert result.boundary is None and result.citation is None
    assert set(result.cells) == set(cells["cell_id"])
    for r in result.cells.values():
        assert (r.jurisdiction, r.agency, r.straddles_boundary) == (None, None, False)
        assert r.disclaimer == "Not legal advice"
        assert len(r.gaps) == 1
        gap = r.gaps[0]
        assert gap.factor == "jurisdiction"
        assert gap.source == jx.PRIMARY_SOURCE
        assert gap.reason == "fetch failure"
        assert jx.FALLBACK_SOURCE in gap.detail
    assert registry.summary() == {jx.PRIMARY_SOURCE: len(cells)}

    gj = result.boundary_geojson()
    assert gj == {"type": "FeatureCollection", "features": []}
    jsonschema.validate(gj, _schema("boundary_3nm.schema.json"))


def test_successful_fetch_is_cached(tmp_path):
    client, calls = _client(tmp_path, {PRIMARY_URL: _ok(_fc(STATE_POLY))})
    jx.assign(client, _cells(), land=LAND)
    jx.assign(client, _cells(), land=LAND)
    assert calls.count(PRIMARY_URL) == 1


def test_record_rejects_mismatched_agency():
    with pytest.raises(ValueError):
        jx.CellJurisdiction("state", "BOEM", False)


# --------------------------------------------- jurisdiction stays out of scores


def test_no_score_function_takes_jurisdiction():
    """Req 8.2: structurally, no score function receives jurisdiction fields."""
    banned = ("jurisdiction", "straddl")
    offenders = []
    for path in sorted(SCORE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if "jurisdiction" in node.module:
                    offenders.append(f"{path.name}: imports {node.module}")
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                a = node.args
                names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
                names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
                for n in names:
                    low = n.lower()
                    if low == "agency" or any(b in low for b in banned):
                        offenders.append(f"{path.name}:{node.name}({n})")
    assert offenders == []
