"""Find repeated list photographs in captured contact pages, without UI input."""
import hashlib
import json
from pathlib import Path
from collections import defaultdict
import numpy as np
from PIL import Image
from list_rows import avatar_centers

root=Path(__file__).resolve().parent
report=[]
for path in sorted((root/'data').glob('maa-full-contacts-v6-sep26-contacts-page-*.png')):
    image=np.array(Image.open(path).convert('RGB'))[:,:,::-1].copy()
    groups=defaultdict(list)
    for y in avatar_centers(image,'contacts'):
        sha=hashlib.sha256(image[y-12:y+12,99:123,:3].tobytes()).hexdigest()
        groups[sha].append(y)
    duplicates=[ys for ys in groups.values() if len(ys)>1]
    if duplicates: report.append({'page':path.stem.split('-')[-1],'duplicated_photos':duplicates})
print(json.dumps(report))
