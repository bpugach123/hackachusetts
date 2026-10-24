"""Live endpoint registry (Req 2.1, 6.1).

Single source of truth for every upstream URL the pipeline hits. Source modules
import from here; nothing else hard-codes a URL. The smoke test
(``python -m siting.sources.smoke``) hits each endpoint once with a tiny bbox and
prints OK/GAP so the "resolve at build start" rows can be confirmed in hour 1.

Each entry is an :class:`Endpoint`. ``source`` matches the ids used by
``provenance.py`` (``KIT_EQUIVALENT_SOURCES`` / ``EXTERNAL_SOURCES``) so a factor's
tags can be derived from the source it came from. ``fallback`` is the URL used when
the primary is unreachable; a few rows have their service path marked
``resolve_at_build_start`` because the host moves the layer often.

Every ``resolve_at_build_start`` row carries a ``resolution`` note: either
``"resolved YYYY-MM-DD: ..."`` (the final layer URL was found in the host's ArcGIS
REST / THREDDS / ERDDAP directory and smoke-tested) or ``"unresolved -> gap: ..."``
(no reliable public endpoint was found; the source degrades through the normal
Data_Gap path and no URL is invented). The smoke test flags any row without one.

No network calls happen at import time. These are declarations only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    """One upstream data endpoint.

    ``kind`` is how the fetch layer talks to it: ``arcgis`` (Esri REST query or
    identify), ``opendap`` (xarray/THREDDS), ``erddap`` (tabledap/griddap), ``rest``
    (plain JSON/CSV/HTTP), or ``file`` (a zip or flat file download).
    """

    source: str
    agency: str
    dataset: str
    url: str
    kind: str  # arcgis | opendap | erddap | rest | file
    fallback: str | None = None
    tier: str = "QUANTIFIED DATA"
    resolve_at_build_start: bool = False
    resolution: str | None = None
    notes: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def unresolved(self) -> bool:
        """True when the build-start resolution found no usable endpoint."""
        return bool(self.resolution and self.resolution.startswith("unresolved"))


# Host roots used by several rows (directory listings checked on 2026-10-24).
MASSGIS_AGOL = "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL"
NEODP_ROOT = "https://services.northeastoceandata.org/arcgis1/rest/services"
NOAA_AGOL = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services"

# --------------------------------------------------------------- shoreline / grid

CENSUS_STATES = Endpoint(
    source="CENSUS_SHORELINE",
    agency="US Census Bureau",
    dataset="Cartographic Boundary 2023, states 1:500k",
    url="https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_state_500k.zip",
    kind="file",
    tier="QUANTIFIED DATA + EXTERNAL ADDITION",
    notes="Filter STATEFP in {25 MA, 33 NH, 44 RI}. No cache + failed fetch is fatal.",
)

# ----------------------------------------------------------------------- velocity

NECOFS_MASSBAY = Endpoint(
    source="NECOFS_FVCOM",
    agency="UMass Dartmouth SMAST",
    dataset="NECOFS FVCOM MassBay forecast",
    url="http://www.smast.umassd.edu:8080/thredds/dodsC/FVCOM/NECOFS/Forecasts/NECOFS_FVCOM_OCEAN_MASSBAY_FORECAST.nc",
    kind="opendap",
    fallback="http://www.smast.umassd.edu:8080/thredds/dodsC/FVCOM/NECOFS/Forecasts/NECOFS_GOM3_FORECAST.nc",
    notes="Depth-averaged ua,va at nearest element (lonc,latc); h at nearest node. "
    "Forecast window ~3-5 days; record t_start/t_end. The GOM3 fallback is listed "
    "in the SMAST forecasts catalog but its .das returned 404 on 2026-10-24.",
)

DOPPIO = Endpoint(
    source="DOPPIO",
    agency="Rutgers / NOAA",
    dataset="Doppio ROMS",
    url="https://tds.marine.rutgers.edu/thredds/dodsC/roms/doppio/2017_da/his/History_Best",
    kind="opendap",
    notes="ubar,vbar at nearest rho point.",
)

MOM6_NWA12 = Endpoint(
    source="MOM6",
    agency="NOAA GFDL / CEFI",
    dataset="MOM6 NWA12 hindcast (CEFI portal, daily raw, release r20230520)",
    url="https://psl.noaa.gov/thredds/dodsC/Projects/CEFI/regional_mom6/cefi_portal/northwest_atlantic/full_domain/hindcast/daily/raw/r20230520/ssu.nwa.full.hcast.daily.raw.r20230520.199301-201912.nc",
    kind="opendap",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: NOAA PSL THREDDS CEFI catalog "
    "(Projects/CEFI/regional_mom6/cefi_portal/northwest_atlantic/full_domain/"
    "hindcast). Only daily-mean SURFACE velocity (ssu/ssv) is published; there is "
    "no ubar/vbar and no sub-daily output, so tidal peaks are not resolved. The "
    "r20250715 release has no ssu/ssv files. The old Rutgers cefi/NWA12 path was "
    "not found.",
    notes="ssu at u-points, ssv (extra['v_url']) at v-points, 1993-01..2019-12. "
    "Daily means cannot give a tidal peak velocity; the velocity resolver should "
    "treat MOM6 as 'no coverage' for peak tidal current rather than under-report it.",
    extra={
        "v_url": "https://psl.noaa.gov/thredds/dodsC/Projects/CEFI/regional_mom6/cefi_portal/northwest_atlantic/full_domain/hindcast/daily/raw/r20230520/ssv.nwa.full.hcast.daily.raw.r20230520.199301-201912.nc",
        "catalog": "https://psl.noaa.gov/thredds/catalog/Projects/CEFI/regional_mom6/cefi_portal/northwest_atlantic/full_domain/hindcast/catalog.html",
    },
)

NOAA_CURRENTS_STATIONS = Endpoint(
    source="NOAA_CURRENTS",
    agency="NOAA CO-OPS",
    dataset="Current predictions station list",
    url="https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=currentpredictions",
    kind="rest",
    notes="Nearest current station within 5 km; label 'predicted current (harmonic)'.",
)

NOAA_CURRENTS_DATA = Endpoint(
    source="NOAA_CURRENTS",
    agency="NOAA CO-OPS",
    dataset="Current predictions",
    url="https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
    kind="rest",
    notes="product=currents_predictions&interval=MAX_SLACK&units=metric&time_zone=gmt.",
)

# ----------------------------------------------------------------- tides (CO-OPS)

COOPS_HARCON_STATIONS = Endpoint(
    source="NOAA_COOPS",
    agency="NOAA CO-OPS",
    dataset="Harmonic constituent station list",
    url="https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=harcon",
    kind="rest",
)

COOPS_HARCON = Endpoint(
    source="NOAA_COOPS",
    agency="NOAA CO-OPS",
    dataset="Harmonic constituents (harcon)",
    url="https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{station}/harcon.json",
    kind="rest",
    notes="M2 amplitude supporting record.",
)

COOPS_DATUMS = Endpoint(
    source="NOAA_COOPS",
    agency="NOAA CO-OPS",
    dataset="Tidal datums",
    url="https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/{station}/datums.json",
    kind="rest",
    notes="MN = MHW - MLW mean range.",
)

# ----------------------------------------------------------------------- waves

NDBC_ACTIVE = Endpoint(
    source="NDBC",
    agency="NOAA NDBC",
    dataset="Active stations",
    url="https://www.ndbc.noaa.gov/activestations.xml",
    kind="rest",
)

NDBC_REALTIME = Endpoint(
    source="NDBC",
    agency="NOAA NDBC",
    dataset="realtime2 standard meteorological",
    url="https://www.ndbc.noaa.gov/data/realtime2/{station}.txt",
    kind="rest",
    notes="Mean WVHT/DPD over the 45-day record; drop MM.",
)

# ------------------------------------------------------------------ grid proximity

HIFLD_TRANSMISSION = Endpoint(
    source="HIFLD_TRANSMISSION",
    agency="HIFLD (DHS)",
    dataset="Electric Power Transmission Lines",
    url="https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query",
    kind="arcgis",
    fallback="https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0/query",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: primary HIFLD Open layer still serves "
    "geometry (layer last edited 2023-09-05; 584 lines in the MA test envelope). "
    "Fallback is the ArcGIS Federal User Community copy titled 'U.S. Electric "
    "Power Transmission Lines (Archive)'.",
    notes="returnGeometry=true; paginate on exceededTransferLimit.",
)

HIFLD_SUBSTATIONS = Endpoint(
    source="HIFLD_SUBSTATIONS",
    agency="HIFLD (DHS)",
    dataset="Electric Substations",
    url="https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query",
    kind="arcgis",
    resolve_at_build_start=True,
    resolution="unresolved -> gap: the HIFLD Open service returns 'Invalid URL' "
    "and no Electric_Substations layer exists in the HIFLD Open or Federal User "
    "Community directories (checked 2026-10-24). Only non-federal, non-MA copies "
    "were found on ArcGIS Online, which are not reliable sources here.",
    notes="Deriving substations from line endpoints is NOT allowed -> Data_Gap.",
)

MASSGIS_UTILITY = Endpoint(
    source="MASSGIS_UTILITY",
    agency="MassGIS",
    dataset="Electricity Providers, by Town (2015)",
    url=f"{MASSGIS_AGOL}/ElectricityProviders/FeatureServer/0/query",
    kind="arcgis",
    tier="PROXY",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: MassGIS ArcGIS Server "
    "(arcgisserver.digital.mass.gov) AGOL/ElectricityProviders layer 0. "
    "gis.mass.gov no longer resolves in DNS.",
    notes="Utility-by-town layer; label 'distribution interconnection below 5 MW'. "
    "Data are as of 2015.",
)

# -------------------------------------------------------- jurisdiction / boundary

MARINECADASTRE_SUBMERGED_LANDS = Endpoint(
    source="MARINECADASTRE_SUBMERGED_LANDS",
    agency="NOAA OCM MarineCadastre",
    dataset="US State Submerged Lands",
    url="https://coast.noaa.gov/arcgis/rest/services/MarineCadastre/USStateSubmergedLands/MapServer/0/query",
    kind="arcgis",
    fallback="https://gis.boem.gov/server/rest/services/BOEM_BSEE/MMC_Layers/FeatureServer/8/query",
    tier="QUANTIFIED DATA + EXTERNAL ADDITION",
    notes="covers(centroid) -> state/MA CZM else federal/BOEM. Boundary line -> "
    "boundary_3nm.geojson. The BOEM fallback ('Submerged Lands Act Boundary', a "
    "polyline) answers with the macOS system trust store, but Python requests/"
    "certifi fails TLS verification for gis.boem.gov (incomplete chain); it "
    "surfaces as a fetch-failure gap. TLS verification is never disabled.",
)

# --------------------------------------------------------- conflict / exclusions

MARINECADASTRE_SHIPPING = Endpoint(
    source="MARINECADASTRE_SHIPPING",
    agency="NOAA OCM / Office of Coast Survey",
    dataset="Shipping Fairways, Lanes, and Zones (Shipping Lanes and Regulations)",
    url="https://encdirect.noaa.gov/arcgis/rest/services/NavigationChartData/MarineTransportation/MapServer/0/query",
    kind="arcgis",
    fallback=f"{NEODP_ROOT}/MarineTransportation_Navigation/MapServer/36/query",
    tier="PROXY + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: coast.noaa.gov MarineCadastre folder no "
    "longer has a ShippingLanes service. NOAA serves the same dataset (InPort "
    "39986) at encdirect.noaa.gov NavigationChartData/MarineTransportation layer "
    "0 (polygons). Fallback: NE Ocean Data Portal 'Traffic Lane' layer 36; cite "
    "NEODP when it is used.",
    notes="InPort 39986.",
)

MARINECADASTRE_CABLES = Endpoint(
    source="MARINECADASTRE_CABLES",
    agency="NOAA OCM MarineCadastre",
    dataset="Submarine Cables",
    url="https://coast.noaa.gov/arcgis/rest/services/Hosted/SubmarineCables/FeatureServer/0/query",
    kind="arcgis",
    fallback=f"{NEODP_ROOT}/MarineTransportation_Navigation/MapServer/32/query",
    tier="PROXY + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: MarineCadastre moved the layer to "
    "coast.noaa.gov Hosted/SubmarineCables (owner marinecadastre_noaa, "
    "polylines). There is no 'SubmarineCableAreas' service now. Fallback: NE Ocean "
    "Data Portal 'Submarine Cable Area' polygons (layer 32); cite NEODP when used.",
)

MARINECADASTRE_MPA = Endpoint(
    source="MARINECADASTRE_MPA",
    agency="NOAA ONMS Marine Protected Areas Center",
    dataset="NOAA Marine Protected Areas Inventory 2023",
    url=f"{NOAA_AGOL}/NOAA_MPA_Inventory_2023/FeatureServer/0/query",
    kind="arcgis",
    tier="QUANTIFIED DATA + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: coast.noaa.gov has no MPAInventory service "
    "now. The inventory is on NOAA's ArcGIS Online org (owner MPA_noaa, item "
    "titled 'NOAA Marine Protected Areas Inventory 2024').",
)

MASSGIS_NHESP = Endpoint(
    source="MASSGIS_NHESP",
    agency="MassGIS / MA NHESP",
    dataset="NHESP Priority Habitats of Rare Species",
    url=f"{MASSGIS_AGOL}/NHESP_Priority_Habitats/FeatureServer/0/query",
    kind="arcgis",
    tier="QUANTIFIED DATA + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: MassGIS AGOL/NHESP_Priority_Habitats layer 0.",
)

MASSGIS_LANDUSE = Endpoint(
    source="MASSGIS_LANDUSE",
    agency="MassGIS",
    dataset="Land Cover / Land Use 2016 (residential)",
    url=f"{MASSGIS_AGOL}/LandCoverLandUse2016/FeatureServer/0/query",
    kind="arcgis",
    tier="PROXY + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: MassGIS AGOL/LandCoverLandUse2016 layer 0. "
    "Filter to residential use codes in the source module.",
)

MASSGIS_BEACHES = Endpoint(
    source="MASSGIS_BEACHES",
    agency="MassGIS",
    dataset="Marine Beaches",
    url=f"{MASSGIS_AGOL}/Marine_Beaches/FeatureServer/2/query",
    kind="arcgis",
    tier="PROXY + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: MassGIS AGOL/Marine_Beaches layer 2 "
    "(polygons; layer 0 holds points). It covers marine beaches generally, not "
    "only DCR public beaches.",
)

NOAA_FISHERIES_RIGHT_WHALE = Endpoint(
    source="NOAA_FISHERIES_RIGHT_WHALE",
    agency="NOAA Fisheries (NMFS)",
    dataset="North Atlantic right whale critical habitat (2016 designation)",
    url=f"{NOAA_AGOL}/WhaleNorthAtlanticRight20160127/FeatureServer/64/query",
    kind="arcgis",
    tier="QUANTIFIED DATA + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: NMFS ESA critical-habitat layer on NOAA's "
    "ArcGIS Online org (WhaleNorthAtlanticRight20160127, layer 64). "
    "mapservices.weather.noaa.gov has no critical-habitat service.",
    notes="Graded conflict sub-score, NOT a hard exclusion (see README).",
)

NEODP_VESSEL_TRAFFIC = Endpoint(
    source="NEODP_VESSEL_TRAFFIC",
    agency="NE Ocean Data Portal",
    dataset="2024 All Vessel Transit Counts (AIS)",
    url=f"{NEODP_ROOT}/MarineTransportation/MapServer/identify",
    kind="arcgis",
    fallback="https://coast.noaa.gov/arcgis/rest/services/MarineCadastre/AISVesselTransitCounts2024/MapServer/identify",
    tier="PROXY",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: NEODP moved to /arcgis1/; layer 198 '2024 "
    "All Vessel Transit Counts' is a raster, so it is read per point with "
    "MapServer/identify ('Pixel Value' = transit count). Fallback: MarineCadastre "
    "AISVesselTransitCounts2024 layer 0, which returns a class value, not a count.",
    extra={"layer_id": 198, "fallback_layer_id": 0},
)

NEODP_DIADROMOUS = Endpoint(
    source="NEODP_DIADROMOUS",
    agency="NE Ocean Data Portal / MA DMF",
    dataset="River Herring and Shad Runs (diadromous fish)",
    url=f"{NEODP_ROOT}/Fish/MapServer/8/query",
    kind="arcgis",
    fallback=f"{MASSGIS_AGOL}/DiadromousFish/FeatureServer/0/query",
    tier="PROXY",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: NEODP Fish/MapServer layer 8 (points). "
    "Fallback: MassGIS AGOL/DiadromousFish layer 0 'Sampling Stations' (MA DMF).",
    notes="Proximity only; never a hard exclusion.",
)

EMOLT = Endpoint(
    source="EMOLT",
    agency="NOAA NEFSC / CFRF",
    dataset="eMOLT bottom-temperature observations (eMOLT_RT)",
    url="https://erddap.emolt.net/erddap/tabledap/eMOLT_RT.csv",
    kind="erddap",
    tier="PROXY",
    notes="Observation points within 5 km -> fishing_activity proxy. The dataset "
    "id 'eMOLT' does not exist on this ERDDAP; eMOLT_RT is the live dataset.",
)

# ------------------------------------------------------------- bathymetry / ports

NCEI_CRM = Endpoint(
    source="NCEI_CRM",
    agency="NOAA NCEI",
    dataset="Coastal Relief Model, Vol. 1 (Northeast Atlantic)",
    url="https://www.ngdc.noaa.gov/thredds/dodsC/crm/crm_vol1.nc",
    kind="opendap",
    tier="QUANTIFIED DATA + EXTERNAL ADDITION",
    notes="Depth fallback (NECOFS h primary) and slope source. Vars x, y, z "
    "(m, negative below sea level). The coastwatch ERDDAP id usgsCeCrm1 "
    "returned 404 on 2026-10-24, so the NCEI THREDDS copy is used.",
)

NTAD_PORTS = Endpoint(
    source="NTAD_PORTS",
    agency="USACE WCSC / BTS NTAD",
    dataset="Principal Ports",
    url="https://geo.dot.gov/server/rest/services/Hosted/principal_ports_view/FeatureServer/0/query",
    kind="arcgis",
    tier="PROXY + EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: geo.dot.gov Hosted/principal_ports_view layer 0.",
)

# --------------------------------------------------------------- stretch sources

EIA_RETAIL = Endpoint(
    source="EIA",
    agency="US EIA",
    dataset="Electricity retail sales (v2)",
    url="https://api.eia.gov/v2/electricity/retail-sales/data/",
    kind="rest",
    tier="EXTERNAL ADDITION + PROXY",
    notes="frequency=monthly, stateid=MA, sectorid=ALL. Needs EIA_API_KEY env var.",
)

CFRF_FISHBOT = Endpoint(
    source="CFRF",
    agency="Commercial Fisheries Research Foundation",
    dataset="Fishing Industry Shared Bottom Oceanographic Timeseries (fishbot)",
    url="https://erddap.ondeckdata.com/erddap/tabledap/fishbot_realtime.csv",
    kind="erddap",
    tier="EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="resolved 2026-10-24: ERDDAP dataset id fishbot_realtime "
    "(fishbot_archive also exists).",
)

HAB_HUB = Endpoint(
    source="HAB_HUB",
    agency="WHOI / NOAA NCCOS",
    dataset="Harmful Algal Bloom events (HABHub)",
    url="https://habhub.whoi.edu/",
    kind="rest",
    tier="EXTERNAL ADDITION",
    resolve_at_build_start=True,
    resolution="unresolved -> gap: habhub.whoi.edu is a web app; its data API "
    "(habhub-api.whoi.edu) needs an authorization token, and no public "
    "unauthenticated endpoint was found (checked 2026-10-24).",
)


# Every endpoint, for the smoke test and the README source list.
ALL_ENDPOINTS: tuple[Endpoint, ...] = (
    CENSUS_STATES,
    NECOFS_MASSBAY,
    DOPPIO,
    MOM6_NWA12,
    NOAA_CURRENTS_STATIONS,
    NOAA_CURRENTS_DATA,
    COOPS_HARCON_STATIONS,
    COOPS_HARCON,
    COOPS_DATUMS,
    NDBC_ACTIVE,
    NDBC_REALTIME,
    HIFLD_TRANSMISSION,
    HIFLD_SUBSTATIONS,
    MASSGIS_UTILITY,
    MARINECADASTRE_SUBMERGED_LANDS,
    MARINECADASTRE_SHIPPING,
    MARINECADASTRE_CABLES,
    MARINECADASTRE_MPA,
    MASSGIS_NHESP,
    MASSGIS_LANDUSE,
    MASSGIS_BEACHES,
    NOAA_FISHERIES_RIGHT_WHALE,
    NEODP_VESSEL_TRAFFIC,
    NEODP_DIADROMOUS,
    EMOLT,
    NCEI_CRM,
    NTAD_PORTS,
    EIA_RETAIL,
    CFRF_FISHBOT,
    HAB_HUB,
)

ENDPOINTS_BY_SOURCE: dict[str, list[Endpoint]] = {}
for _ep in ALL_ENDPOINTS:
    ENDPOINTS_BY_SOURCE.setdefault(_ep.source, []).append(_ep)
del _ep
