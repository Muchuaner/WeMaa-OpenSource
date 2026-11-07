"""Evidence-first storage; display names are never stable contact identities."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def reconcile(previous, current):
    """Use ordered overlap, preserving repeated identical messages.

    No overlap means a gap, not permission to guess which messages are new.
    Initial visible history is a baseline, never a fresh-message notification.
    """
    if previous is None: return {'status': 'baseline', 'new': [], 'overlap': 0}
    if previous == current: return {'status': 'unchanged', 'new': [], 'overlap': len(current)}
    if not current: return {'status': 'gap', 'new': [], 'overlap': 0}
    for length in range(min(len(previous), len(current)), 0, -1):
        if previous[-length:] == current[:length]:
            # A single repeated token cannot locate an occurrence reliably.
            anchor = current[:length]
            occurrences = sum(previous[i:i+length] == anchor for i in range(len(previous)-length+1))
            if occurrences != 1 or (length == 1 and current.count(anchor[0]) > 1):
                return {'status': 'ambiguous', 'new': [], 'overlap': length}
            return {'status': 'append', 'new': current[length:], 'overlap': length}
    return {'status': 'gap', 'new': [], 'overlap': 0}


def payload_tokens(payloads):
    visual_mode=any(p.get('visual') for p in payloads)
    return [digest({'direction':p.get('direction'),'sha':p['visual'].get('sha256')})
            if p.get('visual',{}).get('verified') else digest(p.get('text',''))
            for p in payloads] if visual_mode else [digest(p) for p in payloads]


def vision_functions():
    if __package__:from .message_vision import message_identity,reconcile_visual
    else:from message_vision import message_identity,reconcile_visual
    return message_identity,reconcile_visual


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,started REAL,finished REAL,status TEXT,expected INTEGER,observed INTEGER,detail TEXT);
        CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,wechat_id TEXT UNIQUE,display_name TEXT,avatar_sha TEXT,avatar_path TEXT,facts_json TEXT,last_seen REAL,identity_status TEXT);
        CREATE TABLE IF NOT EXISTS contact_observations(id INTEGER PRIMARY KEY,scan_id TEXT,page INTEGER,contact_id TEXT,list_name TEXT,profile_path TEXT,ocr_json TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS contact_positions(scan_id TEXT,ordinal INTEGER,contact_id TEXT,list_name TEXT,avatar_sha TEXT,frame_path TEXT,PRIMARY KEY(scan_id,ordinal));
        CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY,kind TEXT,owner TEXT,path TEXT,sha TEXT,ocr_json TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,title TEXT,avatar_sha TEXT,identity_status TEXT,last_seen REAL);
        CREATE TABLE IF NOT EXISTS group_aliases(alias TEXT PRIMARY KEY,canonical TEXT,reason TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS group_members(conversation_id TEXT,member TEXT,PRIMARY KEY(conversation_id,member));
        CREATE TABLE IF NOT EXISTS conversation_profiles(conversation_id TEXT PRIMARY KEY,kind TEXT,name TEXT,member_count INTEGER,row_name TEXT,header TEXT,observed_at REAL);
        CREATE TABLE IF NOT EXISTS conversation_routes(conversation_id TEXT PRIMARY KEY,target_json TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS manual_views(conversation_id TEXT PRIMARY KEY,frame_path TEXT,payloads_json TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS contact_chat_links(contact_id TEXT PRIMARY KEY,conversation_id TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS gui_commands(id TEXT PRIMARY KEY,action TEXT,params_json TEXT,expires REAL,status TEXT,detail_json TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY,conversation_id TEXT,frame_path TEXT,tokens_json TEXT,status TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS snapshot_payloads(snapshot_id INTEGER PRIMARY KEY,payloads_json TEXT);
        CREATE TABLE IF NOT EXISTS context_baselines(snapshot_id INTEGER PRIMARY KEY,conversation_id TEXT,source_kind TEXT,source_id TEXT,source_seen REAL,proof_json TEXT,UNIQUE(conversation_id,source_kind,source_id));
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,conversation_id TEXT,snapshot_id INTEGER,ordinal INTEGER,payload_json TEXT,seen REAL,UNIQUE(snapshot_id,ordinal));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT,detail_json TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS reply_outbox(event_id TEXT PRIMARY KEY,conversation_id TEXT,snapshot_id INTEGER,ordinal INTEGER,status TEXT,request_json TEXT,response_json TEXT,result_json TEXT,created REAL,updated REAL,reported INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reply_reconcile(event_id TEXT PRIMARY KEY,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0);
        ''')

    def begin_scan(self, expected=None):
        scan = uuid.uuid4().hex
        # Launcher enforces one desktop worker. Preserve interrupted run evidence.
        self.db.execute("UPDATE scans SET status='interrupted',finished=? WHERE status='running'",(time.time(),))
        self.db.execute('INSERT INTO scans VALUES(?,?,NULL,?,?,0,?)', (scan,time.time(),'running',expected,'{}'))
        self.db.commit()
        return scan

    def finish_scan(self, scan, expected, observed, bottom, detail):
        persisted=True
        if detail.get('coverage_grain')=='aligned visible list positions':
            count,minimum,maximum=self.db.execute('SELECT count(*),min(ordinal),max(ordinal) FROM contact_positions WHERE scan_id=?',(scan,)).fetchone()
            persisted=count==observed and (observed==0 or (minimum==0 and maximum==observed-1))
        status = 'complete' if bottom and expected is not None and observed == expected and persisted else 'incomplete'
        self.db.execute('UPDATE scans SET finished=?,status=?,expected=?,observed=?,detail=? WHERE id=?', (time.time(),status,expected,observed,json.dumps(detail,ensure_ascii=False),scan))
        self.db.commit()
        return status

    def contact(self, scan, page, list_name, wxid, avatar_sha, avatar_path, facts, profile_path, ocr, provisional_key):
        if wxid:
            existing=self.db.execute('SELECT avatar_sha,facts_json FROM contacts WHERE wechat_id=?',(wxid,)).fetchone()
            if (existing and existing[0]!=avatar_sha
                    and json.loads(existing[1]).get('avatar_kind')==facts.get('avatar_kind')):
                facts=dict(facts,unverified_conflicting_wechat_id=wxid)
                self.event('contact_identity_conflict',{'wechat_id_ocr':wxid,'profile_path':profile_path})
                wxid=None
        key = 'wx:' + wxid if wxid else 'provisional:' + digest(provisional_key)
        previous = self.db.execute('SELECT display_name,avatar_sha,facts_json FROM contacts WHERE id=?',(key,)).fetchone()
        self.db.execute('''INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,avatar_sha=excluded.avatar_sha,avatar_path=excluded.avatar_path,facts_json=excluded.facts_json,last_seen=excluded.last_seen''',
            (key,wxid,list_name,avatar_sha,avatar_path,json.dumps(facts,ensure_ascii=False),time.time(),'ocr_wechat_id' if wxid else 'provisional'))
        self.db.execute('INSERT INTO contact_observations(scan_id,page,contact_id,list_name,profile_path,ocr_json,seen) VALUES(?,?,?,?,?,?,?)', (scan,page,key,list_name,profile_path,json.dumps(ocr,ensure_ascii=False),time.time()))
        self.db.commit()
        if previous is None or previous != (list_name,avatar_sha,json.dumps(facts,ensure_ascii=False)):
            self.event('contact_created' if previous is None else 'contact_updated', {'contact_id':key,'name':list_name,'scan_id':scan})
        return key

    def canonical(self, conversation):
        seen=set()
        while conversation not in seen:
            seen.add(conversation)
            row=self.db.execute('SELECT canonical FROM group_aliases WHERE alias=?',(conversation,)).fetchone()
            if not row: break
            conversation=row[0]
        return conversation

    def group_identity(self, row_name, header, avatar_sha, payloads=()):
        """User-defined group identity: same name OR five observed members in common.

        Keep aliases and original history; private chat identities stay unchanged.
        Only positively observed speaker names/IDs count as members.
        """
        import re
        from name_match import normalized,names_match,group_header
        name=normalized(row_name)
        observed=normalized(header)
        group=group_header(name,observed)
        visual=digest([group[0] if group else observed,avatar_sha])
        if not group:
            from group_policy import is_group
            from flows import title_matches
            private=[]
            for cid,raw in self.db.execute('SELECT conversation_id,target_json FROM conversation_routes'):
                route=json.loads(raw)
                if (not is_group(route) and route.get('avatar_sha')==avatar_sha and names_match(route.get('name'),row_name)
                        and title_matches(route.get('name',''),header,route.get('name_clipped',False))):private.append(self.canonical(cid))
            private=set(private)
            if len(private)>1:raise ValueError('PRIVATE_NAME_AMBIGUOUS')
            return next(iter(private)) if private else self.canonical(visual)
        name=group[0]
        members={str(p.get('group_sender_name') or p.get('sender_name') or p.get('sender_wechat_id')) for p in payloads
                 if p.get('sender_identity_verified') or p.get('group_sender_verified')}
        members.discard('None');members.discard('')
        candidates=[];candidate_names=set();exact_candidates=[]
        for cid,old_name in self.db.execute("SELECT conversation_id,name FROM conversation_profiles WHERE kind='group' ORDER BY rowid"):
            canonical=self.canonical(cid)
            known={r[0] for r in self.db.execute('SELECT member FROM group_members WHERE conversation_id=?',(canonical,))}
            if normalized(old_name)==normalized(name):exact_candidates.append(canonical)
            if names_match(old_name,name) or len(known & members)>=5:
                candidates.append(canonical);candidate_names.add(normalized(old_name))
        if exact_candidates:candidates=exact_candidates;candidate_names={normalized(name)}
        candidates=list(dict.fromkeys(candidates))
        if len(candidates)>1 and len(candidate_names)>1:raise ValueError('GROUP_NAME_AMBIGUOUS')
        canonical=candidates[0] if candidates else self.canonical(visual)
        for alias in set(candidates+[visual]):
            if alias!=canonical:
                self.db.execute('UPDATE group_aliases SET canonical=? WHERE canonical=?',(canonical,alias))
                self.db.execute('INSERT OR REPLACE INTO group_aliases VALUES(?,?,?,?)',(alias,canonical,'same_name_or_five_members',time.time()))
                self.db.execute('INSERT OR IGNORE INTO group_members SELECT ?,member FROM group_members WHERE conversation_id=?',(canonical,alias))
        self.db.executemany('INSERT OR IGNORE INTO group_members VALUES(?,?)',[(canonical,m) for m in members])
        self.db.commit()
        return canonical

    def reconcile_groups(self):
        rows=self.db.execute("SELECT c.id,p.row_name,p.header,c.avatar_sha FROM conversations c JOIN conversation_profiles p ON p.conversation_id=c.id WHERE p.kind='group' ORDER BY c.last_seen").fetchall()
        for cid,name,title,avatar in rows:
            canonical=self.group_identity(name,title,avatar)
            if cid!=canonical:
                self.db.execute('INSERT OR REPLACE INTO group_aliases VALUES(?,?,?,?)',(cid,canonical,'same_name',time.time()))
            # Preserve all archived histories; refresh only the canonical live route.
            route=self.db.execute('SELECT target_json,seen FROM conversation_routes WHERE conversation_id=?',(cid,)).fetchone()
            if route:self.db.execute('INSERT OR REPLACE INTO conversation_routes VALUES(?,?,?)',(canonical,*route))
            self.db.execute('UPDATE conversations SET title=?,avatar_sha=?,last_seen=(SELECT last_seen FROM conversations WHERE id=?) WHERE id=?',(title,avatar,cid,canonical))
        self.db.commit()

    def observe_group(self, conversation, row_name, header):
        """Only an additional, positive count suffix is group evidence.

        A friend's literal nickname Tom(10) must not become a group. Count is
        mutable metadata, never part of the conversation identity.
        """
        import re
        from name_match import group_header
        match=group_header(row_name,header)
        if not match:return False
        row_name,count=match
        previous = self.db.execute('SELECT name,member_count FROM conversation_profiles WHERE conversation_id=?',(conversation,)).fetchone()
        self.db.execute('''INSERT INTO conversation_profiles VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id) DO UPDATE SET name=excluded.name,member_count=excluded.member_count,row_name=excluded.row_name,header=excluded.header,observed_at=excluded.observed_at''',
            (conversation,'group',row_name,count,row_name,header,time.time()))
        self.db.commit()
        if previous != (row_name,count):
            self.event('group_discovered' if previous is None else 'group_updated',{'conversation':conversation,'name':row_name,'member_count':count,'source':'header_count'})
        return True

    def route(self, conversation, target):
        self.db.execute('INSERT INTO conversation_routes VALUES(?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET target_json=excluded.target_json,seen=excluded.seen', (conversation,json.dumps(target,ensure_ascii=False),time.time()))
        self.db.commit()

    def manual_view(self, conversation, title, avatar_sha, frame, payloads):
        now=time.time()
        self.db.execute('INSERT INTO conversations VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,avatar_sha=excluded.avatar_sha,last_seen=excluded.last_seen', (conversation,title,avatar_sha,'visual_provisional',now))
        self.db.execute('INSERT INTO manual_views VALUES(?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET frame_path=excluded.frame_path,payloads_json=excluded.payloads_json,seen=excluded.seen', (conversation,frame,json.dumps(payloads,ensure_ascii=False),now))
        self.db.commit()

    def queue_gui(self, command):
        self.db.execute('INSERT OR IGNORE INTO gui_commands VALUES(?,?,?,?,?,?,?)', (command['id'],command['action'],json.dumps(command['params'],ensure_ascii=False),command['expires_at'],'pending','{}',time.time()))
        self.db.commit()

    def gui_result(self, command_id, status, detail):
        self.db.execute('UPDATE gui_commands SET status=?,detail_json=?,updated=? WHERE id=?', (status,json.dumps(detail,ensure_ascii=False),time.time(),command_id))
        self.db.commit()

    def event(self, kind, detail):
        self.db.execute('INSERT INTO events(kind,detail_json,seen) VALUES(?,?,?)',(kind,json.dumps(detail,ensure_ascii=False),time.time()))
        self.db.commit()

    def frame(self,kind,owner,path,sha,ocr):
        self.db.execute('INSERT INTO frames(kind,owner,path,sha,ocr_json,seen) VALUES(?,?,?,?,?,?)',(kind,owner,path,sha,json.dumps(ocr,ensure_ascii=False),time.time()))
        self.db.commit()

    def contact_seeds(self,scan):
        rows=self.db.execute('SELECT o.id,o.list_name,o.contact_id,c.facts_json FROM contact_observations o JOIN contacts c ON c.id=o.contact_id WHERE o.scan_id=? ORDER BY o.id',(scan,)).fetchall()
        if not rows: raise ValueError('REUSE_SCAN_HAS_NO_EVIDENCE')
        grouped={}
        for observation,name,cid,facts in rows:
            sha=json.loads(facts).get('list_avatar_sha')
            if sha: grouped.setdefault((name,sha),{})[cid]=observation
        return {key:next(iter(matches.values())) for key,matches in grouped.items() if len(matches)==1}

    def contact_position(self,scan,ordinal,cid,name,avatar,frame):
        self.db.execute('INSERT INTO contact_positions VALUES(?,?,?,?,?,?) ON CONFLICT(scan_id,ordinal) DO UPDATE SET contact_id=excluded.contact_id,list_name=excluded.list_name,avatar_sha=excluded.avatar_sha,frame_path=excluded.frame_path',(scan,ordinal,cid,name,avatar,frame))
        self.db.commit()

    def reuse_contact(self,scan,page,observation):
        old=self.db.execute('SELECT contact_id,list_name,profile_path,ocr_json FROM contact_observations WHERE id=?',(observation,)).fetchone()
        if old is None: raise ValueError('REUSE_OBSERVATION_MISSING')
        self.db.execute('INSERT INTO contact_observations(scan_id,page,contact_id,list_name,profile_path,ocr_json,seen) VALUES(?,?,?,?,?,?,?)',(scan,page,*old,time.time()))
        self.db.commit()
        return old[0]

    def verified_snapshot_payloads(self,snapshot_id):
        """Derive timestamp evidence only from the original, pixel-bound frame."""
        row=self.db.execute('SELECT s.frame_path,p.payloads_json FROM snapshots s JOIN snapshot_payloads p ON p.snapshot_id=s.id WHERE s.id=?',(snapshot_id,)).fetchone()
        if not row:return []
        values=json.loads(row[1])
        upgraded=self.verify_context_pixels(row[0],values)
        if upgraded!=values:
            self.db.execute('UPDATE snapshot_payloads SET payloads_json=? WHERE snapshot_id=? AND payloads_json=?',
                (json.dumps(upgraded,ensure_ascii=False),snapshot_id,row[1]));self.db.commit()
        return upgraded

    def verify_context_pixels(self,frame,payloads):
        if not any(p.get('direction')=='time' and not p.get('visual',{}).get('time_verified') for p in payloads):return payloads
        if not Path(frame).is_file():return payloads
        import numpy as np
        from PIL import Image
        if __package__:from .message_vision import verify_time_anchors
        else:from message_vision import verify_time_anchors
        message_identity,_=vision_functions()
        try:
            with Image.open(frame) as image:rgb=np.asarray(image.convert('RGB')).copy()
            count=0
            for p in payloads:
                if not message_identity(p):continue
                visual=p['visual'];x,y,w,h=visual['box'];crop=rgb[y:y+h,x:x+w,:3]
                if (x<0 or y<0 or crop.shape!=(h,w,3) or visual['sha256'] not in
                        (hashlib.sha256(crop.tobytes()).hexdigest(),hashlib.sha256(crop[:,:,::-1].copy().tobytes()).hexdigest())):return payloads
                if visual.get('avatar_verified') is True:
                    ax,ay,aw,ah=visual.get('avatar_box',[0,0,0,0]);avatar=rgb[ay:ay+ah,ax:ax+aw,:3]
                    if avatar.shape!=(ah,aw,3) or hashlib.sha256(avatar.tobytes()).hexdigest()!=visual.get('avatar_sha256'):return payloads
                count+=1
            return verify_time_anchors(rgb,payloads,color_order='RGB') if count else payloads
        except (OSError,ValueError,TypeError,IndexError):return payloads

    def exact_chat_frame(self,previous_frame,current_frame):
        from PIL import Image
        import numpy as np
        try:
            with Image.open(previous_frame) as a,Image.open(current_frame) as b:
                if a.size!=(1920,1040) or b.size!=a.size:return False
                return np.array_equal(np.asarray(a.convert('RGB'))[82:887,490:1885],
                                      np.asarray(b.convert('RGB'))[82:887,490:1885])
        except (OSError,ValueError):return False

    def promote_manual_sent_context(self,conversation):
        """Import only a strict, outgoing-only extension of handled history.

        A manual view never proves that an incoming message was handled. Each
        added outgoing bubble must instead match a real verified send journal.
        """
        previous=self.db.execute('''SELECT s.id,s.seen,p.payloads_json FROM snapshots s
            JOIN snapshot_payloads p ON p.snapshot_id=s.id WHERE s.conversation_id=?
            AND s.status IN ('baseline','append','unchanged') ORDER BY s.seen DESC,s.id DESC LIMIT 1''',(conversation,)).fetchone()
        manual=self.db.execute('SELECT frame_path,payloads_json,seen FROM manual_views WHERE conversation_id=?',(conversation,)).fetchone()
        if not previous or not manual or manual[2]<=previous[1]:return None
        source_id=digest([manual[0],manual[2]])
        if self.db.execute("SELECT 1 FROM context_baselines WHERE conversation_id=? AND source_kind='verified_manual_after_send' AND source_id=?",(conversation,source_id)).fetchone():return None
        payloads=self.verify_context_pixels(manual[0],json.loads(manual[1]));message_identity,reconcile_visual=vision_functions()
        overlap=reconcile_visual(self.verified_snapshot_payloads(previous[0]),payloads)
        if not overlap or overlap['status']!='append':return None
        tail=payloads[overlap['overlap']:];outgoing=[]
        for p in tail:
            if p.get('message_kind') in ('time','sender_label','avatar_ocr'):continue
            if p.get('direction')!='outgoing' or not message_identity(p):return None
            outgoing.append(p)
        if not outgoing:return None
        known=[]
        conversation_title=self.db.execute('SELECT title FROM conversations WHERE id=?',(conversation,)).fetchone()
        for did,raw,detail in self.db.execute("SELECT id,params_json,detail_json FROM gui_commands WHERE action='send_delivery' AND status='succeeded' AND updated>? AND updated<=?",(previous[1],manual[2])):
            params=json.loads(raw);proof=json.loads(detail);evidence=proof.get('evidence',{})
            target=proof.get('diagnostic',{}).get('preclick_target',{})
            if (params.get('conversation_id')==conversation and proof.get('status')=='sent'
                    and proof.get('reason')=='MEDIA_NEW_RIGHT_BUBBLE_AND_EMPTY_DRAFT_VERIFIED'
                    and target.get('title_verified') is True and conversation_title and target.get('title')==conversation_title[0]
                    and evidence.get('title_verified') is True and evidence.get('outgoing_verified') is True):
                identity=message_identity(proof.get('diagnostic',{}).get('outgoing'))
                if identity:known.append((did,identity))
        used=[]
        for p in outgoing:
            candidates=[did for did,identity in known if identity==message_identity(p) and did not in used]
            if len(candidates)!=1:return None
            used.append(candidates[0])
        cursor=self.db.execute('INSERT INTO snapshots(conversation_id,frame_path,tokens_json,status,seen) VALUES(?,?,?,?,?)',
            (conversation,manual[0],json.dumps(payload_tokens(payloads)),'baseline',manual[2]))
        snapshot=cursor.lastrowid
        self.db.execute('INSERT INTO snapshot_payloads VALUES(?,?)',(snapshot,json.dumps(payloads,ensure_ascii=False)))
        proof={'previous_snapshot_id':previous[0],'delivery_ids':used,'overlap':overlap['overlap'],
               'metadata':overlap.get('metadata',{}),'historical_notifications':False}
        self.db.execute('INSERT INTO context_baselines VALUES(?,?,?,?,?,?)',
            (snapshot,conversation,'verified_manual_after_send',source_id,manual[2],json.dumps(proof,ensure_ascii=False)))
        self.db.commit()
        self.event('verified_post_send_context_saved',{'conversation':conversation,'snapshot_id':snapshot,'source':'verified_manual_after_send'})
        return snapshot

    def record_verified_send_context(self,conversation,title,avatar_sha,frame,payloads,result,*,delivery_id,occurrence_enricher=None,observation_head=None,on_observation=None):
        """Keep actual after-send history only after a strict visual extension.

        Concurrent incoming messages remain in the new tail for durable capture.
        Ambiguous history is retained for review and never becomes a baseline.
        """
        message_identity,reconcile_visual=vision_functions()
        evidence=result.get('evidence',{});diagnostic=result.get('diagnostic',{})
        journal=self.db.execute('SELECT status,params_json,detail_json FROM gui_commands WHERE id=? AND action=?',(delivery_id,'send_delivery')).fetchone()
        if (not journal or journal[0]!='succeeded' or json.loads(journal[1]).get('conversation_id')!=conversation
                or json.loads(journal[2])!=result):raise ValueError('POST_SEND_JOURNAL_NOT_BOUND')
        if result.get('status')!='sent' or not all(evidence.get(k) is True for k in ('title_verified','outgoing_verified')):
            raise ValueError('POST_SEND_RESULT_NOT_VERIFIED')
        before=diagnostic.get('history_before',[]);outgoing=diagnostic.get('outgoing')
        strict=reconcile_visual(before,payloads)
        matches=[i for i,p in enumerate(payloads) if message_identity(p)==message_identity(outgoing)
                 and p.get('visual',{}).get('box')==outgoing.get('visual',{}).get('box')] if isinstance(outgoing,dict) and message_identity(outgoing) else []
        if not strict or strict['status']!='append' or len(matches)!=1 or matches[0]<strict['overlap']:
            raise ValueError('POST_SEND_CONTEXT_NOT_UNIQUE')
        snapshot=self.snapshot(conversation,title,avatar_sha,frame,payloads,occurrence_enricher=occurrence_enricher,
            observation_head=observation_head,on_observation=on_observation)
        if snapshot['status'] not in ('append','review') or not snapshot.get('metadata',{}).get('new_tail_verbatim'):
            raise ValueError('POST_SEND_HISTORY_NOT_RECONCILED')
        if matches[0]<snapshot['overlap']:raise ValueError('POST_SEND_OUTGOING_NOT_NEW')
        snapshot['incoming_ordinals']=[i for i in range(snapshot['overlap'],len(payloads)) if payloads[i].get('direction')=='incoming']
        return snapshot

    def review_snapshot(self,snapshot_id,reason):
        self.db.execute("UPDATE snapshots SET status='review' WHERE id=?",(snapshot_id,));self.db.commit()
        self.event('snapshot_context_review',{'snapshot_id':snapshot_id,'reason':reason})

    def snapshot(self, conversation, title, avatar_sha, frame, payloads, *, activation_baseline=False,occurrence_enricher=None,reconciliation_guard=None,observation_head=None,on_observation=None,captured_at=None):
        now=time.time()
        snapshot_seen=now if captured_at is None else captured_at
        if (type(snapshot_seen) not in (int,float) or not math.isfinite(snapshot_seen)
                or not 0<snapshot_seen<=now):
            raise ValueError('SNAPSHOT_CAPTURE_TIME_INVALID')
        self.db.execute('INSERT INTO conversations VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,avatar_sha=excluded.avatar_sha,last_seen=excluded.last_seen', (conversation,title,avatar_sha,'visual_provisional',snapshot_seen))
        if not activation_baseline:self.promote_manual_sent_context(conversation)
        previous = self.db.execute('''SELECT s.tokens_json,p.payloads_json,s.id,s.frame_path
            FROM snapshots s JOIN snapshot_payloads p ON p.snapshot_id=s.id
            WHERE s.conversation_id=? AND s.status IN ('baseline','append','unchanged')
            ORDER BY s.seen DESC,s.id DESC LIMIT 1''',(conversation,)).fetchone()
        if observation_head is not None:
            # The receive cursor may include individually unverified messages.
            # Their durable work rows, rather than snapshot status, own handling.
            observed=self.db.execute('''SELECT s.tokens_json,p.payloads_json,s.id,s.frame_path
                FROM snapshots s JOIN snapshot_payloads p ON p.snapshot_id=s.id
                WHERE s.id=? AND s.conversation_id=?''',(observation_head,conversation)).fetchone()
            if observed:previous=observed
        if previous:previous=(previous[0],json.dumps(self.verified_snapshot_payloads(previous[2]),ensure_ascii=False),previous[2],previous[3])
        # A new worker observes existing visible history without processing it.
        # Old unsupported cards must not prevent that read-only baseline forever.
        if activation_baseline: previous=None
        visual_mode=any(p.get('visual') for p in payloads)
        if visual_mode and previous and not any(p.get('visual') for p in json.loads(previous[1])):
            # A schema upgrade is a new read-only baseline, not incoming history.
            previous=None
        tokens = payload_tokens(payloads)
        result = reconcile(json.loads(previous[0]) if previous else None, tokens)
        if visual_mode and previous:
            if __package__: from .message_vision import reconcile_visual
            else: from message_vision import reconcile_visual
            recovered=reconcile_visual(json.loads(previous[1]),payloads,result)
            if recovered: result=recovered
            elif result['status']!='unchanged' or not self.exact_chat_frame(previous[3],frame):
                # A rejected visual overlap is not permission to fall back to
                # a short/repeated text hash and invent an incoming occurrence.
                result={'status':'ambiguous' if result['status']=='ambiguous' else 'gap','new':[],'overlap':0,
                        'metadata':{'strategy':'exact_bubble_pixels','reason':'insufficient_unique_visual_anchors'}}
            else:
                # Full unchanged viewport pixels locate even one long message
                # by its fixed position. Carry its pending occurrence through a
                # native-copy retry; this branch never registers new messages.
                message_identity,_=vision_functions()
                old=[(i,p) for i,p in enumerate(json.loads(previous[1])) if message_identity(p)]
                current=[(i,p) for i,p in enumerate(payloads) if message_identity(p)]
                if old and len(old)==len(current) and all(
                        message_identity(a)==message_identity(b) and a['visual']['box']==b['visual']['box']
                        for (_,a),(_,b) in zip(old,current)):
                    result=dict(result,metadata={'strategy':'exact_unchanged_chat_viewport',
                        'matched_occurrences':{str(ni):oi for (oi,_),(ni,_) in zip(old,current)}})
            if result['status'] in ('gap','ambiguous') and on_observation and observation_head==previous[2]:
                # A complete recent timestamp later than this live observation
                # proves a fresh tail independently of old viewport overlap.
                # Initial/manual/history captures never enter this path.
                if __package__:from .fresh_tail import locate_fresh_tail
                else:from fresh_tail import locate_fresh_tail
                prior_seen=self.db.execute('SELECT seen FROM snapshots WHERE id=?',(previous[2],)).fetchone()[0]
                fresh=locate_fresh_tail(payloads,frame,prior_seen,snapshot_seen)
                if fresh:result=fresh
            result=dict(result,new=tokens[result['overlap']:] if result['status']=='append' else [])
            # Do not commit an uncertain new OCR read as a handled message.
            # Re-reading can then establish a stable append against the old anchor.
            if result['status']=='append' and any(p.get('direction')=='incoming' and not p.get('reply_safe') and not (p.get('message_kind')=='media' and p.get('visual',{}).get('verified') is True) for p in payloads[result['overlap']:]):
                result=dict(result,status='review',new=[])
        if previous and result['status']=='gap' and not visual_mode:
            if __package__:
                from .ocr_context import recover_ocr_overlap
            else:
                from ocr_context import recover_ocr_overlap
            recovered=recover_ocr_overlap(json.loads(previous[1]),payloads,result)
            if recovered:
                result=dict(recovered,new=tokens[recovered['overlap']:])
        result['previous_snapshot_id']=previous[2] if previous else None
        if occurrence_enricher and result['status'] in ('append','unchanged'):
            enriched=occurrence_enricher(result,payloads)
            if isinstance(enriched,list):payloads[:]=enriched
        if reconciliation_guard:result=reconciliation_guard(result,payloads)
        # Snapshot, occurrence tasks and receive cursor commit together. A crash
        # cannot make the next frame unchanged without leaving recovery work.
        try:
            cursor = self.db.execute('INSERT INTO snapshots(conversation_id,frame_path,tokens_json,status,seen) VALUES(?,?,?,?,?)',(conversation,frame,json.dumps(tokens),result['status'],snapshot_seen))
            snapshot = cursor.lastrowid
            self.db.execute('INSERT INTO snapshot_payloads VALUES(?,?)',(snapshot,json.dumps(payloads,ensure_ascii=False)))
            result['snapshot_id']=snapshot
            if on_observation:
                on_observation(result,payloads)
            elif result['status'] == 'append':
                for ordinal in range(result['overlap'],len(payloads)):
                    self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,conversation,snapshot,ordinal,json.dumps(payloads[ordinal],ensure_ascii=False),snapshot_seen))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return result

    def reconcile_snapshot(self, conversation, title, avatar_sha, frame, payloads):
        tokens = payload_tokens(payloads)
        cursor = self.db.execute('INSERT INTO snapshots(conversation_id,frame_path,tokens_json,status,seen) VALUES(?,?,?,?,?)',
            (conversation, frame, json.dumps(tokens), 'reconcile', time.time()))
        snapshot = cursor.lastrowid
        self.db.execute('INSERT INTO snapshot_payloads VALUES(?,?)', (snapshot, json.dumps(payloads, ensure_ascii=False)))
        self.db.commit()
        return {'snapshot_id': snapshot, 'status': 'reconcile', 'tokens': tokens}

    def queue_reply(self, request, snapshot, ordinal):
        now=time.time()
        cursor=self.db.execute('INSERT OR IGNORE INTO reply_outbox(event_id,conversation_id,snapshot_id,ordinal,status,request_json,created,updated) VALUES(?,?,?,?,?,?,?,?)',
            (request['event_id'],request['conversation_id'],snapshot,ordinal,'queued',json.dumps(request,ensure_ascii=False),now,now))
        self.db.commit()
        if cursor.rowcount == 1:
            self.event('reply_queued',{'event_id':request['event_id'],'conversation':request['conversation_id']})
        return cursor.rowcount==1

    def reply_rows(self, statuses):
        self.db.row_factory=sqlite3.Row
        rows=self.db.execute('SELECT * FROM reply_outbox WHERE status IN ('+','.join('?' for _ in statuses)+') ORDER BY created',tuple(statuses)).fetchall()
        self.db.row_factory=None
        return [dict(r) for r in rows]

    def reply_update(self,event,status,response=None,result=None,reported=False):
        self.db.execute('UPDATE reply_outbox SET status=?,updated=?,response_json=coalesce(?,response_json),result_json=coalesce(?,result_json),reported=? WHERE event_id=?',
            (status,time.time(),json.dumps(response,ensure_ascii=False) if response is not None else None,json.dumps(result,ensure_ascii=False) if result is not None else None,int(reported),event))
        self.db.commit()

    def reconcile_state(self,event):
        row=self.db.execute('SELECT attempts,next_attempt FROM reply_reconcile WHERE event_id=?',(event,)).fetchone()
        return {'attempts':row[0] if row else 0,'next_attempt':row[1] if row else 0.0}

    def reconcile_schedule(self,event,attempts,next_attempt):
        self.db.execute('INSERT INTO reply_reconcile(event_id,attempts,next_attempt) VALUES(?,?,?)'
            ' ON CONFLICT(event_id) DO UPDATE SET attempts=excluded.attempts,next_attempt=excluded.next_attempt',
            (event,attempts,next_attempt))
        self.db.commit()
