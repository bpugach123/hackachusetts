"""Provenance model and output validator (Req 3.1, 3.2, 3.3, 3.8).

Every factor written to Static_Output carries:
- exactly one primary tag (QUANTIFIED DATA or PROXY), plus EXTERNAL ADDITION when
  its source is not a Hackachusetts kit-equivalent dataset (see `tags_for`);
- a Citation with non-empty agency, dataset, endpoint and a YYYY-MM-DD date;
- `value is None` if and only if `gap is not None`.

Velocity factors (`velocity_*`) with a value must be labeled "modeled current" and
cite NECOFS FVCOM, Doppio, or MOM6 with a model run date. NOAA CO-OPS current
predictions are the one exception: label "predicted current (harmonic)", tagged
QUANTIFIED DATA, and cited to CO-OPS current predictions.

`validate(cells)` returns every violation as a
`ProvenanceError(cell_id, factor, message)`.
The run must get an empty list before anything is written.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Literal, get_args

from siting.fetch.gaps import GAP_REASONS, DataGap

# --------------------------------------------------------------------------- tags

Tag = Literal["QUANTIFIED DATA", "PROXY", "EXTERNAL ADDITION"]
PrimaryTag = Literal["QUANTIFIED DATA", "PROXY"]

QUANTIFIED: Tag = "QUANTIFIED DATA"
PROXY: Tag = "PROXY"
EXTERNAL_ADDITION: Tag = "EXTERNAL ADDITION"

ALL_TAGS: frozenset[str] = frozenset(get_args(Tag))
PRIMARY_TAGS: frozenset[str] = frozenset(get_args(PrimaryTag))

DateKind = Literal["model_run", "fetch"]
DATE_KINDS: frozenset[str] = frozenset(get_args(DateKind))

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MODELED_CURRENT = "modeled current"
PREDICTED_CURRENT = "predicted current (harmonic)"
VELOCITY_MODELS: tuple[str, ...] = ("NECOFS FVCOM", "Doppio", "MOM6")
VELOCITY_PREFIX = "velocity_"

# ------------------------------------------------------- kit-equivalent registry

# Sources that stand in for Hackachusetts kit datasets (design: Provenance section).
# Factors from these sources get NO EXTERNAL ADDITION tag; every other source does.
KIT_EQUIVALENT_SOURCES: frozenset[str] = frozenset(
    {
        "NECOFS_FVCOM",
        "DOPPIO",
        "MOM6",
        "NOAA_COOPS",  # tides: harcon, datums
        "NOAA_CURRENTS",  # CO-OPS current predictions
        "NDBC",
        "HIFLD_TRANSMISSION",
        "HIFLD_SUBSTATIONS",
        "MASSGIS_UTILITY",  # electric utility by town
        "NEODP_DIADROMOUS",  # diadromous fish stations
        "NEODP_VESSEL_TRAFFIC",  # NE Ocean Data Portal layers
        "NEODP",
        "EMOLT",
    }
)

# Known non-kit sources, listed so typos surface in `is_known_source`. Anything not
# in KIT_EQUIVALENT_SOURCES is treated as EXTERNAL ADDITION regardless.
EXTERNAL_SOURCES: frozenset[str] = frozenset(
    {
        "CENSUS_SHORELINE",
        "MARINECADASTRE_SUBMERGED_LANDS",
        "BOEM_SLA",
        "MARINECADASTRE_SHIPPING",
        "MARINECADASTRE_CABLES",
        "MARINECADASTRE_MPA",
        "MASSGIS_NHESP",
        "MASSGIS_LANDUSE",
        "MASSGIS_BEACHES",
        "NOAA_FISHERIES_RIGHT_WHALE",
        "NCEI_CRM",
        "NTAD_PORTS",
        "EIA",
        "CFRF",
        "HAB_HUB",
    }
)


def is_kit_equivalent(source: str) -> bool:
    return source in KIT_EQUIVALENT_SOURCES


def is_known_source(source: str) -> bool:
    return source in KIT_EQUIVALENT_SOURCES or source in EXTERNAL_SOURCES


def tags_for(primary: PrimaryTag, *sources: str) -> list[Tag]:
    """Tags for a factor derived from `sources`.

    EXTERNAL ADDITION is added when any contributing source is not kit-equivalent.
    """
    if primary not in PRIMARY_TAGS:
        raise ValueError(
            f"primary tag must be one of {sorted(PRIMARY_TAGS)}, got {primary!r}"
        )
    if not sources:
        raise ValueError("tags_for needs at least one source")
    tags: list[Tag] = [primary]
    if any(not is_kit_equivalent(s) for s in sources):
        tags.append(EXTERNAL_ADDITION)
    return tags


# ----------------------------------------------------------------------- models


@dataclass(frozen=True)
class Citation:
    id: str
    agency: str
    dataset: str
    endpoint: str
    date: str  # YYYY-MM-DD; model run date or fetch date
    date_kind: DateKind
    note: str | None = None  # e.g. "modeled current", "not blended"

    def to_dict(self) -> dict:
        out: dict = {
            "id": self.id,
            "agency": self.agency,
            "dataset": self.dataset,
            "endpoint": self.endpoint,
            "date": self.date,
            "date_kind": self.date_kind,
        }
        if self.note is not None:
            out["note"] = self.note
        return out


@dataclass
class Factor:
    name: str
    value: float | str | None
    unit: str | None
    tags: list[Tag]
    citation: Citation
    gap: DataGap | None = None  # value is None iff gap is not None
    label: str | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        """Serialize to the schema's Factor shape.

        Citation is referenced by id; optional keys are omitted.
        """
        out: dict = {
            "value": self.value,
            "unit": self.unit,
            "tags": list(self.tags),
            "citation_id": self.citation.id,
        }
        if self.label is not None:
            out["label"] = self.label
        if self.note is not None:
            out["note"] = self.note
        if self.gap is not None:
            out["gap"] = self.gap.to_dict()
        return out


@dataclass
class FactorResult:
    """What a component scorer returns for one cell (Track C → Track A contract).

    `score` is the component or sub-score (0-100) or None; `factors` are keyed by
    factor name; `gaps` includes component-level gaps; `omitted` names sub-scores
    left out of a weighted mean; `extra` carries component-specific CellRecord
    fields (e.g. `exclusion_layers`, `straddles_boundary`).
    """

    score: float | None = None
    factors: dict[str, Factor] = field(default_factory=dict)
    gaps: list[DataGap] = field(default_factory=list)
    omitted: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, factor: Factor) -> Factor:
        self.factors[factor.name] = factor
        if factor.gap is not None:
            self.gaps.append(factor.gap)
        return factor


@dataclass(frozen=True)
class ProvenanceError:
    cell_id: str
    factor: str
    message: str

    def __str__(self) -> str:
        return f"{self.cell_id} / {self.factor}: {self.message}"


class ProvenanceValidationError(Exception):
    """Raised by `check` when validation fails; carries every error (Req 3.8)."""

    def __init__(self, errors: list[ProvenanceError]):
        self.errors = errors
        lines = "\n".join(f"  {e}" for e in errors)
        super().__init__(f"{len(errors)} provenance error(s):\n{lines}")


# -------------------------------------------------------------------- validator


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _is_iso_date(v: Any) -> bool:
    if not isinstance(v, str) or not ISO_DATE_RE.match(v):
        return False
    try:
        _date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _resolve_citation(factor: Any, citations: Mapping[str, Any] | None) -> Any:
    """Factor objects carry the Citation; serialized factors carry `citation_id`."""
    if isinstance(factor, Factor):
        return factor.citation
    cit = _get(factor, "citation")
    if cit is not None:
        return cit
    cid = _get(factor, "citation_id")
    if cid is None or citations is None:
        return None
    return citations.get(cid)


def _check_factor(
    name: str, factor: Any, citations: Mapping[str, Any] | None
) -> list[str]:
    msgs: list[str] = []

    # Tags: exactly one primary tag; only known tags; no duplicates.
    tags = _get(factor, "tags")
    if not isinstance(tags, (list, tuple)) or not tags:
        msgs.append("missing provenance tag")
        tags = []
    else:
        unknown = [t for t in tags if t not in ALL_TAGS]
        if unknown:
            msgs.append(f"unknown tag(s) {unknown}")
        if len(set(tags)) != len(tags):
            msgs.append("duplicate tags")
        n_primary = sum(1 for t in tags if t in PRIMARY_TAGS)
        if n_primary != 1:
            msgs.append(
                f"expected exactly one of QUANTIFIED DATA / PROXY, found {n_primary}"
            )

    # Citation: non-empty agency/dataset/endpoint + ISO date + known date kind.
    cit = _resolve_citation(factor, citations)
    if cit is None:
        msgs.append("missing citation")
    else:
        for key in ("agency", "dataset", "endpoint"):
            if not _nonempty_str(_get(cit, key)):
                msgs.append(f"citation {key} is empty")
        if not _is_iso_date(_get(cit, "date")):
            msgs.append(f"citation date {_get(cit, 'date')!r} is not YYYY-MM-DD")
        if _get(cit, "date_kind") not in DATE_KINDS:
            msgs.append(
                f"citation date_kind {_get(cit, 'date_kind')!r} "
                f"not in {sorted(DATE_KINDS)}"
            )

    # value is None <=> gap is not None.
    value = _get(factor, "value")
    gap = _get(factor, "gap")
    if value is None and gap is None:
        msgs.append("value is null without a DataGap")
    elif value is not None and gap is not None:
        msgs.append("value present but a DataGap is attached")
    if gap is not None:
        reason = _get(gap, "reason")
        if reason not in GAP_REASONS:
            msgs.append(f"gap reason {reason!r} not in {sorted(GAP_REASONS)}")
        if not _nonempty_str(_get(gap, "source")):
            msgs.append("gap source is empty")

    # Velocity labeling (applies to values; a gap carries no velocity value).
    if name.startswith(VELOCITY_PREFIX) and value is not None:
        msgs.extend(_check_velocity(factor, tags, cit))

    return msgs


def _check_velocity(factor: Any, tags: Iterable[str], cit: Any) -> list[str]:
    label = _get(factor, "label")
    dataset = str(_get(cit, "dataset") or "") if cit is not None else ""
    agency = str(_get(cit, "agency") or "") if cit is not None else ""

    if label == PREDICTED_CURRENT:
        # NOAA CO-OPS harmonic predictions: not a model, separate rule.
        msgs = []
        primary = [t for t in tags if t in PRIMARY_TAGS]
        if primary != [QUANTIFIED]:
            msgs.append("predicted current must be tagged QUANTIFIED DATA")
        text = f"{agency} {dataset}".lower()
        if "co-ops" not in text and "current prediction" not in text:
            msgs.append("predicted current must cite NOAA CO-OPS current predictions")
        return msgs

    msgs = []
    if label != MODELED_CURRENT:
        msgs.append(
            f"velocity label must be {MODELED_CURRENT!r} "
            f"(or {PREDICTED_CURRENT!r} for NOAA predictions), got {label!r}"
        )
    if cit is not None:
        if not any(m.lower() in dataset.lower() for m in VELOCITY_MODELS):
            msgs.append(
                f"velocity citation dataset must name one of {list(VELOCITY_MODELS)}"
            )
        if _get(cit, "date_kind") != "model_run":
            msgs.append(
                "velocity citation must carry the model run date "
                "(date_kind 'model_run')"
            )
    return msgs


def _cell_parts(cell: Any) -> tuple[str, Mapping[str, Any]]:
    # Accept CellRecord-like objects/dicts or GeoJSON Features
    # (properties = CellRecord).
    props = _get(cell, "properties")
    rec = props if isinstance(props, Mapping) else cell
    cell_id = _get(rec, "cell_id") or "<unknown cell>"
    factors = _get(rec, "factors") or {}
    return str(cell_id), factors


def validate(
    cells: Iterable[Any], citations: Iterable[Any] | Mapping[str, Any] | None = None
) -> list[ProvenanceError]:
    """Return every provenance violation across `cells` (empty list = OK to write).

    `cells` items expose `cell_id` and `factors` ({name: Factor | schema dict}),
    or are GeoJSON Features whose `properties` do. `citations` (list or
    {id: Citation}) resolves `citation_id` on serialized factors; Factor objects
    carry their own Citation.
    """
    cit_map: dict[str, Any] | None = None
    if citations is not None:
        if isinstance(citations, Mapping):
            cit_map = dict(citations)
        else:
            cit_map = {_get(c, "id"): c for c in citations}

    errors: list[ProvenanceError] = []
    for cell in cells:
        cell_id, factors = _cell_parts(cell)
        for name, factor in factors.items():
            for msg in _check_factor(name, factor, cit_map):
                errors.append(ProvenanceError(cell_id, name, msg))
    return errors


def check(
    cells: Iterable[Any], citations: Iterable[Any] | Mapping[str, Any] | None = None
) -> None:
    """Raise ProvenanceValidationError listing every (cell_id, factor) if invalid."""
    errors = validate(cells, citations)
    if errors:
        raise ProvenanceValidationError(errors)
