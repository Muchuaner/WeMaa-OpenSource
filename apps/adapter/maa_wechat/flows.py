"""Maa pipeline driven contact crawl and read-only recent-chat polling."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import time
from PIL import Image
from store import Store, digest


def box(item):
    value = item['box']
    return [value[k] for k in ('x','y','w','h')] if isinstance(value, dict) else value


def lines(items):
    rows = []
    for item in sorted(items, key=lambda i: (box(i)[1], box(i)[0])):
        x,y,w,h = box(item)
        center = y+h/2
        if rows and abs(center-rows[-1]['y']) <= 10:
            rows[-1]['text'] += ' '+item['text']
            rows[-1]['items'].append(item)
        else:
            rows.append({'y': center, 'text': item['text'], 'items': [item]})
    return rows


def named_avatar_rows(centers,items,kind):
    rows=[]
    for index,y in enumerate(centers):
        lower,upper=(y-18,y+18) if kind=='contacts' else (y-25,y+4)
        name_items=[i for i in items if lower<=box(i)[1]+box(i)[3]/2<=upper]
        preview_items=[i for i in items if y+4<box(i)[1]+box(i)[3]/2<=y+30]
        rows.append({'index':index,'y':y,'name':' '.join(i['text'] for i in sorted(name_items,key=lambda q:box(q)[0])).strip(),'name_clipped':kind=='recent' and any(box(i)[0]+box(i)[2]>=396 for i in name_items),'items':name_items,'preview':' '.join(i['text'] for i in sorted(preview_items,key=lambda q:box(q)[0]))})
    return rows


def title_matches(list_name,title,clipped=False):
    # OCR inserts spaces; WeChat adds a numeric member count to group titles.
    expected=re.sub(r'\s+','',list_name)
    observed=re.sub(r'\s+','',title)
    if not expected: return False
    from name_match import names_match,group_header
    if names_match(expected,observed) or group_header(expected,observed): return True
    prefix=re.sub(r'(?:\.{3}|…+)$','',expected)
    return (clipped or prefix!=expected) and len(prefix)>=8 and observed.startswith(prefix)


def conversation_title(list_name,title):
    expected=re.sub(r'\s+','',list_name)
    observed=re.sub(r'\s+','',title)
    # Only remove a proven UI count suffix, never an arbitrary personal name suffix.
    from name_match import group_header
    group=group_header(expected,observed)
    return group[0] if group else observed


def progress(root, value):
    from state_io import atomic_json
    return atomic_json(root/'data'/'progress.json',value,best_effort=True)


def save_crop(root, image, rect, label):
    x,y,w,h=rect
    crop=image[y:y+h,x:x+w,:3]
    sha=hashlib.sha256(crop.tobytes()).hexdigest()
    folder=root/'data'/'avatars'
    folder.mkdir(exist_ok=True)
    path=folder/(sha+'.png')
    if not path.exists(): Image.fromarray(crop[:,:,::-1]).save(path)
    return sha,str(path)


def scroll_top(runtime,label,*,max_batches=15,wheel_delta=1200):
    """Maa verifies the top thumb or two unchanged small wheel batches."""
    image,_=runtime.capture(label+'-top-00')
    prior=hashlib.sha256(image[80:1030,65:465].tobytes()).hexdigest()
    stable=0
    if runtime.recognize_image('ListAtTop',image): return
    for step in range(1,max_batches+1):
        runtime.node('ContactsScrollUp',{'ContactsScrollUp':{'dy':wheel_delta,'repeat':4,'repeat_delay':80}})
        image,_=runtime.capture(f'{label}-top-{step:02d}')
        current=hashlib.sha256(image[80:1030,65:465].tobytes()).hexdigest()
        stable=stable+1 if current==prior else 0
        if runtime.recognize_image('ListAtTop',image) or stable>=2: return
        prior=current
    raise RuntimeError('LIST_TOP_NOT_VERIFIED')


def ensure_category(runtime,category,want):
    params={'category':category,'want':want}
    result=runtime.node('ContactCategoryState',{'ContactCategoryState':{'custom_recognition_param':params}},allow_miss=True)
    if not result['succeeded']:
        opposite='expanded' if want=='collapsed' else 'collapsed'
        runtime.node('ToggleContactCategory',{'ToggleContactCategory':{'custom_recognition_param':{'category':category,'want':opposite}}})
        result=runtime.node('ContactCategoryState',{'ContactCategoryState':{'custom_recognition_param':params}})
    for node in result['detail']['nodes']:
        for hit in (node.get('recognition') or {}).get('all_results',[]):
            detail=hit.get('detail')
            if isinstance(detail,str): detail=json.loads(detail)
            if detail and 'state' in detail: return detail
    raise RuntimeError('CATEGORY_EVIDENCE_MISSING')


def reuse_visual(name,image,y,seeds,root):
    """Exact inner-photo equality with at most two pixels of centering jitter."""
    import numpy as np
    current=image[y-12:y+12,99:123,:3]
    matches=set()
    for (old_name,sha),observation in seeds.items():
        if old_name!=name: continue
        path=root/'data'/'avatars'/(sha+'.png')
        if not path.exists(): continue
        prior=np.array(Image.open(path).convert('RGB'))[:,:,::-1]
        if prior.shape!=(24,24,3): continue
        anchor=prior[2:22,2:22]
        if any(np.array_equal(anchor,current[2+dy:22+dy,2+dx:22+dx]) for dy in range(-2,3) for dx in range(-2,3)):
            matches.add(observation)
    return next(iter(matches)) if len(matches)==1 else None


def contact_rows(runtime,image,expected,category=None):
    from contact_alignment import normalized_photo
    items=runtime.recognize_image('ReadContactList',image)
    centers=[]
    for hit in runtime.recognize_image('ListRows',image,'contacts'):
        detail=hit.get('detail') or {}
        if isinstance(detail,str): detail=json.loads(detail)
        centers.extend(detail.get('centers',[]))
    selected=[]
    for hit in runtime.recognize_image('ContactSelectedRow',image):
        x,y,w,h=box(hit)
        if 45<=h<=60: selected.append(round(y+h/2))
    for cy in selected:
        near=[i for i,y in enumerate(centers) if abs(cy-y)<=12]
        if near: centers[min(near,key=lambda i:abs(cy-centers[i]))]=cy
        else: centers.append(cy)
    floors=[box(i)[1]+box(i)[3]+20 for i in items if i['text'].strip()==str(expected) and box(i)[0]>430]
    floor=max(floors,default=80)
    if category:
        from categories import label_key
        headers=runtime.recognize_image('ReadContactCategories',image)
        header_bottoms=[box(i)[1]+box(i)[3]+20 for i in headers if label_key(i['text'])==label_key(category)]
        if header_bottoms: floor=max(floor,max(header_bottoms))
    for candidate in lines(items):
        cy=round(candidate['y'])
        if cy<=floor or cy>1010 or all(box(i)[0]>400 for i in candidate['items']): continue
        near=[i for i,y in enumerate(centers) if abs(cy-y)<=10]
        patch=image[cy-15:cy+15,99:123,:3].astype('int16')
        background=image[cy,65,:3].astype('int16')
        mask=abs(patch-background).max(axis=2)>5
        if not near and (mask.sum(axis=1)>=2).sum()<20: continue
        if near:
            index=min(near,key=lambda i:abs(cy-centers[i]))
            centers[index]=cy
        else: centers.append(cy)
    rows=named_avatar_rows(sorted(set(y for y in centers if y>floor)),items,'contacts')
    for row in rows:
        row['photo']=normalized_photo(image[row['y']-12:row['y']+12,99:123,:3],image[row['y'],65,:3])
    return rows


def contact_crawl(runtime, root, run_id, max_pages=100,reuse_scan=None,scope='personal',resume_scan=None):
    store=Store(root/'data'/'wechat.sqlite3')
    scan=resume_scan or store.begin_scan()
    observed=set()
    positions={}
    previous_rows=[]
    stable_rows=0
    errors=[]
    bottom=False
    expected=None
    prior_page=None
    repeated=0
    seeds=store.contact_seeds(reuse_scan) if reuse_scan else {}
    try:
        category={'personal':'联系人','enterprise':'企业微信联系人','public':'公众号、服务号'}[scope]
        if resume_scan:
            import numpy as np
            saved=store.db.execute('SELECT status,expected,detail FROM scans WHERE id=?',(scan,)).fetchone()
            if not saved or saved[0]!='incomplete' or json.loads(saved[2]).get('scope')!=scope: raise RuntimeError('RESUME_SCAN_NOT_ELIGIBLE')
            entries=store.db.execute('SELECT ordinal,contact_id,frame_path FROM contact_positions WHERE scan_id=? ORDER BY ordinal',(scan,)).fetchall()
            if not entries or [r[0] for r in entries]!=list(range(len(entries))): raise RuntimeError('RESUME_POSITIONS_NOT_CONTIGUOUS')
            expected=saved[1]
            expanded=ensure_category(runtime,category,'expanded')
            displayed_count=expanded['count']
            if displayed_count is None: raise RuntimeError('RESUME_CATEGORY_COUNT_NOT_VERIFIED')
            manager_count=json.loads(saved[2]).get('manager_friend_count') if scope=='personal' else None
            expected=displayed_count
            positions={r[0]:r[1] for r in entries}
            observed=set(positions.values())
            prior_image=np.array(Image.open(entries[-1][2]).convert('RGB'))[:,:,::-1].copy()
            previous_rows=contact_rows(runtime,prior_image,expected,category)
            if not previous_rows or len(previous_rows)>len(entries): raise RuntimeError('RESUME_FRAME_NOT_VALID')
            for index,row in enumerate(previous_rows): row['ordinal']=len(entries)-len(previous_rows)+index
            store.event('contact_resume',{'scan':scan,'source_frame':entries[-1][2],'positions':len(entries),'run_id':run_id})
        else:
            runtime.node('OpenContacts')
        # Position focus in list, then Maa scroll to top. Never click profile controls.
            runtime.node('FocusList')
            scroll_top(runtime,run_id)
            collapsed={}
            for section in ('公众号、服务号','企业微信联系人','联系人'):
                collapsed[section]=ensure_category(runtime,section,'collapsed')
            expanded=ensure_category(runtime,category,'expanded')
            expected=expanded['count'] if expanded['count'] is not None else collapsed[category]['count']
            if expected is None: raise RuntimeError('CONTACT_CATEGORY_COUNT_MISSING')
            displayed_count=expected
            manager_count=None
            if scope=='personal':
                manager_count=runtime.friend_count(run_id)['count']
        for page in range(max_pages):
            image,page_path=runtime.capture(f'{run_id}-contacts-page-{page:03d}')
            page_hash=hashlib.sha256(image[120:1030,65:465].tobytes()).hexdigest()
            if page_hash==prior_page:
                repeated+=1
                if repeated>=2: bottom=True;break
            else: repeated=0
            prior_page=page_hash
            current_rows=contact_rows(runtime,image,expected,category)
            from contact_alignment import overlap,stationary,photo_match
            unchanged_rows=stationary(previous_rows,current_rows)
            stable_rows=stable_rows+1 if unchanged_rows else 0
            if stable_rows>=2: bottom=True;break
            common=len(current_rows) if unchanged_rows else overlap(previous_rows,current_rows)
            start=previous_rows[-common]['ordinal'] if common else 0
            for index,row in enumerate(current_rows):
                row['ordinal']=start+index
                row['needs_refresh']=index<common and not photo_match(previous_rows[-common+index]['photo'],row['photo'])
            panel_sha=hashlib.sha256(image[80:900,1030:1430,:3].tobytes()).hexdigest()
            selected_y=next((r['y'] for r in current_rows if int(image[r['y'],65,1])>120 and int(image[r['y'],65,2])<100 and int(image[r['y'],65,0])<170),None)
            for row in current_rows:
                y=row['y']
                name=row['name']
                avatar_sha,avatar_path=save_crop(root,image,[99,y-12,24,24],name)
                visual_key=(name,avatar_sha) if name else (page,y,avatar_sha)
                ordinal=row['ordinal']
                if ordinal in positions and not row['needs_refresh']:
                    store.contact_position(scan,ordinal,positions[ordinal],name,avatar_sha,page_path)
                    continue
                duplicates=sum(other['name']==name and other['photo'].tobytes()==row['photo'].tobytes() for other in current_rows)>1
                seed=None if duplicates or row['needs_refresh'] else (seeds.get(visual_key) or reuse_visual(name,image,y,seeds,root))
                if seed:
                    key=store.reuse_contact(scan,page,seed)
                    positions[ordinal]=key
                    store.contact_position(scan,ordinal,key,name,avatar_sha,page_path)
                    observed.add(key)
                    progress(root,{'mode':'contacts','scope':scope,'scan':scan,'page':page,'expected':expected,'observed':len(positions),'status':'running','reuse_scan':reuse_scan,'overlap_rows':common})
                    continue
                runtime.node('SelectContactRow',{'SelectContactRow':{'target':[180,y,1,1]}})
                profile,profile_path=runtime.capture(f'{run_id}-profile-{page:03d}-{y:04d}')
                b,g,r=[int(v) for v in profile[y,65,:3]]
                if not (g>120 and r<100 and b<170):
                    raise RuntimeError(f'CONTACT_SELECTION_NOT_VERIFIED:{page}:{y}')
                # Recognize the exact profile frame associated with this click.
                profile_ocr=runtime.recognize_image('ReadContactProfile',profile)
                if not profile_ocr: raise RuntimeError('CONTACT_PROFILE_EMPTY')
                profile_lines=lines(profile_ocr)
                text='\n'.join(r['text'] for r in profile_lines)
                from contact_alignment import name_key
                header=''.join(i['text'] for i in profile_ocr if 90<=box(i)[1]<=115)
                expected_name=name_key(name)
                header_match=len(expected_name)>=2 and expected_name[:min(8,len(expected_name))] in name_key(header)
                new_panel=hashlib.sha256(profile[80:900,1030:1430,:3].tobytes()).hexdigest()
                if new_panel==panel_sha and selected_y!=y and not header_match: raise RuntimeError('CONTACT_PROFILE_NOT_UPDATED')
                if scope=='public' and not header_match: raise RuntimeError('PUBLIC_PROFILE_HEADER_NOT_VERIFIED')
                panel_sha=new_panel
                selected_y=y
                wxid=None
                match=re.search(r'微信号\s*[:：]\s*([A-Za-z0-9_\-]+)',text)
                if match:
                    wxid=match.group(1)
                if not name:
                    header_names=[i['text'] for i in profile_ocr if 90<=box(i)[1]<=115 and box(i)[0]>1100]
                    name=' '.join(header_names) or '未识别昵称'
                facts={'scope':scope,'list_ordinal':ordinal,'profile_header_match':header_match,'visible_lines':profile_lines,'profile_ocr_unverified':True,'avatar_kind':'profile_60x60','list_avatar_sha':avatar_sha,'list_ocr_score_min':min((float(i.get('score',0)) for i in row['items']),default=0)}
                profile_avatar_sha,profile_avatar_path=save_crop(root,profile,[1042,95,60,60],name)
                key=store.contact(scan,page,name,wxid,profile_avatar_sha,profile_avatar_path,facts,profile_path,profile_ocr,[scope,scan,ordinal])
                positions[ordinal]=key
                store.contact_position(scan,ordinal,key,name,avatar_sha,page_path)
                observed.add(key)
                progress(root,{'mode':'contacts','scope':scope,'scan':scan,'page':page,'expected':expected,'observed':len(positions),'status':'running','overlap_rows':common})
            previous_rows=current_rows
            runtime.node('FocusList')
            runtime.node('ContactsScrollDown')
        status=store.finish_scan(scan,expected,len(positions),bottom,{'errors':errors,'bottom_verified':bottom,'max_pages':max_pages,'scope':scope,'displayed_category_count':displayed_count,'manager_friend_count':manager_count,'independent_contact_ids':len(observed),'coverage_grain':'aligned visible list positions'})
        result={'scope':scope,'scan':scan,'expected':expected,'observed':len(positions),'independent_contact_ids':len(observed),'status':status,'bottom_verified':bottom}
        progress(root,result)
        return result
    except Exception as exc:
        errors.append(str(exc))
        store.finish_scan(scan,expected,len(positions),False,{'errors':errors,'scope':scope})
        progress(root,{'mode':'contacts','scope':scope,'status':'failed','observed':len(positions),'error':str(exc)})
        raise
    finally: store.db.close()


def recent_rows(items):
    groups={}
    for item in items:
        x,y,w,h=box(item)
        # 65px rows beginning at y81. Names are first text line in each row.
        index=int((y+h/2-81)//65)
        if index<0 or index>13: continue
        center=81+index*65+32
        if y+h/2>center+4: continue
        groups.setdefault(index,[]).append(item)
    return [{'index':i,'y':81+i*65+32,'name':' '.join(v['text'] for v in sorted(items,key=lambda q:box(q)[0]))} for i,items in sorted(groups.items())]


def watch_recent(runtime,root,run_id,cycles=1,max_pages=30):
    store=Store(root/'data'/'wechat.sqlite3')
    runtime.node('OpenRecent')
    count=0
    cache={}
    completed_cycles=0
    identity_rejections=0
    try:
        progress(root,{'mode':'recent','status':'starting','chats_read':0})
        for cycle in range(cycles):
            scroll_top(runtime,f'{run_id}-{cycle:04d}')
            last_page=None
            repeats=0
            seen=set()
            bottom=False
            for page in range(max_pages):
                image,path=runtime.capture(f'{run_id}-recent-{cycle:04d}-{page:03d}')
                page_hash=hashlib.sha256(image[80:1030,65:465].tobytes()).hexdigest()
                repeats=repeats+1 if page_hash==last_page else 0
                last_page=page_hash
                if repeats>=2: bottom=True;break
                item_ocr=runtime.recognize_image('ReadRecentList',image)
                row_hits=runtime.recognize_image('ListRows',image,'recent')
                centers=[]
                for hit in row_hits:
                    detail=hit.get('detail') or {}
                    if isinstance(detail,str): detail=json.loads(detail)
                    centers.extend(detail.get('centers',[]))
                rows=named_avatar_rows(centers,item_ocr,'recent')
                badges=[]
                for hit in runtime.recognize_image('UnreadBadges',image):
                    _,y,w,h=box(hit)
                    if 5<=w<=20 and 5<=h<=20: badges.append(y+h/2)
                for row in rows:
                    row['unread']=any(abs(y-(row['y']-18))<=8 for y in badges)
                    row['avatar_sha'],_=save_crop(root,image,[78,row['y']-13,24,26],row['name'])
                rows.sort(key=lambda r:(not r['unread'],r['index']))
                for row in rows:
                    if not row['name']:
                        store.event('unreadable_recent_name',{'frame':path,'y':row['y'],'unread':row['unread']})
                        continue
                    # Visual identities are provisional; never used for sending.
                    cid=digest([row['name'],row['avatar_sha']])
                    if cid in seen and not row['unread']: continue
                    fingerprint=digest([row['name'],row['preview'],row['unread']])
                    prior=cache.get(cid)
                    if prior and prior['fingerprint']==fingerprint and not row['unread'] and time.monotonic()-prior['read_at']<60: continue
                    selected=runtime.node('SelectRecentRow',{'SelectRecentRow':{'target':[200,row['y'],1,1],'custom_recognition_param':{'y':row['y'],'name':row['name'],'avatar_sha':row['avatar_sha']}}},allow_miss=True)
                    if not selected['succeeded']:
                        identity_rejections+=1
                        store.event('recent_row_changed',{'frame':path,'y':row['y']})
                        continue
                    chat_image,chat_path=runtime.capture(f'{run_id}-chat-{cycle:04d}-{page:03d}-{row["index"]:02d}')
                    badges=runtime.recognize_image('ChatTitleBlueBadge',chat_image)
                    badge_x=min((box(hit)[0] for hit in badges if box(hit)[0]>510 and 5<=box(hit)[2]<=25 and 5<=box(hit)[3]<=25),default=None)
                    title_roi=[490,35,badge_x-493,40] if badge_x else None
                    title_items=runtime.recognize_image('ReadChatTitle',chat_image,roi=title_roi)
                    title=' '.join(i['text'] for i in sorted(title_items,key=lambda q:box(q)[0])).strip()
                    if not title_matches(row['name'],title,row['name_clipped']):
                        identity_rejections+=1
                        store.event('title_mismatch',{'expected':row['name'],'observed':title,'frame':path})
                        continue
                    from text_copy import read_messages
                    payloads,ocr=read_messages(runtime,chat_image,group_chat=bool(re.search(r'[（(]\d+[）)]$',title)))
                    # The full verified header, not a clipped list prefix, owns history.
                    conversation=store.group_identity(row['name'],title,row['avatar_sha'],payloads)
                    store.frame('chat',conversation,chat_path,hashlib.sha256(chat_image.tobytes()).hexdigest(),ocr)
                    result=store.snapshot(conversation,title,row['avatar_sha'],chat_path,payloads)
                    store.event('chat_observed',{'conversation':conversation,'snapshot_id':result['snapshot_id'],'run_id':run_id,'unread':row['unread'],'diff_status':result['status'],'new_count':len(result['new']),'list_name_clipped':row['name_clipped'],'context_recovery':result.get('metadata')})
                    seen.add(cid)
                    cache[cid]={'fingerprint':fingerprint,'read_at':time.monotonic()}
                    count+=1
                progress(root,{'mode':'recent','cycle':cycle+1,'page':page,'chats_read':count,'visible_conversations_seen':len(seen),'unread_rows':sum(r['unread'] for r in rows),'status':'running'})
                # Maa wheel has its own target; do not click an unverified chat
                # merely to focus scrolling (that would silently clear a badge).
                runtime.node('ContactsScrollDown')
            store.event('recent_cycle_complete',{'cycle':cycle+1,'bottom_verified':bottom,'conversations_seen':len(seen),'max_pages':max_pages})
            if bottom: completed_cycles+=1
            if cycle+1<cycles: time.sleep(1)
        result={'mode':'recent','status':'complete' if completed_cycles==cycles and identity_rejections==0 else 'incomplete','cycles':cycles,'completed_cycles':completed_cycles,'identity_rejections':identity_rejections,'chats_read':count,'sending_enabled':False,'last_cycle_bottom_verified':bottom}
        progress(root,result)
        return result
    except Exception as exc:
        progress(root,{'mode':'recent','status':'failed','chats_read':count,'error':str(exc),'sending_enabled':False})
        raise
    finally: store.db.close()
