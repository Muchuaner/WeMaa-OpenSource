"""Measure actual scroll displacement using Maa screenshot photo anchors."""
import hashlib
import json
from pathlib import Path
import numpy as np
from PIL import Image
from list_rows import avatar_centers

root=Path(__file__).resolve().parent
paths=sorted((root/'data').glob('maa-full-contacts-v6-sep26-contacts-page-*.png'))
previous=None
report=[]
for path in paths:
    image=np.array(Image.open(path).convert('RGB'))[:,:,::-1].copy()
    centers=avatar_centers(image,'contacts')
    anchors={}
    for y in centers:
        for dy in range(-2,3):
            sha=hashlib.sha256(image[y-8+dy:y+8+dy,103:119,:3].tobytes()).hexdigest()
            anchors.setdefault(sha,[]).append(y+dy)
    if previous:
        old_path,old_centers,old_image=previous
        deltas=[]
        for y in old_centers:
            sha=hashlib.sha256(old_image[y-8:y+8,103:119,:3].tobytes()).hexdigest()
            matches=anchors.get(sha,[])
            if len(matches)==1: deltas.append(y-matches[0])
        report.append({'from':old_path.stem.split('-')[-1],'to':path.stem.split('-')[-1],'anchors':len(deltas),'delta_median':float(np.median(deltas)) if deltas else None})
    previous=path,centers,image
print(json.dumps(report))
