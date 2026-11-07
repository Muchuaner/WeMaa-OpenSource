import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import liveness


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        liveness.touch()

    def test_stalled_worker_is_killed_with_evidence(self):
        exited = threading.Event()
        codes = []
        def fake_exit(code):
            codes.append(code)
            exited.set()
        liveness._state['touched'] = time.monotonic() - 3600
        liveness.start(self.root, 'run-test', stall_seconds=0, check_seconds=0.01, exit=fake_exit)
        self.assertTrue(exited.wait(5))
        self.assertEqual(codes, [3])
        records = [json.loads(line) for line in
                   (self.root / 'data' / 'liveness.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[-1]['run_id'], 'run-test')
        self.assertGreaterEqual(records[-1]['stalled_seconds'], 3600 - 60)
        progress = json.loads((self.root / 'data' / 'progress.json').read_text(encoding='utf-8'))
        self.assertEqual(progress['status'], 'stalled')

    def test_fresh_touches_never_kill(self):
        codes = []
        def fake_exit(code):
            codes.append(code)
        liveness.start(self.root, 'run-fresh', stall_seconds=3600, check_seconds=0.01, exit=fake_exit)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            liveness.touch()
            time.sleep(0.02)
        self.assertEqual(codes, [])


if __name__ == '__main__':
    unittest.main()
