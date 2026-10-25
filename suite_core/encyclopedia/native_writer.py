"""Explicit bridge called by an already initialized, approved native writer."""
from .publication import Publication, publish


def capture_and_publish(*, capture, tags, categories, aliases, relationships,
                        source, revision, guard, catalog_id, generation,
                        destination, expected_token):
    """Supply CollectiGen capture_tag_snapshot explicitly; never initialize core.

    Guard must be reentrant and shared by all native mutations/publications.
    Source attribution/scope and destination require host approval before call.
    """
    # A genuine no-path DTO retains the original no-core-import bridge.
    path = getattr(tags, "path", None)
    if path is None:
        with guard:
            snapshot = capture(tags=tags, categories=categories, aliases=aliases,
                               relationships=relationships, source=source, revision=revision)
            publication = Publication(catalog_id, generation, snapshot)
            return publish(publication, destination, expected_token=expected_token,
                           revision=revision, guard=guard)
    # Every persisted path uses the SAME checked classifier as native capture;
    # no lexical suffix test may dispatch an alias into detached legacy mode.
    import sys
    from importlib import import_module
    package = type(tags).__module__.rpartition(".")[0]
    if not package or package not in sys.modules:
        raise ValueError("Persisted publication requires an already-loaded native leaf namespace")
    snapshot_catalog_admission = import_module(package + ".catalog_admission").snapshot_catalog_admission
    with guard:
        with snapshot_catalog_admission(tags, role="publication") as admission:
            if admission is not None and admission.enrolled and catalog_id != admission.catalog_id:
                raise ValueError("Publication belongs to another catalog incarnation")
            snapshot = capture(tags=tags, categories=categories, aliases=aliases,
                               relationships=relationships, source=source, revision=revision)
            publication = Publication(catalog_id, generation, snapshot)
            return publish(publication, destination, expected_token=expected_token,
                           revision=revision, guard=guard)
