"""Marine hard-exclusion layers (Req 7.1, 7.3).

Thin wrappers over :func:`siting.sources.arcgis.fetch_layer` for the marine
layers declared in ``endpoints.py``. The MarineCadastre services named in the
design have moved; ``endpoints.py`` documents the current hosts.

* Shipping fairways, lanes and zones (NOAA OCS ENC Direct, InPort 39986).
  Filtered to traffic separation schemes, their lanes and precautionary areas.
  "Area to be Avoided", "Recommended Routes" and "Speed Restrictions/Right
  Whales" share the layer but are not shipping lanes, so they are left out.
  The fallback (NE Ocean Data Portal "Traffic Lane") holds lanes only.
* Submarine cable areas (NE Ocean Data Portal, NOAA ENC data). Every cable area
  is used; the fallback is NOAA OCM's submarine cable layer.
* NOAA MPA Inventory, filtered to no-take, no-access and no-impact sites
  (``Prot_Lvl``; "No Impact" is stricter than no-take). The OceanReports
  fallback has no protection-level field, so it cannot apply the filter and is
  not used: without the primary the layer becomes a Data_Gap instead of
  excluding every MPA.

Tags: shipping and cables are PROXY + EXTERNAL ADDITION; the MPA Inventory is
QUANTIFIED DATA + EXTERNAL ADDITION (``provenance.tags_for``).
"""

from __future__ import annotations

from dataclasses import replace

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.sources.arcgis import Envelope, Layer, fetch_layer
from siting.sources.endpoints import (
    MARINECADASTRE_CABLES,
    MARINECADASTRE_MPA,
    MARINECADASTRE_SHIPPING,
)

# Citation ids (shipping and cables match output/sample/metadata.json).
SHIPPING_CITATION_ID = "marinecadastre-fairways"
CABLES_CITATION_ID = "marinecadastre-cables"
MPA_CITATION_ID = "noaa-mpa-inventory"

# ENC Direct THEMELAYER values that are shipping lanes (checked 2026-10-24 over
# MA waters; the layer also holds ATBAs, recommended routes and whale speed zones).
SHIPPING_THEMES: tuple[str, ...] = (
    "Traffic Separation Schemes/Traffic Lanes",
    "Traffic Separation Schemes",
    "Precautionary Areas",
)
SHIPPING_WHERE = "THEMELAYER IN ({})".format(
    ",".join(f"'{t}'" for t in SHIPPING_THEMES)
)
SHIPPING_FIELDS = "THEMELAYER,OBJNAM"

# MPA Inventory protection levels that forbid extraction or access.
MPA_LEVELS: tuple[str, ...] = ("No Take", "No Access", "No Impact")
MPA_WHERE = "Prot_Lvl IN ({})".format(",".join(f"'{lv}'" for lv in MPA_LEVELS))
MPA_FIELDS = "Site_ID,Site_Name,Prot_Lvl,Fish_Rstr"
# Primary only: the fallback cannot filter by protection level (see above).
MPA_NO_TAKE = replace(MARINECADASTRE_MPA, fallback=None)


def fetch_shipping_lanes(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """TSS lanes, separation zones and precautionary areas inside ``envelope``."""
    return fetch_layer(
        client,
        MARINECADASTRE_SHIPPING,
        envelope,
        citation_id=SHIPPING_CITATION_ID,
        where=SHIPPING_WHERE,
        fallback_where="1=1",
        out_fields=SHIPPING_FIELDS,
    )


def fetch_cable_areas(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """Submarine cable area polygons inside ``envelope``."""
    return fetch_layer(
        client, MARINECADASTRE_CABLES, envelope, citation_id=CABLES_CITATION_ID
    )


def fetch_mpa_no_take(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """No-take / no-access / no-impact MPA Inventory sites inside ``envelope``."""
    return fetch_layer(
        client,
        MPA_NO_TAKE,
        envelope,
        citation_id=MPA_CITATION_ID,
        where=MPA_WHERE,
        out_fields=MPA_FIELDS,
    )
