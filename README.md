# ip-routing-daemon

I built a small IP router to learn how the underlying idea works. This is a learning project.

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

## Tests and limits

There are 7 test source files in this copy. No test pass count is claimed here: I have not re-run the project suite as part of this move. This is a source snapshot, and running it on a new machine may need environment-specific setup.


Project checks run from this directory through the path-filtered hub workflows: `ip-routing-daemon-report.yml`, `ip-routing-daemon-ci.yml`, `ip-routing-daemon-cpp-router.yml`.

I later merged this into my [home-server-ops](https://github.com/jackaI1/home-server-ops) project, and the current version and more detail are at [this project](https://github.com/jackaI1/home-server-ops/tree/main/projects/ip-routing-daemon).
