# Privacy and network use

CollectiGen is offline-first. It has no telemetry, advertising, analytics, account requirement, cloud image processing, or background upload of images, Image Knowledge, System Knowledge, prompts, metadata, model output, paths, settings, or support bundles.

Normal image browsing, editing, searching, review, automation, metadata work, local AI analysis with installed models, and use of the Learning Library work without a network connection. User libraries and authoritative `.tags.json` sidecars stay where the user chose to store them. Rebuildable indexes, caches, settings, job state, downloaded models, and update state remain local.

A library explicitly marked **Local Only** is a hard privacy boundary. Its image bytes, thumbnails, video frames, Image Knowledge, Metadata, prompts, tags, descriptions, paths, filenames, library identity, derived indexes, and model results must not be transmitted to a website, hosted service, telemetry system, remote generator, general shared library, or cloud-synchronized folder. Local Only may include explicitly approved backup copies on user-controlled private network storage or offline media, provided the destination is designated for Local Only backup, access is restricted to the user's intended private boundary, it is not treated as an active shared library, and its contents, manifests, and logs are not exposed to hosted services or development assistants. Network adapters may search public remote information only from terms the user deliberately enters into that adapter; they may not inspect or derive a query from Local Only data. Leaving that privacy zone requires a separately confirmed, scope-specific export or move that previews the exact files and information crossing it. Merely enabling an online source, update check, backup, or future paid service never grants that permission.

Codex and other hosted development assistants are external processing boundaries, not part of CollectiGen's offline runtime. The application must not expose Local Only content to them automatically. Maintainer workflow also treats designated Local Only paths as out of scope for routine discovery or testing: a developer must not ask an assistant to enumerate, preview, parse, attach, or summarize real Local Only content unless the user separately authorizes one narrow read after being told that the selected content will be processed by that service. Filesystem sandboxing limits what an assistant can reach but does not make hosted model processing local; use synthetic fixtures for development and testing.

**Local Only describes storage and sharing, not permission to use local features.**
Local browsing, editing, analysis, training, generation planning and generation
must remain available. Local application-data storage for plans, caches, job records,
and results does not itself cross this boundary and need not live inside the
original library folder. These derived records retain the same no-sharing and
no-upload requirements. Blur is a display preference and does not block local
processing. Unclassified local files receive the same no-sharing protection;
their lack of a named library does not prohibit local work. A local generator
connection must use local processing, not relay protected content to a remote
backend. Existing explicit sharing/export confirmations still apply when data
actually leaves local storage for shared, synchronized, or remote destinations.

## User-initiated network actions

CollectiGen connects to an external service only for a visible action or a confirmed repair:

- **Check for Updates** contacts the configured GitHub release endpoint. Installing an online update downloads only the selected signed release assets from GitHub. Offline update files remain supported.
- **Models → Provider Evidence** sends only deliberately entered public model
  terms, IDs/URLs or a separately disclosed full-file model hash to Civitai or
  Hugging Face. Lookups and searches are explicit; no library-derived query,
  provider image, prompt or Image Knowledge is transmitted. A separate exact
  acquisition review permits the selected public model file to download from
  supported provider/CDN hosts. Browsing cannot install, register or launch it.
  Cached attributed metadata remains usable offline; credentials are not saved
  in that cache or model records. Local guidance-file imports make no network
  request.
- **Setup or repair application components** may contact PyPI/files.pythonhosted.org for exact Python packages, the reviewed official PyTorch package index where required, or SourceForge for the pinned ExifTool archive after confirmation.
- **Set up an AI capability** may install Python packages from PyPI and download an exact reviewed model revision from Hugging Face after confirmation. Model inference then runs locally. The confirmation identifies separate model or NVIDIA terms when applicable.
- A damaged portable runtime may offer a confirmed staged repair from the pinned official sources. The existing runtime is retained if verification fails.
- **Open GitHub Feedback…** copies the reviewed readable report to the local clipboard and opens CollectiGen's public GitHub feedback form in the user's browser. The application does not submit or upload anything. GitHub receives report text or an attachment only if the user pastes, attaches, and submits it.

Those services receive ordinary connection information such as the device IP address, request time, requested URL, and standard client headers. CollectiGen does not add image content, Image Knowledge, prompts, local paths, or library identifiers to these requests. The external service's own privacy policy governs information it receives.

Offline Model Packs and Offline AI Component Packs are local files created only
after the user chooses an export destination. Import reads only the chosen local
pack and does not contact its recorded source. Model packs contain model files
and their reviewed source/revision/license manifest. Component packs contain the
complete installed optional Python environment, including distribution metadata
and retained license files, but no images, prompts, Image Knowledge, model
weights, user settings, or library paths. The
[offline-first release audit](docs/OFFLINE_FIRST_AUDIT.md) records the exact pack
and network boundaries.

## Support bundles

Feedback reports and support bundles are created locally and are not uploaded
automatically. **Feedback…** excludes images, captions, tags, descriptions,
generator payloads, libraries, paths, settings, logs, machine/account identity,
and selected-image identity by default. Optional screenshots, a filename plus
one-way path fingerprint, allowlisted sanitized settings, and bounded redacted
logs require explicit inclusion and an exact privacy review. Public/package
builds default to a local `feedback` folder beside the running application and
allow a final folder override. A private development checkout may offer its
ignored maintainer inbox, but choosing it does not authorize an external
service or assistant to read the report; the user must separately request that
processing. The complete record, tracking, copy/export, and redaction workflow
is described in `docs/SUPPORT_BUNDLES.md`.

GitHub issues and their attachments are public. Review the pasted report and
every attachment before submitting; do not use the public form for private
images, Local Only data, credentials, unredacted diagnostics, or security
vulnerabilities. A screenshot containing Questionable or Explicit images may be
attached only after its exact preview confirms every such image stays fully
blurred. Never attach unblurred pixels, original images, or videos with that
content; use text instead when blur cannot be confirmed. Even redacted
diagnostics can reveal application versions, timing, enabled capabilities, and
error context.

## Changes

The planned local-training integration uses normal internet connectivity for
user-directed setup and acquisition of tools, dependencies, models and data.
Computation runs on the user's GPU/CPU; CollectiGen does not require an app/trainer
firewall. Future user-controlled network workers/clusters require their own
execution and data-transfer contract before support is offered.
Supported automatic acquisition follows the user's setup policy. Network access
alone never authorizes uploading images, captions, training data or model results;
sharing and any remote processing need their own explicit supported choice and
must respect Local Only/export boundaries. This planned direction does not add
an implemented network action to the current list above.

Any future telemetry, account, hosted processing, or automatic network behavior requires an explicit roadmap contract, user-facing choice, documentation update, and review before implementation. The release audit in `tools/Audit-PublicComponents.py` checks that this statement and the component inventory are present.
