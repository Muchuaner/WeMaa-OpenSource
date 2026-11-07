"""Read-only acceptance evidence for one unique message; no desktop input."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time


def compact(value): return re.sub(r'\s+', '', value)
def stamp(value): return datetime.fromtimestamp(value, timezone.utc).isoformat()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('token')
    parser.add_argument('--prefix',default='MAA0926')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.token): raise ValueError('INVALID_TOKEN')
    root=Path(__file__).resolve().parent
    db=sqlite3.connect(f'file:{(root/"data"/"wechat.sqlite3").as_posix()}?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON')
    try:
        report={'checked_utc':stamp(time.time()),'token':args.token,'snapshot_hits':[],'message_hits':[],'near_hits':[]}
        # Search readable evidence, not deduplication hashes or append-only rows.
        for sid,cid,title,status,seen,frame,raw in db.execute('''
            SELECT s.id,s.conversation_id,c.title,s.status,s.seen,s.frame_path,p.payloads_json
            FROM snapshots s JOIN snapshot_payloads p ON p.snapshot_id=s.id
            JOIN conversations c ON c.id=s.conversation_id ORDER BY s.id'''):
            payloads=json.loads(raw)
            matches=[(i,p) for i,p in enumerate(payloads) if args.token in compact(p.get('text',''))]
            near=[(i,p) for i,p in enumerate(payloads) if args.prefix in compact(p.get('text',''))]
            item={'snapshot_id':sid,'conversation_id':cid,'title':title,'status':status,'seen_utc':stamp(seen),'seen_unix':seen,'frame':frame,'payload_count':len(payloads)}
            if matches:
                report['snapshot_hits'].append(dict(item,matches=matches))
            elif near:
                report['near_hits'].append(dict(item,matches=near))
        for mid,cid,title,sid,ordinal,seen,raw in db.execute('''
            SELECT m.id,m.conversation_id,c.title,m.snapshot_id,m.ordinal,m.seen,m.payload_json
            FROM messages m JOIN conversations c ON c.id=m.conversation_id ORDER BY m.seen'''):
            payload=json.loads(raw)
            if args.token in compact(payload.get('text','')):
                report['message_hits'].append({'message_id':mid,'conversation_id':cid,'title':title,'snapshot_id':sid,'ordinal':ordinal,'seen_utc':stamp(seen),'payload':payload})
        owners=set(hit['conversation_id'] for hit in report['snapshot_hits'])
        report['matching_conversation_ids']=sorted(owners)
        report['matching_snapshot_count']=len(report['snapshot_hits'])
        report['incremental_message_count']=len(report['message_hits'])
        report['snapshot_status_counts']=list(db.execute('SELECT status,count(*) FROM snapshots GROUP BY status'))
        report['latest_snapshot']=db.execute('SELECT id,status,seen,frame_path FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
        report['latest_events']=[]
        for kind,raw,seen in db.execute('SELECT kind,detail_json,seen FROM events ORDER BY id DESC LIMIT 150'):
            detail=json.loads(raw)
            if detail.get('conversation') in owners or kind=='recent_cycle_complete':
                report['latest_events'].append({'kind':kind,'detail':detail,'seen_utc':stamp(seen)})
                if len(report['latest_events'])>=12: break
        print(json.dumps(report,ensure_ascii=True,indent=2))
    finally:
        db.close()


if __name__=='__main__': main()
