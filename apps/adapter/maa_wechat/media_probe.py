"""Bounded visual teaching probes, never send a message."""
import argparse,json,time,traceback
from pathlib import Path
from runner import Runtime,ROOT,plain
from store import Store
from manual_gui import find_recent,observe,open_contact
from send import _target,_draft
def trial_cache(item):
 import shutil
 item=dict(item);item.setdefault('asset_id',115)
 source=Path(item['path']);folder=ROOT/'data'/'media-cache'/item['id'];folder.mkdir(exist_ok=True)
 destination=folder/(str(item['asset_id'])+source.suffix)
 if source.resolve()!=destination.resolve():shutil.copyfile(source,destination)
 item['path']=str(destination);return item

p=argparse.ArgumentParser();p.add_argument('--step',required=True);p.add_argument('--id',required=True);args=p.parse_args()
result={}
try:
 r=Runtime();r.run_id=args.id;s=Store(ROOT/'data'/'wechat.sqlite3')
 if args.step in ('group','member'):
  cid='e4a4fb079144b50ed88c2676b16190849cac1e91bd131f341523015946f3fc25'
  target=json.loads(s.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(cid,)).fetchone()[0])
  row,page=find_recent(r,ROOT,target,args.id,time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-group',page)
  if actual!=cid:raise ValueError('GROUP_ID_MISMATCH')
  result['target']=target
  if args.step=='member':
   from sender_identity import verify_member
   image,_=r.capture(args.id+'-member-before');result['identity']=verify_member(r,image,args.id,target,None,expected_wx='muchuan2b')
 elif args.step=='tom':
  contact={}
  cid,target=open_contact(r,ROOT,s,'wx:muchuan2b',args.id,time.time()+120,proof=contact);result.update(conversation_id=cid,target=target,contact=contact)
  from sender_identity import verify_member
  image,_=r.capture(args.id+'-private-before');result['private']=verify_member(r,image,args.id,target,None,expected_wx='muchuan2b')
 elif args.step=='attach':
  image,_=r.capture(args.id+'-attach-before');cid='e4a4fb079144b50ed88c2676b16190849cac1e91bd131f341523015946f3fc25'
  target=json.loads(s.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(cid,)).fetchone()[0])
  row,page=find_recent(r,ROOT,target,args.id+'-group',time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-group',page)
  image,_=r.capture(args.id+'-group-before');result['target']=_target(r,image,target,enforce_row_y=False)
  if not result['target']['verified']:raise ValueError('MEDIA_PROBE_TARGET_NOT_VERIFIED')
  if not _draft(r,image)['verified']:
   candidates=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
   if len(candidates)!=1:raise ValueError('MEDIA_PROBE_DRAFT_PRESENT_NO_HELPER_ROUTE')
   cid,target=candidates[0];row,page=find_recent(r,ROOT,target,args.id+'-helper',time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-helper',page)
   image,_=r.capture(args.id+'-helper-before')
   if actual!=cid or not _draft(r,image)['verified']:raise ValueError('MEDIA_PROBE_HELPER_NOT_SAFE')
   result['target']=target

  r.node('OpenMediaPicker');result['opened']=True
 elif args.step=='verifyhelper':
  from media_vision import media_payloads,image_match
  from PIL import Image
  import hashlib
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0];image,path=r.capture(args.id+'-verify')
  if not _target(r,image,target,enforce_row_y=False)['verified'] or not _draft(r,image)['verified']:raise ValueError('PROBE_HELPER_NOT_VERIFIED')
  with Image.open(ROOT/'data'/'media-cache'/'115.jpg') as source:matches=[m for m in media_payloads(image,[]) if m['direction']=='outgoing' and image_match(image,m['visual']['box'],source)]
  if len(matches)!=1:raise ValueError('PROBE_HELPER_MEDIA_NOT_UNIQUE')
  result['manual_evidence']={'conversation_id':cid,'title_verified':True,'outgoing_verified':True,'matching_media':matches,'frame_sha256':hashlib.sha256(image.tobytes()).hexdigest(),'frame':path}
  did='00000000000000730000000000000000'
  s.gui_result(did,'failed',{'status':'uncertain','reason':'TRIAL_INTERRUPTED_AFTER_CLICK_NO_RETRY','manual_evidence':result['manual_evidence']})
 elif args.step=='sendhelper':
  from media_send import send_media_verified
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0];row,page=find_recent(r,ROOT,target,args.id,time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-helper',page)
  if actual!=cid:raise ValueError('PROBE_HELPER_ID_MISMATCH')
  did='00000000000000730000000000000000'
  existing=s.db.execute('SELECT status,detail_json FROM gui_commands WHERE id=?',(did,)).fetchone()
  if existing:result['delivery']={'duplicate':True,'status':existing[0],'detail':json.loads(existing[1])}
  else:
   item={'id':did,'asset_id':115,'type':'image','path':str(ROOT/'data'/'media-cache'/'115.jpg'),'sha256':'618329ac6ad0b13460b5e42ae90b594f7a8c143de545d45153388feb36f01c28','mime':'image/jpeg','size':434931,'conversation_id':cid}
   s.queue_gui({'id':did,'action':'media_acceptance_trial','params':item,'expires_at':time.time()+120})
   s.gui_result(did,'executing',{'status':'executing','phase':'before_import'})
   final=send_media_verified(r,ROOT,args.id,target,trial_cache(item),persist=lambda value:s.gui_result(did,'executing',value))
   s.gui_result(did,'succeeded' if final['status']=='sent' else 'failed',final);s.event('media_acceptance_trial',{'delivery_id':did,'asset_id':115,'result':final})
   result['delivery']=final
 elif args.step=='discard':
  from media_send import discard_probe_preview
  choices=[json.loads(t) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  result['discard']=discard_probe_preview(r,choices[0],{'path':str(ROOT/'data'/'media-cache'/'115.jpg')},args.id)
 elif args.step=='cancel':
  from media_send import owned_windows,OwnedPopup
  choices=[w for w in owned_windows(r,args.id) if w['class']=='#32770' and w['title'] in ('打开文件','选择文件')]
  for w in sorted(choices,key=lambda w:w['shape'][0]):
   popup=OwnedPopup(r,w);popup.node('MediaDialogEscape')
  result['cancelled_dialogs']=len(choices)
 elif args.step=='import':
  from media_send import import_media
  sha='618329ac6ad0b13460b5e42ae90b594f7a8c143de545d45153388feb36f01c28'
  item={'id':'00000000000000730000000000000000','path':str(ROOT/'data'/'media-cache'/'115.jpg'),'sha256':sha,'mime':'image/jpeg','size':434931}
  result['import']=import_media(r,trial_cache(item),args.id)
 elif args.step=='verify123':
  from media_vision import media_payloads,image_match
  from send import _payloads
  from PIL import Image
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0];image,path=r.capture(args.id+'-verify')
  if not _target(r,image,target,enforce_row_y=False)['verified'] or not _draft(r,image)['verified']:raise ValueError('PROBE_HELPER_NOT_VERIFIED')
  with Image.open(ROOT/'data'/'media-cache'/'123.png') as source:matches=[p for p in media_payloads(image,_payloads(r,image)) if p['direction']=='outgoing' and image_match(image,p['visual']['box'],source)]
  if len(matches)!=1:raise ValueError('PROBE_IMAGE123_NOT_UNIQUE')
  result['manual_evidence']={'matching_media':matches,'frame':path,'title_verified':True,'outgoing_verified':True}
 elif args.step in ('sendpnghelper','sendjpeghelperfinal','sendvideohelperfinal'):
  from media_send import send_media_verified
  import hashlib
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0];jpeg=args.step=='sendjpeghelperfinal';video=args.step=='sendvideohelperfinal';aid=130 if video else 115 if jpeg else 123
  did='00000000000000820000000000000002' if video else '00000000000000730000000000000002' if jpeg else '000000000000007b0000000000000001'
  old=s.db.execute('SELECT status,detail_json FROM gui_commands WHERE id=?',(did,)).fetchone()
  if old:result['delivery']={'duplicate':True,'status':old[0],'detail':json.loads(old[1])}
  else:
   row,page=find_recent(r,ROOT,target,args.id,time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-helper',page)
   if actual!=cid:raise ValueError('PROBE_HELPER_ID_CHANGED')
   path=ROOT/'data'/'media-cache'/('130.mp4' if video else '115.jpg' if jpeg else '123.png');raw=path.read_bytes()
   item=trial_cache({'id':did,'asset_id':aid,'type':'video' if video else 'image','path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'mime':'video/mp4' if video else 'image/jpeg' if jpeg else 'image/png','size':len(raw),'conversation_id':cid})
   s.queue_gui({'id':did,'action':'media_acceptance_trial','params':item,'expires_at':time.time()+300});s.gui_result(did,'executing',{'status':'executing','phase':'before_import'})
   final=send_media_verified(r,ROOT,args.id,target,item,persist=lambda value:s.gui_result(did,'executing',value))
   s.gui_result(did,'succeeded' if final['status']=='sent' else 'failed',final);s.event('media_acceptance_trial',{'delivery_id':did,'asset_id':aid,'result':final});result['delivery']=final
 elif args.step=='playvideo':
  from media_vision import media_payloads,video_match
  from media_send import owned_windows
  import av,hashlib
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0];image,path=r.capture(args.id+'-before')
  if not _target(r,image,target,enforce_row_y=False)['verified'] or not _draft(r,image)['verified']:raise ValueError('PROBE_HELPER_NOT_VERIFIED')
  with av.open(str(ROOT/'data'/'media-cache'/'130.mp4')) as container:source=next(container.decode(container.streams.video[0])).to_image()
  matches=[p for p in media_payloads(image,[]) if p['direction']=='outgoing' and video_match(image,p['visual']['box'],source)]
  if len(matches)!=1:raise ValueError('PROBE_SENT_VIDEO_NOT_UNIQUE')
  result['bubble_evidence']={'matching_media':matches,'title_verified':True,'frame':path,'frame_sha256':hashlib.sha256(image.tobytes()).hexdigest()}
  x,y,w,h=matches[0]['visual']['box'];r.node('OpenSentVideo',{'OpenSentVideo':{'target':[x+w//2,y+h//2,1,1]}})
  result['playback_frames']=[]
  for i in range(3):
   result['playback_frames'].append(owned_windows(r,args.id+'-play-'+str(i)));time.sleep(.8)
 elif args.step in ('sendvideohelper','sendvideohelper2'):

  from media_send import send_media_verified,video_card,exclusive_preview
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0]
  did='0000000000000082000000000000000'+('1' if args.step=='sendvideohelper2' else '0')
  old=s.db.execute('SELECT status,detail_json FROM gui_commands WHERE id=?',(did,)).fetchone()
  if old:result['delivery']={'duplicate':True,'status':old[0],'detail':json.loads(old[1])}
  else:
   item={'id':did,'type':'video','asset_id':130,'path':str(ROOT/'data'/'media-cache'/'130.mp4'),'mime':'video/mp4','size':10427271,'sha256':'876bd84516f2cfed7bcca17c1840b628f2f82e1b2df4dc04179af23abed8619e','conversation_id':cid}
   image,_=r.capture(args.id+'-owned-preview');cards=video_card(r,image,item,preview=True)
   if not _target(r,image,target,enforce_row_y=False)['verified'] or len(cards)!=1 or not exclusive_preview(image,cards[0]['visual']['box']):raise ValueError('PROBE_OWNED_VIDEO_PREVIEW_NOT_VERIFIED')
   r.node('FocusChatInput');r.node('OwnedDraftEnd');r.node('OwnedDraftBackspace')
   image,_=r.capture(args.id+'-empty')
   if not _draft(r,image)['verified']:raise ValueError('PROBE_VIDEO_DRAFT_NOT_EMPTY')
   row,page=find_recent(r,ROOT,target,args.id,time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-helper',page)
   if actual!=cid:raise ValueError('PROBE_HELPER_ID_CHANGED')
   s.queue_gui({'id':did,'action':'media_acceptance_trial','params':item,'expires_at':time.time()+300});s.gui_result(did,'executing',{'status':'executing','phase':'before_import'})
   final=send_media_verified(r,ROOT,args.id,target,trial_cache(item),persist=lambda value:s.gui_result(did,'executing',value))
   s.gui_result(did,'succeeded' if final['status']=='sent' else 'failed',final);s.event('media_acceptance_trial',{'delivery_id':did,'asset_id':130,'result':final});result['delivery']=final
 elif args.step=='checkvideo':
  from media_send import video_card,exclusive_preview
  item={'path':str(ROOT/'data'/'media-cache'/'130.mp4'),'size':10427271}
  image,_=r.capture(args.id+'-check');result['cards']=video_card(r,image,item,preview=True)
  result['ocr']=r.recognize_image('Read',image,roi=[500,904,300,86])
  result['exclusive']=len(result['cards'])==1 and exclusive_preview(image,result['cards'][0]['visual']['box'])
 elif args.step=='previewvideo':
  from media_send import import_media
  choices=[(c,json.loads(t)) for c,t in s.db.execute('SELECT conversation_id,target_json FROM conversation_routes') if json.loads(t).get('name')=='文件传输助手']
  if len(choices)!=1:raise ValueError('PROBE_HELPER_ROUTE_NOT_UNIQUE')
  cid,target=choices[0];row,page=find_recent(r,ROOT,target,args.id,time.time()+120);actual,target=observe(r,ROOT,s,row,args.id+'-helper',page)
  image,_=r.capture(args.id+'-before')
  if actual!=cid or not _draft(r,image)['verified']:raise ValueError('PROBE_HELPER_NOT_EMPTY')
  item={'id':'00000000000000820000000000000000','path':str(ROOT/'data'/'media-cache'/'130.mp4'),'mime':'video/mp4','size':10427271,'sha256':'876bd84516f2cfed7bcca17c1840b628f2f82e1b2df4dc04179af23abed8619e'}
  import av
  with av.open(item['path']) as container:
   stream=container.streams.video[0];source=next(container.decode(stream)).to_image();source.save(ROOT/'data'/(args.id+'-source.png'))
   result['video']={'duration':float(stream.duration*stream.time_base),'size':[source.width,source.height]}
  r.node('OpenMediaPicker');result['import']=import_media(r,trial_cache(item),args.id)
 elif args.step=='closeplayer':
  from media_send import owned_windows,VerifiedMediaViewer
  rows=[w for w in owned_windows(r,args.id) if w['title']=='图片和视频' and w['class']=='Qt51514QWindowIcon' and w['shape']==[761,1380,3]]
  if len(rows)!=1:raise ValueError('PROBE_VIDEO_PLAYER_NOT_UNIQUE')
  VerifiedMediaViewer(r,rows[0]).node('MediaViewerClose');result['closed']=True
 elif args.step=='verifyplatform':
  from media_send import owned_windows
  from send import _ocr_scaled
  from PIL import Image
  import numpy as np,re,hashlib
  rows=[w for w in owned_windows(r,args.id) if w['class']=='Qt51514QWindowToolSaveBits' and w['shape']==[405,328,3]]
  if len(rows)!=1:raise ValueError('PLATFORM_PROFILE_NOT_UNIQUE')
  im=np.asarray(Image.open(rows[0]['path']).convert('RGB'))[:,:,::-1].copy();roi=[0,0,328,405]
  values=[r.recognize_image('Read',im,roi=roi),_ocr_scaled(r,im,'Read',roi,2)]
  ids=[re.findall(r'微信号\s*[:：]\s*([A-Za-z0-9_-]{3,128})',' '.join(h['text'] for h in hits)) for hits in values]
  if len(ids[0])!=1 or ids[0]!=ids[1]:raise ValueError('PLATFORM_WECHAT_ID_NOT_STABLE')
  result['platform']={'wechat_id':ids[0][0],'stable':True,'profile_sha256':hashlib.sha256(im.tobytes()).hexdigest(),'frame':rows[0]['path']}
  r.node('DismissProfile')
 elif args.step=='platform':
  r.node('OpenOwnProfile');result['opened']=True
 if args.step=='inspect':
  from maa.toolkit import Toolkit
  import ctypes
  from ctypes import wintypes
  result['desktop']=[]
  for w in Toolkit.find_desktop_windows():
   rr=wintypes.RECT();pid=wintypes.DWORD();ctypes.windll.user32.GetClientRect(w.hwnd,ctypes.byref(rr));ctypes.windll.user32.GetWindowThreadProcessId(w.hwnd,ctypes.byref(pid))
   result['desktop'].append({'title':w.window_name,'class':w.class_name,'hwnd':w.hwnd,'size':[rr.right,rr.bottom],'pid':pid.value,'owner':ctypes.windll.user32.GetWindow(w.hwnd,4)})
  for row in s.db.execute('SELECT payloads_json FROM manual_views WHERE conversation_id=?',('e4a4fb079144b50ed88c2676b16190849cac1e91bd131f341523015946f3fc25',)):
   result['payloads']=[{k:p.get(k) for k in ('visual','group_sender_name','group_sender_verified')} for p in json.loads(row[0])[-3:]]
 image,path=r.capture(args.id+'-after');result['frame']=path
 # Window discovery/capture is scoped to this WeChat process, no input sent here.
 from media_send import owned_windows
 result['windows']=owned_windows(r,args.id)
except Exception as exc:
 result.update(error=str(exc),traceback=traceback.format_exc())
 from maa.toolkit import Toolkit
 import ctypes
 from ctypes import wintypes
 rows=[]
 for window in Toolkit.find_desktop_windows():
  rect=wintypes.RECT();ctypes.windll.user32.GetClientRect(window.hwnd,ctypes.byref(rect))
  rows.append({'title':window.window_name,'class':window.class_name,'hwnd':window.hwnd,'width':rect.right,'height':rect.bottom})
 result['desktop']=rows

(ROOT/'data'/(args.id+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
