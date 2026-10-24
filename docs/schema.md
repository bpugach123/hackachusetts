# Static_Output schema

Schema version: `1.0`. The Pipeline writes these files to `output/`; `output/sample/` holds a hand-built copy with the same shapes. Machine-readable JSON Schema (draft 2020-12) for each file lives in `docs/schema/`, and `pipeline/tests/test_schema.py` validates sample and real output against them.

| File | Schema | Content |
|---|---|---|
| `metadata.json` | `schema/metadata.schema.json` | Run info, Device_Config, citations, gap summary, factor legend |
| `cells.geojson` | `schema/cells.schema.json` | FeatureCollection, one Polygon Feature per Cell, properties = `CellRecord` |
| `timeseries.json` | `schema/timeseries.schema.json` | Per-cell velocity speed series (raw input for frontend CF recompute) |
| `boundary_3nm.geojson` | `schema/boundary_3nm.schema.json` | State/federal 3 nm boundary line(s) for the map toggle |
| `validation.json` | `schema/validation.schema.json` | Known-site validation report |
| `sentiment.json` | `schema/sentiment.schema.json` | (Stretch) Tier 3 narrative notes for top-5 cells |

Conventions:
- "Null" = the field is always present but may be `null`. "Optional" = the key may be absent.
- Numbers are SI unless the unit column says otherwise. Scores are 0-100, rounded to 1 dp at write time.
- Coordinates are WGS84 decimal degrees. GeoJSON positions are `[lon, lat]`.
- Dates are `YYYY-MM-DD`; timestamps are ISO 8601 UTC with a `Z` suffix.
- A Data_Gap value is always `null` with a `DataGap` record. No field ever substitutes 0, a default, or a neighbor value for a gap.

## Shared types

### `DataGap`

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `factor` | string | — | no | Factor key or component name (`resource`, `feasibility`, `conflict`, `jurisdiction`, ...) |
| `source` | string | — | no | Source name, e.g. `NDBC realtime2` |
| `reason` | `"fetch failure"` \| `"empty response"` \| `"no coverage"` | — | no | Closed set (Req 2.6) |
| `detail` | string | — | optional | Free text, e.g. `no API key`, `nearest station 31.2 km > 25 km` |

### `Citation` (in `metadata.json` `citations[]`)

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `id` | string | — | no | Unique; referenced by `Factor.citation_id` |
| `agency` | string | — | no | Non-empty |
| `dataset` | string | — | no | Non-empty; velocity citations name NECOFS FVCOM, Doppio, or MOM6 |
| `endpoint` | string | — | no | Non-empty URL or service path |
| `date` | string `YYYY-MM-DD` | — | no | Model run date or fetch date |
| `date_kind` | `"model_run"` \| `"fetch"` | — | no | Model velocity sources use `model_run` |
| `note` | string | — | optional | e.g. `modeled current`, `not blended`, price period |

### `Factor`

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `value` | number \| string | see factor table | yes | `null` if and only if `gap` is present |
| `unit` | string | — | yes | `null` for dimensionless / text values |
| `tags` | array of `"QUANTIFIED DATA"` \| `"PROXY"` \| `"EXTERNAL ADDITION"` | — | no | Exactly one of QUANTIFIED DATA / PROXY, optionally plus EXTERNAL ADDITION |
| `citation_id` | string | — | no | Must match a `metadata.citations[].id` |
| `label` | string | — | optional | Required on `velocity_*`: `modeled current` or `predicted current (harmonic)`. `utility` uses `distribution interconnection below 5 MW` |
| `note` | string | — | optional | Supporting detail (station id/distance, record period, `not blended`) |
| `gap` | `DataGap` | — | optional | Present if and only if `value` is `null` |

### Factor keys (closed set)

`CellRecord.factors` may contain only these keys. Stretch or not-computed factors are absent rather than null. The provenance legend has one row per key.

| Key | Value type | Unit | Component | Typical tags |
|---|---|---|---|---|
| `velocity_peak` | number | m/s | Resource (stream) | QUANTIFIED DATA |
| `velocity_source_period` | string (`"{source} {t_start}..{t_end}"`) | — | Resource (stream) | QUANTIFIED DATA |
| `power_density` | number | W/m² | Resource (stream) | QUANTIFIED DATA |
| `depth` | number | m | Resource / Feasibility | QUANTIFIED DATA (+ EXTERNAL ADDITION if CRM) |
| `qualifying_area` | number | km² | Resource (stream) | QUANTIFIED DATA |
| `cf_stream` | number [0, 1] | — | Resource (stream) | QUANTIFIED DATA |
| `energy_stream_kwh` | number | kWh/yr | Resource (stream) | QUANTIFIED DATA |
| `tidal_range` | number | m | Resource (rise/fall) | QUANTIFIED DATA |
| `coops_station` | string (station id) | — | Resource (rise/fall) | QUANTIFIED DATA |
| `energy_cycle_j` | number | J | Resource (rise/fall) | QUANTIFIED DATA |
| `cf_rise_fall` | number [0, 1] | — | Resource (rise/fall) | QUANTIFIED DATA |
| `energy_rise_fall_kwh` | number | kWh/yr | Resource (rise/fall) | QUANTIFIED DATA |
| `wave_power` | number | kW/m | Context only (`note: "not blended"`) | QUANTIFIED DATA |
| `wave_hs_mean` | number | m | Feasibility | QUANTIFIED DATA |
| `dist_transmission_km` | number | km (0.01) | Grid Proximity | QUANTIFIED DATA |
| `dist_substation_km` | number | km (0.01) | Grid Proximity | QUANTIFIED DATA |
| `utility` | string | — | Grid Proximity (info) | PROXY |
| `vessel_traffic` | number 0-100 | percentile | Conflict | PROXY |
| `fishing_activity` | number 0-100 | percentile | Conflict | PROXY |
| `diadromous_fish_km` | number | km | Conflict | PROXY |
| `whale_critical_habitat` | number (0 or 100) | — | Conflict | QUANTIFIED DATA + EXTERNAL ADDITION |
| `slope_deg` | number | deg | Feasibility (stretch) | QUANTIFIED DATA + EXTERNAL ADDITION |
| `port_km` | number | km | Feasibility | PROXY + EXTERNAL ADDITION |
| `price_usd_kwh` | number | USD/kWh | Profitability (stretch) | PROXY + EXTERNAL ADDITION |
| `gross_usd` | number | USD/yr | Profitability (stretch) | PROXY + EXTERNAL ADDITION |
| `m_transport` | number ≥ 1 | — | Profitability (stretch) | PROXY |
| `m_transmission` | number ≥ 1 | — | Profitability (stretch) | PROXY |
| `m_maint` | number ≥ 1 | — | Profitability (stretch) | PROXY |
| `net_usd` | number | USD/yr | Profitability (stretch) | PROXY + EXTERNAL ADDITION |

## `metadata.json`

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `schema_version` | `"1.0"` | — | no | |
| `run_timestamp_utc` | string ISO 8601 UTC | — | no | e.g. `2025-01-01T14:03:22Z` |
| `cell_size_mi` | number [0.25, 5] | statute mi | no | |
| `device_config` | object `{key: {value, unit}}` | per key | no | Every Device_Config key used in the run |
| `device_config.*.value` | number \| string \| boolean \| array \| object | per key | yes | |
| `device_config.*.unit` | string | — | yes | `null` for dimensionless |
| `citations` | `Citation[]` | — | no | Deduplicated; ids unique |
| `gap_summary` | object `{source: integer ≥ 0}` | count | no | Data_Gap count per source |
| `factors` | array of `{name, tags, citation_id, unit, label?}` | — | no | One entry per factor key present in the run; `name` is from the closed key set |
| `tier3_notice` | string | — | no | Tier 3 content is narrative only and not scored |
| `disclaimer` | `"Not legal advice"` | — | no | |
| `known_limitations` | string[] | — | no | e.g. NECOFS forecast window |

## `cells.geojson`

FeatureCollection. Each Feature: `type: "Feature"`, optional `id` (= `cell_id`), `geometry` = GeoJSON Polygon (cell square, WGS84), `properties` = `CellRecord`.

### `CellRecord`

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `cell_id` | string `C{size_m}-{iiii}-{jjjj}` | — | no | Deterministic for a given Cell_Size |
| `i`, `j` | integer ≥ 0 | grid index | no | Offset from fixed origin |
| `centroid_lat` | number | deg | no | 6 dp |
| `centroid_lon` | number | deg | no | 6 dp |
| `hard_exclusion` | boolean | — | no | True if and only if `exclusion_layers` is non-empty |
| `exclusion_layers` | string[] | — | no | Every triggering layer name |
| `exclusion_check_incomplete` | string[] | — | no | Exclusion layers that failed to fetch |
| `jurisdiction` | `"state"` \| `"federal"` | — | yes | `null` on boundary gap |
| `agency` | `"MA CZM"` \| `"BOEM"` | — | yes | Paired with `jurisdiction` |
| `straddles_boundary` | boolean | — | no | |
| `disclaimer` | `"Not legal advice"` | — | no | |
| `resource_path` | `"tidal_stream"` \| `"rise_fall"` | — | yes | `null` when Resource is null |
| `scores.resource` | number 0-100 | score | yes | |
| `scores.profitability` | number 0-100 | score | yes, optional | Absent when Profitability was not computed (stretch) |
| `scores.feasibility` | number 0-100 | score | yes | |
| `scores.grid` | number 0-100 | score | yes | |
| `scores.conflict` | number 0-100 | score | yes | 0 = no conflict, 100 = max |
| `scores.tidal_stream_sub` | number 0-100 | score | yes | |
| `scores.rise_fall_sub` | number 0-100 | score | yes | |
| `overall_default` | number 0-100 | score | no | Default weights; 0 if excluded |
| `factors` | object `{FactorKey: Factor}` | — | no | Closed key set above |
| `feasibility_omitted` | array of `"depth"` \| `"slope"` \| `"wave_exposure"` \| `"port_distance"` | — | no | Sub-scores left out of the mean |
| `stream_threshold_failed` | array of `"power_density"` \| `"depth"` \| `"area"` | — | no | Non-empty when the stream sub-score is 0 by threshold |
| `energy_reason` | string | — | yes | `"below EPRI thresholds"` or `null` |
| `gaps` | `DataGap[]` | — | no | All gaps for the cell, including component-level gaps |

## `timeseries.json`

Object keyed by `cell_id`, only for cells with velocity data.

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `t0` | string ISO 8601 UTC | — | no | First sample time |
| `dt_s` | number > 0 | s | no | Sample spacing |
| `speeds_ms` | number[] ≥ 0 | m/s | no | Depth-averaged speed, non-empty |

## `boundary_3nm.geojson`

FeatureCollection of `LineString` / `MultiLineString` / `Polygon` / `MultiPolygon` features. `features` is empty when the boundary fetch failed (cells then carry a jurisdiction gap).

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `features[].geometry` | GeoJSON geometry | deg | no | WGS84 |
| `features[].properties.citation_id` | string | — | no | Matches a metadata citation |
| `features[].properties.disclaimer` | `"Not legal advice"` | — | no | |
| `features[].properties.name` | string | — | optional | |

## `validation.json`

Array with exactly 5 entries, one per Validation_Site.

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `site` | `"Muskeget Channel"` \| `"Woods Hole"` \| `"Cape Cod Canal/Bourne"` \| `"Boston Harbor approaches"` \| `"Cape Cod Bay"` | — | no | |
| `ref_lat`, `ref_lon` | number | deg | no | Req 15.1 coordinates |
| `path` | `"tidal_stream"` \| `"rise_fall"` | — | no | |
| `matched_cell_id` | string | — | yes | `null` when no cell within 3 mi |
| `distance_mi` | number ≥ 0 | statute mi | yes | |
| `resource_percentile` | number 0-100 | percentile | yes | `null` when not evaluated |
| `status` | `"pass"` \| `"validation failure"` \| `"not evaluated"` | — | no | |
| `reason` | string | — | yes | Required (non-null) when `status` is `not evaluated` |

## `sentiment.json` (stretch)

Object keyed by `cell_id`. Tier 3 narrative only; never read by any score function.

| Field | Type | Unit | Null | Notes |
|---|---|---|---|---|
| `note` | string | — | no | Qualitative text, no numeric score |
| `citations` | array (≥ 1) of `{agency, title, url, date}` | — | no | Public comment record, nearby project reception, or local news; `date` is `YYYY-MM-DD` |
