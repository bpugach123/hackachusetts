---
inclusion: always
---

# Hackachusetts tidal siting tool — for companies deploying tidal energy devices

Goal: rank MA coastal locations for tidal energy device deployment, for a company
audience evaluating real deployment decisions.

## Data sourcing
Use the Hackachusetts kit datasets as the core/required layer, but actively seek 
out additional reliable public data wherever the kit has a gap — don't leave a 
factor unscored just because the kit didn't provide it directly. "Reliable" means: 
government agencies (federal, state, municipal), established research institutions 
(universities, NOAA/USGS/BOEM-adjacent labs), and recognized industry/regulatory 
bodies (ISO-NE, FERC, state energy offices). Avoid unverified blogs, forums, or 
any source that doesn't show its own methodology or provenance. Always cite the 
specific source and, where possible, the specific dataset/endpoint used — never 
a vague "public data."

Known gaps worth actively searching for, beyond what's in the kit:
- Permitting/legal: BOEM (Bureau of Ocean Energy Management) lease area and 
  consultation data for federal waters; MA EEA/CZM (Coastal Zone Management) 
  guidance for state waters; municipal harbor plans or waterfront ordinances 
  for specific towns under consideration
- Public sentiment: public comment records or meeting minutes from BOEM/CZM 
  proceedings on nearby offshore energy projects (e.g. offshore wind siting 
  fights are a real, citable proxy for likely community reaction in the same 
  waters); local news coverage of marine energy or offshore wind in the 
  specific town, if searchable
- Commercial/port activity: USACE (Army Corps) navigation channel data, 
  state port authority data, NOAA nautical chart shipping lane layers
- Protected species/habitat beyond diadromous fish: NOAA Fisheries critical 
  habitat designations, MA Natural Heritage & Endangered Species Program data

## Data tiers — never blur these in the UI or in scoring
TIER 1 (quantified, real data — kit or external):
- Tidal current velocity: NECOFS FVCOM / Doppio / MOM6 (MODELED, not observed — 
  always label as "modeled current" in any UI/output, cite model name + run date)
- Tidal water level/harmonics: NOAA CO-OPS API (Boston gauge in zip is Aug 2024 
  only — pull harmonic constituents product=harcon for other stations/full-year 
  profile, don't extrapolate from one month)
- Grid proximity: C3 transmission lines (attributes only, NO geometry in zip — 
  use live ArcGIS bbox query for shapes) + electric utility by town

TIER 2 (proxy, must be labeled as proxy in output — kit or external):
- Commercial/port fit: Northeast Ocean Data Portal layers, plus USACE/port 
  authority data where it fills a gap (live, no bulk file — cite portal + 
  specific layer used)
- Regulatory friction: diadromous fish monitoring stations + state (0-3nm) vs 
  federal (3-200nm) water jurisdiction, plus BOEM/CZM permitting-history data 
  where available for the specific area

TIER 3 (narrative only, never scored numerically):
- Public sentiment — no numeric dataset exists for this, in-kit or out. Search 
  for real signal (comment records, nearby offshore-energy project reception, 
  local coverage) and surface it as a qualitative flag per top candidate, never 
  as an invented number.
- Local law/permitting — the Tier 2 regulatory proxy above is NOT legal advice, 
  even when backed by BOEM/CZM data. Always state this explicitly in output.

## Rules
- Power scales with current velocity CUBED — never use linear scaling for 
  energy potential.
- Never fabricate a value when a live API call fails, a search turns up nothing, 
  or a station has no data for a given location/period — surface the gap, 
  don't interpolate or guess silently.
- Always cite which model/station/portal/external source was actually used, 
  with enough specificity (agency, dataset name, date) that a user could verify it.
- Prefer a real external source with an honest "this is a proxy" label over 
  leaving a factor blank — but never promote a weak or unverifiable external 
  source to Tier 1 just to fill a gap.