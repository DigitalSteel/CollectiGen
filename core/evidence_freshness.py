from __future__ import annotations

from collections.abc import Iterable

from .ai_interrogation import split_plain_language
from .image_fact_decisions import ImageFindingDecisionService
from .settings import normalize_plain_language_exclusion
from .tag_collection import normalize_tag


def current_evidence_keys_by_source(report) -> dict[str, dict[str, set[str]]]:
    """Return successfully read typed evidence, keyed by exact source family."""

    result={"tag":{},"description":{}}
    for source in getattr(report,"sources",()):
        if str(getattr(source,"error","") or "").strip():
            continue
        source_key=str(getattr(source,"name","") or "Unknown").strip().casefold()
        if not source_key:
            continue
        result["tag"][source_key]={
            normalize_tag(value)
            for value in getattr(source,"tags",())
            if normalize_tag(value)
        }
        result["description"][source_key]={
            normalize_plain_language_exclusion(sentence)
            for raw in getattr(source,"plain_language",())
            for sentence in split_plain_language(raw)
            if normalize_plain_language_exclusion(sentence)
        }
    return result


def current_candidate_sources(
    value: str,kind: str,sources: Iterable[str],
    current_keys: dict[str, dict[str, set[str]]],
) -> tuple[str, ...]:
    """Keep unknown/unreadable sources and exact values from current readable sources."""

    source_values=tuple(dict.fromkeys(
        str(source).strip() for source in sources if str(source).strip()
    ))
    evidence_kind="tag" if kind=="tag" else "description"
    key=(
        normalize_tag(value) if evidence_kind=="tag"
        else normalize_plain_language_exclusion(value)
    )
    live=current_keys.get(evidence_kind,{})
    return tuple(
        source for source in source_values
        if source.casefold() not in live or key in live[source.casefold()]
    )


def current_source_fingerprint(record, name: str) -> str:
    """Reuse a saved digest only while the source's cheap file signature matches."""
    base = ImageFindingDecisionService(record.annotation).source_fingerprint(name)
    source_key = str(name or "").strip().casefold()
    fingerprints = record.annotation.source.get("source_fingerprints", {})
    fingerprints = fingerprints if isinstance(fingerprints, dict) else {}
    if source_key == "txt caption":
        path, stored, prefix = record.caption_path, fingerprints.get("txt", {}), "txt-live"
    elif source_key == "image metadata":
        metadata = fingerprints.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        path, stored = record.image_path, metadata.get("input", {})
        prefix = f"metadata-live:{metadata.get('reader_version', '')}"
    else:
        return base
    stored = stored if isinstance(stored, dict) else {}
    try:
        stat = path.stat()
    except OSError:
        return base
    if stored.get("size") == stat.st_size and stored.get("mtime_ns") == stat.st_mtime_ns:
        return base
    return f"{prefix}:{stat.st_size}:{stat.st_mtime_ns}"


def current_candidate_findings(cached_findings, report, *, record=None) -> list[dict]:
    """Project current typed sources for review without editing saved evidence.

    Fresh observations join cached ones; only successfully read sources can
    retire an old observation. Decisions remain the finding service's job.
    """
    current_keys = current_evidence_keys_by_source(report)
    fingerprints = {
        source.name: current_source_fingerprint(record, source.name)
        for source in getattr(report, "sources", ())
        if record is not None and not source.error
    }
    findings: dict[tuple[str, str], dict] = {}

    def add(kind, value, sources):
        key = normalize_tag(value) if kind == "tag" else normalize_plain_language_exclusion(value)
        if not key:
            return
        row = findings.setdefault((kind, key), {"kind": kind, "value": value, "sources": []})
        for source in sources or ("Unknown",):
            if source not in row["sources"]:
                row["sources"].append(source)
            if source in fingerprints:
                row.setdefault("source_fingerprints", {})[source] = fingerprints[source]

    for source in getattr(report, "sources", ()):
        if str(getattr(source, "error", "") or "").strip():
            continue
        name = str(getattr(source, "name", "") or "Unknown").strip() or "Unknown"
        for value in getattr(source, "tags", ()):
            add("tag", value, (name,))
        for raw in getattr(source, "plain_language", ()):
            for sentence in split_plain_language(raw):
                add("description", sentence, (name,))
    for finding in cached_findings if isinstance(cached_findings, list) else ():
        if not isinstance(finding, dict) or finding.get("kind") not in {"tag", "description"}:
            continue
        kind = finding["kind"]
        sources = finding.get("sources")
        sources = sources if isinstance(sources, (list, tuple)) else []
        values = ((str(finding.get("value") or ""),) if kind == "tag"
                  else split_plain_language(finding.get("value")))
        for value in values:
            active = current_candidate_sources(value, kind, sources, current_keys)
            if not sources or active:
                add(kind, value, active)
    return list(findings.values())
