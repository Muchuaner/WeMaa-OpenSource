"""Ordered overlap on Maa row evidence; names/photos alone are not entity IDs."""
import re
import unicodedata
import numpy as np

def name_key(value):
    normalized=unicodedata.normalize('NFKC',value)
    words=re.sub(r'\W+','',normalized)
    return words or re.sub(r'\s+','',normalized)

def normalized_photo(photo,background):
    """Transparent avatars inherit gray/selected-green row backgrounds."""
    result=photo.copy()
    mask=(np.abs(photo.astype(np.int16)-np.asarray(background,dtype=np.int16))<=6).all(axis=2)
    result[mask]=127
    return result

def photo_match(a,b):
    if a.shape!=(24,24,3) or b.shape!=(24,24,3): return False
    anchor=a[4:20,2:22].astype(np.float32)
    # Edge background/rounded corners may vary while the opaque core is exact.
    core=a[8:16,2:22].astype(np.float32)
    if core.std()>20:
        for dy in range(-4,5):
            if float(np.abs(core-b[8+dy:16+dy,2:22].astype(np.float32)).mean())<=1: return True
    for dy in range(-4,5):
        for dx in range(-1,2):
            sample=b[4+dy:20+dy,2+dx:22+dx].astype(np.float32)
            difference=float(np.abs(anchor-sample).mean())
            if difference<=1: return True
            if difference>8: continue
            aa=anchor.ravel()-anchor.mean()
            bb=sample.ravel()-sample.mean()
            denominator=float(np.linalg.norm(aa)*np.linalg.norm(bb))
            if denominator and float(aa@bb)/denominator>=.98: return True
    return False

def overlap(previous,current):
    if not previous: return 0
    matrix=[[photo_match(a['photo'],b['photo']) for b in current] for a in previous]
    row_counts=[sum(row) for row in matrix]
    col_counts=[sum(row[j] for row in matrix) for j in range(len(current))]
    # Repeated default/company photographs need the visible name too.
    for i,a in enumerate(previous):
        for j,b in enumerate(current):
            if matrix[i][j] and (row_counts[i]>1 or col_counts[j]>1):
                matrix[i][j]=bool(name_key(a['name'])) and name_key(a['name'])==name_key(b['name'])
    lengths=[]
    for length in range(1,min(len(previous),len(current))+1):
        misses=[k for k in range(length) if not matrix[len(previous)-length+k][k]]
        accepted=not misses
        if len(misses)==1:
            k=misses[0]
            i=len(previous)-length+k
            key=name_key(previous[i]['name'])
            unique=bool(key) and sum(name_key(r['name'])==key for r in previous)==1 and sum(name_key(r['name'])==key for r in current)==1
            other_key=name_key(current[k]['name'])
            if length>=8 and len(key)>=2 and len(key)==len(other_key) and sum(a!=b for a,b in zip(key,other_key))==1:
                unique=sum(name_key(r['name'])==key for r in previous)==1 and sum(name_key(r['name'])==other_key for r in current)==1
            interior=0<k<length-1 and matrix[i-1][k-1] and matrix[i+1][k+1]
            # A clipped edge avatar may change, but an exact unique name and
            # three consecutive neighboring photo anchors still locate it.
            edge=(k==0 and length>=4 and all(matrix[i+j][j] for j in range(1,4))) or (k==length-1 and length>=4 and all(matrix[i-j][k-j] for j in range(1,4)))
            accepted=unique and (interior or edge)
        if 1<len(misses)<=2 and length>=8 and length-len(misses)>=6:
            # Long ordered overlaps can contain both a selected transparent
            # avatar and a clipped edge avatar. Every missing photo needs an
            # unique name context; adjacent misses must be interior and flanked.
            accepted=all(b-a>1 for a,b in zip(misses,misses[1:])) or (misses[0]>=2 and misses[-1]<=length-3)
            fuzzy_count=0
            for k in misses:
                key=name_key(previous[len(previous)-length+k]['name'])
                other_key=name_key(current[k]['name'])
                fuzzy=len(key)>=2 and len(key)==len(other_key) and sum(a!=b for a,b in zip(key,other_key))==1
                fuzzy_count+=int(key!=other_key)
                accepted=accepted and bool(key) and (key==other_key or fuzzy) and sum(name_key(r['name'])==key for r in previous)==1 and sum(name_key(r['name'])==other_key for r in current)==1
            accepted=accepted and fuzzy_count<=1
        if accepted:
            lengths.append(length)
    if not lengths: raise ValueError('CONTACT_PAGE_NO_VERIFIED_OVERLAP')
    length=max(lengths)
    if length<2: raise ValueError('CONTACT_PAGE_OVERLAP_TOO_WEAK')
    anchors=sum(sum(matrix[len(previous)-length+k])==1 and sum(row[k] for row in matrix)==1 for k in range(length))
    if anchors<2: raise ValueError('CONTACT_PAGE_OVERLAP_AMBIGUOUS')
    return length

def stationary(previous,current):
    if not previous or len(previous)!=len(current): return False
    if any(abs(a['y']-b['y'])>2 for a,b in zip(previous,current)): return False
    matches=[(bool(name_key(a['name'])) and name_key(a['name'])==name_key(b['name'])) or photo_match(a['photo'],b['photo']) for a,b in zip(previous,current)]
    return all(matches) or (len(matches)>=4 and sum(matches)>=len(matches)-1)
