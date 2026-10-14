#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winver.h>
#include <cstdio>
#include <cwchar>
#include <string>
#include <vector>
#include "prerequisite_contract.h"
#ifdef _MSC_VER
#pragma comment(lib, "version.lib")
#endif

#if !defined(_WIN64) || (!defined(_M_X64) && !defined(__x86_64__)) || defined(_M_ARM64EC) || defined(__arm64ec__)
#error This prerequisite checker must be built for Windows x64.
#endif

// Reads resource bytes; never loads a prerequisite DLL into this process.
static bool text_version(const std::wstring& path, Version& result) {
    DWORD ignored = 0;
    DWORD size = GetFileVersionInfoSizeW(path.c_str(), &ignored);
    if (!size) return false;
    std::vector<unsigned char> bytes(size);
    if (!GetFileVersionInfoW(path.c_str(), 0, size, bytes.data())) return false;
    struct Translation { WORD language; WORD codepage; };
    Translation* translations = nullptr;
    UINT count = 0;
    std::uint32_t first = 0x040904E4;
    if (VerQueryValueW(bytes.data(), L"\\VarFileInfo\\Translation",
        reinterpret_cast<void**>(&translations), &count)) {
        if (!translations || count < sizeof(Translation)) return false;
        first = (static_cast<std::uint32_t>(translations[0].language) << 16) | translations[0].codepage;
    }
    return translated_version(first, [&](std::uint32_t id) {
        wchar_t query[80];
        std::swprintf(query, sizeof(query) / sizeof(query[0]), L"\\StringFileInfo\\%08x\\FileVersion", static_cast<unsigned>(id));
        wchar_t* value = nullptr;
        UINT length = 0;
        if (VerQueryValueW(bytes.data(), query, reinterpret_cast<void**>(&value), &length)
            && value && length) {
            std::size_t actual = 0;
            while (actual < length && value[actual]) ++actual;
            return std::wstring(value, actual);
        }
        return std::wstring();
    }, result);
}

int wmain(int argc, wchar_t**) {
    if (argc != 1) {
        fwprintf(stderr, L"Media Organizer prerequisite checker accepts no arguments.\n");
        return 2;
    }
    wchar_t directory[MAX_PATH + 1] = {};
    UINT length = GetSystemDirectoryW(directory, MAX_PATH + 1);
    if (!length || length > MAX_PATH) {
        fwprintf(stderr, L"Cannot locate the Windows x64 system directory.\n");
        return 2;
    }
    bool ready = true;
    for (const wchar_t* name : required_files) {
        std::wstring path = std::wstring(directory) + L"\\" + name;
        DWORD attributes = GetFileAttributesW(path.c_str());
        Version version{};
        if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) {
            fwprintf(stderr, L"%ls is missing or unreadable.\n", name);
            ready = false;
        } else if (!text_version(path, version)) {
            fwprintf(stderr, L"%ls has no readable four-part FileVersion text.\n", name);
            ready = false;
        } else if (!at_least_minimum(version)) {
            fwprintf(stderr, L"%ls is older than 14.44.35211.0.\n", name);
            ready = false;
        }
    }
    if (!ready) {
        fwprintf(stderr, L"Read WINDOWS_PREREQUISITES.md for the official Microsoft Visual C++ x64 installation instructions. No repair was attempted.\n");
        return 1;
    }
    return 0;
}
