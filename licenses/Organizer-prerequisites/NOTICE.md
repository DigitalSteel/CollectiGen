# Native prerequisite checker support notices

OrganizerPrerequisites.exe is compiled project source with statically linked LLVM C++/compiler support and MinGW-w64 support. LLVM uses Apache-2.0 with LLVM exceptions; MinGW-w64 has component-specific notices and permissive/public-domain grants. The unmodified license texts in this directory apply to their respective support components, not all of CollectiGen. The compiler itself, its bundled Python/BusyBox and other development tools are not distributed in this Organizer package.

Windows KERNEL32, VERSION and UCRT API-set imports are system dependencies, not bundled Microsoft redistributable DLLs. The separately required VC-runtime installer and its ten checked DLLs remain Microsoft-provided and are not redistributed here. See WINDOWS_PREREQUISITES.md.
