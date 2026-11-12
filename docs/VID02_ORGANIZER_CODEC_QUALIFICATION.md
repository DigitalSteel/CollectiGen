# Organizer video playback runtime

VID-02 / VID-03 / SUITE-10B. November 11, 2026. Delivery route: existing unpublished Media Organizer a.0.5.0 candidate.

The existing organizer-video-windows profile supports Windows x64 with the shipped CPython 3.13.15 runtime. Setup reviews and installs the exact hash-locked PyAV 18.1.0 wheel in the optional software environment. No codec is bundled or installed automatically. The stock shipping wrapper must take tools/optional-ai-profiles.json from the exact current public source, not an older runtime baseline.

Qualified wheel: av-18.1.0-cp311-abi3-win_amd64.whl, SHA-256 ea1480b7a8d5405cb5f382b344731bf125fd2c1c6fae3964f6c48595628387ff. Reviewed profile SHA-256: 710048a96ad612bd6ff3d67d7be0de9ab7d309c0942e0a1f9daeffa910cce9c1. Interpreter SHA-256: 85b71d8c6ec1905935f74be0c9869aae198d00e98f39df699ec66f9c5a84cecd.

The initial stock native campaign used source a3972b4107eee5f52a74d555d2bdf0a9709a393f and package SHA-256 5b96ec5719e5abf18b4f0b11daf358f1e1153ab7e32a9b6bbdc7267440a67c3b, its actual packaged interpreter and a private qualification copy of this profile. Proposed production metadata correctly refused admission. A deliberately failed install activation restored the exact prior fixture store; the same reviewed plan then installed successfully. Native admission, five-frame 64x64 full decode, early preview-worker drain, changed-native-file refusal and exact-restoration admission passed. The UI parent did not import PyAV.

VID-02P1 repairs the subsequently measured repeated ancestor-check cost, retaining fresh per-file hashes and redirect refusal. VID-02P2 serializes the subsequently captured concurrent import-cache refresh race. Their source/native timing evidence and regression scope are recorded in VID02P1_PLAYBACK_ADMISSION.md and VID02P2_OPTIONAL_CACHE_STARTUP.md.

Retained native receipts are in vid02-packaged-codec-875x19s3/RESULT.json and PATH_COST_NATIVE.json. Exact rebuilt GUI, saved-state, frame advance, Close/drain, signature, installed recovery and publication belong to the candidate delivery receipts. This profile's ABI/install qualification does not substitute for those gates. Source or an unsigned ZIP alone is not a delivered update. No Manager encoder, generation model, GPU, arbitrary-format, Root/Reader/C3A or full Video parity qualification is implied. Organizer keeps generation execution disabled. No manual-testing hold is added.
