import argparse,json
from pathlib import Path
import numpy as np
from PIL import Image
from list_rows import avatar_centers
parser=argparse.ArgumentParser()
parser.add_argument('a');parser.add_argument('b')
args=parser.parse_args()
a=np.array(Image.open(args.a).convert('RGB'))[:,:,::-1]
b=np.array(Image.open(args.b).convert('RGB'))[:,:,::-1]
from contact_alignment import normalized_photo
report=[]
for ya,yb in [(806,411),(860,466),(968,573)]:
    aa=normalized_photo(a[ya-12:ya+12,99:123],a[ya,65]).astype(float)
    bb=normalized_photo(b[yb-12:yb+12,99:123],b[yb,65]).astype(float)
    costs=[]
    for dy in range(-8,9):
        for dx in range(-1,2):
            u=aa[8:16,2:22];v=bb[8+dy:16+dy,2+dx:22+dx]
            costs.append((float(abs(u-v).mean()),dy,dx))
    report.append({'ys':[ya,yb],'best':sorted(costs)[:3]})
print(json.dumps(report))
