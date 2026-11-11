# Generator Setup in Suite Manager

Manager now hosts the existing full Local Generation (SwarmUI) setup surface:
Connect or install, Run and status, Models and import, and Maintenance.
Open **Applications > Generator Setup**, or **Generator Setup** from an image plan.
When a plan is open, **Return to Generation Plan** keeps the same draft.

Installation, connection, model import, lifecycle and recovery actions reuse the
original Setup services and their explicit review dialogs. Opening Setup starts
no download, runtime or generation. Checked managed and existing installations
can both supply Generate; request readiness still checks the selected model.
Manager owns its model registry, generation preferences and imported presets
beneath its admitted Manager data directory. Existing all-in-one settings remain
separate. Pending operations show activity and elapsed time; Close/Return retains
the host until owned child operations can finish.

Source scope: GEN-09 / GEN-10. Focused tests use generated profiles and fake
runtime I/O. They do not claim an actual SwarmUI installation, runtime lifecycle
or installed-package acceptance. Signed package delivery remains a separate
open delivery row for the unpublished Manager 0.6.0 candidate.

Related Training correction: the retained-result Validation work belongs to
TRN-09 (and TRN-04 performance), not TRN-10 Anima. Historical evidence filenames
are retained so artifact identities remain traceable.

## Dashboard status and return

Manager Dashboard now shows the saved Local Image Generation state, runtime
version, last check, work and resource observations. Saved observations are
labeled; opening Dashboard contacts no service. Set Up Generation and offline
or attention states open the existing Generator Setup. Ready opens the existing
image draft chooser; Busy opens the existing generation work view. These actions
recheck the saved state and current Manager session before dispatch. Returning
from Setup refreshes the same card without changing the Dashboard selection.
