"""eMOLT fishing-activity points from ERDDAP (Req 7.4).

eMOLT (NOAA NEFSC / CFRF partnership) logs bottom temperature on commercial
fishing gear, so where its records fall is a PROXY for where fishing happens.
It is kit-equivalent (no EXTERNAL ADDITION).

One tabledap ``.csv`` query over the Grid bbox (+ margin) returns one point per
fishing operation (``tow_id``):

* ``segment_type=3`` keeps records taken while the gear was fishing on the
  bottom (1/2 are the gear being set/hauled).
* ``orderByMin("tow_id,latitude")`` keeps one record per tow. The raw dataset
  logs about one record per second (hundreds of thousands of rows over MA
  waters, which the server cannot return inside the fetch timeout), so a raw
  record count would measure logging rate, not fishing. Counting tows is the
  observation count that reflects activity.
* ``time`` is not requested: adding it to this aggregation makes the server
  fail (HTTP 520, checked 2026-10-24). Counts therefore span the dataset's
  full record (2018 onward), not a recent window.

A fetch failure, an ERDDAP "no matching results" response (HTTP 404) or an
empty table gives a run-wide DataGap. Nothing is substituted (Req 2.6).
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import numpy as np
import shapely
from pyproj import Transformer

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap
from siting.provenance import Citation
from siting.sources.arcgis import Envelope
from siting.sources.endpoints import EMOLT, Endpoint

EMOLT_CITATION_ID = "emolt"
VARIABLES = ("tow_id", "latitude", "longitude")
FISHING_SEGMENT = 3
COORD_DP = 4

_TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:26919", always_xy=True)


@dataclass
class FishingPoints:
    """One point per eMOLT fishing operation inside the query envelope."""

    tow_ids: list[str]
    lons: np.ndarray
    lats: np.ndarray
    points_utm: np.ndarray  # shapely Points, EPSG:26919
    citation: Citation
    url: str

    def __len__(self) -> int:
        return len(self.tow_ids)


def query_url(base: str, envelope: Envelope) -> str:
    """Full tabledap ``.csv`` URL (constraints are not key=value params)."""
    xmin, ymin, xmax, ymax = (round(float(v), COORD_DP) for v in envelope)
    parts = [
        ",".join(VARIABLES),
        f"latitude>={ymin}",
        f"latitude<={ymax}",
        f"longitude>={xmin}",
        f"longitude<={xmax}",
        f"segment_type={FISHING_SEGMENT}",
        'orderByMin("tow_id,latitude")',
    ]
    return f"{base.rstrip('/')}.csv?" + quote("&".join(parts), safe="&=,")


def emolt_citation(
    ep: Endpoint = EMOLT,
    url: str | None = None,
    date: str | None = None,
    note: str | None = None,
) -> Citation:
    return Citation(
        id=EMOLT_CITATION_ID,
        agency=ep.agency,
        dataset=ep.dataset,
        endpoint=(url or ep.url).rstrip("/") + ".csv",
        date=date or datetime.now(UTC).strftime("%Y-%m-%d"),
        date_kind="fetch",
        note=note,
    )


def parse_csv(payload: bytes) -> list[tuple[str, float, float]]:
    """``(tow_id, lon, lat)`` rows; ERDDAP's second row holds units and is skipped.

    Rows with a missing or non-finite coordinate are dropped. Repeated tow ids
    keep their first row. Zero usable rows -> :class:`EmptyResponse`.
    """
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
    if not rows:
        raise EmptyResponse("empty table")
    header = [h.strip() for h in rows[0]]
    missing = [v for v in VARIABLES if v not in header]
    if missing:
        raise ValueError(f"eMOLT csv: missing columns {missing}")
    i_tow, i_lat, i_lon = (header.index(v) for v in VARIABLES)
    body = rows[1:]
    if body and body[0][i_lat].strip() == "degrees_north":
        body = body[1:]
    out: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for r in body:
        if len(r) < len(header):
            continue
        try:
            lat, lon = float(r[i_lat]), float(r[i_lon])
        except ValueError:
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        tow = r[i_tow].strip()
        if tow in seen:
            continue
        seen.add(tow)
        out.append((tow, lon, lat))
    if not out:
        raise EmptyResponse("no rows with coordinates")
    return out


def fetch_fishing_points(
    client: FetchClient, envelope: Envelope, ep: Endpoint = EMOLT
) -> FishingPoints | DataGap:
    """eMOLT fishing-operation points inside ``envelope``, or a run-wide gap."""
    if ep.status == "UNRESOLVED":
        detail = f"endpoint unresolved: {ep.notes or 'no reliable public dataset'}"
        return DataGap("*", ep.source, "fetch failure", detail)
    bases = [ep.url] + ([ep.fallback] if ep.fallback else [])
    gaps: list[tuple[str, DataGap]] = []
    for i, base in enumerate(bases):
        url = query_url(base, envelope)
        got = client.get(ep.source, url, None, parse_csv)
        if isinstance(got, DataGap):
            gaps.append((base, got))
            continue
        tows = [t for t, _, _ in got]
        lons = np.array([x for _, x, _ in got], dtype=float)
        lats = np.array([y for _, _, y in got], dtype=float)
        x, y = _TO_UTM.transform(lons, lats)
        note = "fallback mirror" if i > 0 else None
        cit = emolt_citation(ep, base, client.fetch_date(ep.source, url, None), note)
        return FishingPoints(
            tows, lons, lats, shapely.points(np.asarray(x), np.asarray(y)), cit, base
        )
    detail = " | ".join(
        f"{b}: {g.reason}" + (f" ({g.detail})" if g.detail else "") for b, g in gaps
    )
    return DataGap("*", ep.source, gaps[0][1].reason, detail)
