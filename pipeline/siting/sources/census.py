"""Shoreline / land mask: US Census cartographic boundary states 1:500k (Req 1.4, 1.5).

``fetch_shoreline(client)`` downloads ``cb_2023_us_state_500k.zip`` through the
fetch layer (cache + retry), keeps STATEFP 25 (MA), 33 (NH) and 44 (RI), and
returns ``(GeoDataFrame, Citation)``. The GeoDataFrame keeps the source CRS
(NAD83, EPSG:4269); ``grid.build`` reprojects it.

The shoreline is required: a failed fetch with no usable cache entry raises
:class:`FatalSourceError` naming the source and failure reason (Req 1.5). A
payload missing any of the three states is treated as an empty response, so it
is never cached and never used.

Source: US Census Bureau, Cartographic Boundary Files 2023 (states, 1:500,000),
https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap, FatalSourceError
from siting.provenance import Citation
from siting.sources.endpoints import CENSUS_STATES

# Name used in the fatal error message (design: Error Handling table).
SOURCE_LABEL = "US Census cb_2023_us_state_500k"

MA, NH, RI = "25", "33", "44"
STATEFPS: tuple[str, ...] = (MA, NH, RI)


def parse(payload: bytes) -> gpd.GeoDataFrame:
    """Zipped shapefile bytes -> MA/NH/RI rows (STATEFP, STUSPS, NAME, geometry).

    Raises :class:`EmptyResponse` when any of the three states is missing, and
    lets other read errors propagate (the fetch layer treats them as unparseable).
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "states.zip"
        path.write_bytes(payload)
        gdf = gpd.read_file(f"zip://{path}")
    if "STATEFP" not in gdf.columns:
        raise ValueError("shapefile has no STATEFP column")
    statefp = gdf["STATEFP"].astype(str).str.zfill(2)
    gdf = gdf.assign(STATEFP=statefp)[statefp.isin(STATEFPS)]
    missing = sorted(set(STATEFPS) - set(gdf["STATEFP"]))
    if missing:
        raise EmptyResponse(f"STATEFP {missing} not in shapefile")
    keep = [c for c in ("STATEFP", "STUSPS", "NAME") if c in gdf.columns]
    out = gdf[[*keep, "geometry"]].sort_values("STATEFP").reset_index(drop=True)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)


def fetch_shoreline(client: FetchClient) -> tuple[gpd.GeoDataFrame, Citation]:
    """Return the MA/NH/RI land polygons and their Citation, or raise fatally."""
    ep = CENSUS_STATES
    result = client.get(ep.source, ep.url, None, parse)
    if isinstance(result, DataGap):
        raise FatalSourceError.from_gap(SOURCE_LABEL, result)
    fetch_date = client.fetch_date(ep.source, ep.url, None) or datetime.now(
        UTC
    ).strftime("%Y-%m-%d")
    citation = Citation(
        id=ep.source,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=ep.url,
        date=fetch_date,
        date_kind="fetch",
    )
    return result, citation
