import json
import time
import zipfile
from pathlib import Path

import pytest

from core.default_sync import sync_promoted_defaults
from core.knowledge_pack import (
    export_knowledge_pack,
    import_knowledge_pack,
    plan_knowledge_pack_import,
)
from core.consumer_policies import (
    EXACT_TAG,RELATIONSHIP_KIND,SEARCH_ORGANIZATION,ConsumerPolicyRecord,
    consumer_policy_record_id,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _root(path: Path) -> Path:
    defaults = path / "data" / "defaults"
    user = path / "data" / "user"
    defaults.mkdir(parents=True)
    user.mkdir(parents=True)
    _write(defaults / "foundation_tags.json", {
        "schema_version": 5,
        "tags": [
            {"id": "blue", "display": "blue", "category": "colors"},
            {"id": "red", "display": "red", "category": "colors"},
            {"id": "purple", "display": "purple", "category": "colors"},
        ],
        "merge_rules": [],
    })
    _write(defaults / "community_tags.json", {"schema_version": 1, "tags": []})
    _write(defaults / "promoted_tags.json", {
        "schema_version": 1,
        "tags": [],
        "merge_rules": [{
            "id": "rule_color_mix",
            "source_tags": ["blue", "red"],
            "target_tag": "purple",
        }],
        "removed_tag_ids": [],
        "merged_tag_ids": {},
    })
    _write(defaults / "categories.json", {
        "schema_version": 5,
        "categories": [{
            "id": "colors", "name": "Colors", "description": "Shipped colors",
            "color": "#112233", "color_policy": "own",
        }],
    })
    _write(defaults / "patterns.json", {
        "schema_version": 1,
        "patterns": [{
            "id": "color_word", "name": "Color word", "regex": "blue|red",
            "category": "colors", "enabled": True,
        }],
    })
    _write(defaults / "series.json", {
        "schema_version": 3,
        "series": [{"id": "main_source", "name": "Main Source"}],
    })
    _write(defaults / "characters.json", {
        "schema_version": 5,
        "characters": [{
            "id": "hero", "name": "Hero", "series_id": "main_source",
            "identity_type": "canonical",
        }],
    })
    _write(defaults / "outfits.json", {
        "schema_version": 3,
        "outfits": [{
            "id": "hero_coat", "name": "Hero Coat",
            "series_id": "main_source", "character_ids": ["hero"],
        }],
    })
    _write(defaults / "relationships.json", {
        "schema_version": 1, "relationships": [],
    })
    sync_promoted_defaults(defaults, user)
    return path


def _make_complete_user_patch(root: Path) -> None:
    user = root / "data" / "user"
    tags = _read(user / "tags.json")
    next(item for item in tags["tags"] if item["id"] == "blue")["notes"] = "Reviewed blue"
    tags["tags"].append({
        "id": "teal", "display": "teal", "category": "personal_colors",
        "future_tag_field": {"kept": True},
    })
    tags["merge_rules"] = [
        item for item in tags["merge_rules"] if item["id"] != "rule_color_mix"
    ]
    tags["merge_rules"].append({
        "id": "rule_teal_blue", "source_tags": ["teal", "blue"],
        "target_tag": "purple", "future_rule_field": "kept",
    })
    tags["disabled_default_merge_rule_ids"] = ["rule_color_mix"]
    _write(user / "tags.json", tags)

    _write(user / "categories.json", {
        "schema_version": 5,
        "categories": [
            {
                "id": "colors", "name": "Colors", "description": "Reviewed colors",
                "color": "#112233", "color_policy": "own",
            },
            {
                "id": "personal_colors", "name": "Personal colors",
                "future_category_field": ["kept"],
            },
        ],
        "disabled_default_ids": [],
    })
    _write(user / "patterns.json", {
        "schema_version": 1,
        "patterns": [{
            "id": "teal_word", "name": "Teal word", "regex": "teal",
            "category": "personal_colors", "future_pattern_field": True,
        }],
        "disabled_default_ids": ["color_word"],
    })

    series = _read(user / "series.json")
    series["series"][0]["original_style_tags"] = ["reviewed style"]
    series["series"].append({
        "id": "side_source", "name": "Side Source", "future_source_field": 7,
    })
    _write(user / "series.json", series)

    characters = _read(user / "characters.json")
    characters["characters"][0]["creator"] = "Reviewed creator"
    characters["characters"].append({
        "id": "side_hero", "name": "Side Hero", "series_id": "side_source",
        "identity_type": "canonical", "future_character_field": {"kept": True},
    })
    _write(user / "characters.json", characters)

    outfits = _read(user / "outfits.json")
    outfits["outfits"][0]["style_tags"] = ["formal"]
    outfits["outfits"].append({
        "id": "side_coat", "name": "Side Coat", "series_id": "side_source",
        "character_ids": ["side_hero"], "future_outfit_field": "kept",
    })
    _write(user / "outfits.json", outfits)


def test_schema_eight_round_trips_every_non_relationship_record_lifecycle(tmp_path):
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    _make_complete_user_patch(source)

    pack = export_knowledge_pack(source, source / "records.zip")
    with zipfile.ZipFile(pack) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        tags_patch = json.loads(archive.read("data/user/tags.json"))
    assert manifest["schema_version"] == 8
    assert tags_patch["record_base_fingerprints"]["tags"]["blue"] != "missing"
    assert tags_patch["record_base_fingerprints"]["tags"]["teal"] == "missing"
    assert tags_patch["disabled_default_merge_rule_ids"] == ["rule_color_mix"]

    plan = plan_knowledge_pack_import(destination, pack)
    assert plan.can_apply
    outcomes = {(item.kind, item.stable_id): item.outcome for item in plan.record_decisions}
    assert outcomes[("tag", "blue")] == "override"
    assert outcomes[("tag", "teal")] == "add"
    assert outcomes[("merge_rule", "rule_color_mix")] == "disable_default"
    assert outcomes[("pattern", "color_word")] == "disable_default"
    assert outcomes[("category", "colors")] == "override"
    assert outcomes[("series", "main_source")] == "override"
    assert outcomes[("character", "hero")] == "override"
    assert outcomes[("outfit", "hero_coat")] == "override"

    result = import_knowledge_pack(destination, pack, plan=plan)
    assert result["tags"]["added"] == 1
    assert result["tags"]["overridden"] == 1
    assert result["merge_rules"]["disabled_defaults"] == 1
    assert result["patterns"]["disabled_defaults"] == 1
    assert result["categories"]["overridden"] == 1
    assert result["series"]["overridden"] == 1
    assert result["characters"]["overridden"] == 1
    assert result["outfits"]["overridden"] == 1
    assert result["backup"]["path"]

    tags = _read(destination / "data" / "user" / "tags.json")
    assert next(item for item in tags["tags"] if item["id"] == "teal")[
        "future_tag_field"
    ] == {"kept": True}
    assert tags["disabled_default_merge_rule_ids"] == ["rule_color_mix"]
    assert all(item["id"] != "rule_color_mix" for item in tags["merge_rules"])
    categories = _read(destination / "data" / "user" / "categories.json")
    assert next(
        item for item in categories["categories"] if item["id"] == "personal_colors"
    )["future_category_field"] == ["kept"]

    repeated = plan_knowledge_pack_import(destination, pack)
    assert repeated.can_apply
    assert repeated.record_decisions
    assert {item.outcome for item in repeated.record_decisions} == {"already_present"}
    unchanged = import_knowledge_pack(destination, pack, plan=repeated)
    assert unchanged["backup"]["path"] == ""


def test_record_patch_holds_when_default_base_or_local_override_diverged(tmp_path):
    source = _root(tmp_path / "source")
    changed_default = _root(tmp_path / "changed-default")
    local_override = _root(tmp_path / "local-override")
    _write(source / "data" / "user" / "categories.json", {
        "schema_version": 5,
        "categories": [{
            "id": "colors", "name": "Colors", "description": "Pack review",
            "color": "#112233", "color_policy": "own",
        }],
        "disabled_default_ids": [],
    })
    pack = export_knowledge_pack(source, source / "category-override.zip")

    defaults = _read(changed_default / "data" / "defaults" / "categories.json")
    defaults["categories"][0]["description"] = "New shipped revision"
    _write(changed_default / "data" / "defaults" / "categories.json", defaults)
    changed_plan = plan_knowledge_pack_import(changed_default, pack)
    changed = next(item for item in changed_plan.record_decisions if item.kind == "category")
    assert changed.outcome == "hold"
    assert "shipped default changed" in changed.reason

    _write(local_override / "data" / "user" / "categories.json", {
        "schema_version": 5,
        "categories": [{
            "id": "colors", "name": "Colors", "description": "My local review",
            "color": "#112233", "color_policy": "own",
        }],
        "disabled_default_ids": [],
    })
    local_plan = plan_knowledge_pack_import(local_override, pack)
    local = next(item for item in local_plan.record_decisions if item.kind == "category")
    assert local.outcome == "hold"
    assert "different local Category" in local.reason


def test_record_dependencies_and_semantic_identities_remap_cross_install(tmp_path):
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    source_user = source / "data" / "user"
    destination_user = destination / "data" / "user"

    source_records = {
        "categories.json": ("categories", [{"id": "source_group", "name": "Shared group"}]),
        "patterns.json": ("patterns", [{
            "id": "source_pattern", "name": "Shared pattern", "regex": "shared",
            "category": "source_group",
        }]),
        "series.json": ("series", [{"id": "source_work", "name": "Shared Work"}]),
        "characters.json": ("characters", [{
            "id": "source_hero", "name": "Shared Hero", "series_id": "source_work",
            "identity_type": "canonical",
        }]),
        "outfits.json": ("outfits", [{
            "id": "source_coat", "name": "Shared Coat", "series_id": "source_work",
            "character_ids": ["source_hero"],
        }]),
    }
    destination_records = {
        "categories.json": ("categories", [{"id": "local_group", "name": "Shared group"}]),
        "patterns.json": ("patterns", [{
            "id": "local_pattern", "name": "Shared pattern", "regex": "shared",
            "category": "local_group",
        }]),
        "series.json": ("series", [{"id": "local_work", "name": "Shared Work"}]),
        "characters.json": ("characters", [{
            "id": "local_hero", "name": "Shared Hero", "series_id": "local_work",
            "identity_type": "canonical",
        }]),
        "outfits.json": ("outfits", [{
            "id": "local_coat", "name": "Shared Coat", "series_id": "local_work",
            "character_ids": ["local_hero"],
        }]),
    }
    for filename, (key, records) in source_records.items():
        _write(source_user / filename, {
            "schema_version": 5, key: records, "disabled_default_ids": [],
        })
    for filename, (key, records) in destination_records.items():
        _write(destination_user / filename, {
            "schema_version": 5, key: records, "disabled_default_ids": [],
        })
    source_tags = _read(source_user / "tags.json")
    source_tags["tags"].append({
        "id": "source_shared", "display": "shared tag", "category": "source_group",
    })
    source_tags["merge_rules"].append({
        "id": "source_rule", "source_tags": ["shared tag", "blue"],
        "target_tag": "purple",
    })
    _write(source_user / "tags.json", source_tags)
    destination_tags = _read(destination_user / "tags.json")
    destination_tags["tags"].append({
        "id": "local_shared", "display": "shared tag", "category": "local_group",
    })
    destination_tags["merge_rules"].append({
        "id": "local_rule", "source_tags": ["shared tag", "blue"],
        "target_tag": "purple",
    })
    _write(destination_user / "tags.json", destination_tags)

    pack = export_knowledge_pack(source, source / "remap.zip")
    plan = plan_knowledge_pack_import(destination, pack)
    assert plan.can_apply
    selected = {
        item.kind: (item.stable_id, item.outcome) for item in plan.record_decisions
        if item.label.casefold().startswith("shared") or item.kind == "merge_rule"
    }
    assert selected["tag"] == ("local_shared", "already_present")
    assert selected["category"] == ("local_group", "already_present")
    assert selected["pattern"] == ("local_pattern", "already_present")
    assert selected["series"] == ("local_work", "already_present")
    assert selected["character"] == ("local_hero", "already_present")
    assert selected["outfit"] == ("local_coat", "already_present")
    assert selected["merge_rule"] == ("local_rule", "already_present")


def test_schema_seven_record_import_remains_legacy_compatible(tmp_path):
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    tags = _read(source / "data" / "user" / "tags.json")
    tags["tags"].append({"id": "teal", "display": "teal", "category": "colors"})
    _write(source / "data" / "user" / "tags.json", tags)
    current = export_knowledge_pack(source, source / "current.zip")
    legacy = source / "legacy.zip"
    with zipfile.ZipFile(current, "r") as incoming, zipfile.ZipFile(
        legacy, "w", zipfile.ZIP_DEFLATED
    ) as outgoing:
        for name in incoming.namelist():
            payload = incoming.read(name)
            if name == "manifest.json":
                manifest = json.loads(payload)
                manifest["schema_version"] = 7
                payload = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
            outgoing.writestr(name, payload)

    plan = plan_knowledge_pack_import(destination, legacy)
    assert plan.can_apply
    assert not plan.record_decisions
    result = import_knowledge_pack(destination, legacy, plan=plan)
    assert result["tags"]["added"] == 1
    assert any(
        item["id"] == "teal"
        for item in _read(destination / "data" / "user" / "tags.json")["tags"]
    )


def test_twenty_thousand_record_preview_is_exact_and_bounded(tmp_path):
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    tags = _read(source / "data" / "user" / "tags.json")
    tags["tags"].extend(
        {
            "id": f"portable_{index:05d}",
            "display": f"Portable {index:05d}",
            "category": "colors",
        }
        for index in range(20_000)
    )
    _write(source / "data" / "user" / "tags.json", tags)
    pack = export_knowledge_pack(source, source / "large.zip")

    started = time.perf_counter()
    plan = plan_knowledge_pack_import(destination, pack)
    elapsed = time.perf_counter() - started

    tag_decisions = [item for item in plan.record_decisions if item.kind == "tag"]
    assert plan.can_apply
    assert len(tag_decisions) == 20_000
    assert all(item.outcome == "add" for item in tag_decisions)
    assert elapsed < 10.0

    tags["tags"].append({
        "id": "portable_over_limit",
        "display": "Portable over limit",
        "category": "colors",
    })
    _write(source / "data" / "user" / "tags.json", tags)
    with pytest.raises(ValueError, match="20,000 non-relationship"):
        export_knowledge_pack(source, source / "too-large.zip")


def _consumer_policy(selector_type,selector_id,mode="discover_related",**extras):
    return ConsumerPolicyRecord(
        consumer_policy_record_id(selector_type,selector_id,SEARCH_ORGANIZATION),
        selector_type,selector_id,SEARCH_ORGANIZATION,mode,
        "Reviewed portable policy.",layer="user",reviewed=True,
        extras=extras,
    ).to_dict()


def test_schema_nine_round_trips_optional_consumer_policy_patch(tmp_path):
    source=_root(tmp_path/"source"); destination=_root(tmp_path/"destination")
    policy=_consumer_policy(
        RELATIONSHIP_KIND,"specificity",future_policy_field={"kept":True},
    )
    _write(source/"data"/"user"/"consumer_policies.json",{
        "schema_version":1,"policy_version":1,"layer":"user",
        "future_local_root":{"preserved_at_source":True},"policies":[policy],
    })

    pack=export_knowledge_pack(source,source/"policy.zip")
    with zipfile.ZipFile(pack) as archive:
        manifest=json.loads(archive.read("manifest.json"))
        patch=json.loads(archive.read("data/user/consumer_policies.json"))
    assert manifest["schema_version"]==9
    assert patch["policies"][0]["future_policy_field"]=={"kept":True}

    plan=plan_knowledge_pack_import(destination,pack)
    decision=next(item for item in plan.record_decisions if item.kind=="consumer_policy")
    assert plan.can_apply
    assert decision.outcome=="add"
    result=import_knowledge_pack(destination,pack,plan=plan)
    restored=_read(destination/"data"/"user"/"consumer_policies.json")
    assert result["consumer_policies"]["added"]==1
    assert restored["layer"]=="user"
    assert restored["future_local_root"]=={"preserved_at_source":True}
    assert restored["policies"][0]["future_policy_field"]=={"kept":True}


def test_consumer_policy_pack_remaps_exact_tag_and_holds_local_override(tmp_path):
    source=_root(tmp_path/"source"); destination=_root(tmp_path/"destination")
    source_tags=_read(source/"data"/"user"/"tags.json")
    source_tags["tags"].append({"id":"source_teal","display":"teal","category":"colors"})
    _write(source/"data"/"user"/"tags.json",source_tags)
    local_tags=_read(destination/"data"/"user"/"tags.json")
    local_tags["tags"].append({"id":"local_teal","display":"teal","category":"colors"})
    _write(destination/"data"/"user"/"tags.json",local_tags)
    source_policy=_consumer_policy(EXACT_TAG,"source_teal","canonicalize_identity")
    _write(source/"data"/"user"/"consumer_policies.json",{
        "schema_version":1,"layer":"user","policies":[source_policy],
    })
    pack=export_knowledge_pack(source,source/"tag-policy.zip")
    plan=plan_knowledge_pack_import(destination,pack)
    decision=next(item for item in plan.record_decisions if item.kind=="consumer_policy")
    assert decision.outcome=="add"
    assert decision.stable_id==consumer_policy_record_id(EXACT_TAG,"local_teal",SEARCH_ORGANIZATION)
    import_knowledge_pack(destination,pack,plan=plan)
    restored=_read(destination/"data"/"user"/"consumer_policies.json")["policies"][0]
    assert restored["selector"]["id"]=="local_teal"

    source_policy["mode"]="discover_related"
    _write(source/"data"/"user"/"consumer_policies.json",{
        "schema_version":1,"layer":"user","policies":[source_policy],
    })
    changed=export_knowledge_pack(source,source/"changed-tag-policy.zip")
    changed_plan=plan_knowledge_pack_import(destination,changed)
    changed_decision=next(item for item in changed_plan.record_decisions if item.kind=="consumer_policy")
    assert changed_decision.outcome=="hold"
    assert "different local Consumer policy" in changed_decision.reason


def test_malformed_or_downgraded_policy_section_holds_the_whole_pack(tmp_path):
    source=_root(tmp_path/"source"); destination=_root(tmp_path/"destination")
    _write(source/"data"/"user"/"consumer_policies.json",{
        "schema_version":1,"layer":"user",
        "policies":[_consumer_policy(RELATIONSHIP_KIND,"specificity")],
    })
    pack=export_knowledge_pack(source,source/"policy.zip")
    tampered=source/"tampered.zip"
    with zipfile.ZipFile(pack) as archive:
        files={name:archive.read(name) for name in archive.namelist()}
    policy=json.loads(files["data/user/consumer_policies.json"])
    policy["policies"][0]["consumer"]="future_consumer"
    files["data/user/consumer_policies.json"]=(json.dumps(policy)+"\n").encode()
    manifest=json.loads(files["manifest.json"])
    files["manifest.json"]=(json.dumps(manifest)+"\n").encode()
    with zipfile.ZipFile(tampered,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,payload in files.items():archive.writestr(name,payload)
    plan=plan_knowledge_pack_import(destination,tampered)
    assert not plan.can_apply
    assert any("consumer-policy patch is not safe" in issue for issue in plan.issues)

    manifest["schema_version"]=8
    files["manifest.json"]=(json.dumps(manifest)+"\n").encode()
    downgraded=source/"downgraded.zip"
    with zipfile.ZipFile(downgraded,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,payload in files.items():archive.writestr(name,payload)
    downgraded_plan=plan_knowledge_pack_import(destination,downgraded)
    assert not downgraded_plan.can_apply
    assert any("schema 9" in issue for issue in downgraded_plan.issues)


def _rewrite_record_pack(pack, filename, content):
    with zipfile.ZipFile(pack) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files[filename] = content
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)


def test_record_preview_reads_each_shared_payload_once_per_plan(tmp_path, monkeypatch):
    import core.knowledge_pack as module
    from collections import Counter
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    _make_complete_user_patch(source)
    pack = export_knowledge_pack(source, source / "records.zip")
    read = module._read_payload_checked
    record_plan = module._record_import_plan
    passes = []
    active = None

    def observe_read(path):
        if active is not None and Path(path).is_relative_to(active[0]):
            active[1][Path(path).name] += 1
        return read(path)

    def observe_plan(app_root, work, manifest, **kwargs):
        nonlocal active
        counts = Counter()
        active = (work / "data/user", counts)
        try:
            return record_plan(app_root, work, manifest, **kwargs)
        finally:
            active = None
            passes.append(counts)

    monkeypatch.setattr(module, "_read_payload_checked", observe_read)
    monkeypatch.setattr(module, "_record_import_plan", observe_plan)
    first = plan_knowledge_pack_import(destination, pack)
    second = plan_knowledge_pack_import(destination, pack)
    assert first.can_apply and first == second
    assert len(passes) == 2
    for counts in passes:
        assert counts["tags.json"] == 1
        assert counts["characters.json"] == 1
        assert counts and all(value == 1 for value in counts.values())


@pytest.mark.parametrize("malformed", [b"{malformed", b"[]"])
def test_record_preview_rereads_changed_payload_and_preserves_malformed_refusal(tmp_path, malformed):
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    _make_complete_user_patch(source)
    pack = export_knowledge_pack(source, source / "records.zip")
    assert plan_knowledge_pack_import(destination, pack).can_apply
    _rewrite_record_pack(pack, "data/user/characters.json", malformed)
    with pytest.raises(ValueError):
        plan_knowledge_pack_import(destination, pack)


def test_record_payload_reuse_keeps_pack_freshness_and_local_correction_guards(tmp_path):
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    _make_complete_user_patch(source)
    pack = export_knowledge_pack(source, source / "records.zip")
    first = plan_knowledge_pack_import(destination, pack)
    assert first.can_apply
    with zipfile.ZipFile(pack) as archive:
        payload = json.loads(archive.read("data/user/tags.json"))
    next(row for row in payload["tags"] if row["id"] == "blue")["notes"] = "Changed after preview"
    _rewrite_record_pack(pack, "data/user/tags.json", json.dumps(payload).encode())
    before = {p.relative_to(destination): p.read_bytes() for p in (destination / "data/user").rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="changed"):
        import_knowledge_pack(destination, pack, plan=first)
    assert {p.relative_to(destination): p.read_bytes() for p in (destination / "data/user").rglob("*") if p.is_file()} == before
    fresh = plan_knowledge_pack_import(destination, pack)
    assert fresh.pack_fingerprint != first.pack_fingerprint and fresh.record_patches != first.record_patches
    local = destination / "data/user/tags.json"
    payload = _read(local)
    next(row for row in payload["tags"] if row["id"] == "blue")["notes"] = "Keep local correction"
    _write(local, payload)
    corrected = plan_knowledge_pack_import(destination, pack)
    with pytest.raises(RuntimeError, match="changed"):
        import_knowledge_pack(destination, pack, plan=fresh)
    assert any(item.kind == "tag" and item.stable_id == "blue" and item.outcome == "hold"
               for item in corrected.record_decisions)
    assert next(row for row in _read(local)["tags"] if row["id"] == "blue")["notes"] == "Keep local correction"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("resource", ["member_count", "aggregate_bytes", "wide_record"])
def test_export_refuses_reader_resource_overflow_without_publishing(tmp_path, monkeypatch, existing, resource):
    import core.knowledge_pack as module
    source = _root(tmp_path / "source")
    _make_complete_user_patch(source)
    if resource == "wide_record":
        path = source / "data/user/tags.json"
        payload = _read(path)
        payload["tags"][0]["notes"] = "Synthetic wide record " * 1024
        _write(path, payload)
    baseline = export_knowledge_pack(source, source / "baseline.zip")
    with zipfile.ZipFile(baseline) as archive:
        sizes = [item.file_size for item in archive.infolist()]
    if resource == "member_count":
        monkeypatch.setattr(module, "MAX_PACK_FILES", len(sizes) - 1)
        message = "too many files"
    else:
        if resource == "aggregate_bytes":
            # Every individual member fits; the combined resource must hold.
            limit = (max(sizes) + sum(sizes)) // 2
            assert max(sizes) < limit < sum(sizes)
        else:
            limit = max(sizes) // 2
            assert max(sizes) > limit
        monkeypatch.setattr(module, "MAX_PACK_UNCOMPRESSED_BYTES", limit)
        message = "too large"
    output = source / "selected.zip"
    previous = baseline.read_bytes()
    if existing:
        output.write_bytes(previous)
    before = {p.relative_to(source): p.read_bytes() for p in (source / "data").rglob("*") if p.is_file()}
    monkeypatch.setattr(zipfile.ZipFile, "testzip",
                        lambda _archive: pytest.fail("Resource refusal must precede payload read-back."))
    with pytest.raises(ValueError, match=message):
        export_knowledge_pack(source, output)
    assert (output.read_bytes() == previous) if existing else not output.exists()
    assert {p.relative_to(source): p.read_bytes() for p in (source / "data").rglob("*") if p.is_file()} == before


def test_export_reader_resource_exact_boundary_round_trips(tmp_path, monkeypatch):
    import core.knowledge_pack as module
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    _make_complete_user_patch(source)
    baseline = export_knowledge_pack(source, source / "baseline.zip")
    with zipfile.ZipFile(baseline) as archive:
        entries = archive.infolist()
        count, size = len(entries), sum(item.file_size for item in entries)
    monkeypatch.setattr(module, "MAX_PACK_FILES", count)
    monkeypatch.setattr(module, "MAX_PACK_UNCOMPRESSED_BYTES", size)
    output = export_knowledge_pack(source, source / "boundary.zip")
    assert plan_knowledge_pack_import(destination, output).can_apply


def test_record_staging_copies_existing_records_once_per_table(tmp_path, monkeypatch):
    import core.knowledge_pack as module
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    additions = [{"id": f"synthetic_{i}", "display": f"Synthetic {i}",
                  "category": "colors"} for i in range(128)]
    _write(source / "data/user/tags.json", {"schema_version": 10, "tags": additions})
    local = {"id": "existing_local_sentinel", "display": "Existing local",
             "category": "colors", "future_local": {"opaque": [1, 2, 3]}}
    _write(destination / "data/user/tags.json", {"schema_version": 10, "tags": [local]})
    pack = export_knowledge_pack(source, source / "records.zip")
    plan = plan_knowledge_pack_import(destination, pack)
    assert plan.can_apply
    dumps = module.json.dumps
    copies = []

    def count_existing_copy(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("id") == local["id"]:
            copies.append(value)
        return dumps(value, *args, **kwargs)

    monkeypatch.setattr(module.json, "dumps", count_existing_copy)
    stage = tmp_path / "stage"
    _results, paths = module._stage_record_import(destination, stage, plan)
    assert len(copies) == 1
    records = _read(paths[destination / "data/user/tags.json"])["tags"]
    expected = [dict(patch.payload, id=patch.local_id) for patch in plan.record_patches
                if patch.kind == "tag"]
    assert records == [local, *expected]
    assert _read(destination / "data/user/tags.json")["tags"] == [local]


def test_enrolled_record_import_validates_staged_categories_in_one_root(tmp_path):
    from suite_core.catalog_identity_candidate import CatalogIdentity, encode_identity
    source = _root(tmp_path / "source")
    destination = _root(tmp_path / "destination")
    _write(source / "data/user/categories.json", {"schema_version": 6,
           "categories": [{"id": "new_category", "name": "Synthetic Category"}]})
    _write(source / "data/user/tags.json", {"schema_version": 10,
           "tags": [{"id": "new_tag", "display": "Synthetic Tag", "category": "new_category"}]})
    (destination / "data/user/catalog_identity.json").write_bytes(encode_identity(CatalogIdentity(
        "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002")))
    default_before = {p.name: p.read_bytes() for p in (destination / "data/defaults").glob("*.json")}
    pack = export_knowledge_pack(source, source / "records.zip")
    plan = plan_knowledge_pack_import(destination, pack)
    assert plan.can_apply and plan.catalog_identity_binding
    result = import_knowledge_pack(destination, pack, plan=plan)
    receipt = _read(Path(result["backup"]["path"]) / "recovery.json")
    assert receipt["schema_version"] == 5 and receipt["status"] == "complete"
    assert {p.name: p.read_bytes() for p in (destination / "data/defaults").glob("*.json")} == default_before
    assert any(r["id"] == "new_tag" for r in _read(destination / "data/user/tags.json")["tags"])


# Foundation authority: generated recipients only, using the native transaction.
def _foundation_fixture(tmp_path):
    from hashlib import sha256
    import core.knowledge_pack as native
    from core.default_catalog_release import ApprovedDefaultPackRelease
    from suite_core.catalog_identity_candidate import CatalogIdentity, encode_identity
    root = _root(tmp_path / "recipient")
    user = root / "data/user/tags.json"
    payload = _read(user)
    payload["future_history"] = {"opaque": [1, {"original_assignment": "blue"}], "inactive": True}
    payload["removed_tag_ids"] = ["historical_tombstone"]
    payload["tags"].append({"id": "local_only_synthetic", "display": "local test", "category": "colors",
                            "notes": "retain user correction", "future_field": {"raw": [7, 8]}})
    _write(user, payload)
    _write(root / "data/user/categories.json", {"schema_version": 6, "categories": [],
        "disabled_default_ids": ["historical_category"], "future_history": {"retain": [1, 2]}})
    _write(root / "data/user/relationships.json", {"schema_version": 1,
        "relationships": [{"id": "retained_override", "kind": "identity_alias", "endpoints": [
            {"kind": "alias_text", "id": "user spelling"}, {"kind": "tag", "id": "local_only_synthetic"}],
            "source": "user", "layer": "user", "future": {"opaque": [9]}}], "future_history": {"keep": True}})
    (root / "data/user/catalog_identity.json").write_bytes(encode_identity(CatalogIdentity(
        "00000000-0000-0000-0000-000000000011", "00000000-0000-0000-0000-000000000012")))
    defaults = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "data/defaults").glob("*.json")}
    def alter(name, update):
        key = "data/defaults/" + name
        value = json.loads(defaults[key]); update(value)
        defaults[key] = (json.dumps(value, indent=2) + "\n").encode()
    alter("foundation_tags.json", lambda v: v["tags"].append({"id": "fresh_default", "display": "fresh default", "category": "colors", "future": {"kept": True}}))
    alter("categories.json", lambda v: v.update(future_definition={"inactive": ["retained"]}))
    alter("patterns.json", lambda v: v.update(future_pattern="retained"))
    alter("relationships.json", lambda v: v["relationships"].append({"id": "fresh_alias", "kind": "identity_alias", "endpoints": [{"kind": "alias_text", "id": "fresh spelling"}, {"kind": "tag", "id": "fresh_default"}], "layer": "default", "source": "synthetic", "reviewed": True, "future": {"raw": True}}))
    alter("relationships.json", lambda v: v["relationships"].append({"id": "retained_override", "kind": "identity_alias", "endpoints": [{"kind": "alias_text", "id": "default spelling"}, {"kind": "tag", "id": "blue"}], "layer": "default", "source": "synthetic"}))
    defaults["data/defaults/concepts.json"] = b'{"schema_version":1,"concepts":[{"id":"inactive_definition","enabled":false,"future":{"raw":[1,2]}}],"inactive":{"opaque":[1,2]}}'
    defaults["data/defaults/modifiers.json"] = b'{"schema_version":1,"modifiers":[],"future":{"keep":true}}'
    rows = tuple(sorted(defaults.items()))
    inventory = tuple((name, sha256(raw).hexdigest()) for name, raw in rows)
    proposal = native.FoundationProposal(1, "approved generated inventory", inventory, rows)
    snapshot = sha256(json.dumps(inventory).encode()).hexdigest()
    metadata = ("defaultTagDatabase", "synthetic-1", snapshot, "2026-10-10", ("Generated Foundation",), "synthetic approved source")
    digest = sha256(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    approved = ApprovedDefaultPackRelease(metadata[0], metadata[1], metadata[3], metadata[4], metadata[5], snapshot, digest)
    pack = tmp_path / "carrier.zip"
    with zipfile.ZipFile(pack, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "tagmanager_knowledge_pack", "schema_version": 8, "content_rating_boundary": "explicit"}))
        archive.writestr("data/user/tags.json", json.dumps({"schema_version": 10, "tags": [], "merge_rules": []}))
    plan = native.plan_knowledge_pack_import(root, pack, approved_default_release=approved, foundation_proposal=proposal)
    assert plan.can_apply, plan.issues
    before = {p: p.read_bytes() if p.exists() else None for p in native._foundation_plan_paths(root, proposal)}
    return native, root, pack, plan, approved, proposal, before


def _assert_foundation_before(before):
    assert all((p.read_bytes() if p.exists() else None) == raw for p, raw in before.items())


def test_foundation_native_complete_preserves_mixed_history_and_repeat_noop(tmp_path):
    native, root, pack, plan, approved, proposal, before = _foundation_fixture(tmp_path)
    result = native.import_knowledge_pack(root, pack, plan=plan)
    receipt = Path(result["backup"]["path"]) / "recovery.json"
    value = _read(receipt)
    assert value["status"] == "complete" and value["foundation_transition"]["version"] == 1
    native._require_foundation_authority(root, value, phase="after")
    assert all((root / name).read_bytes() == raw for name, raw in proposal.payloads)
    assert all((p.read_bytes() if p.exists() else None) == raw for p, raw in before.items() if p.parent == root / "data/user" and p.name != "default_knowledge_state.json")
    assert "fresh_default" not in {r["id"] for r in _read(root / "data/user/tags.json")["tags"]}
    assert native.observe_approved_default_pack_release(root, approved.pack_id)["content_version"] == approved.content_version
    receipts = {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")}
    repeat = native.plan_knowledge_pack_import(root, pack, approved_default_release=approved, foundation_proposal=proposal)
    repeated = native.import_knowledge_pack(root, pack, plan=repeat)
    assert repeated["backup"]["path"] == ""
    assert {p: p.read_bytes() for p in receipts} == receipts


@pytest.mark.parametrize("at", range(1, 8))
def test_foundation_interruption_each_participant_then_selected_restart(tmp_path, monkeypatch, at):
    native, root, pack, plan, _approved, _proposal, before = _foundation_fixture(tmp_path)
    atomic = native._atomic_bytes
    count = 0
    def interrupt(path, raw):
        nonlocal count
        atomic(path, raw)
        if path.is_relative_to(root / "data"):
            count += 1
            if count == at:
                raise SystemExit("generated interruption")
    monkeypatch.setattr(native, "_atomic_bytes", interrupt)
    with pytest.raises(SystemExit):
        native.import_knowledge_pack(root, pack, plan=plan)
    receipt = next((root / "backups/knowledge-pack-import").glob("*/recovery.json"))
    assert _read(receipt)["status"] == "pending"
    with pytest.raises(RuntimeError, match="Pending"):
        native.plan_knowledge_pack_import(root, pack)
    monkeypatch.setattr(native, "_atomic_bytes", atomic)
    assert native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)
    _assert_foundation_before(before)
    assert _read(receipt)["status"] == "restored"
    assert not native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)


def _pending_foundation(tmp_path, monkeypatch):
    native, root, pack, plan, approved, proposal, before = _foundation_fixture(tmp_path)
    atomic_json = native._atomic_json
    def interrupt(path, value):
        if path.name == "recovery.json" and value.get("status") == "complete":
            raise SystemExit("all after, receipt pending")
        atomic_json(path, value)
    monkeypatch.setattr(native, "_atomic_json", interrupt)
    with pytest.raises(SystemExit):
        native.import_knowledge_pack(root, pack, plan=plan)
    monkeypatch.setattr(native, "_atomic_json", atomic_json)
    receipt = next((root / "backups/knowledge-pack-import").glob("*/recovery.json"))
    return native, root, plan, before, receipt


def test_foundation_invalid_last_backup_preflights_before_any_restore(tmp_path, monkeypatch):
    native, root, plan, before, receipt = _pending_foundation(tmp_path, monkeypatch)
    value = _read(receipt)
    last = next(row for row in reversed(value["files"]) if row["existed"])
    (receipt.parent / last["backup"]).write_bytes(b"invalid generated backup")
    current = {p: p.read_bytes() if p.exists() else None for p in before}
    monkeypatch.setattr(native, "_atomic_bytes", lambda *_a: pytest.fail("restore before complete preflight"))
    with pytest.raises(RuntimeError, match="backup"):
        native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)
    _assert_foundation_before(current)
    assert _read(receipt)["status"] == "pending"


@pytest.mark.parametrize("drift", ["identity", "membership", "participant", "dependency", "user_override", "tracking", "epoch"])
def test_foundation_selected_recovery_holds_drift_without_write(tmp_path, monkeypatch, drift):
    native, root, plan, before, receipt = _pending_foundation(tmp_path, monkeypatch)
    if drift == "identity":
        path = root / "data/user/catalog_identity.json"
        value = _read(path); value["incarnation_id"] = "00000000-0000-0000-0000-000000000013"; _write(path, value)
    elif drift == "membership":
        _write(root / "data/defaults/unexpected_foundation.json", {"future": True})
    elif drift == "participant":
        (root / "data/defaults/concepts.json").write_bytes(b'{"unexpected":true}')
    elif drift == "dependency":
        (root / "data/defaults/series.json").write_bytes(b'{"unexpected":true}')
    elif drift == "user_override":
        (root / "data/user/tags.json").write_bytes(b'{"unexpected":true}')
    elif drift == "tracking":
        (root / "data/user/default_knowledge_state.json").write_bytes(b'{"unexpected":true}')
    else:
        other = root / "backups/knowledge-pack-import/newer/recovery.json"
        _write(other, {"schema_version": 2, "status": "restored"})
    current = {p: p.read_bytes() if p.exists() else None for p in before}
    monkeypatch.setattr(native, "_atomic_bytes", lambda *_a: pytest.fail("drift recovery wrote"))
    with pytest.raises((RuntimeError, ValueError)):
        native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)
    _assert_foundation_before(current)


@pytest.mark.parametrize("published", [False, True])
def test_foundation_durable_completion_failure_uses_actual_receipt(tmp_path, monkeypatch, published):
    native, root, pack, plan, _approved, proposal, before = _foundation_fixture(tmp_path)
    atomic = native._atomic_json
    def fail_completion(path, value):
        if path.name == "recovery.json" and value.get("status") == "complete":
            if published: atomic(path, value)
            raise OSError("generated completion failure")
        atomic(path, value)
    monkeypatch.setattr(native, "_atomic_json", fail_completion)
    if published:
        native.import_knowledge_pack(root, pack, plan=plan)
        assert all((root / name).read_bytes() == raw for name, raw in proposal.payloads)
    else:
        with pytest.raises(OSError, match="completion failure"):
            native.import_knowledge_pack(root, pack, plan=plan)
        _assert_foundation_before(before)
    receipt = next((root / "backups/knowledge-pack-import").glob("*/recovery.json"))
    assert _read(receipt)["status"] == ("complete" if published else "restored")


@pytest.mark.parametrize("fault", ["participant", "tracking", "receipt"])
def test_foundation_interrupted_restore_restart_skips_restored_files(tmp_path, monkeypatch, fault):
    native, root, plan, before, receipt = _pending_foundation(tmp_path, monkeypatch)
    atomic, atomic_json = native._atomic_bytes, native._atomic_json
    restored = []
    def interrupt(path, raw):
        atomic(path, raw)
        if path.is_relative_to(root / "data"):
            restored.append(path)
            if fault == "participant" or fault == "tracking" and path.name == "default_knowledge_state.json":
                raise SystemExit("interrupted restore")
    def interrupt_receipt(path, value):
        if path == receipt and value.get("status") == "restored":
            raise SystemExit("restored bytes, pending receipt")
        atomic_json(path, value)
    monkeypatch.setattr(native, "_atomic_bytes", interrupt)
    if fault == "receipt": monkeypatch.setattr(native, "_atomic_json", interrupt_receipt)
    with pytest.raises(SystemExit):
        native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)
    assert _read(receipt)["status"] == "pending"
    monkeypatch.setattr(native, "_atomic_json", atomic_json)
    def no_repeat(path, raw):
        assert path not in restored
        atomic(path, raw)
    monkeypatch.setattr(native, "_atomic_bytes", no_repeat)
    assert native.recover_selected_knowledge_pack_import(root, receipt, catalog_identity_binding=plan.catalog_identity_binding)
    _assert_foundation_before(before)


def test_foundation_preview_identity_membership_and_source_inventory_guards(tmp_path):
    from dataclasses import replace
    native, root, pack, plan, approved, proposal, before = _foundation_fixture(tmp_path)
    with pytest.raises(ValueError, match="inventory"):
        replace(proposal, source_inventory=proposal.source_inventory[:-1])
    with pytest.raises(ValueError, match="version"):
        replace(proposal, version=True)
    with pytest.raises(ValueError):
        replace(proposal, payloads=(("data/user/tags.json", b"{}"),))
    _write(root / "data/defaults/new_member.json", {})
    with pytest.raises(RuntimeError, match="changed after preview"):
        native.import_knowledge_pack(root, pack, plan=plan)
    assert not (root / "backups/knowledge-pack-import").exists()


def test_foundation_native_host_threads_typed_proposal(tmp_path):
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    from suite_core.catalog_identity_candidate import decode_identity
    native, root, pack, _plan, approved, proposal, _before = _foundation_fixture(tmp_path)
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    host = NativeTagCatalogAdapter(root, native=native, catalog_id=identity.publication_catalog_id,
        source_reference="generated approved host", foundation_proposal=proposal)
    plan = host.plan(pack, approved_default_release=approved)
    assert plan.foundation_proposal is proposal and plan.catalog_identity_binding
    result = host.apply(pack, plan)
    assert _read(Path(result["backup"]["path"]) / "recovery.json")["status"] == "complete"


@pytest.mark.parametrize("unavailable", ["tag_tombstone", "category_disabled", "unknown_tag"])
def test_foundation_staged_effective_dependencies_hold_before_any_live_write(tmp_path, unavailable):
    from dataclasses import replace
    from hashlib import sha256
    native, root, pack, _plan, approved, proposal, before = _foundation_fixture(tmp_path)
    if unavailable == "tag_tombstone":
        path = root / "data/user/tags.json"
        value = _read(path); value["removed_tag_ids"].append("fresh_default"); _write(path, value)
    elif unavailable == "category_disabled":
        path = root / "data/user/categories.json"
        value = _read(path); value["disabled_default_ids"].append("colors"); _write(path, value)
    payloads = dict(proposal.payloads)
    relative = "data/defaults/relationships.json"
    value = json.loads(payloads[relative])
    if unavailable == "unknown_tag": value["relationships"][0]["endpoints"][1]["id"] = "nonexistent_default"
    if unavailable == "category_disabled":
        value["relationships"].append({"id": "category_link", "kind": "entity_tag", "endpoints": [
            {"kind": "category", "id": "colors"}, {"kind": "tag", "id": "blue"}]})
    payloads[relative] = json.dumps(value).encode()
    rows = tuple(sorted(payloads.items()))
    proposal = replace(proposal, payloads=rows, source_inventory=tuple((n, sha256(b).hexdigest()) for n, b in rows))
    current = {p: p.read_bytes() if p.exists() else None for p in before}
    with pytest.raises(ValueError):
        native.plan_knowledge_pack_import(root, pack, foundation_proposal=proposal, approved_default_release=approved)
    _assert_foundation_before(current)
    assert not (root / "backups/knowledge-pack-import").exists()


def test_foundation_user_patch_carrier_never_promotes_defaults(tmp_path):
    native, root, pack, _plan, approved, proposal, before = _foundation_fixture(tmp_path)
    replacement = tmp_path / "user-patch.zip"
    with zipfile.ZipFile(pack) as source, zipfile.ZipFile(replacement, "w") as target:
        target.writestr("manifest.json", source.read("manifest.json"))
        target.writestr("data/user/tags.json", json.dumps({"schema_version": 10, "tags": [
            {"id": "incoming", "display": "Incoming", "category": "colors"}]}))
    with pytest.raises(ValueError, match="user record"):
        native.plan_knowledge_pack_import(root, replacement, foundation_proposal=proposal, approved_default_release=approved)
    _assert_foundation_before(before)


def test_foundation_changed_reviewed_participant_holds_before_backup(tmp_path):
    native, root, pack, plan, _approved, _proposal, _before = _foundation_fixture(tmp_path)
    _write(root / "data/defaults/categories.json", {"categories": [], "unexpected": True})
    with pytest.raises(RuntimeError, match="changed after preview"):
        native.import_knowledge_pack(root, pack, plan=plan)
    assert not (root / "backups/knowledge-pack-import").exists()



def test_foundation_repeat_cannot_reuse_release_with_different_source_inventory(tmp_path):
    from dataclasses import replace
    from hashlib import sha256
    native, root, pack, plan, approved, proposal, _before = _foundation_fixture(tmp_path)
    native.import_knowledge_pack(root, pack, plan=plan)
    payloads = dict(proposal.payloads)
    payloads["data/defaults/concepts.json"] = b'{"concepts":[],"unexpected_new_source":true}'
    rows = tuple(sorted(payloads.items()))
    proposal = replace(proposal, payloads=rows, source_inventory=tuple((n, sha256(b).hexdigest()) for n, b in rows))
    current = {p: p.read_bytes() if p.exists() else None for p in native._foundation_plan_paths(root, proposal)}
    with pytest.raises(RuntimeError, match="source inventory differs"):
        native.plan_knowledge_pack_import(root, pack, foundation_proposal=proposal, approved_default_release=approved)
    _assert_foundation_before(current)



def test_foundation_checks_participant_path_before_fingerprinting(tmp_path, monkeypatch):
    native, root, pack, _plan, approved, proposal, _before = _foundation_fixture(tmp_path)
    checked, fingerprint = native._check_export_path, native._file_fingerprint
    refused = root / "data/defaults/concepts.json"
    def check(path):
        if path == refused: raise RuntimeError("generated redirected participant refusal")
        checked(path)
    def observe(path):
        assert path != refused, "Foundation read bytes before canonical path check"
        return fingerprint(path)
    monkeypatch.setattr(native, "_check_export_path", check)
    monkeypatch.setattr(native, "_file_fingerprint", observe)
    with pytest.raises(RuntimeError, match="redirected participant"):
        native.plan_knowledge_pack_import(root, pack, approved_default_release=approved, foundation_proposal=proposal)



def test_foundation_completed_undo_holds_before_inverse_publication(tmp_path):
    import core.knowledge_pack as native
    from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox
    sandbox = SyntheticPackUndoSandbox.create(tmp_path / "recipient", native=native)
    native, root, pack, plan, _approved, proposal, _before = _foundation_fixture(tmp_path)
    result = native.import_knowledge_pack(root, pack, plan=plan)
    receipt = Path(result["backup"]["path"]) / "recovery.json"
    selection = native.select_enrolled_completed_pack(sandbox, receipt)
    current = {p: p.read_bytes() if p.exists() else None for p in native._foundation_plan_paths(root, proposal)}
    receipts = {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")}
    with pytest.raises(RuntimeError, match="inverse authority qualification"):
        native.undo_enrolled_completed_pack(sandbox, selection)
    _assert_foundation_before(current)
    assert {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")} == receipts
    assert not tuple((root / "backups/knowledge-pack-import").glob("synthetic-undo-*"))



def test_foundation_guard_preserves_enrolled_legacy_completed_undo(tmp_path):
    import core.knowledge_pack as native
    from suite_core.completed_pack_undo_candidate import SyntheticPackUndoSandbox
    sandbox = SyntheticPackUndoSandbox.create(tmp_path / "recipient", native=native)
    native, root, _pack, _plan, _approved, _proposal, _before = _foundation_fixture(tmp_path)
    source = _root(tmp_path / "ordinary-source")
    _write(source / "data/user/tags.json", {"schema_version": 10, "tags": [
        {"id": "ordinary_user_addition", "display": "ordinary user addition", "category": "colors"}]})
    pack = native.export_knowledge_pack(source, source / "ordinary-user-patch.zip")
    before = {p: p.read_bytes() if p.exists() else None for p in native._pack_authority_paths(root)}
    plan = native.plan_knowledge_pack_import(root, pack)
    assert plan.can_apply, (plan.issues, plan.record_decisions)
    result = native.import_knowledge_pack(root, pack, plan=plan)
    receipt = Path(result["backup"]["path"]) / "recovery.json"
    assert "foundation_transition" not in _read(receipt)
    selection = native.select_enrolled_completed_pack(sandbox, receipt)
    inverse = native.undo_enrolled_completed_pack(sandbox, selection)
    assert _read(inverse)["status"] == "restored"
    _assert_foundation_before(before)



def _foundation_proposal_edit(proposal, filename, mutate):
    from dataclasses import replace
    from hashlib import sha256
    payloads = dict(proposal.payloads)
    value = json.loads(payloads["data/defaults/" + filename]); mutate(value)
    payloads["data/defaults/" + filename] = (json.dumps(value, indent=2) + "\n").encode()
    rows = tuple(sorted(payloads.items()))
    return replace(proposal, payloads=rows, source_inventory=tuple((n, sha256(b).hexdigest()) for n, b in rows))


def _foundation_release(version, proposal, *, pack_id="defaultTagDatabase"):
    from suite_core.database_actions import CatalogRelease
    from suite_core.knowledge import Snapshot, Source, EffectiveTag, Category, Lookup
    from suite_core.knowledge.catalog_migration_preview import PackRevision
    payloads = dict(proposal.payloads)
    rows = [r for name in ("foundation_tags.json", "community_tags.json", "promoted_tags.json")
            for r in json.loads(payloads["data/defaults/" + name]).get("tags", ())]
    tags = tuple(EffectiveTag(r["id"], r.get("display", r["id"]), r.get("category", "colors"),
                             tuple(r.get("aliases", ())), r.get("content_sensitivity", {}).get("minimum_rating", ""),
                             r.get("content_sensitivity", {}).get("classification_status", "unclassified")) for r in rows)
    snapshot = Snapshot(Source("generated approved Foundation", version, "generated-" + version, ("colors",), kind="reviewed_portable"),
                        tags, (Category("colors", "Colors", "Colors"),),
                        tuple(Lookup(t.display, "canonical_name", t.id, (t.id,)) for t in tags))
    return CatalogRelease(PackRevision(pack_id, version, snapshot), "2026-10-10", ("Generated Foundation release",), "generated approved Foundation")


@pytest.mark.parametrize("repackage", [False, True])
def test_foundation_adapter_repeat_identity_ignores_predecessor_and_carrier(tmp_path, repackage):
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    from suite_core.catalog_identity_candidate import decode_identity
    native, root, pack, _plan, _approved, proposal, _before = _foundation_fixture(tmp_path)
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    host = NativeTagCatalogAdapter(root, native=native, catalog_id=identity.publication_catalog_id, source_reference="synthetic")
    release = _foundation_release("A", proposal)
    plan = host.plan_default_release(pack, release, release, False, foundation_proposal=proposal)
    host.apply(pack, plan)
    before = {p: p.read_bytes() if p.exists() else None for p in native._foundation_plan_paths(root, proposal)}
    receipts = {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")}
    if repackage:
        replacement = tmp_path / "repackaged-carrier.zip"
        with zipfile.ZipFile(pack) as source, zipfile.ZipFile(replacement, "w") as target:
            for row in source.infolist(): target.writestr(row.filename, source.read(row.filename))
            target.comment = b"different packaging, identical release and Foundation"
        pack = replacement
    repeat = host.plan_default_release(pack, release, release, True, foundation_proposal=proposal)
    result = host.apply(pack, repeat)
    assert result["backup"]["path"] == ""
    _assert_foundation_before(before)
    assert {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")} == receipts


@pytest.mark.parametrize("change", ["bytes", "reference"])
def test_foundation_adapter_same_release_source_drift_never_bypasses_guard(tmp_path, change):
    from dataclasses import replace
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    from suite_core.catalog_identity_candidate import decode_identity
    native, root, pack, _plan, _approved, proposal, _before = _foundation_fixture(tmp_path)
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    host = NativeTagCatalogAdapter(root, native=native, catalog_id=identity.publication_catalog_id, source_reference="synthetic")
    release = _foundation_release("A", proposal)
    host.apply(pack, host.plan_default_release(pack, release, release, False, foundation_proposal=proposal))
    before = {p: p.read_bytes() if p.exists() else None for p in native._foundation_plan_paths(root, proposal)}
    modified = (_foundation_proposal_edit(proposal, "concepts.json", lambda v: v.update(unreviewed_change=True))
                if change == "bytes" else replace(proposal, source_reference="different source reference"))
    with pytest.raises(RuntimeError, match="source inventory differs"):
        host.plan_default_release(pack, release, release, True, foundation_proposal=modified)
    _assert_foundation_before(before)


def test_foundation_adapter_update_then_repeat_preserves_tracking_and_future_fields(tmp_path):
    from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
    from suite_core.catalog_identity_candidate import decode_identity
    native, root, pack, _plan, _approved, proposal_a, _before = _foundation_fixture(tmp_path)
    identity = decode_identity((root / "data/user/catalog_identity.json").read_bytes())
    host = NativeTagCatalogAdapter(root, native=native, catalog_id=identity.publication_catalog_id, source_reference="synthetic")
    release_a = _foundation_release("A", proposal_a)
    host.apply(pack, host.plan_default_release(pack, release_a, release_a, False, foundation_proposal=proposal_a))
    state_path = root / "data/user/default_knowledge_state.json"
    state = _read(state_path); state["future_tracking"] = {"preserve": [1, 2]}; _write(state_path, state)
    proposal_b = _foundation_proposal_edit(proposal_a, "concepts.json", lambda v: v.update(approved_new_definition={"opaque": True}))
    release_b = _foundation_release("B", proposal_b)
    host.apply(pack, host.plan_default_release(pack, release_a, release_b, True, foundation_proposal=proposal_b))
    before = {p: p.read_bytes() if p.exists() else None for p in native._foundation_plan_paths(root, proposal_b)}
    receipts = {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")}
    result = host.apply(pack, host.plan_default_release(pack, release_b, release_b, True, foundation_proposal=proposal_b))
    assert result["backup"]["path"] == ""
    assert _read(state_path)["future_tracking"] == {"preserve": [1, 2]}
    _assert_foundation_before(before)
    assert {p: p.read_bytes() for p in (root / "backups/knowledge-pack-import").glob("*/recovery.json")} == receipts


@pytest.mark.parametrize("defect", ["missing_id", "id_and_name", "alias_collision"])
def test_foundation_canonical_identity_findings_hold_before_receipt(tmp_path, defect):
    native, root, pack, _plan, approved, proposal, before = _foundation_fixture(tmp_path)
    def corrupt(value):
        if defect == "missing_id": value["tags"].append({"display": "missing identity", "category": "colors"})
        elif defect == "id_and_name": value["tags"].append({"id": "blue", "display": "red", "category": "colors"})
        else: value["tags"][0]["aliases"] = ["red"]
    proposal = _foundation_proposal_edit(proposal, "foundation_tags.json", corrupt)
    plan = native.plan_knowledge_pack_import(root, pack, foundation_proposal=proposal, approved_default_release=approved)
    assert not plan.can_apply
    assert any("Foundation identity:" in issue for issue in plan.issues)
    with pytest.raises(ValueError, match="hold"):
        native.import_knowledge_pack(root, pack, plan=plan)
    _assert_foundation_before(before)
    assert not (root / "backups/knowledge-pack-import").exists()


@pytest.mark.parametrize("boundary", ["general", "questionable", "explicit"])
def test_foundation_content_review_uses_source_not_empty_carrier(tmp_path, boundary):
    native, root, pack, _plan, approved, proposal, before = _foundation_fixture(tmp_path)
    proposal = _foundation_proposal_edit(proposal, "foundation_tags.json", lambda v: v["tags"][-1].update(
        content_sensitivity={"minimum_rating": "explicit", "classification_status": "reviewed", "classification_confidence": 1.0}, future_opaque={"preserve": [1, 2]}))
    replacement = tmp_path / "rated-carrier.zip"
    with zipfile.ZipFile(pack) as source, zipfile.ZipFile(replacement, "w") as target:
        manifest = json.loads(source.read("manifest.json")); manifest["content_rating_boundary"] = boundary
        target.writestr("manifest.json", json.dumps(manifest)); target.writestr("data/user/tags.json", source.read("data/user/tags.json"))
    plan = native.plan_knowledge_pack_import(root, replacement, foundation_proposal=proposal, approved_default_release=approved)
    assert any(row.stable_id == "fresh_default" and row.rating == "explicit" for row in plan.content_findings)
    assert plan.can_apply == (boundary == "explicit")
    if boundary != "explicit":
        assert any("Foundation content understates" in issue for issue in plan.issues)
        with pytest.raises(ValueError): native.import_knowledge_pack(root, replacement, plan=plan)
        _assert_foundation_before(before)
        assert not (root / "backups/knowledge-pack-import").exists()


def test_foundation_raw_budget_preserves_archive_budget():
    from hashlib import sha256
    import core.knowledge_pack as native
    assert native.MAX_PACK_UNCOMPRESSED_BYTES == 50 * 1024 * 1024
    raw = b'{"padding":"' + b'x' * (26 * 1024 * 1024) + b'"}'
    name = "data/defaults/bounded_source.json"
    second_name = "data/defaults/bounded_source_two.json"
    payloads = ((name, raw), (second_name, raw))
    inventory = tuple((n, sha256(b).hexdigest()) for n, b in payloads)
    native.FoundationProposal(1, "synthetic source envelope", inventory, payloads)
    oversized = b'{"padding":"' + b'x' * native.MAX_PACK_UNCOMPRESSED_BYTES + b'"}'
    with pytest.raises(ValueError, match="participant exceeds native recovery byte budget"):
        native.FoundationProposal(1, "synthetic source envelope", ((name, sha256(oversized).hexdigest()),), ((name, oversized),))
    third_name = "data/defaults/bounded_source_three.json"
    aggregate_payloads = (*payloads, (third_name, raw))
    aggregate_inventory = tuple((n, sha256(b).hexdigest()) for n, b in aggregate_payloads)
    with pytest.raises(ValueError, match="byte budget"):
        native.FoundationProposal(1, "synthetic source envelope", aggregate_inventory, aggregate_payloads)


@pytest.mark.parametrize("change", ["unchanged_history", "new_relationship", "new_unavailability"])
def test_foundation_retained_unavailable_relationship_authority(tmp_path, change):
    native, root, pack, _plan, approved, proposal, _before = _foundation_fixture(tmp_path)
    row = {"id":"preserved_history", "kind":"identity_alias", "endpoints":[
        {"kind":"alias_text","id":"retained historical spelling"}, {"kind":"tag","id":"blue"}],
        "layer":"default", "source":"synthetic retained history", "future":{"opaque":True}}
    if change != "new_relationship":
        value=_read(root / "data/defaults/relationships.json");value["relationships"].append(row)
        _write(root / "data/defaults/relationships.json",value)
    proposal=_foundation_proposal_edit(proposal,"relationships.json",lambda v:v["relationships"].append(row))
    if change == "new_unavailability":
        proposal=_foundation_proposal_edit(proposal,"foundation_tags.json",lambda v:v.update(tags=[r for r in v["tags"] if r["id"] != "blue"]))
        value=_read(root / "data/user/tags.json");value["tags"]=[r for r in value["tags"] if r["id"] != "blue"]
        _write(root / "data/user/tags.json",value)
    else:
        value=_read(root / "data/user/tags.json");value["removed_tag_ids"].append("blue")
        _write(root / "data/user/tags.json",value)
    if change == "unchanged_history":
        plan=native.plan_knowledge_pack_import(root,pack,approved_default_release=approved,foundation_proposal=proposal)
        assert plan.can_apply, plan.issues
    else:
        with pytest.raises(ValueError,match="effective dependencies"):
            native.plan_knowledge_pack_import(root,pack,approved_default_release=approved,foundation_proposal=proposal)


@pytest.mark.parametrize("case",["historical_redirect","canonical_precedence"])
def test_foundation_sealed_default_alias_surface_uses_native_identity_authority(tmp_path,case):
    native,root,pack,plan,approved,proposal,before=_foundation_fixture(tmp_path)
    if case=="historical_redirect":
        proposal=_foundation_proposal_edit(proposal,"promoted_tags.json",lambda v:v.setdefault("merged_tag_ids",{}).update(historical_alias_owner="fresh_default"))
        target="historical_alias_owner";alias="historical exact spelling"
    else:target="fresh_default";alias="blue"
    def edit(v):
        v["relationships"].append({"id":"sealed_alias_regression","kind":"identity_alias","reviewed":True,"source":"synthetic","layer":"default",
            "details":{"compatibility_projection":"tag.aliases","default_compiler":"reviewed_tag_aliases_v1","alias_order":"0"},
            "endpoints":[{"kind":"alias_text","id":alias},{"kind":"tag","id":target}]})
    proposal=_foundation_proposal_edit(proposal,"relationships.json",edit)
    plan=native.plan_knowledge_pack_import(root,pack,approved_default_release=approved,foundation_proposal=proposal)
    sealed=native.review_foundation_import_plan(root,plan)
    current={row[0]:row for row in sealed.foundation_default_tags}
    assert (alias in current["fresh_default"][3])==(case=="historical_redirect")
    assert "local_only_synthetic" not in current
    _assert_foundation_before(before)
