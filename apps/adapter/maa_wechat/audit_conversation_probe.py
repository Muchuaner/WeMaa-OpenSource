"""Read-only context dump for a single acceptance-test conversation."""
import argparse,json,re,sqlite3
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('conversation')
args=parser.parse_args()
if not re.fullmatch('[a-f0-9]{64}',args.conversation): raise ValueError('INVALID_CONVERSATION')
root=Path(__file__).resolve().parent
db=sqlite3.connect(f'file:{(root/"data"/"wechat.sqlite3").as_posix()}?mode=ro',uri=True)
db.execute('PRAGMA query_only=ON')
rows=[dict(snapshot_id=sid,status=status,seen=seen,frame=frame,payloads=json.loads(payloads)) for sid,status,seen,frame,payloads in db.execute('''SELECT s.id,s.status,s.seen,s.frame_path,p.payloads_json FROM snapshots s JOIN snapshot_payloads p ON p.snapshot_id=s.id WHERE s.conversation_id=? ORDER BY s.id DESC LIMIT 10''',(args.conversation,))]
print(json.dumps(rows,ensure_ascii=True,indent=2))
db.close()
