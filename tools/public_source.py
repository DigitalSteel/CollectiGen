from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable


PRIVATE_PATH_PARTS = {
    ".agents",
    ".codex",
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "backups",
    "data/user",
    "data/cache",
    "data/models",
    "data/optional-components",
    "data/runtime",
    "data/update-state",
    "diagnostics",
    "dist",
    "exports",
    "logs",
    "output",
    "training",
}
PRIVATE_EXACT_PATHS = {
    "AGENTS.md",
    "GUI_NOTES.txt",
    "UPDATE_NOTES.txt",
    "saved_rules.json",
    "test_core.py",
    "core/knowledge_promotion.py",
    "qt_app/knowledge_promotion_dialog.py",
    "data/defaults/promotion_exclusions.json",
    "data/defaults/promotion_manifest.json",
    "docs/CODEX_TASK_TEMPLATE.md",
    "docs/COMMUNITY_KNOWLEDGE_SOURCES.md",
    "docs/KNOWLEDGE_RELEASE_POLICY.md",
    "docs/ROADMAP.md",
    "docs/VISION.md",
    "tools/Build-CommunityKnowledge.py",
    "tools/Review-KnowledgePromotion.py",
}
PUBLIC_GITHUB_PATHS = {
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feedback.yml",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
}
PUBLIC_GITHUB_DIRECTORIES = {
    ".github",".github/ISSUE_TEMPLATE",".github/workflows",
}
PRIVATE_DEFAULT_KEYS = {
    "backup",
    "backup_path",
    "candidate_ids",
    "example_images",
    "image_paths",
    "local_path",
    "operator",
    "operator_notes",
    "dataset_path",
    "image_path",
    "prompt",
    "promoted_at",
    "raw_evidence",
    "raw_payload",
    "reviewed_at",
}
TEXT_SUFFIXES = {
    "",
    ".bat",
    ".cfg",
    ".cs",
    ".csproj",
    ".csv",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
CONTENT_RULES = (
    ("Windows absolute path", re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]Users[\\/][^\\/\s\"']+")),
    ("Unix home path", re.compile(r"(?i)(?:/Users/|/home/)[^/\s\"']+")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("likely credential", re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*[\"'][^\"']{8,}[\"']")),
    ("personal email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@(?!users\.noreply\.github\.com\b)[A-Z0-9.-]+\.[A-Z]{2,}\b")),
)
CONTENT_SCAN_EXEMPT = {"tools/public_source.py", "tests/test_public_source.py"}
# Pinned upstream configuration provenance, not permission for local home paths.
# sd-scripts 6721028c79ee85a78b3a06dfd8954dae310a1cce, Apache-2.0.
# Preserve original source bytes; any change requires a fresh provenance review.
REVIEWED_UPSTREAM_MATCHES = {
    # Reviewed local detector/test literals, using the same exact-byte, path,
    # rule and line binding. These are not file-wide content-scan exemptions.
    # The example.org address and citation credentials are synthetic;
    # the Unix match is regex syntax.
    "tests/test_tag_public_projection_review.py": (
        "8cf2b7da39e0f61c85a1a7f315a1ab3899d9e763f7be94236682a7d798394385",
        frozenset({("personal email", 76), ("personal email", 146)}),
    ),
    "tools/tag_public_projection_review.py": (
        "ebcc9b43113dd67aab1f2f76dd44c7051bfc51337a15b5c51ac6afd42750bd1b",
        frozenset({("Unix home path", 201)}),
    ),
    "tools/trainer-sources/sd-scripts/library/hunyuan_image_text_encoder.py": (
        "2d940f764c484b29904668ab5e3c52753737b62183db5dc244266e6de19e1ab5",
        frozenset({("Unix home path", 179)}),
    ),
}
_MODULE_ROOT = Path(__file__).resolve().parents[1]


class PublicSourceError(RuntimeError):
    pass


def audit_private_distribution_completeness(
    root: Path,
    *,
    service_factory: Callable[[Path], Any] | None = None,
) -> list[str]:
    """Explicitly audit private-maintainer knowledge before a real release export."""
    root = root.resolve()
    if os.environ.get("TAG_MANAGER_PYTEST") == "1" and root == _MODULE_ROOT:
        raise PublicSourceError(
            "Routine pytest cannot audit the live private System Knowledge authority; "
            "inject a synthetic completeness check into build_public_source()."
        )
    private_service = root / "core" / "knowledge_promotion.py"
    if service_factory is None:
        if not private_service.is_file() or not (root / "data" / "user").is_dir():
            return []
        try:
            from core.knowledge_promotion import KnowledgePromotionService
        except ImportError as exc:
            return [f"Private knowledge completeness check could not start: {exc}"]
        service_factory = KnowledgePromotionService
    return list(service_factory(root).distribution_findings())


def _knowledge_name(value: Any) -> str:
    return " ".join(str(value or "").replace("_", " ").casefold().split())


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _normalized_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise PublicSourceError(f"Manifest path is not a safe repository-relative path: {value}")
    return path.as_posix()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise PublicSourceError("Unsupported public-source manifest schema.")
    files = [_normalized_path(value) for value in payload.get("files", [])]
    if len(files) != len(set(files)):
        raise PublicSourceError("Public-source manifest files must be unique.")
    payload["files"] = sorted(files, key=str.casefold)
    for value in files:
        lowered = value.casefold()
        if value in PRIVATE_EXACT_PATHS or (value not in PUBLIC_GITHUB_PATHS and any(
            lowered == part.casefold() or lowered.startswith(part.casefold() + "/")
            for part in PRIVATE_PATH_PARTS
        )):
            raise PublicSourceError(f"Private path is allowlisted: {value}")
    reviewed = {_normalized_path(value) for value in payload.get("reviewed_defaults", [])}
    if not reviewed.issubset(set(files)):
        raise PublicSourceError("Every reviewed default must also be explicitly allowlisted.")
    if payload.get("export_scope") == "component-source":
        components = payload.get("component_ids")
        known = {"collectigen.tag-encyclopedia": "suite_core/encyclopedia/MODULE_MANIFEST.json",
                 "collectigen.suite-manager": "suite_core/manager/MODULE_MANIFEST.json"}
        if (type(components) is not list or not components
                or any(type(c) is not str or c not in known for c in components)
                or len(set(components)) != len(components)
                or reviewed or payload.get("required_provenance") or ".gitattributes" in files
                or any(path.startswith("data/") and path != "data/defaults/release-trust.json" for path in files)
                or any(known[c] not in files for c in components)):
            raise PublicSourceError("Component source requires exact component manifests and no catalog/default content.")
    elif payload.get("export_scope") is not None:
        raise PublicSourceError("Unsupported public export scope.")
    return payload


def _git_reader(root: Path, source_ref: str) -> Callable[[str], bytes]:
    def read(relative: str) -> bytes:
        result = subprocess.run(
            ["git", "show", f"{source_ref}:{relative}"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise PublicSourceError(f"Could not read {relative} from {source_ref}: {detail}")
        return result.stdout

    return read


def _tree_reader(root: Path) -> Callable[[str], bytes]:
    def read(relative: str) -> bytes:
        path = root / Path(relative)
        if path.is_symlink() or not path.is_file():
            raise PublicSourceError(f"Allowlisted source is missing, not a file, or a symlink: {relative}")
        return path.read_bytes()

    return read


def sanitize_public_default(value: Any, removed: dict[str, int] | None = None) -> Any:
    removed = removed if removed is not None else {}
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in PRIVATE_DEFAULT_KEYS:
                removed[key] = removed.get(key, 0) + 1
                continue
            result[key] = sanitize_public_default(item, removed)
        promotion = result.get("promotion")
        if isinstance(promotion, dict):
            result["promotion"] = {"reviewed": bool(promotion.get("reviewed"))}
        return result
    if isinstance(value, list):
        return [sanitize_public_default(item, removed) for item in value]
    return value


def prepare_files(
    manifest: dict[str, Any],
    source_reader: Callable[[str], bytes],
    defaults_reader: Callable[[str], bytes] | None = None,
    working_tree_reviewed_defaults: set[str] | None = None,
) -> tuple[dict[str, bytes], dict[str, Any], str]:
    defaults_reader = defaults_reader or source_reader
    reviewed = set(manifest["reviewed_defaults"])
    working_tree_reviewed_defaults = {
        _normalized_path(value) for value in (working_tree_reviewed_defaults or set())
    }
    unknown_overrides = working_tree_reviewed_defaults - reviewed
    if unknown_overrides:
        raise PublicSourceError(
            "Working-tree reviewed-default overrides are not reviewed defaults: "
            + ", ".join(sorted(unknown_overrides, key=str.casefold))
        )
    prepared: dict[str, bytes] = {}
    default_changes: dict[str, Any] = {}
    for relative in manifest["files"]:
        raw = (
            source_reader(relative)
            if relative not in reviewed or relative in working_tree_reviewed_defaults
            else defaults_reader(relative)
        )
        if relative in reviewed:
            try:
                original = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PublicSourceError(f"Reviewed default is not valid UTF-8 JSON: {relative}") from exc
            removed: dict[str, int] = {}
            sanitized = sanitize_public_default(original, removed)
            clean = _json_bytes(sanitized)
            default_changes[relative] = {
                "source_sha256": _sha256(raw),
                "public_sha256": _sha256(clean),
                "canonicalized": raw != clean,
                "private_fields_removed": dict(sorted(removed.items())),
            }
            raw = clean
        prepared[relative] = raw
    provenance = {}
    for relative in manifest["required_provenance"]:
        if relative not in prepared:
            raise PublicSourceError(f"Required provenance file is not allowlisted: {relative}")
        provenance[relative] = _sha256(prepared[relative])
    public_defaults = {
        "schema_version": 1,
        "files": {
            relative: {"sha256": _sha256(prepared[relative]), "size_bytes": len(prepared[relative])}
            for relative in sorted(reviewed, key=str.casefold)
        },
        "provenance": provenance,
    }
    prepared["data/defaults/public-defaults.json"] = _json_bytes(public_defaults)
    validation = validate_default_references(prepared, manifest)
    if validation:
        raise PublicSourceError("Reviewed-default validation failed:\n" + "\n".join(validation))
    diff_bytes = _json_bytes({"schema_version": 1, "defaults": default_changes})
    return prepared, json.loads(diff_bytes), _sha256(diff_bytes)


def validate_default_references(prepared: dict[str, bytes], manifest: dict[str, Any]) -> list[str]:
    if manifest.get("export_scope") == "component-source":
        for component in manifest["component_ids"]:
            directory = "manager" if component == "collectigen.suite-manager" else "encyclopedia"
            identity = json.loads(prepared[f"suite_core/{directory}/MODULE_MANIFEST.json"])
            if identity.get("module_id") != component or not identity.get("software_version"):
                return ["Component source manifest identity mismatch."]
        trust = prepared.get("data/defaults/release-trust.json")
        if trust is not None:
            from suite_core.discovery.component_release_signature import _trust
            _trust(trust)  # Public-only schema: unknown/private key fields remain rejected.
        return []
    from core.character_projection_storage import (
        character_record_compatibility_payloads,
    )
    from core.default_sync import resolve_effective_tag_defaults
    from core.knowledge_relationships import KnowledgeRelationship

    payloads = {
        relative: json.loads(prepared[relative].decode("utf-8-sig"))
        for relative in manifest["reviewed_defaults"]
    }
    findings: list[str] = []

    def records(name: str, key: str) -> list[dict[str, Any]]:
        value = payloads[f"data/defaults/{name}.json"].get(key, [])
        return [item for item in value if isinstance(item, dict)]

    categories = records("categories", "categories")
    category_ids = {str(item.get("id") or "") for item in categories}
    concepts = records("concepts", "concepts")
    concept_ids = {str(item.get("id") or "") for item in concepts}
    modifier_groups = payloads["data/defaults/modifiers.json"].get("groups", {})
    modifier_ids = set(modifier_groups) if isinstance(modifier_groups, dict) else set()
    series = records("series", "series")
    series_ids = {str(item.get("id") or "") for item in series}
    characters = records("characters", "characters")
    character_ids = {str(item.get("id") or "") for item in characters}
    outfits = records("outfits", "outfits")
    outfit_ids = {str(item.get("id") or "") for item in outfits}
    variant_reviews = records("character_variant_reviews", "reviews")
    promoted_payload = payloads["data/defaults/promoted_tags.json"]
    removed_tag_ids = {
        str(value) for value in promoted_payload.get("removed_tag_ids", [])
    }
    historical_tag_ids = {
        str(item.get("id") or "")
        for name in ("foundation_tags", "community_tags", "promoted_tags")
        for item in records(name, "tags")
        if str(item.get("id") or "")
    }
    effective_tags = resolve_effective_tag_defaults(
        (records(name, "tags") for name in (
            "foundation_tags", "community_tags", "promoted_tags",
        )),
        removed_tag_ids=removed_tag_ids,
        merged_tag_ids=dict(promoted_payload.get("merged_tag_ids") or {}),
    )
    findings.extend(effective_tags.findings)
    alias_payload=payloads["data/defaults/tag_aliases.json"]
    aliases_by_tag={
        str(item.get("tag_id") or ""):list(item.get("aliases",()))
        for item in alias_payload.get("tags",()) if isinstance(item,dict)
    }
    tags=[]
    for source in effective_tags.tags:
        tag=json.loads(json.dumps(source))
        aliases=aliases_by_tag.get(str(tag.get("id") or ""),())
        if aliases:
            tag["aliases"]=list(aliases)
        tags.append(tag)
    tags_by_id = {str(item.get("id") or ""): item for item in tags}
    unknown_alias_owners=sorted(set(aliases_by_tag)-historical_tag_ids)
    if unknown_alias_owners:
        findings.append(
            "Reviewed Other Names reference unknown historical Tags: "
            +", ".join(unknown_alias_owners[:10])
        )
    relationship_records = records("relationships", "relationships")
    canonical_relationships = tuple(
        KnowledgeRelationship.from_dict(item) for item in relationship_records
    )
    series = character_record_compatibility_payloads(
        "series", series, canonical_relationships,
    )
    characters = character_record_compatibility_payloads(
        "character", characters, canonical_relationships,
    )
    outfits = character_record_compatibility_payloads(
        "outfit", outfits, canonical_relationships,
    )
    specificity_sources: set[str] = set()
    active_specificity_targets: dict[str, list[str]] = {}
    for relationship in relationship_records:
        if relationship.get("kind") != "specificity":
            continue
        endpoints = relationship.get("endpoints")
        if (
            not relationship.get("directed", True)
            or not isinstance(endpoints, list)
            or len(endpoints) != 2
            or not all(isinstance(endpoint, dict) for endpoint in endpoints)
            or endpoints[0].get("kind") != "tag"
            or endpoints[1].get("kind") != "tag"
        ):
            continue
        source_id = str(endpoints[0].get("id") or "")
        target_id = str(endpoints[1].get("id") or "")
        if not source_id or not target_id:
            continue
        specificity_sources.add(source_id)
        if relationship.get("reviewed") and not relationship.get("disabled"):
            active_specificity_targets.setdefault(source_id, []).append(target_id)
    tag_names = {
        _knowledge_name(form)
        for tag in tags
        for form in (tag.get("display"), *tag.get("aliases", []))
        if _knowledge_name(form)
    }
    def validate_stable_tag_reference(value: Any, label: str) -> None:
        stable_id = str(value or "").strip()
        if not stable_id:
            return
        canonical = effective_tags.canonical_id(stable_id)
        if (
            stable_id in effective_tags.id_remap
            or stable_id in effective_tags.merged_tag_ids
        ) and canonical != stable_id:
            findings.append(
                f"{label} references noncanonical tag {stable_id}; use {canonical}"
            )
        elif stable_id not in tags_by_id:
            findings.append(f"{label} references missing tag {stable_id}")

    for source, target in effective_tags.merged_tag_ids.items():
        if str(source) not in removed_tag_ids:
            findings.append(f"tag redirect source {source} is not removed")
        if (
            str(target) in removed_tag_ids
            and str(target) in historical_tag_ids
            and str(target) not in effective_tags.merged_tag_ids
        ):
            # A reviewed donor may redirect to an identity that was later
            # deliberately retired. Its exact historical record is the
            # recovery target even though it is not live vocabulary.
            continue
        validate_stable_tag_reference(target, f"tag redirect {source}")
    for item in categories:
        parent = str(item.get("parent_id") or "")
        if parent and parent not in category_ids:
            findings.append(f"category {item.get('id')} references missing parent {parent}")
        base_tag_id = str(item.get("base_tag_id") or "")
        if base_tag_id:
            validate_stable_tag_reference(
                base_tag_id, f"category {item.get('id')} base"
            )
    for item in tags:
        category = str(item.get("category") or "")
        concept = str(item.get("concept_id") or "")
        if category and category != "uncategorized" and category not in category_ids:
            findings.append(f"tag {item.get('id')} references missing category {category}")
        if concept and concept not in concept_ids and concept not in modifier_ids:
            findings.append(f"tag {item.get('id')} references missing concept {concept}")
        relations = item.get("relations") if isinstance(item.get("relations"), dict) else {}
        for field in ("parent", "implies", "conflicts"):
            values = relations.get(field)
            values = [] if not values else values if isinstance(values, list) else [values]
            for value in values:
                if str(value) not in tags_by_id and _knowledge_name(value) not in tag_names:
                    findings.append(
                        f"tag {item.get('id')} {field} references missing tag {value}"
                    )
    for item in records("patterns", "patterns"):
        category = str(item.get("category") or "")
        if category and category != "uncategorized" and category not in category_ids:
            findings.append(f"pattern {item.get('id')} references missing category {category}")
    for item in characters:
        series_id = str(item.get("series_id") or "")
        if series_id and series_id not in series_ids:
            findings.append(f"character {item.get('id')} references missing series {series_id}")
    for item in outfits:
        series_id = str(item.get("series_id") or "")
        if series_id and series_id not in series_ids:
            findings.append(f"outfit {item.get('id')} references missing series {series_id}")
        for character_id in item.get("character_ids", []) or []:
            if str(character_id) not in character_ids:
                findings.append(f"outfit {item.get('id')} references missing character {character_id}")
    characters_by_id = {str(item.get("id") or ""): item for item in characters}
    outfits_by_id = {str(item.get("id") or ""): item for item in outfits}
    for kind, records_by_id in (
        ("character", characters_by_id), ("outfit", outfits_by_id),
    ):
        for record_id, record in records_by_id.items():
            representative_forms = {
                _knowledge_name(value)
                for value in (record.get("name"), *record.get("aliases", []))
                if _knowledge_name(value)
            }
            link = record.get("knowledge_tag") if isinstance(record.get("knowledge_tag"), dict) else {}
            representative_tag_id = str(link.get("tag_id") or "")
            if representative_tag_id:
                validate_stable_tag_reference(
                    representative_tag_id, f"{kind} {record_id} representative"
                )
            representative_tag = tags_by_id.get(representative_tag_id)
            if representative_tag is not None:
                representative_forms.update(
                    _knowledge_name(value)
                    for value in (
                        representative_tag.get("display"),
                        *representative_tag.get("aliases", []),
                    )
                    if _knowledge_name(value)
                )
            duplicates = [
                str(value) for value in record.get("sdxl_tags", [])
                if _knowledge_name(value) in representative_forms
            ]
            if duplicates:
                findings.append(
                    f"{kind} {record_id} repeats its representative as a component Tag"
                )
    for record in series:
        link = record.get("knowledge_tag") if isinstance(record.get("knowledge_tag"), dict) else {}
        representative_tag_id = str(link.get("tag_id") or "")
        if representative_tag_id:
            validate_stable_tag_reference(
                representative_tag_id,
                f"character source {record.get('id')} representative",
            )
    for item in variant_reviews:
        variant_id = str(item.get("variant_id") or "")
        base_id = str(item.get("base_character_id") or "")
        source_id = str(item.get("source_id") or "")
        variant_tag = str(item.get("variant_tag") or "")
        variant_type = str(item.get("variant_type") or "")
        expected_category = "character_outfit" if variant_type == "outfit" else "character_identity"
        if item.get("status") != "reviewed":
            findings.append(f"character variant {variant_id} is not reviewed")
        if variant_type not in {"outfit", "ascension", "class_variant", "form"}:
            findings.append(f"character variant {variant_id} has unsupported type {variant_type}")
        if base_id not in character_ids:
            findings.append(f"character variant {variant_id} references missing base {base_id}")
        elif str(characters_by_id[base_id].get("series_id") or "") != source_id:
            findings.append(f"character variant {variant_id} does not share its base Character Source")
        if source_id not in series_ids:
            findings.append(f"character variant {variant_id} references missing source {source_id}")
        record = outfits_by_id.get(variant_id) if variant_type == "outfit" else characters_by_id.get(variant_id)
        if record is None:
            findings.append(f"character variant {variant_id} is missing its typed record")
        else:
            if str(record.get("name") or "") != variant_tag or str(record.get("series_id") or "") != source_id:
                findings.append(f"character variant {variant_id} changed spelling or Character Source")
            link = record.get("knowledge_tag") if isinstance(record.get("knowledge_tag"), dict) else {}
            if link.get("status") != "reviewed" or str(link.get("tag_id") or "") != variant_id:
                findings.append(f"character variant {variant_id} has no reviewed representative Tag")
            if link.get("tag_id"):
                validate_stable_tag_reference(
                    link.get("tag_id"), f"character variant {variant_id} representative"
                )
            if variant_type == "outfit":
                if list(record.get("character_ids", [])) != [base_id] or variant_id in character_ids:
                    findings.append(f"character variant {variant_id} has an invalid outfit relationship")
            elif (
                str(record.get("base_character_id") or "") != base_id
                or str(record.get("variant_type") or "") != variant_type
                or variant_id in outfit_ids
            ):
                findings.append(f"character variant {variant_id} has an invalid identity relationship")
        validate_stable_tag_reference(variant_id,f"character variant {variant_id}")
        tag = tags_by_id.get(variant_id)
        relations = tag.get("relations") if isinstance(tag, dict) and isinstance(tag.get("relations"), dict) else {}
        canonical_parents = tuple(dict.fromkeys(
            active_specificity_targets.get(variant_id, ())
        ))
        broader_id = (
            canonical_parents[0]
            if len(canonical_parents) == 1 else
            ""
            if variant_id in specificity_sources else
            str(relations.get("parent") or "")
        )
        if (
            tag is None
            or str(tag.get("display") or "") != variant_tag
            or tag.get("category") != expected_category
            or broader_id != base_id
        ):
            findings.append(f"character variant {variant_id} has an invalid role or broader Tag")
    relationship_ids: set[str] = set()
    for relationship in relationship_records:
        relationship_id = str(relationship.get("id") or "")
        if not relationship_id or relationship_id in relationship_ids:
            findings.append(f"relationship has missing or duplicate ID {relationship_id!r}")
        relationship_ids.add(relationship_id)
        endpoints = relationship.get("endpoints")
        if not isinstance(endpoints, list) or len(endpoints) < 2:
            findings.append(f"relationship {relationship_id} has fewer than two endpoints")
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                findings.append(f"relationship {relationship_id} has an invalid endpoint")
                continue
            endpoint_kind = str(endpoint.get("kind") or "")
            endpoint_id = str(endpoint.get("id") or "")
            if not endpoint_kind or not endpoint_id:
                findings.append(f"relationship {relationship_id} has an empty endpoint")
            elif endpoint_kind == "tag":
                validate_stable_tag_reference(
                    endpoint_id, f"relationship {relationship_id}"
                )
            elif endpoint_kind == "category" and endpoint_id not in category_ids:
                findings.append(
                    f"relationship {relationship_id} references missing category {endpoint_id}"
                )
    appearance = payloads.get("data/defaults/tag_appearance.json", {})
    for scope, known_ids in (("categories", category_ids), ("tags", set(tags_by_id))):
        scoped = appearance.get(scope, {})
        if not isinstance(scoped, dict):
            findings.append(f"tag appearance {scope} is not keyed by stable ID")
            continue
        for stable_id in scoped:
            if scope == "tags":
                validate_stable_tag_reference(stable_id,"tag appearance")
            elif str(stable_id) not in known_ids:
                findings.append(
                    f"tag appearance {scope} references missing ID {stable_id}"
                )
    if not isinstance(appearance.get("rules", []), list):
        findings.append("tag appearance rules must be a list")
    for name in ("community_tags", "character_variant_reviews", "characters", "series", "outfits"):
        source = payloads[f"data/defaults/{name}.json"].get("source")
        if not isinstance(source, dict) or not source.get("name") or not source.get("version"):
            findings.append(f"{name}.json has no portable source name/version")
    for relative in manifest["required_provenance"]:
        if not prepared.get(relative):
            findings.append(f"required provenance/license file is empty: {relative}")
    return findings


def scan_public_tree(
    root: Path,*,allow_git_metadata: bool=False,
) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        lowered = relative.casefold()
        if allow_git_metadata and (
            lowered==".git" or lowered.startswith(".git/")
        ):
            continue
        if path.is_symlink():
            findings.append(f"symlink: {relative}")
            continue
        if path.is_dir():
            if relative == ".git" or (relative not in PUBLIC_GITHUB_DIRECTORIES and any(
                lowered == part.casefold() or lowered.startswith(part.casefold() + "/")
                for part in PRIVATE_PATH_PARTS
            )):
                findings.append(f"private directory: {relative}")
            continue
        if relative in PRIVATE_EXACT_PATHS:
            findings.append(f"private file: {relative}")
        if path.suffix.casefold() not in TEXT_SUFFIXES or relative in CONTENT_SCAN_EXEMPT:
            continue
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        reviewed = REVIEWED_UPSTREAM_MATCHES.get(relative)
        allowed = frozenset()
        if reviewed is not None:
            if hashlib.sha256(raw).hexdigest() == reviewed[0]:
                allowed = reviewed[1]
            else:
                findings.append(f"stale upstream provenance: {relative}")
        for label, pattern in CONTENT_RULES:
            if label == "personal email" and relative.startswith("licenses/"):
                continue
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                if (label, line) in allowed:
                    continue
                findings.append(f"{label}: {relative}:{line}")
                break
    return findings


def verify_public_inventory(
    root: Path,*,allow_git_metadata: bool=False,
) -> list[str]:
    findings: list[str] = []
    manifest_path = root / "public-source.json"
    if not manifest_path.is_file():
        return ["missing public-source.json"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid public-source.json: {exc}"]
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not manifest.get("manifest_excludes_self"):
        return ["public-source.json has no valid self-excluding file inventory"]
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if (
            path.is_file()
            and path != manifest_path
            and not (
                allow_git_metadata
                and path.relative_to(root).parts
                and path.relative_to(root).parts[0].casefold()==".git"
            )
        )
    }
    expected_paths = set(expected)
    for relative in sorted(expected_paths - actual, key=str.casefold):
        findings.append(f"missing inventoried file: {relative}")
    for relative in sorted(actual - expected_paths, key=str.casefold):
        findings.append(f"unlisted exported file: {relative}")
    for relative in sorted(actual & expected_paths, key=str.casefold):
        data = (root / Path(relative)).read_bytes()
        record = expected.get(relative)
        if not isinstance(record, dict):
            findings.append(f"invalid inventory record: {relative}")
            continue
        if record.get("size_bytes") != len(data) or record.get("sha256") != _sha256(data):
            findings.append(f"inventory mismatch: {relative}")
    return findings


def build_public_source(
    root: Path,
    output: Path,
    manifest_path: Path,
    approved_defaults_digest: str,
    *,
    completeness_check: Callable[[], list[str]],
    source_ref: str = "HEAD",
    working_tree: bool = False,
    reviewed_defaults_ref: str | None = None,
    working_tree_reviewed_defaults: set[str] | None = None,
) -> dict[str, Any]:
    completeness = list(completeness_check())
    if completeness:
        raise PublicSourceError("Distribution knowledge is incomplete:\n" + "\n".join(completeness))
    normalized_overrides = {
        _normalized_path(value) for value in (working_tree_reviewed_defaults or set())
    }
    if normalized_overrides and not working_tree:
        raise PublicSourceError("Working-tree reviewed-default overrides require --working-tree.")
    manifest = load_manifest(manifest_path)
    source_reader = _tree_reader(root) if working_tree else _git_reader(root, source_ref)
    defaults_reader = _git_reader(root, reviewed_defaults_ref) if reviewed_defaults_ref else source_reader
    prepared, default_diff, digest = prepare_files(
        manifest,
        source_reader,
        defaults_reader,
        normalized_overrides,
    )
    if not approved_defaults_digest or approved_defaults_digest.casefold() != digest:
        raise PublicSourceError(
            "Reviewed-default approval digest does not match. "
            f"Review the deterministic default diff and approve digest {digest}."
        )
    if manifest.get("export_scope") == "component-source":
        prepared[".gitattributes"] = b"# Preserve exact inventoried component source bytes.\n* -text\n"
    if output.exists() and any(output.iterdir()):
        raise PublicSourceError("Public export destination must not exist or must be empty.")
    output.mkdir(parents=True, exist_ok=True)
    for relative, data in prepared.items():
        target = output / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    default_diff_bytes = _json_bytes(default_diff)
    (output / "reviewed-defaults.diff.json").write_bytes(default_diff_bytes)
    source_manifest = {
        "schema_version": 1,
        "allowlisted_source_file_count": len(manifest["files"]),
        "source_ref": source_ref if not working_tree else "working-tree",
        "reviewed_defaults_ref": reviewed_defaults_ref or (source_ref if not working_tree else "working-tree"),
        "working_tree_reviewed_defaults": sorted(normalized_overrides, key=str.casefold),
        "fresh_history_required": True,
        "manifest_excludes_self": True,
        "reviewed_defaults_digest": digest,
        "files": {
            relative: {"sha256": _sha256(data), "size_bytes": len(data)}
            for relative, data in sorted(prepared.items(), key=lambda item: item[0].casefold())
        },
    }
    if manifest.get("export_scope") == "component-source":
        source_manifest["export_scope"] = "component-source"
        source_manifest["component_ids"] = manifest["component_ids"]
    source_manifest["files"]["reviewed-defaults.diff.json"] = {
        "sha256": _sha256(default_diff_bytes),
        "size_bytes": len(default_diff_bytes),
    }
    (output / "public-source.json").write_bytes(_json_bytes(source_manifest))
    findings = [*verify_public_inventory(output), *scan_public_tree(output)]
    if findings:
        raise PublicSourceError("Public-source privacy scan failed:\n" + "\n".join(findings))
    return source_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a one-way allowlisted CollectiGen public-source export.")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-ref", default="HEAD")
    parser.add_argument("--working-tree", action="store_true")
    parser.add_argument("--reviewed-defaults-ref")
    parser.add_argument("--working-tree-reviewed-default", action="append", default=[])
    parser.add_argument("--show-defaults-digest", action="store_true")
    parser.add_argument("--approve-defaults-digest", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.repository.resolve()
    manifest_path = (args.manifest or root / "tools" / "public-source-manifest.json").resolve()
    manifest = load_manifest(manifest_path)
    source_reader = _tree_reader(root) if args.working_tree else _git_reader(root, args.source_ref)
    defaults_reader = _git_reader(root, args.reviewed_defaults_ref) if args.reviewed_defaults_ref else source_reader
    overrides = set(args.working_tree_reviewed_default)
    if overrides and not args.working_tree:
        raise PublicSourceError("--working-tree-reviewed-default requires --working-tree.")
    _, default_diff, digest = prepare_files(manifest, source_reader, defaults_reader, overrides)
    if args.show_defaults_digest:
        print(json.dumps({"reviewed_defaults_digest": digest, "diff": default_diff}, indent=2))
        return 0
    if not args.output:
        raise PublicSourceError("--output is required unless --show-defaults-digest is used.")
    result = build_public_source(
        root,
        args.output.resolve(),
        manifest_path,
        args.approve_defaults_digest,
        completeness_check=(lambda: []) if manifest.get("export_scope") == "component-source"
            else lambda: audit_private_distribution_completeness(root),
        source_ref=args.source_ref,
        working_tree=args.working_tree,
        reviewed_defaults_ref=args.reviewed_defaults_ref,
        working_tree_reviewed_defaults=overrides,
    )
    print(
        "Public source exported from "
        f"{result['allowlisted_source_file_count']:,} allowlisted source files; "
        f"{len(result['files']):,} non-self files are inventoried."
    )
    print("Initialize a new repository in the export only after publication approval; never copy private history.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicSourceError as exc:
        print(f"Public source export refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
