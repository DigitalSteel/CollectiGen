# Manager local resource integration - 2026-10-06

Historical unreleased source candidate: 0.1.0-alpha.19. Date is October 6 in America/New_York.
The dated alpha19 RELEASE_HISTORY entry is explicitly unreleased and preserves all
prior entries. At that milestone, cross-file checks aligned version/date/module identity across version,
manifest, history, What's New, in-window NOTES and README.
The parent owns source acceptance, roadmap reconciliation and runtime qualification.
Source integration is landed; the retained alpha19 patch is historical proposal
evidence. Native qualification remains pending; alpha18 and its frozen build
remain intact. Proposal preparation performed no stage, commit, build, repack,
publication or activation; source integration does not qualify a new artifact.

## Product fit and ownership

An explicit Dashboard Refresh lets users review this computer's CPU/RAM/NVIDIA
usage before choosing workloads. It reuses the accepted schemas, reviewed admission
and presentation, Manager cards, completion timer and close coordinator. No catalog,
model, installation, scheduling or workload permission is acquired from measurements.
No source is sampled at import, construction, navigation or timer ticks. Demo is
synthetic, including the in-window Demo route. The accepted Windows provider remains
unchanged; the separately reviewed Linux candidate is excluded.

## Connected behavior and recovery

The entrypoint constructs ApplicationId('suite-manager'), opaque HostId('local'),
the accepted sample_local_resources closure and a lazy ResourceExecutorOwner.
Refresh alone creates one non-daemon worker. The owner retains its Future and Thread;
public is_alive confirms worker teardown separately from Future completion. No UI
join or blocking shutdown exists. Refused submissions return failed acknowledged
Futures without sampling; ambiguous thread creation/start retains a supervision hold.
The route still rejects stale, foreign, out-of-order, malformed and revoked results.
Queued cancellation never samples; running cancellation revokes admission and waits.

The canonical window installs the panel in its current dashboard, replaces it with
the view, revokes on navigation/demo, and polls with its existing lifecycle_timer.
Resource-only timer ticks skip unrelated unconfigured lifecycle rebuilding.
Existing database previews and lifecycle/diagnostic hooks retain their close paths.
Close first revokes the route, closes its owner and ORs sample/teardown holds with
existing close facts. It finishes only after both acknowledgements. Cancel pending
close leaves the resource route inert; reopen after drain to refresh. The existing
deadline requires deliberate cancel/retry and never infers cleanup from elapsed time.
An ambiguous start has no automatic recovery claim: ownership stays supervised.

## Change-to-check map and source closure

Single owner; no delegation or nested workers. Source scope: entrypoint/window,
resource_executor, manifest/version/release history/README/dated notes, this record, focused tests,
one navigation row and one planned UI journey. Existing provider/candidate helpers
are unchanged dependency inputs. The manifest adds only the accepted provider, its
route/binding/presentation and the Manager owner; no main-app BackgroundJobManager,
Linux candidate or new generic executor framework is admitted.

Planner preview selects 63 suite-core modules including unrelated candidates. The
bounded alternative is new integration tests, the reviewed resource candidate,
close policy and the canonical runtime-manifest check. Latest fresh result: 91 tests
passed, 0 failures/errors/skips. All workers, Futures, readers and counters are fake.
Actual main runs against Python QApplication/window doubles. Actual canonical close
method bodies run with Python doubles; neither is visible-control/native evidence.
Candidate tests cover stale/foreign/cancelled/error/unavailable-tool observations and
snapshot admission. Owner tests add single-slot refusal, queued/running cancellation,
ambiguous start, sample exceptions, unknown teardown, deadline/cancel and no automatic
acquisition. Exact baseline/proposed/dependency hashes live in the review bundle's
SOURCE_CLOSURE.json, along with the complete intended touched-path list.

## UI consistency and unrun gates

The planned journey manager-local-resource-refresh is registered under UI-06; this
does not declare suite milestone completion. Closest mature comparison: Manager
Dashboard resource cards and Databases preview. Reuse workspace_heading, visualCard,
18/14 px spacing, 18x16 tile padding, adjacent Refresh/Cancel/status, source/date and
unknown/stale language. Local and detached backend evidence stay separate because
their acquisition and freshness differ. Advanced scope remains explicit host config.

Visible route, first-time comprehension, keyboard navigation, 1080p all supported
themes, actual timers/QObject panel replacement, native worker exit/application
lifetime and real Windows tool availability are unrun gates. No real workers, Qt
app/native launch, live OS/GPU samples, network, installs, model/GPU jobs or catalog
access occurred. No Linux runtime or portable artifact qualification is claimed.

Best steps for testing after source review: open ordinary Dashboard and confirm
unknown values without acquisition; click Refresh and inspect dated CPU/RAM/separate
GPU values; cancel a delayed refresh, leave/return and confirm revoked values do not
return; verify unavailable/stale recovery; enter Demo and confirm synthetic-only
content; close during work, then exercise pending-close cancel/retry and verify
worker teardown precedes exit. Review Databases preview and diagnostic hooks on the
same accepted runtime. Do not run this review phase as a distributed artifact.
