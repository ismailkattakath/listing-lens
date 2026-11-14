import json

import pytest
from conftest import EXAMPLES, ROOT
from scrape_harness import contracts

from listing_lens.commands import TEXT_HINT


@pytest.mark.parametrize(
    ("name", "file"),
    [
        ("listing.v1", "listing.example.json"),
        ("observation.v1", "observation.example.json"),
        ("geocode.v1", "geocode.example.json"),  # cross-file $ref into listing.v1
        ("finding-analyst.input.v1", "finding-analyst.input.example.json"),
    ],
)
def test_examples_validate(name, file):
    contracts.validate(name, json.loads((EXAMPLES / file).read_text()))


@pytest.mark.parametrize("file", sorted(p.name for p in (EXAMPLES / "invalid").glob("*.json")))
def test_invalid_examples_fail(file):
    name = file.split(".")[0] + ".v1"
    with pytest.raises(contracts.ContractError):
        contracts.validate(name, json.loads((EXAMPLES / "invalid" / file).read_text()))


def test_subschema_reference():
    contracts.validate(TEXT_HINT, {"raw": "X St & Y Ave", "kind": "intersection", "extractor": "t"})
    with pytest.raises(contracts.ContractError):
        contracts.validate(TEXT_HINT, {"raw": "x", "kind": "vibes", "extractor": "t"})


def test_agent_input_register_items_are_findings():
    rec = json.loads((EXAMPLES / "finding-analyst.input.example.json").read_text())
    rec["register"] = [{"id": "H1"}]
    assert contracts.errors("finding-analyst.input.v1", rec)


def test_a_panel_revisit_is_a_fetch_reason():
    rec = json.loads((EXAMPLES / "listing.example.json").read_text()) | {"fetch_reason": "panel"}
    contracts.validate("listing.v1", rec)
    with pytest.raises(contracts.ContractError):
        contracts.validate("listing.v1", rec | {"fetch_reason": "panel_typo"})


def test_a_recruit_fetch_is_a_valid_listing_reason():
    rec = json.loads((EXAMPLES / "listing.example.json").read_text())
    rec["fetch_reason"] = "recruit"  # fetch-ads --select recruit (a one-off sample of never-opened ads)
    contracts.validate("listing.v1", rec)
    rec["fetch_reason"] = "recruited"
    with pytest.raises(contracts.ContractError):
        contracts.validate("listing.v1", rec)


@pytest.mark.parametrize(("name", "example"), [("listing.v1", "listing"), ("observation.v1", "observation")])
def test_without_the_sources_sub_schema_a_record_errors_never_passes(name, example):
    """listing.v1's `site` and observation.v1's `card` apply the source's sub-schema by $ref (the site file's
    [pipeline.plugins] schema_dirs). With NO plugin providing that $id, validation must ERROR, never pass."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.exceptions import Unresolvable

    core = ROOT / "schemas"
    docs = [json.loads(p.read_text()) for p in sorted(core.glob("*.schema.json"))]
    registry = Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs)
    schema = next(d for d in docs if d["title"] == name)
    record = json.loads((core / "examples" / f"{example}.example.json").read_text())
    with pytest.raises(Unresolvable):
        list(Draft202012Validator(schema, registry=registry).iter_errors(record))
