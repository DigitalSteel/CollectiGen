# Dataset Close during deferred conflict preparation

BUG-337: the initial conflict-check timer could fire after Dataset requested shutdown, submitting into a stopped background manager. Reuse the page shutdown flag before creating the task. Ordinary conflict preparation remains unchanged.

Acceptance: a queued start after shutdown submits nothing; an active page starts its existing conflict task; actual packaged Organizer Close during first-open has no background-manager exception. This does not qualify Root/Reader, installed update recovery, or model/trainer execution.
