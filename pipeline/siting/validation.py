"""Known-site validation report (Req 15.1-15.6) -> ``validation.json``.

``report(cells)`` checks the 5 hard-coded Validation_Sites against the scored
grid:

* Each site is matched to the Cell whose Centroid is nearest by haversine,
  over ALL cells, Hard_Exclusion cells included (Req 15.2). Ties go to the
  lowest ``cell_id`` so the report is deterministic.
* A match farther than ``MAX_MATCH_MI`` (3 statute miles) -> "not evaluated"
  with the reason; the matched id and distance are reported as null (Req 15.6).
* The site's path sub-score (``tidal_stream_sub`` or ``rise_fall_sub``) is
  ranked against every cell with a non-null value on that path:
  ``pct = 100 * count(value <= v) / n`` (Req 15.2).
* ``pct >= 75`` -> "pass", else "validation failure" (Req 15.4, 15.5). A null
  sub-score at the matched cell is a Data_Gap -> "not evaluated" (Req 15.6).

Every site is evaluated independently and nothing here raises on a bad site,
so the run continues (Req 15.6). Pure: no I/O.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np

from siting.sources.velocity import haversine_km

Path = Literal["tidal_stream", "rise_fall"]
Status = Literal["pass", "validation failure", "not evaluated"]
TIDAL_STREAM: Path = "tidal_stream"
RISE_FALL: Path = "rise_fall"
SUBSCORE_KEY: dict[str, str] = {
    TIDAL_STREAM: "tidal_stream_sub",
    RISE_FALL: "rise_fall_sub",
}

KM_PER_MI = 1.609344
MAX_MATCH_MI = 3.0
PASS_PERCENTILE = 75.0
DISTANCE_DP = 2
PERCENTILE_DP = 1


@dataclass(frozen=True)
class ValidationSite:
    name: str
    lat: float
    lon: float
    path: Path


# Req 15.1 reference coordinates (WGS84, decimal degrees).
SITES: tuple[ValidationSite, ...] = (
    ValidationSite("Muskeget Channel", 41.360, -70.400, TIDAL_STREAM),
    ValidationSite("Woods Hole", 41.521, -70.685, TIDAL_STREAM),
    ValidationSite("Cape Cod Canal/Bourne", 41.741, -70.616, TIDAL_STREAM),
    ValidationSite("Boston Harbor approaches", 42.330, -70.950, RISE_FALL),
    ValidationSite("Cape Cod Bay", 41.850, -70.350, RISE_FALL),
)


@dataclass(frozen=True)
class ValidationEntry:
    """One ``validation.json`` record (docs/schema/validation.schema.json)."""

    site: str
    ref_lat: float
    ref_lon: float
    path: Path
    matched_cell_id: str | None
    distance_mi: float | None
    resource_percentile: float | None
    status: Status
    reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _num(v: Any) -> float | None:
    """Finite float or None (NaN / None / missing are gaps)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _subscore(cell: Mapping[str, Any], key: str) -> float | None:
    """Sub-score from a flat record or a ``scores`` sub-dict (cells.geojson)."""
    if key in cell:
        return _num(cell[key])
    scores = cell.get("scores")
    return _num(scores.get(key)) if isinstance(scores, Mapping) else None


def percentile(value: float, values: Iterable[float]) -> float:
    """``100 * count(values <= value) / n``; ``values`` must be non-empty."""
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        raise ValueError("percentile over an empty set")
    return 100.0 * float(np.count_nonzero(arr <= value)) / arr.size


def _evaluate(
    site: ValidationSite,
    ids: list[str],
    lat: np.ndarray,
    lon: np.ndarray,
    subs: dict[str, list[float | None]],
    gap_reasons: Mapping[str, Mapping[str, str]],
) -> ValidationEntry:
    def not_evaluated(
        reason: str, cid: str | None = None, dist: float | None = None
    ) -> ValidationEntry:
        return ValidationEntry(
            site.name, site.lat, site.lon, site.path, cid, dist, None,
            "not evaluated", reason,
        )

    if not ids:
        return not_evaluated("grid has no cells")

    d_mi = haversine_km(site.lat, site.lon, lat, lon) / KM_PER_MI
    # Nearest centroid; ties -> lowest cell_id (lexsort: last key is primary).
    k = int(np.lexsort((np.asarray(ids), d_mi))[0])
    dist = float(d_mi[k])
    if dist > MAX_MATCH_MI:
        return not_evaluated(
            f"no Centroid within {MAX_MATCH_MI:g} mi "
            f"(nearest {ids[k]} at {dist:.2f} mi)"
        )

    cid = ids[k]
    dist_out = round(dist, DISTANCE_DP)
    key = SUBSCORE_KEY[site.path]
    v = subs[key][k]
    if v is None:
        why = gap_reasons.get(cid, {}).get(site.path)
        reason = f"Data_Gap: {key} is null at {cid}" + (f" ({why})" if why else "")
        return not_evaluated(reason, cid, dist_out)

    pct = percentile(v, (x for x in subs[key] if x is not None))
    status: Status = "pass" if pct >= PASS_PERCENTILE else "validation failure"
    return ValidationEntry(
        site.name, site.lat, site.lon, site.path, cid, dist_out,
        round(pct, PERCENTILE_DP), status, None,
    )


def report(
    cells: Iterable[Mapping[str, Any]],
    gap_reasons: Mapping[str, Mapping[str, str]] | None = None,
    sites: Iterable[ValidationSite] = SITES,
) -> list[ValidationEntry]:
    """Validation entries, one per site, in ``sites`` order.

    ``cells`` are records with ``cell_id``, ``centroid_lat``, ``centroid_lon``
    and ``tidal_stream_sub`` / ``rise_fall_sub`` (flat, or under ``scores``).
    ``gap_reasons`` optionally maps ``cell_id -> {path: reason}`` to explain a
    null sub-score in the report.
    """
    rows = list(cells)
    ids = [str(c["cell_id"]) for c in rows]
    lat = np.asarray([float(c["centroid_lat"]) for c in rows], dtype=float)
    lon = np.asarray([float(c["centroid_lon"]) for c in rows], dtype=float)
    subs = {key: [_subscore(c, key) for c in rows] for key in SUBSCORE_KEY.values()}
    gaps = gap_reasons or {}
    return [_evaluate(s, ids, lat, lon, subs, gaps) for s in sites]


def report_json(
    cells: Iterable[Mapping[str, Any]],
    gap_reasons: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict]:
    """``validation.json`` content."""
    return [e.to_dict() for e in report(cells, gap_reasons)]
