"""Explicit Organizer runtime selection; manifests never select admission policy."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys


PROFILES=("collectigen-core","organizer-image")


class OrganizerRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSelection:
    capability_profile: str
    runtime_root: Path
    program_root: Path

    def __post_init__(self):
        if self.capability_profile not in PROFILES:
            raise ValueError("Unknown Organizer runtime capability profile.")
        for field in ("runtime_root","program_root"):
            path=Path(getattr(self,field)).absolute()
            if path.resolve()!=path:
                raise ValueError("Organizer runtime paths must not traverse redirected directories.")
            object.__setattr__(self,field,path)

    @property
    def interpreter(self):
        return self.runtime_root/"python/python.exe"

    def validate(self):
        from runtime_contract import load_expected_runtime_contract,validate_runtime
        from version import VERSION
        if Path(sys.executable).resolve()!=self.interpreter.resolve():
            raise OrganizerRuntimeError("Start Organizer with the explicitly selected private runtime interpreter.")
        lock=("requirements-organizer-image-runtime.lock" if self.capability_profile=="organizer-image"
              else "requirements-runtime.lock")
        expected=load_expected_runtime_contract(self.program_root/"tools/runtime-pins.json",
            self.program_root/lock,capability_profile=self.capability_profile)
        checked=validate_runtime(self.runtime_root/"python/collectigen-runtime.json",
            application_version=VERSION,runtime_root=self.runtime_root,verify_hashes=True,
            expected_contract=expected,capability_profile=self.capability_profile)
        if not checked.ready:
            raise OrganizerRuntimeError("The explicitly selected Organizer runtime is unavailable:\n"+
                "\n".join(checked.issues)+"\n"+self.repair_guidance())
        return checked

    def repair_command(self):
        # The caller selected policy/root before reading any runtime manifest.
        return ["powershell.exe","-NoProfile","-File",str(self.program_root/"tools/Repair-PortableRuntime.ps1"),
            "-CapabilityProfile",self.capability_profile,"-RuntimeRoot",str(self.runtime_root),
            "-Destination",str(self.runtime_root/"python")]

    def repair(self):
        return subprocess.run(self.repair_command(),check=False,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)).returncode

    def repair_guidance(self):
        return (f"Selected profile: {self.capability_profile}; runtime root: {self.runtime_root}.\n"
            "Explicit repair: "+subprocess.list2cmdline(self.repair_command()))

    def dependency_factory(self):
        if self.capability_profile=="collectigen-core":return None
        from functools import partial
        from core.dependencies import DependencyService
        return partial(DependencyService,capability_profile=self.capability_profile)
