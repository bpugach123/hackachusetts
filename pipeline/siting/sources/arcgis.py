"""Shared ArcGIS REST feature-layer query (Req 2.2-2.7, 6.1).

Every ArcGIS-backed source (HIFLD, MassGIS, MarineCadastre, ...) goes through
:func:`fetch_layer`:

* Envelope query in WGS84 (``geometryType=esriGeometryEnvelope``, ``inSR`` /
  ``outSR`` 4326), ``returnGeometry=true``, ``f=geojson``.
* Paginated with ``resultOffset`` until the page no longer reports
  ``exceededTransferLimit`` (top-level or under ``properties``, depending on
  the server). Every page goes through :class:`FetchClient` (cache + retry).
* Primary URL first, then the endpoint's ``fallback`` mirror when the primary
  gives a gap. The citation names the URL actually used.
* Zero features (or no usable geometry) -> ``DataGap("empty response")``. An
  ArcGIS ``{"error": ...}`` body -> "fetch failure". ``status="UNRESOLVED"``
  endpoints are not queried and give a "fetch failure" gap. Nothing is
  substituted (Req 2.6, 2.7).

Geometries are returned in both WGS84 and UTM 19N (EPSG:26919, meters), the
projection every distance and intersection test runs in.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import Citation
from siting.sources.endpoints import Endpoint

UTM_CRS = "EPSG:26919"
WGS84 = "EPSG:4326"
PAGE_SIZE = 2000  # HIFLD/MassGIS maxRecordCount; servers may cap lower
MAX_PAGES = 500  # hard stop against a server that never clears the flag
ENVELOPE_DP = 4  # round the envelope so the cache key is stable
_DENSIFY_M = 5000.0

_TO_UTM = Transformer.from_crs(WGS84, UTM_CRS, always_xy=True)
_TO_WGS84 = Transformer.from_crs(UTM_CRS, WGS84, always_xy=True)

Envelope = tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax) lon/lat


# ----------------------------------------------------------------- envelope


def to_utm(geom: BaseGeometry) -> BaseGeometry:
    return shapely.transform(
        geom, lambda c: np.column_stack(_TO_UTM.transform(c[:, 0], c[:, 1]))
    )


def envelope_with_margin(
    bounds_utm: Sequence[float], margin_km: float = 50.0
) -> Envelope:
    """WGS84 envelope covering a UTM bbox expanded by ``margin_km`` (Req 6.1).

    The expanded box is densified before reprojection so the lon/lat envelope
    fully contains it (UTM edges are curved in lon/lat).
    """
    minx, miny, maxx, maxy = (float(v) for v in bounds_utm)
    m = margin_km * 1000.0
    ring = shapely.segmentize(box(minx - m, miny - m, maxx + m, maxy + m), _DENSIFY_M)
    ll = shapely.transform(
        ring, lambda c: np.column_stack(_TO_WGS84.transform(c[:, 0], c[:, 1]))
    )
    x0, y0, x1, y1 = ll.bounds
    f = 10**ENVELOPE_DP
    # Round outward so rounding never shrinks the envelope.
    return (
        float(np.floor(x0 * f) / f),
        float(np.floor(y0 * f) / f),
        float(np.ceil(x1 * f) / f),
        float(np.ceil(y1 * f) / f),
    )


def grid_envelope(cells: Any, margin_km: float = 50.0) -> Envelope:
    """Envelope = Grid bbox + ``margin_km`` for a ``grid.build`` GeoDataFrame."""
    if "geometry_utm" in getattr(cells, "columns", ()):
        bounds = shapely.total_bounds(np.asarray(cells["geometry_utm"].values))
    else:
        x, y = _TO_UTM.transform(
            np.asarray(cells["centroid_lon"], float),
            np.asarray(cells["centroid_lat"], float),
        )
        bounds = (np.min(x), np.min(y), np.max(x), np.max(y))
    return envelope_with_margin(bounds, margin_km)


# -------------------------------------------------------------------- layer


@dataclass
class Layer:
    """Features of one ArcGIS layer inside the envelope."""

    source: str
    geoms_wgs84: list[BaseGeometry]
    geoms_utm: list[BaseGeometry]
    properties: list[dict]
    citation: Citation
    url: str  # layer URL actually used (primary or fallback)
    pages: int = 1
    extra: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.geoms_utm)


@dataclass(frozen=True)
class Page:
    features: list[dict]
    exceeded: bool


def parse_page(payload: bytes, *, first: bool) -> Page:
    """One ``f=geojson`` page. The first page must have features."""
    doc = json.loads(payload)
    if not isinstance(doc, dict):
        raise ValueError("ArcGIS: response is not a JSON object")
    if "error" in doc:
        err = doc["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise ValueError(f"ArcGIS error: {msg}")
    feats = doc.get("features")
    if not isinstance(feats, list):
        raise ValueError("ArcGIS: no 'features' array")
    if first and not feats:
        raise EmptyResponse("zero features")
    props = doc.get("properties") if isinstance(doc.get("properties"), dict) else {}
    exceeded = bool(
        doc.get("exceededTransferLimit") or props.get("exceededTransferLimit")
    )
    return Page(features=feats, exceeded=exceeded)


def query_params(
    envelope: Envelope,
    *,
    where: str = "1=1",
    out_fields: str = "*",
    offset: int = 0,
    page_size: int = PAGE_SIZE,
    polygon: dict | None = None,
    max_allowable_offset: float | None = None,
) -> dict:
    """Query parameters. ``polygon`` (Esri JSON, WGS84) replaces the envelope."""
    if polygon is not None:
        geometry = json.dumps(polygon, separators=(",", ":"))
        geometry_type = "esriGeometryPolygon"
    else:
        xmin, ymin, xmax, ymax = envelope
        geometry = f"{xmin},{ymin},{xmax},{ymax}"
        geometry_type = "esriGeometryEnvelope"
    params = {
        "where": where,
        "geometry": geometry,
        "geometryType": geometry_type,
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "returnGeometry": "true",
        "outFields": out_fields,
        "resultOffset": str(offset),
        "resultRecordCount": str(page_size),
        "f": "geojson",
    }
    if max_allowable_offset is not None:
        params["maxAllowableOffset"] = f"{max_allowable_offset:g}"
    return params


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def citation_for(
    cid: str,
    ep: Endpoint,
    url: str | None = None,
    date: str | None = None,
    note: str | None = None,
) -> Citation:
    """Fetch-dated citation for ``ep`` (``url`` = the layer URL actually used)."""
    return Citation(
        id=cid,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=(url or ep.url).rstrip("/") + "/query",
        date=date or _today(),
        date_kind="fetch",
        note=note,
    )


def _fetch_from(
    client: FetchClient,
    ep: Endpoint,
    layer_url: str,
    envelope: Envelope,
    where: str,
    out_fields: str,
    page_size: int,
    polygon: dict | None = None,
    max_allowable_offset: float | None = None,
) -> tuple[list[dict], int, str | None] | DataGap:
    """All pages from one layer URL -> (features, pages, fetch date) or a gap."""
    url = layer_url.rstrip("/") + "/query"
    features: list[dict] = []
    offset = 0
    fetch_date: str | None = None
    for page_no in range(MAX_PAGES):
        params = query_params(
            envelope,
            where=where,
            out_fields=out_fields,
            offset=offset,
            page_size=page_size,
            polygon=polygon,
            max_allowable_offset=max_allowable_offset,
        )
        first = page_no == 0
        page = client.get(
            ep.source, url, params, lambda b, first=first: parse_page(b, first=first)
        )
        if isinstance(page, DataGap):
            if not first:  # a partial layer is not a layer: drop it (Req 2.6)
                detail = f"page {page_no + 1} (offset {offset}): {page.detail}"
                return DataGap(page.factor, page.source, page.reason, detail)
            return page
        if first:
            fetch_date = client.fetch_date(ep.source, url, params)
        features.extend(page.features)
        if not page.exceeded or not page.features:
            return features, page_no + 1, fetch_date
        offset += len(page.features)
    return DataGap(
        "*",
        ep.source,
        "fetch failure",
        f"exceededTransferLimit still set after {MAX_PAGES} pages",
    )


def _to_layer(
    ep: Endpoint, features: list[dict], citation: Citation, url: str, pages: int
) -> Layer | DataGap:
    geoms_ll: list[BaseGeometry] = []
    props: list[dict] = []
    for f in features:
        g = f.get("geometry") if isinstance(f, dict) else None
        if not g:
            continue
        try:
            geom = shape(g)
        except Exception:  # malformed geometry: skip the feature, never invent one
            continue
        if geom.is_empty:
            continue
        geoms_ll.append(geom)
        props.append(dict(f.get("properties") or {}))
    if not geoms_ll:
        return DataGap("*", ep.source, "empty response", "no features with geometry")
    geoms_utm = [to_utm(g) for g in geoms_ll]
    return Layer(ep.source, geoms_ll, geoms_utm, props, citation, url, pages)


def _unresolved_gap(ep: Endpoint) -> DataGap:
    """Run-wide gap for an endpoint that was never resolved (not queried)."""
    detail = f"endpoint {ep.source} is UNRESOLVED"
    if ep.notes:
        detail += f": {ep.notes}"
    return DataGap("*", ep.source, "fetch failure", detail)


def fetch_layer(
    client: FetchClient,
    ep: Endpoint,
    envelope: Envelope,
    *,
    citation_id: str,
    where: str | None = None,
    out_fields: str = "*",
    page_size: int = PAGE_SIZE,
    fallback_where: str | None = None,
) -> Layer | DataGap:
    """Fetch every feature of ``ep`` intersecting ``envelope`` (primary, then mirror).

    ``fallback_where`` filters the mirror when its schema differs from the
    primary's (defaults to ``where``).

    Returns a :class:`Layer` or a run-wide (``factor="*"``) :class:`DataGap`;
    callers relabel the gap per factor.
    """
    if ep.status == "UNRESOLVED":
        return _unresolved_gap(ep)
    where = where or ep.extra.get("where") or "1=1"
    urls = [ep.url] + ([ep.fallback] if ep.fallback else [])
    wheres = [where, fallback_where or where]
    gaps: list[DataGap] = []
    for i, url in enumerate(urls):
        got = _fetch_from(client, ep, url, envelope, wheres[i], out_fields, page_size)
        if isinstance(got, DataGap):
            gaps.append(got)
            continue
        features, pages, date = got
        note = "fallback mirror" if i > 0 else None
        layer = _to_layer(
            ep, features, citation_for(citation_id, ep, url, date, note), url, pages
        )
        if isinstance(layer, DataGap):
            gaps.append(layer)
            continue
        return layer
    return _all_failed(ep, urls, gaps)


def _unresolved_gap(ep: Endpoint) -> DataGap:
    return DataGap(
        "*",
        ep.source,
        "fetch failure",
        f"endpoint unresolved: {ep.notes or 'no reliable public layer'}",
    )


def _all_failed(ep: Endpoint, urls: list[str], gaps: list[DataGap]) -> DataGap:
    """Every URL failed: report the primary's reason, with each URL's detail."""
    detail = " | ".join(
        f"{u}: {g.reason}" + (f" ({g.detail})" if g.detail else "")
        for u, g in zip(urls, gaps, strict=True)
    )
    return DataGap("*", ep.source, gaps[0].reason, detail)


# ------------------------------------------------------------ tiled queries
# Dense land layers (MassGIS Land Use: ~30k residential polygons in a 10 km box)
# cannot be pulled over the whole Grid envelope. Instead the Grid is cut into
# square tiles and each tile is queried with the union of its cell polygons, so
# the server returns only features that can touch a cell. GET URLs above ~12 kB
# are rejected by the MassGIS gateway (502, checked 2026-10-24), so each query
# polygon is capped at MAX_QUERY_VERTICES and falls back to its convex hull,
# then its envelope. Both are supersets, so no intersecting feature is dropped.

TILE_KM = 8.0
TILE_BUFFER_M = 10.0  # covers lon/lat rounding and UTM->WGS84 edge curvature
MAX_QUERY_VERTICES = 150
POLYGON_DP = 6


def cell_polygons_utm(cells: Any) -> np.ndarray:
    """Cell polygons in UTM 19N: ``geometry_utm``, else WGS84 ``geometry``."""
    cols = getattr(cells, "columns", ())
    if "geometry_utm" in cols:
        return np.asarray(list(cells["geometry_utm"]), dtype=object)
    if "geometry" in cols:
        return np.asarray([to_utm(g) for g in cells["geometry"]], dtype=object)
    raise ValueError("cells need a 'geometry_utm' (UTM) or 'geometry' (WGS84) column")


def _to_wgs84(geom: BaseGeometry) -> BaseGeometry:
    return shapely.transform(
        geom, lambda c: np.column_stack(_TO_WGS84.transform(c[:, 0], c[:, 1]))
    )


def _exteriors(geom: BaseGeometry) -> list[Polygon]:
    """Outer shells only (holes dropped: a superset is a safe query area)."""
    parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    return [Polygon(p.exterior) for p in parts if isinstance(p, Polygon)]


def _n_vertices(polys: list[Polygon]) -> int:
    return sum(len(p.exterior.coords) for p in polys)


def esri_polygon(polys_wgs84: Sequence[Polygon]) -> dict:
    """Esri JSON polygon (clockwise outer rings, WGS84, 6 dp)."""
    rings = []
    for p in polys_wgs84:
        ring = orient(p, sign=-1.0).exterior.coords
        rings.append([[round(x, POLYGON_DP), round(y, POLYGON_DP)] for x, y in ring])
    return {"rings": rings, "spatialReference": {"wkid": 4326}}


def query_tiles(
    cells: Any,
    tile_km: float = TILE_KM,
    max_vertices: int = MAX_QUERY_VERTICES,
) -> list[dict]:
    """One Esri JSON query polygon per occupied tile, in a stable order."""
    polys = cell_polygons_utm(cells)
    if polys.size == 0:
        return []
    size = tile_km * 1000.0
    cents = shapely.centroid(polys)
    keys = zip(
        np.floor(shapely.get_x(cents) / size).astype(int).tolist(),
        np.floor(shapely.get_y(cents) / size).astype(int).tolist(),
        strict=True,
    )
    groups: dict[tuple[int, int], list[BaseGeometry]] = {}
    for k, poly in zip(keys, polys, strict=True):
        groups.setdefault(k, []).append(poly)
    out: list[dict] = []
    for k in sorted(groups):
        area = shapely.union_all(groups[k]).buffer(TILE_BUFFER_M, join_style="mitre")
        shells = _exteriors(area.simplify(0.01))
        if _n_vertices(shells) > max_vertices:
            shells = [Polygon(area.convex_hull.exterior)]
        if _n_vertices(shells) > max_vertices:
            shells = [box(*area.bounds)]
        out.append(esri_polygon([_to_wgs84(p) for p in shells]))
    return out


def _fetch_tiles_from(
    client: FetchClient,
    ep: Endpoint,
    url: str,
    tiles: Sequence[dict],
    where: str,
    out_fields: str,
    page_size: int,
    max_allowable_offset: float | None,
) -> tuple[list[dict], int, str | None] | DataGap:
    """Every tile from one URL. An empty tile is normal; any failure drops all."""
    features: list[dict] = []
    seen: set[str] = set()
    pages = 0
    dates: list[str] = []
    for n, tile in enumerate(tiles):
        xs = [x for ring in tile["rings"] for x, _ in ring]
        ys = [y for ring in tile["rings"] for _, y in ring]
        env = (min(xs), min(ys), max(xs), max(ys))
        got = _fetch_from(
            client,
            ep,
            url,
            env,
            where,
            out_fields,
            page_size,
            polygon=tile,
            max_allowable_offset=max_allowable_offset,
        )
        if isinstance(got, DataGap):
            if got.reason == "empty response":
                continue  # no features in this tile
            detail = f"tile {n + 1}/{len(tiles)}: {got.detail or got.reason}"
            return DataGap(got.factor, got.source, got.reason, detail)
        feats, p, date = got
        pages += p
        if date:
            dates.append(date)
        for f in feats:  # a feature spanning tiles is returned once per tile
            key = json.dumps(
                [f.get("geometry"), f.get("properties")], sort_keys=True, default=str
            )
            if key not in seen:
                seen.add(key)
                features.append(f)
    if not features:
        return DataGap("*", ep.source, "empty response", "zero features in every tile")
    return features, pages, (min(dates) if dates else None)


def fetch_layer_tiles(
    client: FetchClient,
    ep: Endpoint,
    tiles: Sequence[dict],
    *,
    citation_id: str,
    where: str | None = None,
    out_fields: str = "*",
    page_size: int = PAGE_SIZE,
    max_allowable_offset: float | None = None,
    fallback_where: str | None = None,
) -> Layer | DataGap:
    """Like :func:`fetch_layer`, but queried tile by tile (see :func:`query_tiles`).

    ``max_allowable_offset`` (degrees) asks the server to generalize geometry;
    it is recorded in the citation note.
    """
    if ep.status == "UNRESOLVED":
        return _unresolved_gap(ep)
    if not tiles:
        return DataGap("*", ep.source, "empty response", "no query tiles")
    where = where or ep.extra.get("where") or "1=1"
    urls = [ep.url] + ([ep.fallback] if ep.fallback else [])
    wheres = [where, fallback_where or where]
    gaps: list[DataGap] = []
    for i, url in enumerate(urls):
        got = _fetch_tiles_from(
            client,
            ep,
            url,
            tiles,
            wheres[i],
            out_fields,
            page_size,
            max_allowable_offset,
        )
        if isinstance(got, DataGap):
            gaps.append(got)
            continue
        features, pages, date = got
        notes = ["fallback mirror"] if i > 0 else []
        if max_allowable_offset is not None:
            notes.append(f"geometry generalized to {max_allowable_offset:g} deg")
        cit = citation_for(citation_id, ep, url, date, "; ".join(notes) or None)
        layer = _to_layer(ep, features, cit, url, pages)
        if isinstance(layer, DataGap):
            gaps.append(layer)
            continue
        return layer
    return _all_failed(ep, urls, gaps)
