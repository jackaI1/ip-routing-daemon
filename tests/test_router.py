import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from router_sim import Network


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.net = Network([('A', 'B'), ('B', 'D'), ('A', 'C'), ('C', 'E'), ('E', 'D')])

    def test_initial_forwarding_and_failed_link_recovery(self):
        self.assertEqual(self.net.send('A', 'D'), ['A', 'B', 'D'])
        self.net.fail_link('B', 'D', 'first')
        self.assertIsNone(self.net.send('A', 'D'))
        self.net.settle()
        self.assertEqual(self.net.send('A', 'D'), ['A', 'C', 'E', 'D'])
        complete = [e for e in self.net.events if e['event'] == 'routes_converged']
        self.assertEqual(complete[0]['t_ms'], 10)
        self.assertEqual(complete[0]['failure_id'], 'first')

    def test_updates_travel_one_hop_per_tick(self):
        net = Network([('A', 'B'), ('B', 'C'), ('C', 'D'), ('D', 'E')])
        net.fail_link('D', 'E', 'chain')
        net.tick()
        self.assertIn('E', net.tables['A'])
        self.assertFalse(any(e['event'] == 'routes_converged' for e in net.events))
        net.settle()
        self.assertNotIn('E', net.tables['A'])
        self.assertEqual(net.events[-1]['t_ms'], 30)

    def test_partition_still_converges_and_reports_no_route(self):
        self.net.fail_link('B', 'D', 'first')
        self.net.settle()
        self.net.fail_link('A', 'C', 'second')
        self.net.settle()
        self.assertIsNone(self.net.send('A', 'D'))
        self.assertEqual(self.net.events[-1]['reason'], 'no_route')
        self.assertEqual(len([e for e in self.net.events if e['event'] == 'routes_converged']), 2)

    def test_bad_packet_and_hop_limit_are_distinct(self):
        self.assertIsNone(self.net.send('A', 'unknown'))
        self.assertIsNone(self.net.send('A', 'D', ttl=1))
        self.assertEqual([e['reason'] for e in self.net.events], ['unknown_destination', 'ttl_expired'])
        self.assertEqual(self.net.events[0]['event'], 'malformed_packet')

    def test_rejects_invalid_failures_without_logging_them(self):
        with self.assertRaises(ValueError):
            self.net.fail_link('A', 'D', 'bad')
        self.net.fail_link('B', 'D', 'one')
        with self.assertRaises(ValueError):
            self.net.fail_link('A', 'C', 'one')
        with self.assertRaises(ValueError):
            self.net.fail_link('B', 'D', 'two')
        self.assertEqual(len(self.net.events), 1)

    def test_overlapping_failures_are_correlated_individually(self):
        self.net.fail_link('B', 'D', 'one')
        self.net.fail_link('A', 'C', 'two')
        self.net.settle()
        completed = [e['failure_id'] for e in self.net.events if e['event'] == 'routes_converged']
        self.assertEqual(completed, ['one', 'two'])

    def test_queued_update_cannot_cross_a_newly_failed_link(self):
        net = Network([('A', 'B'), ('B', 'C'), ('C', 'D'), ('D', 'E'), ('E', 'B')])
        net.fail_link('A', 'B', 'first')
        net.fail_link('B', 'C', 'second')
        net.tick()
        self.assertNotIn(('A', 'B'), net.known['C'])
        net.settle()
        self.assertIn(('A', 'B'), net.known['C'])
        self.assertEqual(net.events[-1]['t_ms'], 30)

    def test_timeout_never_claims_convergence(self):
        self.net.fail_link('B', 'D', 'one')
        with self.assertRaises(TimeoutError):
            self.net.settle(max_ticks=0)
        self.assertFalse(any(e['event'] == 'routes_converged' for e in self.net.events))

    def test_demo_writes_actual_events_and_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run.jsonl'
            subprocess.run([sys.executable, 'router_sim.py', '--log', str(path)], check=True, capture_output=True)
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len([e for e in events if e['event'] == 'link_failure']), 2)
        results = [e for e in events if e['event'] == 'test_result']
        self.assertEqual(len(results), 4)
        self.assertTrue(all(e['status'] == 'pass' for e in results))
        self.assertEqual({e.get('reason') for e in events if e['event'] == 'packet_drop'},
                         {'forwarding_loop', 'no_route', 'ttl_expired'})


if __name__ == '__main__':
    unittest.main()
