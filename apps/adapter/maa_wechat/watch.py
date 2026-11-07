"""Adaptive Maa desktop loop; badges preempt quiet reads and model waiting."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import time
import numpy as np
from PIL import Image
import liveness
from activity_scheduler import ActivityScheduler
from watch_policy import quiet_scan_allowed,NavigationBackoff,eligible_visible_rows
from store import Store,digest
from flows import box,named_avatar_rows,save_crop,scroll_top,title_matches,conversation_title,progress
from message_vision import extract_message_payloads


def atomic(path,value):
    from state_io import atomic_json
    return atomic_json(path,value)


def normal_folded_entry(image):
    """The normal list's orange folded-chat icon is not an inner-page header."""
    pixels=image[108:143,74:110,:3].astype(np.int16)
    blue,green,red=pixels[:,:,0],pixels[:,:,1],pixels[:,:,2]
    orange=(red>200)&(green>70)&(green<220)&(blue<130)&(red-green>35)
    return int(orange.sum())>=80


def read_rows(runtime,root,label,scheduler=None):
    image,path=runtime.capture(label)
    # The selected Chat icon also covers the folded-chat sublist.
    if runtime.recognize_image('FoldedChatsHeader',image):
        if not runtime.recognize_image('FoldedChatsBack',image):
            if not normal_folded_entry(image): raise ValueError('FOLDED_CHAT_BACK_NOT_VERIFIED')
        else:
            runtime.node('FoldedChatsBack')
            image,path=runtime.capture(label+'-unfolded')
            if runtime.recognize_image('FoldedChatsHeader',image) and not normal_folded_entry(image):
                raise ValueError('FOLDED_CHAT_EXIT_NOT_VERIFIED')
    hits=runtime.recognize_image('ListRows',image,'recent')
    centers=[]
    for hit in hits:
        detail=hit.get('detail') or {}
        if isinstance(detail,str): detail=json.loads(detail)
        centers.extend(detail.get('centers',[]))
    rows=[r for r in named_avatar_rows(centers,runtime.recognize_image('ReadRecentList',image),'recent')
          if re.sub(r'\s+','',r['name']) != '折叠的聊天']
    from recent_name import name_roi,row_name
    blue=runtime.recognize_image('ChatTitleBlueBadge',image,roi=[115,80,285,950])
    for row in rows:
        if name_roi(row['y'],blue):
            row['name'],row['items']=row_name(runtime,image,row['y'])
            row['name_clipped']=False
    badges=[];badge_boxes=[]
    for hit in runtime.recognize_image('UnreadBadges',image):
        _,y,w,h=box(hit)
        if 5<=w<=20 and 5<=h<=20:
            badges.append(y+h/2);badge_boxes.append(box(hit))
    for row in rows:
        row['unread']=any(abs(y-(row['y']-18))<=8 for y in badges)
        row['unread_count']=None
        if row['unread']:
            matches=[b for b in badge_boxes if abs(b[1]+b[3]/2-(row['y']-18))<=8 and b[2]>=12]
            if len(matches)==1:
                x,y,w,h=matches[0];crop=image[y-1:y+h+1,x-1:x+w+1];votes=[]
                for scale in (3,4):
                    resized=np.asarray(Image.fromarray(crop).resize((crop.shape[1]*scale,crop.shape[0]*scale),Image.Resampling.LANCZOS))
                    digits=runtime.recognize_image('Read',resized,roi=[0,0,resized.shape[1],resized.shape[0]])
                    text=''.join(d.get('text','') for d in digits).strip()
                    if re.fullmatch(r'[1-9][0-9]?',text) and all(float(d.get('score',0))>=.8 for d in digits):votes.append(int(text))
                if len(votes)==2 and votes[0]==votes[1]:row['unread_count']=votes[0]
        row['avatar_sha'],_=save_crop(root,image,[78,row['y']-13,24,26],row['name'])
        row['key']=digest([row['name'],row['avatar_sha']])
    if scheduler is not None:
        state=scheduler.dump_state()['conversations']
        unknown=[r for r in rows if state.get(r['key'],{}).get('last_activity_at') is None]
        if unknown:
            times=runtime.recognize_image('ReadRecentTimes',image)
            for row in unknown:
                hints=[h for h in times if row['y']-25<=box(h)[1]+box(h)[3]/2<=row['y']+4 and float(h.get('score',0))>=.85]
                if len(hints)==1: scheduler.register_sidebar_hint(row['key'],hints[0]['text'])
    return rows,image,path


def read_title(runtime,image,row):
    badges=runtime.recognize_image('ChatTitleBlueBadge',image)
    badge_x=min((box(h)[0] for h in badges if box(h)[0]>510 and 5<=box(h)[2]<=25 and 5<=box(h)[3]<=25),default=None)
    items=runtime.recognize_image('ReadChatTitle',image,roi=[490,35,badge_x-493,40] if badge_x else None)
    title=' '.join(i['text'] for i in sorted(items,key=lambda q:box(q)[0])).strip()
    from name_match import group_header
    matched=title_matches(row['name'],title,row.get('name_clipped',False))
    group=group_header(row['name'],title)
    incomplete_group=bool(matched and group and not title.rstrip().endswith((')', '）')))
    if not matched or incomplete_group:
        from send import _ocr_scaled
        enlarged=[]
        roi=[490,35,badge_x-493,40] if badge_x else [490,35,1100,40]
        for scale in (2,3):
            hits=_ocr_scaled(runtime,image,'ReadChatTitle',roi,scale)
            enlarged.append(' '.join(h['text'] for h in sorted(hits,key=lambda q:box(q)[0])).strip())
        from name_match import names_match
        if all(title_matches(row['name'],v,row.get('name_clipped',False)) for v in enlarged) and names_match(*enlarged):
            if not incomplete_group or all(v.rstrip().endswith((')', '）')) and group_header(row['name'],v)==group for v in enlarged):
                return enlarged[0]
        # Preserve the raw partial observation if either scale disagrees. It
        # remains unverified in telemetry; never manufacture the missing suffix.
        if incomplete_group:return title
        raise ValueError('CHAT_TITLE_MISMATCH:'+json.dumps({'expected':row['name'],'observed':title},ensure_ascii=True))
    return title


def consensus(runtime,image,hits,scale=2):
    # Independent scale changes expose recognizer ambiguity. Never replace
    # short Chinese text with a dictionary guess or a fuzzy nearest neighbor.
    enlarged=np.asarray(Image.fromarray(image).resize((round(image.shape[1]*scale),round(image.shape[0]*scale)),Image.Resampling.LANCZOS))
    additional=runtime.recognize_image('ReadChatMessages',enlarged,roi=[round(v*scale) for v in (490,82,1395,805)])
    mapped=[]
    for item in additional:
        value=dict(item);value['box']=[round(v/scale) for v in box(item)];mapped.append(value)
    return [mapped]


def is_group_title(row,title):
    from name_match import group_header
    return group_header(row['name'],title) is not None


def incoming_requests(payloads,result,row,title,conversation,now,enabled_at):
    """Only a stable append from this activation may produce an inbound event."""
    if result['status'] not in ('append','unread_recovery') or now<enabled_at: return []
    group=is_group_title(row,title)
    context=[{'role':'user' if p.get('direction')=='incoming' else 'assistant','content':p['text']}
             for p in payloads[:result['overlap']] if p.get('direction') in ('incoming','outgoing') and p.get('ocr',{}).get('stable') and p.get('text') and len(p['text'])<=2000][-20:]
    output=[]
    for ordinal in result.get('ordinals',range(result['overlap'],len(payloads))):
        p=payloads[ordinal]
        media=p.get('message_kind')=='media' and p.get('visual',{}).get('verified') is True
        if p.get('direction')!='incoming' or (not media and (not p.get('reply_safe') or p.get('message_kind') not in ('text',None))): continue
        sender=p.get('group_sender_name') if group else conversation_title(row['name'],title)
        if group and not p.get('group_sender_verified'): continue
        if not sender or not p.get('text','').strip() or len(p['text'])>2000: continue
        from message_vision import message_identity
        occurrence=result.get('source_ordinals',{}).get(ordinal,result.get('source_ordinals',{}).get(str(ordinal),ordinal))
        event=digest(['maa-incoming-recovery-v2',conversation,result['event_seed'],message_identity(p),occurrence]) if result.get('event_seed') else digest(['maa-incoming-v1',conversation,result['snapshot_id'],ordinal])
        event=result.get('event_ids',{}).get(ordinal,result.get('event_ids',{}).get(str(ordinal),event))
        output.append((ordinal,{'gateway_id':'win-wechat-maa','event_id':event,'conversation_id':conversation,
             'conversation_title':title,'conversation_type':'group' if group else 'private','sender_name':sender,
             'sender_verified':True,'direction':'incoming','stable_ocr':True,'observed_at':now,'text':'[收到图片/视频，内容尚未解析]' if media else p['text'][:2000],'context':context,
             **({'media_notice':True} if media else {})}))
    return output


def dispatched_event(store,event_id):
    """A durable journal entry also covers already terminal, deduplicated work."""
    if store.db.execute('SELECT 1 FROM reply_outbox WHERE event_id=?',(event_id,)).fetchone():return True
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='creation_events'").fetchone():
        return bool(store.db.execute('SELECT 1 FROM creation_events WHERE id=?',(event_id,)).fetchone())
    return False


def message_profile_proof(proof,payload):
    """A contact route proof is not a profile proof for a message avatar."""
    return (isinstance(proof,dict) and proof.get('stable') is True
            and isinstance(proof.get('display_name'),str) and bool(proof['display_name'].strip())
            and bool(re.fullmatch(r'[A-Za-z0-9_-]{3,128}',str(proof.get('wechat_id',''))))
            and all(re.fullmatch(r'[a-f0-9]{64}',str(proof.get(k,''))) for k in
                    ('profile_sha256','message_frame_sha256','message_avatar_sha256'))
            and payload.get('visual',{}).get('avatar_verified') is True
            and proof['message_avatar_sha256']==payload['visual'].get('avatar_sha256'))


def dispatch_incoming(requests,store,client,creation,experiment,runtime,image,payloads,snapshot_id,target,run_id,first_seen=None,result=None):
    """Count persisted dispatches, not OCR candidates or attempted profile clicks.

    Existing reply/creation journals are terminal for this dispatch step. Their
    original send, failure and uncertainty states are never replaced or replayed.
    """
    from private_baseline import receive_only
    if any(receive_only(store.db,body['conversation_id']) for _,body in requests):
        return {'accepted':[],'rejected':[{'ordinal':i,'reason':'PRIVATE_RECEIVE_ONLY'} for i,_ in requests]}
    store.db.execute('''CREATE TABLE IF NOT EXISTS inbound_dispatch
        (event_id TEXT PRIMARY KEY,conversation_id TEXT,event_seed TEXT,source_ordinal INTEGER,
         snapshot_id INTEGER,ordinal INTEGER,request_json TEXT,target_json TEXT,status TEXT,
         reason TEXT,created REAL,updated REAL,UNIQUE(event_seed,source_ordinal))''')
    store.db.commit()
    from creation_client import candidate
    from group_policy import allowed
    result=result or {}
    accepted=[];rejected=[]
    for ordinal,original in requests:
        body=dict(original)
        if first_seen is not None:body['observed_at']=first_seen
        from reply_policy import permits
        origin_seen=first_seen
        if result.get('event_seed') and store.db.execute("SELECT 1 FROM sqlite_master WHERE name='inbound_work'").fetchone():
            origin=store.db.execute('SELECT seen FROM inbound_work WHERE seed=?',(result['event_seed'],)).fetchone()
            if origin:origin_seen=origin[0]
        if not permits(store.db,body,origin_seen):
            rejected.append({'ordinal':ordinal,'reason':'AUTO_REPLY_BEFORE_ACTIVATION'});continue
        occurrence=result.get('source_ordinals',{}).get(ordinal,result.get('source_ordinals',{}).get(str(ordinal),ordinal))
        snapshot_ordinal=result.get('snapshot_ordinals',{}).get(ordinal,ordinal)
        seed=result.get('event_seed') or body['event_id']
        saved=store.db.execute('SELECT event_id,request_json,status,reason FROM inbound_dispatch WHERE event_seed=? AND source_ordinal=?',(seed,occurrence)).fetchone()
        if saved:
            body=json.loads(saved[1])
        event_id=body['event_id']
        if not saved:
            now=time.time()
            store.db.execute('INSERT OR IGNORE INTO inbound_dispatch VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (event_id,body['conversation_id'],seed,occurrence,snapshot_id,snapshot_ordinal,
                 json.dumps(body,ensure_ascii=False),json.dumps(target,ensure_ascii=False),'captured','dispatch_not_started',now,now))
            store.db.commit()
        if dispatched_event(store,event_id):
            store.db.execute("UPDATE inbound_dispatch SET status='queued',reason='durable_event_exists',updated=? WHERE event_id=?",(time.time(),event_id));store.db.commit()
            accepted.append(ordinal);continue
        if saved and saved[2] in ('queued','review'):
            rejected.append({'event_id':event_id,'ordinal':ordinal,'snapshot_id':snapshot_id,
                             'route':'retained','reason':saved[3] or 'INBOUND_DISPATCH_REQUIRES_REVIEW'})
            continue
        kind='reply'
        try:
            if not allowed(getattr(client,'root',Path(store.db.execute('PRAGMA database_list').fetchone()[2]).parent.parent),target,body['conversation_id'],body.get('conversation_type')):
                raise ValueError('GROUP_NOT_WHITELISTED')
            if experiment.applies_to(body):
                kind='experiment'
                experiment.queue_incoming(body,snapshot_id,snapshot_ordinal,target,runtime,image,payloads[ordinal])
            elif candidate(body['text']):
                kind='creation'
                from sender_identity import verify_member
                proof=verify_member(runtime,image,run_id+'-'+event_id[:12],target,payloads[ordinal])
                from sender_directory import SenderDirectory
                directory=SenderDirectory(store)
                directory.remember(body['conversation_id'],payloads[ordinal],proof,target['name'])
                payloads[ordinal].update(directory.enrich(body['conversation_id'],[payloads[ordinal]],target['name'])[0])
                # The creation endpoint rejects extra proof keys. Avatar binding
                # stays in our local sender directory and dispatch journal.
                body['identity']={k:proof[k] for k in ('wechat_id','display_name','profile_sha256',
                    'message_frame_sha256','message_identity','stable') if k in proof}
                store.db.execute('UPDATE inbound_dispatch SET request_json=?,updated=? WHERE event_id=?',
                    (json.dumps(body,ensure_ascii=False),time.time(),event_id));store.db.commit()
                creation.queue_event(body)
            else:
                # 核心人物身份只能来自这条消息头像对应的资料页；昵称不能代替微信号。
                from sender_directory import SenderDirectory
                directory = SenderDirectory(store)
                payload = payloads[ordinal]
                observation = payload.get('sender_observation_key')
                proof_row = store.db.execute(
                    'SELECT proof_json FROM message_sender_observations WHERE observation_key=?',
                    (observation,)).fetchone() if payload.get('sender_identity_verified') else None
                expected_wx=None
                if body.get('conversation_type') == 'private':
                    link = store.db.execute(
                        'SELECT contact_id FROM contact_chat_links WHERE conversation_id=?',
                        (body['conversation_id'],)).fetchone()
                    if link:
                        hist = store.db.execute(
                            'SELECT proof_json FROM contact_chat_history WHERE contact_id=? AND conversation_id=?',
                            (link[0], body['conversation_id'])).fetchone()
                        if hist:
                            from contact_identity import valid_profile_proof
                            route_proof=json.loads(hist[0])
                            if (valid_profile_proof(route_proof,link[0][3:])
                                    and route_proof.get('contact_id')==link[0]
                                    and route_proof.get('conversation_id')==body['conversation_id']):
                                expected_wx=route_proof['wechat_id']
                proof=json.loads(proof_row[0]) if proof_row else None
                fresh=not message_profile_proof(proof,payload)
                if fresh:
                    from sender_identity import verify_member
                    options={'expected_wx':expected_wx} if expected_wx else {}
                    proof = verify_member(runtime,image,run_id+'-identity-'+event_id[:12],target,payload,**options)
                if not message_profile_proof(proof,payload):
                    raise ValueError('MESSAGE_SENDER_PROOF_NOT_BOUND')
                if expected_wx and proof['wechat_id']!=expected_wx:
                    raise ValueError('MESSAGE_SENDER_PROFILE_CONFLICT')
                if fresh:
                    directory.remember(body['conversation_id'], payload, proof, target.get('name'))
                    payload.update(directory.enrich(body['conversation_id'],[payload],target.get('name'))[0])
                body['identity'] = {k:proof[k] for k in ('wechat_id','display_name','profile_sha256',
                    'message_frame_sha256','message_avatar_sha256','stable')}
                if fresh and body['conversation_type'] == 'private':
                    contact_id = 'wx:' + proof['wechat_id']
                    if store.db.execute('SELECT 1 FROM contacts WHERE id=?',(contact_id,)).fetchone():
                        from contact_identity import link_verified_chat
                        # The restored chat frame was verified by verify_member;
                        # adapt its evidence to the separate route-proof schema.
                        route_proof=dict(proof,observation_sha256=proof['message_frame_sha256'])
                        link_verified_chat(store,contact_id,body['conversation_id'],target,route_proof)
                store.db.execute('UPDATE inbound_dispatch SET request_json=?,updated=? WHERE event_id=?',
                    (json.dumps(body,ensure_ascii=False),time.time(),event_id));store.db.commit()
                client.queue(body,snapshot_id,snapshot_ordinal,target)
            if not dispatched_event(store,event_id):raise ValueError('INBOUND_DISPATCH_NOT_PERSISTED')
            store.db.execute("UPDATE inbound_dispatch SET status='queued',reason='dispatch_persisted',updated=? WHERE event_id=?",(time.time(),event_id));store.db.commit()
            accepted.append(ordinal)
        except Exception as exc:
            detail={'event_id':event_id,'ordinal':ordinal,'snapshot_id':snapshot_id,
                    'route':kind,'reason':str(exc)[:160]}
            rejected.append(detail)
            store.db.execute("UPDATE inbound_dispatch SET status='review',reason=?,updated=? WHERE event_id=?",(detail['reason'],time.time(),event_id));store.db.commit()
            store.event('creation_identity_rejected' if kind=='creation' else 'inbound_dispatch_rejected',detail)
            if detail['reason'] in ('SENDER_EXISTING_POPUP','SENDER_PROFILE_NOT_RESTORED',
                    'SENDER_PROFILE_NOT_DISMISSED','SENDER_CHAT_NOT_RESTORED',
                    'MESSAGE_MOVED_DURING_PROFILE_CHECK','SENDER_PROFILE_WINDOW_NOT_UNIQUE'):
                raise
    return {'accepted':accepted,'rejected':rejected}


def record_dispatch_outcome(inbound_work,row,result,expected,dispatch,store):
    """A failed dispatch retains the original capture and seed for review."""
    accepted=set(dispatch['accepted']);missing=[i for i in expected if i not in accepted]
    if not missing:
        inbound_work.update(row['key'],'queued','reply_queued',result['snapshot_id'])
        return
    reason='dispatch_not_persisted' if dispatch['rejected'] else 'partially_unverified'
    inbound_work.update(row['key'],'review',reason,result['snapshot_id'])
    work=inbound_work.get(row['key'])
    store.event('inbound_dispatch_review',{'row_key':row['key'],'snapshot_id':result['snapshot_id'],
                'event_seed':work['seed'],'missing_ordinals':missing,'accepted_ordinals':list(accepted),
                'rejected':dispatch['rejected']})


def refresh_selected_position(runtime,image,row,selected):
    """最近列表重排后，复用当前独立核验的已打开会话，而非旧行坐标。"""
    if len(selected)!=1:return False
    x,y,w,h=box(selected[0]);center=y+h//2
    if center==row['y'] or not 90<=center<=1010:return False
    photo=image[center-13:center+13,78:102,:3]
    if photo.shape!=(26,24,3) or hashlib.sha256(photo.tobytes()).hexdigest()!=row['avatar_sha']:
        return False
    from recent_name import row_name
    from name_match import names_match
    name,_=row_name(runtime,image,center)
    if not names_match(name,row['name']):return False
    try:read_title(runtime,image,row)
    except ValueError:return False
    # 仅在同一新帧中的唯一选中行、昵称、头像和标题全部相符后改坐标。
    previous=row['y'];row['y']=center
    runtime.selection_position_refresh={'previous_y':previous,'verified_y':center,
        'name':row['name'],'avatar_sha':row['avatar_sha']}
    return True


def select(runtime,row):
    # Re-clicking an already open row is unnecessary and can toggle WeChat's
    # selected state. Reuse only a proven active target, not a remembered click.
    runtime.selection_failure=None
    runtime.selection_diagnostics=None
    runtime.selection_position_refresh=None
    last_node=None
    def reject(reason,image,hits):
        detail={'reason':reason,'expected_name':row['name'],'expected_y':row['y'],
                'selected_boxes':[box(hit) for hit in hits],'node_result':last_node}
        directory=getattr(runtime,'navigation_evidence_dir',None)
        if isinstance(directory,(str,Path)):
            path=Path(directory)/('selection-failure-'+str(time.time_ns())+'.png')
            try:
                Image.fromarray(image[:,:,:3][:,:,::-1]).save(path)
                detail['frame_path']=str(path)
                detail['frame_sha256']=hashlib.sha256(image.tobytes()).hexdigest()
            except OSError:detail['frame_save_failed']=True
        runtime.selection_diagnostics=detail
        return False
    for attempt in range(2):
        current,_=runtime.capture(getattr(runtime,'run_id','maa')+'-selection-check')
        selected=runtime.recognize_image('ContactSelectedRow',current)
        if refresh_selected_position(runtime,current,row,selected):return True
        if any(box(h)[1]<=row['y']<box(h)[1]+box(h)[3] for h in selected):
            try: read_title(runtime,current,row);return True
            except ValueError: pass
        result=runtime.node('SelectRecentRow',{'SelectRecentRow':{'target':[200,row['y'],1,1],
            'custom_recognition_param':{'y':row['y'],'name':row['name'],'avatar_sha':row['avatar_sha']}}},allow_miss=True)
        last_node=result
        if not result['succeeded']: return reject('guarded_navigation_missed',current,selected)
        after,_=runtime.capture(getattr(runtime,'run_id','maa')+'-selection-after')
        active=runtime.recognize_image('ContactSelectedRow',after)
        if any(box(h)[1]<=row['y']<box(h)[1]+box(h)[3] for h in active):
            try: read_title(runtime,after,row);return True
            except ValueError as exc:
                runtime.selection_failure={'reason':str(exc),'frame':_}
                return reject('selected_title_mismatch',after,active)
        # No selected row/empty chat proves a selection miss, not another
        # target. A second guarded navigation click is safe; send is never retried.
        # PostMessage 完成不等于微信已重绘。先在下一次循环读新帧；若仍未
        # 选中，只有原昵称和头像再次通过 Custom guard 才允许第二次导航。
        # 最多两次导航，标题不匹配仍立即停止；这里没有任何草稿或发送动作。
    return reject('selected_row_not_confirmed',after,active)


def sidebar_changes(rows,image,previews,selected_state):
    """Wake on changed previews or the open chat's pixels, without fake activity.

    An open conversation does not acquire an unread badge. Pixel change is a
    read hint only: the usual ordered message diff must still prove new input.
    """
    changed=[];selected=[]
    for row in rows:
        b,g,r=[int(v) for v in image[row['y'],65,:3]]
        active=g>120 and r<100 and b<170
        if active: selected.append(row)
        old=previews.get(row['key'])
        new={'sha':hashlib.sha256(image[row['y']+1:row['y']+29,115:465,:3].tobytes()).hexdigest(),
             'time_sha':hashlib.sha256(image[row['y']-25:row['y'],400:465,:3].tobytes()).hexdigest(),
             'selected':active}
        # 两种选中背景分别保存最后观测，不能在导航换色时覆盖另一种背景的
        # 未处理像素边沿。背景变化本身不是新消息，正文仍由有序气泡证据确认。
        comparable=old if isinstance(old,dict) and old.get('selected') is active else (
            old.get('alternate') if isinstance(old,dict) else None)
        if (isinstance(comparable,dict) and comparable.get('selected') is active
                and comparable.get('sha') and (comparable['sha']!=new['sha']
                or comparable.get('time_sha') and comparable['time_sha']!=new['time_sha'])):
            changed.append(dict(row,wake_reason='preview_pixels'))
        if isinstance(old,dict):
            alternate=old if old.get('selected') is not active else old.get('alternate')
            if isinstance(alternate,dict):
                new['alternate']={k:alternate[k] for k in ('sha','time_sha','selected') if k in alternate}
        previews[row['key']]=new
    current={}
    if len(selected)==1:
        row=selected[0];sha=hashlib.sha256(image[82:887,490:1885,:3].tobytes()).hexdigest()
        current={'key':row['key'],'sha':sha}
        if selected_state.get('key')==row['key'] and selected_state.get('sha')!=sha and not any(r['key']==row['key'] for r in changed):
            changed.append(dict(row,wake_reason='chat_pixels'))
    return changed,current


def filter_changed_wakes(changed,cooldowns,now):
    """Defer only noisy chat-area pixels; badges and preview evidence bypass."""
    return [row for row in changed if row.get('unread') or row.get('wake_reason')!='chat_pixels' or cooldowns.get(row['key'],0)<=now]


def selected_after_wake_filter(previous,current,changed,filtered):
    """Keep a deferred pixel edge observable after the short cooldown expires."""
    if previous.get('key')==current.get('key') and any(
        row['key']==current.get('key') and row.get('wake_reason')=='chat_pixels' and row not in filtered for row in changed):
        return previous
    return current


def planned_visible_reads(urgent,changed,warmup,message_pending,inbound_pending,ordinary):
    """Fresh visible input and one-time activation reads precede retry work."""
    if urgent:return urgent
    if changed:
        return [dict(changed[0],schedule={'reason':'visible_message_change',
            'wake_reason':changed[0].get('wake_reason'),'priority':1000,'weight':1})]
    if warmup is not None:return [warmup]
    return message_pending or inbound_pending or ordinary


def pending_visible_wakes(rows,hints,pending,now):
    """Keep a bounded read hint until observed; it grants no dispatch authority."""
    for key,value in list(pending.items()):
        if (not isinstance(value,dict) or type(value.get('seen')) not in (int,float)
                or not 0<=now-value['seen']<=300):pending.pop(key,None)
    for row in hints:
        old=pending.get(row['key'])
        if not old or old.get('avatar_sha')!=row.get('avatar_sha'):
            pending[row['key']]={'seen':now,'avatar_sha':row.get('avatar_sha'),
                'name':row.get('name'),'wake_reason':row.get('wake_reason')}
        elif row.get('wake_reason')=='preview_pixels':old['wake_reason']='preview_pixels'
    if len(pending)>100:
        for key in sorted(pending,key=lambda k:pending[k]['seen'])[:-100]:pending.pop(key,None)
    return [dict(row,wake_reason=pending[row['key']]['wake_reason']) for row in rows
            if row['key'] in pending and pending[row['key']].get('avatar_sha')==row.get('avatar_sha')
            and pending[row['key']].get('name')==row.get('name')]


def finish_read_hint(row, source, current, pending, now):
    """读取期间的新像素必须留下重读提示；旧帧读完不能确认新视口已读。"""
    if (source.shape == current.shape
            and np.array_equal(source[35:887,490:1885,:3],current[35:887,490:1885,:3])):
        pending.pop(row['key'],None)
        return False
    pending[row['key']]={'seen':now,'avatar_sha':row['avatar_sha'],
                         'name':row['name'],'wake_reason':'post_read_pixels'}
    return True


def list_fingerprint(image):
    return hashlib.sha256(image[80:1030,65:465,:3].tobytes()).hexdigest()


def seek_cold_page(runtime,label,wanted,scheduler,image,path):
    """Restore a wheel-distance bookmark with at most two bounded actions.

    A bookmark is a scroll *distance*, not a claim about exact viewport pages:
    Qt may clamp a large wheel delta. Selection always verifies the actual
    visible name, avatar and title afterward. Win32's signed 16-bit wheel
    increment is kept below 32768. Red badges and the top-scan deadline are
    checked between actions; an unfinished seek must not advance the bookmark.
    """
    if type(wanted) is not int or not 0<=wanted<=100: raise ValueError('INVALID_COLD_BOOKMARK')
    reached=0
    while reached<wanted:
        if scheduler.badge_scan_due(): return reached,image,path,'badge_scan_due'
        step=min(50,wanted-reached)  # 50 * the old 5-notch batch = 30000.
        before=list_fingerprint(image)
        runtime.node('ContactsScrollDown',{'ContactsScrollDown':{'dy':-600*step,
            'repeat':1,'repeat_delay':0,'post_delay':200}})
        reached+=step
        image,path=runtime.capture(label+'-seek-'+str(reached))
        if runtime.recognize_image('UnreadBadges',image): return reached,image,path,'unread'
        if before==list_fingerprint(image): return reached,image,path,'scroll_unchanged'
    return reached,image,path,'reached'


def send_ready_on_rows(client,runtime,run_id,rows,*,allow_opening=True):
    """Ready replies may match any observed page, never a nickname alone."""
    if client is None: return False
    for item in client.ready():
        body=json.loads(item['request_json'])
        if not allow_opening and (body.get('_opening') or body.get('_greeting')):continue
        target=(body.get('_target') or {})
        from name_match import names_match
        match=[r for r in rows if names_match(r['name'],target.get('name')) and r['avatar_sha']==target.get('avatar_sha')]
        if len(match)!=1: continue
        row=match[0]
        if json.loads(item['request_json']).get('_opening') and row.get('unread'): continue
        if not select(runtime,row): continue
        client.send(item,runtime,run_id,dict(target,row_y=row['y']))
        return True
    return False


def ready_page_hint(client,max_pages,tried,now):
    """Use the originating viewport as a hint, with bounded stale-hint retries."""
    if client is None: return None
    for item in client.ready():
        event=item['event_id'];target=(json.loads(item['request_json']).get('_target') or {})
        page=target.get('list_page')
        if type(page) is not int or not 0<page<max_pages: continue
        if now-tried.get(event,float('-inf'))<15: continue
        tried[event]=now
        return page
    return None


def advance_cold_bookmark(previous,previous_hash,wanted,page_hash,reason):
    """Only completed seeks or actual unmoving pixels alter scan progress."""
    if reason=='scroll_unchanged' or (reason=='reached' and previous_hash==page_hash): return 0,None
    if reason=='reached': return wanted,page_hash
    return previous,previous_hash


def startup_warmup(rows,states,attempted,*,enabled,urgent=False):
    """Pick one fresh-activation baseline, independent of persisted backoff.

    This pure selector changes neither activity nor baseline state. A failed
    read is still attempted once, but only the existing reliable snapshot path
    can establish an activation baseline. Retired chats remain badge-only.
    """
    if not enabled or urgent: return None
    candidates=[]
    for index,row in enumerate(rows):
        state=states.get(row.get('key'))
        if not row.get('name') or row.get('key') in attempted or state is None or state.get('retired'): continue
        weight=state.get('weight',0)
        candidates.append((-weight,index,dict(row,schedule={'reason':'startup_warmup','priority':100+weight,'weight':weight})))
    return min(candidates,key=lambda item:item[:2])[2] if candidates else None


def resume_activation(data, store, run_id, now):
    """Consume an operator-created, short-lived code-update checkpoint once.

    Normal restarts still establish history without replying. A planned update
    can retain explicitly recorded, already verified baselines in the SAME
    activation; chat text and telemetry cannot create this local checkpoint.
    """
    path=Path(data)/'activation-update.json'
    try: raw=path.read_text(encoding='utf-8')
    except FileNotFoundError: return None
    path.unlink()
    try:
        value=json.loads(raw);old=json.loads((Path(data)/'reply-runtime.json').read_text(encoding='utf-8'))
        if (value['new_run_id']!=run_id or value['old_run_id']!=old['run_id']
                or value['enabled_at']!=old['enabled_at'] or old.get('auto_reply_enabled') is not True
                or not 0<=now-value['created_at']<=120 or not 0<value['enabled_at']<=value['created_at']): return None
        baselines=set();routes=set()
        for cid,sid in value['baselines'].items():
            row=store.db.execute('SELECT conversation_id,status,seen FROM snapshots WHERE id=?',(sid,)).fetchone()
            if not row or row[0]!=cid or row[1] not in ('baseline','append','unchanged') or not value['enabled_at']<=row[2]<=value['created_at']: return None
            baselines.add(cid)
            route=store.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(cid,)).fetchone()
            if route:
                target=json.loads(route[0])
                if now-row[2]<=300:routes.add(digest([target['name'],target['avatar_sha']]))
        store.event('activation_update_resumed',{'run_id':run_id,'previous_run_id':old['run_id'],'baseline_count':len(baselines)})
        return value['enabled_at'],baselines,routes
    except (ValueError,KeyError,TypeError,FileNotFoundError): return None


def watch(runtime,root,run_id,cycles=1,max_pages=30,auto_reply=False,continuous=False,*,activation=None):
    if type(max_pages) is not int or not 1<=max_pages<=100: raise ValueError('INVALID_MAX_PAGES')
    root=Path(root);data=root/'data';store=Store(data/'wechat.sqlite3');state_path=data/'activity-scheduler.json'
    runtime.navigation_evidence_dir=data
    from inbound_work import InboundWork
    inbound_work=InboundWork(store)
    from inbound_messages import InboundMessages
    inbound_messages=InboundMessages(store)
    def inbound_health():
        legacy=inbound_work.health();messages=inbound_messages.health()
        counts=dict(legacy['status_counts'])
        for status in ('pending','review'):
            count=messages['status_counts'].get(status,0)
            if count:counts[status]=counts.get(status,0)+count
        return dict(legacy,status_counts=counts,messages=messages)
    def inbound_count():
        counts=inbound_messages.health()['status_counts']
        return inbound_work.count()+sum(counts.get(status,0) for status in ('pending','review'))
    try:
        persisted=json.loads(state_path.read_text(encoding='utf-8'));scheduler=ActivityScheduler(persisted)
    except (FileNotFoundError,ValueError): persisted={};scheduler=ActivityScheduler()
    client=None;reconciler=None
    if auto_reply:
        from reply_client import ReplyClient
        client=ReplyClient(root,store)
        from reply_reconcile import ReplyReconciler
        reconciler=ReplyReconciler(root,store)
    enabled_at=time.time();activation_baselines=set();warmup_attempted=set();cold_page=0
    resumed=activation.restore(run_id,enabled_at) if auto_reply and activation is not None else None
    if resumed is None and auto_reply: resumed=resume_activation(data,store,run_id,enabled_at)
    if resumed: enabled_at,activation_baselines,warmup_attempted=resumed
    if auto_reply and activation is not None:
        activation.remember(run_id,enabled_at,activation_baselines,warmup_attempted)
    cold_hash=persisted.get('watch_cold_hash');tick=0;reads=0;rejected=0;interruptions=0
    previews=persisted.get('watch_previews',{});selected_state=persisted.get('watch_selected',{});ready_page_tried={};chat_pixel_cooldowns={};navigation_backoff=NavigationBackoff();copy_cache={}
    pending_wakes=persisted.get('watch_pending_wakes',{})
    if not isinstance(pending_wakes,dict):pending_wakes={}
    from inactive_chats import InactiveChats
    inactive_cleanup=InactiveChats(root,store)
    def save_scheduler():
        atomic(state_path,dict(scheduler.dump_state(),watch_cold_page=cold_page,watch_cold_hash=cold_hash,
            watch_previews=dict(list(previews.items())[-1000:]),watch_selected=selected_state,
            watch_pending_wakes=pending_wakes))
    from group_invites import initialize,policy as invite_policy
    initialize(store)
    from friend_requests import FriendRequests
    friend_requests=FriendRequests(root,store)
    from manual_gui import recover_interrupted_gui
    recover_interrupted_gui(store)
    from creation_client import CreationClient
    creation=CreationClient(root,store)
    from greeting_client import GreetingClient
    experiment=GreetingClient(root,store,client)
    atomic(data/'reply-runtime.json',{'run_id':run_id,'enabled_at':enabled_at,'auto_reply_enabled':auto_reply,'continuous':continuous})
    from desktop_input import InputDesktopGate
    input_gate=InputDesktopGate();open_recent_pending=True;input_was_waiting=False
    try:
        while continuous or tick<cycles:
            liveness.touch()
            desktop_input=input_gate.check(runtime,run_id)
            atomic(data/'desktop-input.json',desktop_input)
            if client: client.pump()
            experiment.pump()
            creation.pump()
            if not desktop_input['ready']:
                input_was_waiting=True
                progress(root,{'mode':'recent','status':'waiting_for_input','run_id':run_id,
                    'cycle':tick,'chats_read':reads,'identity_rejections':rejected,
                    'continuous':continuous,'sending_enabled':False,'auto_reply_enabled':False,
                    'auto_reply_configured':auto_reply,'manual_commands_enabled':False,
                    'desktop_input':desktop_input,'inbound_health':inbound_health(),
                    'pending_replies':len(store.reply_rows(('queued','ready','send_started'))),
                    'pending_inbound_work':inbound_count(),
                    'pending_media_deliveries':creation.pending_deliveries()})
                time.sleep(1);continue
            if input_was_waiting:
                progress(root,{'mode':'recent','status':'running','run_id':run_id,'cycle':tick,
                    'chats_read':reads,'identity_rejections':rejected,'continuous':continuous,
                    'sending_enabled':auto_reply,'auto_reply_enabled':auto_reply,
                    'auto_reply_configured':auto_reply,'manual_commands_enabled':True,
                    'desktop_input':desktop_input})
                input_was_waiting=False
            if open_recent_pending:
                runtime.node('OpenRecent');open_recent_pending=False
            from watch_policy import polling_enabled
            if creation.process_one(runtime,run_id):
                save_scheduler();continue
            from manual_gui import process_one
            if process_one(runtime,root,store,run_id):
                save_scheduler();continue
            from watch_policy import polling_enabled
            if not polling_enabled(root):
                progress(root,{'mode':'recent','status':'polling_paused','run_id':run_id,
                    'continuous':continuous,'polling_enabled':False,'sending_enabled':False,
                    'desktop_input':desktop_input,
                    'auto_reply_enabled':False,'manual_commands_enabled':True,
                    'pending_media_deliveries':creation.pending_deliveries()})
                time.sleep(1);continue
            if continuous and friend_requests.process(runtime,run_id):
                save_scheduler();continue
            if not scheduler.badge_scan_due(): time.sleep(.2);continue
            tick+=1
            scroll_top(runtime,f'{run_id}-top')
            visible_rows,image,path=read_rows(runtime,root,run_id+'-recent-live',scheduler)
            rows,excluded_rows=eligible_visible_rows(root,store,visible_rows)
            # Collect completed network work before any further quiet GUI work.
            creation.pump()
            if creation.delivery_ready(): continue
            inbound_work.observe(rows)
            inbound_work.retire_badges(rows)
            visible_page=0
            hints,next_selected=sidebar_changes(rows,image,previews,selected_state)
            deferred=pending_visible_wakes(rows,hints,pending_wakes,time.time())
            changed=filter_changed_wakes(deferred,chat_pixel_cooldowns,time.time())
            selected_state=selected_after_wake_filter(selected_state,next_selected,hints,changed)
            scheduler.mark_badge_scan()
            # Ready red-derived replies have precedence over further quiet OCR.
            if send_ready_on_rows(client,runtime,run_id,rows,allow_opening=False):
                save_scheduler();continue
            # A persisted unchanged backoff may be an hour long, while this
            # process has no activation baseline yet. Warm current active rows
            # once before cold traversal; selection itself never renews activity.
            warmup=None
            quiet=not creation.delivery_in_flight()
            if auto_reply and quiet:
                for row in rows:
                    if row.get('name'): scheduler.register(row['key'])
                warmup=startup_warmup([r for r in rows if navigation_backoff.eligible(r)],{r['key']:scheduler.status(r['key']) for r in rows if r.get('name')},
                    warmup_attempted,enabled=True,urgent=any(r['unread'] for r in rows) or bool(changed))
            # Automatic reads never leave the current top viewport. Old page
            # bookmarks remain historical evidence, not navigation permission.
            creation.pump()
            if creation.delivery_ready(): continue
            available=[r for r in rows if navigation_backoff.eligible(r)]
            changed=[r for r in changed if navigation_backoff.eligible(r)]
            message_pending=inbound_messages.pending(available)
            # Fresh unread input takes precedence over verification retries in
            # another chat. The saved occurrence remains due for a later tick.
            urgent=[r for r in available if r.get('unread')]
            planned=planned_visible_reads(
                scheduler.plan(urgent,active_budget=0,unread_budget=1) if urgent else [],
                changed,warmup,message_pending,inbound_work.pending(available),
                scheduler.plan([r for r in available if r['name']],active_budget=2 if quiet else 0,unread_budget=1))
            planned=[row for row in planned if inbound_work.eligible(row) and navigation_backoff.eligible(row)]
            # Uncertain receipts converge only when fresh observation shows no live work waiting.
            if not planned and reconciler and not (client and client.ready()) and not inbound_count() and not creation.delivery_in_flight():
                if reconciler.process_one(runtime, run_id):
                    save_scheduler(); continue
            for row in planned:
                creation.pump()
                if creation.delivery_ready(): break
                if row['schedule']['reason'] in ('due','baseline','startup_warmup') and creation.delivery_in_flight(): break
                # Failures do not count as a baseline; the attempted set only
                # prevents one failing row from monopolising startup work.
                if auto_reply: warmup_attempted.add(row['key'])
                from name_match import names_confusable
                similar=[r for r in rows if names_confusable(r.get('name'),row['name'])]
                if len(similar)>1:
                    store.event('polling_name_ambiguous',{'name':row['name'],'candidates':[r['name'] for r in similar]})
                    navigation_backoff.failed(row);rejected+=1;continue
                if not select(runtime,row):
                    navigation_backoff.failed(row)
                    inbound_work.navigation_failed(row['key'])
                    rejected+=1;scheduler.observe(row['key'],False,reliable=False,was_unread=bool(row['unread']));store.event('recent_row_changed',{'run_id':run_id,'name':row['name'],'key':row['key'],'frame':path,'selection_failure':getattr(runtime,'selection_failure',None) if isinstance(getattr(runtime,'selection_failure',None),dict) else None,'selection_diagnostics':getattr(runtime,'selection_diagnostics',None) if isinstance(getattr(runtime,'selection_diagnostics',None),dict) else None});continue
                refresh=getattr(runtime,'selection_position_refresh',None)
                if isinstance(refresh,dict):store.event('selected_conversation_position_refreshed',refresh)
                chat,chat_path=runtime.capture(run_id+'-chat-'+str(time.time_ns()))
                captured_at=time.time()
                try: title=read_title(runtime,chat,row)
                except ValueError:
                    navigation_backoff.failed(row)
                    inbound_work.navigation_failed(row['key'])
                    rejected+=1;scheduler.observe(row['key'],False,reliable=False,was_unread=bool(row['unread']));store.event('title_mismatch',{'expected':row['name'],'frame':chat_path});continue
                navigation_backoff.succeeded(row)
                from text_copy import read_messages
                group=is_group_title(row,title)
                cache_key=(title,row['avatar_sha'])
                payloads,ocr=read_messages(runtime,chat,group_chat=group,previous_payloads=copy_cache.get(cache_key))
                copy_cache[cache_key]=payloads
                if len(copy_cache)>100:copy_cache.pop(next(iter(copy_cache)))
                from group_invites import process_card
                if process_card(runtime,root,store,row,title,chat,ocr,run_id+'-invite-'+str(time.time_ns())):
                    warmup_attempted.discard(row['key'])
                    interruptions+=1;break
                if group:
                    from sender_ocr import refine_group_senders
                    payloads=refine_group_senders(runtime,chat,payloads)
                try:conversation=store.group_identity(row['name'],title,row['avatar_sha'],payloads)
                except ValueError as exc:
                    if str(exc) not in ('GROUP_NAME_AMBIGUOUS','PRIVATE_NAME_AMBIGUOUS'):raise
                    rejected+=1
                    store.event('polling_group_identity_review' if group else 'polling_private_identity_review',{'name':row['name'],'reason':str(exc),'frame':chat_path})
                    navigation_backoff.failed(row)
                    continue
                target={'name':row['name'],'name_clipped':row.get('name_clipped',False),'conversation_title':title,
                        'avatar_sha':row['avatar_sha'],'row_y':row['y'],'list_page':visible_page}
                from sender_directory import SenderDirectory
                directory=SenderDirectory(store)
                payloads=directory.enrich(conversation,payloads,row['name'])
                store.frame('chat',conversation,chat_path,hashlib.sha256(chat.tobytes()).hexdigest(),ocr)
                from reply_policy import permits
                from private_baseline import receive_only
                allow_reply=not receive_only(store.db,conversation)
                head=inbound_messages.head(conversation)
                prior_work=inbound_work.get(row['key'])
                # First observation after upgrading never relabels an old review
                # or capture as new. Subsequent exact appends have independent work.
                suppress_new=head is None and bool(prior_work and prior_work['status'] in ('review','captured'))
                source_seen=prior_work['seen'] if head is None and prior_work and prior_work['status']=='pending' else captured_at
                result=store.snapshot(conversation,title,row['avatar_sha'],chat_path,payloads,
                    captured_at=captured_at,
                    occurrence_enricher=lambda r,p:directory.transfer_occurrences(conversation,p,r,title),
                    reconciliation_guard=(lambda r,p:inbound_work.guard_review_append(row,r,p)) if head is None else None,
                    observation_head=head,
                    on_observation=lambda r,p:inbound_messages.record(conversation,target,r,p,
                        enabled=bool(client and allow_reply and permits(store.db,{'conversation_id':conversation,'observed_at':source_seen},source_seen)),
                        suppress_new=suppress_new,now=source_seen))
                store.observe_group(conversation,row['name'],title)
                store.route(conversation,{'name':row['name'],'name_clipped':row.get('name_clipped',False),'conversation_title':title,'avatar_sha':row['avatar_sha'],'row_y':row['y'],'list_page':visible_page})
                if group:
                    # Persist candidates before any profile click. Resolve only
                    # pending occurrences, rather than rereading historical people.
                    now=time.time()
                    indices=[int(i) for i,event in result.get('message_event_ids',{}).items()
                             if (item:=inbound_messages.item(event)) and item['status']=='pending' and item['next_attempt']<=now]
                    if head is None and not indices:indices=list(range(len(payloads)))
                    try:
                        resolved=directory.resolve_visible(runtime,chat,conversation,[payloads[i] for i in indices],target,run_id+'-sender')
                        for index,payload in zip(indices,resolved):payloads[index]=payload
                    except ValueError as exc:
                        if str(exc)!='MESSAGE_MOVED_DURING_PROFILE_CHECK':raise
                        navigation_backoff.failed(row);inbound_work.navigation_failed(row['key'])
                        scheduler.observe(row['key'],False,reliable=False,was_unread=bool(row['unread']))
                        store.event('polling_sender_identity_review',{'conversation':conversation,'name':row['name'],
                            'reason':str(exc),'frame':chat_path,'action':'persisted_candidates_reobserve_no_dispatch'})
                        rejected+=1;continue
                inbound_work.record_context_gap(row,result)
                if result['status']=='append':
                    for ordinal,p in enumerate(payloads[result['overlap']:],start=result['overlap']):
                        if p.get('direction')=='incoming':
                            store.event('message_received',{'conversation':conversation,'name':row['name'],'snapshot_id':result['snapshot_id'],'ordinal':ordinal,'reply_safe':bool(p.get('reply_safe'))})
                # Persisted unique message overlap remains valid across a
                # restart. Unknown unread history needs a verified badge count
                # and bounded complete tail, or a durable review record.
                if result['status'] in ('baseline','append','unchanged'):activation_baselines.add(conversation)
                work=inbound_work.get(row['key'])
                retry_first_seen=work['seen'] if work and work['reason']=='unstable_append' else None
                message_mode=bool(result.get('message_event_ids') or head is not None) and not (work and work['status']=='captured')
                if message_mode and work and work['status']=='pending' and result.get('registered_message_ordinals'):
                    inbound_work.update(row['key'],'delegated','per_message_receipts',result['snapshot_id'])
                    work=inbound_work.get(row['key'])
                if (not message_mode and work and work['status']=='review' and work['reason']=='context_gap_not_verified' and result['status']=='append'
                        and not result.get('metadata',{}).get('independent_review_append')):
                    repaired=inbound_work.repair_review_append(row,result,payloads)
                    result=repaired or dict(result,status='review',new=[],
                        metadata=dict(result.get('metadata',{}),reason='GAP_REVIEW_REQUIRES_OPERATOR'))
                if not message_mode and work and work['status'] in ('pending','captured'):
                    recovered=inbound_work.recover(row,payloads)
                    if recovered:
                        for i,original in recovered:
                            # Captured text stays verbatim; the current frame owns
                            # coordinates, avatars, names and profile bindings.
                            payloads[i]=dict(payloads[i],text=original['text'],ocr=original.get('ocr',{}),
                                             reply_safe=bool(original.get('reply_safe') and payloads[i].get('reply_safe')))
                        payloads=directory.enrich(conversation,payloads,row['name'])
                        result=dict(result,status='unread_recovery',overlap=0,ordinals=[i for i,_ in recovered],event_seed=work['seed'])
                        result['source_ordinals']=inbound_work.source_ordinals(row,recovered)
                    elif result['status']!='append':
                        if inbound_work.review_context_gap(row,result):
                            store.event('inbound_review',{'name':row['name'],'reason':'context_gap_not_verified','snapshot_id':result['snapshot_id']})
                        elif not inbound_work.retry_unstable_append(row,result):
                            inbound_work.update(row['key'],'review','unread_tail_not_verified',result['snapshot_id'])
                            store.event('inbound_review',{'name':row['name'],'reason':'unread_tail_not_verified','snapshot_id':result['snapshot_id']})
                has_new=bool(result.get('registered_message_ordinals')) or (result['status']=='append' and any(p.get('direction') in ('incoming','outgoing') for p in payloads[result['overlap']:]))
                if has_new: chat_pixel_cooldowns.pop(row['key'],None)
                elif result['status'] in ('baseline','unchanged'): chat_pixel_cooldowns[row['key']]=time.time()+15
                scheduler.observe(row['key'],has_new,reliable=result['status'] not in ('gap','ambiguous','review'),was_unread=bool(row['unread']))
                if client and message_mode:
                    inbound_messages.process(result,payloads,row,title,target,client,creation,experiment,
                        runtime,chat,run_id,enabled_at)
                elif client and allow_reply:
                    requests=incoming_requests(payloads,result,row,title,conversation,time.time(),enabled_at)
                    if result['status'] in ('append','unread_recovery') and any(payloads[i].get('direction')=='incoming' for i in result.get('ordinals',range(result['overlap'],len(payloads)))):
                        ordinals=result.get('ordinals',[i for i in range(result['overlap'],len(payloads)) if payloads[i].get('direction')=='incoming'])
                        captured=inbound_work.capture(row,result,payloads,ordinals)
                        work=inbound_work.get(row['key'])
                        result['event_seed']=work['seed']
                        if not captured:result=dict(result,status='review',new=[])
                        requests=incoming_requests(payloads,result,row,title,conversation,time.time(),enabled_at)
                    if requests:
                        target={'name':row['name'],'name_clipped':row.get('name_clipped',False),'conversation_title':title,'avatar_sha':row['avatar_sha'],'row_y':row['y'],'list_page':visible_page}
                        dispatch=dispatch_incoming(requests,store,client,creation,experiment,runtime,chat,payloads,result['snapshot_id'],target,run_id,retry_first_seen,result)
                        expected=[i for i in result.get('ordinals',range(result['overlap'],len(payloads))) if payloads[i].get('direction')=='incoming']
                        record_dispatch_outcome(inbound_work,row,result,expected,dispatch,store)
                    elif (inbound_work.get(row['key']) or {}).get('status') in ('pending','captured') and not (result['status']=='review' and (inbound_work.get(row['key']) or {}).get('reason')=='unstable_append'):
                        inbound_work.update(row['key'],'review','sender_or_content_not_verified',result['snapshot_id'])
                try:
                    needs_reread=finish_read_hint(row,chat,runtime.capture_image(),pending_wakes,time.time())
                except (RuntimeError,OSError):
                    # 截图暂不可用也不能确认旧提示已处理；只重读，不新增来信。
                    pending_wakes[row['key']]={'seen':time.time(),'avatar_sha':row['avatar_sha'],
                                              'name':row['name'],'wake_reason':'post_read_capture_pending'}
                    needs_reread=True
                # 只确认实际读取过的源帧预览。读取后才到达的内容必须继续
                # 对比这个旧帧，不能被下一次“已选中”背景转换吞掉。
                source_avatar=hashlib.sha256(np.ascontiguousarray(chat[row['y']-13:row['y']+13,78:102,:3]).tobytes()).hexdigest()
                if source_avatar==row['avatar_sha']:
                    _hints,captured_selected=sidebar_changes([row],chat,previews,{})
                    if captured_selected:selected_state=captured_selected
                store.event('chat_observed',{'conversation':conversation,'name':row['name'],'snapshot_id':result['snapshot_id'],'run_id':run_id,'unread':row['unread'],
                    'captured_at':captured_at,'processed_at':time.time(),'post_read_changed':needs_reread,
                    'diff_status':result['status'],'new_count':len(result['new']),'context_recovery':result.get('metadata'),'schedule':row['schedule'],
                    'stable_incoming':sum(p.get('direction')=='incoming' and p.get('reply_safe',False) for p in payloads),'auto_reply_enabled':auto_reply and allow_reply,'receive_only':not allow_reply})
                reads+=1
                creation.pump()
                if not has_new or scheduler.badge_scan_due(): interruptions+=1;break
            # No proactive contact greetings or hide-chat cleanup in this scope.
            save_scheduler()
            progress(root,{'mode':'recent','status':'running','run_id':run_id,'cycle':tick,'chats_read':reads,'identity_rejections':rejected,
                 'sending_enabled':auto_reply,'auto_reply_enabled':auto_reply,'polling_enabled':True,'continuous':continuous,'red_check_interval_seconds':3,
                 'polling_scope':'main_visible','cold_page_interval_seconds':None,'idle_interruptions':interruptions,'cold_page':0,'unread_rows':sum(r['unread'] for r in rows),
                 'visible_rows':len(visible_rows),'eligible_visible_rows':len(rows),'excluded_visible_rows':excluded_rows,
                 'quiet_scan_enabled':False,'visible_proactive_reads_enabled':True,'proactive_greetings_enabled':False,
                 'pending_media_deliveries':creation.pending_deliveries(),
                 'media_delivery_health':creation.delivery_health(),
                 'inbound_health':inbound_health(),
                 'inactive_chat_cleanup':dict(inactive_cleanup.result,enabled=False,reason='main_visible_scope'),
                 'auto_accept_group_invites':invite_policy(root)['auto_accept_group_invites'],
                 'friend_requests':friend_requests.health(),
                 'pending_replies':len(store.reply_rows(('queued','ready','send_started'))),'pending_inbound_work':inbound_count(),'manual_commands_enabled':True})
        return {'mode':'recent','status':'complete','chats_read':reads,'cycles':tick,'sending_enabled':auto_reply,'identity_rejections':rejected}
    except Exception as exc:
        progress(root,{'mode':'recent','status':'failed','run_id':run_id,'chats_read':reads,'error':str(exc),'sending_enabled':False,'auto_reply_enabled':False})
        raise
    finally:
        if client: client.close()
        creation.close()
        experiment.close()
        store.db.close()
        atomic(data/'reply-runtime.json',{'run_id':run_id,'auto_reply_enabled':False,'continuous':False,'stopped_at':time.time()})
