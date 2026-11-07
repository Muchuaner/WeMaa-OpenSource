import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
from state_io import atomic_json

class AtomicTests(unittest.TestCase):
    def test_windows_temporary_read_lock_retries_and_keeps_json_complete(self):
        import os
        real=os.replace;calls=[]
        def replace(source,target):
            calls.append(str(source))
            if len(calls)<3:raise PermissionError('share lock')
            return real(source,target)
        with tempfile.TemporaryDirectory() as folder,patch('state_io.os.replace',side_effect=replace),patch('state_io.time.sleep'):
            p=Path(folder)/'progress.json';p.write_text('{"old":1}')
            self.assertTrue(atomic_json(p,{'status':'polling_paused'}))
            self.assertEqual(json.loads(p.read_text()),{'status':'polling_paused'})
            self.assertFalse(list(p.parent.glob('*.tmp')))
    def test_busy_telemetry_keeps_previous_state_without_killing_worker(self):
        with tempfile.TemporaryDirectory() as folder,patch('state_io.os.replace',side_effect=PermissionError('busy')),patch('state_io.time.sleep'):
            p=Path(folder)/'progress.json';p.write_text('{"old":1}')
            self.assertFalse(atomic_json(p,{'new':1},best_effort=True))
            self.assertEqual(json.loads(p.read_text()),{'old':1})
            self.assertFalse(list(p.parent.glob('*.tmp')))
            with self.assertRaises(PermissionError):atomic_json(p,{'critical':1})
