"""Northeast Ocean Data Portal conflict layers (Req 7.4, 7.5).

* **AIS vessel transit counts** (``endpoints.NEODP_VESSEL_TRAFFIC``): a raster
  on a MapServer, so it is sampled with ``/identify`` at each cell centroid
  (one request per point, cached by :class:`FetchClient`). A multipoint
  identify returns a single pixel, so points cannot be batched (checked
  2026-10-24). The primary is probed with the first point; if that probe is a
  "fetch failure" every point goes to the MarineCadastre fallback instead, so
  one run never mixes values from the two rasters (the fallback is a classified
  rendering with different pixel values; only ranks are used downstream).
  A ``NoData`` pixel (no recorded transits, or outside the raster) is reported
  per point as "no coverage". It is never read as zero (Req 2.6).
* **Diadromous fish stations** (``endpoints.NEODP_DIADROMOUS``): MA DMF station
  points through :func:`siting.sources.arcgis.fetch_layer`. Proximity only;
  never a hard exclusion (Req 7.5).

Both are kit-equivalent (no EXTERNAL ADDITION) and tagged PROXY.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import Citation
from siting.sources.arcgis import Envelope, Layer, fetch_layer
from siting.sources.endpoints import NEODP_DIADROMOUS, NEODP_VESSEL_TRAFFIC, Endpoint

# Citation ids (match output/sample/metadata.json).
VESSEL_CITATION_ID = "neodp-vessel"
DIADROMOUS_CITATION_ID = "neodp-diadromous"

# "*": the fallback (NEODP River Herring and Shad Runs) has a different schema.
DIADROMOUS_FIELDS = "*"
POINT_DP = 5  # ~1 m; keeps identify cache keys stable
_EXTENT_PAD_DEG = 0.01
NO_DATA = "NoData"


# ------------------------------------------------------------ diadromous fish


def fetch_diadromous_stations(
    client: FetchClient, envelope: Envelope
) -> Layer | DataGap:
    """Diadromous fish station points inside ``envelope``, or a run-wide gap."""
    return fetch_layer(
        client,
        NEODP_DIADROMOUS,
        envelope,
        citation_id=DIADROMOUS_CITATION_ID,
        out_fields=DIADROMOUS_FIELDS,
    )


# ------------------------------------------------------------ vessel traffic


@dataclass
class VesselSamples:
    """AIS transit count per input point (``None`` -> see ``gaps[i]``)."""

    values: list[float | None]
    gaps: list[DataGap | None]
    citation: Citation
    url: str
    layer: int
    extra: dict = field(default_factory=dict)


def vessel_citation(
    ep: Endpoint = NEODP_VESSEL_TRAFFIC,
    url: str | None = None,
    layer: int | None = None,
    date: str | None = None,
    note: str | None = None,
) -> Citation:
    """Fetch-dated citation naming the MapServer ``/identify`` endpoint used."""
    lyr = ep.extra.get("layer", 0) if layer is None else layer
    base = (url or ep.url).rstrip("/")
    return Citation(
        id=VESSEL_CITATION_ID,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=f"{base}/identify?layers=all:{lyr}",
        date=date or datetime.now(UTC).strftime("%Y-%m-%d"),
        date_kind="fetch",
        note=note,
    )


def identify_params(lon: float, lat: float, layer: int) -> dict:
    """``/identify`` parameters for one point (tolerance 0 = the pixel itself)."""
    x, y = round(float(lon), POINT_DP), round(float(lat), POINT_DP)
    p = _EXTENT_PAD_DEG
    return {
        "geometry": f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "sr": "4326",
        "layers": f"all:{layer}",
        "tolerance": "0",
        "mapExtent": f"{x - p},{y - p},{x + p},{y + p}",
        "imageDisplay": "100,100,96",
        "returnGeometry": "false",
        "f": "json",
    }


def parse_identify(payload: bytes) -> float | None:
    """Pixel value from an identify response; ``None`` for NoData / no result.

    The NEODP raster reports ``"Pixel Value"``; the classified MarineCadastre
    raster reports ``"UniqueValue.Pixel Value"``.
    """
    doc = json.loads(payload)
    if not isinstance(doc, dict):
        raise ValueError("identify: response is not a JSON object")
    if "error" in doc:
        err = doc["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise ValueError(f"ArcGIS error: {msg}")
    results = doc.get("results")
    if not isinstance(results, list):
        raise ValueError("identify: no 'results' array")
    for r in results:
        attrs = (r.get("attributes") or {}) if isinstance(r, dict) else {}
        keys = sorted(
            (k for k in attrs if k.endswith("Pixel Value")),
            key=lambda k: k != "Pixel Value",
        )
        for k in keys:
            v = attrs[k]
            if v is None or str(v).strip() == NO_DATA:
                return None
            f = float(v)
            return None if math.isnan(f) else f
    return None


def _sample(
    client: FetchClient, ep: Endpoint, url: str, layer: int, lon: float, lat: float
) -> float | None | DataGap:
    return client.get(
        ep.source,
        url.rstrip("/") + "/identify",
        identify_params(lon, lat, layer),
        parse_identify,
    )


def sample_vessel_traffic(
    client: FetchClient,
    points_lonlat: Sequence[tuple[float, float]],
    ep: Endpoint = NEODP_VESSEL_TRAFFIC,
) -> VesselSamples | DataGap:
    """AIS transit count at each ``(lon, lat)``, or a run-wide gap.

    Per-point failures become per-point gaps. A run-wide gap is returned only
    when the endpoint is UNRESOLVED or every point failed.
    """
    if ep.status == "UNRESOLVED":
        detail = f"endpoint unresolved: {ep.notes or 'no reliable public layer'}"
        return DataGap("*", ep.source, "fetch failure", detail)
    if not points_lonlat:
        return DataGap("*", ep.source, "empty response", "no sample points")

    url, layer, note = ep.url, int(ep.extra.get("layer", 0)), None
    lon0, lat0 = points_lonlat[0]
    first = _sample(client, ep, url, layer, lon0, lat0)
    if isinstance(first, DataGap) and first.reason == "fetch failure" and ep.fallback:
        fb_layer = int(ep.extra.get("fallback_layer", 0))
        fb_first = _sample(client, ep, ep.fallback, fb_layer, lon0, lat0)
        if not isinstance(fb_first, DataGap):
            url, layer, note, first = ep.fallback, fb_layer, "fallback mirror", fb_first

    values: list[float | None] = []
    gaps: list[DataGap | None] = []
    for i, (lon, lat) in enumerate(points_lonlat):
        got = first if i == 0 else _sample(client, ep, url, layer, lon, lat)
        if isinstance(got, DataGap):
            values.append(None)
            gaps.append(got)
        elif got is None:
            values.append(None)
            gaps.append(
                DataGap(
                    "*",
                    ep.source,
                    "no coverage",
                    "NoData pixel (no recorded AIS transits or outside the raster)",
                )
            )
        else:
            values.append(float(got))
            gaps.append(None)

    hard = [g for g in gaps if g is not None and g.reason != "no coverage"]
    if len(hard) == len(points_lonlat):
        return DataGap("*", ep.source, hard[0].reason, hard[0].detail)
    date = client.fetch_date(
        ep.source, url.rstrip("/") + "/identify", identify_params(lon0, lat0, layer)
    )
    cit = vessel_citation(ep, url, layer, date, note)
    return VesselSamples(values, gaps, cit, url, layer)
