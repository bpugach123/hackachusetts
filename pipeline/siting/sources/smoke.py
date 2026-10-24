"""Live endpoint smoke test: ``python -m siting.sources.smoke`` (Req 2.1, 6.1).

Hits every endpoint in :mod:`siting.sources.endpoints` once with a tiny request
and prints one line per endpoint:

    OK|GAP <name> <url> <reason>

* ArcGIS ``/query`` layers: a bbox query over Buzzards Bay / Vineyard Sound
  (``resultRecordCount=1``, generalized geometry) through :class:`FetchClient`.
* ArcGIS raster ``/identify`` layers: one identify at the Woods Hole passage.
* ERDDAP tabledap: the same bbox, last 30 days, ``orderByLimit("1")``.
* CO-OPS / NDBC / EIA REST: one small request (Boston 8443970, buoy 44013, the
  current-prediction station nearest Woods Hole).
* THREDDS OPeNDAP: a plain GET of ``<url>.das``. Census zip: a plain HEAD.

When the primary fails and a ``fallback`` exists, the fallback is tried and the
line says which URL answered. ``resolve_at_build_start`` rows marked
``unresolved -> gap`` print GAP without a request. A row with no resolution note
prints GAP ``unannotated``. Nothing here writes into ``data/cache``: the
FetchClient uses a throwaway cache dir so every run is live.

This is a manual hour-1 / pre-release check, not part of CI. It never raises for
a failing endpoint and always exits 0 once every line is printed.
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from siting.fetch.client import EmptyResponse, FetchClient
from siting.fetch.gaps import DataGap
from siting.sources import endpoints as E

# Tiny probe area: Buzzards Bay mouth / Vineyard Sound (~33 km x 28 km).
BBOX = (-70.95, 41.40, -70.55, 41.65)  # min lon, min lat, max lon, max lat
PROBE_POINT = (-70.68, 41.52)  # Woods Hole passage (lon, lat)
BOSTON_TIDE_STATION = "8443970"
BOSTON_BUOY = "44013"
HTTP_TIMEOUT_S = 40


@dataclass(frozen=True)
class Result:
    ok: bool
    url: str
    reason: str


def _endpoint_names() -> dict[int, str]:
    return {id(v): k for k, v in vars(E).items() if isinstance(v, E.Endpoint)}


# ------------------------------------------------------------------ parsers


def _parse_arcgis(payload: bytes) -> str:
    data = json.loads(payload)
    if "error" in data:
        err = data["error"]
        raise ValueError(f"ArcGIS error {err.get('code')}: {err.get('message')}")
    if "results" in data:  # identify
        n = len(data["results"])
        vals = [r.get("attributes", {}) for r in data["results"][:1]]
        return f"identify: {n} result(s) {vals}"
    feats = data.get("features")
    if feats is None:
        raise ValueError("no 'features' key in ArcGIS response")
    geom = "with geometry" if any(f.get("geometry") for f in feats) else "no geometry"
    return f"{len(feats)} feature(s) in probe bbox ({geom})"


def _parse_json_count(key: str) -> Callable[[bytes], str]:
    def parse(payload: bytes) -> str:
        data = json.loads(payload)
        if "error" in data:
            raise ValueError(str(data["error"])[:200])
        items = data.get(key)
        if not items:
            raise EmptyResponse(f"no '{key}' in response")
        return f"{len(items)} {key}"

    return parse


def _parse_text_nonempty(label: str) -> Callable[[bytes], str]:
    def parse(payload: bytes) -> str:
        text = payload.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise EmptyResponse("empty body")
        return f"{label}: {len(lines)} line(s)"

    return parse


def _parse_erddap_csv(payload: bytes) -> str:
    rows = payload.decode("utf-8", errors="replace").strip().splitlines()
    if len(rows) < 3:  # header + units + >= 1 row
        raise EmptyResponse("no rows")
    return f"{len(rows) - 2} row(s), first {rows[2]}"


# ------------------------------------------------------------------ probes


def _arcgis_params(url: str, ep: E.Endpoint, fallback: bool) -> dict[str, Any]:
    if url.endswith("/identify"):
        layer = ep.extra.get("fallback_layer_id" if fallback else "layer_id", 0)
        lon, lat = PROBE_POINT
        return {
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "sr": 4326,
            "layers": f"all:{layer}",
            "tolerance": 1,
            "mapExtent": ",".join(str(v) for v in BBOX),
            "imageDisplay": "400,400,96",
            "returnGeometry": "false",
            "f": "json",
        }
    return {
        "geometry": ",".join(str(v) for v in BBOX),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "maxAllowableOffset": 0.05,  # keep the probe tiny (huge state polygons)
        "outSR": 4326,
        "resultRecordCount": 1,
        "f": "json",
    }


def _erddap_url(url: str) -> str:
    lon0, lat0, lon1, lat1 = BBOX
    query = (
        f"latitude,longitude,time&latitude>={lat0}&latitude<={lat1}"
        f"&longitude>={lon0}&longitude<={lon1}&time>=now-30days"
        '&orderByLimit("1")'
    )
    return f"{url}?{urllib.parse.quote(query, safe='&=,()-')}"


def _plain(method: str, url: str) -> Result:
    """HEAD/GET outside the fetch layer (THREDDS .das, file HEAD)."""
    import requests

    try:
        resp = requests.request(
            method,
            url,
            timeout=HTTP_TIMEOUT_S,
            allow_redirects=True,
            headers={"User-Agent": "siting-smoke/0.1"},
        )
    except requests.RequestException as exc:
        return Result(False, url, f"fetch failure: {type(exc).__name__}: {exc}"[:240])
    if resp.status_code >= 400:
        return Result(False, url, f"fetch failure: HTTP {resp.status_code}")
    size = resp.headers.get("Content-Length") or str(len(resp.content))
    return Result(True, url, f"HTTP {resp.status_code}, {size} bytes")


def _via_client(
    client: FetchClient,
    source: str,
    url: str,
    params: dict | None,
    parse: Callable[[bytes], str],
) -> Result:
    out = client.get(source, url, params, parse, factor="smoke")
    if isinstance(out, DataGap):
        detail = f": {out.detail}" if out.detail else ""
        return Result(False, url, f"{out.reason}{detail}"[:240])
    return Result(True, url, out)


def _nearest_current_station(client: FetchClient) -> str | None:
    def parse(payload: bytes) -> str:
        stations = json.loads(payload).get("stations") or []
        if not stations:
            raise EmptyResponse("no stations")
        lon0, lat0 = PROBE_POINT

        def dist(s: dict) -> float:
            dlat = math.radians(s["lat"] - lat0)
            dlon = math.radians(s["lng"] - lon0) * math.cos(math.radians(lat0))
            return math.hypot(dlat, dlon)

        best = min(stations, key=dist)
        return str(best["id"])

    out = client.get(
        E.NOAA_CURRENTS_STATIONS.source,
        E.NOAA_CURRENTS_STATIONS.url,
        None,
        parse,
        factor="smoke",
    )
    return None if isinstance(out, DataGap) else out


def _probe_rest(client: FetchClient, ep: E.Endpoint) -> Result:
    if ep is E.EIA_RETAIL:
        key = os.environ.get("EIA_API_KEY")
        if not key:
            return Result(
                False, ep.url, "fetch failure: no API key (EIA_API_KEY unset)"
            )
        params = {
            "api_key": key,
            "frequency": "monthly",
            "data[]": "price",
            "facets[stateid][]": "MA",
            "facets[sectorid][]": "ALL",
            "length": 1,
        }
        return _via_client(client, ep.source, ep.url, params, _parse_eia)
    if ep is E.NOAA_CURRENTS_DATA:
        station = _nearest_current_station(client)
        if station is None:
            return Result(False, ep.url, "fetch failure: current station list failed")
        params = {
            "product": "currents_predictions",
            "station": station,
            "date": "today",
            "interval": "MAX_SLACK",
            "units": "metric",
            "time_zone": "gmt",
            "format": "json",
            "application": "siting_smoke",
        }
        return _via_client(client, ep.source, ep.url, params, _parse_currents(station))
    if ep in (E.COOPS_HARCON, E.COOPS_DATUMS):
        url = ep.url.format(station=BOSTON_TIDE_STATION)
        key = "HarmonicConstituents" if ep is E.COOPS_HARCON else "datums"
        return _via_client(
            client, ep.source, url, {"units": "metric"}, _parse_json_count(key)
        )
    if ep in (E.NOAA_CURRENTS_STATIONS, E.COOPS_HARCON_STATIONS):
        return _via_client(
            client, ep.source, ep.url, None, _parse_json_count("stations")
        )
    if ep is E.NDBC_REALTIME:
        url = ep.url.format(station=BOSTON_BUOY)
        return _via_client(
            client, ep.source, url, None, _parse_text_nonempty(BOSTON_BUOY)
        )
    if ep is E.NDBC_ACTIVE:
        return _via_client(client, ep.source, ep.url, None, _parse_ndbc_active)
    return _via_client(client, ep.source, ep.url, None, _parse_text_nonempty("body"))


def _parse_eia(payload: bytes) -> str:
    data = json.loads(payload)
    rows = (data.get("response") or {}).get("data") or []
    if not rows:
        raise EmptyResponse("no rows")
    return f"latest period {rows[0].get('period')}"


def _parse_currents(station: str) -> Callable[[bytes], str]:
    def parse(payload: bytes) -> str:
        data = json.loads(payload)
        if "error" in data:
            raise ValueError(str(data["error"])[:200])
        preds = (data.get("current_predictions") or {}).get("cp") or []
        if not preds:
            raise EmptyResponse("no predictions")
        return f"station {station}: {len(preds)} max/slack event(s) today"

    return parse


def _parse_ndbc_active(payload: bytes) -> str:
    n = payload.count(b"<station ")
    if n == 0:
        raise EmptyResponse("no <station> elements")
    return f"{n} stations"


def probe(client: FetchClient, ep: E.Endpoint, url: str, fallback: bool) -> Result:
    if ep.kind == "file":
        return _plain("HEAD", url)
    if ep.kind == "opendap":
        return _plain("GET", f"{url}.das")
    if ep.kind == "erddap":
        return _via_client(client, ep.source, _erddap_url(url), None, _parse_erddap_csv)
    if ep.kind == "arcgis":
        if not (url.endswith("/query") or url.endswith("/identify")):
            return Result(False, url, "unresolved: no layer query URL")
        params = _arcgis_params(url, ep, fallback)
        return _via_client(client, ep.source, url, params, _parse_arcgis)
    return _probe_rest(client, ep)


def check(client: FetchClient, ep: E.Endpoint) -> Result:
    if ep.resolve_at_build_start and not ep.resolution:
        return Result(False, ep.url, "unannotated resolve_at_build_start entry")
    if ep.unresolved:
        return Result(False, ep.url, ep.resolution or "unresolved")
    primary = probe(client, ep, ep.url, fallback=False)
    if primary.ok or not ep.fallback:
        return primary
    alt = probe(client, ep, ep.fallback, fallback=True)
    if alt.ok:
        return Result(
            True, alt.url, f"via fallback ({alt.reason}); primary {primary.reason}"
        )
    return Result(False, ep.url, f"primary {primary.reason}; fallback {alt.reason}")


def main(argv: list[str] | None = None) -> int:
    names = _endpoint_names()
    n_ok = 0
    with tempfile.TemporaryDirectory(prefix="siting-smoke-") as tmp:
        # Live every time; short real backoff so a flaky host gets its retries.
        client = FetchClient(tmp, sleep=lambda s: time.sleep(min(s, 1.0)))
        for ep in E.ALL_ENDPOINTS:
            res = check(client, ep)
            n_ok += res.ok
            status = "OK " if res.ok else "GAP"
            print(f"{status} {names.get(id(ep), ep.source)} {res.url} {res.reason}")
            sys.stdout.flush()
    total = len(E.ALL_ENDPOINTS)
    print(f"-- {n_ok}/{total} OK, {total - n_ok} GAP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
