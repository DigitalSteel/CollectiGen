# Media Organizer a.0.4.14

2026-11-03 (America/New_York)

Category Details now checks its original save-readiness callback for every result
route,including accept/done,Close and Escape. A pending or refused save keeps the
dialog open. Reentrant completion cannot bypass readiness or deliver twice;
changed owners/callbacks and callback errors retain a deliberate retry boundary.
The Qt Close event is refused before readiness runs,so an exception cannot leave
closure admitted.

This compatible patch changes only the small dialog completion class. The actual
editor,autosave,flush,discard,store and recovery authorities are unchanged. All
earlier fixes remain included. The public host has no Learning reader integration;
that separate source work is not shipped. VALIDATION.json distinguishes generated
callback-only component evidence and isolated idle host lifecycle/update recovery
from actual Category editor flush/save/discard,private data,original modal/native,
DPI/AT,clean-machine/independent novice and live Manager acceptance. Those remain
open. No data schema,native library or unattended-update change.
