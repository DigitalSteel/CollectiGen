# CollectiGen Suite Manager

**Suite Manager a.0.7.4** provides component installation plus dedicated Generate
and Training workspaces. Use the complete Windows ZIP for a fresh installation.

[Release and notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.7.4-alpha.1) ·
[Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.7.4-alpha.1/SuiteManager-a.0.7.4-windows-x64.zip) ·
[SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.7.4-alpha.1/SuiteManager-a.0.7.4-windows-x64.zip.sha256)

Extract into a new, short writable folder such as `C:\CollectiGen`, keep the
complete folder together, and run **CollectiGen.exe**. Long installation paths
can prevent Qt's Windows plugin from loading. Keep the previous package and
separate data backups. Review and confirm each installation; restart when asked.

Generate and Manager update actions wrap to the available window width. Tab moves
from update notes to the next action. Existing drafts, confirmation and recovery
stay with their original owners.

Find interrupted Training packages from **Training > Review Interrupted Training Package**. Use **Choose Package Location** to list packages by name, or **Browse Exact Receipt** for advanced selection. Search shows elapsed activity and **Cancel Safely**;review leaves package files unchanged.

Manager a.0.7.4 checks GitHub for newer compatible signed Reader and Organizer releases. Ordinary module updates no longer require a new Manager build. Review and confirm each installation in Applications. Generate and Training remain in Manager.

Training Package Review now shows the target, image counts and next preparation
action. It distinguishes package verification from unchecked trainer readiness
and returns to **Training**. History management and the a.0.6.7 launcher fixes remain included.

Training history and saved SDXL preparations now include **Forget From This List**
and **Recover Saved List**. Forget removes only the saved-list entry; reviews,
checkpoints, outputs and registrations remain in place. Recovery finishes the
original pending list transaction within the active Manager session.

## Workspaces

Use the **Help** group or **F1** in Generate and Training for the matching
workflow guide. Reading Help preserves your open work and starts no operation.

- **Dashboard** summarizes suite and saved generator readiness and links to the
  work that needs attention.
- **Generate** creates or reopens image and video drafts, reviews generation
  jobs, and reconnects a checked local generator. The existing editor owns
  prompts, model choices, settings, alternatives, queues, results and recovery.
  The draft switcher retains multiple image and video requests. Model-guided
  planning opens a separate request without replacing another draft; Close
  consults every retained request. Saved setups also open in a separate draft
  while retaining the image and video work already open. Plan a Remix and
  Plan Remix from Verified Result now open separate drafts as well; cancelling
  source selection or refusing a changed result preserves existing work.
- **Training** opens an Organizer data package, reviews a local trainer, and
  prepares SDXL or Anima training through the existing workflow controllers.
  The open-review selector returns to an exact retained run or setup review
  without starting it again. Saved runs, preparations, experiments, sample comparisons and interrupted
  package recovery remain available here. Comparisons return to Training.
- **Applications** installs or connects Reader and Organizer and provides the
  existing Models and Ollama workspaces. Library files stay with Organizer.
- **Databases** connects and reviews Tag Database content before Apply, retaining
  local corrections and the existing content recovery route.
- **Servers** reads local CPU, RAM and GPU observations on explicit refresh.
  It also links to configured external model runners. Observations do not
  establish that a model fits or that a generator is ready.
- **Dependencies** and **Settings** retain component status, shared locations,
  presentation and the existing recovery/setup links.

## Working across components

Prepare a collection, captions and a Training package in Organizer, then open
that package in Manager's Training workspace. Saved Dataset drafts can also be
exported from Organizer for source and caption review in Manager. Completed
Training results can be registered and reviewed for use in Generate.

Generate keeps neutral drafts available offline. Submission uses a separately
checked local runtime and an explicit Start action. Verified generated output
returns through Organizer's existing Add Images review; original output and
raw generation lineage are preserved rather than promoted into confirmed tags.

Applications discovers compatible signed Reader and Organizer releases.
Currently available: Reader a.0.3.1 and Organizer a.0.5.5. Review and confirm installation.
Tag Database is reviewed content, rather than another application installer.
For a.0.6.6 and earlier, use the full Windows ZIP in a new folder to receive
the fixed launcher. The built-in update in those versions cannot replace it.
The a.0.6.7 → a.0.6.8 update and reopening passed in an isolated installation.
Rollback testing remains incomplete. Keep your previous folder and separate
data backups.

## Source startup

Use the release tag or its matching source asset to reproduce this version; the
default branch may be a landing/source checkpoint.

Run `python -B -m suite_core.manager` from a checkout with Python and PySide6.
Use `--theme system|dark|light|midnight` and `--color-blind-mode` for presentation.
`--demo` displays synthetic data. No dated task directory or `--suite-source`
is needed. A normal host supplies its own admitted profile and component
services; Demo does not authorize installations, execution or private data reads.

Keep component versions, signed package identities and rollback artifacts with
their release. See `WHATS_NEW.md` for versioned release changes. Historical
qualification receipts remain evidence for their exact original artifacts;
they do not qualify a rebuilt candidate.
