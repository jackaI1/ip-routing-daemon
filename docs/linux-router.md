# My TCP failover lab

I use a second, smaller topology to check the router with real TCP data. The original [six-namespace lab](design.md) connects two end computers through four routers and checks pings, fragments and the reporting pipeline. This four-namespace lab runs its TCP endpoints inside routers 1 and 4. It disables Linux kernel forwarding in every router, so traffic between them must pass through the C++ packet engine.

## Run it

On a disposable Linux machine, install a C++20 compiler, Make, Python 3.9+, iproute2 and ping:

```sh
sudo apt-get update
sudo apt-get install -y g++ make python3 iproute2 iputils-ping
make all unit
sudo python3 lab/netns.py
cat reports/netns/result.json
cat reports/netns/cleanup.json
```

The lab needs root to create network namespaces. It exits when finished and keeps reports, not a running network. `--binary` selects another compiled `routerd` and `--output` selects a report directory. Run from the repository root. Each run uses unique namespace and veth names, so it does not replace an existing network. Python, Perl and pytest are needed for the full existing test suite; see the README.

On macOS, `make unit` runs the portable C++ core. The daemon needs Linux. If Docker is already running with a Linux engine:

```sh
docker build -f lab/Dockerfile -t ip-routing-lab .
mkdir -p reports
docker run --rm --privileged --name ip-routing-lab \
  -v "$PWD/reports:/lab/reports" ip-routing-lab
```

Privileged mode is needed for namespaces and TUN devices. I run this trusted lab on a disposable local development engine, not a shared production engine. The container removes itself after the test; its reports remain in the mounted directory.

## What runs

```text
          cost 1       cost 1
       1 -------- 2 -------- 4
        \                   /
  cost 5 \                 / cost 5
          ------- 3 -------
```

Each router has a `tun0` interface, a virtual interface that hands IP packets to my program. Router 1 owns `10.200.1.0/24`; routers 2, 3 and 4 own the corresponding subnets. Ping and the TCP client run inside router 1's namespace, and the TCP server runs inside router 4's namespace. The endpoints use `.1` in each subnet.

The system sends remote `10.200.0.0/16` traffic into `tun0`. C++ reads each IPv4 packet, validates its header, finds the matching route with the longest network prefix, reduces its hop limit (TTL), updates the header checksum, and sends it to the next router. It wraps the packet in UDP on a directly connected veth pair. UDP carries the original TCP connection's IP packets. At router 4, C++ writes the packet into TUN for local delivery. The kernel handles local TCP sockets, but cannot forward between routers because `net.ipv4.ip_forward` is zero.

The lab uses the existing `src/main.cpp` daemon and `src/router.cpp` core, with the same hello, flooding, sequence rejection, advertisement expiry and weighted shortest-path implementation as the original lab. Each router starts with only its own configured neighbors and prefix. There is one protocol implementation to maintain. The inner MTU is 1400 bytes, leaving room for the UDP envelope inside a 1500-byte link. The [design notes](design.md) describe configuration and protocol limits. My TUN handling follows the [Linux kernel interface documentation](https://docs.kernel.org/networking/tuntap.html).

## What I check

The lab verifies the primary `1-2-4` next hops in both directions, sends three pings and transfers 1,048,576 bytes over TCP. The receiver returns the SHA-256 digest, which the sender checks independently against its original payload. A completed handshake alone would not catch missing or changed data.

I then take down the `2-4` veth, wait for both directions to select `1-3-4`, repeat ping and TCP checks, and verify that router 3's forwarding counter increased. The measured recovery starts after the link-down command finishes and ends when the new endpoint route snapshots are observed. It includes polling delay and does not claim every router has converged at that exact instant. The original lab separately checks every router against an independent calculation.

Next I restore the link, pause router 2 while its interfaces remain up, and resume it. A TCP payload must arrive after each transition. Finally I isolate router 1, require its remote route to disappear and require ping to fail. The lab opens fresh TCP connections after each transition; it does not claim zero packet loss or uninterrupted sessions during convergence. This is a trusted IPv4 lab with an unauthenticated custom protocol, not a production router.

## Reports and cleanup

`reports/netns/` contains route snapshots (`r1.json` through `r4.json`), daemon logs, `result.json`, `ownership.json` and `cleanup.json`. Ownership records contain exact PIDs, Linux process start ticks, namespace names, commands and PID-specific stop commands. UDP port 47000 belongs to each router namespace; TCP port 18080 belongs to the test server in router 4's namespace. No server listens in the host network namespace.

Cleanup runs through normal completion, exceptions and SIGTERM. It resumes paused children, requests termination, waits and kills only its own unresponsive child processes, deletes its namespaces and leftover setup links, and verifies what remains. Every cleanup operation is attempted even if an earlier one fails. Errors and remaining resources are saved before reporting failure. A cleanup failure is a failed test. SIGKILL or machine failure cannot execute Python cleanup; if that happens, use the recorded namespace names and PIDs, checking start ticks before stopping anything.

Two portable failure-injection tests check that a namespace deletion error does not skip other namespaces and that a process signal race does not skip waiting or namespace cleanup. Linux CI runs this lab twice, with separate output directories, and uploads results and cleanup records as `linux-router-lab`.
