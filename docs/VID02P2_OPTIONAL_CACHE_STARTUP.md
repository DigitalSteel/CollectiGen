# Optional runtime startup cache refresh

VID-02P2, linked to VID-02P1, VID-02, VID-03 and SUITE-10B. November 11, 2026.

The real Organizer candidate failed Dataset preparation while two dependency-service paths refreshed Python's import caches. The retained traceback ends in PathFinder.invalidate_caches with a KeyError for an importer-cache entry. The exception prevented normal Dataset/Ingest readiness; it was not a missing runtime or manual-testing criterion.

All four existing dependency cache-refresh sites now use one module RLock. Requests still refresh exactly once, errors still propagate, and no retry, cache suppression or global interpreter patch is introduced. The lock serializes only cache invalidation; installation, removal, recovery and profile validation keep their existing authorities.

The focused two-thread overlap/error-propagation regressions and affected dependency tests passed 40 cases. The earlier 52-case path/profile/dependency run is retained; those runs represent 54 distinct cases, not 92. Native package startup, saved playback/Close and signed delivery remain separate qualification recorded by the candidate's delivery receipts. Source checks alone are not a delivered update.

Retained evidence: vid02-packaged-codec-875x19s3/DATASET_PREPARATION_EXCEPTION.txt, CACHE_SERIALIZATION_TESTS.xml and existing failed packaged UI results. No Root/Reader/C3A trial or user manual-testing hold is added.
