"""HIFLD transmission lines and substations for Grid Proximity (Req 6.1, 6.6).

Both layers are queried with ``returnGeometry=true`` over the Grid bbox + 50 km
(:func:`siting.sources.arcgis.grid_envelope`), paginated on
``exceededTransferLimit``, primary URL first and then the mirror from
:mod:`siting.sources.endpoints`.

HIFLD Open was retired in Aug 2025; transmission lines come from archived
copies (see ``endpoints.HIFLD_TRANSMISSION``). The substation layer is
UNRESOLVED, so it yields a Data_Gap. Substations are never derived from line
endpoints (that would fabricate locations).
"""

from __future__ import annotations

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.sources.arcgis import Envelope, Layer, fetch_layer
from siting.sources.endpoints import HIFLD_SUBSTATIONS, HIFLD_TRANSMISSION

TRANSMISSION_SOURCE = HIFLD_TRANSMISSION.source  # "HIFLD_TRANSMISSION"
SUBSTATIONS_SOURCE = HIFLD_SUBSTATIONS.source  # "HIFLD_SUBSTATIONS"

# Citation ids (match output/sample/metadata.json).
TRANSMISSION_CITATION_ID = "hifld-transmission"
SUBSTATIONS_CITATION_ID = "hifld-substations"

TRANSMISSION_FIELDS = "ID,OWNER,VOLTAGE,VOLT_CLASS,STATUS"
SUBSTATION_FIELDS = "*"


def fetch_transmission(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """Transmission line geometries inside ``envelope``, or a run-wide gap."""
    return fetch_layer(
        client,
        HIFLD_TRANSMISSION,
        envelope,
        citation_id=TRANSMISSION_CITATION_ID,
        out_fields=TRANSMISSION_FIELDS,
    )


def fetch_substations(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """Substation points inside ``envelope``, or a run-wide gap."""
    return fetch_layer(
        client,
        HIFLD_SUBSTATIONS,
        envelope,
        citation_id=SUBSTATIONS_CITATION_ID,
        out_fields=SUBSTATION_FIELDS,
    )
