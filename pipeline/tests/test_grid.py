"""Grid build + Census shoreline source (Req 1.1, 1.4, 1.5, 1.6, 1.7). No network."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import shapely
from shapely.geometry import Point, box

from siting import grid
from siting.config import DeviceConfig
from siting.fetch.client import FetchClient, Response, TransportError
from siting.fetch.gaps import FatalSourceError
from siting.sources import census
from siting.sources.endpoints import CENSUS_STATES

MI = grid.M_PER_MI
NM = grid.M_PER_NM


def shoreline(*rows: tuple[str, shapely.Geometry]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"STATEFP": [r[0] for r in rows]},
        geometry=[r[1] for r in rows],
        crs=grid.UTM_CRS,
    )


# Synthetic coast in UTM 19N: MA square with an island, NH north of it sharing
# the top edge (junction at its east end), RI west of it sharing the left edge
# (junction at its south end).
MA_BOX = box(300_000, 4_600_000, 340_000, 4_640_000)
ISLAND = box(360_000, 4_590_000, 365_000, 4_595_000)
NH_BOX = box(300_000, 4_640_000, 330_000, 4_680_000)
RI_BOX = box(260_000, 4_600_000, 300_000, 4_630_000)


def three_states() -> gpd.GeoDataFrame:
    return shoreline(
        (grid.MA, shapely.union(MA_BOX, ISLAND)),
        (grid.NH, NH_BOX),
        (grid.RI, RI_BOX),
    )


def cfg(cell_mi: float = 1.0, extent_nm: float = 12.0) -> DeviceConfig:
    return DeviceConfig(cell_size_mi=cell_mi, outer_extent_nm=extent_nm)


# ------------------------------------------------------------------- grid


def test_columns_ids_and_alignment():
    g = grid.build(cfg(1.0, 6.0), three_states())
    assert list(g.columns) == [*grid.COLUMNS, "geometry"]
    assert g.crs.to_string() == grid.WGS84
    assert g["geometry_utm"].crs.to_string() == grid.UTM_CRS
    assert len(g) > 0 and g["cell_id"].is_unique
    s = MI
    row = g.iloc[0]
    assert row.cell_id == f"C1609-{row.i:04d}-{row.j:04d}"
    minx, miny, maxx, maxy = row.geometry_utm.bounds
    assert minx == pytest.approx(grid.X0 + row.i * s)
    assert miny == pytest.approx(grid.Y0 + row.j * s)
    assert maxx - minx == pytest.approx(s) and maxy - miny == pytest.approx(s)


def test_centroid_latlon_rounded_and_matches_geometry():
    g = grid.build(cfg(1.0, 6.0), three_states())
    assert np.array_equal(g["centroid_lat"], g["centroid_lat"].round(6))
    c = g.geometry.iloc[0].centroid
    assert c.y == pytest.approx(g["centroid_lat"].iloc[0], abs=1e-4)
    assert c.x == pytest.approx(g["centroid_lon"].iloc[0], abs=1e-4)


def test_deterministic_across_runs():
    a = grid.build(cfg(0.5, 4.0), three_states())
    b = grid.build(cfg(0.5, 4.0), three_states())
    assert a["cell_id"].tolist() == b["cell_id"].tolist()
    assert a["centroid_lat"].tolist() == b["centroid_lat"].tolist()
    assert a["centroid_lon"].tolist() == b["centroid_lon"].tolist()


def test_no_centroid_on_land_and_island_waters_included():
    shore = three_states()
    g = grid.build(cfg(1.0, 6.0), shore)
    land = shapely.union_all(shore.geometry.values)
    cents = g["geometry_utm"].centroid
    assert not any(land.covers(p) for p in cents)
    # Water just off the island (well beyond MA mainland buffer edge) is gridded.
    near_island = cents.distance(ISLAND) < 2 * MI
    assert near_island.any()


def test_water_cells_inside_extent_are_all_kept():
    # MA alone (no clips): every aligned centroid in the buffer ring is kept.
    shore = shoreline((grid.MA, MA_BOX))
    c = cfg(1.0, 3.0)
    g = grid.build(c, shore)
    ring = MA_BOX.buffer(3.0 * NM).difference(MA_BOX)
    s = MI
    ii = np.arange(-20, 200)
    jj = np.arange(0, 120)
    gx, gy = np.meshgrid(grid.X0 + (ii + 0.5) * s, grid.Y0 + (jj + 0.5) * s)
    expected = int(shapely.contains_xy(ring, gx, gy).sum())
    assert len(g) == expected


def test_outer_extent_limits_distance_from_ma():
    g = grid.build(cfg(1.0, 3.0), three_states())
    ma = shapely.union(MA_BOX, ISLAND)
    assert (g["geometry_utm"].centroid.distance(ma) <= 3.0 * NM).all()


def test_lateral_clips_drop_nh_and_ri_waters():
    g = grid.build(cfg(1.0, 12.0), three_states())
    cents = g["geometry_utm"].centroid
    # NH junction (330000, 4640000): nothing north of its parallel.
    # RI junction (300000, 4600000): nothing west of its meridian.
    _, nh_lat = grid._TO_WGS84.transform(330_000, 4_640_000)
    ri_lon, _ = grid._TO_WGS84.transform(300_000, 4_600_000)
    assert (g["centroid_lat"] <= nh_lat).all()
    assert (g["centroid_lon"] >= ri_lon).all()
    # Waters east of MA and south of the RI junction remain.
    assert (cents.x > 340_000).any() and (cents.y < 4_600_000).any()


def test_smaller_cells_change_ids_and_count():
    a = grid.build(cfg(1.0, 3.0), three_states())
    b = grid.build(cfg(0.25, 3.0), three_states())
    assert a["cell_id"].iloc[0].startswith("C1609-")
    assert b["cell_id"].iloc[0].startswith("C402-")
    assert len(b) > 10 * len(a)


def test_requires_massachusetts():
    with pytest.raises(ValueError, match="Massachusetts"):
        grid.build(cfg(), shoreline((grid.NH, NH_BOX)))


# ----------------------------------------------------------------- census


class FakeTransport:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, url, params):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def states_zip(tmp_path: Path, statefps: list[str]) -> bytes:
    gdf = gpd.GeoDataFrame(
        {
            "STATEFP": statefps,
            "STUSPS": [f"S{s}" for s in statefps],
            "NAME": [f"State {s}" for s in statefps],
        },
        geometry=[Point(-71 + k, 42).buffer(0.1) for k in range(len(statefps))],
        crs="EPSG:4269",
    )
    shp_dir = tmp_path / "shp"
    shp_dir.mkdir()
    gdf.to_file(shp_dir / "cb_2023_us_state_500k.shp")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in sorted(shp_dir.iterdir()):
            zf.write(f, f.name)
    return buf.getvalue()


def make_client(tmp_path: Path, transport) -> FetchClient:
    return FetchClient(tmp_path / "cache", transport=transport, sleep=lambda s: None)


def test_census_filters_states_and_cites(tmp_path):
    payload = states_zip(tmp_path, ["25", "33", "44", "36"])
    client = make_client(tmp_path, FakeTransport(Response(200, payload)))
    gdf, cit = census.fetch_shoreline(client)
    assert gdf["STATEFP"].tolist() == ["25", "33", "44"]
    assert gdf.crs is not None
    assert cit.agency == "US Census Bureau"
    assert cit.endpoint == CENSUS_STATES.url
    assert "cb_2023_us_state_500k" in cit.dataset
    assert cit.date_kind == "fetch" and len(cit.date) == 10


def test_census_uses_cache_without_network(tmp_path):
    payload = states_zip(tmp_path, ["25", "33", "44"])
    census.fetch_shoreline(make_client(tmp_path, FakeTransport(Response(200, payload))))
    offline = FakeTransport(TransportError("offline"))
    gdf, _ = census.fetch_shoreline(make_client(tmp_path, offline))
    assert offline.calls == 0 and len(gdf) == 3


def test_census_fetch_failure_without_cache_is_fatal(tmp_path):
    t = FakeTransport(*[TransportError("connection refused")] * 3)
    with pytest.raises(FatalSourceError) as ei:
        census.fetch_shoreline(make_client(tmp_path, t))
    assert ei.value.source == census.SOURCE_LABEL
    assert "fetch failure" in ei.value.reason
    assert "connection refused" in str(ei.value)
    assert t.calls == 3


def test_census_missing_state_is_fatal_and_not_cached(tmp_path):
    payload = states_zip(tmp_path, ["25", "36"])
    client = make_client(tmp_path, FakeTransport(Response(200, payload)))
    with pytest.raises(FatalSourceError, match="empty response"):
        census.fetch_shoreline(client)
    assert not list((tmp_path / "cache").rglob("*.bin"))


def test_census_shoreline_feeds_grid(tmp_path):
    payload = states_zip(tmp_path, ["25", "33", "44"])
    gdf, _ = census.fetch_shoreline(
        make_client(tmp_path, FakeTransport(Response(200, payload)))
    )
    g = grid.build(cfg(1.0, 3.0), gdf)
    assert len(g) > 0 and g["cell_id"].is_unique
