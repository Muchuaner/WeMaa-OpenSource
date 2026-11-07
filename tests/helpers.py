from copy import deepcopy
import json
from types import SimpleNamespace

class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, **_kw):
        self.values[key] = value
        return True

    def mget(self, keys):
        return [self.get(key) for key in keys]

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def ltrim(self, key, start, end):
        self.lists[key] = self.lrange(key, start, end)

    def expire(self, *_args):
        return True

    def pipeline(self):
        return MemoryPipeline(self)

class MemoryPipeline:
    def __init__(self, cache):
        self.cache = cache
        self.transaction = False
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def watch(self, *_args):
        self.transaction = False
        self.pending = []

    def multi(self):
        self.transaction = True

    def execute(self):
        results = [getattr(self.cache, name)(*args, **kwargs) for name, args, kwargs in self.pending]
        self.pending = []
        self.transaction = False
        return results

    def __getattr__(self, name):
        def command(*args, **kwargs):
            if self.transaction:
                self.pending.append((name, args, kwargs))
                return self
            return getattr(self.cache, name)(*args, **kwargs)
        return command

def observation(**updates):
    row = {"schema_version": 1, "gateway_id": "win-wechat-maa", "collected_at": 1000,
           "runtime": {"worker_state": "Ready", "trial_state": "Ready", "old_gateway_state": "Disabled",
                       "progress": {}, "versions": {"maafw": "5.14.0"},
                       "layout": {"screen": [1920, 1080], "client": [1920, 1040], "divider_x": 480, "dpi": 96}},
           "summary": {"conversation_count": 1, "message_row_count": 1, "snapshot_count": 2},
           "scans": [], "contacts": [], "conversations": [], "events": [], "actions": [],
           "backups": [], "lessons": {}, "acceptance": {}}
    row.update(updates)
    return row
