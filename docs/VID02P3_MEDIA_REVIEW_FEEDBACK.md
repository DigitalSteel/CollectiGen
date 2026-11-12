# Media review completion and retry feedback

VID-02P3, linked to VID-02, VID-03, UI-06 and SUITE-10B. November 11, 2026.

Final native playback did advance frames and drain on Close, but the inspected return capture still said Checking Video runtime for playback. The inspected busy inventory state also named the active scan without explaining the existing Refresh recovery. A separate failed-driver cleanup retained a queued-load-after-shutdown exception.

The existing dialogs now say when playback is open and when media review is ready after it closes. If another operation prevents media inspection, the review names Refresh as the next step after that operation finishes. Both idle Close routes mark the dialog closing before queued initialization can start work. Existing active-worker cancellation/drain and the selected record remain authoritative; no new retry coordinator or automatic install/restart is added.

Four focused synthetic Qt cases pass: Close and window-close before initialization start no late operation; busy review exposes the existing enabled Refresh with the named recovery; closed playback retains the selected record and returns ready feedback. Retained tests are MEDIA_COMPLETION_TESTS.xml (two passing unaffected cases) and MEDIA_CLOSE_RECHECK.xml (two corrected Close-route cases) under vid02-packaged-codec-875x19s3. An initial test-harness ownership API error is retained, not counted as a passing case. Together with the earlier source cases this is 58 distinct cases, not the sum of repeated runs.

Real candidate playback before this feedback repair used the shipped supported profile and normal visible Dataset/Ingest/View/Review Media/Play/Close controls. The worker-friendly stock test wait gave first visible frame in14.33 seconds, advancing frames, drained decoder work and normal exit. Earlier QTest-only wait timeouts are not a matched before/after production performance measurement. The exact successor artifact and inspected feedback captures belong to its delivery receipt; source checks alone are not a delivered update. No manual-testing hold or Root/Reader/C3A trial is added.
