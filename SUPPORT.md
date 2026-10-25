# Support

CollectiGen offers community support through GitHub Issues after the repository is public. No paid support, service-level agreement, guaranteed response time, or guaranteed resolution is currently offered.

Roadmap item `BUS-01` plans a future Official distribution with an exact support entitlement. That entitlement does not exist until its duration, eligible versions, channels, response boundary, exclusions, refund treatment, and end-of-period behavior are published before sale. A purchase must not imply a guaranteed fix or service level that the published terms do not provide. Community users retain the public issue route, and an expired Official support or update period never disables the installed application or its local data.

During the public alpha period, best-effort community support covers only the latest public prerelease; an alpha is evaluation software and carries no response-time, resolution, compatibility, or data-recovery guarantee. After a stable release exists, only the latest stable release is maintained unless a release note explicitly says otherwise. Users of superseded public versions should update to the latest applicable release before requesting diagnosis; the documented rollback path remains available for a failed update but does not extend support for an obsolete version.

Use **Help → Feedback…** to create a stable local report
classified as a bug, difficult/confusing workflow, or feature/improvement
request, with current context, desired result, example steps, version/platform
context, and an exact privacy manifest. Optional screenshots, selected-image
identity, sanitized settings, and redacted logs start excluded and remain
removable or editable before saving. The readable report is shown first during
privacy review. Saving creates one clearly named shareable ZIP by default with
only the reviewed contents. Individual ZIPs are capped at 20 MB for attachment
compatibility. The success message can copy the readable report and open
CollectiGen's unified GitHub feedback form with the stable report ID and
summary already in the issue title. Paste the copied report and, if useful,
attach only its matching reviewed ZIP. CollectiGen never submits the issue or
uploads the report or ZIP automatically.
Long-lived modal editors, setup, review, and management workflows provide the
same **Feedback…** action while the main Help menu is disabled. Opening it
keeps staged work in place and returns focus; the report identifies only the
sanitized workflow and active tab unless the user explicitly includes more.
New report folders and individual ZIPs include the stable `CBR-*` ID followed
by a shortened safe form of the summary. The summary is limited to 120
characters so report names remain manageable.
The writing prompts adapt to the selected feedback type, and reproduction
steps or examples are optional. Reports default to the local `feedback` folder
beside the running CollectiGen application; choose another folder in the final
review when needed.
Use **Export Collected Feedback…** to select multiple reviewed reports for one
verified uploadable archive. Reports already included in an earlier export are
unchecked and visibly labelled by default. Export history is stored only in
each original report's separate tracking file; it records local packaging, not
whether an attachment was actually uploaded. Bulk archives are for deliberate
maintainer collection rather than the normal public issue path.
Before submitting a public issue, search existing open and resolved feedback.
GitHub issue text and attachments are public: do not submit private images,
Local Only data, credentials, unredacted diagnostics, or vulnerability details.
Attach a screenshot containing Questionable or Explicit images only after its
exact preview confirms every such image stays fully blurred. Never attach
unblurred pixels, original images, or videos with that content. A text-only
report can still describe the problem.
Security vulnerabilities follow `SECURITY.md` and the private reporting route.
Stable report IDs and content fingerprints let maintainers recognize repeated
submissions. Reports are investigated and dispositioned before they become
implementation scope. A private development inbox is local-only and is
intended only for maintainers working from a private source checkout.

General usage documentation starts in `README.md`; recovery and update behavior is documented in `docs/UPDATE_AND_MIGRATION.md`.

The supported public-alpha and planned v1 distribution is the official portable Windows x64 ZIP. There
is no supported installer, system-wide deployment, automatic file association,
or Add/Remove Programs entry. Moving an intact portable folder is supported when
its destination is writable; deleting that folder is not a safe uninstall until
wanted user-owned state stored beneath it has been copied or exported. Download,
warning, verification, privilege, and removal expectations are documented in
`docs/WINDOWS_DISTRIBUTION.md`.
