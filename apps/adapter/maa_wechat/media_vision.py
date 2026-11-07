"""Conservative opaque media anchors from complete avatar-aligned rectangles."""
import hashlib
import numpy as np
from PIL import Image

def composer_top(image):
    a=np.asarray(image)
    if a.shape!=(1040,1920,3):return None
    strip=a[600:930,1000:1700].astype(np.int16)
    neutral=(strip.max(axis=2)-strip.min(axis=2)<=2)&(strip.min(axis=2)>=220)&(strip.max(axis=2)<=240)
    rows=np.flatnonzero(neutral.sum(axis=1)>=650)+600
    pairs=[int(y) for y in rows if y+1 in rows]
    return max(pairs) if pairs else None

def runs(values,gap=2):
    points=np.flatnonzero(values);result=[]
    for p in points:
        if result and p-result[-1][1]<=gap+1:result[-1][1]=int(p)
        else:result.append([int(p),int(p)])
    return result

def media_payloads(image,text_payloads):
    a=np.asarray(image);top=composer_top(a)
    if top is None:return []
    end=top-8;bg=np.median(a[100:500,490:493].reshape(-1,3),axis=0)
    result=[]
    for direction,left,right,avatar_x in [('incoming',545,1050,500),('outgoing',1260,1845,1858)]:
        band=a[82:end,left:right].astype(np.int16);mask=np.max(abs(band-bg),axis=2)>3
        for sy,ey in runs(mask.sum(axis=1)>=8):
            y=sy+82;h=ey-sy+1
            if not 64<=h<=450 or y<=82 or y+h>=end:continue
            for sx,ex in runs(mask[sy:ey+1].sum(axis=0)>=1):
                x=left+sx;w=ex-sx+1
                if not 64<=w<=500:continue
                # Complete outer alignment and an adjacent detailed avatar are
                # mandatory; partial/offscreen pictures never become anchors.
                if direction=='incoming' and abs(x-550)>8:continue
                if direction=='outgoing' and abs(x+w-1844)>3:continue
                avatar=a[max(82,y-25):min(end,y+36),avatar_x:avatar_x+36].astype(np.int16)
                if not avatar.size or (np.max(abs(avatar-bg),axis=2)>25).mean()<.18:continue
                overlapping=False
                for p in text_payloads:
                    if p.get('message_kind')!='text' or not (p.get('visual') or {}).get('verified'):continue
                    px,py,pw,ph=p['visual']['box']
                    overlap=max(0,min(x+w,px+pw)-max(x,px))*max(0,min(y+h,py+ph)-max(y,py))
                    if overlap>min(w*h,pw*ph)*.5:overlapping=True;break
                if overlapping:continue
                crop=np.ascontiguousarray(a[y:y+h,x:x+w])
                result.append({'text':'[图片/视频]','sender':'unresolved','source':'maa_media_rectangle',
                    'direction':direction,'message_kind':'media','reply_safe':False,
                    'group_sender_name':None,'group_sender_verified':False,
                    'visual':{'box':[x,y,w,h],'sha256':hashlib.sha256(crop.tobytes()).hexdigest(),'verified':True},
                    'ocr':{'stable':False,'raw_hits':[]}})
    return result

def image_match(image,box,source):
    x,y,w,h=box
    if min(w,h)<64 or abs(w/h-source.width/source.height)>.06:return False
    expected=np.asarray(source.convert('RGB').resize((w,h),Image.Resampling.LANCZOS)).astype(np.float32)
    actual=np.asarray(image)[y:y+h,x:x+w,::-1].astype(np.float32)
    # Ignore rounded corners, but compare all interior pixels and texture.
    expected=expected[4:-4,4:-4];actual=actual[4:-4,4:-4]
    if not expected.size:return False
    mae=float(np.abs(expected-actual).mean())
    if mae>12:return False
    if expected.std()>8:
        corr=np.corrcoef(expected.ravel(),actual.ravel())[0,1]
        return bool(np.isfinite(corr) and corr>.94)
    return mae<3

def preview_box(image,source):
    top=composer_top(image)
    if top is None or top>=896:return None
    width=min(200,round(200*source.width/source.height));height=min(200,round(200*source.height/source.width))
    for x in range(502,508):
        for y in range(top+8,top+16):
            for w in range(width-1,width+2):
                for h in range(height-1,height+2):
                    box=[x,y,w,h]
                    if y+h<990 and image_match(image,box,source):return box
    return None


def sent_image_match(image, box, source):
    """Account for a one-pixel horizontal inset in a rounded sent thumbnail.

    The rectangle detector sees the opaque interior, whereas WeChat resizes the
    source to the full thumbnail. Keep the same pixel/correlation thresholds;
    this adjustment applies only to avatar-aligned outgoing history anchors.
    """
    if image_match(image, box, source):return True
    x,y,w,h=box
    if abs(x+w-1844)>3 or x<1260 or y<=82:return False
    return image_match(image, [x-1,y,w+2,h], source)


def video_match(image,box,source):
    x,y,w,h=box
    if min(w,h)<64 or abs(w/h-source.width/source.height)>.06:return False
    expected=np.asarray(source.convert('RGB').resize((w,h),Image.Resampling.LANCZOS)).astype(np.float32)
    actual=np.asarray(image)[y:y+h,x:x+w,::-1].astype(np.float32)
    if expected.shape!=actual.shape:return False
    yy,xx=np.mgrid[:h,:w];mask=((xx-w/2)**2+(yy-h/2)**2>26**2)
    mask[:4]=False;mask[-4:]=False;mask[:,:4]=False;mask[:,-4:]=False
    mask[max(0,h-25):,max(0,w-45):]=False
    if mask.mean()<.60:return False
    a,b=expected[mask],actual[mask]
    if np.abs(a-b).mean()>12 or a.std()<8:return False
    corr=np.corrcoef(a.ravel(),b.ravel())[0,1]
    # Require the observed central white play control as well as matching content.
    center=actual[max(0,h//2-22):h//2+23,max(0,w//2-22):w//2+23]
    white=(center.min(axis=2)>235).sum()
    return bool(np.isfinite(corr) and corr>.92 and white>60)
