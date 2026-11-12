# Consistent Organizer saved-plan label

BUG-339: late Dataset creation can bind the shared Open Training label after the Organizer creation hook has already run. The action still resumes Dataset drafts, but its wording incorrectly suggests a Training execution workspace.

Reuse the existing Organizer-only label policy during final host command rendering as well as page creation. Retain the same source button, handler, readiness and all-in-one navigation; do not introduce a second resume command. Acceptance covers late command binding, idempotent refresh and actual packaged Dataset rendering/Close.
