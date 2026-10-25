"""Explicit canonical-writer admission for configured global-data history."""
from .concept_groups import ConceptGroupError


def collection_history_factory(configuration):
    """Resolve every operation afresh; failed opt-in authority never becomes Off."""
    return _CollectionHistoryFactory(configuration)


class _CollectionHistoryFactory:
    def __init__(self, configuration):
        self.configuration = configuration

    def off_guard(self):
        return self.configuration.off_guard("collections")

    def __call__(self):
        from .collection_history import CollectionHistoryAdapter
        descriptor = self.configuration.resolve("collections")
        if descriptor.status in {"not_configured", "disabled"}:
            return None
        if descriptor.status != "configured":
            raise ConceptGroupError(descriptor.reason)
        try:
            return CollectionHistoryAdapter.from_configuration(self.configuration, descriptor)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise ConceptGroupError("Configured Collection history is unavailable: " + str(exc)) from exc
