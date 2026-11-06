#include "router.hpp"
#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/if_tun.h>
#include <net/if.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <csignal>
#include <cstring>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <stdexcept>
#include <utility>

using namespace routing;
using Clock = std::chrono::steady_clock;
constexpr uint16_t port = 47000;
constexpr auto dead_time = std::chrono::milliseconds(1250);
constexpr auto refresh = std::chrono::milliseconds(500);
constexpr auto max_age = std::chrono::seconds(4);
volatile sig_atomic_t running = 1;
void stop(int) { running = 0; }
struct Fd {
    int value;
    explicit Fd(int v) : value(v) { if (v < 0) throw std::runtime_error(std::strerror(errno)); }
    ~Fd() { close(value); }
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;
};
struct Peer {
    uint32_t id, address, cost;
    Clock::time_point seen{};
    bool alive = false;
};
struct Config {
    uint32_t id = 0;
    std::string tun, status;
    std::vector<Peer> peers;
    std::vector<Prefix> prefixes;
};
uint32_t number(const std::string& text) {
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("expected positive integer");
    auto n = std::stoull(text);
    if (!n || n > UINT32_MAX) throw std::invalid_argument("integer out of range");
    return static_cast<uint32_t>(n);
}
Config config(const std::string& path) {
    std::ifstream file(path);
    if (!file) throw std::runtime_error("cannot read config: " + path);
    Config c;
    std::set<uint32_t> ids, addresses;
    std::set<Prefix> prefixes;
    std::string line;
    while (std::getline(file, line)) {
        line = line.substr(0, line.find('#'));
        std::istringstream input(line);
        std::string key, a, b, d, extra;
        if (!(input >> key)) continue;
        if (!(input >> a)) throw std::invalid_argument("missing config value");
        if (key == "router" && !c.id) c.id = number(a);
        else if (key == "tun" && c.tun.empty() && a.size() < IFNAMSIZ && a.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") == std::string::npos) c.tun = a;
        else if (key == "status" && c.status.empty()) c.status = a;
        else if (key == "prefix") {
            auto p = Prefix::parse(a);
            if (!prefixes.insert(p).second) throw std::invalid_argument("duplicate prefix");
            c.prefixes.push_back(p);
        } else if (key == "peer" && (input >> b >> d)) {
            const auto id = number(a), address = ip(b), cost = number(d);
            if (!ids.insert(id).second || !addresses.insert(address).second) throw std::invalid_argument("duplicate peer");
            c.peers.push_back({id, address, cost, {}, false});
        } else throw std::invalid_argument("unknown or duplicate config directive: " + key);
        if (input >> extra) throw std::invalid_argument("extra config value");
    }
    if (!c.id || c.tun.empty() || c.status.empty() || c.prefixes.empty() || c.peers.size() > 64 || c.prefixes.size() > 64 || ids.contains(c.id))
        throw std::invalid_argument("config requires router, tun, status, 1-64 prefixes, and up to 64 distinct peers");
    return c;
}
void event(const std::string& name, const std::string& fields = "") {
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
    std::cout << "{\"ts_ms\":" << ms << ",\"event\":\"" << name << "\"" << fields << "}" << std::endl;
}
class Router {
    Config c;
    Fd tun, socket;
    std::map<uint32_t, Lsa> db;
    std::map<uint32_t, Clock::time_point> age;
    // Keep high-water marks after expiry so replayed advertisements cannot revive routes.
    std::map<uint32_t, uint64_t> sequences;
    std::vector<Route> routes;
    uint64_t sequence, forwarded = 0, delivered = 0, dropped = 0;
    Clock::time_point last_refresh{}, last_hello{};
    static int open_tun(const std::string& name) {
        Fd fd(open("/dev/net/tun", O_RDWR | O_NONBLOCK | O_CLOEXEC));
        ifreq req{};
        std::copy(name.begin(), name.end(), req.ifr_name);
        req.ifr_flags = IFF_TUN | IFF_NO_PI;
        if (ioctl(fd.value, TUNSETIFF, &req) < 0) throw std::runtime_error(std::string("TUNSETIFF: ") + std::strerror(errno));
        return std::exchange(fd.value, -1);
    }
    void send(const Peer& peer, const Bytes& bytes) {
        sockaddr_in to{}; to.sin_family = AF_INET; to.sin_port = htons(port); to.sin_addr.s_addr = htonl(peer.address);
        if (sendto(socket.value, bytes.data(), bytes.size(), 0, reinterpret_cast<sockaddr*>(&to), sizeof(to)) < 0)
            event("send_error", ",\"peer\":" + std::to_string(peer.id));
    }
    void flood(const Bytes& bytes, uint32_t except = 0) {
        for (const auto& p : c.peers) if (p.alive && p.id != except) send(p, bytes);
    }
    void recompute() { routes = shortest_paths(c.id, db); }
    void originate() {
        Lsa own{c.id, ++sequence, {}, c.prefixes};
        for (const auto& p : c.peers) if (p.alive) own.links.push_back({p.id, p.cost});
        db[c.id] = own;
        flood(encode(own));
        recompute();
        event("route_update", ",\"router\":" + std::to_string(c.id) + ",\"routes\":" + std::to_string(routes.size()));
    }
    void drop(const std::string& reason) {
        ++dropped;
        // Bound log volume under malformed traffic; the status counter remains exact.
        if (dropped <= 20 || dropped % 1000 == 0) event("drop", ",\"reason\":\"" + reason + "\"");
    }
    void forward(Bytes packet) {
        auto error = prepare_packet(packet);
        if (!error.empty()) { drop(error); return; }
        auto route = longest_match(routes, destination(packet));
        if (!route) { drop("no_route"); return; }
        if (!route->next_hop) {
            if (write(tun.value, packet.data(), packet.size()) != static_cast<ssize_t>(packet.size())) drop("tun_write");
            else ++delivered;
        } else {
            auto next = std::find_if(c.peers.begin(), c.peers.end(), [&](const auto& p) { return p.id == route->next_hop && p.alive; });
            if (next == c.peers.end()) { drop("next_hop_down"); return; }
            send(*next, envelope(packet)); ++forwarded;
        }
    }
    void receive() {
        std::array<uint8_t, 65536> buffer{};
        sockaddr_in from{}; socklen_t length = sizeof(from);
        auto n = recvfrom(socket.value, buffer.data(), buffer.size(), 0, reinterpret_cast<sockaddr*>(&from), &length);
        if (n < 0) return;
        auto peer = std::find_if(c.peers.begin(), c.peers.end(), [&](const auto& p) { return p.address == ntohl(from.sin_addr.s_addr); });
        if (peer == c.peers.end() || from.sin_port != htons(port)) { drop("unknown_peer"); return; }
        Bytes bytes(buffer.begin(), buffer.begin() + n);
        if (auto id = decode_hello(bytes)) {
            if (*id != peer->id) { drop("peer_id_mismatch"); return; }
            peer->seen = Clock::now();
            if (!peer->alive) {
                peer->alive = true;
                event("link_up", ",\"peer\":" + std::to_string(peer->id));
                originate();
                // A recovering peer needs the full database, not only this router's LSA.
                for (const auto& [_, lsa] : db) send(*peer, encode(lsa));
            }
        } else if (message_type(bytes) == lsa_type && peer->alive) {
            auto lsa = decode_lsa(bytes);
            if (!lsa) { drop("malformed_lsa"); return; }
            if (lsa->origin == c.id) return;
            auto known = sequences.find(lsa->origin);
            if (known != sequences.end() && lsa->sequence <= known->second) return;
            if (known == sequences.end() && sequences.size() >= 255) { drop("database_full"); return; }
            sequences[lsa->origin] = lsa->sequence;
            db[lsa->origin] = *lsa; age[lsa->origin] = Clock::now();
            flood(bytes, peer->id); recompute();
        } else if (auto packet = unwrap(bytes); packet && peer->alive) forward(std::move(*packet));
        else drop("malformed_message");
    }
    void status() {
        const auto temp = c.status + ".tmp";
        std::ofstream out(temp);
        out << "{\"router\":" << c.id << ",\"forwarded\":" << forwarded << ",\"delivered\":" << delivered
            << ",\"dropped\":" << dropped << ",\"routes\":[";
        bool comma = false;
        for (const auto& r : routes) {
            if (comma) out << ',';
            comma = true;
            out << "{\"prefix\":\"" << r.prefix.str() << "\",\"next_hop\":" << r.next_hop << ",\"cost\":" << r.cost << '}';
        }
        out << "]}\n"; out.close();
        if (!out || rename(temp.c_str(), c.status.c_str()) != 0) throw std::runtime_error("cannot write status");
    }
public:
    explicit Router(Config conf) : c(std::move(conf)), tun(open_tun(c.tun)),
        socket(::socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0)),
        sequence(static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count())) {
        sockaddr_in bind_address{}; bind_address.sin_family = AF_INET; bind_address.sin_port = htons(port);
        if (bind(socket.value, reinterpret_cast<sockaddr*>(&bind_address), sizeof(bind_address)) < 0)
            throw std::runtime_error(std::string("bind UDP 47000: ") + std::strerror(errno));
        originate(); status();
    }
    void run() {
        while (running) {
            const auto now = Clock::now();
            if (now - last_hello >= std::chrono::milliseconds(250)) {
                for (const auto& p : c.peers) send(p, hello(c.id));
                last_hello = now;
            }
            bool changed = false;
            for (auto& p : c.peers) if (p.alive && now - p.seen > dead_time) {
                p.alive = false; changed = true;
                event("link_down", ",\"peer\":" + std::to_string(p.id));
            }
            for (auto it = age.begin(); it != age.end();) {
                if (now - it->second > max_age) { db.erase(it->first); it = age.erase(it); changed = true; }
                else ++it;
            }
            if (changed || now - last_refresh >= refresh) {
                originate(); last_refresh = now; status();
            }
            pollfd fds[] = {{tun.value, POLLIN, 0}, {socket.value, POLLIN, 0}};
            if (poll(fds, 2, 50) < 0 && errno != EINTR) throw std::runtime_error("poll failed");
            if (fds[0].revents & POLLIN) {
                std::array<uint8_t, 65536> buffer{};
                auto n = read(tun.value, buffer.data(), buffer.size());
                if (n > 0) forward(Bytes(buffer.begin(), buffer.begin() + n));
            }
            if (fds[1].revents & POLLIN) receive();
        }
        status();
    }
};
int main(int argc, char** argv) {
    try {
        if (argc == 3 && std::string(argv[1]) == "--check-config") { config(argv[2]); std::cout << "config valid\n"; return 0; }
        if (argc != 2) { std::cerr << "usage: routerd CONFIG | routerd --check-config CONFIG\n"; return 2; }
        std::signal(SIGINT, stop); std::signal(SIGTERM, stop);
        Router router(config(argv[1])); router.run();
    } catch (const std::exception& e) { std::cerr << "routerd: " << e.what() << '\n'; return 1; }
}
