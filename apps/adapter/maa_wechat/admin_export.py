"""Read-only, evidence-first export for the authenticated Maa administration page.

This module never constructs a Maa controller or Store. All DB reads share one
SQLite read transaction; GUI execution remains in the existing locked launcher.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import time

from PIL import Image

SCOPES = ('personal', 'enterprise', 'public')


def decoded(raw, default=None):
    try:
        return json.loads(raw) if raw else ({} if default is None else default)
    except (ValueError, TypeError):
        return {} if default is None else default


def read_json(path, default=None):
    try:
        return decoded(Path(path).read_text(encoding='utf-8-sig'), default)
    except OSError:
        return {} if default is None else default


class Assets:
    """Content-addressed PNGs from data only, not raw-pixel identity hashes."""
    def __init__(self, root):
        self.root = (Path(root) / 'data').resolve()
        self.cache = {}
        self.items = {}
        self.active = set()

    def image(self, filename, kind='frame'):
        if not filename:
            return None
        path = Path(filename).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() != '.png':
            return None
        try:
            stat = path.stat()
            if stat.st_size > 10 * 1024 * 1024:
                return None
            key = (str(path), stat.st_mtime_ns, stat.st_size, kind)
            self.active.add(key)
            if key not in self.cache:
                with Image.open(path) as original:
                    picture = original.convert('RGB')
                    if picture.width > 1920 or picture.height > 1080:
                        return None
                    if kind == 'profile' and picture.size == (1920, 1040):
                        picture = picture.crop((1020, 70, 1500, 920))
                    if kind == 'frame':
                        picture.thumbnail((1280, 720))
                    output = io.BytesIO()
                    picture.save(output, format='PNG', optimize=True)
                    raw = output.getvalue()
                sha = hashlib.sha256(raw).hexdigest()
                self.cache[key] = (sha, raw)
            sha, raw = self.cache[key]
            self.items[sha] = raw
            return sha
        except (OSError, ValueError):
            return None

    def media_crop(self,filename,box):
        if not filename or not isinstance(box,list) or len(box)!=4 or any(not isinstance(v,int) for v in box):return None
        path=Path(filename).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower()!='.png':return None
        try:
            stat=path.stat();key=(str(path),stat.st_mtime_ns,stat.st_size,'media',tuple(box));self.active.add(key)
            if key not in self.cache:
                with Image.open(path) as original:
                    x,y,w,h=box
                    if not (64<=w<=500 and 64<=h<=450 and x>=490 and y>=82 and x+w<=1895 and y+h<=887):return None
                    if original.size!=(1920,1040):return None
                    picture=original.crop((x,y,x+w,y+h)).convert('RGB');picture.thumbnail((360,360))
                    out=io.BytesIO();picture.save(out,format='PNG',optimize=True);raw=out.getvalue()
                self.cache[key]=(hashlib.sha256(raw).hexdigest(),raw)
            sha,raw=self.cache[key];self.items[sha]=raw;return sha
        except (OSError,ValueError):return None

    def message_avatar(self, filename, payload):
        """Export the speaker crop from this message's frame, never the chat icon."""
        visual = payload.get('visual') or {}
        box = visual.get('avatar_box')
        pixel_sha = visual.get('avatar_sha256')
        direction = payload.get('direction')
        if (visual.get('avatar_verified') is not True
                or visual.get('avatar_source') != 'same_frame_message_avatar'
                or visual.get('avatar_color_order', 'RGB') != 'RGB'
                or direction not in ('incoming', 'outgoing')
                or not isinstance(box, list) or len(box) != 4
                or any(type(value) is not int for value in box)
                or not isinstance(pixel_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', pixel_sha)
                or not filename):
            return None
        x, y, w, h = box
        # The calibrated official-client frame has message avatars in its two
        # outer columns. These bounds exclude the composite conversation icon.
        if not (24 <= w <= 64 and 24 <= h <= 64 and abs(w-h) <= 3
                and y >= 82 and y+h <= 887
                and (490 <= x and x+w <= 545 if direction == 'incoming'
                     else 1820 <= x and x+w <= 1900)):
            return None
        path = Path(filename).resolve()
        if not path.is_relative_to(self.root) or path.suffix.lower() != '.png':
            return None
        try:
            stat = path.stat()
            if stat.st_size > 10 * 1024 * 1024:
                return None
            key = (str(path), stat.st_mtime_ns, stat.st_size, 'message_avatar', tuple(box), pixel_sha)
            self.active.add(key)
            if key not in self.cache:
                with Image.open(path) as original:
                    if original.size != (1920, 1040):
                        return None
                    crop = original.crop((x, y, x+w, y+h)).convert('RGB')
                    if hashlib.sha256(crop.tobytes()).hexdigest() != pixel_sha:
                        return None
                    output = io.BytesIO()
                    crop.save(output, format='PNG', optimize=True)
                    raw = output.getvalue()
                self.cache[key] = (hashlib.sha256(raw).hexdigest(), raw)
            sha, raw = self.cache[key]
            self.items[sha] = raw
            return sha
        except (OSError, ValueError):
            return None


def verified_private_links(db, profiles, aliases):
    """Export only account bindings with stored full-profile proof, never names."""
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'contact_chat_links','contact_chat_history'} <= tables:return {}
    def canonical(cid):
        seen=set()
        while cid in aliases:
            if cid in seen:return None
            seen.add(cid);cid=aliases[cid]
        return cid
    result={}
    for row in db.execute('''SELECT l.contact_id,l.conversation_id,c.wechat_id,c.facts_json,c.identity_status
        FROM contact_chat_links l JOIN contacts c ON c.id=l.contact_id'''):
        cid=canonical(row['conversation_id']);wxid=row['wechat_id']
        if (not cid or cid in profiles or row['identity_status']!='ocr_wechat_id'
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',str(wxid or ''))
                or decoded(row['facts_json']).get('scope')!='personal'):continue
        verified=[]
        for history in db.execute('SELECT conversation_id,seen,proof_json FROM contact_chat_history WHERE contact_id=?',(row['contact_id'],)):
            proof=decoded(history['proof_json'])
            if (canonical(history['conversation_id'])==cid and proof.get('stable') is True
                    and proof.get('wechat_id')==wxid
                    and re.fullmatch(r'[a-f0-9]{64}',str(proof.get('profile_sha256','')))):
                verified.append(history['seen'])
        if verified:
            result.setdefault(cid,[]).append({'contact_id':row['contact_id'],'wechat_id':wxid,
                'profile_verified':True,'verified_at':max(verified)})
    return {cid:rows[0] for cid,rows in result.items() if len(rows)==1}


def payload_assets(payload, frame_path, assets):
    """Attach remotely usable assets only after checking the original frame."""
    payload.pop('sender_avatar_asset', None)
    avatar = assets.message_avatar(frame_path, payload)
    if avatar:
        payload['sender_avatar_asset'] = avatar
    if payload.get('message_kind') == 'media':
        payload['media_asset'] = assets.media_crop(frame_path, (payload.get('visual') or {}).get('box'))
    return payload


def tail_actions(path, limit=80):
    try:
        with Path(path).open('rb') as source:
            source.seek(0, 2)
            size = source.tell()
            source.seek(max(0, size - 512 * 1024))
            if size > 512 * 1024:
                source.readline()
            lines = source.read().decode('utf-8', errors='replace').splitlines()
        output = []
        for line in lines[-limit:]:
            item = decoded(line)
            if item:
                output.append({k: item.get(k) for k in
                               ('node', 'succeeded', 'elapsed_ms', 'time_unix', 'run_id')})
        return output[::-1]
    except OSError:
        return []


def export(root, runtime, assets=None):
    root = Path(root).resolve()
    assets = assets or Assets(root)
    assets.items = {}
    assets.active = set()
    data = root / 'data'
    try:
        from group_policy import configuration
        runtime=dict(runtime,group_whitelist=configuration(root))
    except (OSError,ValueError,TypeError):
        runtime=dict(runtime,group_whitelist=None)
    progress_path = data / 'progress.json'
    progress = read_json(progress_path)
    from watch_policy import polling_configuration
    polling=polling_configuration(root)
    progress = dict(progress,polling_enabled=polling['enabled'],polling_scope=polling['scope'])
    desktop_input = read_json(data / 'desktop-input.json')
    checked_at = desktop_input.get('checked_at')
    fresh_desktop = (bool(runtime.get('run_id')) and desktop_input.get('run_id') == runtime.get('run_id')
                     and type(checked_at) in (int, float) and 0 <= time.time()-checked_at <= 10)
    if fresh_desktop:
        # Normal loop progress need not repeat the probe's evidence. The
        # separate read-only file is useful only for this run and a fresh check.
        progress = dict(progress, desktop_input=desktop_input)
    runtime = dict(runtime, progress=progress,sending_enabled=bool(runtime.get('sending_enabled',False)),
                   auto_reply_enabled=bool(runtime.get('auto_reply_enabled',False)),
                   execution_time_limit_seconds=0 if runtime.get('continuous') else 7200,
                   progress_updated_at=progress_path.stat().st_mtime if progress_path.exists() else None)
    if ((progress.get('run_id') == runtime.get('run_id')
            and progress.get('status') in ('waiting_for_window', 'waiting_for_input', 'window_recovered', 'failed', 'polling_paused'))
            or progress.get('polling_enabled') is False
            or (fresh_desktop and desktop_input.get('ready') is False)):
        runtime.update(sending_enabled=False, auto_reply_enabled=False)
    report = {
        'schema_version': 1, 'gateway_id': 'win-wechat-maa', 'collected_at': time.time(),
        'runtime': runtime, 'contacts': [], 'conversations': [], 'scans': [],
        'events': [], 'actions': tail_actions(data / 'actions.jsonl'),
        'lessons': read_json(root / 'resource' / 'teaching-path.json'),
        'acceptance': read_json(data / 'message-acceptance-sep26.json'),
        'backups': read_json(data / 'admin-backups.json', []),
    }
    db = sqlite3.connect((data / 'wechat.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    try:
        all_scans = []
        completed = {}
        latest = {}
        for row in db.execute('SELECT * FROM scans ORDER BY coalesce(finished,started) DESC'):
            item = dict(row)
            detail = decoded(item['detail'])
            scope = detail.get('scope')
            if scope not in SCOPES:
                continue
            positions = list(db.execute('SELECT ordinal FROM contact_positions WHERE scan_id=? ORDER BY ordinal', (item['id'],)))
            ordinals = [r[0] for r in positions]
            valid = (ordinals == list(range(len(ordinals))) and item['expected'] is not None
                     and item['observed'] == item['expected'] == len(ordinals))
            item.update(scope=scope, detail=detail, position_count=len(ordinals),
                        coverage_valid=valid, bottom_verified=bool(detail.get('bottom_verified')))
            all_scans.append(item)
            latest.setdefault(scope, item)
            if item['status'] == 'complete' and valid and item['bottom_verified']:
                completed.setdefault(scope, item)
        # Keep both current progress and latest verified coverage, never failed
        # historical contacts or sums of overlapping populations.
        chosen = {s['id']: s for s in [*latest.values(), *completed.values()]}
        report['scans'] = sorted(chosen.values(), key=lambda s: s['started'], reverse=True)
        for scope, scan in completed.items():
            for row in db.execute('''SELECT p.ordinal,p.list_name,c.*,
                       (SELECT o.profile_path FROM contact_observations o
                        WHERE o.scan_id=p.scan_id AND o.contact_id=p.contact_id
                        ORDER BY o.id DESC LIMIT 1) profile_path
                       FROM contact_positions p JOIN contacts c ON c.id=p.contact_id
                       WHERE p.scan_id=? ORDER BY p.ordinal''', (scan['id'],)):
                item = dict(row)
                report['contacts'].append({
                    'id': item['id'], 'scope': scope, 'scan_id': scan['id'],
                    'ordinal': item['ordinal'], 'list_name': item['list_name'],
                    'display_name': item['display_name'], 'wechat_id': item['wechat_id'],
                    'identity_status': item['identity_status'], 'last_seen': item['last_seen'],
                    'avatar_asset': assets.image(item['avatar_path'], 'avatar'),
                    'profile_asset': assets.image(item['profile_path'], 'profile'),
                    'facts': decoded(item['facts_json']),
                })
        events = []
        last_observations = {}
        for row in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 500'):
            item = dict(row)
            item['detail'] = decoded(item.pop('detail_json'))
            events.append(item)
            if item['kind'] == 'chat_observed':
                last_observations.setdefault(item['detail'].get('conversation'),
                                             dict(item['detail'], observed_at=item['seen']))
        report['events'] = events
        profiles = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_profiles'").fetchone():
            profiles = {r['conversation_id']:dict(r) for r in db.execute('SELECT * FROM conversation_profiles')}
        routes = set()
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_routes'").fetchone():
            routes={r[0] for r in db.execute('SELECT conversation_id FROM conversation_routes')}
        aliases = dict(db.execute('SELECT alias,canonical FROM group_aliases')) if db.execute("SELECT 1 FROM sqlite_master WHERE name='group_aliases'").fetchone() else {}
        private_links=verified_private_links(db,profiles,aliases)
        receipt_table=db.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_message_work'").fetchone()
        receipt_fields=',w.event_id AS receipt_event_id,w.first_seen AS receipt_first_seen' if receipt_table else ''
        receipt_join=' LEFT JOIN inbound_message_work w ON w.source_snapshot_id=m.snapshot_id AND w.source_ordinal=m.ordinal AND w.conversation_id=m.conversation_id ' if receipt_table else ''
        for row in db.execute('SELECT * FROM conversations ORDER BY last_seen DESC LIMIT 500'):
            if row['id'] in aliases: continue
            item = dict(row)
            cid = item['id']
            item['aliases'] = [a for a,c in aliases.items() if c==cid]
            item['manual_target_observed'] = cid in routes
            if cid in private_links:item['private_contact']=private_links[cid]
            if cid in profiles:
                profile=profiles[cid]
                # Old/clipped group rows can disagree with their saved header.
                # Keep the raw DB evidence, but never promote that count/profile
                # as verified telemetry or let it block all private contacts.
                count=profile.get('member_count');row_name=profile.get('row_name');header=profile.get('header')
                valid=(profile.get('kind')=='group' and type(count) is int and 1<=count<=9999
                       and isinstance(row_name,str) and isinstance(header,str)
                       and re.fullmatch(re.escape(re.sub(r'\s+','',row_name))+r'[（(]'+str(count)+r'[）)]',re.sub(r'\s+','',header)))
                if valid:item['group_profile']=profile
                else:
                    item['group_profile_status']='unverified'
                    item['group_profile_reason']='GROUP_PROFILE_HEADER_MISMATCH'
            # The cropped PNG was named with the raw pixel SHA by flows.save_crop.
            item['avatar_asset'] = assets.image(data / 'avatars' / (item['avatar_sha'] + '.png'), 'avatar')
            item.pop('avatar_sha', None)
            snap = db.execute('SELECT * FROM snapshots WHERE conversation_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
            if snap:
                payload = db.execute('SELECT payloads_json FROM snapshot_payloads WHERE snapshot_id=?', (snap['id'],)).fetchone()
                item['latest_snapshot'] = {
                    'id': snap['id'], 'status': snap['status'], 'seen': snap['seen'],
                    'frame_asset': assets.image(snap['frame_path']),
                    'payloads': decoded(payload[0], []) if payload else [],
                }
                item['latest_status'] = snap['status']
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_views'").fetchone():
                view=db.execute('SELECT * FROM manual_views WHERE conversation_id=?',(cid,)).fetchone()
                if view and (not snap or view['seen']>snap['seen']):
                    item['latest_snapshot']={'id':0,'status':'manual_view','seen':view['seen'],'frame_asset':assets.image(view['frame_path']),'payloads':decoded(view['payloads_json'],[])}
                    item['latest_status']='manual_view'
            if item.get('latest_snapshot'):
                latest=item['latest_snapshot']
                frame_path=view['frame_path'] if latest['id']==0 else snap['frame_path']
                for payload in latest.get('payloads',[]):
                    payload_assets(payload, frame_path, assets)
            observation = last_observations.get(cid)
            # This is the red dot before opening the chat, not its current unread
            # state. Missing observations are unknown, never fabricated as read.
            item['unread'] = observation.get('unread') if observation else None
            item['unread_at_observation'] = item['unread']
            item['unread_observed_at'] = observation.get('observed_at') if observation else None
            item['message_row_count'] = db.execute('SELECT count(*) FROM messages WHERE conversation_id=?', (cid,)).fetchone()[0]
            item['messages'] = []
            for msg in db.execute('SELECT m.*,s.frame_path,s.status AS source_snapshot_status'+receipt_fields+
                       ' FROM messages m LEFT JOIN snapshots s ON s.id=m.snapshot_id '+receipt_join+
                       ' WHERE m.conversation_id=? ORDER BY m.seen DESC,m.ordinal DESC LIMIT 200', (cid,)):
                message = dict(msg)
                receipt_event=message.pop('receipt_event_id',None)
                receipt_seen=message.pop('receipt_first_seen',None)
                if receipt_event and receipt_event[:32]==message['id']:
                    message['message_receipt']={'kind':'unique_visual_append','event_id':receipt_event,
                        'snapshot_id':message['snapshot_id'],'ordinal':message['ordinal'],'first_seen':receipt_seen}
                message['payload'] = payload_assets(decoded(message.pop('payload_json')), message.pop('frame_path'), assets)
                item['messages'].append(message)
            item['messages'].reverse()
            report['conversations'].append(item)
        report['summary'] = {
            'conversation_count': len(report['conversations']),
            'message_row_count': db.execute('SELECT count(*) FROM messages').fetchone()[0],
            'snapshot_count': db.execute('SELECT count(*) FROM snapshots').fetchone()[0],
            'snapshot_status_counts': dict(db.execute('SELECT status,count(*) FROM snapshots GROUP BY status')),
        }
        report['source_updated_at'] = db.execute('SELECT max(seen) FROM events').fetchone()[0]
    finally:
        db.rollback()
        db.close()
    # Paths are evidence on Windows, not remotely addressable file requests.
    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()
                    if not (k.endswith('_path') or k in ('frame', 'path', 'traceback', 'windows'))}
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, str) and ('D:\\WeChatMaa' in value or 'C:\\Users\\' in value):
            return Path(value.replace('\\', '/')).name
        return value
    assets.cache = {key: value for key, value in assets.cache.items() if key in assets.active}
    return redact(report), assets.items
