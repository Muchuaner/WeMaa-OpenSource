"""Native message copy. Body pixels are masked before metadata OCR.

Only the existing GUI owner calls this module. Never fall back to OCR body text.
"""
import hashlib
import json
from pathlib import Path
import time
import numpy as np
try:
    from .message_vision import DEFAULT_CHAT_ROI, _bubble_for_hit, _fill_component_box, extract_message_payloads
except ImportError:
    from message_vision import DEFAULT_CHAT_ROI, _bubble_for_hit, _fill_component_box, extract_message_payloads


def bubble_seeds(image):
    rgb=image[:,:,:3][:,:,::-1]
    rx,ry,rw,rh=DEFAULT_CHAT_ROI
    background=np.median(rgb[ry:ry+rh,rx:rx+3].reshape(-1,3),axis=0).astype(np.int16)
    bubbles=[];seeds=[]
    for x in (560,1824):
        for y in range(ry+12,ry+rh-12,6):
            if any(b['box'][0]<=x<b['box'][0]+b['box'][2] and b['box'][1]<=y<b['box'][1]+b['box'][3] for b in bubbles):continue
            fill=rgb[y,x].astype(np.int16)
            if not ((fill.max()-fill.min()<=8 and fill.min()>=205 and np.max(abs(fill-background))>=6)
                    or (fill[1]>=170 and fill[1]-fill[0]>=25 and fill[1]-fill[2]>=30 and fill[0]>=80)):continue
            b=_bubble_for_hit(rgb,[x,y,2,2],DEFAULT_CHAT_ROI,background)
            if not b:
                # Mask clipped bodies too, but do not turn them into complete
                # copy/reply evidence or let OCR recover partial body text.
                rect=_fill_component_box(rgb,(x,y),fill,[rx,ry,rw,rh],DEFAULT_CHAT_ROI)
                bx,by,bw,bh=rect
                if bw>=22 and bh>=22 and (by<=ry or by+bh>=ry+rh) and (35<=bx-rx<=115 or 10<=rx+rw-bx-bw<=100):
                    bubbles.append({'box':rect,'partial':True})
                continue
            if any(b['box']==q['box'] for q in bubbles):continue
            bubbles.append(b);seeds.append({'text':'NATIVE_COPY_PENDING','box':[x,y,2,2],'score':1.0})
    return bubbles,seeds


def metadata_hits(runtime,image):
    bubbles,seeds=bubble_seeds(image)
    masked=image.copy()
    background=np.median(image[100:500,490:493,:3].reshape(-1,3),axis=0).astype(np.uint8)
    for b in bubbles:
        x,y,w,h=b['box'];masked[y:y+h,x:x+w,:3]=background
    native=runtime.recognize_image('ReadChatMessages',masked)
    from PIL import Image
    scale=2
    large=np.asarray(Image.fromarray(masked).resize((masked.shape[1]*scale,masked.shape[0]*scale)))
    more=runtime.recognize_image('ReadChatMessages',large,roi=[v*scale for v in DEFAULT_CHAT_ROI])
    mapped=[dict(q,box=[round(v/scale) for v in q['box']]) for q in more]
    def outside(hit):
        from message_vision import _box
        rect=_box(hit)
        if not rect:return False
        x,y,w,h=rect
        return not any(max(x,b['box'][0])<min(x+w,b['box'][0]+b['box'][2]) and max(y,b['box'][1])<min(y+h,b['box'][1]+b['box'][3]) for b in bubbles)
    return [h for h in native if outside(h)]+seeds,[[h for h in mapped if outside(h)]+seeds]


class Clipboard:
    """CF_UNICODETEXT with sequence/owner proof; no clearing user clipboard."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c=ctypes;self.u=ctypes.WinDLL('user32',use_last_error=True);self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.u.GetClipboardOwner.restype=wintypes.HWND
        self.u.GetClipboardData.argtypes=[wintypes.UINT];self.u.GetClipboardData.restype=wintypes.HANDLE
        self.u.OpenClipboard.argtypes=[wintypes.HWND];self.u.OpenClipboard.restype=wintypes.BOOL
        self.k.GlobalLock.argtypes=[wintypes.HGLOBAL];self.k.GlobalLock.restype=wintypes.LPVOID
        self.k.GlobalUnlock.argtypes=[wintypes.HGLOBAL]
        self.k.GlobalSize.argtypes=[wintypes.HGLOBAL];self.k.GlobalSize.restype=ctypes.c_size_t
    def sequence(self):return self.u.GetClipboardSequenceNumber()
    def stage(self,text,hwnd):
        """仅供出站粘贴暂存，不能作为原生气泡复制或发送证据。"""
        from ctypes import wintypes
        if not isinstance(text,str) or not text or '\0' in text:
            raise ValueError('PASTE_TEXT_INVALID')
        encoded=text.encode('utf-16-le')+b'\0\0'
        self.k.GlobalAlloc.argtypes=[wintypes.UINT,self.c.c_size_t]
        self.k.GlobalAlloc.restype=wintypes.HGLOBAL
        self.k.GlobalFree.argtypes=[wintypes.HGLOBAL]
        self.u.SetClipboardData.argtypes=[wintypes.UINT,wintypes.HANDLE]
        self.u.SetClipboardData.restype=wintypes.HANDLE
        handle=self.k.GlobalAlloc(0x42,len(encoded)) # MOVEABLE | ZEROINIT，包含分配器填充字节。
        if not handle:raise ValueError('PASTE_ALLOCATION_FAILED')
        opened=False
        try:
            pointer=self.k.GlobalLock(handle)
            if not pointer:raise ValueError('PASTE_TEXT_LOCK_FAILED')
            try:self.c.memmove(pointer,encoded,len(encoded))
            finally:self.k.GlobalUnlock(handle)
            if not self.u.OpenClipboard(hwnd):raise ValueError('PASTE_CLIPBOARD_BUSY')
            opened=True
            if not self.u.EmptyClipboard():raise ValueError('PASTE_CLIPBOARD_CLEAR_FAILED')
            if not self.u.SetClipboardData(13,handle):raise ValueError('PASTE_CLIPBOARD_WRITE_FAILED')
            handle=None # Windows 取得缓冲区所有权。
        finally:
            if opened:self.u.CloseClipboard()
            if handle:self.k.GlobalFree(handle)
        return self.sequence()

    def staged_matches(self,text,sequence,hwnd):
        # stage 与消费之间任何剪贴板变化都必须停止，禁止粘贴别人的新内容。
        if self.sequence()!=sequence:return False
        try: value=self.read(sequence-1,hwnd)
        except ValueError:return False
        return self.sequence()==sequence and value==text
    def read(self,sequence,hwnd):
        from ctypes import wintypes
        if self.sequence()==sequence:raise ValueError('COPY_CLIPBOARD_NOT_CHANGED')
        a=wintypes.DWORD();b=wintypes.DWORD()
        self.u.GetWindowThreadProcessId(self.u.GetClipboardOwner(),self.c.byref(a))
        self.u.GetWindowThreadProcessId(hwnd,self.c.byref(b))
        if not a.value or a.value!=b.value:raise ValueError('COPY_CLIPBOARD_OWNER_MISMATCH')
        if not self.u.OpenClipboard(None):raise ValueError('COPY_CLIPBOARD_BUSY')
        try:
            if not self.u.IsClipboardFormatAvailable(13):raise ValueError('COPY_NO_UNICODE_TEXT')
            handle=self.u.GetClipboardData(13);size=self.k.GlobalSize(handle)
            if not handle or not 2<=size<=16*1024*1024:raise ValueError('COPY_TEXT_SIZE_INVALID')
            pointer=self.k.GlobalLock(handle)
            if not pointer:raise ValueError('COPY_TEXT_LOCK_FAILED')
            try:text=self.c.string_at(pointer,size).decode('utf-16-le').split('\0',1)[0]
            finally:self.k.GlobalUnlock(handle)
            if not text:raise ValueError('COPY_TEXT_EMPTY')
            return text
        finally:self.u.CloseClipboard()


def menu_copy_target(passes):
    if not 2<=len(passes)<=3:return None
    targets=[]
    for hits in passes:
        high=[h for h in hits if h.get('score',0)>=.85]
        # Text-only native menu; images/files must not masquerade as text.
        words={''.join(h.get('text','').split()) for h in high}
        if '放大阅读' not in words:continue
        copies=[h for h in high if ''.join(h.get('text','').split())=='复制']
        if len(copies)!=1:continue
        targets.append(copies[0]['box'])
    if len(targets)<2:return None
    a=targets[0]
    if any(abs(a[0]+a[2]/2-b[0]-b[2]/2)>5 or abs(a[1]+a[3]/2-b[1]-b[3]/2)>5 for b in targets[1:]):return None
    left,top=max(b[0] for b in targets),max(b[1] for b in targets)
    right,bottom=min(b[0]+b[2] for b in targets),min(b[1]+b[3] for b in targets)
    if right-left<10 or bottom-top<8:return None
    return [left,top,right-left,bottom-top]


def unchanged(runtime,image):
    current=runtime.capture_image()
    if current.shape!=image.shape or not np.array_equal(current[35:887,490:1885,:3],image[35:887,490:1885,:3]):
        raise ValueError('COPY_MESSAGE_FRAME_CHANGED')


def unchanged_message(runtime,image,payload):
    """Protect the exact copy target while unrelated incoming bubbles can grow.

    Message copy protects its own body and portrait. A moved
    target still requires a new observation; old coordinates are never retried.
    """
    current=runtime.capture_image()
    if current.shape!=image.shape or not np.array_equal(current[35:75,490:1885,:3],image[35:75,490:1885,:3]):
        raise ValueError('COPY_MESSAGE_FRAME_CHANGED')
    visual=payload.get('visual') or {}
    # Message bodies end at 1885; the independently extracted right avatar
    # occupies [1858, 1894). Keep the stricter body boundary and protect the
    # complete avatar plus its border, rather than clipping it to the body ROI.
    regions=[(visual.get('box'),1885)]
    if visual.get('avatar_verified') is True:regions.append((visual.get('avatar_box'),1900))
    for region,right_limit in regions:
        if not isinstance(region,list) or len(region)!=4 or any(type(v) is not int for v in region):
            raise ValueError('COPY_MESSAGE_TARGET_NOT_VERIFIED')
        x,y,w,h=region
        if w<=0 or h<=0 or x<490 or y<82 or x+w>right_limit or y+h>887 or x+w>image.shape[1]:
            raise ValueError('COPY_MESSAGE_TARGET_NOT_VERIFIED')
        # Borders are part of the target, including two pixels around the crop.
        left,top=max(490,x-2),max(82,y-2)
        right,bottom=min(right_limit,x+w+2),min(887,y+h+2)
        if not np.array_equal(current[top:bottom,left:right,:3],image[top:bottom,left:right,:3]):
            raise ValueError('COPY_MESSAGE_FRAME_CHANGED')


def copy_bubble(runtime,image,payload,label):
    from media_send import owned_windows
    from inactive_chats import RecentMenu
    from send import _ocr_scaled
    unchanged_message(runtime,image,payload)
    if any(w['class']=='Qt51514QWindowToolSaveBits' for w in owned_windows(runtime,label+'-existing-menu')):
        raise ValueError('COPY_EXISTING_POPUP')
    x,y,w,h=payload['visual']['box']
    clipboard=Clipboard();before=clipboard.sequence();popup=None
    try:
        runtime.node('CopyMessageMenu',{'CopyMessageMenu':{'target':[x+min(20,w//2),y+min(20,h//2),1,1]}})
        choices=[r for r in owned_windows(runtime,label) if r['class']=='Qt51514QWindowToolSaveBits' and r['title']=='Weixin']
        if len(choices)!=1:raise ValueError('COPY_MENU_NOT_UNIQUE')
        popup=RecentMenu(runtime,choices[0]);frame,path=popup.capture(label+'-menu')
        roi=[0,0,frame.shape[1],frame.shape[0]]
        passes=[runtime.recognize_image('Read',frame,roi=roi),_ocr_scaled(runtime,frame,'Read',roi,2)]
        if menu_copy_target(passes) is None:passes.append(_ocr_scaled(runtime,frame,'Read',roi,3))
        Path(path).with_suffix('.json').write_text(json.dumps(passes,ensure_ascii=False),encoding='utf-8')
        target=menu_copy_target(passes)
        if target is None:raise ValueError('COPY_TEXT_MENU_NOT_VERIFIED')
        popup.node('CopyMessageSelect',{'CopyMessageSelect':{'recognition':'DirectHit','action':'Click','target':target,'post_delay':150}})
        for _ in range(10):
            if clipboard.sequence()!=before:break
            time.sleep(.05)
        text=clipboard.read(before,runtime.hwnd)
        unchanged_message(runtime,image,payload)
        return text,{'verified':True,'method':'native_context_menu','menu_frame':path,
                     'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),'characters':len(text)}
    finally:
        try:
            if popup:
                popup.escape()
            else:
                # The menu may already have vanished. Escape sent to the main
                # WeChat HWND can hide the entire client to tray. Only a fresh,
                # uniquely observed process-owned context menu may receive it.
                remaining=[row for row in owned_windows(runtime,label+'-cleanup')
                           if row['class']=='Qt51514QWindowToolSaveBits' and row['title']=='Weixin']
                if len(remaining)==1:RecentMenu(runtime,remaining[0]).escape()
        except Exception:
            pass


def read_messages(runtime,image,*,group_chat=False,previous_payloads=None):
    hits,passes=metadata_hits(runtime,image)
    payloads=extract_message_payloads(image,hits,consensus_passes=passes,group_chat=group_chat)
    # Reuse text only for a uniquely located historical occurrence. Newly
    # appended identical messages still obtain their own native clipboard proof.
    previous_payloads=previous_payloads or []
    from message_vision import reconcile_visual,message_identity
    overlap=reconcile_visual(previous_payloads,payloads) if previous_payloads else None
    matches=(overlap or {}).get('metadata',{}).get('matched_occurrences',{})
    for ordinal,p in enumerate(payloads):
        if p.get('message_kind')!='text' or p.get('direction') not in ('incoming','outgoing'):continue
        p['text']='';p['reply_safe']=False;p['source']='maa_native_clipboard'
        p['ocr']={'stable':False,'raw_hits':[],'reason':'body_ocr_disabled'}
        original_index=matches.get(str(ordinal),matches.get(ordinal))
        original=previous_payloads[original_index] if type(original_index) is int and 0<=original_index<len(previous_payloads) else None
        if (original and message_identity(original)==message_identity(p)
                and original.get('text_copy',{}).get('verified') is True
                and original.get('text_copy',{}).get('method')=='native_context_menu'):
            p['text']=original['text'];p['text_copy']=dict(original['text_copy'],reused_verified_occurrence=True)
            p['ocr']['stable']=True
            p['reply_safe']=p['direction']=='incoming' and (not group_chat or p.get('group_sender_verified') is True)
            continue
        try:
            text,proof=copy_bubble(runtime,image,p,f'copy-{time.time_ns()}-{ordinal}')
            p['text']=text;p['text_copy']=proof
            # Compatibility field for downstream stability gates. No OCR votes.
            p['ocr']['stable']=True
            p['reply_safe']=p['direction']=='incoming' and (not group_chat or p.get('group_sender_verified') is True)
        except (ValueError,RuntimeError) as exc:
            p['text_copy']={'verified':False,'reason':str(exc)}
    return payloads,hits


def _draft_copy_keys(runtime):
    """Read-only keys require the verified WeChat window to own foreground."""
    import ctypes
    from ctypes import wintypes
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetForegroundWindow.restype=wintypes.HWND
    if user.GetForegroundWindow()!=runtime.hwnd:raise ValueError('DRAFT_FOREGROUND_CHANGED')
    if any(user.GetAsyncKeyState(key)&0x8000 for key in (16,17,18,91,92)):
        raise ValueError('DRAFT_USER_MODIFIER_HELD')
    # No typing, paste, delete, Enter or send keys are allowed here.
    try:
        user.keybd_event(17,0,0,0)
        for key in (65,67):
            if user.GetForegroundWindow()!=runtime.hwnd:raise ValueError('DRAFT_FOREGROUND_CHANGED')
            user.keybd_event(key,0,0,0);user.keybd_event(key,0,2,0)
            time.sleep(.05)
    finally:
        user.keybd_event(17,0,2,0)
    time.sleep(.1)


def draft_recipient_regions(runtime,image):
    """绑定已验证原帧的标题、选中条、头像和名称，不把新来信当成草稿变化。"""
    if image.shape[:2]!=(1040,1920):raise ValueError('DRAFT_TARGET_NOT_VERIFIED')
    rows=runtime.recognize_image('ContactSelectedRow',image)
    boxes=[r.get('box') for r in rows]
    if len(boxes)!=1 or not isinstance(boxes[0],list) or len(boxes[0])!=4:
        raise ValueError('DRAFT_TARGET_NOT_VERIFIED')
    x,y,w,h=boxes[0]
    if (any(type(v) is not int for v in boxes[0]) or not 50<=x<=75
            or not 1<=w<=16 or not 30<=h<=90 or y<0 or y+h>1040 or not 94<=y+h//2<=1010):
        raise ValueError('DRAFT_TARGET_NOT_VERIFIED')
    center=y+h//2
    return [(490,35,1395,40),(x,y,w,h),(78,center-13,24,26),(112,center-23,188,25)]


def unchanged_draft_recipient(runtime,image,regions):
    current=runtime.capture_image()
    if current.shape!=image.shape:raise ValueError('DRAFT_RECIPIENT_CHANGED')
    # Incoming messages may move the same selected row. Rebind only through
    # one complete selected strip, with exact title/avatar/name pixels.
    fresh=draft_recipient_regions(runtime,current)
    for label,old,new in zip(('title','strip','avatar','name'),regions,fresh):
        x,y,w,h=old;nx,ny,nw,nh=new
        if (x,w,h)!=(nx,nw,nh) or not np.array_equal(
                current[ny:ny+nh,nx:nx+nw,:3],image[y:y+h,x:x+w,:3]):
            raise ValueError('DRAFT_RECIPIENT_CHANGED:'+label)


def native_draft(runtime,image,expected):
    """Read the focused draft twice; never write, clear, or send clipboard text."""
    regions=draft_recipient_regions(runtime,image)
    guard=lambda:unchanged_draft_recipient(runtime,image,regions)
    guard()
    clipboard=Clipboard();copies=[]
    runtime.node('FocusChatInput')
    for _ in range(2):
        guard()
        sequence=clipboard.sequence()
        _draft_copy_keys(runtime)
        copies.append(clipboard.read(sequence,runtime.hwnd))
        guard()
    return {'verified':all(value==expected for value in copies),
            'method':'native_draft_clipboard','reads':len(copies),
            'sha256':[hashlib.sha256(value.encode()).hexdigest() for value in copies]}
