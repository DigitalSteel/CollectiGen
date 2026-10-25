from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .tag_database import TagDatabase, TagRecord
from .tag_collection import normalize_tag


class KnowledgeCurationStaleError(RuntimeError):
    """The saved authorities used by a maintenance preview changed."""


def _fingerprint_path(path: Path) -> str:
    return (
        hashlib.sha256(path.read_bytes()).hexdigest()
        if path.exists() else "missing"
    )


def _atomic_json(path: Path,payload: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(
        json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_bytes(path: Path,content: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+".restore.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _discovery_payload(registry) -> dict:
    return {
        "schema_version":1,
        "discovered_tags":[
            item.to_dict() for item in sorted(
                registry.items.values(),key=lambda item:item.display.casefold(),
            )
        ],
    }


@dataclass
class KnowledgeCurationChange:
    area: str
    detail: str
    tag_id: str = ""
    display: str = ""
    discovery_name: str = ""
    target_id: str = ""


@dataclass
class KnowledgeCurationReport:
    categories_assigned: list[str] = field(default_factory=list)
    category_change_ids: list[str] = field(default_factory=list)
    concepts_assigned: list[str] = field(default_factory=list)
    concept_change_ids: list[str] = field(default_factory=list)
    relations_added: list[str] = field(default_factory=list)
    relation_change_ids: list[str] = field(default_factory=list)
    sensitivity_classified: list[str] = field(default_factory=list)
    sensitivity_change_ids: list[str] = field(default_factory=list)
    discoveries_reconciled: int = 0
    discovery_names: list[str] = field(default_factory=list)
    changes: list[KnowledgeCurationChange] = field(default_factory=list)
    unchanged: int = 0
    backup: Path | None = None
    source_fingerprints: dict[str,str] = field(default_factory=dict)


class KnowledgeCurator:
    """Adds conservative, reusable structure without replacing user decisions."""

    EXACT: dict[str, tuple[str, str | None]] = {
        "gem": ("objects", "object"),
        "grabbing": ("action", "action"),
        "pants pull": ("clothing", "clothing"),
        "panty lift": ("clothing", "clothing"),
        "panty pull": ("clothing", "clothing"),
        "shirt lift": ("clothing", "clothing"),
        "sleeveless": ("clothing", "clothing"),
        "wedgie": ("clothing", "clothing"),
        "sweat": ("effects", "effect"),
        "tree": ("objects", "object"),
        "window": ("objects", "object"),
        "closed eyes": ("appearance_eyes", "eyes"),
        "short hair": ("appearance_hair_length", "hair"),
        "long hair": ("appearance_hair_length", "hair"),
        "very long hair": ("appearance_hair_length", "hair"),
        "full body": ("composition", None),
        "hat": ("clothing_accessories", "hat"),
        "black gloves": ("clothing_accessories", "gloves"),
        "red thighhighs": ("clothing_accessories", "clothing"),
        "white headwear": ("clothing_accessories", "hat"),
        "insect wings": ("species", "wings"),
        "transparent wings": ("species", "wings"),
        "horns": ("species", "species_trait"),
        "leopard ears": ("species", "species_trait"),
        "leopard tail": ("species", "species_trait"),
        "tail": ("appearance_body", "body"),
        "legs": ("appearance_body", "legs"),
        "toes": ("appearance_body", "toes"),
        "midriff": ("appearance_body", "midriff"),
        "penis": ("appearance_body", "body"),
        "horse penis": ("appearance_body", "body"),
        "flower": ("objects", "object"),
        "foliage": ("scene_background", "background"),
        "leaf": ("objects", "object"),
        "plant": ("objects", "object"),
        "pillow": ("objects", "object"),
        "pillar": ("objects", "object"),
        "water": ("scene_background", "background"),
        "river": ("scene_location", "location"),
        "waterfall": ("scene_location", "location"),
        "nature": ("scene", "scene"),
        "scenery": ("scene", "scene"),
        "sky": ("scene_background", "background"),
        "night sky": ("scene_background", "background"),
        "starry sky": ("scene_background", "background"),
        "star (sky)": ("scene_background", "background"),
        "night": ("time", "scene"),
        "full moon": ("time", "scene"),
        "glowing": ("effects", "effect"),
        "light particles": ("effects", "effect"),
        "sparkle": ("effects", "effect"),
        "ice": ("scene_background", "background"),
        "watermark": ("quality", None),
        "mosaic censoring": ("quality", None),
        "uncensored": ("quality", None),
        "wet": ("other", None),
        "heavy breathing": ("expression", "expression"),
        "saliva": ("appearance_face", None),
        "saliva trail": ("appearance_face", None),
        "riding": ("action", "action"),
        "horseback riding": ("action", "action"),
        "licking": ("action", "action"),
        "restrained": ("pose", "pose"),
        "weapon on back": ("action", "action"),
    }

    SUFFIX: tuple[tuple[str, str, str | None], ...] = (
        (" dress", "clothing_dresses", "dress"),
        (" shirt", "clothing_tops", "shirt"),
        (" pants", "clothing_bottoms", "pants"),
        (" shorts", "clothing_bottoms", "shorts"),
        (" sleeves", "clothing_tops", "clothing"),
        (" clothes", "clothing", "clothing"),
        (" swimsuit", "clothing_underwear", "underwear"),
    )

    CATEGORY_CONCEPT = {
        "action": "action", "pose": "pose", "expression": "expression",
        "scene": "scene", "scene_location": "location", "scene_background": "background",
        "objects": "object", "effects": "effect", "species": "species_trait",
        "rating": "rating", "style": "style", "clothing": "clothing",
        "appearance_body": "body", "image_subject_count": "subject_count",
    }

    # Contrary subject-count combinations are canonical image-scoped records in
    # data/defaults/relationships.json. Curation records only the tag semantics.
    SUBJECTS = {
        "1girl": ("female", "1"), "2girls": ("female", "2"),
        "3girls": ("female", "3"), "4girls": ("female", "4"),
        "1boy": ("male", "1"), "2boys": ("male", "2"),
        "3boys": ("male", "3"), "4boys": ("male", "4"),
        "multiple girls": ("female", "multiple"),
        "multiple boys": ("male", "multiple"), "solo": ("all", "1"),
    }

    CATEGORY_REPAIRS = {
        "long hair": ("hair", "appearance_hair_length"),
        "short shorts": ("shorts", "clothing_bottoms"),
    }

    SENSITIVITY: dict[str, tuple[str, list[str]]] = {
        "general": ("general", []),
        "sensitive": ("sensitive", []),
        "questionable": ("questionable", []),
        "explicit": ("explicit", []),
        "animal penis": ("explicit", ["genitalia"]),
        "anus": ("explicit", ["genitalia"]),
        "bestiality": ("explicit", ["sexual activity", "bestiality"]),
        "child on child": ("explicit", ["sexual activity", "minor sexual content"]),
        "clitoral stimulation": ("explicit", ["sexual activity"]),
        "clitoris": ("explicit", ["genitalia"]),
        "cum": ("explicit", ["sexual fluids"]),
        "cunnilingus": ("explicit", ["sexual activity"]),
        "dildo": ("explicit", ["sex toy"]),
        "female ejaculation": ("explicit", ["sexual fluids"]),
        "female masturbation": ("explicit", ["sexual activity"]),
        "fingering": ("explicit", ["sexual activity"]),
        "fingering through clothes": ("explicit", ["sexual activity"]),
        "fingering through panties": ("explicit", ["sexual activity"]),
        "masturbation through clothes": ("explicit", ["sexual activity"]),
        "penis": ("explicit", ["genitalia"]),
        "pussy juice": ("explicit", ["sexual fluids"]),
        "sex": ("explicit", ["sexual activity"]),
        "sex toy": ("explicit", ["sex toy"]),
        "spread pussy": ("explicit", ["genitalia", "sexual activity"]),
        "vaginal object insertion": ("explicit", ["sexual activity"]),
        "vibrator": ("explicit", ["sex toy"]),
        "gore": ("explicit", ["graphic violence", "gore"]),
        "guro": ("explicit", ["graphic violence", "gore"]),
        "disembowelment": ("explicit", ["graphic violence", "gore"]),
        "decapitation": ("explicit", ["graphic violence", "gore"]),
        "beheading": ("explicit", ["graphic violence", "gore"]),
        "severed head": ("explicit", ["graphic violence", "gore"]),
        "severed limb": ("explicit", ["graphic violence", "gore"]),
        "severed body part": ("explicit", ["graphic violence", "gore"]),
        "vivisection": ("explicit", ["graphic violence", "gore"]),
        "blood": ("sensitive", ["violence"]),
        "corpse": ("sensitive", ["violence"]),
        "injury": ("sensitive", ["violence"]),
        "wound": ("sensitive", ["violence"]),
        "nude": ("questionable", ["nudity"]),
        "naked": ("questionable", ["nudity"]),
        "topless": ("questionable", ["nudity"]),
        "bottomless": ("questionable", ["nudity"]),
        "nipples": ("questionable", ["nudity"]),
        "lingerie": ("sensitive", ["suggestive content"]),
        "underwear": ("sensitive", ["suggestive content"]),
        "panties": ("sensitive", ["suggestive content"]),
        "bra": ("sensitive", ["suggestive content"]),
        "cleavage": ("sensitive", ["suggestive content"]),
        "bdsm": ("sensitive", ["suggestive content"]),
        "bondage outfit": ("sensitive", ["suggestive content"]),
        "bondage": ("sensitive", ["suggestive content"]),
        "crotchless panties": ("sensitive", ["suggestive content"]),
        "cupless bra": ("sensitive", ["suggestive content"]),
        "open bra": ("sensitive", ["suggestive content"]),
        "underwear only": ("sensitive", ["suggestive content"]),
        "bra lift": ("questionable", ["suggestive content"]),
        "panties aside": ("questionable", ["suggestive content"]),
        "thong aside": ("questionable", ["suggestive content"]),
        "bikini top lift": ("questionable", ["suggestive content"]),
        "hand in panties": ("questionable", ["suggestive content"]),
    }

    def __init__(
        self,database: TagDatabase,
        reviewed_sensitivity: dict[str,tuple[str,list[str]]] | None=None,
        *,guard_paths=(),
    ) -> None:
        self.database=database
        self.reviewed_sensitivity=reviewed_sensitivity or self.SENSITIVITY
        self.guard_paths=tuple(Path(value) for value in guard_paths)

    def _fingerprints(self,discovery_registry=None) -> dict[str,str]:
        paths=[self.database.path]
        if discovery_registry is not None:
            paths.append(discovery_registry.path)
        paths.extend(self.guard_paths)
        return {str(path):_fingerprint_path(path) for path in paths}

    def _proposal(self, record: TagRecord) -> tuple[str | None, str | None]:
        tag = normalize_tag(record.display)
        if tag in self.SUBJECTS:
            return "image_subject_count", "subject_count"
        if tag in self.EXACT:
            return self.EXACT[tag]
        for suffix, category, concept in self.SUFFIX:
            if tag.endswith(suffix):
                return category, concept
        return None, None

    def preview(self, *, discovery_registry=None) -> KnowledgeCurationReport:
        """Return the exact deterministic changes without touching live stores."""
        before=self._fingerprints(discovery_registry)
        with TemporaryDirectory() as folder:
            shadow=deepcopy(self.database)
            shadow.path=Path(folder)/"tags.json"
            shadow._loaded_bytes=None
            shadow_discovery=deepcopy(discovery_registry) if discovery_registry is not None else None
            if shadow_discovery is not None:
                shadow_discovery.path=Path(folder)/"discovered.json"
            report=KnowledgeCurator(shadow,self.reviewed_sensitivity).apply(
                backup=False,discovery_registry=shadow_discovery,
            )
        after=self._fingerprints(discovery_registry)
        if before!=after:
            raise KnowledgeCurationStaleError(
                "System Knowledge or its reviewed maintenance authorities "
                "changed while the safe-fix report was being prepared. Scan "
                "and review it again."
            )
        report.source_fingerprints=after
        return report

    def _create_reviewed_backup(self,discovery_registry):
        stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory=(
            self.database.path.parent/"backups"/"system-knowledge-curation"/
            f"curation_{stamp}_{uuid4().hex[:8]}"
        )
        directory.mkdir(parents=True,exist_ok=False)
        entries=[]
        for index,path in enumerate((self.database.path,discovery_registry.path)):
            backup=directory/f"{index}_{path.name}"
            existed=path.exists()
            if existed:
                shutil.copy2(path,backup)
            entries.append({
                "target":str(path.resolve()),"backup":backup.name,
                "existed":existed,
            })
        manifest=directory/"recovery.json"
        _atomic_json(manifest,{
            "schema_version":1,
            "kind":"system_knowledge_curation",
            "created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status":"applying","files":entries,
        })
        return directory,manifest,entries

    @staticmethod
    def _restore_reviewed_backup(directory,entries,written_targets):
        errors=[]
        for entry in entries:
            target=Path(entry["target"])
            if target not in written_targets:
                continue
            try:
                if entry["existed"]:
                    _atomic_bytes(
                        target,(directory/entry["backup"]).read_bytes(),
                    )
                elif target.exists():
                    target.unlink()
            except Exception as exc:
                errors.append(f"{target}: {exc}")
        return errors

    def apply_reviewed(
        self,preview: KnowledgeCurationReport,*,discovery_registry,
        selected_tag_ids: set[str] | None=None,
        selected_discovery_names: set[str] | None=None,
    ) -> KnowledgeCurationReport:
        """Apply one exact reviewed report as a verified two-file transaction."""
        requested_tag_ids=(
            None if selected_tag_ids is None
            else {str(value) for value in selected_tag_ids}
        )
        requested_discoveries=(
            None if selected_discovery_names is None
            else {normalize_tag(value) for value in selected_discovery_names}
        )
        reviewed_tag_ids={
            str(change.tag_id) for change in preview.changes if change.tag_id
        }
        reviewed_discoveries={
            normalize_tag(value) for value in preview.discovery_names
        }
        if (
            requested_tag_ids is not None
            and not requested_tag_ids.issubset(reviewed_tag_ids)
        ) or (
            requested_discoveries is not None
            and not requested_discoveries.issubset(reviewed_discoveries)
        ):
            raise ValueError(
                "Checked System Knowledge changes must come from the reviewed "
                "safe-fix report."
            )
        expected=dict(getattr(preview,"source_fingerprints",{}) or {})
        if not expected or self._fingerprints(discovery_registry)!=expected:
            raise KnowledgeCurationStaleError(
                "System Knowledge or the reviewed discovery list changed after "
                "this safe-fix review. Scan and review it again."
            )
        with TemporaryDirectory() as folder:
            staged_knowledge=deepcopy(self.database)
            staged_knowledge.path=Path(folder)/"tags.json"
            staged_knowledge._loaded_bytes=None
            staged_discovery=deepcopy(discovery_registry)
            staged_discovery.path=Path(folder)/"discovered.json"
            report=KnowledgeCurator(
                staged_knowledge,self.reviewed_sensitivity,
            ).apply(
                backup=False,discovery_registry=staged_discovery,
                selected_tag_ids=requested_tag_ids,
                selected_discovery_names=requested_discoveries,
            )
            expected_knowledge=staged_knowledge._payload()
            expected_discovery=_discovery_payload(staged_discovery)
            tag_should_write=any(
                bool(change.tag_id) for change in report.changes
            )
            discovery_should_write=bool(report.discoveries_reconciled)

        if self._fingerprints(discovery_registry)!=expected:
            raise KnowledgeCurationStaleError(
                "System Knowledge or the reviewed discovery list changed while "
                "the checked fixes were being prepared. Nothing was written; "
                "scan and review them again."
            )
        directory,manifest,entries=self._create_reviewed_backup(
            discovery_registry,
        )
        manifest_payload=json.loads(manifest.read_text(encoding="utf-8"))
        written_targets=set()
        try:
            tag_key=str(self.database.path)
            if _fingerprint_path(self.database.path)!=expected[tag_key]:
                raise KnowledgeCurationStaleError(
                    "System Knowledge changed before the checked fixes could "
                    "be saved. Nothing was written; scan and review them again."
                )
            if tag_should_write:
                _atomic_json(self.database.path,expected_knowledge)
                written_targets.add(self.database.path.resolve())

            remaining=(discovery_registry.path,*self.guard_paths)
            if any(
                _fingerprint_path(path)!=expected[str(path)]
                for path in remaining
            ):
                raise KnowledgeCurationStaleError(
                    "The reviewed discovery list or maintenance authorities "
                    "changed before the checked fixes could be saved. The "
                    "System Knowledge write was restored; scan again."
                )
            if discovery_should_write:
                _atomic_json(discovery_registry.path,expected_discovery)
                written_targets.add(discovery_registry.path.resolve())

            actual_knowledge=TagDatabase(self.database.path)._payload()
            actual_discovery=_discovery_payload(
                type(discovery_registry)(discovery_registry.path)
            )
            if actual_knowledge!=expected_knowledge:
                raise RuntimeError(
                    "System Knowledge failed read-back verification."
                )
            if actual_discovery!=expected_discovery:
                raise RuntimeError(
                    "The reviewed discovery list failed read-back verification."
                )
            manifest_payload.update({
                "status":"complete",
                "tag_records":len({
                    change.tag_id for change in report.changes if change.tag_id
                }),
                "discoveries_reconciled":report.discoveries_reconciled,
            })
            _atomic_json(manifest,manifest_payload)
        except Exception as exc:
            recovery_errors=self._restore_reviewed_backup(
                directory,entries,written_targets,
            )
            manifest_payload.update({
                "status":"recovery_failed" if recovery_errors else "restored",
                "error":str(exc),
            })
            if recovery_errors:
                manifest_payload["recovery_errors"]=recovery_errors
            try:
                _atomic_json(manifest,manifest_payload)
            except Exception as manifest_exc:
                recovery_errors.append(
                    f"Could not update the recovery record: {manifest_exc}"
                )
            state=(
                "Recovery needs attention"
                if recovery_errors else "The original files were restored"
            )
            if isinstance(exc,KnowledgeCurationStaleError):
                raise KnowledgeCurationStaleError(
                    f"{exc} {state}. Recovery record: {manifest}"
                ) from exc
            raise RuntimeError(
                f"{state} after System Knowledge maintenance stopped. {exc} "
                f"Recovery record: {manifest}"
            ) from exc
        report.backup=directory
        report.source_fingerprints=self._fingerprints(discovery_registry)
        return report

    def apply(
        self,
        *,
        backup: bool = True,
        discovery_registry=None,
        selected_tag_ids: set[str] | None = None,
        selected_discovery_names: set[str] | None = None,
    ) -> KnowledgeCurationReport:
        report = KnowledgeCurationReport()
        if backup and self.database.path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = self.database.path.parent / "backups" / f"tags_before_curation_{stamp}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.database.path, target); report.backup = target

        records = list(self.database.tags.values())
        for record in records:
            if selected_tag_ids is not None and record.id not in selected_tag_ids:
                continue
            category, concept = self._proposal(record)
            tag = normalize_tag(record.display)
            repair = self.CATEGORY_REPAIRS.get(tag)
            if repair and record.category == repair[0]:
                record.category = repair[1]; report.categories_assigned.append(record.display); report.category_change_ids.append(record.id)
                report.changes.append(KnowledgeCurationChange("Category","Assign reviewed category",record.id,record.display,target_id=record.category))
            if record.category == "uncategorized" and category:
                record.category = category; report.categories_assigned.append(record.display); report.category_change_ids.append(record.id)
                report.changes.append(KnowledgeCurationChange("Category","Assign reviewed category",record.id,record.display,target_id=record.category))
            if not record.concept_id:
                concept = concept or self.CATEGORY_CONCEPT.get(record.category)
                if concept:
                    record.concept_id = concept; report.concepts_assigned.append(record.display); report.concept_change_ids.append(record.id)
                    report.changes.append(KnowledgeCurationChange("Reusable meaning",f"Link reviewed reusable meaning {concept}",record.id,record.display))

            sensitivity = self.reviewed_sensitivity.get(tag)
            if sensitivity and record.content_sensitivity.classification_status == "unclassified":
                rating, flags = sensitivity
                record.content_sensitivity.minimum_rating = rating
                record.content_sensitivity.content_flags = list(flags)
                record.content_sensitivity.classification_confidence = 1.0
                record.content_sensitivity.classification_status = "reviewed"
                record.content_sensitivity.provenance.append({
                    "source": "tagmanager_reviewed_knowledge",
                    "rule": "obvious_content_sensitivity_v3",
                })
                report.sensitivity_classified.append(record.display); report.sensitivity_change_ids.append(record.id)
                report.changes.append(KnowledgeCurationChange("Sensitivity",f"Use reviewed {rating} classification",record.id,record.display))
            if tag in self.SUBJECTS:
                kind, count = self.SUBJECTS[tag]
                subject_changes = []
                if "subject_kind" not in record.properties:
                    record.properties["subject_kind"] = kind
                    subject_changes.append(f"subject kind {kind}")
                if "count" not in record.properties:
                    record.properties["count"] = count
                    subject_changes.append(f"subject count {count}")
                if subject_changes:
                    report.changes.append(KnowledgeCurationChange(
                        "Subject semantics",
                        f"Record reviewed {' and '.join(subject_changes)}",
                        record.id,
                        record.display,
                    ))
        if discovery_registry is not None:
            known={normalize_tag(item.display) for item in records}
            selected_discoveries=None if selected_discovery_names is None else {normalize_tag(value) for value in selected_discovery_names}
            stale=[
                item.display for key,item in discovery_registry.items.items()
                if key in known and (selected_discoveries is None or key in selected_discoveries)
            ]
            report.discovery_names=sorted(stale,key=str.casefold)
            report.changes.extend(
                KnowledgeCurationChange("Discovery","Remove duplicate discovery entry",display=value,discovery_name=value)
                for value in report.discovery_names
            )
            report.discoveries_reconciled=discovery_registry.reconcile_known(stale)
        report.unchanged = sum(1 for item in records if item.display not in report.categories_assigned and item.display not in report.concepts_assigned and item.display not in report.sensitivity_classified)
        self.database.save()
        return report
