# Implementation Plan: MA Tidal Buoy Siting Model

## Overview

Python Pipeline (Track A scoring, Track C geo data) writes Static_Output. A React + Leaflet TypeScript Frontend (Track B) reads it. Track D owns the schema, the hand-built sample output, and the README.

The plan is ordered so a WORKING CORE lands first: grid, fetch/cache/gap layer, provenance, Resource, Grid Proximity, Conflict + exclusions, Jurisdiction, validation report, output writer, Overall recompute, and map + table UI. Track B builds against `output/sample/` from hour 1, so it never waits on the Pipeline.

Track labels: [A] scoring backend, [B] frontend, [C] grid proximity / conflict / jurisdiction data, [D] writeup + sample data.

All STRETCH work is grouped in the "STRETCH GOALS" section near the end. Every stretch sub-task is marked optional (`*`), so scope can be cut without re-planning. Core does not depend on any stretch task.

## Tasks

### CORE

- [ ] 1. Scaffold, contracts, and hour-1 unblockers
  - [x] 1.1 [A] Scaffold the Python pipeline package
    - Create `pipeline/pyproject.toml` (geopandas, shapely, xarray, netCDF4, requests, pyyaml, jsonschema; dev: pytest, hypothesis) with pinned versions
    - Create the `pipeline/siting/` layout from the design (`fetch/`, `sources/`, `score/`, `run.py` stub) and `pipeline/tests/`
    - Create top-level `config/`, `data/cache/` (with `.gitkeep`), `output/`, `docs/`
    - Create `.gitignore` covering `data/cache/*`, `.env`, `*.key`, `node_modules/`, `__pycache__/`
    - _Requirements: 16.1, 16.6_
  - [x] 1.2 [B] Scaffold the frontend
    - Vite + React + TS + react-leaflet + leaflet in `frontend/`; vitest + fast-check dev deps (pinned)
    - Read `VITE_DATA_DIR` (default `/output`, `/output/sample` for sample mode); create `src/data`, `src/model`, `src/components`, `tests/`
    - _Requirements: 16.1_
  - [x] 1.3 [D] Write the Static_Output schema docs
    - `docs/schema.md` plus `docs/schema/{metadata,cells,timeseries,boundary_3nm,validation,sentiment}.schema.json` matching the design's `CellRecord`, `Factor`, `DataGap`, metadata, and validation shapes
    - List each field with type, unit, and nullability; include the closed factor-key set
    - _Requirements: 16.4, 3.4_
  - [x] 1.4 [C] Endpoint smoke test (first hour)
    - Create `pipeline/siting/sources/endpoints.py` holding every URL from the design's endpoint table
    - Create `python -m siting.sources.smoke`: hit each endpoint once with a tiny bbox and print OK/GAP per source
    - Resolve the "resolve at build start" endpoints (MOM6 NWA12, MassGIS utility layer, BTS/NTAD ports, NE Ocean Data Portal vessel + diadromous layers, NOAA Fisheries right whale, MarineCadastre cable/shipping/MPA, HIFLD mirror). Record final URLs or the chosen fallback in `endpoints.py`
    - _Requirements: 2.1, 6.1_
  - [x] 1.5 [D] Hand-build `output/sample/` (unblocks Track B)
    - 12 cells covering: 2 Hard_Exclusion cells, 1 velocity Data_Gap, 1 straddling cell, both resource paths, and one profitability-absent variant
    - Include `metadata.json`, `cells.geojson`, `timeseries.json`, `boundary_3nm.geojson`, `validation.json`, all with realistic tags and citations
    - _Requirements: 16.5_
  - [ ] 1.6 [D] Write a schema conformance test
    - `pipeline/tests/test_schema.py` validates every file under `output/sample/` (and `output/` when present) against `docs/schema/*.schema.json`
    - _Requirements: 16.4, 16.5_

- [x] 2. [A] Config, score helpers, and provenance model
  - [x] 2.1 [A] Implement Device_Config loading and validation
    - `config/device_config.yaml` with every key and unit from the design's `DeviceConfig`, plus comments citing the sources for seawater density and Cp
    - `siting/config.py` `load(path)`: apply defaults; raise `ConfigError(param, accepted_range)` for a non-numeric or out-of-range `cell_size_mi` [0.25, 5] or `outer_extent_nm` [3, 200]
    - _Requirements: 1.1, 1.2, 1.3, 12.1_
  - [x] 2.2 [A] Property test: config validation gates the run
    - **Property 2: Config validation gates the run**
    - **Validates: Requirements 1.2, 1.3**
    - `tests/test_config_props.py` (hypothesis)
  - [x] 2.3 [A] Implement `score/ramps.py`
    - `ramp(x, x0, x1, y0, y1)` supporting descending ramps; `weighted_mean_available(scores, weights)` returning the value plus the omitted names
    - _Requirements: 6.4, 6.5, 6.6, 7.4, 7.5, 7.6, 9.1, 9.3, 9.4, 9.5, 9.6_
  - [ ]* 2.4 [A] Property test: linear ramp correctness (Python side)
    - **Property 16: Linear ramp correctness**
    - **Validates: Requirements 6.4, 7.5, 9.1, 9.2, 9.3, 9.4**
    - `tests/test_ramp_props.py`
  - [ ]* 2.5 [A] Property test: weighted mean of available sub-scores
    - **Property 17: Weighted mean of available sub-scores**
    - **Validates: Requirements 6.5, 6.6, 7.4, 7.6, 9.5, 9.6**
    - `tests/test_weighted_mean_props.py`
  - [x] 2.6 [A] Implement `provenance.py`
    - `Tag`, `Citation`, `Factor`, `FactorResult`, and `validate(cells) -> list[ProvenanceError]`, enforcing the design's rules: one primary tag, non-empty citation with an ISO date, "modeled current" label plus a model name for model velocity, the NOAA predictions exception, and `value is None ⇔ gap is not None`
    - Kit-equivalent source registry that decides when EXTERNAL ADDITION is added
    - _Requirements: 3.1, 3.2, 3.3, 3.8_
  - [ ]* 2.7 [A] Property test: provenance completeness validator
    - **Property 8: Provenance completeness validator**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.8**
    - `tests/test_provenance_props.py`

- [x] 3. [A] Fetch, cache, and Data_Gap layer
  - [x] 3.1 [A] Implement `fetch/client.py`, `fetch/cache.py`, `fetch/gaps.py`
    - sha256 cache key, `data/cache/{source}/{key}.bin` + `.meta.json`; on a cache hit, parse with no network call; on a parse failure, delete and refetch
    - `requests` timeout 60 s, 3 total attempts with 2 s / 4 s backoff; OPeNDAP via the same retry wrapper, with subsets cached as netCDF bytes
    - `DataGap(source, reason ∈ {fetch failure, empty response, no coverage})`; `GapRegistry.add/summary`
    - Injectable transport and sleep so tests run without the network
    - _Requirements: 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_
  - [ ]* 3.2 [A] Property test: cache round trip and corruption recovery
    - **Property 4: Cache round trip and corruption recovery**
    - **Validates: Requirements 2.2, 2.3, 2.4**
    - `tests/test_cache_props.py` with a fake transport and tmp cache dir
  - [ ]* 3.3 [A] Property test: bounded retry
    - **Property 5: Bounded retry**
    - **Validates: Requirements 2.5**
    - `tests/test_retry_props.py`

- [x] 4. [A] Land-masked grid
  - [x] 4.1 [A] Implement the shoreline source and `grid.py`
    - `sources/census.py`: fetch the cb_2023 500k states zip (STATEFP 25, 33, 44) through the fetch layer; with no cache and a failed fetch, raise `FatalSourceError`
    - `grid.build(cfg, shoreline)`: EPSG:26919, fixed origin (230000, 4540000), deterministic `cell_id`, outer-extent buffer, RI-meridian / NH-parallel clip, centroid-in-water filter, lat/lon at 6 dp, reprojected to 4326
    - _Requirements: 1.1, 1.4, 1.5, 1.6, 1.7_
  - [ ]* 4.2 [A] Property test: grid alignment and determinism
    - **Property 1: Grid alignment and determinism**
    - **Validates: Requirements 1.1, 1.6, 1.7**
    - `tests/test_grid_props.py` using small synthetic shorelines
  - [ ]* 4.3 [A] Property test: land mask exactness
    - **Property 3: Land mask exactness**
    - **Validates: Requirements 1.4**
    - `tests/test_land_mask_props.py`
  - [ ]* 4.4 [A] Unit test: shoreline failure without cache stops the run
    - `tests/test_grid_failure.py`: `FatalSourceError` names the source and reason
    - _Requirements: 1.5_

- [ ] 5. Checkpoint: foundations
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. [A] Resource score (tidal stream, rise/fall, wave context)
  - [x] 6.1 [A] Implement velocity sources
    - `sources/necofs.py` (MassBay, then GOM3 fallback; `ua`,`va`,`h` at the nearest element/node), `sources/doppio.py`, `sources/mom6.py` (`ubar`,`vbar` at the nearest rho point), `sources/coops_currents.py` (nearest current station within 5 km)
    - Each returns `(source, t_start, t_end, speeds_ms[], model_run_date)` or a `DataGap`; the resolver follows `velocity_source_priority`
    - _Requirements: 4.1, 4.2, 3.3_
  - [x] 6.2 [A] Implement tidal-stream scoring in `score/resource.py`
    - Power density `0.5·ρ·v_peak³`; power-density and depth thresholds; edge-adjacent flood fill for qualifying area; record `stream_threshold_failed`
    - Sub-score `ramp(P, P_min, P_bench, baseline_credit, 100)`; CF from the clipped per-step power; energy = rated·CF·8760; "below EPRI thresholds" when the sub-score is 0
    - Emit `velocity_peak`, `velocity_source_period`, `power_density`, `depth`, `qualifying_area`, `cf_stream`, `energy_stream_kwh` factors with tags and citations; velocity gap → all null
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_
  - [ ]* 6.3 [A] Property test: velocity source priority
    - **Property 10: Velocity source priority**
    - **Validates: Requirements 4.1, 4.2**
    - `tests/test_velocity_priority_props.py`
  - [ ]* 6.4 [A] Property test: cubic power density
    - **Property 11: Cubic power density**
    - **Validates: Requirements 4.3**
    - `tests/test_power_density_props.py`
  - [ ]* 6.5 [A] Property test: tidal-stream thresholds and mapping
    - **Property 12: Tidal-stream sub-score thresholds and mapping**
    - **Validates: Requirements 4.4, 4.5, 4.7**
    - `tests/test_stream_score_props.py`
  - [x] 6.6 [A] Implement rise/fall scoring
    - `sources/coops.py`: harcon station list, `harcon.json` (M2 supporting record), `datums.json` MN range; nearest station by haversine; beyond `max_station_distance_km` or empty harcon → gap
    - In `score/resource.py`: `E_cycle = 0.5·ρ·g·A·R²`, sub-score `ramp(R², R_min², R_max², 0, 100)`, CF and energy; emit `tidal_range`, `coops_station`, `energy_cycle_j`, `cf_rise_fall`, `energy_rise_fall_kwh`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.9_
  - [ ]* 6.7 [A] Property test: CF bounds and energy identity
    - **Property 13: Capacity factor bounds and energy identity**
    - **Validates: Requirements 4.6, 5.9**
    - `tests/test_cf_props.py`
  - [ ]* 6.8 [A] Property test: nearest station selection and range scoring
    - **Property 14: Nearest station selection and range scoring**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
    - `tests/test_rise_fall_props.py`
  - [x] 6.9 [A] Implement wave context and the Resource combiner
    - `sources/ndbc.py`: active stations, nearest within `max_buoy_distance_km`, realtime2 parse (drop `MM`), mean WVHT/DPD, record start/end; shared with Feasibility
    - `wave_power`, `wave_hs_mean` factors with `note="not blended"`, excluded from Resource
    - Resource = max of the non-null paths (ties → tidal_stream), `resource_path`; both null → null + Resource gap
    - _Requirements: 5.6, 5.7, 5.8_
  - [ ]* 6.10 [A] Property test: Resource is the max available path
    - **Property 15: Resource is the max available path**
    - **Validates: Requirements 5.7, 5.8**
    - `tests/test_resource_combine_props.py`

- [x] 7. [C] Grid Proximity, Conflict + Hard_Exclusions, Jurisdiction
  - [x] 7.1 [C] Implement Grid Proximity
    - `sources/hifld.py`: transmission lines (`returnGeometry=true`) and substations, envelope = grid bbox + 50 km, paginated on `exceededTransferLimit`; primary/mirror from `endpoints.py`
    - `sources/massgis.py`: utility service territory by town
    - `score/grid_proximity.py`: STRtree nearest distances in UTM km (0.01), utility containing or nearest with label "distribution interconnection below 5 MW" (PROXY), ramp sub-scores, weighted component; a failed or empty layer → per-cell gap and the remaining sub-score only
    - Return `FactorResult` objects for Track A assembly
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_
  - [ ]* 7.2 [C] Property test: nearest distance correctness
    - **Property 18: Nearest distance correctness**
    - **Validates: Requirements 6.2**
    - `tests/test_distance_props.py`
  - [ ]* 7.3 [C] Unit tests for Grid Proximity
    - Utility label and PROXY tag; substation fetch empty → substation gap and component from transmission alone; both empty → null
    - `tests/test_grid_proximity.py`
    - _Requirements: 6.3, 6.6_
  - [x] 7.4 [C] Implement Hard_Exclusions
    - `sources/marinecadastre.py`: shipping fairways, submarine cable areas, MPA Inventory; extend `sources/massgis.py`: NHESP priority habitat, residential land use, public beaches
    - `score/conflict.py` `exclusions(cells, layers)`: polygon `intersects`, record every triggering layer + citation; failed or empty layer → gap, no exclusion from it, `exclusion_check_incomplete` on every cell
    - _Requirements: 7.1, 7.3_
  - [ ]* 7.5 [C] Property test: hard exclusion by intersection
    - **Property 19: Hard exclusion by intersection**
    - **Validates: Requirements 7.1, 7.5**
    - `tests/test_exclusion_props.py`
  - [ ]* 7.6 [C] Unit test: missing exclusion layer → incomplete flag
    - `tests/test_exclusion_incomplete.py`
    - _Requirements: 7.3_
  - [x] 7.7 [C] Implement graded Conflict sub-scores
    - `sources/neodp.py`: AIS vessel transit density and diadromous fish stations; `sources/emolt.py`: ERDDAP observation points
    - In `score/conflict.py`: `vessel_traffic` and `fishing_activity` (percentile-normalized, PROXY), `diadromous_fish_km` ramp to `fish_zero_km` (PROXY, never an exclusion), `whale_critical_habitat` (QUANTIFIED DATA + EXTERNAL ADDITION, graded not excluded); component via `weighted_mean_available`; all null → null + gap
    - _Requirements: 7.4, 7.5, 7.6_
  - [x] 7.8 [C] Implement `jurisdiction.py`
    - Fetch US State Submerged Lands (MA), falling back to the BOEM SLA boundary; `covers(centroid)` → state/MA CZM, else federal/BOEM; `straddles_boundary` when the cell polygon crosses the boundary; `disclaimer: "Not legal advice"`
    - Failed or empty fetch → jurisdiction null + gap on every cell; export the boundary line for `boundary_3nm.geojson`
    - The function signatures keep jurisdiction out of every score function
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - [ ]* 7.9 [C] Property test: jurisdiction by centroid
    - **Property 20: Jurisdiction by centroid**
    - **Validates: Requirements 8.1, 8.3**
    - `tests/test_jurisdiction_props.py`

- [x] 8. [A] Feasibility (core sub-scores: depth, wave, port)
  - [x] 8.1 [A] Implement `score/feasibility.py` core
    - `sources/bathy.py`: depth fallback from the NCEI CRM ERDDAP (NECOFS `h` is primary); `sources/ports.py`: principal ports
    - Depth band score per device path (seabed 15-35, buoy 5-15, ramp to 0 at 10 m outside); wave `ramp(H,1,3,100,0)` reusing the NDBC fetch; port `ramp(d,10,50,100,0)`
    - Equal-weight available mean, `feasibility_omitted` list, all gaps → null + gap
    - _Requirements: 9.1, 9.3, 9.4, 9.5, 9.6_
  - [ ]* 8.2 [A] Unit tests for Feasibility
    - Band edges, device path selection, omitted list, all-gap → null
    - `tests/test_feasibility.py`
    - _Requirements: 9.1, 9.5, 9.6_

- [ ] 9. [A] Validation report, output writer, and run wiring
  - [-] 9.1 [A] Implement `validation.py`
    - Hard-coded 5 sites; nearest centroid over ALL cells (incl. excluded); beyond 3 mi → "not evaluated"; percentile on the site's path sub-score; pass at ≥ 75 else "validation failure"; gap → "not evaluated" + reason
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6_
  - [ ]* 9.2 [A] Property test: validation site matching and status
    - **Property 28: Validation site matching and status**
    - **Validates: Requirements 15.2, 15.4, 15.5, 15.6**
    - `tests/test_validation_props.py`
  - [ ] 9.3 [A] Implement `output.py` and the Python `score/overall.py`
    - `overall_default` with the default weights (excluded → 0), using the same formula as the frontend
    - Write `metadata.json` (run timestamp UTC, cell size, every config key with units, deduplicated citations, gap summary, factors list, tier3 notice, disclaimer, known limitations incl. NECOFS window), `cells.geojson`, `timeseries.json`, `boundary_3nm.geojson`, `validation.json`
    - Atomic write to `output/.tmp/`, then rename; scores rounded to 1 dp at write time
    - _Requirements: 1.6, 2.8, 3.4, 3.6, 7.2, 11.1, 15.3_
  - [ ] 9.4 [A] Wire `run.py` end to end
    - Order: config.validate (exit 2) → shoreline + grid (exit 3) → source fetches → assemble Track A + Track C `FactorResult`s per cell → provenance.validate (list every cell/factor, exit 4, no write) → validation → output.write → print gap summary by source
    - CLI: `python -m siting.run --config ../config/device_config.yaml --out ../output`
    - _Requirements: 1.3, 1.5, 2.6, 2.7, 2.8, 3.8_
  - [ ]* 9.5 [A] Property test: gaps are null, isolated, and counted
    - **Property 6: Gaps are null, isolated, and counted**
    - **Validates: Requirements 2.6, 2.7, 2.8**
    - `tests/test_gap_isolation_props.py`: run the pipeline on synthetic fixtures with a fake transport and random failing source subsets
  - [ ]* 9.6 [A] Property test: non-scoring fields do not affect scores
    - **Property 9: Non-scoring fields do not affect scores**
    - **Validates: Requirements 3.6, 3.7, 5.6, 5.7, 8.2**
    - `tests/test_nonscoring_props.py`
  - [ ]* 9.7 [A] Integration and unit tests for output
    - Metadata contents (3.4); a provenance failure leaves the existing output byte-identical (3.8); grep test that nothing in `pipeline/` references the kit zip (2.1); synthetic run output passes the schema test
    - `tests/test_output.py`, `tests/test_no_kit.py`
    - _Requirements: 2.1, 3.4, 3.8, 16.4_

- [ ] 10. Checkpoint: backend core
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. [B] Frontend pure model
  - [ ] 11.1 [B] Implement `src/data/load.ts`
    - Fetch all Static_Output files from `VITE_DATA_DIR`; type guards; on a malformed file, return an error naming the file + field (no partial render); treat absent components as missing
    - _Requirements: 11.4, 14.5_
  - [ ] 11.2 [B] Implement `model/ramps.ts` and `model/overall.ts`
    - `ramp`, `weightedMeanAvailable`; `overall(c, w)` → `{score, partial, missing}` per the design formula; excluded → 0; `DEFAULT_WEIGHTS`
    - _Requirements: 7.2, 11.1, 11.4, 11.5, 11.6_
  - [ ]* 11.3 [B] Property test: Overall score composition
    - **Property 22: Overall score composition**
    - **Validates: Requirements 7.2, 11.1, 11.4, 11.5, 11.6**
    - `tests/overall.prop.test.ts` (fast-check)
  - [ ]* 11.4 [B] Property test: linear ramp correctness (TS side)
    - **Property 16: Linear ramp correctness**
    - **Validates: Requirements 6.4, 7.5, 9.1, 9.2, 9.3, 9.4**
    - `tests/ramps.prop.test.ts`
  - [ ] 11.5 [B] Implement `model/sort.ts` and `model/bins.ts`
    - `rankRows` (nulls last both directions, tie → cell_id asc), `topN`; `binIndex` with 5 bins + "gap", `radiusPx = 4 + 8·s/100`, null → 4; viridis 5-step + gap `#9e9e9e`
    - _Requirements: 13.2, 13.4, 13.5, 14.2, 14.3, 14.4, 14.6_
  - [ ]* 11.6 [B] Property test: sorting and top-N
    - **Property 24: Sorting and top-N**
    - **Validates: Requirements 14.2, 14.3, 14.4, 14.6**
    - `tests/sort.prop.test.ts`
  - [ ]* 11.7 [B] Property test: marker bins and radius
    - **Property 25: Marker bins and radius**
    - **Validates: Requirements 13.2, 13.4, 13.5**
    - `tests/bins.prop.test.ts`
  - [ ] 11.8 [B] Implement `model/resource.ts` and `model/config.ts`
    - TS ports of power density, the stream sub-score from stored power density / threshold flags, CF from `timeseries.json`, the rise/fall sub-score/CF/energy, and the Resource combiner
    - `validateEdit(key, raw)`: numeric, > 0, Cp and η ≤ 1; error message names the range
    - _Requirements: 12.3, 12.4_
  - [ ]* 11.9 [B] Property test: device config edit validation and reset round trip
    - **Property 26: Device config edit validation and reset round trip**
    - **Validates: Requirements 12.4, 12.5**
    - `tests/config.prop.test.ts`
  - [ ]* 11.10 [A+B] Property test: frontend recompute matches pipeline
    - **Property 27: Frontend recompute matches pipeline**
    - **Validates: Requirements 12.3**
    - Hypothesis writes `pipeline/tests/fixtures/cross_lang.json` (`tests/test_cross_lang_fixture.py`); `frontend/tests/crossLang.test.ts` asserts equality within 1e-6 for CF, energy, both sub-scores, and Resource

- [ ] 12. [B] Frontend components (built against `output/sample/`)
  - [ ] 12.1 [B] Implement app state, gap rendering, and the provenance legend
    - `App.tsx` with one `useReducer` `{data, weights, deviceCfg, activeLayer, selectedCellId, sort, showBoundary}` and `useMemo`-derived rows; error banner on load failure
    - `FactorValue.tsx`: null renders `Data gap — {source}: {reason}`, never 0
    - `ProvenanceLegend.tsx`: one row per distinct factor name with tags and citation
    - _Requirements: 2.9, 3.5_
  - [ ]* 12.2 [B] Property test: gap display never shows zero
    - **Property 7: Gap display never shows zero**
    - **Validates: Requirements 2.9, 8.4, 13.5**
    - `tests/factorValue.prop.test.ts`
  - [ ] 12.3 [B] Implement `MapView.tsx`
    - react-leaflet canvas `CircleMarker`s for non-excluded cells only, `fitBounds` to their centroids, bin colors/radius by the active layer, a radio group for one layer at a time, a legend with 5 bins + "Data gap"
    - Boundary toggle (default off) rendering `boundary_3nm.geojson`; hover tooltip with cell id, Overall, Jurisdiction + "Not legal advice"; click selects the cell
    - _Requirements: 8.5, 8.6, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_
  - [ ] 12.4 [B] Implement `RankedTable.tsx`
    - Non-excluded rows only; rank, id, lat/lon 4 dp, Overall, 5 components at 1 dp, provenance tags, "partial score" flag + missing names, jurisdiction + disclaimer
    - Header `<button>`s with `aria-sort`; first click desc, then toggles; nulls last
    - _Requirements: 8.5, 11.6, 14.1, 14.2, 14.3_
  - [ ]* 12.5 [B] Property test: ranked visibility
    - **Property 23: Ranked visibility**
    - **Validates: Requirements 7.2, 13.1, 14.1**
    - `tests/visibility.prop.test.ts` over the row-derivation and marker-derivation selectors
  - [ ] 12.6 [B] Implement `CellPanel.tsx` and `TopFive.tsx`
    - CellPanel: every raw input via `FactorValue`, all 5 components, both resource sub-scores + energies, tags + citations, gaps, `exclusion_check_incomplete`, `feasibility_omitted`, jurisdiction + straddle flag + disclaimer; stays open until closed or another marker is clicked
    - TopFive: `topN(rows, 5)` panels refreshed on recompute; "Profitability not computed" when absent; fewer than 5 cells → one panel each
    - _Requirements: 5.5, 7.3, 8.5, 13.8, 14.4, 14.5, 14.6_
  - [ ] 12.7 [B] Implement `WeightSliders.tsx` and `DeviceConfigPanel.tsx`, and wire them in `App.tsx`
    - 5 labeled sliders 0-1 step 0.05 at the defaults (sums not forced to 1)
    - Every config value with its unit; Cell_Size read-only with "re-run Pipeline to change"; inline errors from `validateEdit`; reset restores the Static_Output values; edits recompute derived Resource values and Overall
    - _Requirements: 11.2, 11.3, 12.2, 12.3, 12.4, 12.5_
  - [ ]* 12.8 [B] Component unit tests
    - Boundary toggle default off (8.6); disclaimer in panel, row, and tooltip (8.5); "Profitability not computed" (14.5); tooltip/panel contents (13.7, 13.8); legend has 5 bins + "Data gap" (13.3); jurisdiction gap shows "Data gap" (8.4)
    - `tests/components.test.tsx` (vitest + @testing-library/react)
    - _Requirements: 8.4, 8.5, 8.6, 13.3, 13.7, 13.8, 14.5_
  - [ ]* 12.9 [B] Recompute benchmark
    - 5,000 synthetic cells; a weight change triggers recompute + sort in < 1000 ms
    - `tests/perf.bench.test.ts`
    - _Requirements: 11.3_

- [ ] 13. Core integration
  - [ ] 13.1 [A+B] Point the frontend at real pipeline output
    - `frontend/vite.config.ts`: serve the repo `output/` dir; `npm run dev:sample` and `npm run dev` scripts in `frontend/package.json` that switch `VITE_DATA_DIR`
    - Fix any schema mismatches the schema conformance test flags between real and sample output
    - _Requirements: 16.4, 16.5_
  - [ ] 13.2 [D] Draft the README
    - Setup for Pipeline and Frontend, the single pipeline command, the single frontend command, data source list with a citation per source (from `endpoints.py`), "Not legal advice", the right whale graded-not-excluded design note, known limitations, and "How Kiro was used" naming specs, steering files, and hooks, each with a concrete example from this project
    - _Requirements: 16.2, 16.3_

- [ ] 14. Checkpoint: WORKING CORE
  - Ensure all tests pass, ask the user if questions arise.

### STRETCH GOALS (optional; cut freely, nothing in CORE depends on these)

- [ ] 15. STRETCH [A] Feasibility slope refinement
  - [ ]* 15.1 [A] STRETCH: add the slope sub-score to `score/feasibility.py`
    - Max gradient in degrees over the CRM 3×3 neighborhood from `sources/bathy.py`; `ramp(s,5,15,100,0)`; included in the equal-weight mean when available
    - _Requirements: 9.2, 9.5_
  - [ ]* 15.2 [A] STRETCH: unit tests for the slope sub-score
    - `tests/test_slope.py`: flat → 100, ≥ 15° → 0, missing CRM → omitted
    - _Requirements: 9.2, 9.5_

- [ ] 16. STRETCH [A+B] Profitability
  - [ ]* 16.1 [A] STRETCH: implement `sources/eia.py` and `score/profitability.py`, and wire them into `run.py`
    - EIA v2 retail-sales MA/ALL, latest 12 months averaged, ¢→USD; `EIA_API_KEY` env var, missing → gap "fetch failure: no API key"
    - Gross = max energy × price with `device_path`; `m_transport`, `m_transmission`, `m_maint` (PROXY, ≥ 1); net; min-max over non-excluded non-null (all equal → 50); price gap → null for all cells
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_
  - [ ]* 16.2 [A] STRETCH: property test for Profitability normalization and multipliers
    - **Property 21: Profitability normalization and multipliers**
    - **Validates: Requirements 10.1, 10.2, 10.4**
    - `tests/test_profitability_props.py`
  - [ ]* 16.3 [B] STRETCH: frontend Profitability breakdown and recompute
    - `model/profitability.ts` (TS port of multipliers, net, min-max); in `TopFive.tsx` and CellPanel show price USD/kWh, source + period, gross, each multiplier, net, and "Illustrative, not a financial projection"
    - _Requirements: 10.6, 12.3, 14.4_
  - [ ]* 16.4 [A+B] STRETCH: extend the cross-language fixture to Profitability
    - **Property 27: Frontend recompute matches pipeline** (Profitability part)
    - **Validates: Requirements 12.3**

- [ ] 17. STRETCH [C] CFRF fishbot and HAB Hub conflict layers
  - [ ]* 17.1 [C] STRETCH: add `sources/cfrf.py` and `sources/hab.py`, with `cfrf_fishbot` and `hab_events` sub-scores in `score/conflict.py`
    - Tagged EXTERNAL ADDITION with citations; any fetch failure → gap, and the sub-score drops out of the weighted mean
    - _Requirements: 7.7_
  - [ ]* 17.2 [C] STRETCH: unit tests for the new sub-scores and their gap paths
    - `tests/test_conflict_stretch.py`
    - _Requirements: 7.7_

- [ ] 18. STRETCH [D+B] Top-5 public sentiment notes (Tier 3 narrative)
  - [ ]* 18.1 [D] STRETCH: author `sentiment.json` and add a pass-through in `output.py`
    - `{cell_id: {note, citations[]}}` for the current top-5 cells, citing public comment records, a nearby offshore project's reception, or local news; text only, never read by any score function
    - _Requirements: 3.6, 3.7, 14.7_
  - [ ]* 18.2 [B] STRETCH: render sentiment notes in `TopFive.tsx`
    - "Tier 3 narrative" label, citations, no numeric value; "No public sentiment source found" when missing
    - _Requirements: 14.7, 14.8_

### RELEASE

- [ ] 19. Public repo readiness
  - [ ] 19.1 [D] Finalize repo hygiene and the README
    - Check that `.gitignore` covers the cache, `.env`, keys, and build artifacts; add `.env.example` with an `EIA_API_KEY=` placeholder
    - Finalize the README setup commands so they match `pyproject.toml` / `package.json` scripts; confirm `output/sample/` is committed and referenced in the README
    - _Requirements: 16.1, 16.2, 16.5, 16.6_
  - [ ]* 19.2 [D] Write a repo hygiene test
    - `pipeline/tests/test_repo_hygiene.py`: `git ls-files` contains nothing under `data/cache/` (except `.gitkeep`), no `.env` / `*.key`, and no Python under `frontend/` or TS under `pipeline/`
    - _Requirements: 16.1, 16.6_

- [ ] 20. Final checkpoint
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- `*` = optional. Core-path property tests are optional so the team can ship. If time is tight, run 2.7 (provenance), 3.3 (retry), 4.2 (grid), 11.3 (overall), and 11.6 (sorting) first; they guard the core critical path.
- Every task in the STRETCH GOALS section is optional, and nothing in CORE depends on them. The frontend already treats absent Profitability and sentiment as missing (11.1, 12.6).
- Track hand-offs: 1.3 → 1.5 → all of Track B; 1.4 resolves endpoints for Tracks A/C; 7.x return `FactorResult`s that 9.4 assembles; 11.10 couples Tracks A and B through a shared fixture.
- The demo video (Track D) is not a coding task, so it is not listed. Record it after checkpoint 14 or 20.
- `sources/coops_currents.py` (6.1) is split from `sources/coops.py` (6.6) so the two can be built in parallel.
- Property tests: hypothesis `max_examples=100`, fast-check `numRuns: 100`. Tag each test `Feature: ma-tidal-buoy-siting-model, Property {n}: {title}`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["1.4", "1.5", "2.1", "2.3", "2.6", "11.1", "11.2", "11.5"] },
    { "id": 2, "tasks": ["1.6", "2.2", "2.4", "2.5", "2.7", "3.1", "11.3", "11.4", "11.6", "11.7", "11.8", "12.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "4.1", "7.1", "11.9", "12.2", "12.3", "12.4", "12.6"] },
    { "id": 4, "tasks": ["4.2", "4.3", "4.4", "6.1", "7.2", "7.3", "7.4", "12.5", "12.7"] },
    { "id": 5, "tasks": ["6.2", "7.5", "7.6", "7.7", "12.8", "12.9"] },
    { "id": 6, "tasks": ["6.3", "6.4", "6.5", "6.6", "7.8"] },
    { "id": 7, "tasks": ["6.7", "6.8", "6.9", "7.9"] },
    { "id": 8, "tasks": ["6.10", "8.1", "9.1"] },
    { "id": 9, "tasks": ["8.2", "9.2", "9.3"] },
    { "id": 10, "tasks": ["9.4", "11.10", "13.2"] },
    { "id": 11, "tasks": ["9.5", "9.6", "9.7", "13.1", "15.1", "16.1", "17.1", "18.1"] },
    { "id": 12, "tasks": ["15.2", "16.2", "16.3", "17.2", "19.1"] },
    { "id": 13, "tasks": ["16.4", "18.2", "19.2"] }
  ]
}
```
