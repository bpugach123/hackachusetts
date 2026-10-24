"""Principal ports for the Feasibility port-distance sub-score (Req 9.4).

Source: USACE Waterborne Commerce Statistics Center Principal Ports, as served
by the BTS National Transportation Atlas Database (NTAD) on geo.dot.gov
(``endpoints.NTAD_PORTS``). Features are port-area polygons with the name in
``portname`` (checked against the layer's field list on 2026-10-24).

The layer is queried over the Grid bbox + :data:`PORT_MARGIN_KM` through
:func:`siting.sources.arcgis.fetch_layer` (cache + retry, paginated). A failed or
empty fetch is a run-wide DataGap; no port location is ever substituted.
Tagged PROXY + EXTERNAL ADDITION: distance to a principal port stands in for
marine-operations access, it is not a measured logistics cost.
"""

from __future__ import annotations

from typing import Any

from siting.fetch.client import FetchClient
from siting.fetch.gaps import DataGap
from siting.sources.arcgis import Envelope, Layer, fetch_layer, grid_envelope
from siting.sources.endpoints import NTAD_PORTS

SOURCE = NTAD_PORTS.source  # "NTAD_PORTS"
# Citation id (matches output/sample/metadata.json).
CITATION_ID = "bts-ports"
# Every cell gets a true nearest distance as long as it is <= this margin: any
# port outside the envelope is farther than the margin from every cell.
PORT_MARGIN_KM = 100.0
PAGE_SIZE = 1000  # layer maxRecordCount
OUT_FIELDS = "portname,port,rank,type"
NAME_FIELDS: tuple[str, ...] = ("portname", "PORTNAME", "port_name", "PORT_NAME")


def port_name(props: dict) -> str | None:
    """The port's name from its attributes, or None."""
    for key in NAME_FIELDS:
        v = props.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def fetch_ports(client: FetchClient, envelope: Envelope) -> Layer | DataGap:
    """Principal-port geometries inside ``envelope``, or a run-wide gap."""
    return fetch_layer(
        client,
        NTAD_PORTS,
        envelope,
        citation_id=CITATION_ID,
        out_fields=OUT_FIELDS,
        page_size=PAGE_SIZE,
    )


def fetch_for_cells(client: FetchClient, cells: Any) -> Layer | DataGap:
    """Ports over the Grid bbox + :data:`PORT_MARGIN_KM`."""
    return fetch_ports(client, grid_envelope(cells, PORT_MARGIN_KM))
