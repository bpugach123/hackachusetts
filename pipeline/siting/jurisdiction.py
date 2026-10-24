"""State vs federal jurisdiction per Cell (Req 8.1-8.5).

``assign(client, cells, land)`` classifies every Cell by its centroid against
the MA state-waters polygon (the Submerged Lands Act 3 nm boundary):

* centroid covered by the state-waters polygon (inside or on the boundary)
  -> ``{"jurisdiction": "state", "agency": "MA CZM"}``;
* anything else -> ``{"jurisdiction": "federal", "agency": "BOEM"}`` (Req 8.1);
* ``straddles_boundary`` is true when the 3 nm boundary line passes through the
  cell polygon's interior. The cell is still classified by its centroid (Req 8.3);
* every record carries ``disclaimer: "Not legal advice"`` (Req 8.5).

Sources (``sources/endpoints.py``, ``MARINECADASTRE_SUBMERGED_LANDS``):

1. NOAA OCM MarineCadastre "US State Submerged Lands" (MapServer/0). The layer is
   one US-wide polygon with no state field, so it is clipped to the grid bbox,
   which selects MA waters (the grid itself is already clipped to MA). The query
   generalizes geometry to ``MAX_OFFSET_DEG`` (~20 m) to keep the payload small.
2. Fallback: BOEM "Submerged Lands Act Boundary" (MMC_Layers/FeatureServer/8).
   This layer is lines only, so the state side is found by polygonizing the
   lines with the bbox edge and keeping the faces that touch land (the US Census
   shoreline passed as ``land``). Without ``land`` the line cannot be oriented,
   so the fallback yields a gap instead of a guess.

If both fail or return nothing usable, every Cell gets ``jurisdiction`` and
``agency`` null plus a ``jurisdiction`` Data_Gap (Req 8.4). No value is
fabricated.

The boundary line is exported by :func:`boundary_geojson` for
``boundary_3nm.geojson``. For the MarineCadastre polygon the line is its outline
minus the bbox clip edges and minus anything within ``LAND_CLEARANCE_M`` of land
(the shoreline side of the polygon). For the BOEM fallback it is the SLA line.

Jurisdiction never feeds a score (Req 8.2): nothing in ``siting.score`` takes a
jurisdiction, agency, or straddle argument, and this module returns its own
records, separate from every Component_Score. ``tests/test_jurisdiction.py``
checks the score signatures structurally.

The Tier 2 regulatory proxy here is NOT legal advice.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from siting.fetch.client import EmptyResponse, FetchClient, require_features
from siting.fetch.gaps import DataGap, GapRegistry
from siting.provenance import Citation
from siting.sources.endpoints import MARINECADASTRE_SUBMERGED_LANDS

DISCLAIMER = "Not legal advice"
FACTOR = "jurisdiction"

Jurisdiction = Literal["state", "federal"]
Agency = Literal["MA CZM", "BOEM"]
STATE: Jurisdiction = "state"
FEDERAL: Jurisdiction = "federal"
AGENCY: dict[str, Agency] = {STATE: "MA CZM", FEDERAL: "BOEM"}

PRIMARY_SOURCE = MARINECADASTRE_SUBMERGED_LANDS.source
FALLBACK_SOURCE = "BOEM_SLA"  # provenance.EXTERNAL_SOURCES id for the fallback
PRIMARY_CITATION_ID = "marinecadastre-ssl"  # matches output/sample/metadata.json
FALLBACK_CITATION_ID = "boem-sla"

WGS84 = "EPSG:4326"
UTM_CRS = "EPSG:26919"
BBOX_MARGIN_DEG = 0.1  # query box = cell bounds + margin, snapped outward
BBOX_SNAP_DEG = 0.01  # stable cache key across tiny bound changes
MAX_OFFSET_DEG = 0.0002  # ArcGIS maxAllowableOffset (~20 m) for the big polygon
LAND_CLEARANCE_M = 926.0  # 0.5 nm: polygon edges closer to land are shoreline
_EDGE_EPS_DEG = 1e-7  # strips segments lying on the bbox clip edge
EXPORT_DP = 6

BBox = tuple[float, float, float, float]


# ----------------------------------------------------------------------- models


@dataclass(frozen=True)
class CellJurisdiction:
    """Jurisdiction fields for one Cell (CellRecord fields in docs/schema.md)."""

    jurisdiction: Jurisdiction | None
    agency: Agency | None
    straddles_boundary: bool
    gap: DataGap | None = None
    disclaimer: str = DISCLAIMER

    def __post_init__(self) -> None:
        expected = None if self.jurisdiction is None else AGENCY[self.jurisdiction]
        if self.agency != expected:
            raise ValueError(
                f"agency {self.agency!r} does not match jurisdiction "
                f"{self.jurisdiction!r}"
            )
        if (self.jurisdiction is None) != (self.gap is not None):
            raise ValueError("jurisdiction is null if and only if a gap is set")

    @property
    def gaps(self) -> list[DataGap]:
        return [] if self.gap is None else [self.gap]

    def to_dict(self) -> dict:
        """CellRecord fields. The gap goes into the cell's ``gaps`` list."""
        return {
            "jurisdiction": self.jurisdiction,
            "agency": self.agency,
            "straddles_boundary": self.straddles_boundary,
            "disclaimer": self.disclaimer,
        }


@dataclass(frozen=True)
class Boundary:
    """State-waters polygon and 3 nm line (both WGS84) with their Citation."""

    state_waters: BaseGeometry
    line: BaseGeometry
    citation: Citation


@dataclass
class JurisdictionResult:
    cells: dict[str, CellJurisdiction] = field(default_factory=dict)
    boundary: Boundary | None = None
    gap: DataGap | None = None

    @property
    def citation(self) -> Citation | None:
        return None if self.boundary is None else self.boundary.citation

    def boundary_geojson(self) -> dict:
        return boundary_geojson(self.boundary)


# ------------------------------------------------------------- geometry helpers


def _parts(geom: BaseGeometry) -> list[BaseGeometry]:
    return [g for g in shapely.get_parts(geom) if not g.is_empty]


def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """Polygon parts of ``geom`` (any type) unioned; empty Polygon if none."""
    polys: list[BaseGeometry] = []
    for g in _parts(geom):
        if g.geom_type in ("Polygon", "MultiPolygon"):
            polys.append(g)
        elif g.geom_type == "GeometryCollection":
            polys.append(_polygonal(g))
    polys = [p for p in polys if not p.is_empty]
    return shapely.union_all(polys) if polys else shapely.Polygon()


def _lineal(geom: BaseGeometry) -> BaseGeometry:
    """Line parts of ``geom`` merged; empty LineString if none."""
    lines: list[BaseGeometry] = []
    for g in _parts(geom):
        if g.geom_type in ("LineString", "LinearRing"):
            lines.append(shapely.LineString(g.coords))
        elif g.geom_type == "MultiLineString":
            lines.extend(_parts(g))
        elif g.geom_type == "GeometryCollection":
            sub = _lineal(g)
            if not sub.is_empty:
                lines.extend(_parts(sub))
    if not lines:
        return shapely.LineString()
    return shapely.line_merge(shapely.union_all(lines))


def _inner_box(bbox: BBox) -> BaseGeometry:
    xmin, ymin, xmax, ymax = bbox
    e = _EDGE_EPS_DEG
    return box(xmin + e, ymin + e, xmax - e, ymax - e)


def _to_crs(geom: BaseGeometry, src: str, dst: str) -> BaseGeometry:
    return gpd.GeoSeries([geom], crs=src).to_crs(dst).iloc[0]


def query_bbox(cells: gpd.GeoDataFrame, margin: float = BBOX_MARGIN_DEG) -> BBox:
    """WGS84 bounds of ``cells`` plus ``margin``, snapped outward to 0.01 deg."""
    geoms = cells.geometry
    if geoms.crs is not None and geoms.crs.to_string() != WGS84:
        geoms = geoms.to_crs(WGS84)
    xmin, ymin, xmax, ymax = (float(v) for v in geoms.total_bounds)
    s = BBOX_SNAP_DEG
    return (
        round(math.floor((xmin - margin) / s) * s, 2),
        round(math.floor((ymin - margin) / s) * s, 2),
        round(math.ceil((xmax + margin) / s) * s, 2),
        round(math.ceil((ymax + margin) / s) * s, 2),
    )


def land_from_shoreline(shoreline: gpd.GeoDataFrame) -> BaseGeometry:
    """All land polygons in ``shoreline`` (census MA/NH/RI) unioned, in WGS84."""
    gs = shoreline.geometry
    if gs.crs is not None and gs.crs.to_string() != WGS84:
        gs = gs.to_crs(WGS84)
    return shapely.union_all(list(gs.values))


# --------------------------------------------------------------------- queries


def query_params(bbox: BBox, *, generalize: bool) -> dict:
    """ArcGIS ``/query`` params: features intersecting ``bbox`` as WGS84 GeoJSON."""
    xmin, ymin, xmax, ymax = bbox
    params = {
        "where": "1=1",
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    if generalize:
        params["maxAllowableOffset"] = str(MAX_OFFSET_DEG)
        params["geometryPrecision"] = str(EXPORT_DP)
    return params


def _load_features(payload: bytes) -> list[dict]:
    """ArcGIS GeoJSON bytes -> features. Errors and partial pages are rejected."""
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    if "error" in data:
        err = data["error"]
        msg = err.get("message", "error") if isinstance(err, dict) else str(err)
        raise ValueError(f"ArcGIS error: {msg}")
    props = data.get("properties") or {}
    if data.get("exceededTransferLimit") or props.get("exceededTransferLimit"):
        # A partial boundary would misclassify cells; never use it.
        raise ValueError("exceededTransferLimit: boundary response is partial")
    return [f for f in require_features(data)["features"] if f.get("geometry")]


def parse_state_waters(bbox: BBox) -> Callable[[bytes], BaseGeometry]:
    """Parser for the MarineCadastre polygon layer, clipped to ``bbox``."""

    def parse(payload: bytes) -> BaseGeometry:
        clipped = []
        for f in _load_features(payload):
            g = shape(f["geometry"])
            if g.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            # Clip first: make_valid on the US-wide polygon is far slower.
            c = shapely.clip_by_rect(g, *bbox)
            if not c.is_empty:
                clipped.append(_polygonal(shapely.make_valid(c)))
        state = _polygonal(shapely.union_all(clipped)) if clipped else None
        if state is None or state.is_empty:
            raise EmptyResponse("no state submerged lands inside the grid bbox")
        return state

    return parse


def parse_boundary_lines(bbox: BBox) -> Callable[[bytes], BaseGeometry]:
    """Parser for the BOEM SLA line layer, clipped to ``bbox``."""

    def parse(payload: bytes) -> BaseGeometry:
        lines = [
            g
            for g in (shape(f["geometry"]) for f in _load_features(payload))
            if g.geom_type in ("LineString", "MultiLineString")
        ]
        if not lines:
            raise EmptyResponse("no boundary lines in response")
        clipped = _lineal(shapely.union_all(lines).intersection(box(*bbox)))
        if clipped.is_empty:
            raise EmptyResponse("no boundary lines inside the grid bbox")
        return clipped

    return parse


# ------------------------------------------------------------ boundary building


def boundary_line(
    state_waters: BaseGeometry, bbox: BBox, land: BaseGeometry | None = None
) -> BaseGeometry:
    """3 nm line from the state-waters polygon outline.

    Drops the bbox clip edges. With ``land``, also drops outline within
    ``LAND_CLEARANCE_M`` of land, which is the shoreline side of the polygon.
    """
    outline = _lineal(state_waters.boundary.intersection(_inner_box(bbox)))
    if land is None or land.is_empty or outline.is_empty:
        return outline
    outline_utm = _to_crs(outline, WGS84, UTM_CRS)
    shore_utm = _to_crs(land, WGS84, UTM_CRS).buffer(LAND_CLEARANCE_M)
    return _lineal(_to_crs(outline_utm.difference(shore_utm), UTM_CRS, WGS84))


def state_waters_from_lines(
    lines: BaseGeometry, land: BaseGeometry | None, bbox: BBox
) -> BaseGeometry:
    """Faces of (lines + bbox edge) that touch land, unioned.

    Raises :class:`EmptyResponse` when the line cannot be oriented: no land, or
    the line does not separate any land-free (federal) face from the land side.
    """
    if land is None or land.is_empty:
        raise EmptyResponse("no shoreline to orient the boundary line")
    frame = box(*bbox)
    noded = shapely.union_all([lines.intersection(frame), frame.boundary])
    faces = _parts(shapely.polygonize(_parts(noded)))
    if not faces:
        raise EmptyResponse("boundary lines enclose no area")
    shapely.prepare(land)
    state = [f for f in faces if land.intersects(f)]
    if not state or len(state) == len(faces):
        raise EmptyResponse("boundary lines do not separate state and federal waters")
    return shapely.union_all(state)


def _citation(
    client: FetchClient,
    cid: str,
    source: str,
    agency: str,
    dataset: str,
    url: str,
    params: dict,
    note: str,
) -> Citation:
    date = client.fetch_date(source, url, params) or datetime.now(UTC).strftime(
        "%Y-%m-%d"
    )
    return Citation(
        id=cid,
        agency=agency,
        dataset=dataset,
        endpoint=url,
        date=date,
        date_kind="fetch",
        note=note,
    )


def fetch_boundary(
    client: FetchClient, bbox: BBox, land: BaseGeometry | None = None
) -> Boundary | DataGap:
    """MarineCadastre polygon, else the BOEM SLA line, else a jurisdiction gap."""
    ep = MARINECADASTRE_SUBMERGED_LANDS
    url = ep.url.rstrip("/") + "/query"
    params = query_params(bbox, generalize=True)
    primary = client.get(
        ep.source, url, params, parse_state_waters(bbox), factor=FACTOR
    )
    if not isinstance(primary, DataGap):
        citation = _citation(
            client,
            PRIMARY_CITATION_ID,
            ep.source,
            ep.agency,
            f"{ep.dataset} (3 nm boundary)",
            url,
            params,
            note=f"generalized to {MAX_OFFSET_DEG} deg; Not legal advice",
        )
        return Boundary(primary, boundary_line(primary, bbox, land), citation)

    fb_url = (ep.fallback or "").rstrip("/") + "/query"
    fb_params = query_params(bbox, generalize=False)
    lines = (
        client.get(
            FALLBACK_SOURCE,
            fb_url,
            fb_params,
            parse_boundary_lines(bbox),
            factor=FACTOR,
        )
        if ep.fallback
        else DataGap(FACTOR, FALLBACK_SOURCE, "fetch failure", detail="no fallback")
    )
    fallback_gap: DataGap | None = lines if isinstance(lines, DataGap) else None
    if fallback_gap is None:
        try:
            state = state_waters_from_lines(lines, land, bbox)
        except EmptyResponse as exc:
            fallback_gap = DataGap(FACTOR, FALLBACK_SOURCE, "empty response", str(exc))
        else:
            citation = _citation(
                client,
                FALLBACK_CITATION_ID,
                FALLBACK_SOURCE,
                "BOEM",
                "Submerged Lands Act Boundary (MMC_Layers layer 8)",
                fb_url,
                fb_params,
                note="state side oriented with the US Census shoreline; "
                "Not legal advice",
            )
            return Boundary(state, lines, citation)

    detail = (
        f"{primary.reason}"
        + (f" ({primary.detail})" if primary.detail else "")
        + f"; fallback {FALLBACK_SOURCE}: {fallback_gap.reason}"
        + (f" ({fallback_gap.detail})" if fallback_gap.detail else "")
    )
    return DataGap(FACTOR, PRIMARY_SOURCE, primary.reason, detail=detail)


# --------------------------------------------------------------- classification


def classify(
    cells: gpd.GeoDataFrame, boundary: Boundary
) -> dict[str, CellJurisdiction]:
    """Per-cell jurisdiction by centroid; straddle when the line crosses the cell.

    ``cells`` needs ``cell_id``, ``centroid_lon``, ``centroid_lat`` and polygon
    geometry (reprojected to WGS84 if needed). Pure: no I/O.
    """
    geoms = cells.geometry
    if geoms.crs is not None and geoms.crs.to_string() != WGS84:
        geoms = geoms.to_crs(WGS84)
    polys = np.asarray(geoms.array, dtype=object)
    pts = shapely.points(
        np.asarray(cells["centroid_lon"], dtype=float),
        np.asarray(cells["centroid_lat"], dtype=float),
    )
    state_poly = boundary.state_waters
    shapely.prepare(state_poly)
    in_state = shapely.covers(state_poly, pts)

    line = boundary.line
    if line.is_empty:
        straddles = np.zeros(len(polys), dtype=bool)
    else:
        shapely.prepare(line)
        # Crosses = meets the cell's interior, not just its edge or a corner.
        straddles = shapely.intersects(line, polys) & ~shapely.touches(line, polys)

    out: dict[str, CellJurisdiction] = {}
    for cid, s, x in zip(cells["cell_id"], in_state, straddles, strict=True):
        j = STATE if bool(s) else FEDERAL
        out[str(cid)] = CellJurisdiction(j, AGENCY[j], bool(x))
    return out


def gap_records(cell_ids: Iterable[str], gap: DataGap) -> dict[str, CellJurisdiction]:
    """Null jurisdiction + the gap on every cell (Req 8.4)."""
    g = gap.for_factor(FACTOR)
    return {str(c): CellJurisdiction(None, None, False, gap=g) for c in cell_ids}


def assign(
    client: FetchClient,
    cells: gpd.GeoDataFrame,
    land: BaseGeometry | None = None,
    registry: GapRegistry | None = None,
) -> JurisdictionResult:
    """Fetch the boundary and classify every cell; register gaps if given."""
    if len(cells) == 0:
        return JurisdictionResult()
    boundary = fetch_boundary(client, query_bbox(cells), land)
    if isinstance(boundary, DataGap):
        records = gap_records(cells["cell_id"], boundary)
        if registry is not None:
            for cid, rec in records.items():
                registry.extend(cid, rec.gaps)
        return JurisdictionResult(records, None, boundary.for_factor(FACTOR))
    return JurisdictionResult(classify(cells, boundary), boundary, None)


# ---------------------------------------------------------------------- export


def boundary_geojson(boundary: Boundary | None) -> dict:
    """``boundary_3nm.geojson`` content; ``features`` is empty on a gap."""
    if boundary is None or boundary.line.is_empty:
        return {"type": "FeatureCollection", "features": []}
    line = _lineal(shapely.set_precision(boundary.line, 10.0**-EXPORT_DP))
    if line.is_empty:
        return {"type": "FeatureCollection", "features": []}
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(shapely.to_geojson(line)),
                "properties": {
                    "citation_id": boundary.citation.id,
                    "disclaimer": DISCLAIMER,
                    "name": "MA state/federal 3 nm boundary (Submerged Lands Act)",
                },
            }
        ],
    }
