#include "router.hpp"
#include <arpa/inet.h>
#include <algorithm>
#include <limits>
#include <queue>
#include <set>
#include <stdexcept>
#include <tuple>

namespace routing {
namespace {
uint32_t mask(uint8_t length) { return length == 0 ? 0 : UINT32_MAX << (32 - length); }
void put(Bytes& b, uint64_t v, unsigned n) {
    for (unsigned i = n; i > 0; --i) b.push_back(static_cast<uint8_t>(v >> ((i - 1) * 8)));
}
struct Reader {
    const Bytes& b;
    size_t pos = 5;
    uint64_t get(size_t n) {
        if (n > b.size() - pos) throw std::out_of_range("truncated message");
        uint64_t v = 0;
        for (size_t i = 0; i < n; ++i) v = (v << 8) | b[pos++];
        return v;
    }
};
Bytes header(uint8_t type) { return {'J','R','T',1,type}; }
}
uint32_t ip(const std::string& value) {
    in_addr address{};
    if (inet_pton(AF_INET, value.c_str(), &address) != 1) throw std::invalid_argument("invalid IPv4 address");
    return ntohl(address.s_addr);
}
std::string ip_string(uint32_t value) {
    in_addr address{htonl(value)};
    char buffer[INET_ADDRSTRLEN]{};
    if (!inet_ntop(AF_INET, &address, buffer, sizeof(buffer))) throw std::runtime_error("inet_ntop");
    return buffer;
}
Prefix Prefix::parse(const std::string& text) {
    const auto slash = text.find('/');
    if (slash == std::string::npos) throw std::invalid_argument("prefix needs /length");
    const auto suffix = text.substr(slash + 1);
    if (suffix.empty() || suffix.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("invalid prefix length");
    const auto n = std::stoul(suffix);
    if (n > 32) throw std::invalid_argument("prefix length exceeds 32");
    const auto address = ip(text.substr(0, slash));
    return {address & mask(static_cast<uint8_t>(n)), static_cast<uint8_t>(n)};
}
bool Prefix::contains(uint32_t address) const { return (address & mask(length)) == network; }
std::string Prefix::str() const { return ip_string(network) + "/" + std::to_string(length); }

std::vector<Route> shortest_paths(uint32_t self, const std::map<uint32_t, Lsa>& db) {
    // Only reciprocal advertised edges are usable. This prevents a stale, one-sided
    // advertisement from resurrecting a failed link during convergence.
    using Key = std::pair<uint64_t, uint32_t>; // total cost, first hop (tie break)
    std::map<uint32_t, Key> best{{self, {0, 0}}};
    using Item = std::tuple<uint64_t, uint32_t, uint32_t>;
    std::priority_queue<Item, std::vector<Item>, std::greater<>> queue;
    queue.emplace(0, 0, self);
    while (!queue.empty()) {
        auto [cost, first, node] = queue.top(); queue.pop();
        if (best.at(node) != Key{cost, first}) continue;
        auto current = db.find(node);
        if (current == db.end()) continue;
        for (const auto& link : current->second.links) {
            auto remote = db.find(link.peer);
            if (remote == db.end() || link.cost == 0) continue;
            const auto& back = remote->second.links;
            if (std::none_of(back.begin(), back.end(), [node](const auto& l) { return l.peer == node && l.cost > 0; })) continue;
            const uint32_t hop = node == self ? link.peer : first;
            Key candidate{cost + link.cost, hop};
            auto old = best.find(link.peer);
            if (old == best.end() || candidate < old->second) {
                best[link.peer] = candidate;
                queue.emplace(candidate.first, candidate.second, link.peer);
            }
        }
    }
    std::map<Prefix, Route> selected;
    for (const auto& [node, path] : best) {
        auto found = db.find(node);
        if (found == db.end()) continue;
        for (auto prefix : found->second.prefixes) {
            Route route{prefix, path.second, path.first, node};
            auto old = selected.find(prefix);
            if (old == selected.end() || std::tie(route.cost, route.next_hop, route.origin) <
                std::tie(old->second.cost, old->second.next_hop, old->second.origin)) selected.insert_or_assign(prefix, route);
        }
    }
    std::vector<Route> routes;
    for (const auto& [_, route] : selected) routes.push_back(route);
    std::sort(routes.begin(), routes.end(), [](const Route& a, const Route& b) {
        return a.prefix.length > b.prefix.length;
    });
    return routes;
}
std::optional<Route> longest_match(const std::vector<Route>& routes, uint32_t address) {
    std::optional<Route> result;
    for (const auto& route : routes) {
        if (route.prefix.contains(address) && (!result || route.prefix.length > result->prefix.length ||
            (route.prefix.length == result->prefix.length && std::tie(route.cost, route.next_hop, route.origin) <
             std::tie(result->cost, result->next_hop, result->origin)))) result = route;
    }
    return result;
}
uint8_t message_type(const Bytes& b) {
    if (b.size() < 5 || b[0] != 'J' || b[1] != 'R' || b[2] != 'T' || b[3] != 1) return 0;
    return b[4];
}
Bytes hello(uint32_t id) { auto b = header(hello_type); put(b, id, 4); return b; }
std::optional<uint32_t> decode_hello(const Bytes& b) {
    if (message_type(b) != hello_type || b.size() != 9) return {};
    Reader r{b}; auto id = static_cast<uint32_t>(r.get(4));
    if (!id) return {};
    return id;
}
Bytes encode(const Lsa& lsa) {
    if (lsa.links.size() > 64 || lsa.prefixes.size() > 64) throw std::invalid_argument("LSA exceeds bounds");
    auto b = header(lsa_type);
    put(b, lsa.origin, 4); put(b, lsa.sequence, 8);
    put(b, lsa.links.size(), 1); put(b, lsa.prefixes.size(), 1);
    for (auto link : lsa.links) { put(b, link.peer, 4); put(b, link.cost, 4); }
    for (auto p : lsa.prefixes) { put(b, p.network, 4); put(b, p.length, 1); }
    return b;
}
std::optional<Lsa> decode_lsa(const Bytes& b) {
    if (message_type(b) != lsa_type) return {};
    try {
        Reader r{b};
        Lsa l{static_cast<uint32_t>(r.get(4)), r.get(8), {}, {}};
        auto links = r.get(1), prefixes = r.get(1);
        if (!l.origin || !l.sequence || links > 64 || prefixes > 64) return {};
        std::set<uint32_t> peers;
        std::set<Prefix> networks;
        for (size_t i = 0; i < links; ++i) {
            Link link{static_cast<uint32_t>(r.get(4)), static_cast<uint32_t>(r.get(4))};
            if (!link.peer || link.peer == l.origin || !link.cost || !peers.insert(link.peer).second) return {};
            l.links.push_back(link);
        }
        for (size_t i = 0; i < prefixes; ++i) {
            Prefix p{static_cast<uint32_t>(r.get(4)), static_cast<uint8_t>(r.get(1))};
            if (p.length > 32 || (p.network & mask(p.length)) != p.network || !networks.insert(p).second) return {};
            l.prefixes.push_back(p);
        }
        if (r.pos != b.size()) return {};
        return l;
    } catch (const std::out_of_range&) { return {}; }
}
Bytes envelope(const Bytes& packet) {
    auto b = header(data_type); b.insert(b.end(), packet.begin(), packet.end()); return b;
}
std::optional<Bytes> unwrap(const Bytes& b) {
    if (message_type(b) != data_type || b.size() < 25 || b.size() > 65005) return {};
    return Bytes(b.begin() + 5, b.end());
}
uint16_t checksum(const uint8_t* p, size_t size) {
    uint32_t sum = 0;
    for (size_t i = 0; i + 1 < size; i += 2) sum += (static_cast<uint32_t>(p[i]) << 8) | p[i+1];
    if (size % 2) sum += static_cast<uint32_t>(p[size-1]) << 8;
    while (sum >> 16) sum = (sum & 0xffff) + (sum >> 16);
    return static_cast<uint16_t>(~sum);
}
std::string prepare_packet(Bytes& p) {
    if (p.size() < 20 || p.size() > 65000 || (p[0] >> 4) != 4) return "invalid_ipv4";
    const size_t h = (p[0] & 15) * 4;
    if (h < 20 || h > p.size()) return "invalid_header";
    const size_t length = (static_cast<size_t>(p[2]) << 8) | p[3];
    if (length != p.size()) return "invalid_length";
    if (checksum(p.data(), h) != 0) return "invalid_checksum";
    if (p[8] <= 1) return "ttl_expired";
    --p[8]; p[10] = p[11] = 0;
    const auto c = checksum(p.data(), h);
    p[10] = static_cast<uint8_t>(c >> 8); p[11] = static_cast<uint8_t>(c);
    return "";
}
uint32_t destination(const Bytes& p) {
    return (static_cast<uint32_t>(p.at(16)) << 24) | (static_cast<uint32_t>(p.at(17)) << 16) |
           (static_cast<uint32_t>(p.at(18)) << 8) | p.at(19);
}
}
