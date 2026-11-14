"""Portable catalog decisions over generated data and the stock draft transaction."""
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from core.generation_prompt_catalog import (PromptCatalog, capture_prompt_catalog, catalog_draft, catalog_from_plan)
from core.generation_planning import (GenerationPlan, GenerationPlanValidationError, save_plan, load_plan_with_digest, outfit_association_scope)
from core.generation_saved_work import import_selected_saved_work


def payload():
    return {"version": 1, "tags": [{"id": "tag-blue", "display": "blue dress", "aliases": ["azure garment"]}],
            "characters": [{"id": "char-one", "name": "One"}, {"id": "char-two", "name": "Two"}],
            "outfits": [dict(id="own", name="Own", text="blue dress", reviewed=True, associated_character_ids=["char-one"]),
                        dict(id="other", name="Other", text="red hat", reviewed=True, associated_character_ids=["char-two"]),
                        dict(id="generic", name="Generic", text="green scarf", reviewed=True, associated_character_ids=[])]}


def test_catalog_roundtrip_uses_stock_guarded_import_without_changing_export(tmp_path):
    catalog = PromptCatalog.from_payload(payload())
    carrier = tmp_path / "catalog.json"
    save_plan(carrier, catalog_draft(catalog))
    before = carrier.read_bytes()
    copied = import_selected_saved_work(tmp_path / "manager", "drafts", carrier)
    plan, token = load_plan_with_digest(copied)
    assert catalog_from_plan(plan).fingerprint == catalog.fingerprint
    revised = plan.revise({"positive_prompt": "deliberate image request"}, source="user")
    save_plan(copied, revised, expected_digest=token)
    reopened, _ = load_plan_with_digest(copied)
    assert catalog_from_plan(reopened).payload == payload()
    assert reopened.request.positive_prompt == "deliberate image request"
    assert carrier.read_bytes() == before
    assert copied != carrier


def test_capture_projects_only_canonical_prompt_fields_and_relationships():
    raw = payload()
    knowledge = NS(tags={"tag-blue": NS(id="tag-blue", display="blue dress", extras={"private_path": "excluded"})},
                   identity_names=lambda record: ("blue dress", "azure garment"))
    outfits = {row["id"]: NS(id=row["id"], name=row["name"], sdxl_tags=row["text"].split(", "), extras={"unused": "excluded"})
               for row in raw["outfits"]}
    characters = NS(characters={row["id"]: NS(**row) for row in raw["characters"]}, outfits=outfits,
        associated_character_ids=lambda identity: next(row["associated_character_ids"] for row in raw["outfits"] if row["id"] == identity))
    result = capture_prompt_catalog(knowledge, characters)
    assert "excluded" not in result._encoded.decode()
    assert result.identity_names(result.tags["tag-blue"]) == ("blue dress", "blue dress", "azure garment")
    groups = outfit_association_scope("char-one", result.characters, result.outfits)["groups"]
    assert [row["id"] for row in groups["associated"]] == ["own"]
    assert [row["id"] for row in groups["other"]] == ["other"]
    assert [row["id"] for row in groups["generic"]] == ["generic"]


@pytest.mark.parametrize("change", [
    lambda p: p.update(version=True),
    lambda p: p["tags"].append(deepcopy(p["tags"][0])),
    lambda p: p["characters"].append(deepcopy(p["characters"][0])),
    lambda p: p["outfits"][0].update(reviewed=False),
    lambda p: p["outfits"][0].update(image_path="excluded"),
    lambda p: p["tags"][0].update(aliases="not a list"),
])
def test_malformed_catalog_refused_before_it_can_be_adopted(change):
    value = payload(); change(value)
    plan = GenerationPlan.create(None).revise({"target_context": {"organizer_prompt_catalog": value}})
    with pytest.raises(GenerationPlanValidationError):
        catalog_from_plan(plan)


def test_plain_draft_has_no_catalog_and_large_projection_is_not_truncated(monkeypatch):
    assert catalog_from_plan(GenerationPlan.create(None)) is None
    monkeypatch.setattr("core.generation_prompt_catalog.MAX_TAGS", 0)
    with pytest.raises(GenerationPlanValidationError, match="bounded"):
        PromptCatalog.from_payload(payload())
