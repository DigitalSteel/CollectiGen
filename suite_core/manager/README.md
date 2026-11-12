# CollectiGen Suite Manager

**Suite Manager a.0.6.3** provides component installation plus dedicated Generate
and Training workspaces. Use the complete Windows ZIP for a fresh installation.

[Release and notes](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.suite-manager/v0.6.3-alpha.1) ·
[Windows ZIP](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.6.3-alpha.1/SuiteManager-a.0.6.3-windows-x64.zip) ·
[SHA-256](https://github.com/DigitalSteel/CollectiGen/releases/download/collectigen.suite-manager/v0.6.3-alpha.1/SuiteManager-a.0.6.3-windows-x64.zip.sha256)

Extract into a new, short writable folder such as `C:\CollectiGen`, keep the
complete folder together, and run **CollectiGen.exe**. Long installation paths
can prevent Qt's Windows plugin from loading. Keep the previous package and
separate data backups. Review and confirm each installation; restart when asked.

## Unreleased window-fit correction

The a.0.6.4 source candidate wraps Generate and update actions to the available
width and restores Tab navigation from update notes. Downloads above remain
the published a.0.6.3 release until this successor is qualified and delivered.

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
  while retaining the image and video work already open.
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

This release offers signed Reader a.0.3.1 and Organizer a.0.5.0
downloads. Review and confirm installation in each component workspace.
Tag Database is reviewed content, rather than another application installer.
Use the full Windows ZIP in a new folder for a fresh installation. The
a.0.6.2 client can find and stage a.0.6.3; applying that update and rollback
have not yet been exercised. Earlier clients need the full ZIP because they
retain the old Apply timeout or predate the managed-update baseline.

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
