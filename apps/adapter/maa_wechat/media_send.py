"""Maa-only owned-window media import and one-shot visual delivery."""
import ctypes,hashlib,json,time,re
from ctypes import wintypes
from pathlib import Path
import numpy as np
from PIL import Image

def owned_windows(runtime,label):
    from maa.toolkit import Toolkit
    from maa.controller import Win32Controller,MaaWin32ScreencapMethodEnum as Capture,MaaWin32InputMethodEnum as Input
    user=ctypes.windll.user32;owner=wintypes.DWORD();user.GetWindowThreadProcessId(runtime.hwnd,ctypes.byref(owner));rows=[]
    for w in Toolkit.find_desktop_windows():
        pid=wintypes.DWORD();user.GetWindowThreadProcessId(w.hwnd,ctypes.byref(pid))
        if w.hwnd==runtime.hwnd or pid.value!=owner.value:continue
        c=Win32Controller(w.hwnd,Capture.PrintWindow,Input.PostMessage,Input.PostMessage);c.set_screenshot_use_raw_size(True)
        if not c.post_connection().wait().succeeded:continue
        im=c.post_screencap().wait().get()
        if im is None or not im.size:continue
        path=Path(__file__).parent/'data'/f'{label}-window-{w.hwnd}.png';Image.fromarray(im[:,:,:3][:,:,::-1]).save(path)
        rows.append({'hwnd':w.hwnd,'title':w.window_name,'class':w.class_name,'shape':list(im.shape),'path':str(path)})
    return rows

class OwnedPopup:
    """Input is scoped to one observed, process-owned native dialog."""
    def __init__(self,runtime,row):
        from maa.controller import Win32Controller,MaaWin32ScreencapMethodEnum as Capture,MaaWin32InputMethodEnum as Input
        from maa.tasker import Tasker
        self.runtime=runtime;self.row=row
        self.controller=Win32Controller(row['hwnd'],Capture.PrintWindow,Input.Seize,Input.Seize)
        self.controller.set_screenshot_use_raw_size(True)
        if not self.controller.post_connection().wait().succeeded:raise ValueError('MEDIA_DIALOG_CONNECTION_FAILED')
        self.tasker=Tasker();self.tasker.bind(runtime.resource,self.controller)
        if not self.tasker.inited:raise ValueError('MEDIA_DIALOG_TASKER_FAILED')
    def guard(self):
        user=ctypes.windll.user32;pid=wintypes.DWORD();rootpid=wintypes.DWORD()
        user.GetWindowThreadProcessId(self.row['hwnd'],ctypes.byref(pid));user.GetWindowThreadProcessId(self.runtime.hwnd,ctypes.byref(rootpid))
        owner=user.GetWindow(self.row['hwnd'],4)
        visited=set()
        while owner and owner!=self.runtime.hwnd and owner not in visited:
            visited.add(owner);owner=user.GetWindow(owner,4)
        if not user.IsWindow(self.row['hwnd']) or pid.value!=rootpid.value or owner!=self.runtime.hwnd:raise ValueError('MEDIA_DIALOG_OWNER_CHANGED')
    def capture(self,label):
        self.guard();im=self.controller.post_screencap().wait().get()
        if im is None or list(im.shape)!=self.row['shape']:raise ValueError('MEDIA_DIALOG_GEOMETRY_CHANGED')
        path=Path(__file__).parent/'data'/(label+'.png');Image.fromarray(im[:,:,:3][:,:,::-1]).save(path);return im,str(path)
    def node(self,name,override=None):
        self.capture(getattr(self.runtime,'run_id','media')+'-'+name+'-before')
        job=self.tasker.post_task(name,override or {}).wait()
        from runner import plain,ROOT
        detail={'run_id':getattr(self.runtime,'run_id',None),'node':name,'owned_hwnd':self.row['hwnd'],'time_unix':time.time(),'succeeded':job.succeeded,'detail':plain(job.get())}
        with (ROOT/'data'/'actions.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(detail,ensure_ascii=False)+'\n')
        if not job.succeeded:raise ValueError('MEDIA_DIALOG_NODE_FAILED:'+name)
        return detail

class VerifiedMediaViewer(OwnedPopup):
    """The observed WeChat viewer is an unowned top-level window of the same PID."""
    def guard(self):
        user=ctypes.windll.user32;pid=wintypes.DWORD();rootpid=wintypes.DWORD()
        user.GetWindowThreadProcessId(self.row['hwnd'],ctypes.byref(pid));user.GetWindowThreadProcessId(self.runtime.hwnd,ctypes.byref(rootpid))
        title=ctypes.create_unicode_buffer(256);klass=ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(self.row['hwnd'],title,256);user.GetClassNameW(self.row['hwnd'],klass,256)
        if not user.IsWindow(self.row['hwnd']) or pid.value!=rootpid.value or title.value!='图片和视频' or klass.value!='Qt51514QWindowIcon' or self.row['shape']!=[761,1380,3]:raise ValueError('MEDIA_VIEWER_IDENTITY_CHANGED')

def file_dialog(runtime,label):
    for attempt in range(12):
        candidates=[w for w in owned_windows(runtime,label+'-'+str(attempt)) if w['class']=='#32770' and w['title']=='选择文件' and w['shape']==[501,944,3]]
        if len(candidates)>1:raise ValueError('MEDIA_FILE_DIALOG_NOT_UNIQUE')
        if candidates:return OwnedPopup(runtime,candidates[0])
        time.sleep(.25)
    raise ValueError('MEDIA_FILE_DIALOG_NOT_OBSERVED')

def import_media(runtime,item,label):
    path=Path(item['path']).resolve();cache=(Path(__file__).parent/'data'/'media-cache').resolve()
    system_image=item.get('media_key') in ('command_menu_v1','asset_catalog_v1') and not item.get('asset_id') and item.get('mime')=='image/png' and item.get('type')=='image'
    if not re.fullmatch('[a-f0-9]{32}',str(item.get('id',''))) or (not system_image and (item.get('media_key') or not isinstance(item.get('asset_id'),int) or item['asset_id']<=0)):raise ValueError('MEDIA_CACHE_ID_INVALID')
    basename=('catalog' if item.get('media_key')=='asset_catalog_v1' else 'menu') if system_image else str(item['asset_id'])
    if item.get('mime')=='video/mp4' and path.stem=='v'+item['id'][:8]:basename=path.stem
    if path.parent!=cache/item['id'] or not path.is_file() or path.name!=basename+{'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp','video/mp4':'.mp4'}.get(item['mime'],''):
        raise ValueError('MEDIA_CACHE_PATH_INVALID')
    raw=path.read_bytes()
    if len(raw)!=item['size'] or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('MEDIA_CACHE_DIGEST_CHANGED')
    popup=file_dialog(runtime,label)
    popup.node('MediaFolderFocus');popup.node('MediaFolderSelect')
    popup.node('MediaPathInput',{'MediaPathInput':{'input_text':str(path.parent)}});popup.node('MediaPathEnter')
    popup.node('MediaNameFocus');popup.node('MediaNameSelect');popup.node('MediaNameInput',{'MediaNameInput':{'input_text':path.name}})
    popup.node('MediaNameDefocus')
    frame,proof_path=popup.capture(label+'-filename')
    from send import _normal,_ocr_scaled
    roi=[192,430,500,30]
    passes=[runtime.recognize_image('Read',frame,roi=roi)]+[_ocr_scaled(runtime,frame,'Read',roi,scale) for scale in (1.5,2,3)]
    (Path(__file__).parent/'data'/(label+'-filename-ocr.json')).write_text(json.dumps({'passes':passes,'expected':path.name},ensure_ascii=False),encoding='utf-8')
    correct=sum(_normal(' '.join(h['text'] for h in hits))==path.name and all(h.get('score',0)>=.80 for h in hits) for hits in passes)
    if correct<2:raise ValueError('MEDIA_FILENAME_NOT_VERIFIED')
    popup.node('MediaFileOpen')
    return {'file_sha256':item['sha256'],'filename_frame':proof_path}

def exclusive_preview(image,box):
    from media_vision import composer_top
    top=composer_top(image)
    if top is None:return False
    region=image[top+8:990,500:1895].astype(np.int16)
    mask=np.max(abs(region-249),axis=2)>5
    x,y,w,h=box
    y0=max(0,y-top-12);y1=min(mask.shape[0],y+h-top-4)
    x0=max(0,x-504);x1=min(mask.shape[1],x+w-496)
    mask[y0:y1,x0:x1]=False
    if int(mask.sum())<=3:return True
    # WeChat's image insertion caret spans the preview height, unlike a text
    # caret. Only a 1–2px green vertical line immediately after this preview is
    # allowed; any other residual draft content still fails closed.
    ys,xs=np.nonzero(mask);colors=region[ys,xs]
    green=(colors[:,1]-colors[:,0]>40)&(colors[:,1]-colors[:,2]>40)
    absolute_x=xs+500;absolute_y=ys+top+8
    return bool(green.all() and absolute_x.max()-absolute_x.min()<=1
                and absolute_x.min()>=x+w+2 and absolute_x.max()<=x+w+5
                and absolute_y.min()>=y-3 and absolute_y.max()<=y+h+3
                and h-8<=absolute_y.max()-absolute_y.min()<=h+6
                and len(xs)<=2*(h+6))

def sent_card_box(image, name_y, top):
    """Find a complete fixed-width card from its borders, not OCR glyph height."""
    x,w,h=1618,226,60
    candidates=[]
    for y in range(max(84,name_y-16),min(top-68,name_y-3)+1):
        above=image[y-2:y,x+8:x+w-8].astype(np.int16)
        below=image[y+h:y+h+2,x+8:x+w-8].astype(np.int16)
        corner=image[y+2:y+8,x+2:x+8].astype(np.int16)
        avatar=image[y:y+36,1858:1894].astype(np.int16)
        if (np.max(abs(above-249))<=3 and np.max(abs(below-249))<=3
                and np.max(abs(corner-238))<=6
                and (np.max(abs(avatar-249),axis=2)>25).mean()>.18):
            candidates.append([x,y,w,h])
    return candidates[0] if len(candidates)==1 else None


def video_card(runtime,image,item,*,preview=False):
    """Accept the observed MP4 attachment card, using two independent OCR passes."""
    from send import _ocr_scaled,_normal
    from media_vision import composer_top
    top=composer_top(image)
    if top is None:return []
    roi=[500,top+8,300,990-top-8] if preview else [1260,82,585,top-90]
    if roi[3]<30:return []
    passes=[runtime.recognize_image('Read',image,roi=roi)]
    passes.extend(_ocr_scaled(runtime,image,'Read',roi,scale) for scale in (2,3))
    filename=Path(item['path']).name
    names=[[h for h in hits if _normal(h.get('text',''))==filename and h.get('score',0)>=.80] for hits in passes]
    result=[]
    for pass_id,hits in enumerate(names):
      for hit in hits:
        x,y,w,h=hit['box']
        if not any(i!=pass_id and any(abs(v['box'][0]-x)<=5 and abs(v['box'][1]-y)<=5 for v in values) for i,values in enumerate(names)):continue
        # Fixed imported card geometry observed in this WeChat version.
        box=[504,top+12,226,60] if preview else sent_card_box(image,y,top)
        if box is None:continue
        bx,by,bw,bh=box
        if by<82 or by+bh>(990 if preview else top-8):continue
        crop=image[by:by+bh,bx:bx+bw]
        if crop.shape!=(60,226,3):continue
        background=crop[2:8,2:8].astype(np.int16)
        if np.max(abs(background-238))>6:continue
        # The card must contain the video-file icon and expected byte-length label.
        region=[bx+8,by+31,160,25]
        expected=f'{item["size"]/1024:.1f}K' if item['size']<1024*1024 else f'{item["size"]/1024/1024:.1f}M'
        passes=[runtime.recognize_image('Read',image,roi=region),_ocr_scaled(runtime,image,'Read',region,2)]
        if not all(any(_normal(v.get('text',''))==expected and v.get('score',0)>=.8 for v in hits) for hits in passes):continue
        icon=crop[10:51,180:214]
        if not icon.size or (np.max(abs(icon.astype(np.int16)-238),axis=2)>15).sum()<100:continue
        if not any(abs(v['visual']['box'][0]-bx)<=5 and abs(v['visual']['box'][1]-by)<=5 for v in result):
            result.append({'visual':{'box':box,'sha256':hashlib.sha256(crop.tobytes()).hexdigest()},'direction':'outgoing','message_kind':'video'})
    return result


def discard_failed_preview(runtime, root, store, conversation, delivery_id, target, label):
    """Remove only a frozen, exclusively owned preview that never reached Send."""
    from send import _target, _draft
    from media_vision import composer_top
    row=store.db.execute('SELECT action,status,params_json,detail_json FROM gui_commands WHERE id=?',(delivery_id,)).fetchone()
    if not row or row[:2]!=('send_delivery','failed'):raise ValueError('DISCARD_SOURCE_INVALID')
    item=json.loads(row[2]);proof=json.loads(row[3]);diag=proof.get('diagnostic',{})
    if (proof.get('reason') not in {'MEDIA_EXACT_PREVIEW_NOT_VERIFIED','HTTP Error 500: Internal Server Error'} or diag.get('send_click_attempted') is not False
            or store.canonical(item.get('conversation_id',''))!=conversation or item.get('type')!='video'
            or diag.get('import',{}).get('file_sha256')!=item.get('sha256')):
        raise ValueError('DISCARD_SOURCE_NOT_PROVEN')
    source=Path(item['path']).resolve()
    if source.parent!=(Path(root)/'data/media-cache'/delivery_id).resolve() or hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('DISCARD_ASSET_CHANGED')
    frames=[v for v in diag.get('frames',[]) if v.get('stage')=='prepared']
    if len(frames)!=1:raise ValueError('DISCARD_PREVIEW_MISSING')
    saved=np.asarray(Image.open(frames[0]['path']).convert('RGB'))[:,:,::-1].copy()
    if hashlib.sha256(saved.tobytes()).hexdigest()!=frames[0]['sha256']:raise ValueError('DISCARD_EVIDENCE_CHANGED')
    top=composer_top(saved)
    if top is None:raise ValueError('DISCARD_GEOMETRY_INVALID')
    box=[504,top+12,226,60];x,y,w,h=box
    current,_=runtime.capture(label+'-discard-before')
    if (not _target(runtime,current,target,enforce_row_y=False)['verified']
            or not exclusive_preview(saved,box) or not exclusive_preview(current,box)
            or not np.array_equal(saved[y+2:y+h-2,x+2:x+w-2],current[y+2:y+h-2,x+2:x+w-2])
            or np.max(abs(saved[y:y+h,x:x+w].astype(np.int16)-current[y:y+h,x:x+w].astype(np.int16)))>1):
        raise ValueError('DISCARD_PREVIEW_CHANGED')
    runtime.node('FocusChatInput');runtime.node('OwnedDraftEnd');runtime.node('OwnedDraftBackspace')
    after,path=runtime.capture(label+'-discard-after')
    if not _target(runtime,after,target,enforce_row_y=False)['verified'] or not _draft(runtime,after)['verified']:raise ValueError('DISCARD_EMPTY_NOT_VERIFIED')
    return {'status':'reviewed','reason':'OWNED_UNSENT_MEDIA_PREVIEW_DISCARDED','frame':path,'delivery_id':delivery_id,'send_click_attempted':False}

def discard_probe_preview(runtime,target,item,label):
    from send import _target,_draft
    from media_vision import preview_box
    image,_=runtime.capture(label+'-discard-before')
    with Image.open(item['path']) as source:box=preview_box(image,source)
    if not _target(runtime,image,target,enforce_row_y=False)['verified'] or not box or not exclusive_preview(image,box):raise ValueError('PROBE_PREVIEW_NOT_EXCLUSIVELY_OWNED')
    runtime.node('FocusChatInput');runtime.node('OwnedDraftEnd');runtime.node('OwnedDraftBackspace')
    after,path=runtime.capture(label+'-discard-after')
    if not _target(runtime,after,target,enforce_row_y=False)['verified'] or not _draft(runtime,after)['verified']:raise ValueError('PROBE_PREVIEW_DISCARD_NOT_VERIFIED')
    return {'discarded':True,'frame':path}

def _send_media_once(runtime,root,label,target,item,*,persist=None,refresh_row_position=False):
    """At most one send click. Every missing postcondition is uncertain."""
    from send import _target,_draft,_payloads
    from media_vision import preview_box,media_payloads,sent_image_match,video_match
    diagnostic={'frames':[],'send_click_attempted':False,'draft_retained':False,'history_before':[],'after_payloads':[]};evidence={'title_verified':False,'outgoing_verified':False,'media_sha256':item.get('sha256')}
    def result(status,reason):return {'status':status,'reason':reason,'evidence':evidence,'diagnostic':diagnostic}
    def frame(stage):
        im,path=runtime.capture(label+'-media-'+stage)
        evidence['frame_sha256']=hashlib.sha256(im.tobytes()).hexdigest()
        diagnostic['frames'].append({'stage':stage,'path':path,'sha256':evidence['frame_sha256']})
        return im
    try:
        before=frame('before')
        verified_target=_target(runtime,before,target,enforce_row_y=not refresh_row_position)
        if not verified_target['verified']:return result('blocked','MEDIA_TARGET_NOT_VERIFIED')
        if refresh_row_position:
            diagnostic['row_position_refresh']={'previous':target.get('row_y'),'verified':verified_target['row_y']}
            target=dict(target,row_y=verified_target['row_y'])
        if not _draft(runtime,before)['verified']:return result('blocked','MEDIA_EXISTING_DRAFT')
        if owned_windows(runtime,label+'-initial'):return result('blocked','MEDIA_EXISTING_POPUP')
        video=item.get('type')=='video'
        if item.get('type') not in ('image','video'):return result('blocked','MEDIA_TYPE_INVALID')
        source=None
        if video:
            import av
            with av.open(item['path']) as container:
                if not container.streams.video:raise ValueError('MEDIA_VIDEO_NOT_DECODABLE')
                source=next(container.decode(container.streams.video[0])).to_image().convert('RGB')
        else:
            with Image.open(item['path']) as image:source=image.convert('RGB').copy()
        def history(im):
            name=re.sub(r'\s+','',target.get('name',''));title=re.sub(r'\s+','',target.get('conversation_title',''))
            from name_match import group_header
            group=group_header(name,title) is not None
            values=_payloads(runtime,im,group_chat=group)
            if group:
                from sender_ocr import refine_group_senders
                values=refine_group_senders(runtime,im,values)
            return values
        def matching(im,values):
            matches=[p for p in media_payloads(im,values) if p['direction']=='outgoing' and (video_match(im,p['visual']['box'],source) if video else sent_image_match(im,p['visual']['box'],source))]
            # The native picker can send MP4 as an attachment card. Its exact
            # cache filename and byte-length label are independently OCR checked.
            return matches + (video_card(runtime,im,item) if video else [])
        def preview(im):
            cards=video_card(runtime,im,item,preview=True) if video else []
            return cards[0]['visual']['box'] if video and len(cards)==1 else None if video else preview_box(im,source)
        diagnostic['history_before']=history(before)
        existing=len(matching(before,diagnostic['history_before']))
        diagnostic['matching_media_before']=existing
        ready=frame('preimport')
        if not _target(runtime,ready,target)['verified'] or not _draft(runtime,ready)['verified']:return result('blocked','MEDIA_TARGET_OR_DRAFT_CHANGED')
        diagnostic['draft_retained']=True
        runtime.node('OpenMediaPicker');diagnostic['import']=import_media(runtime,item,label)
        prepared=frame('prepared');box=preview(prepared)
        if not box or not exclusive_preview(prepared,box):return result('blocked','MEDIA_EXACT_PREVIEW_NOT_VERIFIED')
        final=frame('preclick')
        final_target=_target(runtime,final,target);final_box=preview(final)
        diagnostic['preclick_target']=final_target;diagnostic['prepared_box']=box;diagnostic['preclick_box']=final_box
        if not final_target['verified'] or not final_box or any(abs(a-b)>2 for a,b in zip(final_box,box)) or not exclusive_preview(final,final_box):return result('blocked','MEDIA_TARGET_OR_PREVIEW_CHANGED')
        if persist:persist({'status':'executing','phase':'send_click_started','media_sha256':item['sha256']})
        immediately=frame('confirmed-preclick');instant_box=preview(immediately)
        if not _target(runtime,immediately,target)['verified'] or not instant_box or not exclusive_preview(immediately,instant_box):return result('blocked','MEDIA_CHANGED_DURING_PERMISSION_CHECK')
        diagnostic['send_click_attempted']=True
        action=runtime.node('SendDraft',{'SendDraft':{'repeat':1,'next':[],'on_error':[]}},allow_miss=True)
        last_sha=None
        deadline=time.monotonic()+60
        for attempt in range(8):
            if time.monotonic()>deadline:break
            after=frame('after-'+str(attempt));verified=_target(runtime,after,target,enforce_row_y=False)['verified'];evidence['title_verified']=verified
            if not verified:return result('uncertain','MEDIA_TARGET_CHANGED_AFTER_SEND')
            if owned_windows(runtime,label+'-post-'+str(attempt)):return result('uncertain','MEDIA_POPUP_AFTER_SEND')
            if not _draft(runtime,after)['verified']:time.sleep(.3);continue
            after_payloads=history(after)
            diagnostic['after_payloads'].append({'stage':'after-'+str(attempt),'payloads':after_payloads})
            matches=matching(after,after_payloads)
            if len(matches)>existing:
                newest=max(matches,key=lambda p:p['visual']['box'][1]);x,y,w,h=newest['visual']['box']
                failure=after[y:y+h,max(490,x-40):x-3]
                if failure.size and ((failure[:,:,2]>180)&(failure[:,:,1]<100)&(failure[:,:,0]<100)).sum()>8:return result('uncertain','MEDIA_FAILURE_INDICATOR')
                if failure.size and (np.max(abs(failure.astype(np.int16)-250),axis=2)>30).sum()>8:
                    last_sha=None;time.sleep(.3);continue
                sha=newest['visual']['sha256']
                if sha==last_sha and action.get('succeeded') is True:
                    evidence['outgoing_verified']=True;diagnostic['outgoing']=newest
                    return result('sent','MEDIA_NEW_RIGHT_BUBBLE_AND_EMPTY_DRAFT_VERIFIED')
                last_sha=sha
            time.sleep(.3)
        return result('uncertain','MEDIA_SEND_POSTCONDITION_NOT_VERIFIED')
    except Exception as exc:return result('uncertain' if diagnostic['send_click_attempted'] else 'blocked',str(exc)[:160])


def send_media_verified(runtime,root,label,target,item,*,persist=None,refresh_row_position=False):
    from send import retryable_send,MAX_SEND_RETRIES
    from group_policy import allowed
    if not allowed(root,target,item.get('conversation_id')):
        return {'status':'blocked','reason':'GROUP_NOT_WHITELISTED','evidence':{},'diagnostic':{'send_click_attempted':False}}
    attempts=[]
    for attempt in range(MAX_SEND_RETRIES+1):
        options={'refresh_row_position':True} if refresh_row_position else {}
        result=_send_media_once(runtime,root,label,target,item,persist=persist,**options)
        attempts.append({'attempt':attempt+1,'status':result.get('status'),'reason':result.get('reason')})
        path=Path(root)/'data'
        if path.exists():
            with (path/'send-attempts.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({'event_id':item.get('id'),**attempts[-1]},ensure_ascii=False)+'\n');stream.flush()
        if not retryable_send(result) or attempt==MAX_SEND_RETRIES:break
        time.sleep(3)
    result.setdefault('diagnostic',{})['attempts']=attempts
    return result
