# Frozen Suite Manager component attribution

Reviewed 2026-10-09. Static evidence for the 905-file build-03 Suite candidate,
source manifest SHA-256
`63dafae2f728606495ca2b1328184020d2d4b618a6548d311609209021ea8f0f`,
payload inventory SHA-256
`abc009249207da99b001b4150cb7c4a817d3185ac8dbb4357703a879bf3dc0a6`.
All inventoried payload hashes were verified without executing the package.
This is a maintainer attribution review, not a release-ready component inventory.

| Component | Actual conveyed material | Attribution disposition |
| --- | --- | --- |
| Velopack 1.2.161 | `velopack/velopack.pyd`, metadata and upstream SBOM in `_bootstrap` and `Apps/Manager/current/_internal` | MIT top-level license; all 139 SBOM component source notices retained; package inclusion pending |
| PyInstaller 6.22.3 | Generated executable bootloaders and embedded loader support | Stock generated-bundle exception; build-tool installation is not conveyed |
| PyAV / FFmpeg | None in this exact inventory | No video-library attribution blocker for this candidate; the older portable-runtime contract remains separate |
| Ollama | CollectiGen integration/preparation code; no Ollama runner, installer or models | No bundled Ollama runtime/version claimed |
| Python 3.14.7 | `python314.dll` and standard runtime support | Exact license retained in source; package inclusion and version wording pending |

## Velopack SDK and transitive notices

The exact native SDK hash is
`6fd683ca5a19c54125ac49204e962d5dbe4b124f8ffe01afe1f518272f397352`.
Its installed metadata says 1.2.161/MIT. The package's top-level license names
Caelan Sayler (2021) and Velopack Ltd. (2024), consistent with the
[official 1.2.161 license](https://github.com/velopack/velopack/blob/1.2.161/LICENSE).
The [retained license](Velopack-1.2.161-LICENSE.txt) preserves those notices.
No `Update.exe`, `vpk` tool or separate installer is in this payload.

The [retained upstream CycloneDX SBOM](Velopack-1.2.161-SBOM.json) is byte-exact,
SHA-256 `abe4c25623102601a621e48d60a10837c57ebc7fb7b966ca7e9d4642bc3b6d87`.
It lists 139 components, including build/platform dependencies; its upstream
build paths are provenance, not CollectiGen user data or installed paths.
Do not interpret its root `0.0.0` package version as the installed SDK version.

An SPDX expression is not a complete copyright/license notice. The
[exact third-party notice appendix](Velopack-1.2.161-ThirdParty-Notices.json)
now retains 268 upstream notice records covering all 139 exact SBOM components,
SHA-256 `f3d32c110edc6d66ff4889cfc57dec72f4419715213e07c5a56c237d36c50779`.
Each entry records official registry/source-archive attempts, checksum-verified
crate provenance, and each notice's original path, bytes, SHA-256 and full text.
No source archive was extracted, compiled or executed. No global tool was installed.

The collection reuses the existing Qt appendix's conservative source-text record
approach with stock archive readers. It retains all packaged license alternatives
and nested vendored notices, including ring 0.17.14's BoringSSL/other-bits notices,
Unicode-licensed ICU material, target-lexicon 0.13.5's LLVM exception and
webpki-roots 1.0.9's CDLA-Permissive-2.0 license. No AND/OR expression or license
choice is silently changed. Derivative 2.2.0 and log-panics 2.1.0 have registry
`MIT/Apache-2.0` literals versus SBOM `MIT OR Apache-2.0`; both exact license files
and both raw expressions are retained. Typenum's short LICENSE pointer is retained
alongside its complete MIT/Apache texts. The published Velopack Rust crate omits
a license text, so its exact upstream 1.2.161 top-level license is retained instead.

There are no unresolved source-notice components. This conservative superset can
over-attribute build/platform sources and does not establish the SDK binary's
complete linked dependency set or legal acceptance. It must be included in a fresh
package; the prepared candidate and public-source allowlist remain unchanged.

## PyInstaller and absent integrations

[PyInstaller 6.22.3 licensing](https://pyinstaller.org/en/v6.22.3/license.html)
permits generated bundles under the application's license, subject to dependency
licenses, and says its license/acknowledgment need not accompany a generated
bundle. Distributed modifications to PyInstaller itself have separate duties.
No such modification is established by this stock build evidence.

[PyAV 18.1.0](https://github.com/PyAV-Org/PyAV/blob/v18.1.0/LICENSE.txt)
uses BSD-3-Clause; [FFmpeg](https://ffmpeg.org/legal.html) has build-dependent
LGPL/GPL and corresponding-source obligations. Neither is conveyed here. Keep
the existing PyAV/codec hold for packages that actually bundle them; do not add
those runtimes to this package to satisfy an unrelated inventory.

[Ollama's upstream source license](https://github.com/ollama/ollama/blob/main/LICENSE)
is MIT. This candidate conveys no Ollama release; that reference does not qualify
a future binary archive, its dependencies, installer, GUI application or models.
Review the exact selected archive/version and model terms before conveying them.

## Remaining package blockers

- The bundled Python DLL reports 3.14.7, but the bundled general runtime notice
  describes 3.13.15. No PSF license file appears in the payload inventory. The
  [retained Python license](Python-3.14.7-LICENSE.txt), hash
  `935cf13e19f8c31b497d20b05d73623431a226b230c3599bc30fa3348979bc68`,
  comes from the hash-pinned declared Python build input. Include it and correct
  the notice when preparing a fresh candidate.
- Both support trees contain Microsoft `MSVCP140*` and `VCRUNTIME140*` DLL copies,
  conflicting with CollectiGen's approved system-prerequisite distribution policy.
  Packaging must resolve this; this review does not authorize DLL removal or a
  policy change.
- The existing `Audit-PublicComponents.py` inventories the portable interpreter
  layout. Its passing result would not validate this frozen candidate section.
- Include the pinned Velopack notice appendix and other new notice files through
  the separately owned public-source and package allowlists.

Product fit: accurate notices reuse the sealed stock package evidence while
keeping optional acquisition, user model/data authority and publication approval
separate. No package bytes or runtime behavior are changed by this review.
