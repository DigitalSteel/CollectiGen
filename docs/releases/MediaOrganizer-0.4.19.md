# Media Organizer a.0.4.19

2026-11-04 (America/New_York)

## Bug fixes

Collected Feedback ZIP export now refuses destinations inside any selected
report folder and refuses existing output files before loading or writing.
Choose a new ZIP name outside the selected folders and retry. Normal new
archives preserve original reports and retain verified archive readback.

This patch ports the completed destination preflight only. The shared ZIP
writer, report format, UI and providers are unchanged. It adds no upload,
migration or unattended installation/restart. Earlier fixes remain included.

Generated report preservation and ordinary new ZIP output are qualified.
Native aliases, path races and atomic concurrent no-clobber are not qualified;
do not infer those guarantees from this preflight. Actual/private reports,
full UI/novice workflows and private Learning integration remain unqualified.
Signed inventory, isolated idle saved-state startup/Close and scoped update/
rollback qualification are recorded separately from those open boundaries.
