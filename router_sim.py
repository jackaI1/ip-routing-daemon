#!/usr/bin/env python3
"""Deterministic link-state routing lab. No host routes or sockets are changed."""
import argparse
from collections import deque
import json
from pathlib import Path


class Network:
    """Flood failed links one hop per tick; each router computes its own routes."""

    def __init__(self, links):
        self.links = {tuple(sorted(edge)) for edge in links}
        if not self.links or any(len(e) != 2 or e[0] == e[1] for e in self.links):
            raise ValueError('provide at least one non-loop link')
        self.nodes = sorted({node for edge in self.links for node in edge})
        self.failed = set()
        self.known = {node: set() for node in self.nodes}
        self.tables = {node: self._routes(node, set()) for node in self.nodes}
        self.pending = []
        self.failures = {}
        self.events = []
        self.t_ms = 0

    def _emit(self, event, **fields):
        self.events.append(dict(event=event, t_ms=self.t_ms, **fields))

    def _neighbors(self, node, failed):
        return sorted(b if a == node else a for a, b in self.links - failed if node in (a, b))

    def _routes(self, start, failed):
        # Breadth-first search finds shortest paths because every link costs one hop.
        routes = {start: (start, 0)}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            for neighbor in self._neighbors(node, failed):
                if neighbor not in routes:
                    routes[neighbor] = (neighbor if node == start else routes[node][0], routes[node][1] + 1)
                    queue.append(neighbor)
        return routes

    def fail_link(self, a, b, failure_id):
        edge = tuple(sorted((a, b)))
        if edge not in self.links or edge in self.failed or failure_id in self.failures or not failure_id:
            raise ValueError('failure needs a live link and an unused nonempty ID')
        self.failed.add(edge)
        self.failures[failure_id] = False
        self._emit('link_failure', failure_id=failure_id, link='-'.join(edge))
        for node in edge:
            self.known[node].add(edge)
            self.tables[node] = self._routes(node, self.known[node])
            self.pending.extend((node, neighbor, edge) for neighbor in self._neighbors(node, self.failed))

    def tick(self):
        self.t_ms += 10
        incoming, self.pending = self.pending, []
        for sender, node, edge in incoming:
            if tuple(sorted((sender, node))) in self.failed:
                continue
            if edge in self.known[node]:
                continue
            self.known[node].add(edge)
            self.tables[node] = self._routes(node, self.known[node])
            self.pending.extend((node, neighbor, edge) for neighbor in self._neighbors(node, self.failed))
        # This observer uses the actual surviving graph, independently of local views.
        if all(self.tables[node] == self._routes(node, self.failed) for node in self.nodes):
            for failure_id, complete in self.failures.items():
                if not complete:
                    self._emit('routes_converged', failure_id=failure_id)
                    self.failures[failure_id] = True

    def settle(self, max_ticks=100):
        for _ in range(max_ticks):
            self.tick()
            if all(self.failures.values()):
                return
        raise TimeoutError('routing tables did not converge within the tick limit')

    def send(self, source, destination, ttl=16):
        if source not in self.tables or destination not in self.tables:
            self._emit('malformed_packet', node=source, reason='unknown_destination')
            return None
        path, node = [source], source
        while node != destination:
            if ttl <= 0:
                self._emit('packet_drop', node=node, reason='ttl_expired')
                return None
            route = self.tables[node].get(destination)
            if route is None:
                self._emit('packet_drop', node=node, reason='no_route')
                return None
            neighbor = route[0]
            if tuple(sorted((node, neighbor))) in self.failed:
                self._emit('packet_drop', node=node, reason='link_down')
                return None
            if neighbor in path:
                self._emit('packet_drop', node=node, reason='forwarding_loop')
                return None
            node = neighbor
            path.append(node)
            ttl -= 1
        self._emit('packet_delivered', node=node)
        return path


def run_demo():
    net = Network([('A', 'B'), ('B', 'D'), ('A', 'C'), ('C', 'E'), ('E', 'D')])
    checks = []

    def check(name, passed):
        checks.append(passed)
        net._emit('test_result', name=name, status='pass' if passed else 'fail')

    check('initial_shortest_path', net.send('A', 'D') == ['A', 'B', 'D'])
    net.send('A', 'D', ttl=1)
    net.send('A', 'unknown')
    net.fail_link('B', 'D', 'failure-1')
    check('drop_before_update', net.send('A', 'D') is None)
    net.settle()
    check('alternate_path', net.send('A', 'D') == ['A', 'C', 'E', 'D'])
    net.fail_link('A', 'C', 'failure-2')
    net.settle()
    check('partition_has_no_route', net.send('A', 'D') is None)
    return net.events, all(checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, required=True, help='JSON Lines output file (parent must exist)')
    args = parser.parse_args()
    events, passed = run_demo()
    with args.log.open('w', encoding='utf-8') as output:
        for event in events:
            output.write(json.dumps(event, sort_keys=True) + '\n')
    print(f'{len(events)} events written to {args.log}; scenario checks: {"PASS" if passed else "FAIL"}')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
