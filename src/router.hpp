#pragma once
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace routing {
using Bytes = std::vector<uint8_t>;
uint32_t ip(const std::string& value);
std::string ip_string(uint32_t value);
struct Prefix {
    uint32_t network;
    uint8_t length;
    static Prefix parse(const std::string& text);
    bool contains(uint32_t address) const;
    std::string str() const;
    auto operator<=>(const Prefix&) const = default;
};
struct Link {
    uint32_t peer;
    uint32_t cost;
    auto operator<=>(const Link&) const = default;
};
struct Lsa {
    uint32_t origin;
    uint64_t sequence;
    std::vector<Link> links;
    std::vector<Prefix> prefixes;
};
struct Route {
    Prefix prefix;
    uint32_t next_hop; // zero means this router's attached network
    uint64_t cost;
    uint32_t origin;
};
std::vector<Route> shortest_paths(uint32_t self, const std::map<uint32_t, Lsa>& database);
std::optional<Route> longest_match(const std::vector<Route>& routes, uint32_t destination);

constexpr uint8_t hello_type = 1, lsa_type = 2, data_type = 3;
Bytes hello(uint32_t id);
Bytes encode(const Lsa& lsa);
std::optional<Lsa> decode_lsa(const Bytes& bytes);
std::optional<uint32_t> decode_hello(const Bytes& bytes);
uint8_t message_type(const Bytes& bytes);
Bytes envelope(const Bytes& packet);
std::optional<Bytes> unwrap(const Bytes& bytes);
uint16_t checksum(const uint8_t* bytes, size_t size);
// Validates the entire IPv4 header and length before mutating TTL/checksum.
// Returns an empty string on success, otherwise a stable drop reason.
std::string prepare_packet(Bytes& packet);
uint32_t destination(const Bytes& packet);
}
