"""Fetch layer: cache, retry, DataGap results, GapRegistry (Req 2.2-2.8)."""

from __future__ import annotations

import json

import numpy as np
import pytest
import xarray as xr

from siting.fetch.cache import Cache, cache_key, canonical_json
from siting.fetch.client import (
    BACKOFF_S,
    EmptyResponse,
    FetchClient,
    Response,
    TransportError,
    require_features,
    require_finite,
)
from siting.fetch.gaps import DataGap, GapRegistry

URL = "https://example.gov/api"
PARAMS = {"b": 2, "a": 1}


class FakeTransport:
    """Replays a scripted list of Responses or exceptions, recording calls."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(body: bytes) -> Response:
    return Response(200, body)


@pytest.fixture
def sleeps():
    return []


def client(tmp_path, transport, sleeps, opener=None) -> FetchClient:
    return FetchClient(
        tmp_path, transport=transport, opener=opener, sleep=sleeps.append
    )


# ---------------------------------------------------------------- cache


def test_cache_key_is_order_independent_and_source_specific():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert cache_key("S", URL, {"a": 1, "b": 2}) == cache_key("S", URL, PARAMS)
    assert cache_key("S", URL, PARAMS) != cache_key("T", URL, PARAMS)
    assert len(cache_key("S", URL, PARAMS)) == 64


def test_live_fetch_writes_bin_and_meta(tmp_path, sleeps):
    t = FakeTransport(ok(b'{"x": 1}'))
    c = client(tmp_path, t, sleeps)
    assert c.get("NOAA_COOPS", URL, PARAMS, json.loads) == {"x": 1}

    key = cache_key("NOAA_COOPS", URL, PARAMS)
    d = tmp_path / "NOAA_COOPS"
    assert (d / f"{key}.bin").read_bytes() == b'{"x": 1}'
    meta = json.loads((d / f"{key}.meta.json").read_text())
    assert meta["url"] == URL and meta["params"] == PARAMS
    assert len(meta["fetch_date"]) == 10
    assert c.fetch_date("NOAA_COOPS", URL, PARAMS) == meta["fetch_date"]


def test_cache_hit_makes_no_network_call(tmp_path, sleeps):
    c = client(tmp_path, FakeTransport(ok(b"[1]")), sleeps)
    c.get("S", URL, PARAMS, json.loads)

    offline = FakeTransport()  # any call would pop from an empty list
    c2 = client(tmp_path, offline, sleeps)
    assert c2.get("S", URL, PARAMS, json.loads) == [1]
    assert offline.calls == []


def test_unparseable_cache_entry_is_evicted_and_refetched(tmp_path, sleeps):
    key = cache_key("S", URL, PARAMS)
    Cache(tmp_path).write("S", key, b"not json", URL, PARAMS)
    t = FakeTransport(ok(b"[2]"))
    assert client(tmp_path, t, sleeps).get("S", URL, PARAMS, json.loads) == [2]
    assert len(t.calls) == 1
    assert (tmp_path / "S" / f"{key}.bin").read_bytes() == b"[2]"


def test_unreadable_meta_is_treated_as_miss(tmp_path, sleeps):
    key = cache_key("S", URL, PARAMS)
    Cache(tmp_path).write("S", key, b"[1]", URL, PARAMS)
    (tmp_path / "S" / f"{key}.meta.json").write_text("{broken")
    t = FakeTransport(ok(b"[3]"))
    assert client(tmp_path, t, sleeps).get("S", URL, PARAMS, json.loads) == [3]
    assert len(t.calls) == 1


# ---------------------------------------------------------------- retry


def test_retries_then_succeeds_with_backoff(tmp_path, sleeps):
    t = FakeTransport(TransportError("timeout"), Response(503, b""), ok(b"[1]"))
    assert client(tmp_path, t, sleeps).get("S", URL, PARAMS, json.loads) == [1]
    assert len(t.calls) == 3
    assert sleeps == list(BACKOFF_S)


def test_three_failures_give_fetch_failure_gap_and_no_cache(tmp_path, sleeps):
    t = FakeTransport(
        Response(500, b"err"), TransportError("conn reset"), Response(404, b"")
    )
    gap = client(tmp_path, t, sleeps).get(
        "HIFLD_TRANSMISSION", URL, PARAMS, json.loads, factor="dist_transmission_km"
    )
    assert isinstance(gap, DataGap)
    assert (gap.factor, gap.source, gap.reason) == (
        "dist_transmission_km",
        "HIFLD_TRANSMISSION",
        "fetch failure",
    )
    assert gap.detail == "TransportError: HTTP 404"
    assert len(t.calls) == 3 and sleeps == [2.0, 4.0]
    assert not (tmp_path / "HIFLD_TRANSMISSION").exists()


# ---------------------------------------------------------------- empty


def test_empty_body_is_empty_response_gap(tmp_path, sleeps):
    gap = client(tmp_path, FakeTransport(ok(b"")), sleeps).get(
        "S", URL, PARAMS, json.loads
    )
    assert gap.reason == "empty response" and gap.source == "S"
    assert gap.factor == "*"
    assert not (tmp_path / "S").exists()


def test_zero_features_is_empty_response_gap(tmp_path, sleeps):
    body = b'{"type": "FeatureCollection", "features": []}'
    gap = client(tmp_path, FakeTransport(ok(body)), sleeps).get(
        "S", URL, PARAMS, lambda b: require_features(json.loads(b))
    )
    assert isinstance(gap, DataGap) and gap.reason == "empty response"


def test_all_nan_is_empty_response_gap(tmp_path, sleeps):
    gap = client(tmp_path, FakeTransport(ok(b"[null, null]")), sleeps).get(
        "S", URL, PARAMS, lambda b: require_finite(json.loads(b))
    )
    assert isinstance(gap, DataGap) and gap.reason == "empty response"


def test_require_helpers_pass_real_data():
    fc = {"features": [{"type": "Feature"}]}
    assert require_features(fc) is fc
    vals = require_finite([1, None, "x"])
    assert vals[0] == 1.0 and np.isnan(vals[1]) and np.isnan(vals[2])
    with pytest.raises(EmptyResponse):
        require_finite([])


def test_unparseable_live_body_is_gap_not_value(tmp_path, sleeps):
    gap = client(tmp_path, FakeTransport(ok(b"<html>")), sleeps).get(
        "S", URL, PARAMS, json.loads
    )
    assert isinstance(gap, DataGap) and gap.reason == "fetch failure"
    assert gap.detail.startswith("unparseable response")
    assert not (tmp_path / "S").exists()


# ---------------------------------------------------------------- OPeNDAP


def _ds(values) -> xr.Dataset:
    return xr.Dataset(
        {"ua": ("time", np.asarray(values, dtype=float))},
        coords={"time": np.arange(len(values))},
    )


def _select(ds):
    return ds.sel(time=slice(0, 2))


def test_opendap_subset_cached_as_netcdf_and_reused(tmp_path, sleeps):
    opens: list[str] = []

    def opener(url):
        opens.append(url)
        return _ds([0.5, 1.0, 1.5, 2.0])

    p = {"var": "ua", "t": [0, 2]}
    c = client(tmp_path, FakeTransport(), sleeps, opener=opener)
    ds = c.get_opendap("NECOFS_FVCOM", URL, p, _select)
    assert list(ds.ua.values) == [0.5, 1.0, 1.5]

    key = cache_key("NECOFS_FVCOM", URL, p)
    assert (tmp_path / "NECOFS_FVCOM" / f"{key}.bin").read_bytes()[:3] == b"\x89HD"

    def no_open(url):
        raise AssertionError("network used on cache hit")

    c2 = client(tmp_path, FakeTransport(), sleeps, opener=no_open)
    assert list(c2.get_opendap("NECOFS_FVCOM", URL, p, _select).ua.values) == [
        0.5,
        1.0,
        1.5,
    ]
    assert opens == [URL]


def test_opendap_retries_then_fetch_failure(tmp_path, sleeps):
    def opener(url):
        raise OSError("DAP server down")

    gap = client(tmp_path, FakeTransport(), sleeps, opener=opener).get_opendap(
        "DOPPIO", URL, {"v": 1}, _select, factor="velocity_peak"
    )
    assert (gap.factor, gap.source, gap.reason) == (
        "velocity_peak",
        "DOPPIO",
        "fetch failure",
    )
    assert sleeps == [2.0, 4.0]


def test_opendap_all_nan_is_empty_response(tmp_path, sleeps):
    gap = client(
        tmp_path, FakeTransport(), sleeps, opener=lambda u: _ds([np.nan] * 4)
    ).get_opendap("MOM6", URL, {"v": 1}, _select)
    assert isinstance(gap, DataGap) and gap.reason == "empty response"
    assert not (tmp_path / "MOM6").exists()


# ---------------------------------------------------------------- registry


def test_gap_registry_add_and_summary():
    reg = GapRegistry()
    g = reg.add("c1", "velocity_peak", "NECOFS_FVCOM", "no coverage")
    assert g == DataGap("velocity_peak", "NECOFS_FVCOM", "no coverage")
    reg.add("c2", "velocity_peak", "NECOFS_FVCOM", "no coverage")
    reg.add("*", DataGap("*", "EIA", "fetch failure", detail="HTTP 500"))
    assert reg.summary() == {"EIA": 1, "NECOFS_FVCOM": 2}
    assert len(reg) == 3


def test_gap_registry_rejects_bad_input():
    reg = GapRegistry()
    with pytest.raises(ValueError):
        reg.add("c1", "f", "S", "timeout")
    with pytest.raises(ValueError):
        reg.add("c1", "f")


def test_gap_for_factor_relabels_and_serializes():
    gap = DataGap("*", "NDBC", "empty response").for_factor("wave_power_kwm")
    assert gap.to_dict() == {
        "factor": "wave_power_kwm",
        "source": "NDBC",
        "reason": "empty response",
    }
