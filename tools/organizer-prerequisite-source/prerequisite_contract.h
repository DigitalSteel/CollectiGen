#pragma once
#include <cstddef>
#include <cstdint>
#include <climits>
#include <string>

struct Version { std::uint32_t parts[4]; };
static constexpr Version minimum_version{{14, 44, 35211, 0}};
static constexpr const wchar_t* required_files[] = {
    L"concrt140.dll", L"msvcp140.dll", L"msvcp140_1.dll", L"msvcp140_2.dll",
    L"msvcp140_codecvt_ids.dll", L"vcamp140.dll", L"vccorlib140.dll",
    L"vcomp140.dll", L"vcruntime140.dll", L"vcruntime140_1.dll"
};

static bool decimal_digit(wchar_t value) {
    WORD kind = 0;
    return GetStringTypeW(CT_CTYPE1, &value, 1, &kind) && (kind & C1_DIGIT);
}

// Select the first complete regex-shaped match before validating its values.
// Unicode decimal matches are selected but refused by ASCII numeric conversion;
// they must not be skipped to accept a later version.
static bool parse_version(const wchar_t* text, std::size_t length, Version& out) {
    for (std::size_t start = 0; start < length && text[start]; ++start) {
        if (!decimal_digit(text[start])) continue;
        std::size_t beginnings[4]{}, ends[4]{};
        std::size_t pos = start;
        bool matched = true;
        for (int part = 0; part < 4; ++part) {
            if (pos >= length || !decimal_digit(text[pos])) { matched = false; break; }
            beginnings[part] = pos;
            while (pos < length && decimal_digit(text[pos])) ++pos;
            ends[part] = pos;
            if (part < 3 && (pos >= length || text[pos++] != L'.')) { matched = false; break; }
        }
        if (!matched) continue;
        Version candidate{};
        for (int part = 0; part < 4; ++part) {
            std::uint32_t value = 0;
            for (std::size_t i = beginnings[part]; i < ends[part]; ++i) {
                if (text[i] < L'0' || text[i] > L'9') return false;
                unsigned digit = static_cast<unsigned>(text[i] - L'0');
                if (value > (static_cast<unsigned>(INT_MAX) - digit) / 10) return false;
                value = value * 10 + digit;
            }
            candidate.parts[part] = value;
        }
        out = candidate;
        return true;
    }
    return false;
}

template<class Lookup>
static bool translated_version(std::uint32_t first, Lookup lookup, Version& result) {
    const std::uint32_t choices[] = {first, 0x040904B0, 0x040904E4, 0x04090000};
    for (unsigned i = 0; i < 4; ++i) {
        if (i && choices[i] == first) continue;
        std::wstring text = lookup(choices[i]);
        if (!text.empty()) return parse_version(text.c_str(), text.size(), result);
    }
    return false;
}
static bool at_least_minimum(const Version& version) {
    for (int i = 0; i < 4; ++i) {
        if (version.parts[i] != minimum_version.parts[i])
            return version.parts[i] > minimum_version.parts[i];
    }
    return true;
}
