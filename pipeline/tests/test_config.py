"""Unit tests for siting.config (Req 1.1, 1.2, 1.3, 12.1)."""

from dataclasses import fields
from pathlib import Path

import pytest

from siting import config
from siting.config import ConfigError, DeviceConfig

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "device_config.yaml"


def _write(tmp_path, text: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_repo_config_loads_and_matches_defaults():
    assert config.load(REPO_CONFIG) == DeviceConfig()


def test_repo_config_lists_every_key():
    import yaml

    raw = yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))
    assert set(raw) == {f.name for f in fields(DeviceConfig)}


def test_empty_file_applies_defaults(tmp_path):
    cfg = config.load(_write(tmp_path, ""))
    assert cfg.cell_size_mi == 1.0
    assert cfg.outer_extent_nm == 12.0
    assert cfg.rated_power_kw == 100
    assert cfg.tidal_cycles_per_year == 705.8
    assert cfg.seawater_density == 1025


def test_partial_section_merges_with_defaults(tmp_path):
    cfg = config.load(_write(tmp_path, "depth_bands:\n  buoy: [4, 12]\n"))
    assert cfg.depth_bands == {"seabed": (15.0, 35.0), "buoy": (4, 12)}


@pytest.mark.parametrize("value", [0.25, 1, 5])
def test_cell_size_boundaries_accepted(tmp_path, value):
    assert (
        config.load(_write(tmp_path, f"cell_size_mi: {value}\n")).cell_size_mi == value
    )


@pytest.mark.parametrize("value", [3, 200])
def test_outer_extent_boundaries_accepted(tmp_path, value):
    assert (
        config.load(_write(tmp_path, f"outer_extent_nm: {value}\n")).outer_extent_nm
        == value
    )


@pytest.mark.parametrize(
    "param,value,expected_range",
    [
        ("cell_size_mi", "0.24", "[0.25, 5]"),
        ("cell_size_mi", "5.01", "[0.25, 5]"),
        ("cell_size_mi", "'1'", "[0.25, 5]"),
        ("cell_size_mi", "abc", "[0.25, 5]"),
        ("cell_size_mi", "true", "[0.25, 5]"),
        ("cell_size_mi", ".nan", "[0.25, 5]"),
        ("cell_size_mi", "null", "[0.25, 5]"),
        ("outer_extent_nm", "2.9", "[3, 200]"),
        ("outer_extent_nm", "201", "[3, 200]"),
        ("outer_extent_nm", ".inf", "[3, 200]"),
        ("outer_extent_nm", "[12]", "[3, 200]"),
    ],
)
def test_invalid_gate_values_raise_with_param_and_range(
    tmp_path, param, value, expected_range
):
    with pytest.raises(ConfigError) as exc:
        config.load(_write(tmp_path, f"{param}: {value}\n"))
    assert exc.value.param == param
    assert expected_range in exc.value.accepted_range
    assert param in str(exc.value) and expected_range in str(exc.value)


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError) as exc:
        config.load(_write(tmp_path, "cell_size_miles: 1\n"))
    assert exc.value.param == "cell_size_miles"


def test_as_metadata_has_value_and_unit_for_every_key():
    meta = config.as_metadata(DeviceConfig())
    assert meta["rated_power_kw"] == {"value": 100.0, "unit": "kW"}
    assert meta["cell_size_mi"]["unit"] == "mi"
    assert meta["seawater_density"]["unit"] == "kg/m³"
    assert meta["depth_bands.seabed"] == {"value": [15.0, 35.0], "unit": "m"}
    assert meta["feasibility.port_km"]["unit"] == "km"
    assert meta["profitability.baseline_annual_cost_usd"]["unit"] == "USD/yr"
    assert meta["conflict_weights"] == {"value": None, "unit": "weight (dimensionless)"}
    # Every dataclass field is represented, directly or as a dotted section.
    for f in fields(DeviceConfig):
        assert any(k == f.name or k.startswith(f.name + ".") for k in meta), f.name
    for entry in meta.values():
        assert set(entry) == {"value", "unit"} and entry["unit"]


def test_as_metadata_is_json_serializable():
    import json

    json.dumps(config.as_metadata(DeviceConfig()))
