"""Real ICMP crosses the C++ data plane; Linux has no remote host routes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("topology", ROOT / "lab/topology.py")
topology = importlib.util.module_from_spec(spec)
spec.loader.exec_module(topology)

REASON = "requires Linux root, iproute2, ping, and /dev/net/tun"
AVAILABLE = (sys.platform == "linux" and os.geteuid() == 0 and shutil.which("ip")
             and shutil.which("ping") and Path("/dev/net/tun").exists())
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not AVAILABLE, reason=REASON)]


@pytest.fixture
def lab(tmp_path):
    with topology.Lab(ROOT / "build/routerd", tmp_path) as network:
        yield network
    assert json.loads((tmp_path / "resources.json").read_text())["cleaned"]


def test_bidirectional_lpm_and_fragmentation(lab):
    assert lab.route(1, "10.4.0.0/16", 2)
    assert lab.route(1, "10.4.0.0/24", 2)
    lab.ping()
    lab.ping("h4", "10.1.0.2")
    # Payload exceeds the 1400-byte TUN MTU and must survive as IPv4 fragments.
    lab.ping(size=3000)
    topology.wait_for(lambda: lab.status(2).get("forwarded", 0) > 0,
                      description="C++ middle-router forwarding counter")
    routes = lab.ip("r1", "-j", "route", "show").stdout
    assert not any(r.get("dst") == "10.4.0.0/24" for r in json.loads(routes))


def test_link_failure_and_restoration(lab, record_property):
    lab.ping()
    started = time.monotonic()
    lab.link(2, 4, "down")
    topology.wait_for(lambda: lab.route(1, "10.4.0.0/24", 3) and lab.route(4, "10.1.0.0/24", 3),
                      timeout=8, description="reroute in both directions")
    elapsed = time.monotonic() - started
    record_property("convergence_seconds", round(elapsed, 3))
    print(f"failure convergence: {elapsed:.3f}s", flush=True)
    assert elapsed < 8
    lab.ping()
    lab.ping("h4", "10.1.0.2")
    topology.wait_for(lambda: lab.status(3).get("forwarded", 0) > 0,
                      description="alternate-router forwarding counter")
    lab.link(2, 4, "up")
    topology.wait_for(lambda: lab.route(1, "10.4.0.0/24", 2) and lab.route(4, "10.1.0.0/24", 2),
                      description="restored shortest paths")
    lab.ping()


def test_partition_withdraws_routes(lab):
    lab.link(1, 2, "down")
    lab.link(1, 3, "down")
    topology.wait_for(lambda: not any(r["prefix"] == "10.4.0.0/24" for r in lab.status(1)["routes"]),
                      description="unreachable route withdrawal")
    assert lab.ping(count=1, check=False).returncode != 0
    lab.link(1, 3, "up")
    topology.wait_for(lambda: lab.route(1, "10.4.0.0/24", 3) and lab.route(4, "10.1.0.0/24", 3),
                      description="partition recovery")
    lab.ping()


def test_daemon_loss_expires_advertisement(lab):
    process = lab.processes[1]  # r2
    process.terminate()
    assert process.wait(timeout=5) == 0
    topology.wait_for(lambda: lab.route(1, "10.4.0.0/24", 3), description="daemon-loss reroute")
    topology.wait_for(lambda: not any(r["prefix"] == "10.4.0.0/16" for r in lab.status(1)["routes"]),
                      description="expired origin withdrawal")
    lab.ping()


def test_malformed_peer_packets_do_not_crash(lab):
    # Temporarily replace the owned r2 daemon with a short-lived socket at its address.
    # This reaches the daemon's configured-peer parser, not only its unknown-peer filter.
    lab.processes[1].terminate()
    assert lab.processes[1].wait(timeout=5) == 0
    lab.exec("r2", sys.executable, "-c", """
import socket, struct
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('172.16.12.2', 47000))
s.sendto(b'JRT\\x01\\x02', ('172.16.12.1', 47000))
p = bytearray(20)
p[0] = 0x45; p[3] = 20; p[8] = 1; p[9] = 1
p[12:16] = socket.inet_aton('10.1.0.2'); p[16:20] = socket.inet_aton('10.4.0.2')
words = struct.unpack('!10H', p)
c = sum(words)
while c >> 16: c = (c & 65535) + (c >> 16)
p[10:12] = struct.pack('!H', (~c) & 65535)
s.sendto(b'JRT\\x01\\x03' + p, ('172.16.12.1', 47000))
s.close()
""")
    log = lab.output / "r1.log"
    topology.wait_for(lambda: 'malformed_lsa' in log.read_text() and 'ttl_expired' in log.read_text(),
                      description="validated drop reasons")
    topology.wait_for(lambda: lab.route(1, "10.4.0.0/24", 3), description="healthy alternate route")
    lab.ping()


def test_partial_start_failure_cleans_resources(tmp_path):
    binary = tmp_path / "fails"
    binary.write_text("#!/bin/sh\nexit 42\n")
    binary.chmod(0o755)
    lab = topology.Lab(binary, tmp_path / "failed-lab")
    with pytest.raises((RuntimeError, FileNotFoundError)):
        with lab:
            pytest.fail("failing binary unexpectedly started")
    # A deliberately failing daemon is reported, but resources still must be removed.
    assert all(p.poll() is not None for p in lab.processes)
    names = subprocess.run(["ip", "netns", "list"], check=True, text=True, capture_output=True).stdout
    assert lab.tag not in names
    assert json.loads((lab.output / "resources.json").read_text())["cleaned"]
