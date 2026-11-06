#include "router.hpp"
#include <cassert>
#include <functional>
#include <iostream>
#include <random>
#include <stdexcept>

using namespace routing;
Prefix p(const char* s) { return Prefix::parse(s); }
Bytes packet() {
    Bytes b(28, 0); b[0] = 0x45; b[3] = 28; b[8] = 64; b[9] = 1;
    b[12] = 10; b[15] = 2; b[16] = 10; b[17] = 4; b[19] = 2;
    auto c = checksum(b.data(), 20); b[10] = c >> 8; b[11] = c & 255;
    return b;
}
std::map<uint32_t, Lsa> diamond() {
    return {{1, {1, 1, {{2, 1}, {3, 2}}, {p("10.1.0.0/24")}}},
            {2, {2, 1, {{1, 1}, {4, 1}}, {p("10.4.0.0/16")}}},
            {3, {3, 1, {{1, 2}, {4, 2}}, {p("10.3.0.0/24")}}},
            {4, {4, 1, {{2, 1}, {3, 2}}, {p("10.4.0.0/24")}}}};
}
void prefixes() {
    assert(p("10.1.2.9/24").str() == "10.1.2.0/24");
    assert(p("0.0.0.0/0").contains(ip("255.255.255.255")));
    assert(p("255.255.255.255/32").contains(UINT32_MAX));
    assert(!p("10.0.0.0/8").contains(ip("11.0.0.1")));
    for (const auto* bad : {"10.0.0.1", "10.0.0.1/33", "10.0.0.1/-1", "10.0.0.1/8x", "999.0.0.0/8", "10.0.0.1/"}) {
        bool threw = false;
        try { Prefix::parse(bad); } catch (const std::exception&) { threw = true; }
        assert(threw);
    }
}
void lpm() {
    std::vector<Route> routes{{p("0.0.0.0/0"), 1, 0, 1}, {p("10.0.0.0/8"), 2, 1, 2},
        {p("10.4.0.0/24"), 3, 99, 3}, {p("10.4.0.2/32"), 4, 999, 4}};
    assert(longest_match(routes, ip("10.4.0.2"))->next_hop == 4);
    assert(longest_match(routes, ip("10.4.0.3"))->next_hop == 3);
    assert(longest_match(routes, ip("10.8.0.1"))->next_hop == 2);
    assert(longest_match(routes, ip("192.0.2.1"))->next_hop == 1);
    assert(!longest_match({}, 0));
}
void paths() {
    auto db = diamond();
    auto routes = shortest_paths(1, db);
    const auto r = longest_match(routes, ip("10.4.0.2"));
    assert(r && r->next_hop == 2 && r->cost == 2 && r->prefix.length == 24);
    assert(longest_match(routes, ip("10.1.0.2"))->next_hop == 0);
}
void failure() {
    auto db = diamond();
    db[2].links = {{1, 1}}; // only one end has noticed the failed edge
    auto r = longest_match(shortest_paths(1, db), ip("10.4.0.2"));
    assert(r && r->next_hop == 3 && r->cost == 4);
    db[3].links = {{1, 2}};
    auto routes = shortest_paths(1, db);
    for (auto route : routes) assert(route.origin != 4);
    db = diamond();
    assert(longest_match(shortest_paths(1, db), ip("10.4.0.2"))->cost == 2);
}
void ties() {
    auto db = diamond(); db[1].links[1].cost = 1; db[3].links[1].cost = 1;
    assert(longest_match(shortest_paths(1, db), ip("10.4.0.2"))->next_hop == 2);
    // A cycle and an unreachable advertisement must not create routes or spin.
    db[99] = {99, 1, {{100, 1}}, {p("192.0.2.0/24")}};
    assert(!longest_match(shortest_paths(1, db), ip("192.0.2.1")));
}
void wire() {
    auto lsa = diamond().at(1); auto bytes = encode(lsa); auto decoded = decode_lsa(bytes);
    assert(decoded && decoded->origin == 1 && decoded->sequence == 1);
    assert(decoded->links == lsa.links && decoded->prefixes == lsa.prefixes);
    assert(decode_hello(hello(42)) == 42); assert(!decode_hello(hello(0)));
    for (size_t n = 0; n < bytes.size(); ++n) assert(!decode_lsa(Bytes(bytes.begin(), bytes.begin() + n)));
    bytes.push_back(0); assert(!decode_lsa(bytes));
    lsa.links.push_back(lsa.links[0]); assert(!decode_lsa(encode(lsa)));
    lsa = diamond().at(1); lsa.prefixes[0].length = 33; assert(!decode_lsa(encode(lsa)));
    lsa = diamond().at(1); lsa.links[0].cost = 0; assert(!decode_lsa(encode(lsa)));
    lsa = diamond().at(1); lsa.links.resize(65);
    bool threw = false; try { encode(lsa); } catch (const std::invalid_argument&) { threw = true; } assert(threw);
}
void ipv4() {
    auto b = packet(); assert(destination(b) == ip("10.4.0.2"));
    assert(prepare_packet(b).empty() && b[8] == 63 && checksum(b.data(), 20) == 0);
    assert(unwrap(envelope(b)) == b);
    b = packet(); b[8] = 1; b[10] = b[11] = 0;
    auto c = checksum(b.data(), 20); b[10] = c >> 8; b[11] = c & 255;
    assert(prepare_packet(b) == "ttl_expired");
    b = packet(); b[10] ^= 1; assert(prepare_packet(b) == "invalid_checksum");
    b = packet(); b[3] = 27; assert(prepare_packet(b) == "invalid_length");
    b = packet(); b[0] = 0x4f; assert(prepare_packet(b) == "invalid_header");
    b = packet(); b[0] = 0x65; assert(prepare_packet(b) == "invalid_ipv4");
    b.clear(); assert(prepare_packet(b) == "invalid_ipv4");
}
void options_fragments() {
    auto b = packet(); b.insert(b.begin() + 20, 4, 0); b[0] = 0x46; b[3] = 32;
    b[6] = 0x20; // Forward fragments independently, without reading transport headers.
    b[10] = b[11] = 0; auto c = checksum(b.data(), 24); b[10] = c >> 8; b[11] = c & 255;
    assert(prepare_packet(b).empty() && checksum(b.data(), 24) == 0 && b[6] == 0x20);
}
void fuzz() {
    std::mt19937 rng(12345);
    for (unsigned i = 0; i < 20000; ++i) {
        Bytes b(rng() % 300);
        for (auto& byte : b) byte = rng() & 255;
        if (i % 2 == 0 && b.size() >= 5) { b[0]='J'; b[1]='R'; b[2]='T'; b[3]=1; b[4]=lsa_type; }
        decode_lsa(b); decode_hello(b); unwrap(b); prepare_packet(b);
        // Mutate real wire messages too, so random tests reach inside the parser.
        auto real = encode(diamond().at(1)); real[rng() % real.size()] = rng() & 255; decode_lsa(real);
    }
}
int main(int argc, char** argv) {
    std::map<std::string, std::function<void()>> cases{{"prefixes", prefixes}, {"lpm", lpm}, {"paths", paths},
        {"failure", failure}, {"ties", ties}, {"wire", wire}, {"ipv4", ipv4}, {"options_fragments", options_fragments}, {"fuzz", fuzz}};
    if (argc != 2 || !cases.contains(argv[1])) return 2;
    cases.at(argv[1])(); std::cout << argv[1] << " passed\n";
}
