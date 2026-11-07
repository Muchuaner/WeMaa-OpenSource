"""Reversible recent-list cleanup, using the sole Maa GUI owner."""
import json,time,re
from pathlib import Path
from datetime import timezone,timedelta
from activity_scheduler import parse_sidebar_time
from flows import box

TZ=timezone(timedelta(hours=8))

def norm(s):return re.sub(r'\s+','',str(s or ''))

def protected_rows(store):
    keys=set();avatars=set();names=set();db=store.db
    def protect(route):
        if not isinstance(route,dict):return
        if route.get('key'):keys.add(route['key'])
        if route.get('avatar_sha'):avatars.add(route['avatar_sha'])
        if route.get('name'):names.add(norm(route['name']))
    for key,route in db.execute("SELECT row_key,route_json FROM inbound_work WHERE status IN ('pending','captured','review')"):
        keys.add(key);protect(json.loads(route))
    # Protect every outstanding delivery route, including terminal uncertain sends.
    cids={r[0] for r in db.execute("SELECT conversation_id FROM reply_outbox WHERE status IN ('queued','ready','send_started','uncertain')")}
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'creation_deliveries' in tables:
        cols={r[1] for r in db.execute('PRAGMA table_info(creation_deliveries)')}
        if {'conversation_id','status'}<=cols:
            cids.update(r[0] for r in db.execute("SELECT conversation_id FROM creation_deliveries WHERE status NOT IN ('sent','failed','cancelled')"))
    for cid,route in db.execute('SELECT conversation_id,target_json FROM conversation_routes'):
        if cid in cids:protect(json.loads(route))
    return keys,avatars,names

def eligible(row,label_votes,*,now,protected,selected=False,threshold=7200):
    keys,avatars,names=protected
    if row.get('unread') or row.get('unread_count'):return False,'unread'
    if selected:return False,'selected'
    if row['key'] in keys or row.get('avatar_sha') in avatars or norm(row.get('name')) in names:return False,'pending_or_review'
    if not row.get('name') or row.get('name_clipped') or not row.get('avatar_sha'):return False,'identity'
    if len(label_votes)!=2 or norm(label_votes[0])!=norm(label_votes[1]):return False,'time_not_verified'
    observed=parse_sidebar_time(norm(label_votes[0]),now=now,timezone=TZ)
    if observed is None or now-observed<threshold+60:return False,'active_or_unknown'
    return True,'inactive'

def menu_target(passes):
    """Two exact independent OCR votes; never use a positional delete fallback."""
    if len(passes)!=2:return None
    targets=[]
    for hits in passes:
        texts={norm(h.get('text')) for h in hits if h.get('score',0)>=.85}
        if '取消置顶' in texts:return None
        if not {'置顶','标为未读','独立窗口显示','不显示','删除'}<=texts:return None
        matches=[h for h in hits if norm(h.get('text'))=='不显示' and h.get('score',0)>=.9]
        if len(matches)!=1:return None
        x,y,w,h=box(matches[0])
        if not (24<=x<180 and 150<=y<270 and w>10 and 8<h<35):return None
        deletes=[box(q) for q in hits if norm(q.get('text'))=='删除']
        if len(deletes)!=1 or deletes[0][1]<y+h+15:return None
        targets.append([x,y,w,h])
    if any(abs(a-b)>4 for a,b in zip(*targets)):return None
    return targets[0]

class InactiveChats:
    def __init__(self,root,store):
        self.root=Path(root);self.store=store;self.last=0;self.result={'hidden':0,'threshold_seconds':7200}
        self.skipped=set()
        self.ledger=self.root/'data/inactive-hide-ledger.jsonl'
    def enabled(self):
        try:config=json.loads((self.root/'data/inactive-hide-policy.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):return False
        return config.get('enabled') is True and config.get('threshold_seconds')==7200
    def busy(self):
        db=self.store.db
        return any(db.execute('SELECT 1 FROM '+t+' WHERE '+w).fetchone() for t,w in [
            ('gui_commands',"status IN ('pending','executing')"),('reply_outbox',"status IN ('queued','ready','send_started')"),('creation_events',"status='pending'")])
    def record(self,event):
        with self.ledger.open('a',encoding='utf-8') as f:f.write(json.dumps(dict(time=time.time(),**event),ensure_ascii=False)+'\n')
        self.store.event('inactive_chat_hide',event)
    def run_one(self,runtime,label,*,manual=False):
        from watch_policy import quiet_scan_allowed
        from watch import read_rows
        from send import _ocr_scaled
        from media_send import owned_windows
        if not self.enabled() or self.busy() or (not manual and (not quiet_scan_allowed() or time.time()-self.last<60)):return None
        self.last=time.time()
        rows,image,path=read_rows(runtime,self.root,label+'-before')
        if any(r.get('unread') for r in rows):return {'status':'red_preempted'}
        selected=runtime.recognize_image('ContactSelectedRow',image)
        times=runtime.recognize_image('ReadRecentTimes',image)
        protected=protected_rows(self.store);uncertain=set()
        if self.ledger.exists():
            for line in self.ledger.read_text(encoding='utf-8').splitlines():
                event=json.loads(line)
                if event.get('status') in ('attempting','uncertain'):uncertain.add(event.get('avatar_sha'))
                elif event.get('status')=='hidden':uncertain.discard(event.get('avatar_sha'))
        decisions=[];candidate=None
        for row in rows:
            native=[h for h in times if row['y']-25<=box(h)[1]+box(h)[3]/2<=row['y']+4 and h.get('score',0)>=.85]
            vote=[native[0]['text']] if len(native)==1 else []
            if vote:
                hits=_ocr_scaled(runtime,image,'Read',[400,row['y']-25,72,27],2)
                if len(hits)==1 and hits[0].get('score',0)>=.85:vote.append(hits[0]['text'])
            active=any(box(h)[1]<=row['y']<box(h)[1]+box(h)[3] for h in selected)
            ok,reason=eligible(row,vote,now=time.time(),protected=protected,selected=active)
            if row.get('avatar_sha') in self.skipped:ok,reason=False,'menu_skipped'
            if row.get('avatar_sha') in uncertain:ok,reason=False,'previous_uncertain'
            if sum(norm(q['name'])==norm(row['name']) for q in rows)>1:ok,reason=False,'duplicate_name'
            decisions.append({'name':row['name'],'reason':reason,'time_votes':vote})
            if ok and candidate is None:candidate=row
        if candidate is None:return {'status':'no_candidate','decisions':decisions}
        # Re-read immediately before context click. A new red badge cancels cleanup.
        fresh,fresh_image,_=read_rows(runtime,self.root,label+'-guard')
        if any(r.get('unread') for r in fresh):return {'status':'red_preempted'}
        match=[r for r in fresh if r['key']==candidate['key'] and r['y']==candidate['y']]
        if len(match)!=1:return {'status':'row_changed'}
        row=match[0]
        fresh_times=runtime.recognize_image('ReadRecentTimes',fresh_image)
        hints=[h for h in fresh_times if row['y']-25<=box(h)[1]+box(h)[3]/2<=row['y']+4 and h.get('score',0)>=.85]
        last=[d['time_votes'] for d in decisions if d['name']==row['name']][0]
        if len(hints)!=1 or norm(hints[0]['text'])!=norm(last[0]) or self.busy():return {'status':'activity_changed'}
        selected_now=runtime.recognize_image('ContactSelectedRow',fresh_image)
        if any(box(h)[1]<=row['y']<box(h)[1]+box(h)[3] for h in selected_now):return {'status':'selected'}
        runtime.node('SelectRecentRow',{'SelectRecentRow':{'target':[200,row['y'],1,1],'contact':1,
            'custom_recognition_param':{'y':row['y'],'name':row['name'],'avatar_sha':row['avatar_sha']}}})
        popup=None;clicked=False
        try:
            choices=[w for w in owned_windows(runtime,label+'-menu') if w['class']=='Qt51514QWindowToolSaveBits' and w['title']=='Weixin' and w['shape'][1:]==[210,3] and 200<=w['shape'][0]<=400]
            if len(choices)!=1:raise ValueError('HIDE_MENU_NOT_UNIQUE')
            popup=RecentMenu(runtime,choices[0])
            im,menu_path=popup.capture(label+'-menu-proof')
            passes=[runtime.recognize_image('Read',im,roi=[0,0,210,im.shape[0]]),_ocr_scaled(runtime,im,'Read',[0,0,210,im.shape[0]],2)]
            target=menu_target(passes)
            if target is None:
                self.skipped.add(row['avatar_sha'])
                return {'status':'menu_not_verified','name':row['name']}
            event={'name':row['name'],'key':row['key'],'avatar_sha':row['avatar_sha'],'before':path,'menu':menu_path,'status':'attempting'}
            self.record(event);clicked=True
            popup.node('HideRecentMenu',{'HideRecentMenu':{'recognition':'OCR','roi':[24,target[1]-5,160,target[3]+10],
                'expected':['^不显示$'],'threshold':.9,'action':'Click','target':target,'post_delay':250,'timeout':500}})
            acknowledge_notice(runtime,label)
            after,_,after_path=read_rows(runtime,self.root,label+'-after')
            same=any(q['avatar_sha']==row['avatar_sha'] or norm(q['name'])==norm(row['name']) for q in after)
            # Require stable unchanged neighboring rows: list absence alone might be scrolling.
            neighbors={q['avatar_sha'] for q in fresh if q['key']!=row['key']}
            overlap=sum(q['avatar_sha'] in neighbors for q in after)
            verified=not same and overlap>=min(3,len(neighbors))
            event.update(status='hidden' if verified else 'uncertain',after=after_path)
            self.record(event)
            if verified:self.result['hidden']+=1
            self.result.update(last_status=event['status'],last_name=row['name'])
            return event
        except Exception as exc:
            self.skipped.add(row['avatar_sha'])
            event={'status':'uncertain' if clicked else 'guard_failed','name':row['name'],'key':row['key'],'avatar_sha':row['avatar_sha'],'error':str(exc)}
            self.record(event);return event
        finally:
            if popup:
                try:popup.escape()
                except Exception:pass
            elif not clicked:
                # Dismiss only the context menu we just opened when its geometry
                # is unsupported; never leave it over the live recent list.
                try:runtime.node('HideMenuEscape',allow_miss=True)
                except Exception:pass

class RecentMenu:
    def __init__(self,runtime,row):
        from maa.controller import Win32Controller,MaaWin32ScreencapMethodEnum as C,MaaWin32InputMethodEnum as I
        from maa.tasker import Tasker
        self.runtime=runtime;self.row=row
        self.controller=Win32Controller(row['hwnd'],C.PrintWindow,I.PostMessage,I.PostMessage)
        self.controller.set_screenshot_use_raw_size(True)
        assert self.controller.post_connection().wait().succeeded
        self.tasker=Tasker();self.tasker.bind(runtime.resource,self.controller)
    def guard(self):
        import ctypes
        from ctypes import wintypes
        u=ctypes.windll.user32;p=wintypes.DWORD();root=wintypes.DWORD()
        u.GetWindowThreadProcessId(self.row['hwnd'],ctypes.byref(p));u.GetWindowThreadProcessId(self.runtime.hwnd,ctypes.byref(root))
        klass=ctypes.create_unicode_buffer(256);u.GetClassNameW(self.row['hwnd'],klass,256)
        if not u.IsWindowVisible(self.row['hwnd']) or p.value!=root.value or klass.value!='Qt51514QWindowToolSaveBits':raise ValueError('HIDE_MENU_OWNER_CHANGED')
    def capture(self,label):
        from PIL import Image
        self.guard();im=self.controller.post_screencap().wait().get()
        if im is None or list(im.shape)!=self.row['shape']:raise ValueError('HIDE_MENU_GEOMETRY_CHANGED')
        path=Path(__file__).parent/'data'/(label+'.png')
        Image.fromarray(im[:,:,::-1]).save(path);return im,str(path)
    def node(self,name,override):
        self.guard();job=self.tasker.post_task(name,override).wait()
        if not job.succeeded:raise ValueError('HIDE_MENU_CLICK_FAILED')
    def escape(self):
        self.node('HideMenuEscape',{'HideMenuEscape':{'recognition':'DirectHit','action':'ClickKey','key':27,'post_delay':100}})


def notice_target(passes):
    if len(passes)!=2:return None
    targets=[]
    for hits in passes:
        high=[h for h in hits if h.get('score',0)>=.85]
        text=norm(''.join(h['text'] for h in sorted(high,key=lambda h:(box(h)[1],box(h)[0]))))
        words={norm(h['text']) for h in high}
        if not {'不显示聊天','我知道了'}<=words:return None
        if not all(s in text for s in ['不会删除聊天记录','有新消息','重新显示','搜索','找回']):return None
        buttons=[h for h in high if norm(h['text'])=='我知道了']
        if len(buttons)!=1:return None
        x,y,w,h=box(buttons[0])
        if not (900<=x<=1015 and 555<=y<=590 and w<=100):return None
        targets.append([x,y,w,h])
    if any(abs(a-b)>5 for a,b in zip(*targets)):return None
    return targets[0]

def acknowledge_notice(runtime,label):
    from send import _ocr_scaled
    im,path=runtime.capture(label+'-notice-check');roi=[795,390,330,257]
    native=runtime.recognize_image('Read',im,roi=roi)
    if not any(norm(h.get('text'))=='不显示聊天' for h in native):return False
    target=notice_target([native,_ocr_scaled(runtime,im,'Read',roi,2)])
    if target is None:raise ValueError('HIDE_NOTICE_NOT_VERIFIED')
    runtime.node('Read',{'Read':{'recognition':'OCR','roi':roi,'expected':['^我知道了$'],
        'threshold':.9,'action':'Click','target':target,'post_delay':300}})
    frame,_=runtime.capture(label+'-notice-after')
    if any(norm(h.get('text'))=='不显示聊天' for h in runtime.recognize_image('Read',frame,roi=roi)):raise ValueError('HIDE_NOTICE_STILL_VISIBLE')
    return True
