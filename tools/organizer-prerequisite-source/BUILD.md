# Organizer prerequisite checker source

This source builds the small Windows x86-64 console checker, not Media Organizer itself. It reads ten Microsoft VC-runtime file/version resources before Python starts. It neither loads those DLLs nor installs anything. Project source follows the root LICENSE.

Built with LLVM-MinGW 20261006 / LLVM 23.1.3, official asset SHA-256 317492c456aa27ee607a5919f1d2d38dcdc1112516a24d0bf4b00d078f52d17a. From an extracted project-local compiler, use its explicit x86_64-w64-mingw32-clang++.exe path:

```text
-std=c++17 -O2 -Wall -Wextra -Werror -DUNICODE -D_UNICODE -municode -static -static-libstdc++ organizer_prerequisites.cpp -lversion -o OrganizerPrerequisites.exe
```

The checker imports Windows KERNEL32/VERSION and UCRT API sets. Static C++/MinGW support notices are in licenses/Organizer-prerequisites. Compilation is not a claim of clean-machine or enterprise-policy admission. Retain component licenses when rebuilding/distributing. Exact source and output correspondence must be regenerated for a changed build.
