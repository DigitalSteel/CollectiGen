# Third-party notices â€” Organizer image candidate

Third-party works retain their licenses; the project's AGPL and documentation licenses do not replace those terms. This candidate uses the previously pinned image runtime, not the separate frozen Manager or optional Video/model runtimes.

| Component | Retained version and notice |
|---|---|
| Python | 3.13.15; Python Software Foundation license and incorporated-software notices in Runtime/python/LICENSE.txt |
| pip | 26.2.1; MIT and vendored notices in its bundled distribution licenses directory |
| Pillow | 12.3.0; MIT-CMU and incorporated-software notices in its bundled distribution licenses directory |
| PySide6 Essentials / shiboken6 | 6.11.2; module-specific LGPL-3.0-only/GPL-3.0-only options and exact wheel license files |
| ExifTool | 13.59; Artistic and other upstream terms, including the retained complete Strawberry Perl license archive |

The exact 47 current notice paths/hashes and package identities remain in [CURRENT_ORGANIZER_RUNTIME_NOTICES.json](licenses/Organizer-image/CURRENT_ORGANIZER_RUNTIME_NOTICES.json), with its [scope explanation](licenses/Organizer-image/CURRENT_ORGANIZER_RUNTIME_NOTICES.md). Their unchanged runtime notice files accompany the runtime; this summary does not replace them. Microsoft Visual C++ is a separate system prerequisite, not conveyed by this candidate.

The official PySide6 Essentials wheel also includes Qt Lottie, QML Compiler and Quick Timeline binaries. Preserve the complete [GPL-3.0-only](licenses/GPL-3.0-only.txt) and [LGPL-3.0-only](licenses/LGPL-3.0-only.txt) texts and module-specific wheel notices. Commercial reference texts in those wheels establish no commercial entitlement. Corresponding Qt/PySide/shiboken source and the precise library-replacement/build route must accompany a public release; see [SOURCE_DELIVERY.md](SOURCE_DELIVERY.md). Integrity checks do not grant new licensing restrictions or prevent lawful modifications from corresponding source.

The included resolution-adapter source derives the RRDBNet architecture from Xintao Wang et al.'s ESRGAN reference, Copyright 2018 Xintao Wang et al., under Apache-2.0. Its [original upstream source](https://github.com/xinntao/ESRGAN/blob/master/RRDBNet_arch.py) and [license](https://github.com/xinntao/ESRGAN/blob/master/LICENSE) retain that attribution. CollectiGen adapts loading, validation, tiling, cancellation and lifecycle handling. No weights or resolution workflow qualification is granted by including that source.

Publisher-declared vocabulary references retained by the candidate are SmilingWolf/wd-eva02-large-tagger-v3 (selected tag names/types/counts, Apache-2.0) and kierarkia/danbooru-wiki-2026 (declared WTFPL, notice version2). Their exact notice texts are [Apache-2.0](licenses/seed/Apache-2.0.txt) and [WTFPL-2](licenses/seed/WTFPL-2.txt). Original project selection/additions have their separate [CC-BY-SA-4.0](licenses/seed/CC-BY-SA-4.0.txt) scope. Retain all rich default resources with existing source citations and license scopes. User-reported Codex research/modification and investigation of imports resolves the process question; no copied third-party passage was established. Missing writer traces alone justify neither a blanket hold nor wholesale rewriting. This is bounded disposition, not legal certainty or untested model-utility qualification; exact matched resource/notice release admission remains separate.

No model weights, Microsoft installer, PyAV/FFmpeg runtime or frozen PyInstaller bootloader is conveyed by this image interpreter package. Optional future components require their own exact notices and rights before distribution.

## Native prerequisite checker

The bundled read-only checker uses statically linked LLVM/MinGW-w64 support. See [its component notices](licenses/Organizer-prerequisites/NOTICE.md), including unchanged license texts. Its [source and build instructions](tools/organizer-prerequisite-source/BUILD.md) are included. Windows system DLLs remain external dependencies.
