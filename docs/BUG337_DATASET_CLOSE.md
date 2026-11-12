# Dataset Close during deferred conflict preparation

BUG-337: the initial conflict-check timer could fire after Dataset requested shutdown, submitting into a stopped background manager. Reuse the page shutdown flag and the canonical job manager shutting_down property before creating the task. A lazily created page can have its page flag unset after global shutdown begins; both boundaries matter. Ordinary conflict preparation remains unchanged.

Acceptance: a queued start after page or global shutdown submits nothing; an active page starts its existing conflict task; actual packaged Organizer Close during first-open has no background-manager exception. This does not qualify Root/Reader, installed update recovery, or model/trainer execution.
