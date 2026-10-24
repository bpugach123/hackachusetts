"""HTTP and OPeNDAP fetch with cache, bounded retry, and Data_Gap results.

Covers Req 2.2-2.8. ``FetchClient.get`` is the entry point every HTTP source
module uses:

    client.get(source, url, params, parse) -> T | DataGap

``FetchClient.get_opendap`` is the OPeNDAP twin. It opens a THREDDS dataset with
xarray, applies a ``select`` subset, loads it, and caches the subset as netCDF
bytes. It goes through the same cache and retry wrapper:

    client.get_opendap(source, url, params, select) -> xarray.Dataset | DataGap

Behavior (both paths):

* Cache hit whose bytes ``parse`` successfully -> return the parsed value, no
  network call (Req 2.3). A parse failure on cached bytes evicts the entry and
  falls through to a live fetch (Req 2.4).
* Live fetch: up to 3 attempts total, sleeping 2 s then 4 s between them
  (Req 2.5). A status >= 400, a timeout, or a connection error is a failed
  attempt. Three failures -> ``DataGap(factor, source, "fetch failure")``.
* An empty body -> ``DataGap(..., "empty response")``. A successful live fetch is
  cached before parsing, so the next run is offline-reproducible.
* ``parse`` may raise :class:`EmptyResponse` to signal zero features / all-NaN as
  an "empty response" gap rather than a crash. Any other parse error on a live
  payload (an HTML error page, an ArcGIS ``{"error": ...}`` body) evicts the entry
  and returns a "fetch failure" gap. ``get`` never raises for network or parse
  problems.

The transport, OPeNDAP opener, and sleep are injected (``transport``,
``opendap_open``, ``sleep``) so tests run with fakes and no real delay. The
default transport uses ``requests`` with a 60 s timeout; the default sleep is
``time.sleep``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from siting.fetch.cache import Cache, cache_key
from siting.fetch.gaps import DataGap

if TYPE_CHECKING:  # pragma: no cover - typing only
    import xarray as xr

T = TypeVar("T")

DEFAULT_TIMEOUT_S = 60
MAX_ATTEMPTS = 3
BACKOFF_S = (2.0, 4.0)  # sleep before attempt 2, then before attempt 3


class EmptyResponse(Exception):
    """Raised by a ``parse`` fn for an empty/zero-feature/all-NaN payload."""


class TransportError(Exception):
    """A failed transport attempt (timeout, connection error, or status >= 400)."""


@dataclass(frozen=True)
class Response:
    status: int
    content: bytes


class Transport(Protocol):
    def __call__(self, url: str, params: dict | None) -> Response: ...


# Opens ``url``, applies ``select``, loads the subset, returns netCDF bytes.
OpendapOpener = Callable[[str, Callable[["xr.Dataset"], "xr.Dataset"]], bytes]


def _requests_transport(url: str, params: dict | None) -> Response:
    """Default transport. Imported lazily so tests need not install requests."""
    import requests

    try:
        resp = requests.get(
            url,
            params=params,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"User-Agent": "siting-pipeline/0.1 (MA tidal siting model)"},
        )
    except requests.RequestException as exc:  # timeout, connection error, etc.
        raise TransportError(str(exc)) from exc
    return Response(status=resp.status_code, content=resp.content)


def dataset_to_netcdf_bytes(ds: xr.Dataset) -> bytes:
    """Serialize a (loaded) dataset to netCDF4 bytes for the cache."""
    return bytes(ds.to_netcdf(engine="netcdf4"))


def open_netcdf_bytes(payload: bytes) -> xr.Dataset:
    """Parse cached netCDF bytes back into a fully loaded ``xarray.Dataset``."""
    import netCDF4
    import xarray as xr

    nc = netCDF4.Dataset("inmemory.nc", memory=payload)
    try:
        ds = xr.open_dataset(xr.backends.NetCDF4DataStore(nc)).load()
    finally:
        nc.close()
    if not ds.data_vars:
        raise EmptyResponse("subset has no data variables")
    float_vars = [v for v in ds.data_vars if ds[v].dtype.kind == "f"]
    # An empty array counts as all-NaN (``.all()`` of nothing is True).
    if float_vars and all(bool(ds[v].isnull().all()) for v in float_vars):
        raise EmptyResponse("subset is all-NaN")
    return ds


def _xarray_opendap_open(url: str, select: Callable[[xr.Dataset], xr.Dataset]) -> bytes:
    """Default OPeNDAP opener: ``open_dataset`` + ``select`` + ``.load()``."""
    import xarray as xr

    try:
        with xr.open_dataset(url) as ds:
            sub = select(ds).load()
    except (OSError, RuntimeError, KeyError) as exc:  # DAP / netCDF errors
        raise TransportError(f"OPeNDAP: {exc}") from exc
    return dataset_to_netcdf_bytes(sub)


class FetchClient:
    """Fetches bytes through the cache and retry policy, then parses them.

    ``factor`` labels any resulting gap so the scorer and the gap summary can
    attribute it. Supply it per call via :meth:`get`.
    """

    def __init__(
        self,
        cache_dir: str | Path = "data/cache",
        *,
        transport: Transport | None = None,
        opendap_open: OpendapOpener | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.cache = Cache(cache_dir)
        self._transport = transport or _requests_transport
        self._opendap_open = opendap_open or _xarray_opendap_open
        # Tests inject a no-op sleep; production backs off for real (Req 2.5).
        self._sleep = sleep or time.sleep

    def _retry(self, attempt_once: Callable[[], bytes]) -> bytes | DataGap:
        """Run ``attempt_once`` up to MAX_ATTEMPTS times with the backoff schedule."""
        last: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return attempt_once()
            except TransportError as exc:
                last = str(exc)
                if attempt < MAX_ATTEMPTS:
                    self._sleep(BACKOFF_S[attempt - 1])
        return DataGap("-", "-", "fetch failure", detail=last)

    def _http_once(self, url: str, params: dict | None) -> bytes:
        resp = self._transport(url, params)
        if resp.status >= 400:
            raise TransportError(f"HTTP {resp.status}")
        return resp.content

    def _cached(
        self,
        source: str,
        url: str,
        params: dict | None,
        parse: Callable[[bytes], T],
        factor: str,
        attempt_once: Callable[[], bytes],
    ) -> T | DataGap:
        key = cache_key(source, url, params)

        # 1. Cache hit that parses -> no network.
        entry = self.cache.read(source, key)
        if entry is not None:
            try:
                if not entry.payload:
                    raise EmptyResponse("empty cached payload")
                return parse(entry.payload)
            except EmptyResponse:
                return DataGap(factor, source, "empty response")
            except Exception:
                # Corrupt/unparseable cache: drop it and fetch live (Req 2.4).
                self.cache.evict(source, key)

        # 2. Live fetch with retry.
        result = self._retry(attempt_once)
        if isinstance(result, DataGap):
            return DataGap(factor, source, "fetch failure", detail=result.detail)
        payload = result
        if not payload:
            return DataGap(factor, source, "empty response")

        # 3. Cache then parse. Cache even if parsing later flags "empty" so the
        #    bytes are reproducible; an unparseable body is evicted.
        self.cache.write(source, key, payload, url, params)
        try:
            return parse(payload)
        except EmptyResponse:
            return DataGap(factor, source, "empty response")
        except Exception as exc:
            self.cache.evict(source, key)
            return DataGap(
                factor,
                source,
                "fetch failure",
                detail=f"unparseable response: {type(exc).__name__}: {exc}"[:300],
            )

    def get(
        self,
        source: str,
        url: str,
        params: dict | None,
        parse: Callable[[bytes], T],
        *,
        factor: str = "",
    ) -> T | DataGap:
        """Return parsed data or a :class:`DataGap` (never raises for network/parse).

        ``parse`` turns bytes into the typed result and may raise
        :class:`EmptyResponse` for an empty payload.
        """
        return self._cached(
            source, url, params, parse, factor, lambda: self._http_once(url, params)
        )

    def get_opendap(
        self,
        source: str,
        url: str,
        params: dict,
        select: Callable[[xr.Dataset], xr.Dataset],
        parse: Callable[[bytes], T] = open_netcdf_bytes,  # type: ignore[assignment]
        *,
        factor: str = "",
    ) -> T | DataGap:
        """OPeNDAP subset through the same cache + retry wrapper.

        ``params`` must describe the subset (variables, index or coordinate
        ranges) because it is part of the cache key; ``select`` applies that
        subset to the opened dataset. The subset is cached as netCDF bytes and
        ``parse`` (default :func:`open_netcdf_bytes`) turns them back into a
        loaded ``xarray.Dataset``.
        """
        return self._cached(
            source,
            url,
            params,
            parse,
            factor,
            lambda: self._opendap_open(url, select),
        )
