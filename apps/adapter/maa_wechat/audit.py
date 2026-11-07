import json
from pathlib import Path
import sqlite3

root=Path(__file__).resolve().parent
db=sqlite3.connect(f'file:{(root/"data"/"wechat.sqlite3").as_posix()}?mode=ro',uri=True)
report={
    'contacts':db.execute('SELECT count(*) FROM contacts').fetchone()[0],
    'contacts_with_ocr_wechat_id':db.execute('SELECT count(*) FROM contacts WHERE wechat_id IS NOT NULL').fetchone()[0],
    'contact_observations':db.execute('SELECT count(*) FROM contact_observations').fetchone()[0],
    'scans':list(db.execute('SELECT id,status,expected,observed FROM scans ORDER BY started DESC LIMIT 5')),
    'conversations':db.execute('SELECT count(*) FROM conversations').fetchone()[0],
    'snapshots':db.execute('SELECT count(*) FROM snapshots').fetchone()[0],
    'new_messages':db.execute('SELECT count(*) FROM messages').fetchone()[0],
    'events':list(db.execute('SELECT kind,count(*) FROM events GROUP BY kind')),
}
print(json.dumps(report,ensure_ascii=False,indent=2))
db.close()
