// Minimal JSON parser for simple flat JSON objects used by the assignment.
// Supports string and integer values, no arrays or nesting required.
#pragma once

#include <string>
#include <unordered_map>
#include <fstream>
#include <cctype>

static inline std::string trim(const std::string &s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace((unsigned char)s[a])) ++a;
    while (b > a && std::isspace((unsigned char)s[b-1])) --b;
    return s.substr(a, b-a);
}

static inline std::unordered_map<std::string,std::string> parse_simple_json_file(const std::string &path) {
    std::unordered_map<std::string,std::string> out;
    std::ifstream f(path);
    if (!f.is_open()) return out;
    std::string s((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    size_t i = 0, n = s.size();
    auto skip_ws = [&]() {
        while (i < n && std::isspace((unsigned char)s[i])) ++i;
    };
    skip_ws();
    if (i < n && s[i] == '{') ++i;
    while (i < n) {
        skip_ws();
        if (i >= n || s[i] == '}') break;
        // parse key
        if (s[i] != '"') break;
        ++i;
        size_t start = i;
        while (i < n && s[i] != '"') ++i;
        std::string key = s.substr(start, i-start);
        ++i; // skip quote
        skip_ws();
        if (i < n && s[i] == ':') ++i;
        skip_ws();
        // parse value
        std::string value;
        if (i < n && s[i] == '"') {
            ++i; start = i;
            while (i < n && s[i] != '"') ++i;
            value = s.substr(start, i-start);
            ++i;
        } else {
            start = i;
            // number or token until comma/bracket
            while (i < n && s[i] != ',' && s[i] != '}') ++i;
            value = trim(s.substr(start, i-start));
        }
        out[key] = value;
        skip_ws();
        if (i < n && s[i] == ',') { ++i; continue; }
    }
    return out;

}
