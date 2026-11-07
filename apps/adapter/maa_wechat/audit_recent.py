"""Read-only diagnostics for the new Maa database; no desktop input."""
import json
from pathlib import Path
import sqlite3

root=Path(__file__).resolve().parent
db=sqlite3.connect(f'file:{(root/"data"/"wechat.sqlite3").as_posix()}?mode=ro',uri=True)
report={
    'latest_events':list(db.execute('SELECT kind,detail_json FROM events ORDER BY id DESC LIMIT 12')),
    'snapshot_statuses':list(db.execute('SELECT status,count(*) FROM snapshots GROUP BY status')),
    'baseline_payloads':db.execute('SELECT count(*) FROM snapshot_payloads').fetchone()[0],
}
print(json.dumps(report,ensure_ascii=False,indent=2))
db.close()
