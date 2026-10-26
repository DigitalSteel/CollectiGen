from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from .annotation import GenerationRecord, ImageAnnotation
from .caption_import import parse_generated_caption
from .metadata_sync import MetadataSyncService
from .tag_collection import TagCollection, normalize_tag


@dataclass
class EvidenceSource:
    name: str
    available: bool = False
    tags: list[str] = field(default_factory=list)
    plain_language: list[str] = field(default_factory=list)
    ambiguous_fragments: list[str] = field(default_factory=list)
    type_issues: list[str] = field(default_factory=list)
    raw_text: str = ""
    detected_format: str = ""
    app_owned_projection: bool = False
    details: list[str] = field(default_factory=list)
    generation_records: list[GenerationRecord] = field(default_factory=list)
    raw_generation_fields: dict[str, object] = field(default_factory=dict)
    standard_metadata: dict[str, object] = field(default_factory=dict)
    tag_fields: dict[str, object] = field(default_factory=dict)
    description_fields: dict[str, object] = field(default_factory=dict)
    error: str = ""
    semantic_lookup_names: list[str] | None = None


@dataclass
class ImageEvidenceReport:
    image: Path
    sources: list[EvidenceSource]

    @property
    def all_tags(self) -> list[str]:
        combined = TagCollection()
        for source in self.sources:
            combined.extend(source.tags)
        return combined.to_list()

    @property
    def all_plain_language(self) -> list[str]:
        combined: list[str] = []
        known: set[str] = set()
        for source in self.sources:
            for raw in source.plain_language:
                value=str(raw).strip(); key=" ".join(value.casefold().split())
                if value and key not in known:
                    combined.append(value); known.add(key)
        return combined

    def tag_sources(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        display: dict[str, str] = {}
        for source in self.sources:
            for tag in source.tags:
                key = normalize_tag(tag)
                display.setdefault(key, tag)
                result.setdefault(key, []).append(source.name)
        return {display[key]: names for key, names in result.items()}


def workflow_source_lookup_names(report):
    """Return complete emitted/queried TXT semantic inputs, including unknowns."""
    sources=[source for source in getattr(report,"sources",()) if source.name=="TXT caption"]
    if not sources or any(source.error or getattr(source,"semantic_lookup_names",None) is None for source in sources):
        return None
    return sorted({value for source in sources for value in source.semantic_lookup_names})


class ImageEvidenceInspector:
    """Read every non-destructive evidence source associated with an image."""

    def __init__(self, metadata: MetadataSyncService | None = None, *, knowledge=None) -> None:
        self.metadata = metadata or MetadataSyncService()
        self.knowledge = knowledge

    def inspect(self, image: Path, *, include_metadata: bool = True) -> ImageEvidenceReport:
        image = Path(image)
        json_source = self._read_json(image.with_suffix(".tags.json"), image.name)
        projection_state = None
        confirmed_tags: list[str] = []
        if json_source.available and not json_source.error:
            try:
                annotation = ImageAnnotation.from_dict(
                    json.loads(image.with_suffix(".tags.json").read_text(encoding="utf-8")),
                    image.name,
                )
                projection_state = annotation.projection_state.get("txt")
                confirmed_tags = annotation.all_structured_tags()
            except Exception:
                projection_state = None
        sources = [self._read_txt(image.with_suffix(".txt"), projection_state, confirmed_tags), json_source]
        if include_metadata:
            sources.append(self._read_metadata(image))
        return ImageEvidenceReport(
            image=image,
            sources=sources,
        )

    def _read_txt(self, path: Path, projection_state=None, confirmed_tags=()) -> EvidenceSource:
        source = EvidenceSource("TXT caption", available=path.exists(),semantic_lookup_names=[])
        if not path.exists():
            return source
        try:
            raw_text = path.read_bytes().decode("utf-8")
            confirmed={normalize_tag(value) for value in confirmed_tags}
            def known(value):
                source.semantic_lookup_names.append(str(value))
                return (normalize_tag(value) in confirmed
                    or (self.knowledge is not None and self.knowledge.find(value) is not None))
            parsed = parse_generated_caption(raw_text, known_tags=known, projection_state=projection_state)
            # Include emitted values even when caption typing itself did not
            # consult vocabulary; their later readiness identities still can.
            source.semantic_lookup_names.extend((*parsed.sdxl_tags,*parsed.plain_language,*parsed.ambiguous_fragments))
            source.tags = parsed.sdxl_tags
            source.plain_language = parsed.plain_language
            source.ambiguous_fragments = parsed.ambiguous_fragments
            source.raw_text = parsed.raw_text
            source.detected_format = parsed.format
            source.app_owned_projection = parsed.app_owned_projection
        except Exception as exc:
            source.error = str(exc)
        return source

    @staticmethod
    def _read_json(path: Path, image_name: str) -> EvidenceSource:
        source = EvidenceSource("JSON sidecar", available=path.exists())
        if not path.exists():
            return source
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            annotation = ImageAnnotation.from_dict(payload, image_name)
            source.tags = annotation.all_structured_tags()
            source.plain_language = list(annotation.plain_language)
            source.generation_records = list(annotation.generation_records)
            source.standard_metadata = dict(annotation.standard_metadata)
        except Exception as exc:
            source.error = str(exc)
        return source

    def _read_metadata(self, image: Path) -> EvidenceSource:
        source = EvidenceSource("Image metadata", available=image.exists())
        try:
            values = self.metadata.read_embedded(image)
            source.tags = values.get("tags", [])
            source.plain_language = values.get("plain_language", [])
            source.details = values.get("generation_info", [])
            source.generation_records = [
                GenerationRecord.from_dict(item)
                for item in values.get("generation_records", [])
                if isinstance(item, dict)
            ]
            source.raw_generation_fields = dict(values.get("raw_generation_fields", {}))
            source.standard_metadata = dict(values.get("standard_metadata", {}))
            source.tag_fields = dict(values.get("tag_fields", {}))
            source.description_fields = dict(values.get("description_fields", {}))
            source.ambiguous_fragments = list(values.get("ambiguous_fragments", []))
            source.type_issues = list(values.get("type_issues", []))
            source.available = bool(
                source.tags or source.plain_language or source.details
                or source.ambiguous_fragments or source.type_issues
                or source.generation_records or source.standard_metadata
            )
        except Exception as exc:
            source.available = False
            source.error = str(exc)
        return source
