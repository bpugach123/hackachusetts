# Requirements Document

## Introduction

The MA Tidal Buoy Siting Model ranks Massachusetts coastal-water locations for tidal energy device deployment, for companies evaluating real deployment decisions. A Python pipeline generates a land-masked grid over MA coastal waters, fetches public data live, computes five 0-100 component scores (Resource, Profitability, Feasibility, Grid Proximity, Conflict) per grid cell, and writes static JSON/GeoJSON with provenance and citations. A React + Leaflet (TypeScript) frontend loads the static files, recombines an overall score client-side from adjustable weights, and presents a map, a sortable ranked table, and top-5 detail views. There is no runtime server.

Recorded decision: the steering file designates the Hackachusetts kit datasets as the core layer. The user explicitly overrode this during clarification: all data is fetched live from public sources, and the kit zip is not used. Kit-equivalent data (diadromous fish stations, electric utility by town) comes from live public equivalents.

Delivery constraint: due 8pm on the build day, built end-to-end with Kiro. Core scope = grid, Resource, Grid Proximity, Conflict, map and table UI. Stretch scope = Profitability, Feasibility refinements, CFRF/HAB Hub layers, top-5 sentiment notes. Requirements tagged "(Stretch)" belong to stretch scope.

## Glossary

- **Pipeline**: The Python data pipeline (xarray, netCDF/OPeNDAP, geopandas, shapely, requests) that generates the grid, fetches data, computes scores, and writes static output files.
- **Frontend**: The React + Leaflet TypeScript single-page application that loads the Static_Output and renders the map, table, and detail views.
- **Static_Output**: The JSON/GeoJSON files written by the Pipeline and read by the Frontend.
- **Grid**: The set of square cells covering MA coastal waters from the shoreline to the configured outer extent, with land removed.
- **Cell**: One square of the Grid, scored at its Centroid.
- **Centroid**: The geometric center point of a Cell.
- **Cell_Size**: The configurable Grid edge length, default 1 statute mile.
- **Device_Config**: The configuration file holding reference device assumptions and scoring parameters.
- **Tidal_Stream_Device**: Reference device of 100 kW rated power with a rotor diameter of approximately 5 m.
- **Rise_Fall_Device**: Reference 100 kW-class device that extracts energy from tidal range, with a configurable pad area.
- **Power_Coefficient**: The dimensionless fraction (Cp, 0-1) of kinetic power in the swept rotor area that the Tidal_Stream_Device converts to electrical power, stored in the Device_Config.
- **Conversion_Efficiency**: The dimensionless fraction (0-1) of rise/fall potential energy per tidal cycle that the Rise_Fall_Device converts to electrical energy, stored in the Device_Config.
- **Derived_Capacity_Factor**: The capacity factor (0-1) computed by the Pipeline from the resource data for a Cell and device type, as defined in Requirements 4 and 5.
- **Component_Score**: One of Resource, Profitability, Feasibility, Grid Proximity, or Conflict, each on a 0-100 scale.
- **Sub_Score**: A 0-100 intermediate score that feeds a Component_Score.
- **Overall_Score**: The weighted combination of Component_Scores, clamped to 0-100.
- **Hard_Exclusion**: A condition that removes a Cell from ranking (protected habitat, residential or public-beach shoreline, shipping lanes, cable crossings).
- **Data_Gap**: An explicit marker recorded when a fetch fails, returns empty, or has no data for a Cell or period.
- **Provenance_Tag**: A per-factor label: QUANTIFIED DATA (steering Tier 1), PROXY (steering Tier 2), or EXTERNAL ADDITION (non-kit government, research, or regulatory source; may co-occur with QUANTIFIED DATA or PROXY).
- **Citation**: A source reference naming agency, dataset or endpoint, and model run date or fetch date.
- **Cache**: The local raw-fetch cache directory `data/cache/`.
- **Jurisdiction**: State waters (0-3 nm, MA CZM) or federal waters (3-200 nm, BOEM).
- **Legal_Disclaimer**: The text "Not legal advice" attached to every Jurisdiction or regulatory output.
- **Validation_Site**: One of Muskeget Channel, Woods Hole, Cape Cod Canal/Bourne (tidal-stream), Boston Harbor approaches, or Cape Cod Bay (rise/fall), with reference coordinates listed in Requirement 15.

## Requirements

### Requirement 1: Configurable land-masked grid

**User Story:** As a siting analyst, I want a configurable grid over MA coastal waters, so that every candidate location is scored consistently.

#### Acceptance Criteria

1. THE Pipeline SHALL generate square Cells of edge length Cell_Size, aligned to a single fixed origin, that cover MA coastal waters between the New Hampshire and Rhode Island state boundaries (including waters around Nantucket, Martha's Vineyard, and the Elizabeth Islands) from the shoreline out to an outer extent read from the Device_Config, with a default of 12 nautical miles and a minimum of 3 nautical miles.
2. THE Pipeline SHALL read Cell_Size from the Device_Config with a default of 1 statute mile when no value is set, and SHALL accept values from 0.25 to 5 statute miles inclusive.
3. IF Cell_Size or the outer extent in the Device_Config is non-numeric or outside its accepted range, THEN THE Pipeline SHALL stop before fetching any data, report an error naming the invalid parameter and its accepted range, and leave the existing Static_Output unchanged.
4. THE Pipeline SHALL remove every Cell whose Centroid falls on land according to a public shoreline dataset named in a Citation, and SHALL keep every Cell whose Centroid falls in water.
5. IF the shoreline dataset cannot be fetched and no Cache entry exists for it, THEN THE Pipeline SHALL stop, report an error naming the shoreline source and failure reason, and leave the existing Static_Output unchanged.
6. THE Pipeline SHALL record for each Cell in the Static_Output a unique identifier, Centroid latitude and Centroid longitude in WGS84 decimal degrees with at least 5 decimal places, and the Cell polygon geometry.
7. WHEN the Pipeline is run twice with the same Cell_Size and outer extent, THE Pipeline SHALL produce the same set of Cell identifiers with the same Centroid coordinates in both runs.

### Requirement 2: Live data sourcing, caching, and data gaps

**User Story:** As a siting analyst, I want all inputs fetched live from public sources with gaps shown honestly, so that I can trust and verify every number.

#### Acceptance Criteria

1. THE Pipeline SHALL fetch every input dataset from a live public government, research-institution, or recognized regulatory source, and SHALL NOT read any input from the Hackachusetts kit zip.
2. WHEN a fetch succeeds with a non-empty response, THE Pipeline SHALL store the raw response in the Cache keyed by source and request parameters.
3. WHEN a fetch is requested and a readable Cache entry exists for the same source and request parameters, THE Pipeline SHALL use the cached response without making a network request.
4. IF a Cache entry is unreadable or cannot be parsed, THEN THE Pipeline SHALL discard that entry and perform a live fetch.
5. IF a live fetch receives no response within 60 seconds or returns an error, THEN THE Pipeline SHALL retry up to 3 total attempts before treating the fetch as failed.
6. IF a fetch fails after all attempts, returns an empty response, or has no data covering a Cell or requested period, THEN THE Pipeline SHALL record a Data_Gap for each affected factor and Cell containing the source name and a failure reason of fetch failure, empty response, or no coverage, and SHALL continue processing the remaining factors and Cells.
7. IF a factor value is a Data_Gap, THEN THE Pipeline SHALL store the factor value as null and SHALL NOT interpolate, substitute a default, or fill the value from neighboring Cells.
8. WHEN a Pipeline run completes, THE Pipeline SHALL report the total count of Data_Gaps grouped by source.
9. THE Frontend SHALL display each Data_Gap as "Data gap" with its source name and failure reason in the map breakdown, the ranked table, and the top-5 detail panels wherever the affected factor is shown, and SHALL NOT display a Data_Gap as 0.

### Requirement 3: Provenance and citations

**User Story:** As a company decision-maker, I want every factor labeled by data tier and source, so that I can tell measured data from proxies.

#### Acceptance Criteria

1. THE Pipeline SHALL attach to every factor in every Cell breakdown exactly one of the Provenance_Tags QUANTIFIED DATA or PROXY, plus the EXTERNAL ADDITION Provenance_Tag when the source is not a Hackachusetts kit-equivalent dataset.
2. THE Pipeline SHALL attach to every factor in every Cell breakdown a Citation containing the agency name, the dataset name or endpoint, and either the model run date or the fetch date in ISO 8601 format (YYYY-MM-DD).
3. THE Pipeline SHALL label every tidal current velocity value with the text "modeled current" and SHALL include the model name (NECOFS FVCOM, Doppio, or MOM6) and the model run date in that value's Citation.
4. THE Static_Output SHALL include one metadata record containing the Pipeline run timestamp in ISO 8601 UTC format, the Cell_Size, every Device_Config key and value used in the run, and the deduplicated list of all Citations referenced by any Cell.
5. THE Frontend SHALL display one provenance legend table containing exactly one row per factor, with columns for factor name, Provenance_Tags, and Citation.
6. THE Pipeline SHALL store Tier 3 content (public sentiment, local law) only as narrative text fields.
7. THE Pipeline SHALL exclude every Tier 3 content field from the computation of every Component_Score and Overall_Score.
8. IF any factor in any Cell lacks a Provenance_Tag or a Citation when the Pipeline writes the Static_Output, THEN THE Pipeline SHALL stop without writing the Static_Output and SHALL report each affected Cell identifier and factor name.

### Requirement 4: Resource score — tidal stream

**User Story:** As a device developer, I want tidal-stream resource scored on velocity cubed against industry thresholds, so that energy potential is ranked correctly.

#### Acceptance Criteria

1. THE Pipeline SHALL obtain per Cell the depth-averaged tidal current speed time series in m/s at the Centroid over the fetched model period and the peak speed of that series, using sources in the Device_Config priority order (default: NECOFS FVCOM, then Doppio, then MOM6, then NOAA current predictions), and SHALL record the source used and the start and end dates of the period.
2. IF no velocity source in the priority order returns data for a Cell, THEN THE Pipeline SHALL record a Data_Gap for velocity and SHALL set the tidal-stream sub-score, power density, Derived_Capacity_Factor, and tidal-stream annual energy estimate for that Cell to null.
3. THE Pipeline SHALL compute tidal-stream power density in W/m² as 0.5 × seawater density × peak velocity³, where seawater density is read from the Device_Config with a default of 1025 kg/m³.
4. IF a Cell's power density is at most the Device_Config minimum power density (default 500 W/m², approximately 1 m/s), OR the Centroid depth is at most the Device_Config minimum depth (default 5 m), OR the combined area of the Cell and its edge-adjacent Cells meeting both thresholds is at most the Device_Config minimum qualifying area (default 0.5 km²), THEN THE Pipeline SHALL set the tidal-stream sub-score to 0 and store a non-empty threshold-failure record that names every failed threshold (power density, depth, qualifying area), with one or more thresholds per Cell.
5. WHEN a Cell meets all three EPRI thresholds in criterion 4, THE Pipeline SHALL assign a tidal-stream sub-score of the Device_Config baseline credit (default 40) at the minimum power density, increasing linearly in power density to 100 at the power density of the Device_Config benchmark velocity (default 2.5 m/s, within the 2-3 m/s UK benchmark range), and capped at 100 above the benchmark velocity.
6. WHEN a Cell has a non-null tidal-stream sub-score greater than 0, THE Pipeline SHALL compute the tidal-stream Derived_Capacity_Factor as the mean, over every time step of the velocity time series, of min(0.5 × seawater density × rotor swept area (π × (rotor diameter ÷ 2)²) × Power_Coefficient × v³, rated power) divided by rated power, clamped to 0-1, SHALL compute the Tidal_Stream_Device annual energy estimate in kWh/yr as rated power (kW) × Derived_Capacity_Factor × 8760 h, and SHALL store the Derived_Capacity_Factor with the Power_Coefficient, rotor swept area, seawater density, and time series start and end dates used.
7. IF a Cell's tidal-stream sub-score is 0, THEN THE Pipeline SHALL store a Tidal_Stream_Device annual energy estimate of 0 kWh/yr with the reason "below EPRI thresholds".

### Requirement 5: Resource score — rise/fall and wave context

**User Story:** As a device developer, I want rise/fall potential and wave context shown alongside tidal stream, so that I can compare device types per location.

#### Acceptance Criteria

1. THE Pipeline SHALL obtain per Cell the mean tidal range in meters from NOAA CO-OPS water level and harmonic constituent (product=harcon) data for the station nearest the Centroid by great-circle distance, and SHALL record the station identifier and the station distance in km.
2. IF the nearest NOAA CO-OPS station is farther than the Device_Config maximum station distance (default 25 km) from the Centroid, or the station returns no harcon data, THEN THE Pipeline SHALL record a Data_Gap for tidal range and SHALL set the rise/fall sub-score, Derived_Capacity_Factor, and rise/fall energy values for that Cell to null.
3. THE Pipeline SHALL compute rise/fall energy potential per tidal cycle in joules as 0.5 × seawater density × 9.81 m/s² × Rise_Fall_Device pad area (m²) × tidal range² (m²), using pad area and seawater density from the Device_Config.
4. THE Pipeline SHALL compute the rise/fall sub-score as 0 at tidal range at or below the Device_Config minimum range (default 1.0 m), increasing linearly in tidal range² to 100 at the Device_Config maximum range (default 3.0 m), and capped at 100 above the maximum range.
5. WHERE both tidal-stream and rise/fall sub-scores are non-null for a Cell, THE Pipeline SHALL store both sub-scores and their energy estimates, and THE Frontend SHALL display both sub-scores and both energy estimates in the Cell breakdown.
6. THE Pipeline SHALL compute wave power per Cell in kW/m as (ρ g² / 64π) × H² × T, using the mean significant wave height H (m) and mean (dominant) wave period T (s) over the available NDBC realtime2 record from the nearest NDBC buoy within the Device_Config maximum buoy distance (default 100 km), and SHALL record the buoy identifier and the record start and end dates, and flag the value "not blended".
7. THE Pipeline SHALL set the Resource Component_Score to the larger of the non-null tidal-stream and rise/fall sub-scores, SHALL exclude wave power from the Resource Component_Score, and SHALL record which path produced the score.
8. IF both the tidal-stream and rise/fall sub-scores for a Cell are null, THEN THE Pipeline SHALL set the Resource Component_Score to null and record a Data_Gap for Resource.
9. WHEN a Cell has a non-null rise/fall sub-score greater than 0, THE Pipeline SHALL compute the rise/fall Derived_Capacity_Factor as (rise/fall energy per tidal cycle converted to kWh × Conversion_Efficiency × Device_Config tidal cycles per year (default 705.8)) ÷ (rated power (kW) × 8760 h), clamped to 0-1, SHALL compute the Rise_Fall_Device annual energy estimate in kWh/yr as rated power (kW) × Derived_Capacity_Factor × 8760 h, and SHALL store the Derived_Capacity_Factor with the energy per cycle, Conversion_Efficiency, and tidal cycles per year used.

### Requirement 6: Grid Proximity score

**User Story:** As a project developer, I want distance to transmission and substations scored, so that interconnection cost is reflected.

#### Acceptance Criteria

1. THE Pipeline SHALL fetch HIFLD transmission line geometry (returnGeometry=true) and substation locations via live ArcGIS queries bounded to a box covering the full Grid plus a 50 km margin, and tag the results QUANTIFIED DATA with a Citation including the fetch date.
2. THE Pipeline SHALL compute for each Centroid the straight-line distance in kilometers (rounded to 0.01 km) to the nearest transmission line geometry and, separately, to the nearest substation location, and store both distances in the Cell breakdown.
3. THE Pipeline SHALL identify the electric utility service territory containing each Cell, or the nearest territory when no territory contains the Cell, store the utility name with the label "distribution interconnection below 5 MW", and tag the result PROXY.
4. THE Pipeline SHALL convert each stored distance to a 0-100 sub-score using a linear mapping read from the Device_Config: 100 at or below a full-score distance (default 1 km), 0 at or above a zero-score distance (default 30 km), and linearly interpolated between the two distances.
5. THE Pipeline SHALL compute the Grid Proximity Component_Score as the weighted mean of the transmission and substation sub-scores using weights read from the Device_Config (default 0.5 each), rounded to one decimal place on a 0-100 scale.
6. IF the transmission line or substation fetch fails or returns zero features, THEN THE Pipeline SHALL record a Data_Gap for the affected distance in every Cell, set the affected sub-score to null, and compute the Grid Proximity Component_Score from the remaining sub-score alone, or set the score to null when both sub-scores are null.

### Requirement 7: Conflict score and hard exclusions

**User Story:** As a permitting lead, I want hard exclusions applied first and remaining conflicts graded, so that unbuildable sites are removed and risk is visible.

#### Acceptance Criteria

1. WHEN a Cell polygon intersects (shares any area or line crossing) a protected habitat, residential or public-beach shoreline, shipping lane, or cable crossing geometry, THE Pipeline SHALL mark the Cell as Hard_Exclusion and record every triggering layer name with its Citation.
2. WHILE a Cell is marked Hard_Exclusion, THE Pipeline SHALL set the Cell Overall_Score to 0, and THE Frontend SHALL omit the Cell from the ranked table and the ranked map markers.
3. IF a Hard_Exclusion layer fetch fails or returns zero features, THEN THE Pipeline SHALL perform all of the following: record a Data_Gap naming the layer, leave every Cell unexcluded by that layer, and flag every Cell as "exclusion check incomplete" with the missing layer name; and THE Frontend SHALL display the "exclusion check incomplete" flag with the layer name in the Cell breakdown.
4. THE Pipeline SHALL compute the Conflict Component_Score for each non-excluded Cell on a 0-100 scale, where 0 means no detected conflict and 100 means maximum conflict, as the weighted mean of non-null 0-100 sub-scores for Northeast Ocean Data Portal and NOAA Digital Coast layers, eMOLT fishing-activity data tagged PROXY, and diadromous fish station proximity tagged PROXY, using sub-score weights read from the Device_Config (default equal weights).
5. THE Pipeline SHALL compute the diadromous fish proximity sub-score as 100 at 0 km from the nearest station, 0 at or beyond a Device_Config distance (default 5 km), and linearly interpolated between, and SHALL exclude diadromous fish proximity as a sole Hard_Exclusion trigger.
6. IF every Conflict sub-score for a non-excluded Cell is null, THEN THE Pipeline SHALL set the Conflict Component_Score to null and record a Data_Gap for the Cell.
7. (Stretch) WHERE CFRF fishbot (ERDDAP) or HAB Hub data is available, THE Pipeline SHALL include the data as additional Conflict sub-scores tagged EXTERNAL ADDITION.

### Requirement 8: Jurisdiction

**User Story:** As a permitting lead, I want each cell's state or federal jurisdiction shown, so that I know which agency process applies.

#### Acceptance Criteria

1. THE Pipeline SHALL classify each Cell as state waters (MA CZM) when its Centroid lies on the landward side of or on the 3 nm state/federal boundary line, and as federal waters (BOEM) when its Centroid lies seaward of that line, using a public boundary dataset named in a Citation; IF the boundary dataset is available and a Centroid cannot be affirmatively classified as landward of or on the 3 nm boundary line, THEN THE Pipeline SHALL classify the Cell as federal waters (BOEM).
2. THE Pipeline SHALL store the Jurisdiction classification and the responsible agency name as Cell fields separate from the Conflict Component_Score, and SHALL exclude Jurisdiction from every numeric score.
3. WHEN a Cell polygon intersects the 3 nm boundary line, THE Pipeline SHALL classify the Cell by its Centroid and flag the Cell as "straddles state/federal boundary".
4. IF the boundary dataset fetch fails or returns zero features, THEN THE Pipeline SHALL set every Cell Jurisdiction to null with a Data_Gap, and THE Frontend SHALL display "Data gap" in place of the Jurisdiction.
5. THE Frontend SHALL display the Legal_Disclaimer text "Not legal advice" within the same panel, table row, or tooltip as every Jurisdiction or regulatory output.
6. THE Frontend SHALL provide a map layer toggle, off by default, that shows the 3 nm state/federal boundary line when on and hides the line when off.

### Requirement 9: Feasibility score

**User Story:** As an operations engineer, I want depth, slope, wave exposure, and port distance scored, so that installation and maintenance practicality is reflected.

#### Acceptance Criteria

1. THE Pipeline SHALL score depth per device type against depth bands set in the Device_Config, with defaults of 15-35 m for seabed Tidal_Stream_Devices and 5-15 m for buoy devices. Depth inside the band scores 100. Depth outside the band drops linearly to 0 at 10 m beyond the nearest band edge.
2. WHERE seabed slope data is available for a Cell, THE Pipeline SHALL score slope as 100 at or below 5 degrees, dropping linearly to 0 at or above 15 degrees, and include that sub-score in the Feasibility Component_Score.
3. THE Pipeline SHALL score wave exposure from the mean significant wave height at the nearest NDBC buoy as 100 at or below 1 m, dropping linearly to 0 at or above 3 m, and record the buoy identifier in the Citation.
4. THE Pipeline SHALL score port distance from the Centroid to the nearest port in a public port dataset named in a Citation, as 100 at or below 10 km, dropping linearly to 0 at or above 50 km.
5. THE Pipeline SHALL compute the Feasibility Component_Score on a 0-100 scale as the equal-weighted mean of the depth, slope, wave exposure, and port distance sub-scores available for the Cell. The Pipeline SHALL list any sub-scores left out of the mean in the Cell breakdown.
6. IF every Feasibility sub-score for a Cell is a Data_Gap, THEN THE Pipeline SHALL set the Feasibility Component_Score for that Cell to null and record a Data_Gap for Feasibility.

### Requirement 10: Profitability score (Stretch)

**User Story:** As a business lead, I want an illustrative revenue-minus-cost score per cell, so that I can compare relative economic attractiveness.

#### Acceptance Criteria

1. THE Pipeline SHALL compute gross revenue per Cell in USD/yr as the higher of the Tidal_Stream_Device and Rise_Fall_Device annual kWh/yr estimates multiplied by a single average electricity price in USD/kWh. The price SHALL come from ISO-NE LMP or EIA Massachusetts price data tagged EXTERNAL ADDITION, and the Citation SHALL record the price period and the device path used.
2. THE Pipeline SHALL compute a transport multiplier from port distance, a transmission multiplier from grid distance, and a maintenance multiplier from port distance and wave exposure. Each multiplier SHALL be at least 1.0, increase linearly with its input according to slopes set in the Device_Config, be tagged PROXY, and reuse the Feasibility and Grid Proximity inputs.
3. THE Pipeline SHALL compute net value per Cell in USD/yr as gross revenue minus (the baseline annual cost from the Device_Config × transport multiplier × transmission multiplier × maintenance multiplier).
4. THE Pipeline SHALL min-max normalize net value across all Cells that are not Hard_Exclusions to give the Profitability Component_Score, with the lowest net value scoring 0 and the highest scoring 100. If every Cell has the same net value, every Cell SHALL score 50.
5. IF the electricity price fetch fails or returns empty, THEN THE Pipeline SHALL set the Profitability Component_Score to null for every Cell and record a Data_Gap for the price source with the failure reason.
6. THE Frontend SHALL display per Cell the price value in USD/kWh, the price benchmark source and period, gross revenue in USD/yr, each cost multiplier, net value in USD/yr, and the label "Illustrative, not a financial projection".

### Requirement 11: Overall score and adjustable weights

**User Story:** As a decision-maker, I want to adjust scoring weights live, so that I can test how priorities change the ranking.

#### Acceptance Criteria

1. THE Frontend SHALL compute Overall_Score by default as 0.30 × Resource + 0.25 × Profitability + 0.15 × Feasibility + 0.15 × Grid Proximity − 0.15 × Conflict, clamped to 0-100.
2. THE Frontend SHALL provide one weight slider per Component_Score with a range of 0.00 to 1.00 in steps of 0.05, starting at the default weights from criterion 1. The weights SHALL NOT need to sum to 1.
3. WHEN a user changes a weight slider, THE Frontend SHALL recompute every Overall_Score and re-rank the table within 1 second for a Grid of up to 5,000 Cells.
4. IF one or more of the Resource, Profitability, Feasibility, or Grid Proximity Component_Scores for a Cell is null, whether as a Data_Gap or missing from the Static_Output, THEN THE Frontend SHALL compute the positive part of Overall_Score as (sum of weight × score over the available positive components) × (sum of all positive weights ÷ sum of the available positive weights), subtract Conflict weight × Conflict, and clamp the result to 0-100. If the available positive weights sum to 0, the positive part SHALL be 0.
5. IF the Conflict Component_Score for a Cell is null, THEN THE Frontend SHALL leave the Conflict term out of Overall_Score for that Cell.
6. IF any Component_Score used in a Cell's Overall_Score is null, THEN THE Frontend SHALL flag the Cell as "partial score" in the ranked table and the Cell breakdown, and list the missing Component_Scores by name.

### Requirement 12: Device configuration

**User Story:** As a device developer, I want the reference device assumptions visible and editable, so that scores reflect my hardware.

#### Acceptance Criteria

1. THE Device_Config SHALL store, with units: rated power (kW, default 100), rotor diameter (m, default 5), Rise_Fall_Device pad area (m²), Power_Coefficient for the Tidal_Stream_Device (dimensionless, 0 to 1), Conversion_Efficiency for the Rise_Fall_Device (dimensionless, 0 to 1), tidal cycles per year (default 705.8), seawater density (kg/m³, default 1025), depth bands per device type (m), Feasibility sub-score thresholds, Profitability cost multiplier slopes and baseline annual cost (USD/yr), and Cell_Size (statute miles, default 1).
2. THE Frontend SHALL display every Device_Config value with its unit, and mark Cell_Size as editable only by re-running the Pipeline.
3. WHEN a user edits a Device_Config value other than Cell_Size in the Frontend, THE Frontend SHALL recompute, within 1 second, the Derived_Capacity_Factors, energy estimates, Component_Scores, and Overall_Scores that depend on that value and can be derived from raw inputs stored in the Static_Output.
4. IF a user enters a Device_Config value that is not numeric, is less than or equal to 0, or is a Power_Coefficient or Conversion_Efficiency outside 0 to 1, THEN THE Frontend SHALL reject the edit, keep the previous value, and show an error message next to the field naming the allowed range.
5. WHEN a user selects the reset control, THE Frontend SHALL restore every Device_Config value to the value in the Static_Output and recompute every dependent score. Frontend edits SHALL NOT change the Device_Config file.

### Requirement 13: Map view

**User Story:** As a siting analyst, I want a map of scored cells, so that I can see spatial patterns.

#### Acceptance Criteria

1. WHEN the Frontend finishes loading the Static_Output, THE Frontend SHALL render a Leaflet map fitted to the bounding box of all non-excluded Centroids, with exactly one marker per non-excluded Centroid and no markers for Hard_Exclusion Cells.
2. THE Frontend SHALL color each marker by one of five Overall_Score bins (0-19.99, 20-39.99, 40-59.99, 60-79.99, 80-100), each with a distinct color, and scale marker radius linearly from 4 px at Overall_Score 0 to 12 px at Overall_Score 100.
3. THE Frontend SHALL display a map legend listing the five score bins with their colors and the score range of each bin.
4. WHEN a user selects one of the five Component_Score layers, THE Frontend SHALL recolor and resize every marker using the same five bins applied to the selected Component_Score instead of Overall_Score, with at most one score layer active at a time.
5. IF the active layer's score for a Cell is null or a Data_Gap, THEN THE Frontend SHALL render that Cell's marker in a neutral color not used by any score bin at the 4 px radius and list "Data gap" for that color in the legend.
6. WHEN a user toggles the Jurisdiction boundary layer, THE Frontend SHALL show or hide the 3 nm state/federal boundary line without changing the markers.
7. WHEN a user hovers over a marker, THE Frontend SHALL display a tooltip with the Cell identifier, Overall_Score, and Jurisdiction.
8. WHEN a user clicks a marker, THE Frontend SHALL open a Cell breakdown panel showing every raw input, all five Component_Scores, the Provenance_Tags and Citation of each factor, every Data_Gap with source name and failure reason, the Jurisdiction, and the Legal_Disclaimer, and the panel SHALL stay open until the user closes it. WHEN a user clicks a different marker while the Cell breakdown panel is open, THE Frontend SHALL update the open panel in place with the newly clicked Cell's data on that single click.

### Requirement 14: Ranked table and top-5 detail

**User Story:** As a decision-maker, I want a sortable ranking with detail on the best sites, so that I can shortlist candidates.

#### Acceptance Criteria

1. THE Frontend SHALL display a table with one row per non-excluded Cell, showing rank, Cell identifier, Centroid latitude and longitude to 4 decimal places, Overall_Score, the five Component_Scores to 1 decimal place, and the Provenance_Tags of each row's factors.
2. WHEN the table first renders, THE Frontend SHALL sort rows by Overall_Score descending, number rank 1 as the highest Overall_Score, and break ties by Cell identifier ascending.
3. WHEN a user clicks a column header, THE Frontend SHALL sort the table descending by that column. WHEN the user clicks the same header again, THE Frontend SHALL switch between descending and ascending, always place null or Data_Gap values last, and show the active sort column and direction in the header.
4. THE Frontend SHALL display a detail panel for each of the 5 Cells with the highest Overall_Score under the current weights, showing price value, price benchmark, revenue, each cost multiplier, and net value, and SHALL refresh the 5 panels whenever Overall_Score is recomputed.
5. IF the Profitability Component_Score is absent from the Static_Output, THEN THE Frontend SHALL show the indication "Profitability not computed" in each top-5 detail panel in place of only the profitability breakdown section (price value, price benchmark, revenue, cost multipliers, and net value), and SHALL display every other panel element (Cell identifier, Overall_Score, the other Component_Scores, Jurisdiction with the Legal_Disclaimer, and the sentiment note when present) unchanged.
6. IF fewer than 5 non-excluded Cells exist, THEN THE Frontend SHALL display one detail panel per non-excluded Cell.
7. (Stretch) THE Frontend SHALL display in each top-5 detail panel a qualitative public sentiment and conflict note labeled "Tier 3 narrative", with at least one Citation to a public comment record, the reception of a nearby offshore energy project, or local news coverage, and SHALL show no numeric value derived from the note.
8. (Stretch) IF no sentiment source is found for a top-5 Cell, THEN THE Frontend SHALL display "No public sentiment source found" in that Cell's sentiment note area.

### Requirement 15: Validation against known sites

**User Story:** As a judge or reviewer, I want results checked against known strong sites, so that I can trust the model.

#### Acceptance Criteria

1. THE Pipeline SHALL use these Validation_Site reference coordinates (WGS84, decimal degrees): Muskeget Channel 41.360, -70.400 (tidal-stream); Woods Hole 41.521, -70.685 (tidal-stream); Cape Cod Canal/Bourne 41.741, -70.616 (tidal-stream); Boston Harbor approaches 42.330, -70.950 (rise/fall); Cape Cod Bay 41.850, -70.350 (rise/fall).
2. THE Pipeline SHALL match each Validation_Site to the Grid Cell whose Centroid is closest to the reference coordinate, including Hard_Exclusion Cells, and SHALL compute the Resource rank percentile against every Cell in the Grid that has a non-null value for that site's resource path.
3. WHEN the Pipeline completes a run, THE Pipeline SHALL write a validation report to the Static_Output with one entry per Validation_Site containing the site name, reference coordinate, matched Cell identifier, distance from reference coordinate to the matched Centroid in statute miles, resource path, Resource rank percentile, and pass or fail status.
4. IF a tidal-stream Validation_Site's matched Cell ranks below the 75th percentile for tidal-stream Resource, THEN THE Pipeline SHALL mark that site "validation failure" in the report.
5. IF a rise/fall Validation_Site's matched Cell ranks below the 75th percentile for rise/fall Resource, THEN THE Pipeline SHALL mark that site "validation failure" in the report.
6. IF no Centroid lies within 3 statute miles of a Validation_Site's own reference coordinate, or that Validation_Site's own matched Cell resource value is a Data_Gap, THEN THE Pipeline SHALL mark that Validation_Site, and only that Validation_Site, "not evaluated" in the report with the reason, SHALL evaluate every other Validation_Site independently of that outcome, and SHALL continue the Pipeline run.

### Requirement 16: Repository and delivery

**User Story:** As a hackathon team of four, I want a clean public repo with parallel-friendly structure, so that we can ship by 8pm.

#### Acceptance Criteria

1. THE repository SHALL keep the Pipeline, Frontend, Device_Config, Cache, and Static_Output in 5 separate top-level directories, with no Pipeline source in the Frontend directory and no Frontend source in the Pipeline directory.
2. THE repository SHALL include a README with these sections: setup steps for the Pipeline and Frontend, the single command that runs the Pipeline, the single command that starts the Frontend, the data source list with a Citation per source, the Legal_Disclaimer, and "How Kiro was used".
3. THE README "How Kiro was used" section SHALL name the Kiro features the team used (such as specs, steering files, and hooks) and give at least one concrete example of each feature named from this project.
4. THE repository SHALL include a schema document for every Static_Output file that lists each field with its type, unit where applicable, and whether the field can be null.
5. THE repository SHALL include a sample Static_Output dataset of at least 10 Cells in total, counting the required Cells, that matches the documented schema and includes at least one Hard_Exclusion Cell and at least one Cell with a Data_Gap, so that the Frontend can run without a Pipeline run.
6. THE repository SHALL exclude Cache contents, credentials, and API keys from version control.
