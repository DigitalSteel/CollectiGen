# CollectiGen Suite Manager

The **a.0.6.0 development candidate** provides component setup, Generate and
Training workspaces. The published Manager remains **a.0.5.1**. Use the main
GitHub README download table for the version you can currently install; source
capabilities described here do not update an older executable.

## Workspaces

- **Dashboard** summarizes suite and saved generator readiness and links to the
  work that needs attention.
- **Generate** creates or reopens image and video drafts, reviews generation
  jobs, and reconnects a checked local generator. The existing editor owns
  prompts, model choices, settings, alternatives, queues, results and recovery.
  The draft switcher retains multiple image and video requests. Model-guided
  planning opens a separate request without replacing another draft; Close
  consults every retained request.
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

The candidate includes the existing signed Reader a.0.3.1 and Organizer a.0.4.23
download offers. Review and confirm installation in each component workspace.
Tag Database is reviewed content, rather than another application installer.
Manager's first managed baseline and subsequent self-updates use their existing
separate package and recovery contracts; this candidate is not a delivered
self-update.

## Source startup

Run `python -B -m suite_core.manager` from a checkout with Python and PySide6.
Use `--theme system|dark|light|midnight` and `--color-blind-mode` for presentation.
`--demo` displays synthetic data. No dated task directory or `--suite-source`
is needed. A normal host supplies its own admitted profile and component
services; Demo does not authorize installations, execution or private data reads.

Keep component versions, signed package identities and rollback artifacts with
their release. See `WHATS_NEW.md` for current candidate changes. Historical
qualification receipts remain evidence for their exact original artifacts;
they do not qualify a rebuilt candidate.
