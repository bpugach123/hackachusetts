# Implementation Plan: MA tidal siting pipeline → Perseus

Two repos. Quote the Perseus path everywhere (it contains a space).

- PIPE = `/Users/michaellaschaszberger/Desktop/hackachusetts` (spec: `.kiro/specs/ma-tidal-buoy-siting-model/`, package `pipeline/siting`, venv `pipeline/.venv`)
- PERSEUS = `"/Users/michaellaschaszberger/Downloads/perseus-night-watch 2"` (not a git repo; React 19 + esbuild; one class component `src/App.jsx`)

Commands (verified to exist during planning):
- Pipeline tests: `cd /Users/michaellaschaszberger/Desktop/hackachusetts/pipeline && .venv/bin/python -m pytest -q` (67 passing at plan time)
- Pipeline lint: `cd /Users/michaellaschaszberger/Desktop/hackachusetts/pipeline && .venv/bin/ruff check .`
- Pipeline run (after task 9.4): `cd /Users/michaellaschaszberger/Desktop/hackachusetts/pipeline && .venv/bin/python -m siting.run --config ../config/device_config.yaml --out ../output`
- Perseus: `cd "/Users/michaellaschaszberger/Downloads/perseus-night-watch 2" && npm install` (once; node_modules absent), `npm run build`, and the new `npm test` (= `node --test test/`, Node v24 built-in runner)

Network to NOAA CO-OPS, NECOFS THREDDS, NDBC, MarineCadastre and Census returned HTTP 200 at plan time. Other "resolve at build start" endpoints are unverified.

## State found during exploration

- tasks.md marks only 1.1-1.3 done, but code already exists for 2.1 (`siting/config.py` + `config/device_config.yaml`), 2.3 (`score/ramps.py`), 2.6 (`provenance.py`), 3.1 (`fetch/client.py`, `cache.py`, `gaps.py`) and part of 1.4 (`sources/endpoints.py`, no `smoke.py`). Tests exist for config, ramps, provenance; none for the fetch layer. Treat these tasks as "verify against the task text, fill gaps, then check off". Do not rewrite working code.
- `run.py` is a stub returning 1. `output/` is empty (no sample, no real run).
- `hackachusetts/frontend/` is a Vite/TS scaffold (only `dataDir.ts`). It is NOT the integration target.
- Perseus point model (`renderVals()` pin branch, App.jsx ~L318-354) uses `intensity()` blobs + `STATIONS` blend, `econ()` (5 m rotor, $0.12/kWh), hand-drawn policy polygons, coastline utility tags 0-4, and a 50/30/20 score. `buildField()` (L155) uses `CORRIDORS` directions and `0.12 + intensity()` magnitude. Zoomed dots (L430-460) also use `intensity()`. `pt.method` is computed but never rendered.
- Commit policy (env.md): do NOT commit in either repo. Perseus is not a git repo; do not `git init` it.

## Design decisions

- D1. Spec frontend tasks (11.x, 12.x, 13.1, 16.3, 18.2) are implemented inside Perseus, not in `hackachusetts/frontend/` (left untouched, not deleted). Reason: the user named Perseus as the frontend; building a second UI would split the demo. tasks.md entries for these get checked with a note `(implemented in Perseus: <file>)`.
- D2. Perseus stays plain JS (ESM + JSDoc), no TypeScript, no new dependencies (runtime or dev). Pure model ports go to `src/model/*.js` and projection to `src/geo/proj.js`, following the split order in Perseus `structure.md`. `App.jsx` stays the one class component and imports them; esbuild bundles them. Tests use `node --test` with seeded random-loop property tests in place of fast-check (`numRuns` 100 equivalent).
- D3. Projection lives once, in `src/geo/proj.js`: `lonLatToMap(lon, lat) → {x: (lon + 72.30) * 445.88, y: (42.92 - lat) * 600}` and its inverse. Both the import script and the app import it. The pipeline keeps canonical lon/lat output (schema unchanged).
- D4. Shipping: a Node script `scripts/import-siting.mjs` (no deps) reads a pipeline Static_Output directory, validates required fields, and writes `public/data/siting.json` (compact, map units), `public/data/timeseries.json` (copied as is; lazy-loaded), `public/data/siting-heat.svg` and `public/data/siting-policy.svg` (generated from cells in map units, 1605.2 × 1044 viewBox), and copies the cross-language fixture into `test/fixtures/`. `build.mjs` already copies `public/` to `dist/`, so it needs no change. It refuses an output dir whose metadata says `"sample": true` unless `--allow-sample` is passed, and then the app labels everything SAMPLE DATA.
- D5. Startup: `componentDidMount` fetches `./data/siting.json`. On success it stores it on `this.siting` (not in React state, to keep re-render cost flat), builds the pin lookup index, calls `buildField()` again, and sets `state.dataStatus = 'pipeline' | 'sample'`. On failure (missing, HTTP error, bad shape) `dataStatus = 'simulated'`; the app behaves exactly as today and shows a `SIMULATED DATA · pipeline output not loaded` badge in the brand area. Constructor accepts optional `props.siting` (pre-loaded data) so tests can inject a fixture without fetch.
- D6. When pipeline data is loaded, pins use pipeline values only. A null factor shows `Data gap — {source}: {reason}`; it never falls back to a schematic number and never shows 0. Pins farther than the cell half-diagonal from any cell centroid show `Outside model grid` as a gap. The schematic model is used only in `simulated` mode.
- D7. Perseus score = spec Overall_Score via the ported `overall(components, weights)` (default weights .30/.25/.15/.15/.15, Conflict subtracted, excluded → 0, `partial` flag). The 50/30/20 formula stays only in simulated mode. Update the "Point model" section of Perseus `.kiro/steering/tech.md` to say so. This is a deliberate behavior change: it is the point of "plug in the model".
- D8. Pin lookup: at load, bucket cells into a map-unit grid (16-unit buckets); the pin result is cached on `this.pinKey` (pin x|y + weights + device config version) so the ~80 ms render does no searching. Particle field and zoomed dots read the same index once per build / per view key.
- D9. `buildField()`: flow direction stays from `CORRIDORS` (schematic, labeled). Magnitude per 8-unit field cell = nearest pipeline cell `velocity_peak / max_velocity_peak` mapped into the existing 0.12-1.15 visual range. Field cells with no pipeline velocity get `mg = 0` (no streaks). No invented flow where data is missing.
- D10. TopFive / RankedTable are not built as panels (Perseus principles: no fixed featured sites, lean UI). Their logic (`rankRows`, `topN`) is ported and tested, and surfaced as one line in the pin report: `Rank N of M modeled cells`. Top-5 cells drive the Tier 3 sentiment flags. WeightSliders + DeviceConfigPanel become one compact "Model" settings popover floating over land (also covers Perseus spec task 8). Boundary toggle becomes a "3 nm line" layer toggle, default OFF.
- D11. Perseus hand-drawn policy rules (Ocean Plan prohibited area `G.atl`, TSS, federal channel, Stellwagen, ferry, whale season by date) stay as notes prefixed `Approximate zone:`, since the pipeline does not cover them. Pipeline hard exclusions set status Prohibited/Excluded with layer names. Jurisdiction comes from the pipeline (MarineCadastre submerged lands), replacing the `dC <= 30` heuristic, when available.
- D12. Revenue uses the pipeline EIA price factor when stretch 16 produced it, else `$0.12/kWh (assumed)`.
- D13. No commits in either repo (env.md decision; git-safety: user did not ask).

## (a) Spec task order, CORE vs STRETCH

Work strictly in tasks.md order; check each box as it is finished and its tests pass. Optional `*` property/unit tests inside CORE sections are run, not cut (they are cheap and guard the core). Checkpoints 5, 10, 14, 20: run the full suite, then check off; do not stop to ask unless blocked.

1. CORE §1 remainder: 1.4 smoke + endpoint resolution, 1.5 `output/sample/` (12 cells; metadata `"sample": true`), 1.6 schema test.
2. CORE §2: verify 2.1/2.3/2.6, add 2.2, 2.4, 2.5, 2.7.
3. CORE §3: verify 3.1, add fetch unit tests, 3.2, 3.3.
4. CORE §4: 4.1-4.4 grid. Checkpoint 5.
5. CORE §6: 6.1-6.10 resource. CORE §8: 8.1-8.2 feasibility (depends on 6.9 NDBC + NECOFS `h`).
6. CORE §7: 7.1-7.9 grid proximity, exclusions, conflict, jurisdiction.
7. CORE §9: 9.1-9.7, then a real run into `output/`. Checkpoint 10.
8. CORE §11 (in Perseus, D1/D2): 11.1 → `src/model/load.js`, 11.2 → `ramps.js`, `overall.js`, 11.5 → `sort.js`, `bins.js`, 11.8 → `resource.js`, `config.js`, 11.3/11.4/11.6/11.7/11.9 → `test/*.test.mjs`, 11.10 → `pipeline/tests/test_cross_lang_fixture.py` + `test/crossLang.test.mjs`.
9. CORE §12-13 (in Perseus): 12.1 provenance + `fmtFactor` gap formatter, 12.2 test, 12.3 heat/policy SVG layers + 3 nm toggle, 12.4/12.6 pin report (D10), 12.5 test, 12.7 Model popover, 12.8 render tests, 12.9 benchmark (5,000 cells < 1000 ms in node). 13.1 → import script + `npm run data:import`. 13.2 README in PIPE root. Checkpoint 14.
10. STRETCH, in cut order (cut first → last): 17 (CFRF/HAB) and 15 (slope) are cut first; 16 (profitability: code + gap path even without `EIA_API_KEY`; real price only if the key is set in the environment) next; 18 (sentiment, Tier 3) is kept if at all possible because it is a steering obligation. Every cut task is left unchecked and listed in tasks.md Notes and the final report.
11. RELEASE §19-20: 19.1, 19.2 (hygiene test must cover untracked-but-not-ignored files via `git ls-files --cached --others --exclude-standard`, since nothing is committed), final checkpoint.

## (b) Pipeline outputs → Perseus point model

Conversion: import script calls `lonLatToMap(centroid_lon, centroid_lat)` per cell; boundary_3nm line coords and validation sites likewise. Pin → lon/lat for display uses `mapToLonLat` (same as the existing `coord` string). Cell half-size in map units = `cell_size_mi * 1.609344 / 0.185 / 2`.

| Perseus piece | Today | With pipeline data (per pin, from matched cell) |
|---|---|---|
| `onLand()` | coastline PIP | unchanged (matches the drawn basemap); a water pin with no cell → `Outside model grid` gap |
| Peak current | `intensity()` + STATIONS blend | `factors.velocity_peak` (m/s → kn ÷ 0.5144) with label `modeled current · {model} run {date}` or `predicted current (harmonic) · NOAA CO-OPS {station}` |
| `econ()` power density | `0.5·1025·v³` (kW/m²) | `factors.power_density` (W/m², already cubic). Yield/yr = `energy_stream_kwh` or `energy_rise_fall_kwh` per `resource_path`, recomputed by `model/resource.js` when device config is edited |
| Output now / earned since today | M2 cosine × econ | keep M2 timing (simulated) but scale with pipeline device config and `velocity_peak`: `P(t) = min(0.5·ρ·A·Cp·(v_peak·A(t)·|cos|)³, rated)` — cubic, labeled `tide timing simulated (M2)` |
| Tidal range in report | `rangeAt()` RST IDW | `factors.tidal_range` (CO-OPS datums MN) + `coops_station`; range layer PNG and range dots stay RST, labeled approximate |
| Wave context | none | `wave_hs_mean`, `wave_power` with `not blended` |
| Policy status/notes | hand-drawn rules | pipeline `hard_exclusion` + `exclusion_layers`, `exclusion_check_incomplete`, `whale_critical_habitat`, `vessel_traffic`, `fishing_activity`, `diadromous_fish_km`, Conflict score; plus D11 approximate-zone notes |
| Jurisdiction | `dC <= 30` heuristic | `jurisdiction`, `agency`, `straddles_boundary`, `disclaimer` |
| Grid | coastline tag 0-4 + nearestCoast cable | `dist_transmission_km`, `dist_substation_km`, `utility` (PROXY label) and Grid score; landing/cable from `nearestCoast` stays, labeled `approx. (schematic coastline)`; contacts EVER/NGRID chosen by matching the pipeline utility name (Eversource/NSTAR → EVER; National Grid/Massachusetts Electric/Nantucket Electric → NGRID; municipal → MLP, no contact); utility gap → `Data gap`, no contact card |
| Score | 50/30/20 | `overall()` (D7) + `partial`/`missing`, Rank N of M |
| `buildField()` | `0.12 + intensity()` | D9 |
| Zoomed dots | `intensity()` | nearest-cell `velocity_peak` (no dot where null) |
| Heat / policy layers | static SVG | `public/data/siting-heat.svg` (Overall, viridis 5 bins + gray gap), `siting-policy.svg` (excluded cells + 3 nm line); old SVGs used in simulated mode |
| Validation | none | sites from `validation.json` listed in the Model popover with status |

## (c) Static JSON shipping and regenerate command

`siting.json` shape: `{schema_version, sample, run_timestamp_utc, cell_size_mi, cell_half_units, device_config, citations, gap_summary, tier3_notice, disclaimer, known_limitations, factors_legend, cells: [{id, x, y, lat, lon, ex, exl, inc, jur, agency, straddle, path, scores, overall_default, factors, omitted, thr, ereason, gaps}], boundary: [[x,y]...][], validation: [...], sentiment: {cell_id: {note, citations}} | null, max_velocity}`. Factor objects keep `value, unit, tags, citation_id, label, note, gap`. The loader type-guards it and returns `{ok:false, file, field}` on a bad shape (no partial render).

Regenerate (documented in both READMEs):

```
cd /Users/michaellaschaszberger/Desktop/hackachusetts/pipeline
.venv/bin/python -m siting.run --config ../config/device_config.yaml --out ../output
cd "/Users/michaellaschaszberger/Downloads/perseus-night-watch 2"
npm run data:import -- /Users/michaellaschaszberger/Desktop/hackachusetts/output
npm run build
```

`data:import` = `node scripts/import-siting.mjs`; the source dir is argv[2] or `$SITING_OUTPUT`, with a clear error if neither exists.

## (d) What stays simulated in Perseus (must stay labeled)

Coastline/islands/basemap SVGs (schematic), tide timing `OM`/`PH`/`amp` and the tide clock, particle directions (`CORRIDORS`), landing point and cable run (schematic coastline), range layer PNG + range dots (`RST`, approximate), hand-drawn zone shapes (D11), the whale-season date rule, $0.12/kWh when no EIA price, and everything in `simulated` mode. Labels: report badge `DROPPED PIN · SITING MODEL` vs `DROPPED PIN · SIMULATED`; a one-line `Simulated:` caption under the tide card and on the cable value; hint text for particles says `direction schematic · speed from modeled current`.

## (e) Data tier / provenance / not-legal-advice obligations per surfaced field

Every surfaced pipeline field shows its tag chip and has a row in a collapsible `Sources` section of the report (`<details>`, closed by default): agency, dataset, endpoint, date + date_kind, label/note.

- Tier 1 QUANTIFIED DATA: velocity_peak (label `modeled current` + model + run date; or `predicted current (harmonic)`), power_density, cf/energy, depth, tidal_range + coops_station (harcon station; never extrapolated from the Boston month), wave_*, dist_transmission_km, dist_substation_km, whale_critical_habitat, jurisdiction boundary. EXTERNAL ADDITION chip where tagged.
- Tier 2 PROXY (chip text `PROXY`): utility (`distribution interconnection below 5 MW`), vessel_traffic, fishing_activity, diadromous_fish_km, shipping/cable/residential exclusions, port_km, profitability multipliers. Policy tab shows `Regulatory proxy · Not legal advice` beside status and jurisdiction.
- Tier 3: sentiment notes shown only as text flags labeled `Tier 3 narrative · not scored` with citations; never a number, never read by any score. Local law: the not-legal-advice line always shows on the Policy tab.
- Gaps: `Data gap — {source}: {reason}`, never 0. Metadata `gap_summary` and `known_limitations` (NECOFS window) appear in the Model popover.

## (f) Verification per repo

Pipeline: pytest -q and ruff check after every task; schema test validates `output/sample/` and real `output/`; `python -m siting.sources.smoke` before the real run; a real run exits 0 and prints a gap summary; provenance validator returns no errors.

Perseus: `npm test` (model unit/property tests, loader guard tests, cross-language fixture within 1e-6, render tests via `react-dom/server` `renderToString` and direct `renderVals()` calls for both themes and for pipeline/sample/simulated modes asserting `Not legal advice`, `modeled current`, `Data gap`, `Tier 3 narrative` and that no gap renders as `0`), the 5,000-cell benchmark, `npm run data:import` on real output, then `npm run build` exits 0 and `dist/data/siting.json` exists.

Needs verification during implementation (do not assume):
- Every "resolve at build start" endpoint (MOM6, MassGIS utility/NHESP/land use/beaches, NOAA Fisheries right whale, NEODP vessel/diadromous, NTAD ports, HIFLD hosting). Unresolved → gap path, recorded in endpoints.py and README.
- NECOFS MassBay coverage of Buzzards Bay / Nantucket Sound (per-cell distance check; GOM3 fallback) and OPeNDAP subset size/time for thousands of cells.
- Real run wall time at `cell_size_mi: 1`; do not shrink scope silently. If it is impractical, report it and ask.
- Size of `siting.json` (target < 5 MB) and Perseus load time.
- Mismatch between the hand-drawn coastline and Census shoreline (water pins with no cell). Report how often it happens at a few test pins.
- renderVals cost with data loaded (time 100 calls in a node test; target < 2 ms each).
- Both themes visually: cannot be verified headless beyond render tests; say so in the final report.

## Ordered work items

- [ ] 1. Pipeline foundations: tasks 1.4, 1.5, 1.6, 2.1-2.7, 3.1-3.3, 4.1-4.4, checkpoint 5. (FEAT-001)
      Files: pipeline/siting/sources/{endpoints,smoke,census}.py, pipeline/siting/grid.py, output/sample/*, pipeline/tests/test_{schema,config_props,ramp_props,weighted_mean_props,provenance_props,fetch,cache_props,retry_props,grid_props,land_mask_props,grid_failure}.py
      Verify: pytest -q and ruff check pass; smoke prints OK/GAP per source.
- [ ] 2. Resource + Feasibility: tasks 6.1-6.10, 8.1-8.2. (FEAT-002)
      Files: pipeline/siting/sources/{necofs,doppio,mom6,coops_currents,coops,ndbc,bathy,ports}.py, pipeline/siting/score/{resource,feasibility}.py, tests per tasks.md.
      Verify: pytest -q and ruff pass.
- [ ] 3. Grid proximity, conflict, jurisdiction: tasks 7.1-7.9. (FEAT-003)
      Files: pipeline/siting/sources/{hifld,massgis,marinecadastre,neodp,emolt}.py, pipeline/siting/score/{grid_proximity,conflict}.py, pipeline/siting/jurisdiction.py, tests per tasks.md.
      Verify: pytest -q and ruff pass.
- [ ] 4. Validation, output, run wiring, real run: tasks 9.1-9.7, checkpoint 10. (FEAT-004)
      Files: pipeline/siting/{validation,output,run}.py, pipeline/siting/score/overall.py, tests per tasks.md, output/*.
      Verify: pytest -q, ruff, smoke, real run exit 0, schema test over output/.
- [ ] 5. Perseus data layer and model ports: tasks 11.1-11.10, 13.1, 12.9. (FEAT-005)
      Files: PERSEUS src/geo/proj.js, src/model/{load,ramps,overall,sort,bins,resource,config,format,index}.js, scripts/import-siting.mjs, package.json scripts, test/*.test.mjs, public/data/*; PIPE pipeline/tests/test_cross_lang_fixture.py.
      Verify: npm test, pytest -q, npm run data:import, npm run build.
- [ ] 6. Perseus UI integration: tasks 12.1-12.8, 13.2, checkpoint 14. (FEAT-006)
      Files: PERSEUS src/App.jsx, README.md, .kiro/steering/tech.md, test/render.test.mjs; PIPE README.md.
      Verify: npm test, npm run build, pytest -q.
- [ ] 7. Stretch + release: tasks 15-18 (cut order in §a), 19.1, 19.2, 20; re-run pipeline, re-import, rebuild. (FEAT-007)
      Verify: pytest -q, ruff, npm test, npm run data:import, npm run build.
