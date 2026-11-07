"""Inspect missing avatar-only rows in saved screenshots, never touch UI."""
import json
from pathlib import Path
import numpy as np
from PIL import Image
from list_rows import avatar_centers
import argparse
parser=argparse.ArgumentParser()
parser.add_argument('--prefix',default='maa-full-contacts-v4-sep26')
args=parser.parse_args()

root=Path(__file__).resolve().parent
report=[]
for path in sorted((root/'data').glob(args.prefix+'-contacts-page-*.png')):
    image=np.array(Image.open(path).convert('RGB'))[:,:,::-1].copy()
    centers=avatar_centers(image,'contacts')
    gaps=[]
    for a,b in zip(centers,centers[1:]):
        if 100<=b-a<=300: gaps.append([a,b])
    if gaps: report.append({'page':path.stem.split('-')[-1],'centers':centers,'gaps':gaps})
print(json.dumps(report))
