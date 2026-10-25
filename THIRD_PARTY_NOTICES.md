# Third-party notices

CollectiGen includes third-party runtime components and compiled factual vocabulary derived from public sources. Third-party components retain their own licenses; no project-level license changes or replaces those terms. The exact bundled Python distributions are recorded in `THIRD_PARTY_COMPONENTS.json` in a portable package and in the runtime manifest. The original dataset exports, model weights, and wiki bodies are not distributed with this repository.

## Bundled application runtime

The separate frozen Suite Manager candidate has a different component inventory
from the portable interpreter package described below. See the
[exact frozen Manager attribution review](licenses/Manager-Frozen-Bundle-Attribution.md)
for bundled Velopack/PyInstaller support, absent PyAV/FFmpeg and Ollama runtimes,
and the remaining notice/package blockers. This source review does not certify
or alter any already prepared package.

Portable Windows packages contain the following core components:

- Python 3.13.15 under the Python Software Foundation License. The runtime retains `data/runtime/python/LICENSE.txt`; source is available from [python.org](https://www.python.org/downloads/release/python-31315/).
- pip 26.2.1 under the MIT License and Pillow 12.3.0 under the MIT-CMU License. Their installed distribution license files remain in the bundled Python runtime.
- PySide6 Essentials 6.11.2 and shiboken6 6.11.2 under their module-specific LGPL-3.0-only/GPL-3.0-only options. CollectiGen imports Qt Core, GUI, and Widgets and dynamically loads the unmodified shared libraries, but the official Essentials wheel also conveys other Qt module binaries, including GPL-only Lottie, QML Compiler, and Quick Timeline components. The package includes [LGPL-3.0-only](licenses/LGPL-3.0-only.txt), [GPL-3.0-only](licenses/GPL-3.0-only.txt), and the [Qt 6.11.2 third-party attribution appendix](licenses/Qt-6.11.2-ThirdParty-Attributions.json). The exact official Essentials wheel is `pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl`, SHA-256 `c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1`. PySide source is available from the [official Qt for Python 6.11.2 archive](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/); corresponding Qt source is the official [`qt-everywhere-src-6.11.2.zip`](https://download.qt.io/official_releases/qt/6.11/6.11.2/single/qt-everywhere-src-6.11.2.zip), SHA-256 `0bc81a7420e2d5c99ac241f5c37ca3fa3e666f91f178fbac4bb2b5de23b4757c`. The wheel omits Qt's build-time SBOM, so the package inventory identifies every exact PySide6 Essentials/shiboken6 wheel-RECORD-owned binary while the appendix conservatively retains all 203 Qt 6.11.2 upstream attribution records and all 89 unique source `LICENSES` texts. This deliberately may over-attribute source/example components and does not assert undocumented wheel build flags. Replacing the PySide6/Qt runtime files with compatible builds is permitted; CollectiGen does not add technical restrictions to prevent that replacement.
- ExifTool 13.59 under the Artistic License and the other terms included by its Windows package. The portable runtime retains `exiftool_files/LICENSE` and `exiftool_files/Licenses_Strawberry_Perl.zip`; the reviewed archive comes from the [official ExifTool distribution](https://sourceforge.net/projects/exiftool/files/exiftool-13.59_64.zip/download).
- The official Python and PySide6 binary distributions contain unmodified Microsoft Visual C++ runtime DLLs. CollectiGen's public portable package removes those copies and instead requires Microsoft Visual C++ v14 Redistributable (x64) 14.44.35211.0 or newer as a system prerequisite. The Microsoft runtime and installer are not part of CollectiGen's repository, package, component inventory, or license grant. Online setup links to [Microsoft's official current download instructions](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170); offline recovery can verify and run a user-supplied Microsoft-signed installer. Microsoft documents the separate licensing limits for redistributing its package or binaries in its [redistribution guidance](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170).

The machine-readable source contract is `tools/public-components.json`. `tools/Audit-PublicComponents.py` compares it with the exact runtime and rejects missing or drifted license evidence.

## Optional local AI components

The native resolution adapter in `core/resolution_ai.py` adapts the RRDBNet
architecture from [Xintao Wang et al.'s ESRGAN reference](https://github.com/xinntao/ESRGAN/blob/master/RRDBNet_arch.py),
Copyright 2018 Xintao Wang et al., under [Apache-2.0](licenses/Apache-2.0.txt).
CollectiGen modifies loading, validation, key mapping, tiling, cancellation,
identity checks and lifecycle handling. It loads only explicitly selected local
Safetensors weights; no upstream weights or executable model loaders are bundled.
The upstream [license](https://github.com/xinntao/ESRGAN/blob/master/LICENSE)
remains applicable to the adapted architecture implementation.

Optional Python packages and model files are not bundled with the application or public source. Setup displays the source and applicable terms before a user-approved network install. The installed Python distributions keep their upstream metadata, license files, and notices.

- WD v3 tagger models are downloaded from the listed [SmilingWolf repositories](https://huggingface.co/SmilingWolf) at exact reviewed revisions under Apache-2.0.
- JoyCaption's [source repository](https://github.com/fpgaminer/joycaption) is Apache-2.0. CollectiGen optionally downloads the separate [fancyfeast/llama-joycaption-beta-one-hf-llava](https://huggingface.co/fancyfeast/llama-joycaption-beta-one-hf-llava) model at an exact reviewed revision only after confirmation. That model uses the Llama 3.1 Community License and Acceptable Use Policy; it is not bundled and is not described as OSI open source. Optional JoyCaption support is **Built with Llama**. The repository retains the [Llama 3.1 Community License](licenses/Llama-3.1-Community.txt), identifies the separate terms before acquisition, and retains the model's installation manifest and notices with the downloaded files. Final legal review of this conservative treatment remains a public-release gate.
- The optional [florence-community/Florence-2-base-ft](https://huggingface.co/florence-community/Florence-2-base-ft) checkpoint is identified as MIT-licensed and is downloaded only at CollectiGen's exact reviewed revision after confirmation. It is not bundled. CollectiGen uses the native Transformers implementation and does not execute model-supplied Python code.
- NumPy, ONNX Runtime GPU, Hugging Face Hub, PyTorch, Transformers, Accelerate, and BitsAndBytes are installed only after confirmation from the exact wheel-and-hash closures in `tools/optional-ai-profiles.json`. The reviewed WD CPU and NVIDIA CUDA profiles use PyPI; Plain Language models use PyPI plus the official PyTorch CUDA 12.8 wheel index and require a supported NVIDIA GPU. After installation, `data/runtime/python/tag-manager-installed-components.json` records the selected profile hash, every locked requirement, all exact installed distribution versions, license metadata, and hashes of bundled license/notice files. NVIDIA CUDA/cuDNN components use separate [NVIDIA proprietary software terms](https://docs.nvidia.com/cuda/eula/index.html). On 2026-08-31 all three exact Windows x64/Python 3.13.15 closures reconstructed together in a fresh isolated environment: 39 distributions passed read-back, imports, and `pip check`; ONNX Runtime exposed TensorRT, CUDA, and CPU providers; and PyTorch exposed CUDA 12.8. The wheel/model identities are unchanged from the earlier representative pinned WD CPU/CUDA, JoyCaption NF4, and Florence-2 BF16 inference evidence; this patch-runtime refresh did not read or rerun private model files.

## Local training source and tokenizer assets

The reviewed sd-scripts snapshot at revision
`6721028c79ee85a78b3a06dfd8954dae310a1cce` is included under
`tools/trainer-sources/sd-scripts`, with its unchanged Apache-2.0
[license](tools/trainer-sources/sd-scripts/LICENSE.md). The runtime catalog
records every included source hash and its upstream attribution. CollectiGen's
versioned training adapter creates its reviewed runtime patch separately; it
does not modify the bundled original source or an existing trainer installation.

The eight SDXL tokenizer vocabulary/configuration files retain their original
bytes from `stabilityai/stable-diffusion-xl-base-1.0` revision
`462165984030d82259a11f4367a4eed129e94a7b`, together with the repository's
[CreativeML Open RAIL++-M license](licenses/sdxl-tokenizer-source-LICENSE.md).
Base model weights are not bundled. Runtime profile/catalog files describe the
separate Python 3.11 trainer and pinned dependencies; obtaining those software
artifacts remains a separately confirmed Setup operation, with original
dependency notices retained in the installed environment.

## SmilingWolf WD selected-tag vocabulary

- Source: [SmilingWolf/wd-swinv2-tagger-v3](https://huggingface.co/SmilingWolf/wd-swinv2-tagger-v3)
- Material used: `selected_tags.csv` names, broad tag types, and occurrence counts
- Upstream license: Apache License 2.0
- License copy: [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt)

CollectiGen's compiled records add its own categories, sensitivity classifications, relationships, provenance, and canonical-resolution behavior. No WD model weights are included in the knowledge defaults.

## kierarkia Danbooru wiki 2026 dataset

- Source: [kierarkia/danbooru-wiki-2026](https://huggingface.co/datasets/kierarkia/danbooru-wiki-2026)
- Material used: factual tag/character/series names, aliases, broad record types, post counts, and selected tag-group relationships
- Dataset license: WTFPL version 2
- License copy: [licenses/WTFPL-2.txt](licenses/WTFPL-2.txt)

CollectiGen does not redistribute wiki prose or source images. The generated defaults retain source/version provenance and aggregate facts only.
