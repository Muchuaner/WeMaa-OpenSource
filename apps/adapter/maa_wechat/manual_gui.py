"""Manual admin intents are executed only by the existing GUI owner.

Persistent claim before navigation/input, terminal uncertainty never replayed.
The server supplies selectors, not coordinates or unverified identity proofs.
"""
import hashlib
import json
import math
import re
import time
import unicodedata

from flows import box,contact_rows,ensure_category,lines,scroll_top,conversation_title,reuse_visual
from store import digest

GUI_ACTIONS = {'send_message','send_contact_message','open_conversation','open_contact','review_message','process_creation','audit_recent','audit_history','resolve_context','recover_media','discard_media_preview','accept_private_baseline','activate_private_replies','activate_reply_scope'}


def validate_intent(command):
    action=command.get('action');params=command.get('params',{})
    if action not in GUI_ACTIONS or not re.fullmatch('[a-f0-9]{32}',command.get('id','')):
        raise ValueError('MANUAL_COMMAND_INVALID')
    if action=='activate_reply_scope':
        from reply_policy import SCOPE
        expiry=command.get('expires_at',0)
        if (set(params)!={'scope','reason'} or params['scope']!=SCOPE
                or not isinstance(params['reason'],str) or not 1<=len(params['reason'].strip())<=400
                or type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=time.time()):
            raise ValueError('REPLY_SCOPE_PARAMETERS_INVALID')
        return
    allowed={'contact_id','wechat_id','conversation_id','snapshot_id','review_hash','reason','receive_only'} if action=='accept_private_baseline' else {'contact_id','wechat_id','conversation_id','snapshot_id','review_hash','reason','receive_only','auto_reply_expires_at'} if action=='activate_private_replies' else {'contact_id','wechat_id','text'} if action=='send_contact_message' else {'conversation_id','contact_id'} if action=='audit_history' else {'conversation_id','expected_context','decisions'} if action=='resolve_context' else {'conversation_id','delivery_id'} if action in ('recover_media','discard_media_preview') else set() if action=='audit_recent' else {'conversation_id','text_sha256'} if action in ('review_message','process_creation') else {'contact_id'} if action=='open_contact' else {'conversation_id','text'} if action=='send_message' else {'conversation_id'}
    if set(params)-allowed: raise ValueError('MANUAL_PARAMETERS_INVALID')
    if action=='audit_history' and bool(params.get('contact_id'))==bool(params.get('conversation_id')):raise ValueError('HISTORY_TARGET_INVALID')
    if action not in ('open_contact','send_contact_message','audit_recent') and not (action=='audit_history' and params.get('contact_id')) and not re.fullmatch('[a-f0-9]{64}',str(params.get('conversation_id',''))): raise ValueError('CONVERSATION_ID_INVALID')
    if (action in ('open_contact','send_contact_message','accept_private_baseline','activate_private_replies') or (action=='audit_history' and params.get('contact_id'))) and (not isinstance(params.get('contact_id'),str) or not 1<=len(params['contact_id'])<=256): raise ValueError('CONTACT_ID_INVALID')
    if action in ('review_message','process_creation') and not re.fullmatch('[a-f0-9]{64}',str(params.get('text_sha256',''))):raise ValueError('MESSAGE_TEXT_HASH_INVALID')
    if action in ('send_message','send_contact_message'):
        text=params.get('text')
        if not isinstance(text,str) or not text.strip() or len(text)>400 or any(ord(c)<32 and c not in '\n\t' for c in text): raise ValueError('MANUAL_TEXT_INVALID')
    if action=='send_contact_message':
        if any(unicodedata.category(c) in {'Cc','Zl','Zp'} for c in text): raise ValueError('MANUAL_TEXT_INVALID')
        wxid=params.get('wechat_id')
        if not isinstance(wxid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',wxid): raise ValueError('CONTACT_WECHAT_ID_INVALID')
    if action=='resolve_context' and (not re.fullmatch('[a-f0-9]{64}',str(params.get('expected_context',''))) or not isinstance(params.get('decisions'),dict)):raise ValueError('REVIEW_PARAMETERS_INVALID')
    if action in ('accept_private_baseline','activate_private_replies'):
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',str(params.get('wechat_id','')))
                or type(params.get('snapshot_id')) is not int or params['snapshot_id']<=0
                or not re.fullmatch('[a-f0-9]{64}',str(params.get('review_hash','')))
                or not isinstance(params.get('reason'),str) or not params['reason'].strip() or len(params['reason'])>400
                or params.get('receive_only') is not (action=='accept_private_baseline')):raise ValueError('BASELINE_CONFIRMATION_INVALID')
    if action=='activate_private_replies':
        expiry=params.get('auto_reply_expires_at')
        if type(expiry) not in (int,float) or not math.isfinite(expiry) or not time.time()<expiry<=time.time()+3600:raise ValueError('REPLY_ACTIVATION_EXPIRY_INVALID')
    if action in ('recover_media','discard_media_preview') and not re.fullmatch('[a-f0-9]{32}',str(params.get('delivery_id',''))):raise ValueError('RECOVERY_PARAMETERS_INVALID')
    expires=command.get('expires_at',0)
    if not isinstance(expires,(int,float)) or isinstance(expires,bool) or not math.isfinite(expires) or expires<=time.time(): raise ValueError('MANUAL_COMMAND_EXPIRED')


def observe(runtime,root,store,row,label,page,*,previous_payloads=None):
    from watch import read_title,consensus,is_group_title
    from message_vision import extract_message_payloads
    image,path=runtime.capture(label)
    title=read_title(runtime,image,row)
    from text_copy import read_messages
    options={'previous_payloads':previous_payloads} if previous_payloads is not None else {}
    payloads,hits=read_messages(runtime,image,group_chat=is_group_title(row,title),**options)
    cid=store.group_identity(row['name'],title,row['avatar_sha'],payloads)
    from sender_directory import SenderDirectory
    frame_sha=hashlib.sha256(image[82:887,490:1885,:3].copy().tobytes()).hexdigest()
    payloads=SenderDirectory(store).enrich(cid,payloads,row['name'],frame_sha)
    # UI observations must never advance the automatic reply deduplication cursor.
    store.manual_view(cid,title,row['avatar_sha'],path,payloads)
    store.observe_group(cid,row['name'],title)
    target={'name':row['name'],'name_clipped':row.get('name_clipped',False),'conversation_title':title,'avatar_sha':row['avatar_sha'],'row_y':row['y'],'list_page':page}
    store.route(cid,target)
    return cid,target


def find_recent(runtime,root,target,label,expires):
    from watch import read_rows,select,list_fingerprint
    runtime.node('OpenRecent');scroll_top(runtime,label)
    previous=None
    for page in range(100):
        if time.time()>expires: raise ValueError('MANUAL_COMMAND_EXPIRED')
        rows,image,path=read_rows(runtime,root,label+'-recent-'+str(page))
        from group_policy import is_group
        from name_match import names_match
        hits=[r for r in rows if names_match(r['name'],target['name']) and (is_group(target) or r['avatar_sha']==target['avatar_sha'])]
        from name_match import normalized
        exact=[r for r in hits if normalized(r['name'])==normalized(target['name'])]
        if exact:hits=exact
        if len(hits)>1: raise ValueError('TARGET_AMBIGUOUS')
        if hits and hits[0]['y']>970:
            runtime.node('ContactsScrollDown',{'ContactsScrollDown':{'dy':-120,'repeat':1}})
            continue
        if hits:
            if select(runtime,hits[0]):return hits[0],page
            if getattr(runtime,'selection_failure',None):raise ValueError('TARGET_CHANGED')
            # A wheel animation can move the list between the read and the
            # guarded click. Re-read once without scrolling; all name/avatar
            # checks and the post-click title check still apply. This retries
            # navigation only, before any draft or send operation.
            fresh,_,_=read_rows(runtime,root,label+'-recent-stable-'+str(page))
            again=[r for r in fresh if names_match(r['name'],target['name']) and (is_group(target) or r['avatar_sha']==target['avatar_sha'])]
            exact=[r for r in again if normalized(r['name'])==normalized(target['name'])]
            if exact:again=exact
            if len(again)>1:raise ValueError('TARGET_AMBIGUOUS')
            if len(again)!=1 or again[0]['y']>970 or not select(runtime,again[0]):raise ValueError('TARGET_CHANGED')
            return again[0],page
        fingerprint=list_fingerprint(image)
        if fingerprint==previous: break
        previous=fingerprint
        runtime.node('ContactsScrollDown')
    raise ValueError('TARGET_NOT_FOUND')


def contact_hint(runtime,root,store,scan,wanted,expected,label,interrupt=None):
    """Calibrate a scroll using actual scan positions; the result is only a hint.

    Every subsequent open still verifies photo, name, profile ID and chat. This
    avoids restarting a hundreds-contact sequential crawl for each daily slot.
    """
    if wanted<15:return
    positions={}
    for ordinal,name,sha in store.db.execute('SELECT ordinal,list_name,avatar_sha FROM contact_positions WHERE scan_id=?',(scan,)):
        positions.setdefault((name,sha),[]).append(ordinal)
    def location(image):
        found=[]
        for row in contact_rows(runtime,image,expected,'联系人'):
            y=row['y'];sha=hashlib.sha256(image[y-12:y+12,99:123,:3].tobytes()).hexdigest()
            values=positions.get((row['name'],sha),[])
            if len(values)==1:found.append(values[0])
        return min(found) if found else None
    image,_=runtime.capture(label+'-hint-top');start=location(image)
    if start is None:return
    if interrupt and interrupt():raise ValueError('CONTACT_NAVIGATION_PREEMPTED')
    if interrupt:runtime.node('OpenContacts')
    runtime.node('ContactsScrollDown',{'ContactsScrollDown':{'dy':-120,'repeat':5}})
    image,_=runtime.capture(label+'-hint-calibrated');current=location(image)
    if current is None or current<=start:
        scroll_top(runtime,label+'-hint-reset');return
    step=(current-start)/5
    # Qt caps deltas and accelerates repeated wheel events. Re-observe actual
    # scan positions after EVERY batch; correct overshoot by verified scrolling
    # back instead of restarting a blind hundreds-contact crawl.
    for attempt in range(16):
        if 0<=wanted-current<=12:break
        if interrupt and interrupt():raise ValueError('CONTACT_NAVIGATION_PREEMPTED')
        if interrupt:runtime.node('OpenContacts')
        backwards=current>wanted
        distance=current-wanted+8 if backwards else wanted-current-8
        amount=min(25,max(1,math.ceil(distance/max(1,step))))
        node='ContactsScrollUp' if backwards else 'ContactsScrollDown'
        runtime.node(node,{node:{'dy':120 if backwards else -120,'repeat':amount,'repeat_delay':30}})
        image,_=runtime.capture(label+'-hint-position-'+str(attempt));located=location(image)
        if located is None:scroll_top(runtime,label+'-hint-reset');return
        if located==current:break
        step=max(step,abs(located-current)/amount);current=located
    located=current
    if located is None or located>wanted:scroll_top(runtime,label+'-hint-reset')
    store.event('experiment_contact_route_hint',{'wanted_ordinal':wanted,'observed_ordinal':located,'calibrated_rows_per_scroll':step})


def open_contact(runtime,root,store,contact_id,label,expires,proof=None,interrupt=None,allow_hint=True):
    """Contact list photo + profile wxid before opening the actual chat."""
    contact=store.db.execute('SELECT wechat_id,display_name,facts_json FROM contacts WHERE id=?',(contact_id,)).fetchone()
    if not contact: raise ValueError('CONTACT_NOT_FOUND')
    wxid,name,facts_raw=contact;facts=json.loads(facts_raw)
    if not wxid or not facts.get('list_avatar_sha'): raise ValueError('CONTACT_IDENTITY_INSUFFICIENT')
    positions=store.db.execute("SELECT p.scan_id,p.list_name,p.avatar_sha,p.ordinal FROM contact_positions p JOIN scans s ON s.id=p.scan_id WHERE p.contact_id=? AND s.status='complete' ORDER BY s.started DESC",(contact_id,)).fetchall()
    if not positions: raise ValueError('CONTACT_POSITION_NOT_VERIFIED')
    latest=[p for p in positions if p[0]==positions[0][0]]
    if len(latest)!=1: raise ValueError('CONTACT_POSITION_AMBIGUOUS')
    scan,name,list_avatar_sha,ordinal=latest[0]
    name_hints={name,contact[1]}
    visible=facts.get('visible_lines',[])
    if visible and isinstance(visible[0],dict):name_hints.add(visible[0].get('text',''))
    category={'personal':'联系人','enterprise':'企业微信联系人','public':'公众号、服务号'}.get(facts.get('scope'))
    if not category: raise ValueError('CONTACT_SCOPE_INVALID')
    runtime.node('OpenContacts');runtime.node('FocusList');scroll_top(runtime,label,max_batches=160,wheel_delta=120)
    for section in ('公众号、服务号','企业微信联系人','联系人'): ensure_category(runtime,section,'collapsed')
    expected=ensure_category(runtime,category,'expanded')['count']
    if allow_hint and facts.get('scope')=='personal':contact_hint(runtime,root,store,scan,ordinal,expected,label,interrupt)
    previous=None
    for page in range(100):
        if time.time()>expires: raise ValueError('MANUAL_COMMAND_EXPIRED')
        image,path=runtime.capture(label+'-contacts-'+str(page))
        candidates=[]
        for row in contact_rows(runtime,image,expected,category):
            y=row['y'];sha=hashlib.sha256(image[y-12:y+12,99:123,:3].tobytes()).hexdigest()
            from name_match import names_match
            if sha==list_avatar_sha or any(names_match(row['name'],hint) for hint in name_hints if hint): candidates.append(row)
        if len(candidates)>1: raise ValueError('CONTACT_AMBIGUOUS')
        if candidates:
            row=candidates[0];runtime.node('SelectContactRow',{'SelectContactRow':{'target':[180,row['y'],1,1]}})
            profile,path=runtime.capture(label+'-profile')
            b,g,r=[int(v) for v in profile[row['y'],65,:3]]
            if not (g>120 and r<100 and b<170): raise ValueError('CONTACT_SELECTION_NOT_VERIFIED')
            text='\n'.join(i['text'] for i in lines(runtime.recognize_image('ReadContactProfile',profile)))
            matches=re.findall(r'微信号\s*[:：]\s*([A-Za-z0-9_\-]+)',text)
            if matches!=[wxid]: raise ValueError('CONTACT_PROFILE_ID_MISMATCH')
            from send import _ocr_scaled
            second='\n'.join(i['text'] for i in lines(_ocr_scaled(runtime,profile,'ReadContactProfile',[1030,80,400,820],2)))
            if re.findall(r'微信号\s*[:：]\s*([A-Za-z0-9_\-]+)',second)!=[wxid]: raise ValueError('CONTACT_PROFILE_ID_NOT_STABLE')
            verified_proof=dict(wechat_id=wxid,stable=True,profile_sha256=hashlib.sha256(profile.tobytes()).hexdigest(),observation_sha256=hashlib.sha256(image.tobytes()).hexdigest())
            if proof is not None:proof.update(verified_proof)
            runtime.node('ContactOpenChat')
            from watch import read_rows
            rows,image,path=read_rows(runtime,root,label+'-opened')
            # Find the unique selected row. A title alone is never a target.
            selected=runtime.recognize_image('ContactSelectedRow',image)
            active=[r for r in rows if any(box(h)[1]<=r['y']<box(h)[1]+box(h)[3] for h in selected)]
            if len(active)!=1: raise ValueError('OPENED_CHAT_SELECTION_AMBIGUOUS')
            from flows import title_matches
            if not title_matches(row['name'],active[0]['name']): raise ValueError('OPENED_CHAT_NAME_MISMATCH')
            cid,target=observe(runtime,root,store,active[0],label+'-chat',0)
            from contact_identity import link_verified_chat
            cid=link_verified_chat(store,contact_id,cid,target,verified_proof)
            return cid,target
        fingerprint=hashlib.sha256(image[80:1030,65:475,:3].tobytes()).hexdigest()
        if fingerprint==previous: break
        previous=fingerprint
        if interrupt is not None:
            if interrupt(): raise ValueError('CONTACT_NAVIGATION_PREEMPTED')
            # Restore this read-only contact page after the recent badge check.
            runtime.node('OpenContacts')
        runtime.node('ContactsScrollDown')
    if allow_hint:
        return open_contact(runtime,root,store,contact_id,label+'-full',expires,proof,interrupt,allow_hint=False)
    raise ValueError('CONTACT_NOT_FOUND_ON_DESKTOP')


def recover_interrupted_gui(store):
    store.db.execute("UPDATE gui_commands SET status='failed',detail_json=? WHERE status='executing'",(json.dumps({'status':'uncertain','reason':'WORKER_INTERRUPTED_NO_RETRY'}),))
    store.db.commit()


def process_one(runtime,root,store,run_id):
    actions=sorted(GUI_ACTIONS)
    row=store.db.execute("SELECT id,action,params_json,expires FROM gui_commands WHERE status='pending' AND action IN ("+
                         ','.join('?' for _ in actions)+") ORDER BY updated LIMIT 1",actions).fetchone()
    if not row: return False
    cid,action,params_raw,expires=row;params=json.loads(params_raw)
    store.gui_result(cid,'executing',{'status':'executing'})  # durable before any GUI effect
    detail={'status':'failed','reason':'MANUAL_EXECUTION_FAILED'}
    effects_started=False
    try:
        validate_intent({'id':cid,'action':action,'params':params,'expires_at':expires})
        if action=='activate_reply_scope':
            from reply_policy import activate_all
            detail=dict(status='reviewed',**activate_all(store,params,cid))
            store.gui_result(cid,'succeeded',detail);return True
        if runtime is not None:runtime.selection_failure=None
        label=run_id+'-manual-'+cid
        if action=='audit_recent':
            from watch import read_rows
            runtime.node('OpenRecent');scroll_top(runtime,label+'-top')
            rows,image,path=read_rows(runtime,root,label+'-recent')
            routes=[(c,json.loads(v)) for c,v in store.db.execute('SELECT conversation_id,target_json FROM conversation_routes')]
            observed=[]
            from name_match import names_match
            from group_policy import is_group
            for r in rows:
                matched=sorted({store.canonical(c) for c,v in routes if names_match(v.get('name'),r['name']) and (is_group(v) or v.get('avatar_sha')==r['avatar_sha'])})
                observed.append({'name':r['name'],'unread':bool(r.get('unread')),'conversation_id':matched[0] if len(matched)==1 else None})
            detail={'status':'observed','rows':observed,'frame_path':path,'observed_at':time.time()}
            store.gui_result(cid,'succeeded',detail);store.event('manual_recent_audit',dict(detail,command_id=cid));return True
        profile_proof={}
        if action in ('open_contact','send_contact_message','accept_private_baseline','activate_private_replies') or (action=='audit_history' and params.get('contact_id')):
            if action in ('send_contact_message','accept_private_baseline','activate_private_replies'):
                contact=store.db.execute('SELECT wechat_id,facts_json FROM contacts WHERE id=?',(params['contact_id'],)).fetchone()
                if not contact or contact[0]!=params['wechat_id'] or json.loads(contact[1]).get('scope')!='personal':
                    raise ValueError('PRIVATE_CONTACT_IDENTITY_MISMATCH')
                conversation,target=open_contact(runtime,root,store,params['contact_id'],label,expires,proof=profile_proof)
                from group_policy import is_group
                if is_group(target) or profile_proof.get('stable') is not True or profile_proof.get('wechat_id')!=params['wechat_id']:
                    raise ValueError('PRIVATE_PROFILE_BINDING_NOT_VERIFIED')
            else:
                conversation,target=open_contact(runtime,root,store,params['contact_id'],label,expires)
            selected=dict(name=target['name'],avatar_sha=target['avatar_sha'],y=target['row_y']);page=target['list_page']
        else:
            conversation=store.canonical(params['conversation_id'])
            saved=store.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(conversation,)).fetchone()
            if not saved: raise ValueError('CONVERSATION_ROUTE_NOT_OBSERVED')
            expected=json.loads(saved[0]);selected,page=find_recent(runtime,root,expected,label,expires)
            cached=store.db.execute('SELECT payloads_json FROM manual_views WHERE conversation_id=?',(conversation,)).fetchone()
            cache_options={'previous_payloads':json.loads(cached[0])} if cached else {}
            actual,target=observe(runtime,root,store,selected,label+'-chat',page,**cache_options)
            if actual!=conversation: raise ValueError('CONVERSATION_ID_CHANGED')
        if action=='audit_history':
            from send import _draft
            pages=[];last=None
            for number in range(8):
                if time.time()>expires:raise ValueError('MANUAL_COMMAND_EXPIRED')
                v=store.db.execute('SELECT frame_path,payloads_json,seen FROM manual_views WHERE conversation_id=?',(conversation,)).fetchone()
                from context_recovery import review_hash
                h=review_hash(json.loads(v[1]));pages.append(dict(frame_path=v[0],payloads=json.loads(v[1]),seen=v[2]))
                if h==last:break
                last=h
                image,_=runtime.capture(label+'-history-guard')
                if not _draft(runtime,image)['verified']:raise ValueError('HISTORY_DRAFT_NOT_EMPTY')
                runtime.node('ContactsScrollUp',{'ContactsScrollUp':{'target':[1100,450,1,1],'dy':1200,'repeat':1}})
                actual,target=observe(runtime,root,store,selected,label+'-history-'+str(number),page)
                if actual!=conversation:raise ValueError('CONVERSATION_ID_CHANGED')
            runtime.node('ContactsScrollDown',{'ContactsScrollDown':{'target':[1100,450,1,1],'dy':-1200,'repeat':10}})
            actual,target=observe(runtime,root,store,selected,label+'-history-bottom',page)
            if actual!=conversation:raise ValueError('CONVERSATION_ID_CHANGED')
            detail={'status':'reviewed','conversation_id':conversation,'pages':pages,'bounded':True}
        elif action in ('accept_private_baseline','activate_private_replies'):
            if time.time()>expires:raise ValueError('MANUAL_COMMAND_EXPIRED')
            if conversation!=params['conversation_id']:raise ValueError('BASELINE_CONVERSATION_CHANGED')
            from private_baseline import accept_baseline
            sid=accept_baseline(store,params,cid,profile_proof)
            detail={'status':'reviewed','conversation_id':conversation,'snapshot_id':sid,
                    'approved_hash':params['review_hash'],'receive_only':params['receive_only'],'historical_notifications':False}
        elif action=='resolve_context':
            from context_recovery import resolve_visible
            sid=resolve_visible(store,conversation,params['expected_context'],params['decisions'],cid)
            detail={'status':'reviewed','conversation_id':conversation,'snapshot_id':sid}
        elif action=='discard_media_preview':
            from media_send import discard_failed_preview
            detail=discard_failed_preview(runtime,root,store,conversation,params['delivery_id'],target,label)
        elif action=='recover_media':
            from context_recovery import retry_source
            from group_policy import allowed
            from media_send import send_media_verified
            item=retry_source(store,conversation,params['delivery_id'],cid)
            if not allowed(root,target):raise ValueError('GROUP_NOT_ALLOWED')
            from pathlib import Path
            import shutil
            source=Path(item['path']).resolve()
            if source.parent!=(Path(root)/'data/media-cache'/params['delivery_id']).resolve() or hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('RECOVERY_ASSET_CHANGED')
            folder=Path(root)/'data/media-cache'/cid;folder.mkdir(parents=True,exist_ok=True)
            copy=folder/source.name;shutil.copy2(source,copy)
            item=dict(item,id=cid,conversation_id=conversation,path=str(copy))
            def persist(value):store.gui_result(cid,'executing',value)
            effects_started=True
            result=send_media_verified(runtime,root,label,target,item,persist=persist,refresh_row_position=True)
            detail=dict(result,conversation_id=conversation,recovery_of=params['delivery_id'])
            observe(runtime,root,store,selected,label+'-after',page)
        elif action in ('review_message','process_creation'):

            from PIL import Image
            import numpy as np
            from sender_identity import verify_member
            from sender_directory import SenderDirectory
            view=store.db.execute('SELECT frame_path,payloads_json FROM manual_views WHERE conversation_id=?',(conversation,)).fetchone()
            payloads=json.loads(view[1])
            matches=[p for p in payloads if p.get('direction')=='incoming' and p.get('message_kind')=='text' and p.get('text_copy',{}).get('verified') and hashlib.sha256(p.get('text','').encode()).hexdigest()==params['text_sha256']]
            if len(matches)!=1:raise ValueError('MESSAGE_NOT_UNIQUELY_VISIBLE')
            payload=matches[0]
            image=np.array(Image.open(view[0]).convert('RGB'))[:,:,::-1].copy()
            proof=verify_member(runtime,image,label+'-sender',target,payload)
            SenderDirectory(store).remember(conversation,payload,proof,target['name'])
            detail={'status':'reviewed','conversation_id':conversation,'text':payload['text'],
                    'identity':{k:proof[k] for k in ('wechat_id','display_name','profile_sha256','message_frame_sha256','message_identity','stable')},'observed_at':time.time()}
            if action=='process_creation':
                from creation_client import candidate
                from watch import is_group_title
                if not candidate(payload['text']):raise ValueError('NOT_EXPLICIT_CREATION_REQUEST')
                body={'gateway_id':'win-wechat-maa','event_id':'manual-'+cid,'conversation_id':conversation,
                    'conversation_title':target['conversation_title'],'conversation_type':'group' if is_group_title(target,target['conversation_title']) else 'private',
                    'sender_name':proof['display_name'],'sender_verified':True,'direction':'incoming','stable_ocr':True,
                    'observed_at':time.time(),'text':payload['text'],'context':[], 'identity':detail['identity']}
                store.db.execute('INSERT OR IGNORE INTO creation_events VALUES(?,?,?,?)',(body['event_id'],json.dumps(body,ensure_ascii=False),'pending',time.time()));store.db.commit()
                detail.update(status='creation_queued',event_id=body['event_id'])
        elif action in ('send_message','send_contact_message'):
            if time.time()>expires: raise ValueError('MANUAL_COMMAND_EXPIRED')
            from send import send_verified
            effects_started=True
            def check_expiry():
                if time.time()>expires: raise ValueError('MANUAL_COMMAND_EXPIRED')
            options={'preflight':check_expiry} if action=='send_contact_message' else {}
            cached=store.db.execute('SELECT payloads_json FROM manual_views WHERE conversation_id=?',(conversation,)).fetchone()
            if cached:
                options['previous_payloads']=json.loads(cached[0])
            result=send_verified(runtime,root,label,dict(target,conversation_id=conversation,reply_text=params['text'],event_id=cid),refresh_row_position=True,**options)
            status=result.get('status')
            evidence=result.get('evidence') or {}
            if status=='sent' and not (evidence.get('title_verified') and evidence.get('outgoing_verified')): status='uncertain'
            detail=dict(result,status=status,conversation_id=conversation)
            if action=='send_contact_message':
                detail['evidence']=dict(evidence,profile_verified=True)
                detail['profile_proof']=profile_proof
            # 已核验的发送回执先落盘；只读上下文刷新不能延迟回执，更不能
            # 因刷新失败把真实送达改成 uncertain。后续入站仍由自动收件核验。
            after=(result.get('diagnostic') or {}).get('after_payloads') or []
            cache_options={'previous_payloads':after[-1]['payloads']} if after else {}
            if status=='sent':
                store.gui_result(cid,'succeeded',detail)
                try: observe(runtime,root,store,selected,label+'-after',page,**cache_options)
                except Exception: detail['context_refresh_failed']=True
            elif action!='send_contact_message':
                observe(runtime,root,store,selected,label+'-after',page,**cache_options)
        else: detail={'status':'opened','conversation_id':conversation}
    except Exception as exc:
        detail={'status':'uncertain' if action in ('send_message','send_contact_message','recover_media') and effects_started else 'failed','reason':str(exc)[:160]}
        if action in ('send_message','send_contact_message','recover_media') and not effects_started:detail['diagnostic']={'send_click_attempted':False,'draft_prepared':False,'phase':'navigation_or_validation'}
        failure=getattr(runtime,'selection_failure',None)
        if isinstance(failure,dict):
            failure=dict(failure)
            try:_,failure['frame']=runtime.capture(label+'-selection-failure')
            except Exception:pass
            detail['selection_failure']=failure
    store.gui_result(cid,'succeeded' if detail['status'] in ('sent','opened','reviewed','creation_queued') else 'failed',detail)
    store.event('manual_'+str(detail['status']),dict(detail,command_id=cid))
    return True
