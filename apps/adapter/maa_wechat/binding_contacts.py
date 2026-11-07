"""Avatar -> verified profile -> friend/private route, owned by the sole Worker."""
import hashlib,json,re,time
import numpy as np
from PIL import Image


def button(hits,labels):
    candidates=[h for h in hits if h.get('text','').strip() in labels and h.get('score',0)>=.92
                and isinstance(h.get('box'),list) and len(h['box'])==4]
    if len(candidates)!=1:raise ValueError('BINDING_PROFILE_ACTION_NOT_UNIQUE')
    return candidates[0]


def click_button(runtime,popup,labels,label,*,expected_wx=None):
    from send import _ocr_scaled
    im,_=popup.capture(label+'-before')
    roi=[0,0,im.shape[1],im.shape[0]]
    a=runtime.recognize_image('Read',im,roi=roi);b=_ocr_scaled(runtime,im,'Read',roi,2)
    first,second=button(a,labels),button(b,labels)
    if first['text'].strip()!=second['text'].strip():raise ValueError('BINDING_PROFILE_ACTION_NOT_STABLE')
    if expected_wx:
        for hits in (a,b):
            ids=re.findall(r'微信号\s*[:：]\s*([A-Za-z0-9_-]{3,128})',' '.join(h['text'] for h in hits))
            if ids!=[expected_wx]:raise ValueError('BINDING_PROFILE_ID_CHANGED')
    current,_=popup.capture(label+'-preclick')
    if not np.array_equal(current,im):raise ValueError('BINDING_PROFILE_CHANGED')
    x,y,w,h=first['box']
    sx,sy,sw,sh=second['box']
    if abs((x+w/2)-(sx+sw/2))>6 or abs((y+h/2)-(sy+sh/2))>6:raise ValueError('BINDING_PROFILE_BUTTON_MOVED')
    popup.node('BindingProfileAction',{'BindingProfileAction':{'target':[x+w//2,y+h//2,1,1]}})


def contact_action(runtime,store,did,already_requested,card,profile,proof,label):
    from media_send import OwnedPopup,owned_windows
    popup=OwnedPopup(runtime,card)
    hits=runtime.recognize_image('Read',profile,roi=[0,0,profile.shape[1],profile.shape[0]])
    names={h.get('text','').strip() for h in hits if h.get('score',0)>=.92}
    if '发消息' in names:
        click_button(runtime,popup,{'发消息'},label,expected_wx=proof['wechat_id'])
        return {'navigated':True,'status':'private_opened'}
    if not names.intersection({'添加到通讯录','添加好友'}):raise ValueError('BINDING_PROFILE_CONTACT_STATE_UNKNOWN')
    if already_requested:return {'navigated':False,'status':'waiting_friend'}
    # Durable journal BEFORE either friend action; a crash never repeats it.
    store.gui_result(did,'executing',{'status':'friend_request_started','wechat_id':proof['wechat_id']})
    click_button(runtime,popup,{'添加到通讯录','添加好友'},label,expected_wx=proof['wechat_id'])
    dialogs=[]
    for attempt in range(8):
        dialogs=[w for w in owned_windows(runtime,label+'-friend-'+str(attempt)) if w['hwnd']!=card['hwnd']]
        if dialogs:break
        time.sleep(.25)
    if len(dialogs)!=1:raise ValueError('BINDING_FRIEND_DIALOG_NOT_UNIQUE')
    dialog=OwnedPopup(runtime,dialogs[0])
    click_button(runtime,dialog,{'发送','确定'},label+'-request')
    # Any remaining dialog is uncertain, never resend a request.
    time.sleep(.5)
    if any(w['hwnd']==dialogs[0]['hwnd'] for w in owned_windows(runtime,label+'-friend-after')):
        raise ValueError('BINDING_FRIEND_REQUEST_NOT_VERIFIED')
    store.event('binding_friend_request_sent',{'delivery_id':did,'wechat_id':proof['wechat_id']})
    return {'navigated':False,'status':'waiting_friend'}


def process_binding_contact(client,runtime,run_id):
    store=client.store;now=time.time()
    rows=store.db.execute("SELECT id,params_json,expires,status,updated FROM gui_commands WHERE action='send_delivery' AND status IN ('pending','waiting_friend') ORDER BY updated").fetchall()
    row=next((r for r in rows if json.loads(r[1]).get('binding_contact') and (r[3]=='pending' or now-r[4]>=60)),None)
    if not row:return False
    did,raw,expires,status,_=row;item=json.loads(raw);contact=item['binding_contact'];label=run_id+'-bind-'+did[:12]
    waiting=status=='waiting_friend';result={'status':'failed','reason':'BINDING_NOT_STARTED','evidence':{}}
    try:
        if now>expires:raise ValueError('BINDING_CONTACT_EXPIRED')
        if not waiting:
            store.gui_result(did,'executing',{'status':'claim_started'})
            if client.api('/creation/deliveries/'+did+'/claim',{},'POST').get('permit') is not True:raise ValueError('BINDING_CLAIM_NOT_GRANTED')
        else:store.gui_result(did,'executing',{'status':'friend_check_started'})
        from manual_gui import find_recent,observe
        from group_policy import allowed,is_group
        saved=store.db.execute('SELECT target_json FROM conversation_routes WHERE conversation_id=?',(contact['source_conversation_id'],)).fetchone()
        if not saved:raise ValueError('BINDING_SOURCE_ROUTE_MISSING')
        origin=json.loads(saved[0])
        if not allowed(client.root,origin,contact['source_conversation_id']):raise ValueError('GROUP_NOT_WHITELISTED')
        selected,page=find_recent(runtime,client.root,origin,label,expires)
        cid,target=observe(runtime,client.root,store,selected,label+'-origin',page)
        if cid!=store.canonical(contact['source_conversation_id']):raise ValueError('BINDING_ORIGIN_CHANGED')
        image,_=runtime.capture(label+'-source')
        from text_copy import read_messages
        from message_vision import message_identity
        payloads,_=read_messages(runtime,image,group_chat=is_group(target))
        candidates=[p for p in payloads if p.get('direction')=='incoming' and
                    hashlib.sha256(p.get('text','').encode()).hexdigest()==contact['message_text_sha256'] and
                    hashlib.sha256(str(message_identity(p)).encode()).hexdigest()==contact['proof']['message_identity']]
        if len(candidates)!=1:raise ValueError('BINDING_ORIGINAL_MESSAGE_NOT_UNIQUE')
        from sender_identity import verify_member
        proof=verify_member(runtime,image,label,target,candidates[0],expected_wx=contact['wechat_id'],
            on_profile=lambda card,profile,proof:contact_action(runtime,store,did,waiting,card,profile,proof,label))
        if proof['contact_action']['status']=='waiting_friend':
            store.gui_result(did,'waiting_friend',{'status':'waiting_friend','wechat_id':contact['wechat_id']})
            return True
        from watch import read_rows
        from flows import box,title_matches
        active,image,_=read_rows(runtime,client.root,label+'-private')
        selected=runtime.recognize_image('ContactSelectedRow',image)
        active=[r for r in active if any(box(h)[1]<=r['y']<box(h)[1]+box(h)[3] for h in selected)]
        if len(active)!=1:raise ValueError('BINDING_PRIVATE_SELECTION_NOT_VERIFIED')
        private_cid,private_target=observe(runtime,client.root,store,active[0],label+'-private-route',0)
        if is_group(private_target):raise ValueError('BINDING_PRIVATE_CHAT_REQUIRED')
        private_proof={k:proof[k] for k in ('wechat_id','display_name','profile_sha256','message_frame_sha256','message_identity','stable')}
        private_proof['message_frame_sha256']=hashlib.sha256(image.tobytes()).hexdigest()
        private_proof['message_identity']=hashlib.sha256(json.dumps([private_cid,proof['wechat_id'],proof['message_identity']]).encode()).hexdigest()
        link=client.api('/creation/deliveries/'+did+'/binding-link',{'conversation_id':private_cid,'conversation_type':'private','identity':private_proof},'POST')
        from send import send_verified
        def preflight():
            if client.api('/creation/deliveries/'+did+'/preflight').get('permit') is not True:raise ValueError('BINDING_LINK_REVOKED_BEFORE_SEND')
        result=send_verified(runtime,client.root,label,dict(private_target,reply_text=link['text'],event_id=did),preflight=preflight)
    except Exception as exc:
        current=store.db.execute('SELECT status FROM gui_commands WHERE id=?',(did,)).fetchone()[0]
        result={'status':'uncertain' if current=='executing' else 'failed','reason':str(exc)[:160],'evidence':{}}
    if result['status']=='blocked':result['status']='failed'
    store.gui_result(did,'succeeded' if result['status']=='sent' else 'failed',result)
    client.receipt(did,result);store.event('binding_contact_'+result['status'],{'delivery_id':did,'reason':result.get('reason')})
    return True
