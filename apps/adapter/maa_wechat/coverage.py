"""Audit a specific contact scan without changing it."""
import argparse
import json
from pathlib import Path
import sqlite3

parser=argparse.ArgumentParser()
parser.add_argument('scan')
args=parser.parse_args()
root=Path(__file__).resolve().parent
db=sqlite3.connect(f'file:{(root/"data"/"wechat.sqlite3").as_posix()}?mode=ro',uri=True)
rows=list(db.execute('SELECT contact_id,list_name,profile_path FROM contact_observations WHERE scan_id=?',(args.scan,)))
names={}
for cid,name,path in rows: names.setdefault(cid,set()).add(name)
positions=list(db.execute('SELECT ordinal,contact_id FROM contact_positions WHERE scan_id=? ORDER BY ordinal',(args.scan,)))
report={'observations':len(rows),'independent_ids':len(names),'position_count':len(positions),'positions_contiguous': [r[0] for r in positions]==list(range(len(positions))),'position_contact_ids':len(set(r[1] for r in positions)),'different_list_names_same_id':{k:sorted(v) for k,v in names.items() if len(v)>1},'scan':db.execute('SELECT status,expected,observed,detail FROM scans WHERE id=?',(args.scan,)).fetchone()}
print(json.dumps(report,ensure_ascii=True,indent=2))
db.close()
