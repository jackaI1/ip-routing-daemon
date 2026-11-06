# Router design and reproduction notes

## Packet path

The lab's host-facing veth pairs act like cables. Router namespaces enable Linux IP forwarding only to move packets between the host-facing interface and TUN. Remote destinations use a single kernel default route to `rt0`. The daemon reads those IPv4 packets, validates their header and length, decreases TTL, looks up the longest matching prefix and sends them to the selected neighbour over UDP port 47000. A neighbour repeats this decision. An attached destination uses next-hop zero and writes the packet into TUN for delivery by Linux.

The physical-lab equivalent of the UDP transport is four point-to-point links. Each has its own `/30` underlay network, reachable directly with no kernel routing protocol. There is no kernel route to the other host's network and no direct host-to-host tunnel. Every intermediate router executes the same C++ forwarding code. Linux also decrements TTL when it forwards between a host-facing interface and TUN, so edge routers consume an extra TTL compared with a conventional hardware router.

The TUN MTU is 1400. The underlay MTU is 1500, leaving room for the five-byte framing header and UDP/IPv4 headers. Oversized host packets are fragmented by Linux before entering TUN. Each fragment has its own validated IPv4 header; the daemon does not interpret the transport payload. IPv4 options are checksum-validated and carried unchanged, not executed. TTL <= 1, invalid checksums, invalid lengths and absent routes are dropped. ICMP error generation, IPv6, ARP in userspace, NAT and throughput optimization are outside this version.

References I used: [Linux TUN documentation](https://kernel.org/doc/html/latest/networking/tuntap.html) and [network namespace manual](https://www.man7.org/linux/man-pages/man7/network_namespaces.7.html).

## Control messages

Every datagram starts with `JRT`, version byte 1 and a type byte. Multibyte values are unsigned big-endian integers; the code encodes fields individually without casting untrusted buffers into structs.

| Type | Contents |
| --- | --- |
| 1, hello | Router ID, 4 bytes |
| 2, link-state advertisement | Origin ID (4), sequence (8), link count (1), prefix count (1), links (peer ID + cost, 8 each), prefixes (network + prefix length, 5 each) |
| 3, data | Complete IPv4 packet after the five-byte header |

A datagram is accepted only from a configured neighbour's IPv4 address and UDP port. A hello must also have that neighbour's ID. Advertisements reject truncated or trailing bytes, duplicate peers or prefixes, zero costs, invalid prefix lengths, host bits in a network address and excess counts. There are at most 64 neighbours and 64 prefixes per router and 256 origins including self. The data envelope is bounded at 65,000 inner bytes. These are resource bounds, not security against a malicious configured peer.

A new adjacency sends the full known database to the recovered neighbour. New advertisements are flooded to live neighbours except their sender. The originating router refreshes its advertisement every 500 ms with an increasing sequence. Initial sequences use wall-clock microseconds to permit a normal process restart; a system clock moved backwards can delay acceptance of a restarted origin until its sequence catches up. Sequence high-water marks remain after expiry to reject replayed advertisements, bounded to 255 remote origins per process lifetime. Changing more than that requires restarting the lab.

Hello interval: 250 ms. Dead-neighbour timeout: 1250 ms. Remote advertisement expiry: four seconds. Equal or older advertisements do not refresh the expiry timer. A periodic tick and a route snapshot occur at most about 550 ms apart under light load. Tests use an eight-second convergence deadline rather than claiming a hard real-time guarantee. Congestion can delay heartbeats; there is no congestion control or reliable transport.

Shortest paths only traverse edges advertised by both endpoints. Costs may be directional; each positive outbound cost contributes to that direction's path. Equal-cost paths choose the lower first-hop router ID, then the lower advertising origin for duplicate prefixes. A sorted table makes route snapshots readable; lookup independently checks prefix lengths rather than depending on table order. A lost route disappears without a manually installed fallback. Temporary loops during convergence are bounded by TTL.

## Configuration

The daemon accepts a path to a text file:

```text
router 1
tun rt0
status /tmp/r1-status.json
prefix 10.1.0.0/24
peer 2 172.16.12.2 1
peer 3 172.16.13.2 2
```

`router` is a nonzero numeric ID. `prefix` describes an attached range and can repeat for different ranges. `peer` supplies a neighbour ID, its directly reachable address and a positive cost. `status` must point to a writable location with no spaces. Blank lines and `#` comments are supported. Use `build/routerd --check-config FILE` to validate without root. Duplicate directives, peer IDs, peer addresses and prefixes are rejected.

The daemon opens `/dev/net/tun` using `IFF_TUN | IFF_NO_PI`. The lab waits for that adapter, sets its MTU and adds the kernel default route. The daemon stays in the foreground, polls TUN and UDP, and closes both descriptors on SIGINT or SIGTERM. It atomically replaces a JSON status file with routes and forwarding, delivery and drop counters. Logs are JSON lines with monotonic `ts_ms`, `event`, and event-specific fields. Drop logs are rate-limited after the first 20 messages while counters continue increasing.

## Lab ownership and failure reproduction

`sudo python3 lab/topology.py --output build/my-run` creates unique `jr<random>-*` namespaces, four routers and two hosts. Router IDs follow the README diagram. Link 1-2 has `172.16.12.0/30`, 2-4 has `172.16.24.0/30`, and so on. Each namespace has its own UDP port 47000; nothing binds that port on the host. No host sysctl or route is changed.

Router 2 has a dummy attached `10.4.0.0/16` network and router 4 has `10.4.0.0/24`. This intentionally tests overlapping ranges: after failure, the /24 through router 3 still wins over the cheaper /16 through router 2. The full partition test requires the /24 to be withdrawn; the stopped-router test also checks withdrawal of router 2's aggregate.

The Python context owns exact Popen handles and writes PIDs, `/proc` start ticks, namespace names and cleanup commands to `resources.json`. It sends SIGTERM, waits, escalates only its own handles if necessary, deletes its own namespaces, and checks for remaining processes and namespace names. The same cleanup runs after partial startup failure and Python exceptions. SIGTERM on the command-line demo becomes a Python interruption so the context can unwind. SIGKILL or a host crash cannot execute cleanup; on an interrupted disposable runner, the recorded commands identify only the owned resources. Check PID start ticks before using a saved kill command because PIDs can be reused.

Each test uses a fresh topology. No background servers, browser pages or persistent fixtures are needed. A nonzero daemon exit, including a sanitizer finding, fails the test even if the ping already succeeded. The startup-failure test deliberately runs a binary that exits with code 42 and verifies its resources are removed. CI checks for leftover lab namespaces and routerd processes after both the tests and demo.

## Live observations and the existing reporter

The lab writes `events.jsonl` in the existing report format. Its independent Python observer uses a heap-based weighted shortest-path calculation over administratively enabled links, then compares every prefix's cost and next hop at all four routers. It records `routes_converged` only after all snapshots agree. The failure clock starts immediately before the link-down command; snapshot publication and 100 ms observation polling are included in the measured duration. This checks tables, followed by real ping probes, rather than inferring convergence from one successful packet.

The observation stream records failures, convergence, ping scenario results and successful echo replies. It does not translate the daemon's rate-limited debug drop logs into supposedly complete packet-loss counts. Pass the observation stream to `tools/routing-report.pl`; individual daemon logs use a separate schema with `ts_ms` and diagnostic events. The original Python simulator, Perl library, fixtures and Test::More suite remain available unchanged. Its no-root reproduction guide is [simulation-report.md](simulation-report.md).
