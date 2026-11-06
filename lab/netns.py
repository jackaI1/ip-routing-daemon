#!/usr/bin/env python3
"""Four real C++ routers. Requires Linux root, iproute2, ping and Python 3."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def run(*args, **kwargs):
    kwargs.setdefault('timeout', 10)
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def cleanup_resources(processes, handles, created, owned_links, output, tag):
    """Try every owned resource even if another cleanup operation fails."""
    errors = []

    def attempt(label, action):
        try:
            return action()
        except Exception as error:
            errors.append(f'{label}: {type(error).__name__}: {str(error)[:300]}')
            return None

    for process in reversed(processes):
        if process.poll() is None:
            attempt(f'resume PID {process.pid}', lambda p=process: p.send_signal(signal.SIGCONT))
            attempt(f'terminate PID {process.pid}', process.terminate)
    for process in reversed(processes):
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            attempt(f'kill PID {process.pid}', process.kill)
            attempt(f'wait PID {process.pid}', lambda p=process: p.wait(timeout=3))
        except Exception as error:
            errors.append(f'wait PID {process.pid}: {error}')
    for handle in handles:
        attempt('close log', handle.close)
    for name in reversed(created):
        attempt(f'delete namespace {name}', lambda n=name: run('ip', 'netns', 'del', n))
    remaining_links = []
    for name in owned_links:
        # A setup failure can leave a not-yet-moved veth in the root namespace.
        shown = attempt(f'inspect setup link {name}', lambda n=name: subprocess.run(
            ['ip', 'link', 'show', n], capture_output=True, timeout=5))
        if shown is not None and shown.returncode == 0:
            attempt(f'delete setup link {name}', lambda n=name: run('ip', 'link', 'del', n))
        # Verification must run even when deletion failed. Unknown state is conservative.
        shown = attempt(f'verify setup link {name}', lambda n=name: subprocess.run(
            ['ip', 'link', 'show', n], capture_output=True, timeout=5))
        if shown is None or shown.returncode == 0:
            remaining_links.append(name)
    listed = attempt('verify namespaces', lambda: run('ip', 'netns', 'list'))
    names = {line.split()[0] for line in listed.stdout.splitlines() if line.split()} if listed else set(created)
    remaining_ns = [name for name in created if name in names]
    remaining_pids = [p.pid for p in processes if p.poll() is None]
    remaining = len(remaining_ns) + len(remaining_pids) + len(remaining_links)
    cleanup = {'processes_stopped': len(processes) - len(remaining_pids),
               'namespaces_removed': len(created) - len(remaining_ns),
               'remaining_owned': remaining, 'remaining_namespaces': remaining_ns,
               'remaining_pids': remaining_pids, 'remaining_links': remaining_links,
               'errors': errors, 'udp_port': 47000, 'tcp_port': 18080, 'scope': tag}
    (output / 'cleanup.json').write_text(json.dumps(cleanup, indent=2) + '\n')
    if remaining or errors:
        raise RuntimeError(f'cleanup: {remaining} resources remain; {len(errors)} errors; see {output / "cleanup.json"}')
    print(f'Cleanup verified: {len(processes)} processes stopped, {len(created)} namespaces removed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', default='build/routerd')
    parser.add_argument('--output', default='reports/netns')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('run with sudo on Linux (or the documented privileged container)')
    binary = str(Path(args.binary).resolve())
    if not Path(binary).is_file():
        parser.error('build the router with make first')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    tag = 'iprd-' + uuid.uuid4().hex[:8]
    namespaces = {i: f'{tag}-{i}' for i in range(1, 5)}
    created, processes, handles, owned_links = [], [], [], []
    checks = []
    ownership = []

    def ns(i, *command, **kwargs):
        return run('ip', 'netns', 'exec', namespaces[i], *command, **kwargs)

    def state(i):
        try:
            return json.loads((output / f'r{i}.json').read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def wait_for(label, predicate, timeout=12):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            for process in processes[:4]:
                if process.poll() is not None:
                    raise AssertionError(f'router stopped during {label}: {process.returncode}')
            if predicate():
                elapsed = round((time.monotonic() - start) * 1000)
                checks.append({'check': label, 'elapsed_ms': elapsed})
                print(f'PASS {label} ({elapsed} ms)', flush=True)
                return elapsed
            time.sleep(.05)
        raise AssertionError(f'timed out: {label}')

    def route(i, prefix, next_hop):
        return any(r['prefix'] == prefix and r['next_hop'] == next_hop for r in state(i).get('routes', []))

    def ping():
        return ns(1, 'ping', '-n', '-c', '3', '-W', '2', '10.200.4.1').stdout

    def spawn(i, command, log_name):
        log = open(output / log_name, 'w')
        handles.append(log)
        process = subprocess.Popen(['ip', 'netns', 'exec', namespaces[i], *command], stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)
        ownership.append({'pid': process.pid, 'start_ticks': Path(f'/proc/{process.pid}/stat').read_text().split()[21],
                          'namespace': namespaces[i], 'command': command, 'cleanup': f'kill -TERM {process.pid}'})
        (output / 'ownership.json').write_text(json.dumps(ownership, indent=2) + '\n')
        return process

    def tcp_transfer():
        # A fresh connection each phase verifies a real kernel TCP handshake and payload.
        client = """import hashlib,socket
payload=b'route-me'*131072
with socket.create_connection(('10.200.4.1',18080),timeout=8) as s:
 s.settimeout(8); s.sendall(payload); s.shutdown(socket.SHUT_WR)
 digest=b''
 while True:
  b=s.recv(100)
  if not b: break
  digest+=b
 assert digest.decode()==hashlib.sha256(payload).hexdigest(),digest
print(len(payload))
"""
        size = int(ns(1, sys.executable, '-c', client, timeout=20).stdout.strip())
        checks.append({'check': 'TCP SHA-256 payload verified', 'bytes': size})
        print(f'PASS TCP payload verified ({size} bytes)', flush=True)

    # On SIGTERM/CI cancellation, unwind through finally and remove only our objects.
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'signal {signum}')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix=tag) as configdir:
            for i, name in namespaces.items():
                run('ip', 'netns', 'add', name)
                created.append(name)
                ns(i, 'ip', 'link', 'set', 'lo', 'up')
                ns(i, 'sysctl', '-qw', 'net.ipv4.ip_forward=0')
                ns(i, 'sysctl', '-qw', 'net.ipv4.conf.all.rp_filter=0')
                ns(i, 'sysctl', '-qw', 'net.ipv4.conf.default.rp_filter=0')
            peers = {i: [] for i in namespaces}
            # Lower costs select 1-2-4; 1-3-4 remains available after 2-4 fails.
            for index, (a, b, cost) in enumerate([(1, 2, 1), (2, 4, 1), (1, 3, 5), (3, 4, 5)], 1):
                va, vb = f'{tag[-8:]}{a}{b}a', f'{tag[-8:]}{a}{b}b'
                run('ip', 'link', 'add', va, 'type', 'veth', 'peer', 'name', vb)
                owned_links.extend([va, vb])
                run('ip', 'link', 'set', va, 'netns', namespaces[a])
                run('ip', 'link', 'set', vb, 'netns', namespaces[b])
                for router, iface, suffix in [(a, va, 1), (b, vb, 2)]:
                    ns(router, 'ip', 'addr', 'add', f'172.30.{index}.{suffix}/30', 'dev', iface)
                    ns(router, 'ip', 'link', 'set', iface, 'up')
                peers[a].append(f'peer {b} 172.30.{index}.1 172.30.{index}.2 {cost}')
                peers[b].append(f'peer {a} 172.30.{index}.2 172.30.{index}.1 {cost}')
                if (a, b) == (2, 4):
                    failed_iface = va
            for i in namespaces:
                config = Path(configdir) / f'{i}.conf'
                config.write_text(f'router {i}\ntun tun0\nstatus {output / f"r{i}.json"}\nprefix 10.200.{i}.0/24\n' + '\n'.join(' '.join(line.split()[:2] + line.split()[3:]) for line in peers[i]) + '\n')
                (output / f'r{i}.json').unlink(missing_ok=True)
                spawn(i, [binary, str(config)], f'r{i}.log')
            for i in namespaces:
                wait_for(f'router {i} TUN ready', lambda i=i: bool(state(i)))
                ns(i, 'ip', 'addr', 'add', f'10.200.{i}.1/24', 'dev', 'tun0')
                ns(i, 'ip', 'link', 'set', 'tun0', 'mtu', '1400', 'up')
                ns(i, 'ip', 'route', 'add', '10.200.0.0/16', 'dev', 'tun0', 'src', f'10.200.{i}.1')
                assert ns(i, 'sysctl', '-n', 'net.ipv4.ip_forward').stdout.strip() == '0'
            checks.append({'check': 'kernel forwarding disabled', 'routers': 4})
            print('PASS kernel forwarding disabled in all four routers', flush=True)
            server_code = """import socket,hashlib
with socket.socket() as listener:
 listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 listener.bind(('10.200.4.1',18080)); listener.listen(8)
 while True:
  c,_=listener.accept()
  with c:
   h=hashlib.sha256()
   while True:
    b=c.recv(65536)
    if not b: break
    h.update(b)
   c.sendall(h.hexdigest().encode())
"""
            server = spawn(4, [sys.executable, '-u', '-c', server_code], 'tcp-server.log')
            wait_for('initial path 1-2-4', lambda: route(1, '10.200.4.0/24', 2) and route(4, '10.200.1.0/24', 2))
            # Server readiness is checked explicitly rather than relying on startup timing.
            wait_for('TCP listener ready', lambda: f':{18080:04X} ' in ns(4, 'cat', '/proc/net/tcp').stdout)
            print(ping().splitlines()[-2], flush=True)
            tcp_transfer()
            before = state(3).get('forwarded', 0)
            ns(2, 'ip', 'link', 'set', failed_iface, 'down')
            recovery = wait_for('failed link reroutes to 1-3-4', lambda: route(1, '10.200.4.0/24', 3) and route(4, '10.200.1.0/24', 3))
            print(ping().splitlines()[-2], flush=True)
            tcp_transfer()
            wait_for('alternate router forwarded real packets', lambda: state(3).get('forwarded', 0) > before)
            ns(2, 'ip', 'link', 'set', failed_iface, 'up')
            wait_for('restored link returns to 1-2-4', lambda: route(1, '10.200.4.0/24', 2) and route(4, '10.200.1.0/24', 2))
            tcp_transfer()
            # Leave veth carrier up, but stop router 2 sending hello messages.
            processes[1].send_signal(signal.SIGSTOP)
            wait_for('silent neighbor reroutes to 1-3-4', lambda: route(1, '10.200.4.0/24', 3) and route(4, '10.200.1.0/24', 3))
            tcp_transfer()
            processes[1].send_signal(signal.SIGCONT)
            wait_for('resumed neighbor returns to primary', lambda: route(1, '10.200.4.0/24', 2) and route(4, '10.200.1.0/24', 2))
            tcp_transfer()
            # Cut both of router 1's links; it must withdraw all remote routes.
            for peer_line in peers[1]:
                local = peer_line.split()[2]
                data = json.loads(ns(1, 'ip', '-j', 'addr').stdout)
                iface = next(x['ifname'] for x in data if any(a.get('local') == local for a in x.get('addr_info', [])))
                ns(1, 'ip', 'link', 'set', iface, 'down')
            wait_for('partition removes unreachable route', lambda: not any(r['prefix'] == '10.200.4.0/24' for r in state(1).get('routes', [])))
            result = subprocess.run(['ip', 'netns', 'exec', namespaces[1], 'ping', '-n', '-c', '1', '-W', '1', '10.200.4.1'], capture_output=True, text=True)
            assert result.returncode != 0, 'partition unexpectedly delivered traffic'
            checks.append({'check': 'partition rejects traffic'})
            (output / 'result.json').write_text(json.dumps({'checks': checks, 'failure_recovery_ms': recovery}, indent=2) + '\n')
            print(f'PASS {len(checks)} lab checks; failure recovery {recovery} ms', flush=True)
    finally:
        cleanup_resources(processes, handles, created, owned_links, output, tag)


if __name__ == '__main__':
    main()
