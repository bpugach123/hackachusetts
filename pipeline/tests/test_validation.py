"""Known-site validation report (Req 15.1-15.6). No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from siting.validation import SITES, percentile, report, report_json

ROOT = Path(__file__).resolve().parents[2]


def cell(cid, lat, lon, ts=None, rf=None, **extra):
    return {
        "cell_id": cid,
        "centroid_lat": lat,
        "centroid_lon": lon,
        "tidal_stream_sub": ts,
        "rise_fall_sub": rf,
        **extra,
    }


def by_site(entries):
    return {e.site: e for e in entries}


def test_sites_match_req_15_1():
    got = {(s.name, s.lat, s.lon, s.path) for s in SITES}
    assert got == {
        ("Muskeget Channel", 41.360, -70.400, "tidal_stream"),
        ("Woods Hole", 41.521, -70.685, "tidal_stream"),
        ("Cape Cod Canal/Bourne", 41.741, -70.616, "tidal_stream"),
        ("Boston Harbor approaches", 42.330, -70.950, "rise_fall"),
        ("Cape Cod Bay", 41.850, -70.350, "rise_fall"),
    }


def test_percentile_counts_ties_as_at_or_below():
    assert percentile(3.0, [1.0, 2.0, 3.0, 4.0]) == 75.0
    assert percentile(4.0, [4.0, 4.0]) == 100.0
    with pytest.raises(ValueError):
        percentile(1.0, [])


def test_pass_fail_gap_and_out_of_range():
    cells = [
        # Muskeget: nearest is a high stream cell, flagged excluded (still matched).
        cell("C1609-0001-0001", 41.361, -70.401, ts=90.0, hard_exclusion=True),
        cell("C1609-0001-0002", 41.40, -70.40, ts=10.0),
        cell("C1609-0001-0003", 41.45, -70.40, ts=20.0),
        cell("C1609-0001-0004", 41.46, -70.40, ts=30.0),
        # Woods Hole: low stream value -> failure.
        cell("C1609-0002-0001", 41.520, -70.684, ts=20.0),
        # Cape Cod Canal: gap on the stream path.
        cell("C1609-0003-0001", 41.741, -70.615, ts=None, rf=50.0),
        # Boston Harbor: rise/fall pass (only cell with a rf value besides canal).
        cell("C1609-0004-0001", 42.331, -70.951, rf=80.0),
        # Cape Cod Bay: nothing within 3 mi.
    ]
    out = by_site(report(cells, {"C1609-0003-0001": {"tidal_stream": "no coverage"}}))

    m = out["Muskeget Channel"]
    assert (m.matched_cell_id, m.status, m.reason) == ("C1609-0001-0001", "pass", None)
    assert m.resource_percentile == 100.0  # 5 non-null stream values, 90 is max
    assert 0 < m.distance_mi < 0.2

    w = out["Woods Hole"]
    assert w.status == "validation failure"
    assert w.resource_percentile == 60.0  # {10, 20, 20} <= 20 of 5

    c = out["Cape Cod Canal/Bourne"]
    assert c.status == "not evaluated" and c.resource_percentile is None
    assert c.matched_cell_id == "C1609-0003-0001"
    assert "tidal_stream_sub" in c.reason and "no coverage" in c.reason

    b = out["Boston Harbor approaches"]
    assert b.status == "pass" and b.resource_percentile == 100.0

    bay = out["Cape Cod Bay"]
    assert bay.status == "not evaluated"
    assert bay.matched_cell_id is None and bay.distance_mi is None
    assert "3 mi" in bay.reason


def test_empty_grid_marks_every_site_not_evaluated():
    out = report([])
    assert len(out) == 5
    assert all(e.status == "not evaluated" and e.reason for e in out)


def test_sample_cells_reproduce_sample_report_and_schema():
    jsonschema = pytest.importorskip("jsonschema")
    sample = ROOT / "output" / "sample"
    feats = json.loads((sample / "cells.geojson").read_text())["features"]
    got = report_json(f["properties"] for f in feats)

    schema = json.loads((ROOT / "docs/schema/validation.schema.json").read_text())
    jsonschema.validate(got, schema)
    want = json.loads((sample / "validation.json").read_text())
    assert [(e["site"], e["matched_cell_id"], e["status"]) for e in got] == [
        (e["site"], e["matched_cell_id"], e["status"]) for e in want
    ]
