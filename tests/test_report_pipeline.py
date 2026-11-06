"""Exercise the producer and Perl CLI together; independently read CSV with Python."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReportPipelineTests(unittest.TestCase):
    def test_generated_events_become_measured_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, text, csv_path = [Path(tmp) / name for name in ('events.jsonl', 'report.txt', 'report.csv')]
            subprocess.run([sys.executable, 'router_sim.py', '--log', str(log)], check=True, capture_output=True)
            result = subprocess.run(['perl', 'tools/routing-report.pl', '--strict', '--text', str(text),
                                     '--csv', str(csv_path), str(log)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            with csv_path.open() as stream:
                rows = list(csv.DictReader(stream))
            recoveries = [row for row in rows if row['record_type'] == 'convergence']
            self.assertEqual([(r['link'], r['convergence_ms']) for r in recoveries], [('B-D', '10'), ('A-C', '20')])
            self.assertEqual({r['reason']: r['count'] for r in rows if r['record_type'] == 'packet'},
                             {'unknown_destination': '1', 'forwarding_loop': '1', 'no_route': '1', 'ttl_expired': '1'})
            checks = [r for r in rows if r['record_type'] == 'test']
            self.assertEqual(len(checks), 4)
            self.assertTrue(all(r['status'] == 'pass' for r in checks))
            self.assertIn('rejected lines: 0', text.read_text())

    def test_independent_files_and_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = [root / 'a,"run".jsonl', root / 'b.jsonl']
            events = [dict(event='link_failure', t_ms=0, failure_id='f1', link='A-B'),
                      dict(event='packet_drop', t_ms=1, node='A', reason='bad,"quoted"'),
                      dict(event='routes_converged', t_ms=5, failure_id='f1')]
            for log in logs:
                log.write_text(''.join(json.dumps(e) + '\n' for e in events))
            csv_path = root / 'report.csv'
            subprocess.run(['perl', 'tools/routing-report.pl', '--strict', '--text', str(root / 'report.txt'),
                            '--csv', str(csv_path), *map(str, logs)], check=True, capture_output=True)
            with csv_path.open() as stream:
                rows = list(csv.DictReader(stream))
            recoveries = [r for r in rows if r['record_type'] == 'convergence']
            self.assertEqual([(r['source'], r['convergence_ms']) for r in recoveries], [(str(p), '5') for p in logs])
            self.assertEqual([r['reason'] for r in rows if r['record_type'] == 'packet'], ['bad,"quoted"'] * 2)

    def test_strict_mode_still_saves_damaged_log_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'damaged.jsonl'
            log.write_text('garbage\n{"event":')
            result = subprocess.run(['perl', 'tools/routing-report.pl', '--strict', '--text', str(root / 'report.txt'),
                                     '--csv', str(root / 'report.csv'), str(log)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertTrue((root / 'report.csv').is_file())
            self.assertIn('invalid_json: 2', (root / 'report.txt').read_text())


if __name__ == '__main__':
    unittest.main()
