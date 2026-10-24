"""Unit tests for siting.provenance (Req 3.1, 3.2, 3.3, 3.8)."""

from dataclasses import replace

import pytest

from siting.fetch.gaps import DataGap
from siting.provenance import (
    EXTERNAL_ADDITION,
    PROXY,
    QUANTIFIED,
    Citation,
    Factor,
    FactorResult,
    ProvenanceError,
    ProvenanceValidationError,
    check,
    tags_for,
    validate,
)

NECOFS = Citation(
    id="necofs-massbay",
    agency="UMass Dartmouth SMAST",
    dataset="NECOFS FVCOM MassBay forecast",
    endpoint="http://www.smast.umassd.edu:8080/thredds/dodsC/FVCOM/NECOFS/Forecasts/NECOFS_FVCOM_OCEAN_MASSBAY_FORECAST.nc",
    date="2025-01-01",
    date_kind="model_run",
)
CURRENTS = Citation(
    id="coops-currents",
    agency="NOAA CO-OPS",
    dataset="Current predictions",
    endpoint="https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
    date="2025-01-02",
    date_kind="fetch",
)
HIFLD = Citation(
    id="hifld-tx",
    agency="DHS HIFLD",
    dataset="Electric Power Transmission Lines",
    endpoint="https://services1.arcgis.com/.../FeatureServer/0/query",
    date="2025-01-02",
    date_kind="fetch",
)


def velocity(**kw) -> Factor:
    base = Factor(
        "velocity_peak", 2.1, "m/s", [QUANTIFIED], NECOFS, label="modeled current"
    )
    return replace(base, **kw)


def dist(**kw) -> Factor:
    base = Factor("dist_transmission_km", 3.25, "km", [QUANTIFIED], HIFLD)
    return replace(base, **kw)


def cell(*factors: Factor, cell_id="C1609-0001-0002"):
    return {"cell_id": cell_id, "factors": {f.name: f for f in factors}}


# ------------------------------------------------------------------ registry


def test_kit_equivalent_source_gets_no_external_addition():
    assert tags_for(QUANTIFIED, "HIFLD_TRANSMISSION") == [QUANTIFIED]
    assert tags_for(PROXY, "EMOLT") == [PROXY]


def test_non_kit_source_gets_external_addition():
    assert tags_for(QUANTIFIED, "NOAA_FISHERIES_RIGHT_WHALE") == [
        QUANTIFIED,
        EXTERNAL_ADDITION,
    ]
    assert tags_for(PROXY, "UNLISTED_SOURCE") == [PROXY, EXTERNAL_ADDITION]


def test_mixed_sources_get_external_addition():
    # e.g. gross_usd = energy (kit) x EIA price (external)
    assert tags_for(PROXY, "NECOFS_FVCOM", "EIA") == [PROXY, EXTERNAL_ADDITION]


def test_tags_for_rejects_bad_primary():
    with pytest.raises(ValueError):
        tags_for(EXTERNAL_ADDITION, "EIA")  # type: ignore[arg-type]


# ------------------------------------------------------------- serialization


def test_factor_serializes_to_schema_shape():
    assert velocity().to_dict() == {
        "value": 2.1,
        "unit": "m/s",
        "tags": ["QUANTIFIED DATA"],
        "citation_id": "necofs-massbay",
        "label": "modeled current",
    }
    gap = DataGap("dist_transmission_km", "HIFLD transmission", "fetch failure")
    assert dist(value=None, gap=gap).to_dict() == {
        "value": None,
        "unit": "km",
        "tags": ["QUANTIFIED DATA"],
        "citation_id": "hifld-tx",
        "gap": {
            "factor": "dist_transmission_km",
            "source": "HIFLD transmission",
            "reason": "fetch failure",
        },
    }


def test_datagap_rejects_unknown_reason():
    with pytest.raises(ValueError):
        DataGap("depth", "NCEI CRM", "timeout")  # type: ignore[arg-type]


def test_factor_result_collects_gaps():
    gap = DataGap("dist_transmission_km", "HIFLD transmission", "empty response")
    r = FactorResult(score=None)
    r.add(dist(value=None, gap=gap))
    assert r.factors["dist_transmission_km"].gap == gap
    assert r.gaps == [gap]


# ---------------------------------------------------------------- validator


def test_valid_cells_pass():
    gap = DataGap(
        "dist_transmission_km",
        "HIFLD transmission",
        "no coverage",
        detail="outside envelope",
    )
    cells = [
        cell(velocity(), dist()),
        cell(dist(value=None, gap=gap), cell_id="C1609-0002-0002"),
    ]
    assert validate(cells) == []
    check(cells)  # does not raise


@pytest.mark.parametrize(
    "factor, fragment",
    [
        (dist(tags=[]), "missing provenance tag"),
        (dist(tags=[EXTERNAL_ADDITION]), "exactly one"),
        (dist(tags=[QUANTIFIED, PROXY]), "exactly one"),
        (dist(citation=replace(HIFLD, agency="")), "agency"),
        (dist(citation=replace(HIFLD, endpoint=" ")), "endpoint"),
        (dist(citation=replace(HIFLD, date="2025/01/02")), "YYYY-MM-DD"),
        (dist(citation=replace(HIFLD, date="2025-13-40")), "YYYY-MM-DD"),
        (dist(value=None), "null without a DataGap"),
        (
            dist(gap=DataGap("dist_transmission_km", "HIFLD", "fetch failure")),
            "DataGap is attached",
        ),
    ],
)
def test_each_rule_reports_cell_and_factor(factor, fragment):
    errors = validate([cell(velocity(), factor)])
    assert errors, fragment
    assert all(
        e.cell_id == "C1609-0001-0002" and e.factor == "dist_transmission_km"
        for e in errors
    )
    assert any(fragment in e.message for e in errors)


def test_model_velocity_requires_label_model_name_and_run_date():
    bad_label = validate([cell(velocity(label=None))])
    assert any("modeled current" in e.message for e in bad_label)

    bad_model = validate(
        [cell(velocity(citation=replace(NECOFS, dataset="Some ocean model")))]
    )
    assert any("NECOFS FVCOM" in e.message for e in bad_model)

    bad_kind = validate([cell(velocity(citation=replace(NECOFS, date_kind="fetch")))])
    assert any("model run date" in e.message for e in bad_kind)

    for model in ("Doppio ROMS", "MOM6 NWA12 hindcast"):
        assert validate([cell(velocity(citation=replace(NECOFS, dataset=model)))]) == []


def test_noaa_predictions_exception():
    ok = velocity(label="predicted current (harmonic)", citation=CURRENTS)
    assert validate([cell(ok)]) == []

    proxy = replace(ok, tags=[PROXY])
    assert any("QUANTIFIED DATA" in e.message for e in validate([cell(proxy)]))

    wrong_source = replace(ok, citation=HIFLD)
    assert any("CO-OPS" in e.message for e in validate([cell(wrong_source)]))


def test_velocity_gap_needs_no_label():
    gap = DataGap("velocity_peak", "NECOFS FVCOM", "no coverage")
    assert validate([cell(velocity(value=None, gap=gap, label=None))]) == []


def test_serialized_factors_resolve_citation_ids():
    feature = {
        "type": "Feature",
        "properties": {
            "cell_id": "C1609-0003-0004",
            "factors": {"velocity_peak": velocity().to_dict()},
        },
    }
    assert validate([feature], citations=[NECOFS.to_dict()]) == []
    errors = validate([feature], citations=[])
    assert errors == [
        ProvenanceError("C1609-0003-0004", "velocity_peak", "missing citation")
    ]


def test_check_lists_every_error():
    cells = [
        cell(dist(tags=[]), cell_id="A"),
        cell(dist(citation=replace(HIFLD, dataset="")), cell_id="B"),
    ]
    with pytest.raises(ProvenanceValidationError) as exc:
        check(cells)
    assert {(e.cell_id, e.factor) for e in exc.value.errors} == {
        ("A", "dist_transmission_km"),
        ("B", "dist_transmission_km"),
    }
    assert "A / dist_transmission_km" in str(exc.value)
