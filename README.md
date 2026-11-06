# IP routing lab

I built a small network routing lab because I wanted to understand why packets can disappear after a connection breaks, even when another path exists. My routers pass updates to their neighbors, choose paths, and forward packets. I use a report to see how long they take to agree on the new paths and why packets were lost along the way.

This runs entirely on my computer as a simulation. It does not change my computer's network settings or need router hardware.

## Try it

From a checkout of this repository, with Python 3.9 or newer and Perl 5.20 or newer:

```sh
mkdir -p reports
python3 router_sim.py --log reports/demo.jsonl
perl tools/routing-report.pl --text reports/summary.txt --csv reports/summary.csv reports/demo.jsonl
cat reports/summary.txt
```

I get a readable text report and a CSV file I can open in a spreadsheet. Both programs use libraries included with their language, so there are no packages to install. The output folder must already exist.

## What I look for in the report

The demo starts with two paths from router A to router D:

```text
A -- B -- D
|         |
C -- E ---+
```

First, I break B-D. A briefly sends traffic toward B using an old path, while B already knows the link is gone. They send the packet back toward each other, which the simulation detects as a forwarding loop. After the updates travel, A uses A-C-E-D instead. Then I break A-C, separating the network into two pieces.

The link-failure part of the report shows:

```text
failure-1 [B-D] at 0 ms: 10 ms
failure-2 [A-C] at 10 ms: 20 ms
```

**Convergence** means every router's table has settled on the correct paths for the surviving network. The report subtracts the failure time from the matching convergence time. These are simulated milliseconds: each update step is 10 ms, not a measurement of physical router speed. The second failure still converges, but A can no longer reach D. Agreement about an unreachable destination is a correct result too.

The demo also reports one packet for each of these reasons:

| Category / reason | What happened |
| --- | --- |
| `packet_drop / forwarding_loop` | Routers briefly disagreed and tried to send a packet back to a router it had already visited. |
| `packet_drop / ttl_expired` | A packet ran out of its allowed number of hops. A hop is one trip between neighboring routers. |
| `packet_drop / no_route` | The sender had no path to the destination after the network split. |
| `malformed_packet / unknown_destination` | I deliberately sent a packet to a destination outside the network. |

I also record four scenario checks, such as whether the alternate path actually delivered a packet. Their `pass` or `fail` results appear in both reports. They are separate from the automated test suite.

## Report other runs

The reporter reads JSON Lines: one event per line. It accepts several files, keeping each run's times and failure IDs separate:

```sh
perl tools/routing-report.pl --strict \
  --text reports/combined.txt --csv reports/combined.csv \
  reports/demo.jsonl t/fixtures/mixed.jsonl
```

That command writes both reports and exits with status **2**, because the mixed fixture deliberately contains a failed check, an unfinished recovery, and damaged lines. Without `--strict`, it writes the same report and exits with status 0. Status 1 means a command-line or file error.

An unfinished recovery is labelled `incomplete`, with an empty duration in CSV. A damaged log line is counted under rejected lines, with its reason and line number. It never counts as a malformed packet: one describes a broken log record, the other describes a packet the router rejected. The report keeps up to 20 diagnostic examples and counts every rejected line. A complete final JSON record works without a final newline; a truncated record is rejected.

The CSV has a `record_type` column: `summary`, `convergence`, `packet`, `test`, `rejected`, or `diagnostic`. I filter on `convergence` to compare recovery times, or `packet` to group losses by `category` and `reason`. Each row carries its input `source`. Blank cells mean that field does not apply. Formula-like text is prefixed with an apostrophe so opening an untrusted log in a spreadsheet does not execute it.

## How the routing part works

Each router starts with the same topology, meaning the list of connections. When a link fails, its two endpoints learn immediately. They send a link-state update, a message describing the broken connection, to their neighbors. Each step moves updates one hop farther. Each router independently uses breadth-first search to find shortest paths in its current view, with every connection costing one hop.

Packets follow those local tables rather than a prewritten route. A separate observer compares every table with shortest paths in the actual remaining network before recording `routes_converged`. If failures overlap, that event means the tables agree with the network after all failures seen so far. Each failure keeps its own start time and ID.

I can change the links with the `Network` class in `router_sim.py`, call `fail_link`, send probes with `send`, and advance time with `tick` or `settle`. The built-in demo is fixed so I can reproduce a report exactly. It models link failures, not restored links or a complete Internet routing protocol. It is a routing experiment, not a daemon that installs operating-system routes.

The Perl reporter uses `strict` and `warnings`. I separated parsing and report formatting into `lib/Routing/Report.pm`; `tools/routing-report.pl` handles arguments and files. It validates event fields and timestamps, rejects duplicate or unmatched failure IDs, and checks that accepted events stay in time order. It reads in bounded chunks and rejects records larger than 64 KiB. Output paths cannot overwrite input logs, including their symlink or hardlink aliases.

The [event format](docs/event-format.md) explains how another router or test runner can produce compatible logs. Missing records can hide losses or make a duration unreliable; a report describes the events it received, not evidence that every packet was logged.

## Tests and CI

```sh
prove -l t
python3 -m unittest discover -s tests -v
```

I have 96 Test::More checks for parsing, damaged input, report output, CSV escaping, and command-line errors, plus 12 Python tests for routing and the complete producer-to-report pipeline. The pipeline reads the CSV independently with Python's CSV parser and checks both measured times and packet counts.

GitHub Actions runs both suites on Linux and macOS for pull requests and pushes to main. It also generates the demo, runs the Perl report with `--strict`, and saves the event log and reports as downloadable artifacts.
