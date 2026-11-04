# Media Organizer a.0.4.22

2026-11-04 (America/New_York)

## Bug fixes

Feedback Archive opens its review controls before loading saved-report metadata.
Loading runs through the existing read-only task service while the interface
continues responding. Unknown-total activity and elapsed time remain visible;
report selection and archive creation wait for a current successful result.
If loading fails, restore access and choose Retry Loading Reports. Your chosen
archive destination is retained. Closing ignores the read result; an already
running read may finish in the background without changing reports.

The metadata parser, worker engine, report bytes, archive/history writes,
theme and parent completion are unchanged. The earlier known-archive result
guard remains included. This does not make archive creation/history writes
asynchronous or add private Learning integration. No automatic retry, upload,
unattended installation or restart is enabled.

Qualification uses generated metadata and stock Qt task/lifetime cases, focused
loading/result/failure/retry states, signed inventory, isolated idle saved-state
startup/Close and update/rollback. Actual/private reports, full ordinary parent,
native/DPI/AT/novice, scale and remaining Archive write/reading acceptance stay open.
