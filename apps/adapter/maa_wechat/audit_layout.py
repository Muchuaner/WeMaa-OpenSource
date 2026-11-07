import argparse
import json
import numpy as np
from PIL import Image
from layout import check_frame
parser=argparse.ArgumentParser()
parser.add_argument('image')
args=parser.parse_args()
image=np.array(Image.open(args.image).convert('RGB'))[:,:,::-1].copy()
try: status=check_frame(image)
except Exception as exc: status=str(exc)
ys=np.arange(140,1000,17)
report={'guard':status,'median_columns':{str(x):np.median(image[ys,x,:3],axis=0).tolist() for x in range(470,491)}}
print(json.dumps(report))
