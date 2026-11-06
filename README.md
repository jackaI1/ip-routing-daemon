# My small IP router

I built a small IP router because I wanted to see how a network finds another way through when a cable stops working. I can send traffic between two computers in a Linux lab, break a connection in the middle, and watch the routers choose another path without changing any routes by hand.

The lab runs on one Linux machine. It creates six isolated network environments: two end computers and four routers. My C++ program carries the packets between routers and shares updates about which connections are alive. When the shorter path fails, traffic moves to the other side of the network.

To try it on Ubuntu 24.04 or a Linux VM:

```sh
sudo apt-get update
sudo apt-get install -y g++ make python3-venv iproute2 iputils-ping
git clone https://github.com/jackaI1/ip-routing-daemon.git
cd ip-routing-daemon
make -j2
sudo python3 lab/topology.py
```

The demo pings before failure, takes down the connection between routers 2 and 4, pings through router 3, restores the connection, and pings again. It prints the time taken to change paths. It removes its four router processes and six network environments on exit; logs stay in `build/lab/`. It needs root to create the isolated network, so I run it in a disposable VM or a CI runner. macOS can run the core tests, but the network demo needs Linux.

## Check real TCP data after a failure

I also run a four-router lab with the sending and receiving programs inside routers 1 and 4. This version disables Linux kernel forwarding everywhere and checks a 1 MiB TCP payload against its SHA-256 digest, a fingerprint of the received bytes. It tests the preferred path, a broken link, link restoration and a router that stops answering while its interfaces remain up.

```sh
sudo python3 lab/netns.py
cat reports/netns/result.json
cat reports/netns/cleanup.json
```

The script uses the same C++ router as the first demo. It checks the new next hops and actual delivered bytes, then removes its five processes and four isolated networks. [My TCP lab notes](docs/linux-router.md) explain the topology, Linux container option, recovery measurement and cleanup records. Each phase opens a fresh TCP connection, so this checks recovery rather than uninterrupted sessions.

## What happens to a packet

```text
                 r2
               /    \
computer A -- r1      r4 -- computer B
               \    /
                 r3

A: 10.1.0.2                  B: 10.4.0.2
r1-r2-r4: cost 2             r1-r3-r4: cost 4
```

Each router advertises the networks attached to it. It also sends a small greeting to each neighbour every quarter second. If greetings stop for 1.25 seconds, it announces the missing connection. The routers share those announcements and calculate new paths.

When traffic arrives, my program uses **longest-prefix match**: it chooses the most specific matching address range. A route for `10.4.0.0/24` covers fewer addresses than `10.4.0.0/16`, so it wins even if it costs more. The lab deliberately includes both ranges to check this with actual pings. Among paths to the same range, the router chooses the lowest total cost.

## Running the tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make -j2
.venv/bin/python -m pytest -q -m 'not integration'
sudo .venv/bin/python -m pytest -v -s

# Rebuild everything with memory and undefined-behaviour checks.
make clean
make -j2 SANITIZE=1
sudo env ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 \
  UBSAN_OPTIONS=halt_on_error=1 .venv/bin/python -m pytest -v -s
```

The network tests check traffic in both directions, large fragmented packets, link failure and restoration, a partition with no remaining path, a stopped router, malformed traffic, and cleanup after a startup failure. `failure convergence: ...s` reports how long it took to observe the alternate routes; the test requires less than eight seconds. On macOS, `make` builds the portable C++ core and pytest skips Linux-only cases.

## Reading the recovery report

I also keep a small simulator for quick experiments without root access. Its Perl reporter works with both the simulated experiment and the live lab's observation stream:

```sh
# Report the live demo I just ran.
perl tools/routing-report.pl --strict --text build/live-summary.txt \
  --csv build/live-summary.csv build/lab/events.jsonl
cat build/live-summary.txt

# Run the original simulation without changing any network settings.
mkdir -p reports
python3 router_sim.py --log reports/demo.jsonl
perl tools/routing-report.pl --strict --text reports/summary.txt \
  --csv reports/summary.csv reports/demo.jsonl
```

The live report shows the measured time from breaking a link until every router's routes match an independent calculation of the surviving network. It also lists the ping checks and delivered probes. The simulator uses a deterministic clock, so its times describe simulation steps rather than real recovery speed. I kept [the simulation and reporting walkthrough](docs/simulation-report.md), including damaged-log handling, CSV columns and exit codes. The [event format](docs/event-format.md) describes compatible input. The daemon's individual `r*.log` files are debug logs; I pass the lab's `events.jsonl` to the reporter.

## How I put it together

- `src/router.cpp` contains address matching, shortest-path calculation, protocol encoding and IPv4 validation.
- `src/main.cpp` is the Linux daemon. It owns a virtual network adapter, exchanges UDP messages, detects failed neighbours and forwards packets.
- `lab/topology.py` builds and removes the network. It also records the exact router PIDs, start times and namespace-local ports in `resources.json`.
- `tests/` checks the C++ core, configuration handling and real network traffic with pytest.

A **network namespace** is a separate Linux network stack with its own interfaces and route table. A **TUN adapter** passes IP packets between Linux and a program. I use TUN to get host traffic into my daemon, then wrap each packet in UDP to send it over a direct router connection. At intermediate routers, C++ selects the next neighbour. Linux has no remote host routes that can bypass that decision. At the destination router, the daemon puts the packet back into TUN and Linux delivers it to the attached computer.

The control protocol is a small custom **link-state protocol**: routers flood descriptions of their connections and run Dijkstra's shortest-path algorithm over the resulting map. It uses sequence numbers to reject duplicate updates, expiry to discard missing routers, and reciprocal links to avoid trusting stale, one-sided connections. It is not OSPF and does not interoperate with commercial routers.

I kept this an IPv4 lab router. It validates packet lengths and header checksums, reduces the packet lifetime counter, and forwards fragments without reassembling them. It drops expired packets and missing routes with logged reasons; it does not generate ICMP error replies. The UDP protocol has no authentication or encryption and belongs only on a trusted, isolated lab network. More details, configuration examples and timing limits are in [the design notes](docs/design.md).

## CI and test coverage

[Router CI](https://github.com/jackaI1/ip-routing-daemon/actions/workflows/ci.yml) runs on pull requests and on `main`. Each Ubuntu job builds the daemon, runs all 38 pytest cases (26 daemon/core cases and 12 existing simulator/report cases), runs the README demo, and checks that no lab namespaces or daemons remain. One job uses the normal build; the other enables AddressSanitizer and UndefinedBehaviorSanitizer, including leak detection on graceful shutdown. The parser stress test uses 20,000 seeded random messages plus 20,000 mutations of valid messages per build. CI saves the test report, router logs, route snapshots, live recovery reports and cleanup records as downloadable artifacts.

The existing reporting workflow also runs `prove -l t` (96 Perl checks) and `python3 -m unittest discover -s tests -v` (12 simulator/report tests) on Linux and macOS. The pytest cases are under `tests/native/` so the original unittest command still works without installing pytest.

The TCP lab workflow runs `lab/netns.py` twice and saves its payload checks, route snapshots, recovery times and cleanup records. Two portable failure-injection tests cover errors during cleanup; the full pytest suite now has 40 cases. `make unit` runs the nine C++ core cases directly, and `make test` runs pytest plus the 96 Perl checks.
