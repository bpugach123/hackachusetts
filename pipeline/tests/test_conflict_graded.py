"""Graded Conflict sub-scores and component (Req 7.4, 7.5, 7.6).

No network: sources run against fake transports; scoring uses hand-built inputs.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import numpy as np
import pandas as pd
import pytest
import shapely
from pyproj import Transformer
from shapely.geometry import Point, box

from siting import provenance
from siting.config import DeviceConfig
from siting.fetch.client import FetchClient, Response
from siting.fetch.gaps import DataGap
from siting.provenance import EXTERNAL_ADDITION, PROXY, QUANTIFIED, FactorResult
from siting.score import conflict as cf
from siting.sources import emolt, neodp
from siting.sources.arcgis import Layer, citation_for
from siting.sources.emolt import FishingPoints
from siting.sources.endpoints import (
    EMOLT,
    NEODP_DIADROMOUS,
    NEODP_VESSEL_TRAFFIC,
    NOAA_FISHERIES_RIGHT_WHALE,
)
from siting.sources.neodp import VesselSamples

TO_LL = Transformer.from_crs("EPSG:26919", "EPSG:4326", always_xy=True)
X0, Y0 = 340000.0, 4690000.0  # UTM 19N, near Boston Harbor
S = 1609.344  # 1 mi cell
DATE = "2026-10-24"


def ll(geom):
    return shapely.transform(
        geom, lambda c: np.column_stack(TO_LL.transform(c[:, 0], c[:, 1]))
    )


def cells_df(n: int) -> pd.DataFrame:
    polys = [box(X0 + i * S, Y0, X0 + (i + 1) * S, Y0 + S) for i in range(n)]
    return pd.DataFrame(
        {"cell_id": [f"C-{i}" for i in range(n)], "geometry_utm": polys}
    )


def center(i: int) -> tuple[float, float]:
    return X0 + (i + 0.5) * S, Y0 + 0.5 * S


def layer(ep, geoms_utm, cid: str) -> Layer:
    return Layer(
        ep.source,
        [ll(g) for g in geoms_utm],
        list(geoms_utm),
        [{} for _ in geoms_utm],
        citation_for(cid, ep, date=DATE),
        ep.url,
    )


def vessel(values, gaps=None) -> VesselSamples:
    gaps = gaps or [
        None
        if v is not None
        else DataGap("*", NEODP_VESSEL_TRAFFIC.source, "no coverage", "NoData")
        for v in values
    ]
    return VesselSamples(list(values), gaps, neodp.vessel_citation(date=DATE), "u", 198)


def fishing(points_utm) -> FishingPoints:
    pts = shapely.points(np.asarray(points_utm, float).reshape(-1, 2))
    return FishingPoints(
        [str(i) for i in range(len(pts))],
        np.zeros(len(pts)),
        np.zeros(len(pts)),
        pts,
        emolt.emolt_citation(date=DATE),
        EMOLT.url,
    )


def gap(source: str) -> DataGap:
    return DataGap("*", source, "fetch failure", "HTTP 503")


def all_gaps() -> cf.ConflictInputs:
    return cf.ConflictInputs(
        vessel=gap(NEODP_VESSEL_TRAFFIC.source),
        fishing=gap(EMOLT.source),
        fish_stations=gap(NEODP_DIADROMOUS.source),
        whale=gap(NOAA_FISHERIES_RIGHT_WHALE.source),
    )


def full_inputs(n: int = 3) -> cf.ConflictInputs:
    # Station at cell 0's centroid; whale polygon over cell 2 only; 3 tows
    # near cell 1 and 1 near cell 0 (cells are 1.6 km apart, radius 5 km).
    x2 = X0 + 2 * S
    return cf.ConflictInputs(
        vessel=vessel([10.0, 40.0, 40.0][:n]),
        fishing=fishing([center(0), center(1), center(1), center(1)]),
        fish_stations=layer(NEODP_DIADROMOUS, [Point(center(0))], "neodp-diadromous"),
        whale=layer(
            NOAA_FISHERIES_RIGHT_WHALE,
            [box(x2 + 100, Y0 + 100, x2 + 500, Y0 + 500)],
            cf.WHALE_CITATION_ID,
        ),
    )


def cell_records(results: dict[str, FactorResult]):
    return [{"cell_id": c, "factors": r.factors} for c, r in results.items()]


# --------------------------------------------------------------- percentile


def test_percentile_min_rank_ties_and_nulls():
    assert cf.percentile_ranks([5.0, 1.0, None, 5.0, 9.0]) == [
        pytest.approx(100 / 3),
        0.0,
        None,
        pytest.approx(100 / 3),
        100.0,
    ]
    assert cf.percentile_ranks([7.0]) == [0.0]
    assert cf.percentile_ranks([0.0, 0.0]) == [0.0, 0.0]
    assert cf.percentile_ranks([None, float("nan")]) == [None, None]


# ---------------------------------------------------------- graded scoring


def test_subscores_tags_citations_and_component():
    cells = cells_df(3)
    res = cf.score_conflict(cells, full_inputs(), DeviceConfig())
    assert provenance.validate(cell_records(res)) == []

    r0, r1, r2 = res["C-0"], res["C-1"], res["C-2"]
    # vessel: [10, 40, 40] -> min-rank percentiles [0, 50, 50]
    assert [r.factors["vessel_traffic"].value for r in (r0, r1, r2)] == [0, 50, 50]
    # fishing: every tow is within 5 km of every cell -> equal counts -> 0
    assert r1.factors["fishing_activity"].note.startswith("4 eMOLT")
    # diadromous: 0 km at cell 0 -> 100; 1.61 km at cell 1 -> ramp
    assert r0.factors["diadromous_fish_km"].value == 0.0
    d1 = r1.factors["diadromous_fish_km"].value
    assert d1 == pytest.approx(1.61, abs=0.01)
    assert r0.extra["conflict_subscores"]["diadromous_fish"] == 100.0
    assert r1.extra["conflict_subscores"]["diadromous_fish"] == pytest.approx(
        100 * (1 - d1 / 5.0)
    )
    # whale: only cell 2 intersects
    assert [r.factors["whale_critical_habitat"].value for r in (r0, r1, r2)] == [
        0.0,
        0.0,
        100.0,
    ]

    # Tags: PROXY (kit-equivalent) vs QUANTIFIED + EXTERNAL ADDITION (whale).
    for name in ("vessel_traffic", "fishing_activity", "diadromous_fish_km"):
        assert r0.factors[name].tags == [PROXY]
    assert r0.factors["whale_critical_habitat"].tags == [QUANTIFIED, EXTERNAL_ADDITION]
    assert r0.factors["vessel_traffic"].citation.id == "neodp-vessel"
    assert r0.factors["fishing_activity"].citation.id == "emolt"
    assert r0.factors["diadromous_fish_km"].citation.id == "neodp-diadromous"
    assert r0.factors["whale_critical_habitat"].citation.id == "nmfs-narw-ch"

    # Component = equal-weight mean of the four sub-scores.
    for r in (r0, r1, r2):
        subs = r.extra["conflict_subscores"]
        assert r.score == pytest.approx(np.mean(list(subs.values())))
        assert 0.0 <= r.score <= 100.0
        assert r.gaps == []


def test_configured_weights_and_missing_subscore_renormalizes():
    cells = cells_df(3)
    inputs = full_inputs()
    inputs.vessel = vessel([None, 40.0, 10.0])
    cfg = DeviceConfig(
        conflict_weights={"whale_critical_habitat": 3.0, "vessel_traffic": 1.0}
    )
    res = cf.score_conflict(cells, inputs, cfg)
    assert provenance.validate(cell_records(res)) == []
    # C-2: vessel 0 (lowest), whale 100 -> (1*0 + 3*100) / 4
    assert res["C-2"].score == pytest.approx(75.0)
    # C-0: vessel NoData -> gap; only whale (0) carries weight
    r0 = res["C-0"]
    assert r0.factors["vessel_traffic"].value is None
    assert r0.factors["vessel_traffic"].gap.reason == "no coverage"
    assert r0.score == 0.0
    assert "vessel_traffic" in r0.omitted


def test_unknown_conflict_weight_key_is_rejected():
    cfg = DeviceConfig(conflict_weights={"whales": 1.0})
    with pytest.raises(ValueError, match="whales"):
        cf.score_conflict(cells_df(1), full_inputs(1), cfg)


def test_all_subscores_null_gives_null_component_and_conflict_gap():
    res = cf.score_conflict(cells_df(2), all_gaps(), DeviceConfig())
    assert provenance.validate(cell_records(res)) == []
    for r in res.values():
        assert r.score is None
        assert all(f.value is None and f.gap is not None for f in r.factors.values())
        conflict_gaps = [g for g in r.gaps if g.factor == "conflict"]
        assert len(conflict_gaps) == 1
        assert conflict_gaps[0].reason == "fetch failure"
        assert set(r.omitted) == set(cf.CONFLICT_SUBSCORES)


def test_excluded_cells_keep_exclusion_fields_and_get_no_component():
    cells = cells_df(2)
    excl = {
        "C-0": FactorResult(
            extra={cf.HARD_EXCLUSION: True, cf.EXCLUSION_LAYERS: ["shipping_lanes"]}
        ),
        "C-1": FactorResult(extra={cf.HARD_EXCLUSION: False, cf.EXCLUSION_LAYERS: []}),
    }
    res = cf.score_conflict(cells, full_inputs(2), DeviceConfig(), excl)
    assert res["C-0"].score is None
    assert res["C-0"].extra[cf.EXCLUSION_LAYERS] == ["shipping_lanes"]
    assert not [g for g in res["C-0"].gaps if g.factor == "conflict"]
    assert "whale_critical_habitat" in res["C-0"].factors
    assert res["C-1"].score is not None
    assert res["C-1"].extra[cf.HARD_EXCLUSION] is False


def test_diadromous_station_inside_cell_never_excludes():
    cells = cells_df(1)
    excl = cf.exclusions(cells, {})  # no exclusion layers configured
    res = cf.score_conflict(cells, full_inputs(1), DeviceConfig(), excl)
    r = res["C-0"]
    assert r.extra["conflict_subscores"]["diadromous_fish"] == 100.0
    assert r.extra[cf.HARD_EXCLUSION] is False
    assert r.score is not None


def test_registry_kit_equivalence():
    assert provenance.is_kit_equivalent(NEODP_VESSEL_TRAFFIC.source)
    assert provenance.is_kit_equivalent(NEODP_DIADROMOUS.source)
    assert provenance.is_kit_equivalent(EMOLT.source)
    assert not provenance.is_kit_equivalent(NOAA_FISHERIES_RIGHT_WHALE.source)
    assert provenance.is_known_source(NOAA_FISHERIES_RIGHT_WHALE.source)


# ------------------------------------------------------------------ sources


class Transport:
    """Routes by URL substring; a callable route sees ``(url, params)``."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params or {})))
        for key in sorted(self.routes, key=len, reverse=True):
            if key in url:
                body = self.routes[key]
                if callable(body):
                    body = body(url, dict(params or {}))
                if isinstance(body, int):
                    return Response(body, b"")
                return Response(200, body)
        return Response(404, b"")


def client(tmp_path, transport) -> FetchClient:
    return FetchClient(tmp_path, transport=transport, sleep=lambda s: None)


def identify(value) -> bytes:
    attrs = {"Pixel Value": value} if value is not None else {}
    return json.dumps({"results": [{"layerId": 198, "attributes": attrs}]}).encode()


def test_parse_identify_values_and_nodata():
    assert neodp.parse_identify(identify("38.000000")) == 38.0
    assert neodp.parse_identify(identify("NoData")) is None
    assert neodp.parse_identify(b'{"results": []}') is None
    fb = {"results": [{"attributes": {"UniqueValue.Pixel Value": "2"}}]}
    assert neodp.parse_identify(json.dumps(fb).encode()) == 2.0
    with pytest.raises(ValueError):
        neodp.parse_identify(b'{"error": {"message": "bad"}}')


def test_vessel_samples_per_point_with_nodata(tmp_path):
    def route(url, params):
        x = float(params["geometry"].split(",")[0])
        return identify("NoData" if x > -70.5 else "12")

    t = Transport({"northeastoceandata.org": route})
    got = neodp.sample_vessel_traffic(
        client(tmp_path, t), [(-70.9, 42.3), (-70.1, 42.0)]
    )
    assert isinstance(got, VesselSamples)
    assert got.values == [12.0, None]
    assert got.gaps[1].reason == "no coverage"
    assert all(p["layers"] == "all:198" for _, p in t.calls)
    assert got.citation.note is None


def test_vessel_falls_back_when_primary_probe_fails(tmp_path):
    t = Transport({"northeastoceandata.org": 503, "coast.noaa.gov": identify("3")})
    got = neodp.sample_vessel_traffic(
        client(tmp_path, t), [(-70.9, 42.3), (-70.8, 42.3)]
    )
    assert isinstance(got, VesselSamples)
    assert got.values == [3.0, 3.0]
    assert got.citation.note == "fallback mirror"
    assert "coast.noaa.gov" in got.citation.endpoint
    fb_calls = [p for u, p in t.calls if "coast.noaa.gov" in u]
    assert fb_calls and all(p["layers"] == "all:0" for p in fb_calls)


def test_vessel_all_failed_is_run_wide_gap(tmp_path):
    t = Transport({})
    got = neodp.sample_vessel_traffic(client(tmp_path, t), [(-70.9, 42.3)])
    assert isinstance(got, DataGap)
    assert got.reason == "fetch failure"


EMOLT_CSV = (
    b"tow_id,latitude,longitude\n"
    b",degrees_north,degrees_east\n"
    b"63,41.5253,-70.6742\n"
    b"64,42.3019,-70.689\n"
    b"64,42.3020,-70.689\n"
    b"65,NaN,-70.6\n"
)


def test_emolt_query_and_parse(tmp_path):
    t = Transport({"erddap.emolt.net": EMOLT_CSV})
    got = emolt.fetch_fishing_points(client(tmp_path, t), (-71.3, 41.2, -69.8, 42.9))
    assert isinstance(got, FishingPoints)
    assert got.tow_ids == ["63", "64"]  # duplicate tow and NaN row dropped
    url = unquote(t.calls[0][0])
    assert url.startswith(EMOLT.url + ".csv?tow_id,latitude,longitude&")
    for part in ("latitude>=41.2", "longitude<=-69.8", "segment_type=3"):
        assert part in url
    assert 'orderByMin("tow_id,latitude")' in url
    assert got.citation.id == "emolt"
    assert got.citation.endpoint == EMOLT.url + ".csv"


def test_emolt_empty_or_failed_is_gap(tmp_path):
    header_only = b"tow_id,latitude,longitude\n,degrees_north,degrees_east\n"
    got = emolt.fetch_fishing_points(
        client(tmp_path / "a", Transport({"erddap": header_only})), (0, 0, 1, 1)
    )
    assert isinstance(got, DataGap) and got.reason == "empty response"
    got = emolt.fetch_fishing_points(
        client(tmp_path / "b", Transport({"erddap": 404})), (0, 0, 1, 1)
    )
    assert isinstance(got, DataGap) and got.reason == "fetch failure"
    assert got.source == EMOLT.source
