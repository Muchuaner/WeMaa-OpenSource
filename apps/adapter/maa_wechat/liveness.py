"""Hard-kill a frozen worker; Task Scheduler restarts it with durable state.

Maa .wait() calls have no timeout. When the controller hangs inside native
code no in-process handler can run, so a daemon thread compares the last
liveness touch and exits the whole process. Queues, receipts and evidence
live in sqlite; an interrupted send stays uncertain and is never replayed.
"""
import json
import os
import threading
import time
from pathlib import Path

STALL_SECONDS = 600
CHECK_SECONDS = 5
_state = {'touched': time.monotonic()}


def touch():
    _state['touched'] = time.monotonic()


def start(root, run_id, *, stall_seconds=STALL_SECONDS, check_seconds=CHECK_SECONDS, exit=os._exit):
    root = Path(root)
    def supervise():
        while True:
            time.sleep(check_seconds)
            stalled = time.monotonic() - _state['touched']
            if stalled <= stall_seconds:
                continue
            record = {'time': time.time(), 'run_id': run_id, 'stalled_seconds': round(stalled)}
            try:
                (root / 'data').mkdir(exist_ok=True)
                with (root / 'data' / 'liveness.jsonl').open('a', encoding='utf-8') as output:
                    output.write(json.dumps(record, ensure_ascii=False) + '\n')
                from flows import progress
                progress(root, {'mode': 'watchdog', 'run_id': run_id, 'status': 'stalled',
                                'stalled_seconds': record['stalled_seconds']})
            except Exception:
                pass
            exit(3)
            return
    threading.Thread(target=supervise, daemon=True, name='liveness').start()
