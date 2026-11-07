"""Maa custom recognition for avatar-backed rows, including emoji-only names."""
import json
import hashlib
import numpy as np
from maa.custom_recognition import CustomRecognition


def avatar_centers(image, kind):
    left,width=(96,30) if kind=='contacts' else (74,36)
    # Exclude the avatar's upper-right corner, where unread badges attach.
    right=left+width-4 if kind=='contacts' else left+width-12
    band=image[80:1030,left+2:right,:3].astype(np.int16)
    background=image[80:1030,65,:3].astype(np.int16)
    signal=np.abs(band-background[:,None,:]).max(axis=2).mean(axis=1)>(8 if kind=='contacts' else 14)
    indices=np.flatnonzero(signal)
    groups=[]
    for y in indices:
        if groups and int(y)-groups[-1][-1]<=3: groups[-1].append(int(y))
        else: groups.append([int(y)])
    centers=[(g[0]+g[-1]+1)//2+80 for g in groups
             if 24<=g[-1]-g[0]+1<=42 and g[0]>2 and g[-1]<948]
    if kind=='recent' and len(centers)>=3:
        # Recent cards have a verified fixed 65px pitch, even while scrolled.
        from collections import Counter
        phase=Counter(y%65 for y in centers).most_common(1)[0][0]
        centers=[y for y in range(90,1011) if y%65==phase and min(centers)-4<=y<=max(centers)+4]
    return centers


class ListRows(CustomRecognition):
    def analyze(self, context, argv):
        params=argv.custom_recognition_param
        if isinstance(params,str): params=json.loads(params or '{}')
        kind=(params or {}).get('kind','contacts')
        centers=avatar_centers(argv.image,kind)
        if not centers: return None
        return self.AnalyzeResult(box=(70,80,400,950),detail={'kind':kind,'centers':centers})


class VerifyRecentRow(CustomRecognition):
    def analyze(self,context,argv):
        params=argv.custom_recognition_param
        if isinstance(params,str): params=json.loads(params or '{}')
        y=int(params['y'])
        if not 90<=y<=1010: return None
        photo=argv.image[y-13:y+13,78:102,:3]
        if hashlib.sha256(photo.tobytes()).hexdigest()!=params['avatar_sha']: return None
        from recent_name import name_roi
        badge=context.run_recognition('ChatTitleBlueBadge',argv.image,{'ChatTitleBlueBadge':{'roi':[115,y-25,285,29]}})
        hits=[{'box':list(h.box)} for h in badge.filtered_results] if badge else []
        roi=name_roi(y,hits)
        detail=context.run_recognition('ReadRecentList',argv.image,{'ReadRecentList':{'roi':roi}} if roi else {})
        if not detail: return None
        names=[]
        for hit in detail.all_results:
            x,hy,w,h=hit.box
            if y-25<=hy+h/2<=y+4: names.append((x,hit.text))
        name=' '.join(text for _,text in sorted(names)).strip()
        from name_match import names_match
        if not names_match(name,params['name']): return None
        return self.AnalyzeResult(box=(115,y-25,285,30),detail={'name':name,'avatar_verified':True})
