# Feature: ma-tidal-buoy-siting-model, Property 2: Config validation gates the run
"""Property tests for Device_Config validation (Property 2).

**Validates: Requirements 1.2, 1.3**

Valid in-range grid parameters load with their value, unset parameters fall
back to defaults, and any non-numeric or out-of-range value raises a
ConfigError naming the parameter and its accepted range, without touching an
existing Static_Output directory.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from siting import config
from siting.config import CELL_SIZE_RANGE, OUTER_EXTENT_RANGE, ConfigError

GATED = {
    "cell_size_mi": CELL_SIZE_RANGE,
    "outer_extent_nm": OUTER_EXTENT_RANGE,
}
DEFAULTS = {"cell_size_mi": 1.0, "outer_extent_nm": 12.0}

param_names = st.sampled_from(sorted(GATED))


def _write_and_load(raw: dict) -> config.DeviceConfig:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "device_config.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return config.load(path)


def _in_range(name: str) -> st.SearchStrategy[float]:
    lo, hi = GATED[name]
    return st.one_of(
        st.sampled_from([lo, hi]),  # inclusive bounds
        st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False),
    )


def _out_of_range(name: str) -> st.SearchStrategy[float]:
    lo, hi = GATED[name]
    return st.one_of(
        st.floats(max_value=lo, exclude_max=True, allow_nan=False),
        st.floats(min_value=hi, exclude_min=True, allow_nan=False),
        st.sampled_from([math.nan, math.inf, -math.inf]),
    )


non_numeric = st.one_of(
    st.text(max_size=10),
    st.none(),
    st.booleans(),
    st.lists(st.floats(allow_nan=False), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)


@settings(max_examples=100)
@given(data=st.data())
def test_valid_values_load_with_that_value(data: st.DataObject) -> None:
    raw = {name: data.draw(_in_range(name), label=name) for name in GATED}
    cfg = _write_and_load(raw)
    for name, value in raw.items():
        assert getattr(cfg, name) == pytest.approx(value)


@settings(max_examples=100)
@given(present=st.sets(param_names), data=st.data())
def test_unset_parameters_use_defaults(present: set[str], data: st.DataObject) -> None:
    raw = {name: data.draw(_in_range(name), label=name) for name in present}
    cfg = _write_and_load(raw)
    for name in GATED:
        expected = raw.get(name, DEFAULTS[name])
        assert getattr(cfg, name) == pytest.approx(expected)


def _assert_rejects(name: str, bad: object, out_dir: Path) -> None:
    before = {p: p.read_bytes() for p in out_dir.rglob("*") if p.is_file()}
    with pytest.raises(ConfigError) as exc:
        _write_and_load({name: bad})
    lo, hi = GATED[name]
    assert exc.value.param == name
    msg = str(exc.value)
    assert name in msg
    assert f"{lo:g}" in msg and f"{hi:g}" in msg
    after = {p: p.read_bytes() for p in out_dir.rglob("*") if p.is_file()}
    assert after == before


@settings(max_examples=100)
@given(data=st.data())
def test_out_of_range_values_raise_config_error(data: st.DataObject) -> None:
    name = data.draw(param_names, label="param")
    bad = data.draw(_out_of_range(name), label="value")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "metadata.json").write_text('{"run": "previous"}')
        _assert_rejects(name, bad, out)


@settings(max_examples=100)
@given(name=param_names, bad=non_numeric)
def test_non_numeric_values_raise_config_error(name: str, bad: object) -> None:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        (out / "metadata.json").write_text('{"run": "previous"}')
        _assert_rejects(name, bad, out)
