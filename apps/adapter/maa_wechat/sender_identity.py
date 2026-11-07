"""Bind a specific message avatar to its visible profile WeChat ID."""
import hashlib,re,time
import numpy as np
from PIL import Image
from send import _target,_ocr_scaled
from message_vision import message_identity,extract_message_payloads
from watch import consensus,is_group_title


def verify_member(runtime,image,label,target,payload,expected_wx=None,on_profile=None):
    if not _target(runtime,image,target,enforce_row_y=False)['verified']:
        raise ValueError('SENDER_CHAT_NOT_VERIFIED')
    group=is_group_title(target,target['conversation_title'])
    if payload is None:
        from text_copy import read_messages
        all_payloads,_=read_messages(runtime,image,group_chat=group)
        candidates=[p for p in all_payloads if p.get('direction')=='incoming' and
                    (p.get('group_sender_verified') if group else p.get('reply_safe'))]
        if not candidates:raise ValueError('SENDER_MESSAGE_NOT_VISIBLE')
        payload=candidates[-1]
    visual=payload.get('visual') or {}
    avatar=visual.get('avatar_box')
    token=message_identity(payload)
    if (not token or visual.get('avatar_verified') is not True or not isinstance(avatar,list)
            or len(avatar)!=4 or any(type(v) is not int for v in avatar)):
        raise ValueError('MESSAGE_AVATAR_NOT_VERIFIED')
    x,y,w,h=visual['box'];ax,ay,aw,ah=avatar
    if not (490<=ax and 82<=ay and 20<=aw<=64 and 20<=ah<=64 and ax+aw<=1895 and ay+ah<=887):
        raise ValueError('MESSAGE_AVATAR_NOT_VISIBLE')
    avatar_sha=hashlib.sha256(np.ascontiguousarray(image[ay:ay+ah,ax:ax+aw,:3][:,:,::-1]).tobytes()).hexdigest()
    if avatar_sha!=visual.get('avatar_sha256'):raise ValueError('MESSAGE_AVATAR_HASH_MISMATCH')
    before,path=runtime.capture(label+'-identity-anchor')
    regions=[(x,y,w,h),(ax,ay,aw,ah)]
    if any(not np.array_equal(before[ry:ry+rh,rx:rx+rw],image[ry:ry+rh,rx:rx+rw]) for rx,ry,rw,rh in regions):
        raise ValueError('MESSAGE_MOVED_BEFORE_PROFILE')
    from media_send import owned_windows
    if owned_windows(runtime,label+'-existing-profile'):
        raise ValueError('SENDER_EXISTING_POPUP')
    # Even a failed action can have opened a card. Observe the postcondition
    # once, never retry the click or act through an unobserved leftover popup.
    try:
        action=runtime.node('OpenMessageProfile',{'OpenMessageProfile':{'target':[ax+aw//2,ay+ah//2,1,1]}},allow_miss=True)
    except Exception as exc:
        raise ValueError('SENDER_PROFILE_WINDOW_NOT_UNIQUE') from exc
    try:
        cards=[]
        for attempt in range(12):
            cards=[v for v in owned_windows(runtime,label+'-profile-'+str(attempt))
                   if v['class']=='Qt51514QWindowToolSaveBits' and 250<=v['shape'][1]<=600 and 250<=v['shape'][0]<=850]
            if cards:break
            time.sleep(.25)
        if len(cards)>1:
            raise ValueError('SENDER_PROFILE_WINDOW_NOT_UNIQUE')
        if not cards:
            if owned_windows(runtime,label+'-unexpected-profile'):
                raise ValueError('SENDER_PROFILE_WINDOW_NOT_UNIQUE')
            raise ValueError('SENDER_PROFILE_CLICK_FAILED' if not action.get('succeeded') else 'SENDER_PROFILE_WINDOW_NOT_UNIQUE')
    except Exception as exc:
        if str(exc) in ('SENDER_PROFILE_CLICK_FAILED','SENDER_PROFILE_WINDOW_NOT_UNIQUE'):raise
        raise ValueError('SENDER_PROFILE_WINDOW_NOT_UNIQUE') from exc
    navigated=False
    try:
        card=cards[0]
        profile=np.asarray(Image.open(card['path']).convert('RGB'))[:,:,::-1].copy()
        roi=[0,0,profile.shape[1],profile.shape[0]]
        first=runtime.recognize_image('Read',profile,roi=roi)
        second=_ocr_scaled(runtime,profile,'Read',roi,2)
        def wxid(hits):
            text=' '.join(v['text'] for v in hits)
            ids=re.findall(r'微信号\s*[:：]\s*([A-Za-z0-9_-]{3,128})',text)
            return ids[0] if len(ids)==1 else None
        a,b=wxid(first),wxid(second)
        if not a or a!=b or (expected_wx and a!=expected_wx):
            raise ValueError('SENDER_PROFILE_ID_NOT_STABLE')
        display_name=payload.get('group_sender_name') or payload.get('sender_name')
        if payload.get('direction')=='outgoing':
            from message_vision import consensus_text
            votes=[]
            for pass_id,hits in enumerate((first,second)):
                labels=[v for v in hits if isinstance(v.get('box'),list) and len(v['box'])==4
                        and v['box'][0]>=105 and 32<=v['box'][1]<=72
                        and not re.search(r'微信号|地区|昵称|备注',v.get('text',''))]
                if len(labels)==1:votes.append({'text':labels[0]['text'],'score':labels[0].get('score',0),'pass_id':pass_id})
            name=consensus_text(votes,min_score=.85,ignore_whitespace=True)
            if name['stable']:display_name=name['text']
        proof={'wechat_id':a,'display_name':display_name or
               ('本机账号' if payload.get('direction')=='outgoing' else target['name']),
               'profile_sha256':hashlib.sha256(profile.tobytes()).hexdigest(),
               'profile_path':card['path'],'message_frame_sha256':hashlib.sha256(before.tobytes()).hexdigest(),
               'message_identity':hashlib.sha256(str(token).encode()).hexdigest(),
               'message_avatar_sha256':avatar_sha,'message_avatar_box':avatar,'stable':True}
        if on_profile is not None:
            outcome=on_profile(card,profile,proof)
            navigated=outcome.get('navigated') is True
            proof['contact_action']=outcome
    finally:
        if not navigated:
            try:
                try:runtime.node('DismissProfile')
                except Exception as exc:raise ValueError('SENDER_PROFILE_NOT_DISMISSED') from exc
                if owned_windows(runtime,label+'-profile-dismissed'):
                    raise ValueError('SENDER_PROFILE_NOT_DISMISSED')
                after,_=runtime.capture(label+'-identity-restored')
                if not _target(runtime,after,target,enforce_row_y=False)['verified']:
                    raise ValueError('SENDER_CHAT_NOT_RESTORED')
                if any(not np.array_equal(after[ry:ry+rh,rx:rx+rw],before[ry:ry+rh,rx:rx+rw]) for rx,ry,rw,rh in regions):
                    raise ValueError('MESSAGE_MOVED_DURING_PROFILE_CHECK')
            except Exception as exc:
                if str(exc) in ('SENDER_PROFILE_NOT_DISMISSED','SENDER_CHAT_NOT_RESTORED','MESSAGE_MOVED_DURING_PROFILE_CHECK'):
                    raise
                raise ValueError('SENDER_PROFILE_NOT_RESTORED') from exc
    return proof
