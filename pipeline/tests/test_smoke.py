"""Offline tests for the endpoint registry and smoke-test probes (no network)."""

from __future__ import annotations

import pytest
import requests

from siting.provenance import is_known_source
from siting.sources import smoke
from siting.sources.endpoints import ALL_ENDPOINTS, Endpoint


class FakeResp:
    def __init__(self, status=200, json_data=None, text="", content=b""):
        self.status_code = status
        self._json = json_data
        self.text = text
        self._content = content
        self.headers = {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=1):
        yield self._content[:chunk_size]

    def close(self):
        pass


def getter(resp=None, exc=None):
    calls = []

    def get(url, params=None, timeout=None, stream=False):
        calls.append((url, params))
        if exc is not None:
            raise exc
        return resp

    get.calls = calls
    return get


def ep(**kw) -> Endpoint:
    base = dict(source="X", agency="A", dataset="D", url="https://h/x/0", kind="arcgis")
    base.update(kw)
    return Endpoint(**base)


# ------------------------------------------------------------------ registry


def test_registry_sources_are_known_to_provenance():
    for e in ALL_ENDPOINTS:
        assert is_known_source(e.source), e.source


def test_verified_entries_have_date_and_unresolved_have_none():
    for e in ALL_ENDPOINTS:
        assert e.status in {"VERIFIED", "UNRESOLVED"}
        if e.status == "VERIFIED":
            assert e.verified
        else:
            assert e.verified is None and "Data_Gap" in (e.notes or "")


def test_no_api_key_hardcoded():
    for e in ALL_ENDPOINTS:
        assert "api_key=" not in e.url
        assert "api_key" not in e.extra.get("smoke_params", {})


# -------------------------------------------------------------------- probes


def test_arcgis_count_ok_and_zero_is_gap():
    assert smoke.probe_arcgis(
        ep(), ep().url, getter(FakeResp(json_data={"count": 3})), 1
    ).ok
    p = smoke.probe_arcgis(ep(), ep().url, getter(FakeResp(json_data={"count": 0})), 1)
    assert not p.ok and "zero features" in p.reason


def test_arcgis_error_payload_is_gap():
    g = getter(FakeResp(json_data={"error": {"message": "Invalid URL"}}))
    p = smoke.probe_arcgis(ep(), ep().url, g, 1)
    assert not p.ok and "Invalid URL" in p.reason


def test_arcgis_passes_where_and_bbox():
    g = getter(FakeResp(json_data={"count": 1}))
    e = ep(extra={"where": "a = 'b'", "smoke_bbox": (1, 2, 3, 4)})
    smoke.probe_arcgis(e, e.url, g, 1)
    url, params = g.calls[0]
    assert url.endswith("/0/query")
    assert params["where"] == "a = 'b'" and params["geometry"] == "1,2,3,4"


@pytest.mark.parametrize(
    "exc,reason",
    [
        (requests.exceptions.SSLError("x"), "TLS"),
        (requests.exceptions.Timeout("x"), "timeout"),
        (requests.exceptions.ConnectionError("x"), "connection"),
    ],
)
def test_network_errors_become_gaps_not_exceptions(exc, reason):
    p = smoke.probe_arcgis(ep(), ep().url, getter(exc=exc), 1)
    assert not p.ok and reason in p.reason


def test_http_error_is_gap():
    p = smoke.probe_opendap(ep(kind="opendap"), "u", getter(FakeResp(status=404)), 1)
    assert not p.ok and p.reason == "HTTP 404"


def test_opendap_missing_vars():
    e = ep(kind="opendap", extra={"vars": ("ua", "va")})
    g = getter(FakeResp(text="Dataset {\n Float32 ua[time = 1];\n}"))
    p = smoke.probe_opendap(e, e.url, g, 1)
    assert not p.ok and "va" in p.reason


def test_raster_unique_value_key():
    data = {"results": [{"attributes": {"UniqueValue.Pixel Value": "2"}}]}
    e = ep(kind="arcgis_raster", extra={"layer": 0})
    assert smoke.probe_arcgis_raster(
        e, e.url, getter(FakeResp(json_data=data)), 1, 0
    ).ok
    nodata = {"results": [{"attributes": {"Pixel Value": "NoData"}}]}
    assert not smoke.probe_arcgis_raster(
        e, e.url, getter(FakeResp(json_data=nodata)), 1, 0
    ).ok


def test_rest_missing_api_key_is_gap_without_request(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    e = ep(kind="rest", extra={"api_key_env": "EIA_API_KEY", "expect_key": "response"})
    g = getter(FakeResp(json_data={"response": [1]}))
    p = smoke.probe_rest(e, e.url, g, 1)
    assert not p.ok and p.reason == "no API key" and g.calls == []


def test_rest_api_key_never_in_reason(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "SECRET123")
    e = ep(kind="rest", extra={"api_key_env": "EIA_API_KEY", "expect_key": "response"})
    g = getter(FakeResp(json_data={"error": {"message": "bad key SECRET123"}}))
    p = smoke.probe_rest(e, e.url, g, 1)
    assert not p.ok and "SECRET123" not in p.reason


def test_unresolved_reported_as_gap_without_request():
    e = ep(status="UNRESOLVED", verified=None, notes="UNRESOLVED -> Data_Gap: gone")
    g = getter(FakeResp(json_data={"count": 5}))
    r = smoke.smoke_one(e, g, 1)
    assert not r.ok and g.calls == []
    assert smoke.format_result(r).startswith("GAP")


def test_fallback_rescues_primary():
    e = ep(fallback="https://h/y/0")

    def get(url, params=None, timeout=None, stream=False):
        if "/x/" in url:
            return FakeResp(status=500)
        return FakeResp(json_data={"count": 2})

    r = smoke.smoke_one(e, get, 1)
    assert r.ok and "via fallback" in smoke.format_result(r)
