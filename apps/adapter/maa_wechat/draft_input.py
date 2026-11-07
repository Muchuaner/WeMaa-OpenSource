"""完整 Unicode 草稿经微信原生菜单粘贴；不清除草稿、不点击发送。"""
import hashlib
import time

from text_copy import Clipboard, unchanged


def paste_target(passes):
    """只接受至少两次独立识别相同的微信输入框菜单。"""
    targets=[]
    for hits in passes:
        high=[h for h in hits if h.get('score',0)>=.9]
        words={''.join(h.get('text','').split()) for h in high}
        if words!={'粘贴','边写边译'}:continue
        choices=[h['box'] for h in high if ''.join(h.get('text','').split())=='粘贴']
        if len(choices)==1:targets.append(choices[0])
    if len(targets)<2:return None
    if any(abs(a-b)>4 for box in targets[1:] for a,b in zip(targets[0],box)):return None
    x,y,w,h=targets[0]
    return [x,y,w,h] if x>=0 and y>=0 and w>=10 and h>=8 else None


def paste_draft(runtime,image,text):
    from media_send import owned_windows
    from inactive_chats import RecentMenu
    from send import _draft, _ocr_scaled
    label='draft-paste-'+str(time.time_ns())
    clipboard=Clipboard();popup=None
    def guard():
        unchanged(runtime,image)
        if not _draft(runtime,runtime.capture_image())['verified']:
            raise ValueError('PASTE_DRAFT_NOT_EMPTY')
    guard()
    if any(w['class']=='Qt51514QWindowToolSaveBits' for w in owned_windows(runtime,label+'-existing')):
        raise ValueError('PASTE_EXISTING_POPUP')
    sequence=clipboard.stage(text,runtime.hwnd)
    try:
        guard()
        if not clipboard.staged_matches(text,sequence,runtime.hwnd):
            raise ValueError('PASTE_CLIPBOARD_CHANGED')
        runtime.node('CopyMessageMenu',{'CopyMessageMenu':{'target':[650,930,1,1]}})
        choices=[w for w in owned_windows(runtime,label)
                 if w['class']=='Qt51514QWindowToolSaveBits' and w['title']=='Weixin']
        if len(choices)!=1:raise ValueError('PASTE_MENU_NOT_UNIQUE')
        popup=RecentMenu(runtime,choices[0]);frame,path=popup.capture(label+'-menu')
        roi=[0,0,frame.shape[1],frame.shape[0]]
        passes=[runtime.recognize_image('Read',frame,roi=roi),_ocr_scaled(runtime,frame,'Read',roi,2)]
        target=paste_target(passes)
        if target is None:raise ValueError('PASTE_MENU_NOT_VERIFIED')
        guard()
        if not clipboard.staged_matches(text,sequence,runtime.hwnd):
            raise ValueError('PASTE_CLIPBOARD_CHANGED')
        # 菜单窗口自身再核验像素，不能向已消失或替换的菜单发点击。
        fresh,_=popup.capture(label+'-preclick')
        import numpy as np
        if not np.array_equal(fresh,frame):raise ValueError('PASTE_MENU_CHANGED')
        popup.node('CopyMessageSelect',{'CopyMessageSelect':{
            'recognition':'DirectHit','action':'Click','target':target,'post_delay':150}})
        return {'method':'native_paste_menu','menu_frame':path,
                'input_sha256':hashlib.sha256(text.encode()).hexdigest(),'characters':len(text)}
    finally:
        # 永远不向微信主窗口发 Escape，它会把微信最小化到托盘。
        try:
            remaining=[w for w in owned_windows(runtime,label+'-cleanup')
                       if w['class']=='Qt51514QWindowToolSaveBits' and w['title']=='Weixin']
            if len(remaining)==1:RecentMenu(runtime,remaining[0]).escape()
        except Exception:
            # 已成功粘贴后，清理观察失败不能把本次输入改成另一轮输入；
            # 发送器仍必须在新帧上验证目标、精确正文和一次性发送。
            pass
