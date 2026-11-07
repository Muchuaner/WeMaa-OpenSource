"""Verified invitation cards, one join click, persisted receipts in the GUI owner."""
from __future__ import annotations
import ctypes,hashlib,json,re,time
from ctypes import wintypes
from pathlib import Path
import numpy as np
from PIL import Image
from flows import box

DEFAULT_POLICY={'auto_accept_group_invites':True}

def policy(root):
    path=Path(root)/'data'/'group-invite-policy.json'
    try:value=json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:return dict(DEFAULT_POLICY)
    if not isinstance(value,dict) or type(value.get('auto_accept_group_invites')) is not bool:raise ValueError('INVALID_GROUP_INVITE_POLICY')
    return value

def card_candidates(hits):
    return [h for h in hits if h.get('text','').strip()=='邀请你加入群聊' and h.get('score',0)>=.9
            and 540<=box(h)[0]<=800 and 90<=box(h)[1]<=790 and box(h)[1]+90<=887]

def page_state(hits):
    names=[h for h in hits if 130<=box(h)[1]<=250 and h.get('score',0)>=.9 and re.fullmatch(r'.+[（(][1-9][0-9]{0,4}[）)]',h.get('text','').strip())]
    joins=[h for h in hits if h.get('text','').strip()=='加入群聊' and h.get('score',0)>=.95 and 620<=box(h)[1]<=720]
    if len(names)!=1 or len(joins)!=1:return None
    return {'title':names[0]['text'].strip(),'join_box':box(joins[0])}

def process_chain(pid):
    """Read native process ancestry; no private WeChat protocol or memory."""
    class ENTRY(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    k=ctypes.windll.kernel32;k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    k.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(ENTRY)];k.Process32NextW.argtypes=k.Process32FirstW.argtypes;k.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=k.CreateToolhelp32Snapshot(2,0);rows={};e=ENTRY();e.dwSize=ctypes.sizeof(e)
    try:
        more=k.Process32FirstW(handle,ctypes.byref(e))
        while more:
            rows[e.th32ProcessID]=(e.th32ParentProcessID,e.szExeFile);more=k.Process32NextW(handle,ctypes.byref(e))
    finally:k.CloseHandle(handle)
    result=[];seen=set()
    while pid in rows and pid not in seen:
        seen.add(pid);parent,name=rows[pid];result.append((pid,name));pid=parent
    return result

def belongs_to_wechat(hwnd,main):
    u=ctypes.windll.user32;a=wintypes.DWORD();b=wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd,ctypes.byref(a));u.GetWindowThreadProcessId(main,ctypes.byref(b))
    chain=process_chain(a.value)
    for pid,name in chain:
        if pid==b.value:return name.lower()=='weixin.exe'
        if name.lower() not in ('wechatappex.exe','weixin.exe'):return False
    return False

class InvitePage:
    def __init__(self,runtime,window):
        from maa.controller import Win32Controller,MaaWin32ScreencapMethodEnum as Capture,MaaWin32InputMethodEnum as Input
        from maa.tasker import Tasker
        self.runtime=runtime;self.hwnd=window.hwnd
        self.controller=Win32Controller(self.hwnd,Capture.PrintWindow,Input.PostMessageWithCursorPos,Input.PostMessage)
        self.controller.set_screenshot_use_raw_size(True)
        if not self.controller.post_connection().wait().succeeded:raise ValueError('INVITE_PAGE_CONNECTION_FAILED')
        self.tasker=Tasker();self.tasker.bind(runtime.resource,self.controller)
    def capture(self,label):
        if not belongs_to_wechat(self.hwnd,self.runtime.hwnd):raise ValueError('INVITE_PAGE_OWNER_CHANGED')
        im=self.controller.post_screencap().wait().get()
        if im is None or im.shape!=(792,584,3):raise ValueError('INVITE_PAGE_GEOMETRY_CHANGED')
        path=Path(__file__).parent/'data'/(label+'.png');Image.fromarray(im[:,:,::-1]).save(path)
        return im,str(path)
    def read(self,image):return self.runtime.recognize_image('Read',image,roi=[0,0,584,792])
    def click(self,name):
        self.capture(self.runtime.run_id+'-'+name+'-before')
        job=self.tasker.post_task(name).wait()
        from runner import plain,ROOT
        record={'run_id':self.runtime.run_id,'node':name,'hwnd':self.hwnd,'time_unix':time.time(),'succeeded':job.succeeded,'detail':plain(job.get())}
        with (ROOT/'data'/'actions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
        if not job.succeeded:raise ValueError('INVITE_PAGE_ACTION_FAILED:'+name)


def invitation_pages(runtime):
    from maa.toolkit import Toolkit
    return [InvitePage(runtime,w) for w in Toolkit.find_desktop_windows() if w.class_name=='Chrome_WidgetWin_0' and belongs_to_wechat(w.hwnd,runtime.hwnd)]

def accept_page(runtime,store,key,label,page):
    prior=store.db.execute('SELECT status,detail_json FROM group_invites WHERE id=?',(key,)).fetchone()
    if not prior or prior[0]!='opened':return {'status':prior[0] if prior else 'blocked','duplicate':True,'join_clicks':0}
    a,path=page.capture(label+'-invitation');first=page_state(page.read(a))
    from send import _ocr_scaled
    second=page_state(_ocr_scaled(runtime,a,'Read',[0,0,584,792],2))
    if not first or not second or first['title']!=second['title']:raise ValueError('INVITE_PAGE_NOT_VERIFIED')
    current,_=page.capture(label+'-prejoin')
    if not np.array_equal(a[170:220,130:430],current[170:220,130:430]) or page_state(page.read(current))!=first:raise ValueError('INVITE_PAGE_CHANGED')
    # Durable before the external membership action; never replay after a crash.
    store.db.execute('UPDATE group_invites SET status=?,detail_json=?,updated=? WHERE id=?',('join_started',json.dumps({'title':first['title'],'frame':path},ensure_ascii=False),time.time(),key));store.db.commit()
    page.click('AcceptGroupInvite')
    time.sleep(2)
    result={'status':'uncertain','title':first['title'],'invitation_frame':path,'join_clicks':1}
    try:
        after,after_path=page.capture(label+'-afterjoin');result.update(after_frame=after_path,after_ocr=page.read(after))
    except Exception as exc:result['after_error']=str(exc)
    expected=re.sub(r'[（(][0-9]+[）)]$','',first['title'])
    from send import _target,_ocr_scaled
    for attempt in range(5):
        main,main_path=runtime.capture(label+'-joined-main-'+str(attempt));result['main_frame']=main_path
        result['main_title']=' '.join(h['text'] for h in runtime.recognize_image('ReadChatTitle',main))
        if re.fullmatch(re.escape(expected)+r'(?:\s*[（(][0-9]+[）)])?',result['main_title']):
            if joined_evidence(runtime,main,expected):
                result.update(status='accepted',selected_row_verified=True,stable_header=True);break
        time.sleep(.5)
    store.db.execute('UPDATE group_invites SET status=?,detail_json=?,updated=? WHERE id=?',(result['status'],json.dumps(result,ensure_ascii=False),time.time(),key));store.db.commit();store.event('group_invite_result',dict(result,invite_id=key))
    return result

def initialize(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS group_invites(id TEXT PRIMARY KEY,status TEXT,detail_json TEXT,updated REAL)')
    store.db.execute("UPDATE group_invites SET status='uncertain' WHERE status IN ('opened','join_started')")
    store.db.commit()

def process_card(runtime,root,store,row,title,image,hits,label):
    if not policy(root)['auto_accept_group_invites']:return False
    candidates=card_candidates(hits)
    if len(candidates)!=1:return False
    h=candidates[0];x,y,w,height=box(h);roi=[550,y-10,247,100]
    from send import _ocr_scaled,_target
    second=card_candidates(_ocr_scaled(runtime,image,'ReadChatMessages',[490,82,1395,805],2))
    if len(second)!=1 or abs(box(second[0])[1]-y)>4:return False
    key=hashlib.sha256((row['key']+'|').encode()+image[roi[1]:roi[1]+roi[3],550:797,:3].tobytes()).hexdigest()
    if store.db.execute('SELECT 1 FROM group_invites WHERE id=?',(key,)).fetchone():return False
    target=dict(row,conversation_title=title)
    current,_=runtime.capture(label+'-card-preclick')
    if not _target(runtime,current,target,enforce_row_y=False)['verified'] or not np.array_equal(image[y-10:y+90,550:797],current[y-10:y+90,550:797]):return False
    if invitation_pages(runtime):
        store.event('group_invite_blocked',{'reason':'EXISTING_WEB_WINDOW','name':row['name']});return False
    store.db.execute('INSERT INTO group_invites VALUES(?,?,?,?)',(key,'opened',json.dumps({'inviter':row['name'],'title':title},ensure_ascii=False),time.time()));store.db.commit()
    page=None
    try:
        runtime.node('OpenGroupInviteCard',{'OpenGroupInviteCard':{'roi':roi,'target':[x+w//2,y+40,1,1],'target_offset':[0,0,0,0]}})
        pages=[]
        for attempt in range(12):
            pages=invitation_pages(runtime)
            if pages:break
            time.sleep(.25)
        if len(pages)!=1:raise ValueError('INVITE_PAGE_NOT_UNIQUE')
        page=pages[0]
        result=accept_page(runtime,store,key,label,page)
    except Exception as exc:
        prior=store.db.execute('SELECT status FROM group_invites WHERE id=?',(key,)).fetchone()[0]
        result={'status':'uncertain' if prior=='join_started' else 'blocked','reason':str(exc),'inviter':row['name']}
        store.db.execute('UPDATE group_invites SET status=?,detail_json=?,updated=? WHERE id=?',(result['status'],json.dumps(result,ensure_ascii=False),time.time(),key));store.db.commit();store.event('group_invite_result',dict(result,invite_id=key))
    finally:
        if page:
            try:page.click('CloseGroupInvitePage')
            except Exception as exc:store.event('group_invite_close_failed',{'reason':str(exc)})
        runtime.node('OpenRecent')
    return True


def joined_evidence(runtime,image,expected):
    from send import _ocr_scaled
    def text(hits):return ''.join(re.sub(r'\s+','',h['text']).replace('（','(').replace('）',')') for h in hits if h.get('score',0)>=.9)
    headers=[runtime.recognize_image('ReadChatTitle',image),_ocr_scaled(runtime,image,'ReadChatTitle',[490,35,900,40],2)]
    if len({text(h) for h in headers})!=1 or not re.fullmatch(re.escape(expected)+r'(?:[（(][0-9]+[）)])?',text(headers[0])):return False
    selected=runtime.recognize_image('ContactSelectedRow',image)
    if len(selected)!=1:return False
    _,y,_,height=box(selected[0])
    badges=[box(h)[0] for h in runtime.recognize_image('InviteSidebarBlueBadge',image) if y+4<=box(h)[1]<=y+40]
    right=min(badges) if badges else 400
    roi=[115,y+4,right-118,35]
    if roi[2]<30:return False
    names=[]
    for scale in (2,1.5,3,4):
        names.append(text(_ocr_scaled(runtime,image,'Read',roi,scale)))
        if names.count(expected)>=2:return True
    return False
