"""Targeted nickname reads from the same verified chat frame, without guessing."""
from message_vision import TIME_PATTERN, consensus_text
from send import _ocr_scaled


def refine_group_senders(runtime,image,payloads,limit=2):
    result=list(payloads)
    unresolved=[i for i,p in enumerate(result) if p.get('direction')=='incoming'
                and p.get('message_kind')=='text' and p.get('ocr',{}).get('stable')
                and p.get('visual',{}).get('verified')][-limit:]
    for index in unresolved:
        original=result[index];x,y,w,h=original['visual']['box']
        # Nickname is above, aligned with the bubble; never read a profile from
        # another screen or substitute a remembered contact name.
        if x<535 or y<114 or y+h>887:continue
        roi=[max(490,x-5),max(82,y-32),min(365,1885-max(490,x-5)),30]
        votes=[];raw=[]
        for scale in (3,4):
            hits=_ocr_scaled(runtime,image,'Read',roi,scale)
            labels=[]
            for hit in hits:
                hx,hy,hw,hh=hit.get('box',[0,0,0,0])
                if (x-5<=hx<=x+30 and hx+hw<=x+360 and 0<hh<=24 and 1<=y-(hy+hh)<=24
                        and hit.get('text','').strip()
                        and not TIME_PATTERN.fullmatch(hit['text'].strip())):labels.append(hit)
            if not labels or max(v['box'][1] for v in labels)-min(v['box'][1] for v in labels)>6:continue
            labels.sort(key=lambda v:v['box'][0])
            votes.append({'text':' '.join(v['text'] for v in labels),
                          'score':min(float(v.get('score',0)) for v in labels),'pass_id':'nickname-crop-'+str(scale)})
            raw.extend(dict(v,pass_id='nickname-crop-'+str(scale)) for v in labels)
        sender=consensus_text(votes,min_score=.85,ignore_whitespace=True)
        if not sender['stable']:continue
        left=min(v['box'][0] for v in raw);top=min(v['box'][1] for v in raw)
        right=max(v['box'][0]+v['box'][2] for v in raw);bottom=max(v['box'][1]+v['box'][3] for v in raw)
        result[index]=dict(original,group_sender_name=sender['text'],group_sender_verified=True,
                           group_sender_ocr=dict(sender,source='same_frame_nickname_crop',
                                                 box=[left,top,right-left,bottom-top],raw_hits=raw,
                                                 previous=original.get('group_sender_ocr')),
                           reply_safe=bool(original.get('ocr',{}).get('stable') and original.get('visual',{}).get('verified')))
    return result
