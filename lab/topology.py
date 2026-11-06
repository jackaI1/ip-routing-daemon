#!/usr/bin/env python3
"""An isolated diamond network. Context ownership covers partial startup failures too."""
import argparse
import heapq
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

LINKS = [(1, 2, 1), (2, 4, 1), (1, 3, 2), (3, 4, 2)]


def run(*args, check=True):
    return subprocess.run([str(a) for a in args], check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)


def wait_for(check, timeout=10, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}")


class Lab:
    def __init__(self, binary, output):
        self.binary = Path(binary).resolve()
        self.output = Path(output).resolve()
        self.tag = "jr" + uuid.uuid4().hex[:8]
        self.names = []
        self.processes = []
        self.logs = []
        self.ownership = []
        self.cleaned = False
        self.started = time.monotonic()
        self.failures = 0
        self.active_links = {(a, b) for a, b, _ in LINKS}

    def ns(self, node):
        return f"{self.tag}-{node}"

    def ip(self, node, *args, check=True):
        return run("ip", "-n", self.ns(node), *args, check=check)

    def exec(self, node, *args, check=True):
        return run("ip", "netns", "exec", self.ns(node), *args, check=check)

    def create_ns(self, node):
        name = self.ns(node)
        run("ip", "netns", "add", name)
        self.names.append(name)
        self.ip(node, "link", "set", "lo", "up")

    def pair(self, left, right, li, ri, la, ra):
        # Create the pair inside an owned namespace, so even a partial failure is scoped.
        self.ip(left, "link", "add", li, "type", "veth", "peer", "name", ri)
        self.ip(left, "link", "set", ri, "netns", self.ns(right))
        for node, interface, address in [(left, li, la), (right, ri, ra)]:
            self.ip(node, "address", "add", address, "dev", interface)
            self.ip(node, "link", "set", interface, "up")

    def __enter__(self):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("the topology needs Linux and root (sudo)")
        if not self.binary.is_file() or not shutil.which("ip") or not shutil.which("ping"):
            raise RuntimeError("build routerd and install iproute2 and iputils-ping first")
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "events.jsonl").write_text("")
        try:
            for node in ["r1", "r2", "r3", "r4", "h1", "h4"]:
                self.create_ns(node)
            for a, b, _ in LINKS:
                self.pair(f"r{a}", f"r{b}", f"to{b}", f"to{a}",
                          f"172.16.{a * 10 + b}.1/30", f"172.16.{a * 10 + b}.2/30")
            for n in [1, 4]:
                self.pair(f"r{n}", f"h{n}", "lan0", "eth0", f"10.{n}.0.1/24", f"10.{n}.0.2/24")
                self.ip(f"h{n}", "route", "add", "default", "via", f"10.{n}.0.1")
            # A covering network on r2 competes with r4's more specific /24.
            # If forwarding chose the cheapest match instead of the longest, ping fails.
            self.ip("r2", "link", "add", "aggregate", "type", "dummy")
            self.ip("r2", "address", "add", "10.4.99.1/16", "dev", "aggregate")
            self.ip("r2", "link", "set", "aggregate", "up")
            for n in range(1, 5):
                node = f"r{n}"
                self.exec(node, "sysctl", "-qw", "net.ipv4.ip_forward=1")
                # This changes only this lab namespace, never the host's sysctls.
                for interface in ["all", "default"]:
                    self.exec(node, "sysctl", "-qw", f"net.ipv4.conf.{interface}.rp_filter=0")
                for interface in json.loads(self.ip(node, "-j", "link", "show").stdout):
                    self.exec(node, "sysctl", "-qw", f"net.ipv4.conf.{interface['ifname']}.rp_filter=0")
                self.ip(node, "address", "add", f"10.255.0.{n}/32", "dev", "lo")
                lines = [f"router {n}", "tun rt0", f"status {self.output / (node + '.json')}", f"prefix 10.255.0.{n}/32"]
                if n in (1, 4):
                    lines.append(f"prefix 10.{n}.0.0/24")
                if n == 2:
                    lines.append("prefix 10.4.0.0/16")
                for a, b, cost in LINKS:
                    if n == a:
                        lines.append(f"peer {b} 172.16.{a * 10 + b}.2 {cost}")
                    elif n == b:
                        lines.append(f"peer {a} 172.16.{a * 10 + b}.1 {cost}")
                config = self.output / f"{node}.conf"
                config.write_text("\n".join(lines) + "\n")
                logfile = open(self.output / f"{node}.log", "w")
                self.logs.append(logfile)
                process = subprocess.Popen(["ip", "netns", "exec", self.ns(node), str(self.binary), str(config)],
                                           stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
                self.processes.append(process)
                self.ownership.append({"namespace": self.ns(node), "pid": process.pid,
                                       "start_ticks": Path(f"/proc/{process.pid}/stat").read_text().split()[21],
                                       "port": "UDP 47000 inside " + self.ns(node),
                                       "cleanup": f"kill -TERM {process.pid}; ip netns del {self.ns(node)}"})
                self.record()
                wait_for(lambda: self.tun_ready(node, process), description=f"{node} TUN")
                self.ip(node, "link", "set", "rt0", "mtu", "1400", "up")
                self.ip(node, "route", "add", "default", "dev", "rt0")
            wait_for(self.routes_agree, description="all initial shortest paths")
            return self
        except BaseException:
            self.close()
            raise

    def tun_ready(self, node, process):
        if process.poll() is not None:
            raise RuntimeError(f"{node} exited with {process.returncode}; see {self.output / (node + '.log')}")
        return self.ip(node, "link", "show", "rt0", check=False).returncode == 0

    def status(self, n):
        try:
            return json.loads((self.output / f"r{n}.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"routes": []}

    def route(self, n, prefix, hop):
        return any(r["prefix"] == prefix and r["next_hop"] == hop for r in self.status(n)["routes"])

    def routes_agree(self):
        """Independent weighted-path observer checks every route on every router."""
        prefixes = {n: [f"10.255.0.{n}/32"] for n in range(1, 5)}
        prefixes[1].append("10.1.0.0/24")
        prefixes[2].append("10.4.0.0/16")
        prefixes[4].append("10.4.0.0/24")
        for source in range(1, 5):
            best = {source: (0, 0)}
            queue = [(0, 0, source)]
            while queue:
                cost, first, node = heapq.heappop(queue)
                if best[node] != (cost, first):
                    continue
                for a, b, weight in LINKS:
                    if (a, b) not in self.active_links or node not in (a, b):
                        continue
                    peer = b if node == a else a
                    hop = peer if node == source else first
                    candidate = (cost + weight, hop)
                    if peer not in best or candidate < best[peer]:
                        best[peer] = candidate
                        heapq.heappush(queue, (*candidate, peer))
            expected = {prefix: (hop, cost) for node, (cost, hop) in best.items() for prefix in prefixes[node]}
            observed = {r["prefix"]: (r["next_hop"], r["cost"]) for r in self.status(source)["routes"]}
            if observed != expected:
                return False
        return True

    def event(self, kind, **fields):
        record = {"event": kind, "t_ms": int((time.monotonic() - self.started) * 1000), **fields}
        with (self.output / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")

    def failure(self, a, b):
        self.failures += 1
        failure_id = f"failure-{self.failures}"
        self.event("link_failure", failure_id=failure_id, link=f"r{a}-r{b}")
        return failure_id

    def converged(self, failure_id):
        if not self.routes_agree():
            raise AssertionError("cannot record convergence before every route agrees")
        self.event("routes_converged", failure_id=failure_id)

    def ping(self, source="h1", target="10.4.0.2", *, size=56, count=3, check=True):
        result = self.exec(source, "ping", "-n", "-c", str(count), "-W", "1", "-M", "dont",
                           "-s", str(size), target, check=False)
        success = result.returncode == 0 and ", 0% packet loss" in result.stdout
        self.event("test_result", name=f"ping-{source}-{target}-{size}", status="pass" if success else "fail")
        if success:
            for _ in range(count):
                self.event("packet_delivered", node=target)
        if check and not success:
            raise AssertionError(f"ping did not deliver every probe: {result.stdout} {result.stderr}")
        return result

    def link(self, a, b, state):
        self.ip(f"r{a}", "link", "set", f"to{b}", state)
        self.ip(f"r{b}", "link", "set", f"to{a}", state)
        edge = tuple(sorted((a, b)))
        if state == "up":
            self.active_links.add(edge)
        else:
            self.active_links.discard(edge)

    def record(self):
        (self.output / "resources.json").write_text(json.dumps({"tag": self.tag, "namespaces": self.names,
            "processes": self.ownership, "cleaned": self.cleaned}, indent=2) + "\n")

    def close(self):
        errors = []
        daemon_errors = []
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode != 0:
                daemon_errors.append(f"router PID {process.pid} exited {process.returncode}; inspect sanitizer logs")
        for logfile in self.logs:
            logfile.close()
        for name in reversed(self.names):
            # Check for extra processes before deleting the namespace. Never kill unknown PIDs.
            occupants = run("ip", "netns", "pids", name, check=False).stdout.strip()
            if occupants:
                errors.append(f"unexpected processes in {name}: {occupants}")
            result = run("ip", "netns", "del", name, check=False)
            if result.returncode:
                errors.append(f"could not delete namespace {name}: {result.stderr.strip()}")
        remaining = run("ip", "netns", "list").stdout.splitlines()
        if any(line.split()[0] in self.names for line in remaining):
            errors.append("owned namespaces remain")
        if any(process.poll() is None for process in self.processes):
            errors.append("owned router processes remain")
        self.cleaned = not errors
        self.record()
        if errors or daemon_errors:
            raise RuntimeError("; ".join(errors + daemon_errors))

    def __exit__(self, *_):
        self.close()


def demo(binary, output):
    with Lab(binary, output) as lab:
        print("Before failure: h1 -> r1 -> r2 -> r4 -> h4", flush=True)
        print(lab.ping().stdout, end="")
        start = time.monotonic()
        failure_id = lab.failure(2, 4)
        lab.link(2, 4, "down")
        wait_for(lab.routes_agree, timeout=8, description="all alternate paths")
        lab.converged(failure_id)
        elapsed = time.monotonic() - start
        print(f"After failure: h1 -> r1 -> r3 -> r4 -> h4 (routes changed in {elapsed:.3f}s)", flush=True)
        print(lab.ping().stdout, end="")
        lab.link(2, 4, "up")
        wait_for(lab.routes_agree, description="all restored shortest paths")
        print("Restored shorter path", flush=True)
        print(lab.ping().stdout, end="")
    print(f"Cleaned all six namespaces and four daemons. Logs: {Path(output).resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default="build/routerd")
    parser.add_argument("--output", default="build/lab")
    args = parser.parse_args()
    def interrupt(signum, _frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    signal.signal(signal.SIGTERM, interrupt)
    demo(args.binary, args.output)
