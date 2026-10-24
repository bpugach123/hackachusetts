"""MassGIS ArcGIS layers (MassGIS ArcGIS Server, ``arcgisserver.digital.mass.gov``).

One fetch function per layer, all built on :func:`fetch_massgis_layer` (shared
envelope query, pagination, mirror fallback and gap handling from
:mod:`siting.sources.arcgis`). Task 7.4 adds the exclusion layers (NHESP
priority habitat, residential land use, public beaches) here the same way:
declare the endpoint in ``endpoints.py`` and add a thin ``fetch_*`` wrapper.

Layers so far:

* Electricity Providers, by Town (MA DPU / MassGIS) -> utility service territory
  for Grid Proximity (Req 6.3). Tagged PROXY.
* Hard-exclusion layers (Req 7.1): NHESP Priority Habitats of Rare Species,
  residential Land Use 2016 polygons, and DCR Marine Beaches. NHESP and Land Use
  are dense statewide polygon layers, so they are queried tile by tile with the
  cell polygons (:func:`siting.sources.arcgis.fetch_layer_tiles`) instead of one
  Grid envelope. Marine Beaches holds only "Public" and "Semi-Public" beaches
  (checked 2026-10-24), so every beach is used.
"""

from __future__ import annotations

from collections.abc import Sequence

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.sources.arcgis import Envelope, Layer, fetch_layer, fetch_layer_tiles
from siting.sources.endpoints import (
    MASSGIS_BEACHES,
    MASSGIS_LANDUSE,
    MASSGIS_NHESP,
    MASSGIS_UTILITY,
    Endpoint,
)

UTILITY_SOURCE = MASSGIS_UTILITY.source  # "MASSGIS_UTILITY"
UTILITY_CITATION_ID = "massgis-utility"
UTILITY_FIELDS = "TOWN,ELEC,ELEC_LABEL"

# Statewide MA envelope (lon/lat) for layers that cover towns, not the sea.
MA_ENVELOPE: Envelope = (-73.6, 41.1, -69.8, 42.95)


def fetch_massgis_layer(
    client: FetchClient,
    ep: Endpoint,
    envelope: Envelope,
    *,
    citation_id: str,
    where: str | None = None,
    out_fields: str = "*",
) -> Layer | DataGap:
    """Any MassGIS feature layer inside ``envelope`` (Layer or run-wide gap)."""
    return fetch_layer(
        client,
        ep,
        envelope,
        citation_id=citation_id,
        where=where,
        out_fields=out_fields,
    )


def fetch_utility_territory(
    client: FetchClient, envelope: Envelope = MA_ENVELOPE
) -> Layer | DataGap:
    """Town polygons with the electric provider (fields TOWN, ELEC, ELEC_LABEL)."""
    return fetch_massgis_layer(
        client,
        MASSGIS_UTILITY,
        envelope,
        citation_id=UTILITY_CITATION_ID,
        out_fields=UTILITY_FIELDS,
    )


def utility_name(props: dict) -> str | None:
    """Provider name for a town record: ``ELEC_LABEL``, else ``ELEC``."""
    for key in ("ELEC_LABEL", "ELEC"):
        v = props.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ------------------------------------------------------------ exclusion layers

NHESP_CITATION_ID = "massgis-nhesp-priority"
LANDUSE_CITATION_ID = "massgis-landuse-residential"
BEACHES_CITATION_ID = "massgis-marine-beaches"

NHESP_FIELDS = "PRIHAB_ID,VERSION"

# Land Use 2016 USEGENCODE classes that are residential (distinct values checked
# 2026-10-24): 10 mixed use primarily residential, 11 single family,
# 12 multi-family, 13 residential other.
RESIDENTIAL_USE_CODES: tuple[int, ...] = (10, 11, 12, 13)
RESIDENTIAL_WHERE = "USEGENCODE IN ({})".format(
    ",".join(str(c) for c in RESIDENTIAL_USE_CODES)
)
LANDUSE_FIELDS = "USEGENCODE,USEGENNAME"

# Server-side generalization (degrees, ~5 m) for the two dense polygon layers.
# Keeps payloads tractable; it can only move an edge by about 5 m, which matters
# only for a polygon that overlaps a cell by less than that.
DENSE_LAYER_OFFSET_DEG = 0.00005

BEACH_FIELDS = "BEACHNAME,TOWNNAME"


def fetch_nhesp_priority_habitat(
    client: FetchClient, tiles: Sequence[dict]
) -> Layer | DataGap:
    """NHESP Priority Habitat polygons touching the query tiles."""
    return fetch_layer_tiles(
        client,
        MASSGIS_NHESP,
        tiles,
        citation_id=NHESP_CITATION_ID,
        out_fields=NHESP_FIELDS,
        max_allowable_offset=DENSE_LAYER_OFFSET_DEG,
    )


def fetch_residential_landuse(
    client: FetchClient, tiles: Sequence[dict]
) -> Layer | DataGap:
    """Residential Land Use 2016 polygons touching the query tiles."""
    return fetch_layer_tiles(
        client,
        MASSGIS_LANDUSE,
        tiles,
        citation_id=LANDUSE_CITATION_ID,
        where=RESIDENTIAL_WHERE,
        out_fields=LANDUSE_FIELDS,
        max_allowable_offset=DENSE_LAYER_OFFSET_DEG,
    )


def fetch_public_beaches(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """Public and semi-public marine beach lines (points on the fallback)."""
    return fetch_massgis_layer(
        client,
        MASSGIS_BEACHES,
        envelope,
        citation_id=BEACHES_CITATION_ID,
        out_fields=BEACH_FIELDS,
    )
