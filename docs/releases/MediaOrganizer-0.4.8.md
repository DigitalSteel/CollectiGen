# Media Organizer a.0.4.8

2026-11-03 (America/New_York)

Fixes final Close ordering: when background shutdown is incomplete, the window
stays open and retains its pending callbacks, tasks and queued actions. A
deliberate later Close cleans up once after shutdown succeeds. Existing page
vetoes and image/data authority remain unchanged; Close is not replayed for you.

This compatible alpha patch retains a.0.4.7's indicator contrast improvement,
image workflow, runtime and data interfaces. It adds no model execution, video,
Training/Generation or unattended installation. Qualification details are in
VALIDATION.json. Active native editor/reader jobs, non-elevated/clean-machine,
assistive-technology and independent novice acceptance are not claimed.
