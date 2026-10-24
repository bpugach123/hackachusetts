"""Device_Config loading and validation (Req 1.1, 1.2, 1.3, 12.1).

``load(path)`` reads ``config/device_config.yaml``, fills defaults for any
missing key, and validates the grid parameters before anything else runs.
The pipeline calls it first so an invalid Cell_Size or outer extent stops the
run before any fetch and before the Static_Output is touched.

``as_metadata(cfg)`` returns ``{key: {"value": ..., "unit": ...}}`` for the
``device_config`` block of ``metadata.json``. Nested sections are flattened
to dotted keys (``depth_bands.seabed``, ``profitability.baseline_annual_cost_usd``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

# Accepted ranges (inclusive) for the parameters that gate the run.
CELL_SIZE_RANGE = (0.25, 5.0)
OUTER_EXTENT_RANGE = (3.0, 200.0)

# Hard_Exclusion layer groups (``score/conflict.py`` EXCLUSION_GROUPS).
HARD_EXCLUSION_LAYER_IDS: tuple[str, ...] = (
    "protected_habitat",
    "residential_shoreline",
    "shipping_lanes",
    "cable_areas",
)


class ConfigError(ValueError):
    """Invalid Device_Config value. Names the parameter and its accepted range."""

    def __init__(self, param: str, accepted_range: str, detail: str | None = None):
        self.param = param
        self.accepted_range = accepted_range
        msg = f"{param} must be {accepted_range}"
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


def _default_depth_bands() -> dict:
    return {"seabed": (15.0, 35.0), "buoy": (5.0, 15.0)}


def _default_feasibility() -> dict:
    # (full-score threshold, zero-score threshold) for each linear ramp.
    return {"slope_deg": (5.0, 15.0), "wave_hs_m": (1.0, 3.0), "port_km": (10.0, 50.0)}


def _default_profitability() -> dict:
    # Illustrative PROXY slopes (Req 10.2). Each multiplier = 1 + k * input, k >= 0.
    return {
        "k_transport_per_km": 0.01,
        "k_transmission_per_km": 0.01,
        "k_maint_port_per_km": 0.01,
        "k_maint_wave_per_m": 0.10,
        "baseline_annual_cost_usd": 20000.0,
    }


def _default_exclusion_layers() -> list[str]:
    return [
        "protected_habitat",
        "residential_shoreline",
        "shipping_lanes",
        "cable_areas",
    ]


@dataclass(frozen=True)
class DeviceConfig:
    cell_size_mi: float = 1.0
    outer_extent_nm: float = 12.0
    rated_power_kw: float = 100.0
    rotor_diameter_m: float = 5.0
    power_coefficient: float = 0.40
    pad_area_m2: float = 50.0
    conversion_efficiency: float = 0.30
    tidal_cycles_per_year: float = 705.8
    seawater_density: float = 1025.0
    velocity_source_priority: tuple = (
        "NECOFS_FVCOM",
        "DOPPIO",
        "MOM6",
        "NOAA_CURRENTS",
    )
    min_power_density_wm2: float = 500.0
    min_depth_m: float = 5.0
    min_qualifying_area_km2: float = 0.5
    baseline_credit: float = 40.0
    benchmark_velocity_ms: float = 2.5
    max_station_distance_km: float = 25.0
    max_buoy_distance_km: float = 100.0
    min_range_m: float = 1.0
    max_range_m: float = 3.0
    grid_full_km: float = 1.0
    grid_zero_km: float = 30.0
    grid_weights: dict = field(
        default_factory=lambda: {"transmission": 0.5, "substation": 0.5}
    )
    fish_zero_km: float = 5.0
    conflict_weights: dict | None = None  # None = equal weights
    depth_bands: dict = field(default_factory=_default_depth_bands)
    feasibility: dict = field(default_factory=_default_feasibility)
    profitability: dict = field(default_factory=_default_profitability)
    hard_exclusion_layers: list = field(default_factory=_default_exclusion_layers)


# Unit for every key. Dict-valued sections map sub-key -> unit.
UNITS: dict[str, Any] = {
    "cell_size_mi": "mi",
    "outer_extent_nm": "nmi",
    "rated_power_kw": "kW",
    "rotor_diameter_m": "m",
    "power_coefficient": "dimensionless",
    "pad_area_m2": "m²",
    "conversion_efficiency": "dimensionless",
    "tidal_cycles_per_year": "cycles/yr",
    "seawater_density": "kg/m³",
    "velocity_source_priority": "source id",
    "min_power_density_wm2": "W/m²",
    "min_depth_m": "m",
    "min_qualifying_area_km2": "km²",
    "baseline_credit": "score (0-100)",
    "benchmark_velocity_ms": "m/s",
    "max_station_distance_km": "km",
    "max_buoy_distance_km": "km",
    "min_range_m": "m",
    "max_range_m": "m",
    "grid_full_km": "km",
    "grid_zero_km": "km",
    "grid_weights": "weight (dimensionless)",
    "fish_zero_km": "km",
    "conflict_weights": "weight (dimensionless)",
    "depth_bands": "m",
    "feasibility": {"slope_deg": "deg", "wave_hs_m": "m", "port_km": "km"},
    "profitability": {
        "k_transport_per_km": "1/km",
        "k_transmission_per_km": "1/km",
        "k_maint_port_per_km": "1/km",
        "k_maint_wave_per_m": "1/m",
        "baseline_annual_cost_usd": "USD/yr",
    },
    "hard_exclusion_layers": "layer id",
}

_FIELD_NAMES = {f.name for f in fields(DeviceConfig)}
_DICT_SECTIONS = ("grid_weights", "depth_bands", "feasibility", "profitability")
_PAIR_SECTIONS = ("depth_bands", "feasibility")


def _fmt(x: float) -> str:
    return f"{x:g}"


def _validate_range(name: str, value: Any, bounds: tuple[float, float]) -> float:
    lo, hi = bounds
    accepted = f"numeric in [{_fmt(lo)}, {_fmt(hi)}]"
    # bool is an int subclass in Python; YAML `true` is not a valid size.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(name, accepted, f"got {value!r}")
    if math.isnan(value) or not (lo <= value <= hi):
        raise ConfigError(name, accepted, f"got {value!r}")
    return float(value)


def from_dict(raw: dict | None) -> DeviceConfig:
    """Build a validated DeviceConfig from a parsed mapping, applying defaults."""
    raw = dict(raw or {})
    unknown = sorted(set(raw) - _FIELD_NAMES)
    if unknown:
        raise ConfigError(unknown[0], "a known Device_Config key", "unknown key")

    # Gate parameters (Req 1.2, 1.3). A missing key falls back to its default.
    for name, bounds in (
        ("cell_size_mi", CELL_SIZE_RANGE),
        ("outer_extent_nm", OUTER_EXTENT_RANGE),
    ):
        if name in raw:
            raw[name] = _validate_range(name, raw[name], bounds)

    defaults = DeviceConfig()
    # Dict sections merge over defaults so a partial section keeps the other sub-keys.
    for name in _DICT_SECTIONS:
        if name in raw:
            if not isinstance(raw[name], dict):
                raise ConfigError(name, "a mapping", f"got {raw[name]!r}")
            merged = {**getattr(defaults, name), **raw[name]}
            if name in _PAIR_SECTIONS:
                merged = {
                    k: tuple(v) if isinstance(v, (list, tuple)) else v
                    for k, v in merged.items()
                }
            raw[name] = merged
    if "velocity_source_priority" in raw:
        raw["velocity_source_priority"] = tuple(raw["velocity_source_priority"])
    if "hard_exclusion_layers" in raw:
        raw["hard_exclusion_layers"] = validate_exclusion_layers(
            raw["hard_exclusion_layers"]
        )

    return DeviceConfig(**raw)


def validate_exclusion_layers(value: Any) -> list[str]:
    """``hard_exclusion_layers`` must be a list of known layer group ids.

    Right whale critical habitat and diadromous fish are graded Conflict
    sub-scores, never exclusions, so they are not accepted here (Req 7.5).
    """
    accepted = f"a list drawn from {list(HARD_EXCLUSION_LAYER_IDS)}"
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ConfigError("hard_exclusion_layers", accepted, f"got {value!r}")
    bad = [v for v in value if v not in HARD_EXCLUSION_LAYER_IDS]
    if bad:
        raise ConfigError("hard_exclusion_layers", accepted, f"unknown id {bad[0]!r}")
    return list(dict.fromkeys(value))  # drop repeats, keep order


def load(path: str | Path) -> DeviceConfig:
    """Load and validate the Device_Config YAML.

    Raises ConfigError on invalid values.
    """
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is not None and not isinstance(raw, dict):
        raise ConfigError("device_config", "a YAML mapping of key: value")
    return from_dict(raw)


def _jsonable(v: Any) -> Any:
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def as_metadata(cfg: DeviceConfig) -> dict[str, dict[str, Any]]:
    """Every config key with value and unit, for metadata.json ``device_config``.

    Dict sections with fixed sub-keys are flattened to ``section.sub_key``.
    ``conflict_weights`` stays a single entry because it may be null.
    """
    out: dict[str, dict[str, Any]] = {}
    for f in fields(DeviceConfig):
        value = getattr(cfg, f.name)
        unit = UNITS[f.name]
        if f.name in _DICT_SECTIONS:
            for sub, sub_val in value.items():
                sub_unit = (
                    unit.get(sub, "unspecified") if isinstance(unit, dict) else unit
                )
                out[f"{f.name}.{sub}"] = {"value": _jsonable(sub_val), "unit": sub_unit}
        else:
            out[f.name] = {"value": _jsonable(value), "unit": unit}
    return out
