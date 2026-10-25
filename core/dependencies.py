from __future__ import annotations

import atexit
import base64
import csv
import importlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path, PurePosixPath

from version import VERSION

from .ai_interrogation import (
    FLORENCE2_MODEL,JOYCAPTION_MODEL,MODEL_ACQUISITIONS,
    PLAIN_LANGUAGE_MODELS,WD_EVA02_MODEL,WD_TAGGER_MODELS,
    Florence2Backend,JoyCaptionBackend,WDEva02Backend,_record_model_installation,
    installed_model_revision,
)
from .paths import APP_ROOT, DATA_DIR, PROGRAM_ROOT
from .settings import AppSettings
from .profile_paths import unredirected_directory


_NO_CONSOLE={"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
EXIFTOOL_VERSION = "13.59"
EXIFTOOL_WINDOWS_URL = f"https://sourceforge.net/projects/exiftool/files/exiftool-{EXIFTOOL_VERSION}_64.zip/download"
EXIFTOOL_WINDOWS_SHA256 = "44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec"
PRIVATE_EXIFTOOL_DIR = DATA_DIR / "runtime" / "exiftool"
OPTIONAL_COMPONENT_ROOT = DATA_DIR / "optional-components"
OPTIONAL_COMPONENT_INVENTORY = OPTIONAL_COMPONENT_ROOT / "installed-components.json"
LEGACY_OPTIONAL_COMPONENT_INVENTORY = (
    Path(sys.executable).resolve().parent / "tag-manager-installed-components.json"
)
OPTIONAL_AI_PROFILES = PROGRAM_ROOT / "tools" / "optional-ai-profiles.json"
MODEL_PACK_MANIFEST = "collectigen-model-pack.json"
MODEL_PACK_SCHEMA_VERSION = 1
MODEL_PACK_FREE_SPACE_RESERVE = 256 * 1024 * 1024
MODEL_PACK_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MODEL_PACK_MAX_FILES = 100_000
COMPONENT_PACK_MANIFEST = "collectigen-components-pack.json"
COMPONENT_PACK_SCHEMA_VERSION = 1
COMPONENT_PACK_FREE_SPACE_RESERVE = 512 * 1024 * 1024
COMPONENT_PACK_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
COMPONENT_PACK_MAX_FILES = 250_000
COMPONENT_PACK_MAX_UNPACKED_BYTES = 20 * 1024 * 1024 * 1024
JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB = 24 * 1024
RECOMMENDED_WD_CONSENSUS_MODEL = "SmilingWolf/wd-swinv2-tagger-v3"
HIGH_F1_WD_CONSENSUS_MODEL = "SmilingWolf/wd-vit-large-tagger-v3"
FASTEST_WD_MODEL = "SmilingWolf/wd-vit-tagger-v3"
WINDOWS_RESERVED_FILE_STEMS = {
    "con","prn","aux","nul",*(f"com{value}" for value in range(1,10)),*(f"lpt{value}" for value in range(1,10)),
}
_EXIFTOOL_JUNCTION_LOCK = threading.Lock()
_EXIFTOOL_JUNCTION_ROOT: Path | None = None
_EXIFTOOL_JUNCTION_SOURCE: Path | None = None


@dataclass(frozen=True)
class NvidiaGpuProfile:
    index: int
    name: str
    total_memory_mib: int

    @property
    def total_memory_gib(self) -> float:
        return self.total_memory_mib / 1024


@dataclass(frozen=True)
class OptionalRuntimeImportCheck:
    loaded: bool
    summary: str
    version: str = ""
    providers: tuple[str,...] = ()


class _DependencyVerificationInterrupted(Exception):
    def __init__(self,error: Exception) -> None:
        self.error=error


@dataclass(frozen=True)
class VideoInstallPlan:
    profile_id: str
    reviewed_profile_sha256: str
    component_site: Path
    destination_root: Path
    inventory_sha256: str
    interpreter_path: Path
    interpreter_sha256: str
    python_version: str


@dataclass(frozen=True)
class VideoRuntimeBinding(VideoInstallPlan):
    version: str


@dataclass(frozen=True)
class VideoAdmission:
    """Fresh durable component admission; consumers must recheck before launch."""

    ready: bool
    issues: tuple[str,...]
    profile_id: str = "organizer-video-windows"
    site_path: Path | None = None
    inventory_sha256: str = ""
    binding: VideoRuntimeBinding | None = None


@dataclass(frozen=True)
class WdSetupPreset:
    key: str
    label: str
    model_ids: tuple[str, ...]
    consensus_minimum: int
    summary: str


WD_SETUP_PRESETS = {
    "fastest": WdSetupPreset(
        key="fastest",
        label="Fastest",
        model_ids=(FASTEST_WD_MODEL,),
        consensus_minimum=1,
        summary=(
            "Lowest expected model size and processing cost. Best for quick, "
            "reviewable suggestions; a single model does not provide multi-model consensus."
        ),
    ),
    "balanced": WdSetupPreset(
        key="balanced",
        label="Balanced (Recommended)",
        model_ids=(WD_EVA02_MODEL,RECOMMENDED_WD_CONSENSUS_MODEL),
        consensus_minimum=2,
        summary=(
            "Combines the strongest included published-F1 model with a smaller, "
            "architecture-diverse second opinion."
        ),
    ),
    "high_f1_pair": WdSetupPreset(
        key="high_f1_pair",
        label="Highest Published-F1 Pair (Slower)",
        model_ids=(WD_EVA02_MODEL,HIGH_F1_WD_CONSENSUS_MODEL),
        consensus_minimum=2,
        summary=(
            "Combines the two included models with the highest published "
            "validation F1 scores. Both are large models, so this pair requires "
            "more storage and processing than Balanced."
        ),
    ),
    "most_capable": WdSetupPreset(
        key="most_capable",
        label="Most Capable (Slowest)",
        model_ids=tuple(WD_TAGGER_MODELS),
        consensus_minimum=3,
        summary=(
            "Uses every supported WD v3 model and a strict majority. Offers the "
            "broadest ensemble evidence with the highest download, storage, and processing cost."
        ),
    ),
}


def matching_wd_setup_preset(
    model_ids: tuple[str, ...] | list[str],consensus_minimum: int,
) -> str:
    """Return the matching shortcut key without changing a manual selection."""
    selected=tuple(dict.fromkeys(
        value for value in model_ids if value in WD_TAGGER_MODELS
    ))
    minimum=max(1,int(consensus_minimum))
    for key,preset in WD_SETUP_PRESETS.items():
        if (
            len(selected)==len(preset.model_ids)
            and set(selected)==set(preset.model_ids)
            and minimum==preset.consensus_minimum
        ):
            return key
    return "custom"


@dataclass(frozen=True)
class RecommendedAiSetup:
    """One first-run local-AI selection plus safe settings for detected hardware."""

    gpu_profile: NvidiaGpuProfile | None
    wd_preset_key: str
    wd_model_ids: tuple[str, ...]
    wd_consensus_minimum: int
    wd_execution_provider: str
    wd_batch_size: int
    joycaption_enabled: bool
    joycaption_quantization: str


def recommended_first_run_ai_setup(
    *, gpu_profile: NvidiaGpuProfile | None = None,detect: bool = True,
    wd_preset_key: str = "balanced",
    wd_model_ids: tuple[str, ...] | list[str] | None = None,
    wd_consensus_minimum: int | None = None,
) -> RecommendedAiSetup:
    """Combine a WD shortcut or manual selection with safe hardware settings."""
    preset=WD_SETUP_PRESETS.get(wd_preset_key,WD_SETUP_PRESETS["balanced"])
    selected=tuple(dict.fromkeys(
        value for value in (preset.model_ids if wd_model_ids is None else wd_model_ids)
        if value in WD_TAGGER_MODELS
    ))
    requested_minimum=(
        preset.consensus_minimum
        if wd_consensus_minimum is None else int(wd_consensus_minimum)
    )
    consensus_minimum=max(1,min(requested_minimum,max(1,len(selected))))
    resolved_key=(
        matching_wd_setup_preset(selected,consensus_minimum)
        if wd_model_ids is not None or wd_consensus_minimum is not None
        else preset.key
    )
    profile=(
        gpu_profile if gpu_profile is not None or not detect
        else nvidia_gpu_profile(0)
    )
    if profile is None:
        provider="cpu"; batch_size=1
    else:
        provider="cuda"
        if profile.total_memory_mib>=24*1024:
            batch_size=8
        elif profile.total_memory_mib>=16*1024:
            batch_size=4
        elif profile.total_memory_mib>=8*1024:
            batch_size=2
        else:
            batch_size=1
    return RecommendedAiSetup(
        gpu_profile=profile,
        wd_preset_key=resolved_key,
        wd_model_ids=selected,
        wd_consensus_minimum=consensus_minimum,
        wd_execution_provider=provider,
        wd_batch_size=batch_size,
        joycaption_enabled=profile is not None,
        joycaption_quantization=(
            "bf16" if profile is not None and
            profile.total_memory_mib>=JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB
            else "nf4"
        ),
    )


@lru_cache(maxsize=17)
def nvidia_gpu_profile(gpu_index: int = 0) -> NvidiaGpuProfile | None:
    """Return stable total dedicated VRAM without importing an AI runtime."""
    executable=shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        result=subprocess.run(
            [
                executable,
                f"--id={int(gpu_index)}",
                "--query-gpu=index,name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=True,capture_output=True,text=True,timeout=5,**_NO_CONSOLE,
        )
        row=next(csv.reader(result.stdout.splitlines(),skipinitialspace=True),None)
        if row is None or len(row)<3:
            return None
        index=int(row[0].strip())
        name=row[1].strip()
        total_memory_mib=int(round(float(row[2].strip())))
        if index<0 or not name or total_memory_mib<=0:
            return None
        return NvidiaGpuProfile(index,name,total_memory_mib)
    except (
        OSError,StopIteration,TypeError,ValueError,subprocess.SubprocessError,
    ):
        return None


def recommended_joycaption_quantization(
    settings: AppSettings,
    *,
    nf4_ready: bool,
    gpu_profile: NvidiaGpuProfile | None = None,
) -> str:
    """Choose a first-use mode without overriding an explicit saved choice."""
    current=str(getattr(settings,"joycaption_quantization","bf16"))
    current=current if current in {"bf16","nf4"} else "bf16"
    if bool(getattr(settings,"joycaption_quantization_explicit",False)):
        return current
    profile=(
        gpu_profile
        if gpu_profile is not None
        else nvidia_gpu_profile(int(getattr(settings,"joycaption_gpu_index",0)))
    )
    if (
        profile is not None
        and profile.total_memory_mib<JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB
        and nf4_ready
    ):
        return "nf4"
    return "bf16"


def _optional_component_site() -> Path:
    return OPTIONAL_COMPONENT_ROOT / "python"


def _activate_optional_component_site() -> None:
    site_path = str(_optional_component_site().resolve(strict=False))
    if site_path not in sys.path:
        sys.path.insert(0, site_path)


_activate_optional_component_site()


def _ascii_path(path: Path) -> bool:
    return str(path).isascii()


def _ascii_temp_roots() -> tuple[Path,...]:
    candidates=[Path(tempfile.gettempdir())]
    system_root=os.environ.get("SystemRoot","").strip()
    if system_root:
        candidates.append(Path(system_root)/"Temp")
    unique=[]
    for candidate in candidates:
        try: resolved=candidate.resolve(strict=False)
        except OSError: continue
        if _ascii_path(resolved) and resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _cleanup_exiftool_junction() -> None:
    global _EXIFTOOL_JUNCTION_ROOT,_EXIFTOOL_JUNCTION_SOURCE
    root=_EXIFTOOL_JUNCTION_ROOT
    _EXIFTOOL_JUNCTION_ROOT=None; _EXIFTOOL_JUNCTION_SOURCE=None
    if root is None or not root.name.startswith("CollectiGen-ExifTool-"):
        return
    link=root/"runtime"
    try:
        if os.path.lexists(link): link.rmdir()
        root.rmdir()
    except OSError:
        pass


def _launchable_bundled_exiftool(path: Path) -> Path:
    """Return an ASCII launcher path for ExifTool's legacy Windows bootstrap."""
    global _EXIFTOOL_JUNCTION_ROOT,_EXIFTOOL_JUNCTION_SOURCE
    source=path.resolve(strict=True)
    if sys.platform!="win32" or _ascii_path(source):
        return source
    with _EXIFTOOL_JUNCTION_LOCK:
        if (
            _EXIFTOOL_JUNCTION_ROOT is not None
            and _EXIFTOOL_JUNCTION_SOURCE==source
            and (_EXIFTOOL_JUNCTION_ROOT/"runtime"/source.name).is_file()
        ):
            return _EXIFTOOL_JUNCTION_ROOT/"runtime"/source.name
        _cleanup_exiftool_junction()
        powershell=Path(os.environ.get("SystemRoot",r"C:\Windows"))/"System32"/"WindowsPowerShell"/"v1.0"/"powershell.exe"
        last_error: Exception | None=None
        for temp_root in _ascii_temp_roots():
            root=None
            try:
                temp_root.mkdir(parents=True,exist_ok=True)
                root=Path(tempfile.mkdtemp(prefix="CollectiGen-ExifTool-",dir=temp_root))
                link=root/"runtime"
                environment=os.environ.copy()
                environment["COLLECTIGEN_EXIFTOOL_LINK"]=str(link)
                environment["COLLECTIGEN_EXIFTOOL_SOURCE"]=str(source.parent)
                subprocess.run(
                    [str(powershell),"-NoProfile","-NonInteractive","-Command",
                     "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:COLLECTIGEN_EXIFTOOL_LINK -Target $env:COLLECTIGEN_EXIFTOOL_SOURCE | Out-Null"],
                    check=True,capture_output=True,text=True,timeout=15,env=environment,**_NO_CONSOLE,
                )
                candidate=link/source.name
                if not candidate.is_file():
                    raise RuntimeError("The ExifTool compatibility junction did not expose the executable.")
                _EXIFTOOL_JUNCTION_ROOT=root; _EXIFTOOL_JUNCTION_SOURCE=source
                return candidate
            except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
                last_error=exc
                if root is not None:
                    try:
                        link=root/"runtime"
                        if os.path.lexists(link): link.rmdir()
                        root.rmdir()
                    except OSError:
                        pass
        raise RuntimeError(
            "Bundled ExifTool could not create its temporary ASCII Windows compatibility path."
        ) from last_error


atexit.register(_cleanup_exiftool_junction)


@dataclass(frozen=True)
class DependencyComponent:
    name: str
    group: str
    purpose: str
    installed: bool
    detail: str
    package: str = ""
    required: bool = False
    action: str = ""
    version: str = ""
    location: str = ""
    size_bytes: int = 0
    license_name: str = ""
    license_url: str = ""
    source_url: str = ""
    network_notice: str = ""


@dataclass(frozen=True)
class DependencyRemovalPlan:
    capability: str
    label: str
    model_paths: tuple[Path,...]
    distributions: tuple[str,...]
    retained: tuple[str,...]
    size_bytes: int
    effect: str
    model_ids: tuple[str,...] = ()
    runtime_only: bool = False


@dataclass(frozen=True)
class DependencyRemovalResult:
    label: str
    reclaimed_bytes: int
    removed_distributions: tuple[str,...]
    warnings: tuple[str,...] = ()


@dataclass(frozen=True)
class ModelPackInfo:
    model_id: str
    display_name: str
    revision: str
    license_name: str
    license_url: str
    source_url: str
    size_bytes: int
    file_count: int


@dataclass(frozen=True)
class ComponentPackInfo:
    profile_ids: tuple[str,...]
    display_names: tuple[str,...]
    python_version: str
    runtime_platform: str
    size_bytes: int
    file_count: int


class DependencyService:
    """UI-independent component inventory and approved setup operations."""

    WD_PACKAGES = {
        "Pillow", "NumPy", "ONNX Runtime", "NVIDIA CUDA libraries", "Hugging Face Hub",
    }
    JOY_PACKAGES = {
        "Pillow", "Hugging Face Hub", "PyTorch", "Transformers", "Accelerate",
        "BitsAndBytes",
    }
    JOY_BASE_PACKAGES = JOY_PACKAGES-{"BitsAndBytes"}
    JOY_HARDWARE_COMPONENT = "NVIDIA GPU"
    JOY_COMPONENTS = {*JOY_PACKAGES, JOY_HARDWARE_COMPONENT}
    CAPABILITY_PROFILES = frozenset({"collectigen-core","organizer-image"})
    VIDEO_PROFILE_ID = "organizer-video-windows"
    OPTIONAL_SOURCE_INDEXES = frozenset({
        "https://pypi.org/simple","https://download.pytorch.org/whl/cu128",
    })
    CORE_DISTRIBUTIONS = {"pillow","pyside6","pyside6-essentials","shiboken6","pip","av"}
    WD_REMOVABLE_DISTRIBUTIONS = ("onnxruntime-gpu","onnxruntime")
    JOY_REMOVABLE_DISTRIBUTIONS = (
        "torch","transformers","accelerate","bitsandbytes",
    )

    @classmethod
    def _optional_profile_runtime_issue(cls,payload: dict) -> str:
        target=payload.get("target",{})
        expected_python=str(target.get("python") or "")
        current_python=sys.version.split()[0]
        if not expected_python or current_python==expected_python:
            return ""
        try:
            package=json.loads(
                (APP_ROOT/"portable-package.json").read_text(encoding="utf-8-sig")
            )
        except (OSError,json.JSONDecodeError,TypeError):
            package={}
        installed_portable=(
            isinstance(package,dict)
            and package.get("schema_version") in {1,2}
            and package.get("artifact")=="collectigen-portable-windows-x64"
        )
        if installed_portable:
            return (
                "Local AI setup is unavailable because this portable runtime is "
                f"Python {current_python}; the reviewed local-AI setup requires "
                f"Python {expected_python}. Close CollectiGen, run "
                "Start_CollectiGen.bat again, and choose the verified runtime "
                "repair when prompted. CollectiGen will not install the reviewed "
                "AI packages into this runtime."
            )
        return (
            "Local AI setup is unavailable in this source checkout because it is "
            f"running on Python {current_python}; the reviewed local-AI setup "
            f"requires Python {expected_python}. Run the verified portable "
            "CollectiGen build, or recreate this development environment with the "
            f"required Python {expected_python} runtime, then reopen Guided Setup. "
            "CollectiGen will not install the reviewed AI packages into this "
            "interpreter."
        )

    @classmethod
    def optional_profile_runtime_issue(cls) -> str:
        return cls._optional_profile_runtime_issue(
            cls._optional_profile_contract(validate_runtime=False)
        )

    @classmethod
    def _optional_profile_contract(cls,*,validate_runtime: bool=True) -> dict:
        try:
            payload=json.loads(OPTIONAL_AI_PROFILES.read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError) as exc:
            raise RuntimeError("The reviewed optional-AI profile contract is unavailable or invalid.") from exc
        if payload.get("schema_version")!=1 or payload.get("roadmap_id")!="REL-03":
            raise RuntimeError("The reviewed optional-AI profile contract has an unsupported schema.")
        issue=cls._optional_profile_runtime_issue(payload) if validate_runtime else ""
        if issue:
            raise RuntimeError(issue)
        return payload

    @classmethod
    def _profile_installed_issues(cls,profile: dict) -> list[str]:
        issues=[]
        for package in profile.get("packages",[]):
            name=str(package.get("name") or "")
            expected=str(package.get("version") or "")
            try: actual=metadata.version(name)
            except metadata.PackageNotFoundError: actual=""
            if actual!=expected: issues.append(f"{name}: expected {expected}, installed {actual or 'missing'}")
        return issues

    def optional_profile_id(self,capability: str) -> str:
        if capability=="video":
            if self.capability_profile!="organizer-image":
                raise ValueError("Video setup requires the Organizer image service context.")
            if sys.platform!="win32" or sys.maxsize<=2**32 or sys.version_info<(3,11):
                raise RuntimeError("The Video profile requires Windows x64 and Python 3.11+.")
            return self.VIDEO_PROFILE_ID
        if capability=="joy": return "joycaption"
        if capability=="wd":
            preference=str(getattr(self.settings,"wd_execution_provider","auto") or "auto")
            return "wd-cuda" if preference!="cpu" and shutil.which("nvidia-smi") else "wd-cpu"
        raise ValueError(
            "Only SDXL Tag and Plain Language models have optional install profiles."
        )

    def optional_profile(self,capability: str) -> tuple[str,dict]:
        profile_id=self.optional_profile_id(capability)
        profile=self._optional_profile_contract(
            validate_runtime=capability!="video",
        ).get("profiles",{}).get(profile_id)
        if not isinstance(profile,dict) or profile.get("support")!="supported":
            raise RuntimeError(f"The {profile_id} optional-AI profile has not passed compatibility verification.")
        if profile.get("hardware",{}).get("requires_nvidia_gpu") and not shutil.which("nvidia-smi"):
            raise RuntimeError(
                "The reviewed Plain Language model profile requires a supported NVIDIA GPU."
            )
        packages=profile.get("packages",[])
        if not packages:
            raise RuntimeError(f"The {profile_id} optional-AI profile contains no locked packages.")
        normalized=set()
        for package in packages:
            name=str(package.get("name") or "").strip()
            version=str(package.get("version") or "").strip()
            digest=str(package.get("sha256") or "").strip().casefold()
            key=self._normalize_distribution(name)
            if not name or not version or len(digest)!=64 or key in normalized:
                raise RuntimeError(f"The {profile_id} optional-AI profile contains an invalid package record.")
            normalized.add(key)
        return profile_id,profile

    def __init__(
        self, settings: AppSettings | None = None, *,
        capability_profile: str = "collectigen-core",
        runtime_exiftool: Path | None = None,
    ) -> None:
        if capability_profile not in self.CAPABILITY_PROFILES:
            raise ValueError("Unsupported dependency service capability profile.")
        self.capability_profile=capability_profile
        # Explicit caller-owned runtime admission; never persist into settings.
        self.runtime_exiftool = Path(runtime_exiftool) if runtime_exiftool is not None else None
        self.settings = settings or AppSettings.load()
        self._component_cache: dict[str,DependencyComponent] | None = None
        self._component_sizes_loaded = False
        self._distribution_sizes: dict[str,int] = {}
        self.recovered_optional_components=self._recover_interrupted_optional_install()
        self.optional_cleanup_pending=OPTIONAL_COMPONENT_ROOT.with_name(
            OPTIONAL_COMPONENT_ROOT.name+".previous"
        ).exists()
        self.recovered_removals=self._recover_interrupted_removals()
        self.recovered_installs=self._recover_interrupted_installs()

    def preview_video_install(self,*,check_interrupt=None) -> VideoInstallPlan:
        """Bind a review to the exact current destination without installing anything."""
        if check_interrupt is not None: check_interrupt()
        profile_id=self.optional_profile_id("video")
        profile=self._optional_profile_contract(validate_runtime=False).get("profiles",{}).get(profile_id)
        if not isinstance(profile,dict):
            raise RuntimeError("The reviewed Video profile is unavailable.")
        packages=profile.get("packages",[])
        if (len(packages)!=1 or packages[0].get("name")!="av"
            or packages[0].get("version")!="18.1.0"
            or packages[0].get("filename")!="av-18.1.0-cp311-abi3-win_amd64.whl"):
            raise RuntimeError("The finite Video profile has an unsupported package closure.")
        self._optional_acquisition_groups(packages)
        root=OPTIONAL_COMPONENT_ROOT
        site=root/"python"
        inventory=root/"installed-components.json"
        self._safe_component_directory(root)
        self._safe_component_directory(site)
        if inventory.exists() or inventory.is_symlink():
            self._safe_component_file(inventory,root)
            issues=self._optional_environment_issues(root,check_interrupt=check_interrupt)
            if issues:
                raise RuntimeError("The recorded optional environment is inconsistent: "+"; ".join(issues))
        elif root.exists() and any(root.iterdir()):
            raise RuntimeError("Optional component payload has no verified inventory; recovery is required.")
        interpreter=Path(sys.executable).resolve(strict=True)
        return VideoInstallPlan(
            profile_id,self._profile_sha256(profile),site.resolve(),root.resolve(),
            self._sha256_file(inventory,check_interrupt=check_interrupt) if inventory.exists() else "",
            interpreter,self._sha256_file(interpreter,check_interrupt=check_interrupt),sys.version.split()[0],
        )

    def setup_video(self,*,progress=None,expected_plan: VideoInstallPlan | None=None) -> str:
        return self.install_profile("video",progress=progress,expected_plan=expected_plan)

    def admit_video_runtime(self,*,check_interrupt=None) -> VideoRuntimeBinding:
        """Check cancellation/deadline between reads; propagate caller exceptions."""
        if check_interrupt is None:
            return self._admit_video_runtime()
        def guarded_check():
            try:
                check_interrupt()
            except Exception as exc:
                raise _DependencyVerificationInterrupted(exc) from exc
        try:
            return self._admit_video_runtime(check_interrupt=guarded_check)
        except _DependencyVerificationInterrupted as exc:
            raise exc.error

    def _admit_video_runtime(self,*,check_interrupt=None) -> VideoRuntimeBinding:
        """Fresh durable admission and isolated native load; never load DLLs in the UI."""
        if check_interrupt is not None: check_interrupt()
        try:
            self.optional_profile("video")  # Proposed/unqualified profiles always refuse.
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        plan=self.preview_video_install(check_interrupt=check_interrupt)
        root=plan.destination_root
        payload=self._read_optional_inventory(root)
        issues=list(self._optional_environment_issues(root,check_interrupt=check_interrupt))
        if payload.get("storage_layout")!="durable-optional-python-v1":
            issues.append("The Video component storage layout is unsupported.")
        if plan.profile_id not in payload.get("profile_ids",[]):
            issues.append("The reviewed Video profile is not installed in durable storage.")
        if issues:
            raise RuntimeError("Video component admission refused: "+"; ".join(dict.fromkeys(issues)))
        probe=r"""
import importlib.machinery,json,sys
from pathlib import Path
site=Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0,str(site))
import av
from av import _core
for module in (av,_core):
    if not Path(module.__file__).resolve(strict=True).is_relative_to(site):
        raise RuntimeError("Video module origin is outside the admitted site")
if not any(str(_core.__file__).endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES):
    raise RuntimeError("Video native extension did not load")
if av.__version__!="18.1.0" or not av.library_versions:
    raise RuntimeError("Video version or linked libraries are unavailable")
av.VideoFrame(2,2,"rgb24")
print(json.dumps({"version":av.__version__}))
"""
        if check_interrupt is not None: check_interrupt()
        try:
            result=subprocess.run(
                [str(plan.interpreter_path),"-I","-B","-c",probe,str(plan.component_site)],
                check=True,capture_output=True,text=True,timeout=20,**_NO_CONSOLE,
            )
            if check_interrupt is not None: check_interrupt()
            if result.returncode!=0 or json.loads(result.stdout).get("version")!="18.1.0":
                raise ValueError("Unexpected Video native probe version")
            if plan!=self.preview_video_install(check_interrupt=check_interrupt) or self._optional_environment_issues(root,check_interrupt=check_interrupt):
                raise RuntimeError("The Video runtime changed during admission.")
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            raise RuntimeError("The isolated Video native load failed.") from exc
        return VideoRuntimeBinding(**vars(plan),version="18.1.0")

    def video_admission(self) -> VideoAdmission:
        try:
            binding=self.admit_video_runtime()
        except (ValueError,RuntimeError,OSError) as exc:
            return VideoAdmission(False,(str(exc),))
        return VideoAdmission(True,(),binding.profile_id,binding.component_site,
                              binding.inventory_sha256,binding)

    def _video_component(self,include_sizes: bool=False) -> DependencyComponent:
        organizer=self.capability_profile=="organizer-image"
        admission=self.video_admission() if organizer else None
        return DependencyComponent(
            "PyAV","Video" if organizer else "Core application",
            "Local video verification and playback",
            admission.ready if admission is not None else self._loadable("av"),
            "; ".join(admission.issues) if admission is not None and admission.issues
            else "Python package: PyAV with FFmpeg libraries",
            "" if organizer else "av==18.1.0",not organizer,"package",
            "18.1.0" if admission is not None and admission.ready else self._distribution_version("av"),
            str(admission.site_path) if admission is not None and admission.ready else self._distribution_location("av"),
            self._installed_distribution_size("av") if include_sizes else 0,
            license_name="BSD-3-Clause; bundled FFmpeg licenses apply",
            license_url="https://github.com/PyAV-Org/PyAV/blob/main/LICENSE.txt",
            source_url="https://pypi.org/project/av/18.1.0/",
        )

    @staticmethod
    def _required_model_files(model_id: str) -> tuple[str,...]:
        if model_id in WD_TAGGER_MODELS:
            return ("model.onnx", "selected_tags.csv")
        if model_id in PLAIN_LANGUAGE_MODELS:
            return ("config.json",)
        raise ValueError("The model identity is not supported by this CollectiGen version.")

    @classmethod
    def _model_files_ready(cls, path: Path, model_id: str) -> bool:
        return all((path / relative).is_file() for relative in cls._required_model_files(model_id))

    @staticmethod
    def _model_display_name(model_id: str) -> str:
        if model_id in WD_TAGGER_MODELS:
            return WD_TAGGER_MODELS[model_id][0]
        if model_id in PLAIN_LANGUAGE_MODELS:
            return PLAIN_LANGUAGE_MODELS[model_id][0]
        raise ValueError("The model identity is not supported by this CollectiGen version.")

    @classmethod
    def model_display_name(cls, model_id: str) -> str:
        return cls._model_display_name(model_id)

    def _model_target(self, model_id: str) -> Path:
        if model_id in WD_TAGGER_MODELS:
            return self.wd_model_dir_for(model_id)
        if model_id in PLAIN_LANGUAGE_MODELS:
            return self.plain_language_model_dir_for(model_id)
        raise ValueError("The model identity is not supported by this CollectiGen version.")

    def model_target(self, model_id: str) -> Path:
        """Return the configured local target for one approved model."""
        return self._model_target(model_id)

    def _recover_interrupted_installs(self) -> tuple[Path,...]:
        model_directory=str(getattr(self.settings,"ai_models_directory","") or "")
        if not model_directory: return ()
        root=Path(model_directory)
        if not root.is_dir() or root.is_symlink(): return ()
        recovered=[]
        model_ids=(*WD_TAGGER_MODELS,*PLAIN_LANGUAGE_MODELS)
        for model_id in model_ids:
            target=self._model_target(model_id)
            backup=target.with_name(target.name+".tagmanager-previous")
            if backup.is_dir() and not backup.is_symlink():
                try:
                    if not target.exists() and not target.is_symlink():
                        backup.rename(target); recovered.append(target)
                    elif target.is_symlink():
                        pass
                    elif self._model_files_ready(target,model_id):
                        shutil.rmtree(backup)
                    else:
                        if target.is_symlink(): target.unlink()
                        elif target.exists(): shutil.rmtree(target)
                        backup.rename(target); recovered.append(target)
                except OSError:
                    pass
            try: stages=list(root.glob(target.name+".tagmanager-install-*"))
            except OSError: stages=[]
            for stage in stages:
                if stage.is_symlink() or not stage.is_dir(): continue
                try: shutil.rmtree(stage)
                except OSError: pass
        return tuple(recovered)

    def _recover_interrupted_removals(self) -> tuple[Path,...]:
        model_directory=str(getattr(self.settings,"ai_models_directory","") or "")
        if not model_directory: return ()
        root=Path(model_directory)
        if not root.is_dir() or root.is_symlink(): return ()
        known_names={
            *(directory for _display,directory in WD_TAGGER_MODELS.values()),
            *(directory for _display,directory,_summary in PLAIN_LANGUAGE_MODELS.values()),
        }
        recovered=[]
        try: candidates=list(root.glob("*.tagmanager-remove-*"))
        except OSError: return ()
        for quarantine in candidates:
            if quarantine.is_symlink(): continue
            original_name=quarantine.name.split(".tagmanager-remove-",1)[0]
            original=root/original_name
            if original_name not in known_names or original.exists() or original.is_symlink(): continue
            try: quarantine.rename(original)
            except OSError: continue
            recovered.append(original)
        return tuple(recovered)

    @staticmethod
    def _loadable(module: str) -> bool:
        try:
            importlib.import_module(module)
            return True
        except (ImportError,OSError,RuntimeError):
            return False

    @staticmethod
    def check_wd_runtime_import() -> OptionalRuntimeImportCheck:
        """Check native ONNX loading in the app interpreter, without model inference.

        A child process contains an optional extension crash and avoids loading
        ONNX into the Qt process merely to inspect Setup readiness.
        """
        site=_optional_component_site()
        script=(
            "import json,sys; "
            "sys.path.insert(0,sys.argv[1]); "
            "import numpy; from PIL import Image; import onnxruntime as runtime; "
            "print(json.dumps({'version':runtime.__version__,"
            "'providers':runtime.get_available_providers(),"
            "'origin':runtime.__file__}))"
        )
        environment=os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"]="1"
        environment["PYTHONNOUSERSITE"]="1"
        try:
            result=subprocess.run(
                [sys.executable,"-I","-B","-c",script,str(site)],
                capture_output=True,text=True,timeout=20,
                env=environment,**_NO_CONSOLE,
            )
        except subprocess.TimeoutExpired:
            return OptionalRuntimeImportCheck(
                False,"The AI Tag runtime check timed out. Repair the shared runtime or try Check Again."
            )
        except OSError:
            return OptionalRuntimeImportCheck(
                False,"The current Python runtime could not start a native-load check. Repair the shared AI Tag runtime."
            )
        if result.returncode:
            detail=(result.stderr or "").casefold()
            if "dll load failed" in detail or "specified module could not be found" in detail:
                reason="ONNX Runtime cannot load a native DLL."
            elif "no module named" in detail:
                reason="ONNX Runtime is not available to this Python runtime."
            else:
                reason="ONNX Runtime could not load in this Python runtime."
            return OptionalRuntimeImportCheck(
                False,f"{reason} Repair the shared AI Tag runtime, restart, then Check Again. "
                "Still failing? Send Feedback."
            )
        try:
            payload=json.loads(result.stdout.strip().splitlines()[-1])
            version=str(payload["version"])
            providers=tuple(str(value) for value in payload["providers"])
            origin=Path(str(payload["origin"])).resolve(strict=False)
            if not version or not providers:
                raise ValueError("Incomplete runtime response")
            if not origin.is_relative_to(site.resolve(strict=False)):
                return OptionalRuntimeImportCheck(
                    False,"ONNX Runtime loaded from outside the reviewed optional components. "
                    "Repair the shared AI Tag runtime, then check again."
                )
        except (IndexError,KeyError,TypeError,ValueError,json.JSONDecodeError):
            return OptionalRuntimeImportCheck(
                False,"The AI Tag runtime returned an incomplete check result. Repair or check again."
            )
        return OptionalRuntimeImportCheck(
            True,"ONNX Runtime loads in the current Python environment. Model execution has not been tested.",
            version,providers,
        )

    @staticmethod
    def _normalize_distribution(name: str) -> str:
        return str(name).strip().casefold().replace("_","-").replace(".","-")

    @staticmethod
    def _profile_sha256(profile: dict) -> str:
        return hashlib.sha256(
            json.dumps(profile,sort_keys=True,separators=(",",":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _sha256_file(path: Path,*,check_interrupt=None) -> str:
        digest=hashlib.sha256()
        if check_interrupt is not None: check_interrupt()
        with path.open("rb") as stream:
            while True:
                if check_interrupt is not None: check_interrupt()
                block=stream.read(1024*1024)
                if not block: break
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _distribution_map(cls, path: Path | None = None) -> dict[str,list[metadata.Distribution]]:
        result: dict[str,list[metadata.Distribution]]={}
        distributions=metadata.distributions(path=[str(path)]) if path is not None else metadata.distributions()
        for distribution in distributions:
            name=str(distribution.metadata.get("Name") or "")
            if name:
                result.setdefault(cls._normalize_distribution(name),[]).append(distribution)
        return result

    @classmethod
    def _exact_distribution(
        cls,name: str,version: str,*,path: Path | None=None,
    ) -> metadata.Distribution | None:
        for distribution in cls._distribution_map(path).get(cls._normalize_distribution(name),[]):
            if str(distribution.version)==str(version):
                return distribution
        return None

    @staticmethod
    def _safe_component_directory(path: Path) -> Path:
        try:
            return unredirected_directory(path)
        except (OSError,RuntimeError) as exc:
            raise RuntimeError("Optional component directory is redirected or invalid.") from exc

    @classmethod
    def _safe_component_file(cls,path: Path,root: Path) -> Path:
        admitted=cls._safe_component_directory(root)
        parent=cls._safe_component_directory(path.parent)
        if not parent.is_relative_to(admitted):
            raise RuntimeError("Optional component file escapes its admitted root.")
        try:
            info=path.lstat()
        except OSError as exc:
            raise RuntimeError("Optional component file is unavailable.") from exc
        if (not stat.S_ISREG(info.st_mode)
            or getattr(info,"st_file_attributes",0)&0x400):
            raise RuntimeError("Optional component file is redirected or not regular.")
        absolute=Path(os.path.abspath(path))
        if absolute.resolve(strict=True)!=absolute:
            raise RuntimeError("Optional component file resolves elsewhere.")
        return absolute

    @classmethod
    def _verified_component_files(cls,site: Path,*,check_interrupt=None) -> dict[str,dict]:
        """Retain installation-authored bytes, independently of editable RECORDs."""
        if check_interrupt is not None: check_interrupt()
        admitted=cls._safe_component_directory(site)
        files={}
        if not site.is_dir():
            raise RuntimeError("The optional component site is unavailable.")
        for directory,subdirectories,names in os.walk(site,followlinks=False):
            if check_interrupt is not None: check_interrupt()
            cls._safe_component_directory(Path(directory))
            for name in subdirectories:
                if check_interrupt is not None: check_interrupt()
                cls._safe_component_directory(Path(directory)/name)
            for name in names:
                if check_interrupt is not None: check_interrupt()
                path=cls._safe_component_file(Path(directory)/name,admitted)
                relative=path.relative_to(admitted).as_posix()
                files[relative]={"sha256":cls._sha256_file(path,check_interrupt=check_interrupt),"size_bytes":path.stat().st_size}
        return files

    @classmethod
    def _retained_install_issues(cls,payload: dict,site: Path,*,check_interrupt=None) -> tuple[str,...]:
        try:
            actual=cls._verified_component_files(site,check_interrupt=check_interrupt)
        except RuntimeError as exc:
            return (str(exc),)
        recorded=payload.get("verified_install_files")
        if recorded is None:
            if cls.VIDEO_PROFILE_ID in payload.get("profile_ids",[]):
                return ("Video requires retained verified-install file evidence; recovery is required.",)
            return ()  # Existing AI-only inventories remain readable, never Video-admitted.
        if not isinstance(recorded,dict) or not recorded:
            return ("The retained verified-install file evidence is invalid.",)
        if actual!=recorded:
            return ("Optional component files differ from retained verified-install evidence.",)
        return ()

    @classmethod
    def _distribution_record_valid(cls,distribution: metadata.Distribution,*,check_interrupt=None) -> bool:
        if check_interrupt is not None: check_interrupt()
        try:
            root=cls._safe_component_directory(Path(distribution.locate_file("")))
            info=getattr(distribution,"_path",None)
            if not isinstance(info,Path):
                return False
            cls._safe_component_file(info/"RECORD",root)
            record=distribution.read_text("RECORD")
        except (OSError,RuntimeError):
            return False
        if not record:
            return False
        verified=0
        try:
            rows=csv.reader(record.splitlines())
            for row in rows:
                if check_interrupt is not None: check_interrupt()
                if len(row)<2:
                    return False
                relative,digest=row[0],row[1]
                if not digest:
                    if relative.casefold().endswith(".dist-info/record"):
                        continue
                    return False
                algorithm,separator,encoded=digest.partition("=")
                if separator!="=" or algorithm.casefold()!="sha256":
                    return False
                parts=PurePosixPath(relative)
                if parts.is_absolute() or "\\" in relative:
                    return False
                file_root=root
                if ".." in parts.parts:
                    # Stock interpreter packages can record their Scripts entries.
                    # Admit only those inside this exact interpreter environment;
                    # durable component RECORDs may never escape their own site.
                    runtime_root=Path(sys.executable).resolve().parent
                    if runtime_root.name.casefold() in {"scripts","bin"}:
                        runtime_root=runtime_root.parent
                    if (not root.is_relative_to(runtime_root)
                        or root.is_relative_to(OPTIONAL_COMPONENT_ROOT)):
                        return False
                    file_root=runtime_root
                source=cls._safe_component_file(Path(distribution.locate_file(parts)),file_root)
                if not source.is_file():
                    return False
                expected=base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4)).hex()
                if cls._sha256_file(source,check_interrupt=check_interrupt)!=expected:
                    return False
                verified+=1
        except (OSError,ValueError,RuntimeError):
            return False
        return verified>0

    @classmethod
    def _profile_issues_at_path(cls,profile: dict,path: Path) -> list[str]:
        installed=cls._distribution_map(path)
        issues=[]
        for package in profile.get("packages",[]):
            name=str(package.get("name") or "")
            expected=str(package.get("version") or "")
            actual=next(
                (str(value.version) for value in installed.get(cls._normalize_distribution(name),[])
                 if str(value.version)==expected),
                "",
            )
            if actual!=expected:
                versions=", ".join(
                    sorted({str(value.version) for value in installed.get(cls._normalize_distribution(name),[])})
                )
                issues.append(f"{name}: expected {expected}, installed {versions or 'missing'}")
        return issues

    @classmethod
    def _read_optional_inventory(cls,root: Path | None=None) -> dict:
        directory=root or OPTIONAL_COMPONENT_ROOT
        target=directory/"installed-components.json"
        try:
            cls._safe_component_directory(directory)
            cls._safe_component_file(target,directory)
            payload=json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError,RuntimeError,json.JSONDecodeError):
            return {}
        return payload if isinstance(payload,dict) else {}

    @classmethod
    def _optional_environment_issues(cls,root: Path,*,check_interrupt=None) -> tuple[str,...]:
        if check_interrupt is not None: check_interrupt()
        issues=[]
        try:
            cls._safe_component_directory(root)
            cls._safe_component_directory(root/"python")
            cls._safe_component_file(root/"installed-components.json",root)
        except RuntimeError as exc:
            return (str(exc),)
        payload=cls._read_optional_inventory(root)
        if payload.get("schema_version")!=2:
            issues.append("The installed-component inventory schema is missing or unsupported.")
        if payload.get("python_version")!=sys.version.split()[0]:
            issues.append(
                "The optional-component Python version does not match this runtime "
                f"({payload.get('python_version') or 'missing'} != {sys.version.split()[0]})."
            )
        if not isinstance(payload.get("profile_ids"),list):
            issues.append("The installed-component inventory has no profile list.")
        if issues:
            return tuple(issues)
        try:
            contract=cls._optional_profile_contract(validate_runtime=False)
        except RuntimeError as exc:
            return (str(exc),)
        profiles=contract.get("profiles",{})
        site=root/"python"
        issues.extend(cls._retained_install_issues(payload,site,check_interrupt=check_interrupt))
        if issues:
            return tuple(issues)
        installed=cls._distribution_map(site)
        verified_records=set()
        for profile_id in payload["profile_ids"]:
            if check_interrupt is not None: check_interrupt()
            profile=profiles.get(profile_id)
            if not isinstance(profile,dict):
                issues.append(f"The {profile_id} optional profile is no longer in the reviewed contract.")
                continue
            issues.extend(
                f"{profile_id}: {value}" for value in cls._profile_issues_at_path(profile,site)
            )
            for package in profile.get("packages",[]):
                if check_interrupt is not None: check_interrupt()
                name=str(package.get("name") or "")
                version=str(package.get("version") or "")
                key=(cls._normalize_distribution(name),version)
                if key in verified_records:
                    continue
                verified_records.add(key)
                candidates=installed.get(key[0],[])
                if not any(
                    str(value.version)==version
                    and cls._distribution_record_valid(value,check_interrupt=check_interrupt)
                    for value in candidates
                ):
                    issues.append(f"{name} {version}: installed-file RECORD verification failed.")
            expected=cls._profile_sha256(profile)
            recorded=next(
                (value for value in payload.get("profiles",[])
                 if isinstance(value,dict) and value.get("id")==profile_id),
                {},
            )
            if recorded.get("sha256")!=expected:
                issues.append(f"The {profile_id} reviewed-profile fingerprint does not match.")
        return tuple(dict.fromkeys(issues))

    @classmethod
    def _optional_environment_valid(cls,root: Path) -> bool:
        return not cls._optional_environment_issues(root)

    @classmethod
    def _recover_interrupted_optional_install(cls) -> bool:
        root=OPTIONAL_COMPONENT_ROOT
        parent=root.parent
        backup=root.with_name(root.name+".previous")
        try:
            cls._safe_component_directory(root)
            cls._safe_component_directory(backup)
        except RuntimeError:
            return False
        recovered=False
        try:
            if backup.is_dir() and not backup.is_symlink():
                if not root.exists():
                    backup.rename(root); recovered=True
                else:
                    # A staged environment passes complete RECORD read-back
                    # verification before its same-volume rename to ``root``.
                    # Once both names exist, the swap completed and ``backup``
                    # is cleanup-only. Re-hashing the replacement here repeats
                    # a multi-gigabyte verification and can block the UI thread.
                    shutil.rmtree(backup)
            for stage in parent.glob(f".{root.name}-install-*"):
                if stage.is_dir():
                    cls._safe_component_directory(stage)
                    shutil.rmtree(stage)
        except (OSError,RuntimeError):
            pass
        if root.is_dir():
            _activate_optional_component_site()
            importlib.invalidate_caches()
        return recovered

    @classmethod
    def _active_optional_profile_ids(cls) -> tuple[str,...]:
        contract=cls._optional_profile_contract(validate_runtime=False)
        profiles=contract.get("profiles",{})
        payload=cls._read_optional_inventory()
        recorded=payload.get("profile_ids",[]) if payload.get("schema_version")==2 else []
        inventory=OPTIONAL_COMPONENT_ROOT/"installed-components.json"
        if inventory.exists() or inventory.is_symlink():
            issues=cls._optional_environment_issues(OPTIONAL_COMPONENT_ROOT)
            if issues:
                raise RuntimeError("Recorded optional profiles cannot be silently discarded: "+"; ".join(issues))
            return tuple(dict.fromkeys(recorded))
        if OPTIONAL_COMPONENT_ROOT.exists() and any(OPTIONAL_COMPONENT_ROOT.iterdir()):
            raise RuntimeError("Optional component payload has no inventory; recovery is required.")
        active=[]

        try:
            legacy=json.loads(LEGACY_OPTIONAL_COMPONENT_INVENTORY.read_text(encoding="utf-8-sig"))
        except (OSError,json.JSONDecodeError):
            legacy={}
        legacy_id=str(legacy.get("profile_id") or "")
        if legacy_id in profiles and not cls._profile_installed_issues(profiles[legacy_id]):
            active.append(legacy_id)

        return tuple(dict.fromkeys(active))

    @classmethod
    def _trusted_optional_distribution_roots(
        cls,profiles: dict[str,dict],
    ) -> tuple[Path,...]:
        roots=[]
        if cls._optional_environment_valid(OPTIONAL_COMPONENT_ROOT):
            roots.append(_optional_component_site())
        try:
            legacy=json.loads(LEGACY_OPTIONAL_COMPONENT_INVENTORY.read_text(encoding="utf-8-sig"))
        except (OSError,json.JSONDecodeError):
            legacy={}
        legacy_id=str(legacy.get("profile_id") or "")
        profile=profiles.get(legacy_id)
        if (
            isinstance(profile,dict)
            and legacy.get("schema_version")==2
            and legacy.get("profile_sha256")==cls._profile_sha256(profile)
        ):
            runtime_root=Path(sys.executable).resolve().parent
            for value in sys.path:
                if not value:
                    continue
                candidate=Path(value).resolve(strict=False)
                if candidate==runtime_root or runtime_root in candidate.parents:
                    roots.append(candidate)
        return tuple(dict.fromkeys(path.resolve(strict=False) for path in roots))

    @classmethod
    def _profile_union(cls,profile_ids: tuple[str,...]) -> tuple[dict[str,dict],list[dict]]:
        contract=cls._optional_profile_contract(validate_runtime=False)
        available=contract.get("profiles",{})
        profiles: dict[str,dict]={}
        packages: dict[str,dict]={}
        for profile_id in profile_ids:
            profile=available.get(profile_id)
            if not isinstance(profile,dict) or profile.get("support")!="supported":
                raise RuntimeError(f"The {profile_id} optional-AI profile is unavailable.")
            profiles[profile_id]=profile
            for package in profile.get("packages",[]):
                key=cls._normalize_distribution(package.get("name",""))
                previous=packages.get(key)
                if previous is not None and (
                    previous.get("version")!=package.get("version")
                    or str(previous.get("sha256","")).casefold()!=str(package.get("sha256","")).casefold()
                ):
                    raise RuntimeError(
                        f"The reviewed optional profiles require conflicting {package.get('name')} versions."
                    )
                packages[key]=dict(package)
        return profiles,[packages[key] for key in sorted(packages)]

    @classmethod
    def _copy_distribution_to_site(
        cls,distribution: metadata.Distribution,target: Path,
    ) -> None:
        source_root=cls._safe_component_directory(Path(distribution.locate_file("")))
        cls._safe_component_directory(target)
        files=tuple(distribution.files or ())
        if not files:
            raise RuntimeError(
                f"Installed {distribution.metadata.get('Name') or 'package'} has no auditable file inventory."
            )
        for relative in files:
            original=Path(distribution.locate_file(relative))
            # Installed script records outside site-packages are not payload copies.
            # Refuse redirects in every copied path before resolving it.
            if not Path(os.path.abspath(original)).is_relative_to(source_root):
                continue
            cls._safe_component_directory(original.parent)
            if original.is_dir():
                source=cls._safe_component_directory(original)
            elif original.exists() or original.is_symlink():
                source=cls._safe_component_file(original,source_root)
            else:
                continue
            try:
                safe_relative=source.relative_to(source_root)
            except ValueError:
                continue
            destination=target/safe_relative
            if source.is_symlink():
                raise RuntimeError(f"Refusing to copy a linked optional-component file: {source}")
            if source.is_dir():
                destination.mkdir(parents=True,exist_ok=True)
                continue
            if not source.is_file():
                continue
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists():
                if not destination.is_file() or cls._sha256_file(destination)!=cls._sha256_file(source):
                    raise RuntimeError(f"Optional profiles contain conflicting files: {safe_relative}")
                continue
            shutil.copy2(source,destination)

    @classmethod
    def _normalize_staged_distribution_record(
        cls,distribution: metadata.Distribution,component_site: Path,
    ) -> None:
        root=cls._safe_component_directory(component_site)
        rows=[]
        record_paths=[]
        for relative in distribution.files or ():
            original=Path(distribution.locate_file(relative))
            if not original.exists() and not original.is_symlink():
                continue
            candidate=cls._safe_component_file(original,root)
            try:
                safe_relative=candidate.relative_to(root)
            except ValueError:
                continue
            if not candidate.is_file() or candidate.is_symlink():
                continue
            portable=safe_relative.as_posix()
            if (
                safe_relative.name.casefold()=="record"
                and safe_relative.parent.name.casefold().endswith(".dist-info")
                and safe_relative.parent.parent==Path(".")
            ):
                record_paths.append(candidate)
                continue
            digest=base64.urlsafe_b64encode(
                bytes.fromhex(cls._sha256_file(candidate))
            ).rstrip(b"=").decode("ascii")
            rows.append((portable,f"sha256={digest}",str(candidate.stat().st_size)))
        if len(record_paths)!=1:
            raise RuntimeError(
                f"Staged {distribution.metadata.get('Name') or 'package'} has "
                f"{len(record_paths)} top-level RECORD files; exactly one is required."
            )
        record_path=record_paths[0]
        rows.append((record_path.relative_to(root).as_posix(),"",""))
        output=io.StringIO(newline="")
        writer=csv.writer(output,lineterminator="\n")
        writer.writerows(rows)
        record_path.write_text(output.getvalue(),encoding="utf-8",newline="")

    @classmethod
    def _distribution_version(cls,*names: str) -> str:
        for name in names:
            try:
                return metadata.version(name)
            except metadata.PackageNotFoundError:
                continue
        return ""

    @classmethod
    def _distribution_location(cls,*names: str) -> str:
        for name in names:
            try:
                return str(Path(metadata.distribution(name).locate_file("")).resolve())
            except metadata.PackageNotFoundError:
                continue
        return ""

    @staticmethod
    def _directory_size(path: Path) -> int:
        if path.is_symlink() or not path.exists(): return 0
        total=0
        try:
            for root,_directories,files in os.walk(path,followlinks=False):
                for filename in files:
                    candidate=Path(root)/filename
                    try: total+=candidate.stat(follow_symlinks=False).st_size
                    except OSError: continue
        except OSError:
            return total
        return total

    @classmethod
    def _component_path_size(cls,path: Path) -> int:
        """Measure one component without walking an unrelated system directory."""
        try:
            if path.is_file(): return path.stat().st_size
        except OSError:
            return 0
        return cls._directory_size(path)

    @classmethod
    def _exiftool_size(cls,path: Path) -> int:
        try:
            resolved=path.resolve(strict=False)
            bundled=PRIVATE_EXIFTOOL_DIR.resolve(strict=False)
        except OSError:
            return 0
        if resolved==bundled or bundled in resolved.parents:
            return cls._directory_size(PRIVATE_EXIFTOOL_DIR)
        return cls._component_path_size(path)

    def _distribution_size(self,name: str) -> int:
        key=self._normalize_distribution(name)
        if key in self._distribution_sizes: return self._distribution_sizes[key]
        try: distribution=metadata.distribution(name)
        except metadata.PackageNotFoundError: return 0
        total=0
        for relative in distribution.files or ():
            try:
                candidate=Path(distribution.locate_file(relative))
                if candidate.is_file(): total+=candidate.stat().st_size
            except OSError:
                continue
        self._distribution_sizes[key]=total
        return total

    def _installed_distribution_size(self,*names: str) -> int:
        for name in names:
            if self._distribution_version(name): return self._distribution_size(name)
        return 0

    @staticmethod
    def format_size(size_bytes: int) -> str:
        size=float(max(0,size_bytes))
        for unit in ("B","KiB","MiB","GiB","TiB"):
            if size<1024 or unit=="TiB":
                return f"{size:.0f} {unit}" if unit in {"B","KiB"} else f"{size:.1f} {unit}"
            size/=1024
        return "0 B"

    @property
    def wd_model_dir(self) -> Path:
        return self.wd_model_dir_for(WD_EVA02_MODEL)

    def wd_model_dir_for(self, model_id: str) -> Path:
        return Path(self.settings.ai_models_directory) / WD_TAGGER_MODELS[model_id][1]

    @staticmethod
    def wd_component_name(model_id: str) -> str:
        if model_id == WD_EVA02_MODEL:
            return "WD EVA02 model"
        return f"{WD_TAGGER_MODELS[model_id][0]} model"

    @classmethod
    def wd_component_names(cls) -> set[str]:
        return {cls.wd_component_name(model_id) for model_id in WD_TAGGER_MODELS}

    @classmethod
    def model_id_for_component(cls, name: str) -> str:
        wd_model=next(
            (model_id for model_id in WD_TAGGER_MODELS if cls.wd_component_name(model_id) == name),
            "",
        )
        if wd_model:
            return wd_model
        return next(
            (
                model_id for model_id in PLAIN_LANGUAGE_MODELS
                if cls.plain_language_component_name(model_id)==name
            ),
            "",
        )

    @staticmethod
    def plain_language_component_name(model_id: str) -> str:
        return f"{PLAIN_LANGUAGE_MODELS[model_id][0]} model"

    def plain_language_model_dir_for(self,model_id: str) -> Path:
        return (
            Path(self.settings.ai_models_directory)
            /PLAIN_LANGUAGE_MODELS[model_id][1]
        )

    @property
    def joy_model_dir(self) -> Path:
        return self.plain_language_model_dir_for(JOYCAPTION_MODEL)

    def exiftool_source_path(self) -> str:
        configured = self.settings.exiftool_path.strip()
        bundled = PRIVATE_EXIFTOOL_DIR / "exiftool.exe"
        if configured and Path(configured).is_file():
            return configured
        if self.runtime_exiftool is not None:
            # An explicitly selected runtime must not borrow a system tool.
            return str(self.runtime_exiftool) if self.runtime_exiftool.is_file() else ""
        if bundled.is_file():
            return str(bundled)
        return shutil.which("exiftool") or ""

    def exiftool_path(self) -> str:
        source=self.exiftool_source_path()
        bundled = PRIVATE_EXIFTOOL_DIR / "exiftool.exe"
        if source:
            try:
                if (Path(source).resolve(strict=False)==bundled.resolve(strict=False)
                    or (self.runtime_exiftool is not None
                        and Path(source).resolve(strict=False)==self.runtime_exiftool.resolve(strict=False))):
                    return str(_launchable_bundled_exiftool(Path(source)))
            except (OSError,RuntimeError):
                pass
        return source

    @classmethod
    def exiftool_ready(cls, path: str) -> bool:
        if not path: return False
        try:
            return bool(cls._verify_exiftool(Path(path)))
        except (OSError,subprocess.SubprocessError,RuntimeError):
            return False

    def components(self,*,include_sizes: bool=True) -> list[DependencyComponent]:
        if self._component_cache is not None and not include_sizes:
            return list(self._component_cache.values())
        measured=lambda callable_:callable_() if include_sizes else 0
        exif_source=self.exiftool_source_path(); exif=self.exiftool_path()
        try: exif_version=self._verify_exiftool(Path(exif)) if exif else ""
        except (OSError,subprocess.SubprocessError,RuntimeError): exif_version=""
        exif_ready=bool(exif_version)
        numpy_version=self._distribution_version("numpy")
        onnx_version=self._distribution_version("onnxruntime-gpu","onnxruntime")
        hub_version=self._distribution_version("huggingface-hub")
        torch_version=self._distribution_version("torch")
        transformers_version=self._distribution_version("transformers")
        accelerate_version=self._distribution_version("accelerate")
        bitsandbytes_version=self._distribution_version("bitsandbytes")
        gpu_present=bool(shutil.which("nvidia-smi"))
        gpu_requested=gpu_present and str(getattr(self.settings,"wd_execution_provider","auto") or "auto")!="cpu"
        profile_contract=self._optional_profile_contract(validate_runtime=False)
        wd_profile=profile_contract.get("profiles",{}).get(self.optional_profile_id("wd"),{})
        joy_profile=profile_contract.get("profiles",{}).get("joycaption",{})
        wd_profile_ready=not self._profile_installed_issues(wd_profile)
        joy_profile_issues=self._profile_installed_issues(joy_profile)
        joy_quantization=str(
            getattr(self.settings,"joycaption_quantization","bf16") or "bf16"
        )
        if joy_quantization not in {"bf16","nf4"}:
            joy_quantization="bf16"
        selected_caption_model=str(
            getattr(self.settings,"plain_language_model",JOYCAPTION_MODEL)
            or JOYCAPTION_MODEL
        )
        needs_bitsandbytes=(
            selected_caption_model==JOYCAPTION_MODEL
            and joy_quantization=="nf4"
        )
        joy_profile_ready=not [
            issue for issue in joy_profile_issues
            if needs_bitsandbytes
            or not issue.casefold().startswith("bitsandbytes:")
        ]
        joy_full_profile_ready=not joy_profile_issues
        cuda_ready=(
            not gpu_requested
            or (
                wd_profile_ready
                and bool(onnx_version)
                and (
                    not include_sizes
                    or WDEva02Backend.device_label()=="NVIDIA GPU (CUDA)"
                )
            )
        )
        components = [
            DependencyComponent("Python 3.11+","Core application","Runs CollectiGen and its default video decoder",sys.version_info>=(3,11),f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",required=True,version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",location=sys.executable),
            DependencyComponent("PySide6","Core application","Displays the unified interface",self._loadable("PySide6"),"Python package: PySide6 Essentials","PySide6_Essentials==6.11.2",True,"package",self._distribution_version("PySide6-Essentials","PySide6"),self._distribution_location("PySide6-Essentials","PySide6"),measured(lambda:self._installed_distribution_size("PySide6-Essentials","PySide6")),license_name="LGPL-3.0-only or GPL-3.0-only by Qt module",license_url="https://doc.qt.io/qt-6/licensing.html",source_url="https://download.qt.io/official_releases/QtForPython/pyside6/"),
            DependencyComponent("Pillow","Core application","Image previews and galleries",self._loadable("PIL"),"Python package: Pillow","Pillow==12.3.0",True,"package",self._distribution_version("Pillow"),self._distribution_location("Pillow"),measured(lambda:self._distribution_size("Pillow")),license_name="MIT-CMU",license_url="https://github.com/python-pillow/Pillow/blob/main/LICENSE",source_url="https://pypi.org/project/Pillow/"),
            self._video_component(include_sizes),
            DependencyComponent("NumPy","AI tag suggestions","Prepares images for the WD tagger",bool(numpy_version) and wd_profile_ready,"Reviewed optional profile","numpy==2.2.5",action="package",version=numpy_version,location=self._distribution_location("numpy"),size_bytes=measured(lambda:self._distribution_size("numpy")),license_name="BSD-3-Clause and bundled notices",source_url="https://pypi.org/project/numpy/",network_notice="Installed from the exact reviewed PyPI wheel profile only after setup confirmation."),
            DependencyComponent("ONNX Runtime","AI tag suggestions","Runs the WD tagger locally",bool(onnx_version),"Reviewed optional profile","onnxruntime-gpu==1.26.0",action="package",version=onnx_version,location=self._distribution_location("onnxruntime-gpu","onnxruntime"),size_bytes=measured(lambda:self._installed_distribution_size("onnxruntime-gpu","onnxruntime")),license_name="MIT and bundled notices",source_url="https://pypi.org/project/onnxruntime-gpu/",network_notice="Installed from the exact reviewed PyPI wheel profile only after setup confirmation."),
            DependencyComponent("NVIDIA CUDA libraries","AI tag suggestions","Lets ONNX Runtime use an NVIDIA GPU",cuda_ready,("Not needed without an NVIDIA GPU" if not gpu_present else "Not selected for the CPU profile") if not gpu_requested else ("GPU runtime libraries" if cuda_ready else "CUDA/cuDNN libraries are missing"),"onnxruntime-gpu[cuda,cudnn]==1.26.0",action="package",version="Available" if cuda_ready and gpu_requested else ("Not needed" if not gpu_requested else "Missing"),license_name="NVIDIA proprietary software terms",license_url="https://docs.nvidia.com/cuda/eula/index.html",source_url="https://pypi.org/project/nvidia-cudnn-cu12/",network_notice="Optional proprietary GPU runtime packages are installed from the exact reviewed PyPI wheel profile only after setup confirmation."),
            DependencyComponent("Hugging Face Hub","AI models","Downloads approved model files",bool(hub_version),"Shared by approved model installers","huggingface-hub==0.34.4",action="package",version=hub_version,location=self._distribution_location("huggingface-hub"),size_bytes=measured(lambda:self._distribution_size("huggingface-hub")),license_name="Apache-2.0",source_url="https://pypi.org/project/huggingface-hub/",network_notice="Contacts Hugging Face only for a model download that the user confirms."),
            DependencyComponent(self.JOY_HARDWARE_COMPONENT,"AI descriptions","Runs the reviewed caption-model profile",gpu_present,"The reviewed caption-model profile requires a supported NVIDIA GPU",version="Available" if gpu_present else "Required",location=shutil.which("nvidia-smi") or "",source_url="https://developer.nvidia.com/cuda-gpus"),
            DependencyComponent("PyTorch","AI descriptions","Runs local caption models",bool(torch_version) and joy_profile_ready,"Reviewed NVIDIA CUDA profile","torch==2.7.0+cu128",action="package",version=torch_version,location=self._distribution_location("torch"),size_bytes=measured(lambda:self._distribution_size("torch")),license_name="BSD-3-Clause and bundled notices",source_url="https://download.pytorch.org/whl/cu128",network_notice="Installed from the exact reviewed official PyTorch CUDA wheel profile only after setup confirmation."),
            DependencyComponent("Transformers","AI descriptions","Loads and runs local caption models",bool(transformers_version),"Reviewed optional profile","transformers==4.56.2",action="package",version=transformers_version,location=self._distribution_location("transformers"),size_bytes=measured(lambda:self._distribution_size("transformers")),license_name="Apache-2.0",source_url="https://pypi.org/project/transformers/",network_notice="Installed from the exact reviewed PyPI wheel profile only after setup confirmation."),
            DependencyComponent("Accelerate","AI descriptions","Loads local caption models efficiently",bool(accelerate_version),"Reviewed optional profile","accelerate==1.6.0",action="package",version=accelerate_version,location=self._distribution_location("accelerate"),size_bytes=measured(lambda:self._distribution_size("accelerate")),license_name="Apache-2.0",source_url="https://pypi.org/project/accelerate/",network_notice="Installed from the exact reviewed PyPI wheel profile only after setup confirmation."),
            DependencyComponent("BitsAndBytes","AI descriptions","Offers the optional 4-bit NF4 JoyCaption memory mode",bool(bitsandbytes_version) and joy_full_profile_ready,"Reviewed optional profile","bitsandbytes==0.48.1",action="package",version=bitsandbytes_version,location=self._distribution_location("bitsandbytes"),size_bytes=measured(lambda:self._distribution_size("bitsandbytes")),license_name="MIT",source_url="https://pypi.org/project/bitsandbytes/",network_notice="Installed from the exact reviewed Windows wheel profile only after setup confirmation; 4-bit mode remains an explicit user choice."),
            DependencyComponent("ExifTool","Core application","Reads and writes generation metadata inside images",exif_ready,exif_source if exif_ready else "Missing or unable to run",required=True,action="exiftool",version=exif_version,location=exif_source,size_bytes=measured(lambda:self._exiftool_size(Path(exif_source))) if exif_ready else 0),
        ]
        model_components = []
        for model_id, (display, _directory) in WD_TAGGER_MODELS.items():
            model_dir = self.wd_model_dir_for(model_id)
            ready = (model_dir / "model.onnx").is_file() and (model_dir / "selected_tags.csv").is_file()
            model_components.append(DependencyComponent(
                self.wd_component_name(model_id), "AI tag suggestions",
                f"Adds {display} to selectable local taggers", ready, str(model_dir), action="wd_model",
                version=installed_model_revision(model_dir,model_id) or model_id,location=str(model_dir),size_bytes=measured(lambda path=model_dir:self._directory_size(path)),
                license_name=MODEL_ACQUISITIONS[model_id].license_name,
                license_url=MODEL_ACQUISITIONS[model_id].license_url,
                source_url=MODEL_ACQUISITIONS[model_id].source_url,
                network_notice="Downloaded from Hugging Face only after confirmation; analysis then runs locally.",
            ))
        for model_id,(display,_directory,_summary) in PLAIN_LANGUAGE_MODELS.items():
            model_dir=self.plain_language_model_dir_for(model_id)
            ready=(model_dir/"config.json").is_file()
            model_components.append(DependencyComponent(
                self.plain_language_component_name(model_id),
                "AI descriptions",
                f"Adds {display} as a selectable local caption model",
                ready,str(model_dir),action="joy_model",
                version=installed_model_revision(model_dir,model_id) or model_id,
                location=str(model_dir),
                size_bytes=measured(
                    lambda path=model_dir:self._directory_size(path)
                ),
                license_name=MODEL_ACQUISITIONS[model_id].license_name,
                license_url=MODEL_ACQUISITIONS[model_id].license_url,
                source_url=MODEL_ACQUISITIONS[model_id].source_url,
                network_notice="Downloaded from Hugging Face only after confirmation; analysis then runs locally.",
            ))
        insert_at = next(index for index, item in enumerate(components) if item.name == "PyTorch")
        components[insert_at:insert_at] = model_components
        self._component_cache={item.name:item for item in components}
        self._component_sizes_loaded=include_sizes
        return components

    def component(self, name: str) -> DependencyComponent | None:
        if self._component_cache is None: self.components(include_sizes=False)
        return self._component_cache.get(name) if self._component_cache is not None else None

    def capability_ready(self,capability: str) -> bool:
        """Whether every configured local component for an AI capability is ready."""
        if capability=="video":
            return self.video_admission().ready
        components={item.name:item for item in self.components(include_sizes=False)}
        if capability=="wd":
            enabled=[
                value for value in self.settings.enabled_wd_taggers
                if value in WD_TAGGER_MODELS
            ] or [WD_EVA02_MODEL]
            names={*self.WD_PACKAGES,*(self.wd_component_name(value) for value in enabled)}
        elif capability=="joy":
            model_id=str(
                getattr(self.settings,"plain_language_model",JOYCAPTION_MODEL)
                or JOYCAPTION_MODEL
            )
            if model_id not in PLAIN_LANGUAGE_MODELS:
                model_id=JOYCAPTION_MODEL
            names={
                *self.JOY_BASE_PACKAGES,self.JOY_HARDWARE_COMPONENT,
                self.plain_language_component_name(model_id),
            }
            if (
                model_id==JOYCAPTION_MODEL
                and str(getattr(
                    self.settings,"joycaption_quantization","bf16"
                ))=="nf4"
            ):
                names.add("BitsAndBytes")
        else:
            return False
        return all(components.get(name) is not None and components[name].installed for name in names)

    def missing_packages(self, names: set[str] | None = None, *, required: bool = False) -> list[str]:
        if self._component_cache is None: self.components(include_sizes=False)
        return [item.package for item in (self._component_cache or {}).values()
                if item.package and not item.installed and (not required or item.required)
                and (names is None or item.name in names)]

    def _persistent_profile_removal_packages(self,capability: str) -> tuple[str,...]:
        payload=self._read_optional_inventory()
        if (
            payload.get("schema_version")!=2
            or not OPTIONAL_COMPONENT_ROOT.is_dir()
            or not self._optional_environment_valid(OPTIONAL_COMPONENT_ROOT)
        ):
            return ()
        active=tuple(value for value in payload.get("profile_ids",[]) if isinstance(value,str))
        removed_ids=tuple(
            value for value in active
            if (value.startswith("wd-") if capability=="wd" else value=="joycaption")
        )
        if not removed_ids:
            return ()
        retained_ids=tuple(value for value in active if value not in removed_ids)
        _removed_profiles,removed_packages=self._profile_union(removed_ids)
        _retained_profiles,retained_packages=self._profile_union(retained_ids)
        retained_names={self._normalize_distribution(value["name"]) for value in retained_packages}
        protected={self._normalize_distribution(value) for value in self.CORE_DISTRIBUTIONS}
        installed=self._distribution_map(_optional_component_site())
        return tuple(
            str(value["name"]) for value in removed_packages
            if self._normalize_distribution(value["name"]) not in retained_names|protected
            and any(
                str(distribution.version)==str(value["version"])
                for distribution in installed.get(self._normalize_distribution(value["name"]),[])
            )
        )

    def removal_plan(
        self,capability: str,*,model_ids: list[str] | None=None,
    ) -> DependencyRemovalPlan:
        if capability not in {"wd","joy"}: raise ValueError("Only optional AI capabilities can be removed.")
        paths: list[Path]=[]; selected_ids: list[str]=[]
        if capability=="joy":
            candidates=(
                model_ids if model_ids is not None
                else list(PLAIN_LANGUAGE_MODELS)
            )
            for model_id in candidates:
                if model_id not in PLAIN_LANGUAGE_MODELS:
                    continue
                path=self.plain_language_model_dir_for(model_id)
                if path.exists() or path.is_symlink():
                    paths.append(path); selected_ids.append(model_id)
            label=(
                self.plain_language_component_name(selected_ids[0])
                if len(selected_ids)==1 else
                "AI Description model files"
            )
            if self.settings.plain_language_model in selected_ids:
                effect=(
                    "Turns off automatic AI Description generation; existing "
                    "image evidence is preserved."
                )
            else:
                effect=(
                    "Removes only the selected unused caption-model files; the "
                    "current AI Description workflow and existing image evidence "
                    "are preserved."
                )
        else:
            candidates=model_ids if model_ids is not None else list(WD_TAGGER_MODELS)
            for model_id in candidates:
                if model_id not in WD_TAGGER_MODELS: continue
                path=self.wd_model_dir_for(model_id)
                if path.exists() or path.is_symlink(): paths.append(path); selected_ids.append(model_id)
            label=(
                self.wd_component_name(selected_ids[0])
                if len(selected_ids)==1 else "WD model files"
            )
            effect="Removed models stop producing new AI Tags; existing image evidence is preserved."
        size=sum(self._directory_size(path) for path in paths)
        return DependencyRemovalPlan(
            capability,label,tuple(paths),(),(),size,effect,tuple(selected_ids),
        )

    def runtime_removal_plan(self,capability: str) -> DependencyRemovalPlan:
        """Measure one removable shared capability runtime without model files."""
        if capability not in {"wd","joy"}:
            raise ValueError("Only optional AI capability runtimes can be removed.")
        persistent=self._persistent_profile_removal_packages(capability)
        candidates=persistent or (
            self.WD_REMOVABLE_DISTRIBUTIONS
            if capability=="wd" else self.JOY_REMOVABLE_DISTRIBUTIONS
        )
        installed=tuple(
            name for name in candidates if self._distribution_version(name)
        )
        size=sum(self._distribution_size(name) for name in installed)
        label=(
            "Shared AI Tag runtime" if capability=="wd" else
            "Shared AI Description runtime"
        )
        effect=(
            "Installed AI Tag models remain on disk, but new AI Tags stay "
            "disabled until the shared runtime is installed again."
            if capability=="wd" else
            "Installed AI Description models remain on disk, but new AI "
            "Descriptions stay disabled until the shared runtime is installed again."
        )
        return DependencyRemovalPlan(
            capability,label,(),installed,(
                "All installed model files",
                "Pillow (core image support)",
                "Packages shared by another installed optional profile",
            ),size,effect,runtime_only=True,
        )

    @classmethod
    def uninstall_packages(cls,distributions: tuple[str,...]) -> None:
        protected={cls._normalize_distribution(name) for name in cls.CORE_DISTRIBUTIONS}
        requested={cls._normalize_distribution(name) for name in distributions}
        if requested & protected: raise RuntimeError("A core application package cannot be removed.")
        if not distributions: return
        try:
            subprocess.run(
                [sys.executable,"-m","pip","uninstall","--yes",*distributions],
                check=True,capture_output=True,text=True,**_NO_CONSOLE,
            )
        except subprocess.CalledProcessError as exc:
            detail=(exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Capability package removal failed:\n\n{detail}") from exc
        importlib.invalidate_caches()

    def remove(self,plan: DependencyRemovalPlan) -> DependencyRemovalResult:
        if plan.capability not in {"wd","joy"}: raise ValueError("The removal plan is not an optional AI capability.")
        model_root=Path(self.settings.ai_models_directory).resolve(strict=False)
        allowed_paths=(
            {
                self.plain_language_model_dir_for(model_id).resolve(strict=False)
                for model_id in PLAIN_LANGUAGE_MODELS
            } if plan.capability=="joy" else
            {self.wd_model_dir_for(model_id).resolve(strict=False) for model_id in WD_TAGGER_MODELS}
        )
        persistent_allowed=self._persistent_profile_removal_packages(plan.capability)
        allowed_distributions=persistent_allowed or (
            self.JOY_REMOVABLE_DISTRIBUTIONS if plan.capability=="joy" else self.WD_REMOVABLE_DISTRIBUTIONS
        )
        if any(self._normalize_distribution(name) not in {
            self._normalize_distribution(value) for value in allowed_distributions
        } for name in plan.distributions):
            raise RuntimeError("The removal plan includes a package that is not exclusively owned by this capability.")
        quarantined: list[tuple[Path,Path]]=[]
        original_settings={
            "suggest_wd_for_missing_tags":self.settings.suggest_wd_for_missing_tags,
            "auto_run_wd_on_managed_images":self.settings.auto_run_wd_on_managed_images,
            "enabled_wd_taggers":list(self.settings.enabled_wd_taggers),
            "suggest_joycaption_for_missing_descriptions":self.settings.suggest_joycaption_for_missing_descriptions,
            "auto_run_joy_on_managed_images":self.settings.auto_run_joy_on_managed_images,
        }
        for path in plan.model_paths:
            resolved=path.resolve(strict=False)
            if resolved not in allowed_paths or resolved==model_root or model_root not in resolved.parents:
                raise RuntimeError(f"Refusing to remove an unrecognized model folder: {path}")
        try:
            if plan.runtime_only:
                if plan.capability=="joy":
                    self.settings.suggest_joycaption_for_missing_descriptions=False
                    self.settings.auto_run_joy_on_managed_images=False
                else:
                    self.settings.suggest_wd_for_missing_tags=False
                    self.settings.auto_run_wd_on_managed_images=False
            elif plan.capability=="joy":
                removed=set(plan.model_ids)
                if self.settings.plain_language_model in removed:
                    self.settings.suggest_joycaption_for_missing_descriptions=False
                    self.settings.auto_run_joy_on_managed_images=False
            else:
                removed=set(plan.model_ids)
                self.settings.enabled_wd_taggers=[value for value in self.settings.enabled_wd_taggers if value not in removed]
                remaining=[
                    value for value in self.settings.enabled_wd_taggers
                    if value in WD_TAGGER_MODELS and (self.wd_model_dir_for(value)/"model.onnx").is_file()
                ]
                if not remaining:
                    self.settings.suggest_wd_for_missing_tags=False
                    self.settings.auto_run_wd_on_managed_images=False
            self.settings.save()
            for path in plan.model_paths:
                if not path.exists() and not path.is_symlink(): continue
                quarantine=path.with_name(path.name+f".tagmanager-remove-{uuid.uuid4().hex}")
                path.rename(quarantine); quarantined.append((path,quarantine))
            if plan.distributions and not self._remove_persistent_profile(plan.capability):
                self.uninstall_packages(plan.distributions)
        except Exception:
            for original,quarantine in reversed(quarantined):
                if quarantine.exists() or quarantine.is_symlink(): quarantine.rename(original)
            for name,value in original_settings.items(): setattr(self.settings,name,value)
            self.settings.save()
            raise
        warnings=[]
        for original,quarantine in quarantined:
            try:
                if quarantine.is_symlink(): quarantine.unlink()
                else: shutil.rmtree(quarantine)
            except OSError as exc:
                try:
                    if not original.exists() and not original.is_symlink(): quarantine.rename(original)
                except OSError:
                    warnings.append(f"Cleanup remains at {quarantine}: {exc}")
                else:
                    warnings.append(f"Model cleanup could not finish, so the files were restored at {original}: {exc}")
        self._component_cache=None
        return DependencyRemovalResult(plan.label,plan.size_bytes,plan.distributions,tuple(warnings))

    @classmethod
    def _write_installed_component_inventory(
        cls,profile_ids: tuple[str,...],profiles: dict[str,dict],*,
        component_site: Path,target_root: Path,
    ) -> None:
        distributions=[]
        inventory_root=cls._safe_component_directory(component_site)
        verified_files=cls._verified_component_files(component_site)
        for distribution in metadata.distributions(path=[str(component_site)]):
            values=distribution.metadata; name=str(values.get("Name") or "")
            if not name: continue
            classifiers=[
                value for value in (values.get_all("Classifier") or [])
                if value.startswith("License ::")
            ]
            license_files={}
            for relative in distribution.files or ():
                if not any(token in Path(str(relative)).name.casefold() for token in ("license","copying","notice")):
                    continue
                try:
                    absolute=Path(distribution.locate_file(relative)).resolve()
                    path=absolute.relative_to(inventory_root).as_posix()
                    license_files[path]={
                        "path":path,"size_bytes":absolute.stat().st_size,
                        "sha256":hashlib.sha256(absolute.read_bytes()).hexdigest(),
                    }
                except (OSError,ValueError):
                    continue
            distributions.append({
                "name":name,"version":distribution.version,
                "license_expression":values.get("License-Expression") or "",
                "license":values.get("License") or "",
                "license_classifiers":classifiers,
                "license_files":[license_files[path] for path in sorted(license_files,key=str.casefold)],
            })
        profile_records=[]
        requested=[]
        for profile_id in profile_ids:
            profile=profiles[profile_id]
            roots=[str(value) for value in profile.get("roots",[])]
            requested.extend(roots)
            profile_records.append({
                "id":profile_id,
                "display_name":str(profile.get("display_name") or profile_id),
                "sha256":cls._profile_sha256(profile),
                "roots":roots,
                "locked_requirements":[
                    f"{value['name']}=={value['version']} --hash=sha256:{value['sha256']}"
                    for value in profile.get("packages",[])
                ],
            })
        payload={
            "schema_version":2,
            "storage_layout":"durable-optional-python-v1",
            "verified_install_files":verified_files,
            "python_version":sys.version.split()[0],
            "requested_requirements":list(dict.fromkeys(str(value) for value in requested)),
            "profile_ids":list(profile_ids),
            "profiles":profile_records,
            "installed_distributions":sorted(distributions,key=lambda value:cls._normalize_distribution(value["name"])),
        }
        target_root.mkdir(parents=True,exist_ok=True)
        target=target_root/"installed-components.json"
        temporary=target.with_name(target.name+".tmp")
        temporary.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
        temporary.replace(target)

    @classmethod
    def install_packages(cls,packages: list[str]) -> str:
        if not packages:
            return "All selected Python packages are already installed."
        try:
            subprocess.run([sys.executable,"-m","pip","install",*packages],check=True,capture_output=True,text=True,**_NO_CONSOLE)
        except subprocess.CalledProcessError as exc:
            detail=(exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"Package installation failed:\n\n{detail}") from exc
        importlib.invalidate_caches()
        return "Installed: " + ", ".join(packages)

    @classmethod
    def _optional_acquisition_groups(cls,packages: list[dict]) -> dict[str,list[dict]]:
        groups: dict[str,list[dict]]={}
        for package in packages:
            source=str(package.get("source_index") or "")
            if source not in cls.OPTIONAL_SOURCE_INDEXES:
                raise RuntimeError("The optional package source is outside the finite official-source policy.")
            name=str(package.get("name") or "")
            version=str(package.get("version") or "")
            digest=str(package.get("sha256") or "").casefold()
            if (not name or not all(c.isalnum() or c in "-_." for c in name)
                or not version or not all(c.isalnum() or c in ".+_-" for c in version)
                or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest)):
                raise RuntimeError("The optional package lock contains an invalid package record.")
            if cls._normalize_distribution(name)=="av" and source!="https://pypi.org/simple":
                raise RuntimeError("Video packages must use the official PyPI source.")
            groups.setdefault(source,[]).append(package)
        return groups

    @classmethod
    def _acquire_optional_packages(cls,packages: list[dict],site: Path,stage: Path) -> None:
        # Disable global/user/site configuration as well as inherited PIP_* options.
        # Separate invocations prevent a retained AI extra index from sourcing Video.
        groups=cls._optional_acquisition_groups(packages)
        environment={key:value for key,value in os.environ.items()
                     if not key.upper().startswith("PIP_")}
        environment["PIP_CONFIG_FILE"]=os.devnull
        for index,(source,locked) in enumerate(sorted(groups.items())):
            lock=stage/f"missing-{index}.lock"
            lock.write_text(
                "\n".join(f"{p['name']}=={p['version']} --hash=sha256:{p['sha256']}"
                          for p in locked)+"\n",encoding="utf-8",
            )
            command=[
                sys.executable,"-I","-m","pip","--isolated","install",
                "--disable-pip-version-check","--no-cache-dir","--require-hashes",
                "--only-binary=:all:","--no-deps","--index-url",source,
                "--target",str(site),"--requirement",str(lock),
            ]
            try:
                subprocess.run(command,check=True,capture_output=True,text=True,
                               env=environment,**_NO_CONSOLE)
            except subprocess.CalledProcessError as exc:
                detail=(exc.stderr or exc.stdout or str(exc)).strip()
                raise RuntimeError(f"Reviewed profile installation failed:\n\n{detail}") from exc
            finally:
                lock.unlink(missing_ok=True)

    @classmethod
    def _verified_video_reuse_site(cls,profiles: dict[str,dict]) -> Path | None:
        """Legacy AI readability never qualifies unrelated Video bytes for reuse."""
        profile=profiles.get(cls.VIDEO_PROFILE_ID)
        if not isinstance(profile,dict):
            return None
        payload=cls._read_optional_inventory()
        if (cls.VIDEO_PROFILE_ID not in payload.get("profile_ids",[])
            or not isinstance(payload.get("verified_install_files"),dict)
            or not payload["verified_install_files"]):
            return None
        recorded=next((value for value in payload.get("profiles",[])
                       if isinstance(value,dict) and value.get("id")==cls.VIDEO_PROFILE_ID),{})
        if (recorded.get("sha256")!=cls._profile_sha256(profile)
            or cls._optional_environment_issues(OPTIONAL_COMPONENT_ROOT)):
            return None
        return cls._safe_component_directory(_optional_component_site())

    def _stage_optional_profiles(
        self,profile_ids: tuple[str,...],*,allow_download: bool,progress=None,
    ) -> Path:
        if progress is not None:
            progress(1,0,"Checking retained local AI packages…")
        self._recover_interrupted_optional_install()
        profiles,packages=self._profile_union(profile_ids)
        root=OPTIONAL_COMPONENT_ROOT
        self._safe_component_directory(root)
        root.parent.mkdir(parents=True,exist_ok=True)
        stage=root.parent/f".{root.name}-install-{uuid.uuid4().hex}"
        component_site=stage/"python"
        component_site.mkdir(parents=True)
        try:
            installed: dict[str,list[metadata.Distribution]]={}
            for trusted_root in self._trusted_optional_distribution_roots(profiles):
                for key,values in self._distribution_map(trusted_root).items():
                    installed.setdefault(key,[]).extend(values)
            verified_video_site=self._verified_video_reuse_site(profiles)
            reusable: list[metadata.Distribution]=[]
            missing=[]
            for package in packages:
                candidates=installed.get(self._normalize_distribution(package["name"]),[])
                distribution=next(
                    (
                        value for value in candidates
                        if str(value.version)==str(package["version"])
                        and (
                            self._normalize_distribution(package["name"])!="av"
                            or (verified_video_site is not None
                                and Path(value.locate_file("")).absolute()==verified_video_site)
                        )
                        and self._distribution_record_valid(value)
                    ),
                    None,
                )
                if distribution is None:
                    missing.append(package)
                else:
                    reusable.append(distribution)
            if missing and not allow_download:
                names=", ".join(str(value["name"]) for value in missing)
                raise RuntimeError(
                    "The retained optional profile cannot be reconstructed from verified local files: "+names
                )
            if missing:
                if progress is not None:
                    progress(
                        1,0,
                        f"Acquiring {len(missing):,} missing reviewed AI package"
                        f"{'' if len(missing)==1 else 's'}…",
                    )
                self._acquire_optional_packages(missing,component_site,stage)
            if reusable and progress is not None:
                progress(1,0,"Rebuilding the combined AI environment from verified local packages…")
            for distribution in reusable:
                self._copy_distribution_to_site(distribution,component_site)
            if progress is not None:
                progress(1,0,"Recording the combined AI package inventory…")
            for distribution in metadata.distributions(path=[str(component_site)]):
                self._normalize_staged_distribution_record(distribution,component_site)
            for profile_id,profile in profiles.items():
                issues=self._profile_issues_at_path(profile,component_site)
                if issues:
                    raise RuntimeError(
                        f"Reviewed {profile_id} profile verification failed:\n\n"+"\n".join(issues)
                    )
            self._write_installed_component_inventory(
                profile_ids,profiles,component_site=component_site,target_root=stage,
            )
            if progress is not None:
                progress(1,0,"Verifying the combined AI environment before activation…")
            issues=self._optional_environment_issues(stage)
            if issues:
                raise RuntimeError(
                    "The staged optional-component inventory failed read-back verification:\n\n"
                    +"\n".join(issues)
                )
            return stage
        except Exception:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage,ignore_errors=True)
            raise

    @classmethod
    def _commit_optional_stage(cls,stage: Path) -> bool:
        root=OPTIONAL_COMPONENT_ROOT
        backup=root.with_name(root.name+".previous")
        cls._safe_component_directory(root)
        cls._safe_component_directory(backup)
        cls._safe_component_directory(stage)
        if backup.exists():
            raise RuntimeError("A previous optional-component recovery remains incomplete.")
        swapped=False
        try:
            if root.exists():
                root.rename(backup); swapped=True
            stage.rename(root)
        except Exception:
            if not root.exists() and swapped and backup.exists():
                backup.rename(root)
            raise
        cleanup_pending=False
        if backup.exists() and not backup.is_symlink():
            try:
                shutil.rmtree(backup)
            except OSError:
                cleanup_pending=backup.exists()
        _activate_optional_component_site()
        importlib.invalidate_caches()
        return cleanup_pending

    def install_profile(
        self,capability: str,*,progress=None,expected_plan: VideoInstallPlan | None=None,
    ) -> str:
        if capability=="video":
            if expected_plan is None:
                raise ValueError("Video installation requires the reviewed install plan.")
            try:
                current_plan=self.preview_video_install()
            except RuntimeError as exc:
                raise RuntimeError(f"The reviewed Video install plan is stale or inconsistent: {exc}") from exc
            if expected_plan!=current_plan:
                raise RuntimeError("The reviewed Video install plan is stale; review again.")
        profile_id,profile=self.optional_profile(capability)
        active=list(self._active_optional_profile_ids())
        if profile_id.startswith("wd-"):
            active=[value for value in active if not value.startswith("wd-")]
        if profile_id not in active:
            active.append(profile_id)
        ordered=tuple(sorted(dict.fromkeys(active)))
        stage=self._stage_optional_profiles(ordered,allow_download=True,progress=progress)
        if progress is not None:
            progress(1,0,"Activating the verified combined AI environment…")
        self.optional_cleanup_pending=self._commit_optional_stage(stage)
        self._component_cache=None
        return f"Installed reviewed profile: {profile.get('display_name') or profile_id}"

    def _remove_persistent_profile(self,capability: str) -> bool:
        payload=self._read_optional_inventory()
        if payload.get("schema_version")!=2 or not OPTIONAL_COMPONENT_ROOT.is_dir():
            return False
        active=tuple(
            value for value in payload.get("profile_ids",[])
            if isinstance(value,str)
        )
        if capability=="wd":
            retained=tuple(value for value in active if not value.startswith("wd-"))
        else:
            retained=tuple(value for value in active if value!="joycaption")
        if retained==active:
            return False
        stage=self._stage_optional_profiles(retained,allow_download=False)
        self.optional_cleanup_pending=self._commit_optional_stage(stage)
        return True

    def setup_wd(
        self, packages: list[str] | None = None, *, download_model: bool = False,
        model_ids: list[str] | None = None,progress=None,
    ) -> str:
        if packages is None:
            packages=self.missing_packages(self.WD_PACKAGES)
            if packages:
                if progress is None:
                    self.install_profile("wd")
                else:
                    self.install_profile("wd",progress=progress)
        elif packages:
            if progress is not None:
                progress(1,0,"Installing the selected SDXL Tag packages…")
            self.install_packages(packages)
        selected = model_ids or [
            model_id for model_id in self.settings.enabled_wd_taggers if model_id in WD_TAGGER_MODELS
        ] or [WD_EVA02_MODEL]
        for model_id in selected:
            model_dir = self.wd_model_dir_for(model_id)
            ready = (model_dir / "model.onnx").is_file() and (model_dir / "selected_tags.csv").is_file()
            if download_model and not ready:
                if progress is not None:
                    progress(1,0,f"Downloading and verifying {WD_TAGGER_MODELS[model_id][0]}…")
                self._install_model_transactionally(
                    model_id,lambda stage:WDEva02Backend.download(stage,model_id),
                )
            if not (model_dir / "model.onnx").is_file() or not (model_dir / "selected_tags.csv").is_file():
                raise RuntimeError(f"{WD_TAGGER_MODELS[model_id][0]} did not produce all required model files.")
        return f"{len(selected):,} local WD tagger(s) are ready."

    def setup_joycaption(
        self, packages: list[str] | None = None, *, download_model: bool = False,
        model_id: str | None=None,progress=None,
    ) -> str:
        selected=str(
            model_id
            or getattr(self.settings,"plain_language_model",JOYCAPTION_MODEL)
            or JOYCAPTION_MODEL
        )
        if selected not in PLAIN_LANGUAGE_MODELS:
            raise ValueError("The Plain Language model identity is unsupported.")
        display=PLAIN_LANGUAGE_MODELS[selected][0]
        model_dir=self.plain_language_model_dir_for(selected)
        if packages is None:
            wanted=set(self.JOY_BASE_PACKAGES)
            if (
                selected==JOYCAPTION_MODEL
                and str(getattr(
                    self.settings,"joycaption_quantization","bf16"
                ))=="nf4"
            ):
                wanted.add("BitsAndBytes")
            packages=self.missing_packages(wanted)
            if packages:
                if progress is None:
                    self.install_profile("joy")
                else:
                    self.install_profile("joy",progress=progress)
        elif packages:
            if progress is not None:
                progress(1,0,"Installing the selected Plain Language packages…")
            self.install_packages(packages)
        if download_model and not (model_dir/"config.json").is_file():
            if progress is not None:
                progress(1,0,f"Downloading and verifying {display}…")
            installer=(
                JoyCaptionBackend.download
                if selected==JOYCAPTION_MODEL else Florence2Backend.download
            )
            self._install_model_transactionally(selected,installer)
        if not (model_dir/"config.json").is_file():
            raise RuntimeError(
                f"{display} setup did not produce its model configuration."
            )
        return f"{display} written descriptions are ready."

    def _install_model_transactionally(self, model_id: str, installer) -> Path:
        target=self._model_target(model_id); parent=target.parent
        parent.mkdir(parents=True,exist_ok=True)
        if not os.access(parent,os.R_OK | os.W_OK):
            raise RuntimeError(f"The AI model location is read-only: {parent}")
        stage=target.with_name(target.name+f".tagmanager-install-{uuid.uuid4().hex}")
        stage.mkdir()
        try:
            installer(stage)
            if not self._model_files_ready(stage,model_id):
                raise RuntimeError(f"{self._model_display_name(model_id)} did not produce all required model files.")
            _record_model_installation(stage,MODEL_ACQUISITIONS[model_id])
            self._commit_staged_model(stage,target,model_id)
        finally:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage,ignore_errors=True)
        self._component_cache=None
        return target

    def _commit_staged_model(self, stage: Path, target: Path, model_id: str) -> None:
        backup=target.with_name(target.name+".tagmanager-previous")
        if backup.exists() or backup.is_symlink():
            raise RuntimeError(f"A previous model recovery is still pending at {backup}.")
        had_existing=target.exists() or target.is_symlink()
        if had_existing:
            if target.is_symlink():
                raise RuntimeError("CollectiGen will not replace a linked model folder.")
            target.rename(backup)
        try:
            stage.rename(target)
            if not self._model_files_ready(target,model_id):
                raise RuntimeError("The installed model failed its required-file verification.")
        except Exception:
            if target.is_symlink(): target.unlink()
            elif target.exists(): shutil.rmtree(target,ignore_errors=True)
            if had_existing and backup.exists(): backup.rename(target)
            raise
        if backup.exists():
            try: shutil.rmtree(backup)
            except OSError: pass

    @staticmethod
    def _validated_pack_path(name: str, *, manifest_name: str) -> PurePosixPath:
        relative=PurePosixPath(name); parts=relative.parts
        invalid_part=any(
            not part or part.rstrip(" .")!=part or any(character in '<>:"|?*' for character in part)
            or part.split(".",1)[0].casefold() in WINDOWS_RESERVED_FILE_STEMS
            for part in parts
        )
        if (
            not name or not parts or relative.is_absolute() or relative.as_posix()!=name
            or ".." in parts or "\\" in name or name==manifest_name or invalid_part
        ):
            raise RuntimeError("The offline pack has an unsafe file path.")
        return relative

    @classmethod
    def _component_pack_sources(
        cls,
    ) -> tuple[tuple[tuple[str,dict],...],tuple[tuple[Path,str],...]]:
        root=OPTIONAL_COMPONENT_ROOT
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("No verified optional AI component environment is installed.")
        issues=cls._optional_environment_issues(root)
        if issues:
            raise RuntimeError(
                "The installed optional AI components cannot be exported until they pass verification:\n\n"
                +"\n".join(issues)
            )
        inventory=cls._read_optional_inventory(root)
        profile_ids=tuple(str(value) for value in inventory.get("profile_ids",[]) if isinstance(value,str))
        if not profile_ids:
            raise RuntimeError("No reviewed optional AI profile is installed.")
        profiles,packages=cls._profile_union(profile_ids)
        root_resolved=root.resolve(); site=root/"python"; installed=cls._distribution_map(site)
        sources: dict[str,Path]={"installed-components.json":root/"installed-components.json"}
        for package in packages:
            name=str(package["name"]); version=str(package["version"])
            distribution=next((
                value for value in installed.get(cls._normalize_distribution(name),[])
                if str(value.version)==version and cls._distribution_record_valid(value)
            ),None)
            if distribution is None:
                raise RuntimeError(f"{name} {version} is not a verified installed distribution.")
            for relative in distribution.files or ():
                source=Path(distribution.locate_file(relative)).resolve()
                try: portable=source.relative_to(root_resolved).as_posix()
                except ValueError as exc:
                    raise RuntimeError(f"An optional-component file escaped its managed root: {source}") from exc
                if source.is_symlink():
                    raise RuntimeError(f"Linked optional-component files cannot be exported: {portable}")
                if not source.is_file():
                    continue
                previous=sources.get(portable)
                if previous is not None and cls._sha256_file(previous)!=cls._sha256_file(source):
                    raise RuntimeError(f"Optional distributions disagree about shared file {portable}.")
                sources[portable]=source
        ordered=tuple((sources[name],name) for name in sorted(sources,key=str.casefold))
        return tuple((profile_id,profiles[profile_id]) for profile_id in profile_ids),ordered

    @classmethod
    def component_pack_available(cls) -> bool:
        try:
            return (
                OPTIONAL_COMPONENT_ROOT.is_dir()
                and bool(cls._active_optional_profile_ids())
            )
        except RuntimeError:
            return False

    @classmethod
    def _read_component_pack_manifest(cls,archive: Path) -> tuple[dict,tuple[dict,...]]:
        if not archive.is_file():
            raise FileNotFoundError("The selected offline component pack does not exist.")
        try:
            with zipfile.ZipFile(archive) as package:
                infos=package.infolist(); by_name={item.filename:item for item in infos}
                normalized={item.filename.casefold() for item in infos}
                if (
                    len(by_name)!=len(infos) or len(normalized)!=len(infos)
                    or COMPONENT_PACK_MANIFEST not in by_name
                ):
                    raise RuntimeError("The offline component pack has duplicate entries or no CollectiGen manifest.")
                manifest_info=by_name[COMPONENT_PACK_MANIFEST]
                if manifest_info.flag_bits & 1 or (manifest_info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("The offline component pack manifest is encrypted or linked.")
                if manifest_info.file_size>COMPONENT_PACK_MAX_MANIFEST_BYTES:
                    raise RuntimeError("The offline component pack manifest is unreasonably large.")
                payload=json.loads(package.read(COMPONENT_PACK_MANIFEST).decode("utf-8"))
        except (OSError,zipfile.BadZipFile,KeyError,UnicodeError,json.JSONDecodeError) as exc:
            raise RuntimeError("The offline component pack is unreadable or damaged.") from exc
        if (
            not isinstance(payload,dict)
            or payload.get("schema_version")!=COMPONENT_PACK_SCHEMA_VERSION
            or payload.get("kind")!="collectigen-components-pack"
            or payload.get("complete") is not True
        ):
            raise RuntimeError("The offline component pack uses an unsupported format.")
        if str(payload.get("python_version") or "")!=sys.version.split()[0]:
            raise RuntimeError(
                "The offline component pack targets a different Python runtime "
                f"({payload.get('python_version') or 'missing'} != {sys.version.split()[0]})."
            )
        runtime_platform=f"{sys.implementation.name}-{sysconfig.get_platform()}"
        if str(payload.get("runtime_platform") or "")!=runtime_platform:
            raise RuntimeError(
                "The offline component pack targets a different Python platform "
                f"({payload.get('runtime_platform') or 'missing'} != {runtime_platform})."
            )
        raw_profiles=payload.get("profiles")
        if not isinstance(raw_profiles,list) or not raw_profiles:
            raise RuntimeError("The offline component pack has no reviewed profile identity.")
        contract=cls._optional_profile_contract(validate_runtime=False); available=contract.get("profiles",{})
        profile_ids=[]; display_names=[]
        for raw in raw_profiles:
            if not isinstance(raw,dict):
                raise RuntimeError("The offline component pack has an invalid profile record.")
            profile_id=str(raw.get("id") or ""); profile=available.get(profile_id)
            if (
                not isinstance(profile,dict) or profile.get("support")!="supported"
                or str(raw.get("sha256") or "")!=cls._profile_sha256(profile)
                or profile_id in profile_ids
            ):
                raise RuntimeError("The offline component pack profile is not the exact reviewed profile.")
            profile_ids.append(profile_id)
            display_names.append(str(profile.get("display_name") or profile_id))
        raw_files=payload.get("files")
        if not isinstance(raw_files,list) or not raw_files or len(raw_files)>COMPONENT_PACK_MAX_FILES:
            raise RuntimeError("The offline component pack has no bounded file inventory.")
        records=[]; names=set(); normalized_names=set(); total=0
        for raw in raw_files:
            if not isinstance(raw,dict):
                raise RuntimeError("The offline component pack has an invalid file record.")
            name=str(raw.get("path") or ""); cls._validated_pack_path(name,manifest_name=COMPONENT_PACK_MANIFEST)
            digest=str(raw.get("sha256") or "").casefold(); size=raw.get("size_bytes"); normalized=name.casefold()
            if (
                name in names or normalized in normalized_names or type(size) is not int or size<0
                or len(digest)!=64 or any(value not in "0123456789abcdef" for value in digest)
            ):
                raise RuntimeError("The offline component pack has an unsafe or invalid file inventory.")
            total+=size
            if total>COMPONENT_PACK_MAX_UNPACKED_BYTES:
                raise RuntimeError("The offline component pack exceeds the unpacked-size safety limit.")
            names.add(name); normalized_names.add(normalized)
            records.append({"path":name,"size_bytes":size,"sha256":digest})
        if "installed-components.json" not in names:
            raise RuntimeError("The offline component pack is missing its installed-component inventory.")
        with zipfile.ZipFile(archive) as package:
            actual={item.filename for item in package.infolist() if not item.is_dir()}
            if actual!={COMPONENT_PACK_MANIFEST,*names}:
                raise RuntimeError("The offline component pack contains unlisted or missing files.")
            for record in records:
                info=package.getinfo(record["path"])
                if info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("The offline component pack contains an encrypted or linked file.")
                if info.file_size!=record["size_bytes"]:
                    raise RuntimeError("The offline component pack file sizes do not match its manifest.")
        payload["_profile_ids"]=profile_ids; payload["_display_names"]=display_names
        return payload,tuple(records)

    @classmethod
    def inspect_component_pack(cls,path: str | Path) -> ComponentPackInfo:
        payload,records=cls._read_component_pack_manifest(Path(path))
        return ComponentPackInfo(
            tuple(payload["_profile_ids"]),tuple(payload["_display_names"]),
            str(payload["python_version"]),str(payload["runtime_platform"]),
            sum(value["size_bytes"] for value in records),len(records),
        )

    def import_component_pack(self,path: str | Path) -> str:
        archive=Path(path); payload,records=self._read_component_pack_manifest(archive)
        contract=self._optional_profile_contract(validate_runtime=False)
        for profile_id in payload["_profile_ids"]:
            profile=contract["profiles"][profile_id]
            if profile.get("hardware",{}).get("requires_nvidia_gpu") and not shutil.which("nvidia-smi"):
                raise RuntimeError(
                    f"{profile.get('display_name') or profile_id} requires a supported NVIDIA GPU on this computer."
                )
        root=OPTIONAL_COMPONENT_ROOT; parent=root.parent
        if root.is_symlink() or parent.is_symlink():
            raise RuntimeError("The optional-component storage location cannot be a linked folder.")
        parent.mkdir(parents=True,exist_ok=True)
        needed=sum(value["size_bytes"] for value in records)+COMPONENT_PACK_FREE_SPACE_RESERVE
        try: available=int(shutil.disk_usage(parent).free)
        except OSError as exc:
            raise RuntimeError("Free space at the optional-component location could not be checked.") from exc
        if available<needed:
            raise RuntimeError(
                f"The offline component pack needs {self.format_size(needed)} free at {parent}; "
                f"only {self.format_size(available)} is available. Existing components were preserved."
            )
        self._recover_interrupted_optional_install()
        stage=parent/f".{root.name}-install-{uuid.uuid4().hex}"; stage.mkdir()
        try:
            with zipfile.ZipFile(archive) as package:
                for record in records:
                    destination=stage.joinpath(*PurePosixPath(record["path"]).parts)
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    digest=hashlib.sha256()
                    with package.open(record["path"]) as source,destination.open("wb") as output:
                        while block:=source.read(1024*1024):
                            output.write(block); digest.update(block)
                    if digest.hexdigest()!=record["sha256"]:
                        raise RuntimeError(
                            f"Offline component verification failed for {record['path']}; existing components were preserved."
                        )
            issues=self._optional_environment_issues(stage)
            if issues:
                raise RuntimeError(
                    "The offline component pack failed installed-environment verification; existing components were preserved:\n\n"
                    +"\n".join(issues)
                )
            self.optional_cleanup_pending=self._commit_optional_stage(stage)
        finally:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage,ignore_errors=True)
        self._component_cache=None
        return "Imported and verified optional AI components for offline use: "+", ".join(payload["_display_names"])

    def export_component_pack(self,path: str | Path) -> str:
        profiles,sources=self._component_pack_sources(); root=OPTIONAL_COMPONENT_ROOT.resolve()
        destination=Path(path)
        try: resolved_destination=destination.resolve(strict=False)
        except OSError as exc:
            raise RuntimeError("The component-pack destination could not be resolved safely.") from exc
        if resolved_destination==root or root in resolved_destination.parents:
            raise RuntimeError("Choose an export destination outside the installed optional-component folder.")
        records=[]; total=0
        for source,name in sources:
            size=source.stat().st_size; total+=size
            if total>COMPONENT_PACK_MAX_UNPACKED_BYTES:
                raise RuntimeError("The installed optional AI environment exceeds the component-pack size limit.")
            records.append({"path":name,"size_bytes":size,"sha256":self._sha256_file(source)})
        destination.parent.mkdir(parents=True,exist_ok=True)
        try: available=int(shutil.disk_usage(destination.parent).free)
        except OSError as exc:
            raise RuntimeError("Free space at the component-pack destination could not be checked.") from exc
        if available<total+COMPONENT_PACK_FREE_SPACE_RESERVE:
            raise RuntimeError("The component-pack destination does not have enough free space for a safe export.")
        payload={
            "schema_version":COMPONENT_PACK_SCHEMA_VERSION,
            "kind":"collectigen-components-pack",
            "complete":True,
            "python_version":sys.version.split()[0],
            "runtime_platform":f"{sys.implementation.name}-{sysconfig.get_platform()}",
            "profiles":[{
                "id":profile_id,
                "display_name":str(profile.get("display_name") or profile_id),
                "sha256":self._profile_sha256(profile),
            } for profile_id,profile in profiles],
            "files":records,
        }
        temporary=destination.with_name(destination.name+f".tmp-{uuid.uuid4().hex}")
        try:
            with zipfile.ZipFile(temporary,"w",compression=zipfile.ZIP_DEFLATED,allowZip64=True) as package:
                package.writestr(COMPONENT_PACK_MANIFEST,json.dumps(payload,indent=2,sort_keys=True)+"\n")
                for source,name in sources:
                    package.write(source,name)
            verified,verified_records=self._read_component_pack_manifest(temporary)
            if tuple(verified["_profile_ids"])!=tuple(value[0] for value in profiles):
                raise RuntimeError("The exported component-pack profile identity changed during verification.")
            with zipfile.ZipFile(temporary) as package:
                for record in verified_records:
                    digest=hashlib.sha256()
                    with package.open(record["path"]) as source:
                        while block:=source.read(1024*1024): digest.update(block)
                    if digest.hexdigest()!=record["sha256"]:
                        raise RuntimeError(f"Export verification failed for {record['path']}.")
            temporary.replace(destination)
        finally:
            if temporary.exists(): temporary.unlink()
        return "Exported verified optional AI components for offline use: "+", ".join(
            str(profile.get("display_name") or profile_id) for profile_id,profile in profiles
        )

    @classmethod
    def _read_model_pack_manifest(cls, archive: Path) -> tuple[dict,tuple[dict,...]]:
        if not archive.is_file(): raise FileNotFoundError("The selected offline model pack does not exist.")
        try:
            with zipfile.ZipFile(archive) as package:
                infos=package.infolist(); by_name={item.filename:item for item in infos}
                if len(by_name)!=len(infos) or MODEL_PACK_MANIFEST not in by_name:
                    raise RuntimeError("The offline model pack has a duplicate entry or no CollectiGen manifest.")
                manifest_info=by_name[MODEL_PACK_MANIFEST]
                if manifest_info.flag_bits & 1 or (manifest_info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("The offline model pack manifest is encrypted or linked.")
                if manifest_info.file_size>MODEL_PACK_MAX_MANIFEST_BYTES:
                    raise RuntimeError("The offline model pack manifest is unreasonably large.")
                payload=json.loads(package.read(MODEL_PACK_MANIFEST).decode("utf-8"))
        except (OSError,zipfile.BadZipFile,KeyError,UnicodeError,json.JSONDecodeError) as exc:
            raise RuntimeError("The offline model pack is unreadable or damaged.") from exc
        if not isinstance(payload,dict) or payload.get("schema_version")!=MODEL_PACK_SCHEMA_VERSION or payload.get("kind")!="collectigen-model-pack":
            raise RuntimeError("The offline model pack uses an unsupported format.")
        model_id=str(payload.get("model_id") or "")
        if model_id not in MODEL_ACQUISITIONS:
            raise RuntimeError("The offline model pack names an unsupported model.")
        acquisition=MODEL_ACQUISITIONS[model_id]
        if str(payload.get("revision") or "")!=acquisition.revision:
            raise RuntimeError("The offline model pack revision is not the exact reviewed revision.")
        if (
            payload.get("complete") is not True
            or str(payload.get("license") or "")!=acquisition.license_name
            or str(payload.get("license_url") or "")!=acquisition.license_url
            or str(payload.get("source_url") or "")!=acquisition.source_url
        ):
            raise RuntimeError("The offline model pack identity, terms, or completion record does not match the reviewed model.")
        raw_files=payload.get("files")
        if not isinstance(raw_files,list) or not raw_files or len(raw_files)>MODEL_PACK_MAX_FILES:
            raise RuntimeError("The offline model pack has no verified file inventory.")
        records=[]; names=set(); normalized_names=set()
        for raw in raw_files:
            if not isinstance(raw,dict): raise RuntimeError("The offline model pack has an invalid file record.")
            name=str(raw.get("path") or ""); relative=PurePosixPath(name)
            digest=str(raw.get("sha256") or "").casefold(); size=raw.get("size_bytes")
            parts=relative.parts
            invalid_part=any(
                not part or part.rstrip(" .")!=part or any(character in '<>:"|?*' for character in part)
                or part.split(".",1)[0].casefold() in WINDOWS_RESERVED_FILE_STEMS
                for part in parts
            )
            normalized=name.casefold()
            if (
                not name or relative.is_absolute() or relative.as_posix()!=name or ".." in parts or "\\" in name
                or name==MODEL_PACK_MANIFEST or name in names or type(size) is not int or size<0
                or normalized in normalized_names or invalid_part
                or len(digest)!=64 or any(value not in "0123456789abcdef" for value in digest)
            ):
                raise RuntimeError("The offline model pack has an unsafe or invalid file inventory.")
            names.add(name); normalized_names.add(normalized)
            records.append({"path":name,"size_bytes":size,"sha256":digest})
        with zipfile.ZipFile(archive) as package:
            actual={item.filename for item in package.infolist() if not item.is_dir()}
            if actual!={MODEL_PACK_MANIFEST,*names}:
                raise RuntimeError("The offline model pack contains unlisted or missing files.")
            for record in records:
                info=package.getinfo(record["path"])
                if info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("The offline model pack contains an encrypted or linked file.")
                if info.file_size!=record["size_bytes"]:
                    raise RuntimeError("The offline model pack file sizes do not match its manifest.")
        if not set(cls._required_model_files(model_id)).issubset(names):
            raise RuntimeError("The offline model pack is missing required model files.")
        return payload,tuple(records)

    @classmethod
    def inspect_model_pack(cls, path: str | Path) -> ModelPackInfo:
        archive=Path(path); payload,records=cls._read_model_pack_manifest(archive)
        model_id=str(payload["model_id"]); acquisition=MODEL_ACQUISITIONS[model_id]
        return ModelPackInfo(
            model_id,cls._model_display_name(model_id),acquisition.revision,
            acquisition.license_name,acquisition.license_url,acquisition.source_url,
            sum(item["size_bytes"] for item in records),len(records),
        )

    @classmethod
    def _verify_model_pack_content(cls, archive: Path, records: tuple[dict,...]) -> None:
        with zipfile.ZipFile(archive) as package:
            for record in records:
                digest=hashlib.sha256()
                with package.open(record["path"]) as source:
                    while block:=source.read(1024*1024): digest.update(block)
                if digest.hexdigest()!=record["sha256"]:
                    raise RuntimeError(f"Offline model verification failed for {record['path']}.")

    def import_model_pack(self, path: str | Path) -> str:
        archive=Path(path); payload,records=self._read_model_pack_manifest(archive)
        model_id=str(payload["model_id"]); target=self._model_target(model_id); parent=target.parent
        parent.mkdir(parents=True,exist_ok=True)
        if not os.access(parent,os.R_OK | os.W_OK):
            raise RuntimeError(f"The AI model location is read-only: {parent}")
        needed=sum(item["size_bytes"] for item in records)+MODEL_PACK_FREE_SPACE_RESERVE
        try: available=int(shutil.disk_usage(parent).free)
        except OSError as exc: raise RuntimeError("Free space at the AI model location could not be checked.") from exc
        if available<needed:
            raise RuntimeError(
                f"The offline model pack needs {self.format_size(needed)} free at {parent}; "
                f"only {self.format_size(available)} is available. The existing model was preserved."
            )
        stage=target.with_name(target.name+f".tagmanager-install-{uuid.uuid4().hex}"); stage.mkdir()
        try:
            with zipfile.ZipFile(archive) as package:
                for record in records:
                    destination=stage.joinpath(*PurePosixPath(record["path"]).parts)
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    digest=hashlib.sha256()
                    with package.open(record["path"]) as source,destination.open("wb") as output:
                        while block:=source.read(1024*1024):
                            output.write(block); digest.update(block)
                    if digest.hexdigest()!=record["sha256"]:
                        raise RuntimeError(f"Offline model verification failed for {record['path']}; the existing model was preserved.")
            if not self._model_files_ready(stage,model_id):
                raise RuntimeError("The offline model pack did not produce all required model files.")
            _record_model_installation(stage,MODEL_ACQUISITIONS[model_id])
            self._commit_staged_model(stage,target,model_id)
        finally:
            if stage.exists() and not stage.is_symlink(): shutil.rmtree(stage,ignore_errors=True)
        self._component_cache=None
        return f"Imported and verified {self._model_display_name(model_id)} for offline use."

    def export_model_pack(self, model_id: str, path: str | Path) -> str:
        source=self._model_target(model_id)
        if not self._model_files_ready(source,model_id):
            raise RuntimeError("The selected model is not completely installed.")
        if installed_model_revision(source,model_id)!=MODEL_ACQUISITIONS[model_id].revision:
            raise RuntimeError(
                "The selected model has no verified exact-revision record. Reinstall or import a verified model before exporting it."
            )
        destination=Path(path)
        try:
            resolved_destination=destination.resolve(strict=False); resolved_source=source.resolve(strict=True)
        except OSError as exc: raise RuntimeError("The model or export destination could not be resolved safely.") from exc
        if resolved_destination==resolved_source or resolved_source in resolved_destination.parents:
            raise RuntimeError("Choose an export destination outside the installed model folder.")
        candidates=[]
        for candidate in sorted(source.rglob("*"),key=lambda value:str(value).casefold()):
            if candidate.is_symlink(): raise RuntimeError("Linked files cannot be exported in a verified model pack.")
            if not candidate.is_file(): continue
            relative=candidate.relative_to(source).as_posix()
            if relative==MODEL_PACK_MANIFEST or {".cache",".git","__pycache__"} & set(Path(relative).parts): continue
            candidates.append((candidate,relative))
        acquisition=MODEL_ACQUISITIONS[model_id]
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_name(destination.name+f".tmp-{uuid.uuid4().hex}")
        try:
            with zipfile.ZipFile(temporary,"w",compression=zipfile.ZIP_DEFLATED,allowZip64=True) as package:
                files=[]
                for candidate,relative in candidates:
                    digest=hashlib.sha256(); size=0
                    with candidate.open("rb") as input_file,package.open(relative,"w",force_zip64=True) as output_file:
                        while block:=input_file.read(1024*1024):
                            output_file.write(block); digest.update(block); size+=len(block)
                    files.append({"path":relative,"size_bytes":size,"sha256":digest.hexdigest()})
                payload={
                    "schema_version":MODEL_PACK_SCHEMA_VERSION,"kind":"collectigen-model-pack",
                    "model_id":model_id,"revision":acquisition.revision,
                    "license":acquisition.license_name,"license_url":acquisition.license_url,
                    "source_url":acquisition.source_url,"complete":True,"files":files,
                }
                package.writestr(MODEL_PACK_MANIFEST,json.dumps(payload,indent=2,sort_keys=True)+"\n")
            _payload,records=self._read_model_pack_manifest(temporary)
            self._verify_model_pack_content(temporary,records)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return f"Exported verified {self._model_display_name(model_id)} pack to {destination}."

    def download_exiftool(self) -> str:
        PRIVATE_EXIFTOOL_DIR.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as temp_value:
            temp=Path(temp_value); archive=temp/"exiftool.zip"
            request=urllib.request.Request(
                EXIFTOOL_WINDOWS_URL,headers={"User-Agent":f"CollectiGen/{VERSION}"},
            )
            with urllib.request.urlopen(request,timeout=120) as response, archive.open("wb") as output:
                shutil.copyfileobj(response,output)
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest.casefold()!=EXIFTOOL_WINDOWS_SHA256.casefold():
                raise RuntimeError(
                    "ExifTool archive verification failed; the existing Metadata component was preserved."
                )
            extracted=temp/"extracted"
            with zipfile.ZipFile(archive) as package:
                root=extracted.resolve()
                for member in package.infolist():
                    target=(extracted/member.filename).resolve()
                    if target!=root and root not in target.parents:
                        raise RuntimeError("The ExifTool archive contains an unsafe path.")
                package.extractall(extracted)
            executable=next(extracted.rglob("exiftool(-k).exe"),None) or next(extracted.rglob("exiftool.exe"),None)
            if executable is None: raise RuntimeError("The official archive did not contain the expected executable.")
            staged=temp/"verified-exiftool"; shutil.copytree(executable.parent,staged)
            staged_executable=staged/executable.name; installed_in_stage=staged/"exiftool.exe"
            if staged_executable != installed_in_stage: staged_executable.rename(installed_in_stage)
            self._verify_exiftool(installed_in_stage)
            backup=PRIVATE_EXIFTOOL_DIR.with_name(PRIVATE_EXIFTOOL_DIR.name+".previous")
            if not PRIVATE_EXIFTOOL_DIR.exists() and backup.exists(): backup.rename(PRIVATE_EXIFTOOL_DIR)
            if backup.exists(): shutil.rmtree(backup)
            had_existing=PRIVATE_EXIFTOOL_DIR.exists()
            if had_existing: PRIVATE_EXIFTOOL_DIR.rename(backup)
            try:
                shutil.copytree(staged,PRIVATE_EXIFTOOL_DIR)
                self._verify_exiftool(PRIVATE_EXIFTOOL_DIR/"exiftool.exe")
            except Exception:
                if PRIVATE_EXIFTOOL_DIR.exists(): shutil.rmtree(PRIVATE_EXIFTOOL_DIR)
                if had_existing and backup.exists(): backup.rename(PRIVATE_EXIFTOOL_DIR)
                raise
            if backup.exists(): shutil.rmtree(backup)
            installed=PRIVATE_EXIFTOOL_DIR/"exiftool.exe"
        self.settings.exiftool_path=str(installed); self.settings.save()
        return f"ExifTool {EXIFTOOL_VERSION} is ready at {installed}."

    def choose_exiftool(self, path: str | Path) -> str:
        selected=Path(path)
        if not selected.is_file(): raise FileNotFoundError("The selected ExifTool executable does not exist.")
        version=self._verify_exiftool(selected)
        self.settings.exiftool_path=str(selected); self.settings.save()
        return f"Using ExifTool {version} at {selected}."

    @staticmethod
    def _verify_exiftool(path: Path) -> str:
        result=subprocess.run([str(path),"-ver"],check=True,capture_output=True,text=True,timeout=15,**_NO_CONSOLE)
        version=result.stdout.strip()
        if not version: raise RuntimeError("ExifTool did not report a version.")
        return version
